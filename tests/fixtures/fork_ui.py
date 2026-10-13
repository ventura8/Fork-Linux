"""Driving the official Fork in the real tier (tests/e2e): X11 windows, processes, repositories.

Shared by the real-tier modules; every helper takes the ``e2e`` fixture of
``tests/e2e/conftest.py`` (its isolated environment, display and directories). Screenshots go
to the root's ``shots`` directory: they show Fork's logo, so they never enter the repository.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

import pytest

WM_CLASS = "fork.exe"
WELCOME_TITLE = "User information"
MAIN_TITLE = "Fork"
MAIN_MIN_WIDTH = 800
WINDOW_TIMEOUT = 180.0
# Offsets of the controls inside Fork's first-run dialogs (measured with 2.23.2 on 1600x1000).
WELCOME_NAME = (460, 138)
WELCOME_EMAIL = (460, 194)
WELCOME_FINISH = (523, 337)
# The git-instance dialog ("Fork", 680 wide) changes height with its state and the UI font
# (Selawik: 382 to start, 446 while updating, 340 when done), but its one button, Start
# (disabled while updating) or Close, stays centred 41 px above the bottom edge.
GIT_INSTANCE_BUTTON_FROM_BOTTOM = 41

T = TypeVar("T")


def xdotool(e2e: Any, *args: str, check: bool = False) -> str:
    """``xdotool ARGS`` on the tier's display; its stdout."""
    result = subprocess.run(
        ["xdotool", *args], env=e2e.env, capture_output=True, text=True, timeout=30, check=False
    )
    if check and result.returncode != 0:
        pytest.fail(f"xdotool {' '.join(args)} failed: {result.stderr}")
    return result.stdout


def windows(e2e: Any) -> list[dict[str, Any]]:
    """Visible Fork windows: id, title and geometry."""
    found = []
    for wid in xdotool(e2e, "search", "--onlyvisible", "--class", WM_CLASS).split():
        shell = xdotool(e2e, "getwindowgeometry", "--shell", wid)
        geometry = dict(line.split("=", 1) for line in shell.splitlines() if "=" in line)
        try:
            window = {key.lower(): int(geometry[key]) for key in ("X", "Y", "WIDTH", "HEIGHT")}
        except (KeyError, ValueError):
            continue
        window["id"] = wid
        window["title"] = xdotool(e2e, "getwindowname", wid).strip()
        found.append(window)
    return found


def click(e2e: Any, window: dict[str, Any], offset: tuple[int, int]) -> None:
    """Click at ``offset`` inside ``window`` (a negative coordinate counts from the far edge)."""
    dx, dy = offset
    x = window["x"] + (dx if dx >= 0 else window["width"] + dx)
    y = window["y"] + (dy if dy >= 0 else window["height"] + dy)
    xdotool(e2e, "mousemove", str(x), str(y), "click", "1", check=True)
    time.sleep(0.7)


def find_window(e2e: Any, title: str) -> dict[str, Any] | None:
    """The newest visible Fork window called ``title``."""
    return next((window for window in reversed(windows(e2e)) if window["title"] == title), None)


def wait_for(check: Callable[[], T | None], timeout: float, what: str) -> T:
    """Poll ``check`` until it returns something truthy; fail after ``timeout`` seconds."""
    deadline = time.monotonic() + timeout
    while True:
        found = check()
        if found:
            return found
        if time.monotonic() > deadline:
            pytest.fail(f"timed out after {timeout:.0f} s waiting for {what}")
        time.sleep(0.5)


