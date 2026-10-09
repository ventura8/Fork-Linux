"""A fake git-bridge build for the Python tests: shims, the fake daemon and a fake host git.

:func:`install` points ``FORK_LINUX_SHIMS_DIR`` / ``FORK_LINUX_LIBEXEC_DIR`` at a temporary
build holding ``fl-shim.exe`` / ``fl-launch.exe`` (tiny ``MZ`` files), a copy of
``tests/fakes/bridge/fl-bridge-helper`` (the fake daemon, run by this Python) and an
executable ``fork-linux-host``. :func:`git_runner` answers ``git --version`` like a host git.
:func:`kill_daemons` stops every fake daemon recorded in ``$FL_FAKE_DAEMON_LOG``.
"""

from __future__ import annotations

import json
import os
import signal
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from fork_linux import bridge, resources
from fork_linux.procrun import RecordingRunner

FAKE_HELPER = Path(__file__).resolve().parent.parent / "fakes" / "bridge" / "fl-bridge-helper"


@dataclass
class Kit:
    """Where the fake build lives."""

    shims: Path
    libexec: Path
    helper: Path
    log: Path
    git: Path

    def starts(self) -> list[dict[str, object]]:
        """One record per fake daemon start."""
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]


def install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, host_helper: bool = True) -> Kit:
    """Create the fake build and point fork-linux at it."""
    shims = tmp_path / "bridge-build"
    shims.mkdir(exist_ok=True)
    (shims / bridge.SHIM).write_bytes(b"MZ shim")
    (shims / bridge.LAUNCH).write_bytes(b"MZ launch")
    libexec = tmp_path / "bridge-libexec"
    libexec.mkdir(exist_ok=True)
    helper = libexec / bridge.HELPER
    source = FAKE_HELPER.read_text(encoding="utf-8").split("\n", 1)[1]
    helper.write_text(f"#!{sys.executable}\n{source}", encoding="utf-8")
    helper.chmod(0o755)
    if host_helper:
        host = libexec / resources.HOST_HELPER
        host.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        host.chmod(0o755)
    git = tmp_path / "host-bin" / "git"
    git.parent.mkdir(exist_ok=True)
    git.write_text("#!/bin/sh\n", encoding="utf-8")
    log = tmp_path / "fake-daemon.jsonl"
    monkeypatch.setenv(resources.SHIMS_ENV, str(shims))
    monkeypatch.setenv(resources.LIBEXEC_ENV, str(libexec))
    monkeypatch.setenv("FL_FAKE_DAEMON_LOG", str(log))
    return Kit(shims, libexec, helper, log, git)


def git_runner(kit: Kit, version: str = "2.53.0", runner: RecordingRunner | None = None) -> RecordingRunner:
    """A runner whose ``git`` (one file per version, so the version cache never mixes them up)
    answers ``git version <version>``."""
    runner = RecordingRunner() if runner is None else runner
    git = kit.git.parent / version / "git"
    git.parent.mkdir(parents=True, exist_ok=True)
    git.write_text("#!/bin/sh\n", encoding="utf-8")
    runner.which_map["git"] = str(git)
    runner.responses["git"] = f"git version {version}\n"
    return runner


def kill_daemons(kit: Kit) -> None:
    """SIGKILL every fake daemon that is still running."""
    for start in kit.starts():
        pid = start["pid"]
        assert isinstance(pid, int)
        try:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
        except (ProcessLookupError, ChildProcessError):
            pass
