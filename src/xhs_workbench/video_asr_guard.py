"""Own one ASR child after its native worker exits or is killed.

The worker passes a pipe read end and its held video lock to this process.
Only the worker holds the pipe write end. EOF therefore means the worker died,
including SIGKILL; this guard stops and reaps its own child before releasing
the inherited lock. No PID from a file or untrusted message is ever signalled.
"""

from __future__ import annotations

import os
import select
import signal
import subprocess
import sys
import time
from collections.abc import Sequence


class _OwnerGone(Exception):
    pass


def _owner_gone(owner_fd: int) -> bool:
    ready, _, _ = select.select([owner_fd], [], [], 0)
    return bool(ready) and os.read(owner_fd, 1) == b""


def _stop_child(child: subprocess.Popen[bytes]) -> None:
    if child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=3)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=3)
    if child.stdout is not None:
        child.stdout.close()


def guard(owner_fd: int, timeout_ms: int, media_fd: int, lock_fd: int,
          command: Sequence[str]) -> int:
    """Forward only the bounded child result; stop it on parent death/deadline."""
    if not 1 <= timeout_ms <= 1_800_000 or owner_fd < 0 or lock_fd < 0 or not command:
        return 1
    if media_fd >= 0:
        os.fstat(media_fd)
    os.fstat(lock_fd)
    os.fstat(owner_fd)
    termination_requested = False

    def on_terminate(_number: int, _frame: object) -> None:
        nonlocal termination_requested
        termination_requested = True

    signal.signal(signal.SIGTERM, on_terminate)
    deadline = time.monotonic() + timeout_ms / 1000
    child: subprocess.Popen[bytes] | None = None
    try:
        if termination_requested or _owner_gone(owner_fd):
            raise _OwnerGone
        child_env = os.environ.copy()
        child_env.update({
            "XHS_ASR_OWNER_FD": str(owner_fd),
            "XHS_ASR_LOCK_FD": str(lock_fd),
            "XHS_ASR_DEADLINE_NS": str(time.monotonic_ns() + timeout_ms * 1_000_000),
        })
        child = subprocess.Popen(
            list(command), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, close_fds=True,
            pass_fds=tuple(sorted({fd for fd in (media_fd, owner_fd, lock_fd) if fd >= 0})),
            env=child_env,
        )
        if child.stdout is None:
            raise OSError("ASR stdout unavailable")
        output = bytearray()
        stdout_open = True
        while True:
            if termination_requested or _owner_gone(owner_fd):
                raise _OwnerGone
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            if child.poll() is not None and not stdout_open:
                break
            readable, _, _ = select.select(
                [owner_fd, child.stdout] if stdout_open else [owner_fd],
                [], [], min(.1, remaining),
            )
            if owner_fd in readable and os.read(owner_fd, 1) == b"":
                raise _OwnerGone
            if stdout_open and child.stdout in readable:
                chunk = os.read(child.stdout.fileno(), 64 * 1024)
                if chunk:
                    output.extend(chunk)
                    if len(output) > 1024 * 1024:
                        raise ValueError("ASR output exceeds limit")
                else:
                    stdout_open = False
        child.stdout.close()
        sys.stdout.buffer.write(output)
        sys.stdout.buffer.flush()
        return child.returncode or 0
    except BaseException:  # noqa: BLE001 - worker death and SIGTERM must reap the child.
        if child is not None:
            _stop_child(child)
        return 1
    finally:
        for descriptor in (owner_fd, media_fd, lock_fd):
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass


def main() -> int:
    args = sys.argv[1:]
    if len(args) < 6 or args[0] != "run":
        return 1
    try:
        return guard(int(args[1]), int(args[2]), int(args[3]), int(args[4]), args[5:])
    except (OSError, ValueError):
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
