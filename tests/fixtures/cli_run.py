"""Helpers for the command tests (tests/test_cmd_*.py): an isolated CLI run and Fork's files."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from fork_linux import cli
from fork_linux.fork_layout import ForkLayout
from fork_linux.paths import Paths

USER = "tester"


def isolate(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never root, user :data:`USER`, no FORK_LINUX_* overrides, no display, no desktop probes."""
    monkeypatch.setattr(cli, "_is_root", lambda: False)
    for name in list(os.environ):
        if name.startswith("FORK_LINUX_"):
            monkeypatch.delenv(name, raising=False)
    for name in ("DISPLAY", "WAYLAND_DISPLAY", "VISUAL", "EDITOR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("USER", USER)
    monkeypatch.setenv("FORK_LINUX_DISPLAY_THEME", "dark")
    monkeypatch.setenv("FORK_LINUX_DISPLAY_DPI", "96")


def run_cli(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    """``fork-linux *argv`` in-process: (exit code, stdout, stderr)."""
    code = cli.main(list(argv), prog="fork-linux")
    out, err = capsys.readouterr()
    return code, out, err


def run_json(capsys: pytest.CaptureFixture[str], *argv: str) -> Any:
    """``fork-linux --json *argv``; asserts success and returns the parsed output."""
    code, out, err = run_cli(capsys, "--json", *argv)
    assert code == 0, err
    return json.loads(out)


def paths() -> Paths:
    """The paths the CLI uses in this (isolated) environment."""
    return Paths.from_env()


def layout() -> ForkLayout:
    """Fork's files for :data:`USER` in the default prefix."""
    return ForkLayout(paths(), USER)


def make_prefix(where: Paths) -> None:
    """A prefix with our marker and drive links (C: -> drive_c, Z: -> /)."""
    dosdevices = where.prefix / "dosdevices"
    dosdevices.mkdir(parents=True, exist_ok=True)
    (where.prefix / "drive_c" / "users" / USER).mkdir(parents=True, exist_ok=True)
    for drive, target in (("c:", "../drive_c"), ("z:", "/")):
        if not os.path.lexists(dosdevices / drive):
            (dosdevices / drive).symlink_to(target)
    where.prefix_meta_dir.mkdir(parents=True, exist_ok=True)
    where.created_by_marker.write_text("fork-linux test\n", encoding="utf-8")


def fork_running(monkeypatch: pytest.MonkeyPatch, running: bool = True) -> None:
    """Pretend Fork runs (or not) in every prefix."""
    from fork_linux import procs

    monkeypatch.setattr(procs, "fork_pids", lambda prefix, **_kw: [4242] if running else [])
    monkeypatch.setattr(procs, "fork_running", lambda prefix, **_kw: running)


def write_json(path: Path, data: Any) -> None:
    """Write ``data`` as JSON, creating parent directories."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
