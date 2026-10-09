"""Tests for fork_linux.procs: finding Fork in our prefix through a (fake) /proc."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from fork_linux import procs
from fork_linux.errors import ExitCode, ForkRunning

FORK_CMD = ["C:\\users\\tester\\AppData\\Local\\Fork\\current\\Fork.exe", "Z:\\home\\tester\\repo"]


@pytest.fixture
def prefix(tmp_path: Path) -> Path:
    path = tmp_path / "data" / "prefix"
    path.mkdir(parents=True)
    return path


@pytest.fixture
def proc(tmp_path: Path) -> Path:
    root = tmp_path / "proc"
    root.mkdir()
    return root


def _process(proc: Path, pid: int | str, *, env: dict[str, str] | None, cmdline: list[str] | None) -> Path:
    directory = proc / str(pid)
    directory.mkdir()
    if env is not None:
        block = b"".join(f"{key}={value}".encode() + b"\0" for key, value in env.items())
        (directory / "environ").write_bytes(block)
    if cmdline is not None:
        (directory / "cmdline").write_bytes(b"".join(arg.encode() + b"\0" for arg in cmdline))
    return directory


def test_prefix_pids_matches_resolved_wineprefix(proc: Path, prefix: Path, tmp_path: Path) -> None:
    alias = tmp_path / "alias"
    alias.symlink_to(prefix)
    _process(proc, 101, env={"HOME": "/home/t", "WINEPREFIX": str(prefix)}, cmdline=["wineserver"])
    _process(proc, 102, env={"WINEPREFIX": f"{prefix}/"}, cmdline=FORK_CMD)
    _process(proc, 103, env={"WINEPREFIX": str(alias)}, cmdline=FORK_CMD)
    _process(proc, 104, env={"WINEPREFIX": str(tmp_path / "other")}, cmdline=FORK_CMD)
    _process(proc, 105, env={"WINEPREFIX": "data/prefix"}, cmdline=FORK_CMD)
    _process(proc, 106, env={"PATH": "/usr/bin"}, cmdline=FORK_CMD)
    _process(proc, 107, env=None, cmdline=FORK_CMD)
    _process(proc, os.getpid(), env={"WINEPREFIX": str(prefix)}, cmdline=FORK_CMD)
    (proc / "self").mkdir()
    (proc / "12a").mkdir()
    (proc / "\u0661\u0662").mkdir()
    (proc / "uptime").write_text("1 2")
    assert procs.prefix_pids(prefix, proc_root=proc) == [101, 102, 103]
    assert procs.prefix_pids(alias, proc_root=proc) == [101, 102, 103]


def test_fork_pids_filters_on_command_line(proc: Path, prefix: Path) -> None:
    env = {"WINEPREFIX": str(prefix)}
    _process(proc, 201, env=env, cmdline=FORK_CMD)
    _process(proc, 202, env=env, cmdline=["C:\\USERS\\T\\FORK\\CURRENT\\FORK.EXE"])
    _process(proc, 203, env=env, cmdline=["C:\\users\\t\\Fork\\Update.exe", "apply", "--exeName", "Fork.exe"])
    _process(proc, 204, env=env, cmdline=["C:\\users\\t\\Fork\\current\\Fork.RI.exe"])
    _process(proc, 205, env=env, cmdline=["/opt/wine/bin/wineserver"])
    _process(proc, 206, env=env, cmdline=None)
    _process(proc, 207, env=env, cmdline=["wine", "C:\\users\\t\\Fork\\current\\Fork.exe"])
    assert procs.fork_pids(prefix, proc_root=proc) == [201, 202, 207]
    assert procs.fork_running(prefix, proc_root=proc) is True


def test_nothing_running(proc: Path, prefix: Path) -> None:
    _process(proc, 301, env={"WINEPREFIX": str(prefix)}, cmdline=["wineserver"])
    assert procs.fork_pids(prefix, proc_root=proc) == []
    assert procs.fork_running(prefix, proc_root=proc) is False
    procs.require_closed(prefix, proc_root=proc)


def test_require_closed_raises(proc: Path, prefix: Path) -> None:
    _process(proc, 401, env={"WINEPREFIX": str(prefix)}, cmdline=FORK_CMD)
    _process(proc, 402, env={"WINEPREFIX": str(prefix)}, cmdline=FORK_CMD)
    with pytest.raises(ForkRunning, match=r"pid 401, 402") as info:
        procs.require_closed(prefix, proc_root=proc)
    assert "close Fork first" in info.value.hint
    assert info.value.exit_code == ExitCode.FORK_RUNNING


def test_other_users_processes_are_ignored(proc: Path, prefix: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _process(proc, 501, env={"WINEPREFIX": str(prefix)}, cmdline=FORK_CMD)
    real_uid = os.getuid()
    monkeypatch.setattr(procs.os, "getuid", lambda: real_uid + 1)
    assert procs.fork_pids(prefix, proc_root=proc) == []


def test_missing_proc_root(tmp_path: Path, prefix: Path) -> None:
    assert procs.prefix_pids(prefix, proc_root=tmp_path / "no-proc") == []


class _VanishedEntry:
    """A /proc entry whose process exited between listing and stat()."""

    name = "601"
    path = "/proc/601"

    def stat(self, *, follow_symlinks: bool = True) -> Any:
        raise FileNotFoundError(2, "No such file or directory")


def test_vanished_entries_are_skipped(proc: Path, prefix: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(procs.os, "scandir", lambda _root: [_VanishedEntry()])
    assert procs.prefix_pids(prefix, proc_root=proc) == []


def test_real_proc_scan_finds_nothing_in_a_fresh_prefix(prefix: Path) -> None:
    assert procs.fork_running(prefix) is False
    assert procs.prefix_pids(prefix) == []


def test_read_is_capped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "environ"
    path.write_bytes(b"A" * 100)
    monkeypatch.setattr(procs, "MAX_BYTES", 10)
    assert procs._read(path) == b"A" * 10
