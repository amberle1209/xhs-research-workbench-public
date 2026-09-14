import hashlib
import json
import multiprocessing
import os
import signal
import stat
import subprocess
from copy import deepcopy
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Self

import pytest
from pydantic import TypeAdapter, ValidationError

from xhs_workbench import xhs_adapter, xhs_bridge
from xhs_workbench.xhs_adapter import IsolatedXhsAdapter

JPEG = b"\xff\xd8\xfffixture-image"
_MISSING = object()


class RecordingRunner:
    def __init__(self, result: object) -> None:
        self.result = result
        self.calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def __call__(self, *args: object, **kwargs: object) -> object:
        self.calls.append((args, kwargs))
        return self.result


def _hold_profile_lock(auth_dir: Path, ready: object, release: object) -> None:
    """Keep a separate process' real advisory lock held until released."""
    with xhs_adapter._exclusive_profile_lock(auth_dir):
        ready.set()  # type: ignore[union-attr]
        release.wait(timeout=10)  # type: ignore[union-attr]


def _completed_status() -> object:
    return SimpleNamespace(
        returncode=0,
        stdout=_response({"auth_status": "authenticated"}),
        stderr=b"",
    )


def test_default_auth_directory_is_home_owned(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A changed working directory must not relocate the persistent profile."""
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))
    adapter = IsolatedXhsAdapter(runner=RecordingRunner(_completed_status()))

    response = adapter.auth_status()

    assert response.status == "complete"
    assert adapter._auth_dir == (tmp_path / ".local" / "auth").resolve()


@pytest.mark.parametrize("operation", ["status", "login", "search", "account"])
def test_competing_profile_lock_skips_every_bridge_invocation(
    tmp_path: Path, operation: str
) -> None:
    """Removing the pre-launch lock lets a conflicting call execute the runner."""
    auth_dir = tmp_path / "auth"
    runner = RecordingRunner(_completed_status())
    adapter = IsolatedXhsAdapter(runner=runner, auth_dir=auth_dir)

    with xhs_adapter._exclusive_profile_lock(auth_dir):
        response = (
            adapter.auth_status()
            if operation == "status"
            else adapter.login()
            if operation == "login"
            else adapter.collect_search("AI workflow", 1, tmp_path / "staging")
            if operation == "search"
            else adapter.collect_account("author_a", 1, tmp_path / "staging")
        )

    assert response.status == "failed"
    assert response.error_code == "profile_busy"
    assert runner.calls == []


def test_busy_profile_does_not_build_or_launch_default_bridge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Moving argv construction ahead of the lock could start a competing child."""
    auth_dir = tmp_path / "auth"
    adapter = IsolatedXhsAdapter(auth_dir=auth_dir)

    def bridge_must_not_start(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("a competing bridge must not start")

    monkeypatch.setattr(xhs_adapter, "_bridge_argv", bridge_must_not_start)
    monkeypatch.setattr(xhs_adapter, "_run_default_bridge", bridge_must_not_start)
    with xhs_adapter._exclusive_profile_lock(auth_dir):
        response = adapter.auth_status()

    assert response.status == "failed"
    assert response.error_code == "profile_busy"


def test_competing_process_fails_without_launching_bridge(tmp_path: Path) -> None:
    """A spawned process proves the lock is kernel-visible, not process-local state."""
    auth_dir = tmp_path / "auth"
    auth_dir.mkdir()
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    release = context.Event()
    holder = context.Process(target=_hold_profile_lock, args=(auth_dir, ready, release))
    holder.start()
    runner = RecordingRunner(_completed_status())
    adapter = IsolatedXhsAdapter(runner=runner, auth_dir=auth_dir)
    try:
        assert ready.wait(timeout=10)
        response = adapter.auth_status()
        assert response.status == "failed"
        assert response.error_code == "profile_busy"
        assert runner.calls == []
    finally:
        release.set()
        holder.join(timeout=10)
        if holder.is_alive():
            holder.terminate()
            holder.join(timeout=10)
    assert holder.exitcode == 0


def test_profile_lock_releases_after_runner_failure(tmp_path: Path) -> None:
    """Removing the unlock finally block leaves the shared profile unavailable."""
    auth_dir = tmp_path / "auth"

    def raising_runner(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("runner failed")

    response = IsolatedXhsAdapter(runner=raising_runner, auth_dir=auth_dir).auth_status()

    assert response.status == "failed"
    assert response.error_code == "bridge_launch_failed"
    with xhs_adapter._exclusive_profile_lock(auth_dir):
        pass


def test_profile_lock_rejects_symlink_and_wrong_owner_without_launching_bridge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Removing no-follow or owner checks risks attaching to an attacker-owned lock."""
    auth_dir = tmp_path / "auth"
    auth_dir.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("content-free")
    lock_path = auth_dir / ".xhs-workbench.lock"
    lock_path.symlink_to(outside)
    symlink_runner = RecordingRunner(_completed_status())

    symlink_response = IsolatedXhsAdapter(runner=symlink_runner, auth_dir=auth_dir).auth_status()

    assert symlink_response.status == "failed"
    assert symlink_response.error_code == "bridge_launch_failed"
    assert symlink_runner.calls == []
    lock_path.unlink()
    original_fstat = xhs_adapter.os.fstat
    monkeypatch.setattr(
        xhs_adapter.os,
        "fstat",
        lambda descriptor: type(
            "ForeignLock", (), {"st_mode": stat.S_IFREG | 0o600, "st_uid": os.geteuid() + 1}
        )(),
    )
    owner_runner = RecordingRunner(_completed_status())

    owner_response = IsolatedXhsAdapter(runner=owner_runner, auth_dir=auth_dir).auth_status()

    assert owner_response.status == "failed"
    assert owner_response.error_code == "bridge_launch_failed"
    assert owner_runner.calls == []
    monkeypatch.setattr(xhs_adapter.os, "fstat", original_fstat)


def test_profile_lock_is_mode_0600(tmp_path: Path) -> None:
    """Removing the explicit chmod can leave the cross-process coordination file exposed."""
    auth_dir = tmp_path / "auth"
    auth_dir.mkdir()

    with xhs_adapter._exclusive_profile_lock(auth_dir):
        pass

    assert (auth_dir / ".xhs-workbench.lock").stat().st_mode & 0o777 == 0o600


def _response(payload: dict[str, object] | None = None) -> bytes:
    payload = deepcopy(payload or {"notes": []})
    notes = payload.get("notes")
    if isinstance(notes, list):
        for note in notes:
            if isinstance(note, dict) and "media_manifest_version" in note:
                note.setdefault("media_discovery_truncated", False)
                note.setdefault("media_candidates", [])
    return json.dumps(
        {"status": "complete", "payload": payload, "error_code": None}
    ).encode()


def _account_payload(
    *,
    requested_count: int = 1,
    notes: list[dict[str, object]] | None = None,
    candidate_attempts: list[dict[str, object]] | None = None,
    detail_delay_ms: list[int] | None = None,
) -> dict[str, object]:
    resolved_notes = notes if notes is not None else []
    resolved_attempts = (
        candidate_attempts
        if candidate_attempts is not None
        else [
            {
                "position": position,
                "outcome": "unavailable",
                "stage": "candidate",
                "reason": "candidate_shortfall",
            }
            for position in range(1, requested_count + 1)
        ]
    )
    return {
        "account": {
            "account_id": "author_a",
            "profile_url": "https://www.xiaohongshu.com/user/profile/author_a",
            "platform_metrics": {},
            "field_statuses": {
                "name": "not_exposed",
                "bio": "not_exposed",
                "note_count": "not_exposed",
                "follower_count": "not_exposed",
                "avatar": "not_exposed",
                "platform_metrics": "not_exposed",
            },
        },
        "notes": resolved_notes,
        "requested_count": requested_count,
        "candidate_attempts": resolved_attempts,
        "pacing_summary": {
            "policy": "conservative_jitter_v1",
            "profile_open_delay_ms": 2_000,
            "detail_delay_ms": detail_delay_ms if detail_delay_ms is not None else [],
        },
    }


def _account_note(note_id: str = "note_a", position: int = 1) -> dict[str, object]:
    return {
        "note_id": note_id,
        "canonical_url": f"https://www.xiaohongshu.com/explore/{note_id}",
        "metrics": {},
        "source_position": position,
        "media_manifest_version": 2,
        "media_discovered_count": 0,
    }


def _child_stdout_with_published_at(published_at: datetime) -> bytes:
    """Use the child model's JSON serializer at the actual process boundary."""
    note = xhs_bridge.BridgeNote(
        media_manifest_version=2,
        note_id="note_a",
        canonical_url="https://www.xiaohongshu.com/explore/note_a",
        metrics={},
        source_position=1,
        media_discovered_count=0,
        published_at=published_at,
    )
    response = xhs_bridge.BridgeResponse(
        status="partial",
        payload=xhs_bridge.SearchPayload(notes=[note]).model_dump(exclude_none=True),
        error_code=xhs_bridge.BridgeErrorCode.MEDIA_PARTIAL,
    )
    return response.model_dump_json().encode()


@pytest.mark.parametrize(
    "published_at",
    [
        datetime.fromisoformat("2023-11-14T22:13:21"),
        datetime(2023, 11, 14, 22, 13, 21, 123456, tzinfo=UTC),
        datetime(2023, 11, 14, 22, 13, 21, tzinfo=timezone(timedelta(hours=8))),
    ],
)
def test_adapter_accepts_and_normalizes_child_serialized_publication_timestamps(
    tmp_path: Path, published_at: datetime
) -> None:
    """Removing the timestamp boundary parser makes this child stdout reject."""
    stdout = _child_stdout_with_published_at(published_at)
    adapter = IsolatedXhsAdapter(
        runner=RecordingRunner(SimpleNamespace(returncode=0, stdout=stdout, stderr=b"")),
        auth_dir=tmp_path / "auth",
    )

    response = adapter.collect_search("AI workflow", 1, tmp_path / "staging")

    assert response.status == "partial"
    assert response.error_code == "media_partial"
    note = response.payload["notes"][0]
    assert isinstance(note, dict)
    assert isinstance(note["published_at"], datetime)
    child_timestamp = json.loads(stdout)["payload"]["notes"][0]["published_at"]
    assert note["published_at"] == datetime.fromisoformat(child_timestamp)
    assert json.loads(response.model_dump_json())["payload"]["notes"][0]["published_at"] == TypeAdapter(
        datetime
    ).dump_python(note["published_at"], mode="json")


@pytest.mark.parametrize("offset", (timedelta(seconds=30), -timedelta(seconds=30)))
def test_bridge_note_rejects_subminute_utc_offset_before_child_serialization(
    offset: timedelta,
) -> None:
    with pytest.raises(ValidationError, match="whole-minute UTC offset"):
        _child_stdout_with_published_at(
            datetime(2023, 11, 14, 22, 13, 21, tzinfo=timezone(offset))
        )


@pytest.mark.parametrize(
    "published_at",
    [
        "2023-11-14 22:13:21Z",
        "2023-11-14T22:13:21+00",
        "2023-11-14T22:13:21.1234567Z",
        "2023-11-14T22:13:21+08:00:01",
        "2023-11-14T22:13:21+00:00",
        "2023-11-14T22:13:21-00:00",
        "not-a-date",
        1_700_000_001,
        True,
        {"malformed": "timestamp"},
    ],
)
def test_adapter_rejects_noncanonical_child_publication_timestamps(
    tmp_path: Path, published_at: object
) -> None:
    """Only the bridge serializer's exact datetime strings cross this JSON boundary."""
    decoded = json.loads(_child_stdout_with_published_at(datetime(2023, 11, 14, 22, 13, 21, tzinfo=UTC)))
    decoded["payload"]["notes"][0]["published_at"] = published_at
    stdout = json.dumps(decoded).encode()
    adapter = IsolatedXhsAdapter(
        runner=RecordingRunner(SimpleNamespace(returncode=0, stdout=stdout, stderr=b"")),
        auth_dir=tmp_path / "auth",
    )

    response = adapter.collect_search("AI workflow", 1, tmp_path / "staging")

    assert response.status == "failed"
    assert response.error_code == "invalid_bridge_output"
    assert str(published_at) not in response.model_dump_json()


def test_adapter_uses_fixed_argv_stdin_only_and_two_directory_variables(tmp_path: Path) -> None:
    runner = RecordingRunner(SimpleNamespace(returncode=0, stdout=_response(), stderr=b""))
    adapter = IsolatedXhsAdapter(runner=runner, auth_dir=tmp_path / "auth")
    staging = tmp_path / "output" / ".staging"

    response = adapter.collect_search("AI workflow", 1, staging)

    assert response.status == "complete"
    assert len(runner.calls) == 1
    args, kwargs = runner.calls[0]
    assert list(args[0]) == [
        "uv",
        "run",
        "--isolated",
        "--project",
        str(Path(xhs_adapter.__file__).resolve().parents[2]),
        "--extra",
        "upstream",
        "python",
        "-m",
        "xhs_workbench.xhs_bridge",
    ]
    request = json.loads(kwargs["input"])
    assert request == {"operation": "search", "keyword": "AI workflow", "limit": 1}
    assert kwargs["capture_output"] is True
    assert kwargs["timeout"] == 168
    assert kwargs["text"] is False
    environment = kwargs["env"]
    assert isinstance(environment, dict)
    assert environment["XHS_WORKBENCH_AUTH_DIR"] == str((tmp_path / "auth").resolve())
    assert environment["XHS_WORKBENCH_ASSET_STAGING_DIR"] == str(staging.resolve())
    assert staging.is_dir()


def test_adapter_pins_child_bridge_to_the_loaded_project_not_the_caller_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    unrelated_project = tmp_path / "unrelated-project"
    unrelated_project.mkdir()
    (unrelated_project / "pyproject.toml").write_text("[project]\nname = 'unrelated'\n")
    monkeypatch.chdir(unrelated_project)
    runner = RecordingRunner(SimpleNamespace(returncode=0, stdout=_response(), stderr=b""))
    adapter = IsolatedXhsAdapter(runner=runner, auth_dir=tmp_path / "auth")

    response = adapter.collect_search("AI workflow", 1, tmp_path / "staging")

    argv = list(runner.calls[0][0][0])
    project_index = argv.index("--project")
    loaded_project = Path(xhs_adapter.__file__).resolve().parents[2]
    assert argv[project_index + 1] == str(loaded_project)
    assert argv[project_index + 1] != str(unrelated_project)
    assert str(loaded_project) not in response.model_dump_json()


def test_adapter_fails_closed_when_the_loaded_project_cannot_be_identified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = RecordingRunner(SimpleNamespace(returncode=0, stdout=_response(), stderr=b""))
    monkeypatch.setattr(
        xhs_adapter,
        "__file__",
        str(tmp_path / "site-packages" / "xhs_workbench" / "xhs_adapter.py"),
    )
    adapter = IsolatedXhsAdapter(runner=runner, auth_dir=tmp_path / "auth")

    response = adapter.auth_status()

    assert response.status == "failed"
    assert response.error_code == "bridge_launch_failed"
    assert runner.calls == []


@pytest.mark.parametrize("omitted_field", ["title", "body", "note_type"])
def test_adapter_accepts_an_omitted_nullable_account_note_field(
    tmp_path: Path, omitted_field: str
) -> None:
    note = {
        "note_id": "note_a",
        "canonical_url": "https://www.xiaohongshu.com/explore/note_a",
        "title": "visible title",
        "body": "visible body",
        "tags": [],
        "note_type": "normal",
        "metrics": {},
        "source_position": 1,
        "media_manifest_version": 2,
        "media_discovered_count": 0,
    }
    note.pop(omitted_field)
    payload = _account_payload(
        notes=[note],
        candidate_attempts=[
            {
                "position": 1,
                "note_id": "note_a",
                "outcome": "complete",
                "stage": "detail_projection",
            }
        ],
        detail_delay_ms=[3_000],
    )
    adapter = IsolatedXhsAdapter(
        runner=RecordingRunner(
            SimpleNamespace(returncode=0, stdout=_response(payload), stderr=b"")
        ),
        auth_dir=tmp_path / "auth",
    )

    response = adapter.collect_account("author_a", 1, tmp_path / "staging")

    assert response.status == "complete"
    assert response.error_code is None
    assert omitted_field not in response.payload["notes"][0]


def test_adapter_allows_longer_timeout_only_for_project_browser_login(tmp_path: Path) -> None:
    runner = RecordingRunner(
        SimpleNamespace(
            returncode=0,
            stdout=_response({"auth_status": "action_required"}),
            stderr=b"",
        )
    )
    adapter = IsolatedXhsAdapter(runner=runner, auth_dir=tmp_path / "auth")

    response = adapter.login()

    assert response.status == "complete"
    assert runner.calls[0][1]["timeout"] == 300


@pytest.mark.parametrize(
    ("operation", "limit", "expected_timeout"),
    [
        ("search", 1, 168),
        ("search", 10, 1329),
        ("account", 1, 263),
        ("account", 5, 923),
        ("account", 10, 1748),
    ],
)
def test_adapter_collection_timeout_covers_limited_detail_reads_with_a_finite_cap(
    tmp_path: Path, operation: str, limit: int, expected_timeout: int
) -> None:
    payload = {"notes": []} if operation == "search" else _account_payload(requested_count=limit)
    runner = RecordingRunner(
        SimpleNamespace(returncode=0, stdout=_response(payload), stderr=b"")
    )
    adapter = IsolatedXhsAdapter(runner=runner, auth_dir=tmp_path / "auth")

    response = (
        adapter.collect_search("AI workflow", limit, tmp_path / "staging")
        if operation == "search"
        else adapter.collect_account("author_a", limit, tmp_path / "staging")
    )

    assert response.status == "complete"
    assert runner.calls[0][1]["timeout"] == expected_timeout


def test_adapter_rejects_an_account_payload_with_a_different_requested_count(
    tmp_path: Path,
) -> None:
    """Ignoring the request limit lets a child report a shorter collection as complete."""
    adapter = IsolatedXhsAdapter(
        runner=RecordingRunner(
            SimpleNamespace(
                returncode=0,
                stdout=_response(_account_payload(requested_count=2)),
                stderr=b"",
            )
        ),
        auth_dir=tmp_path / "auth",
    )

    response = adapter.collect_account("author_a", 1, tmp_path / "staging")

    assert response.status == "failed"
    assert response.error_code == "invalid_bridge_output"


@pytest.mark.parametrize(
    ("candidate_attempts", "notes", "detail_delay_ms"),
    [
        (
            [
                {
                    "position": 1,
                    "outcome": "unavailable",
                    "stage": "candidate",
                    "reason": "candidate_shortfall",
                },
                {
                    "position": 1,
                    "outcome": "unavailable",
                    "stage": "candidate",
                    "reason": "candidate_shortfall",
                },
            ],
            [],
            [],
        ),
        (
            [
                {
                    "position": 2,
                    "outcome": "unavailable",
                    "stage": "candidate",
                    "reason": "candidate_shortfall",
                }
            ],
            [],
            [],
        ),
        (
            [
                {
                    "position": 1,
                    "outcome": "unavailable",
                    "stage": "candidate",
                    "reason": "candidate_shortfall",
                }
            ],
            [_account_note()],
            [],
        ),
        (
            [
                {
                    "position": 1,
                    "outcome": "unavailable",
                    "stage": "candidate",
                    "reason": "candidate_shortfall",
                }
            ],
            [],
            [3_000],
        ),
    ],
    ids=("duplicate", "gapped", "note-without-complete", "pacing-count"),
)
def test_adapter_rejects_an_invalid_account_attempt_ledger(
    tmp_path: Path,
    candidate_attempts: list[dict[str, object]],
    notes: list[dict[str, object]],
    detail_delay_ms: list[int],
) -> None:
    """Removing any ledger invariant admits a deceptive account bridge response."""
    adapter = IsolatedXhsAdapter(
        runner=RecordingRunner(
            SimpleNamespace(
                returncode=0,
                stdout=_response(
                    _account_payload(
                        notes=notes,
                        candidate_attempts=candidate_attempts,
                        detail_delay_ms=detail_delay_ms,
                    )
                ),
                stderr=b"",
            )
        ),
        auth_dir=tmp_path / "auth",
    )

    response = adapter.collect_account("author_a", 1, tmp_path / "staging")

    assert response.status == "failed"
    assert response.error_code == "invalid_bridge_output"


@pytest.mark.parametrize("field_statuses", [
    {
        "name": "not_exposed",
        "bio": "not_exposed",
        "note_count": "not_exposed",
        "follower_count": "not_exposed",
        "avatar": "not_exposed",
    },
    {
        "name": "not_exposed",
        "bio": "not_exposed",
        "note_count": "not_exposed",
        "follower_count": "not_exposed",
        "avatar": "not_exposed",
        "platform_metrics": "not_exposed",
        "extra": "not_exposed",
    },
], ids=("missing", "extra"))
def test_adapter_rejects_account_field_statuses_without_the_exact_six_keys(
    tmp_path: Path, field_statuses: dict[str, str]
) -> None:
    """A partial field-status map lets unavailable evidence look unexamined."""
    payload = _account_payload()
    account = payload["account"]
    assert isinstance(account, dict)
    account["field_statuses"] = field_statuses
    adapter = IsolatedXhsAdapter(
        runner=RecordingRunner(
            SimpleNamespace(returncode=0, stdout=_response(payload), stderr=b"")
        ),
        auth_dir=tmp_path / "auth",
    )

    response = adapter.collect_account("author_a", 1, tmp_path / "staging")

    assert response.status == "failed"
    assert response.error_code == "invalid_bridge_output"


def test_default_runner_starts_a_session_and_reaps_its_process_group_on_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class TimedOutProcess:
        pid = 4242

        def __init__(self) -> None:
            self.communicate_calls: list[tuple[bytes | None, float | None]] = []

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def communicate(
            self, input: bytes | None = None, timeout: float | None = None
        ) -> tuple[bytes, bytes]:
            self.communicate_calls.append((input, timeout))
            if len(self.communicate_calls) < 3:
                raise subprocess.TimeoutExpired("bridge", timeout, output=b"unsafe")
            return b"", b""

        def kill(self) -> None:
            raise AssertionError("default runner must not kill only the bridge process")

        def wait(self) -> int:
            return -9

    process = TimedOutProcess()
    popen_calls: list[dict[str, object]] = []
    killed_groups: list[tuple[int, signal.Signals]] = []

    def fake_popen(*_args: object, **kwargs: object) -> "TimedOutProcess":
        popen_calls.append(kwargs)
        return process

    monkeypatch.setattr(xhs_adapter.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        xhs_adapter.os,
        "killpg",
        lambda pgid, sig: killed_groups.append((pgid, signal.Signals(sig))),
    )
    adapter = IsolatedXhsAdapter(auth_dir=tmp_path / "auth")

    response = adapter.collect_search("AI workflow", 1, tmp_path / "staging")

    assert response.status == "failed"
    assert response.error_code == "bridge_timeout"
    assert popen_calls[0]["start_new_session"] is True
    assert killed_groups == [(4242, signal.SIGTERM), (4242, signal.SIGKILL)]
    assert len(process.communicate_calls) == 3
    assert "unsafe" not in response.model_dump_json()


def test_default_runner_kills_the_group_when_its_leader_exits_after_term(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class LeaderExitedProcess:
        pid = 4243

        def __init__(self) -> None:
            self.communicate_calls: list[tuple[bytes | None, float | None]] = []

        def communicate(
            self, input: bytes | None = None, timeout: float | None = None
        ) -> tuple[bytes, bytes]:
            self.communicate_calls.append((input, timeout))
            if len(self.communicate_calls) == 1:
                raise subprocess.TimeoutExpired("bridge", timeout, output=b"unsafe")
            return b"", b""

    process = LeaderExitedProcess()
    killed_groups: list[tuple[int, signal.Signals]] = []

    def killpg_when_present(pgid: int, sig: int) -> None:
        killed_groups.append((pgid, signal.Signals(sig)))
        if sig == signal.SIGKILL:
            raise ProcessLookupError

    monkeypatch.setattr(xhs_adapter.subprocess, "Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(xhs_adapter.os, "killpg", killpg_when_present)
    adapter = IsolatedXhsAdapter(auth_dir=tmp_path / "auth")

    response = adapter.collect_search("AI workflow", 1, tmp_path / "staging")

    assert response.status == "failed"
    assert response.error_code == "bridge_timeout"
    assert killed_groups == [(4243, signal.SIGTERM), (4243, signal.SIGKILL)]
    assert len(process.communicate_calls) == 3
    assert "unsafe" not in response.model_dump_json()


@pytest.mark.parametrize(
    "result",
    [
        SimpleNamespace(returncode=1, stdout=b"", stderr=b"sensitive upstream output"),
        SimpleNamespace(returncode=0, stdout=b"not-json", stderr=b""),
        SimpleNamespace(returncode=0, stdout=b"{}\n{}", stderr=b""),
        SimpleNamespace(returncode=0, stdout=b"x" * (1024 * 1024 + 1), stderr=b""),
    ],
)
def test_adapter_fails_closed_without_retaining_subprocess_output(
    tmp_path: Path, result: object
) -> None:
    adapter = IsolatedXhsAdapter(runner=RecordingRunner(result), auth_dir=tmp_path / "auth")

    response = adapter.collect_search("AI workflow", 1, tmp_path / "staging")

    assert response.status == "failed"
    assert response.payload == {}
    assert response.error_code is not None
    assert "sensitive upstream output" not in response.model_dump_json()


def test_adapter_fails_closed_on_timeout(tmp_path: Path) -> None:
    def timeout_runner(*_args: object, **_kwargs: object) -> object:
        raise subprocess.TimeoutExpired("bridge", 30, output=b"unsafe")

    adapter = IsolatedXhsAdapter(runner=timeout_runner, auth_dir=tmp_path / "auth")

    response = adapter.collect_search("AI workflow", 1, tmp_path / "staging")

    assert response.status == "failed"
    assert response.error_code == "bridge_timeout"
    assert "unsafe" not in response.model_dump_json()


def test_adapter_rejects_traversal_and_symlink_staging_paths(tmp_path: Path) -> None:
    adapter = IsolatedXhsAdapter(
        runner=RecordingRunner(SimpleNamespace(returncode=0, stdout=_response(), stderr=b"")),
        auth_dir=tmp_path / "auth",
    )

    with pytest.raises(ValueError):
        adapter.collect_search("AI workflow", 1, tmp_path / "output" / ".." / "escape")

    real_dir = tmp_path / "real"
    real_dir.mkdir()
    link = tmp_path / "linked"
    link.symlink_to(real_dir, target_is_directory=True)
    with pytest.raises(ValueError):
        adapter.collect_search("AI workflow", 1, link)


def test_adapter_rejects_staged_asset_escape_or_symlink(tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    staging.mkdir()
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"outside")
    (staging / "cover.jpg").symlink_to(outside)
    runner = RecordingRunner(
        SimpleNamespace(
            returncode=0,
            stdout=_response(
                {
                    "notes": [
                        {
                            "note_id": "note_a",
                            "canonical_url": "https://www.xiaohongshu.com/explore/note_a",
                            "title": None,
                            "body": None,
                            "tags": [],
                            "note_type": None,
                            "metrics": {},
                            "source_position": 1,
                            "media_manifest_version": 2,
                            "media_discovered_count": 0,
                            "cover": {
                                "staging_name": "cover.jpg",
                                "mime_type": "image/jpeg",
                                "size_bytes": 7,
                                "sha256": "a" * 64,
                            },
                        }
                    ]
                }
            ),
            stderr=b"",
        )
    )
    adapter = IsolatedXhsAdapter(runner=runner, auth_dir=tmp_path / "auth")

    response = adapter.collect_search("AI workflow", 1, staging)

    assert response.status == "failed"
    assert response.error_code == "unsafe_staging_asset"


def test_adapter_public_surface_has_no_mutation_methods() -> None:
    public_names = {name.lower() for name in dir(IsolatedXhsAdapter) if not name.startswith("_")}
    for forbidden in ("like", "favorite", "follow", "comment", "post", "publish", "delete"):
        assert not any(forbidden in name for name in public_names)


@pytest.mark.parametrize(
    "stdout",
    [
        b'{"status":"complete","payload":{"unallowlisted":"safe"},"error_code":null}',
        b'{"status":"complete","payload":{"auth_status":"authenticated"},"error_code":"free-form"}',
        b'{"status":"complete","payload":{"auth_status":NaN},"error_code":null}',
        b'{"status":"complete","payload":{"auth_status":"authenticated","auth_status":"failed"},"error_code":null}',
    ],
)
def test_adapter_rejects_unallowlisted_or_nonstandard_protocol_values(
    tmp_path: Path, stdout: bytes
) -> None:
    adapter = IsolatedXhsAdapter(
        runner=RecordingRunner(SimpleNamespace(returncode=0, stdout=stdout, stderr=b"")),
        auth_dir=tmp_path / "auth",
    )

    response = adapter.auth_status()

    assert response.status == "failed"
    assert response.error_code == "invalid_bridge_output"


def test_adapter_rejects_a_concealed_sensitive_value_in_an_allowlisted_field(tmp_path: Path) -> None:
    stdout = _response(
        {
            "notes": [
                {
                    "note_id": "note_a",
                    "canonical_url": "https://www.xiaohongshu.com/explore/note_a",
                    "title": "fixture",
                    "body": "xsec=concealed-secret",
                    "tags": [],
                    "note_type": None,
                    "metrics": {},
                    "source_position": 1,
                    "media_manifest_version": 2,
                    "media_discovered_count": 0,
                }
            ]
        }
    )
    adapter = IsolatedXhsAdapter(
        runner=RecordingRunner(SimpleNamespace(returncode=0, stdout=stdout, stderr=b"")),
        auth_dir=tmp_path / "auth",
    )

    response = adapter.collect_search("AI workflow", 1, tmp_path / "staging")

    assert response.status == "failed"
    assert response.error_code == "unsafe_bridge_response"


@pytest.mark.parametrize(
    "body",
    [
        "Refresh_Token : concealed-secret",
        "caption https://cdn.example.invalid/image.jpg?sign=concealed-secret",
    ],
)
def test_adapter_rejects_sensitive_assignments_and_embedded_query_urls(
    tmp_path: Path, body: str
) -> None:
    stdout = _response(
        {
            "notes": [
                {
                    "note_id": "note_a",
                    "canonical_url": "https://www.xiaohongshu.com/explore/note_a",
                    "title": "fixture",
                    "body": body,
                    "tags": [],
                    "note_type": None,
                    "metrics": {},
                    "source_position": 1,
                    "media_manifest_version": 2,
                    "media_discovered_count": 0,
                }
            ]
        }
    )
    adapter = IsolatedXhsAdapter(
        runner=RecordingRunner(SimpleNamespace(returncode=0, stdout=stdout, stderr=b"")),
        auth_dir=tmp_path / "auth",
    )

    response = adapter.collect_search("AI workflow", 1, tmp_path / "staging")

    assert response.status == "failed"
    assert response.error_code == "unsafe_bridge_response"


@pytest.mark.parametrize("avatar_body", [None, b"not-a-jpeg"])
def test_adapter_rejects_missing_or_forged_account_avatar(
    tmp_path: Path, avatar_body: bytes | None
) -> None:
    staging = tmp_path / "staging"
    staging.mkdir()
    if avatar_body is not None:
        (staging / "avatar.jpg").write_bytes(avatar_body)
    payload = _account_payload()
    payload["avatar"] = {
        "staging_name": "avatar.jpg",
        "mime_type": "image/jpeg",
        "size_bytes": len(avatar_body) if avatar_body is not None else 1,
        "sha256": hashlib.sha256(avatar_body or b"x").hexdigest(),
    }
    adapter = IsolatedXhsAdapter(
        runner=RecordingRunner(
            SimpleNamespace(returncode=0, stdout=_response(payload), stderr=b"")
        ),
        auth_dir=tmp_path / "auth",
    )

    response = adapter.collect_account("author_a", 1, staging)

    assert response.status == "failed"
    assert response.error_code == "unsafe_staging_asset"


def test_adapter_rejects_account_media_that_exceeds_run_budget_with_avatar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Omitting the avatar from the parent recheck accepts an oversized run."""
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "avatar.jpg").write_bytes(JPEG)
    (staging / "note_a-image-001.jpg").write_bytes(JPEG)
    payload = _account_payload(
        notes=[
            {
                "note_id": "note_a",
                "canonical_url": "https://www.xiaohongshu.com/explore/note_a",
                "title": None,
                "body": None,
                "tags": [],
                "note_type": None,
                "metrics": {},
                "source_position": 1,
                "media_manifest_version": 2,
                "media_discovered_count": 1,
                "media_candidates": [
                    {
                        "note_id": "note_a",
                        "role": "image",
                        "position": 1,
                        "status": "downloaded",
                        "staging_name": "note_a-image-001.jpg",
                        "mime_type": "image/jpeg",
                        "size_bytes": len(JPEG),
                        "sha256": hashlib.sha256(JPEG).hexdigest(),
                    }
                ],
            }
        ],
        candidate_attempts=[
            {
                "position": 1,
                "note_id": "note_a",
                "outcome": "complete",
                "stage": "detail_projection",
            }
        ],
        detail_delay_ms=[3_000],
    )
    payload["avatar"] = {
        "staging_name": "avatar.jpg",
        "mime_type": "image/jpeg",
        "size_bytes": len(JPEG),
        "sha256": hashlib.sha256(JPEG).hexdigest(),
    }
    monkeypatch.setattr(xhs_adapter, "MAX_RUN_MEDIA_BYTES", len(JPEG))
    adapter = IsolatedXhsAdapter(
        runner=RecordingRunner(
            SimpleNamespace(returncode=0, stdout=_response(payload), stderr=b"")
        ),
        auth_dir=tmp_path / "auth",
    )

    response = adapter.collect_account("author_a", 1, staging)

    assert response.status == "failed"
    assert response.error_code == "unsafe_staging_asset"


@pytest.mark.parametrize(
    ("name", "mime_type", "size_bytes", "body"),
    [
        ("cover.jpg", "image/jpeg", 1, b"x"),
        ("cover.jpg", "image/jpeg", True, b"x"),
        ("cover.jpg", "image/jpeg", 16 * 1024 * 1024, b"x" * (16 * 1024 * 1024)),
    ],
)
def test_adapter_rejects_forged_or_oversized_staged_media(
    tmp_path: Path, name: str, mime_type: str, size_bytes: int, body: bytes
) -> None:
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / name).write_bytes(body)
    payload = {
        "notes": [
            {
                "note_id": "note_a",
                "canonical_url": "https://www.xiaohongshu.com/explore/note_a",
                "title": None,
                "body": None,
                "tags": [],
                "note_type": None,
                "metrics": {},
                "source_position": 1,
                "media_manifest_version": 2,
                "media_discovered_count": 0,
                "cover": {
                    "staging_name": name,
                    "mime_type": mime_type,
                    "size_bytes": size_bytes,
                    "sha256": hashlib.sha256(body).hexdigest(),
                },
            }
        ]
    }
    adapter = IsolatedXhsAdapter(
        runner=RecordingRunner(
            SimpleNamespace(returncode=0, stdout=_response(payload), stderr=b"")
        ),
        auth_dir=tmp_path / "auth",
    )

    response = adapter.collect_search("AI workflow", 1, staging)

    assert response.status == "failed"
    assert response.error_code in {"unsafe_staging_asset", "invalid_bridge_output"}


def test_adapter_does_not_create_through_a_parent_symlink(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    parent_link = tmp_path / "parent-link"
    parent_link.symlink_to(outside, target_is_directory=True)
    adapter = IsolatedXhsAdapter(
        runner=RecordingRunner(SimpleNamespace(returncode=0, stdout=_response(), stderr=b"")),
        auth_dir=tmp_path / "auth",
    )

    with pytest.raises(ValueError):
        adapter.collect_search("AI workflow", 1, parent_link / "new-staging")

    assert not (outside / "new-staging").exists()


def _invoke_media_candidate(
    tmp_path: Path,
    candidate: dict[str, object],
    body: bytes = JPEG,
    *,
    status: str = "complete",
    manifest_version: object = 2,
) -> object:
    """Exercise the parent boundary with an untrusted bridge media manifest."""
    staging = tmp_path / "staging"
    staging.mkdir()
    name = candidate.get("staging_name")
    if isinstance(name, str):
        (staging / name).write_bytes(body)
    discovered_count = 1 if candidate.get("role") == "image" else 0
    note: dict[str, object] = {
        "note_id": "note_a",
        "canonical_url": "https://www.xiaohongshu.com/explore/note_a",
        "metrics": {},
        "source_position": 1,
        "media_discovered_count": discovered_count,
        "media_discovery_truncated": False,
        "media_candidates": [candidate],
    }
    if manifest_version is not _MISSING:
        note["media_manifest_version"] = manifest_version
    payload = {"notes": [note]}
    encoded = json.dumps(
        {
            "status": status,
            "payload": payload,
            "error_code": "media_partial" if status == "partial" else None,
        }
    ).encode()
    adapter = IsolatedXhsAdapter(
        runner=RecordingRunner(SimpleNamespace(returncode=0, stdout=encoded, stderr=b"")),
        auth_dir=tmp_path / "auth",
    )
    return adapter.collect_search("AI workflow", 1, staging)


def _invoke_media_manifest(
    tmp_path: Path,
    candidates: list[dict[str, object]],
    *,
    discovered_count: int,
    discovery_truncated: bool,
) -> object:
    """Invoke a partial bridge response with an explicit image-slot ledger."""
    staging = tmp_path / "staging"
    staging.mkdir()
    note = {
        "note_id": "note_a",
        "canonical_url": "https://www.xiaohongshu.com/explore/note_a",
        "metrics": {},
        "source_position": 1,
        "media_manifest_version": 2,
        "media_discovered_count": discovered_count,
        "media_discovery_truncated": discovery_truncated,
        "media_candidates": candidates,
    }
    encoded = json.dumps(
        {"status": "partial", "payload": {"notes": [note]}, "error_code": "media_partial"}
    ).encode()
    adapter = IsolatedXhsAdapter(
        runner=RecordingRunner(SimpleNamespace(returncode=0, stdout=encoded, stderr=b"")),
        auth_dir=tmp_path / "auth",
    )
    return adapter.collect_search("AI workflow", 1, staging)


def _missing_image(position: int, *, reason: str = "source_not_exposed") -> dict[str, object]:
    return {
        "note_id": "note_a",
        "role": "image",
        "position": position,
        "status": "missing",
        "missing_reason": reason,
    }


@pytest.mark.parametrize("discovery_truncated", [False, True])
def test_adapter_rejects_an_incomplete_discovered_image_slot_range(
    tmp_path: Path, discovery_truncated: bool
) -> None:
    """Every discovered, representable image position must have its own slot."""
    response = _invoke_media_manifest(
        tmp_path,
        [_missing_image(1)],
        discovered_count=2,
        discovery_truncated=discovery_truncated,
    )

    assert response.status == "failed"
    assert response.error_code == "unsafe_staging_asset"


def test_adapter_accepts_all_100_representable_discovered_image_slots(tmp_path: Path) -> None:
    """Discovery above the cap represents exactly slots 1 through 100."""
    candidates = [
        _missing_image(position)
        if position <= 20
        else {
            "note_id": "note_a",
            "role": "image",
            "position": position,
            "status": "rejected",
            "missing_reason": "slot_limit",
        }
        for position in range(1, 101)
    ]

    response = _invoke_media_manifest(
        tmp_path, candidates, discovered_count=101, discovery_truncated=True
    )

    assert response.status == "partial"


def test_adapter_keeps_a_video_only_note_with_zero_discovered_images(tmp_path: Path) -> None:
    response = _invoke_media_manifest(
        tmp_path,
        [
            {
                "note_id": "note_a",
                "role": "video",
                "position": 1,
                "status": "missing",
                "duration_ms": 1000,
                "missing_reason": "source_not_exposed",
            }
        ],
        discovered_count=0,
        discovery_truncated=False,
    )

    assert response.status == "partial"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("staging_name", "other-image-001.jpg"),
        ("size_bytes", len(JPEG) + 1),
        ("sha256", "0" * 64),
        ("mime_type", "image/png"),
    ],
)
def test_adapter_rejects_malformed_downloaded_media(
    tmp_path: Path, field: str, value: object
) -> None:
    """A child cannot relabel, resize, or substitute a staged image slot."""
    candidate: dict[str, object] = {
        "note_id": "note_a",
        "role": "image",
        "position": 1,
        "status": "downloaded",
        "staging_name": "note_a-image-001.jpg",
        "mime_type": "image/jpeg",
        "size_bytes": len(JPEG),
        "sha256": hashlib.sha256(JPEG).hexdigest(),
    }
    candidate[field] = value

    result = _invoke_media_candidate(tmp_path, candidate)

    assert result.status == "failed"
    assert result.error_code == "unsafe_staging_asset"


def test_adapter_accepts_a_well_formed_missing_slot_without_a_file(tmp_path: Path) -> None:
    candidate: dict[str, object] = {
        "note_id": "note_a",
        "role": "image",
        "position": 1,
        "status": "missing",
        "missing_reason": "source_not_exposed",
    }

    response = _invoke_media_candidate(tmp_path, candidate, status="partial")

    assert response.status == "partial"


@pytest.mark.parametrize("manifest_version", [_MISSING, True, 1, "2"])
def test_adapter_rejects_missing_or_nonexact_bridge_manifest_version(
    tmp_path: Path, manifest_version: object
) -> None:
    """The parent rejects non-integer V2 versions before model normalization."""
    candidate: dict[str, object] = {
        "note_id": "note_a",
        "role": "image",
        "position": 1,
        "status": "missing",
        "missing_reason": "source_not_exposed",
    }

    response = _invoke_media_candidate(
        tmp_path, candidate, status="partial", manifest_version=manifest_version
    )

    assert response.status == "failed"
    assert response.error_code == "invalid_bridge_output"


def test_adapter_rejects_a_fake_mp4_prefix_without_valid_boxes(tmp_path: Path) -> None:
    """A video signature prefix cannot bypass bounded container validation."""
    body = b"\x00\x00\x00\x18ftypisomnot-a-container"
    candidate: dict[str, object] = {
        "note_id": "note_a",
        "role": "video",
        "position": 1,
        "status": "downloaded",
        "staging_name": "note_a-video.mp4",
        "mime_type": "video/mp4",
        "size_bytes": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
        "duration_ms": 1000,
    }

    result = _invoke_media_candidate(tmp_path, candidate, body)

    assert result.status == "failed"
    assert result.error_code == "unsafe_staging_asset"


def test_adapter_accepts_a_webm_with_an_unknown_size_segment(tmp_path: Path) -> None:
    """A bounded valid header may be followed by an unknown-size WebM Segment."""
    body = b"\x1aE\xdf\xa3\x87B\x82\x84webm\x18S\x80g\xff"
    candidate: dict[str, object] = {
        "note_id": "note_a",
        "role": "video",
        "position": 1,
        "status": "downloaded",
        "staging_name": "note_a-video.webm",
        "mime_type": "video/webm",
        "size_bytes": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
        "duration_ms": 1000,
    }

    result = _invoke_media_candidate(tmp_path, candidate, body)

    assert result.status == "complete"


def test_adapter_timeout_error_does_not_echo_request_or_environment_values(tmp_path: Path) -> None:
    def timeout_runner(*_args: object, **_kwargs: object) -> object:
        raise subprocess.TimeoutExpired("bridge", 30, output=b"unsafe")

    secret_keyword = "https://example.invalid/private?token=secret"
    staging = tmp_path / "private-staging"
    adapter = IsolatedXhsAdapter(
        runner=timeout_runner,
        auth_dir=tmp_path / "profile-private",
    )

    response = adapter.collect_search(secret_keyword, 1, staging)
    rendered = response.model_dump_json()

    assert response.error_code == "bridge_timeout"
    for forbidden in (secret_keyword, str(staging), "profile-private", "unsafe"):
        assert forbidden not in rendered
