"""Real tier of the Fork-update scenarios: the official Fork N-1 (2.23.1) updating to N.

F1, F2, F3 (positive), F4, F5, F6, F7, F9, F10, F19 and F23 of docs/qa/UPDATE-SCENARIOS.md, against
the official installers and Fork's own updater (Velopack, File > Check for Updates...). The
tests run in file order and build on each other in one prefix of their own,
``<root>/fork-update``: an earlier failure makes the later ones fail fast (``conftest.py``).

Gated by ``FL_E2E_FORK=1`` and ``FL_E2E_FORK_UPDATES=1`` (network: Fork 2.23.1 from
``cdn.fork.dev``, Fork's update feeds and its delta package); containers only:
``scripts/e2e-docker.sh --name upd pytest tests/e2e/test_fork_update.py``. The bridge
scenarios (F7, F19, F23) need the built bridge (``scripts/build-bridge.sh``, ``build-bridge/``).

What Fork does is read from its own records, never guessed from pixels: ``velopack.log`` (which
feed was read, what was found, what was applied), ``current\\sq.version``, ``packages\\``,
``/proc`` (processes and their environment), the bridge daemon's log and native git.
Desktop notifications and dialogs go to stand-in ``notify-send`` / ``zenity`` scripts that
record what they were asked to show. Screenshots stay in ``<root>/fork-update/shots``.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

import pytest

from fixtures import real_tier
from fork_linux import APP_ID, APP_NAME, feeds, manifest, resources, versions

if os.environ.get("FL_E2E_FORK") != "1" or os.environ.get("FL_E2E_FORK_UPDATES") != "1":
    pytest.skip(
        "requires FL_E2E_FORK=1 and FL_E2E_FORK_UPDATES=1 (real Wine, the official Fork 2.23.1 -> 2.23.2, "
        "Fork's update feeds, Xvfb and xdotool)",
        allow_module_level=True,
    )
# Containers only (AGENTS.md hard rule 18).
real_tier.enforce()

T = TypeVar("T")

OLD = "2.23.1"
NEW = manifest.load().fork_default
SETUP = ("setup", "--accept-fork-eula", "--no-gui", "--fork-version", OLD, "--allow-untested")
SETUP_TIMEOUT = 3600.0
WINDOW_TIMEOUT = 180.0
UPDATE_TIMEOUT = 180.0
POLL = 0.3
WM_CLASS = "fork.exe"
MAIN_TITLE = "Fork"
MAIN_MIN_WIDTH = 800
WELCOME_TITLE = "User information"
CHECKING_TITLE = "Checking for updates..."
READY_TITLE = "Fork update is ready to be installed"
# The main window opens there (seeded before each start; no window manager on the display).
MAIN_GEOMETRY = '{"Left": 40, "Top": 30, "Width": 1500, "Height": 940, "WindowState": 0}'
# Offsets measured with Fork 2.23.1 / 2.23.2 on the 1600x1000 display.
WELCOME_NAME = (460, 138)
WELCOME_EMAIL = (460, 194)
WELCOME_FINISH = (523, 337)
GIT_INSTANCE_WIDTH = 680
GIT_INSTANCE_BUTTON_FROM_BOTTOM = 41
FILE_MENU_CHECK_FOR_UPDATES = (60, 260)  # inside the File menu popup (Alt+F)
READY_RESTART = (91, -35)  # "Restart and Update" (negative: from the far edge)
READY_CLOSE = (-52, -35)
CHECKING_CLOSE = (-52, -33)
# Local Changes (Ctrl+1) in the 1500x940 main window with the tab list collapsed.
STAGE_BUTTON = (521, 127)
COMMIT_SUBJECT = (1020, 854)
# Our environment in the Fork process Velopack restarts (spike S9); Wine adds only its own.
FORK_ENV = ("WINEPREFIX", "WINESERVER", "WINELOADER", "WINEDEBUG", "WINEDLLOVERRIDES", "WINEHOME", "FL_WINE")
WINE_ADDS = {"WINESERVERSOCKET", "WINEPRELOADRESERVE", "GNUTLS_SYSTEM_PRIORITY_FILE"}
NOTIFY_SLEEP = 90
# What the scenarios observed while Velopack restarted Fork (test_f1 records, test_f10 checks).
RESTART: dict[str, Any] = {}


# -- the environment --------------------------------------------------------------------------------


class Fail(AssertionError):
    """A step of the scenario did not happen."""


def wait_until(check: Callable[[], T | None], timeout: float, what: str, poll: float = POLL) -> T:
    """Poll ``check`` until it returns something truthy; fail with ``what`` after ``timeout`` seconds."""
    deadline = time.monotonic() + timeout
    while True:
        value = check()
        if value:
            return value
        if time.monotonic() > deadline:
            raise Fail(f"timed out after {timeout:.0f} s waiting for {what}")
        time.sleep(poll)


FAKE_NOTIFY = f"""#!/bin/sh
printf 'notify-send %s\\n' "$*" >> "$FL_E2E_UI_LOG"
exec sleep {NOTIFY_SLEEP}
"""
FAKE_ZENITY = """#!/bin/sh
printf 'zenity %s\\n' "$*" >> "$FL_E2E_UI_LOG"
case " $* " in *" --progress "*) cat > /dev/null ;; esac
exit 0
"""


@dataclass
class Upd:
    """The isolated environment of this module: its own root, prefix and stand-in desktop tools."""

    e2e: Any
    root: Path

    @property
    def env(self) -> dict[str, str]:
        return self.e2e.env

    @property
    def home(self) -> Path:
        return self.e2e.home

    @property
    def prefix(self) -> Path:
        return self.e2e.prefix

    @property
    def fork_dir(self) -> Path:
        user = self.env.get("USER") or Path.home().name
        return self.prefix / "drive_c" / "users" / user / "AppData" / "Local" / "Fork"

    @property
    def ui_log(self) -> Path:
        return self.e2e.logs / "desktop-ui.log"

    @property
    def fake_bin(self) -> Path:
        return self.root / "fakebin"

    def cli(self, *args: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return self.e2e.cli(*args, **kwargs)

    def cli_json(self, *args: str) -> Any:
        return json.loads(self.cli(*args, "--json").stdout)

    def launch_env(self) -> dict[str, str]:
        """A desktop launch: the stand-in notify-send and zenity first on PATH."""
        return {"PATH": f"{self.fake_bin}{os.pathsep}{self.env['PATH']}", "FL_E2E_UI_LOG": str(self.ui_log)}

    def ui_messages(self) -> str:
        try:
            return self.ui_log.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""

    def state(self) -> dict[str, Any]:
        return json.loads((self.prefix / ".fork-linux" / "state.json").read_text(encoding="utf-8"))

    def settings(self) -> dict[str, Any]:
        return json.loads((self.fork_dir / "settings.json").read_text(encoding="utf-8-sig"))

    def installed(self) -> str | None:
        found = self.cli_json("update", "--check")["installed"]
        return found if isinstance(found, str) else None

    def packages(self) -> list[str]:
        return sorted(path.name for path in (self.fork_dir / "packages").glob("Fork-*.nupkg"))

    def own_log(self) -> str:
        """fork-linux's own log (``<state>/logs/fork-linux.log``): every process, with its pid."""
        try:
            return (self.home / ".local" / "state" / "fork-linux" / "logs" / "fork-linux.log").read_text(
                encoding="utf-8", errors="replace"
            )
        except FileNotFoundError:
            return ""

    def velopack_log(self) -> str:
        try:
            return (self.fork_dir / "velopack.log").read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            return ""


