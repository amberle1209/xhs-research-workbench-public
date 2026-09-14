"""Exact visible-time binding tests for extension candidate selection."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from xhs_workbench.extension_import import (
    ExtensionJob,
    ExtensionJobState,
    _bound_publication_time,
)
from xhs_workbench.extension_models import (
    ExtensionBeginJob,
    ExtensionCandidateSnapshot,
    ExtensionCandidateUnavailable,
    ExtensionTimeEvidence,
)
from xhs_workbench.models import RunStatus

SHANGHAI = ZoneInfo("Asia/Shanghai")


def test_publication_binds_exact_visible_iso_style_time() -> None:
    published_at = datetime(2026, 3, 5, tzinfo=SHANGHAI)

    assert _bound_publication_time(
        published_at,
        ExtensionTimeEvidence(kind="published", raw_text="发布于 2026-03-05 00:00"),
    ) == published_at


def test_publication_binds_exact_visible_chinese_time() -> None:
    published_at = datetime(2024, 2, 29, 9, 30, tzinfo=SHANGHAI)

    assert _bound_publication_time(
        published_at,
        ExtensionTimeEvidence(kind="published", raw_text="2024年02月29日 09:30"),
    ) == published_at


@pytest.mark.parametrize(
    "raw_text",
    ["发布于 ٢٠٢٦-٠٣-٠٥ ٠٠:٠٠", "٢٠٢٦年٠٣月٠٥日 ٠٠:٠٠"],
    ids=("iso_style_unicode_digits", "chinese_style_unicode_digits"),
)
def test_publication_rejects_unicode_digits_in_visible_time(raw_text: str) -> None:
    """Visible-time grammar is ASCII-only, matching the TypeScript wire contract."""
    assert _bound_publication_time(
        datetime(2026, 3, 5, tzinfo=SHANGHAI),
        ExtensionTimeEvidence(kind="published", raw_text=raw_text),
    ) is None


@pytest.mark.parametrize(
    "evidence",
    [
        ExtensionTimeEvidence(kind="edited", raw_text="编辑于 2026-03-05"),
        ExtensionTimeEvidence(kind="published", raw_text="03-05 00:00"),
        ExtensionTimeEvidence(kind="published", raw_text="1小时前"),
        ExtensionTimeEvidence(kind="unknown", raw_text="发布时间未知"),
        None,
    ],
    ids=("edited", "yearless", "relative", "unknown", "absent"),
)
def test_publication_rejects_non_absolute_or_non_published_evidence(
    evidence: ExtensionTimeEvidence | None,
) -> None:
    assert _bound_publication_time(datetime(2026, 3, 5, tzinfo=SHANGHAI), evidence) is None


def test_publication_rejects_visible_and_offset_timestamp_mismatch() -> None:
    assert _bound_publication_time(
        datetime(2026, 3, 5, tzinfo=SHANGHAI),
        ExtensionTimeEvidence(kind="published", raw_text="发布于 2026-03-05 00:01"),
    ) is None


def test_publication_normalizes_offset_bearing_timestamp_to_shanghai() -> None:
    published_at = datetime(2026, 3, 4, 16, tzinfo=ZoneInfo("UTC"))

    assert _bound_publication_time(
        published_at,
        ExtensionTimeEvidence(kind="published", raw_text="发布于 2026-03-05 00:00"),
    ) == datetime(2026, 3, 5, tzinfo=SHANGHAI)


def test_cutoff_is_inclusive_at_exact_180_days() -> None:
    cutoff = datetime(2026, 8, 28, 12, tzinfo=SHANGHAI) - timedelta(days=180)
    assert cutoff == datetime(2026, 3, 1, 12, tzinfo=SHANGHAI)


def test_cutoff_rejects_one_second_before_boundary() -> None:
    cutoff = datetime(2026, 8, 28, 12, tzinfo=SHANGHAI) - timedelta(days=180)
    assert cutoff - timedelta(seconds=1) < cutoff


def _begin_job(**overrides: object) -> ExtensionBeginJob:
    values: dict[str, object] = {
        "protocol_version": "1.0",
        "kind": "begin_job",
        "job_id": "job_123",
        "collection_surface": "extension_search",
        "source_page_url": "https://www.xiaohongshu.com/explore/source_123",
        "requested_count": 3,
        "candidate_scan_limit": 5,
        "publication_cutoff": "2026-03-01T12:00:00+08:00",
        "selection_order": "exact_likes_desc",
    }
    values.update(overrides)
    return ExtensionBeginJob(**values)


def _snapshot(
    position: int,
    note_id: str,
    *,
    published_at: str | None = "2026-03-01T12:00:00+08:00",
    time_evidence: ExtensionTimeEvidence | None = None,
    likes: dict[str, object] | None = None,
    job_id: str = "job_123",
) -> ExtensionCandidateSnapshot:
    if time_evidence is None and published_at is not None:
        time_evidence = ExtensionTimeEvidence(kind="published", raw_text="发布于 2026-03-01 12:00")
    return ExtensionCandidateSnapshot(
        protocol_version="1.0",
        kind="candidate_snapshot",
        job_id=job_id,
        source_position=position,
        note_id=note_id,
        canonical_url=f"https://www.xiaohongshu.com/explore/{note_id}",
        published_at=published_at,
        time_evidence=time_evidence,
        metrics={"likes": likes} if likes is not None else {},
    )


def _exact_likes(value: int) -> dict[str, object]:
    return {"raw_value": str(value), "normalized_value": value, "precision": "exact"}


def test_exact_likes_selection_sorts_then_ties_by_source_position_and_note_id() -> None:
    job = ExtensionJob.begin(_begin_job())
    job.add_candidate(_snapshot(3, "note_c", likes=_exact_likes(20)))
    job.add_candidate(_snapshot(2, "note_b", likes=_exact_likes(20)))
    job.add_candidate(_snapshot(1, "note_a", likes=_exact_likes(50)))

    summary = job.finish_scan()

    assert summary is not None
    assert job.selected_note_ids == ("note_a", "note_b", "note_c")
    assert [candidate.note_id for candidate in job.selected_candidates] == [
        "note_a",
        "note_b",
        "note_c",
    ]
    assert [(entry.note_id, entry.selection_rank) for entry in summary.entries] == [
        ("note_c", 3),
        ("note_b", 2),
        ("note_a", 1),
    ]


def test_page_order_selection_freezes_candidates_in_source_order() -> None:
    job = ExtensionJob.begin(
        _begin_job(
            requested_count=5,
            candidate_scan_limit=6,
            publication_cutoff=None,
            selection_order="page_order",
            source_page_url="https://www.xiaohongshu.com/search_result",
        )
    )
    job.add_candidate(_snapshot(3, "note_c", likes=_exact_likes(20)))
    job.add_candidate(_snapshot(2, "note_b", likes=_exact_likes(50)))
    job.add_candidate(_snapshot(1, "note_a", likes=_exact_likes(10)))

    summary = job.finish_scan()

    assert summary is not None
    assert job.selected_note_ids == ("note_a", "note_b", "note_c")
    assert summary.selection_basis == "page_order"


def test_page_order_finish_persists_a_search_run_summary(tmp_path: Path) -> None:
    job = ExtensionJob(
        "page_order_run",
        tmp_path / "page_order_run",
        _begin_job(
            requested_count=5,
            candidate_scan_limit=5,
            publication_cutoff=None,
            selection_order="page_order",
            source_page_url="https://www.xiaohongshu.com/search_result",
        ),
    )
    for position, note_id in enumerate(("note_a", "note_b", "note_c", "note_d", "note_e"), start=1):
        job.add_candidate(_snapshot(position, note_id, likes=_exact_likes(position)))
    job.finish_scan()
    for note_id in ("note_a", "note_b", "note_c", "note_d", "note_e"):
        job.record_page_detail_outcome(note_id, "enriched")

    run = job.finish_job()

    assert run.extension_search_run is not None
    assert run.extension_search_run.source_page_url == "https://www.xiaohongshu.com/search_result"
    assert run.extension_search_run.selected_count == 5
    assert {entry.detail_outcome for entry in run.extension_selection.entries} == {"enriched"}


def test_page_order_accepts_only_frozen_rank_ordered_details_after_selection(
    tmp_path: Path,
) -> None:
    """A search summary freezes first; later detail snapshots only enrich that frozen list."""
    job = ExtensionJob(
        "page_order_post_freeze",
        tmp_path / "page_order_post_freeze",
        _begin_job(
            requested_count=5,
            candidate_scan_limit=5,
            publication_cutoff=None,
            selection_order="page_order",
            source_page_url="https://www.xiaohongshu.com/search_result",
        ),
    )
    note_ids = ("note_a", "note_b", "note_c", "note_d", "note_e")
    for position, note_id in enumerate(note_ids, start=1):
        job.add_candidate(_snapshot(position, note_id, likes=_exact_likes(position)))
    job.finish_scan()

    for position, note_id in enumerate(note_ids, start=1):
        job.add_candidate(_snapshot(position, note_id, likes=_exact_likes(position + 10)))

    run = job.finish_job()

    assert run.status is RunStatus.COMPLETE
    assert run.extension_search_run is not None
    assert run.extension_search_run.selected_count == 5
    assert run.extension_search_run.enriched_count == 5
    assert [entry.detail_outcome for entry in run.extension_selection.entries] == ["enriched"] * 5


@pytest.mark.parametrize(
    ("outcome", "terminal_error", "expected_status", "expected_error", "expected_enriched"),
    [
        ("detail_unavailable", None, RunStatus.PARTIAL, "detail_unavailable", 4),
        ("login_required", "login_required", RunStatus.PARTIAL, "login_required", 4),
        ("challenge_detected", "challenge_detected", RunStatus.PARTIAL, "challenge_detected", 4),
        ("stopped", "stopped", RunStatus.STOPPED, "stopped", 4),
    ],
    ids=("detail_unavailable", "login_required", "challenge_detected", "stopped"),
)
def test_page_order_import_preserves_selected_detail_and_terminal_outcomes(
    tmp_path: Path,
    outcome: str,
    terminal_error: str | None,
    expected_status: RunStatus,
    expected_error: str,
    expected_enriched: int,
) -> None:
    job = ExtensionJob(
        "page_order_terminal",
        tmp_path / "page_order_terminal",
        _begin_job(
            requested_count=5,
            candidate_scan_limit=5,
            publication_cutoff=None,
            selection_order="page_order",
            source_page_url="https://www.xiaohongshu.com/search_result",
        ),
    )
    for position, note_id in enumerate(("note_a", "note_b", "note_c", "note_d", "note_e"), start=1):
        job.add_candidate(_snapshot(position, note_id, likes=_exact_likes(position)))
    job.finish_scan()
    for note_id in ("note_a", "note_b", "note_c", "note_d"):
        job.record_page_detail_outcome(note_id, "enriched")
    if terminal_error is None:
        job.record_page_detail_outcome("note_e", outcome)
    else:
        job.set_page_terminal_error(terminal_error)

    run = job.finish_job()

    assert run.status is expected_status
    assert run.error_code == expected_error
    assert run.extension_search_run is not None
    assert run.extension_search_run.enriched_count == expected_enriched
    assert run.extension_selection.entries[-1].selection_rank == 5
    assert run.extension_selection.entries[-1].detail_outcome == outcome


def test_page_order_import_rejects_persistence_without_explicit_detail_outcomes(
    tmp_path: Path,
) -> None:
    job = ExtensionJob(
        "page_order_missing_detail",
        tmp_path / "page_order_missing_detail",
        _begin_job(
            requested_count=5,
            candidate_scan_limit=5,
            publication_cutoff=None,
            selection_order="page_order",
            source_page_url="https://www.xiaohongshu.com/search_result",
        ),
    )
    for position, note_id in enumerate(("note_a", "note_b", "note_c", "note_d", "note_e"), start=1):
        job.add_candidate(_snapshot(position, note_id, likes=_exact_likes(position)))
    job.finish_scan()

    with pytest.raises(ValueError, match="explicit detail outcome"):
        job.finish_job()


@pytest.mark.parametrize(
    ("terminal_error", "expected_status"),
    [
        ("login_required", RunStatus.PARTIAL),
        ("challenge_detected", RunStatus.PARTIAL),
        ("stopped", RunStatus.STOPPED),
    ],
)
def test_page_order_terminal_transition_atomically_marks_remaining_ranks(
    tmp_path: Path, terminal_error: str, expected_status: RunStatus
) -> None:
    job = ExtensionJob(
        "page_order_atomic_terminal",
        tmp_path / "page_order_atomic_terminal",
        _begin_job(
            requested_count=5,
            candidate_scan_limit=5,
            publication_cutoff=None,
            selection_order="page_order",
            source_page_url="https://www.xiaohongshu.com/search_result",
        ),
    )
    for position, note_id in enumerate(("note_a", "note_b", "note_c", "note_d", "note_e"), start=1):
        job.add_candidate(_snapshot(position, note_id, likes=_exact_likes(position)))
    job.finish_scan()
    job.record_page_detail_outcome("note_a", "enriched")
    job.set_page_terminal_error(terminal_error)

    with pytest.raises(ValueError, match="terminal"):
        job.record_page_detail_outcome("note_b", "enriched")
    run = job.finish_job()

    assert run.status is expected_status
    assert [entry.detail_outcome for entry in run.extension_selection.entries] == [
        "enriched",
        terminal_error,
        terminal_error,
        terminal_error,
        terminal_error,
    ]


def test_page_order_detail_transition_rejects_out_of_order_rank(tmp_path: Path) -> None:
    job = ExtensionJob(
        "page_order_rank_order",
        tmp_path / "page_order_rank_order",
        _begin_job(
            requested_count=5,
            candidate_scan_limit=5,
            publication_cutoff=None,
            selection_order="page_order",
            source_page_url="https://www.xiaohongshu.com/search_result",
        ),
    )
    for position, note_id in enumerate(("note_a", "note_b", "note_c", "note_d", "note_e"), start=1):
        job.add_candidate(_snapshot(position, note_id, likes=_exact_likes(position)))
    job.finish_scan()

    with pytest.raises(ValueError, match="next frozen rank"):
        job.record_page_detail_outcome("note_b", "enriched")


@pytest.mark.parametrize("reason", ["sponsored", "invalid_card"])
def test_page_order_import_retains_scan_exclusions_without_selecting_them(reason: str) -> None:
    job = ExtensionJob.begin(
        _begin_job(
            requested_count=5,
            candidate_scan_limit=6,
            publication_cutoff=None,
            selection_order="page_order",
            source_page_url="https://www.xiaohongshu.com/search_result",
        )
    )
    job.add_candidate_unavailable(
        ExtensionCandidateUnavailable(
            protocol_version="1.0",
            kind="candidate_unavailable",
            job_id="job_123",
            source_position=1,
            note_id="excluded_card",
            reason=reason,
        )
    )
    for position, note_id in enumerate(("note_a", "note_b", "note_c", "note_d", "note_e"), start=2):
        job.add_candidate(_snapshot(position, note_id, likes=_exact_likes(position)))

    summary = job.finish_scan()

    assert summary is not None
    excluded = summary.entries[0]
    assert excluded.inclusion == "excluded"
    assert excluded.reason == reason
    assert excluded.selection_rank is None


@pytest.mark.parametrize(
    ("likes", "reason"),
    [
        ({"raw_value": "1万+", "normalized_value": 10_000, "precision": "display_rounded"}, "likes_not_exact"),
        (None, "likes_unavailable"),
        ({"precision": "not_exposed"}, "likes_not_exact"),
    ],
    ids=("rounded", "missing", "not_exposed"),
)
def test_selection_excludes_non_exact_or_unavailable_likes(
    likes: dict[str, object] | None, reason: str
) -> None:
    job = ExtensionJob.begin(_begin_job(requested_count=1))
    job.add_candidate(_snapshot(1, "note_a", likes=likes))

    summary = job.finish_scan()

    assert summary is not None
    assert summary.entries[0].outcome == "excluded"
    assert summary.entries[0].reason == reason
    assert summary.entries[0].likes_eligible is False
    assert job.selected_note_ids == ()


def test_finite_window_requires_bound_visible_publication_time_and_inclusive_cutoff() -> None:
    job = ExtensionJob.begin(_begin_job(requested_count=2))
    job.add_candidate(_snapshot(1, "note_a", likes=_exact_likes(20)))
    job.add_candidate(
        _snapshot(
            2,
            "note_b",
            published_at="2026-03-01T11:59:59+08:00",
            likes=_exact_likes(50),
        )
    )
    job.add_candidate(
        _snapshot(
            3,
            "note_c",
            published_at="2026-03-01T12:00:00+08:00",
            time_evidence=ExtensionTimeEvidence(kind="edited", raw_text="编辑于 2026-03-01"),
            likes=_exact_likes(40),
        )
    )

    summary = job.finish_scan()

    assert summary is not None
    assert job.selected_note_ids == ("note_a",)
    assert [(entry.note_id, entry.reason) for entry in summary.entries] == [
        ("note_a", None),
        ("note_b", "publication_time_unavailable"),
        ("note_c", "publication_time_unavailable"),
    ]


def test_all_window_keeps_exact_likes_candidate_with_unavailable_time() -> None:
    job = ExtensionJob.begin(_begin_job(publication_cutoff=None, requested_count=1))
    job.add_candidate(
        _snapshot(
            1,
            "note_a",
            published_at=None,
            time_evidence=None,
            likes=_exact_likes(20),
        )
    )

    summary = job.finish_scan()

    assert summary is not None
    assert summary.entries[0].publication_eligible is True
    assert job.selected_note_ids == ("note_a",)


@pytest.mark.parametrize(
    ("publication_cutoff", "expected_ids", "expected_status", "publication_eligible"),
    [
        ("2026-03-01T12:00:00+08:00", (), "partial", False),
        (None, ("note_a",), "complete", True),
    ],
    ids=("finite_window", "all_window"),
)
def test_invalid_offset_timestamp_remains_a_finite_selection_result(
    publication_cutoff: str | None,
    expected_ids: tuple[str, ...],
    expected_status: str,
    publication_eligible: bool,
) -> None:
    job = ExtensionJob.begin(_begin_job(publication_cutoff=publication_cutoff, requested_count=1))
    job.add_candidate(
        _snapshot(
            1,
            "note_a",
            published_at="2026-02-30T10:00:00+08:00",
            time_evidence=ExtensionTimeEvidence(
                kind="published", raw_text="发布于 2026-02-30 10:00"
            ),
            likes=_exact_likes(20),
        )
    )

    summary = job.finish_scan()

    assert summary is not None
    assert job.state is ExtensionJobState.SCAN_FINISHED
    assert job.selected_note_ids == expected_ids
    assert job.selection_status == expected_status
    assert summary.entries[0].publication_eligible is publication_eligible
    if publication_cutoff is not None:
        assert summary.entries[0].reason == "publication_time_unavailable"


def test_invalid_offset_cutoff_fails_closed_with_a_finite_selection_result() -> None:
    job = ExtensionJob.begin(
        _begin_job(publication_cutoff="2026-02-30T10:00:00+08:00", requested_count=1)
    )
    job.add_candidate(_snapshot(1, "note_a", likes=_exact_likes(20)))

    summary = job.finish_scan()

    assert summary is not None
    assert job.state is ExtensionJobState.SCAN_FINISHED
    assert job.selected_note_ids == ()
    assert job.selection_status == "partial"
    assert summary.entries[0].reason == "publication_time_unavailable"


def test_selection_retains_every_inspected_candidate_and_is_partial_when_short() -> None:
    job = ExtensionJob.begin(_begin_job(requested_count=3))
    job.add_candidate(_snapshot(1, "note_a", likes=_exact_likes(10)))
    job.add_candidate(_snapshot(2, "note_b", likes=None))

    summary = job.finish_scan()

    assert summary is not None
    assert summary.candidate_scanned_count == 2
    assert len(summary.entries) == 2
    assert [entry.source_position for entry in summary.entries] == [1, 2]
    assert job.selection_status == "partial"
    assert [entry.selection_rank for entry in summary.entries] == [1, None]


def test_batch_detail_unavailable_is_retained_and_forces_a_truthful_partial_run() -> None:
    """Removing the unavailable fact would falsely make a partial batch complete."""
    job = ExtensionJob.begin(_begin_job(requested_count=1, candidate_scan_limit=2))
    job.add_candidate_unavailable(
        ExtensionCandidateUnavailable(
            protocol_version="1.0",
            kind="candidate_unavailable",
            job_id="job_123",
            source_position=1,
            note_id="note_unavailable",
            reason="detail_unavailable",
        )
    )
    job.add_candidate(_snapshot(2, "note_selected", likes=_exact_likes(50)))

    summary = job.finish_scan()

    assert summary is not None
    assert job.selected_note_ids == ("note_selected",)
    assert job.selection_status == "partial"
    assert [(entry.note_id, entry.outcome, entry.reason) for entry in summary.entries] == [
        ("note_selected", "selected", None),
        ("note_unavailable", "unavailable", "detail_unavailable"),
    ]


def test_selection_keeps_exact_eligible_candidate_beyond_requested_count_in_ledger() -> None:
    job = ExtensionJob.begin(_begin_job(requested_count=2))
    job.add_candidate(_snapshot(1, "note_a", likes=_exact_likes(30)))
    job.add_candidate(_snapshot(2, "note_b", likes=_exact_likes(20)))
    job.add_candidate(_snapshot(3, "note_c", likes=_exact_likes(10)))

    summary = job.finish_scan()

    assert summary is not None
    assert job.selected_note_ids == ("note_a", "note_b")
    extra_entry = summary.entries[2]
    assert extra_entry.outcome == "excluded"
    assert extra_entry.reason == "selection_limit_reached"
    assert extra_entry.publication_eligible is True
    assert extra_entry.likes_eligible is True
    assert extra_entry.exact_likes == 10
    assert extra_entry.selection_rank is None


def test_selection_rejects_duplicate_candidate_identity_limit_and_one_way_states() -> None:
    job = ExtensionJob.begin(_begin_job(requested_count=1, candidate_scan_limit=1))
    candidate = _snapshot(1, "note_a", likes=_exact_likes(10))
    job.add_candidate(candidate)
    with pytest.raises(ValueError, match="duplicate"):
        job.add_candidate(candidate)
    with pytest.raises(ValueError, match="scan limit"):
        job.add_candidate(_snapshot(2, "note_b", likes=_exact_likes(20)))
    job.finish_scan()
    assert job.state is ExtensionJobState.SCAN_FINISHED
    with pytest.raises(ValueError, match="scan is not open"):
        job.add_candidate(_snapshot(2, "note_b", likes=_exact_likes(20)))
    with pytest.raises(ValueError, match="finish"):
        job.finish_scan()


def test_current_note_bypasses_batch_filters_and_binds_its_only_snapshot() -> None:
    job = ExtensionJob.begin(
        _begin_job(
            collection_surface="extension_current",
            requested_count=1,
            candidate_scan_limit=2,
            publication_cutoff="2026-03-01T12:00:00+08:00",
        )
    )
    job.add_candidate(_snapshot(1, "note_a", published_at=None, time_evidence=None, likes=None))
    with pytest.raises(ValueError, match="only one"):
        job.add_candidate(_snapshot(2, "note_b", likes=_exact_likes(1)))

    assert job.finish_scan() is None
    assert job.selected_note_ids == ("note_a",)
    assert [candidate.note_id for candidate in job.selected_candidates] == ["note_a"]


def test_current_note_finish_without_snapshot_rejects_without_closing_the_scan() -> None:
    job = ExtensionJob.begin(
        _begin_job(
            collection_surface="extension_current",
            requested_count=1,
            candidate_scan_limit=1,
        )
    )

    with pytest.raises(ValueError, match="exactly one"):
        job.finish_scan()

    assert job.state is ExtensionJobState.OPEN


def test_explicit_host_run_directory_never_uses_extension_job_id(tmp_path: Path) -> None:
    """A path derived from the extension correlation ID would expose host storage control."""
    job = ExtensionJob("host_run_123", tmp_path / "host_run_123", _begin_job(job_id="job_123"))

    assert job.run_id == "host_run_123"
    assert job.output_dir.name == "host_run_123"
    assert "job_123" not in job.output_dir.parts
    job.discard()
