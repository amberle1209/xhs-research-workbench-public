"""Media persistence tests for the bounded extension import path."""

from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path

import pytest

from xhs_workbench import extension_import
from xhs_workbench.extension_import import ExtensionJob
from xhs_workbench.extension_models import (
    ExtensionBeginJob,
    ExtensionCandidateSnapshot,
    ExtensionMediaBegin,
    ExtensionMediaChunk,
    ExtensionMediaEnd,
    ExtensionMediaMissing,
)
from xhs_workbench.media import (
    MAX_IMAGE_BYTES,
    MAX_NOTE_MEDIA_BYTES,
    MAX_RUN_MEDIA_BYTES,
    MAX_VIDEO_BYTES,
)

WEBP = b"RIFF\x08\x00\x00\x00WEBPfixture"


def _mp4_box(kind: bytes, payload: bytes = b"") -> bytes:
    return (8 + len(payload)).to_bytes(4, "big") + kind + payload


MP4 = _mp4_box(b"ftyp", b"isom\x00\x00\x00\x00") + _mp4_box(b"moov") + _mp4_box(
    b"mdat", b"frame"
)


def _request() -> ExtensionBeginJob:
    return ExtensionBeginJob(
        protocol_version="1.0",
        kind="begin_job",
        job_id="extension_job",
        collection_surface="extension_current",
        source_page_url="https://www.xiaohongshu.com/explore/note_123",
        requested_count=1,
        candidate_scan_limit=1,
        publication_cutoff=None,
        selection_order="exact_likes_desc",
    )


def _snapshot(
    *,
    role: str = "image",
    note_id: str = "note_123",
    source_position: int = 1,
    likes: int = 7,
    media_slots: list[dict[str, object]] | None = None,
) -> ExtensionCandidateSnapshot:
    return ExtensionCandidateSnapshot(
        protocol_version="1.0",
        kind="candidate_snapshot",
        job_id="extension_job",
        source_position=source_position,
        note_id=note_id,
        canonical_url=f"https://www.xiaohongshu.com/explore/{note_id}",
        metrics={"likes": {"raw_value": str(likes), "normalized_value": likes, "precision": "exact"}},
        media_slots=(
            [{"note_id": note_id, "role": role, "position": 1}]
            if media_slots is None
            else media_slots
        ),
    )


def _begin(
    *,
    role: str = "image",
    sequence: int = 1,
    size_limit_bytes: int = 15 * 1024 * 1024,
    note_id: str = "note_123",
    position: int = 1,
) -> ExtensionMediaBegin:
    return ExtensionMediaBegin(
        protocol_version="1.0",
        kind="media_begin",
        job_id="extension_job",
        note_id=note_id,
        role=role,
        position=position,
        sequence=sequence,
        size_limit_bytes=size_limit_bytes,
    )


def _batch_request(*, requested_count: int = 1) -> ExtensionBeginJob:
    return ExtensionBeginJob(
        protocol_version="1.0",
        kind="begin_job",
        job_id="extension_job",
        collection_surface="extension_search",
        source_page_url="https://www.xiaohongshu.com/search_result",
        requested_count=requested_count,
        candidate_scan_limit=2,
        publication_cutoff=None,
        selection_order="exact_likes_desc",
    )


def test_chunked_image_recomputes_and_publishes_verified_local_asset(tmp_path: Path) -> None:
    """A missing host validator must fail this test by lacking the media methods."""
    job = ExtensionJob("host_run_123", tmp_path / "host_run_123", _request())
    job.add_candidate(_snapshot())
    job.finish_scan()
    job.begin_media(_begin())

    encoded = base64.b64encode(WEBP).decode("ascii")
    job.append_media_chunk(
        ExtensionMediaChunk(
            protocol_version="1.0",
            kind="media_chunk",
            job_id="extension_job",
            note_id="note_123",
            role="image",
            position=1,
            sequence=1,
            chunk_index=0,
            data_base64=encoded[:12],
        )
    )
    job.append_media_chunk(
        ExtensionMediaChunk(
            protocol_version="1.0",
            kind="media_chunk",
            job_id="extension_job",
            note_id="note_123",
            role="image",
            position=1,
            sequence=1,
            chunk_index=1,
            data_base64=encoded[12:],
        )
    )
    slot = job.finish_media(
        ExtensionMediaEnd(
            protocol_version="1.0",
            kind="media_end",
            job_id="extension_job",
            note_id="note_123",
            role="image",
            position=1,
            sequence=1,
            mime_type="image/webp",
            sha256=hashlib.sha256(WEBP).hexdigest(),
        )
    )

    assert slot.status == "downloaded"
    assert slot.asset is not None
    assert slot.asset.local_path == "assets/note_123-image-001.webp"
    assert slot.asset.sha256 == hashlib.sha256(WEBP).hexdigest()
    assert (tmp_path / "host_run_123" / slot.asset.local_path).read_bytes() == WEBP
    run = job.finish_job()
    assert run.status.value == "complete"
    assert (tmp_path / "host_run_123" / ".staging").is_dir()
    with pytest.raises(ValueError, match="not open"):
        job.begin_media(_begin())


def test_media_rejects_replayed_or_out_of_order_chunks_without_publishing(tmp_path: Path) -> None:
    """Removing chunk-order checks would let a replay alter a verified asset."""
    job = ExtensionJob("host_run_456", tmp_path / "host_run_456", _request())
    job.add_candidate(_snapshot())
    job.finish_scan()
    job.begin_media(_begin())
    first = base64.b64encode(WEBP).decode("ascii")

    with pytest.raises(ValueError, match="out of order"):
        job.append_media_chunk(
            ExtensionMediaChunk(
                protocol_version="1.0", kind="media_chunk", job_id="extension_job",
                note_id="note_123", role="image", position=1, sequence=1, chunk_index=1,
                data_base64=first,
            )
        )

    with pytest.raises(ValueError, match="already"):
        job.begin_media(_begin())

    job.discard()
    assert not (tmp_path / "host_run_456").exists()


def test_media_begin_requires_the_next_monotonic_sequence(tmp_path: Path) -> None:
    """Removing global sequence ordering would accept reordered media transfers."""
    job = ExtensionJob("host_run_sequence", tmp_path / "host_run_sequence", _request())
    job.add_candidate(_snapshot())
    job.finish_scan()
    out_of_order = _begin().model_copy(update={"sequence": 2})

    with pytest.raises(ValueError, match="sequence"):
        job.begin_media(out_of_order)

    job.discard()


