"""Native Messaging frame tests, written before the transport implementation."""

from __future__ import annotations

import io
import json
import struct
from pathlib import Path

import pytest
from pydantic import ValidationError

from xhs_workbench.extension_models import (
    NATIVE_REQUEST_ADAPTER,
    NATIVE_RESPONSE_ADAPTER,
    REQUEST_TO_ALLOWED_RESPONSE_KINDS,
    HealthResult,
    validate_response_for_request,
)
from xhs_workbench.native_protocol import (
    MAX_EXTENSION_TO_HOST_MESSAGE_BYTES,
    NativeProtocolError,
    read_native_message,
    write_native_message,
)

REQUEST_FIXTURES = (
    "request_health.json", "request_begin_job.json", "request_candidate_snapshot.json", "request_candidate_unavailable.json",
    "request_finish_scan.json", "request_media_begin.json", "request_media_begin_rejected.json",
    "request_media_chunk.json", "request_media_end.json", "request_media_missing.json",
    "request_finish_job.json", "request_stop_job.json", "request_open_report.json",
)
RESPONSE_FIXTURES = (
    "response_health.json", "response_begin_job.json", "response_candidate_snapshot.json", "response_candidate_unavailable.json",
    "response_finish_scan.json", "response_media_begin.json", "response_media_begin_rejected.json",
    "response_media_chunk.json", "response_media_end.json", "response_media_missing.json",
    "response_finish_job.json", "response_stop_job.json", "response_open_report.json",
    "response_error_health.json", "response_error_begin_job.json", "response_error_candidate_snapshot.json", "response_error_candidate_unavailable.json",
    "response_error_finish_scan.json", "response_error_media_begin.json", "response_error_media_begin_rejected.json",
    "response_error_media_chunk.json", "response_error_media_end.json", "response_error_media_missing.json",
    "response_error_finish_job.json", "response_error_stop_job.json", "response_error_open_report.json",
    "response_error_job.json",
)


def _health_frame() -> bytes:
    payload = json.dumps({"protocol_version": "1.0", "kind": "health"}).encode("utf-8")
    return struct.pack("@I", len(payload)) + payload


def test_round_trips_native_endian_json_message() -> None:
    request = read_native_message(io.BytesIO(_health_frame()))
    stream = io.BytesIO()

    write_native_message(stream, HealthResult(protocol_version="1.0", kind="health_result", status="ready", host_version="0.1.0"))

    written = stream.getvalue()
    size = struct.unpack("@I", written[:4])[0]
    assert request.kind == "health"
    assert json.loads(written[4:].decode("utf-8"))["kind"] == "health_result"
    assert size == len(written[4:])


def test_writer_flushes_a_response_while_the_native_port_stays_open() -> None:
    """Chrome must see each response before the host blocks on its next request."""

    class FlushRequiredBuffer:
        def __init__(self) -> None:
            self.pending = bytearray()
            self.visible = bytearray()

        def write(self, value: bytes) -> int:
            self.pending.extend(value)
            return len(value)

        def flush(self) -> None:
            self.visible.extend(self.pending)
            self.pending.clear()

    stream = FlushRequiredBuffer()

    write_native_message(
        stream,  # type: ignore[arg-type]
        HealthResult(
            protocol_version="1.0",
            kind="health_result",
            status="ready",
            host_version="0.1.0",
        ),
    )

    size = struct.unpack("@I", stream.visible[:4])[0]
    assert size == len(stream.visible[4:])
    assert json.loads(stream.visible[4:].decode("utf-8"))["kind"] == "health_result"


@pytest.mark.parametrize(
    "stream",
    [
        io.BytesIO(),
        io.BytesIO(b"\x01\x00"),
        io.BytesIO(struct.pack("@I", 2) + b"{"),
        io.BytesIO(struct.pack("@I", 0)),
        io.BytesIO(struct.pack("@I", MAX_EXTENSION_TO_HOST_MESSAGE_BYTES + 1)),
        io.BytesIO(struct.pack("@I", 2) + b"\xff\xff"),
        io.BytesIO(struct.pack("@I", 1) + b"{"),
    ],
)
def test_rejects_malformed_frames_with_finite_non_echoing_code(stream: io.BytesIO) -> None:
    with pytest.raises(NativeProtocolError) as raised:
        read_native_message(stream)

    assert raised.value.code in {"invalid_frame", "message_limit_exceeded", "invalid_request"}
    assert "{" not in str(raised.value)
    assert "\ufffd" not in str(raised.value)


def test_rejects_response_at_or_above_host_limit() -> None:
    class TooLarge:
        def model_dump_json(self, **_: object) -> str:
            return '{"x":"' + ("a" * (1024 * 1024)) + '"}'

    with pytest.raises(NativeProtocolError, match="message_limit_exceeded"):
        write_native_message(io.BytesIO(), TooLarge())  # type: ignore[arg-type]


def test_reads_multiple_sequential_messages() -> None:
    stream = io.BytesIO(_health_frame() + _health_frame())

    assert read_native_message(stream).kind == "health"
    assert read_native_message(stream).kind == "health"


