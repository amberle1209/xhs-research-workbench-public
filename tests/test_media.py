from pathlib import Path

from xhs_workbench.media import (
    MAX_DISCOVERED_IMAGE_SLOTS,
    MAX_IMAGE_BYTES,
    MAX_IMAGES_PER_NOTE,
    MAX_NOTE_MEDIA_BYTES,
    MAX_RUN_MEDIA_BYTES,
    MAX_VIDEO_BYTES,
    sniff_media_mime,
    validate_media_container,
)


def test_sniff_media_mime_accepts_supported_headers() -> None:
    assert sniff_media_mime(b"\xff\xd8\xfffixture") == "image/jpeg"
    assert sniff_media_mime(b"\x89PNG\r\n\x1a\nfixture") == "image/png"
    assert sniff_media_mime(b"RIFF\x00\x00\x00\x00WEBPfixture") == "image/webp"
    assert sniff_media_mime(b"\x00\x00\x00\x18ftypisomfixture") == "video/mp4"
    assert sniff_media_mime(b"\x1aE\xdf\xa3fixture") == "video/webm"
    assert sniff_media_mime(b"#EXTM3U\n") is None


def _mp4_box(kind: bytes, payload: bytes = b"") -> bytes:
    return (8 + len(payload)).to_bytes(4, "big") + kind + payload


def test_validate_media_container_requires_complete_mp4_box_structure(tmp_path: Path) -> None:
    valid = (
        _mp4_box(b"ftyp", b"isom\x00\x00\x00\x00")
        + _mp4_box(b"moov")
        + _mp4_box(b"mdat", b"frame")
    )
    path = tmp_path / "valid.mp4"
    path.write_bytes(valid)
    with path.open("rb") as stream:
        assert validate_media_container(stream, "video/mp4", len(valid)) is True
    fake = b"\x00\x00\x00\x18ftypisomnot-a-container"
    path.write_bytes(fake)
    with path.open("rb") as stream:
        assert validate_media_container(stream, "video/mp4", len(fake)) is False
    empty_ftyp = _mp4_box(b"ftyp") + _mp4_box(b"moov") + _mp4_box(b"mdat", b"frame")
    path.write_bytes(empty_ftyp)
    with path.open("rb") as stream:
        assert validate_media_container(stream, "video/mp4", len(empty_ftyp)) is False


def test_validate_media_container_requires_webm_doctype_and_segment(tmp_path: Path) -> None:
    valid = b"\x1aE\xdf\xa3\x87B\x82\x84webm\x18S\x80g\x80"
    path = tmp_path / "valid.webm"
    path.write_bytes(valid)
    with path.open("rb") as stream:
        assert validate_media_container(stream, "video/webm", len(valid)) is True
    path.write_bytes(b"\x1aE\xdf\xa3\x84junk")
    with path.open("rb") as stream:
        assert validate_media_container(stream, "video/webm", path.stat().st_size) is False
    bare_segment = b"\x1aE\xdf\xa3\x87B\x82\x84webm\x18S\x80g"
    path.write_bytes(bare_segment)
    with path.open("rb") as stream:
        assert validate_media_container(stream, "video/webm", len(bare_segment)) is False
    unknown_size_segment = b"\x1aE\xdf\xa3\x87B\x82\x84webm\x18S\x80g\xff"
    path.write_bytes(unknown_size_segment)
    with path.open("rb") as stream:
        assert validate_media_container(stream, "video/webm", len(unknown_size_segment)) is True
    two_byte_unknown_size_segment = b"\x1aE\xdf\xa3\x87B\x82\x84webm\x18S\x80g\x7f\xff"
    path.write_bytes(two_byte_unknown_size_segment)
    with path.open("rb") as stream:
        assert (
            validate_media_container(stream, "video/webm", len(two_byte_unknown_size_segment))
            is True
        )


def test_validate_media_container_preserves_stream_position(tmp_path: Path) -> None:
    path = tmp_path / "image.jpg"
    path.write_bytes(b"\xff\xd8\xfffixture")
    with path.open("rb") as stream:
        stream.seek(2)
        assert validate_media_container(stream, "image/jpeg", path.stat().st_size) is True
        assert stream.tell() == 2


def test_media_limits_are_the_approved_v2_values() -> None:
    assert MAX_IMAGES_PER_NOTE == 20
    assert MAX_DISCOVERED_IMAGE_SLOTS == 100
    assert MAX_IMAGE_BYTES == 15 * 1024 * 1024
    assert MAX_VIDEO_BYTES == 100 * 1024 * 1024
    assert MAX_NOTE_MEDIA_BYTES == 120 * 1024 * 1024
    assert MAX_RUN_MEDIA_BYTES == 250 * 1024 * 1024
