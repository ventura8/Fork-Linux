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


UPDATE_CMD = ["C:\\users\\tester\\AppData\\Local\\Fork\\Update.exe", "apply", "--waitPid", "201", "--restart"]


def test_f10_velopack_apply_counts_as_busy(proc: Path, prefix: Path) -> None:
    """UPD-3: Velopack's Update.exe replacing current\\ means Fork is busy, though no Fork.exe runs."""
    env = {"WINEPREFIX": str(prefix)}
    _process(proc, 401, env=env, cmdline=UPDATE_CMD)
    _process(proc, 402, env=env, cmdline=["wine", "c:/users/tester/appdata/local/fork/UPDATE.EXE", "patch"])
    _process(proc, 403, env=env, cmdline=["C:\\Program Files\\Other\\Update.exe", "apply"])
    _process(proc, 404, env={"WINEPREFIX": "/elsewhere"}, cmdline=UPDATE_CMD)
    _process(proc, 405, env=env, cmdline=None)
    assert procs.updater_pids(prefix, proc_root=proc) == [401, 402]
    assert procs.fork_pids(prefix, proc_root=proc) == []
    assert procs.fork_running(prefix, proc_root=proc) is False
    with pytest.raises(ForkRunning, match=r"updating itself .*pid 401, 402") as exc:
        procs.require_closed(prefix, proc_root=proc)
    assert "wait until Fork has restarted" in exc.value.hint


def test_wait_for_updater(proc: Path, prefix: Path) -> None:
    updater = _process(proc, 501, env={"WINEPREFIX": str(prefix)}, cmdline=UPDATE_CMD)
    clock = [0.0]
    slept: list[float] = []

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        clock[0] += seconds
        if len(slept) == 3 and (updater / "cmdline").exists():
            (updater / "cmdline").unlink()  # Velopack is done

    def now() -> float:
        return clock[0]

    assert procs.wait_for_updater(prefix, timeout=10, proc_root=proc, sleep=sleep, clock=now) is True
    assert slept == [0.5, 0.5, 0.5]
    _process(proc, 502, env={"WINEPREFIX": str(prefix)}, cmdline=UPDATE_CMD)
    slept.clear()
    assert procs.wait_for_updater(prefix, timeout=1, proc_root=proc, sleep=sleep, clock=now, poll=0.4) is False
    assert slept == [0.4, 0.4, 0.4]


def test_wait_for_updater_defaults_to_real_time(proc: Path, prefix: Path) -> None:
    assert procs.wait_for_updater(prefix, timeout=0, proc_root=proc) is True


def test_wineserver_exes(proc: Path, prefix: Path, tmp_path: Path) -> None:
    env = {"WINEPREFIX": str(prefix)}
    server = tmp_path / "wine-a" / "bin" / "wineserver"
    (_process(proc, 601, env=env, cmdline=["wineserver"]) / "exe").symlink_to(server)
    (_process(proc, 602, env=env, cmdline=["wineserver"]) / "exe").symlink_to(server)
    preloader = tmp_path / "wine-a" / "bin" / "wine64-preloader"
    (_process(proc, 603, env=env, cmdline=FORK_CMD) / "exe").symlink_to(preloader)
    _process(proc, 604, env=env, cmdline=["wineserver"])  # exe unreadable
    (_process(proc, 605, env={"WINEPREFIX": "/x"}, cmdline=["wineserver"]) / "exe").symlink_to(server)
    assert procs.wineserver_exes(prefix, proc_root=proc) == [server]


def test_running_matches_program_names(proc: Path) -> None:
    (_process(proc, 301, env=None, cmdline=None) / "comm").write_bytes(b"Thunar\n")
    (_process(proc, 302, env=None, cmdline=None) / "comm").write_bytes(b"thunar-volman\n")
    (_process(proc, 303, env=None, cmdline=None) / "comm").write_bytes(b"gnome-terminal-\n")
    _process(proc, 304, env=None, cmdline=None)  # no comm: vanished or unreadable
    assert procs.running(["thunar"], proc_root=proc) == [301]
    # Program names longer than the kernel keeps still match.
    assert procs.running(["gnome-terminal-server"], proc_root=proc) == [303]
    assert procs.running([], proc_root=proc) == []


class _Clock:
    """A clock that advances by ``step`` seconds per reading."""

    def __init__(self, step: float) -> None:
        self.now = 0.0
        self.step = step

    def __call__(self) -> float:
        self.now += self.step
        return self.now


