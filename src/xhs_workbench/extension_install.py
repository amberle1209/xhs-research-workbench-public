"""Local-only macOS Chrome extension installation and integrity checks."""

from __future__ import annotations

import base64
import ctypes
import errno
import hashlib
import json
import os
import platform
import secrets
import stat
import sys
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

from xhs_workbench.extension_identity import derive_extension_id
from xhs_workbench.path_security import has_acl_allow_entry as _has_acl_allow_entry
from xhs_workbench.path_security import has_acl_allow_entry_fd as _has_acl_allow_entry_fd
from xhs_workbench.path_security import has_extended_acl as _has_extended_acl
from xhs_workbench.path_security import has_extended_acl_fd as _has_extended_acl_fd

EXTENSION_HOST_NAME = "com.xhs_workbench.native_host"
_CONFIG_NAME = "extension.json"
_HOST_MANIFEST_NAME = f"{EXTENSION_HOST_NAME}.json"
_BUNDLE_FILES = (
    "manifest.json",
    "service-worker.js",
    "content-script.js",
    "popup.html",
    "popup.css",
    "popup.js",
)
_CONFIG_MODE = 0o600
_DIRECTORY_MODE = 0o700
_RENAME_EXCL = 0x00000004


class ExtensionInstallError(ValueError):
    """Raised when local installer state is missing, unsafe, or altered."""


@dataclass(frozen=True)
class _InstallPaths:
    home: Path
    config: Path
    bundle: Path
    host_manifest: Path
    shim: Path
    uv_tools: Path


@dataclass(frozen=True)
class _FileBinding:
    """A file identity held across the recoverable uninstall transaction."""

    path: Path
    recovery_name: str
    sha256: str
    identity: tuple[int, int]


@dataclass(frozen=True)
class _HomeBinding:
    """The verified HOME capability used for every installer-owned ancestor."""

    path: Path
    descriptor: int
    identity: tuple[int, int]


@dataclass(frozen=True)
class _DirectoryBinding:
    """A private parent held across an install publication sequence."""

    path: Path
    descriptor: int
    identity: tuple[int, int]
    home: _HomeBinding | None = None


@dataclass(frozen=True)
class _PublishedFile:
    """A newly-published entry that may only be retained, never path-deleted."""

    parent: _DirectoryBinding
    name: str
    path: Path
    sha256: str
    identity: tuple[int, int]
def _home_directory() -> Path:
    return Path.home()


def _platform_system() -> str:
    return platform.system()


def _require_darwin() -> None:
    if _platform_system() != "Darwin":
        raise ExtensionInstallError("Chrome Native Messaging installation is supported only on macOS")


def _paths() -> _InstallPaths:
    home = _home_directory().resolve(strict=True)
    return _InstallPaths(
        home=home,
        config=home / ".config" / "xhs-workbench" / _CONFIG_NAME,
        bundle=home / ".local" / "share" / "xhs-workbench" / "chrome-extension",
        host_manifest=(
            home
            / "Library"
            / "Application Support"
            / "Google"
            / "Chrome"
            / "NativeMessagingHosts"
            / _HOST_MANIFEST_NAME
        ),
        shim=home / ".local" / "bin" / "xhs-workbench-native",
        uv_tools=home / ".local" / "share" / "uv" / "tools",
    )


def build_host_manifest(public_key_der: bytes, native_executable: Path) -> dict[str, object]:
    """Build the exact single-origin Chrome Native Messaging manifest."""
    executable = Path(native_executable)
    if not executable.is_absolute() or ".." in executable.parts:
        raise ExtensionInstallError("host manifest path is unsafe")
    extension_id = derive_extension_id(public_key_der)
    return {
        "name": EXTENSION_HOST_NAME,
        "description": "XHS Research Workbench local import host",
        "path": str(executable),
        "type": "stdio",
        "allowed_origins": [f"chrome-extension://{extension_id}/"],
    }


def validate_host_manifest(
    manifest: object, *, expected_extension_id: str, native_executable: Path
) -> None:
    """Reject all host-manifest variance, including an overly broad caller origin."""
    executable = Path(native_executable)
    expected = {
        "name": EXTENSION_HOST_NAME,
        "description": "XHS Research Workbench local import host",
        "path": str(executable),
        "type": "stdio",
        "allowed_origins": [f"chrome-extension://{expected_extension_id}/"],
    }
    if manifest != expected:
        raise ExtensionInstallError("host manifest identity mismatch")


