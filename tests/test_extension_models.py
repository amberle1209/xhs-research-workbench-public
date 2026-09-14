"""Strict, untrusted Chrome-extension wire-model tests."""

from __future__ import annotations

from copy import deepcopy

import pytest
from pydantic import TypeAdapter, ValidationError

from xhs_workbench.extension_models import (
    NATIVE_REQUEST_ADAPTER,
    NATIVE_RESPONSE_ADAPTER,
    REQUEST_TO_ALLOWED_RESPONSE_KINDS,
    ErrorResponse,
    ExtensionBeginJob,
    ExtensionCandidateSnapshot,
    ExtensionCandidateUnavailable,
    NativeRequest,
    validate_response_for_request,
)


def _begin_job(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "protocol_version": "1.0",
        "kind": "begin_job",
        "job_id": "job_123",
        "collection_surface": "extension_search",
        "source_page_url": "https://www.xiaohongshu.com/explore/source_123",
        "requested_count": 5,
        "candidate_scan_limit": 30,
        "publication_cutoff": "2026-08-01T00:00:00+08:00",
        "selection_order": "exact_likes_desc",
    }
    values.update(overrides)
    return values


def _snapshot(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "protocol_version": "1.0",
        "kind": "candidate_snapshot",
        "job_id": "job_123",
        "source_position": 1,
        "note_id": "note_123",
        "canonical_url": "https://www.xiaohongshu.com/explore/note_123",
        "title": "标题",
        "body": "正文",
        "tags": ["AI"],
        "note_type": "normal",
        "published_at": "2026-08-01T10:00:00+08:00",
        "time_evidence": {"kind": "published", "raw_text": "发布于 2026-08-01 10:00"},
        "author_id": "author_123",
        "author_name": "作者",
        "author_profile_url": "https://www.xiaohongshu.com/user/profile/author_123",
        "metrics": {
            "likes": {"raw_value": "25", "normalized_value": 25, "precision": "exact"}
        },
        "metric_provenance": {"likes": "detail_visible_count"},
        "media_slots": [{"note_id": "note_123", "role": "image", "position": 1}],
    }
    values.update(overrides)
    return values


def test_accepts_strict_begin_job_and_candidate_snapshot() -> None:
    begin = ExtensionBeginJob(**_begin_job())
    snapshot = ExtensionCandidateSnapshot(**_snapshot())

    assert begin.candidate_scan_limit == 30
    assert snapshot.metrics.likes is not None
    assert snapshot.metrics.likes.normalized_value == 25


def test_progress_allows_saved_media_to_exceed_the_number_of_selected_posts() -> None:
    progress = NATIVE_RESPONSE_ADAPTER.validate_python(
        {
            "protocol_version": "1.0",
            "kind": "progress",
            "job_id": "job_media",
            "phase": "downloading",
            "discovered": 1,
            "inspected": 1,
            "eligible": 1,
            "selected": 1,
            "saved": 3,
        }
    )

    assert progress.saved == 3


def test_page_order_begin_job_is_search_only_and_has_no_cutoff() -> None:
    begin = ExtensionBeginJob(
        **_begin_job(
            requested_count=5,
            candidate_scan_limit=5,
            publication_cutoff=None,
            selection_order="page_order",
            source_page_url="https://www.xiaohongshu.com/search_result",
        )
    )

    assert begin.selection_order == "page_order"
    with pytest.raises(ValidationError):
        ExtensionBeginJob(**_begin_job(selection_order="page_order"))


def test_accepts_only_bounded_correlated_detail_unavailable_fact() -> None:
    unavailable = ExtensionCandidateUnavailable(
        protocol_version="1.0",
        kind="candidate_unavailable",
        job_id="job_123",
        source_position=1,
        note_id="note_123",
        reason="detail_unavailable",
    )

    assert unavailable.model_dump() == {
        "protocol_version": "1.0",
        "kind": "candidate_unavailable",
        "job_id": "job_123",
        "source_position": 1,
        "note_id": "note_123",
        "reason": "detail_unavailable",
    }


