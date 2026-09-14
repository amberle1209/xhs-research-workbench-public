from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

import xhs_workbench.collector as collector_module
from xhs_workbench.collector import VisibleCollector
from xhs_workbench.models import (
    AccountRecord,
    CandidateAttempt,
    MetricValue,
    NoteMediaSlot,
    NoteMetrics,
    PacingSummary,
    RunStatus,
    TimeEvidence,
)
from xhs_workbench.xhs_bridge import (
    AccountPayload,
    BridgeNote,
    BridgeRequest,
    BridgeResponse,
    CoverCandidate,
    SearchPayload,
    StagedMediaCandidate,
    validate_bridge_response,
)

JPEG = b"\xff\xd8\xfffixture-image"
PNG = b"\x89PNG\r\n\x1a\nfixture-image"
WEBP = b"RIFF\x08\x00\x00\x00WEBPfixture"
MP4 = (
    b"\x00\x00\x00\x10ftypisom\x00\x00\x00\x00"
    b"\x00\x00\x00\x08moov"
    b"\x00\x00\x00\x08mdat"
)


def _candidate(
    name: str,
    body: bytes = JPEG,
    *,
    mime_type: str = "image/jpeg",
    sha256: str | None = None,
) -> CoverCandidate:
    return CoverCandidate(
        staging_name=name,
        mime_type=mime_type,
        size_bytes=len(body),
        sha256=sha256 or hashlib.sha256(body).hexdigest(),
    )


def _note(
    note_id: str,
    position: int,
    cover: CoverCandidate | None = None,
    **overrides: object,
) -> BridgeNote:
    values: dict[str, object] = {
        "note_id": note_id,
        "canonical_url": f"https://www.xiaohongshu.com/explore/{note_id}",
        "title": "Visible title",
        "body": "Visible body",
        "tags": ["AI"],
        "note_type": "normal",
        "metrics": {},
        "source_position": position,
        "media_manifest_version": 2,
        "media_discovered_count": 0,
        "cover": cover,
    }
    values.update(overrides)
    return BridgeNote(**values)


def _account_field_statuses(
    account: AccountRecord, avatar: CoverCandidate | None
) -> dict[str, str]:
    return {
        "name": "exposed" if account.name is not None else "not_exposed",
        "bio": (
            "exposed_empty"
            if account.bio == ""
            else "exposed"
            if account.bio is not None
            else "not_exposed"
        ),
        "note_count": "exposed" if account.note_count is not None else "not_exposed",
        "follower_count": "exposed" if account.follower_count is not None else "not_exposed",
        "avatar": "exposed" if avatar is not None else "not_exposed",
        "platform_metrics": "exposed" if account.platform_metrics else "not_exposed",
    }


def _strict_account_payload(
    account: AccountRecord,
    notes: list[BridgeNote],
    avatar: CoverCandidate | None = None,
    *,
    requested_count: int | None = None,
    candidate_attempts: list[CandidateAttempt] | None = None,
    field_statuses: dict[str, str] | None = None,
    pacing_summary: PacingSummary | None = None,
) -> AccountPayload:
    strict_account = account.model_copy(
        update={"field_statuses": field_statuses or _account_field_statuses(account, avatar)}
    )
    strict_requested_count = requested_count or max(len(notes), 1)
    attempts = candidate_attempts or (
        [
            CandidateAttempt(
                position=note.source_position,
                note_id=note.note_id,
                outcome="complete",
                stage="detail_projection",
            )
            for note in notes
        ]
        if notes
        else [
            CandidateAttempt(
                position=1,
                outcome="unavailable",
                stage="candidate",
                reason="candidate_shortfall",
            )
        ]
    )
    return AccountPayload(
        account=strict_account,
        notes=notes,
        requested_count=strict_requested_count,
        candidate_attempts=attempts,
        pacing_summary=pacing_summary
        or PacingSummary(
            policy="conservative_jitter_v1",
            profile_open_delay_ms=2_000,
            detail_delay_ms=[3_000] * len(notes),
        ),
        avatar=avatar,
    )


def _media_candidate(name: str, position: int, body: bytes = JPEG) -> StagedMediaCandidate:
    return StagedMediaCandidate(
        note_id="note_a",
        role="image",
        position=position,
        status="downloaded",
        staging_name=name,
        mime_type="image/jpeg",
        size_bytes=len(body),
        sha256=hashlib.sha256(body).hexdigest(),
    )


def _typed_media_candidate(note_id: str, body: bytes, mime_type: str) -> StagedMediaCandidate:
    extension = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}[mime_type]
    return StagedMediaCandidate(
        note_id=note_id, role="image", position=1, status="downloaded",
        staging_name=f"{note_id}-image-001{extension}", mime_type=mime_type,
        size_bytes=len(body), sha256=hashlib.sha256(body).hexdigest(),
    )


def _media_note(
    candidates: list[StagedMediaCandidate], *, note_id: str = "note_a", source_position: int = 1
) -> BridgeNote:
    discovered = len([item for item in candidates if item.role == "image"])
    return _note(
        note_id,
        source_position,
        media_manifest_version=2,
        media_candidates=candidates,
        media_discovered_count=discovered,
        media_discovery_truncated=False,
        cover=None,
    )


class FakeAdapter:
    def __init__(self, response: BridgeResponse, bodies: dict[str, bytes] | None = None) -> None:
        self.response = response
        self.bodies = bodies or {}
        self.search_calls: list[tuple[str, int, Path]] = []
        self.account_calls: list[tuple[str, int, Path]] = []

    def _write_candidates(self, staging: Path) -> None:
        for name, body in self.bodies.items():
            (staging / name).write_bytes(body)

    def collect_search(self, keyword: str, limit: int, asset_staging_dir: Path) -> BridgeResponse:
        self.search_calls.append((keyword, limit, asset_staging_dir))
        self._write_candidates(asset_staging_dir)
        return self.response

    def collect_account(
        self, account_id: str, limit: int, asset_staging_dir: Path
    ) -> BridgeResponse:
        self.account_calls.append((account_id, limit, asset_staging_dir))
        self._write_candidates(asset_staging_dir)
        return self.response


def _collector(adapter: FakeAdapter) -> VisibleCollector:
    moments = iter(
        [
            datetime(2026, 8, 29, 12, 0, tzinfo=UTC),
            datetime(2026, 8, 29, 12, 0, 1, tzinfo=UTC),
        ]
    )
    return VisibleCollector(
        adapter,
        clock=lambda: next(moments),
        run_id_factory=lambda: "run_fixture_1",
    )


def test_collect_search_keeps_keyword_positions_counts_and_local_cover(tmp_path: Path) -> None:
    cover = _media_candidate("note_a-image-001.jpg", 1)
    adapter = FakeAdapter(
        BridgeResponse(
            status="complete",
            payload=SearchPayload(notes=[_media_note([cover], source_position=4)]).model_dump(exclude_none=True),
        ),
        {cover.staging_name: JPEG},
    )

    run = _collector(adapter).collect_search("AI workflow", 2, tmp_path / "output")

    assert run.mode == "search"
    assert run.input_summary == "AI workflow"
    assert run.requested_count == 2
    assert run.actual_count == 1
    assert run.status.value == "complete"
    assert run.started_at < run.finished_at
    assert run.notes[0].source_position == 4
    assert run.notes[0].cover_local_path == "assets/note_a-image-001.jpg"
    assert run.notes[0].cover_asset is not None
    assert run.notes[0].cover_asset.model_dump() == {
        "local_path": "assets/note_a-image-001.jpg",
        "mime_type": "image/jpeg",
        "size_bytes": len(JPEG),
        "sha256": hashlib.sha256(JPEG).hexdigest(),
    }
    assert (tmp_path / "output" / "assets" / "note_a-image-001.jpg").read_bytes() == JPEG
    assert not (tmp_path / "output" / ".staging").exists()
    assert adapter.search_calls == [("AI workflow", 2, tmp_path / "output" / ".staging")]