def install_extension(output_root: Path) -> dict[str, str]:
    """Install an unpacked bundled extension for exactly the current macOS user."""
    _require_darwin()
    paths = _paths()
    output = _validate_output_root(output_root)
    public_key, source_files = _load_packaged_bundle()
    extension_id = derive_extension_id(public_key)
    shim, native_hash = _validate_uv_tool_shim(paths)

    existing = extension_status()
    if existing["status"] == "ready":
        config = _load_config(paths)
        if config["output_root"] == str(output) and config["extension_id"] == extension_id:
            return _installed_result(paths)
        raise ExtensionInstallError("existing installer configuration conflicts")
    if existing["status"] == "invalid" and _refresh_verified_installation(
        paths, output, extension_id, shim, native_hash, source_files
    ):
        return _installed_result(paths)
    if existing["status"] == "invalid" or _any_install_target_exists(paths):
        raise ExtensionInstallError("existing installer files conflict")

    home_parent, bundle_parent, manifest_parent, config_parent, output_parent = _open_install_parents(
        paths, output
    )
    bundle_hashes: dict[str, str] = {}
    created: list[_PublishedFile] = []
    try:
        for name, source in source_files.items():
            destination = paths.bundle / name
            identity = _atomic_create_at(bundle_parent.descriptor, name, source, mode=_CONFIG_MODE)
            digest = _sha256_bytes(source)
            bundle_hashes[name] = digest
            created.append(_PublishedFile(bundle_parent, name, destination, digest, identity))
        _require_directory_inventory(bundle_parent.descriptor, _BUNDLE_FILES)
        host_manifest = build_host_manifest(public_key, shim)
        host_content = _canonical_json(host_manifest)
        identity = _atomic_create_at(
            manifest_parent.descriptor, paths.host_manifest.name, host_content, mode=_CONFIG_MODE
        )
        created.append(
            _PublishedFile(
                manifest_parent,
                paths.host_manifest.name,
                paths.host_manifest,
                _sha256_bytes(host_content),
                identity,
            )
        )
        _assert_directory_binding(output_parent)
        output_stat = os.fstat(output_parent.descriptor)
        config = {
            "allowed_origin": f"chrome-extension://{extension_id}/",
            "output_root": str(output),
            "output_identity": {"device": output_stat.st_dev, "inode": output_stat.st_ino},
            "extension_id": extension_id,
            "native_executable": str(shim),
            "native_executable_sha256": native_hash,
            "bundle_hashes": bundle_hashes,
            "host_manifest_sha256": _sha256_bytes(host_content),
        }
        config_content = _canonical_json(config)
        identity = _atomic_create_at(
            config_parent.descriptor, paths.config.name, config_content, mode=_CONFIG_MODE
        )
        created.append(
            _PublishedFile(
                config_parent,
                paths.config.name,
                paths.config,
                _sha256_bytes(config_content),
                identity,
            )
        )
        _require_directory_inventory(bundle_parent.descriptor, _BUNDLE_FILES)
        for parent in (bundle_parent, manifest_parent, config_parent, output_parent):
            _assert_directory_binding(parent)
    except Exception:
        _remove_new_installation(paths, created)
        raise
    finally:
        for parent in (output_parent, config_parent, manifest_parent, bundle_parent):
            os.close(parent.descriptor)
        os.close(home_parent.descriptor)
    return _installed_result(paths)


def extension_status() -> dict[str, str]:
    """Return finite installation health without opening Chrome or reading a profile."""
    try:
        _require_darwin()
    except ExtensionInstallError:
        return {"status": "unsupported_platform"}
    try:
        paths = _paths()
        if not paths.config.exists() and not paths.config.is_symlink():
            if _any_install_target_exists(paths):
                raise ExtensionInstallError("partial installer state is unsafe")
            return {"status": "not_installed"}
        config = _load_config(paths)
        source_files = _verify_installed_state(paths, config)
    except (OSError, ValueError, TypeError, json.JSONDecodeError, ExtensionInstallError):
        return {"status": "invalid", "error_code": "extension_identity_mismatch"}
    extension_id = config["extension_id"]
    assert isinstance(extension_id, str)
    return {
        "status": "ready",
        "extension_id": extension_id,
        "build_revision": _build_revision(source_files),
    }


def uninstall_extension() -> dict[str, str]:
    """Remove unchanged installer-owned files and never remove output or the uv tool."""
    _require_darwin()
    paths = _paths()
    if extension_status()["status"] != "ready":
        raise ExtensionInstallError("uninstall refused for altered or absent installer files")
    config = _load_config(paths)
    _verify_installed_state(paths, config)
    bindings = _capture_uninstall_bindings(paths, config)
    recovery = _create_recovery_directory(paths.config.parent)
    try:
        try:
            for binding in bindings:
                _move_verified_binding(binding, recovery)
        except (OSError, ExtensionInstallError) as error:
            raise ExtensionInstallError("uninstall retained recoverable files after a binding change") from error
        try:
            _verify_recovery_bindings(bindings, recovery)
        except (OSError, ExtensionInstallError) as error:
            raise ExtensionInstallError("uninstall retained recoverable files after a recovery check") from error
        home_parent = _open_home_binding(paths.home)
        try:
            try:
                bundle_parent = _open_private_directory_binding(paths.bundle.parent, home=home_parent)
                try:
                    _retain_empty_child(bundle_parent, paths.bundle.name)
                finally:
                    os.close(bundle_parent.descriptor)
            except (OSError, ExtensionInstallError):
                # The recovery directory is already verified; a later ancestor
                # rebind leaves empty installer directories as harmless residue.
                pass
        finally:
            os.close(home_parent.descriptor)
    finally:
        os.close(recovery.descriptor)
        if recovery.home is not None:
            os.close(recovery.home.descriptor)
    return {"status": "uninstalled", "recovery_directory": str(recovery.path)}


def _installed_result(paths: _InstallPaths) -> dict[str, str]:
    """Return an installed response only after re-verifying the published bundle."""
    config = _load_config(paths)
    source_files = _verify_installed_state(paths, config)
    extension_id = _required_string(config, "extension_id")
    return {
        "status": "installed",
        "extension_id": extension_id,
        "extension_directory": str(paths.bundle),
        "build_revision": _build_revision(source_files),
    }