def test_media_hash_mismatch_closes_slot_as_rejected_and_bundle_is_partial(tmp_path: Path) -> None:
    """Removing host hash verification would publish extension-claimed bytes."""
    job = ExtensionJob("host_run_789", tmp_path / "host_run_789", _request())
    job.add_candidate(_snapshot())
    job.finish_scan()
    job.begin_media(_begin())
    job.append_media_chunk(
        ExtensionMediaChunk(
            protocol_version="1.0", kind="media_chunk", job_id="extension_job",
            note_id="note_123", role="image", position=1, sequence=1, chunk_index=0,
            data_base64=base64.b64encode(WEBP).decode("ascii"),
        )
    )
    rejected = job.finish_media(
        ExtensionMediaEnd(
            protocol_version="1.0", kind="media_end", job_id="extension_job",
            note_id="note_123", role="image", position=1, sequence=1,
            mime_type="image/webp", sha256="0" * 64,
        )
    )

    run = job.finish_job()
    output = tmp_path / "host_run_789"
    assert rejected.status == "rejected"
    assert rejected.missing_reason == "mime_mismatch"
    assert run.status.value == "partial"
    assert (output / ".staging").is_dir()
    assert not any((output / "assets").iterdir())
    assert '"precision"' in (output / "results.json").read_text(encoding="utf-8")
    assert "precision" not in (output / "index.html").read_text(encoding="utf-8")


def test_finish_requires_every_declared_slot_and_missing_closes_one_slot(tmp_path: Path) -> None:
    """Removing finite slot closure would render a falsely complete partial run."""
    job = ExtensionJob("host_run_missing", tmp_path / "host_run_missing", _request())
    job.add_candidate(_snapshot())
    job.finish_scan()

    with pytest.raises(ValueError, match="every selected"):
        job.finish_job()
    assert not (tmp_path / "host_run_missing").exists()

    resumed = ExtensionJob("host_run_missing_retry", tmp_path / "host_run_missing_retry", _request())
    resumed.add_candidate(_snapshot())
    resumed.finish_scan()
    closed = resumed.mark_media_missing(
        ExtensionMediaMissing(
            protocol_version="1.0", kind="media_missing", job_id="extension_job",
            note_id="note_123", role="image", position=1, reason="source_not_exposed",
        )
    )
    run = resumed.finish_job()

    assert closed.status == "missing"
    assert run.status.value == "partial"
    assert (tmp_path / "host_run_missing_retry" / "index.html").is_file()


def test_finishing_an_open_transfer_discards_the_incomplete_run(tmp_path: Path) -> None:
    """Removing error cleanup would strand unverified chunks after an interruption."""
    output = tmp_path / "host_run_interrupted"
    job = ExtensionJob("host_run_interrupted", output, _request())
    job.add_candidate(_snapshot())
    job.finish_scan()
    job.begin_media(_begin())

    with pytest.raises(ValueError, match="open media slot"):
        job.finish_job()

    assert job.state.value == "stopped"
    assert not output.exists()


@pytest.mark.parametrize("role", ["image", "video_cover"])
def test_role_mime_mismatch_rejects_without_leaking_a_published_asset(
    tmp_path: Path, role: str
) -> None:
    """A role/MIME mismatch must not publish before NoteMediaSlot rejects it."""
    output = tmp_path / "host_run_mime"
    job = ExtensionJob("host_run_mime", output, _request())
    job.add_candidate(_snapshot(role=role))
    job.finish_scan()
    job.begin_media(_begin(role=role))
    job.append_media_chunk(
        ExtensionMediaChunk(
            protocol_version="1.0", kind="media_chunk", job_id="extension_job",
            note_id="note_123", role=role, position=1, sequence=1, chunk_index=0,
            data_base64=base64.b64encode(MP4).decode("ascii"),
        )
    )

    slot = job.finish_media(
        ExtensionMediaEnd(
            protocol_version="1.0", kind="media_end", job_id="extension_job",
            note_id="note_123", role=role, position=1, sequence=1,
            mime_type="video/mp4", sha256=hashlib.sha256(MP4).hexdigest(),
        )
    )

    assert slot.status == "rejected"
    assert slot.missing_reason == "mime_mismatch"
    assert not any((output / "assets").iterdir())
    assert all(path.name.startswith(".") for path in (output / ".staging").iterdir())
    with pytest.raises(ValueError, match="active"):
        job.finish_media(
            ExtensionMediaEnd(
                protocol_version="1.0", kind="media_end", job_id="extension_job",
                note_id="note_123", role=role, position=1, sequence=1,
                mime_type="video/mp4", sha256=hashlib.sha256(MP4).hexdigest(),
            )
        )


@pytest.mark.parametrize(
    "bad_message", ["wrong_job", "undeclared_slot", "wrong_active_note", "wrong_active_slot", "chunk_before_begin"]
)
def test_media_scope_and_order_reject_unbound_messages(tmp_path: Path, bad_message: str) -> None:
    """Dropping scope checks would accept bytes outside the selected declared slot."""
    job = ExtensionJob(f"host_run_{bad_message}", tmp_path / f"host_run_{bad_message}", _request())
    job.add_candidate(_snapshot())
    job.finish_scan()

    if bad_message == "wrong_job":
        message = _begin().model_copy(update={"job_id": "wrong_job"})
        with pytest.raises(ValueError, match="job ID"):
            job.begin_media(message)
    elif bad_message == "undeclared_slot":
        message = _begin().model_copy(update={"note_id": "other_note"})
        with pytest.raises(ValueError, match="declared"):
            job.begin_media(message)
    elif bad_message in {"wrong_active_note", "wrong_active_slot"}:
        job.begin_media(_begin())
        message = ExtensionMediaChunk(
            protocol_version="1.0", kind="media_chunk", job_id="extension_job",
            note_id=("other_note" if bad_message == "wrong_active_note" else "note_123"),
            role=("image" if bad_message == "wrong_active_note" else "video"),
            position=1, sequence=1, chunk_index=0, data_base64=base64.b64encode(WEBP).decode("ascii"),
        )
        with pytest.raises(ValueError, match="active"):
            job.append_media_chunk(message)
    else:
        with pytest.raises(ValueError, match="active"):
            job.append_media_chunk(
                ExtensionMediaChunk(
                    protocol_version="1.0", kind="media_chunk", job_id="extension_job",
                    note_id="note_123", role="image", position=1, sequence=1, chunk_index=0,
                    data_base64=base64.b64encode(WEBP).decode("ascii"),
                )
            )
    job.discard()


@pytest.mark.parametrize(
    "payload",
    ["%%not-base64%%", base64.b64encode(b"x" * (256 * 1024 + 1)).decode("ascii")],
    ids=["invalid_base64", "oversized_decoded"],
)
def test_invalid_or_oversized_chunk_closes_the_active_slot(tmp_path: Path, payload: str) -> None:
    """A malformed or oversized decoded chunk must not remain open for later finalization."""
    job = ExtensionJob("host_run_bad_chunk", tmp_path / "host_run_bad_chunk", _request())
    job.add_candidate(_snapshot())
    job.finish_scan()
    job.begin_media(_begin())
    message = ExtensionMediaChunk.model_construct(
        protocol_version="1.0", kind="media_chunk", job_id="extension_job", note_id="note_123",
        role="image", position=1, sequence=1, chunk_index=0, data_base64=payload,
    )

    with pytest.raises(ValueError):
        job.append_media_chunk(message)

    assert job._active_media is None
    assert all(path.name.startswith(".") for path in (job.output_dir / ".staging").iterdir())
    assert job.finish_job().status.value == "partial"


