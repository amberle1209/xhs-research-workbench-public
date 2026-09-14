"""Acceptance checks for the macOS package intended for first-time users."""

from __future__ import annotations

import hashlib
import subprocess
import zipfile
from pathlib import Path

ROOT = Path(__file__).parents[1]
RELEASE_ROOT = "XHS Research Workbench"
EXTENSION_FILES = {
    "manifest.json",
    "service-worker.js",
    "content-script.js",
    "popup.html",
    "popup.css",
    "popup.js",
}


def test_macos_user_bundle_contains_a_loadable_extension_and_terminal_instructions(
    tmp_path: Path,
) -> None:
    """A release download must avoid executable scripts blocked by macOS Gatekeeper."""
    wheel_directory = tmp_path / "wheel"
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(wheel_directory)],
        cwd=ROOT,
        check=True,
    )
    wheel = next(wheel_directory.glob("xhs_research_workbench-*-py3-none-any.whl"))
    assert wheel.name == "xhs_research_workbench-0.1.13-py3-none-any.whl"
    bundle = tmp_path / "XHS-Research-Workbench-macOS.zip"

    subprocess.run(
        [
            "python3",
            "scripts/build_macos_user_bundle.py",
            "--wheel",
            str(wheel),
            "--output",
            str(bundle),
        ],
        cwd=ROOT,
        check=True,
    )

    with zipfile.ZipFile(bundle) as archive:
        members = set(archive.namelist())
        extension_prefix = f"{RELEASE_ROOT}/Chrome Extension/"
        extension_members = {
            member.removeprefix(extension_prefix)
            for member in members
            if member.startswith(extension_prefix)
        }
        assert extension_members == EXTENSION_FILES
        assert f"{RELEASE_ROOT}/{wheel.name}" in members
        assert f"{RELEASE_ROOT}/Start Here.txt" in members
        assert not any(member.endswith(".command") for member in members)
        assert archive.read(f"{RELEASE_ROOT}/{wheel.name}") == wheel.read_bytes()

        embedded_wheel = archive.read(f"{RELEASE_ROOT}/{wheel.name}")
        embedded_wheel_path = tmp_path / "embedded.whl"
        embedded_wheel_path.write_bytes(embedded_wheel)
        with zipfile.ZipFile(embedded_wheel_path) as wheel_archive:
            for name in EXTENSION_FILES:
                assert archive.read(f"{extension_prefix}{name}") == wheel_archive.read(
                    f"xhs_workbench/extension_bundle/{name}"
                )

        start_here = archive.read(f"{RELEASE_ROOT}/Start Here.txt").decode("utf-8")
        assert "Chrome Extension" in start_here
        assert "Load unpacked" in start_here
        assert "uv tool install --force './xhs_research_workbench-0.1.13-py3-none-any.whl[video]'" in start_here
        assert "./xhs_research_workbench-*" not in start_here
        assert "~/.local/share/xhs-workbench/chrome-extension" in start_here
        assert "Command + Shift + G" in start_here
        assert "15 分钟" in start_here
        assert "465 MB" in start_here
        assert "10 分钟" in start_here
        assert "xhs-workbench extension-install" in start_here
        assert 'REPORTS_FOLDER="$HOME/Desktop/小红书研究报告"' in start_here
        assert 'mkdir -p "$REPORTS_FOLDER"' in start_here
        assert 'xhs-workbench extension-install --output "$REPORTS_FOLDER"' in start_here
        assert "Documents/XHS Research Workbench Output" not in start_here
        assert "每一行命令的作用" in start_here
        assert "只改 REPORTS_FOLDER 这一行" in start_here
        assert "不要在空格前加入反斜杠" in start_here
        assert "英文半角双引号" in start_here
        assert "完整命令块" in start_here
        assert "`$REPORTS_FOLDER` 是固定变量名" in start_here
        assert "不要写成 `$REPORTS\\_FOLDER`" in start_here
        assert "路径必须是完整路径" in start_here
        assert "xhs-workbench extension-install --output" in start_here
        assert "xhs-workbench extension-status" in start_here
        assert "xhs-workbench extension-uninstall" in start_here
        assert wheel.name in start_here

    assert hashlib.sha256(bundle.read_bytes()).hexdigest()
