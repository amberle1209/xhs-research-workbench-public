"""Native host process, report registry, and cleanup-boundary tests."""

from __future__ import annotations

import hashlib
import inspect
import io
import json
import os
import select
import stat
import struct
import subprocess
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from xhs_workbench import extension_import, native_host
from xhs_workbench.models import CollectionRun

ORIGIN = "chrome-extension://abcdefghijklmnopabcdefghijklmnop/"
PAGE_SECRET = "PAGE_BODY_MUST_NEVER_REACH_STDERR"
REPLACEMENT_INDEX = b"replacement-index-sentinel"


def _frame(payload: dict[str, object]) -> bytes:
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return struct.pack("@I", len(encoded)) + encoded


def _decode_frames(value: bytes) -> list[dict[str, object]]:
    frames: list[dict[str, object]] = []
    offset = 0
    while offset < len(value):
        assert offset + 4 <= len(value)
        size = struct.unpack("@I", value[offset : offset + 4])[0]
        offset += 4
        assert 0 < size <= (1024 * 1024)
        assert offset + size <= len(value)
        payload = json.loads(value[offset : offset + size].decode("utf-8"))
        assert isinstance(payload, dict)
        frames.append(payload)
        offset += size
    return frames


def _write_installer_config(tmp_path: Path) -> tuple[Path, Path]:
    output_root = tmp_path / "reports"
    output_root.mkdir(parents=True, mode=0o700)
    config = tmp_path / ".config" / "xhs-workbench" / "extension.json"
    config.parent.mkdir(parents=True, mode=0o700)
    config.write_text(
        json.dumps({"allowed_origin": ORIGIN, "output_root": str(output_root)}), encoding="utf-8"
    )
    os.chmod(config, 0o600)
    return config, output_root


def _use_default_installer_config(monkeypatch: pytest.MonkeyPatch, config: Path) -> None:
    monkeypatch.setattr(native_host, "_default_config_path", lambda: config)


def _begin_job(
    *, job_id: str = "job_123", surface: str = "extension_search", **overrides: object
) -> dict[str, object]:
    values: dict[str, object] = {
        "protocol_version": "1.0",
        "kind": "begin_job",
        "job_id": job_id,
        "collection_surface": surface,
        "source_page_url": (
            "https://www.xiaohongshu.com/explore/note_123"
            if surface == "extension_current"
            else "https://www.xiaohongshu.com/search_result"
        ),
        "requested_count": 1,
        "candidate_scan_limit": 1,
        "publication_cutoff": None,
        "selection_order": "exact_likes_desc",
    }
    values.update(overrides)
    return values


def _current_snapshot(*, job_id: str = "job_123") -> dict[str, object]:
    return {
        "protocol_version": "1.0",
        "kind": "candidate_snapshot",
        "job_id": job_id,
        "source_position": 1,
        "note_id": "note_123",
        "canonical_url": "https://www.xiaohongshu.com/explore/note_123",
        "title": "页面标题",
        "body": PAGE_SECRET,
        "metrics": {
            "likes": {"raw_value": "7", "normalized_value": 7, "precision": "exact"}
        },
        "media_slots": [{"note_id": "note_123", "role": "image", "position": 1}],
    }


def _run_host(
    config: Path,
    messages: list[dict[str, object]] | bytes,
    *,
    origin: str = ORIGIN,
) -> tuple[int, list[dict[str, object]], bytes]:
    input_bytes = (
        messages
        if isinstance(messages, bytes)
        else b"".join(_frame(message) for message in messages)
    )
    output = io.BytesIO()
    status = native_host.run_native_host(
        origin,
        config_path=config,
        input_stream=io.BytesIO(input_bytes),
        output_stream=output,
    )
    raw_output = output.getvalue()
    return status, _decode_frames(raw_output), raw_output


def _registry_path(config: Path, job_id: str) -> Path:
    return config.parent / hashlib.sha256(job_id.encode("utf-8")).hexdigest()


def _write_finished_registry(
    config: Path,
    output_root: Path,
    *,
    job_id: str = "job_123",
    run_id: str = "host_run_456",
) -> tuple[Path, Path, Path]:
    run_dir = output_root / run_id
    run_dir.mkdir(mode=0o700)
    (run_dir / "assets").mkdir(mode=0o700)
    results_path = run_dir / "results.json"
    results = json.dumps({"run_id": run_id}, separators=(",", ":")).encode("utf-8")
    results_path.write_bytes(results)
    os.chmod(results_path, 0o600)
    index_path = run_dir / "index.html"
    index_path.write_text("<!doctype html><title>report</title>", encoding="utf-8")
    os.chmod(index_path, 0o600)
    root_status = output_root.stat()
    registry = {
        "job_id": job_id,
        "run_id": run_id,
        "run_identity": {"device": run_dir.stat().st_dev, "inode": run_dir.stat().st_ino},
        "output_root_identity": {"device": root_status.st_dev, "inode": root_status.st_ino},
        "results_sha256": hashlib.sha256(results).hexdigest(),
    }
    registry_path = _registry_path(config, job_id)
    registry_path.write_text(json.dumps(registry, separators=(",", ":")), encoding="utf-8")
    os.chmod(registry_path, 0o600)
    return registry_path, run_dir, index_path


def _replace_report_path_binding(
    *,
    output_root: Path,
    run_dir: Path,
    index_path: Path,
    target: str,
    held_path: Path,
) -> Path:
    """Replace one report path after validation while retaining the original elsewhere."""
    if target == "root":
        output_root.rename(held_path)
        replacement_run = output_root / run_dir.name
        replacement_run.mkdir(parents=True, mode=0o700)
        (replacement_run / "assets").mkdir(mode=0o700)
        replacement_results = replacement_run / "results.json"
        replacement_results.write_text('{"run_id":"replacement"}', encoding="utf-8")
        os.chmod(replacement_results, 0o600)
        replacement_index = replacement_run / "index.html"
    elif target == "run":
        run_dir.rename(held_path)
        run_dir.mkdir(mode=0o700)
        (run_dir / "assets").mkdir(mode=0o700)
        replacement_results = run_dir / "results.json"
        replacement_results.write_text('{"run_id":"replacement"}', encoding="utf-8")
        os.chmod(replacement_results, 0o600)
        replacement_index = run_dir / "index.html"
    elif target == "index":
        index_path.replace(held_path)
        replacement_index = index_path
    else:
        raise AssertionError(f"unknown replacement target: {target}")
    replacement_index.write_bytes(REPLACEMENT_INDEX)
    os.chmod(replacement_index, 0o600)
    return replacement_index


