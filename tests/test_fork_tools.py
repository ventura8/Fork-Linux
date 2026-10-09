"""Tests for fork_linux.fork_tools: what Fork's terminal / diff / merge settings point at."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from fork_linux import fork_tools, resources
from fork_linux.pathmap import PathMap
from fork_linux.paths import Paths


@pytest.fixture
def libexec(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "libexec"
    path.mkdir()
    monkeypatch.setenv(resources.LIBEXEC_ENV, str(path))
    return path


def _script(libexec: Path, name: str, mode: int = 0o755) -> Path:
    path = libexec / name
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    path.chmod(mode)
    return path


def test_script_must_be_executable(libexec: Path) -> None:
    assert fork_tools.script(fork_tools.TERMINAL_SCRIPT) is None
    _script(libexec, fork_tools.TERMINAL_SCRIPT, 0o644)
    assert fork_tools.script(fork_tools.TERMINAL_SCRIPT) is None
    path = _script(libexec, fork_tools.TERMINAL_SCRIPT)
    assert fork_tools.script(fork_tools.TERMINAL_SCRIPT) == path


def test_script_win(libexec: Path, tmp_path: Path) -> None:
    _script(libexec, fork_tools.EXPLORER_SCRIPT)
    z_map = PathMap.with_drives({"z": "/"})
    assert fork_tools.script_win(fork_tools.EXPLORER_SCRIPT, z_map) == "Z:" + str(
        libexec / fork_tools.EXPLORER_SCRIPT
    ).replace("/", "\\")
    assert fork_tools.script_win(fork_tools.EXPLORER_SCRIPT, None) is None
    assert fork_tools.script_win(fork_tools.EXPLORER_SCRIPT, PathMap.with_drives({"c": tmp_path / "c"})) is None
    assert fork_tools.script_win(fork_tools.TERMINAL_SCRIPT, z_map) is None


def test_fl_launch_installed(xdg: Path) -> None:
    paths = Paths.from_env({"HOME": str(xdg)})
    assert not fork_tools.fl_launch_installed(paths)
    exe = paths.fork_linux_win_dir.joinpath(*fork_tools.FL_LAUNCH)
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"MZ")
    assert fork_tools.fl_launch_installed(paths)


@pytest.mark.parametrize(
    ("tool", "ours"),
    [
        ({"ApplicationPath": "C:\\fork-linux\\bin\\fl-launch.exe"}, True),
        ({"ApplicationPath": "Z:\\opt\\fork-linux\\lib\\fork-linux-terminal"}, True),
        ({"ApplicationPath": "Z:/opt/x/fork-linux-explorer"}, True),
        ({"ApplicationPath": "C:\\tools\\wt.exe"}, False),
        ({"ApplicationPath": ""}, False),
        ({"ApplicationPath": 5}, False),
        ({}, False),
        (None, False),
        ("C:\\fork-linux\\x", False),
    ],
)
def test_is_ours(tool: Any, ours: bool) -> None:
    assert fork_tools.is_ours(tool) is ours


def test_is_dead() -> None:
    launch = {"ApplicationPath": fork_tools.FL_LAUNCH_WIN}
    assert fork_tools.is_dead(launch, bridge_active=False)
    assert not fork_tools.is_dead(launch, bridge_active=True)
    assert not fork_tools.is_dead({"ApplicationPath": "Z:\\x\\fork-linux-terminal"}, bridge_active=False)
    assert not fork_tools.is_dead({"ApplicationPath": None}, bridge_active=False)
    assert not fork_tools.is_dead(None, bridge_active=False)


def test_wanted(libexec: Path) -> None:
    z_map = PathMap.with_drives({"z": "/"})
    assert fork_tools.wanted(bridge_active=False, fl_launch=True, pathmap=z_map) == {
        "ShellTool": None,
        "ExternalDiffTool": None,
        "MergeTool": None,
        "ExternalDiffTools": None,
        "ExternalMergeTools": None,
    }
    _script(libexec, fork_tools.TERMINAL_SCRIPT)
    shell = fork_tools.wanted(bridge_active=True, fl_launch=False, pathmap=z_map)["ShellTool"]
    assert shell["ApplicationPath"].endswith("\\fork-linux-terminal") and shell["Arguments"] == ""
    bridged = fork_tools.wanted(bridge_active=True, fl_launch=True, pathmap=z_map)
    assert {bridged[key]["ApplicationPath"] for key in fork_tools.TOOL_KEYS} == {fork_tools.FL_LAUNCH_WIN}
    assert bridged["ExternalDiffTool"]["Arguments"] == 'diff "$LOCAL" "$REMOTE"'
    assert bridged["MergeTool"]["Arguments"] == 'merge "$BASE" "$LOCAL" "$REMOTE" "$MERGED"'
    # Fork 2.23 offers "Diff in <name>" only for entries of its tool lists (format Fork itself writes).
    assert bridged["ExternalDiffTools"] == {
        "Type": "Custom",
        "Name": "Linux (fork-linux)",
        "Path": "C:\\fork-linux\\bin\\fl-launch.exe",
        "Arguments": 'diff "$LOCAL" "$REMOTE"',
    }
    assert bridged["ExternalMergeTools"]["Arguments"].startswith("merge ")


def test_tool_lists_keep_the_users_entries() -> None:
    ours = fork_tools.list_entry(fork_tools.DIFF_ARGUMENTS)
    user = {"Type": "Custom", "Name": "My tool", "Path": "C:\\tools\\x.exe", "Arguments": "$LOCAL"}
    stale = {**ours, "Arguments": "old"}
    assert fork_tools.merged_list(None, ours) == [ours]
    assert fork_tools.merged_list(None, None) is None
    assert fork_tools.merged_list([], None) is None
    assert fork_tools.merged_list([stale, user, "odd"], ours) == [user, "odd", ours]
    assert fork_tools.merged_list([user, ours], ours) is None
    assert fork_tools.merged_list([user, ours], None) == [user]
    assert fork_tools.merged_list({"not": "a list"}, ours) is None
    assert not fork_tools.is_our_entry({"Path": 3}) and not fork_tools.is_our_entry("x")
