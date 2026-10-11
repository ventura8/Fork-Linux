"""Drive the official Fork on the tier's Xvfb display: windows, keys, clicks, screenshots, sessions.

Shared by every real-tier module (``tests/e2e/test_real_*.py``) and usable from an exploration
script inside the E2E container (``scripts/e2e-docker.sh shell``): nothing here imports pytest.

There is no window manager on the display. Wine then places and focuses its windows itself, so
the helpers never move or resize a window (that desynchronises Wine's idea of where the window is
from the X server's) and focus a window by clicking into it. Keys go through XTEST
(``xdotool key``), which reaches the focused window like a real keyboard.

Screenshots show Fork's own interface and logo: they stay in the tier's scratch root and are never
copied into the repository (AGENTS.md hard rule 4).
"""

from __future__ import annotations

import contextlib
import functools
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

T = TypeVar("T")

WM_CLASS = "fork.exe"
MAIN_TITLE = "Fork"
MAIN_MIN_WIDTH = 800
WELCOME_TITLE = "User information"
# Offsets of the controls inside Fork's first-run "User information" dialog (Fork 2.23.2).
WELCOME_NAME = (460, 138)
WELCOME_EMAIL = (460, 194)
WELCOME_FINISH = (523, 337)
# The git-instance dialog ("Fork", 680 wide) changes height with its state and the UI font,
# but its one button (Start, then Close) stays centred 41 px above its bottom edge.
GIT_INSTANCE_WIDTH = 680
GIT_INSTANCE_BUTTON_FROM_BOTTOM = 41
WINDOW_TIMEOUT = 180.0
# Where the main window opens (seeded into settings.json before each start): the whole tier
# measures offsets inside a 1500x940 window at (40, 30) on the 1600x1000 display.
MAIN_GEOMETRY = '{"Left": 40, "Top": 30, "Width": 1500, "Height": 940, "WindowState": 0}'
POLL = 0.4
# Keys that a closing window can leave pressed on Xvfb's XTEST keyboard.
STICKY_KEYS = ("alt", "ctrl", "shift", "super", "F4", "Return", "Escape", "Tab")


def fork_version() -> str:
    """``FL_E2E_FORK_VERSION`` (upstream-watch: the version it just found), else the manifest default."""
    from fork_linux import manifest, versions

    wanted = os.environ.get("FL_E2E_FORK_VERSION", "").strip()
    if not wanted:
        return manifest.load().fork_default
    if not versions.is_valid(wanted):
        raise ValueError(f"FL_E2E_FORK_VERSION={wanted!r} is not a Fork version")
    return wanted


class DriverError(AssertionError):
    """A step of the scenario did not happen (raised as an assertion so pytest reports it plainly)."""


@dataclass(frozen=True)
class Window:
    """A visible X window of Fork: its id, title and geometry (root coordinates)."""

    id: str
    title: str
    x: int
    y: int
    width: int
    height: int

    def point(self, dx: int, dy: int) -> tuple[int, int]:
        """Root coordinates of the offset ``(dx, dy)`` inside the window (negative: from the far edge)."""
        return (self.x + (dx if dx >= 0 else self.width + dx), self.y + (dy if dy >= 0 else self.height + dy))

    @property
    def is_main(self) -> bool:
        """Fork's main window: titled "Fork" and wide (dialogs titled "Fork" are narrower)."""
        return self.title == MAIN_TITLE and self.width >= MAIN_MIN_WIDTH


def wait_until(check: Callable[[], T | None], timeout: float, what: str, poll: float = POLL) -> T:
    """Poll ``check`` until it returns something truthy; fail with ``what`` after ``timeout`` seconds."""
    deadline = time.monotonic() + timeout
    while True:
        value = check()
        if value:
            return value
        if time.monotonic() > deadline:
            raise DriverError(f"timed out after {timeout:.0f} s waiting for {what}")
        time.sleep(poll)