def test_golden_json_fixtures_parse_and_reserialize_without_drift() -> None:
    fixture_dir = Path(__file__).parent / "fixtures" / "native_protocol"
    assert tuple(sorted(path.name for path in fixture_dir.glob("request_*.json"))) == tuple(sorted(REQUEST_FIXTURES))
    assert tuple(sorted(path.name for path in fixture_dir.glob("response_*.json"))) == tuple(sorted(RESPONSE_FIXTURES))
    for name in REQUEST_FIXTURES:
        fixture = fixture_dir / name
        raw = json.loads(fixture.read_text(encoding="utf-8"))
        parsed = NATIVE_REQUEST_ADAPTER.validate_python(raw)
        assert parsed.model_dump(exclude_none=True) == raw
        assert json.loads(parsed.model_dump_json(exclude_none=True)) == raw
    for name in RESPONSE_FIXTURES:
        fixture = fixture_dir / name
        raw = json.loads(fixture.read_text(encoding="utf-8"))
        parsed = NATIVE_RESPONSE_ADAPTER.validate_python(raw)
        assert parsed.model_dump(exclude_none=True) == raw
        assert json.loads(parsed.model_dump_json(exclude_none=True)) == raw


def test_begin_job_page_order_fixtures_are_strict_and_legacy_sorting_is_optional() -> None:
    """Removing page-order validation or the legacy default breaks wire compatibility."""
    fixture_dir = Path(__file__).parent / "fixtures" / "native_protocol"
    page_order = json.loads((fixture_dir / "page_order_begin_job.json").read_text(encoding="utf-8"))
    invalid_cutoff = json.loads(
        (fixture_dir / "invalid_page_order_begin_job_cutoff.json").read_text(encoding="utf-8")
    )

    parsed = NATIVE_REQUEST_ADAPTER.validate_python(page_order)
    assert parsed.model_dump() == page_order
    for requested_count in (5, 10):
        accepted = {
            **page_order,
            "requested_count": requested_count,
            "candidate_scan_limit": requested_count,
        }
        assert NATIVE_REQUEST_ADAPTER.validate_python(accepted).requested_count == requested_count
    legacy = json.loads((fixture_dir / "request_begin_job.json").read_text(encoding="utf-8"))
    legacy.pop("selection_order")
    assert NATIVE_REQUEST_ADAPTER.validate_python(legacy).selection_order == "exact_likes_desc"

    for invalid in (
        invalid_cutoff,
        {**page_order, "selection_order": "unknown_order"},
        {**page_order, "requested_count": 1},
        {**page_order, "collection_surface": "extension_account"},
        {**page_order, "source_page_url": "https://www.xiaohongshu.com/search_result?sort=latest"},
        {**page_order, "source_page_url": "https://www.xiaohongshu.com/search_result#latest"},
        {**page_order, "sort_label": "token=secret"},
        {**page_order, "media_policy": "full_media"},
    ):
        with pytest.raises(ValidationError):
            NATIVE_REQUEST_ADAPTER.validate_python(invalid)


def test_golden_success_exchanges_match_the_exact_response_mapping() -> None:
    fixture_dir = Path(__file__).parent / "fixtures" / "native_protocol"
    for request_file in sorted(fixture_dir.glob("request_*.json")):
        request = json.loads(request_file.read_text(encoding="utf-8"))
        response_file = fixture_dir / request_file.name.replace("request_", "response_")
        response = json.loads(response_file.read_text(encoding="utf-8"))
        assert response["kind"] in REQUEST_TO_ALLOWED_RESPONSE_KINDS[request["kind"]]
        parsed_request = NATIVE_REQUEST_ADAPTER.validate_python(request)
        parsed_response = NATIVE_RESPONSE_ADAPTER.validate_python(response)
        assert validate_response_for_request(parsed_request, parsed_response) is parsed_response
        if request["kind"] == "health":
            assert "job_id" not in request and "job_id" not in response
        else:
            assert response["job_id"] == request["job_id"]


def test_golden_error_exchanges_bind_health_and_every_job_request() -> None:
    fixture_dir = Path(__file__).parent / "fixtures" / "native_protocol"
    health = NATIVE_REQUEST_ADAPTER.validate_python(
        json.loads((fixture_dir / "request_health.json").read_text(encoding="utf-8"))
    )
    health_error = NATIVE_RESPONSE_ADAPTER.validate_python(
        json.loads((fixture_dir / "response_error_health.json").read_text(encoding="utf-8"))
    )
    assert "job_id" not in health_error.model_dump(exclude_none=True)
    assert validate_response_for_request(health, health_error) is health_error

    for request_file in sorted(fixture_dir.glob("request_*.json")):
        if request_file.name == "request_health.json":
            continue
        request = NATIVE_REQUEST_ADAPTER.validate_python(
            json.loads(request_file.read_text(encoding="utf-8"))
        )
        response_file = fixture_dir / request_file.name.replace("request_", "response_error_")
        response = NATIVE_RESPONSE_ADAPTER.validate_python(
            json.loads(response_file.read_text(encoding="utf-8"))
        )
        assert response.kind == "error"
        assert response.job_id == request.job_id
        assert validate_response_for_request(request, response) is response


def test_writer_rejects_mismatched_response_when_the_request_is_supplied() -> None:
    request = NATIVE_REQUEST_ADAPTER.validate_python(
        {"protocol_version": "1.0", "kind": "health"}
    )
    response = NATIVE_RESPONSE_ADAPTER.validate_python(
        {"protocol_version": "1.0", "kind": "error", "job_id": "job_123", "code": "invalid_request", "fatal": True}
    )

    with pytest.raises(NativeProtocolError, match="invalid_request"):
        write_native_message(io.BytesIO(), response, request=request)
