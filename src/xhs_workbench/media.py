"""Shared media contracts and bounded integrity checks."""

from __future__ import annotations

from typing import BinaryIO, Literal

MediaRole = Literal["image", "video_cover", "video"]
MediaStatus = Literal["downloaded", "missing", "rejected"]
MediaMissingReason = Literal[
    "source_not_exposed",
    "unsupported_source",
    "unsafe_source",
    "download_failed",
    "mime_mismatch",
    "size_limit",
    "note_budget",
    "run_budget",
    "slot_limit",
    "discovery_limit",
]
MediaMime = Literal["image/jpeg", "image/png", "image/webp", "video/mp4", "video/webm"]

IMAGE_MIME_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})
VIDEO_MIME_TYPES = frozenset({"video/mp4", "video/webm"})

MAX_IMAGES_PER_NOTE = 20
MAX_DISCOVERED_IMAGE_SLOTS = 100
MAX_VIDEOS_PER_NOTE = 1
MAX_IMAGE_BYTES = 15 * 1024 * 1024
MAX_VIDEO_BYTES = 100 * 1024 * 1024
MAX_NOTE_MEDIA_BYTES = 120 * 1024 * 1024
MAX_RUN_MEDIA_BYTES = 250 * 1024 * 1024
MEDIA_IO_TIMEOUT_SECONDS = 15
NOTE_MEDIA_BUDGET_SECONDS = 90

MIME_EXTENSIONS: dict[MediaMime, str] = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
}

_MAX_MP4_TOP_LEVEL_BOXES = 1024
_MAX_WEBM_HEADER_BYTES = 64 * 1024
_MP4_FTYP = b"ftyp"
_MP4_MOOV = b"moov"
_MP4_MDAT = b"mdat"
_WEBM_EBML_HEADER = b"\x1aE\xdf\xa3"
_WEBM_DOCTYPE = b"B\x82"
_WEBM_SEGMENT = b"\x18S\x80g"


def mime_extension(mime_type: str) -> str | None:
    """Return the approved filename extension for a persisted MIME type."""
    if mime_type == "image/jpeg":
        return MIME_EXTENSIONS["image/jpeg"]
    if mime_type == "image/png":
        return MIME_EXTENSIONS["image/png"]
    if mime_type == "image/webp":
        return MIME_EXTENSIONS["image/webp"]
    if mime_type == "video/mp4":
        return MIME_EXTENSIONS["video/mp4"]
    if mime_type == "video/webm":
        return MIME_EXTENSIONS["video/webm"]
    return None


def sniff_media_mime(head: bytes) -> MediaMime | None:
    """Identify a candidate MIME from an exact supported file signature."""
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(head) >= 12 and head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "image/webp"
    if len(head) >= 12 and head[4:8] == _MP4_FTYP:
        return "video/mp4"
    if head.startswith(_WEBM_EBML_HEADER):
        return "video/webm"
    return None


def validate_media_container(file: BinaryIO, mime_type: MediaMime, size_bytes: int) -> bool:
    """Validate an already-staged supported file without changing its stream position."""
    if type(size_bytes) is not int or size_bytes <= 0:
        return False
    try:
        original_position = file.tell()
    except (OSError, ValueError):
        return False
    try:
        file.seek(0, 2)
        if file.tell() != size_bytes:
            return False
        file.seek(0)
        if mime_type in IMAGE_MIME_TYPES:
            return sniff_media_mime(file.read(12)) == mime_type
        if mime_type == "video/mp4":
            return _validate_mp4(file, size_bytes)
        return _validate_webm(file, size_bytes)
    except (OSError, ValueError):
        return False
    finally:
        try:
            file.seek(original_position)
        except (OSError, ValueError):
            pass