def test_terminal_cause_and_finish_scan_extensions_are_strict_and_additive() -> None:
    finish = NATIVE_REQUEST_ADAPTER.validate_python(
        {"protocol_version": "1.0", "kind": "finish_scan", "job_id": "job_123", "scroll_rounds": 2, "sort_label": "综合"}
    )
    stop = NATIVE_REQUEST_ADAPTER.validate_python(
        {"protocol_version": "1.0", "kind": "stop_job", "job_id": "job_123", "terminal_cause": "identity_mismatch"}
    )
    assert finish.scroll_rounds == 2
    assert finish.sort_label == "综合"
    assert stop.terminal_cause == "identity_mismatch"
    for payload in (
        {"protocol_version": "1.0", "kind": "finish_scan", "job_id": "job_123", "scroll_rounds": 3},
        {"protocol_version": "1.0", "kind": "finish_scan", "job_id": "job_123", "unexpected": True},
        {"protocol_version": "1.0", "kind": "stop_job", "job_id": "job_123", "terminal_cause": "unknown"},
        {"protocol_version": "1.0", "kind": "stop_job", "job_id": "job_123", "unexpected": True},
    ):
        with pytest.raises(ValidationError):
            NATIVE_REQUEST_ADAPTER.validate_python(payload)


@pytest.mark.parametrize(
    ("factory", "field", "value"),
    [
        (_begin_job, "requested_count", True),
        (_begin_job, "requested_count", "5"),
        (_begin_job, "requested_count", 5.0),
        (_snapshot, "source_position", True),
        (_snapshot, "source_position", "1"),
        (_snapshot, "source_position", 1.0),
    ],
)
def test_rejects_non_exact_outer_integer_values(
    factory: object, field: str, value: object
) -> None:
    assert callable(factory)
    payload = factory(**{field: value})
    model = ExtensionBeginJob if factory is _begin_job else ExtensionCandidateSnapshot

    with pytest.raises(ValidationError):
        model(**payload)


@pytest.mark.parametrize("value", [True, "25", 25.0])
def test_rejects_non_exact_nested_metric_integer(value: object) -> None:
    payload = _snapshot()
    metrics = deepcopy(payload["metrics"])
    assert isinstance(metrics, dict)
    likes = metrics["likes"]
    assert isinstance(likes, dict)
    likes["normalized_value"] = value

    with pytest.raises(ValidationError):
        ExtensionCandidateSnapshot(**_snapshot(metrics=metrics))


@pytest.mark.parametrize(
    "published_at",
    ["2026-08-01", "2026-08-01T10:00:00", "2026-08-01T10:00:00Z", "not-a-date"],
)
def test_rejects_malformed_or_offset_free_published_at(published_at: str) -> None:
    with pytest.raises(ValidationError):
        ExtensionCandidateSnapshot(**_snapshot(published_at=published_at))


@pytest.mark.parametrize(
    "payload",
    [
        _begin_job(requested_count=21),
        _begin_job(candidate_scan_limit=101),
        _begin_job(requested_count=31, candidate_scan_limit=30),
        _snapshot(title="x" * 201),
        _snapshot(body="x" * 20_001),
        _snapshot(tags=["tag"] * 101),
        _snapshot(canonical_url="https://www.xiaohongshu.com/explore/note_123?x=1"),
        _snapshot(author_profile_url="https://www.xiaohongshu.com/user/profile/author_123#x"),
        _snapshot(
            media_slots=[
                {"note_id": "note_123", "role": "image", "position": 1},
                {"note_id": "note_123", "role": "image", "position": 1},
            ]
        ),
        _snapshot(
            media_slots=[{"note_id": "note_123", "role": "image", "position": item} for item in range(1, 24)]
        ),
        _snapshot(
            media_slots=[
                {"note_id": "note_123", "role": "image", "position": 1},
                {"note_id": "note_123", "role": "video", "position": 1},
            ]
        ),
        _snapshot(media_slots=[{"note_id": "note_123", "role": "image", "position": 1, "url": "https://example.com/media.jpg"}]),
        _snapshot(title="token=secret"),
    ],
)
def test_rejects_out_of_contract_or_sensitive_candidate_input(payload: dict[str, object]) -> None:
    model = ExtensionBeginJob if payload["kind"] == "begin_job" else ExtensionCandidateSnapshot
    with pytest.raises(ValidationError):
        model(**payload)


