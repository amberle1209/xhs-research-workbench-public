"""Fail-closed parent-process adapter for the isolated bridge."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import signal
import stat
import subprocess
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from xhs_workbench.media import (
    IMAGE_MIME_TYPES,
    MAX_DISCOVERED_IMAGE_SLOTS,
    MAX_IMAGE_BYTES,
    MAX_IMAGES_PER_NOTE,
    MAX_NOTE_MEDIA_BYTES,
    MAX_RUN_MEDIA_BYTES,
    MAX_VIDEO_BYTES,
    NOTE_MEDIA_BUDGET_SECONDS,
    VIDEO_MIME_TYPES,
    mime_extension,
    sniff_media_mime,
    validate_media_container,
)
from xhs_workbench.security import sanitize_evidence
from xhs_workbench.xhs_bridge import (
    BridgeErrorCode,
    BridgeRequest,
    BridgeResponse,
    is_safe_retained_text,
    validate_bridge_response,
)

_BRIDGE_ARGV_PREFIX = (
    "uv",
    "run",
    "--isolated",
)
_MAX_PROTOCOL_BYTES = 1024 * 1024
_BRIDGE_TIMEOUT_SECONDS = 30
_LOGIN_TIMEOUT_SECONDS = 300
_PAGE_NAVIGATION_TIMEOUT_SECONDS = 20
_PAGE_DATA_TIMEOUT_SECONDS = 15
_PAGE_SETTLE_TIMEOUT_SECONDS = 4
_PAGE_READ_TIMEOUT_SECONDS = (
    _PAGE_NAVIGATION_TIMEOUT_SECONDS + _PAGE_DATA_TIMEOUT_SECONDS + _PAGE_SETTLE_TIMEOUT_SECONDS
)
_DETAIL_READ_TIMEOUT_SECONDS = _PAGE_READ_TIMEOUT_SECONDS + NOTE_MEDIA_BUDGET_SECONDS
_ACCOUNT_DOM_CARD_DETAIL_TIMEOUT_SECONDS = (
    _PAGE_NAVIGATION_TIMEOUT_SECONDS  # Return to the verified profile.
    + _PAGE_SETTLE_TIMEOUT_SECONDS
    + _PAGE_NAVIGATION_TIMEOUT_SECONDS  # Resolve the exact visible card.
    + _PAGE_NAVIGATION_TIMEOUT_SECONDS  # Wait for the exact card's detail page.
    + _PAGE_SETTLE_TIMEOUT_SECONDS
    + NOTE_MEDIA_BUDGET_SECONDS
)
_SEARCH_BASE_TIMEOUT_SECONDS = _PAGE_READ_TIMEOUT_SECONDS
_ACCOUNT_BASE_TIMEOUT_SECONDS = (
    _PAGE_READ_TIMEOUT_SECONDS  # Profile page.
    + 15  # Profile avatar request.
    + _PAGE_READ_TIMEOUT_SECONDS  # Posts page.
)
_MAX_SEARCH_TIMEOUT_SECONDS = _SEARCH_BASE_TIMEOUT_SECONDS + 10 * _DETAIL_READ_TIMEOUT_SECONDS
_MAX_ACCOUNT_TIMEOUT_SECONDS = (
    _ACCOUNT_BASE_TIMEOUT_SECONDS + 5 + 10 * (_ACCOUNT_DOM_CARD_DETAIL_TIMEOUT_SECONDS + 7)
)
_PROCESS_GROUP_GRACE_SECONDS = 2.0
_Runner = Callable[..., object]


class _ProfileBusy(Exception):
    """The home-owned browser profile already has one active owner."""


@contextmanager
def _exclusive_profile_lock(auth_dir: Path) -> Iterator[None]:
    """Exclusively own a prepared profile directory without following a lock symlink."""
    lock_path = auth_dir / ".xhs-workbench.lock"
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
            raise ValueError("unsafe profile lock")
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise _ProfileBusy from exc
        try:
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


class IsolatedXhsAdapter:
    """The only main-process caller for the optional upstream environment."""

    def __init__(self, *, runner: _Runner | None = None, auth_dir: Path | None = None) -> None:
        self._runner = runner
        self._auth_dir = _prepare_directory(
            auth_dir if auth_dir is not None else Path.home() / ".local" / "auth", mode=0o700
        )

    def collect_search(self, keyword: str, limit: int, asset_staging_dir: Path) -> BridgeResponse:
        """Collect a first-page keyword snapshot through the bridge."""
        staging_dir = _prepare_directory(asset_staging_dir)
        return self._invoke(
            BridgeRequest(operation="search", keyword=keyword, limit=limit),
            staging_dir=staging_dir,
        )

    def collect_account(
        self, account_id: str, limit: int, asset_staging_dir: Path
    ) -> BridgeResponse:
        """Collect one account's profile and first page through the bridge."""
        staging_dir = _prepare_directory(asset_staging_dir)
        return self._invoke(
            BridgeRequest(operation="account", account_id=account_id, limit=limit),
            staging_dir=staging_dir,
        )

    def auth_status(self) -> BridgeResponse:
        """Report only the bridge's safe authentication state."""
        return self._invoke(BridgeRequest(operation="status"), staging_dir=None)

    def login(self) -> BridgeResponse:
        """Request browser-assisted authentication without returning browser data."""
        return self._invoke(BridgeRequest(operation="login"), staging_dir=None)

    def _invoke(self, request: BridgeRequest, *, staging_dir: Path | None) -> BridgeResponse:
        try:
            with _exclusive_profile_lock(self._auth_dir):
                environment = {
                    "PATH": os.environ.get("PATH", ""),
                    "XHS_WORKBENCH_AUTH_DIR": str(self._auth_dir),
                }
                if staging_dir is not None:
                    environment["XHS_WORKBENCH_ASSET_STAGING_DIR"] = str(staging_dir)
                payload = request.model_dump_json(exclude_none=True).encode("utf-8")
                timeout = _timeout_for(request)
                argv = _bridge_argv()
                result = (
                    _run_default_bridge(
                        argv,
                        payload=payload,
                        timeout=timeout,
                        environment=environment,
                    )
                    if self._runner is None
                    else self._runner(
                        argv,
                        input=payload,
                        capture_output=True,
                        timeout=timeout,
                        text=False,
                        env=environment,
                    )
                )
                return self._parse_result(result, request, staging_dir)
        except _ProfileBusy:
            return _failed("profile_busy")
        except subprocess.TimeoutExpired:
            return _failed("bridge_timeout")
        except Exception:  # noqa: BLE001 - process creation must fail closed.
            return _failed("bridge_launch_failed")

    def _parse_result(
        self, result: object, request: BridgeRequest, staging_dir: Path | None
    ) -> BridgeResponse:
        return_code = getattr(result, "returncode", None)
        stdout = getattr(result, "stdout", b"")
        if return_code != 0:
            return _failed("bridge_nonzero_exit")
        if not isinstance(stdout, bytes) or not stdout or len(stdout) > _MAX_PROTOCOL_BYTES:
            return _failed("invalid_bridge_output")
        try:
            decoded = json.loads(
                stdout,
                object_pairs_hook=_reject_duplicate_pairs,
                parse_constant=_reject_constant,
            )
            if (
                type(decoded) is not dict
                or sanitize_evidence(decoded) != decoded
                or _contains_concealed_sensitive_value(decoded)
            ):
                return _failed("unsafe_bridge_response")
            raw_media_error = _raw_media_manifest_error(decoded, request)
            if raw_media_error is not None:
                return _failed(raw_media_error)
            response = validate_bridge_response(request, decoded)
        except Exception:  # noqa: BLE001 - malformed child output is untrusted.
            return _failed("invalid_bridge_output")
        if response.status == "failed":
            return BridgeResponse(status="failed", payload={}, error_code=response.error_code)
        if staging_dir is not None and not _validate_staged_assets(response.payload, staging_dir):
            return _failed("unsafe_staging_asset")
        return response


