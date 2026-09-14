from collections import UserDict

import pytest
from pydantic import ValidationError

from xhs_workbench.media import MediaMime
from xhs_workbench.models import (
    AccountRecord,
    CandidateAttempt,
    CollectionRun,
    ExtensionSearchRunSummary,
    ExtensionSelectionEntry,
    ExtensionSelectionSummary,
    LocalAsset,
    MetricValue,
    NoteMediaSlot,
    NoteMetrics,
    NoteRecord,
    PacingSummary,
    RunStatus,
)


def _asset(
    path: str = "assets/note_a-cover.jpg", mime_type: MediaMime = "image/jpeg"
) -> LocalAsset:
    return LocalAsset(
        local_path=path,
        mime_type=mime_type,
        size_bytes=3,
        sha256="a" * 64,
    )


def _note(**overrides: object) -> NoteRecord:
    values: dict[str, object] = {
        "note_id": "note_a",
        "canonical_url": "https://www.xiaohongshu.com/explore/note_a",
        "metrics": NoteMetrics(
            likes=MetricValue(raw_value="1", normalized_value=1, precision="exact"),
            shares=MetricValue(raw_value="2", normalized_value=2, precision="exact"),
        ),
        "source_position": 1,
    }
    values.update(overrides)
    return NoteRecord(**values)


def _exact_metric(value: int) -> MetricValue:
    return MetricValue(raw_value=str(value), normalized_value=value, precision="exact")


def _selection_entry(
    note_id: str,
    source_position: int,
    outcome: str,
    exact_likes: int,
    selection_rank: int | None,
    **overrides: object,
) -> dict[str, object]:
    values: dict[str, object] = {
        "note_id": note_id,
        "source_position": source_position,
        "outcome": outcome,
        "reason": None,
        "publication_eligible": True,
        "likes_eligible": True,
        "exact_likes": exact_likes,
        "selection_rank": selection_rank,
    }
    values.update(overrides)
    return values


def _page_order_entry(
    note_id: str,
    source_position: int,
    outcome: str,
    selection_rank: int | None,
    **overrides: object,
) -> dict[str, object]:
    values: dict[str, object] = {
        "note_id": note_id,
        "source_position": source_position,
        "outcome": outcome,
        "reason": None if outcome == "selected" else "selection_limit_reached",
        "inclusion": "included",
        "selection_rank": selection_rank,
        "selection_basis": "page_order",
    }
    values.update(overrides)
    return values


def _extension_run(**overrides: object) -> CollectionRun:
    values: dict[str, object] = {
        "run_id": "extension_fixture",
        "mode": "extension_search",
        "input_summary": "AI 工作流",
        "requested_count": 2,
        "actual_count": 2,
        "started_at": "2026-09-01T10:00:00+08:00",
        "finished_at": "2026-09-01T10:00:01+08:00",
        "status": RunStatus.COMPLETE,
        "collection_surface": "extension_search",
        "notes": [
            _note(
                note_id="note_b",
                canonical_url="https://www.xiaohongshu.com/explore/note_b",
                source_position=4,
                selection_rank=1,
                selection_basis="exact_likes_desc",
                metrics=NoteMetrics(likes=_exact_metric(50)),
            ),
            _note(
                note_id="note_a",
                source_position=1,
                selection_rank=2,
                selection_basis="exact_likes_desc",
                metrics=NoteMetrics(likes=_exact_metric(20)),
            ),
        ],
        "extension_selection": {
            "collection_surface": "extension_search",
            "candidate_scan_limit": 10,
            "candidate_scanned_count": 2,
            "requested_count": 2,
            "publication_cutoff": "2026-03-01T00:00:00+08:00",
            "entries": [
                _selection_entry("note_a", 1, "selected", 20, 2),
                _selection_entry("note_b", 4, "selected", 50, 1),
            ],
        },
    }
    values.update(overrides)
    return CollectionRun(**values)


def test_extension_selection_summary_binds_selected_notes_and_ranks() -> None:
    run = _extension_run()

    assert [note.note_id for note in run.notes] == ["note_b", "note_a"]


def test_page_order_selection_requires_the_visible_prefix() -> None:
    summary = ExtensionSelectionSummary(
        collection_surface="extension_search",
        selection_basis="page_order",
        candidate_scan_limit=5,
        candidate_scanned_count=2,
        requested_count=5,
        selected_count=2,
        entries=[
            _page_order_entry("n1", 1, "selected", 1),
            _page_order_entry("n2", 2, "selected", 2),
        ],
    )

    assert [entry.note_id for entry in summary.entries if entry.selection_rank] == ["n1", "n2"]


@pytest.mark.parametrize(
    "entries",
    [
        [_page_order_entry("n1", 1, "selected", 1), _page_order_entry("n2", 2, "selected", 3)],
        [_page_order_entry("n1", 1, "selected", 1), _page_order_entry("n1", 2, "selected", 2)],
        [_page_order_entry("n1", 1, "selected", 1), _page_order_entry("n2", 1, "selected", 2)],
        [
            _page_order_entry("n1", 1, "selected", 1),
            _page_order_entry(
                "ad", 2, "selected", 2, inclusion="excluded", reason="sponsored"
            ),
            _page_order_entry("n2", 3, "selected", 3),
        ],
    ],
    ids=("rank_gap", "duplicate_note", "duplicate_position", "sponsored_in_selected_prefix"),
)
def test_page_order_rejects_invalid_visible_selection(entries: list[dict[str, object]]) -> None:
    with pytest.raises(ValidationError):
        ExtensionSelectionSummary(
            collection_surface="extension_search",
            selection_basis="page_order",
            candidate_scan_limit=5,
            candidate_scanned_count=len(entries),
            requested_count=5,
            selected_count=2,
            entries=entries,
        )