def _load_packaged_bundle() -> tuple[bytes, dict[str, bytes]]:
    bundle_resource = resources.files("xhs_workbench").joinpath("extension_bundle")
    with resources.as_file(bundle_resource) as bundle_path:
        bundle = Path(bundle_path)
        _require_regular_directory(bundle, owner=False)
        files: dict[str, bytes] = {}
        for name in _BUNDLE_FILES:
            path = bundle / name
            _require_regular_file(path, owner=False)
            files[name] = path.read_bytes()
    try:
        manifest = json.loads(files["manifest.json"].decode("utf-8"))
        key = manifest["key"]
        if not isinstance(key, str):
            raise TypeError("manifest key is not text")
        public_key = base64.b64decode(key, validate=True)
        derive_extension_id(public_key)
    except (KeyError, TypeError, ValueError, UnicodeDecodeError) as error:
        raise ExtensionInstallError("packaged extension identity is invalid") from error
    return public_key, files


def _validate_output_root(value: Path) -> Path:
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise ExtensionInstallError("output root is unsafe")
    if _contains_symlink(path):
        raise ExtensionInstallError("output root is unsafe")
    try:
        _require_regular_directory(path, owner=True)
    except FileNotFoundError as error:
        raise ExtensionInstallError("output root is unsafe") from error
    if stat.S_IMODE(os.lstat(path).st_mode) & 0o300 != 0o300 or not os.access(path, os.W_OK):
        raise ExtensionInstallError("output root is not writable")
    return path.resolve(strict=True)


def _validate_uv_tool_shim(paths: _InstallPaths) -> tuple[Path, str]:
    shim = paths.shim
    if not shim.is_absolute() or not shim.exists():
        raise ExtensionInstallError("stable uv tool shim is missing")
    shim_info = os.lstat(shim)
    if not (stat.S_ISLNK(shim_info.st_mode) or stat.S_ISREG(shim_info.st_mode)):
        raise ExtensionInstallError("stable uv tool shim is unsafe")
    if shim_info.st_uid != os.getuid():
        raise ExtensionInstallError("stable uv tool shim owner is unsafe")
    _require_home_relative_directory(paths.shim.parent, paths.home)
    expected_tools = paths.home / ".local" / "share" / "uv" / "tools"
    if paths.uv_tools != expected_tools:
        raise ExtensionInstallError("uv tool environment path is unsafe")
    try:
        _require_home_relative_directory(paths.uv_tools, paths.home)
    except OSError as error:
        raise ExtensionInstallError("uv tool environment is missing") from error
    target = shim.resolve(strict=True)
    if not target.is_relative_to(paths.uv_tools):
        raise ExtensionInstallError("native executable is not in the uv tool environment")
    relative_target = target.relative_to(paths.uv_tools)
    if any(part in {".git", ".venv", ".worktrees"} for part in relative_target.parts):
        raise ExtensionInstallError("native executable is in a development environment")
    _require_safe_descendant_directories(target.parent, paths.uv_tools)
    _require_regular_file(target, owner=True)
    if not os.access(target, os.X_OK):
        raise ExtensionInstallError("native executable is not executable")
    return shim, _sha256_file(target)


def _load_config(paths: _InstallPaths) -> dict[str, Any]:
    _require_regular_directory(paths.config.parent, owner=True, exact_mode=_DIRECTORY_MODE)
    _require_regular_file(paths.config, owner=True, exact_mode=_CONFIG_MODE)
    raw = paths.config.read_bytes()
    decoded = json.loads(raw.decode("utf-8"))
    if not isinstance(decoded, dict):
        raise ExtensionInstallError("installer configuration is invalid")
    required = {
        "allowed_origin",
        "output_root",
        "output_identity",
        "extension_id",
        "native_executable",
        "native_executable_sha256",
        "bundle_hashes",
        "host_manifest_sha256",
    }
    if set(decoded) != required:
        raise ExtensionInstallError("installer configuration is invalid")
    if raw != _canonical_json(decoded):
        raise ExtensionInstallError("installer configuration was altered")
    return decoded