def _bridge_argv() -> tuple[str, ...]:
    project_dir = _loaded_project_dir()
    return _BRIDGE_ARGV_PREFIX + (
        "--project",
        str(project_dir),
        "--extra",
        "upstream",
        "python",
        "-m",
        "xhs_workbench.xhs_bridge",
    )


def _loaded_project_dir() -> Path:
    source_file = Path(__file__).resolve()
    project_dir = source_file.parents[2]
    package_dir = (project_dir / "src" / "xhs_workbench").resolve()
    if source_file.parent != package_dir or not (project_dir / "pyproject.toml").is_file():
        raise RuntimeError("loaded project is unavailable")
    return project_dir


def _prepare_directory(path: Path, *, mode: int = 0o700) -> Path:
    """Create one non-symlinked directory after rejecting explicit traversal."""
    if ".." in path.parts:
        raise ValueError("directory traversal is not allowed")
    if _has_symlink_component(path):
        raise ValueError("symbolic links are not allowed")
    path.mkdir(parents=True, exist_ok=True, mode=mode)
    if _has_symlink_component(path):
        raise ValueError("symbolic links are not allowed")
    resolved = path.resolve(strict=True)
    if not resolved.is_dir():
        raise ValueError("path is not a directory")
    resolved.chmod(mode)
    return resolved