def test_invalid_video_container_closes_slot_without_assets(tmp_path: Path) -> None:
    """An MP4 signature without the required box structure must stay rejected."""
    output = tmp_path / "host_run_invalid_video"
    job = ExtensionJob("host_run_invalid_video", output, _request())
    job.add_candidate(_snapshot(role="video"))
    job.finish_scan()
    job.begin_media(_begin(role="video", size_limit_bytes=100 * 1024 * 1024))
    malformed = b"\x00\x00\x00\x08ftyp"
    job.append_media_chunk(
        ExtensionMediaChunk(
            protocol_version="1.0", kind="media_chunk", job_id="extension_job",
            note_id="note_123", role="video", position=1, sequence=1, chunk_index=0,
            data_base64=base64.b64encode(malformed).decode("ascii"),
        )
    )
    slot = job.finish_media(
        ExtensionMediaEnd(
            protocol_version="1.0", kind="media_end", job_id="extension_job",
            note_id="note_123", role="video", position=1, sequence=1,
            mime_type="video/mp4", sha256=hashlib.sha256(malformed).hexdigest(),
        )
    )

    assert slot.status == "rejected"
    assert not any((output / "assets").iterdir())


def test_polyglot_header_is_not_accepted_as_a_direct_mp4(tmp_path: Path) -> None:
    """A file that starts as one approved type cannot masquerade as another media role."""
    output = tmp_path / "host_run_polyglot"
    job = ExtensionJob("host_run_polyglot", output, _request())
    job.add_candidate(_snapshot(role="video"))
    job.finish_scan()
    job.begin_media(_begin(role="video", size_limit_bytes=MAX_VIDEO_BYTES))
    polyglot = WEBP + MP4
    job.append_media_chunk(
        ExtensionMediaChunk(
            protocol_version="1.0", kind="media_chunk", job_id="extension_job",
            note_id="note_123", role="video", position=1, sequence=1, chunk_index=0,
            data_base64=base64.b64encode(polyglot).decode("ascii"),
        )
    )

    slot = job.finish_media(
        ExtensionMediaEnd(
            protocol_version="1.0", kind="media_end", job_id="extension_job",
            note_id="note_123", role="video", position=1, sequence=1,
            mime_type="video/mp4", sha256=hashlib.sha256(polyglot).hexdigest(),
        )
    )

    assert slot.status == "rejected"
    assert slot.missing_reason == "mime_mismatch"
    assert not any((output / "assets").iterdir())


@pytest.mark.parametrize(
    ("role", "declared", "note_used", "run_used", "expected", "reason"),
    [
        ("image", 100 * 1024 * 1024, 0, 0, 15 * 1024 * 1024, "size_limit"),
        ("video", 100 * 1024 * 1024, 0, 0, 100 * 1024 * 1024, "size_limit"),
        ("image", 15 * 1024 * 1024, 120 * 1024 * 1024, 0, 0, "note_budget"),
        ("image", 15 * 1024 * 1024, 0, 250 * 1024 * 1024, 0, "run_budget"),
    ],
)
def test_begin_media_clamps_role_note_and_run_budget_before_staging(
    tmp_path: Path,
    role: str,
    declared: int,
    note_used: int,
    run_used: int,
    expected: int,
    reason: str,
) -> None:
    """The effective transfer ceiling must be bounded before a large chunk is written."""
    job = ExtensionJob(f"host_run_budget_{role}_{note_used}_{run_used}", tmp_path / f"budget_{role}_{note_used}_{run_used}", _request())
    job.add_candidate(_snapshot(role=role))
    job.finish_scan()
    job._downloaded_note_bytes["note_123"] = note_used
    job._downloaded_run_bytes = run_used
    job.begin_media(_begin(role=role, size_limit_bytes=declared))

    if expected == 0:
        slot = job._slot_results[("note_123", role, 1)]
        assert slot.status == "rejected"
        assert slot.missing_reason == reason
    else:
        assert job._active_media is not None
        assert job._active_media.maximum_bytes == expected
    job.discard()


def test_symlinked_staging_file_discards_without_following_the_link(tmp_path: Path) -> None:
    """Replacing a staging path with a symlink must not make the host follow it."""
    output = tmp_path / "host_run_symlink"
    job = ExtensionJob("host_run_symlink", output, _request())
    job.add_candidate(_snapshot())
    job.finish_scan()
    job.begin_media(_begin())
    assert job._active_media is not None
    staged = job._staging_dir / job._active_media.staging_name
    staged.unlink()
    outside = tmp_path / "outside.webp"
    outside.write_bytes(WEBP)
    staged.symlink_to(outside)
    with pytest.raises(ValueError, match="filesystem"):
        job.finish_media(
            ExtensionMediaEnd(
                protocol_version="1.0", kind="media_end", job_id="extension_job",
                note_id="note_123", role="image", position=1, sequence=1,
                mime_type="image/webp", sha256=hashlib.sha256(WEBP).hexdigest(),
            )
        )

    assert job.state.value == "stopped"
    assert not output.exists()
    assert outside.read_bytes() == WEBP


def test_staging_write_or_finalization_failure_discards_owned_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An internal I/O failure must leave the run stopped rather than stranded."""
    output = tmp_path / "host_run_io_failure"
    job = ExtensionJob("host_run_io_failure", output, _request())
    job.add_candidate(_snapshot())
    job.finish_scan()
    job.begin_media(_begin())
    monkeypatch.setattr(extension_import, "_write_all", lambda _fd, _chunk: (_ for _ in ()).throw(OSError("fail")))

    with pytest.raises(ValueError, match="staging write"):
        job.append_media_chunk(
            ExtensionMediaChunk(
                protocol_version="1.0", kind="media_chunk", job_id="extension_job",
                note_id="note_123", role="image", position=1, sequence=1, chunk_index=0,
                data_base64=base64.b64encode(WEBP).decode("ascii"),
            )
        )

    assert job.state.value == "stopped"
    assert not output.exists()


def test_finalization_staging_error_and_messages_after_finish_are_terminal(tmp_path: Path) -> None:
    """A nonempty staging directory or post-finish message cannot revive a run."""
    output = tmp_path / "host_run_finalize_error"
    job = ExtensionJob("host_run_finalize_error", output, _request())
    job.add_candidate(_snapshot())
    job.finish_scan()
    job.mark_media_missing(
        ExtensionMediaMissing(
            protocol_version="1.0", kind="media_missing", job_id="extension_job",
            note_id="note_123", role="image", position=1, reason="source_not_exposed",
        )
    )
    (job._staging_dir / ".injected.staging").write_bytes(b"x")

    with pytest.raises(ValueError, match="staging directory"):
        job.finish_job()

    assert job.state.value == "stopped"
    assert not output.exists()


def test_media_reopen_failure_discards_run_instead_of_returning_rejected_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A filesystem failure is terminal, unlike a rejected content container."""
    output = tmp_path / "host_run_reopen_failure"
    job = ExtensionJob("host_run_reopen_failure", output, _request())
    job.add_candidate(_snapshot())
    job.finish_scan()
    job.begin_media(_begin())
    monkeypatch.setattr(
        extension_import,
        "_snapshot_staged_file",
        lambda *_args: (_ for _ in ()).throw(OSError("reopen failed")),
    )

    with pytest.raises(ValueError, match="filesystem"):
        job.finish_media(
            ExtensionMediaEnd(
                protocol_version="1.0", kind="media_end", job_id="extension_job",
                note_id="note_123", role="image", position=1, sequence=1,
                mime_type="image/webp", sha256=hashlib.sha256(WEBP).hexdigest(),
            )
        )

    assert job.state.value == "stopped"
    assert not output.exists()
    with pytest.raises(ValueError, match="not open"):
        job.finish_job()


