"""Installer contract tests for the packaged Chrome extension."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
import tomllib
from pathlib import Path
from typing import Any

import pytest

from xhs_workbench.extension_install import (
    EXTENSION_HOST_NAME,
    ExtensionInstallError,
    build_host_manifest,
    extension_status,
    install_extension,
    uninstall_extension,
    validate_host_manifest,
)

ROOT = Path(__file__).parents[1]
PUBLIC_KEY = b"chrome-extension-public-key-test-vector"
EXPECTED_ID = "fdccfhkafkfpoeejiggchaneihbohfci"


def _use_home(monkeypatch: pytest.MonkeyPatch, home: Path) -> None:
    import xhs_workbench.extension_install as installer

    monkeypatch.setattr(installer, "_home_directory", lambda: home)


def _create_uv_tool(home: Path) -> Path:
    shim = home / ".local" / "bin" / "xhs-workbench-native"
    runtime = home / ".local" / "share" / "uv" / "tools" / "xhs-workbench" / "bin" / "host"
    runtime.parent.mkdir(parents=True, mode=0o700)
    runtime.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    runtime.chmod(0o700)
    shim.parent.mkdir(parents=True, mode=0o700)
    shim.symlink_to(runtime)
    return shim


def _bundle_key() -> bytes:
    manifest = json.loads((ROOT / "src/xhs_workbench/extension_bundle/manifest.json").read_text())
    return base64.b64decode(manifest["key"], validate=True)


def _make_output(tmp_path: Path) -> Path:
    output = tmp_path / "output"
    output.mkdir(mode=0o700)
    return output


def _expected_build_revision() -> str:
    return hashlib.sha256(
        (ROOT / "src/xhs_workbench/extension_bundle/content-script.js").read_bytes()
    ).hexdigest()[:12]


def test_package_and_manifest_release_versions_match_the_extension_release() -> None:
    """A release version mismatch makes an unpacked extension impossible to identify reliably."""
    package = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    manifest = json.loads((ROOT / "chrome_extension/public/manifest.json").read_text(encoding="utf-8"))

    assert package["project"]["version"] == "0.1.13"
    assert manifest["version"] == "0.1.13"


def test_installer_rejects_non_darwin_before_resolving_any_user_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Removing the platform gate would let a non-macOS host mutate an unsupported layout."""
    import xhs_workbench.extension_install as installer

    monkeypatch.setattr(installer, "_platform_system", lambda: "Linux")
    monkeypatch.setattr(
        installer,
        "_paths",
        lambda: (_ for _ in ()).throw(AssertionError("filesystem paths must stay untouched")),
    )

    with pytest.raises(ExtensionInstallError):
        install_extension(tmp_path)
    with pytest.raises(ExtensionInstallError):
        uninstall_extension()
    assert extension_status() == {"status": "unsupported_platform"}


def test_extension_id_uses_chrome_first_sixteen_sha256_bytes() -> None:
    """Replacing Chrome's nibble map would derive a caller origin Chrome never uses."""
    manifest = build_host_manifest(PUBLIC_KEY, Path("/Users/tester/.local/bin/xhs-workbench-native"))

    assert hashlib.sha256(PUBLIC_KEY).hexdigest()[:32] == "532257a05a5fe449866270d4871e7528"
    assert manifest["allowed_origins"] == [f"chrome-extension://{EXPECTED_ID}/"]


def test_host_manifest_is_exact_and_uses_a_stable_absolute_shim() -> None:
    """Changing a host field or accepting a development runtime breaks Chrome's trust boundary."""
    path = Path("/Users/tester/.local/bin/xhs-workbench-native")

    assert build_host_manifest(PUBLIC_KEY, path) == {
        "name": EXTENSION_HOST_NAME,
        "description": "XHS Research Workbench local import host",
        "path": str(path),
        "type": "stdio",
        "allowed_origins": [f"chrome-extension://{EXPECTED_ID}/"],
    }


@pytest.mark.parametrize(
    "manifest",
    [
        {"allowed_origins": ["chrome-extension://abcdefghijklmnopabcdefghijklmnop/", "chrome-extension://bcdefghijklmnopabcdefghijklmnopab/"]},
        {"allowed_origins": ["chrome-extension://*/"]},
        {"path": "relative/native"},
        {"name": "com.other.host"},
        {"type": "tcp"},
    ],
)
def test_host_manifest_rejects_any_origin_or_host_contract_drift(manifest: dict[str, object]) -> None:
    """A permissive manifest would allow an unregistered extension to invoke the native host."""
    expected = build_host_manifest(PUBLIC_KEY, Path("/Users/tester/.local/bin/xhs-workbench-native"))
    expected.update(manifest)

    with pytest.raises(ExtensionInstallError, match="host manifest"):
        validate_host_manifest(
            expected,
            expected_extension_id=EXPECTED_ID,
            native_executable=Path("/Users/tester/.local/bin/xhs-workbench-native"),
        )


