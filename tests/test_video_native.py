"""Native video lifecycle across independent connections and immutable registries."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import time
from pathlib import Path

from test_native_host import (
    _begin_job,
    _current_snapshot,
    _registry_path,
    _run_host,
    _write_installer_config,
)

from xhs_workbench import native_host, video_processing


def _video_messages(*, downloaded: bool, duration_ms: int | None = None) -> list[dict[str, object]]:
    begin = _begin_job(surface="extension_current")
    snapshot = _current_snapshot()
    snapshot["note_type"] = "video"
    snapshot["media_slots"] = [{"note_id": "note_123", "role": "video", "position": 1}]
    messages: list[dict[str, object]] = [
        begin, snapshot, {"protocol_version": "1.0", "kind": "finish_scan", "job_id": "job_123"},
    ]
    if downloaded:
        def box(kind: bytes, payload: bytes = b"") -> bytes:
            return (8 + len(payload)).to_bytes(4, "big") + kind + payload

        mp4 = box(b"ftyp", b"isom\x00\x00\x00\x00") + box(b"moov") + box(b"mdat", b"frame")
        fields = {"protocol_version": "1.0", "job_id": "job_123", "note_id": "note_123",
                  "role": "video", "position": 1, "sequence": 1}
        messages += [
            {**fields, "kind": "media_begin", "size_limit_bytes": 100 * 1024 * 1024},
            {**fields, "kind": "media_chunk", "chunk_index": 0,
             "data_base64": base64.b64encode(mp4).decode("ascii")},
            {**fields, "kind": "media_end", "mime_type": "video/mp4",
             "sha256": hashlib.sha256(mp4).hexdigest()},
        ]
    else:
        messages.append({"protocol_version": "1.0", "kind": "media_missing", "job_id": "job_123",
                         "note_id": "note_123", "role": "video", "position": 1,
                         "reason": "source_not_exposed"})
    metadata: dict[str, object] = {"note_id": "note_123"}
    if duration_ms is not None:
        metadata["duration_ms"] = duration_ms
    messages.append({"protocol_version": "1.0", "kind": "finish_job", "job_id": "job_123",
                     "video_metadata": metadata})
    return messages


def test_missing_long_video_publishes_skipped_priority_without_worker(tmp_path: Path) -> None:
    config, root = _write_installer_config(tmp_path)
    status, responses, _ = _run_host(config, _video_messages(downloaded=False, duration_ms=900001))
    assert status == 0
    assert responses[-1]["video_processing"] == {"status": "skipped_too_long"}
    run_id = json.loads(_registry_path(config, "job_123").read_text())["run_id"]
    report = root / run_id
    assert "超过首期转录时长上限" in (report / "index.html").read_text()
    original_sha = hashlib.sha256((report / "results.json").read_bytes()).hexdigest()
    assert original_sha == json.loads(_registry_path(config, "job_123").read_text())["results_sha256"]
    next_status, next_responses, _ = _run_host(config, [{"protocol_version": "1.0",
                                                        "kind": "video_status", "job_id": "job_123"}])
    assert next_status == 0
    assert next_responses == [{"protocol_version": "1.0", "kind": "video_result",
                               "job_id": "job_123", "processing": {"status": "skipped_too_long"}}]


def test_missing_fifteen_minute_video_reports_not_saved_in_native_result(tmp_path: Path) -> None:
    config, _ = _write_installer_config(tmp_path)
    status, responses, _ = _run_host(config, _video_messages(downloaded=False, duration_ms=900000))
    assert status == 0
    assert responses[-1]["video_processing"] == {"status": "not_started", "reason": "video_not_saved"}


def test_saved_video_worker_is_independent_of_collection_port(tmp_path: Path) -> None:
    config, root = _write_installer_config(tmp_path)
    status, responses, _ = _run_host(config, _video_messages(downloaded=True))
    assert status == 0
    assert responses[-1]["report_available"] is True
    assert responses[-1]["video_processing"]["status"] == "running"
    run_id = json.loads(_registry_path(config, "job_123").read_text())["run_id"]
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline:
        later, video, _ = _run_host(config, [{"protocol_version": "1.0", "kind": "video_status",
                                             "job_id": "job_123"}])
        assert later == 0
        if video[0]["processing"]["status"] == "failed":
            break
        time.sleep(.1)
    else:
        raise AssertionError("detached invalid-video worker never reached finite failure")
    assert video[0]["processing"]["reason"] == "audio_unreadable"
    assert (root / run_id / "assets" / "note_123-video.mp4").is_file()
    assert (root / run_id / "index.html").is_file()


def test_independent_subtitle_failure_is_visible_without_changing_video_outcome(tmp_path: Path) -> None:
    config, root = _write_installer_config(tmp_path)
    messages = _video_messages(downloaded=False)
    messages[-1]["video_metadata"] = {"note_id": "note_123", "subtitle_status": "failed"}
    status, responses, _ = _run_host(config, messages)
    assert status == 0
    assert responses[-1]["video_processing"] == {"status": "not_started", "reason": "video_not_saved"}
    run_id = json.loads(_registry_path(config, "job_123").read_text())["run_id"]
    assert "独立字幕获取失败" in (root / run_id / "index.html").read_text()
    assert json.loads((root / run_id / "processing.json").read_text())["subtitle_status"] == "failed"


def test_busy_current_note_returns_finite_job_in_progress_across_connections(tmp_path: Path) -> None:
    config, root = _write_installer_config(tmp_path)
    loaded = native_host._load_installer_config(config)
    held = video_processing.reserve_video_lock(loaded, "first_job")
    try:
        status, responses, _ = _run_host(config, [_begin_job(surface="extension_current")])
    finally:
        os.close(held)
    assert status == 1
    assert responses == [{"protocol_version": "1.0", "kind": "error", "job_id": "job_123",
                          "code": "job_in_progress", "fatal": False}]
    assert not any(path.joinpath("results.json").exists() for path in root.iterdir())


def test_video_metadata_rejects_another_note_before_publication(tmp_path: Path) -> None:
    config, root = _write_installer_config(tmp_path)
    messages = _video_messages(downloaded=False)
    assert isinstance(messages[-1]["video_metadata"], dict)
    messages[-1]["video_metadata"] = {"note_id": "different"}
    status, responses, _ = _run_host(config, messages)
    assert status == 1
    assert responses[-1]["kind"] == "error"
    assert _registry_path(config, "job_123").exists() is False
    assert not any(path.joinpath("results.json").exists() for path in root.iterdir())