def test_selected_note_scope_excludes_declared_slots_of_unselected_candidates(tmp_path: Path) -> None:
    """Selection, rather than candidate discovery, owns the media acceptance scope."""
    job = ExtensionJob("host_run_selected_scope", tmp_path / "host_run_selected_scope", _batch_request())
    job.add_candidate(_snapshot(note_id="note_selected", likes=9))
    job.add_candidate(_snapshot(note_id="note_unselected", source_position=2, likes=1))
    job.finish_scan()

    assert job.selected_note_ids == ("note_selected",)
    with pytest.raises(ValueError, match="declared by a selected"):
        job.begin_media(_begin(note_id="note_unselected"))

    job.mark_media_missing(
        ExtensionMediaMissing(
            protocol_version="1.0", kind="media_missing", job_id="extension_job",
            note_id="note_selected", role="image", position=1, reason="source_not_exposed",
        )
    )
    assert job.finish_job().status.value == "partial"


def test_replayed_transfer_sequence_is_rejected_after_a_closed_first_slot(tmp_path: Path) -> None:
    """A completed transfer cannot replay its sequence into another declared slot."""
    job = ExtensionJob("host_run_sequence_replay", tmp_path / "host_run_sequence_replay", _request())
    job.add_candidate(
        _snapshot(
            media_slots=[
                {"note_id": "note_123", "role": "image", "position": 1},
                {"note_id": "note_123", "role": "image", "position": 2},
            ]
        )
    )
    job.finish_scan()
    job.begin_media(_begin(sequence=1, position=1))
    rejected = job.finish_media(
        ExtensionMediaEnd(
            protocol_version="1.0", kind="media_end", job_id="extension_job",
            note_id="note_123", role="image", position=1, sequence=1,
            mime_type="image/webp", sha256=hashlib.sha256(WEBP).hexdigest(),
        )
    )

    assert rejected.status == "rejected"
    with pytest.raises(ValueError, match="sequence"):
        job.begin_media(_begin(sequence=1, position=2))
    job.discard()


def test_batch_media_sequence_can_cross_the_per_note_slot_boundary(tmp_path: Path) -> None:
    """A job-global sequence must accommodate selected slots from multiple notes."""
    job = ExtensionJob("host_run_many_sequences", tmp_path / "host_run_many_sequences", _batch_request(requested_count=2))
    first_note = "note_first"
    second_note = "note_second"
    first_slots = [
        {"note_id": first_note, "role": "image", "position": position}
        for position in range(1, 21)
    ]
    second_slots = [
        {"note_id": second_note, "role": "image", "position": position}
        for position in range(1, 4)
    ]
    job.add_candidate(_snapshot(note_id=first_note, likes=9, media_slots=first_slots))
    job.add_candidate(_snapshot(note_id=second_note, source_position=2, likes=8, media_slots=second_slots))
    job.finish_scan()

    sequence = 1
    for note_id, slots in ((first_note, first_slots), (second_note, second_slots)):
        for slot in slots:
            position = slot["position"]
            job.begin_media(_begin(note_id=note_id, position=position, sequence=sequence))
            job.append_media_chunk(
                ExtensionMediaChunk(
                    protocol_version="1.0", kind="media_chunk", job_id="extension_job",
                    note_id=note_id, role="image", position=position, sequence=sequence,
                    chunk_index=0, data_base64=base64.b64encode(WEBP).decode("ascii"),
                )
            )
            job.finish_media(
                ExtensionMediaEnd(
                    protocol_version="1.0", kind="media_end", job_id="extension_job",
                    note_id=note_id, role="image", position=position, sequence=sequence,
                    mime_type="image/webp", sha256=hashlib.sha256(WEBP).hexdigest(),
                )
            )
            sequence += 1

    assert sequence == 24
    assert job.finish_job().status.value == "complete"


@pytest.mark.parametrize(
    ("label", "role", "size_limit_bytes", "note_used", "run_used", "limit", "reason"),
    [
        ("image", "image", MAX_IMAGE_BYTES, 0, 0, MAX_IMAGE_BYTES, "size_limit"),
        ("video", "video", MAX_VIDEO_BYTES, 0, 0, MAX_VIDEO_BYTES, "size_limit"),
        ("note", "image", MAX_IMAGE_BYTES, MAX_NOTE_MEDIA_BYTES - 1, 0, 1, "note_budget"),
        ("run", "image", MAX_IMAGE_BYTES, 0, MAX_RUN_MEDIA_BYTES - 1, 1, "run_budget"),
    ],
)
def test_exact_budget_ceiling_accepts_one_byte_and_rejects_the_next(
    tmp_path: Path,
    label: str,
    role: str,
    size_limit_bytes: int,
    note_used: int,
    run_used: int,
    limit: int,
    reason: str,
) -> None:
    """The 15/100/120/250 MiB ceilings are enforced before a second byte lands."""
    assert (MAX_IMAGE_BYTES, MAX_VIDEO_BYTES, MAX_NOTE_MEDIA_BYTES, MAX_RUN_MEDIA_BYTES) == (
        15 * 1024 * 1024,
        100 * 1024 * 1024,
        120 * 1024 * 1024,
        250 * 1024 * 1024,
    )
    output = tmp_path / f"host_run_append_{label}"
    job = ExtensionJob(f"host_run_append_{label}", output, _request())
    job.add_candidate(_snapshot(role=role))
    job.finish_scan()
    job._downloaded_note_bytes["note_123"] = note_used
    job._downloaded_run_bytes = run_used
    job.begin_media(_begin(role=role, size_limit_bytes=size_limit_bytes))
    assert job._active_media is not None
    if limit > 1:
        # Simulate a sparse prior stream: only the boundary byte is written here.
        job._active_media.size_bytes = limit - 1

    one_byte = base64.b64encode(b"x").decode("ascii")
    job.append_media_chunk(
        ExtensionMediaChunk(
            protocol_version="1.0", kind="media_chunk", job_id="extension_job",
            note_id="note_123", role=role, position=1, sequence=1, chunk_index=0,
            data_base64=one_byte,
        )
    )
    assert job._active_media is not None
    assert job._active_media.size_bytes == limit

    with pytest.raises(ValueError, match="size limit"):
        job.append_media_chunk(
            ExtensionMediaChunk(
                protocol_version="1.0", kind="media_chunk", job_id="extension_job",
                note_id="note_123", role=role, position=1, sequence=1, chunk_index=1,
                data_base64=one_byte,
            )
        )

    slot = job._slot_results[("note_123", role, 1)]
    assert slot.status == "rejected"
    assert slot.missing_reason == reason
    assert job._active_media is None
    assert all(path.name.startswith(".") for path in (output / ".staging").iterdir())
    job.discard()