def test_page_order_rejects_exact_likes_only_cutoff_and_unsafe_sort_label() -> None:
    values = {
        "collection_surface": "extension_search",
        "selection_basis": "page_order",
        "candidate_scan_limit": 5,
        "candidate_scanned_count": 1,
        "requested_count": 5,
        "selected_count": 1,
        "publication_cutoff": "2026-03-01T00:00:00+08:00",
        "entries": [_page_order_entry("n1", 1, "selected", 1)],
    }
    with pytest.raises(ValidationError):
        ExtensionSelectionSummary(**values)
    with pytest.raises(ValidationError):
        ExtensionSearchRunSummary(
            source_page_url="https://www.xiaohongshu.com/search_result",
            sort_label="token=secret",
            requested_count=5,
            selected_count=1,
            enriched_count=0,
            scroll_rounds=0,
            status="partial",
        )


def test_legacy_exact_likes_selection_fixture_remains_unchanged() -> None:
    selection = _extension_run().extension_selection

    assert selection is not None
    assert selection.selection_basis == "exact_likes_desc"
    assert selection.publication_cutoff is not None


def _page_order_run(
    detail_outcomes: list[str],
    *,
    status: RunStatus = RunStatus.COMPLETE,
    error_code: str | None = None,
    search_status: str = "complete",
    search_error_code: str | None = None,
) -> CollectionRun:
    notes = [
        _note(
            note_id=f"page_{index}",
            canonical_url=f"https://www.xiaohongshu.com/explore/page_{index}",
            source_position=index,
            selection_rank=index,
            selection_basis="page_order",
        )
        for index in range(1, 6)
    ]
    entries = [
        _page_order_entry(
            f"page_{index}",
            index,
            "selected",
            index,
            detail_outcome=detail_outcome,
        )
        for index, detail_outcome in enumerate(detail_outcomes, start=1)
    ]
    selected_count = len(entries)
    enriched_count = sum(outcome == "enriched" for outcome in detail_outcomes)
    return CollectionRun(
        run_id="page_order_fixture",
        mode="extension_search",
        input_summary="AI 工作流",
        requested_count=5,
        actual_count=selected_count,
        started_at="2026-09-04T10:00:00+08:00",
        finished_at="2026-09-04T10:00:01+08:00",
        status=status,
        error_code=error_code,
        notes=notes[:selected_count],
        collection_surface="extension_search",
        extension_selection={
            "collection_surface": "extension_search",
            "candidate_scan_limit": 5,
            "candidate_scanned_count": selected_count,
            "requested_count": 5,
            "selected_count": selected_count,
            "selection_basis": "page_order",
            "entries": entries,
        },
        extension_search_run={
            "source_page_url": "https://www.xiaohongshu.com/search_result",
            "requested_count": 5,
            "selected_count": selected_count,
            "enriched_count": enriched_count,
            "scroll_rounds": 0,
            "status": search_status,
            "error_code": search_error_code,
        },
    )


@pytest.mark.parametrize(
    ("detail_outcomes", "status", "error_code", "search_status", "search_error_code"),
    [
        (["enriched"] * 5, RunStatus.COMPLETE, None, "complete", None),
        (["enriched"] * 4 + ["detail_unavailable"], RunStatus.PARTIAL, "detail_unavailable", "partial", "detail_unavailable"),
        (["enriched"] * 3 + ["stopped"] * 2, RunStatus.STOPPED, "stopped", "stopped", "stopped"),
        (["enriched"] * 2 + ["login_required"] * 3, RunStatus.PARTIAL, "login_required", "partial", "login_required"),
        (["enriched"] + ["challenge_detected"] * 4, RunStatus.PARTIAL, "challenge_detected", "partial", "challenge_detected"),
    ],
    ids=("complete", "detail_unavailable", "stopped", "login_required", "challenge_detected"),
)
def test_page_order_run_binds_detail_outcomes_to_counts_and_terminal_state(
    detail_outcomes: list[str],
    status: RunStatus,
    error_code: str | None,
    search_status: str,
    search_error_code: str | None,
) -> None:
    run = _page_order_run(
        detail_outcomes,
        status=status,
        error_code=error_code,
        search_status=search_status,
        search_error_code=search_error_code,
    )

    assert run.extension_search_run is not None
    assert run.extension_search_run.enriched_count == detail_outcomes.count("enriched")


@pytest.mark.parametrize(
    "change",
    [
        {"extension_search_run": {"selected_count": 0, "enriched_count": 0, "status": "partial"}},
        {"status": RunStatus.COMPLETE, "error_code": None, "extension_search_run": {"status": "partial", "error_code": "detail_unavailable"}},
        {"extension_selection": {"entries": [{"detail_outcome": None}]}},
    ],
    ids=("contradictory_counts", "contradictory_status", "missing_detail_outcome"),
)
def test_page_order_run_rejects_contradictory_persisted_facts(change: dict[str, object]) -> None:
    values = _page_order_run(["enriched"] * 5).model_dump()
    for field, update in change.items():
        if field == "extension_selection":
            values[field]["entries"][0].update(update["entries"][0])
        elif field == "extension_search_run":
            values[field].update(update)
        else:
            values[field] = update
    with pytest.raises(ValidationError):
        CollectionRun.model_validate(values)


def test_page_order_persisted_contract_rejects_invalid_count_route_and_failed_error() -> None:
    values = _page_order_run(["enriched"] * 5).model_dump()
    values["extension_selection"]["requested_count"] = 2
    with pytest.raises(ValidationError):
        CollectionRun.model_validate(values)