def test_collect_search_persists_note_evidence(tmp_path: Path) -> None:
    """Removing evidence transfer loses visible detail and search-card context."""
    adapter = FakeAdapter(
        BridgeResponse(
            status="complete",
            payload=SearchPayload(
                notes=[
                    _note(
                        "note_a",
                        4,
                        time_evidence={"kind": "edited", "raw_text": "编辑于 08-12"},
                        metrics={
                            "likes": {
                                "raw_value": "12",
                                "normalized_value": 12,
                                "precision": "exact",
                            },
                            "shares": {
                                "raw_value": "7",
                                "normalized_value": 7,
                                "precision": "exact",
                            },
                        },
                        metric_provenance={
                            "likes": "detail_visible_count",
                            "shares": "search_card_interface",
                        },
                    )
                ]
            ).model_dump(exclude_none=True),
        )
    )

    run = _collector(adapter).collect_search("AI workflow", 1, tmp_path / "output")

    assert run.notes[0].time_evidence is not None
    assert run.notes[0].time_evidence.model_dump() == {
        "kind": "edited",
        "raw_text": "编辑于 08-12",
    }
    assert run.notes[0].metric_provenance == {
        "likes": "detail_visible_count",
        "shares": "search_card_interface",
    }
    assert run.notes[0].source_position == 4


def test_materialized_note_defensively_copies_nested_bridge_evidence(tmp_path: Path) -> None:
    candidate = _note(
        "note_a",
        1,
        tags=["AI"],
        time_evidence=TimeEvidence(kind="edited", raw_text="编辑于 08-12"),
        metrics=NoteMetrics(likes=MetricValue(raw_value="12", normalized_value=12, precision="exact")),
        metric_provenance={"likes": "detail_visible_count"},
    )
    staging = tmp_path / "staging"
    assets = tmp_path / "assets"
    staging.mkdir()
    assets.mkdir()

    notes, media_failed, invalid_note = _collector(
        FakeAdapter(BridgeResponse(status="complete"))
    )._materialize_notes([candidate], 1, staging, assets)

    assert media_failed is False
    assert invalid_note is False
    assert len(notes) == 1
    candidate.tags.append("mutated")
    assert candidate.time_evidence is not None
    candidate.time_evidence.raw_text = "编辑于 09-01"
    assert candidate.metrics.likes is not None
    candidate.metrics.likes.raw_value = "99"
    candidate.metric_provenance["likes"] = "search_card_interface"

    assert notes[0].tags == ["AI"]
    assert notes[0].time_evidence is not None
    assert notes[0].time_evidence.raw_text == "编辑于 08-12"
    assert notes[0].metrics.likes is not None
    assert notes[0].metrics.likes.raw_value == "12"
    assert notes[0].metric_provenance == {"likes": "detail_visible_count"}


def test_collect_account_validates_url_passes_account_id_and_keeps_profile(tmp_path: Path) -> None:
    avatar = _candidate("avatar.jpg")
    account = AccountRecord(
        account_id="author_a",
        profile_url="https://www.xiaohongshu.com/user/profile/author_a",
        name="Visible author",
    )
    adapter = FakeAdapter(
        BridgeResponse(
            status="complete",
            payload=_strict_account_payload(
                account, [_note("note_a", 1)], avatar
            ).model_dump(exclude_none=True),
        ),
        {avatar.staging_name: JPEG},
    )

    run = _collector(adapter).collect_account(
        "https://www.xiaohongshu.com/user/profile/author_a?xsec_token=ephemeral",
        1,
        tmp_path / "output",
    )

    assert adapter.account_calls == [("author_a", 1, tmp_path / "output" / ".staging")]
    assert run.account is not None
    assert run.account.profile_url == "https://www.xiaohongshu.com/user/profile/author_a"
    assert run.account.avatar_local_path == "assets/account-avatar.jpg"
    assert run.account.avatar_asset is not None
    assert run.account.avatar_asset.size_bytes == len(JPEG)
    assert run.actual_count == 1


def test_account_bridge_omitted_not_exposed_metrics_validate_and_collect(tmp_path: Path) -> None:
    raw_response: dict[str, object] = {
        "status": "complete",
        "payload": {
            "account": {
                "account_id": "author_a",
                "profile_url": "https://www.xiaohongshu.com/user/profile/author_a",
                "name": "Visible author",
                "bio": "Visible bio",
                "note_count": {
                    "raw_value": "2",
                    "normalized_value": 2,
                    "precision": "exact",
                },
                "follower_count": {
                    "raw_value": "3",
                    "normalized_value": 3,
                    "precision": "exact",
                    },
                    "platform_metrics": {},
                    "field_statuses": {
                        "name": "exposed",
                        "bio": "exposed",
                        "note_count": "exposed",
                        "follower_count": "exposed",
                        "avatar": "exposed",
                        "platform_metrics": "not_exposed",
                    },
                },
            "notes": [
                {
                    "note_id": "note_a",
                    "canonical_url": "https://www.xiaohongshu.com/explore/note_a",
                    "title": "Visible title",
                    "body": "Visible body",
                    "tags": ["AI"],
                    "note_type": "normal",
                    "metrics": {
                        "collects": {"precision": "not_exposed"},
                        "comments": {"precision": "not_exposed"},
                    },
                    "source_position": 1,
                    "media_manifest_version": 2,
                    "media_discovered_count": 0,
                    "author_id": "author_a",
                    "cover": {
                        "staging_name": "note_a-cover.jpg",
                        "mime_type": "image/jpeg",
                        "size_bytes": len(JPEG),
                        "sha256": hashlib.sha256(JPEG).hexdigest(),
                    },
                }
            ],
                "avatar": {
                    "staging_name": "avatar.jpg",
                "mime_type": "image/jpeg",
                "size_bytes": len(JPEG),
                    "sha256": hashlib.sha256(JPEG).hexdigest(),
                },
                "requested_count": 1,
                "candidate_attempts": [
                    {
                        "position": 1,
                        "note_id": "note_a",
                        "outcome": "complete",
                        "stage": "detail_projection",
                    }
                ],
                "pacing_summary": {
                    "policy": "conservative_jitter_v1",
                    "profile_open_delay_ms": 2_000,
                    "detail_delay_ms": [3_000],
                },
            },
        "error_code": None,
    }

    response = validate_bridge_response(
        BridgeRequest(operation="account", account_id="author_a", limit=1), raw_response
    )
    run = _collector(
        FakeAdapter(response, {"note_a-cover.jpg": JPEG, "avatar.jpg": JPEG})
    ).collect_account("https://www.xiaohongshu.com/user/profile/author_a", 1, tmp_path / "output")

    bridge_notes = response.payload["notes"]
    assert isinstance(bridge_notes, list)
    assert len(bridge_notes) == 1
    bridge_note = bridge_notes[0]
    assert isinstance(bridge_note, dict)
    assert bridge_note["metrics"] == {
        "collects": {"precision": "not_exposed"},
        "comments": {"precision": "not_exposed"},
    }
    assert run.status.value == "complete"
    assert run.notes[0].metrics.collects is not None
    assert run.notes[0].metrics.collects.model_dump(exclude_none=True) == {
        "precision": "not_exposed"
    }
    assert run.notes[0].metrics.comments is not None
    assert run.notes[0].metrics.comments.model_dump(exclude_none=True) == {
        "precision": "not_exposed"
    }