@pytest.mark.parametrize(
    ("label", "role", "size_limit_bytes", "note_used", "run_used", "size_bytes", "reason"),
    [
        ("image", "image", MAX_IMAGE_BYTES, 0, 0, MAX_IMAGE_BYTES, "size_limit"),
        ("video", "video", MAX_VIDEO_BYTES, 0, 0, MAX_VIDEO_BYTES, "size_limit"),
        ("note", "image", MAX_IMAGE_BYTES, MAX_NOTE_MEDIA_BYTES - 1, 0, 1, "note_budget"),
        ("run", "image", MAX_IMAGE_BYTES, 0, MAX_RUN_MEDIA_BYTES - 1, 1, "run_budget"),
    ],
)
def test_finish_media_accepts_exact_fake_boundary_and_rejects_one_byte_over(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    label: str,
    role: str,
    size_limit_bytes: int,
    note_used: int,
    run_used: int,
    size_bytes: int,
    reason: str,
) -> None:
    """Finish-time accounting preserves exact limits without allocating giant payloads."""
    digest = hashlib.sha256(label.encode("ascii")).hexdigest()

    def finish_with_fake_size(fake_size: int, suffix: str):
        job = ExtensionJob(f"host_run_finish_{label}_{suffix}", tmp_path / f"finish_{label}_{suffix}", _request())
        job.add_candidate(_snapshot(role=role))
        job.finish_scan()
        job._downloaded_note_bytes["note_123"] = note_used
        job._downloaded_run_bytes = run_used
        job.begin_media(_begin(role=role, size_limit_bytes=size_limit_bytes))
        monkeypatch.setattr(
            extension_import,
            "_snapshot_staged_file",
            lambda *_args: extension_import._MediaSnapshot(fake_size, digest),
        )
        slot = job.finish_media(
            ExtensionMediaEnd(
                protocol_version="1.0", kind="media_end", job_id="extension_job",
                note_id="note_123", role=role, position=1, sequence=1,
                mime_type="video/mp4" if role == "video" else "image/webp", sha256=digest,
            )
        )
        return job, slot

    accepted, downloaded = finish_with_fake_size(size_bytes, "exact")
    assert downloaded.status == "downloaded"
    assert downloaded.asset is not None and downloaded.asset.size_bytes == size_bytes
    accepted.discard()

    overflow, rejected = finish_with_fake_size(size_bytes + 1, "over")
    assert rejected.status == "rejected"
    assert rejected.missing_reason == reason
    assert not any((overflow.output_dir / "assets").iterdir())
    overflow.discard()


def test_output_path_traversal_and_symlinked_run_directory_are_rejected(tmp_path: Path) -> None:
    """The host cannot be redirected outside its chosen fresh run directory."""
    (tmp_path / "safe").mkdir()
    with pytest.raises(ValueError, match="output directory"):
        ExtensionJob("host_run_traversal", tmp_path / "safe" / ".." / "escape", _request())

    outside = tmp_path / "outside"
    outside.mkdir()
    output_link = tmp_path / "output_link"
    output_link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="output directory"):
        ExtensionJob("host_run_link", output_link, _request())
    assert list(outside.iterdir()) == []


def test_run_directory_rejects_an_ancestor_symlink_but_accepts_safe_ancestors(tmp_path: Path) -> None:
    """A fresh run must not escape through a pre-existing output-root ancestor."""
    safe_parent = tmp_path / "safe_parent"
    safe_parent.mkdir()
    safe_job = ExtensionJob("host_run_safe_ancestor", safe_parent / "fresh_run", _request())
    assert safe_job.output_dir == safe_parent / "fresh_run"
    safe_job.discard()

    outside = tmp_path / "outside_ancestor"
    outside.mkdir()
    parent_link = tmp_path / "parent_link"
    parent_link.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="output directory"):
        ExtensionJob("host_run_ancestor_link", parent_link / "fresh_run", _request())
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("boundary", ["user_stop", "native_port_interruption"])
def test_discard_boundary_removes_owned_run_and_refuses_later_messages(
    tmp_path: Path, boundary: str
) -> None:
    """Both explicit stop and native-port interruption use the finite discard boundary."""
    output = tmp_path / f"host_run_{boundary}"
    job = ExtensionJob(f"host_run_{boundary}", output, _request())
    job.add_candidate(_snapshot())
    job.finish_scan()
    job.begin_media(_begin())

    job.discard()

    assert job.state.value == "stopped"
    assert not output.exists()
    with pytest.raises(ValueError, match="not open"):
        job.finish_job()
    with pytest.raises(ValueError, match="not open"):
        job.begin_media(_begin())


def test_batch_bundle_orders_selected_notes_and_uses_only_relative_local_media(tmp_path: Path) -> None:
    """The bundle renders selection rank and never turns media into a remote source URL."""
    output = tmp_path / "host_run_local_bundle"
    job = ExtensionJob("host_run_local_bundle", output, _batch_request(requested_count=2))
    job.add_candidate(_snapshot(note_id="note_high", likes=10))
    job.add_candidate(_snapshot(note_id="note_low", source_position=2, likes=5))
    job.finish_scan()
    job.begin_media(_begin(note_id="note_high"))
    job.append_media_chunk(
        ExtensionMediaChunk(
            protocol_version="1.0", kind="media_chunk", job_id="extension_job",
            note_id="note_high", role="image", position=1, sequence=1, chunk_index=0,
            data_base64=base64.b64encode(WEBP).decode("ascii"),
        )
    )
    job.finish_media(
        ExtensionMediaEnd(
            protocol_version="1.0", kind="media_end", job_id="extension_job",
            note_id="note_high", role="image", position=1, sequence=1,
            mime_type="image/webp", sha256=hashlib.sha256(WEBP).hexdigest(),
        )
    )
    job.mark_media_missing(
        ExtensionMediaMissing(
            protocol_version="1.0", kind="media_missing", job_id="extension_job",
            note_id="note_low", role="image", position=1, reason="source_not_exposed",
        )
    )
    run = job.finish_job()
    results = (output / "results.json").read_text(encoding="utf-8")
    html = (output / "index.html").read_text(encoding="utf-8")

    assert run.status.value == "partial"
    assert html.index("笔记 ID：note_high") < html.index("笔记 ID：note_low")
    assert 'src="assets/note_high-image-001.webp"' in html
    assert "assets/note_high-image-001.webp" in results
    assert not any(marker in results or marker in html for marker in ("media_url", "signed", "X-Amz-", "token="))
    assert '"metric_provenance"' in results
    assert "metric_provenance" not in html
    assert "precision" not in html


