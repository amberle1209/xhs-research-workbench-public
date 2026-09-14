"""Distribution inventory checks for the bundled Chrome extension."""

from __future__ import annotations

import subprocess
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).parents[1]
PACKAGE_FILES = (
    "__init__.py",
    "cli.py",
    "collector.py",
    "extension_install.py",
    "extension_identity.py",
    "extension_import.py",
    "extension_models.py",
    "isolated_login.py",
    "media.py",
    "models.py",
    "native_host.py",
    "native_protocol.py",
    "path_security.py",
    "persistent_client.py",
    "pinned_https.py",
    "renderer.py",
    "video_asr.py",
    "video_asr_guard.py",
    "video_processing.py",
    "security.py",
    "xhs_adapter.py",
    "xhs_bridge.py",
    "extension_bundle/manifest.json",
    "extension_bundle/service-worker.js",
    "extension_bundle/content-script.js",
    "extension_bundle/popup.html",
    "extension_bundle/popup.css",
    "extension_bundle/popup.js",
)
DIST_INFO = "xhs_research_workbench-0.1.13.dist-info"


def test_wheel_and_sdist_match_the_exact_release_inventory_from_dirty_state(tmp_path: Path) -> None:
    sentinels = (
        ROOT / "chrome_extension" / "node_modules" / ".archive-leak-sentinel",
        ROOT / "src" / "xhs_workbench" / ".archive-leak-sentinel.sqlite",
        ROOT / "src" / "xhs_workbench" / ".DS_Store",
    )
    created_directories: list[Path] = []
    created_sentinels: list[Path] = []
    output = tmp_path / "dist"
    try:
        for sentinel in sentinels:
            if _write_sentinel(sentinel, created_directories):
                created_sentinels.append(sentinel)
        subprocess.run(
            ["uv", "build", "--out-dir", str(output)],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
    finally:
        for sentinel in created_sentinels:
            sentinel.unlink()
        for directory in reversed(created_directories):
            directory.rmdir()

    wheel = next(output.glob("*.whl"))
    sdist = next(output.glob("*.tar.gz"))
    with zipfile.ZipFile(wheel) as archive:
        wheel_members = archive.namelist()
    with tarfile.open(sdist, "r:gz") as archive:
        sdist_members = archive.getnames()

    prefix = sdist_members[0].split("/", 1)[0]
    expected_wheel = {f"xhs_workbench/{name}" for name in PACKAGE_FILES} | {
        f"{DIST_INFO}/METADATA",
        f"{DIST_INFO}/WHEEL",
        f"{DIST_INFO}/entry_points.txt",
        f"{DIST_INFO}/RECORD",
    }
    expected_sdist = {f"{prefix}/src/xhs_workbench/{name}" for name in PACKAGE_FILES} | {
        f"{prefix}/.gitignore",
        f"{prefix}/PKG-INFO",
        f"{prefix}/README.md",
        f"{prefix}/DEVELOPMENT.md",
        f"{prefix}/THIRD_PARTY_NOTICES.md",
        f"{prefix}/pyproject.toml",
    }

    assert set(wheel_members) == expected_wheel
    assert set(sdist_members) == expected_sdist


def _write_sentinel(path: Path, created_directories: list[Path]) -> bool:
    if path.exists():
        return False
    missing_directories: list[Path] = []
    parent = path.parent
    while not parent.exists():
        missing_directories.append(parent)
        parent = parent.parent
    for directory in reversed(missing_directories):
        directory.mkdir()
        created_directories.append(directory)
    path.write_text("must never ship", encoding="utf-8")
    return True