def test_account_collector_persists_complete_candidate_attempt_and_pacing_evidence(
    tmp_path: Path,
) -> None:
    avatar = _candidate("avatar.jpg")
    account = AccountRecord(
        account_id="author_a",
        profile_url="https://www.xiaohongshu.com/user/profile/author_a",
        name="Hana",
        bio="",
    )
    notes = [_note(f"note_{position}", position, author_id="author_a") for position in range(1, 6)]
    payload = _strict_account_payload(
        account,
        notes,
        avatar,
        requested_count=5,
        field_statuses={
            "name": "exposed",
            "bio": "exposed_empty",
            "note_count": "not_exposed",
            "follower_count": "not_exposed",
            "avatar": "exposed",
            "platform_metrics": "not_exposed",
        },
    )
    adapter = FakeAdapter(
        BridgeResponse(status="complete", payload=payload.model_dump(exclude_none=True)),
        {avatar.staging_name: JPEG},
    )

    run = _collector(adapter).collect_account(
        "https://www.xiaohongshu.com/user/profile/author_a", 5, tmp_path / "output"
    )

    assert run.requested_count == 5
    assert run.actual_count == 5
    assert run.status == RunStatus.COMPLETE
    assert [attempt.position for attempt in run.candidate_attempts] == [1, 2, 3, 4, 5]
    assert all(attempt.outcome == "complete" for attempt in run.candidate_attempts)
    assert run.pacing_summary is not None
    assert run.pacing_summary.detail_delay_ms == [3_000] * 5
    assert run.account is not None and run.account.bio == ""
    assert run.account.field_statuses["bio"] == "exposed_empty"


def test_account_collector_persists_partial_candidate_attempt_and_pacing_evidence(
    tmp_path: Path,
) -> None:
    avatar = _candidate("avatar.jpg")
    account = AccountRecord(
        account_id="author_a",
        profile_url="https://www.xiaohongshu.com/user/profile/author_a",
        name="Hana",
    )
    notes = [_note(f"note_{position}", position, author_id="author_a") for position in (1, 2)]
    attempts = [
        CandidateAttempt(
            position=note.source_position,
            note_id=note.note_id,
            outcome="complete",
            stage="detail_projection",
        )
        for note in notes
    ] + [
        CandidateAttempt(
            position=position,
            outcome="unavailable",
            stage="candidate",
            reason="candidate_shortfall",
        )
        for position in (3, 4, 5)
    ]
    payload = _strict_account_payload(
        account,
        notes,
        avatar,
        requested_count=5,
        candidate_attempts=attempts,
    )
    adapter = FakeAdapter(
        BridgeResponse(
            status="partial",
            error_code="detail_unavailable",
            payload=payload.model_dump(exclude_none=True),
        ),
        {avatar.staging_name: JPEG},
    )

    run = _collector(adapter).collect_account(
        "https://www.xiaohongshu.com/user/profile/author_a", 5, tmp_path / "output"
    )

    assert run.requested_count == 5
    assert run.actual_count == 2
    assert run.status == RunStatus.PARTIAL
    assert run.error_code == "detail_unavailable"
    assert [attempt.position for attempt in run.candidate_attempts] == [1, 2, 3, 4, 5]
    assert run.pacing_summary is not None


def test_account_collector_prioritizes_media_scope_candidate_attempt_evidence(
    tmp_path: Path,
) -> None:
    avatar = _candidate("avatar.jpg")
    account = AccountRecord(
        account_id="author_a",
        profile_url="https://www.xiaohongshu.com/user/profile/author_a",
        name="Hana",
    )
    payload = _strict_account_payload(
        account,
        [_note("note_a", 1, author_id="author_a")],
        avatar,
        requested_count=2,
        candidate_attempts=[
            CandidateAttempt(
                position=1,
                note_id="note_a",
                outcome="complete",
                stage="detail_projection",
            ),
            CandidateAttempt(
                position=2,
                note_id="note_b",
                outcome="rejected",
                stage="detail_projection",
                reason="media_scope_invalid",
            ),
        ],
        pacing_summary=PacingSummary(
            policy="conservative_jitter_v1",
            profile_open_delay_ms=2_000,
            detail_delay_ms=[3_000, 3_000],
        ),
    )
    adapter = FakeAdapter(
        BridgeResponse(
            status="partial",
            error_code="detail_unavailable",
            payload=payload.model_dump(exclude_none=True),
        ),
        {avatar.staging_name: JPEG},
    )

    run = _collector(adapter).collect_account(
        "https://www.xiaohongshu.com/user/profile/author_a", 2, tmp_path / "output"
    )

    assert run.status == RunStatus.PARTIAL
    assert run.error_code == "media_scope_invalid"


def test_account_collector_rejects_mismatched_requested_count(tmp_path: Path) -> None:
    avatar = _candidate("avatar.jpg")
    account = AccountRecord(
        account_id="author_a",
        profile_url="https://www.xiaohongshu.com/user/profile/author_a",
        name="Hana",
    )
    payload = _strict_account_payload(
        account,
        [
            _note("note_a", 1, author_id="author_a"),
            _note("note_b", 2, author_id="author_a"),
        ],
        avatar,
        requested_count=2,
    )
    adapter = FakeAdapter(BridgeResponse(status="complete", payload=payload.model_dump(exclude_none=True)))

    run = _collector(adapter).collect_account(
        "https://www.xiaohongshu.com/user/profile/author_a", 1, tmp_path / "output"
    )

    assert run.status == RunStatus.FAILED
    assert run.error_code == "invalid_bridge_payload"
    assert run.candidate_attempts == []
    assert run.pacing_summary is None


@pytest.mark.parametrize(
    "candidate_attempts",
    [
        [
            {
                "position": 2,
                "note_id": "note_a",
                "outcome": "complete",
                "stage": "detail_projection",
            }
        ],
        [
            {
                "position": 1,
                "outcome": "unavailable",
                "stage": "candidate",
                "reason": "candidate_shortfall",
            }
        ],
    ],
    ids=("noncontiguous", "complete_attempt_note_mismatch"),
)
def test_account_collector_rejects_invalid_candidate_attempt_evidence(
    tmp_path: Path, candidate_attempts: list[dict[str, object]]
) -> None:
    avatar = _candidate("avatar.jpg")
    account = AccountRecord(
        account_id="author_a",
        profile_url="https://www.xiaohongshu.com/user/profile/author_a",
        name="Hana",
    )
    payload = _strict_account_payload(
        account, [_note("note_a", 1, author_id="author_a")], avatar
    ).model_dump(exclude_none=True)
    payload["candidate_attempts"] = candidate_attempts
    adapter = FakeAdapter(BridgeResponse(status="complete", payload=payload))

    run = _collector(adapter).collect_account(
        "https://www.xiaohongshu.com/user/profile/author_a", 1, tmp_path / "output"
    )

    assert run.status == RunStatus.FAILED
    assert run.error_code == "invalid_bridge_payload"


