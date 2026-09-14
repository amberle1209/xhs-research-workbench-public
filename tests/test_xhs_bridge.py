import ast
import hashlib
import io
import json
import os
import stat
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar, Self

import pytest

from xhs_workbench import pinned_https, xhs_bridge
from xhs_workbench.media import (
    MAX_DISCOVERED_IMAGE_SLOTS,
    MAX_IMAGE_BYTES,
    MAX_RUN_MEDIA_BYTES,
    MediaMime,
)
from xhs_workbench.models import MetricValue, NoteMetrics, PacingSummary
from xhs_workbench.persistent_client import DetailReadFailure
from xhs_workbench.xhs_bridge import (
    BridgeRequest,
    MediaBudget,
    configure_auth_directory,
    parse_bridge_request,
    run_bridge,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _fixture(name: str) -> dict[str, object]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FakeClient:
    def __init__(self) -> None:
        self.detail_calls: list[tuple[str, str]] = []
        self.pacing_summary_calls = 0
        self.search = _fixture("search.json")["search_feed"]
        self.details = {
            "note_image": _fixture("detail_image.json"),
            "note_video": _fixture("detail_video.json"),
        }
        image_note = self.details["note_image"]["note"]
        assert isinstance(image_note, dict)
        image_note["media_scope_valid"] = True
        image_note["media_discovered_count"] = 0
        image_note["image_list"] = []
        video_note = self.details["note_video"]["note"]
        assert isinstance(video_note, dict)
        video_note["type"] = "normal"
        video_note.pop("video", None)
        self.profile = _fixture("profile.json")
        page_data = self.profile["userPageData"]
        assert isinstance(page_data, dict)
        page_data["fieldStatuses"] = {
            "name": "exposed",
            "bio": "exposed",
            "note_count": "not_exposed",
            "follower_count": "not_exposed",
            "avatar": "not_exposed",
            "platform_metrics": "not_exposed",
        }
        self.posts = _fixture("account_notes.json")["user_posts"]

    def search_notes(self, keyword: str) -> object:
        assert keyword == "AI workflow"
        assert isinstance(self.search, list)
        result = [dict(item) for item in self.search]
        result[0]["xsec_token"] = "ephemeral-only"
        return result

    def get_note_detail(self, note_id: str, xsec_token: str = "") -> object:
        self.detail_calls.append((note_id, xsec_token))
        return self.details[note_id]

    def get_user_info(self, user_id: str) -> object:
        assert user_id == "author_a"
        return self.profile

    def get_user_posts(self, user_id: str) -> object:
        assert user_id == "author_a"
        return self.posts

    def account_pacing_summary(self) -> PacingSummary:
        self.pacing_summary_calls += 1
        return PacingSummary(
            policy="conservative_jitter_v1",
            profile_open_delay_ms=2_000,
            detail_delay_ms=[3_000] * len(self.detail_calls),
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> bool:
        return False


JPEG = b"\xff\xd8\xfffixture-image"


def _bridge_note(**overrides: object) -> xhs_bridge.BridgeNote:
    values: dict[str, object] = {
        "media_manifest_version": 2,
        "note_id": "note_a",
        "canonical_url": "https://www.xiaohongshu.com/explore/note_a",
        "metrics": NoteMetrics(
            likes=MetricValue(raw_value="1", normalized_value=1, precision="exact"),
            shares=MetricValue(raw_value="2", normalized_value=2, precision="exact"),
        ),
        "source_position": 1,
        "media_discovered_count": 0,
    }
    values.update(overrides)
    return xhs_bridge.BridgeNote(**values)


def test_bridge_note_serializes_finite_time_evidence_and_metric_provenance() -> None:
    child_payload = json.loads(
        _bridge_note(
            time_evidence={"kind": "edited", "raw_text": "编辑于 08-12"},
            metric_provenance={
                "likes": "detail_visible_count",
                "shares": "search_card_interface",
            },
        ).model_dump_json()
    )

    assert child_payload["time_evidence"] == {"kind": "edited", "raw_text": "编辑于 08-12"}
    assert child_payload["metric_provenance"] == {
        "likes": "detail_visible_count",
        "shares": "search_card_interface",
    }


@pytest.mark.parametrize(
    "overrides",
    [
        {"time_evidence": {"kind": "approximate", "raw_text": "编辑于 08-12"}},
        {"metric_provenance": {"likes": "inferred"}},
    ],
)
def test_bridge_note_rejects_invalid_time_evidence_or_metric_provenance(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        _bridge_note(**overrides)


def test_bridge_note_rejects_metric_provenance_for_a_not_exposed_metric() -> None:
    with pytest.raises(ValueError, match="provenance requires an exposed metric"):
        _bridge_note(
            metrics=NoteMetrics(
                shares=MetricValue(raw_value=None, normalized_value=None, precision="not_exposed")
            ),
            metric_provenance={"shares": "search_card_interface"},
        )


@pytest.mark.parametrize(
    "raw_text",
    ["", " \t", "\u200b", "https://www.xiaohongshu.com/explore/note_a"],
    ids=("blank", "whitespace", "zero_width_space", "query_free_url"),
)
def test_bridge_note_rejects_empty_or_url_like_time_evidence_text(raw_text: str) -> None:
    with pytest.raises(
        ValueError, match="time evidence raw_text must be safe retained public text"
    ):
        _bridge_note(time_evidence={"kind": "edited", "raw_text": raw_text})


def _three_image_detail() -> dict[str, object]:
    return {
        "note": {
            "id": "note_image",
            "interact_info": {},
            "media_scope_valid": True,
            "media_discovered_count": 3,
            "image_list": [
                {"position": 1, "url": "https://sns-webpic-qc.xhscdn.com/one.jpg?xsec=private"},
                {"position": 2, "url": "https://sns-webpic-qc.xhscdn.com/two.jpg"},
                {"position": 3, "url": "https://sns-webpic-qc.xhscdn.com/three.jpg"},
            ],
        }
    }


def _detail_with_missing_second_image() -> dict[str, object]:
    detail = _three_image_detail()
    note = detail["note"]
    assert isinstance(note, dict)
    image_list = note["image_list"]
    assert isinstance(image_list, list) and isinstance(image_list[1], dict)
    image_list[1]["url"] = None
    return detail


def _fake_media_download(
    url: str, staging_dir: Path, final_name: str, *, max_bytes: int, deadline: float
) -> tuple[MediaMime, int, str]:
    assert url.startswith("https://sns-webpic-qc.xhscdn.com/")
    assert max_bytes == MAX_IMAGE_BYTES
    assert deadline > time.monotonic()
    (staging_dir / final_name).write_bytes(JPEG)
    return "image/jpeg", len(JPEG), hashlib.sha256(JPEG).hexdigest()


def test_bridge_stages_three_images_in_exact_order_without_urls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(xhs_bridge, "_download_media", _fake_media_download)

    note = xhs_bridge._project_note(
        _three_image_detail(),
        "note_image",
        1,
        tmp_path,
        MediaBudget(remaining_bytes=MAX_RUN_MEDIA_BYTES),
    )

    assert note is not None
    assert [(item.role, item.position, item.status) for item in note.media_candidates] == [
        ("image", 1, "downloaded"),
        ("image", 2, "downloaded"),
        ("image", 3, "downloaded"),
    ]
    serialized = note.model_dump_json()
    assert "sns-webpic-qc.xhscdn.com" not in serialized
    assert "xsec" not in serialized.casefold()


def test_bridge_retains_a_missing_middle_image_slot(tmp_path: Path) -> None:
    note = xhs_bridge._project_note(
        _detail_with_missing_second_image(),
        "note_image",
        1,
        tmp_path,
        MediaBudget(remaining_bytes=MAX_RUN_MEDIA_BYTES),
    )

    assert note is not None
    assert note.media_candidates[1].model_dump(exclude_none=True) == {
        "note_id": "note_image",
        "role": "image",
        "position": 2,
        "status": "missing",
        "missing_reason": "source_not_exposed",
    }


@pytest.mark.parametrize("scope_value", [False, None])
def test_bridge_rejects_a_detail_without_one_exact_media_scope(
    tmp_path: Path, scope_value: bool | None
) -> None:
    detail = _three_image_detail()
    raw_note = detail["note"]
    assert isinstance(raw_note, dict)
    if scope_value is None:
        raw_note.pop("media_scope_valid")
    else:
        raw_note["media_scope_valid"] = scope_value
    client = FakeClient()
    client.details["note_image"] = detail

    response = run_bridge(
        BridgeRequest(operation="search", keyword="AI workflow", limit=1),
        lambda _profile: client,
        staging_dir=tmp_path,
    )

    assert response.status == "failed"
    assert response.payload == {}
    assert response.error_code == "media_scope_invalid"


def test_bridge_marks_valid_and_media_unavailable_notes_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = FakeClient()
    client.details["note_image"] = _detail_with_missing_second_image()
    monkeypatch.setattr(xhs_bridge, "_download_media", _fake_media_download)

    response = run_bridge(
        BridgeRequest(operation="search", keyword="AI workflow", limit=1),
        lambda _profile: client,
        staging_dir=tmp_path,
    )

    assert response.status == "partial"
    assert response.error_code == "media_partial"
    assert (
        response.payload["notes"][0]["media_candidates"][1]["missing_reason"]
        == "source_not_exposed"
    )


def test_bridge_rejects_duplicate_image_positions(tmp_path: Path) -> None:
    detail = _three_image_detail()
    note = detail["note"]
    assert isinstance(note, dict)
    images = note["image_list"]
    assert isinstance(images, list) and isinstance(images[1], dict)
    images[1]["position"] = 1

    assert (
        xhs_bridge._project_note(
            detail, "note_image", 1, tmp_path, MediaBudget(remaining_bytes=MAX_RUN_MEDIA_BYTES)
        )
        is None
    )


def test_bridge_retains_slots_above_download_limit_without_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    def unexpected_download(*_args: object, **_kwargs: object) -> tuple[MediaMime, int, str]:
        nonlocal calls
        calls += 1
        raise AssertionError("slot-limited images must not download")

    raw_note: dict[str, object] = {
        "id": "note_image",
        "type": "normal",
        "interact_info": {},
        "media_discovered_count": MAX_DISCOVERED_IMAGE_SLOTS,
        "image_list": [
            {"position": position, "url": "https://sns-webpic-qc.xhscdn.com/image.jpg"}
            for position in range(1, MAX_DISCOVERED_IMAGE_SLOTS + 1)
        ],
    }
    monkeypatch.setattr(xhs_bridge, "_download_media", unexpected_download)

    candidates = xhs_bridge._stage_note_media(
        raw_note, "note_image", tmp_path, MediaBudget(remaining_bytes=MAX_RUN_MEDIA_BYTES)
    )

    assert calls == 20
    assert [(item.position, item.missing_reason) for item in candidates[20:]] == [
        (position, "slot_limit") for position in range(21, MAX_DISCOVERED_IMAGE_SLOTS + 1)
    ]


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("blob:https://www.xiaohongshu.com/id", "unsupported_source"),
        ("data:video/mp4;base64,AA==", "unsupported_source"),
        ("https://sns-video-qc.xhscdn.com/master.m3u8", "unsupported_source"),
    ],
)
def test_video_never_guesses_an_unsupported_direct_source(
    tmp_path: Path, url: str, reason: str
) -> None:
    raw_note = {
        "id": "note_video",
        "type": "video",
        "author_id": "author_a",
        "interact_info": {},
        "video": {"poster": None, "url": url, "duration_ms": 1000},
    }
    candidates = xhs_bridge._stage_note_media(
        raw_note, "note_video", tmp_path, MediaBudget(remaining_bytes=MAX_RUN_MEDIA_BYTES)
    )

    candidate = next(item for item in candidates if item.role == "video")
    assert candidate.status == "rejected"
    assert candidate.missing_reason == reason


@pytest.mark.parametrize(
    "user",
    (
        {},
        {"userId": "access_token=hostile"},
    ),
    ids=("absent", "hostile"),
)
def test_video_without_a_bound_author_is_marked_source_not_exposed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, user: dict[str, str]
) -> None:
    def unexpected_download(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("unbound video media must not be staged")

    monkeypatch.setattr(xhs_bridge, "_download_media", unexpected_download)
    note = xhs_bridge._project_note(
        {
            "note": {
                "id": "note_video",
                "type": "video",
                "user": user,
                "interact_info": {},
                "video": {
                    "poster": "https://sns-webpic-qc.xhscdn.com/poster.jpg",
                    "url": "https://sns-video-qc.xhscdn.com/video.mp4",
                    "duration_ms": 1000,
                },
                "media_scope_valid": True,
                "media_discovered_count": 0,
            }
        },
        "note_video",
        1,
        tmp_path,
        MediaBudget(remaining_bytes=MAX_RUN_MEDIA_BYTES),
    )

    assert note is not None
    video = next(item for item in note.media_candidates if item.role == "video")
    assert video.status == "missing"
    assert video.missing_reason == "source_not_exposed"
    assert "sns-video-qc.xhscdn.com/video.mp4" not in note.model_dump_json()


def test_bridge_marks_discovery_above_the_represented_limit_truncated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    detail = _three_image_detail()
    note = detail["note"]
    assert isinstance(note, dict)
    note["media_discovered_count"] = MAX_DISCOVERED_IMAGE_SLOTS + 1
    note["image_list"] = [
        {"position": position, "url": "https://sns-webpic-qc.xhscdn.com/image.jpg"}
        for position in range(1, MAX_DISCOVERED_IMAGE_SLOTS + 1)
    ]
    monkeypatch.setattr(xhs_bridge, "_download_media", _fake_media_download)

    projected = xhs_bridge._project_note(
        detail, "note_image", 1, tmp_path, MediaBudget(remaining_bytes=MAX_RUN_MEDIA_BYTES)
    )

    assert projected is not None
    assert projected.media_discovered_count == MAX_DISCOVERED_IMAGE_SLOTS + 1
    assert projected.media_discovery_truncated is True
    assert len(projected.media_candidates) == MAX_DISCOVERED_IMAGE_SLOTS
    assert projected.media_candidates[-1].missing_reason == "slot_limit"


def test_media_budget_uses_the_smaller_run_remainder_without_opening_more_transports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed_limits: list[int] = []

    def download(
        _url: str, directory: Path, final_name: str, *, max_bytes: int, deadline: float
    ) -> tuple[MediaMime, int, str]:
        observed_limits.append(max_bytes)
        directory.joinpath(final_name).write_bytes(JPEG)
        return "image/jpeg", len(JPEG), hashlib.sha256(JPEG).hexdigest()

    raw_note = {
        "id": "note_image",
        "type": "normal",
        "media_discovered_count": 1,
        "image_list": [{"position": 1, "url": "https://sns-webpic-qc.xhscdn.com/image.jpg"}],
    }
    run_budget = MediaBudget(remaining_bytes=len(JPEG))
    monkeypatch.setattr(xhs_bridge, "_download_media", download)

    first = xhs_bridge._stage_note_media(raw_note, "note_image", tmp_path, run_budget)
    second = xhs_bridge._stage_note_media(raw_note, "note_image", tmp_path, run_budget)

    assert observed_limits == [len(JPEG)]
    assert first[0].status == "downloaded"
    assert second[0].missing_reason == "run_budget"


def test_media_budget_stops_the_ninth_maximum_image_for_one_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    def download(
        _url: str, directory: Path, final_name: str, *, max_bytes: int, deadline: float
    ) -> tuple[MediaMime, int, str]:
        nonlocal calls
        calls += 1
        assert max_bytes == MAX_IMAGE_BYTES
        directory.joinpath(final_name).write_bytes(JPEG)
        return "image/jpeg", MAX_IMAGE_BYTES, "a" * 64

    raw_note = {
        "id": "note_image",
        "type": "normal",
        "media_discovered_count": 9,
        "image_list": [
            {"position": position, "url": "https://sns-webpic-qc.xhscdn.com/image.jpg"}
            for position in range(1, 10)
        ],
    }
    monkeypatch.setattr(xhs_bridge, "_download_media", download)

    candidates = xhs_bridge._stage_note_media(
        raw_note, "note_image", tmp_path, MediaBudget(remaining_bytes=MAX_RUN_MEDIA_BYTES)
    )

    assert calls == 8
    assert candidates[-1].missing_reason == "note_budget"


class _StreamingResponse:
    def __init__(self, body: bytes, content_type: str, content_length: str | None = None) -> None:
        self._body = body
        self._content_type = content_type
        self._content_length = content_length
        self._offset = 0

    def getheader(self, name: str) -> str | None:
        if name == "Content-Type":
            return self._content_type
        if name == "Content-Length":
            return self._content_length
        return None

    def read(self, amount: int) -> bytes:
        chunk = self._body[self._offset : self._offset + amount]
        self._offset += len(chunk)
        return chunk


def _pinned_response(response: _StreamingResponse):
    @contextmanager
    def open_response(*_args: object, **_kwargs: object):
        yield response

    return open_response


def test_download_rejects_declared_mime_mismatch_without_leaving_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(xhs_bridge, "_validate_media_url", lambda _url: None)
    monkeypatch.setattr(
        xhs_bridge,
        "open_pinned_https",
        _pinned_response(_StreamingResponse(JPEG, "image/png", str(len(JPEG)))),
    )

    with pytest.raises(ValueError, match="media MIME/container mismatch"):
        xhs_bridge._download_media(
            "https://sns-webpic-qc.xhscdn.com/image.jpg",
            tmp_path,
            "note_image-image-001.jpg",
            max_bytes=MAX_IMAGE_BYTES,
            deadline=time.monotonic() + 2,
        )

    assert list(tmp_path.iterdir()) == []


def test_download_timeout_removes_the_temporary_and_final_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(xhs_bridge, "_validate_media_url", lambda _url: None)
    monkeypatch.setattr(
        xhs_bridge,
        "open_pinned_https",
        _pinned_response(_StreamingResponse(JPEG, "image/jpeg", str(len(JPEG)))),
    )

    with pytest.raises(TimeoutError, match="media budget exceeded"):
        xhs_bridge._download_media(
            "https://sns-webpic-qc.xhscdn.com/image.jpg",
            tmp_path,
            "note_image-image-001.jpg",
            max_bytes=MAX_IMAGE_BYTES,
            deadline=0,
        )

    assert list(tmp_path.iterdir()) == []


def test_download_fails_closed_when_the_staging_directory_is_a_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "target"
    target.mkdir()
    staging_link = tmp_path / "staging-link"
    staging_link.symlink_to(target, target_is_directory=True)
    monkeypatch.setattr(xhs_bridge, "_validate_media_url", lambda _url: None)

    with pytest.raises(OSError):
        xhs_bridge._download_media(
            "https://sns-webpic-qc.xhscdn.com/image.jpg",
            staging_link,
            "note_image-image-001.jpg",
            max_bytes=MAX_IMAGE_BYTES,
            deadline=time.monotonic() + 2,
        )

    assert list(target.iterdir()) == []


def test_bridge_protocol_accepts_only_the_new_finite_media_error_codes() -> None:
    assert (
        xhs_bridge.BridgeResponse(
            status="partial", payload={"notes": []}, error_code="media_partial"
        ).error_code
        == xhs_bridge.BridgeErrorCode.MEDIA_PARTIAL
    )
    assert (
        xhs_bridge.BridgeResponse(
            status="failed", payload={}, error_code="media_scope_invalid"
        ).error_code
        == xhs_bridge.BridgeErrorCode.MEDIA_SCOPE_INVALID
    )


@pytest.mark.parametrize("missing_field", ["media_manifest_version", "media_discovered_count"])
def test_bridge_rejects_a_payload_without_required_v2_manifest_fields(missing_field: str) -> None:
    payload: dict[str, object] = {
        "notes": [
            {
                "media_manifest_version": 2,
                "media_discovered_count": 0,
                "note_id": "note_image",
                "canonical_url": "https://www.xiaohongshu.com/explore/note_image",
                "metrics": {},
                "source_position": 1,
            }
        ]
    }
    notes = payload["notes"]
    assert isinstance(notes, list) and isinstance(notes[0], dict)
    notes[0].pop(missing_field)

    with pytest.raises(ValueError):
        xhs_bridge.validate_bridge_response(
            BridgeRequest(operation="search", keyword="AI workflow", limit=1),
            {"status": "complete", "payload": payload, "error_code": None},
        )


def test_slot_limited_image_without_a_source_is_rejected_before_transport(tmp_path: Path) -> None:
    raw_note = {
        "id": "note_image",
        "type": "normal",
        "media_discovered_count": 21,
        "image_list": [{"position": position, "url": None} for position in range(1, 22)],
    }

    candidates = xhs_bridge._stage_note_media(
        raw_note, "note_image", tmp_path, MediaBudget(remaining_bytes=MAX_RUN_MEDIA_BYTES)
    )

    assert candidates[-1].status == "rejected"
    assert candidates[-1].missing_reason == "slot_limit"


def test_download_expired_deadline_never_opens_a_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    @contextmanager
    def never_open(*_args: object, **_kwargs: object):
        nonlocal calls
        calls += 1
        yield _StreamingResponse(JPEG, "image/jpeg", str(len(JPEG)))

    monkeypatch.setattr(xhs_bridge, "_validate_media_url", lambda _url: None)
    monkeypatch.setattr(xhs_bridge, "open_pinned_https", never_open)

    with pytest.raises(TimeoutError, match="media budget exceeded"):
        xhs_bridge._download_media(
            "https://sns-webpic-qc.xhscdn.com/image.jpg",
            tmp_path,
            "note_image-image-001.jpg",
            max_bytes=MAX_IMAGE_BYTES,
            deadline=0,
        )

    assert calls == 0


def test_download_caps_transport_timeout_to_the_remaining_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    received_timeouts: list[object] = []

    @contextmanager
    def open_response(*_args: object, **kwargs: object):
        received_timeouts.append(kwargs["timeout"])
        yield _StreamingResponse(JPEG, "image/jpeg", str(len(JPEG)))

    monkeypatch.setattr(xhs_bridge, "_validate_media_url", lambda _url: None)
    monkeypatch.setattr(xhs_bridge, "open_pinned_https", open_response)
    monkeypatch.setattr(xhs_bridge.time, "monotonic", lambda: 100.0)

    xhs_bridge._download_media(
        "https://sns-webpic-qc.xhscdn.com/image.jpg",
        tmp_path,
        "note_image-image-001.jpg",
        max_bytes=MAX_IMAGE_BYTES,
        deadline=103.9,
    )

    assert received_timeouts == [3]


def test_url_policy_does_not_resolve_dns_before_the_pinned_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        pinned_https.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: pytest.fail("URL policy must not resolve DNS"),
    )

    xhs_bridge._validate_media_url("https://sns-webpic-qc.xhscdn.com/image.jpg?signature=ephemeral")


