"""Detached, bounded local video transcription for immutable extension bundles.

All persisted state is tied to a verified registry entry and a held run
directory. No collected speech, decoder output, URL, or local path crosses the
native response boundary. The base results.json is never revised.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import secrets
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from xhs_workbench.extension_import import _rename_no_replace
from xhs_workbench.extension_models import VideoProcessingSummary
from xhs_workbench.media import MAX_VIDEO_BYTES
from xhs_workbench.models import CollectionRun
from xhs_workbench.path_security import has_extended_acl_fd
from xhs_workbench.renderer import VideoReport, render_video_report

_PROCESSING_NAME = "processing.json"
_JOURNAL_NAME = ".processing-journal.json"
_LOCK_NAME = ".video-processing.lock"
_STOP_NAME = ".video-stop"
_MAX_STATE = 16 * 1024
_MAX_TEXT = 512 * 1024
_MAX_REPORT = 8 * 1024 * 1024
_ACTIVE = frozenset({"running", "preparing_model"})
_STATUSES = frozenset({"running", "preparing_model", "complete", "skipped_too_long",
                       "skipped_no_audio", "no_speech", "failed", "not_started"})
_REASONS = frozenset({"video_not_saved", "duration_unknown", "audio_unreadable",
                      "dependencies_unavailable", "model_preparation_failed", "processing_timeout",
                      "task_interrupted", "stopped", "worker_start_failed", "transcript_too_large",
                      "report_update_failed", "job_in_progress"})
_RECORD_FIELDS = frozenset({"schema_version", "run_id", "job_id", "note_id", "asset_path",
                            "asset_sha256", "revision", "status", "reason", "report_update_failed",
                            "owned_html_sha256", "transcript_path", "transcript_sha256",
                            "subtitle_path", "subtitle_sha256", "subtitle_status", "duration_ms"})
_HEX = frozenset("0123456789abcdef")


def _require_owned_file(info: os.stat_result, descriptor: int, *, links: tuple[int, ...] = (1,)) -> None:
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_nlink not in links or stat.S_IMODE(info.st_mode) & 0o022
            or has_extended_acl_fd(descriptor)):
        raise ValueError("unsafe private file")


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _valid_digest(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and set(value) <= _HEX


def _read_at(directory_fd: int, name: str, limit: int, *, required: bool = True) -> bytes | None:
    if name != Path(name).name or name in {".", ".."}:
        raise ValueError("unsafe private filename")
    try:
        before = os.lstat(name, dir_fd=directory_fd)
    except FileNotFoundError:
        if not required:
            return None
        raise ValueError("missing private file") from None
    if not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or before.st_size > limit:
        raise ValueError("unsafe private file")
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
    try:
        info = os.fstat(descriptor)
        _require_owned_file(info, descriptor)
        if (info.st_dev, info.st_ino) != (before.st_dev, before.st_ino) or info.st_size > limit:
            raise ValueError("private file binding changed")
        chunks: list[bytes] = []
        remaining = limit + 1
        while remaining:
            block = os.read(descriptor, min(remaining, 64 * 1024))
            if not block:
                break
            remaining -= len(block)
            chunks.append(block)
        if not remaining:
            raise ValueError("private file too large")
        after = os.lstat(name, dir_fd=directory_fd)
        if (info.st_dev, info.st_ino, info.st_size) != (after.st_dev, after.st_ino, after.st_size):
            raise ValueError("private file binding changed")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _write_all(descriptor: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        count = os.write(descriptor, view)
        if count <= 0:
            raise OSError("short write")
        view = view[count:]


def _temporary(directory_fd: int, data: bytes) -> str:
    name = f".video-{secrets.token_hex(20)}.tmp"
    descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=directory_fd)
    try:
        _write_all(descriptor, data)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return name


def _create_at(directory_fd: int, name: str, data: bytes) -> None:
    temporary = _temporary(directory_fd, data)
    try:
        _rename_no_replace(directory_fd, temporary, name)
        os.fsync(directory_fd)
    finally:
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except FileNotFoundError:
            pass


def _replace_at(directory_fd: int, name: str, data: bytes) -> None:
    temporary = _temporary(directory_fd, data)
    try:
        os.replace(temporary, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        os.fsync(directory_fd)
    finally:
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except FileNotFoundError:
            pass


def _json_bytes(value: dict[str, object]) -> bytes:
    data = (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    if len(data) > _MAX_STATE:
        raise ValueError("processing record too large")
    return data


def _validate_record(record: object, *, run_id: str | None = None, job_id: str | None = None) -> dict[str, Any]:
    if not isinstance(record, dict) or set(record) != _RECORD_FIELDS:
        raise ValueError("invalid processing record")
    if record["schema_version"] != 1 or type(record["revision"]) is not int or record["revision"] < 0:
        raise ValueError("invalid processing version")
    for key in ("run_id", "job_id", "note_id"):
        value = record[key]
        if not isinstance(value, str) or not value or len(value) > 128 or not all(c.isalnum() or c in "_-" for c in value):
            raise ValueError("invalid processing identity")
    if (run_id is not None and record["run_id"] != run_id) or (job_id is not None and record["job_id"] != job_id):
        raise ValueError("processing identity changed")
    if record["status"] not in _STATUSES or (record["reason"] is not None and record["reason"] not in _REASONS) or type(record["report_update_failed"]) is not bool:
        raise ValueError("invalid processing status")
    if not _valid_digest(record["owned_html_sha256"]):
        raise ValueError("invalid owned report digest")
    if record["subtitle_status"] not in {"available", "not_exposed", "failed"}:
        raise ValueError("invalid independent subtitle status")
    for path_key, hash_key, allowed in (("asset_path", "asset_sha256", "assets/"),
                                         ("transcript_path", "transcript_sha256", "transcript.txt"),
                                         ("subtitle_path", "subtitle_sha256", "independent-subtitles.srt")):
        path, digest = record[path_key], record[hash_key]
        if (path is None) != (digest is None):
            raise ValueError("processing asset pair is invalid")
        if path is not None:
            if not isinstance(path, str) or not _valid_digest(digest):
                raise ValueError("invalid processing asset")
            if path_key == "asset_path":
                if not path.startswith(allowed) or path.count("/") != 1 or Path(path).name in {".", ".."}:
                    raise ValueError("unsafe video path")
            elif path != allowed:
                raise ValueError("unsafe transcript path")
    if (record["subtitle_status"] == "available") != (record["subtitle_path"] is not None):
        raise ValueError("subtitle status disagrees with saved subtitle")
    if record["status"] == "complete" and record["transcript_path"] != "transcript.txt":
        raise ValueError("completed transcription requires a bound text file")
    duration = record["duration_ms"]
    if duration is not None and (type(duration) is not int or not 1 <= duration <= 86_400_000):
        raise ValueError("invalid video duration")
    return record


def load_processing(directory_fd: int, *, run_id: str | None = None, job_id: str | None = None) -> dict[str, Any] | None:
    raw = _read_at(directory_fd, _PROCESSING_NAME, _MAX_STATE, required=False)
    if raw is None:
        return None
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("invalid processing record") from error
    return _validate_record(value, run_id=run_id, job_id=job_id)


def summary(record: dict[str, Any] | None) -> VideoProcessingSummary:
    if record is None:
        return VideoProcessingSummary(status="none")
    reason = record["reason"]
    if record["report_update_failed"] and reason is None and record["status"] != "complete":
        reason = "report_update_failed"
    return VideoProcessingSummary(status=record["status"], reason=reason,
                                  report_update_failed=True if record["report_update_failed"] else None)


def initial_processing(run_dir: Path, job_id: str, note_id: str, *, duration_hint_ms: int | None = None) -> dict[str, Any]:
    """Bind the exact selected note and declared video to a new sidecar."""
    run_fd = os.open(run_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        return initial_processing_at(run_fd, job_id, note_id, duration_hint_ms=duration_hint_ms)
    finally:
        os.close(run_fd)


def initial_processing_at(run_fd: int, job_id: str, note_id: str, *, duration_hint_ms: int | None = None) -> dict[str, Any]:
    data = _read_at(run_fd, "results.json", _MAX_REPORT)
    assert data is not None
    run = CollectionRun.model_validate_json(data)
    if run.collection_surface != "extension_current" or len(run.notes) != 1 or run.notes[0].note_id != note_id:
        raise ValueError("video note identity changed")
    slot = next((item for item in run.notes[0].media_slots if item.role == "video"), None)
    if slot is None:
        raise ValueError("video was not declared")
    asset = slot.asset if slot.status == "downloaded" else None
    if asset is None:
        known = duration_hint_ms or slot.duration_ms
        status = "skipped_too_long" if known is not None and known > 900_000 else "not_started"
        reason = None if status == "skipped_too_long" else "video_not_saved"
    else:
        status, reason = "running", None
    html_data = _read_at(run_fd, "index.html", _MAX_REPORT)
    assert html_data is not None
    return _validate_record({
        "schema_version": 1, "run_id": run.run_id, "job_id": job_id, "note_id": note_id,
        "asset_path": asset.local_path if asset is not None else None,
        "asset_sha256": asset.sha256 if asset is not None else None,
        "revision": 0, "status": status, "reason": reason, "report_update_failed": False,
        "owned_html_sha256": _digest(html_data), "transcript_path": None,
        "transcript_sha256": None, "subtitle_path": None, "subtitle_sha256": None,
        "subtitle_status": "not_exposed",
        "duration_ms": slot.duration_ms if asset is not None else duration_hint_ms,
    })


def write_initial(directory_fd: int, record: dict[str, Any]) -> None:
    _validate_record(record)
    _create_at(directory_fd, _PROCESSING_NAME, _json_bytes(record))


def open_verified_video(run_fd: int, record: dict[str, Any]) -> int:
    """Return a held, owner-checked media descriptor matching the pinned SHA."""
    _validate_record(record)
    path = record["asset_path"]
    if path is None:
        raise ValueError("video was not saved")
    assets_fd = os.open("assets", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=run_fd)
    try:
        info = os.fstat(assets_fd)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or has_extended_acl_fd(assets_fd):
            raise ValueError("unsafe asset directory")
        before = os.lstat(path[7:], dir_fd=assets_fd)
        if not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or before.st_size > MAX_VIDEO_BYTES:
            raise ValueError("unsafe video asset")
        descriptor = os.open(path[7:], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=assets_fd)
        try:
            actual = os.fstat(descriptor)
            # ExtensionJob intentionally retains the opaque staging hard link
            # after publishing one video under assets/.
            _require_owned_file(actual, descriptor, links=(1, 2))
            if (actual.st_dev, actual.st_ino, actual.st_size) != (before.st_dev, before.st_ino, before.st_size):
                raise ValueError("video binding changed")
            digest = hashlib.sha256()
            while block := os.read(descriptor, 64 * 1024):
                digest.update(block)
            after = os.lstat(path[7:], dir_fd=assets_fd)
            if digest.hexdigest() != record["asset_sha256"] or (actual.st_dev, actual.st_ino, actual.st_size) != (after.st_dev, after.st_ino, after.st_size):
                raise ValueError("video content changed")
            os.lseek(descriptor, 0, os.SEEK_SET)
            return descriptor
        except Exception:
            os.close(descriptor)
            raise
    finally:
        os.close(assets_fd)


def _assert_same_video(run_fd: int, media_fd: int, record: dict[str, Any]) -> None:
    """Rehash and rebind the media immediately before any final publication."""
    verified_fd = open_verified_video(run_fd, record)
    try:
        actual = os.fstat(media_fd)
        verified = os.fstat(verified_fd)
        if (actual.st_dev, actual.st_ino, actual.st_size) != (
            verified.st_dev, verified.st_ino, verified.st_size
        ):
            raise ValueError("audio_unreadable")
    finally:
        os.close(verified_fd)


def _render_from_state(run_fd: int, record: dict[str, Any]) -> bytes:
    raw = _read_at(run_fd, "results.json", _MAX_REPORT)
    assert raw is not None
    run = CollectionRun.model_validate_json(raw)
    if run.run_id != record["run_id"] or not any(note.note_id == record["note_id"] for note in run.notes):
        raise ValueError("processing report identity changed")
    transcript = _read_text(run_fd, record["transcript_path"], record["transcript_sha256"])
    subtitle = _read_text(run_fd, record["subtitle_path"], record["subtitle_sha256"])
    report = VideoReport(note_id=record["note_id"], status=record["status"],
                         reason=record["reason"], duration_ms=record["duration_ms"],
                         transcript=transcript, subtitle=subtitle,
                         subtitle_status=record["subtitle_status"],
                         report_update_failed=record["report_update_failed"])
    html = render_video_report(run, report)
    if len(html) > _MAX_REPORT:
        raise ValueError("report is too large")
    return html


def _read_text(run_fd: int, name: str | None, expected: str | None) -> str | None:
    if name is None:
        return None
    raw = _read_at(run_fd, name, _MAX_TEXT)
    if raw is None or _digest(raw) != expected:
        raise ValueError("text content changed")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("invalid text encoding") from error


def _journal_at(run_fd: int) -> dict[str, Any] | None:
    raw = _read_at(run_fd, _JOURNAL_NAME, _MAX_STATE, required=False)
    if raw is None:
        return None
    try:
        journal = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("invalid report revision journal") from error
    if not isinstance(journal, dict) or set(journal) != {"previous", "desired", "revision", "backup"} or not _valid_digest(journal["previous"]) or not _valid_digest(journal["desired"]) or type(journal["revision"]) is not int or journal["revision"] < 1 or journal["backup"] != f".processing-previous-{journal['revision']}.html":
        raise ValueError("invalid report revision journal")
    return journal


def _clear_journal(run_fd: int, journal: dict[str, Any]) -> None:
    os.unlink(_JOURNAL_NAME, dir_fd=run_fd)
    os.fsync(run_fd)
    try:
        backup = _read_at(run_fd, journal["backup"], _MAX_REPORT, required=False)
        if backup is not None and _digest(backup) == journal["previous"]:
            os.unlink(journal["backup"], dir_fd=run_fd)
            os.fsync(run_fd)
    except (OSError, ValueError):
        # Keep opaque recovery residue if the backup was altered.
        pass


def _cas_html_at(run_fd: int, desired: bytes, previous_digest: str) -> None:
    """Pin the owned old inode through temp creation and check immediately before rename."""
    held = os.open("index.html", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=run_fd)
    temporary: str | None = None
    try:
        before = os.fstat(held)
        _require_owned_file(before, held)

        def unchanged() -> bool:
            os.lseek(held, 0, os.SEEK_SET)
            digest = hashlib.sha256()
            remaining = _MAX_REPORT + 1
            while remaining:
                chunk = os.read(held, min(remaining, 64 * 1024))
                if not chunk:
                    break
                remaining -= len(chunk)
                digest.update(chunk)
            current = os.lstat("index.html", dir_fd=run_fd)
            held_info = os.fstat(held)
            _require_owned_file(held_info, held)
            return (remaining > 0 and digest.hexdigest() == previous_digest
                    and (held_info.st_dev, held_info.st_ino, held_info.st_size)
                    == (before.st_dev, before.st_ino, before.st_size)
                    == (current.st_dev, current.st_ino, current.st_size))

        if not unchanged():
            raise ValueError("report was manually edited")
        temporary = _temporary(run_fd, desired)
        if not unchanged():
            raise ValueError("report was manually edited during revision")
        os.replace(temporary, "index.html", src_dir_fd=run_fd, dst_dir_fd=run_fd)
        temporary = None
        os.fsync(run_fd)
    finally:
        os.close(held)
        if temporary is not None:
            os.unlink(temporary, dir_fd=run_fd)


def _mark_report_failure(run_fd: int, record: dict[str, Any]) -> dict[str, Any]:
    failed = {**record, "report_update_failed": True}
    _replace_at(run_fd, _PROCESSING_NAME, _json_bytes(failed))
    return failed


def reconcile(run_fd: int, record: dict[str, Any], *, worker_live: bool) -> dict[str, Any]:
    """Finish a journaled revision or converge a dead worker to a finite state."""
    _validate_record(record)
    # A live worker owns revisions. An observing status/open connection must
    # never consume its in-flight journal or race its atomic HTML replacement.
    if worker_live:
        return record
    journal = _journal_at(run_fd)
    if journal is not None:
        backup = _read_at(run_fd, journal["backup"], _MAX_REPORT, required=False)
        if backup is not None and _digest(backup) != journal["previous"]:
            record = _mark_report_failure(run_fd, record)
            _clear_journal(run_fd, journal)
            return record
        current = _read_at(run_fd, "index.html", _MAX_REPORT)
        assert current is not None
        actual = _digest(current)
        try:
            if record["revision"] == journal["revision"]:
                desired = _render_from_state(run_fd, record)
                if _digest(desired) != journal["desired"]:
                    raise ValueError("report journal mismatch")
                if actual == journal["previous"]:
                    _cas_html_at(run_fd, desired, journal["previous"])
                elif actual != journal["desired"]:
                    raise ValueError("report was manually edited")
                record = {**record, "owned_html_sha256": journal["desired"]}
                _replace_at(run_fd, _PROCESSING_NAME, _json_bytes(record))
            elif record["revision"] != journal["revision"] - 1 or actual != journal["previous"]:
                raise ValueError("report journal mismatch")
        except (OSError, ValueError):
            record = _mark_report_failure(run_fd, record)
        _clear_journal(run_fd, journal)
    if record["status"] in _ACTIVE and not worker_live:
        record = advance_processing(run_fd, record, status="failed", reason="task_interrupted")
    return record


def advance_processing(
    run_fd: int, record: dict[str, Any], *, status: str, reason: str | None = None,
    duration_ms: int | None = None, transcript: str | None = None,
    subtitle: str | None = None,
) -> dict[str, Any]:
    """Durably publish one finite state and an owned same-index HTML revision."""
    _validate_record(record)
    current = load_processing(run_fd, run_id=record["run_id"], job_id=record["job_id"])
    if current is None or current["revision"] != record["revision"]:
        raise ValueError("processing revision changed")
    if status not in _STATUSES or (reason is not None and reason not in _REASONS):
        raise ValueError("invalid video outcome")
    next_record = {**record, "revision": record["revision"] + 1, "status": status,
                   "reason": reason, "duration_ms": duration_ms or record["duration_ms"]}
    for text, name, path_key, hash_key in ((transcript, "transcript.txt", "transcript_path", "transcript_sha256"),
                                          (subtitle, "independent-subtitles.srt", "subtitle_path", "subtitle_sha256")):
        if text is None:
            continue
        data = text.encode("utf-8")
        if not data or len(data) > _MAX_TEXT or "\x00" in text:
            raise ValueError("transcript_too_large")
        _create_at(run_fd, name, data)
        next_record[path_key], next_record[hash_key] = name, _digest(data)
        if name == "independent-subtitles.srt":
            next_record["subtitle_status"] = "available"
    _validate_record(next_record)
    desired = _render_from_state(run_fd, next_record)
    previous = record["owned_html_sha256"]
    observed = _read_at(run_fd, "index.html", _MAX_REPORT)
    if observed is None or _digest(observed) != previous:
        return _mark_report_failure(run_fd, next_record)
    journal = {"previous": previous, "desired": _digest(desired),
               "revision": next_record["revision"],
               "backup": f".processing-previous-{next_record['revision']}.html"}
    _create_at(run_fd, journal["backup"], observed)
    _create_at(run_fd, _JOURNAL_NAME, _json_bytes(journal))
    try:
        _replace_at(run_fd, _PROCESSING_NAME, _json_bytes(next_record))
        _cas_html_at(run_fd, desired, previous)
        next_record["owned_html_sha256"] = _digest(desired)
        _replace_at(run_fd, _PROCESSING_NAME, _json_bytes(next_record))
    except (OSError, ValueError):
        next_record = _mark_report_failure(run_fd, next_record)
    finally:
        _clear_journal(run_fd, journal)
    return next_record


def reserve_video_lock(config: Any, job_id: str) -> int:
    """Reserve the one cross-connection current-note slot before making a run."""
    from xhs_workbench import native_host

    directory_fd = native_host._open_verified_config_directory(config)
    try:
        descriptor = os.open(_LOCK_NAME, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                             0o600, dir_fd=directory_fd)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
                raise ValueError("invalid video lock")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise ValueError("video job already in progress") from error
            os.ftruncate(descriptor, 0)
            _write_all(descriptor, job_id.encode("ascii"))
            os.fsync(descriptor)
            return descriptor
        except Exception:
            os.close(descriptor)
            raise
    finally:
        os.close(directory_fd)


def worker_live(config: Any, job_id: str) -> bool:
    from xhs_workbench import native_host

    directory_fd = native_host._open_verified_config_directory(config)
    try:
        raw = _read_at(directory_fd, _LOCK_NAME, 128, required=False)
        if raw != job_id.encode("ascii"):
            return False
        fd = os.open(_LOCK_NAME, os.O_RDWR | os.O_NOFOLLOW, dir_fd=directory_fd)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            return False
        finally:
            os.close(fd)
    finally:
        os.close(directory_fd)


def _verify_registry_and_run(config: Any, job_id: str, inherited_run_fd: int | None = None) -> int:
    from xhs_workbench import native_host

    verified = native_host._open_verified_report(config, job_id)
    try:
        if inherited_run_fd is not None:
            info = os.fstat(inherited_run_fd)
            if not stat.S_ISDIR(info.st_mode) or (info.st_dev, info.st_ino) != verified.run_identity:
                raise ValueError("worker run identity changed")
        return os.dup(verified.run_fd)
    finally:
        verified.close()


def _validate_base_binding(run_fd: int, record: dict[str, Any]) -> None:
    """A sidecar must describe the precise immutable manifest video, not a forged path."""
    raw = _read_at(run_fd, "results.json", _MAX_REPORT)
    assert raw is not None
    run = CollectionRun.model_validate_json(raw)
    if run.run_id != record["run_id"] or run.collection_surface != "extension_current" or len(run.notes) != 1 or run.notes[0].note_id != record["note_id"]:
        raise ValueError("processing identity changed")
    slot = next((item for item in run.notes[0].media_slots if item.role == "video"), None)
    if slot is None:
        raise ValueError("video declaration changed")
    asset = slot.asset if slot.status == "downloaded" else None
    if (record["asset_path"], record["asset_sha256"]) != (
        asset.local_path if asset is not None else None,
        asset.sha256 if asset is not None else None,
    ):
        raise ValueError("video binding changed")


def processing_for_job(config: Any, job_id: str, *, stop: bool = False) -> VideoProcessingSummary:
    """Resolve a registry entry before reading, stopping, or reconciling state."""
    run_fd = _verify_registry_and_run(config, job_id)
    try:
        record = load_processing(run_fd, job_id=job_id)
        if record is None:
            return summary(None)
        _validate_base_binding(run_fd, record)
        if stop and record["status"] in _ACTIVE:
            try:
                _create_at(run_fd, _STOP_NAME, job_id.encode("ascii"))
            except FileExistsError:
                existing = _read_at(run_fd, _STOP_NAME, 128)
                if existing != job_id.encode("ascii"):
                    raise ValueError("stop identity changed") from None
        record = reconcile(run_fd, record, worker_live=worker_live(config, job_id))
        _read_text(run_fd, record["transcript_path"], record["transcript_sha256"])
        _read_text(run_fd, record["subtitle_path"], record["subtitle_sha256"])
        return summary(record)
    finally:
        os.close(run_fd)


def start_processing(config: Any, job_id: str, note_id: str, *, lock_fd: int,
                     duration_hint_ms: int | None = None, subtitle: str | None = None,
                     subtitle_status: str | None = None) -> VideoProcessingSummary:
    """Publish the base-linked state, then launch a detached owner of the lock."""
    from xhs_workbench import native_host

    verified = native_host._open_verified_report(config, job_id)
    try:
        record = initial_processing_at(verified.run_fd, job_id, note_id,
                                       duration_hint_ms=duration_hint_ms)
        if subtitle is not None:
            data = subtitle.encode("utf-8")
            _create_at(verified.run_fd, "independent-subtitles.srt", data)
            record["subtitle_path"] = "independent-subtitles.srt"
            record["subtitle_sha256"] = _digest(data)
            record["subtitle_status"] = "available"
        elif subtitle_status == "failed":
            record["subtitle_status"] = "failed"
        write_initial(verified.run_fd, record)
        record = advance_processing(verified.run_fd, record, status=record["status"],
                                    reason=record["reason"])
        if record["status"] not in _ACTIVE:
            return summary(record)
        try:
            media_fd = open_verified_video(verified.run_fd, record)
            try:
                subprocess.Popen(
                    [sys.executable, "-m", "xhs_workbench.video_processing", "worker",
                     str(config.config_path), job_id, str(lock_fd), str(verified.run_fd), str(media_fd)],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    start_new_session=True, close_fds=True,
                    pass_fds=(lock_fd, verified.run_fd, media_fd),
                )
            finally:
                os.close(media_fd)
        except (OSError, ValueError):
            record = advance_processing(verified.run_fd, record, status="failed", reason="worker_start_failed")
        return summary(record)
    finally:
        verified.close()


def start_failure(config: Any, job_id: str, note_id: str, *, duration_hint_ms: int | None = None) -> VideoProcessingSummary:
    """Record a finite launch failure after the immutable base was published."""
    run_fd = _verify_registry_and_run(config, job_id)
    try:
        record = load_processing(run_fd, job_id=job_id)
        if record is None:
            record = initial_processing_at(run_fd, job_id, note_id,
                                           duration_hint_ms=duration_hint_ms)
            write_initial(run_fd, record)
        if record["status"] in _ACTIVE:
            record = advance_processing(run_fd, record, status="failed", reason="worker_start_failed")
        return summary(record)
    finally:
        os.close(run_fd)


class _Stopped(Exception):
    pass


class _TimedOut(Exception):
    pass


def _check_stop(run_fd: int, job_id: str) -> None:
    marker = _read_at(run_fd, _STOP_NAME, 128, required=False)
    if marker is not None:
        if marker != job_id.encode("ascii"):
            raise ValueError("stop identity changed")
        raise _Stopped


def _asr_command(operation: str, path: str, model_dir: Path) -> list[str]:
    args = [sys.executable, "-m", "xhs_workbench.video_asr", operation, path]
    if operation == "transcribe":
        args += ["--model", str(model_dir)]
    return args


def _invoke_asr(operation: str, media_fd: int, run_fd: int, job_id: str,
                model_dir: Path, deadline: float, *, lock_fd: int) -> dict[str, Any]:
    _check_stop(run_fd, job_id)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _TimedOut
    path = str(model_dir) if operation == "prepare" else f"/dev/fd/{media_fd}"
    owner_read_fd, owner_write_fd = os.pipe()
    process: subprocess.Popen[bytes] | None = None
    try:
        # The guard inherits the held flock and the pipe read end. If SIGKILL
        # takes out this worker, EOF still makes the guard reap ASR before the
        # lock can be released. No stored PID is ever used to signal a process.
        inherited_media_fd = media_fd if operation != "prepare" else -1
        inherited_lock_fd = lock_fd
        args = [sys.executable, "-m", "xhs_workbench.video_asr_guard", "run",
                str(owner_read_fd), str(max(1, min(1_800_000, int(remaining * 1000)))),
                str(inherited_media_fd), str(inherited_lock_fd),
                *_asr_command(operation, path, model_dir)]
        pass_fds = tuple(fd for fd in (owner_read_fd, inherited_media_fd, inherited_lock_fd)
                         if fd >= 0)
        process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, close_fds=True,
                                   pass_fds=pass_fds, start_new_session=True)
        os.close(owner_read_fd)
        owner_read_fd = -1
        while True:
            _check_stop(run_fd, job_id)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _TimedOut
            try:
                # communicate drains PIPE while the child is running; poll()
                # alone deadlocks as soon as a genuine transcript fills it.
                output, _ = process.communicate(timeout=min(.25, remaining))
                break
            except subprocess.TimeoutExpired:
                continue
    except BaseException:
        # Closing the owner pipe requests guarded child cleanup. Wait for that
        # cleanup before returning a stopped/timed-out result while this worker
        # is still alive; SIGKILL of the worker follows the same EOF path.
        os.close(owner_write_fd)
        owner_write_fd = -1
        if process is not None and process.poll() is None:
            try:
                process.communicate(timeout=7)
            except subprocess.TimeoutExpired:
                # The unreaped Popen child still owns its PID. Kill only that
                # guard's process group if its own bounded cleanup failed.
                if process.poll() is None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.communicate(timeout=3)
        raise
    finally:
        if owner_read_fd >= 0:
            os.close(owner_read_fd)
        if owner_write_fd >= 0:
            os.close(owner_write_fd)
    if len(output) > 1024 * 1024:
        raise ValueError("transcript_too_large")
    try:
        result = json.loads(output.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("audio_unreadable") from error
    if not isinstance(result, dict):
        raise TypeError("audio_unreadable")
    if process.returncode != 0:
        reason = result.get("reason")
        raise ValueError(reason if reason in _REASONS else "audio_unreadable")
    return result


def _check_deadline(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise _TimedOut


def worker(config_path: Path, job_id: str, lock_fd: int, run_fd: int, media_fd: int) -> None:
    """One detached owner; child ASR subprocesses never outlive this worker."""
    from xhs_workbench import native_host, video_asr

    try:
        config = native_host._load_installer_config(config_path)
        verified_run_fd = _verify_registry_and_run(config, job_id, inherited_run_fd=run_fd)
        try:
            record = load_processing(verified_run_fd, job_id=job_id)
            if record is None or record["status"] not in _ACTIVE or not worker_live(config, job_id):
                return
            _validate_base_binding(verified_run_fd, record)
            _assert_same_video(verified_run_fd, media_fd, record)
            deadline = time.monotonic() + 600
            model_dir = config.config_directory / "video-model-small"
            _check_stop(verified_run_fd, job_id)
            probe = _invoke_asr("probe", media_fd, verified_run_fd, job_id, model_dir, deadline,
                                lock_fd=lock_fd)
            _check_deadline(deadline)
            duration = probe.get("duration_ms")
            if type(duration) is not int or duration < 1 or duration > 86_400_000:
                raise ValueError("duration_unknown")
            if duration > 900_000:
                _assert_same_video(verified_run_fd, media_fd, record)
                final = advance_processing(verified_run_fd, record, status="skipped_too_long", duration_ms=duration)
                _final_deadline(verified_run_fd, final, deadline)
                return
            if probe.get("audio_present") is not True:
                _assert_same_video(verified_run_fd, media_fd, record)
                final = advance_processing(verified_run_fd, record, status="skipped_no_audio", duration_ms=duration)
                _final_deadline(verified_run_fd, final, deadline)
                return
            if not video_asr.model_ready(model_dir):
                _check_deadline(deadline)
                record = advance_processing(verified_run_fd, record, status="preparing_model", duration_ms=duration)
                prepare_start = time.monotonic()
                try:
                    _invoke_asr("prepare", media_fd, verified_run_fd, job_id, model_dir,
                                prepare_start + 1800, lock_fd=lock_fd)
                except _TimedOut as error:
                    raise ValueError("model_preparation_failed") from error
                deadline += time.monotonic() - prepare_start
            _check_deadline(deadline)
            _check_stop(verified_run_fd, job_id)
            record = advance_processing(verified_run_fd, record, status="running", duration_ms=duration)
            _check_deadline(deadline)
            result = _invoke_asr("transcribe", media_fd, verified_run_fd, job_id, model_dir,
                                 deadline, lock_fd=lock_fd)
            _check_deadline(deadline)
            _check_stop(verified_run_fd, job_id)
            _assert_same_video(verified_run_fd, media_fd, record)
            status = result.get("status")
            if status == "no_speech":
                final = advance_processing(verified_run_fd, record, status="no_speech")
                _final_deadline(verified_run_fd, final, deadline)
            elif status == "complete":
                transcript = result.get("text")
                if not isinstance(transcript, str) or not transcript.strip() or len(transcript.encode("utf-8")) > _MAX_TEXT:
                    raise ValueError("transcript_too_large")
                final = advance_processing(verified_run_fd, record, status="complete", transcript=transcript)
                _final_deadline(verified_run_fd, final, deadline)
            else:
                raise ValueError("audio_unreadable")
        except Exception as error:  # noqa: BLE001 - finite publication; no raw diagnostics
            current = load_processing(verified_run_fd, job_id=job_id)
            if current is not None and current["status"] in _ACTIVE:
                reason = ("stopped" if isinstance(error, _Stopped) else
                          "processing_timeout" if isinstance(error, _TimedOut) else
                          str(error) if isinstance(error, ValueError) and str(error) in _REASONS else
                          "audio_unreadable")
                try:
                    advance_processing(verified_run_fd, current, status="failed", reason=reason)
                except (OSError, ValueError):
                    pass
        finally:
            os.close(verified_run_fd)
    except (OSError, ValueError):
        # The next verified status/open call reconciles a dead worker. Never
        # reveal an untrusted path or decoder diagnostic on stderr.
        pass
    finally:
        for descriptor in (media_fd, run_fd, lock_fd):
            try:
                os.close(descriptor)
            except OSError:
                pass


def _final_deadline(run_fd: int, final: dict[str, Any], deadline: float) -> None:
    if time.monotonic() >= deadline:
        advance_processing(run_fd, final, status="failed", reason="processing_timeout")


def main() -> int:
    if len(sys.argv) != 7 or sys.argv[1] != "worker":
        return 1
    try:
        worker(Path(sys.argv[2]), sys.argv[3], int(sys.argv[4]), int(sys.argv[5]), int(sys.argv[6]))
        return 0
    except (ValueError, OSError):
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