def test_account_collector_marks_an_unavailable_name_field_status_partial(tmp_path: Path) -> None:
    avatar = _candidate("avatar.jpg")
    account = AccountRecord(
        account_id="author_a",
        profile_url="https://www.xiaohongshu.com/user/profile/author_a",
    )
    payload = _strict_account_payload(
        account,
        [_note("note_a", 1, author_id="author_a")],
        avatar,
        field_statuses={
            "name": "unavailable",
            "bio": "not_exposed",
            "note_count": "not_exposed",
            "follower_count": "not_exposed",
            "avatar": "exposed",
            "platform_metrics": "not_exposed",
        },
    )
    adapter = FakeAdapter(
        BridgeResponse(status="complete", payload=payload.model_dump(exclude_none=True)),
        {avatar.staging_name: JPEG},
    )

    run = _collector(adapter).collect_account(
        "https://www.xiaohongshu.com/user/profile/author_a", 1, tmp_path / "output"
    )

    assert run.status == RunStatus.PARTIAL
    assert run.error_code == "profile_unavailable"
    assert run.account is not None
    assert run.account.field_statuses["name"] == "unavailable"


def test_account_collector_uses_media_error_for_unmaterialized_exposed_avatar_field_status(
    tmp_path: Path,
) -> None:
    avatar = _candidate("avatar.jpg")
    account = AccountRecord(
        account_id="author_a",
        profile_url="https://www.xiaohongshu.com/user/profile/author_a",
        name="Hana",
    )
    payload = _strict_account_payload(
        account, [_note("note_a", 1, author_id="author_a")], avatar
    )
    adapter = FakeAdapter(BridgeResponse(status="complete", payload=payload.model_dump(exclude_none=True)))

    run = _collector(adapter).collect_account(
        "https://www.xiaohongshu.com/user/profile/author_a", 1, tmp_path / "output"
    )

    assert run.status == RunStatus.PARTIAL
    assert run.error_code == "media_unavailable"
    assert run.account is not None and run.account.avatar_asset is None


def test_account_collector_keeps_not_exposed_avatar_field_status_as_profile_partial(
    tmp_path: Path,
) -> None:
    account = AccountRecord(
        account_id="author_a",
        profile_url="https://www.xiaohongshu.com/user/profile/author_a",
        name="Hana",
    )
    payload = _strict_account_payload(
        account,
        [_note("note_a", 1, author_id="author_a")],
        field_statuses={
            "name": "exposed",
            "bio": "not_exposed",
            "note_count": "not_exposed",
            "follower_count": "not_exposed",
            "avatar": "not_exposed",
            "platform_metrics": "not_exposed",
        },
    )
    adapter = FakeAdapter(BridgeResponse(status="complete", payload=payload.model_dump(exclude_none=True)))

    run = _collector(adapter).collect_account(
        "https://www.xiaohongshu.com/user/profile/author_a", 1, tmp_path / "output"
    )

    assert run.status == RunStatus.PARTIAL
    assert run.error_code == "profile_unavailable"
    assert run.account is not None
    assert run.account.account_id == "author_a"
    assert run.account.profile_url == "https://www.xiaohongshu.com/user/profile/author_a"
    assert run.account.avatar_local_path is None
    assert run.account.avatar_asset is None
    assert run.account.field_statuses["avatar"] == "not_exposed"


@pytest.mark.parametrize(
    "bad_second_note",
    [
        _note(
            "note_b",
            2,
            author_id="author_a",
            canonical_url="https://www.xiaohongshu.com/explore/other_note",
        ),
        _media_note(
            [
                StagedMediaCandidate(
                    note_id="note_b",
                    role="image",
                    position=1,
                    status="downloaded",
                    staging_name="note_b-image-001.jpg",
                    mime_type="image/jpeg",
                    size_bytes=len(JPEG),
                    sha256=hashlib.sha256(JPEG).hexdigest(),
                )
            ],
            note_id="note_b",
            source_position=2,
        ).model_copy(update={"author_id": "author_a"}),
    ],
    ids=("bad_identity", "bad_media"),
)
def test_failed_account_materialization_removes_previously_persisted_assets(
    tmp_path: Path, bad_second_note: BridgeNote
) -> None:
    avatar = _candidate("avatar.jpg")
    first_media = _media_candidate("note_a-image-001.jpg", 1)
    account = AccountRecord(
        account_id="author_a",
        profile_url="https://www.xiaohongshu.com/user/profile/author_a",
        name="Hana",
    )
    payload = _strict_account_payload(
        account,
        [
            _media_note([first_media]).model_copy(update={"author_id": "author_a"}),
            bad_second_note,
        ],
        avatar,
        requested_count=2,
    )
    adapter = FakeAdapter(
        BridgeResponse(status="complete", payload=payload.model_dump(exclude_none=True)),
        {avatar.staging_name: JPEG, first_media.staging_name: JPEG},
    )

    run = _collector(adapter).collect_account(
        "https://www.xiaohongshu.com/user/profile/author_a", 2, tmp_path / "output"
    )

    assets = tmp_path / "output" / "assets"
    assert run.status == RunStatus.FAILED
    assert run.error_code == "invalid_bridge_payload"
    assert assets.is_dir()
    assert list(assets.iterdir()) == []
    assert not (tmp_path / "output" / ".staging").exists()


def test_failed_collection_does_not_clean_a_replaced_assets_directory(tmp_path: Path) -> None:
    class ReplacingAssetsAdapter:
        def __init__(self) -> None:
            self.replacement_assets: Path | None = None
            self.renamed_assets: Path | None = None
            self.external_file = tmp_path / "external-sentinel.txt"

        def collect_search(
            self, _keyword: str, _limit: int, asset_staging_dir: Path
        ) -> BridgeResponse:
            output = asset_staging_dir.parent
            assets = output / "assets"
            self.renamed_assets = output / "collector-assets-original"
            assets.rename(self.renamed_assets)
            self.replacement_assets = assets
            assets.mkdir()
            (assets / "replacement-sentinel.txt").write_text("keep", encoding="utf-8")
            self.external_file.write_text("outside", encoding="utf-8")
            (assets / "outside-link").symlink_to(self.external_file)
            raise RuntimeError("synthetic adapter failure")

    adapter = ReplacingAssetsAdapter()
    run = _collector(adapter).collect_search("AI workflow", 1, tmp_path / "output")

    assert run.status == RunStatus.FAILED
    assert run.error_code == "adapter_failure"
    assert adapter.replacement_assets is not None
    assert adapter.renamed_assets is not None
    assert (adapter.replacement_assets / "replacement-sentinel.txt").read_text(encoding="utf-8") == "keep"
    assert adapter.external_file.read_text(encoding="utf-8") == "outside"
    assert adapter.renamed_assets.is_dir()
    assert not (tmp_path / "output" / ".staging").exists()