def test_health_writes_one_framed_ready_response(tmp_path: Path) -> None:
    """Removing the pairing/transport call would leave health without a finite reply."""
    config, _ = _write_installer_config(tmp_path)

    status, responses, raw_output = _run_host(
        config, [{"protocol_version": "1.0", "kind": "health"}]
    )

    assert status == 0
    assert responses == [
        {
            "protocol_version": "1.0",
            "kind": "health_result",
            "status": "ready",
            "host_version": "0.1.3",
        }
    ]
    assert raw_output.startswith(struct.pack("@I", len(raw_output) - 4))


def test_health_response_is_visible_before_the_native_port_closes(tmp_path: Path) -> None:
    """A long-lived Chrome port must receive health before sending its next request."""
    config, _ = _write_installer_config(tmp_path)
    runner = (
        "import sys; from pathlib import Path; "
        "from xhs_workbench.native_host import run_native_host; "
        "raise SystemExit(run_native_host(sys.argv[1], config_path=Path(sys.argv[2]), "
        "input_stream=sys.stdin.buffer, output_stream=sys.stdout.buffer))"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", runner, ORIGIN, str(config)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        assert process.stdin is not None
        assert process.stdout is not None
        process.stdin.write(_frame({"protocol_version": "1.0", "kind": "health"}))
        process.stdin.flush()

        readable, _, _ = select.select([process.stdout], [], [], 2.0)

        assert readable == [process.stdout]
        prefix = process.stdout.read(4)
        size = struct.unpack("@I", prefix)[0]
        response = json.loads(process.stdout.read(size).decode("utf-8"))
        assert response == {
            "protocol_version": "1.0",
            "kind": "health_result",
            "status": "ready",
            "host_version": "0.1.3",
        }
    finally:
        if process.stdin is not None:
            process.stdin.close()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.terminate()
            process.wait(timeout=2)


@pytest.mark.parametrize("kind", ["config_directory", "config", "output_root"])
def test_host_rejects_extended_acls_on_installer_trust_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    """The native host must use the same ACL boundary as the installer status check."""
    config, output_root = _write_installer_config(tmp_path)
    targets = {
        "config_directory": config.parent,
        "config": config,
        "output_root": output_root,
    }
    target = targets[kind]
    monkeypatch.setattr(
        native_host,
        "_has_extended_acl",
        lambda path: Path(path) == target,
        raising=False,
    )

    with pytest.raises(ValueError, match="ACL"):
        native_host._load_installer_config(config)


@pytest.mark.parametrize("kind", ["registry", "run", "results", "index"])
def test_report_opening_rejects_extended_acls_on_bound_report_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    """An ACL cannot delegate any registry or report artifact after host startup."""
    config_path, output_root = _write_installer_config(tmp_path)
    registry, run_dir, index_path = _write_finished_registry(config_path, output_root)
    targets = {
        "registry": registry,
        "run": run_dir,
        "results": run_dir / "results.json",
        "index": index_path,
    }
    target = targets[kind]
    monkeypatch.setattr(
        native_host,
        "_has_extended_acl",
        lambda path: Path(path) == target,
        raising=False,
    )

    with pytest.raises(ValueError, match="ACL"):
        native_host._open_verified_report(native_host._load_installer_config(config_path), "job_123")


def test_host_subprocess_stdout_is_only_framed_json_and_stderr_omits_page_data(tmp_path: Path) -> None:
    """Console diagnostics must not turn rejected page input into a stderr leak."""
    home = tmp_path / "home"
    _config, _ = _write_installer_config(home)
    malformed = {
        "protocol_version": "1.0",
        "kind": "health",
        "page_body": PAGE_SECRET,
    }
    environment = os.environ.copy()
    environment["HOME"] = str(home)

    result = subprocess.run(
        [sys.executable, "-m", "xhs_workbench.native_host", ORIGIN],
        input=_frame(malformed),
        capture_output=True,
        check=False,
        close_fds=True,
        env=environment,
    )

    assert result.returncode != 0
    assert result.stdout == b""
    assert PAGE_SECRET.encode("utf-8") not in result.stderr


def test_metadata_only_zero_selection_job_is_partial_and_registry_uses_only_job_hash(tmp_path: Path) -> None:
    """A zero-selection batch must persist an honest partial bundle without a job-ID path."""
    config, output_root = _write_installer_config(tmp_path)

    status, responses, _ = _run_host(
        config,
        [
            _begin_job(),
            {"protocol_version": "1.0", "kind": "finish_scan", "job_id": "job_123"},
            {"protocol_version": "1.0", "kind": "finish_job", "job_id": "job_123"},
        ],
    )

    assert status == 0
    assert [response["kind"] for response in responses] == [
        "job_started",
        "selection_result",
        "job_result",
    ]
    assert responses[-1] == {
        "protocol_version": "1.0",
        "kind": "job_result",
        "job_id": "job_123",
        "status": "partial",
        "retained_count": 0,
        "report_available": True,
        "report_file": "index.html",
    }
    registry = _registry_path(config, "job_123")
    stored = json.loads(registry.read_text(encoding="utf-8"))
    run_id = stored["run_id"]
    assert registry.name == hashlib.sha256(b"job_123").hexdigest()
    assert stat.S_IMODE(registry.stat().st_mode) == 0o600
    assert run_id != "job_123"
    run_status = (output_root / run_id).stat()
    assert stored["run_identity"] == {"device": run_status.st_dev, "inode": run_status.st_ino}
    assert (output_root / run_id / "results.json").is_file()
    assert (output_root / run_id / "index.html").is_file()
    assert "job_123" not in (output_root / run_id).parts


@pytest.mark.parametrize("media_kind", ["media_begin", "media_chunk", "media_end", "media_missing"])
def test_page_order_returns_frozen_visible_order_and_rejects_full_media(
    tmp_path: Path, media_kind: str
) -> None:
    """Page-order selection preserves its frozen ranks and remains metadata-only."""
    config, _ = _write_installer_config(tmp_path)
    begin = _begin_job(
        job_id="page_order_123",
        requested_count=5,
        candidate_scan_limit=5,
        publication_cutoff=None,
        selection_order="page_order",
        source_page_url="https://www.xiaohongshu.com/search_result",
    )
    messages: list[dict[str, object]] = [begin]
    for source_position in (2, 1, 3, 4, 5):
        note_id = f"note_{source_position}"
        snapshot = _current_snapshot(job_id="page_order_123")
        snapshot.update(
            {
                "source_position": source_position,
                "note_id": note_id,
                "canonical_url": f"https://www.xiaohongshu.com/explore/{note_id}",
                "media_slots": (
                    [{"note_id": note_id, "role": "image", "position": 1}]
                    if source_position == 1
                    else []
                ),
            }
        )
        messages.append(snapshot)
    media_messages: dict[str, dict[str, object]] = {
        "media_begin": {
            "protocol_version": "1.0", "kind": "media_begin", "job_id": "page_order_123",
            "note_id": "note_1", "role": "image", "position": 1, "sequence": 1,
            "size_limit_bytes": 1024,
        },
        "media_chunk": {
            "protocol_version": "1.0", "kind": "media_chunk", "job_id": "page_order_123",
            "note_id": "note_1", "role": "image", "position": 1, "sequence": 1,
            "chunk_index": 0, "data_base64": "YQ==",
        },
        "media_end": {
            "protocol_version": "1.0", "kind": "media_end", "job_id": "page_order_123",
            "note_id": "note_1", "role": "image", "position": 1, "sequence": 1,
            "mime_type": "image/jpeg", "sha256": "0" * 64,
        },
        "media_missing": {
            "protocol_version": "1.0", "kind": "media_missing", "job_id": "page_order_123",
            "note_id": "note_1", "role": "image", "position": 1,
            "reason": "source_not_exposed",
        },
    }
    messages.extend(
        [
                {"protocol_version": "1.0", "kind": "finish_scan", "job_id": "page_order_123", "scroll_rounds": 0},
            media_messages[media_kind],
        ]
    )

    _status, responses, _ = _run_host(config, messages)

    selection = next(response for response in responses if response["kind"] == "selection_result")
    assert selection["selected"] == [
        {"note_id": f"note_{position}", "selection_rank": position}
        for position in range(1, 6)
    ]
    assert responses[-1] == {
        "protocol_version": "1.0",
        "kind": "error",
        "job_id": "page_order_123",
        "code": "invalid_state",
        "fatal": False,
    }


def test_page_order_host_accepts_rank_ordered_detail_snapshots_after_freeze(
    tmp_path: Path,
) -> None:
    config, output_root = _write_installer_config(tmp_path)
    note_ids = ["note_a", "note_b", "note_c", "note_d", "note_e"]
    begin = _begin_job(
        job_id="page_order_post_freeze",
        requested_count=5,
        candidate_scan_limit=5,
        publication_cutoff=None,
        selection_order="page_order",
        source_page_url="https://www.xiaohongshu.com/search_result",
    )
    summaries = []
    details = []
    for position, note_id in enumerate(note_ids, start=1):
        snapshot = _current_snapshot(job_id="page_order_post_freeze")
        snapshot.update({
            "source_position": position,
            "note_id": note_id,
            "canonical_url": f"https://www.xiaohongshu.com/explore/{note_id}",
            "media_slots": [],
        })
        summaries.append(snapshot)
        detail = dict(snapshot)
        detail["title"] = f"detail {note_id}"
        details.append(detail)

    status, responses, _ = _run_host(
        config,
        [begin, *summaries, {"protocol_version": "1.0", "kind": "finish_scan", "job_id": "page_order_post_freeze", "scroll_rounds": 0}, *details, {"protocol_version": "1.0", "kind": "finish_job", "job_id": "page_order_post_freeze"}],
    )

    assert status == 0
    assert responses[-1] == {
        "protocol_version": "1.0", "kind": "job_result", "job_id": "page_order_post_freeze",
        "status": "complete", "retained_count": 5, "report_available": True, "report_file": "index.html",
    }
    stored = json.loads(next(iter(output_root.iterdir())).joinpath("results.json").read_text(encoding="utf-8"))
    assert stored["extension_search_run"]["enriched_count"] == 5


def test_page_order_stop_after_freeze_persists_a_stopped_partial_bundle(
    tmp_path: Path,
) -> None:
    config, output_root = _write_installer_config(tmp_path)
    begin = _begin_job(
        job_id="page_order_stop_after_freeze",
        requested_count=5,
        candidate_scan_limit=5,
        publication_cutoff=None,
        selection_order="page_order",
        source_page_url="https://www.xiaohongshu.com/search_result",
    )
    summaries = []
    for position in range(1, 6):
        note_id = f"note_{position}"
        snapshot = _current_snapshot(job_id="page_order_stop_after_freeze")
        snapshot.update({"source_position": position, "note_id": note_id, "canonical_url": f"https://www.xiaohongshu.com/explore/{note_id}", "media_slots": []})
        summaries.append(snapshot)

    status, responses, raw_output = _run_host(
        config,
        [begin, *summaries, {"protocol_version": "1.0", "kind": "finish_scan", "job_id": "page_order_stop_after_freeze", "scroll_rounds": 0}, summaries[0], {"protocol_version": "1.0", "kind": "stop_job", "job_id": "page_order_stop_after_freeze"}],
    )

    assert status == 0, (responses, raw_output)
    assert responses[-1] == {
        "protocol_version": "1.0", "kind": "job_result", "job_id": "page_order_stop_after_freeze",
        "status": "stopped", "retained_count": 5, "report_available": True, "report_file": "index.html",
    }
    stored = json.loads(next(iter(output_root.iterdir())).joinpath("results.json").read_text(encoding="utf-8"))
    assert stored["status"] == "stopped"
    assert stored["extension_search_run"]["error_code"] == "stopped"


@pytest.mark.parametrize(
    ("cause", "expected_status", "expected_error", "has_report"),
    [
        ("stopped", "stopped", "stopped", True),
        ("login_required", "partial", "login_required", True),
        ("challenge_detected", "partial", "challenge_detected", True),
        ("structural_error", "failed", "structural_error", True),
        ("route_mismatch", "failed", "route_mismatch", True),
        ("identity_mismatch", "failed", "identity_mismatch", True),
    ],
)
def test_page_order_stop_cause_has_an_honest_finite_result(
    tmp_path: Path, cause: str, expected_status: str, expected_error: str | None, has_report: bool
) -> None:
    config, output_root = _write_installer_config(tmp_path)
    job_id = f"cause_{cause}"
    begin = _begin_job(
        job_id=job_id, requested_count=5, candidate_scan_limit=5, publication_cutoff=None,
        selection_order="page_order", source_page_url="https://www.xiaohongshu.com/search_result",
    )
    summaries: list[dict[str, object]] = []
    for position in range(1, 6):
        snapshot = _current_snapshot(job_id=job_id)
        note_id = f"note_{position}"
        snapshot.update({"source_position": position, "note_id": note_id, "canonical_url": f"https://www.xiaohongshu.com/explore/{note_id}", "media_slots": []})
        summaries.append(snapshot)
    status, responses, raw_output = _run_host(config, [
        begin, *summaries,
        {"protocol_version": "1.0", "kind": "finish_scan", "job_id": job_id, "scroll_rounds": 1},
        summaries[0],
        {"protocol_version": "1.0", "kind": "stop_job", "job_id": job_id, "terminal_cause": cause},
    ])
    assert status == 0, raw_output
    result = responses[-1]
    assert result["kind"] == "job_result"
    assert result["status"] == expected_status
    assert result["report_available"] is has_report
    if not has_report:
        assert result["retained_count"] == 0
        assert list(output_root.iterdir()) == []
        return
    if expected_status == "failed":
        assert result["retained_count"] == 0
    stored = json.loads(next(iter(output_root.iterdir())).joinpath("results.json").read_text(encoding="utf-8"))
    assert CollectionRun.model_validate(stored).status.value == expected_status
    assert stored["status"] == expected_status
    assert stored["error_code"] == expected_error
    assert stored["extension_search_run"]["error_code"] == expected_error
    assert stored["extension_search_run"]["scroll_rounds"] == 1


@pytest.mark.parametrize("scroll_rounds", [0, 1, 2])
def test_page_order_persists_observed_scroll_rounds(tmp_path: Path, scroll_rounds: int) -> None:
    config, output_root = _write_installer_config(tmp_path)
    job_id = f"scroll_{scroll_rounds}"
    begin = _begin_job(job_id=job_id, requested_count=5, candidate_scan_limit=5, publication_cutoff=None, selection_order="page_order", source_page_url="https://www.xiaohongshu.com/search_result")
    summaries = []
    for position in range(1, 6):
        item = _current_snapshot(job_id=job_id)
        note_id = f"note_{position}"
        item.update({"source_position": position, "note_id": note_id, "canonical_url": f"https://www.xiaohongshu.com/explore/{note_id}", "media_slots": []})
        summaries.append(item)
    status, responses, raw = _run_host(config, [begin, *summaries, {"protocol_version": "1.0", "kind": "finish_scan", "job_id": job_id, "scroll_rounds": scroll_rounds}, *summaries, {"protocol_version": "1.0", "kind": "finish_job", "job_id": job_id}])
    assert status == 0, raw
    assert responses[-1]["status"] == "complete"
    stored = json.loads(next(iter(output_root.iterdir())).joinpath("results.json").read_text(encoding="utf-8"))
    assert stored["extension_search_run"]["scroll_rounds"] == scroll_rounds


def test_page_order_persists_monotonic_runner_positions_for_a_resliced_scan(tmp_path: Path) -> None:
    """The Host receives run-level positions, not the scanner's per-round virtualized offsets."""
    config, output_root = _write_installer_config(tmp_path)
    job_id = "resliced_positions"
    begin = _begin_job(job_id=job_id, requested_count=5, candidate_scan_limit=5, publication_cutoff=None, selection_order="page_order", source_page_url="https://www.xiaohongshu.com/search_result")
    summaries = []
    for position, note_id in enumerate(("note_first", "note_second", "note_virtualized"), start=1):
        item = _current_snapshot(job_id=job_id)
        item.update({"source_position": position, "note_id": note_id, "canonical_url": f"https://www.xiaohongshu.com/explore/{note_id}", "media_slots": []})
        summaries.append(item)
    status, responses, raw = _run_host(config, [begin, *summaries, {"protocol_version": "1.0", "kind": "finish_scan", "job_id": job_id, "scroll_rounds": 2}, *summaries, {"protocol_version": "1.0", "kind": "finish_job", "job_id": job_id}])
    assert status == 0, raw
    assert responses[-1]["status"] == "partial"
    stored = json.loads(next(iter(output_root.iterdir())).joinpath("results.json").read_text(encoding="utf-8"))
    assert [entry["source_position"] for entry in stored["extension_selection"]["entries"]] == [1, 2, 3]
    assert CollectionRun.model_validate(stored).actual_count == 3


def test_page_order_stores_sponsored_scan_evidence_and_safe_unavailable_summary(tmp_path: Path) -> None:
    config, output_root = _write_installer_config(tmp_path)
    job_id = "sponsored_summary"
    begin = _begin_job(job_id=job_id, requested_count=5, candidate_scan_limit=6, publication_cutoff=None, selection_order="page_order", source_page_url="https://www.xiaohongshu.com/search_result")
    sponsored = {"protocol_version": "1.0", "kind": "candidate_unavailable", "job_id": job_id, "source_position": 1, "note_id": "ad_1", "reason": "sponsored"}
    summaries = []
    for position in range(2, 7):
        item = _current_snapshot(job_id=job_id)
        note_id = f"note_{position}"
        item.update({"source_position": position, "note_id": note_id, "canonical_url": f"https://www.xiaohongshu.com/explore/{note_id}", "title": f"summary {note_id}", "note_type": "normal", "metrics": {"likes": {"raw_value": "12", "normalized_value": 12, "precision": "exact"}}, "metric_provenance": {"likes": "search_card_interface"}, "media_slots": []})
        summaries.append(item)
    unavailable = {"protocol_version": "1.0", "kind": "candidate_unavailable", "job_id": job_id, "source_position": 2, "note_id": "note_2", "reason": "detail_unavailable"}
    status, responses, raw = _run_host(config, [begin, sponsored, *summaries, {"protocol_version": "1.0", "kind": "finish_scan", "job_id": job_id, "scroll_rounds": 2, "sort_label": "综合"}, unavailable, *summaries[1:], {"protocol_version": "1.0", "kind": "finish_job", "job_id": job_id}])
    assert status == 0, raw
    assert responses[-1]["status"] == "partial"
    stored = json.loads(next(iter(output_root.iterdir())).joinpath("results.json").read_text(encoding="utf-8"))
    assert stored["extension_search_run"]["sort_label"] == "综合"
    assert stored["extension_selection"]["entries"][0]["reason"] == "sponsored"
    assert stored["extension_selection"]["entries"][0]["source_position"] == 1
    note = next(note for note in stored["notes"] if note["note_id"] == "note_2")
    assert note["title"] == "summary note_2"
    assert note["metrics"]["likes"]["normalized_value"] == 12


def test_current_note_job_preserves_the_fixed_order_and_hides_host_run_id(tmp_path: Path) -> None:
    """Changing dispatch order must reject the current-note lifecycle rather than fabricate a result."""
    config, output_root = _write_installer_config(tmp_path)
    messages = [
        _begin_job(surface="extension_current"),
        _current_snapshot(),
        {"protocol_version": "1.0", "kind": "finish_scan", "job_id": "job_123"},
        {
            "protocol_version": "1.0",
            "kind": "media_missing",
            "job_id": "job_123",
            "note_id": "note_123",
            "role": "image",
            "position": 1,
            "reason": "source_not_exposed",
        },
        {"protocol_version": "1.0", "kind": "finish_job", "job_id": "job_123"},
    ]

    status, responses, raw_output = _run_host(config, messages)

    assert status == 0
    assert [response["kind"] for response in responses] == [
        "job_started",
        "candidate_result",
        "selection_result",
        "media_result",
        "job_result",
    ]
    assert responses[-1]["status"] == "partial"
    run_id = json.loads(_registry_path(config, "job_123").read_text(encoding="utf-8"))["run_id"]
    assert run_id != "job_123"
    assert run_id.encode("utf-8") not in raw_output
    assert (output_root / run_id / "index.html").is_file()


def test_media_begin_can_return_a_correlated_terminal_budget_rejection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A host-side budget refusal must close the slot before any chunk is accepted."""
    config, _ = _write_installer_config(tmp_path)
    original_job = native_host.ExtensionJob

    class ExhaustedRunBudgetJob(original_job):
        def begin_media(self, message: object) -> object:
            self._downloaded_run_bytes = extension_import.MAX_RUN_MEDIA_BYTES
            return super().begin_media(message)  # type: ignore[arg-type]

    monkeypatch.setattr(native_host, "ExtensionJob", ExhaustedRunBudgetJob)
    status, responses, _ = _run_host(
        config,
        [
            _begin_job(surface="extension_current"),
            _current_snapshot(),
            {"protocol_version": "1.0", "kind": "finish_scan", "job_id": "job_123"},
            {
                "protocol_version": "1.0",
                "kind": "media_begin",
                "job_id": "job_123",
                "note_id": "note_123",
                "role": "image",
                "position": 1,
                "sequence": 1,
                "size_limit_bytes": 1,
            },
            {"protocol_version": "1.0", "kind": "finish_job", "job_id": "job_123"},
        ],
    )

    assert status == 0
    assert [response["kind"] for response in responses] == [
        "job_started",
        "candidate_result",
        "selection_result",
        "media_result",
        "job_result",
    ]
    assert responses[-2] == {
        "protocol_version": "1.0",
        "kind": "media_result",
        "job_id": "job_123",
        "note_id": "note_123",
        "role": "image",
        "position": 1,
        "outcome": "rejected",
        "reason": "run_budget",
    }
    assert responses[-1]["status"] == "partial"


def test_duplicate_begin_and_out_of_order_messages_return_correlated_finite_errors(tmp_path: Path) -> None:
    """Removing host state ownership would allow two jobs or an unattached candidate."""
    config, output_root = _write_installer_config(tmp_path)

    status, responses, _ = _run_host(
        config,
        [
            _begin_job(job_id="job_a"),
            _begin_job(job_id="job_b"),
            {
                "protocol_version": "1.0",
                "kind": "candidate_snapshot",
                "job_id": "job_b",
                "source_position": 1,
                "note_id": "note_123",
                "canonical_url": "https://www.xiaohongshu.com/explore/note_123",
                "metrics": {},
            },
        ],
    )

    assert status == 1
    assert responses[0]["kind"] == "job_started"
    assert responses[1] == {
        "protocol_version": "1.0",
        "kind": "error",
        "job_id": "job_b",
        "code": "internal_error",
        "fatal": True,
    }
    assert len(list(output_root.iterdir())) == 1


def test_protocol_mismatch_is_finite_and_does_not_create_a_run(tmp_path: Path) -> None:
    """A nonmatching protocol version must never reach ExtensionJob construction."""
    config, output_root = _write_installer_config(tmp_path)

    status, responses, _ = _run_host(config, [{"protocol_version": "2.0", "kind": "health"}])

    assert status != 0
    assert responses == [
        {
            "protocol_version": "1.0",
            "kind": "error",
            "code": "unsupported_protocol",
            "fatal": True,
        }
    ]
    assert list(output_root.iterdir()) == []


def test_malformed_input_and_eof_discard_an_unfinished_job(tmp_path: Path) -> None:
    """Removing finally cleanup would leave an interrupted run directory behind."""
    config, output_root = _write_installer_config(tmp_path)
    malformed_after_begin = _frame(_begin_job()) + struct.pack("@I", 1) + b"{"

    status, responses, _ = _run_host(config, malformed_after_begin)

    assert status != 0
    assert [response["kind"] for response in responses] == ["job_started"]
    assert all(path.name.startswith(".xhs-cleanup-") for path in output_root.iterdir())

    status, responses, _ = _run_host(config, [_begin_job(job_id="job_eof")])

    assert status != 0
    assert [response["kind"] for response in responses] == ["job_started"]
    assert all(path.name.startswith(".xhs-cleanup-") for path in output_root.iterdir())


def test_eof_cleanup_failure_is_terminal_and_never_reports_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cleanup fault on port loss must be visible as failure, not a clean successful exit."""
    config, _ = _write_installer_config(tmp_path)
    original_job = native_host.ExtensionJob

    class FailingDiscardJob(original_job):
        def discard(self) -> None:
            raise ValueError("injected cleanup failure")

    monkeypatch.setattr(native_host, "ExtensionJob", FailingDiscardJob)

    status, responses, _ = _run_host(config, [_begin_job(job_id="job_cleanup_failure")])

    assert status != 0
    assert [response["kind"] for response in responses] == ["job_started"]


def test_wrong_origin_is_rejected_before_the_host_reads_stdin(tmp_path: Path) -> None:
    """Reading first would let an unregistered Chrome caller influence host state."""
    config, _ = _write_installer_config(tmp_path)

    class UnreadableInput(io.BytesIO):
        def read(self, size: int = -1) -> bytes:
            raise AssertionError("origin validation must happen before stdin is read")

    status = native_host.run_native_host(
        "chrome-extension://pppppppppppppppppppppppppppppppp/",
        config_path=config,
        input_stream=UnreadableInput(_frame({"protocol_version": "1.0", "kind": "health"})),
        output_stream=io.BytesIO(),
    )

    assert status != 0


def test_host_rejects_a_group_writable_registry_directory(tmp_path: Path) -> None:
    """A writable registry parent would let another local principal replace verified mappings."""
    config, _ = _write_installer_config(tmp_path)
    os.chmod(config.parent, 0o720)

    status, responses, _ = _run_host(
        config, [{"protocol_version": "1.0", "kind": "health"}]
    )

    assert status != 0
    assert responses == []


def test_begin_refuses_a_replaced_configured_output_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A root rebound after config validation must not receive a host-created run."""
    config, output_root = _write_installer_config(tmp_path)
    held_root = tmp_path / "held-output-root"
    original_job = native_host.ExtensionJob
    swapped = False

    class SwapOutputRootJob(original_job):
        def __init__(self, *args: object, **kwargs: object) -> None:
            nonlocal swapped
            if not swapped:
                output_root.rename(held_root)
                output_root.mkdir(mode=0o700)
                swapped = True
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(native_host, "ExtensionJob", SwapOutputRootJob)

    status, responses, _ = _run_host(config, [_begin_job()])

    assert status == 1
    assert responses == [
        {
            "protocol_version": "1.0",
            "kind": "error",
            "job_id": "job_123",
            "code": "internal_error",
            "fatal": True,
        }
    ]
    assert list(output_root.iterdir()) == []
    assert list(held_root.iterdir()) == []


def test_all_host_writes_supply_the_request_for_protocol_correlation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dropping request= would bypass Task 2's job/error response binding."""
    config, _ = _write_installer_config(tmp_path)
    real_write = native_host.write_native_message
    paired_requests: list[object | None] = []

    def require_pair(
        stream: io.BytesIO, response: object, *, request: object | None = None
    ) -> None:
        paired_requests.append(request)
        assert request is not None
        real_write(stream, response, request=request)  # type: ignore[arg-type]

    monkeypatch.setattr(native_host, "write_native_message", require_pair)

    status, responses, _ = _run_host(
        config,
        [
            {"protocol_version": "1.0", "kind": "health"},
            {
                "protocol_version": "1.0",
                "kind": "candidate_snapshot",
                "job_id": "job_123",
                "source_position": 1,
                "note_id": "note_123",
                "canonical_url": "https://www.xiaohongshu.com/explore/note_123",
                "metrics": {},
            },
        ],
    )

    assert status == 1
    assert [response["kind"] for response in responses] == ["health_result", "error"]
    assert len(paired_requests) == 2


def test_open_report_uses_only_the_descriptor_validated_absolute_index_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Replacing the opener argument with Chrome input would turn report opening into path traversal."""
    config, output_root = _write_installer_config(tmp_path)
    _, _, index_path = _write_finished_registry(config, output_root)
    _use_default_installer_config(monkeypatch, config)
    calls: list[tuple[list[str], dict[str, object]]] = []

    def record_open(command: list[str], **kwargs: object) -> object:
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(native_host.subprocess, "run", record_open)

    assert tuple(inspect.signature(native_host.open_report).parameters) == ("job_id",)
    assert native_host.open_report("job_123") is True
    assert len(calls) == 1
    command, kwargs = calls[0]
    assert command == ["open", str(index_path)]
    assert kwargs == {"check": False, "close_fds": True}


def test_native_open_report_resolves_once_before_its_only_opener_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Repeated lookup could race a changed registry between validation and the opener call."""
    config, output_root = _write_installer_config(tmp_path)
    _write_finished_registry(config, output_root)
    real_lookup = native_host._open_verified_report
    resolved: list[str] = []
    calls: list[list[str]] = []

    def count_lookup(loaded: native_host.InstallerConfig, job_id: str) -> native_host._VerifiedReport:
        resolved.append(job_id)
        return real_lookup(loaded, job_id)

    def record_open(command: list[str], **_: object) -> object:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(native_host, "_open_verified_report", count_lookup)
    monkeypatch.setattr(native_host.subprocess, "run", record_open)

    status, responses, _ = _run_host(
        config, [{"protocol_version": "1.0", "kind": "open_report", "job_id": "job_123"}]
    )

    assert status == 0
    assert responses == [
        {"protocol_version": "1.0", "kind": "report_result", "job_id": "job_123", "opened": True}
    ]
    assert resolved == ["job_123"]
    assert len(calls) == 1
    assert calls == [["open", str(output_root / "host_run_456" / "index.html")]]


def test_native_open_report_returns_only_the_verified_terminal_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A recovered popup may trust only the finished bundle's bounded terminal status."""
    config, _ = _write_installer_config(tmp_path)
    messages = json.loads(
        (Path(__file__).parent / "fixtures" / "extension_acceptance" / "current_note_image_messages.json").read_text(encoding="utf-8")
    )
    status, responses, _ = _run_host(config, messages)
    assert status == 0
    assert responses[-1]["kind"] == "job_result"

    monkeypatch.setattr(
        native_host.subprocess,
        "run",
        lambda command, **_: subprocess.CompletedProcess(command, 0),
    )
    status, responses, _ = _run_host(
        config,
        [{"protocol_version": "1.0", "kind": "open_report", "job_id": "acceptance_current"}],
    )

    assert status == 0
    assert responses == [{
        "protocol_version": "1.0",
        "kind": "report_result",
        "job_id": "acceptance_current",
        "opened": True,
        "terminal_status": "complete",
    }]


@pytest.mark.parametrize("job_id", ["../outside", "/tmp/outside", "job_unknown"])
def test_open_report_rejects_arbitrary_paths_unknown_and_unfinished_jobs(
    tmp_path: Path, job_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing verified registry must not be replaced with an input-derived path."""
    config, _ = _write_installer_config(tmp_path)
    _use_default_installer_config(monkeypatch, config)

    with pytest.raises(ValueError):
        native_host.open_report(job_id)


def test_open_report_rejects_forged_registry_job_mapping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hash filename alone must not authorize a mismatched registry payload."""
    config, output_root = _write_installer_config(tmp_path)
    registry, _, _ = _write_finished_registry(config, output_root)
    payload = json.loads(registry.read_text(encoding="utf-8"))
    payload["job_id"] = "job_other"
    registry.write_text(json.dumps(payload), encoding="utf-8")
    os.chmod(registry, 0o600)
    _use_default_installer_config(monkeypatch, config)

    with pytest.raises(ValueError):
        native_host.open_report("job_123")


def test_open_report_rejects_symlinked_registry_run_and_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Following any registry or report symlink would escape the verified run boundary."""
    config, output_root = _write_installer_config(tmp_path)
    registry, run_dir, index_path = _write_finished_registry(config, output_root)
    _use_default_installer_config(monkeypatch, config)
    registry_target = tmp_path / "registry-target"
    registry.replace(registry_target)
    registry.symlink_to(registry_target)
    with pytest.raises(ValueError):
        native_host.open_report("job_123")

    registry.unlink()
    registry_target.replace(registry)
    run_target = tmp_path / "run-target"
    run_dir.replace(run_target)
    run_dir.symlink_to(run_target, target_is_directory=True)
    with pytest.raises(ValueError):
        native_host.open_report("job_123")

    run_dir.unlink()
    run_target.replace(run_dir)
    outside_index = tmp_path / "outside-index.html"
    outside_index.write_text("outside", encoding="utf-8")
    index_path.unlink()
    index_path.symlink_to(outside_index)
    with pytest.raises(ValueError):
        native_host.open_report("job_123")


def test_open_report_rejects_mismatched_manifest_and_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A moved or edited results manifest must not authorize report opening."""
    config, output_root = _write_installer_config(tmp_path)
    registry, run_dir, _ = _write_finished_registry(config, output_root)
    results_path = run_dir / "results.json"
    changed = b'{"run_id":"other_run"}'
    results_path.write_bytes(changed)
    os.chmod(results_path, 0o600)
    payload = json.loads(registry.read_text(encoding="utf-8"))
    payload["results_sha256"] = hashlib.sha256(changed).hexdigest()
    registry.write_text(json.dumps(payload), encoding="utf-8")
    os.chmod(registry, 0o600)
    _use_default_installer_config(monkeypatch, config)

    with pytest.raises(ValueError):
        native_host.open_report("job_123")


def test_verified_report_rejects_another_users_index_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dropping owner validation would let a hostile report file be opened."""
    config_path, output_root = _write_installer_config(tmp_path)
    _, _, index_path = _write_finished_registry(config_path, output_root)
    config = native_host._load_installer_config(config_path)
    index_identity = (index_path.stat().st_dev, index_path.stat().st_ino)
    real_fstat = native_host.os.fstat

    def foreign_index(fd: int) -> os.stat_result:
        metadata = real_fstat(fd)
        if (metadata.st_dev, metadata.st_ino) == index_identity:
            values = list(metadata)
            values[4] = os.getuid() + 1
            return os.stat_result(values)
        return metadata

    monkeypatch.setattr(native_host.os, "fstat", foreign_index)

    with pytest.raises(ValueError):
        native_host._open_verified_report(config, "job_123")


def test_host_interruption_preserves_a_replacement_at_the_final_delete_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A swap after final verification at the host boundary is never pathname-deleted."""
    config, output_root = _write_installer_config(tmp_path)
    run_id = "host_final_delete_race"
    real_rename = extension_import.os.rename
    real_move = extension_import._rename_no_replace
    real_lstat = extension_import.os.lstat
    captured: dict[str, Any] = {}

    def fixed_host_token(size: int) -> str:
        assert size == 32
        return run_id

    def record_atomic_quarantine(parent_fd: int, source: str, destination: str) -> None:
        real_move(parent_fd, source, destination)
        if source == run_id:
            captured["parent_fd"] = parent_fd
            captured["quarantine"] = destination

    def replace_after_final_verification(
        path: object, *args: object, **kwargs: object
    ) -> os.stat_result:
        result = real_lstat(path, *args, **kwargs)
        if (
            path == captured.get("quarantine")
            and kwargs.get("dir_fd") == captured.get("parent_fd")
            and "replacement" not in captured
        ):
            quarantine = captured["quarantine"]
            parent_fd = captured["parent_fd"]
            real_rename(
                quarantine,
                ".held-original-run",
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
            os.mkdir(quarantine, dir_fd=parent_fd)
            replacement_fd = os.open(
                quarantine, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd
            )
            try:
                sentinel_fd = os.open(
                    "sentinel.txt",
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=replacement_fd,
                )
                try:
                    os.write(sentinel_fd, b"replacement-run-sentinel")
                finally:
                    os.close(sentinel_fd)
            finally:
                os.close(replacement_fd)
            captured["replacement"] = quarantine
        return result

    monkeypatch.setattr(native_host, "secrets", types.SimpleNamespace(token_hex=fixed_host_token))
    monkeypatch.setattr(extension_import, "_rename_no_replace", record_atomic_quarantine)
    monkeypatch.setattr(extension_import.os, "lstat", replace_after_final_verification)

    status, responses, _ = _run_host(config, [_begin_job(job_id="job_race")])

    # A binding drift makes cleanup terminal rather than risking a replacement.
    assert status == 1
    assert [response["kind"] for response in responses] == ["job_started"]
    assert captured["replacement"]
    assert (output_root / captured["replacement"] / "sentinel.txt").read_bytes() == b"replacement-run-sentinel"
    assert (output_root / ".held-original-run").is_dir()


def test_cleanup_failure_on_stop_latches_the_host_before_follow_up_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed stop cleanup cannot clear the active job and resume the protocol."""
    config, _ = _write_installer_config(tmp_path)
    original_job = native_host.ExtensionJob

    class FailingDiscardJob(original_job):
        def discard(self) -> None:
            raise ValueError("injected stop cleanup failure")

    monkeypatch.setattr(native_host, "ExtensionJob", FailingDiscardJob)

    status, responses, _ = _run_host(
        config,
        [
            _begin_job(job_id="job_stop"),
            {"protocol_version": "1.0", "kind": "stop_job", "job_id": "job_stop"},
            {"protocol_version": "1.0", "kind": "health"},
        ],
    )

    assert status == 1
    assert [response["kind"] for response in responses] == ["job_started", "error"]
    assert responses[-1] == {
        "protocol_version": "1.0",
        "kind": "error",
        "job_id": "job_stop",
        "code": "internal_error",
        "fatal": True,
    }


def test_cleanup_failure_on_identity_mismatch_latches_before_follow_up_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mismatch cleanup failure is fatal and cannot be followed by health or another job."""
    config, _ = _write_installer_config(tmp_path)
    original_job = native_host.ExtensionJob

    class FailingDiscardJob(original_job):
        def discard(self) -> None:
            raise ValueError("injected mismatch cleanup failure")

    monkeypatch.setattr(native_host, "ExtensionJob", FailingDiscardJob)

    status, responses, _ = _run_host(
        config,
        [
            _begin_job(job_id="job_active"),
            _current_snapshot(job_id="job_other"),
            {"protocol_version": "1.0", "kind": "health"},
        ],
    )

    assert status == 1
    assert [response["kind"] for response in responses] == ["job_started", "error"]
    assert responses[-1] == {
        "protocol_version": "1.0",
        "kind": "error",
        "job_id": "job_other",
        "code": "internal_error",
        "fatal": True,
    }


def test_cleanup_failure_while_handling_an_error_latches_before_follow_up_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ordinary state error still has to clean up its active job before exit."""
    config, _ = _write_installer_config(tmp_path)
    original_job = native_host.ExtensionJob

    class FailingDiscardJob(original_job):
        def discard(self) -> None:
            raise ValueError("injected error cleanup failure")

    monkeypatch.setattr(native_host, "ExtensionJob", FailingDiscardJob)

    status, responses, _ = _run_host(
        config,
        [
            _begin_job(job_id="job_active"),
            _begin_job(job_id="job_second"),
            {"protocol_version": "1.0", "kind": "health"},
        ],
    )

    assert status == 1
    assert [response["kind"] for response in responses] == ["job_started", "error"]
    assert responses[-1] == {
        "protocol_version": "1.0",
        "kind": "error",
        "job_id": "job_second",
        "code": "internal_error",
        "fatal": True,
    }


def test_health_after_begin_is_terminal_and_never_reaches_a_follow_up_message(tmp_path: Path) -> None:
    """Health is an idle-only probe, not an in-job command."""
    config, _ = _write_installer_config(tmp_path)

    status, responses, _ = _run_host(
        config,
        [
            _begin_job(),
            {"protocol_version": "1.0", "kind": "health"},
            {"protocol_version": "1.0", "kind": "finish_scan", "job_id": "job_123"},
        ],
    )

    assert status == 1
    assert [response["kind"] for response in responses] == ["job_started", "error"]
    assert responses[-1]["code"] == "internal_error"
    assert responses[-1]["fatal"] is True


def test_begin_after_finished_session_is_not_processed(tmp_path: Path) -> None:
    """A native process owns at most one begin_job, even if more frames are already buffered."""
    config, _ = _write_installer_config(tmp_path)

    status, responses, _ = _run_host(
        config,
        [
            _begin_job(surface="extension_current"),
            _current_snapshot(),
            {"protocol_version": "1.0", "kind": "finish_scan", "job_id": "job_123"},
            {
                "protocol_version": "1.0",
                "kind": "media_missing",
                "job_id": "job_123",
                "note_id": "note_123",
                "role": "image",
                "position": 1,
                "reason": "source_not_exposed",
            },
            {"protocol_version": "1.0", "kind": "finish_job", "job_id": "job_123"},
            _begin_job(job_id="job_second", surface="extension_current"),
        ],
    )

    assert status == 0
    assert [response["kind"] for response in responses] == [
        "job_started",
        "candidate_result",
        "selection_result",
        "media_result",
        "job_result",
    ]


def test_open_report_for_another_job_is_forbidden_while_a_job_is_active(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An active collection session cannot be repurposed to open another job's report."""
    config, output_root = _write_installer_config(tmp_path)
    _write_finished_registry(config, output_root, job_id="job_finished", run_id="finished_run")
    calls: list[object] = []

    def fail_if_opened(*args: object, **kwargs: object) -> object:
        calls.append((args, kwargs))
        raise AssertionError("open_report must not run while a job is active")

    monkeypatch.setattr(native_host.subprocess, "run", fail_if_opened)

    status, responses, _ = _run_host(
        config,
        [
            _begin_job(job_id="job_active"),
            {"protocol_version": "1.0", "kind": "open_report", "job_id": "job_finished"},
        ],
    )

    assert status == 1
    assert [response["kind"] for response in responses] == ["job_started", "error"]
    assert responses[-1]["code"] == "internal_error"
    assert calls == []


def test_post_error_messages_are_not_processed(tmp_path: Path) -> None:
    """The first protocol error terminates this native-host connection."""
    config, _ = _write_installer_config(tmp_path)

    status, responses, _ = _run_host(
        config,
        [
            _current_snapshot(),
            {"protocol_version": "1.0", "kind": "health"},
            _begin_job(),
        ],
    )

    assert status == 1
    assert responses == [
        {
            "protocol_version": "1.0",
            "kind": "error",
            "job_id": "job_123",
            "code": "invalid_state",
            "fatal": False,
        }
    ]


@pytest.mark.parametrize("target", ["root", "run", "index"])
def test_open_report_rejects_bound_path_replacement_before_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    """Removing the pre-launch binding check would pass a substituted report path to `open`."""
    config, output_root = _write_installer_config(tmp_path)
    _, run_dir, index_path = _write_finished_registry(config, output_root)
    _use_default_installer_config(monkeypatch, config)
    real_lookup = native_host._open_verified_report
    calls: list[list[str]] = []
    replacement: dict[str, Path] = {}

    def replace_after_lookup(
        loaded: native_host.InstallerConfig, job_id: str
    ) -> native_host._VerifiedReport:
        verified_report = real_lookup(loaded, job_id)
        replacement["index"] = _replace_report_path_binding(
            output_root=output_root,
            run_dir=run_dir,
            index_path=index_path,
            target=target,
            held_path=tmp_path / f"held-before-launch-{target}",
        )
        return verified_report

    def record_open(command: list[str], **_: object) -> object:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(native_host, "_open_verified_report", replace_after_lookup)
    monkeypatch.setattr(native_host.subprocess, "run", record_open)

    with pytest.raises(ValueError, match="report identity changed"):
        native_host.open_report("job_123")

    assert calls == []
    assert replacement["index"].read_bytes() == REPLACEMENT_INDEX


@pytest.mark.parametrize("target", ["root", "run", "index"])
def test_open_report_detects_bound_path_replacement_during_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    """The post-launch check reports a same-user replacement observed during the opener call."""
    config, output_root = _write_installer_config(tmp_path)
    _, run_dir, index_path = _write_finished_registry(config, output_root)
    _use_default_installer_config(monkeypatch, config)
    calls: list[tuple[list[str], dict[str, object]]] = []
    replacement: dict[str, Path] = {}

    def replace_during_open(command: list[str], **kwargs: object) -> object:
        calls.append((command, kwargs))
        replacement["index"] = _replace_report_path_binding(
            output_root=output_root,
            run_dir=run_dir,
            index_path=index_path,
            target=target,
            held_path=tmp_path / f"held-during-launch-{target}",
        )
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(native_host.subprocess, "run", replace_during_open)

    # This deliberately proves fail-after-call detection, not capability-safe
    # GUI handoff in the same-UID spawn window that the contract excludes.
    with pytest.raises(ValueError, match="report identity changed"):
        native_host.open_report("job_123")

    assert calls == [(["open", str(index_path)], {"check": False, "close_fds": True})]
    assert replacement["index"].read_bytes() == REPLACEMENT_INDEX


def test_native_open_report_maps_a_nonzero_opener_exit_to_a_paired_fatal_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ignoring an opener failure would falsely acknowledge a report that never opened."""
    config, output_root = _write_installer_config(tmp_path)
    _write_finished_registry(config, output_root)
    calls: list[tuple[list[str], dict[str, object]]] = []

    def failed_open(command: list[str], **kwargs: object) -> object:
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 9)

    monkeypatch.setattr(native_host.subprocess, "run", failed_open)

    status, responses, _ = _run_host(
        config, [{"protocol_version": "1.0", "kind": "open_report", "job_id": "job_123"}]
    )

    assert status == 1
    assert responses == [
        {
            "protocol_version": "1.0",
            "kind": "error",
            "job_id": "job_123",
            "code": "internal_error",
            "fatal": True,
        }
    ]
    assert len(calls) == 1
    assert calls[0][1] == {"check": False, "close_fds": True}
