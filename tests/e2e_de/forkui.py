"""The official Fork's windows, driven with xdotool inside a desktop session (real-Fork tier only).

Fork is started through the installed ``fork-linux`` (never patched, never scripted from the
inside): first-run dialogs are completed by clicking at offsets measured with Fork 2.23.2 (the
same offsets as ``tests/e2e/test_real_fork.py``), and processes are found through ``/proc``.
Screenshots stay in the session's scratch root (they show Fork's logo).
"""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from typing import Any

import dekit

WM_CLASS = "fork.exe"
WELCOME_TITLE = "User information"
MAIN_TITLE = "Fork"
MAIN_MIN_WIDTH = 800
WINDOW_TIMEOUT = 180.0
# Offsets of the controls inside Fork's first-run dialogs (measured with 2.23.2).
WELCOME_NAME = (460, 138)
WELCOME_EMAIL = (460, 194)
WELCOME_FINISH = (523, 337)
# Fork draws its own title bar: the close button sits 25 px from the right edge, 14 px down.
CLOSE_FROM_RIGHT = 25
CLOSE_FROM_TOP = 14
# Seconds to wait for Fork to exit before clicking its close button again.
CLOSE_PATIENCE = 20.0
# What a blank-screen report records (text only).
DIAGNOSTICS = (
    ("xwininfo", "-root", "-tree"),
    ("xset", "q"),
    ("xprop", "-root"),
    ("ps", "-eo", "pid,ppid,stat,etime,args"),
)
# The git-instance dialog's one button stays centred 41 px above its bottom edge.
GIT_INSTANCE_BUTTON_FROM_BOTTOM = 41
SEED_PATTERNS = ("wine-*.tar.xz", "Fork-*.exe", "winetricks-*", "selawik-*.zip")


def seed_caches(home: Path) -> None:
    """Copy the read-only seeds (``FL_E2E_SEED``, ``FL_E2E_WINETRICKS_CACHE``) into the download caches.

    fork-linux re-verifies every seeded file by size and sha256 before using it.
    """
    downloads = home / ".cache" / "fork-linux" / "downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    seed = os.environ.get("FL_E2E_SEED", "")
    if seed and Path(seed).is_dir():
        for pattern in SEED_PATTERNS:
            for source in sorted(Path(seed).glob(pattern)):
                target = downloads / source.name
                if source.is_file() and not target.exists():
                    shutil.copy2(source, target)
    winetricks = os.environ.get("FL_E2E_WINETRICKS_CACHE", "")
    target_cache = home / ".cache" / "winetricks"
    if winetricks and Path(winetricks).is_dir() and not target_cache.exists():
        shutil.copytree(winetricks, target_cache)


def prefix(session: dekit.Session) -> Path:
    """Our Wine prefix in the session's home."""
    return session.home / ".local" / "share" / "fork-linux" / "prefix"


def fork_windows(session: dekit.Session) -> list[dekit.Window]:
    """Visible windows of Fork (``WM_CLASS`` fork.exe)."""
    return [window for window in session.windows() if window.has_class(WM_CLASS)]


def main_window(session: dekit.Session) -> dekit.Window | None:
    """Fork's main window, when it is shown."""
    for window in fork_windows(session):
        if window.name == MAIN_TITLE and window.width >= MAIN_MIN_WIDTH:
            return window
    return None


def click(session: dekit.Session, window: dekit.Window, offset: tuple[int, int]) -> None:
    """Click at ``offset`` inside ``window``: move there, let the pointer settle, then click.

    A hand hovers before it presses; under labwc's Xwayland a press in the same instant as the
    move once left Fork's Repository > Open In File Explorer without effect.
    """
    session.xdotool("mousemove", str(window.x + offset[0]), str(window.y + offset[1]))
    time.sleep(0.3)
    session.xdotool("click", "1")
    time.sleep(0.7)


