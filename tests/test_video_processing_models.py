"""The video additions preserve the strict, finite native wire boundary."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from xhs_workbench.extension_models import (
    NATIVE_REQUEST_ADAPTER,
    NATIVE_RESPONSE_ADAPTER,
    validate_response_for_request,
)


def test_video_status_and_stop_are_job_scoped_and_correlated() -> None:
    request = NATIVE_REQUEST_ADAPTER.validate_python(
        {"protocol_version": "1.0", "kind": "video_status", "job_id": "job_123"}
    )
    response = NATIVE_RESPONSE_ADAPTER.validate_python(
        {"protocol_version": "1.0", "kind": "video_result", "job_id": "job_123",
         "processing": {"status": "running"}}
    )
    assert validate_response_for_request(request, response) is response
    stop = NATIVE_REQUEST_ADAPTER.validate_python(
        {"protocol_version": "1.0", "kind": "video_stop", "job_id": "job_123"}
    )
    assert validate_response_for_request(stop, response) is response
    with pytest.raises(ValueError):
        wrong = response.model_copy(update={"job_id": "different"})
        validate_response_for_request(request, wrong)


@pytest.mark.parametrize(
    "metadata",
    [
        {"note_id": "note_123", "duration_ms": True},
        {"note_id": "note_123", "duration_ms": 86400001},
        {"note_id": "note_123", "subtitle_status": "available"},
        {"note_id": "note_123", "subtitle_srt": "1\n00:00:00,000 --> 00:00:01,000\nHi\n", "subtitle_status": "failed"},
        {"note_id": "note_123", "subtitle_srt": "not an SRT", "subtitle_status": "available"},
        {"note_id": "note_123", "subtitle_srt": "1\n00:00:00,000 --> 00:00:01,000\nNUL\x00\n", "subtitle_status": "available"},
        {"note_id": "note_123", "source_url": "https://example.com/video"},
    ],
)
def test_finish_video_metadata_rejects_invalid_or_extra_fields(metadata: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        NATIVE_REQUEST_ADAPTER.validate_python(
            {"protocol_version": "1.0", "kind": "finish_job", "job_id": "job_123",
             "video_metadata": metadata}
        )


def test_finish_video_metadata_accepts_bounded_independent_subtitles() -> None:
    request = NATIVE_REQUEST_ADAPTER.validate_python(
        {"protocol_version": "1.0", "kind": "finish_job", "job_id": "job_123",
         "video_metadata": {"note_id": "note_123", "duration_ms": 300000,
                            "subtitle_status": "available", "subtitle_srt":
                            "1\n00:00:00,000 --> 00:00:01,000\n独立字幕\n"}}
    )
    assert request.video_metadata is not None
    assert request.video_metadata.duration_ms == 300000


def test_video_response_rejects_arbitrary_reason_or_speech() -> None:
    with pytest.raises(ValidationError):
        NATIVE_RESPONSE_ADAPTER.validate_python(
            {"protocol_version": "1.0", "kind": "video_result", "job_id": "job_123",
             "processing": {"status": "failed", "reason": "my /private/path speech"}}
        )