def _has_symlink_component(path: Path) -> bool:
    current = Path(path.anchor) if path.is_absolute() else Path.cwd()
    for part in path.parts:
        if part in {path.anchor, "."}:
            continue
        current /= part
        if current.is_symlink():
            return True
    return False


def _validate_staged_assets(payload: dict[str, object], staging_dir: Path) -> bool:
    notes = payload.get("notes", [])
    if not isinstance(notes, list):
        return False
    run_media_bytes = 0
    for note in notes:
        if not isinstance(note, dict):
            return False
        note_id = note.get("note_id")
        discovered_count = note.get("media_discovered_count")
        discovery_truncated = note.get("media_discovery_truncated")
        candidates = note.get("media_candidates")
        if (
            type(note.get("media_manifest_version")) is not int
            or note.get("media_manifest_version") != 2
            or not isinstance(note_id, str)
            or type(discovered_count) is not int
            or discovered_count < 0
            or type(discovery_truncated) is not bool
            or not isinstance(candidates, list)
        ):
            return False
        validated_slots: list[tuple[int, str, int, dict[str, object]]] = []
        for candidate in candidates:
            if not isinstance(candidate, dict):
                return False
            validated = _validate_staged_media(candidate, note_id, staging_dir)
            if validated is None:
                return False
            position, role, size_bytes = validated
            validated_slots.append((position, role, size_bytes, candidate))
        note_media_bytes = _validate_note_media_slots(
            validated_slots, discovered_count, discovery_truncated
        )
        if note_media_bytes is None:
            return False
        run_media_bytes += note_media_bytes
        if run_media_bytes > MAX_RUN_MEDIA_BYTES:
            return False
        cover = note.get("cover")
        if cover is not None and (not isinstance(cover, dict) or not _valid_cover(cover, staging_dir)):
            return False
    avatar = payload.get("avatar")
    if avatar is None:
        return True
    if not isinstance(avatar, dict) or not _valid_cover(avatar, staging_dir):
        return False
    avatar_size = avatar.get("size_bytes")
    if type(avatar_size) is not int:
        return False
    return run_media_bytes + avatar_size <= MAX_RUN_MEDIA_BYTES


def _raw_media_manifest_error(decoded: dict[str, object], request: BridgeRequest) -> str | None:
    """Check exact child JSON types before Pydantic can normalize them."""
    if request.operation not in {"search", "account"} or decoded.get("status") == "failed":
        return None
    payload = decoded.get("payload")
    if not isinstance(payload, dict):
        return "invalid_bridge_output"
    notes = payload.get("notes")
    if not isinstance(notes, list):
        return "invalid_bridge_output"
    for note in notes:
        if not isinstance(note, dict):
            return "invalid_bridge_output"
        if (
            type(note.get("media_manifest_version")) is not int
            or note.get("media_manifest_version") != 2
            or type(note.get("media_discovered_count")) is not int
            or note["media_discovered_count"] < 0
            or type(note.get("media_discovery_truncated")) is not bool
            or not isinstance(note.get("media_candidates"), list)
        ):
            return "invalid_bridge_output"
        note_id = note.get("note_id")
        if not isinstance(note_id, str):
            return "invalid_bridge_output"
        candidates = note["media_candidates"]
        assert isinstance(candidates, list)
        for candidate in candidates:
            if not isinstance(candidate, dict):
                return "invalid_bridge_output"
            if not _raw_staged_candidate_is_safe(candidate, note_id):
                return "unsafe_staging_asset"
        if not _raw_image_slot_range_is_complete(
            candidates,
            note["media_discovered_count"],
            note["media_discovery_truncated"],
        ):
            return "unsafe_staging_asset"
    return None