def test_host_manifest_rejects_key_identity_drift() -> None:
    """A rotated extension key must not silently retain the old host registration."""
    expected = build_host_manifest(PUBLIC_KEY, Path("/Users/tester/.local/bin/xhs-workbench-native"))

    with pytest.raises(ExtensionInstallError, match="host manifest"):
        validate_host_manifest(
            expected,
            expected_extension_id="a" * 32,
            native_executable=Path("/Users/tester/.local/bin/xhs-workbench-native"),
        )


def test_install_copies_verified_bundle_and_is_idempotent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Skipping installed-file hashing would allow a modified unpacked extension to look healthy."""
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    _use_home(monkeypatch, home)
    shim = _create_uv_tool(home)
    output = _make_output(tmp_path)

    first = install_extension(output)
    second = install_extension(output)
    installed = home / ".local/share/xhs-workbench/chrome-extension"
    host_manifest = home / (
        "Library/Application Support/Google/Chrome/NativeMessagingHosts/"
        "com.xhs_workbench.native_host.json"
    )
    config = home / ".config/xhs-workbench/extension.json"

    assert first["status"] == "installed"
    assert second["status"] == "installed"
    assert first["extension_id"] == second["extension_id"]
    assert first["extension_directory"] == str(installed)
    assert first["build_revision"] == _expected_build_revision()
    assert second["build_revision"] == _expected_build_revision()
    assert json.loads(host_manifest.read_text()) == build_host_manifest(_bundle_key(), shim)
    assert json.loads(config.read_text())["output_root"] == str(output)
    assert stat.S_IMODE(config.stat().st_mode) == 0o600
    assert stat.S_IMODE(installed.stat().st_mode) == 0o700
    assert (installed / "content-script.js").is_file()
    assert extension_status() == {
        "status": "ready",
        "extension_id": first["extension_id"],
        "build_revision": _expected_build_revision(),
    }


def test_install_refreshes_a_verified_same_id_content_script_bundle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A verified prior release must not be stranded after its packaged bundle changes."""
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    _use_home(monkeypatch, home)
    _create_uv_tool(home)
    output = _make_output(tmp_path)
    install_extension(output)

    bundle_file = home / ".local/share/xhs-workbench/chrome-extension/content-script.js"
    prior_content = b"// previously installed release\n"
    bundle_file.write_bytes(prior_content)
    config_path = home / ".config/xhs-workbench/extension.json"
    prior_config = json.loads(config_path.read_text(encoding="utf-8"))
    prior_config["bundle_hashes"]["content-script.js"] = hashlib.sha256(prior_content).hexdigest()
    config_path.write_bytes(json.dumps(prior_config, sort_keys=True, separators=(",", ":")).encode("utf-8"))

    assert extension_status() == {
        "status": "invalid",
        "error_code": "extension_identity_mismatch",
    }

    upgraded = install_extension(output)

    assert upgraded["status"] == "installed"
    assert upgraded["build_revision"] == _expected_build_revision()
    assert bundle_file.read_bytes() == (
        ROOT / "src/xhs_workbench/extension_bundle/content-script.js"
    ).read_bytes()
    assert extension_status() == {
        "status": "ready",
        "extension_id": upgraded["extension_id"],
        "build_revision": _expected_build_revision(),
    }