@pytest.mark.parametrize(
    ("avatar_status", "avatar_candidate"),
    [
        ("not_exposed", _candidate("avatar.jpg")),
        ("unavailable", _candidate("avatar.jpg")),
        ("exposed", None),
    ],
    ids=("not_exposed_with_candidate", "unavailable_with_candidate", "exposed_without_candidate"),
)
def test_account_collector_rejects_contradictory_avatar_field_evidence(
    tmp_path: Path, avatar_status: str, avatar_candidate: CoverCandidate | None
) -> None:
    account = AccountRecord(
        account_id="author_a",
        profile_url="https://www.xiaohongshu.com/user/profile/author_a",
        name="Hana",
    )
    payload = _strict_account_payload(
        account,
        [_note("note_a", 1, author_id="author_a")],
        avatar_candidate,
        field_statuses={
            "name": "exposed",
            "bio": "not_exposed",
            "note_count": "not_exposed",
            "follower_count": "not_exposed",
            "avatar": avatar_status,
            "platform_metrics": "not_exposed",
        },
    )
    bodies = (
        {avatar_candidate.staging_name: JPEG}
        if avatar_candidate is not None
        else None
    )
    adapter = FakeAdapter(
        BridgeResponse(status="complete", payload=payload.model_dump(exclude_none=True)), bodies
    )

    run = _collector(adapter).collect_account(
        "https://www.xiaohongshu.com/user/profile/author_a", 1, tmp_path / "output"
    )

    assets = tmp_path / "output" / "assets"
    assert run.status == RunStatus.FAILED
    assert run.error_code == "invalid_bridge_payload"
    assert run.candidate_attempts == []
    assert run.pacing_summary is None
    assert assets.is_dir()
    assert list(assets.iterdir()) == []
    assert not (tmp_path / "output" / ".staging").exists()


@pytest.mark.parametrize(
    ("name", "body", "mime_type"),
    [
        ("note_jpeg-cover.jpg", JPEG, "image/jpeg"),
        ("note_png-cover.png", PNG, "image/png"),
        ("note_webp-cover.webp", WEBP, "image/webp"),
    ],
)
def test_persisted_cover_retains_actual_metadata_for_each_allowed_image_type(
    tmp_path: Path, name: str, body: bytes, mime_type: str
) -> None:
    note_id = name.split("-")[0]
    candidate = _typed_media_candidate(note_id, body, mime_type)
    adapter = FakeAdapter(
        BridgeResponse(
            status="complete",
            payload=SearchPayload(notes=[_media_note([candidate], note_id=note_id)]).model_dump(
                exclude_none=True
            ),
        ),
        {candidate.staging_name: body},
    )

    run = _collector(adapter).collect_search("AI workflow", 1, tmp_path / "output")

    asset = run.notes[0].cover_asset
    assert asset is not None
    assert asset.local_path == run.notes[0].cover_local_path
    assert asset.mime_type == mime_type
    assert asset.size_bytes == len(body)
    assert asset.sha256 == hashlib.sha256(body).hexdigest()
    assert asset.model_dump(mode="json")["sha256"] == hashlib.sha256(body).hexdigest()


def test_persisted_cover_is_independent_from_a_staged_hard_link(tmp_path: Path) -> None:
    candidate = _media_candidate("note_a-image-001.jpg", 1)
    adapter = FakeAdapter(
        BridgeResponse(
            status="complete",
            payload=SearchPayload(notes=[_media_note([candidate])]).model_dump(
                exclude_none=True
            ),
        )
    )
    outside = tmp_path / "externally-mutable.jpg"
    outside.write_bytes(JPEG)

    def write_hard_link(staging: Path) -> None:
        (staging / candidate.staging_name).hardlink_to(outside)

    adapter._write_candidates = write_hard_link  # type: ignore[method-assign]
    run = _collector(adapter).collect_search("AI workflow", 1, tmp_path / "output")
    asset_path = tmp_path / "output" / "assets" / "note_a-image-001.jpg"

    assert run.status.value == "complete"
    outside.write_bytes(b"mutated external file")
    assert asset_path.read_bytes() == JPEG


@pytest.mark.parametrize(
    ("name", "body", "mime_type"),
    [
        ("avatar.jpg", JPEG, "image/jpeg"),
        ("avatar.png", PNG, "image/png"),
        ("avatar.webp", WEBP, "image/webp"),
    ],
)
def test_persisted_avatar_retains_actual_metadata_for_each_allowed_image_type(
    tmp_path: Path, name: str, body: bytes, mime_type: str
) -> None:
    avatar = _candidate(name, body, mime_type=mime_type)
    account = AccountRecord(
        account_id="author_a",
        profile_url="https://www.xiaohongshu.com/user/profile/author_a",
        name="Visible author",
        bio="Visible bio",
    )
    adapter = FakeAdapter(
        BridgeResponse(
            status="complete",
            payload=_strict_account_payload(account, [], avatar).model_dump(
                exclude_none=True
            ),
        ),
        {name: body},
    )

    run = _collector(adapter).collect_account(
        "https://www.xiaohongshu.com/user/profile/author_a", 1, tmp_path / "output"
    )

    assert run.account is not None
    asset = run.account.avatar_asset
    assert asset is not None
    assert asset.local_path == run.account.avatar_local_path
    assert asset.mime_type == mime_type
    assert asset.size_bytes == len(body)
    assert asset.sha256 == hashlib.sha256(body).hexdigest()


def test_partial_bridge_keeps_other_notes_and_never_exceeds_limit(tmp_path: Path) -> None:
    adapter = FakeAdapter(
        BridgeResponse(
            status="partial",
            error_code="detail_unavailable",
            payload=SearchPayload(
                notes=[_note("note_a", 2), _note("note_b", 3), _note("note_c", 4)]
            ).model_dump(exclude_none=True),
        )
    )

    run = _collector(adapter).collect_search("AI workflow", 2, tmp_path / "output")

    assert run.status.value == "partial"
    assert run.error_code == "detail_unavailable"
    assert [note.note_id for note in run.notes] == ["note_a", "note_b"]
    assert run.actual_count == 2


@pytest.mark.parametrize(
    ("body", "candidate"),
    [
        (b"", _media_candidate("note_a-image-001.jpg", 1)),
        (b"not-an-image", _media_candidate("note_a-image-001.jpg", 1)),
        (JPEG, StagedMediaCandidate(note_id="note_a", role="image", position=1,
                                    status="downloaded", staging_name="note_a-image-001.jpg",
                                    mime_type="image/jpeg", size_bytes=len(JPEG), sha256="a" * 64)),
        (JPEG + b"extra", _media_candidate("note_a-image-001.jpg", 1)),
    ],
    ids=("empty", "bad-magic", "hash-mismatch", "size-mismatch"),
)
def test_bad_downloaded_slot_fails_closed(
    tmp_path: Path, body: bytes, candidate: StagedMediaCandidate
) -> None:
    adapter = FakeAdapter(
        BridgeResponse(
            status="complete",
            payload=SearchPayload(notes=[_media_note([candidate])]).model_dump(
                exclude_none=True
            ),
        ),
        {candidate.staging_name: body},
    )

    run = _collector(adapter).collect_search("AI workflow", 1, tmp_path / "output")

    assert run.status.value == "failed"
    assert run.error_code == "invalid_bridge_payload"
    assert str(tmp_path) not in run.model_dump_json()
    assert not (tmp_path / "output" / "assets" / candidate.staging_name).exists()