@pytest.mark.parametrize(
    "error_code",
    ["structural_error", "route_mismatch", "identity_mismatch"],
)
def test_page_order_persists_only_finite_failed_states(error_code: str) -> None:
    run = CollectionRun(
        run_id="failed_page_order_fixture",
        mode="extension_search",
        input_summary="AI 工作流",
        requested_count=5,
        actual_count=0,
        started_at="2026-09-04T10:00:00+08:00",
        finished_at="2026-09-04T10:00:01+08:00",
        status=RunStatus.FAILED,
        error_code=error_code,
        notes=[],
        collection_surface="extension_search",
        extension_selection={
            "collection_surface": "extension_search",
            "candidate_scan_limit": 5,
            "candidate_scanned_count": 0,
            "requested_count": 5,
            "selected_count": 0,
            "selection_basis": "page_order",
            "entries": [],
        },
        extension_search_run={
            "source_page_url": "https://www.xiaohongshu.com/search_result",
            "requested_count": 5,
            "selected_count": 0,
            "enriched_count": 0,
            "scroll_rounds": 0,
            "status": "failed",
            "error_code": error_code,
        },
    )

    assert run.error_code == error_code


@pytest.mark.parametrize(
    "detail_outcomes",
    [
        ["enriched", "login_required", "challenge_detected", "challenge_detected", "challenge_detected"],
        ["enriched", "challenge_detected", "enriched", "challenge_detected", "challenge_detected"],
        ["enriched", "stopped", "enriched", "stopped", "stopped"],
    ],
    ids=("mixed_terminal_causes", "enriched_after_challenge", "enriched_after_stop"),
)
def test_page_order_run_rejects_impossible_terminal_histories(detail_outcomes: list[str]) -> None:
    terminal = next(outcome for outcome in detail_outcomes if outcome != "enriched")
    status = RunStatus.STOPPED if terminal == "stopped" else RunStatus.PARTIAL
    with pytest.raises(ValidationError):
        _page_order_run(
            detail_outcomes,
            status=status,
            error_code=terminal,
            search_status=status.value,
            search_error_code=terminal,
        )
    values = _page_order_run(["enriched"] * 5).model_dump()
    values["extension_search_run"]["source_page_url"] = "https://www.xiaohongshu.com/explore/not_search"
    with pytest.raises(ValidationError):
        CollectionRun.model_validate(values)
    values = _page_order_run(["enriched"] * 5).model_dump()
    values.update({"status": RunStatus.FAILED, "error_code": None})
    values["extension_search_run"].update({"status": "failed", "error_code": None})
    with pytest.raises(ValidationError):
        CollectionRun.model_validate(values)


def test_legacy_exact_likes_entry_still_requires_eligibility_facts() -> None:
    with pytest.raises(ValidationError):
        ExtensionSelectionEntry(
            source_position=1,
            note_id="legacy_missing_eligibility",
            outcome="unavailable",
            reason="detail_unavailable",
        )


def test_extension_selection_accepts_exact_candidate_excluded_only_by_requested_limit() -> None:
    selection = _extension_run().extension_selection.model_dump()
    selection["candidate_scanned_count"] = 3
    selection["entries"].append(
        _selection_entry(
            "note_c",
            3,
            "excluded",
            10,
            None,
            reason="selection_limit_reached",
        )
    )

    run = _extension_run(extension_selection=selection)

    entry = run.extension_selection.entries[-1]
    assert entry.reason == "selection_limit_reached"
    assert entry.publication_eligible is True
    assert entry.likes_eligible is True
    assert entry.exact_likes == 10


@pytest.mark.parametrize(
    "entry_update",
    [
        {"publication_eligible": False},
        {"likes_eligible": False},
        {"exact_likes": None},
    ],
    ids=("publication_ineligible", "likes_ineligible", "missing_exact_likes"),
)
def test_extension_selection_rejects_invalid_selection_limit_entry(
    entry_update: dict[str, object],
) -> None:
    selection = _extension_run().extension_selection.model_dump()
    selection["candidate_scanned_count"] = 3
    entry = _selection_entry(
        "note_c",
        3,
        "excluded",
        10,
        None,
        reason="selection_limit_reached",
    )
    entry.update(entry_update)
    selection["entries"].append(entry)

    with pytest.raises(ValidationError):
        _extension_run(extension_selection=selection)


def test_extension_selection_rejects_limit_entry_that_displaces_a_higher_ranked_note() -> None:
    selection = _extension_run().extension_selection.model_dump()
    selection["candidate_scanned_count"] = 3
    selection["entries"].append(
        _selection_entry(
            "note_c",
            3,
            "excluded",
            30,
            None,
            reason="selection_limit_reached",
        )
    )

    with pytest.raises(ValidationError, match="selection prefix"):
        ExtensionSelectionSummary(**selection)


def test_extension_selection_rejects_missing_selected_entry_before_requested_limit() -> None:
    selection = _extension_run().extension_selection.model_dump()
    selection["entries"][0].update(
        {"outcome": "excluded", "reason": "selection_limit_reached", "selection_rank": None}
    )

    with pytest.raises(ValidationError, match="selection prefix"):
        ExtensionSelectionSummary(**selection)


@pytest.mark.parametrize(
    "case",
    ["duplicate_note", "duplicate_source_position", "non_contiguous_rank"],
    ids=("duplicate_note", "duplicate_source_position", "non_contiguous_rank"),
)
def test_extension_selection_rejects_duplicate_identity_or_noncontiguous_rank(
    case: str,
) -> None:
    run = _extension_run()
    if case == "duplicate_note":
        changed: dict[str, object] = {"notes": [run.notes[0], run.notes[0]]}
    elif case == "duplicate_source_position":
        selection = run.extension_selection.model_dump()
        selection["entries"][1]["source_position"] = 1
        changed = {"extension_selection": selection}
    else:
        changed = {
            "notes": [run.notes[0], run.notes[1].model_copy(update={"selection_rank": 3})]
        }
    with pytest.raises(ValidationError):
        _extension_run(**changed)