@pytest.mark.parametrize("failure", ["validation", "publish", "unsafe_publish", "reject_cleanup"])
def test_finish_media_filesystem_failures_discard_the_owned_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """No finish-time I/O failure can leave a later message able to complete the run."""
    output = tmp_path / f"host_run_finish_io_{failure}"
    job = ExtensionJob(f"host_run_finish_io_{failure}", output, _request())
    job.add_candidate(_snapshot())
    job.finish_scan()
    job.begin_media(_begin())
    job.append_media_chunk(
        ExtensionMediaChunk(
            protocol_version="1.0", kind="media_chunk", job_id="extension_job",
            note_id="note_123", role="image", position=1, sequence=1, chunk_index=0,
            data_base64=base64.b64encode(WEBP).decode("ascii"),
        )
    )
    media_end = ExtensionMediaEnd(
        protocol_version="1.0", kind="media_end", job_id="extension_job",
        note_id="note_123", role="image", position=1, sequence=1,
        mime_type="image/webp",
        sha256=(hashlib.sha256(WEBP).hexdigest() if failure in {"publish", "unsafe_publish"} else "0" * 64),
    )
    if failure == "validation":
        monkeypatch.setattr(
            extension_import,
            "validate_media_container",
            lambda *_args: (_ for _ in ()).throw(OSError("validation read failed")),
        )
        media_end = media_end.model_copy(update={"sha256": hashlib.sha256(WEBP).hexdigest()})
    elif failure == "publish":
        monkeypatch.setattr(
            extension_import,
            "_publish_private_file",
            lambda *_args: (_ for _ in ()).throw(OSError("publish failed")),
        )
    elif failure == "unsafe_publish":
        monkeypatch.setattr(
            extension_import,
            "_publish_private_file",
            lambda *_args: (_ for _ in ()).throw(ValueError("unsafe publish state")),
        )
    else:
        monkeypatch.setattr(
            extension_import,
            "_unlink_private_file",
            lambda *_args: (_ for _ in ()).throw(OSError("unlink failed")),
        )

    with pytest.raises(ValueError, match="filesystem|cleanup|finalization"):
        job.finish_media(media_end)

    assert job._active_media is None
    assert job.state.value == "stopped"
    assert not output.exists()
    with pytest.raises(ValueError, match="not open"):
        job.finish_job()


def test_internal_reject_cleanup_failure_is_terminal_and_retains_recovery_residue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Transient lower-level cleanup faults cannot become a rejected-and-continuing slot."""
    output = tmp_path / "host_run_internal_cleanup_open"
    job = ExtensionJob("host_run_internal_cleanup_open", output, _request())
    job.add_candidate(_snapshot())
    job.finish_scan()
    job.begin_media(_begin())
    assert job._active_media is not None
    staging_name = job._active_media.staging_name
    job.append_media_chunk(
        ExtensionMediaChunk(
            protocol_version="1.0", kind="media_chunk", job_id="extension_job",
            note_id="note_123", role="image", position=1, sequence=1, chunk_index=0,
            data_base64=base64.b64encode(WEBP).decode("ascii"),
        )
    )
    original_open = extension_import.os.open
    remaining = {"count": 1}

    def fail_staging_open(path: object, *args: object, **kwargs: object) -> int:
        if (
            path == staging_name
            and kwargs.get("dir_fd") == job._staging_fd
            and remaining["count"]
        ):
            remaining["count"] -= 1
            raise OSError("transient staging directory open failure")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(extension_import.os, "open", fail_staging_open)

    with pytest.raises(ValueError, match="cleanup|filesystem|finalization"):
        job.finish_media(
            ExtensionMediaEnd(
                protocol_version="1.0", kind="media_end", job_id="extension_job",
                note_id="note_123", role="image", position=1, sequence=1,
                mime_type="image/webp", sha256="0" * 64,
            )
        )

    assert remaining["count"] == 0
    assert job._active_media is None
    assert job.state.value == "stopped"
    assert not output.exists()
    with pytest.raises(ValueError, match="not open"):
        job.finish_job()


def test_cleanup_fallback_never_path_deletes_after_an_unexpected_unlink_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cleanup retains an opaque run instead of ever unlinking a mutable name."""
    output = tmp_path / "host_run_persistent_cleanup"
    job = ExtensionJob("host_run_persistent_cleanup", output, _request())
    original_unlink = extension_import.os.unlink

    def fail_every_unlink(path: object, *args: object, **kwargs: object) -> None:
        if kwargs.get("dir_fd") is not None:
            raise AssertionError("cleanup must not unlink through a pathname")
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(extension_import.os, "unlink", fail_every_unlink)

    job.discard()

    assert job.state.value == "stopped"
    assert job.cleanup_requires_recovery is True
    assert not output.exists()


@pytest.mark.parametrize("swap", ["run", "ancestor"])
def test_post_creation_path_replacement_never_uses_external_media_paths(
    tmp_path: Path, swap: str
) -> None:
    """Replacing a visible run path cannot redirect later media or bundle I/O."""
    if swap == "run":
        output = tmp_path / "host_run_path_replaced"
        displaced = tmp_path / "displaced_run"
    else:
        parent = tmp_path / "mutable_parent"
        parent.mkdir()
        output = parent / "host_run_path_replaced"
        displaced = tmp_path / "displaced_parent"
    job = ExtensionJob("host_run_path_replaced", output, _request())
    job.add_candidate(_snapshot())
    job.finish_scan()
    job.begin_media(_begin())
    assert job._active_media is not None
    staging_name = job._active_media.staging_name

    outside = tmp_path / "outside"
    outside.mkdir()
    if swap == "run":
        output.rename(displaced)
        output.symlink_to(outside, target_is_directory=True)
        outside_run = outside
    else:
        output.parent.rename(displaced)
        output.parent.symlink_to(outside, target_is_directory=True)
        outside_run = outside / output.name
        outside_run.mkdir()
    outside_staging = outside_run / ".staging"
    outside_assets = outside_run / "assets"
    outside_staging.mkdir()
    outside_assets.mkdir()
    outside_staged = outside_staging / staging_name
    outside_staged.write_bytes(WEBP)

    job.append_media_chunk(
        ExtensionMediaChunk(
            protocol_version="1.0", kind="media_chunk", job_id="extension_job",
            note_id="note_123", role="image", position=1, sequence=1, chunk_index=0,
            data_base64=base64.b64encode(WEBP).decode("ascii"),
        )
    )
    job.finish_media(
        ExtensionMediaEnd(
            protocol_version="1.0", kind="media_end", job_id="extension_job",
            note_id="note_123", role="image", position=1, sequence=1,
            mime_type="image/webp", sha256=hashlib.sha256(WEBP).hexdigest(),
        )
    )

    with pytest.raises(ValueError, match="output|cleanup|staging"):
        job.finish_job()

    assert outside_staged.read_bytes() == WEBP
    assert list(outside_assets.iterdir()) == []
    assert not (outside_run / "results.json").exists()
    assert not (outside_run / "index.html").exists()
    assert job.state.value == "stopped"

    if swap == "run":
        output.unlink()
        displaced.rename(output)
    else:
        output.parent.unlink()
        displaced.rename(output.parent)
    job.discard()
    if swap == "run":
        assert output.exists()
        assert job.cleanup_requires_recovery is False
    else:
        assert not output.exists()
        assert job.cleanup_requires_recovery is True