def test_symlinked_downloaded_slot_fails_closed_and_staging_is_cleaned(tmp_path: Path) -> None:
    candidate = _media_candidate("note_a-image-001.jpg", 1)
    adapter = FakeAdapter(
        BridgeResponse(
            status="complete",
            payload=SearchPayload(notes=[_media_note([candidate])]).model_dump(
                exclude_none=True
            ),
        )
    )

    def write_symlink(staging: Path) -> None:
        outside = tmp_path / "outside.jpg"
        outside.write_bytes(JPEG)
        (staging / candidate.staging_name).symlink_to(outside)

    adapter._write_candidates = write_symlink  # type: ignore[method-assign]
    run = _collector(adapter).collect_search("AI workflow", 1, tmp_path / "output")

    assert run.status.value == "failed"
    assert not (tmp_path / "output" / ".staging").exists()


def test_not_exposed_avatar_keeps_account_partial_without_marking_it_missing(
    tmp_path: Path,
) -> None:
    account = AccountRecord(
        account_id="author_a",
        profile_url="https://www.xiaohongshu.com/user/profile/author_a",
        name="Visible author",
        bio="Visible bio",
    )
    adapter = FakeAdapter(
        BridgeResponse(
            status="complete",
            payload=_strict_account_payload(account, [], None).model_dump(
                exclude_none=True
            ),
        )
    )

    run = _collector(adapter).collect_account(
        "https://www.xiaohongshu.com/user/profile/author_a", 1, tmp_path / "output"
    )

    assert run.status.value == "partial"
    assert run.account is not None
    assert run.account.avatar_local_path is None
    assert "avatar" not in run.account.missing_fields
    assert run.account.field_statuses["avatar"] == "not_exposed"


def test_failed_bridge_returns_safe_failed_run_and_cleans_staging(tmp_path: Path) -> None:
    adapter = FakeAdapter(BridgeResponse(status="failed", payload={}, error_code="upstream_error"))

    run = _collector(adapter).collect_search("AI workflow", 1, tmp_path / "output")

    assert run.status.value == "failed"
    assert run.error_code == "upstream_error"
    assert run.notes == []
    assert run.finished_at is not None
    assert not (tmp_path / "output" / ".staging").exists()
    assert "upstream" not in run.model_dump_json().replace("upstream_error", "")


def test_existing_output_conflict_fails_closed_without_removing_user_file(tmp_path: Path) -> None:
    output = tmp_path / "output"
    output.mkdir()
    marker = output / "user-file.txt"
    marker.write_text("do not remove", encoding="utf-8")
    adapter = FakeAdapter(BridgeResponse(status="complete", payload={"notes": []}))

    run = _collector(adapter).collect_search("AI workflow", 1, output)

    assert run.status.value == "failed"
    assert run.error_code == "output_conflict"
    assert marker.read_text(encoding="utf-8") == "do not remove"


@pytest.mark.parametrize("limit", [0, 11])
def test_collect_rejects_limit_outside_visible_range(tmp_path: Path, limit: int) -> None:
    adapter = FakeAdapter(BridgeResponse(status="complete", payload={"notes": []}))

    with pytest.raises(ValueError, match="limit"):
        _collector(adapter).collect_search("AI workflow", limit, tmp_path / "output")


def test_invalid_account_url_is_rejected_before_adapter_call(tmp_path: Path) -> None:
    adapter = FakeAdapter(BridgeResponse(status="complete", payload={"notes": []}))

    with pytest.raises(ValueError):
        _collector(adapter).collect_account(
            "https://example.invalid/user/profile/a", 1, tmp_path / "output"
        )

    assert adapter.account_calls == []


@pytest.mark.parametrize(
    "keyword", ["xsec_token=secret", "https://example.invalid/result#fragment"]
)
def test_unsafe_keyword_is_rejected_before_adapter_call(tmp_path: Path, keyword: str) -> None:
    adapter = FakeAdapter(BridgeResponse(status="complete", payload={"notes": []}))

    with pytest.raises(ValueError):
        _collector(adapter).collect_search(keyword, 1, tmp_path / "output")

    assert adapter.search_calls == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"title": "Authorization=secret"},
        {"body": "https://example.invalid/result?token=secret"},
        {"tags": ["https://example.invalid/result#fragment"]},
        {"author_name": "xsec_token=secret"},
    ],
)
def test_unsafe_bridge_text_fails_closed_without_retaining_it(
    tmp_path: Path, overrides: dict[str, object]
) -> None:
    adapter = FakeAdapter(
        BridgeResponse(
            status="complete",
            payload=SearchPayload(notes=[_note("note_a", 1, **overrides)]).model_dump(
                exclude_none=True
            ),
        )
    )

    run = _collector(adapter).collect_search("ordinary Chinese 正文", 1, tmp_path / "output")

    assert run.status.value == "failed"
    assert run.error_code == "invalid_bridge_payload"
    assert "secret" not in run.model_dump_json()


@pytest.mark.parametrize(
    "overrides",
    [
        {"author_id": "xsec_token=unsafe"},
        {
            "author_id": None,
            "author_profile_url": "https://www.xiaohongshu.com/user/profile/author_a",
        },
        {
            "author_id": "author_a",
            "author_profile_url": "https://www.xiaohongshu.com/user/profile/other_author",
        },
    ],
)
def test_unsafe_or_unbound_note_author_identity_fails_closed(
    tmp_path: Path, overrides: dict[str, object]
) -> None:
    adapter = FakeAdapter(
        BridgeResponse(
            status="complete",
            payload=SearchPayload(notes=[_note("note_a", 1, **overrides)]).model_dump(
                exclude_none=True
            ),
        )
    )

    run = _collector(adapter).collect_search("ordinary Chinese 正文", 1, tmp_path / "output")

    assert run.status.value == "failed"
    assert run.error_code == "invalid_bridge_payload"
    assert "xsec_token" not in run.model_dump_json()
    assert "unsafe" not in run.model_dump_json()


@pytest.mark.parametrize(
    ("account_id", "profile_url", "author_id"),
    [
        ("other", "https://www.xiaohongshu.com/user/profile/other", "requested"),
        ("requested", "https://www.xiaohongshu.com/user/profile/other", "requested"),
        ("requested", "https://www.xiaohongshu.com/user/profile/requested", "other"),
    ],
)
def test_account_response_and_notes_must_bind_to_requested_profile(
    tmp_path: Path, account_id: str, profile_url: str, author_id: str
) -> None:
    account = AccountRecord(account_id=account_id, profile_url=profile_url)
    adapter = FakeAdapter(
        BridgeResponse(
            status="complete",
            payload=_strict_account_payload(
                account, [_note("note_a", 1, author_id=author_id)]
            ).model_dump(exclude_none=True),
        )
    )

    run = _collector(adapter).collect_account(
        "https://www.xiaohongshu.com/user/profile/requested", 1, tmp_path / "output"
    )

    assert run.status.value == "failed"
    assert run.error_code == "invalid_bridge_payload"


def test_missing_account_note_author_is_retained_but_marks_run_partial(tmp_path: Path) -> None:
    account = AccountRecord(
        account_id="requested",
        profile_url="https://www.xiaohongshu.com/user/profile/requested",
        name="Visible",
        bio="Visible",
    )
    adapter = FakeAdapter(
        BridgeResponse(
            status="complete",
            payload=_strict_account_payload(
                account, [_note("note_a", 1, author_id=None)]
            ).model_dump(exclude_none=True),
        )
    )

    run = _collector(adapter).collect_account(
        "https://www.xiaohongshu.com/user/profile/requested", 1, tmp_path / "output"
    )

    assert run.status.value == "partial"
    assert "author_id" in run.notes[0].missing_fields