def test_stop_prefix_terms_then_kills_what_is_left(proc: Path, prefix: Path) -> None:
    # E2E: an interrupted setup left winetricks starting Wine programs; deleting the prefix then failed.
    env = {"WINEPREFIX": str(prefix)}
    _process(proc, 301, env=env, cmdline=["/bin/bash", "winetricks", "dotnet48"])
    _process(proc, 302, env=env, cmdline=["wineserver"])
    _process(proc, 303, env={"WINEPREFIX": "/elsewhere"}, cmdline=["wine"])
    sent: list[tuple[int, int]] = []

    def kill(pid: int, number: int) -> None:
        sent.append((pid, number))
        if pid == 302:
            # Gone already (it exited after wineserver -k): the /proc entry goes with it.
            (proc / str(pid) / "environ").unlink()
            raise ProcessLookupError(pid)
        if number == procs.signal.SIGKILL:
            (proc / str(pid) / "environ").unlink()

    stopped = procs.stop_prefix(prefix, proc_root=proc, kill=kill, sleep=lambda _s: None, clock=_Clock(1.0))
    assert stopped == [301, 302]
    # SIGTERM was ignored: SIGKILL once the grace period is over; 303 is another prefix's.
    assert (301, procs.signal.SIGTERM) in sent
    assert (301, procs.signal.SIGKILL) in sent
    assert all(pid != 303 for pid, _number in sent)


def test_stop_prefix_gives_up_after_its_timeout(proc: Path, prefix: Path) -> None:
    _process(proc, 401, env={"WINEPREFIX": str(prefix)}, cmdline=["wine"])
    with pytest.raises(procs.ForkLinuxError, match="processes still run in .*: 401"):
        procs.stop_prefix(prefix, proc_root=proc, kill=lambda _pid, _number: None, sleep=lambda _s: None,
                          clock=_Clock(10.0))
    assert procs.stop_prefix(prefix / "nothing-here", proc_root=proc) == []


def _stat(proc: Path, pid: int, parent: int, comm: str = "bash") -> None:
    directory = proc / str(pid)
    directory.mkdir(exist_ok=True)
    (directory / "stat").write_text(f"{pid} ({comm}) S {parent} {pid} {pid} 0 -1\n", encoding="utf-8")


def test_stop_targets_spare_the_users_shell_and_our_ancestors(proc: Path, prefix: Path) -> None:
    """A shell the user exported WINEPREFIX in runs 'fork-linux setup --reset': it is never signalled."""
    env = {"WINEPREFIX": str(prefix)}
    me = os.getpid()
    # fork-linux runs under a shell (500), which a terminal emulator named like a Wine tool (501) started.
    _stat(proc, me, 500)
    _process(proc, 500, env=env, cmdline=["-bash"])
    _stat(proc, 500, 501)
    _process(proc, 501, env=env, cmdline=["wine-terminal"])
    _stat(proc, 501, 1, comm="wine) S 1 (odd")
    # Another shell and an editor of the user, then Wine's own processes and winetricks.
    _process(proc, 510, env=env, cmdline=["/usr/bin/vim", "notes.txt"])
    _process(proc, 520, env=env, cmdline=["C:\\windows\\system32\\explorer.exe", "/desktop"])
    _process(proc, 521, env=env, cmdline=["/bin/sh", "/opt/runtimes/winetricks/x/winetricks", "dotnet48"])
    named = _process(proc, 522, env=env, cmdline=[])
    (named / "comm").write_text("Fork.exe\n", encoding="utf-8")
    binary = _process(proc, 523, env=env, cmdline=None)
    (binary / "exe").symlink_to("/opt/runtimes/wine/x/bin/wine64-preloader")
    assert procs.stop_targets(prefix, proc_root=proc) == [520, 521, 522, 523]
    sent: list[int] = []

    def kill(pid: int, _number: int) -> None:
        sent.append(pid)
        (proc / str(pid) / "environ").unlink()

    assert procs.stop_prefix(prefix, proc_root=proc, kill=kill, sleep=lambda _s: None) == [520, 521, 522, 523]
    assert sorted(sent) == [520, 521, 522, 523]
    assert procs.prefix_pids(prefix, proc_root=proc) == [500, 501, 510]


@pytest.mark.parametrize("stat", ["", "1 (x)", "1 (x) S", "1 (x) S -", "garbage"])
def test_parent_of_a_malformed_stat(proc: Path, stat: str) -> None:
    (proc / "7").mkdir()
    (proc / "7" / "stat").write_text(stat, encoding="utf-8")
    assert procs._parent(proc, 7) is None
    assert procs._parent(proc, 8) is None
    assert procs._ancestors(proc, 7) == set()


def test_ancestors_stop_at_a_loop(proc: Path) -> None:
    _stat(proc, 11, 12)
    _stat(proc, 12, 11)
    assert procs._ancestors(proc, 11) == {11, 12}