@pytest.mark.parametrize(
    "entry_update",
    [
        {"exact_likes": None},
        {"reason": "likes_not_exact"},
    ],
    ids=("missing_exact_likes", "selected_reason"),
)
def test_extension_selection_rejects_invalid_selected_entry(entry_update: dict[str, object]) -> None:
    selection = _extension_run().extension_selection.model_dump()
    selection["entries"][0].update(entry_update)

    with pytest.raises(ValidationError):
        _extension_run(extension_selection=selection)


def test_extension_selection_rejects_rounded_selected_likes() -> None:
    rounded_note = _extension_run().notes[1].model_copy(
        update={
            "metrics": NoteMetrics(
                likes=MetricValue(raw_value="2万", normalized_value=20, precision="display_rounded")
            )
        }
    )

    with pytest.raises(ValidationError):
        _extension_run(notes=[_extension_run().notes[0], rounded_note])


def test_extension_selection_rejects_missing_note_entry_correspondence() -> None:
    selection = _extension_run().extension_selection.model_dump()
    selection["entries"][1]["note_id"] = "note_c"

    with pytest.raises(ValidationError):
        _extension_run(extension_selection=selection)


@pytest.mark.parametrize(
    "case",
    ["batch_without_summary", "summary_without_surface", "current_with_summary"],
    ids=("batch_without_summary", "summary_without_surface", "current_with_summary"),
)
def test_extension_metadata_rejects_partial_or_current_batch_state(case: str) -> None:
    run = _extension_run()
    if case == "batch_without_summary":
        changed: dict[str, object] = {"extension_selection": None}
    elif case == "summary_without_surface":
        changed = {"collection_surface": None}
    else:
        changed = {
            "collection_surface": "extension_current",
            "extension_selection": run.extension_selection.model_dump(),
        }
    with pytest.raises(ValidationError):
        _extension_run(**changed)


def test_historical_cli_run_rejects_extension_selection_metadata() -> None:
    with pytest.raises(ValidationError):
        CollectionRun(
            run_id="historical_fixture",
            mode="search",
            input_summary="AI 工作流",
            requested_count=1,
            actual_count=0,
            started_at="2026-09-01T10:00:00+08:00",
            finished_at="2026-09-01T10:00:01+08:00",
            status=RunStatus.COMPLETE,
            notes=[],
            extension_selection=_extension_run().extension_selection.model_dump(),
        )


def test_note_record_accepts_finite_time_and_metric_evidence() -> None:
    note = _note(
        time_evidence={"kind": "edited", "raw_text": "编辑于 08-12"},
        metric_provenance={
            "likes": "detail_visible_count",
            "shares": "search_card_interface",
        },
    )

    assert note.time_evidence is not None
    assert note.time_evidence.kind == "edited"
    assert note.metric_provenance["shares"] == "search_card_interface"


def test_note_record_rejects_provenance_for_an_absent_metric() -> None:
    with pytest.raises(ValueError, match="provenance requires an exposed metric"):
        _note(
            metrics=NoteMetrics(shares=None),
            metric_provenance={"shares": "search_card_interface"},
        )


def test_note_record_rejects_unsafe_time_evidence_text() -> None:
    with pytest.raises(ValueError, match="time evidence raw_text must be safe retained public text"):
        _note(time_evidence={"kind": "edited", "raw_text": "access_token=concealed"})


@pytest.mark.parametrize(
    "raw_text",
    ["", " \t", "\u200b", "https://www.xiaohongshu.com/explore/note_a"],
    ids=("blank", "whitespace", "zero_width_space", "query_free_url"),
)
def test_note_record_rejects_empty_or_url_like_time_evidence_text(raw_text: str) -> None:
    with pytest.raises(ValueError, match="time evidence raw_text must be safe retained public text"):
        _note(time_evidence={"kind": "edited", "raw_text": raw_text})


def test_note_record_accepts_an_ordered_downloaded_media_manifest() -> None:
    first = LocalAsset(
        local_path="assets/note_a-image-001.jpg",
        mime_type="image/jpeg",
        size_bytes=3,
        sha256="a" * 64,
    )
    second = LocalAsset(
        local_path="assets/note_a-image-002.webp",
        mime_type="image/webp",
        size_bytes=4,
        sha256="b" * 64,
    )
    note = NoteRecord(
        note_id="note_a",
        canonical_url="https://www.xiaohongshu.com/explore/note_a",
        metrics=NoteMetrics(),
        source_position=1,
        media_manifest_version=2,
        media_discovered_count=2,
        media_slots=[
            NoteMediaSlot(
                note_id="note_a", role="image", position=1, status="downloaded", asset=first
            ),
            NoteMediaSlot(
                note_id="note_a", role="image", position=2, status="downloaded", asset=second
            ),
        ],
        cover_local_path=first.local_path,
        cover_asset=first,
    )

    assert [slot.position for slot in note.media_slots] == [1, 2]


def test_media_slot_requires_reason_and_no_asset_when_missing() -> None:
    slot = NoteMediaSlot(
        note_id="note_a",
        role="image",
        position=2,
        status="missing",
        missing_reason="source_not_exposed",
    )

    assert slot.asset is None
    with pytest.raises(ValueError):
        NoteMediaSlot(note_id="note_a", role="image", position=2, status="missing")


def test_versionless_legacy_cover_record_stays_readable() -> None:
    cover = LocalAsset(
        local_path="assets/note_a-cover.jpg",
        mime_type="image/jpeg",
        size_bytes=3,
        sha256="a" * 64,
    )
    note = NoteRecord(
        note_id="note_a",
        canonical_url="https://www.xiaohongshu.com/explore/note_a",
        metrics=NoteMetrics(),
        source_position=1,
        cover_local_path=cover.local_path,
        cover_asset=cover,
    )

    assert note.media_manifest_version is None and note.media_slots == []