def _verify_installed_state(
    paths: _InstallPaths, config: dict[str, Any], *, verify_native_hash: bool = True
) -> dict[str, bytes]:
    public_key, source_files = _load_packaged_bundle()
    extension_id = derive_extension_id(public_key)
    if config["extension_id"] != extension_id:
        raise ExtensionInstallError("extension identity mismatch")
    expected_origin = f"chrome-extension://{extension_id}/"
    if config["allowed_origin"] != expected_origin:
        raise ExtensionInstallError("extension origin mismatch")
    output = _validate_output_root(Path(_required_string(config, "output_root")))
    identity = config["output_identity"]
    if not isinstance(identity, dict) or identity != {
        "device": output.stat().st_dev,
        "inode": output.stat().st_ino,
    }:
        raise ExtensionInstallError("output root identity mismatch")
    shim, native_hash = _validate_uv_tool_shim(paths)
    if _required_string(config, "native_executable") != str(shim):
        raise ExtensionInstallError("native executable identity mismatch")
    if verify_native_hash and config["native_executable_sha256"] != native_hash:
        raise ExtensionInstallError("native executable identity mismatch")
    _require_regular_directory(paths.bundle, owner=True, exact_mode=_DIRECTORY_MODE)
    if {entry.name for entry in paths.bundle.iterdir()} != set(_BUNDLE_FILES):
        raise ExtensionInstallError("installed bundle inventory is invalid")
    hashes = config["bundle_hashes"]
    if not isinstance(hashes, dict) or set(hashes) != set(_BUNDLE_FILES):
        raise ExtensionInstallError("bundle hash inventory is invalid")
    for name, source in source_files.items():
        installed = paths.bundle / name
        if hashes.get(name) != _sha256_bytes(source) or _sha256_file(installed) != hashes[name]:
            raise ExtensionInstallError("installed bundle identity mismatch")
    _require_regular_directory(paths.host_manifest.parent, owner=True)
    _require_regular_file(paths.host_manifest, owner=True)
    host_bytes = paths.host_manifest.read_bytes()
    if config["host_manifest_sha256"] != _sha256_bytes(host_bytes):
        raise ExtensionInstallError("host manifest hash mismatch")
    manifest = json.loads(host_bytes.decode("utf-8"))
    validate_host_manifest(manifest, expected_extension_id=extension_id, native_executable=shim)
    return source_files


def _refresh_verified_installation(
    paths: _InstallPaths,
    output: Path,
    extension_id: str,
    shim: Path,
    native_hash: str,
    source_files: dict[str, bytes],
) -> bool:
    """Refresh a self-recorded release without accepting unrecorded local changes."""
    try:
        config = _load_config(paths)
        if (
            config["output_root"] != str(output)
            or config["extension_id"] != extension_id
            or config["native_executable"] != str(shim)
        ):
            return False
        _verify_upgradeable_installed_state(paths, config, source_files, extension_id, shim)
        refreshed = dict(config)
        refreshed["native_executable_sha256"] = native_hash
        refreshed["bundle_hashes"] = {
            name: _sha256_bytes(source) for name, source in source_files.items()
        }
        home_parent = _open_home_binding(paths.home)
        bundle_parent = _open_private_directory_binding(paths.bundle, home=home_parent)
        config_parent = _open_private_directory_binding(paths.config.parent, home=home_parent)
        try:
            for name, source in source_files.items():
                installed = paths.bundle / name
                if _sha256_file(installed) == _sha256_bytes(source):
                    continue
                expected = os.lstat(name, dir_fd=bundle_parent.descriptor)
                _atomic_replace_at(
                    bundle_parent.descriptor,
                    name,
                    source,
                    mode=_CONFIG_MODE,
                    expected_identity=(expected.st_dev, expected.st_ino),
                )
            _assert_directory_binding(bundle_parent)
            expected = os.lstat(paths.config.name, dir_fd=config_parent.descriptor)
            _require_regular_file(paths.config, owner=True, exact_mode=_CONFIG_MODE)
            _atomic_replace_at(
                config_parent.descriptor,
                paths.config.name,
                _canonical_json(refreshed),
                mode=_CONFIG_MODE,
                expected_identity=(expected.st_dev, expected.st_ino),
            )
            _assert_directory_binding(config_parent)
        finally:
            os.close(config_parent.descriptor)
            os.close(bundle_parent.descriptor)
            os.close(home_parent.descriptor)
        return True
    except (OSError, TypeError, ValueError, json.JSONDecodeError, ExtensionInstallError):
        return False


def _verify_upgradeable_installed_state(
    paths: _InstallPaths,
    config: dict[str, Any],
    source_files: dict[str, bytes],
    extension_id: str,
    shim: Path,
) -> None:
    """Verify a complete, self-recorded prior bundle before replacing it."""
    expected_origin = f"chrome-extension://{extension_id}/"
    if config["extension_id"] != extension_id or config["allowed_origin"] != expected_origin:
        raise ExtensionInstallError("extension identity mismatch")
    output = _validate_output_root(Path(_required_string(config, "output_root")))
    identity = config["output_identity"]
    if not isinstance(identity, dict) or identity != {
        "device": output.stat().st_dev,
        "inode": output.stat().st_ino,
    }:
        raise ExtensionInstallError("output root identity mismatch")
    validated_shim, _ = _validate_uv_tool_shim(paths)
    if validated_shim != shim or _required_string(config, "native_executable") != str(shim):
        raise ExtensionInstallError("native executable identity mismatch")
    _require_regular_directory(paths.bundle, owner=True, exact_mode=_DIRECTORY_MODE)
    if {entry.name for entry in paths.bundle.iterdir()} != set(_BUNDLE_FILES):
        raise ExtensionInstallError("installed bundle inventory is invalid")
    hashes = config["bundle_hashes"]
    if not isinstance(hashes, dict) or set(hashes) != set(_BUNDLE_FILES):
        raise ExtensionInstallError("bundle hash inventory is invalid")
    for name, source in source_files.items():
        recorded = hashes[name]
        if not isinstance(recorded, str) or _sha256_file(paths.bundle / name) not in {
            recorded,
            _sha256_bytes(source),
        }:
            raise ExtensionInstallError("installed bundle identity mismatch")
    _require_regular_directory(paths.host_manifest.parent, owner=True)
    _require_regular_file(paths.host_manifest, owner=True)
    host_bytes = paths.host_manifest.read_bytes()
    if config["host_manifest_sha256"] != _sha256_bytes(host_bytes):
        raise ExtensionInstallError("host manifest hash mismatch")
    manifest = json.loads(host_bytes.decode("utf-8"))
    validate_host_manifest(manifest, expected_extension_id=extension_id, native_executable=shim)