def test_install_refuses_an_unrecorded_bundle_mutation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An upgrade path must not turn a changed extension file into an accepted release."""
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    _use_home(monkeypatch, home)
    _create_uv_tool(home)
    output = _make_output(tmp_path)
    install_extension(output)

    bundle_file = home / ".local/share/xhs-workbench/chrome-extension/service-worker.js"
    bundle_file.write_text("unrecorded change", encoding="utf-8")

    with pytest.raises(ExtensionInstallError, match="conflict"):
        install_extension(output)


@pytest.mark.parametrize("kind", ["output", "bundle", "config", "host_manifest", "native"])
def test_status_rejects_extended_acls_on_trusted_installer_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, kind: str
) -> None:
    """Mode bits alone must not authorize a path that has a macOS extended ACL."""
    import xhs_workbench.extension_install as installer

    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    _use_home(monkeypatch, home)
    shim = _create_uv_tool(home)
    output = _make_output(tmp_path)
    install_extension(output)
    targets = {
        "output": output,
        "bundle": home / ".local/share/xhs-workbench/chrome-extension",
        "config": home / ".config/xhs-workbench/extension.json",
        "host_manifest": home
        / "Library/Application Support/Google/Chrome/NativeMessagingHosts/com.xhs_workbench.native_host.json",
        "native": shim.resolve(),
    }
    target = targets[kind]
    monkeypatch.setattr(
        installer,
        "_has_extended_acl",
        lambda path: Path(path) == target,
        raising=False,
    )

    assert extension_status() == {"status": "invalid", "error_code": "extension_identity_mismatch"}


def test_install_rejects_an_extended_acl_on_the_output_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A trusted output root must fail closed when ACL inspection reports delegation."""
    import xhs_workbench.extension_install as installer

    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    _use_home(monkeypatch, home)
    _create_uv_tool(home)
    output = _make_output(tmp_path)
    monkeypatch.setattr(
        installer,
        "_has_extended_acl",
        lambda path: Path(path) == output,
        raising=False,
    )

    with pytest.raises(ExtensionInstallError, match="ACL"):
        install_extension(output)


def test_install_allows_a_restrictive_acl_on_the_macos_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The standard macOS HOME deny-delete ACL must not block an otherwise private install."""
    import xhs_workbench.extension_install as installer

    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    home_identity = (home.stat().st_dev, home.stat().st_ino)
    _use_home(monkeypatch, home)
    _create_uv_tool(home)
    output = _make_output(tmp_path)
    monkeypatch.setattr(installer, "_has_extended_acl", lambda path: Path(path) == home)
    monkeypatch.setattr(
        installer,
        "_has_extended_acl_fd",
        lambda descriptor: (os.fstat(descriptor).st_dev, os.fstat(descriptor).st_ino)
        == home_identity,
    )
    monkeypatch.setattr(installer, "_has_acl_allow_entry", lambda path: False, raising=False)
    monkeypatch.setattr(
        installer, "_has_acl_allow_entry_fd", lambda descriptor: False, raising=False
    )

    installed = install_extension(output)

    assert installed["status"] == "installed"
    assert extension_status() == {
        "status": "ready",
        "extension_id": installed["extension_id"],
        "build_revision": _expected_build_revision(),
    }


def test_install_allows_restrictive_acls_on_existing_home_ancestors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Standard deny-delete ACLs on Library ancestors must not block publication."""
    import xhs_workbench.extension_install as installer

    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    application_support = home / "Library/Application Support"
    application_support.mkdir(parents=True, mode=0o700)
    restricted_identities = {
        (path.stat().st_dev, path.stat().st_ino)
        for path in (home / "Library", application_support)
    }
    _use_home(monkeypatch, home)
    _create_uv_tool(home)
    output = _make_output(tmp_path)
    monkeypatch.setattr(installer, "_has_extended_acl", lambda path: False)
    monkeypatch.setattr(
        installer,
        "_has_extended_acl_fd",
        lambda descriptor: (os.fstat(descriptor).st_dev, os.fstat(descriptor).st_ino)
        in restricted_identities,
    )
    monkeypatch.setattr(installer, "_has_acl_allow_entry", lambda path: False)
    monkeypatch.setattr(installer, "_has_acl_allow_entry_fd", lambda descriptor: False)

    installed = install_extension(output)

    assert installed["status"] == "installed"
    assert extension_status() == {
        "status": "ready",
        "extension_id": installed["extension_id"],
        "build_revision": _expected_build_revision(),
    }


