"""Offline wheel and unpacked-extension acceptance checks."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import struct
import subprocess
import zipfile
from pathlib import Path

from xhs_workbench import native_host
from xhs_workbench.extension_identity import derive_extension_id

ROOT = Path(__file__).parents[1]
EXTENSION_DIRECTORY = "xhs_workbench/extension_bundle"
ACCEPTANCE_FIXTURES = ROOT / "tests" / "fixtures" / "extension_acceptance"
EXPECTED_BUNDLE_FILES = frozenset(
    {
        "manifest.json",
        "service-worker.js",
        "content-script.js",
        "popup.html",
        "popup.css",
        "popup.js",
    }
)
FORBIDDEN_PERMISSION_APIS = (
    "chrome.cookies",
    "chrome.debugger",
    "chrome.history",
    "chrome.webRequest",
    "chrome.declarativeNetRequest",
    "chrome.downloads",
    "localStorage",
)
FORBIDDEN_BUNDLE_MARKERS = (
    "sourceMappingURL",
    "eval(",
    "xsec_token",
    "Safe Storage",
    "-----BEGIN",
    "xhs_adapter",
    "xhs_bridge",
    "persistent_client",
    "isolated_login",
)


def _build_wheel(tmp_path: Path) -> Path:
    """Build the local assets and wheel exactly as the release gate does."""
    subprocess.run(["npm", "run", "build"], cwd=ROOT / "chrome_extension", check=True)
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(tmp_path)],
        cwd=ROOT,
        check=True,
    )
    wheels = list(tmp_path.glob("xhs_research_workbench-0.1.13-py3-none-any.whl"))
    assert wheels, "wheel build did not produce the declared release artifact"
    assert len(wheels) == 1
    return wheels[0]


def _frame(message: dict[str, object]) -> bytes:
    payload = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return struct.pack("@I", len(payload)) + payload


def _responses(raw: bytes) -> list[dict[str, object]]:
    parsed: list[dict[str, object]] = []
    offset = 0
    while offset < len(raw):
        size = struct.unpack("@I", raw[offset : offset + 4])[0]
        offset += 4
        parsed.append(json.loads(raw[offset : offset + size].decode("utf-8")))
        offset += size
    return parsed


def _fixture_messages(name: str) -> list[dict[str, object]]:
    raw = json.loads((ACCEPTANCE_FIXTURES / name).read_text(encoding="utf-8"))
    assert isinstance(raw, list)
    assert all(isinstance(message, dict) for message in raw)
    return raw


def _verify_typescript_acceptance_traces() -> None:
    """Reject drift before Python consumes production-controller message traces."""
    subprocess.run(
        ["npm", "run", "verify:acceptance-traces"],
        cwd=ROOT / "chrome_extension",
        check=True,
    )


def _run_fixture_job(tmp_path: Path, name: str) -> tuple[list[dict[str, object]], Path]:
    output_root = tmp_path / "reports"
    output_root.mkdir(parents=True, mode=0o700)
    config = tmp_path / "extension.json"
    config.write_text(
        json.dumps(
            {
                "allowed_origin": "chrome-extension://abcdefghijklmnopabcdefghijklmnop/",
                "output_root": str(output_root),
            }
        ),
        encoding="utf-8",
    )
    os.chmod(config, 0o600)
    output = io.BytesIO()
    status = native_host.run_native_host(
        "chrome-extension://abcdefghijklmnopabcdefghijklmnop/",
        config_path=config,
        input_stream=io.BytesIO(b"".join(_frame(message) for message in _fixture_messages(name))),
        output_stream=output,
    )
    assert status == 0
    return _responses(output.getvalue()), output_root


def _result_directory(output_root: Path, job_id: str) -> Path:
    registry = output_root.parent / hashlib.sha256(job_id.encode("utf-8")).hexdigest()
    run_id = json.loads(registry.read_text(encoding="utf-8"))["run_id"]
    assert isinstance(run_id, str)
    return output_root / run_id


def test_wheel_contains_exact_safe_unpacked_extension_bundle(tmp_path: Path) -> None:
    """Removing a runtime asset or broadening a capability must fail release acceptance."""
    wheel = _build_wheel(tmp_path)

    with zipfile.ZipFile(wheel) as archive:
        extension_entries = {
            name.removeprefix(f"{EXTENSION_DIRECTORY}/")
            for name in archive.namelist()
            if name.startswith(f"{EXTENSION_DIRECTORY}/")
        }
        assert extension_entries == EXPECTED_BUNDLE_FILES
        contents = {
            name: archive.read(f"{EXTENSION_DIRECTORY}/{name}").decode("utf-8")
            for name in EXPECTED_BUNDLE_FILES
        }

    manifest = json.loads(contents["manifest.json"])
    public_key = base64.b64decode(manifest["key"], validate=True)
    assert derive_extension_id(public_key) == "ohcadfmflnjoofmimlidfgnehpkfoofg"
    assert manifest["permissions"] == ["activeTab", "scripting", "storage", "nativeMessaging"]
    assert manifest["optional_host_permissions"] == [
        "https://www.xiaohongshu.com/*",
        "https://*.xhscdn.com/*",
    ]

    rendered_bundle = "\n".join(contents.values())
    assert ".map" not in extension_entries
    assert not any(marker in rendered_bundle for marker in FORBIDDEN_BUNDLE_MARKERS)
    assert not any(api in rendered_bundle for api in FORBIDDEN_PERMISSION_APIS)
    assert "https://www.xiaohongshu.com/search_result" in rendered_bundle
    assert "https://*.xhscdn.com/*" in rendered_bundle
    assert not any(
        forbidden in archive.namelist()
        for forbidden in (".DS_Store", "node_modules", "tests", "fixtures", "private")
    )


def test_cross_language_fixture_jobs_preserve_selection_media_and_local_only_reports(
    tmp_path: Path,
) -> None:
    """Verified production-controller traces exercise native persistence without a browser or profile."""
    _verify_typescript_acceptance_traces()
    current_responses, current_root = _run_fixture_job(
        tmp_path / "current", "current_note_image_messages.json"
    )
    assert [response["kind"] for response in current_responses] == [
        "job_started",
        "candidate_result",
        "selection_result",
        "progress",
        "progress",
        "media_result",
        "job_result",
    ]
    current_directory = _result_directory(current_root, "acceptance_current")
    current_result = json.loads((current_directory / "results.json").read_text(encoding="utf-8"))
    current_media = current_result["notes"][0]["media_slots"][0]["asset"]
    assert current_responses[-1]["status"] == "complete"
    assert current_media["local_path"].startswith("assets/")
    assert current_media["sha256"] == "f26b33c2b0294d41989d6653fd81181b689b549e4c7473ca31f65db738e72c1e"
    assert (current_directory / current_media["local_path"]).is_file()

    batch_responses, batch_root = _run_fixture_job(
        tmp_path / "batch", "batch_ten_candidates_messages.json"
    )
    selection = next(response for response in batch_responses if response["kind"] == "selection_result")
    assert selection["status"] == "partial"
    assert selection["selected"] == [
        {"note_id": "note_06", "selection_rank": 1},
        {"note_id": "note_08", "selection_rank": 2},
        {"note_id": "note_07", "selection_rank": 3},
    ]
    assert batch_responses[-1]["status"] == "partial"
    assert [response["kind"] for response in batch_responses[-4:-1]] == [
        "progress",
        "progress",
        "media_result",
    ]

    batch_directory = _result_directory(batch_root, "acceptance_batch")
    batch_result = json.loads((batch_directory / "results.json").read_text(encoding="utf-8"))
    ledger = batch_result["extension_selection"]["entries"]
    assert [(entry["note_id"], entry["reason"]) for entry in ledger if entry["reason"] is not None] == [
        ("note_01", "likes_not_exact"),
        ("note_02", "likes_unavailable"),
        ("note_03", "publication_time_unavailable"),
        ("note_04", "publication_time_unavailable"),
        ("note_05", "likes_not_exact"),
        ("note_09", "likes_unavailable"),
        ("note_10", "publication_time_unavailable"),
    ]
    assert [(note["note_id"], note["selection_rank"]) for note in batch_result["notes"]] == [
        ("note_06", 1),
        ("note_08", 2),
        ("note_07", 3),
    ]
    selected_media = batch_result["notes"][0]["media_slots"][0]["asset"]
    assert selected_media["local_path"].startswith("assets/")
    assert selected_media["sha256"] == "f26b33c2b0294d41989d6653fd81181b689b549e4c7473ca31f65db738e72c1e"
    assert (batch_directory / selected_media["local_path"]).is_file()

    report = (batch_directory / "index.html").read_text(encoding="utf-8")
    assert report.index("note_06") < report.index("note_08") < report.index("note_07")
    assert "按精确点赞排序，仅来自已检查候选" in report
    retained = (batch_directory / "results.json").read_text(encoding="utf-8") + report
    assert not any(marker in retained for marker in ("?", "xsec_token", "Safe Storage", "Authorization"))
    assert "xhscdn.com" not in retained
