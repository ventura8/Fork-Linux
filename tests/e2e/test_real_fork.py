"""The real thing: setup, doctor, launch, tabs, settings, rollback and purge with the official Fork.

The tests run in file order and build on each other (one prefix, one Fork
session at a time); after a failed step ``conftest.py`` makes the later ones
fail fast. Gated by ``FL_E2E_FORK=1`` in ``conftest.py``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tarfile
import time
from pathlib import Path
from typing import Any

import pytest

from fork_linux import manifest, versions

if os.environ.get("FL_E2E_FORK") != "1":
    pytest.skip(
        "requires FL_E2E_FORK=1 (real Wine + the official Fork download, ~2.5 GB disk, Xvfb and xdotool)",
        allow_module_level=True,
    )



def _target_version() -> str:
    """``FL_E2E_FORK_VERSION`` (upstream-watch: the version it just found), else the manifest default."""
    wanted = os.environ.get("FL_E2E_FORK_VERSION", "").strip()
    if not wanted:
        return manifest.load().fork_default
    if not versions.is_valid(wanted):
        raise pytest.UsageError(f"FL_E2E_FORK_VERSION={wanted!r} is not a Fork version")
    return wanted


FORK_VERSION = _target_version()
# Every setup call names the version, so a re-run sees the same inputs.
SETUP = ("setup", "--accept-fork-eula", "--no-gui", "--fork-version", FORK_VERSION)
WM_CLASS = "fork.exe"
WELCOME_TITLE = "User information"
MAIN_TITLE = "Fork"
MAIN_MIN_WIDTH = 800
SETUP_TIMEOUT = 3600.0
WINDOW_TIMEOUT = 180.0
NOOP_SETUP_SECONDS = 10.0
FAST_PATH_SECONDS = 30.0
# Offsets of the controls inside Fork's first-run dialogs (measured with 2.23.2 on 1600x1000).
WELCOME_NAME = (460, 138)
WELCOME_EMAIL = (460, 194)
WELCOME_FINISH = (523, 337)
GIT_INSTANCE_START_HEIGHT = 374
GIT_INSTANCE_START = (339, 333)
GIT_INSTANCE_DONE_HEIGHT = 336
GIT_INSTANCE_CLOSE = (339, 294)
MIN_STDDEV = 0.02
DARK_MEAN = 0.35

# -- X11 helpers ---------------------------------------------------------------------------------


def _xdotool(e2e: Any, *args: str, check: bool = False) -> str:
    result = subprocess.run(
        ["xdotool", *args], env=e2e.env, capture_output=True, text=True, timeout=30, check=False
    )
    if check and result.returncode != 0:
        pytest.fail(f"xdotool {' '.join(args)} failed: {result.stderr}")
    return result.stdout


def _windows(e2e: Any) -> list[dict[str, Any]]:
    """Visible Fork windows: id, title and geometry."""
    found = []
    for wid in _xdotool(e2e, "search", "--onlyvisible", "--class", WM_CLASS).split():
        shell = _xdotool(e2e, "getwindowgeometry", "--shell", wid)
        geometry = dict(line.split("=", 1) for line in shell.splitlines() if "=" in line)
        try:
            window = {key.lower(): int(geometry[key]) for key in ("X", "Y", "WIDTH", "HEIGHT")}
        except (KeyError, ValueError):
            continue
        window["id"] = wid
        window["title"] = _xdotool(e2e, "getwindowname", wid).strip()
        found.append(window)
    return found


def _click(e2e: Any, window: dict[str, Any], offset: tuple[int, int]) -> None:
    _xdotool(e2e, "mousemove", str(window["x"] + offset[0]), str(window["y"] + offset[1]), "click", "1", check=True)
    time.sleep(0.7)


def _wait_main_window(e2e: Any, *, first_run: bool = False, timeout: float = WINDOW_TIMEOUT) -> dict[str, Any]:
    """Wait for Fork's main window, completing the first-run dialogs on the way."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for window in _windows(e2e):
            if window["title"] == MAIN_TITLE and window["width"] >= MAIN_MIN_WIDTH:
                return window
            if not first_run or window["width"] <= 200:
                continue
            if window["title"] == WELCOME_TITLE:
                _click(e2e, window, WELCOME_NAME)
                _xdotool(e2e, "type", "--delay", "30", "E2E Test", check=True)
                _click(e2e, window, WELCOME_EMAIL)
                _xdotool(e2e, "type", "--delay", "30", "e2e@example.invalid", check=True)
                _click(e2e, window, WELCOME_FINISH)
            elif window["title"] == MAIN_TITLE and window["height"] == GIT_INSTANCE_START_HEIGHT:
                _click(e2e, window, GIT_INSTANCE_START)
            elif window["title"] == MAIN_TITLE and window["height"] == GIT_INSTANCE_DONE_HEIGHT:
                _click(e2e, window, GIT_INSTANCE_CLOSE)
        time.sleep(1)
    pytest.fail(f"no Fork main window within {timeout:.0f} s; windows: {_windows(e2e)}")


