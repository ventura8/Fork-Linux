"""Tests for fork_linux.locking (100% branch coverage gate)."""

from __future__ import annotations

import errno
import fcntl
import json
import os
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from fork_linux import locking
from fork_linux.errors import ExitCode, ForkLinuxError, Locked
from fork_linux.locking import FileLock

SRC = Path(__file__).resolve().parent.parent / "src"


def test_acquire_writes_holder_record(tmp_path: Path) -> None:
    lock_path = tmp_path / "run" / "fork-linux" / "setup.lock"
    lock = FileLock(lock_path, "setup")
    assert not lock.held
    assert lock.acquire() is lock
    assert lock.held
    record = json.loads(lock_path.read_text(encoding="utf-8"))
    assert record["pid"] == os.getpid()
    assert record["purpose"] == "setup"
    assert "T" in record["started"]
    assert stat.S_IMODE(lock_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(lock_path.parent.stat().st_mode) == 0o700
    lock.release()
    assert not lock.held
    assert lock_path.exists(), "the lock file is never unlinked"
    assert lock_path.read_text(encoding="utf-8") == ""


def test_context_manager(tmp_path: Path) -> None:
    lock_path = tmp_path / "setup.lock"
    with FileLock(lock_path, "update") as lock:
        assert lock.held
    assert not lock.held


def _fail_while_holding(lock: FileLock) -> None:
    with lock:
        raise ValueError("boom")


def test_context_manager_releases_on_error(tmp_path: Path) -> None:
    lock_path = tmp_path / "setup.lock"
    lock = FileLock(lock_path, "rollback")
    with pytest.raises(ValueError):
        _fail_while_holding(lock)
    assert not lock.held
    with FileLock(lock_path, "again") as lock:
        assert lock.held


def test_busy_lock_names_the_holder(tmp_path: Path) -> None:
    lock_path = tmp_path / "setup.lock"
    with FileLock(lock_path, "setup"):
        contender = FileLock(lock_path, "uninstall")
        with pytest.raises(Locked) as info:
            contender.acquire()
    err = info.value
    assert err.exit_code == ExitCode.LOCKED
    assert f"held by pid {os.getpid()} (setup) since " in err.message
    assert str(lock_path) in err.message
    assert str(os.getpid()) in err.hint
    with FileLock(lock_path, "after"):
        pass


def test_busy_lock_held_by_another_process(tmp_path: Path) -> None:
    lock_path = tmp_path / "setup.lock"
    script = textwrap.dedent(
        f"""
        import sys
        sys.path.insert(0, {str(SRC)!r})
        from fork_linux.locking import FileLock
        with FileLock({str(lock_path)!r}, "child-setup"):
            print("locked", flush=True)
            sys.stdin.readline()
        """
    )
    child = subprocess.Popen(
        [sys.executable, "-c", script], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "locked"
        contender = FileLock(lock_path, "setup")
        with pytest.raises(Locked, match=rf"held by pid {child.pid} \(child-setup\)"):
            contender.acquire()
    finally:
        child.communicate("\n", timeout=30)
    with FileLock(lock_path, "setup") as lock:
        assert lock.held


def _hold_raw(lock_path: Path, content: bytes) -> int:
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    os.write(fd, content)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return fd


@pytest.mark.parametrize("content", [b"", b"not json", b"[1, 2]", b"\xff\xfe"])
def test_unreadable_holder_record(tmp_path: Path, content: bytes) -> None:
    lock_path = tmp_path / "setup.lock"
    fd = _hold_raw(lock_path, content)
    try:
        contender = FileLock(lock_path, "setup")
        with pytest.raises(Locked) as info:
            contender.acquire()
    finally:
        os.close(fd)
    assert info.value.message.endswith("held by another process")
    assert info.value.hint == "wait for the other fork-linux command to finish"


def test_partial_holder_record(tmp_path: Path) -> None:
    lock_path = tmp_path / "setup.lock"
    fd = _hold_raw(lock_path, json.dumps({"purpose": "doctor"}).encode())
    try:
        contender = FileLock(lock_path, "setup")
        with pytest.raises(Locked) as info:
            contender.acquire()
    finally:
        os.close(fd)
    assert "held by pid ? (doctor) since ?" in info.value.message
    assert info.value.hint == "wait for the other fork-linux command to finish"


def test_double_acquire_is_a_programming_error(tmp_path: Path) -> None:
    lock = FileLock(tmp_path / "setup.lock", "setup")
    with lock:
        with pytest.raises(RuntimeError, match="already held"):
            lock.acquire()


def test_release_without_acquire_is_a_no_op(tmp_path: Path) -> None:
    lock = FileLock(tmp_path / "setup.lock", "setup")
    lock.release()
    assert not lock.held


def test_unexpected_flock_error_closes_and_propagates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    closed: list[int] = []
    real_close = os.close

    def broken_flock(fd: int, operation: int) -> None:
        raise OSError(errno.ENOLCK, "No locks available")

    def tracking_close(fd: int) -> None:
        closed.append(fd)
        real_close(fd)

    monkeypatch.setattr(locking.fcntl, "flock", broken_flock)
    monkeypatch.setattr(locking.os, "close", tracking_close)
    contender = FileLock(tmp_path / "setup.lock", "setup")
    with pytest.raises(OSError, match="No locks available"):
        contender.acquire()
    assert len(closed) == 1


def test_symlinked_lock_file_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere"
    target.write_text("keep", encoding="utf-8")
    link = tmp_path / "setup.lock"
    link.symlink_to(target)
    contender = FileLock(link, "setup")
    with pytest.raises(ForkLinuxError, match="cannot open the lock file") as info:
        contender.acquire()
    assert not isinstance(info.value, Locked)
    assert "symbolic link" in info.value.hint
    assert target.read_text(encoding="utf-8") == "keep"
