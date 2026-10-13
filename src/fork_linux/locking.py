"""Exclusive, non-blocking inter-process lock (``flock``) for setup/update/rollback/uninstall.

The lock file holds ``{"pid", "purpose", "started"}`` of the current holder so a
second invocation can say who is busy. The kernel drops the lock when the
holder exits, so a crashed run never leaves a stale lock behind; the file is
never unlinked (that would race with a waiting process).
"""

from __future__ import annotations

import fcntl
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from types import TracebackType

from .errors import ForkLinuxError, Locked


class FileLock:
    """``with FileLock(paths.lock_file, "setup"):`` -- raises :class:`Locked` when busy."""

    def __init__(self, path: Path, purpose: str) -> None:
        self.path = Path(path)
        self.purpose = purpose
        self._fd: int | None = None

    @property
    def held(self) -> bool:
        """True while this object holds the lock."""
        return self._fd is not None

    def acquire(self) -> FileLock:
        """Take the lock or raise :class:`Locked` naming the current holder."""
        if self._fd is not None:
            raise RuntimeError(f"lock {self.path} is already held by this process")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
        except OSError as exc:
            raise ForkLinuxError(
                f"cannot open the lock file {self.path}: {exc.strerror or exc}",
                hint="check that the directory is writable and the lock file is not a symbolic link",
            ) from exc
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            holder = _read_holder(fd)
            os.close(fd)
            raise Locked(self._busy_message(holder), hint=_busy_hint(holder)) from None
        except BaseException:
            os.close(fd)
            raise
        info = {
            "pid": os.getpid(),
            "purpose": self.purpose,
            "started": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        }
        os.ftruncate(fd, 0)
        os.pwrite(fd, json.dumps(info).encode("utf-8"), 0)
        self._fd = fd
        return self

    def release(self) -> None:
        """Drop the lock (no-op if not held)."""
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            os.ftruncate(fd, 0)
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def __enter__(self) -> FileLock:
        return self.acquire()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()

    def _busy_message(self, holder: dict[str, object] | None) -> str:
        """``... held by pid N (purpose) since T``."""
        prefix = f"another fork-linux operation is in progress (lock {self.path})"
        if holder is None:
            return f"{prefix}: held by another process"
        pid = holder.get("pid", "?")
        purpose = holder.get("purpose", "?")
        started = holder.get("started", "?")
        return f"{prefix}: held by pid {pid} ({purpose}) since {started}"


def _busy_hint(holder: dict[str, object] | None) -> str:
    """What the user can do about a busy lock."""
    if holder is not None and "pid" in holder:
        return f"wait for it to finish, or stop process {holder['pid']} if it is stuck"
    return "wait for the other fork-linux command to finish"


def _read_holder(fd: int) -> dict[str, object] | None:
    """The holder record written by :meth:`FileLock.acquire`, or ``None`` if unreadable."""
    try:
        data = json.loads(os.pread(fd, 4096, 0).decode("utf-8"))
    except ValueError:
        return None
    return data if isinstance(data, dict) else None