def _required_string(config: dict[str, Any], key: str) -> str:
    value = config.get(key)
    if not isinstance(value, str):
        raise ExtensionInstallError("installer configuration is invalid")
    return value


def _contains_symlink(path: Path) -> bool:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if current.is_symlink():
            return True
    return False


def _ensure_private_directory(path: Path, home: Path) -> None:
    if not path.is_absolute() or not path.is_relative_to(home):
        raise ExtensionInstallError("installer path is outside the current user home")
    current = home
    _require_regular_directory(current, owner=True, allow_restrictive_acl=True)
    for part in path.relative_to(home).parts:
        current /= part
        if current.exists():
            _require_regular_directory(current, owner=True)
        else:
            current.mkdir(mode=_DIRECTORY_MODE)
            _require_regular_directory(current, owner=True, exact_mode=_DIRECTORY_MODE)


def _require_home_relative_directory(path: Path, home: Path) -> None:
    if not path.is_absolute() or not path.is_relative_to(home):
        raise ExtensionInstallError("installer path is outside the current user home")
    _require_regular_directory(home, owner=True, allow_restrictive_acl=True)
    _require_safe_descendant_directories(path, home)


def _require_safe_descendant_directories(path: Path, ancestor: Path) -> None:
    if not path.is_relative_to(ancestor):
        raise ExtensionInstallError("installer path is outside its trusted directory")
    current = ancestor
    for part in path.relative_to(ancestor).parts:
        current /= part
        _require_regular_directory(current, owner=True)


def _require_regular_directory(
    path: Path,
    *,
    owner: bool,
    exact_mode: int | None = None,
    allow_restrictive_acl: bool = False,
) -> None:
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise ExtensionInstallError("installer directory is unsafe")
    if owner and info.st_uid != os.getuid():
        raise ExtensionInstallError("installer directory owner is unsafe")
    if owner:
        has_unsafe_acl = (
            _has_acl_allow_entry(path) if allow_restrictive_acl else _has_extended_acl(path)
        )
        if has_unsafe_acl:
            raise ExtensionInstallError("installer directory ACL is unsafe")
    mode = stat.S_IMODE(info.st_mode)
    if mode & 0o022 or (exact_mode is not None and mode != exact_mode):
        raise ExtensionInstallError("installer directory permissions are unsafe")


def _require_regular_file(path: Path, *, owner: bool, exact_mode: int | None = None) -> None:
    info = os.lstat(path)
    if not stat.S_ISREG(info.st_mode):
        raise ExtensionInstallError("installer file is unsafe")
    if owner and info.st_uid != os.getuid():
        raise ExtensionInstallError("installer file owner is unsafe")
    if owner and _has_extended_acl(path):
        raise ExtensionInstallError("installer file ACL is unsafe")
    mode = stat.S_IMODE(info.st_mode)
    if mode & 0o022 or (exact_mode is not None and mode != exact_mode):
        raise ExtensionInstallError("installer file permissions are unsafe")


def _atomic_create(path: Path, data: bytes, *, mode: int) -> None:
    temporary = path.parent / f".{path.name}.{secrets.token_hex(16)}.tmp"
    descriptor = -1
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
        offset = 0
        while offset < len(data):
            offset += os.write(descriptor, data[offset:])
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.link(temporary, path)
        _sync_directory(path.parent)
    except FileExistsError as error:
        raise ExtensionInstallError("installer path already exists") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _atomic_create_at(directory_fd: int, name: str, data: bytes, *, mode: int) -> tuple[int, int]:
    """Publish one direct child through a held parent descriptor without replacement."""
    if Path(name).name != name:
        raise ExtensionInstallError("installer entry name is unsafe")
    temporary_name = f".{name}.{secrets.token_hex(16)}.tmp"
    descriptor = -1
    try:
        descriptor = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            mode,
            dir_fd=directory_fd,
        )
        offset = 0
        while offset < len(data):
            offset += os.write(descriptor, data[offset:])
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.link(
            temporary_name,
            name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
            follow_symlinks=False,
        )
        published = os.lstat(name, dir_fd=directory_fd)
        if not stat.S_ISREG(published.st_mode) or stat.S_ISLNK(published.st_mode):
            raise ExtensionInstallError("installer publication binding changed")
        _sync_descriptor(directory_fd)
        return published.st_dev, published.st_ino
    except FileExistsError as error:
        raise ExtensionInstallError("installer path already exists") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary_name, dir_fd=directory_fd)
        except FileNotFoundError:
            pass