def test_install_rejects_an_allow_acl_on_an_existing_home_ancestor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An ancestor ACL that delegates access must remain outside the trusted traversal."""
    import xhs_workbench.extension_install as installer

    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    library = home / "Library"
    library.mkdir(mode=0o700)
    library_identity = (library.stat().st_dev, library.stat().st_ino)
    _use_home(monkeypatch, home)
    _create_uv_tool(home)
    output = _make_output(tmp_path)
    monkeypatch.setattr(installer, "_has_extended_acl", lambda path: False)
    monkeypatch.setattr(installer, "_has_extended_acl_fd", lambda descriptor: False)
    monkeypatch.setattr(installer, "_has_acl_allow_entry", lambda path: False)
    monkeypatch.setattr(
        installer,
        "_has_acl_allow_entry_fd",
        lambda descriptor: (os.fstat(descriptor).st_dev, os.fstat(descriptor).st_ino)
        == library_identity,
    )

    with pytest.raises(ExtensionInstallError, match="ACL"):
        install_extension(output)


def test_install_rejects_an_allow_acl_on_the_macos_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A HOME ACL that grants access must remain a fail-closed installer boundary."""
    import xhs_workbench.extension_install as installer

    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    home_identity = (home.stat().st_dev, home.stat().st_ino)
    _use_home(monkeypatch, home)
    _create_uv_tool(home)
    output = _make_output(tmp_path)
    monkeypatch.setattr(installer, "_has_extended_acl", lambda path: False)
    monkeypatch.setattr(installer, "_has_extended_acl_fd", lambda descriptor: False)
    monkeypatch.setattr(
        installer, "_has_acl_allow_entry", lambda path: Path(path) == home, raising=False
    )
    monkeypatch.setattr(
        installer,
        "_has_acl_allow_entry_fd",
        lambda descriptor: (os.fstat(descriptor).st_dev, os.fstat(descriptor).st_ino)
        == home_identity,
        raising=False,
    )

    with pytest.raises(ExtensionInstallError, match="ACL"):
        install_extension(output)


def test_install_recovers_from_a_safe_empty_bundle_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A failed first publication may leave only its private empty directory behind."""
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    _use_home(monkeypatch, home)
    _create_uv_tool(home)
    output = _make_output(tmp_path)
    bundle = home / ".local/share/xhs-workbench/chrome-extension"
    bundle.mkdir(parents=True, mode=0o700)

    assert extension_status() == {"status": "not_installed"}

    installed = install_extension(output)

    assert installed["status"] == "installed"
    assert extension_status() == {
        "status": "ready",
        "extension_id": installed["extension_id"],
        "build_revision": _expected_build_revision(),
    }


def test_install_rejects_a_nonempty_bundle_without_a_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Unknown content must never be mistaken for a recoverable empty publication residue."""
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    _use_home(monkeypatch, home)
    _create_uv_tool(home)
    output = _make_output(tmp_path)
    bundle = home / ".local/share/xhs-workbench/chrome-extension"
    bundle.mkdir(parents=True, mode=0o700)
    (bundle / "unknown.txt").write_text("untrusted", encoding="utf-8")

    assert extension_status() == {
        "status": "invalid",
        "error_code": "extension_identity_mismatch",
    }
    with pytest.raises(ExtensionInstallError, match="conflict"):
        install_extension(output)