def _first_run_step(session: dekit.Session, window: dekit.Window) -> None:
    """Answer one of Fork's first-run dialogs (user information, git instance)."""
    if window.name == WELCOME_TITLE:
        click(session, window, WELCOME_NAME)
        session.xdotool("type", "--delay", "30", "E2E Test")
        click(session, window, WELCOME_EMAIL)
        session.xdotool("type", "--delay", "30", "e2e@example.invalid")
        click(session, window, WELCOME_FINISH)
    elif window.name == MAIN_TITLE and window.width > 200:
        click(session, window, (window.width // 2 - 1, window.height - GIT_INSTANCE_BUTTON_FROM_BOTTOM))


def wait_main_window(
    session: dekit.Session, *, first_run: bool = False, timeout: float = WINDOW_TIMEOUT
) -> dekit.Window:
    """Fork's main window, completing the first-run dialogs on the way."""
    def probe() -> dekit.Window | None:
        found = main_window(session)
        if found is None and first_run:
            for window in fork_windows(session):
                _first_run_step(session, window)
        return found

    try:
        return dekit.wait_for(probe, timeout, "Fork's main window")
    except dekit.SessionError:
        session.screenshot("no-main-window")
        _diagnose(session, "no-main-window")
        raise


def drawn_screenshot(session: dekit.Session, name: str, timeout: float = 60) -> Path:
    """A screenshot of Fork's main window once it shows something (not one flat colour).

    Taken again every 2 s: a window that was just mapped (or moved) can still be blank. On a
    timeout the whole screen is kept as ``<name>-screen.png`` for the report.
    """
    def probe() -> Path | None:
        window = main_window(session)
        if window is None:
            return None
        shot = session.screenshot(name, window)
        if session.image_stat(shot, "standard_deviation") > dekit.DRAWN_STDDEV:
            return shot
        time.sleep(2)
        return None

    try:
        return dekit.wait_for(probe, timeout, f"Fork's main window to be drawn ({name})")
    except dekit.SessionError:
        session.screenshot(f"{name}-screen")
        _diagnose(session, name)
        raise


def _diagnose(session: dekit.Session, name: str) -> None:
    """What the X server shows when a screenshot stays blank: logs/<name>-x11.txt (text only)."""
    lines = [repr(window) for window in session.windows()]
    window = main_window(session)
    if window is not None:
        direct = session.shots / f"{name}-direct.png"
        session.run(["magick", "import", "-display", session.display, "-window", window.id, str(direct)], check=False)
        if direct.is_file():
            lines.append(f"direct capture of {window.id}: mean={session.image_stat(direct, 'mean')} "
                         f"sd={session.image_stat(direct, 'standard_deviation')}")
    for argv in DIAGNOSTICS:
        lines.append("$ " + " ".join(argv))
        lines.append(session.run(argv, check=False).stdout)
    (session.logs / f"{name}-x11.txt").write_text("\n".join(lines), encoding="utf-8")


def fork_pids(wine_prefix: Path) -> list[int]:
    """Fork.exe processes running in ``wine_prefix`` (found by their WINEPREFIX)."""
    wanted = f"WINEPREFIX={wine_prefix}".encode()
    found = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            cmdline = Path("/proc", entry, "cmdline").read_bytes()
            environ = Path("/proc", entry, "environ").read_bytes()
        except OSError:
            continue
        if b"Fork.exe" in cmdline and b"current" in cmdline and wanted in environ.split(b"\0"):
            found.append(int(entry))
    return found


CONFINEMENT_FIELDS = ("NoNewPrivs", "Seccomp", "Seccomp_filters")


def confinement(pid: int) -> dict[str, str]:
    """``NoNewPrivs``, ``Seccomp`` and ``Seccomp_filters`` from ``/proc/<pid>/status`` (empty when
    the process is gone).

    Programs Wine starts carry wine-staging's seccomp filter on top of whatever the process that
    started Wine had (one more ``Seccomp_filters``, ``Seccomp: 2``) and ``NoNewPrivs: 1``; a program
    the desktop hand-off started outside Wine's process tree has what any other program of the
    session has (in the tier's container: Docker's default seccomp profile, one filter, and
    ``NoNewPrivs: 0``).
    """
    try:
        lines = Path("/proc", str(pid), "status").read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    fields = dict(line.split(":", 1) for line in lines if ":" in line)
    return {name: fields[name].strip() for name in CONFINEMENT_FIELDS if name in fields}


def handoff_daemon(session: dekit.Session) -> dict[str, Any]:
    """The daemon fork-linux recorded in ``session.json`` for this Fork (``{}`` without one)."""
    try:
        data = json.loads((session.runtime / "fork-linux" / "session.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    record = data.get("bridge") if isinstance(data, dict) else None
    return record if isinstance(record, dict) else {}


def handoff_log(session: dekit.Session) -> str:
    """fork-linux-host's hand-off fallback log (each line: a tool that ran inside Wine's tree)."""
    path = session.home / ".local" / "state" / "fork-linux" / "logs" / "handoff.log"
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def launch(session: dekit.Session, *targets: Path, extra: dict[str, str] | None = None) -> Any:
    """``fork-linux run TARGETS`` in the background (it execs Wine, which outlives the step)."""
    return session.spawn(f"launch-{time.time_ns()}", ["fork-linux", "run", *map(str, targets)], extra=extra)


def close_fork(session: dekit.Session, timeout: float = 60, patience: float = CLOSE_PATIENCE) -> None:
    """Close Fork like a user (its title bar's close button) and wait for its process to end.

    A user whose click did nothing clicks again: once on GNOME (mutter), right after step 07 had
    handed a repository over, Fork was still running 60 s after one click on its close button.
    Every ``patience`` seconds the button is clicked again while the main window is still shown
    (each retry is noted in ``logs/close-fork.txt``); on a timeout the screen and the X server's
    windows are kept (``close-timeout``) for the report.
    """
    deadline = time.monotonic() + timeout
    while True:
        window = main_window(session)
        if window is not None:
            session.activate(window)
            click(session, window, (window.width - CLOSE_FROM_RIGHT, CLOSE_FROM_TOP))
        wait = max(1.0, min(patience, deadline - time.monotonic()))
        try:
            dekit.wait_for(lambda: not fork_pids(prefix(session)), wait, "Fork to exit after its close button")
            return
        except dekit.SessionError:
            if time.monotonic() < deadline:
                with (session.logs / "close-fork.txt").open("a", encoding="utf-8") as log:
                    log.write(f"Fork still running {patience:.0f} s after its close button: clicking it again\n")
                continue
            session.screenshot("close-timeout")
            _diagnose(session, "close-timeout")
            raise


def settings(session: dekit.Session) -> dict[str, Any]:
    """Fork's ``settings.json`` (read only)."""
    user = session.env.get("USER") or session.home.name
    path = prefix(session) / "drive_c" / "users" / user / "AppData" / "Local" / "Fork" / "settings.json"
    return json.loads(path.read_text(encoding="utf-8-sig"))


def make_repo(session: dekit.Session, path: Path) -> Path:
    """A small git repository with one commit."""
    path.mkdir(parents=True, exist_ok=True)
    for argv in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "config", "user.name", "E2E Test"],
        ["git", "config", "user.email", "e2e@example.invalid"],
    ):
        session.run(argv, cwd=path)
    (path / "README.md").write_text(f"# {path.name}\n", encoding="utf-8")
    session.run(["git", "add", "-A"], cwd=path)
    session.run(["git", "commit", "-qm", f"initial {path.name}"], cwd=path)
    return path


def stop_wine(session: dekit.Session) -> None:
    """``wineserver -k`` for our prefix with every managed runtime (the session's teardown)."""
    runtimes = session.home / ".local" / "share" / "fork-linux" / "runtimes" / "wine"
    for server in sorted(runtimes.glob("*/bin/wineserver")):
        session.run([str(server), "-k"], extra={"WINEPREFIX": str(prefix(session))}, check=False, timeout=60)
