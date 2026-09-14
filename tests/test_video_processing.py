"""Worker publication keeps the immutable base report and verified media intact."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

from xhs_workbench import video_asr, video_processing
from xhs_workbench.models import CollectionRun, NoteMediaSlot, NoteMetrics, NoteRecord, RunStatus
from xhs_workbench.renderer import VideoReport, render_video_report, write_result_bundle


def _bundle(tmp_path: Path, *, saved: bool = False) -> tuple[Path, str]:
    root = tmp_path / "reports"
    root.mkdir()
    run_id = "run_123"
    run_dir = root / run_id
    (run_dir / "assets").mkdir(parents=True)
    slots = [NoteMediaSlot(note_id="note_123", role="video", position=1,
                           status="missing", missing_reason="source_not_exposed")]
    if saved:
        from xhs_workbench.models import LocalAsset
        def box(kind: bytes, payload: bytes = b"") -> bytes:
            return (8 + len(payload)).to_bytes(4, "big") + kind + payload

        MP4 = box(b"ftyp", b"isom\x00\x00\x00\x00") + box(b"moov") + box(b"mdat", b"frame")

        asset = run_dir / "assets" / "note_123-video.mp4"
        asset.write_bytes(MP4)
        slots = [NoteMediaSlot(note_id="note_123", role="video", position=1,
                               status="downloaded", asset=LocalAsset(
                                   local_path="assets/note_123-video.mp4", mime_type="video/mp4",
                                   size_bytes=len(MP4), sha256=hashlib.sha256(MP4).hexdigest()))]
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    run = CollectionRun(run_id=run_id, mode="extension", input_summary="extension_current",
                        requested_count=1, actual_count=1, started_at=now, finished_at=now,
                        status=RunStatus.PARTIAL, collection_surface="extension_current",
                        notes=[NoteRecord(note_id="note_123",
                                          canonical_url="https://www.xiaohongshu.com/explore/note_123",
                                          note_type="video", source_position=1, metrics=NoteMetrics(),
                                          media_manifest_version=2, media_slots=slots,
                                          media_discovered_count=0)])
    write_result_bundle(run, run_dir)
    return run_dir, run_id


def _record(run_dir: Path, *, status: str = "running", reason: str | None = None) -> dict[str, object]:
    base = (run_dir / "index.html").read_bytes()
    asset = next((run_dir / "assets").glob("*.mp4"), None)
    return {
        "schema_version": 1, "run_id": "run_123", "job_id": "job_123", "note_id": "note_123",
        "asset_path": "assets/note_123-video.mp4" if asset else None,
        "asset_sha256": hashlib.sha256(asset.read_bytes()).hexdigest() if asset else None,
        "revision": 0, "status": status, "reason": reason, "report_update_failed": False,
        "owned_html_sha256": hashlib.sha256(base).hexdigest(), "transcript_path": None,
        "transcript_sha256": None, "subtitle_path": None, "subtitle_sha256": None,
        "subtitle_status": "not_exposed",
        "duration_ms": None,
    }


def _registered_worker(tmp_path: Path, run_dir: Path) -> tuple[Path, int, int, int]:
    config = tmp_path / ".config" / "xhs-workbench" / "extension.json"
    config.parent.mkdir(parents=True, mode=0o700)
    config.write_text(json.dumps({"allowed_origin": "chrome-extension://abcdefghijklmnopabcdefghijklmnop/",
                                  "output_root": str(run_dir.parent)}))
    os.chmod(config, 0o600)
    registry = {
        "job_id": "job_123", "run_id": "run_123",
        "run_identity": {"device": run_dir.stat().st_dev, "inode": run_dir.stat().st_ino},
        "output_root_identity": {"device": run_dir.parent.stat().st_dev,
                                 "inode": run_dir.parent.stat().st_ino},
        "results_sha256": hashlib.sha256((run_dir / "results.json").read_bytes()).hexdigest(),
    }
    index = config.parent / hashlib.sha256(b"job_123").hexdigest()
    index.write_text(json.dumps(registry))
    os.chmod(index, 0o600)
    run_fd = os.open(run_dir, os.O_RDONLY | os.O_DIRECTORY)
    video_processing.write_initial(run_fd, _record(run_dir))
    from xhs_workbench import native_host

    loaded = native_host._load_installer_config(config)
    lock_fd = video_processing.reserve_video_lock(loaded, "job_123")
    media_fd = video_processing.open_verified_video(run_fd, _record(run_dir))
    return config, lock_fd, run_fd, media_fd


def test_publish_escapes_transcript_and_keeps_original_results_hash(tmp_path: Path) -> None:
    run_dir, _ = _bundle(tmp_path, saved=True)
    before = hashlib.sha256((run_dir / "results.json").read_bytes()).hexdigest()
    fd = os.open(run_dir, os.O_RDONLY | os.O_DIRECTORY)
    try:
        record = _record(run_dir)
        video_processing.write_initial(fd, record)
        final = video_processing.advance_processing(fd, record, status="complete", transcript="你好 <script>alert(1)</script>")
    finally:
        os.close(fd)
    assert hashlib.sha256((run_dir / "results.json").read_bytes()).hexdigest() == before
    assert (run_dir / "transcript.txt").read_text() == "你好 <script>alert(1)</script>"
    html = (run_dir / "index.html").read_text()
    assert "你好 &lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<script>alert(1)</script>" not in html
    assert final["status"] == "complete"
    assert final["report_update_failed"] is False


def test_manual_html_edit_fails_closed_but_preserves_transcript(tmp_path: Path) -> None:
    run_dir, _ = _bundle(tmp_path, saved=True)
    fd = os.open(run_dir, os.O_RDONLY | os.O_DIRECTORY)
    try:
        record = _record(run_dir)
        video_processing.write_initial(fd, record)
        (run_dir / "index.html").write_text("manual edit")
        final = video_processing.advance_processing(fd, record, status="complete", transcript="spoken")
    finally:
        os.close(fd)
    assert (run_dir / "index.html").read_text() == "manual edit"
    assert (run_dir / "transcript.txt").read_text() == "spoken"
    assert final["report_update_failed"] is True
    assert video_processing.summary(final).report_update_failed is True


def test_missing_video_uses_fifteen_minute_priority_boundary(tmp_path: Path) -> None:
    run_dir, _ = _bundle(tmp_path)
    inclusive = video_processing.initial_processing(
        run_dir, "job_123", "note_123", duration_hint_ms=900000
    )
    assert inclusive["status"] == "not_started"
    assert inclusive["reason"] == "video_not_saved"
    over_limit = video_processing.initial_processing(
        run_dir, "job_123", "note_123", duration_hint_ms=900001
    )
    assert over_limit["status"] == "skipped_too_long"
    assert over_limit["reason"] is None
    unknown = video_processing.initial_processing(run_dir, "job_123", "note_123")
    assert unknown["status"] == "not_started"
    assert unknown["reason"] == "video_not_saved"


def test_skipped_video_report_displays_fifteen_minute_limit(tmp_path: Path) -> None:
    run_dir, _ = _bundle(tmp_path)
    run = CollectionRun.model_validate_json((run_dir / "results.json").read_bytes())
    report = render_video_report(run, VideoReport(note_id="note_123", status="skipped_too_long"))
    assert "15 分钟" in report.decode("utf-8")


def test_asset_capability_refuses_symlink_and_sha_mismatch(tmp_path: Path) -> None:
    run_dir, _ = _bundle(tmp_path, saved=True)
    record = _record(run_dir)
    fd = os.open(run_dir, os.O_RDONLY | os.O_DIRECTORY)
    try:
        asset_fd = video_processing.open_verified_video(fd, record)
        os.close(asset_fd)
        (run_dir / "assets" / "note_123-video.mp4").write_bytes(b"tampered")
        with pytest.raises(ValueError):
            video_processing.open_verified_video(fd, record)
        asset = run_dir / "assets" / "note_123-video.mp4"
        asset.unlink()
        asset.symlink_to(run_dir / "results.json")
        with pytest.raises(ValueError):
            video_processing.open_verified_video(fd, record)
    finally:
        os.close(fd)


def test_stale_worker_becomes_finite_failure_without_autoresume(tmp_path: Path) -> None:
    run_dir, _ = _bundle(tmp_path, saved=True)
    record = _record(run_dir)
    fd = os.open(run_dir, os.O_RDONLY | os.O_DIRECTORY)
    try:
        video_processing.write_initial(fd, record)
        updated = video_processing.reconcile(fd, record, worker_live=False)
    finally:
        os.close(fd)
    assert updated["status"] == "failed"
    assert updated["reason"] == "task_interrupted"
    assert "任务中断" in (run_dir / "index.html").read_text()


def test_independent_subtitle_remains_separate_from_audio_transcript(tmp_path: Path) -> None:
    run_dir, _ = _bundle(tmp_path, saved=True)
    fd = os.open(run_dir, os.O_RDONLY | os.O_DIRECTORY)
    try:
        record = _record(run_dir)
        video_processing.write_initial(fd, record)
        updated = video_processing.advance_processing(
            fd, record, status="complete", transcript="口播 <内容>",
            subtitle="1\n00:00:00,000 --> 00:00:01,000\n画面 <字幕>\n",
        )
    finally:
        os.close(fd)
    html = (run_dir / "index.html").read_text()
    assert "音频转录（自动识别）" in html
    assert "独立字幕（页面提供）" in html
    assert "口播 &lt;内容&gt;" in html
    assert "画面 &lt;字幕&gt;" in html
    assert "00:00:00,000 --> 00:00:01,000" not in html
    assert (run_dir / "independent-subtitles.srt").read_text().startswith("1\n00:00:00,000")
    assert updated["transcript_sha256"] != updated["subtitle_sha256"]


def test_report_write_failure_retains_base_html_and_generated_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, _ = _bundle(tmp_path, saved=True)
    original = (run_dir / "index.html").read_bytes()
    fd = os.open(run_dir, os.O_RDONLY | os.O_DIRECTORY)
    def fail_index(_directory_fd: int, _content: bytes, _previous: str) -> None:
        raise OSError("injected disk failure")

    try:
        record = _record(run_dir)
        video_processing.write_initial(fd, record)
        monkeypatch.setattr(video_processing, "_cas_html_at", fail_index)
        final = video_processing.advance_processing(fd, record, status="complete", transcript="saved")
    finally:
        os.close(fd)
    assert (run_dir / "index.html").read_bytes() == original
    assert (run_dir / "transcript.txt").read_text() == "saved"
    assert final["report_update_failed"] is True


def test_reconcile_completes_a_crashed_owned_revision(tmp_path: Path) -> None:
    run_dir, _ = _bundle(tmp_path, saved=True)
    original = (run_dir / "index.html").read_bytes()
    fd = os.open(run_dir, os.O_RDONLY | os.O_DIRECTORY)
    try:
        record = _record(run_dir)
        video_processing.write_initial(fd, record)
        final = video_processing.advance_processing(fd, record, status="complete", transcript="recovered")
        desired = (run_dir / "index.html").read_bytes()
        pending = {**final, "owned_html_sha256": hashlib.sha256(original).hexdigest()}
        video_processing._replace_at(fd, "processing.json", video_processing._json_bytes(pending))
        video_processing._replace_at(fd, "index.html", original)
        backup_name = f".processing-previous-{pending['revision']}.html"
        video_processing._create_at(fd, backup_name, original)
        video_processing._create_at(fd, ".processing-journal.json", video_processing._json_bytes({
            "previous": hashlib.sha256(original).hexdigest(),
            "desired": hashlib.sha256(desired).hexdigest(), "revision": pending["revision"],
            "backup": backup_name,
        }))
        reconciled = video_processing.reconcile(fd, pending, worker_live=False)
    finally:
        os.close(fd)
    assert (run_dir / "index.html").read_bytes() == desired
    assert reconciled["status"] == "complete"
    assert not (run_dir / ".processing-journal.json").exists()


def test_large_child_stdout_is_drained_and_checked_without_pipe_deadlock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, _ = _bundle(tmp_path)
    fd = os.open(run_dir, os.O_RDONLY | os.O_DIRECTORY)
    monkeypatch.setattr(video_processing, "_asr_command", lambda *_args: [
        sys.executable, "-c",
        "import json;print(json.dumps({'status':'complete','text':'文'*80000}))",
    ])
    try:
        result = video_processing._invoke_asr("transcribe", fd, fd, "job_123",
                                              tmp_path / "model", time.monotonic() + 3,
                                              lock_fd=fd)
    finally:
        os.close(fd)
    assert len(result["text"]) == 80_000


def test_guard_stops_child_when_stdout_exceeds_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, _ = _bundle(tmp_path)
    fd = os.open(run_dir, os.O_RDONLY | os.O_DIRECTORY)
    pid_file = tmp_path / "child.pid"
    monkeypatch.setattr(video_processing, "_asr_command", lambda *_args: [
        sys.executable, "-c",
        ("import os,sys,time;from pathlib import Path;"
         "Path(sys.argv[1]).write_text(str(os.getpid()));"
         "sys.stdout.write('x'*2000000);sys.stdout.flush();time.sleep(30)"), str(pid_file),
    ])
    try:
        with pytest.raises(ValueError, match="audio_unreadable"):
            video_processing._invoke_asr("transcribe", fd, fd, "job_123",
                                         tmp_path / "model", time.monotonic() + 3,
                                         lock_fd=fd)
    finally:
        os.close(fd)
    assert pid_file.exists()
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_file.read_text()), 0)


@pytest.mark.parametrize("stop", [False, True])
def test_child_deadline_or_stop_terminates_only_owned_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stop: bool,
) -> None:
    run_dir, _ = _bundle(tmp_path)
    fd = os.open(run_dir, os.O_RDONLY | os.O_DIRECTORY)
    pid_file = tmp_path / "child.pid"
    monkeypatch.setattr(video_processing, "_asr_command", lambda *_args: [
        sys.executable, "-c",
        ("import os,sys,time;from pathlib import Path;"
         "Path(sys.argv[1]).write_text(str(os.getpid()));time.sleep(30)"), str(pid_file),
    ])
    timer = threading.Timer(.1, lambda: (run_dir / ".video-stop").write_bytes(b"job_123"))
    try:
        if stop:
            timer.start()
        with pytest.raises(video_processing._Stopped if stop else video_processing._TimedOut):
            video_processing._invoke_asr("transcribe", fd, fd, "job_123", tmp_path / "model",
                                          time.monotonic() + .6, lock_fd=fd)
    finally:
        if stop:
            timer.join(timeout=1)
        os.close(fd)
    assert pid_file.exists()
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_file.read_text()), 0)


@pytest.mark.parametrize(
    ("duration", "audio", "speech", "expected"),
    [(900000, True, "你好", "complete"), (900001, True, "你好", "skipped_too_long"),
     (900000, False, "你好", "skipped_no_audio"), (900000, True, "", "no_speech")],
)
def test_worker_probe_boundaries_and_audio_statuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, duration: int, audio: bool,
    speech: str, expected: str,
) -> None:
    run_dir, _ = _bundle(tmp_path, saved=True)
    config, lock_fd, run_fd, media_fd = _registered_worker(tmp_path, run_dir)
    monkeypatch.setattr(video_asr, "model_ready", lambda _path: True)

    def engine(operation: str, *_args: object, **kwargs: object) -> dict[str, object]:
        assert kwargs["lock_fd"] == lock_fd
        if operation == "probe":
            return {"duration_ms": duration, "audio_present": audio}
        if operation == "transcribe":
            return {"status": "complete" if speech else "no_speech", "text": speech}
        raise AssertionError("model preparation is unnecessary")

    monkeypatch.setattr(video_processing, "_invoke_asr", engine)
    video_processing.worker(config, "job_123", lock_fd, run_fd, media_fd)
    final = json.loads((run_dir / "processing.json").read_text())
    assert final["status"] == expected
    assert final["duration_ms"] == duration
    assert (run_dir / "transcript.txt").exists() is (expected == "complete")


def test_worker_rehash_catches_video_mutation_during_transcription(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, _ = _bundle(tmp_path, saved=True)
    config, lock_fd, run_fd, media_fd = _registered_worker(tmp_path, run_dir)
    monkeypatch.setattr(video_asr, "model_ready", lambda _path: True)

    def engine(operation: str, *_args: object, **_kwargs: object) -> dict[str, object]:
        if operation == "probe":
            return {"duration_ms": 300000, "audio_present": True}
        (run_dir / "assets" / "note_123-video.mp4").write_bytes(b"mutated")
        return {"status": "complete", "text": "should never appear"}

    monkeypatch.setattr(video_processing, "_invoke_asr", engine)
    video_processing.worker(config, "job_123", lock_fd, run_fd, media_fd)
    final = json.loads((run_dir / "processing.json").read_text())
    assert final["status"] == "failed"
    assert final["reason"] == "audio_unreadable"
    assert not (run_dir / "transcript.txt").exists()


def test_worker_passes_held_lock_to_probe_prepare_and_transcribe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, _ = _bundle(tmp_path, saved=True)
    config, lock_fd, run_fd, media_fd = _registered_worker(tmp_path, run_dir)
    monkeypatch.setattr(video_asr, "model_ready", lambda _path: False)
    calls: list[str] = []

    def engine(operation: str, *_args: object, **kwargs: object) -> dict[str, object]:
        assert kwargs["lock_fd"] == lock_fd
        calls.append(operation)
        if operation == "probe":
            return {"duration_ms": 1000, "audio_present": True}
        if operation == "transcribe":
            return {"status": "complete", "text": "recognized"}
        return {}

    monkeypatch.setattr(video_processing, "_invoke_asr", engine)
    video_processing.worker(config, "job_123", lock_fd, run_fd, media_fd)
    assert calls == ["probe", "prepare", "transcribe"]
    assert json.loads((run_dir / "processing.json").read_text())["status"] == "complete"


def test_complete_processing_record_requires_bound_transcript(tmp_path: Path) -> None:
    run_dir, _ = _bundle(tmp_path, saved=True)
    record = _record(run_dir, status="complete")
    fd = os.open(run_dir, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(ValueError):
            video_processing.write_initial(fd, record)
    finally:
        os.close(fd)


@pytest.mark.parametrize("tamper", ["mode", "hardlink", "acl"])
def test_processing_state_rejects_writable_hardlinked_or_acled_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tamper: str,
) -> None:
    run_dir, _ = _bundle(tmp_path, saved=True)
    fd = os.open(run_dir, os.O_RDONLY | os.O_DIRECTORY)
    try:
        video_processing.write_initial(fd, _record(run_dir))
        path = run_dir / "processing.json"
        if tamper == "mode":
            os.chmod(path, 0o666)
        elif tamper == "hardlink":
            os.link(path, run_dir / "processing-hardlink")
        else:
            monkeypatch.setattr(video_processing, "has_extended_acl_fd", lambda _fd: True, raising=False)
        with pytest.raises(ValueError):
            video_processing.load_processing(fd)
    finally:
        os.close(fd)


def test_manual_html_edit_during_temporary_revision_write_is_never_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, _ = _bundle(tmp_path, saved=True)
    fd = os.open(run_dir, os.O_RDONLY | os.O_DIRECTORY)
    original_temporary = video_processing._temporary

    def edit_during_temp(directory_fd: int, content: bytes) -> str:
        name = original_temporary(directory_fd, content)
        if content.startswith(b"<!doctype html>") and b"CAS_TEST" in content:
            (run_dir / "index.html").write_text("manual edit at temp write point")
        return name

    try:
        record = _record(run_dir)
        video_processing.write_initial(fd, record)
        monkeypatch.setattr(video_processing, "_temporary", edit_during_temp)
        result = video_processing.advance_processing(fd, record, status="complete", transcript="CAS_TEST")
    finally:
        os.close(fd)
    assert result["report_update_failed"] is True
    assert (run_dir / "index.html").read_text() == "manual edit at temp write point"
    assert (run_dir / "transcript.txt").read_text() == "CAS_TEST"


def test_verified_status_never_claims_complete_for_a_tampered_text_file(tmp_path: Path) -> None:
    run_dir, _ = _bundle(tmp_path, saved=True)
    config, lock_fd, run_fd, media_fd = _registered_worker(tmp_path, run_dir)
    try:
        video_processing.advance_processing(run_fd, _record(run_dir), status="complete",
                                            transcript="original recognized text")
    finally:
        os.close(media_fd)
        os.close(run_fd)
        os.close(lock_fd)
    (run_dir / "transcript.txt").write_text("manually replaced text")
    from xhs_workbench import native_host

    loaded = native_host._load_installer_config(config)
    with pytest.raises(ValueError):
        video_processing.processing_for_job(loaded, "job_123")