def _screenshot(e2e: Any, name: str, window: dict[str, Any] | None = None) -> Path:
    """Capture the screen (or ``window``) into the shots directory (never into the repository)."""
    target = e2e.shots / f"{name}.png"
    argv = ["magick", "import", "-display", e2e.display, "-window", "root"]
    if window is not None:
        argv += ["-crop", f"{window['width']}x{window['height']}+{window['x']}+{window['y']}"]
    subprocess.run([*argv, str(target)], env=e2e.env, capture_output=True, timeout=60, check=True)
    return target


def _stat(e2e: Any, image: Path, expr: str) -> float:
    out = subprocess.run(
        ["magick", str(image), "-format", f"%[fx:{expr}]", "info:"],
        env=e2e.env, capture_output=True, text=True, timeout=60, check=True,
    ).stdout
    return float(out.strip())


# -- process helpers -----------------------------------------------------------------------------


def _fork_pids(prefix: Path) -> list[int]:
    """Our Fork.exe processes in ``prefix`` (found by their WINEPREFIX)."""
    wanted = f"WINEPREFIX={prefix}".encode()
    pids = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            cmdline = Path("/proc", entry, "cmdline").read_bytes()
            environ = Path("/proc", entry, "environ").read_bytes()
        except OSError:
            continue
        if b"Fork.exe" in cmdline and b"current" in cmdline and wanted in environ.split(b"\0"):
            pids.append(int(entry))
    return pids


def _launch(e2e: Any, *targets: Path, extra: dict[str, str] | None = None, cli: str = "run") -> subprocess.Popen[bytes]:
    """``fork-linux run TARGETS`` detached (it execs Wine, which outlives the test step)."""
    with open(e2e.logs / f"launch-{time.time_ns()}.log", "wb") as log:
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


def _close_fork(e2e: Any, prefix: Path, timeout: float = 60) -> None:
    """Close Fork like a user (Alt+F4 on the main window) and wait for its process to end."""
    for window in _windows(e2e):
        if window["title"] == MAIN_TITLE and window["width"] >= MAIN_MIN_WIDTH:
            _xdotool(e2e, "windowactivate", "--sync", window["id"])
            _xdotool(e2e, "key", "--window", window["id"], "alt+F4")
    deadline = time.monotonic() + timeout
    while _fork_pids(prefix):
        if time.monotonic() > deadline:
            pytest.fail(f"Fork did not exit within {timeout:.0f} s after Alt+F4")
        time.sleep(0.5)


def _settings(e2e: Any, prefix: Path) -> dict[str, Any]:
    user = os.environ.get("USER") or Path.home().name
    path = prefix / "drive_c" / "users" / user / "AppData" / "Local" / "Fork" / "settings.json"
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], capture_output=True, timeout=60, check=True)


def _make_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.name", "E2E Test")
    _git(path, "config", "user.email", "e2e@example.invalid")
    (path / "README.md").write_text(f"# {path.name}\n", encoding="utf-8")
    script = path / "run.sh"
    script.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    script.chmod(0o755)
    (path / "link.md").symlink_to("README.md")
    _git(path, "add", "-A")
    _git(path, "commit", "-qm", f"initial {path.name}")
    return path


# -- the scenario --------------------------------------------------------------------------------


def test_01_setup_completes_and_resumes(e2e: Any) -> None:
    started = time.monotonic()
    e2e.cli(*SETUP, timeout=SETUP_TIMEOUT)
    (e2e.logs / "timing-setup.txt").write_text(f"{time.monotonic() - started:.1f}\n", encoding="utf-8")
    listed = json.loads(e2e.cli("setup", "--list-steps", "--json").stdout)
    assert listed["complete"] is True
    assert {row["status"] for row in listed["steps"]} <= {"done", "always"}
    started = time.monotonic()
    report = json.loads(e2e.cli(*SETUP, "--json").stdout)
    assert report["ran"] == ["preflight", "finalize"]
    assert time.monotonic() - started < NOOP_SETUP_SECONDS


def test_02_doctor_has_no_failures(e2e: Any) -> None:
    report = json.loads(e2e.cli("doctor", "--deep", "--json").stdout)
    failed = [check for check in report["checks"] if check["status"] == "fail"]
    assert failed == []
    warned = {check["id"] for check in report["checks"] if check["status"] == "warn"}
    assert warned == set(), f"unexpected warnings: {warned}"


def test_03_first_launch_opens_the_repository(e2e: Any) -> None:
    demo = _make_repo(e2e.repos / "demo")
    _make_repo(e2e.repos / "second")
    _launch(e2e, demo)
    window = _wait_main_window(e2e, first_run=True)
    time.sleep(5)
    shot = _screenshot(e2e, "03-main")
    assert _stat(e2e, shot, "standard_deviation") > MIN_STDDEV
    assert _fork_pids(e2e.prefix)
    # settings.json was seeded before the first start and Fork kept the Wine-safe values.
    settings = _settings(e2e, e2e.prefix)
    assert settings["UpdateSubmodulesOnCheckout"] is False
    assert settings["DisableHardwareAcceleration"] is True
    assert window["width"] >= MAIN_MIN_WIDTH