def test_v2_empty_manifest_is_distinct_from_a_legacy_record() -> None:
    note = NoteRecord(
        note_id="note_a",
        canonical_url="https://www.xiaohongshu.com/explore/note_a",
        metrics=NoteMetrics(),
        source_position=1,
        media_manifest_version=2,
        media_discovered_count=0,
    )

    assert note.media_manifest_version == 2 and note.media_slots == []


def test_note_record_rejects_duplicate_media_slots() -> None:
    first = LocalAsset(
        local_path="assets/note_a-image-001.jpg",
        mime_type="image/jpeg",
        size_bytes=3,
        sha256="a" * 64,
    )
    slot = NoteMediaSlot(note_id="note_a", role="image", position=1, status="downloaded", asset=first)

    with pytest.raises(ValueError):
        NoteRecord(
            note_id="note_a",
            canonical_url="https://www.xiaohongshu.com/explore/note_a",
            metrics=NoteMetrics(),
            source_position=1,
            media_manifest_version=2,
            media_discovered_count=2,
            media_slots=[slot, slot],
        )


def test_note_record_rejects_a_media_position_gap() -> None:
    with pytest.raises(ValueError):
        NoteRecord(
            note_id="note_a",
            canonical_url="https://www.xiaohongshu.com/explore/note_a",
            metrics=NoteMetrics(),
            source_position=1,
            media_manifest_version=2,
            media_discovered_count=2,
            media_slots=[
                NoteMediaSlot(
                    note_id="note_a",
                    role="image",
                    position=2,
                    status="missing",
                    missing_reason="source_not_exposed",
                )
            ],
        )


def test_note_record_rejects_a_compatibility_cover_that_is_not_slot_one() -> None:
    first = LocalAsset(
        local_path="assets/note_a-image-001.jpg",
        mime_type="image/jpeg",
        size_bytes=3,
        sha256="a" * 64,
    )
    wrong = LocalAsset(
        local_path="assets/note_a-image-002.jpg",
        mime_type="image/jpeg",
        size_bytes=3,
        sha256="b" * 64,
    )

    with pytest.raises(ValueError):
        NoteRecord(
            note_id="note_a",
            canonical_url="https://www.xiaohongshu.com/explore/note_a",
            metrics=NoteMetrics(),
            source_position=1,
            media_manifest_version=2,
            media_discovered_count=1,
            media_slots=[
                NoteMediaSlot(
                    note_id="note_a", role="image", position=1, status="downloaded", asset=first
                )
            ],
            cover_local_path=wrong.local_path,
            cover_asset=wrong,
        )


def test_note_record_rejects_multiple_represented_video_slots_even_if_missing() -> None:
    with pytest.raises(ValueError):
        NoteRecord(
            note_id="note_a",
            canonical_url="https://www.xiaohongshu.com/explore/note_a",
            metrics=NoteMetrics(),
            source_position=1,
            media_manifest_version=2,
            media_discovered_count=0,
            media_slots=[
                NoteMediaSlot(
                    note_id="note_a",
                    role="video",
                    position=1,
                    status="missing",
                    missing_reason="source_not_exposed",
                ),
                NoteMediaSlot(
                    note_id="note_a",
                    role="video",
                    position=2,
                    status="rejected",
                    missing_reason="unsupported_source",
                ),
            ],
        )


def test_local_asset_retains_json_safe_integrity_metadata() -> None:
    asset = _asset()

    assert asset.model_dump(mode="json") == {
        "local_path": "assets/note_a-cover.jpg",
        "mime_type": "image/jpeg",
        "size_bytes": 3,
        "sha256": "a" * 64,
    }