def _raw_staged_candidate_is_safe(candidate: dict[str, object], note_id: str) -> bool:
    """Reject unsafe slot/file pairings without opening the staging directory."""
    role = candidate.get("role")
    position = candidate.get("position")
    status = candidate.get("status")
    if (
        candidate.get("note_id") != note_id
        or role not in {"image", "video_cover", "video"}
        or type(position) is not int
        or position < 1
        or status not in {"downloaded", "missing", "rejected"}
    ):
        return False
    file_fields = ("staging_name", "mime_type", "size_bytes", "sha256")
    if status != "downloaded":
        return (
            all(field not in candidate for field in file_fields)
            and isinstance(candidate.get("missing_reason"), str)
        )
    if "missing_reason" in candidate or any(field not in candidate for field in file_fields):
        return False
    staging_name = candidate.get("staging_name")
    mime_type = candidate.get("mime_type")
    size_bytes = candidate.get("size_bytes")
    sha256 = candidate.get("sha256")
    if (
        not isinstance(staging_name, str)
        or Path(staging_name).name != staging_name
        or not isinstance(mime_type, str)
        or type(size_bytes) is not int
        or size_bytes <= 0
        or not _is_sha256(sha256)
    ):
        return False
    if role == "video":
        if mime_type not in VIDEO_MIME_TYPES or size_bytes > MAX_VIDEO_BYTES:
            return False
    elif mime_type not in IMAGE_MIME_TYPES or size_bytes > MAX_IMAGE_BYTES:
        return False
    extension = mime_extension(mime_type)
    if extension is None:
        return False
    expected_name = (
        f"{note_id}-image-{position:03d}{extension}"
        if role == "image"
        else f"{note_id}-{role.replace('_', '-')}{extension}"
    )
    return staging_name == expected_name


def _raw_image_slot_range_is_complete(
    candidates: list[object], discovered_count: int, discovery_truncated: bool
) -> bool:
    """Bind the child ledger to every represented image position before disk access."""
    image_positions = [
        candidate["position"]
        for candidate in candidates
        if isinstance(candidate, dict) and candidate.get("role") == "image"
    ]
    represented_count = min(discovered_count, MAX_DISCOVERED_IMAGE_SLOTS)
    return (
        image_positions == list(range(1, represented_count + 1))
        and discovery_truncated == (discovered_count > MAX_DISCOVERED_IMAGE_SLOTS)
    )


def _validate_note_media_slots(
    slots: list[tuple[int, str, int, dict[str, object]]],
    discovered_count: int,
    discovery_truncated: bool,
) -> int | None:
    """Check duplicate/order/limit invariants independently of child models."""
    seen: set[tuple[str, int]] = set()
    by_role: dict[str, list[tuple[int, int, dict[str, object]]]] = {
        "image": [],
        "video_cover": [],
        "video": [],
    }
    downloaded_images = 0
    media_bytes = 0
    for position, role, size_bytes, candidate in slots:
        key = (role, position)
        if key in seen or role not in by_role:
            return None
        seen.add(key)
        by_role[role].append((position, size_bytes, candidate))
        if size_bytes:
            media_bytes += size_bytes
            if role == "image":
                downloaded_images += 1
    image_slots = by_role["image"]
    if (
        len(image_slots) > MAX_DISCOVERED_IMAGE_SLOTS
        or downloaded_images > MAX_IMAGES_PER_NOTE
        or len(by_role["video"]) > 1
        or len(by_role["video_cover"]) > 1
        or media_bytes > MAX_NOTE_MEDIA_BYTES
        or (image_slots and (by_role["video"] or by_role["video_cover"]))
    ):
        return None
    for role, role_slots in by_role.items():
        positions = sorted(position for position, _, _ in role_slots)
        if positions != list(range(1, len(positions) + 1)):
            return None
        if role == "image":
            for position, _, candidate in role_slots:
                if position > MAX_IMAGES_PER_NOTE and not (
                    candidate.get("status") == "rejected"
                    and candidate.get("missing_reason") == "slot_limit"
                ):
                    return None
    expected_image_positions = list(
        range(1, min(discovered_count, MAX_DISCOVERED_IMAGE_SLOTS) + 1)
    )
    if (
        sorted(position for position, _, _ in image_slots) != expected_image_positions
        or discovery_truncated != (discovered_count > MAX_DISCOVERED_IMAGE_SLOTS)
    ):
        return None
    return media_bytes


def _validate_staged_media(
    candidate: dict[str, object], note_id: str, staging_dir: Path
) -> tuple[int, str, int] | None:
    """Revalidate one untrusted staged slot through no-follow descriptors."""
    if not _raw_staged_candidate_is_safe(candidate, note_id):
        return None
    role = candidate.get("role")
    position = candidate.get("position")
    status = candidate.get("status")
    assert isinstance(role, str) and type(position) is int and isinstance(status, str)
    if status != "downloaded":
        return position, role, 0
    staging_name = candidate.get("staging_name")
    size_bytes = candidate.get("size_bytes")
    sha256 = candidate.get("sha256")
    mime_type = candidate.get("mime_type")
    assert isinstance(staging_name, str)
    assert type(size_bytes) is int
    assert isinstance(sha256, str)
    assert isinstance(mime_type, str)
    maximum_bytes = MAX_VIDEO_BYTES if role == "video" else MAX_IMAGE_BYTES
    if not _validate_staged_file(
        staging_dir, staging_name, size_bytes, sha256, mime_type, maximum_bytes
    ):
        return None
    return position, role, size_bytes