def test_native_request_discriminator_rejects_unknown_and_extra_fields() -> None:
    adapter = TypeAdapter(NativeRequest)
    with pytest.raises(ValidationError):
        adapter.validate_python(_begin_job(unexpected="no"))
    with pytest.raises(ValidationError):
        adapter.validate_python({"protocol_version": "1.0", "kind": "unknown"})


@pytest.mark.parametrize(
    "payload",
    [
        {
            "protocol_version": "1.0",
            "kind": "selection_result",
            "job_id": "job_123",
            "scanned_count": 1,
            "eligible_count": 1,
            "selected_count": 1,
            "status": "complete",
            "selected": [{"note_id": "note_123", "selection_rank": 2}],
        },
        {
            "protocol_version": "1.0",
            "kind": "media_result",
            "job_id": "job_123",
            "note_id": "note_123",
            "role": "image",
            "position": 1,
            "outcome": "missing",
        },
        {
            "protocol_version": "1.0",
            "kind": "job_result",
            "job_id": "job_123",
            "status": "complete",
            "retained_count": 1,
            "report_available": False,
            "report_file": "index.html",
        },
    ],
)
def test_rejects_invalid_conditional_response_fields(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        NATIVE_RESPONSE_ADAPTER.validate_python(payload)


def test_health_and_error_public_shapes_keep_job_id_optional_only_for_error() -> None:
    with pytest.raises(ValidationError):
        NATIVE_RESPONSE_ADAPTER.validate_python(
            {
                "protocol_version": "1.0",
                "kind": "health_result",
                "status": "ready",
                "host_version": "0.1.0",
                "job_id": "job_123",
            }
        )
    assert ErrorResponse(protocol_version="1.0", kind="error", code="invalid_request", fatal=True).model_dump(
        exclude_none=True
    ) == {"protocol_version": "1.0", "kind": "error", "code": "invalid_request", "fatal": True}


def test_request_response_mapping_is_exact() -> None:
    assert REQUEST_TO_ALLOWED_RESPONSE_KINDS == {
        "health": frozenset({"health_result", "error"}),
        "begin_job": frozenset({"job_started", "error"}),
        "candidate_snapshot": frozenset({"candidate_result", "error"}),
        "candidate_unavailable": frozenset({"candidate_result", "error"}),
        "finish_scan": frozenset({"selection_result", "error"}),
        "media_begin": frozenset({"progress", "media_result", "error"}),
        "media_chunk": frozenset({"progress", "error"}),
        "media_end": frozenset({"media_result", "error"}),
        "media_missing": frozenset({"media_result", "error"}),
        "finish_job": frozenset({"job_result", "error"}),
        "stop_job": frozenset({"job_result", "error"}),
        "open_report": frozenset({"report_result", "error"}),
        "video_status": frozenset({"video_result", "error"}),
        "video_stop": frozenset({"video_result", "error"}),
    }


@pytest.mark.parametrize(
    "payload",
    [
        {
            "protocol_version": "1.0",
            "kind": "media_begin",
            "job_id": "job_123",
            "note_id": "note_123",
            "role": "video",
            "position": 2,
            "sequence": 1,
            "size_limit_bytes": 1,
        },
        {
            "protocol_version": "1.0",
            "kind": "media_chunk",
            "job_id": "job_123",
            "note_id": "note_123",
            "role": "video_cover",
            "position": 2,
            "sequence": 1,
            "chunk_index": 0,
            "data_base64": "aGVsbG8=",
        },
    ],
)
def test_rejects_non_image_slots_outside_their_declared_position(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(NativeRequest).validate_python(payload)


@pytest.mark.parametrize(
    "field",
    ["title", "body", "note_type", "author_name"],
)
def test_rejects_query_free_media_urls_in_free_candidate_text(field: str) -> None:
    with pytest.raises(ValidationError):
        ExtensionCandidateSnapshot(**_snapshot(**{field: "https://cdn.example/video.mp4"}))


@pytest.mark.parametrize(
    "nested_path",
    [
        ("tags", 0),
        ("time_evidence", "raw_text"),
        ("metrics", "likes", "raw_value"),
    ],
)
def test_rejects_query_free_media_urls_in_nested_free_candidate_text(
    nested_path: tuple[str, object] | tuple[str, str, str],
) -> None:
    payload = _snapshot()
    _set_nested(payload, nested_path, "https://cdn.example/video.mp4")

    with pytest.raises(ValidationError):
        ExtensionCandidateSnapshot(**payload)


def test_exact_metric_raw_value_must_be_canonical_and_equal_to_normalized_value() -> None:
    mismatched = _snapshot()
    _set_nested(mismatched, ("metrics", "likes", "raw_value"), "24")
    with pytest.raises(ValidationError):
        ExtensionCandidateSnapshot(**mismatched)

    leading_zero = _snapshot()
    _set_nested(leading_zero, ("metrics", "likes", "raw_value"), "025")
    with pytest.raises(ValidationError):
        ExtensionCandidateSnapshot(**leading_zero)


@pytest.mark.parametrize("value", ["0", "9007199254740991"])
def test_accepts_exact_metric_raw_value_at_canonical_boundaries(value: str) -> None:
    payload = _snapshot()
    _set_nested(payload, ("metrics", "likes", "raw_value"), value)
    _set_nested(payload, ("metrics", "likes", "normalized_value"), int(value))

    assert ExtensionCandidateSnapshot(**payload).metrics.likes is not None


def test_rejects_native_integers_that_cannot_round_trip_through_javascript() -> None:
    payload = _snapshot()
    _set_nested(payload, ("metrics", "likes", "raw_value"), "9007199254740992")
    _set_nested(payload, ("metrics", "likes", "normalized_value"), 9_007_199_254_740_992)

    with pytest.raises(ValidationError):
        ExtensionCandidateSnapshot(**payload)
    with pytest.raises(ValidationError):
        NATIVE_REQUEST_ADAPTER.validate_python(
            {
                "protocol_version": "1.0",
                "kind": "media_chunk",
                "job_id": "job_123",
                "note_id": "note_123",
                "role": "image",
                "position": 1,
                "sequence": 1,
                "chunk_index": 9_007_199_254_740_992,
                "data_base64": "aGVsbG8=",
            }
        )


def test_python_contract_matches_javascript_url_and_unicode_boundaries() -> None:
    with pytest.raises(ValidationError):
        ExtensionBeginJob(
            **_begin_job(source_page_url="https://www.xiaohongshu.com:443/explore/source_123")
        )

    assert ExtensionCandidateSnapshot(**_snapshot(title="😀" * 200)).title == "😀" * 200
    with pytest.raises(ValidationError):
        ExtensionCandidateSnapshot(**_snapshot(title="😀" * 201))


@pytest.mark.parametrize(
    ("adapter", "payload", "nested_path"),
    [
        (NATIVE_REQUEST_ADAPTER, _begin_job(), ("requested_count",)),
        (NATIVE_REQUEST_ADAPTER, _begin_job(), ("candidate_scan_limit",)),
        (NATIVE_REQUEST_ADAPTER, _snapshot(), ("source_position",)),
        (NATIVE_REQUEST_ADAPTER, _snapshot(), ("metrics", "likes", "normalized_value")),
        (NATIVE_REQUEST_ADAPTER, _snapshot(), ("media_slots", 0, "position")),
        (
            NATIVE_REQUEST_ADAPTER,
            {
                "protocol_version": "1.0", "kind": "media_begin", "job_id": "job_123", "note_id": "note_123", "role": "image", "position": 1, "sequence": 1, "size_limit_bytes": 1,
            },
            ("position",),
        ),
        (
            NATIVE_REQUEST_ADAPTER,
            {
                "protocol_version": "1.0", "kind": "media_begin", "job_id": "job_123", "note_id": "note_123", "role": "image", "position": 1, "sequence": 1, "size_limit_bytes": 1,
            },
            ("sequence",),
        ),
        (
            NATIVE_REQUEST_ADAPTER,
            {
                "protocol_version": "1.0", "kind": "media_begin", "job_id": "job_123", "note_id": "note_123", "role": "image", "position": 1, "sequence": 1, "size_limit_bytes": 1,
            },
            ("size_limit_bytes",),
        ),
        (
            NATIVE_REQUEST_ADAPTER,
            {
                "protocol_version": "1.0", "kind": "media_chunk", "job_id": "job_123", "note_id": "note_123", "role": "image", "position": 1, "sequence": 1, "chunk_index": 0, "data_base64": "aGVsbG8=",
            },
            ("position",),
        ),
        (
            NATIVE_REQUEST_ADAPTER,
            {
                "protocol_version": "1.0", "kind": "media_chunk", "job_id": "job_123", "note_id": "note_123", "role": "image", "position": 1, "sequence": 1, "chunk_index": 0, "data_base64": "aGVsbG8=",
            },
            ("sequence",),
        ),
        (
            NATIVE_REQUEST_ADAPTER,
            {
                "protocol_version": "1.0", "kind": "media_chunk", "job_id": "job_123", "note_id": "note_123", "role": "image", "position": 1, "sequence": 1, "chunk_index": 0, "data_base64": "aGVsbG8=",
            },
            ("chunk_index",),
        ),
        (
            NATIVE_REQUEST_ADAPTER,
            {
                "protocol_version": "1.0", "kind": "media_end", "job_id": "job_123", "note_id": "note_123", "role": "image", "position": 1, "sequence": 1, "mime_type": "image/webp", "sha256": "a" * 64,
            },
            ("position",),
        ),
        (
            NATIVE_REQUEST_ADAPTER,
            {
                "protocol_version": "1.0", "kind": "media_end", "job_id": "job_123", "note_id": "note_123", "role": "image", "position": 1, "sequence": 1, "mime_type": "image/webp", "sha256": "a" * 64,
            },
            ("sequence",),
        ),
        (
            NATIVE_REQUEST_ADAPTER,
            {"protocol_version": "1.0", "kind": "media_missing", "job_id": "job_123", "note_id": "note_123", "role": "image", "position": 1, "reason": "source_not_exposed"},
            ("position",),
        ),
        (
            NATIVE_RESPONSE_ADAPTER,
            {"protocol_version": "1.0", "kind": "candidate_result", "job_id": "job_123", "note_id": "note_123", "source_position": 1, "outcome": "recorded"},
            ("source_position",),
        ),
        (
            NATIVE_RESPONSE_ADAPTER,
            {"protocol_version": "1.0", "kind": "selection_result", "job_id": "job_123", "scanned_count": 1, "eligible_count": 1, "selected_count": 1, "status": "complete", "selected": [{"note_id": "note_123", "selection_rank": 1}]},
            ("scanned_count",),
        ),
        (
            NATIVE_RESPONSE_ADAPTER,
            {"protocol_version": "1.0", "kind": "selection_result", "job_id": "job_123", "scanned_count": 1, "eligible_count": 1, "selected_count": 1, "status": "complete", "selected": [{"note_id": "note_123", "selection_rank": 1}]},
            ("eligible_count",),
        ),
        (
            NATIVE_RESPONSE_ADAPTER,
            {"protocol_version": "1.0", "kind": "selection_result", "job_id": "job_123", "scanned_count": 1, "eligible_count": 1, "selected_count": 1, "status": "complete", "selected": [{"note_id": "note_123", "selection_rank": 1}]},
            ("selected_count",),
        ),
        (
            NATIVE_RESPONSE_ADAPTER,
            {"protocol_version": "1.0", "kind": "selection_result", "job_id": "job_123", "scanned_count": 1, "eligible_count": 1, "selected_count": 1, "status": "complete", "selected": [{"note_id": "note_123", "selection_rank": 1}]},
            ("selected", 0, "selection_rank"),
        ),
        (
            NATIVE_RESPONSE_ADAPTER,
            {"protocol_version": "1.0", "kind": "progress", "job_id": "job_123", "phase": "downloading", "discovered": 1, "inspected": 1, "eligible": 1, "selected": 1, "saved": 0, "current_source_position": 1},
            ("discovered",),
        ),
        (
            NATIVE_RESPONSE_ADAPTER,
            {"protocol_version": "1.0", "kind": "progress", "job_id": "job_123", "phase": "downloading", "discovered": 1, "inspected": 1, "eligible": 1, "selected": 1, "saved": 0, "current_source_position": 1},
            ("inspected",),
        ),
        (
            NATIVE_RESPONSE_ADAPTER,
            {"protocol_version": "1.0", "kind": "progress", "job_id": "job_123", "phase": "downloading", "discovered": 1, "inspected": 1, "eligible": 1, "selected": 1, "saved": 0, "current_source_position": 1},
            ("eligible",),
        ),
        (
            NATIVE_RESPONSE_ADAPTER,
            {"protocol_version": "1.0", "kind": "progress", "job_id": "job_123", "phase": "downloading", "discovered": 1, "inspected": 1, "eligible": 1, "selected": 1, "saved": 0, "current_source_position": 1},
            ("selected",),
        ),
        (
            NATIVE_RESPONSE_ADAPTER,
            {"protocol_version": "1.0", "kind": "progress", "job_id": "job_123", "phase": "downloading", "discovered": 1, "inspected": 1, "eligible": 1, "selected": 1, "saved": 0, "current_source_position": 1},
            ("saved",),
        ),
        (
            NATIVE_RESPONSE_ADAPTER,
            {"protocol_version": "1.0", "kind": "progress", "job_id": "job_123", "phase": "downloading", "discovered": 1, "inspected": 1, "eligible": 1, "selected": 1, "saved": 0, "current_source_position": 1},
            ("current_source_position",),
        ),
        (
            NATIVE_RESPONSE_ADAPTER,
            {"protocol_version": "1.0", "kind": "media_result", "job_id": "job_123", "note_id": "note_123", "role": "image", "position": 1, "outcome": "downloaded"},
            ("position",),
        ),
        (
            NATIVE_RESPONSE_ADAPTER,
            {"protocol_version": "1.0", "kind": "job_result", "job_id": "job_123", "status": "complete", "retained_count": 1, "report_available": True, "report_file": "index.html"},
            ("retained_count",),
        ),
    ],
)
@pytest.mark.parametrize("invalid", [True, "1", 1.0])
def test_rejects_bool_string_and_float_for_every_wire_integer(
    adapter: TypeAdapter[object], payload: dict[str, object], nested_path: tuple[object, ...], invalid: object
) -> None:
    changed = deepcopy(payload)
    _set_nested(changed, nested_path, invalid)

    with pytest.raises(ValidationError):
        adapter.validate_python(changed)


def test_response_correlation_rejects_health_job_error_and_job_mismatch_or_omission() -> None:
    health = NATIVE_REQUEST_ADAPTER.validate_python({"protocol_version": "1.0", "kind": "health"})
    job = NATIVE_REQUEST_ADAPTER.validate_python(_begin_job())
    health_error = NATIVE_RESPONSE_ADAPTER.validate_python(
        {"protocol_version": "1.0", "kind": "error", "job_id": "job_123", "code": "invalid_request", "fatal": True}
    )
    mismatch = NATIVE_RESPONSE_ADAPTER.validate_python(
        {"protocol_version": "1.0", "kind": "error", "job_id": "job_other", "code": "invalid_request", "fatal": True}
    )
    missing = NATIVE_RESPONSE_ADAPTER.validate_python(
        {"protocol_version": "1.0", "kind": "error", "code": "invalid_request", "fatal": True}
    )

    for request, response in [(health, health_error), (job, mismatch), (job, missing)]:
        with pytest.raises(ValueError):
            validate_response_for_request(request, response)


def test_response_correlation_accepts_each_job_request_error_with_matching_job_id() -> None:
    for kind in sorted(set(REQUEST_TO_ALLOWED_RESPONSE_KINDS) - {"health"}):
        request = _request_for_kind(kind)
        response = ErrorResponse(protocol_version="1.0", kind="error", job_id="job_123", code="invalid_state", fatal=False)
        assert validate_response_for_request(request, response) is response


def test_response_correlation_requires_candidate_and_media_slot_echoes() -> None:
    candidate_request = NATIVE_REQUEST_ADAPTER.validate_python(_snapshot())
    wrong_candidate = NATIVE_RESPONSE_ADAPTER.validate_python(
        {
            "protocol_version": "1.0",
            "kind": "candidate_result",
            "job_id": "job_123",
            "note_id": "note_other",
            "source_position": 1,
            "outcome": "recorded",
        }
    )
    wrong_candidate_position = NATIVE_RESPONSE_ADAPTER.validate_python(
        {
            "protocol_version": "1.0",
            "kind": "candidate_result",
            "job_id": "job_123",
            "note_id": "note_123",
            "source_position": 2,
            "outcome": "recorded",
        }
    )
    media_request = NATIVE_REQUEST_ADAPTER.validate_python(
        {
            "protocol_version": "1.0",
            "kind": "media_end",
            "job_id": "job_123",
            "note_id": "note_123",
            "role": "image",
            "position": 1,
            "sequence": 1,
            "mime_type": "image/webp",
            "sha256": "a" * 64,
        }
    )
    wrong_media = NATIVE_RESPONSE_ADAPTER.validate_python(
        {
            "protocol_version": "1.0",
            "kind": "media_result",
            "job_id": "job_123",
            "note_id": "note_123",
            "role": "image",
            "position": 2,
            "outcome": "rejected",
            "reason": "run_budget",
        }
    )
    wrong_media_note = NATIVE_RESPONSE_ADAPTER.validate_python(
        {
            "protocol_version": "1.0",
            "kind": "media_result",
            "job_id": "job_123",
            "note_id": "note_other",
            "role": "image",
            "position": 1,
            "outcome": "rejected",
            "reason": "run_budget",
        }
    )
    wrong_media_role = NATIVE_RESPONSE_ADAPTER.validate_python(
        {
            "protocol_version": "1.0",
            "kind": "media_result",
            "job_id": "job_123",
            "note_id": "note_123",
            "role": "video_cover",
            "position": 1,
            "outcome": "rejected",
            "reason": "run_budget",
        }
    )

    with pytest.raises(ValueError):
        validate_response_for_request(candidate_request, wrong_candidate)
    with pytest.raises(ValueError):
        validate_response_for_request(candidate_request, wrong_candidate_position)
    with pytest.raises(ValueError):
        validate_response_for_request(media_request, wrong_media)
    with pytest.raises(ValueError):
        validate_response_for_request(media_request, wrong_media_note)
    with pytest.raises(ValueError):
        validate_response_for_request(media_request, wrong_media_role)


def test_response_correlation_binds_candidate_result_outcome_to_request_kind() -> None:
    snapshot = NATIVE_REQUEST_ADAPTER.validate_python(_snapshot())
    unavailable = NATIVE_REQUEST_ADAPTER.validate_python(
        {
            "protocol_version": "1.0",
            "kind": "candidate_unavailable",
            "job_id": "job_123",
            "source_position": 1,
            "note_id": "note_123",
            "reason": "detail_unavailable",
        }
    )
    recorded = NATIVE_RESPONSE_ADAPTER.validate_python(
        {
            "protocol_version": "1.0",
            "kind": "candidate_result",
            "job_id": "job_123",
            "note_id": "note_123",
            "source_position": 1,
            "outcome": "recorded",
        }
    )
    unavailable_result = NATIVE_RESPONSE_ADAPTER.validate_python(
        {
            "protocol_version": "1.0",
            "kind": "candidate_result",
            "job_id": "job_123",
            "note_id": "note_123",
            "source_position": 1,
            "outcome": "unavailable",
        }
    )

    assert validate_response_for_request(snapshot, recorded) is recorded
    assert validate_response_for_request(unavailable, unavailable_result) is unavailable_result
    with pytest.raises(ValueError):
        validate_response_for_request(snapshot, unavailable_result)
    with pytest.raises(ValueError):
        validate_response_for_request(unavailable, recorded)


def test_media_begin_accepts_an_exact_correlated_terminal_rejection() -> None:
    request = NATIVE_REQUEST_ADAPTER.validate_python(
        {
            "protocol_version": "1.0",
            "kind": "media_begin",
            "job_id": "job_123",
            "note_id": "note_123",
            "role": "image",
            "position": 1,
            "sequence": 1,
            "size_limit_bytes": 1,
        }
    )
    response = NATIVE_RESPONSE_ADAPTER.validate_python(
        {
            "protocol_version": "1.0",
            "kind": "media_result",
            "job_id": "job_123",
            "note_id": "note_123",
            "role": "image",
            "position": 1,
            "outcome": "rejected",
            "reason": "run_budget",
        }
    )

    assert validate_response_for_request(request, response) is response


def _set_nested(value: dict[str, object], path: tuple[object, ...], replacement: object) -> None:
    target: object = value
    for key in path[:-1]:
        if isinstance(key, int):
            assert isinstance(target, list)
            target = target[key]
        else:
            assert isinstance(target, dict)
            target = target[key]
    last = path[-1]
    if isinstance(last, int):
        assert isinstance(target, list)
        target[last] = replacement
    else:
        assert isinstance(target, dict)
        target[last] = replacement


def _request_for_kind(kind: str) -> NativeRequest:
    payloads: dict[str, dict[str, object]] = {
        "begin_job": _begin_job(),
        "candidate_snapshot": _snapshot(),
        "candidate_unavailable": {"protocol_version": "1.0", "kind": "candidate_unavailable", "job_id": "job_123", "source_position": 1, "note_id": "note_123", "reason": "detail_unavailable"},
        "finish_scan": {"protocol_version": "1.0", "kind": "finish_scan", "job_id": "job_123"},
        "media_begin": {"protocol_version": "1.0", "kind": "media_begin", "job_id": "job_123", "note_id": "note_123", "role": "image", "position": 1, "sequence": 1, "size_limit_bytes": 1},
        "media_chunk": {"protocol_version": "1.0", "kind": "media_chunk", "job_id": "job_123", "note_id": "note_123", "role": "image", "position": 1, "sequence": 1, "chunk_index": 0, "data_base64": "aGVsbG8="},
        "media_end": {"protocol_version": "1.0", "kind": "media_end", "job_id": "job_123", "note_id": "note_123", "role": "image", "position": 1, "sequence": 1, "mime_type": "image/webp", "sha256": "a" * 64},
        "media_missing": {"protocol_version": "1.0", "kind": "media_missing", "job_id": "job_123", "note_id": "note_123", "role": "image", "position": 1, "reason": "source_not_exposed"},
        "finish_job": {"protocol_version": "1.0", "kind": "finish_job", "job_id": "job_123"},
        "stop_job": {"protocol_version": "1.0", "kind": "stop_job", "job_id": "job_123"},
        "open_report": {"protocol_version": "1.0", "kind": "open_report", "job_id": "job_123"},
        "video_status": {"protocol_version": "1.0", "kind": "video_status", "job_id": "job_123"},
        "video_stop": {"protocol_version": "1.0", "kind": "video_stop", "job_id": "job_123"},
    }
    return NATIVE_REQUEST_ADAPTER.validate_python(payloads[kind])