def test_discard_does_not_mistake_an_injected_quarantine_move_failure_for_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed capability move leaves the job terminal until explicit retry."""
    output = tmp_path / "host_run_parent_open_failure"
    job = ExtensionJob("host_run_parent_open_failure", output, _request())
    job.add_candidate(_snapshot())
    job.finish_scan()
    job.begin_media(_begin())
    original_move = extension_import._rename_no_replace

    with monkeypatch.context() as scoped:
        def fail_cleanup_move(parent_fd: int, source: str, destination: str) -> None:
            if source == output.name and parent_fd == job._output_parent_fd:
                raise FileNotFoundError("injected cleanup move failure")
            original_move(parent_fd, source, destination)

        scoped.setattr(extension_import, "_rename_no_replace", fail_cleanup_move)
        with pytest.raises(ValueError, match="owned run cleanup"):
            job.discard()

    assert output.exists()
    assert job.state.value == "stopped"
    assert not (output / "results.json").exists()
    with pytest.raises(ValueError, match="not open"):
        job.finish_job()

    job.discard()
    assert output.exists()


@pytest.mark.parametrize("swap", ["run", "ancestor"])
def test_finish_job_bundle_handoff_never_reads_or_writes_the_replaced_visible_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, swap: str
) -> None:
    """A post-check path swap must not redirect bundle asset or result I/O."""
    if swap == "run":
        output = tmp_path / "host_run_bundle_handoff"
        displaced = tmp_path / "held_run"
        external_root = tmp_path / "external_run"
    else:
        parent = tmp_path / "mutable_parent"
        parent.mkdir()
        output = parent / "host_run_bundle_handoff"
        displaced = tmp_path / "held_parent"
        external_root = tmp_path / "external_parent"
    job = ExtensionJob("host_run_bundle_handoff", output, _request())
    job.add_candidate(_snapshot())
    job.finish_scan()
    job.begin_media(_begin())
    job.append_media_chunk(
        ExtensionMediaChunk(
            protocol_version="1.0", kind="media_chunk", job_id="extension_job",
            note_id="note_123", role="image", position=1, sequence=1, chunk_index=0,
            data_base64=base64.b64encode(WEBP).decode("ascii"),
        )
    )
    job.finish_media(
        ExtensionMediaEnd(
            protocol_version="1.0", kind="media_end", job_id="extension_job",
            note_id="note_123", role="image", position=1, sequence=1,
            mime_type="image/webp", sha256=hashlib.sha256(WEBP).hexdigest(),
        )
    )

    external_root.mkdir()
    if swap == "run":
        external_run = external_root
    else:
        external_run = external_root / output.name
        external_run.mkdir()
    external_assets = external_run / "assets"
    external_assets.mkdir()
    external_asset = external_assets / "note_123-image-001.webp"
    external_asset.write_bytes(b"external-asset-sentinel")
    external_asset_identity = external_assets.stat().st_dev, external_assets.stat().st_ino
    external_asset_reads: list[object] = []

    from xhs_workbench import renderer

    real_open = renderer.os.open
    real_writer = extension_import.write_result_bundle_at

    def track_external_asset_open(path: object, *args: object, **kwargs: object) -> int:
        directory_fd = kwargs.get("dir_fd")
        if isinstance(directory_fd, int):
            try:
                metadata = os.fstat(directory_fd)
            except OSError:
                metadata = None
            if (
                metadata is not None
                and (metadata.st_dev, metadata.st_ino) == external_asset_identity
            ):
                external_asset_reads.append(path)
        return real_open(path, *args, **kwargs)

    def replace_visible_path_at_writer_handoff(
        run: object, run_fd: int, assets_fd: int
    ) -> None:
        if swap == "run":
            output.rename(displaced)
            output.symlink_to(external_root, target_is_directory=True)
        else:
            output.parent.rename(displaced)
            output.parent.symlink_to(external_root, target_is_directory=True)
        real_writer(run, run_fd, assets_fd)

    monkeypatch.setattr(renderer.os, "open", track_external_asset_open)
    monkeypatch.setattr(
        extension_import, "write_result_bundle_at", replace_visible_path_at_writer_handoff
    )

    with pytest.raises(ValueError, match="output|cleanup"):
        job.finish_job()

    assert external_asset.read_bytes() == b"external-asset-sentinel"
    assert external_asset_reads == []
    assert not (external_run / "results.json").exists()
    assert not (external_run / "index.html").exists()
    assert job.state.value == "stopped"


def test_discard_quarantines_a_run_replacement_without_deleting_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A source rebound before the atomic move remains intact as opaque recovery data."""
    output = tmp_path / "host_run_cleanup_handoff"
    displaced = tmp_path / "held_run"
    job = ExtensionJob("host_run_cleanup_handoff", output, _request())
    real_move = extension_import._rename_no_replace
    handoff: dict[str, str] = {}

    def replace_run_before_atomic_move(parent_fd: int, source: str, destination: str) -> None:
        if source == output.name and parent_fd == job._output_parent_fd and not handoff:
            output.rename(displaced)
            output.mkdir(mode=0o700)
            (output / "sentinel.txt").write_bytes(b"replacement-run-sentinel")
            handoff["quarantine"] = destination
        real_move(parent_fd, source, destination)

    monkeypatch.setattr(extension_import, "_rename_no_replace", replace_run_before_atomic_move)

    with pytest.raises(ValueError, match="owned run cleanup"):
        job.discard()

    assert (tmp_path / handoff["quarantine"] / "sentinel.txt").read_bytes() == b"replacement-run-sentinel"
    assert displaced.is_dir()
    assert job.state.value == "stopped"