def _valid_cover(cover: dict[str, object], staging_dir: Path) -> bool:
    staging_name = cover.get("staging_name")
    size_bytes = cover.get("size_bytes")
    sha256 = cover.get("sha256")
    if (
        not isinstance(staging_name, str)
        or Path(staging_name).name != staging_name
        or type(size_bytes) is not int
        or not isinstance(sha256, str)
        or len(sha256) != 64
    ):
        return False
    mime_type = cover.get("mime_type")
    if mime_type not in IMAGE_MIME_TYPES or mime_extension(mime_type) != Path(staging_name).suffix:
        return False
    return _validate_staged_file(
        staging_dir, staging_name, size_bytes, sha256, mime_type, MAX_IMAGE_BYTES
    )


def _validate_staged_file(
    staging_dir: Path,
    name: str,
    expected_size: int,
    expected_sha256: str,
    expected_mime: str,
    maximum_bytes: int,
) -> bool:
    try:
        directory_fd = os.open(staging_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
        finally:
            os.close(directory_fd)
        try:
            info = os.fstat(file_fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size != expected_size:
                return False
            digest = hashlib.sha256()
            head = bytearray()
            observed_size = 0
            with os.fdopen(file_fd, "rb") as handle:
                while chunk := handle.read(64 * 1024):
                    observed_size += len(chunk)
                    if observed_size > maximum_bytes:
                        return False
                    if len(head) < 64:
                        head.extend(chunk[: 64 - len(head)])
                    digest.update(chunk)
                if observed_size != expected_size or digest.hexdigest() != expected_sha256:
                    return False
                actual_mime = sniff_media_mime(bytes(head))
                if actual_mime != expected_mime:
                    return False
                handle.seek(0)
                return validate_media_container(handle, actual_mime, observed_size)
        except Exception:
            os.close(file_fd)
            raise
    except OSError:
        return False


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )


def _reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(_value: str) -> object:
    raise ValueError("non-finite JSON constant")


def _contains_concealed_sensitive_value(value: object) -> bool:
    if isinstance(value, str):
        return not is_safe_retained_text(value)
    if isinstance(value, dict):
        return any(_contains_concealed_sensitive_value(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_concealed_sensitive_value(item) for item in value)
    return False


def _failed(error_code: str) -> BridgeResponse:
    return BridgeResponse(status="failed", payload={}, error_code=BridgeErrorCode(error_code))


def _run_default_bridge(
    argv: tuple[str, ...], *, payload: bytes, timeout: int, environment: dict[str, str]
) -> subprocess.CompletedProcess[bytes]:
    """Run the isolated bridge in a session that can be reaped as one group."""
    process = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=False,
        env=environment,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(input=payload, timeout=timeout)
    except subprocess.TimeoutExpired:
        _reap_process_group(process)
        raise
    if process.returncode is None:
        raise RuntimeError("bridge process did not exit")
    return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)


def _reap_process_group(process: subprocess.Popen[bytes]) -> None:
    _signal_process_group(process.pid, signal.SIGTERM)
    try:
        process.communicate(timeout=_PROCESS_GROUP_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        pass
    finally:
        _signal_process_group(process.pid, signal.SIGKILL)
    process.communicate()


def _signal_process_group(process_group_id: int, signal_number: signal.Signals) -> None:
    try:
        os.killpg(process_group_id, signal_number)
    except ProcessLookupError:
        return


def _timeout_for(request: BridgeRequest) -> int:
    if request.operation == "login":
        return _LOGIN_TIMEOUT_SECONDS
    if request.operation == "search":
        assert request.limit is not None
        return min(
            _SEARCH_BASE_TIMEOUT_SECONDS + request.limit * _DETAIL_READ_TIMEOUT_SECONDS,
            _MAX_SEARCH_TIMEOUT_SECONDS,
        )
    if request.operation == "account":
        assert request.limit is not None
        return min(
            _ACCOUNT_BASE_TIMEOUT_SECONDS
            + 5
            + request.limit * (_ACCOUNT_DOM_CARD_DETAIL_TIMEOUT_SECONDS + 7),
            _MAX_ACCOUNT_TIMEOUT_SECONDS,
        )
    return _BRIDGE_TIMEOUT_SECONDS