def _atomic_replace(path: Path, data: bytes, *, mode: int) -> None:
    temporary = path.parent / f".{path.name}.{secrets.token_hex(16)}.tmp"
    descriptor = -1
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
        offset = 0
        while offset < len(data):
            offset += os.write(descriptor, data[offset:])
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        _require_regular_file(path, owner=True, exact_mode=mode)
        os.replace(temporary, path)
        _sync_directory(path.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _atomic_replace_at(
    directory_fd: int,
    name: str,
    data: bytes,
    *,
    mode: int,
    expected_identity: tuple[int, int],
) -> None:
    """Replace a still-bound direct child using the held parent capability."""
    if Path(name).name != name:
        raise ExtensionInstallError("installer entry name is unsafe")
    temporary_name = f".{name}.{secrets.token_hex(16)}.tmp"
    descriptor = -1
    try:
        descriptor = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            mode,
            dir_fd=directory_fd,
        )
        offset = 0
        while offset < len(data):
            offset += os.write(descriptor, data[offset:])
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        current = os.lstat(name, dir_fd=directory_fd)
        if (current.st_dev, current.st_ino) != expected_identity:
            raise ExtensionInstallError("installer file binding changed")
        os.replace(temporary_name, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        _sync_descriptor(directory_fd)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary_name, dir_fd=directory_fd)
        except FileNotFoundError:
            pass


def _sync_directory(path: Path) -> None:
    descriptor = -1
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _any_install_target_exists(paths: _InstallPaths) -> bool:
    if any(
        path.exists() or path.is_symlink()
        for path in (paths.config, paths.host_manifest)
    ):
        return True
    if not paths.bundle.exists() and not paths.bundle.is_symlink():
        return False
    _require_home_relative_directory(paths.bundle, paths.home)
    _require_regular_directory(
        paths.bundle, owner=True, exact_mode=_DIRECTORY_MODE
    )
    return any(paths.bundle.iterdir())


def _require_directory_inventory(
    descriptor: int, expected: tuple[str, ...]
) -> None:
    if set(os.listdir(descriptor)) != set(expected):
        raise ExtensionInstallError("existing installer files conflict")


def _sha256_file(path: Path) -> str:
    _require_regular_file(path, owner=True)
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _build_revision(source_files: dict[str, bytes]) -> str:
    """Return the privacy-safe revision of the verified packaged content script."""
    content_script = source_files.get("content-script.js")
    if set(source_files) != set(_BUNDLE_FILES) or not isinstance(content_script, bytes):
        raise ExtensionInstallError("packaged extension bundle is invalid")
    return _sha256_bytes(content_script)[:12]


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _capture_uninstall_bindings(paths: _InstallPaths, config: dict[str, Any]) -> list[_FileBinding]:
    """Capture every deletion target before making any installed path disappear."""
    hashes = config["bundle_hashes"]
    assert isinstance(hashes, dict)
    return [
        _capture_file_binding(paths.config, "config", expected_hash=None),
        _capture_file_binding(
            paths.host_manifest, "native-host", expected_hash=config["host_manifest_sha256"]
        ),
        *(
            _capture_file_binding(
                paths.bundle / name, f"bundle-{name}", expected_hash=hashes[name]
            )
            for name in _BUNDLE_FILES
        ),
    ]


def _capture_file_binding(
    path: Path, recovery_name: str, *, expected_hash: object
) -> _FileBinding:
    _require_regular_file(path, owner=True)
    info = os.lstat(path)
    digest = _sha256_file(path)
    if expected_hash is not None and (not isinstance(expected_hash, str) or digest != expected_hash):
        raise ExtensionInstallError("installer file was altered")
    final_info = os.lstat(path)
    identity = info.st_dev, info.st_ino
    if (final_info.st_dev, final_info.st_ino) != identity:
        raise ExtensionInstallError("installer file changed while being verified")
    return _FileBinding(
        path=path,
        recovery_name=recovery_name,
        sha256=digest,
        identity=identity,
    )


def _create_recovery_directory(parent: Path) -> _DirectoryBinding:
    home = _home_directory().resolve(strict=True)
    home_binding = _open_home_binding(home)
    try:
        parent_binding = _open_private_directory_binding(parent, home=home_binding)
        try:
            name = f".extension-uninstall-{secrets.token_hex(16)}"
            os.mkdir(name, mode=_DIRECTORY_MODE, dir_fd=parent_binding.descriptor)
            recovery_fd = os.open(
                name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=parent_binding.descriptor,
            )
            try:
                metadata = os.lstat(name, dir_fd=parent_binding.descriptor)
                info = os.fstat(recovery_fd)
                if (metadata.st_dev, metadata.st_ino) != (info.st_dev, info.st_ino):
                    raise ExtensionInstallError("recovery directory binding changed")
                _require_private_directory_descriptor(recovery_fd, exact_mode=_DIRECTORY_MODE)
                return _DirectoryBinding(
                    path=parent / name,
                    descriptor=recovery_fd,
                    identity=(info.st_dev, info.st_ino),
                    home=home_binding,
                )
            except Exception:
                os.close(recovery_fd)
                raise
        finally:
            os.close(parent_binding.descriptor)
    except BaseException:
        os.close(home_binding.descriptor)
        raise


def _move_verified_binding(binding: _FileBinding, recovery: _DirectoryBinding) -> None:
    """Move one still-bound file into private recovery; it is not deleted here."""
    _require_binding(binding, binding.path)
    if recovery.home is None:
        raise ExtensionInstallError("recovery directory is unbound")
    _assert_directory_binding(recovery)
    source_parent = _open_private_directory_binding(binding.path.parent, home=recovery.home)
    try:
        source_metadata = os.lstat(binding.path.name, dir_fd=source_parent.descriptor)
        if (source_metadata.st_dev, source_metadata.st_ino) != binding.identity:
            raise ExtensionInstallError("installer file changed during uninstall")
        try:
            _rename_no_replace(
                source_parent.descriptor,
                binding.path.name,
                recovery.descriptor,
                binding.recovery_name,
            )
        except FileExistsError as error:
            raise ExtensionInstallError("uninstall recovery path already exists") from error
        _assert_directory_binding(source_parent)
        _assert_directory_binding(recovery)
        _sync_descriptor(source_parent.descriptor)
        _sync_descriptor(recovery.descriptor)
    finally:
        os.close(source_parent.descriptor)