def test_install_rejects_an_unknown_bundle_file_added_during_publication(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A concurrent unknown entry must be caught before an installed result is returned."""
    import xhs_workbench.extension_install as installer

    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    _use_home(monkeypatch, home)
    _create_uv_tool(home)
    output = _make_output(tmp_path)
    bundle = home / ".local/share/xhs-workbench/chrome-extension"
    original_create = installer._atomic_create_at
    injected = False

    def create_and_inject(
        directory_fd: int, name: str, data: bytes, *, mode: int
    ) -> tuple[int, int]:
        nonlocal injected
        identity = original_create(directory_fd, name, data, mode=mode)
        if name == "com.xhs_workbench.native_host.json":
            (bundle / "unknown.txt").write_text("concurrent", encoding="utf-8")
            injected = True
        return identity

    monkeypatch.setattr(installer, "_atomic_create_at", create_and_inject)

    with pytest.raises(ExtensionInstallError, match="conflict"):
        install_extension(output)

    assert injected
    assert (bundle / "unknown.txt").read_text(encoding="utf-8") == "concurrent"
    assert extension_status() == {
        "status": "invalid",
        "error_code": "extension_identity_mismatch",
    }


def test_install_refuses_a_content_script_mutation_after_fresh_publication(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A successful response must not describe a bundle altered after publication completed."""
    import xhs_workbench.extension_install as installer

    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    _use_home(monkeypatch, home)
    _create_uv_tool(home)
    output = _make_output(tmp_path)
    original_load_config = installer._load_config
    mutated = False

    def load_config_then_mutate(paths: Any) -> dict[str, Any]:
        nonlocal mutated
        config = original_load_config(paths)
        if not mutated:
            (paths.bundle / "content-script.js").write_text("post-publication mutation", encoding="utf-8")
            mutated = True
        return config

    monkeypatch.setattr(installer, "_load_config", load_config_then_mutate)

    with pytest.raises(ExtensionInstallError, match="identity mismatch"):
        install_extension(output)

    assert mutated
    assert extension_status() == {
        "status": "invalid",
        "error_code": "extension_identity_mismatch",
    }


def test_install_refuses_a_content_script_mutation_after_verified_refresh(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A refresh cannot report success if its newly published script is replaced before return."""
    import xhs_workbench.extension_install as installer

    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    _use_home(monkeypatch, home)
    _create_uv_tool(home)
    output = _make_output(tmp_path)
    install_extension(output)
    bundle_file = home / ".local/share/xhs-workbench/chrome-extension/content-script.js"
    previous_content = b"// verified previous release\n"
    bundle_file.write_bytes(previous_content)
    config_path = home / ".config/xhs-workbench/extension.json"
    previous_config = json.loads(config_path.read_text(encoding="utf-8"))
    previous_config["bundle_hashes"]["content-script.js"] = hashlib.sha256(
        previous_content
    ).hexdigest()
    config_path.write_bytes(
        json.dumps(previous_config, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    original_replace = installer._atomic_replace_at
    mutated = False

    def replace_then_mutate(
        directory_fd: int,
        name: str,
        data: bytes,
        *,
        mode: int,
        expected_identity: tuple[int, int],
    ) -> None:
        nonlocal mutated
        original_replace(
            directory_fd,
            name,
            data,
            mode=mode,
            expected_identity=expected_identity,
        )
        if name == "extension.json":
            bundle_file.write_text("post-refresh mutation", encoding="utf-8")
            mutated = True

    monkeypatch.setattr(installer, "_atomic_replace_at", replace_then_mutate)

    with pytest.raises(ExtensionInstallError, match="identity mismatch"):
        install_extension(output)

    assert mutated
    assert extension_status() == {
        "status": "invalid",
        "error_code": "extension_identity_mismatch",
    }


def test_install_rejects_config_parent_replacement_after_publication(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A path parent rebound after publication must not receive an installed result."""
    import xhs_workbench.extension_install as installer

    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    _use_home(monkeypatch, home)
    _create_uv_tool(home)
    output = _make_output(tmp_path)
    config_parent = home / ".config/xhs-workbench"
    held_parent = home / "held-config-parent"
    original_link = installer.os.link
    replaced = False

    def replace_parent_after_publish(source: object, destination: object, **kwargs: object) -> None:
        nonlocal replaced
        original_link(source, destination, **kwargs)
        if Path(destination).name == "extension.json" and not replaced:
            replaced = True
            config_parent.rename(held_parent)
            config_parent.mkdir(mode=0o700)

    monkeypatch.setattr(installer.os, "link", replace_parent_after_publish)

    with pytest.raises(ExtensionInstallError, match="binding"):
        install_extension(output)

    assert replaced
    assert (held_parent / "extension.json").is_file()
    assert not (config_parent / "extension.json").exists()


def test_install_rejects_an_ancestor_symlink_replacement_between_traversal_steps(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A verified `.config` ancestor cannot be followed after it is rebound before child creation."""
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    _use_home(monkeypatch, home)
    _create_uv_tool(home)
    output = _make_output(tmp_path)
    config_root = home / ".config"
    held_config_root = home / "held-config"
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    import xhs_workbench.extension_install as installer

    original_mkdir = installer.os.mkdir
    config_root_identity: tuple[int, int] | None = None
    replaced = False

    def replace_ancestor_before_child(name: object, mode: int = 0o777, *, dir_fd: int) -> None:
        nonlocal config_root_identity, replaced
        parent_info = installer.os.fstat(dir_fd)
        if name == ".config" and config_root_identity is None:
            original_mkdir(name, mode=mode, dir_fd=dir_fd)
            created = installer.os.lstat(name, dir_fd=dir_fd)
            config_root_identity = (created.st_dev, created.st_ino)
            return
        if (
            name == "xhs-workbench"
            and (parent_info.st_dev, parent_info.st_ino) == config_root_identity
            and not replaced
        ):
            replaced = True
            config_root.rename(held_config_root)
            config_root.symlink_to(outside, target_is_directory=True)
        original_mkdir(name, mode=mode, dir_fd=dir_fd)

    monkeypatch.setattr(installer.os, "mkdir", replace_ancestor_before_child)

    with pytest.raises(ExtensionInstallError, match="directory|unsafe"):
        install_extension(output)

    assert replaced
    assert not (outside / "xhs-workbench").exists()


def test_uninstall_retains_external_directory_when_cleanup_ancestor_is_replaced(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Post-recovery cleanup must not pathname-rmdir a directory through a rebound ancestor."""
    import xhs_workbench.extension_install as installer

    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    _use_home(monkeypatch, home)
    _create_uv_tool(home)
    output = _make_output(tmp_path)
    install_extension(output)
    bundle_parent = home / ".local/share/xhs-workbench"
    held_bundle_parent = home / "held-bundle-parent"
    outside = tmp_path / "outside"
    external_bundle_parent = outside / "xhs-workbench"
    external_bundle = external_bundle_parent / "chrome-extension"
    external_bundle.mkdir(parents=True, mode=0o700)
    original_verify = installer._verify_recovery_bindings
    replaced = False

    def replace_before_cleanup(bindings: list[Any], recovery: Path) -> None:
        nonlocal replaced
        original_verify(bindings, recovery)
        bundle_parent.rename(held_bundle_parent)
        bundle_parent.symlink_to(external_bundle_parent, target_is_directory=True)
        replaced = True

    monkeypatch.setattr(installer, "_verify_recovery_bindings", replace_before_cleanup)

    result = uninstall_extension()

    assert result["status"] == "uninstalled"
    assert replaced
    assert external_bundle.is_dir()
    assert external_bundle_parent.is_dir()


def test_uninstall_rejects_a_recovery_directory_rebound_after_creation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A same-owner recovery replacement cannot receive verified installer files."""
    import xhs_workbench.extension_install as installer

    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    _use_home(monkeypatch, home)
    _create_uv_tool(home)
    output = _make_output(tmp_path)
    install_extension(output)
    held_recovery = home / "held-recovery"
    original_create = installer._create_recovery_directory
    rebound = False

    def replace_after_create(parent: Path) -> Any:
        nonlocal rebound
        recovery = original_create(parent)
        recovery.path.rename(held_recovery)
        recovery.path.mkdir(mode=0o700)
        rebound = True
        return recovery

    monkeypatch.setattr(installer, "_create_recovery_directory", replace_after_create)

    with pytest.raises(ExtensionInstallError, match="recovery|binding"):
        uninstall_extension()

    assert rebound
    assert held_recovery.is_dir()
    assert (home / ".config/xhs-workbench/extension.json").is_file()


@pytest.mark.parametrize("kind", ["output", "bundle", "config", "host_manifest", "native"])
def test_status_detects_altered_installer_inputs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, kind: str
) -> None:
    """Removing any identity check would report a manually altered installation as ready."""
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    _use_home(monkeypatch, home)
    shim = _create_uv_tool(home)
    output = _make_output(tmp_path)
    install_extension(output)
    targets = {
        "output": output,
        "bundle": home / ".local/share/xhs-workbench/chrome-extension/service-worker.js",
        "config": home / ".config/xhs-workbench/extension.json",
        "host_manifest": home
        / "Library/Application Support/Google/Chrome/NativeMessagingHosts/com.xhs_workbench.native_host.json",
        "native": shim.resolve(),
    }
    target = targets[kind]
    if kind == "output":
        os.chmod(target, 0o777)
    else:
        target.write_text("altered", encoding="utf-8")

    assert extension_status() == {"status": "invalid", "error_code": "extension_identity_mismatch"}


def test_install_rejects_unsafe_output_and_development_or_missing_native_runtime(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Accepting a symlink or worktree executable could make Chrome execute an attacker or deleted checkout."""
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    _use_home(monkeypatch, home)
    output = _make_output(tmp_path)
    symlink_output = tmp_path / "output-link"
    symlink_output.symlink_to(output)

    with pytest.raises(ExtensionInstallError):
        install_extension(symlink_output)
    with pytest.raises(ExtensionInstallError):
        install_extension(output)
    shim = home / ".local/bin/xhs-workbench-native"
    shim.parent.mkdir(parents=True, mode=0o700)
    runtime = tmp_path / "repository" / ".venv" / "bin" / "host"
    runtime.parent.mkdir(parents=True, mode=0o700)
    runtime.write_text("#!/bin/sh\n", encoding="utf-8")
    runtime.chmod(0o700)
    shim.symlink_to(runtime)
    with pytest.raises(ExtensionInstallError):
        install_extension(output)


def test_install_rejects_a_symlinked_uv_tools_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Resolving an intermediate tools symlink could authorize a repository runtime as a uv tool."""
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    _use_home(monkeypatch, home)
    output = _make_output(tmp_path)
    shim = home / ".local/bin/xhs-workbench-native"
    shim.parent.mkdir(parents=True, mode=0o700)
    repository_tools = tmp_path / "repository" / ".venv" / "tools"
    runtime = repository_tools / "xhs-workbench/bin/host"
    runtime.parent.mkdir(parents=True, mode=0o700)
    runtime.write_text("#!/bin/sh\n", encoding="utf-8")
    runtime.chmod(0o700)
    tools_parent = home / ".local/share/uv"
    tools_parent.mkdir(parents=True, mode=0o700)
    (tools_parent / "tools").symlink_to(repository_tools)
    shim.symlink_to(runtime)

    with pytest.raises(ExtensionInstallError):
        install_extension(output)

def test_install_rejects_an_existing_output_root_without_owner_write_permission(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Dropping the write check would defer an unusable output root until a native job runs."""
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    _use_home(monkeypatch, home)
    _create_uv_tool(home)
    output = _make_output(tmp_path)
    output.chmod(0o500)

    with pytest.raises(ExtensionInstallError):
        install_extension(output)


def test_install_rejects_an_effective_filesystem_policy_write_denial(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Mode bits alone cannot prove a user can create a safe result directory."""
    import xhs_workbench.extension_install as installer

    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    _use_home(monkeypatch, home)
    _create_uv_tool(home)
    output = _make_output(tmp_path)
    real_access = os.access
    monkeypatch.setattr(
        installer.os,
        "access",
        lambda path, mode: False if Path(path) == output and mode == os.W_OK else real_access(path, mode),
    )

    with pytest.raises(ExtensionInstallError, match="not writable"):
        install_extension(output)
    assert not (home / ".config/xhs-workbench/extension.json").exists()


def test_install_refreshes_a_verified_uv_tool_upgrade(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Pinning a replaced uv-tool target forever would make a normal tool upgrade unrecoverable."""
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    _use_home(monkeypatch, home)
    shim = _create_uv_tool(home)
    output = _make_output(tmp_path)
    install_extension(output)
    runtime = shim.resolve()
    runtime.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    runtime.chmod(0o700)

    assert extension_status() == {"status": "invalid", "error_code": "extension_identity_mismatch"}
    assert install_extension(output)["status"] == "installed"
    assert extension_status()["status"] == "ready"


def test_uninstall_preserves_output_and_uv_tool_and_refuses_altered_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Blind removal could delete collection evidence or someone else's changed local files."""
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    _use_home(monkeypatch, home)
    shim = _create_uv_tool(home)
    output = _make_output(tmp_path)
    run = output / "immutable-run"
    run.mkdir()
    install_extension(output)
    result = uninstall_extension()

    assert result["status"] == "uninstalled"
    assert Path(result["recovery_directory"]).is_dir()
    assert run.is_dir()
    assert shim.exists()

    install_extension(output)
    altered = home / ".local/share/xhs-workbench/chrome-extension/service-worker.js"
    altered.write_text("altered", encoding="utf-8")
    with pytest.raises(ExtensionInstallError):
        uninstall_extension()
    assert altered.exists()


def test_uninstall_keeps_every_installer_file_recoverable_when_config_is_replaced_mid_transaction(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Deleting an already-checked config after a path swap would destroy a replacement file."""
    import xhs_workbench.extension_install as installer

    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    _use_home(monkeypatch, home)
    _create_uv_tool(home)
    output = _make_output(tmp_path)
    install_extension(output)
    config = home / ".config/xhs-workbench/extension.json"
    held = home / "held-extension.json"
    original_move = installer._move_verified_binding
    replaced = False

    def replace_config_before_move(
        binding: Any, recovery: Path
    ) -> None:
        nonlocal replaced
        if binding.path == config and not replaced:
            replaced = True
            installer.os.rename(config, held)
            config.write_text('{"replacement":true}', encoding="utf-8")
            config.chmod(0o600)
        original_move(binding, recovery)

    monkeypatch.setattr(installer, "_move_verified_binding", replace_config_before_move)

    with pytest.raises(ExtensionInstallError):
        uninstall_extension()

    assert replaced
    assert config.read_text(encoding="utf-8") == '{"replacement":true}'
    assert held.exists()
    assert (home / ".local/share/xhs-workbench/chrome-extension/manifest.json").exists()
    assert (home / ".local/share/xhs-workbench/chrome-extension/service-worker.js").exists()


def test_uninstall_refuses_a_recovery_entry_swap_without_deleting_recoverable_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A pathname unlink after recovery verification could delete a replacement file."""
    import xhs_workbench.extension_install as installer

    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    _use_home(monkeypatch, home)
    _create_uv_tool(home)
    output = _make_output(tmp_path)
    install_extension(output)
    original_verify = installer._verify_recovery_bindings
    held = home / "held-recovery-config"
    swapped = False

    def swap_then_verify(bindings: list[Any], recovery: Any) -> None:
        nonlocal swapped
        config = recovery.path / "config"
        if not swapped:
            swapped = True
            installer.os.rename(config, held)
            config.write_text('{"replacement":true}', encoding="utf-8")
            config.chmod(0o600)
        original_verify(bindings, recovery)

    monkeypatch.setattr(installer, "_verify_recovery_bindings", swap_then_verify)

    with pytest.raises(ExtensionInstallError):
        uninstall_extension()

    assert swapped
    assert held.exists()
    assert any((home / ".config/xhs-workbench").glob(".extension-uninstall-*"))
    assert output.is_dir()


def test_uninstall_never_overwrites_a_recovery_destination_created_before_atomic_move(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Replacing rename would destroy a competing recovery entry created after the path check."""
    import xhs_workbench.extension_install as installer

    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    _use_home(monkeypatch, home)
    _create_uv_tool(home)
    output = _make_output(tmp_path)
    install_extension(output)
    config = home / ".config/xhs-workbench/extension.json"
    created: dict[str, Path] = {}
    original_move = installer._rename_no_replace

    def create_destination_before_publish(
        source_parent_fd: int,
        source_name: str,
        destination_parent_fd: int,
        destination_name: str,
    ) -> None:
        if source_name == "extension.json" and not created:
            recovery = next((home / ".config/xhs-workbench").glob(".extension-uninstall-*"))
            replacement = recovery / destination_name
            replacement.write_text("competing-recovery-entry", encoding="utf-8")
            replacement.chmod(0o600)
            created["destination"] = replacement
        original_move(source_parent_fd, source_name, destination_parent_fd, destination_name)

    monkeypatch.setattr(installer, "_rename_no_replace", create_destination_before_publish)

    with pytest.raises(ExtensionInstallError):
        uninstall_extension()

    assert config.exists()
    assert created["destination"].read_text(encoding="utf-8") == "competing-recovery-entry"


def test_uninstall_closes_source_directory_descriptor_when_recovery_open_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Opening recovery after source must not leak the already-open source descriptor."""
    import xhs_workbench.extension_install as installer

    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    _use_home(monkeypatch, home)
    _create_uv_tool(home)
    output = _make_output(tmp_path)
    install_extension(output)
    original_open = installer._open_private_directory_binding
    original_close = installer.os.close
    opened: list[int] = []
    closed: list[int] = []

    def fail_recovery_open(
        path: Path, *, home: Any = None, create: bool = False
    ) -> Any:
        if path.name.startswith(".extension-uninstall-"):
            raise OSError("injected recovery open failure")
        binding = original_open(path, home=home, create=create)
        opened.append(binding.descriptor)
        return binding

    def record_close(descriptor: int) -> None:
        closed.append(descriptor)
        original_close(descriptor)

    monkeypatch.setattr(installer, "_open_private_directory_binding", fail_recovery_open)
    monkeypatch.setattr(installer.os, "close", record_close)

    with pytest.raises(
        ExtensionInstallError, match="uninstall retained recoverable files after a binding change"
    ):
        uninstall_extension()

    assert opened
    assert opened[0] in closed


def test_uninstall_closes_source_descriptor_when_recovery_close_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Closing recovery must not prevent the source descriptor from being closed."""
    import xhs_workbench.extension_install as installer

    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    _use_home(monkeypatch, home)
    _create_uv_tool(home)
    output = _make_output(tmp_path)
    install_extension(output)
    original_open = installer._open_private_directory_binding
    original_close = installer.os.close
    opened: list[int] = []
    recovery_descriptors: set[int] = set()
    closed: list[int] = []

    def record_open(path: Path, *, home: Any = None, create: bool = False) -> Any:
        binding = original_open(path, home=home, create=create)
        opened.append(binding.descriptor)
        if path.name.startswith(".extension-uninstall-"):
            recovery_descriptors.add(binding.descriptor)
        return binding

    def fail_recovery_close(descriptor: int) -> None:
        closed.append(descriptor)
        original_close(descriptor)
        if descriptor in recovery_descriptors:
            raise OSError("injected recovery close failure")

    monkeypatch.setattr(installer, "_open_private_directory_binding", record_open)
    monkeypatch.setattr(installer.os, "close", fail_recovery_close)

    with pytest.raises(
        ExtensionInstallError, match="uninstall retained recoverable files after a binding change"
    ):
        uninstall_extension()

    assert len(opened) >= 2
    assert opened[0] in closed