def test_discard_quarantines_only_the_root_and_never_recurses_into_children(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Conservative cleanup leaves all child entries intact once the root is isolated."""
    output = tmp_path / "host_run_child_cleanup_handoff"
    job = ExtensionJob("host_run_child_cleanup_handoff", output, _request())
    (output / "assets" / "sentinel.txt").write_bytes(b"child-sentinel")

    def fail_if_recursive(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("cleanup must not traverse a quarantined run")

    monkeypatch.setattr(extension_import, "_remove_directory_contents", fail_if_recursive)

    job.discard()

    residue = next(tmp_path.glob(".xhs-cleanup-*"))
    assert (residue / "assets" / "sentinel.txt").read_bytes() == b"child-sentinel"
    assert job.cleanup_requires_recovery is True


def test_finish_job_rejects_a_staging_replacement_and_retains_it_as_residue(tmp_path: Path) -> None:
    """A replacement staging directory is not trusted or deleted during finalization."""
    output = tmp_path / "host_run_staging_cleanup_handoff"
    job = ExtensionJob("host_run_staging_cleanup_handoff", output, _request())
    job.add_candidate(_snapshot())
    job.finish_scan()
    job.mark_media_missing(
        ExtensionMediaMissing(
            protocol_version="1.0", kind="media_missing", job_id="extension_job",
            note_id="note_123", role="image", position=1, reason="source_not_exposed",
        )
    )
    (output / ".staging").rename(output / ".staging-original")
    (output / ".staging").mkdir(mode=0o700)
    (output / ".staging" / "sentinel.txt").write_bytes(b"replacement-staging-sentinel")

    with pytest.raises(ValueError, match="staging|cleanup"):
        job.finish_job()

    residue = next(tmp_path.glob(".xhs-cleanup-*"))
    assert (residue / ".staging" / "sentinel.txt").read_bytes() == b"replacement-staging-sentinel"
    assert job.state.value == "stopped"
    assert job.cleanup_requires_recovery is True


@pytest.mark.parametrize("kind", ["directory", "regular", "symlink"])
def test_quarantine_reservation_race_never_overwrites_an_occupied_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    """A target created after name selection must survive the no-replace move."""
    parent = tmp_path / "cleanup-parent"
    parent.mkdir(mode=0o700)
    source_name = "owned"
    quarantine_name = ".xhs-cleanup-raced"
    source = parent / source_name
    target = parent / quarantine_name
    if kind == "directory":
        source.mkdir(mode=0o700)
        (source / "owned.txt").write_bytes(b"owned")
    elif kind == "regular":
        source.write_bytes(b"owned")
    else:
        source.symlink_to("owned-target")
    parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        metadata = os.lstat(source_name, dir_fd=parent_fd)
        expected_identity = metadata.st_dev, metadata.st_ino

        def occupy_selected_name(directory_fd: int) -> str:
            assert directory_fd == parent_fd
            if not target.exists() and not target.is_symlink():
                if kind == "directory":
                    target.mkdir(mode=0o700)
                    (target / "sentinel.txt").write_bytes(b"replacement-directory")
                elif kind == "regular":
                    target.write_bytes(b"replacement-regular")
                else:
                    target.symlink_to("replacement-symlink")
            return quarantine_name

        monkeypatch.setattr(extension_import, "_reserve_quarantine_name", occupy_selected_name)

        with pytest.raises(OSError):
            extension_import._quarantine_owned_entry(
                parent_fd, source_name, expected_identity, kind  # type: ignore[arg-type]
            )
    finally:
        os.close(parent_fd)

    assert source.exists() or source.is_symlink()
    if kind == "directory":
        assert (target / "sentinel.txt").read_bytes() == b"replacement-directory"
    elif kind == "regular":
        assert target.read_bytes() == b"replacement-regular"
    else:
        assert os.readlink(target) == "replacement-symlink"


@pytest.mark.parametrize("kind", ["directory", "regular", "symlink"])
def test_final_cleanup_recheck_never_deletes_a_rebound_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    """A replacement after final verification is recovery residue, never a deletion target."""
    parent = tmp_path / "cleanup-parent"
    parent.mkdir(mode=0o700)
    name = ".xhs-cleanup-final-race"
    held_name = ".held-original"
    target = parent / name
    if kind == "directory":
        target.mkdir(mode=0o700)
    elif kind == "regular":
        target.write_bytes(b"owned")
    else:
        target.symlink_to("owned-target")
    parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    held_fd = -1
    rebound = False
    try:
        metadata = os.lstat(name, dir_fd=parent_fd)
        expected_identity = metadata.st_dev, metadata.st_ino
        if kind == "directory":
            held_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
        real_lstat = extension_import.os.lstat
        real_rename = extension_import.os.rename
        lstat_calls = 0

        def replace_after_final_check(path: object, *args: object, **kwargs: object) -> os.stat_result:
            nonlocal lstat_calls, rebound
            result = real_lstat(path, *args, **kwargs)
            if path == name and kwargs.get("dir_fd") == parent_fd:
                lstat_calls += 1
                should_rebind = kind != "directory" or lstat_calls == 2
                if should_rebind and not rebound:
                    real_rename(name, held_name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
                    if kind == "directory":
                        os.mkdir(name, dir_fd=parent_fd)
                        replacement_fd = os.open(
                            name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd
                        )
                        try:
                            sentinel_fd = os.open(
                                "sentinel.txt",
                                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                0o600,
                                dir_fd=replacement_fd,
                            )
                            try:
                                os.write(sentinel_fd, b"replacement-directory")
                            finally:
                                os.close(sentinel_fd)
                        finally:
                            os.close(replacement_fd)
                    elif kind == "regular":
                        replacement_fd = os.open(
                            name,
                            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                            0o600,
                            dir_fd=parent_fd,
                        )
                        try:
                            os.write(replacement_fd, b"replacement-regular")
                        finally:
                            os.close(replacement_fd)
                    else:
                        os.symlink("replacement-symlink", name, dir_fd=parent_fd)
                    rebound = True
            return result

        monkeypatch.setattr(extension_import.os, "lstat", replace_after_final_check)

        with pytest.raises(OSError):
            if kind == "directory":
                extension_import._remove_quarantined_directory(
                    parent_fd, name, expected_identity, held_fd
                )
            elif kind == "regular":
                extension_import._remove_quarantined_regular_file(
                    parent_fd, name, expected_identity
                )
            else:
                extension_import._remove_quarantined_symlink(parent_fd, name, expected_identity)
    finally:
        if held_fd >= 0:
            os.close(held_fd)
        os.close(parent_fd)

    assert rebound
    if kind == "directory":
        assert (target / "sentinel.txt").read_bytes() == b"replacement-directory"
    elif kind == "regular":
        assert target.read_bytes() == b"replacement-regular"
    else:
        assert os.readlink(target) == "replacement-symlink"