def test_04_second_tab_and_fork_alias(e2e: Any) -> None:
    started = time.monotonic()
    second = _launch(e2e, e2e.repos / "second")
    assert second.wait(timeout=FAST_PATH_SECONDS) == 0
    assert time.monotonic() - started < FAST_PATH_SECONDS
    alias = subprocess.run(
        [sys.executable, "-c",
         "import sys; from fork_linux.cli import main; raise SystemExit(main([sys.argv[1]], prog='fork'))",
         str(e2e.repos / "demo")],
        env=e2e.env, cwd=str(e2e.root), capture_output=True, timeout=FAST_PATH_SECONDS, check=False,
    )
    assert alias.returncode == 0, alias.stderr
    assert len(_fork_pids(e2e.prefix)) == 1, "the second launches must hand over to the running Fork"


def test_05_relaunch_applies_dark_theme(e2e: Any) -> None:
    _close_fork(e2e, e2e.prefix)
    _launch(e2e, e2e.repos / "demo", extra={"FORK_LINUX_DISPLAY_THEME": "dark"})
    window = _wait_main_window(e2e)
    settings = _settings(e2e, e2e.prefix)
    assert settings["Theme"] == 1
    assert settings["FollowSystemTheme"] is False
    assert settings["UpdateSubmodulesOnCheckout"] is False
    assert settings["DisableHardwareAcceleration"] is True
    assert isinstance(settings["LayoutScaling"], int)
    time.sleep(5)
    shot = _screenshot(e2e, "05-dark", window)
    assert _stat(e2e, shot, "mean") < DARK_MEAN


def test_07_snapshot_and_rollback(e2e: Any) -> None:
    listing = e2e.cli("snapshot", "list", "--json")
    assert FORK_VERSION in listing.stdout
    refused = e2e.cli("rollback", "--to-version", FORK_VERSION, "--no-pin", check=False)
    assert refused.returncode == 15, "rollback must refuse while Fork runs"
    _close_fork(e2e, e2e.prefix)
    e2e.cli("snapshot", "create")
    e2e.cli("rollback", "--to-version", FORK_VERSION, "--no-pin")
    _launch(e2e, e2e.repos / "demo")
    window = _wait_main_window(e2e)
    assert window["width"] >= MAIN_MIN_WIDTH
    _close_fork(e2e, e2e.prefix)


def test_10_logs_bundle_and_purge_second_prefix(e2e: Any) -> None:
    prefix2 = e2e.root / "prefix2"
    prefix2.mkdir(mode=0o700)
    extra = {"FORK_LINUX_PREFIX": str(prefix2), "FORK_LINUX_ADOPT_PREFIX": "1"}
    # Only the host-side steps: enough for fork-linux to mark the prefix as its own.
    e2e.cli("setup", "--accept-fork-eula", "--no-gui", "--only", "consent", extra=extra)
    assert (prefix2 / ".fork-linux" / "created-by").is_file()

    user = os.environ.get("USER") or Path.home().name
    fork_dir = e2e.prefix / "drive_c" / "users" / user / "AppData" / "Local" / "Fork"
    (fork_dir / "accounts.json").write_text('{"token": "ghp_E2EACCOUNTSJSON000000000000000000"}', encoding="utf-8")
    with open(fork_dir / "logs" / "fork.log", "a", encoding="utf-8") as handle:
        handle.write("E2E https://user:supersecretpw@example.invalid/x.git ghp_abcdefghijklmnopqrstuvwxyz0123456789\n")
    e2e.cli("logs", "--bundle")
    bundles = sorted(e2e.root.glob("fork-linux-logs-*.tar.gz"))
    assert bundles
    with tarfile.open(bundles[-1]) as archive:
        names = archive.getnames()
        assert not any(name.endswith("accounts.json") for name in names)
        text = b"".join(
            archive.extractfile(member).read() for member in archive.getmembers() if member.isfile()
        )
    assert b"supersecretpw" not in text
    assert b"ghp_abcdefghijklmnopqrstuvwxyz" not in text

    e2e.cli("uninstall", extra=extra)
    assert not (e2e.home / ".local" / "share" / "applications" / "io.github.ventura8.ForkLinux.desktop").exists()
    e2e.cli("uninstall", "--purge", "--yes", extra=extra)
    assert not prefix2.exists()
    assert not (e2e.home / ".local" / "share" / "fork-linux").exists()
    assert (e2e.home / ".wine" / "SENTINEL").is_dir(), "~/.wine must never be touched"
    assert (e2e.home / ".cache" / "winetricks").is_dir() or not os.environ.get("FL_E2E_WINETRICKS_CACHE")