@pytest.mark.parametrize(
    "values",
    [
        {
            "local_path": "assets/x.jpg",
            "mime_type": "image/jpeg",
            "size_bytes": 0,
            "sha256": "a" * 64,
        },
        {
            "local_path": "assets/x.jpg",
            "mime_type": "image/jpeg",
            "size_bytes": True,
            "sha256": "a" * 64,
        },
        {
            "local_path": "assets/x.gif",
            "mime_type": "image/gif",
            "size_bytes": 1,
            "sha256": "a" * 64,
        },
        {
            "local_path": "assets/x.jpg",
            "mime_type": "image/jpeg",
            "size_bytes": 1,
            "sha256": "bad",
        },
    ],
)
def test_local_asset_rejects_invalid_integrity_metadata(values: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        LocalAsset(**values)


@pytest.mark.parametrize(
    ("path", "mime_type"),
    [
        ("assets/cover.png", "image/jpeg"),
        ("assets/cover.jpg", "image/png"),
        ("assets/cover.webp", "image/png"),
        ("assets/cover.jpeg", "image/jpeg"),
    ],
)
def test_local_asset_requires_extension_consistent_with_mime(path: str, mime_type: str) -> None:
    with pytest.raises(ValidationError):
        LocalAsset(
            local_path=path,
            mime_type=mime_type,
            size_bytes=1,
            sha256="a" * 64,
        )


def test_note_record_rejects_query_bearing_canonical_url() -> None:
    with pytest.raises(ValidationError):
        NoteRecord(
            note_id="note_a",
            canonical_url="https://www.xiaohongshu.com/explore/note_a?xsec_token=x",
            title="Fixture",
            body="Body",
            note_type="image",
            metrics={},
            source_position=1,
        )


def test_collection_run_serialization_contains_no_sensitive_keys() -> None:
    run = CollectionRun(
        run_id="run_fixture",
        mode="search",
        input_summary="AI 工作流",
        requested_count=1,
        actual_count=0,
        started_at="2026-08-29T10:00:00+08:00",
        finished_at="2026-08-29T10:00:01+08:00",
        status=RunStatus.COMPLETE,
        notes=[],
    )

    serialized = run.model_dump_json()

    for forbidden in ("xsec_token", "Cookie", "Authorization", "web_session"):
        assert forbidden not in serialized


def test_records_retain_metrics_and_explicit_missing_fields() -> None:
    note = NoteRecord(
        note_id="note_a",
        canonical_url="https://www.xiaohongshu.com/explore/note_a",
        title=None,
        body=None,
        tags=[],
        note_type=None,
        published_at=None,
        author_id=None,
        author_name=None,
        author_profile_url=None,
        metrics={
            "likes": MetricValue(
                raw_value="1.2万",
                normalized_value=12000,
                precision="display_rounded",
            )
        },
        source_position=1,
        cover_local_path=None,
        missing_fields=["title", "body", "author_id"],
    )
    account = AccountRecord(
        account_id="author_a",
        profile_url="https://www.xiaohongshu.com/user/profile/author_a",
        avatar_local_path=None,
        name=None,
        bio=None,
        note_count=None,
        follower_count=None,
        platform_metrics={
            "获赞与收藏": MetricValue(raw_value="0", normalized_value=0, precision="exact")
        },
        missing_fields=["avatar_local_path", "name", "bio"],
    )

    assert note.metrics.likes is not None
    assert note.metrics.likes.precision == "display_rounded"
    assert note.missing_fields == ["title", "body", "author_id"]
    assert account.platform_metrics["获赞与收藏"].normalized_value == 0


def test_collection_run_requires_an_explicit_status_even_for_empty_results() -> None:
    with pytest.raises(ValidationError):
        CollectionRun(
            run_id="run_fixture",
            mode="search",
            input_summary="AI 工作流",
            requested_count=1,
            actual_count=0,
            started_at="2026-08-29T10:00:00+08:00",
            finished_at="2026-08-29T10:00:01+08:00",
            notes=[],
        )


def test_output_models_reject_undeclared_sensitive_fields() -> None:
    with pytest.raises(ValidationError):
        CollectionRun(
            run_id="run_fixture",
            mode="search",
            input_summary="AI 工作流",
            requested_count=1,
            actual_count=0,
            started_at="2026-08-29T10:00:00+08:00",
            finished_at="2026-08-29T10:00:01+08:00",
            status=RunStatus.COMPLETE,
            notes=[],
            web_session="secret",
        )


@pytest.mark.parametrize(
    ("field_name", "sensitive_key"),
    [
        ("metrics", "Authorization"),
        ("metrics", "xsec_token"),
        ("platform_metrics", "Cookie"),
        ("platform_metrics", "web_session"),
    ],
)
def test_metric_models_reject_sensitive_mapping_keys(field_name: str, sensitive_key: str) -> None:
    metric = MetricValue(raw_value="1", normalized_value=1, precision="exact")

    if field_name == "metrics":
        with pytest.raises(ValidationError):
            NoteRecord(
                note_id="note_a",
                canonical_url="https://www.xiaohongshu.com/explore/note_a",
                title="Fixture",
                body="Body",
                note_type="image",
                metrics={sensitive_key: metric},
                source_position=1,
            )
    else:
        with pytest.raises(ValidationError):
            AccountRecord(platform_metrics={sensitive_key: metric})


@pytest.mark.parametrize(
    "unsafe_path",
    [
        "",
        "/tmp/note-cover.jpg",
        "https://example.com/cover.jpg",
        "../cover.jpg",
        "assets/../cover.jpg",
        ".",
        "..",
    ],
)
def test_note_record_rejects_unsafe_cover_local_paths(unsafe_path: str) -> None:
    with pytest.raises(ValidationError):
        NoteRecord(
            note_id="note_a",
            canonical_url="https://www.xiaohongshu.com/explore/note_a",
            title="Fixture",
            body="Body",
            note_type="image",
            metrics={},
            source_position=1,
            cover_local_path=unsafe_path,
        )


@pytest.mark.parametrize(
    "unsafe_path",
    [
        "",
        "/tmp/avatar.jpg",
        "file:///tmp/avatar.jpg",
        "../avatar.jpg",
        "assets/../avatar.jpg",
        ".",
        "..",
    ],
)
def test_account_record_rejects_unsafe_avatar_local_paths(unsafe_path: str) -> None:
    with pytest.raises(ValidationError):
        AccountRecord(avatar_local_path=unsafe_path)


def test_local_asset_paths_allow_safe_relative_paths() -> None:
    note = NoteRecord(
        note_id="note_a",
        canonical_url="https://www.xiaohongshu.com/explore/note_a",
        title="Fixture",
        body="Body",
        note_type="image",
        metrics={},
        source_position=1,
        cover_local_path="assets/note_a-cover.jpg",
        cover_asset=_asset("assets/note_a-cover.jpg"),
    )
    account = AccountRecord(
        avatar_local_path="assets/author_a-avatar.jpg",
        avatar_asset=_asset("assets/author_a-avatar.jpg"),
    )

    assert note.cover_local_path == "assets/note_a-cover.jpg"
    assert account.avatar_local_path == "assets/author_a-avatar.jpg"


@pytest.mark.parametrize(
    "factory",
    [
        lambda: NoteRecord(
            note_id="note_a",
            canonical_url="https://www.xiaohongshu.com/explore/note_a",
            title="Fixture",
            body="Body",
            note_type="image",
            metrics={},
            source_position=1,
            cover_asset=_asset("assets/note_a-cover.jpg"),
        ),
        lambda: AccountRecord(
            avatar_local_path="assets/account-avatar.jpg",
            avatar_asset=_asset("assets/different-avatar.jpg"),
        ),
    ],
)
def test_records_require_matching_asset_metadata_and_local_path(factory: object) -> None:
    assert callable(factory)
    with pytest.raises(ValidationError):
        factory()


def test_records_allow_legacy_path_only_assets_but_not_metadata_only_assets() -> None:
    note = NoteRecord(
        note_id="note_a",
        canonical_url="https://www.xiaohongshu.com/explore/note_a",
        title="Fixture",
        body="Body",
        note_type="image",
        metrics={},
        source_position=1,
        cover_local_path="assets/note_a-cover.jpg",
    )
    account = AccountRecord(avatar_local_path="assets/account-avatar.jpg")

    assert note.cover_asset is None
    assert account.avatar_asset is None


@pytest.mark.parametrize(
    ("raw_value", "normalized_value", "precision"),
    [
        ("not shown", None, "not_exposed"),
        (None, 1, "not_exposed"),
        (None, None, "exact"),
        (None, 1000, "display_rounded"),
        ("1千", None, "display_rounded"),
    ],
)
def test_metric_value_rejects_inconsistent_precision(
    raw_value: str | None,
    normalized_value: int | None,
    precision: str,
) -> None:
    with pytest.raises(ValidationError):
        MetricValue(
            raw_value=raw_value,
            normalized_value=normalized_value,
            precision=precision,
        )


@pytest.mark.parametrize("raw_value", ["1.2万", "1w", "1K", "1+", "1.2", "1,200.5"])
def test_metric_value_rejects_rounded_or_abbreviated_exact_raw_values(raw_value: str) -> None:
    with pytest.raises(ValidationError):
        MetricValue(raw_value=raw_value, normalized_value=12000, precision="exact")


def test_metric_value_rejects_exact_raw_value_that_disagrees_with_normalized_value() -> None:
    with pytest.raises(ValidationError):
        MetricValue(raw_value="12,000", normalized_value=12001, precision="exact")


@pytest.mark.parametrize(
    ("raw_value", "normalized_value"),
    [(None, 12000), ("12000", 12000), ("12,000", 12000)],
)
def test_metric_value_allows_provable_exact_raw_values(
    raw_value: str | None, normalized_value: int
) -> None:
    metric = MetricValue(
        raw_value=raw_value,
        normalized_value=normalized_value,
        precision="exact",
    )

    assert metric.normalized_value == normalized_value


def test_account_record_retains_safe_platform_metric_label_without_renaming() -> None:
    account = AccountRecord(
        platform_metrics={
            "获赞与收藏": MetricValue(raw_value="12,000", normalized_value=12000, precision="exact")
        }
    )

    assert list(account.platform_metrics) == ["获赞与收藏"]
    assert account.platform_metrics["获赞与收藏"].normalized_value == 12000


@pytest.mark.parametrize(
    "unsafe_label",
    [
        "",
        "https://example.com/metric",
        "AUTHORIZATION",
        "xsec-token",
        " WEB SESSION ",
        "access_token",
        "access-token",
        "ACCESS-TOKEN",
        "refresh_token",
        "refresh-token",
        "REFRESH-TOKEN",
        "Cookies",
        "COOKIES",
        "a1",
        "signature",
        "sign",
        "token",
    ],
)
def test_account_record_rejects_unsafe_platform_metric_labels(unsafe_label: str) -> None:
    with pytest.raises(ValidationError):
        AccountRecord(
            platform_metrics={
                unsafe_label: MetricValue(raw_value="1", normalized_value=1, precision="exact")
            }
        )


@pytest.mark.parametrize(
    "unsafe_key",
    [b"access_token", b"Cookies", b"WEB SESSION", b"\xe8\x8e\xb7\xe8\xb5\x9e", 1, ("metric",)],
)
def test_account_record_rejects_non_string_platform_metric_keys_before_coercion(
    unsafe_key: object,
) -> None:
    with pytest.raises(ValidationError):
        AccountRecord(
            platform_metrics={
                unsafe_key: MetricValue(raw_value="1", normalized_value=1, precision="exact")
            }
        )


@pytest.mark.parametrize("platform_metrics", [[], [("获赞与收藏", 1)]])
def test_account_record_rejects_non_dict_platform_metrics_before_coercion(
    platform_metrics: object,
) -> None:
    with pytest.raises(ValidationError):
        AccountRecord(platform_metrics=platform_metrics)


def test_account_record_safely_rejects_an_untrusted_dict_subclass() -> None:
    class ExplodingDict(dict[str, MetricValue]):
        def __iter__(self) -> object:
            raise RuntimeError("untrusted iterator detail must not leak")

    with pytest.raises(ValidationError) as error:
        AccountRecord(platform_metrics=ExplodingDict())

    assert "untrusted iterator detail must not leak" not in str(error.value)


def test_account_record_rejects_other_mapping_subclasses() -> None:
    with pytest.raises(ValidationError):
        AccountRecord(
            platform_metrics=UserDict(
                {"获赞与收藏": MetricValue(raw_value="1", normalized_value=1, precision="exact")}
            )
        )


def test_account_evidence_models_are_additive_and_finite() -> None:
    historical = AccountRecord.model_validate_json('{"account_id": "author_a"}')
    assert historical.field_statuses == {}

    account = AccountRecord(field_statuses={"bio": "exposed_empty"})
    assert account.field_statuses == {"bio": "exposed_empty"}

    pacing = PacingSummary(
        policy="conservative_jitter_v1",
        profile_open_delay_ms=2_000,
        detail_delay_ms=[3_000, 7_000],
    )
    attempt = CandidateAttempt(
        position=1,
        note_id="note_a",
        outcome="complete",
        stage="detail_projection",
    )

    assert pacing.detail_delay_ms == [3_000, 7_000]
    assert PacingSummary.model_validate_json(pacing.model_dump_json()) == pacing
    assert attempt.reason is None
    assert CandidateAttempt.model_validate_json(attempt.model_dump_json()) == attempt


def test_collection_run_historical_json_defaults_account_evidence_fields() -> None:
    historical_json = """{
        "run_id": "run_fixture",
        "mode": "search",
        "input_summary": "AI 工作流",
        "requested_count": 1,
        "actual_count": 1,
        "started_at": "2026-08-29T10:00:00+08:00",
        "finished_at": "2026-08-29T10:00:01+08:00",
        "status": "complete",
        "account": {
            "account_id": "author_a"
        },
        "notes": [{
            "note_id": "note_a",
            "canonical_url": "https://www.xiaohongshu.com/explore/note_a",
            "metrics": {},
            "source_position": 1
        }]
    }"""

    reloaded = CollectionRun.model_validate_json(historical_json)

    assert reloaded.candidate_attempts == []
    assert reloaded.pacing_summary is None
    assert reloaded.account is not None
    assert reloaded.account.field_statuses == {}


@pytest.mark.parametrize("value", ([2_999], [7_001], [True], ["3000"]))
def test_pacing_summary_rejects_out_of_range_or_non_integer_detail_delays(
    value: list[object],
) -> None:
    with pytest.raises(ValidationError):
        PacingSummary(
            policy="conservative_jitter_v1",
            profile_open_delay_ms=2_000,
            detail_delay_ms=value,
        )


@pytest.mark.parametrize("profile_open_delay_ms", [True, "2000", 1_999, 5_001])
def test_pacing_summary_rejects_non_integer_or_out_of_range_profile_delay(
    profile_open_delay_ms: object,
) -> None:
    with pytest.raises(ValidationError):
        PacingSummary(
            policy="conservative_jitter_v1",
            profile_open_delay_ms=profile_open_delay_ms,
            detail_delay_ms=[3_000],
        )


def test_account_evidence_models_reject_extra_or_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        AccountRecord(field_statuses={"unknown": "exposed"})
    with pytest.raises(ValidationError):
        AccountRecord(field_statuses={"name": "exposed_empty"})
    with pytest.raises(ValidationError):
        PacingSummary(
            policy="conservative_jitter_v1",
            profile_open_delay_ms=2_000,
            detail_delay_ms=[3_000],
            extra="forbidden",
        )


@pytest.mark.parametrize(
    ("outcome", "stage", "reason", "note_id"),
    [
        ("complete", "detail_projection", None, "note_a"),
        ("unavailable", "candidate", "candidate_shortfall", None),
        ("rejected", "candidate", "candidate_invalid", None),
        ("rejected", "candidate", "candidate_invalid", "note_a"),
        ("unavailable", "card_resolution", "card_not_found", "note_a"),
        ("unavailable", "card_resolution", "card_offscreen", "note_a"),
        ("rejected", "card_resolution", "card_unsafe", "note_a"),
        ("unavailable", "detail_navigation", "detail_timeout", "note_a"),
        ("unavailable", "detail_navigation", "detail_route_mismatch", "note_a"),
        ("unavailable", "detail_navigation", "upstream_unavailable", "note_a"),
        ("unavailable", "detail_projection", "detail_projection_unavailable", "note_a"),
        ("rejected", "detail_projection", "media_scope_invalid", "note_a"),
    ],
)
def test_candidate_attempt_accepts_only_each_legal_attempt_combination(
    outcome: str, stage: str, reason: str | None, note_id: str | None
) -> None:
    attempt = CandidateAttempt(
        position=1,
        note_id=note_id,
        outcome=outcome,
        stage=stage,
        reason=reason,
    )

    assert attempt.model_dump() == {
        "position": 1,
        "note_id": note_id,
        "outcome": outcome,
        "stage": stage,
        "reason": reason,
    }


@pytest.mark.parametrize(
    ("outcome", "stage", "reason", "note_id"),
    [
        ("complete", "detail_projection", "media_scope_invalid", "note_a"),
        ("complete", "detail_projection", None, None),
        ("unavailable", "candidate", None, None),
        ("unavailable", "candidate", "candidate_shortfall", "note_a"),
        ("unavailable", "card_resolution", "card_unsafe", "note_a"),
        ("unavailable", "card_resolution", "card_not_found", None),
        ("rejected", "card_resolution", "card_unsafe", None),
        ("rejected", "detail_navigation", "detail_timeout", "note_a"),
        ("unavailable", "detail_projection", "media_scope_invalid", "note_a"),
        ("rejected", "detail_projection", "detail_projection_unavailable", "note_a"),
    ],
)
def test_candidate_attempt_rejects_illegal_combinations(
    outcome: str, stage: str, reason: str | None, note_id: str | None
) -> None:
    with pytest.raises(ValidationError):
        CandidateAttempt(
            position=1,
            note_id=note_id,
            outcome=outcome,
            stage=stage,
            reason=reason,
        )


@pytest.mark.parametrize(
    "note_id",
    ["note a", "note/a", "https://example.com/note_a", "access_token", "xsec_token", "a1"],
)
def test_candidate_attempt_rejects_unsafe_non_null_note_id(note_id: str) -> None:
    with pytest.raises(ValidationError):
        CandidateAttempt(
            position=1,
            note_id=note_id,
            outcome="complete",
            stage="detail_projection",
        )


@pytest.mark.parametrize("position", [0, 11, True])
def test_candidate_attempt_rejects_out_of_range_or_non_integer_position(position: object) -> None:
    with pytest.raises(ValidationError):
        CandidateAttempt(
            position=position,
            note_id="note_a",
            outcome="complete",
            stage="detail_projection",
        )
