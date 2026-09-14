"""Bounded Chrome Native Messaging framing without input echoing."""

from __future__ import annotations

import json
import struct
from typing import BinaryIO

from pydantic import ValidationError

from xhs_workbench.extension_models import (
    NATIVE_REQUEST_ADAPTER,
    NativeRequest,
    NativeResponse,
    validate_response_for_request,
)

MAX_EXTENSION_TO_HOST_MESSAGE_BYTES = 1024 * 1024
MAX_HOST_TO_EXTENSION_MESSAGE_BYTES = (1024 * 1024) - 4096
_PUBLIC_PROTOCOL_ERRORS = frozenset(
    {"invalid_frame", "message_limit_exceeded", "invalid_request", "unsupported_protocol"}
)


class NativeProtocolError(ValueError):
    """A finite public failure that never contains untrusted input."""

    def __init__(
        self, code: str, *, eof: bool = False, request: NativeRequest | None = None
    ) -> None:
        self.code = code if code in _PUBLIC_PROTOCOL_ERRORS else "invalid_request"
        self.eof = eof
        self.request = request
        super().__init__(self.code)


def _read_exact(stream: BinaryIO, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            raise NativeProtocolError("invalid_frame", eof=not chunks)
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _write_exact(stream: BinaryIO, value: bytes) -> None:
    offset = 0
    while offset < len(value):
        written = stream.write(value[offset:])
        if written is None:
            return
        if written <= 0:
            raise NativeProtocolError("invalid_frame")
        offset += written


def read_native_message(stream: BinaryIO) -> NativeRequest:
    """Read one native-endian, bounded JSON request from ``stream``."""
    prefix = _read_exact(stream, 4)
    size = struct.unpack("@I", prefix)[0]
    if size == 0:
        raise NativeProtocolError("invalid_frame")
    if size > MAX_EXTENSION_TO_HOST_MESSAGE_BYTES:
        raise NativeProtocolError("message_limit_exceeded")
    raw_payload = _read_exact(stream, size)
    try:
        payload = json.loads(raw_payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise NativeProtocolError("invalid_request") from error
    if not isinstance(payload, dict):
        raise NativeProtocolError("invalid_request")
    protocol_version = payload.get("protocol_version")
    if isinstance(protocol_version, str) and protocol_version != "1.0":
        normalized_payload = dict(payload)
        normalized_payload["protocol_version"] = "1.0"
        try:
            normalized_request = NATIVE_REQUEST_ADAPTER.validate_python(normalized_payload)
        except ValidationError as error:
            raise NativeProtocolError("invalid_request") from error
        raise NativeProtocolError("unsupported_protocol", request=normalized_request)
    try:
        return NATIVE_REQUEST_ADAPTER.validate_python(payload)
    except ValidationError as error:
        raise NativeProtocolError("invalid_request") from error


def write_native_message(
    stream: BinaryIO, response: NativeResponse, *, request: NativeRequest | None = None
) -> None:
    """Write exactly one bounded native-endian JSON response to ``stream``."""
    if request is not None:
        try:
            validate_response_for_request(request, response)
        except ValueError as error:
            raise NativeProtocolError("invalid_request") from error
    try:
        payload = response.model_dump_json(exclude_none=True).encode("utf-8")
    except (AttributeError, TypeError, ValueError) as error:
        raise NativeProtocolError("invalid_request") from error
    if len(payload) > MAX_HOST_TO_EXTENSION_MESSAGE_BYTES:
        raise NativeProtocolError("message_limit_exceeded")
    _write_exact(stream, struct.pack("@I", len(payload)))
    _write_exact(stream, payload)
    stream.flush()