def _verify_recovery_bindings(bindings: list[_FileBinding], recovery: _DirectoryBinding) -> None:
    """Confirm all detached files remain intact; retention avoids an unlink race."""
    _assert_directory_binding(recovery)
    for binding in bindings:
        descriptor = os.open(
            binding.recovery_name,
            os.O_RDONLY | os.O_NOFOLLOW,
            dir_fd=recovery.descriptor,
        )
        try:
            info = os.fstat(descriptor)
            metadata = os.lstat(binding.recovery_name, dir_fd=recovery.descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o022
                or (info.st_dev, info.st_ino) != binding.identity
                or (metadata.st_dev, metadata.st_ino) != binding.identity
                or _sha256_descriptor(descriptor) != binding.sha256
            ):
                raise ExtensionInstallError("installer file changed during uninstall")
        finally:
            os.close(descriptor)


def _require_binding(binding: _FileBinding, path: Path) -> None:
    _require_regular_file(path, owner=True)
    info = os.lstat(path)
    if (info.st_dev, info.st_ino) != binding.identity or _sha256_file(path) != binding.sha256:
        raise ExtensionInstallError("installer file changed during uninstall")


def _open_private_directory(path: Path, *, allow_restrictive_acl: bool = False) -> int:
    expected = os.lstat(path)
    _require_regular_directory(
        path, owner=True, allow_restrictive_acl=allow_restrictive_acl
    )
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        actual = os.fstat(descriptor)
        if (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino):
            raise ExtensionInstallError("installer directory binding changed")
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _open_home_binding(path: Path) -> _HomeBinding:
    """Bind the current user's HOME once before traversing installer-owned paths."""
    descriptor = _open_private_directory(path, allow_restrictive_acl=True)
    try:
        _require_private_directory_descriptor(
            descriptor, exact_mode=None, allow_restrictive_acl=True
        )
        info = os.fstat(descriptor)
        return _HomeBinding(
            path=path, descriptor=descriptor, identity=(info.st_dev, info.st_ino)
        )
    except Exception:
        os.close(descriptor)
        raise


def _open_install_parents(
    paths: _InstallPaths, output: Path
) -> tuple[_HomeBinding, _DirectoryBinding, _DirectoryBinding, _DirectoryBinding, _DirectoryBinding]:
    """Open all installation parents or close every predecessor before failing."""
    home = _open_home_binding(paths.home)
    opened: list[_DirectoryBinding] = []
    try:
        for path in (paths.bundle, paths.host_manifest.parent, paths.config.parent):
            binding = _open_private_directory_binding(path, home=home, create=True)
            opened.append(binding)
            if path == paths.bundle:
                _require_directory_inventory(binding.descriptor, ())
        output_parent = _open_private_directory_binding(output)
        return home, opened[0], opened[1], opened[2], output_parent
    except BaseException:
        for binding in reversed(opened):
            try:
                os.close(binding.descriptor)
            except OSError:
                pass
        os.close(home.descriptor)
        raise


def _open_private_directory_binding(
    path: Path, *, home: _HomeBinding | None = None, create: bool = False
) -> _DirectoryBinding:
    """Open a trusted directory, optionally through only a held HOME descriptor."""
    if home is None:
        descriptor = _open_private_directory(path)
        info = os.fstat(descriptor)
        return _DirectoryBinding(path=path, descriptor=descriptor, identity=(info.st_dev, info.st_ino))
    if not path.is_absolute() or not path.is_relative_to(home.path):
        raise ExtensionInstallError("installer path is outside the current user home")
    descriptor = os.dup(home.descriptor)
    created = False
    try:
        _require_private_directory_descriptor(
            descriptor, exact_mode=None, allow_restrictive_acl=True
        )
        components = path.relative_to(home.path).parts
        for index, component in enumerate(components):
            try:
                metadata = os.lstat(component, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(component, mode=_DIRECTORY_MODE, dir_fd=descriptor)
                metadata = os.lstat(component, dir_fd=descriptor)
                created = True
            if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
                raise ExtensionInstallError("installer directory is unsafe")
            next_descriptor = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=descriptor,
            )
            try:
                info = os.fstat(next_descriptor)
                if (metadata.st_dev, metadata.st_ino) != (info.st_dev, info.st_ino):
                    raise ExtensionInstallError("installer directory binding changed")
                _require_private_directory_descriptor(
                    next_descriptor,
                    exact_mode=_DIRECTORY_MODE if created else None,
                    allow_restrictive_acl=not created and index < len(components) - 1,
                )
            except Exception:
                os.close(next_descriptor)
                raise
            os.close(descriptor)
            descriptor = next_descriptor
            created = False
        info = os.fstat(descriptor)
        return _DirectoryBinding(
            path=path,
            descriptor=descriptor,
            identity=(info.st_dev, info.st_ino),
            home=home,
        )
    except Exception:
        os.close(descriptor)
        raise