def _validate_mp4(file: BinaryIO, size_bytes: int) -> bool:
    seen: set[bytes] = set()
    offset = 0
    for _ in range(_MAX_MP4_TOP_LEVEL_BOXES):
        if offset == size_bytes:
            return seen == {_MP4_FTYP, _MP4_MOOV, _MP4_MDAT}
        if size_bytes - offset < 8:
            return False
        header = file.read(8)
        if len(header) != 8:
            return False
        box_size = int.from_bytes(header[:4], "big")
        box_kind = header[4:]
        header_size = 8
        if box_size == 1:
            extended = file.read(8)
            if len(extended) != 8:
                return False
            box_size = int.from_bytes(extended, "big")
            header_size = 16
        if box_size < header_size or box_size > size_bytes - offset:
            return False
        if offset == 0 and (
            box_kind != _MP4_FTYP or box_size - header_size < 8
        ):
            return False
        if box_kind in {_MP4_FTYP, _MP4_MOOV, _MP4_MDAT}:
            if box_kind in seen:
                return False
            seen.add(box_kind)
        payload_size = box_size - header_size
        if payload_size:
            file.seek(payload_size, 1)
        offset += box_size
    return False


def _validate_webm(file: BinaryIO, size_bytes: int) -> bool:
    window = file.read(min(size_bytes, _MAX_WEBM_HEADER_BYTES))
    if not window.startswith(_WEBM_EBML_HEADER):
        return False
    header_size = _read_ebml_size(window, len(_WEBM_EBML_HEADER))
    if header_size is None:
        return False
    declared_size, size_width = header_size
    header_start = len(_WEBM_EBML_HEADER) + size_width
    header_end = header_start + declared_size
    if header_end > len(window):
        return False
    if not _ebml_header_has_webm_doctype(window[header_start:header_end]):
        return False
    return _has_bounded_webm_segment(window, header_end)


def _has_bounded_webm_segment(window: bytes, offset: int) -> bool:
    while offset < len(window):
        element = _read_ebml_element_id(window, offset)
        if element is None:
            return False
        element_id, id_width = element
        size_offset = offset + id_width
        element_size = _read_ebml_size(window, size_offset)
        if element_size is None:
            return element_id == _WEBM_SEGMENT and _is_unknown_ebml_size(window, size_offset)
        size, size_width = element_size
        element_end = size_offset + size_width + size
        if element_end > len(window):
            return False
        if element_id == _WEBM_SEGMENT:
            return True
        offset = element_end
    return False


def _read_ebml_element_id(value: bytes, offset: int) -> tuple[bytes, int] | None:
    if offset >= len(value):
        return None
    first = value[offset]
    marker = 0x80
    width = 1
    while width <= 4 and not first & marker:
        marker >>= 1
        width += 1
    if width > 4 or offset + width > len(value):
        return None
    return value[offset : offset + width], width


def _read_ebml_size(value: bytes, offset: int) -> tuple[int, int] | None:
    if offset >= len(value):
        return None
    first = value[offset]
    marker = 0x80
    width = 1
    while width <= 8 and not first & marker:
        marker >>= 1
        width += 1
    if width > 8 or offset + width > len(value):
        return None
    result = first & (marker - 1)
    for item in value[offset + 1 : offset + width]:
        result = (result << 8) | item
    if result == (1 << (7 * width)) - 1:
        return None
    return result, width


def _is_unknown_ebml_size(value: bytes, offset: int) -> bool:
    if offset >= len(value):
        return False
    first = value[offset]
    marker = 0x80
    width = 1
    while width <= 8 and not first & marker:
        marker >>= 1
        width += 1
    if width > 8 or offset + width > len(value):
        return False
    return first & (marker - 1) == marker - 1 and value[offset + 1 : offset + width] == bytes(
        [0xFF]
    ) * (width - 1)


def _ebml_header_has_webm_doctype(header: bytes) -> bool:
    offset = 0
    while offset < len(header):
        if offset + 2 > len(header):
            return False
        element_id = header[offset : offset + 2]
        offset += 2
        element_size = _read_ebml_size(header, offset)
        if element_size is None:
            return False
        size, width = element_size
        offset += width
        end = offset + size
        if end > len(header):
            return False
        if element_id == _WEBM_DOCTYPE and header[offset:end] == b"webm":
            return True
        offset = end
    return False