def _first_run_dialog(e2e: Any, window: dict[str, Any]) -> None:
    """Answer one of Fork's first-run dialogs (user information, git instance)."""
    if window["title"] == WELCOME_TITLE:
        click(e2e, window, WELCOME_NAME)
        xdotool(e2e, "type", "--delay", "30", "E2E Test", check=True)
        click(e2e, window, WELCOME_EMAIL)
        xdotool(e2e, "type", "--delay", "30", "e2e@example.invalid", check=True)
        click(e2e, window, WELCOME_FINISH)
    elif window["title"] == MAIN_TITLE:
        click(e2e, window, (window["width"] // 2 - 1, window["height"] - GIT_INSTANCE_BUTTON_FROM_BOTTOM))


def main_window(e2e: Any) -> dict[str, Any] | None:
    """Fork's main window, when it is up."""
    return next(
        (window for window in windows(e2e) if window["title"] == MAIN_TITLE and window["width"] >= MAIN_MIN_WIDTH),
        None,
    )


def wait_main_window(e2e: Any, *, first_run: bool = False, timeout: float = WINDOW_TIMEOUT) -> dict[str, Any]:
    """Wait for Fork's main window, completing the first-run dialogs on the way (``first_run``)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = main_window(e2e)
        if found is not None:
            return found
        if first_run:
            for window in windows(e2e):
                if window["width"] > 200:
                    _first_run_dialog(e2e, window)
        time.sleep(1)
    screenshot(e2e, "no-main-window")
    pytest.fail(f"no Fork main window within {timeout:.0f} s; windows: {windows(e2e)} (screen: no-main-window.png)")


def screenshot(e2e: Any, name: str, window: dict[str, Any] | None = None) -> Path:
    """Capture the screen (or ``window``) into the shots directory (never into the repository)."""
    target = e2e.shots / f"{name}.png"
    argv = ["magick", "import", "-display", e2e.display, "-window", "root"]
    if window is not None:
        argv += ["-crop", f"{window['width']}x{window['height']}+{window['x']}+{window['y']}"]
    subprocess.run([*argv, str(target)], env=e2e.env, capture_output=True, timeout=60, check=True)
    return target


def image_stat(e2e: Any, image: Path, expr: str) -> float:
    """ImageMagick ``%[fx:EXPR]`` of ``image`` (``mean``, ``standard_deviation``...)."""
    out = subprocess.run(
        ["magick", str(image), "-format", f"%[fx:{expr}]", "info:"],
        env=e2e.env, capture_output=True, text=True, timeout=60, check=True,
    ).stdout
    return float(out.strip())


def _proc_bytes(pid: str, name: str) -> bytes | None:
    try:
        return Path("/proc", pid, name).read_bytes()
    except OSError:
        return None


def prefix_pids(prefix: Path, needle: bytes) -> list[int]:
    """Processes whose command line contains ``needle`` and whose ``WINEPREFIX`` is ``prefix``."""
    wanted = f"WINEPREFIX={prefix}".encode()
    pids = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        cmdline, environ = _proc_bytes(entry, "cmdline"), _proc_bytes(entry, "environ")
        if cmdline is None or environ is None:
            continue
        if needle in cmdline and wanted in environ.split(b"\0"):
            pids.append(int(entry))
    return sorted(pids)


def fork_pids(prefix: Path) -> list[int]:
    """Our Fork.exe processes in ``prefix``."""
    return [pid for pid in prefix_pids(prefix, b"Fork.exe") if b"current" in (_proc_bytes(str(pid), "cmdline") or b"")]


def launch(
    e2e: Any, *targets: Path, extra: dict[str, str] | None = None, cli: str = "run", log_name: str = ""
) -> subprocess.Popen[bytes]:
    """``fork-linux run TARGETS`` detached (it execs Wine, which outlives the test step).

    Its output (fork-linux's, until it hands over to Wine) goes to ``logs/<log_name>.log``
    (default: ``launch-<time>.log``).
    """
    log_file = e2e.logs / f"{log_name or f'launch-{time.time_ns()}'}.log"
    with open(log_file, "wb") as log:
        argv = [sys.executable, "-m", "fork_linux", cli, *map(str, targets)]
        return subprocess.Popen(
            argv,
            env={**e2e.env, **(extra or {})},
            cwd=str(e2e.root),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )


def close_fork(e2e: Any, prefix: Path, timeout: float = 60) -> None:
    """Close Fork like a user (Alt+F4 on the main window) and wait for its process to end."""
    for window in windows(e2e):
        if window["title"] == MAIN_TITLE and window["width"] >= MAIN_MIN_WIDTH:
            xdotool(e2e, "windowactivate", "--sync", window["id"])
            xdotool(e2e, "key", "--window", window["id"], "alt+F4")
    deadline = time.monotonic() + timeout
    while fork_pids(prefix):
        if time.monotonic() > deadline:
            pytest.fail(f"Fork did not exit within {timeout:.0f} s after Alt+F4")
        time.sleep(0.5)


def settings_file(prefix: Path) -> Path:
    """Fork's ``settings.json`` in ``prefix`` for the current user."""
    user = os.environ.get("USER") or Path.home().name
    return prefix / "drive_c" / "users" / user / "AppData" / "Local" / "Fork" / "settings.json"


def settings(e2e: Any, prefix: Path) -> dict[str, Any]:
    """Fork's ``settings.json`` as a dict."""
    return json.loads(settings_file(prefix).read_text(encoding="utf-8-sig"))


def git(repo: Path, *args: str) -> None:
    """``git -C REPO ARGS`` (must succeed)."""
    subprocess.run(["git", "-C", str(repo), *args], capture_output=True, timeout=60, check=True)


def make_repo(path: Path) -> Path:
    """A small repository with one commit (a file, an executable script and a symlink)."""
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.name", "E2E Test")
    git(path, "config", "user.email", "e2e@example.invalid")
    (path / "README.md").write_text(f"# {path.name}\n", encoding="utf-8")
    script = path / "run.sh"
    script.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    script.chmod(0o755)
    (path / "link.md").symlink_to("README.md")
    git(path, "add", "-A")
    git(path, "commit", "-qm", f"initial {path.name}")
    return path