def test_missing_optional_note_fields_are_explicit(tmp_path: Path) -> None:
    adapter = FakeAdapter(
        BridgeResponse(
            status="complete",
            payload=SearchPayload(
                notes=[
                    _note(
                        "note_a",
                        1,
                        None,
                        title=None,
                        body=None,
                        note_type=None,
                        published_at=None,
                        author_id=None,
                        author_name=None,
                        author_profile_url=None,
                    )
                ]
            ).model_dump(exclude_none=True),
        )
    )

    run = _collector(adapter).collect_search("AI workflow", 1, tmp_path / "output")

    assert {
        "title",
        "body",
        "note_type",
        "published_at",
        "author_id",
        "author_name",
        "author_profile_url",
    }.issubset(run.notes[0].missing_fields)


def test_collector_persists_ordered_media_and_derives_first_cover(tmp_path: Path) -> None:
    candidates = [
        _media_candidate("note_a-image-001.jpg", 1),
        _media_candidate("note_a-image-002.jpg", 2),
        StagedMediaCandidate(
            note_id="note_a",
            role="image",
            position=3,
            status="downloaded",
            staging_name="note_a-image-003.webp",
            mime_type="image/webp",
            size_bytes=len(WEBP),
            sha256=hashlib.sha256(WEBP).hexdigest(),
        ),
    ]
    adapter = FakeAdapter(
        BridgeResponse(status="complete", payload=SearchPayload(notes=[_media_note(candidates)]).model_dump(exclude_none=True)),
        {candidates[0].staging_name: JPEG, candidates[1].staging_name: JPEG,
         candidates[2].staging_name: WEBP},
    )

    note = _collector(adapter).collect_search("AI workflow", 3, tmp_path / "output").notes[0]

    assert [slot.asset.local_path for slot in note.media_slots if slot.asset is not None] == [
        "assets/note_a-image-001.jpg",
        "assets/note_a-image-002.jpg",
        "assets/note_a-image-003.webp",
    ]
    assert note.cover_asset == note.media_slots[0].asset


def test_collector_sorts_reversed_image_candidates_before_serialization(tmp_path: Path) -> None:
    first = _media_candidate("note_a-image-001.jpg", 1)
    second = _media_candidate("note_a-image-002.jpg", 2)
    adapter = FakeAdapter(
        BridgeResponse(
            status="complete",
            payload=SearchPayload(notes=[_media_note([second, first])]).model_dump(exclude_none=True),
        ),
        {first.staging_name: JPEG, second.staging_name: JPEG},
    )

    run = _collector(adapter).collect_search("AI workflow", 1, tmp_path / "output")

    serialized = run.model_dump(mode="json")
    raw_notes = serialized["notes"]
    assert isinstance(raw_notes, list)
    raw_slots = raw_notes[0]["media_slots"]
    assert [slot["position"] for slot in raw_slots] == [1, 2]


def test_media_candidate_order_is_stable_across_roles_and_positions() -> None:
    image = _media_candidate("note_a-image-001.jpg", 1)
    poster = StagedMediaCandidate(
        note_id="note_a", role="video_cover", position=1, status="downloaded",
        staging_name="note_a-video-cover.jpg", mime_type="image/jpeg", size_bytes=len(JPEG),
        sha256=hashlib.sha256(JPEG).hexdigest(),
    )
    video = StagedMediaCandidate(
        note_id="note_a", role="video", position=1, status="downloaded",
        staging_name="note_a-video.mp4", mime_type="video/mp4", size_bytes=len(MP4),
        sha256=hashlib.sha256(MP4).hexdigest(), duration_ms=1000,
    )

    ordered = collector_module._ordered_media_candidates([video, poster, image])

    assert [(item.role, item.position) for item in ordered] == [
        ("image", 1), ("video_cover", 1), ("video", 1),
    ]


def test_persist_media_candidate_closes_staging_descriptor_when_assets_open_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = _media_candidate("note_a-image-001.jpg", 1)
    staging = tmp_path / "staging"
    assets = tmp_path / "assets"
    staging.mkdir()
    assets.mkdir()
    original_open = collector_module.os.open
    original_close = collector_module.os.close
    staging_fd: int | None = None
    closed: list[int] = []

    def fail_assets_open(path: object, flags: int, *args: object, **kwargs: object) -> int:
        nonlocal staging_fd
        if Path(path) == assets:
            raise OSError("simulated assets open failure")
        result = original_open(path, flags, *args, **kwargs)
        if Path(path) == staging:
            staging_fd = result
        return result

    def record_close(fd: int) -> None:
        closed.append(fd)
        original_close(fd)

    monkeypatch.setattr(collector_module.os, "open", fail_assets_open)
    monkeypatch.setattr(collector_module.os, "close", record_close)

    with pytest.raises(ValueError, match="media directories"):
        collector_module._persist_media_candidate(candidate, "note_a", staging, assets)

    assert staging_fd is not None
    assert staging_fd in closed


def test_collector_retains_good_slots_and_marks_exact_missing_slot_partial(tmp_path: Path) -> None:
    candidates = [
        _media_candidate("note_a-image-001.jpg", 1),
        StagedMediaCandidate(note_id="note_a", role="image", position=2, status="missing",
                             missing_reason="source_not_exposed"),
    ]
    adapter = FakeAdapter(
        BridgeResponse(status="partial", error_code="media_partial", payload=SearchPayload(notes=[_media_note(candidates)]).model_dump(exclude_none=True)),
        {candidates[0].staging_name: JPEG},
    )

    run = _collector(adapter).collect_search("AI workflow", 2, tmp_path / "output")

    assert run.status == RunStatus.PARTIAL
    assert run.notes[0].missing_fields.count("media.image.002") == 1
    assert run.notes[0].media_slots[0].status == "downloaded"
    assert run.notes[0].media_slots[1].status == "missing"


def test_collector_never_substitutes_first_image_for_a_missing_slot(tmp_path: Path) -> None:
    candidates = [
        _media_candidate("note_a-image-001.jpg", 1),
        StagedMediaCandidate(note_id="note_a", role="image", position=2, status="missing",
                             missing_reason="source_not_exposed"),
    ]
    adapter = FakeAdapter(
        BridgeResponse(status="partial", error_code="media_partial", payload=SearchPayload(notes=[_media_note(candidates)]).model_dump(exclude_none=True)),
        {candidates[0].staging_name: JPEG},
    )

    note = _collector(adapter).collect_search("AI workflow", 2, tmp_path / "output").notes[0]

    assert [slot.asset for slot in note.media_slots if slot.position == 2] == [None]


def test_complete_bridge_with_an_unavailable_media_slot_is_partial(tmp_path: Path) -> None:
    candidate = StagedMediaCandidate(
        note_id="note_a", role="image", position=1, status="rejected", missing_reason="slot_limit"
    )
    adapter = FakeAdapter(
        BridgeResponse(status="complete", payload=SearchPayload(notes=[_media_note([candidate])]).model_dump(exclude_none=True))
    )

    run = _collector(adapter).collect_search("AI workflow", 1, tmp_path / "output")

    assert run.status == RunStatus.PARTIAL
    assert run.error_code == "media_unavailable"
    assert run.notes[0].missing_fields.count("media.image.001") == 1


