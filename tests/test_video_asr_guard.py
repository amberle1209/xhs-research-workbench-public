"""The ASR subprocess cannot outlive its worker or release the single-task lock early."""

from __future__ import annotations

import errno
import fcntl
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest


@pytest.mark.parametrize("termination", [signal.SIGTERM, signal.SIGKILL])
def test_killed_worker_cannot_orphan_asr_or_release_lock_while_it_runs(
    tmp_path: Path, termination: signal.Signals,
) -> None:
    """An actual worker-side process dies while its actual ASR child sleeps."""
    child_pid_file = tmp_path / "child.pid"
    lock_path = tmp_path / "video.lock"
    sleeper = (
        "import os,sys,time;from pathlib import Path;"
        "Path(sys.argv[1]).write_text(str(os.getpid()));time.sleep(8)"
    )
    harness = textwrap.dedent(
        """
        import fcntl, os, sys, time
        from pathlib import Path
        from xhs_workbench import video_processing as processing
        child_pid_file, lock_path, run_dir, sleeper = sys.argv[1:]
        lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        media_fd = os.open(Path(run_dir) / "media.bin", os.O_RDONLY)
        run_fd = os.open(run_dir, os.O_RDONLY | os.O_DIRECTORY)
        processing._asr_command = lambda *_args: [sys.executable, "-c", sleeper, child_pid_file]
        processing._invoke_asr("probe", media_fd, run_fd, "job_123", Path(run_dir),
                               time.monotonic() + 30, lock_fd=lock_fd)
        """
    )
    (tmp_path / "media.bin").write_bytes(b"media")
    worker = subprocess.Popen(
        [sys.executable, "-c", harness, str(child_pid_file), str(lock_path), str(tmp_path), sleeper],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 3
        while not child_pid_file.exists() and time.monotonic() < deadline:
            if worker.poll() is not None:
                break
            time.sleep(.02)
        assert child_pid_file.exists(), "ASR child did not start"
        child_pid = int(child_pid_file.read_text())
        assert _pid_exists(child_pid)
        worker.send_signal(termination)
        worker.wait(timeout=3)
        deadline = time.monotonic() + 3
        while _pid_exists(child_pid) and time.monotonic() < deadline:
            lock_probe = os.open(lock_path, os.O_RDWR | os.O_NOFOLLOW)
            try:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(lock_probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(lock_probe)
            time.sleep(.03)
        assert not _pid_exists(child_pid), "ASR child remained after worker death"
        lock_probe = os.open(lock_path, os.O_RDWR | os.O_NOFOLLOW)
        try:
            fcntl.flock(lock_probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(lock_probe)
    finally:
        if worker.poll() is None:
            worker.kill()
            worker.wait(timeout=3)


def test_killed_guard_cannot_orphan_real_asr_or_release_lock_early(tmp_path: Path) -> None:
    """The real ASR entrypoint watches its worker even when its guard is killed."""
    child_pid_file = tmp_path / "child-and-guard.pid"
    lock_path = tmp_path / "video.lock"
    (tmp_path / "media.bin").write_bytes(b"media")
    slow_probe = textwrap.dedent(
        """
        import os, sys, time
        from pathlib import Path
        from xhs_workbench import video_asr

        pid_file, media_path = sys.argv[1:]
        def probe(_path):
            Path(pid_file).write_text(f"{os.getpid()} {os.getppid()}")
            time.sleep(8)
            return {"duration_ms": 1000, "audio_present": True}
        video_asr.probe_video = probe
        sys.argv = ["video_asr", "probe", media_path]
        raise SystemExit(video_asr.main())
        """
    )
    harness = textwrap.dedent(
        """
        import fcntl, os, sys, time
        from pathlib import Path
        from xhs_workbench import video_processing as processing
        child_pid_file, lock_path, run_dir, slow_probe = sys.argv[1:]
        lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        media_fd = os.open(Path(run_dir) / "media.bin", os.O_RDONLY)
        run_fd = os.open(run_dir, os.O_RDONLY | os.O_DIRECTORY)
        processing._asr_command = lambda *_args: [
            sys.executable, "-c", slow_probe, child_pid_file, str(Path(run_dir) / "media.bin")
        ]
        try:
            processing._invoke_asr("probe", media_fd, run_fd, "job_123", Path(run_dir),
                                   time.monotonic() + 30, lock_fd=lock_fd)
        except ValueError:
            pass
        """
    )
    worker = subprocess.Popen(
        [sys.executable, "-c", harness, str(child_pid_file), str(lock_path),
         str(tmp_path), slow_probe],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    child_pid = 0
    try:
        deadline = time.monotonic() + 3
        while not child_pid_file.exists() and time.monotonic() < deadline:
            if worker.poll() is not None:
                break
            time.sleep(.02)
        assert child_pid_file.exists(), "real ASR entrypoint did not start"
        child_pid, guard_pid = map(int, child_pid_file.read_text().split())
        assert _pid_exists(child_pid) and _pid_exists(guard_pid)
        os.kill(guard_pid, signal.SIGKILL)
        worker.wait(timeout=3)
        deadline = time.monotonic() + 3
        while _pid_exists(child_pid) and time.monotonic() < deadline:
            lock_probe = os.open(lock_path, os.O_RDWR | os.O_NOFOLLOW)
            try:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(lock_probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(lock_probe)
            time.sleep(.02)
        assert not _pid_exists(child_pid), "real ASR survived guard death"
        lock_probe = os.open(lock_path, os.O_RDWR | os.O_NOFOLLOW)
        try:
            fcntl.flock(lock_probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(lock_probe)
    finally:
        if worker.poll() is None:
            worker.kill()
            worker.wait(timeout=3)
        if child_pid and _pid_exists(child_pid):
            os.kill(child_pid, signal.SIGKILL)


def test_asr_self_watchdog_enforces_absolute_deadline_with_worker_alive(tmp_path: Path) -> None:
    lock_path = tmp_path / "video.lock"
    pid_file = tmp_path / "child.pid"
    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(lock_fd, fcntl.LOCK_EX)
    owner_read_fd, owner_write_fd = os.pipe()
    env = os.environ.copy()
    env.update({
        "XHS_ASR_OWNER_FD": str(owner_read_fd),
        "XHS_ASR_LOCK_FD": str(lock_fd),
        "XHS_ASR_DEADLINE_NS": str(time.monotonic_ns() + 500_000_000),
    })
    script = (
        "import os,sys,time;from pathlib import Path;"
        "from xhs_workbench.video_asr import _start_owner_watchdog;"
        "_start_owner_watchdog();"
        "Path(sys.argv[1]).write_text(str(os.getpid()));time.sleep(8)"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", script, str(pid_file)], env=env,
        pass_fds=(owner_read_fd, lock_fd), stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    os.close(owner_read_fd)
    os.close(lock_fd)
    try:
        deadline = time.monotonic() + 2
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        assert pid_file.exists()
        lock_probe = os.open(lock_path, os.O_RDWR | os.O_NOFOLLOW)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(lock_probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            assert child.wait(timeout=2) == 1
            fcntl.flock(lock_probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(lock_probe)
    finally:
        os.close(owner_write_fd)
        if child.poll() is None:
            child.kill()
            child.wait(timeout=3)


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError as error:
        return error.errno != errno.ESRCH
    return True