def _assert_directory_binding(binding: _DirectoryBinding) -> None:
    """Reject a path rebound away from the parent descriptor held for publication."""
    held = os.fstat(binding.descriptor)
    if (held.st_dev, held.st_ino) != binding.identity:
        raise ExtensionInstallError("installer directory binding changed")
    if binding.home is not None:
        current = _open_private_directory_binding(binding.path, home=binding.home)
        try:
            if current.identity != binding.identity:
                raise ExtensionInstallError("installer directory binding changed")
        finally:
            os.close(current.descriptor)
        return
    observed = os.lstat(binding.path)
    _require_regular_directory(binding.path, owner=True)
    if (observed.st_dev, observed.st_ino) != binding.identity:
        raise ExtensionInstallError("installer directory binding changed")


def _require_private_directory_descriptor(
    descriptor: int,
    *,
    exact_mode: int | None,
    allow_restrictive_acl: bool = False,
) -> None:
    """Validate a traversed directory without returning to a mutable pathname."""
    info = os.fstat(descriptor)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise ExtensionInstallError("installer directory owner is unsafe")
    has_unsafe_acl = (
        _has_acl_allow_entry_fd(descriptor)
        if allow_restrictive_acl
        else _has_extended_acl_fd(descriptor)
    )
    if has_unsafe_acl:
        raise ExtensionInstallError("installer directory ACL is unsafe")
    mode = stat.S_IMODE(info.st_mode)
    if mode & 0o022 or (exact_mode is not None and mode != exact_mode):
        raise ExtensionInstallError("installer directory permissions are unsafe")


def _retain_empty_child(parent: _DirectoryBinding, name: str) -> None:
    """Detach a verified empty child without path-rmdir or deleting a rebound name."""
    if Path(name).name != name:
        raise ExtensionInstallError("installer entry name is unsafe")
    _assert_directory_binding(parent)
    metadata = os.lstat(name, dir_fd=parent.descriptor)
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise ExtensionInstallError("installer directory is unsafe")
    child_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent.descriptor)
    try:
        child_info = os.fstat(child_fd)
        if (metadata.st_dev, metadata.st_ino) != (child_info.st_dev, child_info.st_ino):
            raise ExtensionInstallError("installer directory binding changed")
        _require_private_directory_descriptor(child_fd, exact_mode=_DIRECTORY_MODE)
        if os.listdir(child_fd):
            return
    finally:
        os.close(child_fd)
    _rename_no_replace(
        parent.descriptor,
        name,
        parent.descriptor,
        f".extension-uninstall-empty-{secrets.token_hex(16)}",
    )
    _sync_descriptor(parent.descriptor)


def _rename_no_replace(
    source_parent_fd: int,
    source_name: str,
    destination_parent_fd: int,
    destination_name: str,
) -> None:
    """Atomically publish a direct child only when the recovery name is absent."""
    if Path(source_name).name != source_name or Path(destination_name).name != destination_name:
        raise ExtensionInstallError("unsafe recovery entry name")
    if sys.platform != "darwin":
        raise ExtensionInstallError("atomic recovery move is unavailable")
    libc = ctypes.CDLL(None, use_errno=True)
    renameatx_np = getattr(libc, "renameatx_np", None)
    if renameatx_np is None:
        raise ExtensionInstallError("atomic recovery move is unavailable")
    renameatx_np.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameatx_np.restype = ctypes.c_int
    result = renameatx_np(
        source_parent_fd,
        os.fsencode(source_name),
        destination_parent_fd,
        os.fsencode(destination_name),
        _RENAME_EXCL,
    )
    if result != 0:
        error_number = ctypes.get_errno()
        if error_number == errno.EEXIST:
            raise FileExistsError(error_number, os.strerror(error_number), destination_name)
        raise OSError(error_number, os.strerror(error_number), destination_name)


def _sync_descriptor(descriptor: int) -> None:
    try:
        os.fsync(descriptor)
    except OSError:
        pass


def _remove_new_installation(paths: _InstallPaths, created: list[_PublishedFile]) -> None:
    """Retain new artifacts under opaque recovery names instead of unlinking paths."""
    for published in reversed(created):
        try:
            _assert_directory_binding(published.parent)
            descriptor = os.open(
                published.name,
                os.O_RDONLY | os.O_NOFOLLOW,
                dir_fd=published.parent.descriptor,
            )
            try:
                info = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(info.st_mode)
                    or (info.st_dev, info.st_ino) != published.identity
                    or _sha256_descriptor(descriptor) != published.sha256
                ):
                    continue
            finally:
                os.close(descriptor)
            recovery_name = f".extension-install-{secrets.token_hex(16)}"
            _rename_no_replace(
                published.parent.descriptor,
                published.name,
                published.parent.descriptor,
                recovery_name,
            )
            _sync_descriptor(published.parent.descriptor)
        except (OSError, ExtensionInstallError):
            continue


def _sha256_descriptor(descriptor: int) -> str:
    duplicate = os.dup(descriptor)
    try:
        digest = hashlib.sha256()
        while block := os.read(duplicate, 1024 * 1024):
            digest.update(block)
        return digest.hexdigest()
    finally:
        os.close(duplicate)