class Screen:
    """The tier's X display: xdotool, screenshots and pixel statistics."""

    def __init__(self, env: Mapping[str, str], shots: Path) -> None:
        self.env = dict(env)
        self.shots = shots
        self.display = self.env["DISPLAY"]

    # -- xdotool --------------------------------------------------------------------------------

    def xdotool(self, *args: str, check: bool = False, timeout: float = 30) -> str:
        """Run ``xdotool ARGS`` on the display and return its output."""
        result = subprocess.run(
            ["xdotool", *args], env=self.env, capture_output=True, text=True, timeout=timeout, check=False
        )
        if check and result.returncode != 0:
            raise DriverError(f"xdotool {' '.join(args)} failed: {result.stderr.strip()}")
        return result.stdout

    def windows(self, wm_class: str = WM_CLASS) -> list[Window]:
        """The visible windows of ``wm_class``, newest last."""
        found = []
        for wid in self.xdotool("search", "--onlyvisible", "--class", wm_class).split():
            window = self.window(wid)
            if window is not None:
                found.append(window)
        return found

    def any_windows(self, pattern: str = ".+") -> list[Window]:
        """Visible windows of any program whose title matches ``pattern`` (a credential prompt, a dialog
        of another Windows program), smaller than the screen."""
        found = []
        for wid in self.xdotool("search", "--onlyvisible", "--name", pattern).split():
            window = self.window(wid)
            if window is not None and 60 < window.width < 1600 and window.title:
                found.append(window)
        return found

    def window(self, wid: str) -> Window | None:
        """The geometry and title of one window (None when it is gone)."""
        shell = self.xdotool("getwindowgeometry", "--shell", wid)
        values = dict(line.split("=", 1) for line in shell.splitlines() if "=" in line)
        try:
            x, y, width, height = (int(values[key]) for key in ("X", "Y", "WIDTH", "HEIGHT"))
        except (KeyError, ValueError):
            return None
        return Window(wid, self.xdotool("getwindowname", wid).strip(), x, y, width, height)

    def find(self, title: str | None = None, *, pattern: str | None = None, min_width: int = 0,
             exclude: Iterable[str] = ()) -> Window | None:
        """The newest visible Fork window matching a title (exact or regex) and a minimum width."""
        excluded = set(exclude)
        regex = re.compile(pattern) if pattern else None
        for window in reversed(self.windows()):
            if window.id in excluded or window.width < min_width:
                continue
            if title is not None and window.title != title:
                continue
            if regex is not None and not regex.search(window.title):
                continue
            return window
        return None

    def wait_window(self, title: str | None = None, *, pattern: str | None = None, min_width: int = 0,
                    exclude: Iterable[str] = (), timeout: float = 60) -> Window:
        """Wait for a window like :meth:`find` describes."""
        what = f"a Fork window {title or pattern or ''!r} (min width {min_width})"
        return wait_until(lambda: self.find(title, pattern=pattern, min_width=min_width, exclude=exclude),
                          timeout, what)

    def wait_gone(self, window: Window, timeout: float = 30) -> None:
        """Wait until ``window`` is no longer visible."""
        wait_until(lambda: all(other.id != window.id for other in self.windows()), timeout,
                   f"window {window.title!r} to close")

    def new_window(self, before: Iterable[Window], *, timeout: float = 30, min_width: int = 0) -> Window:
        """Wait for a Fork window that was not in ``before`` (a dialog, a popup menu)."""
        known = {window.id for window in before}
        return wait_until(
            lambda: next((w for w in reversed(self.windows()) if w.id not in known and w.width >= min_width), None),
            timeout, "a new Fork window",
        )

    # -- input ----------------------------------------------------------------------------------

    def click(self, x: int, y: int, *, button: int = 1, repeat: int = 1, settle: float = 0.5) -> None:
        """Click at root coordinates."""
        self.xdotool("mousemove", "--sync", str(x), str(y), check=True)
        self.xdotool("click", "--repeat", str(repeat), "--delay", "120", str(button), check=True)
        time.sleep(settle)

    def click_in(self, window: Window, dx: int, dy: int, **kwargs: Any) -> None:
        """Click at an offset inside ``window`` (negative offsets count from the far edges)."""
        self.click(*window.point(dx, dy), **kwargs)

    def hover(self, x: int, y: int, settle: float = 0.6) -> None:
        """Move the pointer without clicking (tooltips, hover buttons)."""
        self.xdotool("mousemove", "--sync", str(x), str(y), check=True)
        time.sleep(settle)

    def key(self, *keys: str, settle: float = 0.4) -> None:
        """Press key combinations (xdotool names: ``ctrl+p``, ``Return``) on the focused window."""
        self.xdotool("key", "--delay", "90", *keys, check=True)
        time.sleep(settle)

    def type(self, text: str, settle: float = 0.4) -> None:
        """Type text into the focused control; text beyond ASCII is pasted (Ctrl+V) instead.

        ``xdotool type`` maps a spare keycode for each non-ASCII character and now and then loses one
        (a commit subject came out without its last CJK character); the clipboard keeps every byte.
        """
        if text.isascii():
            self.xdotool("type", "--delay", "35", "--", text, check=True)
        else:
            self.paste(text)
        time.sleep(settle)

    def paste(self, text: str) -> None:
        """Paste ``text`` into the focused control through the clipboard (``xclip`` owns it meanwhile)."""
        owner = subprocess.Popen(["xclip", "-quiet", "-selection", "clipboard"], env=self.env,
                                 stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # -quiet: xclip reads the text, then stays in the foreground serving the clipboard until killed.
        with contextlib.suppress(subprocess.TimeoutExpired):
            owner.communicate(text.encode("utf-8"), timeout=0.5)
        try:
            self.key("ctrl+v", settle=1.0)
        finally:
            owner.kill()
            owner.wait()

    def release_keys(self) -> None:
        """Release modifiers and keys a vanished window may have left pressed."""
        self.xdotool("keyup", *STICKY_KEYS)

    # -- pixels ---------------------------------------------------------------------------------

    def screenshot(self, name: str, window: Window | None = None) -> Path:
        """Capture the screen (or ``window``) into the shots directory (never the repository)."""
        self.shots.mkdir(parents=True, exist_ok=True)
        target = self.shots / f"{name}.png"
        argv = ["magick", "import", "-display", self.display, "-window", "root"]
        if window is not None:
            argv += ["-crop", f"{window.width}x{window.height}+{window.x}+{window.y}", "+repage"]
        subprocess.run([*argv, str(target)], env=self.env, capture_output=True, timeout=60, check=True)
        return target

    def stat(self, image: Path, expr: str, crop: tuple[int, int, int, int] | None = None) -> float:
        """An ImageMagick ``fx`` statistic (``mean``, ``standard_deviation``...) of an image region."""
        argv = ["magick", str(image)]
        if crop is not None:
            x, y, width, height = crop
            argv += ["-crop", f"{width}x{height}+{x}+{y}", "+repage"]
        out = subprocess.run([*argv, "-format", f"%[fx:{expr}]", "info:"], env=self.env, capture_output=True,
                             text=True, timeout=60, check=True).stdout
        return float(out.strip())


# -- processes -------------------------------------------------------------------------------------


def fork_pids(prefix: Path) -> list[int]:
    """Fork.exe processes running in ``prefix`` (found by their ``WINEPREFIX``)."""
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
        if b"Fork.exe" in cmdline and wanted in environ.split(b"\0"):
            pids.append(int(entry))
    return pids


def processes(match: Callable[[str], bool]) -> list[tuple[int, str]]:
    """``(pid, command line)`` of the processes whose space-joined command line matches."""
    found = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            cmdline = Path("/proc", entry, "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
        except OSError:
            continue
        if cmdline and match(cmdline):
            found.append((int(entry), cmdline.strip()))
    return found


class Fork:
    """Fork sessions in one prefix: launch through fork-linux, first-run dialogs, close, settings."""

    def __init__(self, screen: Screen, env: Mapping[str, str], prefix: Path, logs: Path, cwd: Path) -> None:
        self.screen = screen
        self.env = dict(env)
        self.prefix = prefix
        self.logs = logs
        self.cwd = cwd

    # -- fork-linux -----------------------------------------------------------------------------

    def cli(self, *args: str, extra: Mapping[str, str] | None = None, timeout: float = 600,
            cwd: Path | None = None, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
        """``python3 -m fork_linux ARGS`` in the tier's environment (never fails by itself)."""
        return subprocess.run(
            [sys.executable, "-m", "fork_linux", *args],
            env={**self.env, **(extra or {})},
            cwd=str(cwd or self.cwd),
            input=stdin,
            stdin=None if stdin is not None else subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

    def launch(self, *targets: str | Path, args: Iterable[str] = (), extra: Mapping[str, str] | None = None,
               cwd: Path | None = None, argv0: str = "run") -> subprocess.Popen[bytes]:
        """Start ``fork-linux run [ARGS] TARGETS`` (or ``fork TARGETS`` with ``argv0='fork'``) detached."""
        self.logs.mkdir(parents=True, exist_ok=True)
        log_path = self.logs / f"launch-{time.time_ns()}.log"
        if argv0 == "fork":
            argv = [sys.executable, "-c",
                    "import sys; from fork_linux.cli import main; raise SystemExit(main(sys.argv[1:], prog='fork'))",
                    *args, *map(str, targets)]
        else:
            argv = [sys.executable, "-m", "fork_linux", argv0, *args, *map(str, targets)]
        with open(log_path, "wb") as log:
            return subprocess.Popen(argv, env={**self.env, **(extra or {})}, cwd=str(cwd or self.cwd),
                                    stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                    start_new_session=True)

    # -- windows --------------------------------------------------------------------------------

    def main_window(self) -> Window | None:
        """Fork's main window when it is visible."""
        return next((window for window in self.screen.windows() if window.is_main), None)

    def wait_main(self, *, timeout: float = WINDOW_TIMEOUT, first_run: bool = True) -> Window:
        """Wait for the main window, answering Fork's first-run dialogs on the way."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for window in self.screen.windows():
                if window.is_main:
                    return window
                if first_run:
                    self._answer_first_run(window)
            time.sleep(1)
        self.screen.screenshot("no-main-window")
        raise DriverError(f"no Fork main window within {timeout:.0f} s; windows: {self.screen.windows()}")

    def _answer_first_run(self, window: Window) -> None:
        if window.width <= 200:
            return
        if window.title == WELCOME_TITLE:
            self.screen.click_in(window, *WELCOME_NAME)
            self.screen.type("E2E Test")
            self.screen.click_in(window, *WELCOME_EMAIL)
            self.screen.type("e2e@example.invalid")
            self.screen.click_in(window, *WELCOME_FINISH)
        elif window.title == MAIN_TITLE and window.width == GIT_INSTANCE_WIDTH:
            self.screen.click_in(window, window.width // 2 - 1, window.height - GIT_INSTANCE_BUTTON_FROM_BOTTOM)

    def place_window(self, geometry: str = MAIN_GEOMETRY) -> None:
        """Seed the main window's geometry (Fork closed): without a window manager it opens there."""
        if self.settings_path().is_file():
            result = self.cli("settings", "set", "MainWindowLocationState", geometry)
            if result.returncode != 0:
                raise DriverError(f"settings set MainWindowLocationState failed: {result.stderr}")

    def start(self, *targets: str | Path, args: Iterable[str] = (), extra: Mapping[str, str] | None = None,
              timeout: float = WINDOW_TIMEOUT, geometry: str = MAIN_GEOMETRY) -> Window:
        """Launch Fork (closed before) and return its main window once it has settled."""
        self.screen.release_keys()
        self.place_window(geometry)
        self.launch(*targets, args=args, extra=extra)
        window = self.wait_main(timeout=timeout)
        time.sleep(3)
        return self.main_window() or window

    def focus(self, window: Window | None = None) -> Window:
        """Give Fork's main window the keyboard focus by clicking its (empty) title strip."""
        window = window or self.main_window()
        if window is None:
            raise DriverError("Fork's main window is not open")
        self.screen.click_in(window, window.width // 2, 8)
        return window

    def close(self, timeout: float = 60) -> None:
        """Close Fork like a user (Alt+F4 on the main window) and wait for its process to end."""
        window = self.main_window()
        if window is not None:
            self.focus(window)
            self.screen.key("alt+F4")
        self.screen.release_keys()
        try:
            wait_until(lambda: not fork_pids(self.prefix), timeout, "Fork to exit after Alt+F4")
        finally:
            self.screen.release_keys()

    def ensure_closed(self, timeout: float = 60) -> None:
        """Close Fork if it runs; kill it (and its Wine processes) when it does not exit."""
        if not fork_pids(self.prefix):
            return
        try:
            self.close(timeout)
        except DriverError:
            self.kill()

    def kill(self) -> None:
        """``wineserver -k`` for this prefix (every managed runtime that could serve it)."""
        runtimes = Path(self.env["XDG_DATA_HOME"]) / "fork-linux" / "runtimes" / "wine"
        for server in sorted(runtimes.glob("*/bin/wineserver")):
            subprocess.run([str(server), "-k"], env={**self.env, "WINEPREFIX": str(self.prefix)},
                           capture_output=True, timeout=60, check=False)
        for pid in fork_pids(self.prefix):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        wait_until(lambda: not fork_pids(self.prefix), 30, "Fork to be killed")

    # -- Fork's files ---------------------------------------------------------------------------

    @property
    def user(self) -> str:
        """The Windows user name of the prefix (Wine uses the Unix login name)."""
        return self.env.get("USER") or Path.home().name

    @property
    def local_dir(self) -> Path:
        """``AppData\\Local`` of the prefix's user."""
        return self.prefix / "drive_c" / "users" / self.user / "AppData" / "Local"

    def forget_tabs(self) -> None:
        """Empty the tab lists of Fork's workspaces, so the next start opens no old repositories.

        Fork reopens every repository tab at each start and refreshes (and fetches) them all in the
        background; a tier that left dozens of tabs open slowed Git Flow's initialization past a minute.
        Fork must be closed (it writes its settings when it exits).
        """
        if not self.settings_path().is_file():
            return
        workspaces = self.settings().get("Workspaces", {}).get("All", [])
        if not any(workspace.get("Repositories") for workspace in workspaces):
            return
        for workspace in workspaces:
            workspace["Repositories"] = []
            workspace["ActiveRepository"] = None
        result = self.cli("settings", "set", "Workspaces.All", json.dumps(workspaces))
        if result.returncode != 0:
            raise DriverError(f"settings set Workspaces.All failed: {result.stderr.strip()}")

    def settings_path(self) -> Path:
        """Fork's ``settings.json``."""
        return self.local_dir / "Fork" / "settings.json"

    def settings(self) -> dict[str, Any]:
        """Fork's ``settings.json`` (Fork writes it with a UTF-8 BOM)."""
        return json.loads(self.settings_path().read_text(encoding="utf-8-sig"))

    def fork_log(self) -> str:
        """Fork's own ``fork.log`` of the current session."""
        try:
            return (self.local_dir / "Fork" / "logs" / "fork.log").read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""


# -- git ---------------------------------------------------------------------------------------------

GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "E2E Fixture",
    "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
    "GIT_COMMITTER_NAME": "E2E Fixture",
    "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
}


def git(repo: Path, *args: str, check: bool = True, env: Mapping[str, str] | None = None) -> str:
    """Native ``git -C repo ARGS`` (the test's own view of the repository); stdout."""
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=120,
                            env={**os.environ, **GIT_IDENTITY, **(env or {})}, check=False)
    if check and result.returncode != 0:
        raise DriverError(f"git {' '.join(args)} in {repo} failed: {result.stderr.strip()}")
    return result.stdout.rstrip("\n")


@functools.cache
def remotes_dir() -> Path:
    """A new directory under ``/tmp`` for this session's bare remotes.

    Fork's git reaches ``/tmp`` through the overlay's url rewrites (the E2E root is not under one of the
    rewritten tops); a new directory per session lets a kept container run the tests again.
    """
    return Path(tempfile.mkdtemp(prefix="fl-e2e-remotes-", dir="/tmp"))


def windows_path(path: Path) -> str:
    """The Windows path Fork and its programs see for a Linux path (Wine's ``Z:`` drive)."""
    return "Z:" + str(path).replace("/", "\\")


def make_repo(path: Path, files: Mapping[str, str] | None = None, *, message: str = "initial") -> Path:
    """A new repository at ``path`` with ``files`` committed on ``main`` (a README by default)."""
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.name", "E2E Test")
    git(path, "config", "user.email", "e2e@example.invalid")
    for name, content in (files or {"README.md": f"# {path.name}\n"}).items():
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    git(path, "add", "-A")
    git(path, "commit", "-qm", message)
    return path


def commit_file(repo: Path, name: str, content: str, message: str) -> str:
    """Write ``name`` and commit it; return the new HEAD."""
    target = repo / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    git(repo, "add", "--", name)
    git(repo, "commit", "-qm", message)
    return git(repo, "rev-parse", "HEAD")