def test_media_missing_fields_are_precise_and_deduplicated() -> None:
    slots = [
        NoteMediaSlot(note_id="note_a", role="image", position=3, status="missing",
                      missing_reason="source_not_exposed"),
        NoteMediaSlot(note_id="note_a", role="image", position=4, status="rejected",
                      missing_reason="slot_limit"),
        NoteMediaSlot(note_id="note_a", role="video_cover", position=1, status="missing",
                      missing_reason="source_not_exposed"),
        NoteMediaSlot(note_id="note_a", role="video", position=1, status="rejected",
                      missing_reason="unsupported_source"),
    ]

    fields = collector_module._media_missing_fields(slots, discovery_truncated=True)

    assert fields == [
        "media.image.003", "media.image.004", "media.video_cover", "media.video",
        "media.discovery_after.100",
    ]


def test_discovery_truncation_is_partial_and_retained_exactly(tmp_path: Path) -> None:
    note = _note(
        "note_a", 1, cover=None, media_manifest_version=2, media_candidates=[],
        media_discovered_count=1, media_discovery_truncated=True,
    )
    adapter = FakeAdapter(
        BridgeResponse(status="complete", payload=SearchPayload(notes=[note]).model_dump(exclude_none=True))
    )

    run = _collector(adapter).collect_search("AI workflow", 1, tmp_path / "output")

    assert run.status == RunStatus.PARTIAL
    assert run.notes[0].media_discovery_truncated is True
    assert run.notes[0].missing_fields.count("media.discovery_after.100") == 1


def test_collector_persists_video_and_poster_without_replacing_them(tmp_path: Path) -> None:
    poster = StagedMediaCandidate(
        note_id="note_a", role="video_cover", position=1, status="downloaded",
        staging_name="note_a-video-cover.jpg", mime_type="image/jpeg", size_bytes=len(JPEG),
        sha256=hashlib.sha256(JPEG).hexdigest(),
    )
    video = StagedMediaCandidate(
        note_id="note_a", role="video", position=1, status="downloaded",
        staging_name="note_a-video.mp4", mime_type="video/mp4", size_bytes=len(MP4),
        sha256=hashlib.sha256(MP4).hexdigest(), duration_ms=1000,
    )
    adapter = FakeAdapter(
        BridgeResponse(status="complete", payload=SearchPayload(notes=[_media_note([poster, video])]).model_dump(exclude_none=True)),
        {poster.staging_name: JPEG, video.staging_name: MP4},
    )

    note = _collector(adapter).collect_search("AI workflow", 1, tmp_path / "output").notes[0]

    assert [(slot.role, slot.asset.local_path if slot.asset else None) for slot in note.media_slots] == [
        ("video_cover", "assets/note_a-video-cover.jpg"),
        ("video", "assets/note_a-video.mp4"),
    ]
    assert note.cover_asset == note.media_slots[0].asset
    assert note.media_slots[1].duration_ms == 1000


def test_media_copy_failure_cleans_destination_and_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    candidate = _media_candidate("note_a-image-001.jpg", 1)
    adapter = FakeAdapter(
        BridgeResponse(status="complete", payload=SearchPayload(notes=[_media_note([candidate])]).model_dump(exclude_none=True)),
        {candidate.staging_name: JPEG},
    )
    monkeypatch.setattr(collector_module, "_write_all", lambda _fd, _body: (_ for _ in ()).throw(OSError()))

    run = _collector(adapter).collect_search("AI workflow", 1, tmp_path / "output")

    assert run.status == RunStatus.FAILED
    assert not (tmp_path / "output" / "assets" / candidate.staging_name).exists()


def test_media_digest_mismatch_after_adapter_validation_fails_closed(tmp_path: Path) -> None:
    candidate = _media_candidate("note_a-image-001.jpg", 1)
    adapter = FakeAdapter(
        BridgeResponse(status="complete", payload=SearchPayload(notes=[_media_note([candidate])]).model_dump(exclude_none=True)),
        {candidate.staging_name: JPEG + b"changed"},
    )

    run = _collector(adapter).collect_search("AI workflow", 1, tmp_path / "output")

    assert run.status == RunStatus.FAILED
    assert not (tmp_path / "output" / "assets" / candidate.staging_name).exists()


def test_cross_note_staging_name_after_adapter_validation_fails_closed(tmp_path: Path) -> None:
    candidate = _media_candidate("note_a-image-001.jpg", 1)
    note = _media_note([candidate])
    note.media_candidates[0].staging_name = "other-image-001.jpg"
    adapter = FakeAdapter(
        BridgeResponse(status="complete", payload=SearchPayload(notes=[note]).model_dump(exclude_none=True)),
        {"other-image-001.jpg": JPEG},
    )

    run = _collector(adapter).collect_search("AI workflow", 1, tmp_path / "output")

    assert run.status == RunStatus.FAILED


def test_collector_rechecks_run_media_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    first = _media_candidate("note_a-image-001.jpg", 1)
    second = StagedMediaCandidate(
        note_id="note_b", role="image", position=1, status="downloaded",
        staging_name="note_b-image-001.jpg", mime_type="image/jpeg", size_bytes=len(JPEG),
        sha256=hashlib.sha256(JPEG).hexdigest(),
    )
    monkeypatch.setattr(collector_module, "MAX_RUN_MEDIA_BYTES", len(JPEG))
    adapter = FakeAdapter(
        BridgeResponse(status="complete", payload=SearchPayload(notes=[_media_note([first]), _media_note([second], note_id="note_b")]).model_dump(exclude_none=True)),
        {first.staging_name: JPEG, second.staging_name: JPEG},
    )

    run = _collector(adapter).collect_search("AI workflow", 2, tmp_path / "output")

    assert run.status == RunStatus.FAILED


def test_collector_rechecks_account_run_budget_with_avatar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Starting note accounting at zero would persist media beyond the account run cap."""
    avatar = _candidate("avatar.jpg")
    note_media = _media_candidate("note_a-image-001.jpg", 1)
    account = AccountRecord(
        account_id="author_a",
        profile_url="https://www.xiaohongshu.com/user/profile/author_a",
        name="Visible author",
        bio="Visible bio",
    )
    monkeypatch.setattr(collector_module, "MAX_RUN_MEDIA_BYTES", len(JPEG))
    adapter = FakeAdapter(
        BridgeResponse(
            status="complete",
            payload=_strict_account_payload(
                account, [_media_note([note_media])], avatar
            ).model_dump(exclude_none=True),
        ),
        {avatar.staging_name: JPEG, note_media.staging_name: JPEG},
    )

    run = _collector(adapter).collect_account(
        "https://www.xiaohongshu.com/user/profile/author_a", 1, tmp_path / "output"
    )

    assert run.status == RunStatus.FAILED


def test_legacy_versionless_cover_record_remains_supported_by_models() -> None:
    from xhs_workbench.models import LocalAsset, NoteMetrics, NoteRecord

    cover = LocalAsset(
        local_path="assets/note_a-cover.jpg", mime_type="image/jpeg", size_bytes=len(JPEG),
        sha256=hashlib.sha256(JPEG).hexdigest(),
    )
    note = NoteRecord(
        note_id="note_a", canonical_url="https://www.xiaohongshu.com/explore/note_a",
        metrics=NoteMetrics(), source_position=1, cover_local_path=cover.local_path, cover_asset=cover,
    )

    assert note.media_manifest_version is None
    assert note.media_slots == []