def _seed_from(source: Path, target: Path) -> None:
    """Hard-link (or copy) the session's verified downloads and winetricks cache into this module's home."""
    if not source.is_dir():
        return
    shutil.copytree(source, target, copy_function=_link_or_copy, dirs_exist_ok=True)


def _link_or_copy(source: str, target: str) -> None:
    try:
        os.link(source, target)
    except OSError:
        shutil.copy2(source, target)


def _write_tool(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


@pytest.fixture(scope="module")
def upd(e2e: Any) -> Iterator[Upd]:
    """This module's own root under the session root: a fresh prefix, never the other modules' one."""
    root = e2e.root / "fork-update"
    env = type(e2e)(root, e2e.display)
    for sub in (".config", ".local/share", ".cache", ".local/state"):
        (env.home / sub).mkdir(parents=True, exist_ok=True)
    (root / "run").mkdir(mode=0o700, exist_ok=True)
    os.chmod(root / "run", 0o700)
    for directory in (env.shots, env.logs, env.repos):
        directory.mkdir(exist_ok=True)
    _seed_from(e2e.home / ".cache" / "fork-linux" / "downloads", env.home / ".cache" / "fork-linux" / "downloads")
    _seed_from(e2e.home / ".cache" / "winetricks", env.home / ".cache" / "winetricks")
    context = Upd(env, root)
    context.fake_bin.mkdir(exist_ok=True)
    _write_tool(context.fake_bin / "notify-send", FAKE_NOTIFY)
    _write_tool(context.fake_bin / "zenity", FAKE_ZENITY)
    try:
        yield context
    finally:
        _kill_wine(context)
        for pid, _cmdline in _processes(lambda cmdline: cmdline == f"sleep {NOTIFY_SLEEP}"):
            _signal(pid, signal.SIGKILL)


def _signal(pid: int, signo: int) -> None:
    try:
        os.kill(pid, signo)
    except ProcessLookupError:
        pass


def _kill_wine(context: Upd) -> None:
    """``wineserver -k`` for this module's prefix with every managed runtime it installed."""
    runtimes = context.home / ".local" / "share" / "fork-linux" / "runtimes" / "wine"
    for server in sorted(runtimes.glob("*/bin/wineserver")):
        subprocess.run([str(server), "-k"], env={**context.env, "WINEPREFIX": str(context.prefix)},
                       capture_output=True, timeout=60, check=False)


# -- processes ----------------------------------------------------------------------------------------


def _processes(match: Callable[[str], bool]) -> list[tuple[int, str]]:
    """``(pid, command line)`` of this user's processes whose space-joined command line matches."""
    found = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            raw = Path("/proc", entry, "cmdline").read_bytes()
        except OSError:
            continue
        cmdline = raw.replace(b"\0", b" ").decode(errors="replace").strip()
        if cmdline and match(cmdline):
            found.append((int(entry), cmdline))
    return found


def _environ(pid: int) -> dict[str, str]:
    """The environment of process ``pid`` ({} once it is gone)."""
    try:
        raw = Path("/proc", str(pid), "environ").read_bytes()
    except OSError:
        return {}
    pairs = (item.split(b"=", 1) for item in raw.split(b"\0") if b"=" in item)
    return {key.decode(errors="replace"): value.decode(errors="replace") for key, value in pairs}


def _in_prefix(context: Upd, pid: int) -> bool:
    return _environ(pid).get("WINEPREFIX") == str(context.prefix)


def fork_pids(context: Upd) -> list[int]:
    """Fork itself in our prefix (not Velopack's hooks ``Fork.exe --veloapp-*``, not ``Update.exe``)."""
    return sorted(
        pid
        for pid, cmdline in _processes(lambda c: "\\Fork\\current\\Fork.exe" in c and "--veloapp" not in c)
        if _in_prefix(context, pid)
    )


def any_fork_pids(context: Upd) -> list[int]:
    """Every Fork.exe and Update.exe of our prefix (Velopack's hooks included)."""
    return sorted(
        pid
        for pid, _cmdline in _processes(lambda c: "\\AppData\\Local\\Fork\\" in c and ".exe" in c.lower())
        if _in_prefix(context, pid)
    )


def updater_pids(context: Upd) -> list[int]:
    """Velopack's ``Update.exe`` in our prefix."""
    return sorted(pid for pid, _c in _processes(lambda c: "\\Fork\\Update.exe" in c) if _in_prefix(context, pid))


def daemons() -> list[tuple[int, str]]:
    """Running bridge daemons."""
    return _processes(lambda cmdline: "fl-bridge-helper" in cmdline and "--daemon" in cmdline)


# -- the screen -----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Window:
    """A visible Fork window (root coordinates)."""

    id: str
    title: str
    x: int
    y: int
    width: int
    height: int

    def point(self, offset: tuple[int, int]) -> tuple[int, int]:
        """Root coordinates of ``offset`` inside the window (negative: from the far edge)."""
        dx, dy = offset
        return (self.x + (dx if dx >= 0 else self.width + dx), self.y + (dy if dy >= 0 else self.height + dy))

    @property
    def is_main(self) -> bool:
        return self.title == MAIN_TITLE and self.width >= MAIN_MIN_WIDTH


class Screen:
    """xdotool and screenshots on the tier's display."""

    def __init__(self, context: Upd) -> None:
        self.context = context

    def xdotool(self, *args: str, check: bool = False) -> str:
        result = subprocess.run(["xdotool", *args], env=self.context.env, capture_output=True, text=True,
                                timeout=30, check=False)
        if check and result.returncode != 0:
            raise Fail(f"xdotool {' '.join(args)} failed: {result.stderr.strip()}")
        return result.stdout

    def windows(self) -> list[Window]:
        found = []
        for wid in self.xdotool("search", "--onlyvisible", "--class", WM_CLASS).split():
            values = dict(line.split("=", 1) for line in self.xdotool("getwindowgeometry", "--shell", wid).splitlines()
                          if "=" in line)
            try:
                x, y, width, height = (int(values[key]) for key in ("X", "Y", "WIDTH", "HEIGHT"))
            except (KeyError, ValueError):
                continue
            found.append(Window(wid, self.xdotool("getwindowname", wid).strip(), x, y, width, height))
        return found

    def find(self, title: str) -> Window | None:
        return next((window for window in reversed(self.windows()) if window.title == title), None)

    def main(self) -> Window | None:
        return next((window for window in self.windows() if window.is_main), None)

    def click(self, window: Window, offset: tuple[int, int], settle: float = 0.8) -> None:
        x, y = window.point(offset)
        self.xdotool("mousemove", "--sync", str(x), str(y), check=True)
        self.xdotool("click", "1", check=True)
        time.sleep(settle)

    def key(self, *keys: str, settle: float = 0.6) -> None:
        self.xdotool("key", "--delay", "90", *keys, check=True)
        time.sleep(settle)

    def type(self, text: str) -> None:
        self.xdotool("type", "--delay", "35", "--", text, check=True)
        time.sleep(0.4)

    def shot(self, name: str) -> Path:
        target = self.context.e2e.shots / f"{name}.png"
        subprocess.run(["magick", "import", "-display", self.context.e2e.display, "-window", "root", str(target)],
                       env=self.context.env, capture_output=True, timeout=60, check=False)
        return target


# -- Fork, as a user drives it -------------------------------------------------------------------------


@dataclass
class Launched:
    """A ``fork-linux run`` / ``fork`` started in the background and the file holding its output."""

    process: subprocess.Popen[bytes]
    log: Path

    def output(self) -> str:
        return self.log.read_text(encoding="utf-8", errors="replace")


class ForkSession:
    """Starting, using and closing Fork in this module's prefix."""

    def __init__(self, context: Upd) -> None:
        self.context = context
        self.screen = Screen(context)

    def place(self) -> None:
        """Seed the main window's geometry and a collapsed tab list (Fork closed): fixed offsets."""
        for key, value in (("MainWindowLocationState", MAIN_GEOMETRY), ("TabListSidebarCollapsed", "true")):
            self.context.cli("settings", "set", key, value)

    def launch(self, *targets: Path, prog: str = "run", name: str = "") -> Launched:
        """``fork-linux run TARGETS`` (or ``fork TARGETS``) as the desktop starts it, detached."""
        log = self.context.e2e.logs / f"{name or 'launch'}-{time.time_ns()}.log"
        if prog == "fork":
            argv = [sys.executable, "-c",
                    "import sys; from fork_linux.cli import main; raise SystemExit(main(sys.argv[1:], prog='fork'))",
                    *map(str, targets)]
        else:
            argv = [sys.executable, "-m", "fork_linux", "run", *map(str, targets)]
        with open(log, "wb") as handle:
            process = subprocess.Popen(argv, env={**self.context.env, **self.context.launch_env()},
                                       cwd=str(self.context.root), stdin=subprocess.DEVNULL, stdout=handle,
                                       stderr=subprocess.STDOUT, start_new_session=True)
        return Launched(process, log)

    def start(self, *targets: Path, first_run: bool = False) -> Window:
        """Start Fork (closed before) and wait for its main window."""
        self.place()
        self.launch(*targets)
        return self.wait_main(first_run=first_run)

    def wait_main(self, *, first_run: bool = False, timeout: float = WINDOW_TIMEOUT) -> Window:
        """The main window; the git-instance dialog is answered, the welcome dialog only on a first run.

        A welcome dialog after the first run means Fork lost its user settings.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for window in self.screen.windows():
                if window.is_main:
                    time.sleep(2)
                    return self.screen.main() or window
                self._dialog(window, first_run)
            time.sleep(1)
        self.screen.shot("no-main-window")
        raise Fail(f"no Fork main window within {timeout:.0f} s; windows: {self.screen.windows()}")

    def _dialog(self, window: Window, first_run: bool) -> None:
        if window.title == WELCOME_TITLE:
            if not first_run:
                self.screen.shot("unexpected-welcome")
                raise Fail("Fork asks for the user information again: it lost its settings")
            self.screen.click(window, WELCOME_NAME)
            self.screen.type("E2E Test")
            self.screen.click(window, WELCOME_EMAIL)
            self.screen.type("e2e@example.invalid")
            self.screen.click(window, WELCOME_FINISH)
        elif window.title == MAIN_TITLE and window.width == GIT_INSTANCE_WIDTH:
            self.screen.click(window, (window.width // 2 - 1, window.height - GIT_INSTANCE_BUTTON_FROM_BOTTOM))

    def focus(self) -> Window:
        """Give the main window the keyboard (a click on its title strip)."""
        window = self.screen.main()
        if window is None:
            raise Fail("Fork's main window is not open")
        self.screen.click(window, (window.width // 2, 8), settle=0.5)
        return window

    def check_for_updates(self) -> int:
        """File > Check for Updates...; returns where ``velopack.log`` stood before."""
        mark = len(self.context.velopack_log())
        self.focus()
        before = {window.id for window in self.screen.windows()}
        self.screen.key("alt+f", settle=1.0)
        popup = wait_until(lambda: next((w for w in self.screen.windows() if w.id not in before), None), 10,
                           "the File menu")
        self.screen.click(popup, FILE_MENU_CHECK_FOR_UPDATES, settle=0.5)
        return mark

    def velopack_says(self, mark: int, *needles: str, timeout: float = 90) -> str:
        """Wait until ``velopack.log`` past ``mark`` holds every needle; return that part."""
        def new_lines() -> str | None:
            text = self.context.velopack_log()[mark:]
            return text if all(needle in text for needle in needles) else None

        return wait_until(new_lines, timeout, f"velopack.log to say {needles}", poll=1)

    def close_dialog(self, title: str, button: tuple[int, int]) -> None:
        """Close one of Fork's update dialogs with its button (Escape when the click is lost)."""
        window = self.screen.find(title)
        if window is None:
            return
        self.screen.click(window, button)
        if self.screen.find(title) is not None:
            self.screen.key("Escape", settle=1.0)
        wait_until(lambda: self.screen.find(title) is None, 20, f"'{title}' to close")

    def close(self, timeout: float = 60) -> None:
        """Close Fork like a user (its dialogs, then Alt+F4) and wait until no Fork.exe is left."""
        self.close_dialog(CHECKING_TITLE, CHECKING_CLOSE)
        if self.screen.main() is not None:
            self.focus()
            self.screen.key("alt+F4", settle=1.0)
        self.screen.xdotool("keyup", "alt", "F4")
        try:
            wait_until(lambda: not any_fork_pids(self.context), 15, "Fork to exit after Alt+F4")
        except Fail:
            # Xvfb's XTEST keyboard sometimes loses the Alt+F4: Fork's own close button.
            main = self.screen.main()
            if main is not None:
                self.screen.click(main, (-24, 15))
            wait_until(lambda: not any_fork_pids(self.context), timeout, "Fork to exit")


@pytest.fixture(scope="module")
def fork(upd: Upd) -> ForkSession:
    return ForkSession(upd)


# -- repositories and files --------------------------------------------------------------------------


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=60, check=False)
    if result.returncode != 0:
        raise Fail(f"git {' '.join(args)} in {repo} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def make_repo(context: Upd, name: str) -> Path:
    """A repository with a .gitignore (Fork shows no banner, the layout does not shift) and one commit."""
    path = context.e2e.repos / name
    if path.exists():
        return path
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.name", "E2E Test")
    git(path, "config", "user.email", "e2e@example.invalid")
    (path / "README.md").write_text(f"# {name}\n", encoding="utf-8")
    (path / ".gitignore").write_text("*.tmp\n", encoding="utf-8")
    git(path, "add", "-A")
    git(path, "commit", "-qm", f"initial {name}")
    return path


def digests(context: Upd) -> dict[str, str]:
    """sha256 of Fork's user files: settings, accounts, custom commands, logs and ForkData."""
    found = {}
    roots = [context.fork_dir / name for name in ("settings.json", "accounts.json", "custom-commands.json", "logs")]
    roots.append(context.fork_dir.parent / "ForkData")
    for root in roots:
        files = [root] if root.is_file() else sorted(p for p in root.rglob("*") if p.is_file())
        for path in files:
            found[str(path.relative_to(context.fork_dir.parent))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return found


def feed_assets(context: Upd) -> list[feeds.FeedAsset]:
    """Fork's update feed, read by the test itself (its own cache)."""
    cache = Path(tempfile.mkdtemp(prefix="feed-", dir=context.root))
    text = feeds.fetch_feed(manifest.load().feed_url, cache, offline=False, validate=feeds.parse_releases_json)
    return feeds.parse_releases_json(text)


def feed_sha(assets: list[feeds.FeedAsset], version: str) -> str:
    return next(a.sha256 for a in assets if a.type == "Full" and a.version == version and a.sha256)


def nupkg_sha(context: Upd, version: str) -> str:
    return hashlib.sha256((context.fork_dir / "packages" / f"Fork-{version}-full.nupkg").read_bytes()).hexdigest()


def open_tabs(context: Upd) -> list[str]:
    """The repositories Fork had open (its workspaces in settings.json, written when it closes)."""
    workspaces = context.settings().get("Workspaces") or {}
    return [repo.lower() for space in workspaces.get("All") or [] for repo in space.get("Repositories") or []]


def has_tab(context: Upd, repo: Path) -> bool:
    return any(tab.endswith("\\repos\\" + repo.name.lower()) for tab in open_tabs(context))


def check(report: dict[str, Any], check_id: str) -> dict[str, Any]:
    return next(item for item in report["checks"] if item["id"] == check_id)


def marker_times(context: Upd, *steps: str) -> dict[str, str]:
    markers = context.state()["steps"]
    return {step: markers.get(step, {}).get("completed", "") for step in steps}


def bridge_log(context: Upd) -> Path:
    logs = sorted((context.home / ".local" / "state" / "fork-linux" / "logs").glob("bridge-*.log"))
    if not logs:
        raise Fail("no bridge daemon log")
    return logs[-1]


def git_sessions(log: Path) -> int:
    return sum(" argv0=git " in line for line in log.read_text(encoding="utf-8").splitlines())


def need_bridge() -> None:
    helper = resources.bridge_helper_path()
    shims = resources.shims_dir()
    if not helper.is_file() or shims is None or not (shims / "fl-shim.exe").is_file():
        pytest.skip(f"the bridge is not built ({helper}): run scripts/build-bridge.sh first")


# -- Fork's own updater -----------------------------------------------------------------------------------


def stage_update(fork: ForkSession) -> str:
    """File > Check for Updates... on develop: Velopack downloads N and says it is ready; what velopack.log said."""
    mark = fork.check_for_updates()
    wait_until(lambda: fork.screen.find(READY_TITLE), UPDATE_TIMEOUT, "Fork's 'ready to be installed' dialog")
    return fork.velopack_says(mark, "Found newer remote release available", f"Fork-{NEW}-full.nupkg")


def no_update_offered(fork: ForkSession, *, feed: str) -> str:
    """File > Check for Updates... reads ``feed`` and finds nothing; no 'ready' dialog comes up."""
    mark = fork.check_for_updates()
    said = fork.velopack_says(mark, feed, "No updates")
    time.sleep(5)
    assert fork.screen.find(READY_TITLE) is None
    fork.screen.shot(f"no-update-{int(time.time())}")
    fork.close_dialog(CHECKING_TITLE, CHECKING_CLOSE)
    return said


def restart_and_update(fork: ForkSession, during: Callable[[], None] | None = None) -> None:
    """Click Restart and Update; ``during`` runs once Velopack's Update.exe is seen; until the new Fork is up."""
    old = set(fork_pids(fork.context))
    ready = wait_until(lambda: fork.screen.find(READY_TITLE), 30, "the ready dialog")
    fork.screen.click(ready, READY_RESTART, settle=0.0)
    wait_until(lambda: updater_pids(fork.context) or not old & set(fork_pids(fork.context)), 60,
               "Velopack's Update.exe to start")
    if during is not None:
        during()
    wait_until(lambda: not old & set(fork_pids(fork.context)), UPDATE_TIMEOUT, "the old Fork to exit")
    wait_until(lambda: set(fork_pids(fork.context)) - old, UPDATE_TIMEOUT, "the restarted Fork")
    fork.wait_main()


def the_one_fork(context: Upd) -> int:
    """The only Fork process left once a forwarded path was handed over (fails when there are more)."""
    time.sleep(3)
    pids = fork_pids(context)
    assert len(pids) == 1, pids
    return pids[0]


def stable_feed() -> str:
    return "/win/stable/releases.win.json"


# -- the scenario ------------------------------------------------------------------------------------------


def test_00_setup_installs_fork_n_minus_1(upd: Upd) -> None:
    """setup --fork-version 2.23.1 --allow-untested: trust on first use, checked against Fork's feed."""
    assert versions.Version(OLD) < versions.Version(NEW)
    upd.cli(*SETUP, timeout=SETUP_TIMEOUT)
    state = upd.state()["fork"]
    assert (state["version"], state["unverified"]) == (OLD, None)
    assert nupkg_sha(upd, OLD) == feed_sha(feed_assets(upd), OLD)
    info = upd.cli_json("update", "--check")
    assert (info["installed"], info["update_available"], info["fork_channel"]) == (OLD, True, "develop")


def test_f9_stable_channel_reads_the_stable_feed(upd: Upd, fork: ForkSession) -> None:
    """Stable lags behind (2.21.1): Fork finds nothing, 'update --fork --latest' is the way."""
    upd.cli("settings", "set", "ApplicationUpdateType", "1")
    text = upd.cli("update", "--check").stdout
    assert "Channel:   stable (Fork's own updater)" in text
    assert "'fork-linux update --fork --latest' installs it" in text
    assert "File > Check for Updates" not in text
    fork.start(make_repo(upd, "demo"), first_run=True)
    said = no_update_offered(fork, feed=stable_feed())
    assert f"localVersion={OLD}" in said
    fork.close()
    # The first launch kept a snapshot of 2.23.1 for rollback (F1's precondition).
    assert [snap["fork_version"] for snap in upd.cli_json("snapshot", "list")] == [OLD]
    upd.cli("settings", "set", "ApplicationUpdateType", "0")
    text = upd.cli("update", "--check").stdout
    assert "Channel:   develop (Fork's own updater)" in text
    assert "or in Fork: File > Check for Updates..., then Restart and Update" in text


def test_f5_known_bad_override_warns_and_doctor_fails(upd: Upd, fork: ForkSession) -> None:
    """An override marks 2.23.1 known-bad: fork warns and still starts it, doctor fails with the rollback hint."""
    override = upd.home / ".config" / "fork-linux" / "manifest.local.json"
    reason = "E2E: marked known-bad by the test"
    override.write_text(json.dumps({"fork": {"known_bad": {OLD: reason}}}), encoding="utf-8")
    try:
        doctor = upd.cli("doctor", "--json", check=False)
        assert doctor.returncode == 17
        version = check(json.loads(doctor.stdout), "fork.version")
        assert version["status"] == "fail"
        assert reason in version["detail"]
        assert "fork-linux rollback" in version["hint"]
        warning = f"warning: the installed version is known not to work well: {reason}"
        assert warning in upd.cli("update", "--check").stdout
        refused = upd.cli("update", "--fork", "--fork-version", OLD, "--allow-untested", check=False)
        assert refused.returncode == 2
        assert "known not to work with fork-linux" in refused.stderr
        fork.start(make_repo(upd, "demo"))
        assert fork_pids(upd)
        warned = upd.ui_messages()
        assert f"Fork {OLD} is known not to work well under Wine: {reason}" in warned
        assert "'fork-linux rollback' goes back to the previous version" in warned
        fork.close()
    finally:
        override.unlink()
    assert check(upd.cli_json("doctor"), "fork.version")["status"] != "fail"


def test_f6_staged_update_reported_and_dropped_when_pinned(upd: Upd, fork: ForkSession) -> None:
    """Velopack staged N but Fork was not restarted: reported, and the pin drops it before Fork starts."""
    fork.start(make_repo(upd, "demo"))
    stage_update(fork)
    fork.screen.shot("f6-ready")
    fork.close_dialog(READY_TITLE, READY_CLOSE)
    fork.close()
    # Velopack deletes the installed version's package once it has staged the newer one.
    assert upd.packages() == [f"Fork-{NEW}-full.nupkg"]
    assert upd.cli_json("update", "--check")["staged"] == [NEW]
    pending = check(upd.cli_json("doctor"), "fork.pending_update")
    assert (pending["status"], NEW in pending["detail"]) == ("info", True)
    upd.cli("config", "set", "fork.update_policy", "pinned")
    pending = check(upd.cli_json("doctor"), "fork.pending_update")
    assert (pending["status"], "pinned" in pending["detail"]) == ("warn", True)
    mark = len(upd.velopack_log())
    fork.start(make_repo(upd, "demo"))
    # Without the pin Velopack would apply the staged package as Fork starts ("Auto apply").
    time.sleep(8)
    assert "Auto apply" not in upd.velopack_log()[mark:]
    assert upd.installed() == OLD
    # 2.23.1's own package came back from its snapshot (UPD-24): the one the feed describes.
    assert upd.packages() == [f"Fork-{OLD}-full.nupkg"]
    assert nupkg_sha(upd, OLD) == feed_sha(feed_assets(upd), OLD)
    assert upd.settings()["ApplicationUpdateType"] == 2
    fork.close()
    upd.cli("config", "set", "fork.update_policy", "auto")


def _probe_during_restart(upd: Upd, fork: ForkSession) -> Callable[[], None]:
    """F10: while Update.exe swaps current\\, 'fork <path>' waits and the commands that need Fork closed refuse."""
    def probe() -> None:
        RESTART["updater_seen"] = bool(updater_pids(upd))
        RESTART["launch"] = fork.launch(make_repo(upd, "second"), prog="fork", name="f10-fork")
        RESTART["update"] = upd.cli("update", "--fork", check=False)
        RESTART["rollback"] = upd.cli("rollback", "--to-version", OLD, check=False)

    return probe


def test_f1_in_app_update_keeps_env_and_notifies(upd: Upd, fork: ForkSession) -> None:
    """File > Check for Updates..., Restart and Update: the new Fork keeps our environment; the next fork says so."""
    fork.start(make_repo(upd, "demo"))
    assert upd.settings()["ApplicationUpdateType"] == 0  # the unpin brought the user's channel back (F4)
    (old_pid,) = fork_pids(upd)
    old_env = _environ(old_pid)
    staged = stage_update(fork)
    restart_and_update(fork, _probe_during_restart(upd, fork))
    launched = RESTART["launch"].process
    wait_until(lambda: launched.poll() is not None, 120, "the 'fork <path>' launched during the restart")
    new_pid = the_one_fork(upd)
    assert new_pid != old_pid
    new_env = _environ(new_pid)
    assert upd.installed() == NEW
    for key in (*FORK_ENV, *(name for name in old_env if name.startswith("GIT_CONFIG_"))):
        assert new_env.get(key) == old_env.get(key), key
    assert set(new_env) - set(old_env) <= WINE_ADDS
    assert f"Package version {NEW} applied successfully" in upd.velopack_log()
    fork.screen.shot("f1-restarted")
    icon_before = marker_times(upd, "icon")
    fork.close()
    RESTART["tab"] = has_tab(upd, make_repo(upd, "second"))
    # The next launch notices the update without delaying Fork's start.
    fork.start(make_repo(upd, "demo"))
    notice = f"Fork updated {OLD} -> {NEW}; 'fork-linux rollback' restores {OLD}"
    assert f"notify-send --app-name={APP_NAME} --icon={APP_ID} {APP_NAME} {notice}" in upd.ui_messages()
    assert _processes(lambda cmdline: cmdline == f"sleep {NOTIFY_SLEEP}"), "notify-send must not hold Fork's start"
    assert marker_times(upd, "icon") != icon_before, "the icon step re-runs for the new Fork.exe"
    assert {snap["fork_version"] for snap in upd.cli_json("snapshot", "list")} == {OLD, NEW}
    report = upd.cli_json("doctor")
    assert check(report, "fork.version")["status"] == "ok"
    assert check(report, "fork.pending_update")["status"] == "ok"
    # A package Velopack rebuilt from the delta differs from the published one the manifest pins; when the
    # delta does not apply, Velopack downloads the full package, which matches.
    rebuilt = "Delta update download complete" in staged
    assert check(report, "fork.integrity")["status"] == ("info" if rebuilt else "ok")


def test_f10_fork_path_during_restart_and_update(upd: Upd, fork: ForkSession) -> None:
    """The 'fork <path>' started while Velopack applied the update reached the restarted Fork, alone."""
    assert RESTART["updater_seen"], "the probe ran while Velopack's Update.exe was running"
    launched = RESTART["launch"]
    assert launched.process.returncode == 0, launched.output()
    waited = f"fork_linux.launcher[{launched.process.pid}]: Fork is updating itself; waiting for Velopack to restart it"
    assert waited in upd.own_log()
    for refused in (RESTART["update"], RESTART["rollback"]):
        assert refused.returncode == 15, refused.stderr
        assert "Fork is running" in refused.stderr or "Fork is updating itself" in refused.stderr
    # The restarted Fork took the path (test_f1 found it the only Fork): it had the repository open.
    assert RESTART["tab"], open_tabs(upd)
    # After the restart (Fork runs again since test_f1's relaunch) a path takes the fast path.
    (running,) = fork_pids(upd)
    started = time.monotonic()
    third = make_repo(upd, "third")
    forwarded = fork.launch(third, prog="fork", name="f10-fast")
    assert forwarded.process.wait(timeout=60) == 0
    assert time.monotonic() - started < 30
    assert the_one_fork(upd) == running
    fork.close()
    assert has_tab(upd, third), open_tabs(upd)


def test_f4_rollback_pins_and_blocks_the_in_app_update(upd: Upd, fork: ForkSession) -> None:
    """rollback --to-version 2.23.1 pins it: Fork's own update check is off and finds nothing; unpin restores it."""
    result = upd.cli("rollback", "--to-version", OLD)
    assert f"Fork is pinned to {OLD}" in result.stdout
    assert upd.installed() == OLD
    assert upd.packages() == [f"Fork-{OLD}-full.nupkg"]  # put back; the newer one dropped
    assert upd.cli("config", "get", "fork.update_policy").stdout.strip() == "pinned"
    fork_state = upd.state()["fork"]
    assert (fork_state["pinned_version"], fork_state["update_type_before_pin"]) == (OLD, 0)
    assert upd.settings()["ApplicationUpdateType"] == 2
    fork.start(make_repo(upd, "demo"))
    no_update_offered(fork, feed=stable_feed())  # Off: a manual check reads the lagging Stable feed
    fork.close()
    fork.start(make_repo(upd, "demo"))
    assert upd.installed() == OLD
    fork.close()
    upd.cli("config", "set", "fork.update_policy", "auto")
    fork.start(make_repo(upd, "demo"))
    assert upd.settings()["ApplicationUpdateType"] == 0
    assert upd.state()["fork"]["update_type_before_pin"] is None
    fork.close()


def _stage_and_commit(fork: ForkSession, repo: Path, subject: str) -> None:
    """Local Changes (Ctrl+1), Stage, a subject, Ctrl+Enter: Fork's own git calls."""
    fork.focus()
    fork.screen.key("ctrl+1", settle=2.0)
    window = fork.focus()
    fork.screen.click(window, STAGE_BUTTON, settle=1.5)
    wait_until(lambda: git(repo, "diff", "--cached", "--name-only"), 30, f"Fork to stage the change in {repo.name}")
    fork.screen.click(window, COMMIT_SUBJECT)
    fork.screen.key("ctrl+a")
    fork.screen.type(subject)
    fork.screen.key("ctrl+Return", settle=2.0)
    wait_until(lambda: git(repo, "log", "-1", "--format=%s") == subject, 60, f"the commit '{subject}'")
    fork.screen.shot(f"commit-{repo.name}")


def _daemon(upd: Upd) -> tuple[int, int]:
    """``(pid, port)`` of the one bridge daemon ``git-bridge status`` reports."""
    status = upd.cli_json("git-bridge", "status")
    daemon = status.get("daemon") or {}
    assert len(daemons()) == 1, daemons()
    return int(daemon["pid"]), int(daemon["port"])


def _daemon_ends_with_fork(upd: Upd, log: Path) -> None:
    started = time.monotonic()
    wait_until(lambda: not daemons(), 30, "the bridge daemon to exit after Fork")
    assert time.monotonic() - started < 20
    assert "daemon exit reason=prefix-idle" in log.read_text(encoding="utf-8")


def test_f7_bridge_survives_the_self_restart(upd: Upd, fork: ForkSession) -> None:
    """Git bridge on, Restart and Update: the same daemon serves the restarted Fork's native git."""
    need_bridge()
    assert upd.installed() == OLD  # test_f4 ended on the rolled-back, unpinned 2.23.1
    upd.cli("git-bridge", "enable")
    repo = make_repo(upd, "bridged")
    (repo / "restart.txt").write_text("staged after Velopack's restart\n", encoding="utf-8")
    fork.start(repo)
    before = _daemon(upd)
    log = bridge_log(upd)
    stage_update(fork)
    restart_and_update(fork)
    assert upd.installed() == NEW
    assert _daemon(upd) == before
    sessions = git_sessions(log)
    _stage_and_commit(fork, repo, "after velopack restart")
    assert git_sessions(log) > sessions, "the restarted Fork's git ran through the daemon"
    assert _daemon(upd) == before
    fork.close()
    _daemon_ends_with_fork(upd, log)


def test_f2_cli_update_preserves_user_data(upd: Upd, fork: ForkSession) -> None:
    """update --fork 2.23.1 -> 2.23.2 with the official installer: Fork's user files are byte-identical."""
    upd.cli("rollback", "--to-version", OLD)
    fork.start(make_repo(upd, "demo"))
    refused = upd.cli("update", "--fork", check=False)
    assert refused.returncode == 15
    fork.close()
    (upd.fork_dir / "accounts.json").write_text('{"e2e": "stays private"}', encoding="utf-8")
    (upd.fork_dir / "custom-commands.json").write_text('[{"Name": "e2e"}]', encoding="utf-8")
    before = digests(upd)
    steps_before = marker_times(upd, "fork_download", "fork_install", "icon")
    result = upd.cli("update", "--fork", timeout=1800)
    assert f"Fork {OLD} -> {NEW} (snapshot " in result.stdout
    assert f"'fork-linux rollback --to-version {OLD}' goes back" in result.stdout
    assert digests(upd) == before
    assert {"settings.json", "accounts.json", "custom-commands.json"} <= {Path(k).name for k in before}
    assert upd.installed() == NEW
    snap = upd.cli_json("snapshot", "list")[0]
    assert (snap["fork_version"], snap["method"]) == (OLD, "copy")
    assert not (Path(snap["path"]) / "data" / "accounts.json").exists()
    assert upd.state()["fork"]["request"] is None
    steps_after = marker_times(upd, "fork_download", "fork_install", "icon")
    assert all(steps_after[step] > steps_before[step] for step in steps_after), (steps_before, steps_after)


def test_f19_update_fork_keeps_the_bridge_working(upd: Upd, fork: ForkSession) -> None:
    """After the CLI update the next fork starts one daemon; staging and committing go through native git."""
    need_bridge()
    repo = make_repo(upd, "bridged")
    (repo / "cli-update.txt").write_text("staged after 'update --fork'\n", encoding="utf-8")
    fork.start(repo)  # no welcome dialog: the user's settings survived the installer
    _daemon(upd)
    log = bridge_log(upd)
    sessions = git_sessions(log)
    _stage_and_commit(fork, repo, "after cli update")
    assert git_sessions(log) > sessions
    fork.close()
    _daemon_ends_with_fork(upd, log)


def test_f3_update_fork_latest_verifies_against_the_feed(upd: Upd) -> None:
    """update --fork --latest: the newest feed version from cdn.fork.dev, its package equal to the feed's sha256."""
    upd.cli("rollback", "--to-version", OLD)
    before = digests(upd)
    assets = feed_assets(upd)
    newest = feeds.latest_full(assets)
    assert newest is not None
    upd.cli("update", "--fork", "--latest", timeout=1800)
    assert upd.installed() == newest.version
    assert nupkg_sha(upd, newest.version) == feed_sha(assets, newest.version)
    assert upd.state()["fork"]["request"] == {"source": "latest"}
    assert digests(upd) == before
    integrity = check(upd.cli_json("doctor"), "fork.integrity")
    assert integrity["status"] in ("ok", "info"), integrity


def test_f23_staged_update_applied_at_the_next_start(upd: Upd, fork: ForkSession) -> None:
    """Velopack applies a staged update by itself when Fork starts: the path and the bridge reach the new Fork."""
    need_bridge()
    upd.cli("rollback", "--to-version", OLD)
    upd.cli("config", "set", "fork.update_policy", "auto")
    fork.start(make_repo(upd, "demo"))
    stage_update(fork)
    fork.close_dialog(READY_TITLE, READY_CLOSE)
    fork.close()
    assert upd.cli_json("update", "--check")["staged"] == [NEW]
    mark = len(upd.velopack_log())
    repo = make_repo(upd, "auto-applied")
    fork.place()
    launched = fork.launch(repo, prog="fork", name="f23-fork")
    fork.velopack_says(mark, "Auto apply is true", f"Package version {NEW} applied successfully",
                       timeout=UPDATE_TIMEOUT)
    fork.wait_main()
    assert launched.process.wait(timeout=60) == 0, launched.output()
    pid = the_one_fork(upd)
    assert upd.installed() == NEW
    port = _daemon(upd)[1]
    env = _environ(pid)
    assert (env["WINEPREFIX"], env.get("FL_BRIDGE_PORT")) == (str(upd.prefix), str(port))
    log = bridge_log(upd)
    wait_until(lambda: git_sessions(log) > 0, 60, "the new Fork's git through the daemon")
    fork.close()
    assert has_tab(upd, repo), open_tabs(upd)
    _daemon_ends_with_fork(upd, log)
    shown = len(upd.ui_messages())
    fork.start(make_repo(upd, "demo"))
    assert f"Fork updated {OLD} -> {NEW}; 'fork-linux rollback' restores {OLD}" in upd.ui_messages()[shown:]
    fork.close()