def test_avatar_cover_staging_uses_the_bounded_pinned_downloader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, int]] = []

    def download(
        source_url: str, directory: Path, final_name: str, *, max_bytes: int, deadline: float
    ) -> tuple[MediaMime, int, str]:
        calls.append((source_url, max_bytes))
        directory.joinpath(final_name).write_bytes(JPEG)
        return "image/jpeg", len(JPEG), hashlib.sha256(JPEG).hexdigest()

    monkeypatch.setattr(xhs_bridge, "_download_media", download)

    cover = xhs_bridge._stage_cover(
        "https://sns-webpic-qc.xhscdn.com/avatar.jpg", "author_a", tmp_path, suffix="avatar"
    )

    assert calls == [("https://sns-webpic-qc.xhscdn.com/avatar.jpg", MAX_IMAGE_BYTES)]
    assert cover is not None and cover.staging_name == "author_a-avatar.jpg"


def test_account_run_budget_includes_avatar_and_note_media(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A separate avatar budget would let this account exceed the run cap."""
    client = FakeClient()
    basic = client.profile["userPageData"]["basicInfo"]
    page_data = client.profile["userPageData"]
    assert isinstance(basic, dict)
    assert isinstance(page_data, dict)
    basic["image"] = "https://sns-webpic-qc.xhscdn.com/avatar.jpg"
    field_statuses = page_data["fieldStatuses"]
    assert isinstance(field_statuses, dict)
    field_statuses["avatar"] = "exposed"
    detail = client.details["note_image"]
    note = detail["note"]
    assert isinstance(note, dict)
    note["media_discovered_count"] = 1
    note["image_list"] = [{"position": 1, "url": "https://sns-webpic-qc.xhscdn.com/note.jpg"}]

    def download(
        _url: str, directory: Path, final_name: str, *, max_bytes: int, deadline: float
    ) -> tuple[MediaMime, int, str]:
        assert max_bytes > 0
        assert deadline > time.monotonic()
        directory.joinpath(final_name).write_bytes(JPEG)
        return "image/jpeg", len(JPEG), hashlib.sha256(JPEG).hexdigest()

    monkeypatch.setattr(xhs_bridge, "MAX_RUN_MEDIA_BYTES", len(JPEG))
    monkeypatch.setattr(xhs_bridge, "_download_media", download)

    response = run_bridge(
        BridgeRequest(operation="account", account_id="author_a", limit=1),
        lambda _cookie: client,
        staging_dir=tmp_path,
    )

    assert response.status == "partial"
    avatar = response.payload["avatar"]
    assert isinstance(avatar, dict) and avatar["size_bytes"] == len(JPEG)
    notes = response.payload["notes"]
    assert isinstance(notes, list) and len(notes) == 1
    candidate = notes[0]["media_candidates"][0]
    assert candidate["status"] == "rejected"
    assert candidate["missing_reason"] == "run_budget"


def test_search_uses_ephemeral_detail_token_and_returns_only_projected_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = FakeClient()
    staging = tmp_path / "staging"
    staging.mkdir()

    response = run_bridge(
        BridgeRequest(operation="search", keyword="AI workflow", limit=1),
        lambda _cookie: client,
        staging_dir=staging,
    )

    assert response.status == "complete"
    assert client.detail_calls == [("note_image", "ephemeral-only")]
    notes = response.payload["notes"]
    assert isinstance(notes, list) and len(notes) == 1
    note = notes[0]
    assert isinstance(note, dict)
    assert note["note_id"] == "note_image"
    assert note["author_profile_url"] == "https://www.xiaohongshu.com/user/profile/author_a"
    assert note["metrics"] == {
        "likes": {"raw_value": "12", "normalized_value": 12, "precision": "exact"},
        "collects": {"raw_value": "3", "normalized_value": 3, "precision": "exact"},
        "comments": {"raw_value": "1", "normalized_value": 1, "precision": "exact"},
        "shares": {"raw_value": "0", "normalized_value": 0, "precision": "exact"},
    }
    assert note["media_manifest_version"] == 2
    assert note["media_candidates"] == []
    serialized = response.model_dump_json()
    assert "ephemeral-only" not in serialized
    assert "example.invalid" not in serialized
    assert str(staging) not in serialized


def test_note_projection_does_not_retain_the_legacy_card_cover(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(xhs_bridge, "_download_media", _fake_media_download)

    note = xhs_bridge._project_note(
        {
            "note": {
                "id": "note_image",
                "cover": {"url": "https://example.invalid/exact-card-cover.jpg"},
                "media_scope_valid": True,
                "media_discovered_count": 1,
                "image_list": [
                    {"position": 1, "url": "https://sns-webpic-qc.xhscdn.com/image.jpg"}
                ],
                "interact_info": {},
            }
        },
        "note_image",
        1,
        tmp_path,
    )

    assert note is not None
    assert note.cover is None
    assert note.media_candidates[0].staging_name == "note_image-image-001.jpg"


@pytest.mark.parametrize(
    ("source", "expected"),
    (
        (
            "http://sns-webpic-qc.xhscdn.com/path/cover.webp?imageView2=2",
            "https://sns-webpic-qc.xhscdn.com/path/cover.webp?imageView2=2",
        ),
        ("http://example.invalid/cover.webp", None),
        ("http://user@sns-webpic-qc.xhscdn.com/cover.webp", None),
        ("http://sns-webpic-qc.xhscdn.com:8080/cover.webp", None),
    ),
)
def test_cover_source_only_upgrades_plain_http_on_an_allowed_xhs_media_host(
    source: str, expected: str | None
) -> None:
    assert xhs_bridge._cover_source({"cover": {"url": source}}) == expected


def test_search_keeps_a_confirmed_empty_candidate_response_complete(tmp_path: Path) -> None:
    class EmptySearchClient(FakeClient):
        def search_notes(self, keyword: str) -> object:
            assert keyword == "AI workflow"
            return []

    response = run_bridge(
        BridgeRequest(operation="search", keyword="AI workflow", limit=1),
        lambda _cookie: EmptySearchClient(),
        staging_dir=tmp_path,
    )

    assert response.status == "complete"
    assert response.payload == {"notes": []}
    assert response.error_code is None


def test_search_fails_safe_when_the_client_rejects_an_untrusted_response(tmp_path: Path) -> None:
    class NoTrustedResponseClient(FakeClient):
        def search_notes(self, keyword: str) -> object:
            raise RuntimeError("search response unavailable")

    response = run_bridge(
        BridgeRequest(operation="search", keyword="AI workflow", limit=1),
        lambda _cookie: NoTrustedResponseClient(),
        staging_dir=tmp_path,
    )

    assert response.status == "failed"
    assert response.payload == {}
    assert response.error_code == "upstream_error"


def test_bridge_preserves_exact_upstream_integer_timestamp(tmp_path: Path) -> None:
    note_id = "6553F1000000000000000000"
    note = xhs_bridge._project_note(
        {
            "note": {
                "id": note_id,
                "time": 1_700_000_001,
                "interact_info": {},
                "media_scope_valid": True,
                "media_discovered_count": 0,
            }
        },
        note_id,
        1,
        tmp_path,
    )

    assert note is not None
    assert note.published_at == datetime(2023, 11, 14, 22, 13, 21, tzinfo=UTC)


@pytest.mark.parametrize(
    "time_fields",
    (
        {"time": "06-27"},
        {},
        {"time": "unavailable"},
        {"time": 10**100},
        {"time": True},
    ),
    ids=("partial", "missing", "malformed", "overflow", "boolean"),
)
def test_bridge_leaves_unavailable_visible_time_absent_for_valid_hex_note_id(
    tmp_path: Path, time_fields: dict[str, object]
) -> None:
    note_id = "6553F1000000000000000000"
    note = xhs_bridge._project_note(
        {
            "note": {
                "id": note_id,
                **time_fields,
                "interact_info": {},
                "media_scope_valid": True,
                "media_discovered_count": 0,
            }
        },
        note_id,
        1,
        tmp_path,
    )

    assert note is not None
    assert note.published_at is None


def test_bridge_projects_current_shared_count_alias_as_exact_shares_metric(tmp_path: Path) -> None:
    note = xhs_bridge._project_note(
        {
            "note": {
                "id": "shared_alias_note",
                "interact_info": {"shared_count": "17"},
                "media_scope_valid": True,
                "media_discovered_count": 0,
            }
        },
        "shared_alias_note",
        1,
        tmp_path,
    )

    assert note is not None
    assert note.metrics.shares is not None
    assert note.metrics.shares.model_dump() == {
        "raw_value": "17",
        "normalized_value": 17,
        "precision": "exact",
    }


@pytest.mark.parametrize(
    "visible_count",
    ("unsupported", "-1", -1),
    ids=("unparseable", "negative-text", "negative-integer"),
)
def test_bridge_keeps_unexposed_visible_metric_without_detail_provenance(
    tmp_path: Path, visible_count: object
) -> None:
    note = xhs_bridge._project_note(
        {
            "note": {
                "id": "unexposed_metric_note",
                "interact_info": {"liked_count": visible_count},
                "metric_provenance": {"likes": "detail_visible_count"},
                "media_scope_valid": True,
                "media_discovered_count": 0,
            }
        },
        "unexposed_metric_note",
        1,
        tmp_path,
    )

    assert note is not None
    assert note.metrics.likes is not None
    assert note.metrics.likes.precision == "not_exposed"
    assert note.metric_provenance == {}


def test_bridge_preserves_visible_time_evidence_and_search_share_provenance(tmp_path: Path) -> None:
    note = xhs_bridge._project_note(
        {
            "note": {
                "id": "shared_evidence_note",
                "time": None,
                "time_evidence": {"kind": "edited", "raw_text": "编辑于 08-12"},
                "interact_info": {
                    "liked_count": "12",
                    "shared_count": "17",
                },
                "metric_provenance": {
                    "likes": "detail_visible_count",
                    "shares": "search_card_interface",
                },
                "media_scope_valid": True,
                "media_discovered_count": 0,
            }
        },
        "shared_evidence_note",
        1,
        tmp_path,
    )

    assert note is not None
    assert note.published_at is None
    assert note.time_evidence is not None
    assert note.time_evidence.model_dump() == {"kind": "edited", "raw_text": "编辑于 08-12"}
    assert note.metric_provenance == {
        "likes": "detail_visible_count",
        "shares": "search_card_interface",
    }


@pytest.mark.parametrize(
    "raw_text",
    (
        "https://example.invalid/time?token=secret",
        "xsec_token=secret",
        "\u200b",
    ),
    ids=("url", "token-like", "invisible"),
)
def test_bridge_drops_unsafe_time_evidence_without_dropping_the_note(
    tmp_path: Path, raw_text: str
) -> None:
    note = xhs_bridge._project_note(
        {
            "note": {
                "id": "unsafe_time_note",
                "time": None,
                "time_evidence": {"kind": "unknown", "raw_text": raw_text},
                "interact_info": {},
                "media_scope_valid": True,
                "media_discovered_count": 0,
            }
        },
        "unsafe_time_note",
        1,
        tmp_path,
    )

    assert note is not None
    assert note.published_at is None
    assert note.time_evidence is None


@pytest.mark.parametrize(
    "note_id",
    [
        "6553F100000000000000000",
        "zzzzzzzz0000000000000000",
        "000000000000000000000000",
        "FFFFFFFF0000000000000000",
    ],
)
def test_bridge_leaves_partial_time_missing_for_invalid_note_ids(
    tmp_path: Path, note_id: str
) -> None:
    note = xhs_bridge._project_note(
        {
            "note": {
                "id": note_id,
                "time": "06-27",
                "interact_info": {},
                "media_scope_valid": True,
                "media_discovered_count": 0,
            }
        },
        note_id,
        1,
        tmp_path,
    )

    assert note is not None
    assert note.published_at is None


@pytest.mark.parametrize(
    "unsafe_author_id",
    [
        "access_token=demo-value",
        "https://example.invalid/profile?signature=demo-value",
        "opaque id",
        "../outside",
        "author/profile",
        "\t",
    ],
)
def test_bridge_drops_unsafe_author_ids_before_response_serialization(
    tmp_path: Path, unsafe_author_id: str
) -> None:
    client = FakeClient()
    detail = client.details["note_image"]
    assert isinstance(detail["note"], dict)
    user = detail["note"]["user"]
    assert isinstance(user, dict)
    user["userId"] = unsafe_author_id

    response = run_bridge(
        BridgeRequest(operation="search", keyword="AI workflow", limit=1),
        lambda _cookie: client,
        staging_dir=tmp_path,
    )

    notes = response.payload["notes"]
    assert isinstance(notes, list) and len(notes) == 1
    note = notes[0]
    assert isinstance(note, dict)
    assert "author_id" not in note
    assert "author_profile_url" not in note
    assert unsafe_author_id not in response.model_dump_json()


def test_bridge_keeps_safe_opaque_author_id_and_its_profile_url(tmp_path: Path) -> None:
    client = FakeClient()
    detail = client.details["note_image"]
    assert isinstance(detail["note"], dict)
    user = detail["note"]["user"]
    assert isinstance(user, dict)
    user["userId"] = "opaque_A-1"

    response = run_bridge(
        BridgeRequest(operation="search", keyword="AI workflow", limit=1),
        lambda _cookie: client,
        staging_dir=tmp_path,
    )

    notes = response.payload["notes"]
    assert isinstance(notes, list) and len(notes) == 1
    note = notes[0]
    assert isinstance(note, dict)
    assert note["author_id"] == "opaque_A-1"
    assert note["author_profile_url"] == "https://www.xiaohongshu.com/user/profile/opaque_A-1"


def test_search_marks_a_mismatched_upstream_detail_partial(tmp_path: Path) -> None:
    client = FakeClient()
    bad_detail = _fixture("detail_image.json")
    assert isinstance(bad_detail["note"], dict)
    bad_detail["note"]["noteId"] = "different_note"
    bad_detail["note"]["media_scope_valid"] = True
    bad_detail["note"]["media_discovered_count"] = 0
    client.details["note_image"] = bad_detail

    response = run_bridge(
        BridgeRequest(operation="search", keyword="AI workflow", limit=1),
        lambda _cookie: client,
        staging_dir=tmp_path,
    )

    assert response.status == "partial"
    assert response.payload == {"notes": []}
    assert response.error_code == "detail_id_mismatch"


def test_search_retains_other_notes_when_one_detail_call_fails(tmp_path: Path) -> None:
    class PartiallyFailingClient(FakeClient):
        def get_note_detail(self, note_id: str, xsec_token: str = "") -> object:
            if note_id == "note_image":
                raise RuntimeError("upstream detail diagnostic")
            return super().get_note_detail(note_id, xsec_token)

    response = run_bridge(
        BridgeRequest(operation="search", keyword="AI workflow", limit=2),
        lambda _cookie: PartiallyFailingClient(),
        staging_dir=tmp_path,
    )

    assert response.status == "partial"
    assert response.error_code == "detail_unavailable"
    notes = response.payload["notes"]
    assert isinstance(notes, list) and len(notes) == 1
    assert notes[0]["note_id"] == "note_video"
    assert notes[0]["author_profile_url"] == "https://www.xiaohongshu.com/user/profile/author_b"
    assert notes[0]["metrics"]["likes"] == {
        "raw_value": "8",
        "normalized_value": 8,
        "precision": "exact",
    }
    assert "upstream detail diagnostic" not in response.model_dump_json()


def test_account_projects_profile_and_first_page_note_details(tmp_path: Path) -> None:
    client = FakeClient()
    image_detail = client.details["note_image"]
    assert isinstance(image_detail["note"], dict)
    image_detail["note"]["imageList"] = []
    response = run_bridge(
        BridgeRequest(operation="account", account_id="author_a", limit=2),
        lambda _cookie: client,
        staging_dir=tmp_path,
    )

    assert response.status == "complete"
    account = response.payload["account"]
    assert isinstance(account, dict)
    assert account["account_id"] == "author_a"
    assert account["profile_url"] == "https://www.xiaohongshu.com/user/profile/author_a"
    assert account["name"] == "Fixture author"
    assert account["bio"] == "Fixture bio"
    assert [item[0] for item in client.detail_calls] == ["note_image", "note_video"]
    assert "xsec_token" not in response.model_dump_json()


def test_account_ledger_freezes_requested_positions_and_retains_detail_outcomes(
    tmp_path: Path,
) -> None:
    """Dropping an early position would falsely make a later note look complete."""

    class LedgerClient(FakeClient):
        def get_note_detail(self, note_id: str, xsec_token: str = "") -> object:
            self.detail_calls.append((note_id, xsec_token))
            if note_id == "note_timeout":
                raise DetailReadFailure("detail_navigation", "detail_timeout")
            return self.details[note_id]

    client = LedgerClient()
    scope_invalid = _fixture("detail_image.json")
    scope_note = scope_invalid["note"]
    assert isinstance(scope_note, dict)
    scope_note["id"] = "note_scope"
    scope_note["media_scope_valid"] = False
    client.details["note_scope"] = scope_invalid
    client.posts = [
        {"id": "note_image"},
        {"id": "note_timeout"},
        {"id": "note_scope"},
        {"id": "unsafe/note"},
    ]

    response = run_bridge(
        BridgeRequest(operation="account", account_id="author_a", limit=5),
        lambda _profile: client,
        staging_dir=tmp_path,
    )

    assert response.status == "partial"
    assert response.error_code == "media_scope_invalid"
    payload = response.payload
    assert payload["requested_count"] == 5
    attempts = payload["candidate_attempts"]
    assert isinstance(attempts, list)
    assert [item["position"] for item in attempts] == [1, 2, 3, 4, 5]
    assert attempts == [
        {
            "position": 1,
            "note_id": "note_image",
            "outcome": "complete",
            "stage": "detail_projection",
        },
        {
            "position": 2,
            "note_id": "note_timeout",
            "outcome": "unavailable",
            "stage": "detail_navigation",
            "reason": "detail_timeout",
        },
        {
            "position": 3,
            "note_id": "note_scope",
            "outcome": "rejected",
            "stage": "detail_projection",
            "reason": "media_scope_invalid",
        },
        {
            "position": 4,
            "outcome": "rejected",
            "stage": "candidate",
            "reason": "candidate_invalid",
        },
        {
            "position": 5,
            "outcome": "unavailable",
            "stage": "candidate",
            "reason": "candidate_shortfall",
        },
    ]
    notes = payload["notes"]
    assert isinstance(notes, list)
    assert [note["source_position"] for note in notes] == [1]
    assert client.detail_calls == [
        ("note_image", ""),
        ("note_timeout", ""),
        ("note_scope", ""),
    ]
    assert payload["pacing_summary"] == {
        "policy": "conservative_jitter_v1",
        "profile_open_delay_ms": 2_000,
        "detail_delay_ms": [3_000, 3_000, 3_000],
    }


def test_account_candidate_invalid_retains_safe_id_without_detail_or_pacing_delay(
    tmp_path: Path,
) -> None:
    """Treating malformed token data as tokenless would make an unsafe detail request."""
    client = FakeClient()
    client.posts = [
        {"id": "note_image", "xsec_token": 7},
        {"id": "note_video"},
    ]

    response = run_bridge(
        BridgeRequest(operation="account", account_id="author_a", limit=2),
        lambda _profile: client,
        staging_dir=tmp_path,
    )

    assert response.status == "partial"
    payload = response.payload
    attempts = payload["candidate_attempts"]
    assert isinstance(attempts, list)
    assert attempts[0] == {
        "position": 1,
        "note_id": "note_image",
        "outcome": "rejected",
        "stage": "candidate",
        "reason": "candidate_invalid",
    }
    assert [item["source_position"] for item in payload["notes"]] == [2]
    assert client.detail_calls == [("note_video", "")]
    assert payload["pacing_summary"]["detail_delay_ms"] == [3_000]


def test_account_shortfall_keeps_the_final_three_positions_unavailable(tmp_path: Path) -> None:
    """Filling a short first page with later records would change the requested evidence set."""
    client = FakeClient()
    client.posts = [{"id": "note_image"}, {"id": "note_video"}]

    response = run_bridge(
        BridgeRequest(operation="account", account_id="author_a", limit=5),
        lambda _profile: client,
        staging_dir=tmp_path,
    )

    payload = response.payload
    assert payload["requested_count"] == 5
    attempts = payload["candidate_attempts"]
    assert isinstance(attempts, list)
    assert [item["position"] for item in attempts] == [1, 2, 3, 4, 5]
    assert attempts[2] == {
        "position": 3,
        "outcome": "unavailable",
        "stage": "candidate",
        "reason": "candidate_shortfall",
    }
    assert [note["source_position"] for note in payload["notes"]] == [1, 2]


def test_account_fails_empty_without_a_pacing_summary_when_every_safe_detail_is_scope_invalid(
    tmp_path: Path,
) -> None:
    """A candidate failure must not mask the all-scope-invalid bridge failure."""
    client = FakeClient()
    scope_invalid = _fixture("detail_image.json")
    scope_note = scope_invalid["note"]
    assert isinstance(scope_note, dict)
    scope_note["media_scope_valid"] = False
    client.details["note_image"] = scope_invalid
    video_invalid = _fixture("detail_video.json")
    video_note = video_invalid["note"]
    assert isinstance(video_note, dict)
    video_invalid["media_scope_valid"] = False
    client.details["note_video"] = video_invalid
    client.posts = [{"id": "note_image"}, {"id": "note_video"}]

    response = run_bridge(
        BridgeRequest(operation="account", account_id="author_a", limit=2),
        lambda _profile: client,
        staging_dir=tmp_path,
    )

    assert response.status == "failed"
    assert response.error_code == "media_scope_invalid"
    assert response.payload == {}
    assert client.pacing_summary_calls == 0


def test_search_payload_and_positions_remain_without_the_account_attempt_ledger(
    tmp_path: Path,
) -> None:
    """Sharing account slots with search would expose account-only protocol fields."""
    client = FakeClient()

    response = run_bridge(
        BridgeRequest(operation="search", keyword="AI workflow", limit=2),
        lambda _profile: client,
        staging_dir=tmp_path,
    )

    assert response.status == "complete"
    assert set(response.payload) == {"notes"}
    assert "candidate_attempts" not in response.model_dump_json()
    notes = response.payload["notes"]
    assert isinstance(notes, list)
    assert [note["source_position"] for note in notes] == [1, 2]


def test_account_projects_pinned_upstream_interaction_lists_with_direct_precedence(
    tmp_path: Path,
) -> None:
    profile = _fixture("profile_interactions.json")
    page_data = profile["userPageData"]
    assert isinstance(page_data, dict)
    page_data["fieldStatuses"] = {
        "name": "exposed",
        "bio": "exposed",
        "note_count": "exposed",
        "follower_count": "exposed",
        "avatar": "not_exposed",
        "platform_metrics": "exposed",
    }
    account, _avatar, code = xhs_bridge._project_profile(profile, "author_a", tmp_path)

    assert code is None
    assert account is not None
    assert account.name == "Metric fixture author"
    assert account.bio == "Metric fixture bio"
    assert account.follower_count is not None
    assert account.follower_count.model_dump() == {
        "raw_value": "12,345",
        "normalized_value": 12345,
        "precision": "exact",
    }
    assert account.note_count is not None
    assert account.note_count.model_dump() == {
        "raw_value": "7",
        "normalized_value": 7,
        "precision": "exact",
    }
    assert account.platform_metrics["interaction"].model_dump() == {
        "raw_value": "1.2万",
        "normalized_value": 12000,
        "precision": "display_rounded",
    }
    assert account.platform_metrics["获赞与收藏"].model_dump() == {
        "raw_value": "2.3万",
        "normalized_value": 23000,
        "precision": "display_rounded",
    }


def test_account_projects_top_level_interactions_and_skips_unsafe_labels(tmp_path: Path) -> None:
    profile = _fixture("profile_interactions.json")
    page_data = profile["userPageData"]
    assert isinstance(page_data, dict)
    page_data["fieldStatuses"] = {
        "name": "exposed",
        "bio": "exposed",
        "note_count": "exposed",
        "follower_count": "exposed",
        "avatar": "not_exposed",
        "platform_metrics": "exposed",
    }
    page_data.pop("interactions")
    profile["interactions"] = [
        {"name": "粉丝", "count": "8"},
        {"type": "notes", "value": "3"},
        {"name": "access-token", "count": "999"},
        {"name": "", "count": "2"},
    ]

    account, _avatar, code = xhs_bridge._project_profile(profile, "author_a", tmp_path)

    assert code is xhs_bridge.BridgeErrorCode.PROFILE_UNAVAILABLE
    assert account is not None
    assert account.follower_count is not None and account.follower_count.normalized_value == 8
    assert account.note_count is not None and account.note_count.normalized_value == 3
    assert "access-token" not in account.platform_metrics
    assert account.field_statuses["platform_metrics"] == "unavailable"


def test_profile_field_statuses_preserve_empty_bio_and_do_not_infer_note_count(
    tmp_path: Path,
) -> None:
    profile = {
        "userPageData": {
            "basicInfo": {"userId": "author_a", "nickname": "Visible author", "desc": "", "noteCount": "999"},
            "interactions": [{"name": "粉丝", "count": "8"}],
            "fieldStatuses": {
                "name": "exposed",
                "bio": "exposed_empty",
                "note_count": "not_exposed",
                "follower_count": "exposed",
                "avatar": "not_exposed",
                "platform_metrics": "exposed",
            },
        }
    }

    account, avatar, code = xhs_bridge._project_profile(profile, "author_a", tmp_path)

    assert code is None
    assert avatar is None
    assert account is not None
    assert account.bio == ""
    assert account.note_count is None
    assert account.follower_count is not None and account.follower_count.normalized_value == 8
    assert account.field_statuses == {
        "name": "exposed",
        "bio": "exposed_empty",
        "note_count": "not_exposed",
        "follower_count": "exposed",
        "avatar": "not_exposed",
        "platform_metrics": "exposed",
    }


def test_profile_field_statuses_report_unavailable_profile(tmp_path: Path) -> None:
    profile = {
        "userPageData": {
            "basicInfo": {"userId": "author_a", "nickname": ""},
            "fieldStatuses": {
                "name": "unavailable",
                "bio": "not_exposed",
                "note_count": "not_exposed",
                "follower_count": "not_exposed",
                "avatar": "not_exposed",
                "platform_metrics": "not_exposed",
            },
        }
    }

    account, avatar, code = xhs_bridge._project_profile(profile, "author_a", tmp_path)

    assert account is None
    assert avatar is None
    assert code is xhs_bridge.BridgeErrorCode.PROFILE_UNAVAILABLE


def test_profile_field_statuses_allow_not_exposed_and_avatar_absence(tmp_path: Path) -> None:
    profile = {
        "userPageData": {
            "basicInfo": {"userId": "author_a", "nickname": "Visible author", "desc": ""},
            "fieldStatuses": {
                "name": "exposed",
                "bio": "exposed_empty",
                "note_count": "not_exposed",
                "follower_count": "not_exposed",
                "avatar": "not_exposed",
                "platform_metrics": "not_exposed",
            },
        }
    }

    account, avatar, code = xhs_bridge._project_profile(profile, "author_a", tmp_path)

    assert code is None
    assert account is not None
    assert avatar is None
    assert account.field_statuses["avatar"] == "not_exposed"


def test_profile_avatar_staging_failure_downgrades_exposed_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = {
        "userPageData": {
            "basicInfo": {
                "userId": "author_a",
                "nickname": "Visible author",
                "image": "https://sns-webpic-qc.xhscdn.com/avatar.jpg",
            },
            "fieldStatuses": {
                "name": "exposed",
                "bio": "not_exposed",
                "note_count": "not_exposed",
                "follower_count": "not_exposed",
                "avatar": "exposed",
                "platform_metrics": "not_exposed",
            },
        }
    }

    monkeypatch.setattr(xhs_bridge, "_stage_cover", lambda *_args, **_kwargs: None)
    account, avatar, code = xhs_bridge._project_profile(profile, "author_a", tmp_path)

    assert account is not None
    assert avatar is None
    assert account.field_statuses["avatar"] == "unavailable"
    assert code is xhs_bridge.BridgeErrorCode.PROFILE_UNAVAILABLE


@pytest.mark.parametrize(
    ("basic", "interactions", "statuses", "fields"),
    (
        (
            {"nickname": "https://example.invalid/name?signature=secret"},
            [],
            {
                "name": "exposed",
                "bio": "not_exposed",
                "note_count": "not_exposed",
                "follower_count": "not_exposed",
                "avatar": "not_exposed",
                "platform_metrics": "not_exposed",
            },
            ("name",),
        ),
        (
            {
                "nickname": "Visible author",
                "desc": "https://example.invalid/bio?signature=secret",
            },
            [],
            {
                "name": "exposed",
                "bio": "exposed",
                "note_count": "not_exposed",
                "follower_count": "not_exposed",
                "avatar": "not_exposed",
                "platform_metrics": "not_exposed",
            },
            ("bio",),
        ),
        (
            {"nickname": "Visible author"},
            [{"name": "access-token", "count": "8"}],
            {
                "name": "exposed",
                "bio": "not_exposed",
                "note_count": "exposed",
                "follower_count": "not_exposed",
                "avatar": "not_exposed",
                "platform_metrics": "exposed",
            },
            ("note_count", "platform_metrics"),
        ),
    ),
)
def test_profile_safe_filtering_downgrades_unretained_exposed_evidence(
    tmp_path: Path,
    basic: dict[str, str],
    interactions: list[dict[str, str]],
    statuses: dict[str, str],
    fields: tuple[str, ...],
) -> None:
    profile = {
        "userPageData": {
            "basicInfo": {"userId": "author_a", **basic},
            "interactions": interactions,
            "fieldStatuses": statuses,
        }
    }

    account, _avatar, code = xhs_bridge._project_profile(profile, "author_a", tmp_path)

    assert account is not None
    assert all(account.field_statuses[field] == "unavailable" for field in fields)
    assert code is xhs_bridge.BridgeErrorCode.PROFILE_UNAVAILABLE


@pytest.mark.parametrize(
    ("interaction", "statuses", "expected_fields"),
    (
        (
            {"name": "notes", "count": "not-a-metric"},
            {"note_count": "exposed", "follower_count": "not_exposed"},
            ("note_count", "platform_metrics"),
        ),
        (
            {"name": "followers", "count": "not-a-metric"},
            {"note_count": "not_exposed", "follower_count": "exposed"},
            ("follower_count", "platform_metrics"),
        ),
        (
            {"name": "获赞与收藏", "count": "not-a-metric"},
            {"note_count": "not_exposed", "follower_count": "not_exposed"},
            ("platform_metrics",),
        ),
    ),
)
def test_profile_invalid_visible_metric_downgrades_exposed_field_evidence(
    tmp_path: Path,
    interaction: dict[str, str],
    statuses: dict[str, str],
    expected_fields: tuple[str, ...],
) -> None:
    profile = {
        "userPageData": {
            "basicInfo": {"userId": "author_a", "nickname": "Visible author"},
            "interactions": [interaction],
            "fieldStatuses": {
                "name": "exposed",
                "bio": "not_exposed",
                **statuses,
                "avatar": "not_exposed",
                "platform_metrics": "exposed",
            },
        }
    }

    account, _avatar, code = xhs_bridge._project_profile(profile, "author_a", tmp_path)

    assert account is not None
    assert all(account.field_statuses[field] == "unavailable" for field in expected_fields)
    assert code is xhs_bridge.BridgeErrorCode.PROFILE_UNAVAILABLE


def test_auth_directory_redirection_never_targets_default_home(tmp_path: Path) -> None:
    class AuthModule:
        CONFIG_DIR = Path("/unsafe/default")
        COOKIE_FILE = Path("/unsafe/default/cookies.json")
        TOKEN_CACHE_FILE = Path("/unsafe/default/token_cache.json")

    auth_dir = tmp_path / "auth"
    configure_auth_directory(AuthModule, auth_dir)

    assert AuthModule.CONFIG_DIR == auth_dir.resolve()
    assert AuthModule.COOKIE_FILE == auth_dir.resolve() / "cookies.json"
    assert AuthModule.TOKEN_CACHE_FILE == auth_dir.resolve() / "token_cache.json"


def test_bridge_main_discards_upstream_console_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class PrintingClient(FakeClient):
        def __init__(self) -> None:
            super().__init__()
            image_detail = self.details["note_image"]
            assert isinstance(image_detail["note"], dict)
            image_detail["note"]["imageList"] = []

        def search_notes(self, keyword: str) -> object:
            print("untrusted upstream diagnostic")
            return super().search_notes(keyword)

    auth_dir = tmp_path / "auth"
    auth_dir.mkdir(mode=0o700)
    (auth_dir / "cookies.json").write_text(
        '{"cookies":{"a1":"private-a1","web_session":"private-session"}}'
    )
    (auth_dir / "cookies.json").chmod(0o600)

    output = io.StringIO()
    input_stream = io.TextIOWrapper(
        io.BytesIO(b'{"operation":"search","keyword":"AI workflow","limit":1}'),
        encoding="utf-8",
    )
    monkeypatch.setattr(xhs_bridge, "_default_client_factory", lambda _cookie: PrintingClient())
    monkeypatch.setitem(os.environ, "XHS_WORKBENCH_AUTH_DIR", str(tmp_path / "auth"))
    monkeypatch.setitem(os.environ, "XHS_WORKBENCH_ASSET_STAGING_DIR", str(tmp_path))
    monkeypatch.setattr(xhs_bridge.sys, "stdin", input_stream)
    monkeypatch.setattr(xhs_bridge.sys, "stdout", output)

    xhs_bridge.main()

    response = json.loads(output.getvalue())
    assert response["status"] == "complete"
    assert "untrusted upstream diagnostic" not in output.getvalue()


def test_bridge_main_passes_only_the_fixed_project_profile_to_its_client_factory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    auth_dir = tmp_path / "auth"
    auth_dir.mkdir(mode=0o700)
    (auth_dir / "cookies.json").write_text(
        '{"cookies":{"a1":"private-a1","web_session":"private-session"}}'
    )
    (auth_dir / "cookies.json").chmod(0o600)
    received_profiles: list[object] = []

    def client_factory(profile_dir: object) -> FakeClient:
        received_profiles.append(profile_dir)
        return FakeClient()

    output = io.StringIO()
    input_stream = io.TextIOWrapper(
        io.BytesIO(b'{"operation":"search","keyword":"AI workflow","limit":1}'),
        encoding="utf-8",
    )
    monkeypatch.setattr(xhs_bridge, "_default_client_factory", client_factory)
    monkeypatch.setitem(os.environ, "XHS_WORKBENCH_AUTH_DIR", str(auth_dir))
    monkeypatch.setitem(os.environ, "XHS_WORKBENCH_ASSET_STAGING_DIR", str(tmp_path))
    monkeypatch.setattr(xhs_bridge.sys, "stdin", input_stream)
    monkeypatch.setattr(xhs_bridge.sys, "stdout", output)

    xhs_bridge.main()

    assert received_profiles == [auth_dir / "browser-profile"]


def test_bridge_source_and_fixtures_expose_no_mutation_or_secret_surface() -> None:
    source = (Path(__file__).parents[1] / "src/xhs_workbench/xhs_bridge.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    imported_modules = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    assert "xhs_cli" + ".cli" not in imported_modules
    defined_names = {
        node.name.lower()
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    for forbidden in ("like", "favorite", "follow", "comment", "delete"):
        assert not any(forbidden in name for name in defined_names)
    for path in FIXTURES.glob("*.json"):
        payload = path.read_text(encoding="utf-8").lower()
        assert "xsec" not in payload
        assert "cookie" not in payload
        assert "access_token" not in payload
        assert "?" not in payload


def test_search_loads_redirected_cookie_and_manages_client_lifecycle(tmp_path: Path) -> None:
    auth_dir = tmp_path / "auth"
    auth_dir.mkdir(mode=0o700)
    (auth_dir / "cookies.json").write_text("{}")
    (auth_dir / "cookies.json").chmod(0o600)

    class AuthModule:
        CONFIG_DIR = auth_dir
        COOKIE_FILE = CONFIG_DIR / "cookies.json"
        TOKEN_CACHE_FILE = CONFIG_DIR / "token_cache.json"
        REQUIRED_COOKIES: ClassVar[frozenset[str]] = frozenset({"a1", "web_session"})

        @staticmethod
        def get_saved_cookie_string() -> str:
            return "a1=private-a1; web_session=private-session"

    class LifecycleClient(FakeClient):
        entered = False
        exited = False

        def __enter__(self) -> Self:
            self.entered = True
            return self

        def __exit__(self, *_args: object) -> bool:
            self.exited = True
            return False

    received_profiles: list[Path] = []
    client = LifecycleClient()
    image_detail = client.details["note_image"]
    assert isinstance(image_detail["note"], dict)
    image_detail["note"]["imageList"] = []

    def factory(profile_dir: Path) -> LifecycleClient:
        received_profiles.append(profile_dir)
        return client

    response = run_bridge(
        BridgeRequest(operation="search", keyword="AI workflow", limit=1),
        factory,
        staging_dir=tmp_path,
        auth_module=AuthModule,
    )

    assert response.status == "complete"
    assert received_profiles == [auth_dir / "browser-profile"]
    assert client.entered is True
    assert client.exited is True
    assert "private-a1" not in response.model_dump_json()
    assert "private-session" not in response.model_dump_json()


def test_login_and_status_require_valid_private_cookie_storage(tmp_path: Path) -> None:
    auth_dir = tmp_path / "auth"
    auth_dir.mkdir(mode=0o700)
    cookie_file = auth_dir / "cookies.json"
    cookie_file.write_text('{"cookies":{"a1":"private-a1","web_session":"private-session"}}')
    cookie_file.chmod(0o600)

    class AuthModule:
        CONFIG_DIR = auth_dir
        COOKIE_FILE = cookie_file
        TOKEN_CACHE_FILE = auth_dir / "token_cache.json"
        REQUIRED_COOKIES: ClassVar[frozenset[str]] = frozenset({"a1", "web_session"})
        saved_calls = 0
        browser_extraction_calls = 0

        @classmethod
        def get_saved_cookie_string(cls) -> str:
            cls.saved_calls += 1
            return "a1=private-a1; web_session=private-session"

        @classmethod
        def get_cookie_string(cls) -> str:
            cls.browser_extraction_calls += 1
            raise AssertionError("system browser extraction must never run")

    status = run_bridge(
        BridgeRequest(operation="status"), lambda _cookie: FakeClient(), auth_module=AuthModule
    )
    login = run_bridge(
        BridgeRequest(operation="login"), lambda _cookie: FakeClient(), auth_module=AuthModule
    )

    assert status.payload == {"auth_status": "authenticated"}
    assert login.payload == {"auth_status": "authenticated"}
    assert AuthModule.saved_calls == 2
    assert AuthModule.browser_extraction_calls == 0

    cookie_file.chmod(0o644)
    invalid = run_bridge(
        BridgeRequest(operation="status"), lambda _cookie: FakeClient(), auth_module=AuthModule
    )
    assert invalid.status == "failed"
    assert invalid.error_code == "auth_invalid"
    assert stat.S_IMODE(cookie_file.stat().st_mode) == 0o644


def test_login_uses_only_project_browser_provider_and_persists_private_cookie(
    tmp_path: Path,
) -> None:
    auth_dir = tmp_path / "auth"
    auth_dir.mkdir(mode=0o700)
    cookie_file = auth_dir / "cookies.json"

    class AuthModule:
        CONFIG_DIR = auth_dir
        COOKIE_FILE = cookie_file
        TOKEN_CACHE_FILE = auth_dir / "token_cache.json"
        REQUIRED_COOKIES: ClassVar[frozenset[str]] = frozenset({"a1", "web_session"})

        @classmethod
        def get_saved_cookie_string(cls) -> None:
            return None

        @classmethod
        def get_cookie_string(cls) -> str:
            raise AssertionError("system browser extraction must never run")

    provider_calls: list[Path] = []

    def project_login_provider(path: Path) -> dict[str, str]:
        provider_calls.append(path)
        return {
            "a1": "private-a1",
            "web_session": "private-session",
            "unrelated": "must-not-be-persisted",
        }

    response = run_bridge(
        BridgeRequest(operation="login"),
        lambda _cookie: FakeClient(),
        auth_module=AuthModule,
        login_provider=project_login_provider,
    )

    assert response.status == "complete"
    assert response.payload == {"auth_status": "authenticated"}
    assert provider_calls == [auth_dir]
    saved = json.loads(cookie_file.read_text())
    assert set(saved["cookies"]) == {"a1", "web_session"}
    assert stat.S_IMODE(cookie_file.stat().st_mode) == 0o600
    assert "private-a1" not in response.model_dump_json()
    assert "private-session" not in response.model_dump_json()


def test_explicit_login_provider_revalidates_and_replaces_existing_cookie(tmp_path: Path) -> None:
    auth_dir = tmp_path / "auth"
    auth_dir.mkdir(mode=0o700)
    cookie_file = auth_dir / "cookies.json"
    cookie_file.write_text('{"cookies":{"a1":"old-a1","web_session":"old-session"}}')
    cookie_file.chmod(0o600)

    class AuthModule:
        CONFIG_DIR = auth_dir
        COOKIE_FILE = cookie_file
        TOKEN_CACHE_FILE = auth_dir / "token_cache.json"
        REQUIRED_COOKIES: ClassVar[frozenset[str]] = frozenset({"a1", "web_session"})

        @staticmethod
        def get_saved_cookie_string() -> str:
            return "a1=old-a1; web_session=old-session"

    provider_calls = 0

    def project_login_provider(_path: Path) -> dict[str, str]:
        nonlocal provider_calls
        provider_calls += 1
        return {"a1": "new-a1", "web_session": "new-session"}

    response = run_bridge(
        BridgeRequest(operation="login"),
        lambda _cookie: FakeClient(),
        auth_module=AuthModule,
        login_provider=project_login_provider,
    )

    assert response.payload == {"auth_status": "authenticated"}
    assert provider_calls == 1
    saved = json.loads(cookie_file.read_text())
    assert saved == {"cookies": {"a1": "new-a1", "web_session": "new-session"}}


def test_login_provider_exception_returns_a_finite_safe_error_code(tmp_path: Path) -> None:
    auth_dir = tmp_path / "auth"
    auth_dir.mkdir(mode=0o700)

    class AuthModule:
        CONFIG_DIR = auth_dir
        COOKIE_FILE = auth_dir / "cookies.json"
        TOKEN_CACHE_FILE = auth_dir / "token_cache.json"
        REQUIRED_COOKIES: ClassVar[frozenset[str]] = frozenset({"a1", "web_session"})

        @staticmethod
        def get_saved_cookie_string() -> None:
            return None

    def failing_provider(_path: Path) -> dict[str, str]:
        raise RuntimeError("private browser diagnostic")

    response = run_bridge(
        BridgeRequest(operation="login"),
        lambda _cookie: FakeClient(),
        auth_module=AuthModule,
        login_provider=failing_provider,
    )

    assert response.status == "failed"
    assert response.payload == {}
    assert response.error_code == "auth_failed"
    assert "private browser diagnostic" not in response.model_dump_json()


@pytest.mark.parametrize(
    "raw_request",
    [
        b'{"operation":"status","operation":"status"}',
        b'{"operation":"status","limit":NaN}',
        b'{"operation":"status","limit":Infinity}',
    ],
)
def test_bridge_request_parser_rejects_duplicate_keys_and_nonfinite_constants(
    raw_request: bytes,
) -> None:
    with pytest.raises(ValueError):
        parse_bridge_request(raw_request)


def test_account_minimal_profile_fallback_fails_without_a_bound_account(tmp_path: Path) -> None:
    class FallbackClient(FakeClient):
        def get_user_info(self, user_id: str) -> object:
            return {"userInfo": {"userId": user_id}}

    response = run_bridge(
        BridgeRequest(operation="account", account_id="author_a", limit=1),
        lambda _cookie: FallbackClient(),
        staging_dir=tmp_path,
    )

    assert response.status == "failed"
    assert response.error_code == "profile_ambiguous"
    assert response.payload == {}


def test_media_url_policy_rejects_non_https_and_unsafe_hosts() -> None:
    with pytest.raises(ValueError):
        xhs_bridge._validate_media_url("http://image.xhscdn.com/cover.jpg")
    with pytest.raises(ValueError):
        xhs_bridge._validate_media_url("https://image.example.invalid/cover.jpg")


@pytest.mark.parametrize("host", ["xhscdn.com", "xiaohongshu.com"])
def test_media_origin_allows_exact_public_allowlist_domains(
    host: str,
) -> None:
    xhs_bridge._validate_media_url(f"https://{host}/cover.jpg")


@pytest.mark.parametrize(
    "value",
    [
        "Access - Token : concealed",
        "refresh_token=concealed",
        "AUTHORIZATION : Bearer concealed",
        "cookies=concealed",
        "sign : concealed",
        "signature=concealed",
        "token : concealed",
        "web session=concealed",
        "xsec_token:concealed",
        "a1 = concealed",
        "caption https://cdn.example.invalid/image.jpg?signature=concealed",
    ],
)
def test_bridge_safe_text_drops_sensitive_assignments_and_embedded_signed_urls(value: str) -> None:
    assert xhs_bridge._safe_text(value) is None


def test_bridge_safe_text_keeps_plain_text_and_query_free_canonical_xhs_url() -> None:
    assert xhs_bridge._safe_text("正常中文正文") == "正常中文正文"
    assert (
        xhs_bridge._safe_text("https://www.xiaohongshu.com/explore/note_image")
        == "https://www.xiaohongshu.com/explore/note_image"
    )
