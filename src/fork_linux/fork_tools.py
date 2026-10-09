"""Which programs Fork's terminal, diff and merge settings should point at.

Fork's "Open in Shell" / Console button runs ``ShellTool``; its external diff
and merge buttons run ``ExternalDiffTool`` / ``MergeTool``. Under Wine the
useful targets are ours:

* with the native-git bridge daemon running (:func:`bridge.host_actions_active`)
  and ``fl-launch.exe`` installed in ``C:\\fork-linux\\bin``: ``fl-launch.exe
  terminal|diff|merge`` for all three;
* otherwise the terminal goes to the ``fork-linux-terminal`` script (a Unix
  ``#!/bin/sh`` script that Wine starts directly; Fork passes the repository
  as its working directory) and the diff / merge tools stay unset, because
  only ``fl-launch.exe`` can wait for a Linux tool and report its result.

A setting that names one of our programs is ours to change or reset;
anything else the user picked is left alone (see :mod:`fork_settings`).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from . import resources
from .errors import ForkLinuxError
from .pathmap import PathMap
from .paths import Paths

TERMINAL_SCRIPT = "fork-linux-terminal"
EXPLORER_SCRIPT = "fork-linux-explorer"
HANDOFF_SCRIPT = "fork-linux-handoff"
URL_SCRIPT = "fork-linux-open-url"
# Every script name Fork's settings or the registry may point at.
OUR_SCRIPTS = (TERMINAL_SCRIPT, EXPLORER_SCRIPT, HANDOFF_SCRIPT, URL_SCRIPT)
FL_LAUNCH = ("bin", "fl-launch.exe")
FL_LAUNCH_WIN = "C:\\fork-linux\\bin\\fl-launch.exe"
OUR_WIN_DIR = "c:\\fork-linux\\"

SHELL_TOOL = "ShellTool"
EXTERNAL_DIFF_TOOL = "ExternalDiffTool"
EXTERNAL_MERGE_TOOL = "MergeTool"
TOOL_KEYS = (SHELL_TOOL, EXTERNAL_DIFF_TOOL, EXTERNAL_MERGE_TOOL)
CUSTOM = "Custom"
# Fork's own values for "nothing configured".
DEFAULT_EXTERNAL_TOOL: dict[str, str] = {"Type": CUSTOM, "ApplicationPath": "", "Arguments": ""}
DEFAULTS: dict[str, Any] = {
    SHELL_TOOL: None,
    EXTERNAL_DIFF_TOOL: DEFAULT_EXTERNAL_TOOL,
    EXTERNAL_MERGE_TOOL: DEFAULT_EXTERNAL_TOOL,
}
# Fork's placeholders for the files it compares / merges.
DIFF_ARGUMENTS = 'diff "$LOCAL" "$REMOTE"'
MERGE_ARGUMENTS = 'merge "$BASE" "$LOCAL" "$REMOTE" "$MERGED"'


def script(name: str) -> Path | None:
    """Our executable helper script ``name`` in this installation's libexec directory, if present."""
    path = resources.libexec_dir() / name
    return path if path.is_file() and os.access(path, os.X_OK) else None


def script_win(name: str, pathmap: PathMap | None) -> str | None:
    """The Windows path (``Z:\\…``) of :func:`script` ``name``, or None when missing or unreachable."""
    path = script(name)
    if path is None or pathmap is None:
        return None
    try:
        return pathmap.unix_to_win(path)
    except ForkLinuxError:
        return None


def fl_launch_installed(paths: Paths) -> bool:
    """``C:\\fork-linux\\bin\\fl-launch.exe`` is in the prefix."""
    return paths.fork_linux_win_dir.joinpath(*FL_LAUNCH).is_file()


def is_ours(tool: Any) -> bool:
    """True if the Fork tool setting ``tool`` runs one of our programs."""
    if not isinstance(tool, dict):
        return False
    app = tool.get("ApplicationPath")
    if not isinstance(app, str) or not app:
        return False
    lowered = app.lower()
    return lowered.startswith(OUR_WIN_DIR) or lowered.replace("/", "\\").rsplit("\\", 1)[-1] in OUR_SCRIPTS


def is_dead(tool: Any, *, bridge_active: bool) -> bool:
    """True if ``tool`` points at ``fl-launch.exe`` while no bridge daemon will answer it."""
    if bridge_active or not isinstance(tool, dict):
        return False
    app = tool.get("ApplicationPath")
    return isinstance(app, str) and app.lower().startswith(OUR_WIN_DIR)


def wanted(*, bridge_active: bool, fl_launch: bool, pathmap: PathMap | None) -> dict[str, Any]:
    """``{ShellTool, ExternalDiffTool, MergeTool}`` values we want (None: no wish for that key)."""
    if bridge_active and fl_launch:
        return {
            SHELL_TOOL: {"Type": CUSTOM, "ApplicationPath": FL_LAUNCH_WIN, "Arguments": "terminal"},
            EXTERNAL_DIFF_TOOL: {"Type": CUSTOM, "ApplicationPath": FL_LAUNCH_WIN, "Arguments": DIFF_ARGUMENTS},
            EXTERNAL_MERGE_TOOL: {"Type": CUSTOM, "ApplicationPath": FL_LAUNCH_WIN, "Arguments": MERGE_ARGUMENTS},
        }
    terminal = script_win(TERMINAL_SCRIPT, pathmap)
    return {
        SHELL_TOOL: None if terminal is None else {"Type": CUSTOM, "ApplicationPath": terminal, "Arguments": ""},
        EXTERNAL_DIFF_TOOL: None,
        EXTERNAL_MERGE_TOOL: None,
    }
