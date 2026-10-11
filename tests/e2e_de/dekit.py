"""The desktop session of the desktop E2E tier: Xvfb, a D-Bus session bus and a desktop's own programs.

Containers only (``scripts/ci-e2e-de.sh``). :class:`Session` starts, from a ``desktops.Desktop``
recipe, an Xvfb display, ``dbus-daemon --session`` (so D-Bus activated services such as
``xfconfd``, the portals and the file manager get the session's environment), the desktop's
settings daemon, window manager and file-manager service, and waits until each is really up.
Every process it starts, and everything those start, carries ``FL_E2E_DE_SESSION`` or the
session's ``DISPLAY`` in its environment (the bridge daemon starts its helpers with a clean
environment, Wine keeps ``DISPLAY``); :meth:`Session.stop` ends exactly those processes (found
through ``/proc``).

The session's home, logs and screenshots live in the E2E root, which ``real_tier.prepare_root``
checked (absolute, symlink-free, never a home directory). Screenshots stay there: they may show
Fork's logo, so they never go into the repository or a CI upload.
"""

from __future__ import annotations

import os
import shlex
import signal
import subprocess
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

import desktops

SCREEN = "1920x1080x24"
SESSION_MARKER = "FL_E2E_DE_SESSION"
FILE_MANAGER1 = "org.freedesktop.FileManager1"
START_TIMEOUT = 30.0
POLL = 0.3
T = TypeVar("T")
# Variables of the container's own environment that the session keeps.
KEPT = ("PATH", "LANG", "USER", "LOGNAME", "TZ", "FL_E2E_SEED", "FL_E2E_WINETRICKS_CACHE", "FL_E2E_FORK_VERSION")


class SessionError(AssertionError):
    """The session (or something waited for in it) did not come up in time."""


def wait_for(probe: Callable[[], T | None], timeout: float, what: str) -> T:
    """Poll ``probe`` until it returns something other than None/False; :class:`SessionError` on timeout."""
    deadline = time.monotonic() + timeout
    while True:
        value = probe()
        if value is not None and value is not False:
            return value
        if time.monotonic() > deadline:
            raise SessionError(f"timed out after {timeout:.0f} s waiting for {what}")
        time.sleep(POLL)


@dataclass(frozen=True)
class Installed:
    """fork-linux as install.sh put it in the session's home."""

    output: str
    prefix: Path
    package_dir: Path
    host_helper: Path
    menu_file: Path


@dataclass(frozen=True)
class Window:
    """A visible top-level X window."""

    id: str
    name: str
    wm_class: tuple[str, ...]
    pid: int | None
    x: int
    y: int
    width: int
    height: int

    def has_class(self, name: str) -> bool:
        """True when ``name`` is the window's instance or class name (case-insensitive)."""
        return name.lower() in (part.lower() for part in self.wm_class)


def _wm_class(xprop: str) -> tuple[str, ...]:
    """``WM_CLASS(STRING) = "thunar", "Thunar"`` -> ("thunar", "Thunar")."""
    _name, sep, value = xprop.partition("=")
    if not sep:
        return ()
    return tuple(part.strip().strip('"') for part in value.split(","))


def _geometry(shell: str) -> dict[str, int]:
    """``xdotool getwindowgeometry --shell`` output as numbers (missing keys are absent)."""
    values = {}
    for line in shell.splitlines():
        key, sep, value = line.partition("=")
        if sep and value.strip().lstrip("-").isdigit():
            values[key.strip().lower()] = int(value)
    return values


def _environ(pid: int) -> list[bytes]:
    try:
        return Path("/proc", str(pid), "environ").read_bytes().split(b"\0")
    except OSError:
        return []


def _all_pids() -> list[int]:
    """Every process of the container except this one."""
    return sorted(int(entry) for entry in os.listdir("/proc") if entry.isdigit() and int(entry) != os.getpid())


def _comm_and_state(pid: int) -> tuple[str, str]:
    """A process's program name and state letter from ``/proc/<pid>/stat`` (("", "") when gone)."""
    try:
        stat = Path("/proc", str(pid), "stat").read_text(encoding="utf-8")
    except OSError:
        return "", ""
    comm = stat[stat.find("(") + 1 : stat.rfind(")")]
    return comm, stat[stat.rfind(")") + 2 : stat.rfind(")") + 3]


def pids_of(program: str) -> list[int]:
    """Live processes (not zombies) whose program name (15 bytes) is ``program`` (case-insensitive)."""
    found = []
    for pid in _all_pids():
        comm, state = _comm_and_state(pid)
        if state not in ("", "Z") and comm.lower() == program.lower()[:15]:
            found.append(pid)
    return found


class Session:
    """A running desktop session on one Xvfb display, built from a :class:`desktops.Desktop`."""

    def __init__(self, desktop: desktops.Desktop, root: Path, display: str = ":50") -> None:
        self.desktop = desktop
        self.root = root
        self.home = root / "home"
        self.logs = root / "logs"
        self.shots = root / "shots"
        self.runtime = root / "run"
        self.display = display
        self.session_id = f"{os.getpid()}{display}"
        # A process belongs to the session when its environment has the marker or the display
        # (the bridge daemon starts its helpers with a clean environment, Wine keeps DISPLAY).
        self._owned = (f"{SESSION_MARKER}={self.session_id}".encode(), f"DISPLAY={display}".encode())
        self._started: list[subprocess.Popen[bytes]] = []
        for directory in (self.home, self.logs, self.shots):
            directory.mkdir(parents=True, exist_ok=True)
        self.runtime.mkdir(mode=0o700, exist_ok=True)
        os.chmod(self.runtime, 0o700)
        self.env = self._base_env()

    # --- environment ----------------------------------------------------------------------------

    def _base_env(self) -> dict[str, str]:
        home = self.home
        env = {key: os.environ[key] for key in KEPT if key in os.environ}
        env.update(
            {
                "HOME": str(home),
                "XDG_CONFIG_HOME": str(home / ".config"),
                "XDG_DATA_HOME": str(home / ".local" / "share"),
                "XDG_CACHE_HOME": str(home / ".cache"),
                "XDG_STATE_HOME": str(home / ".local" / "state"),
                "XDG_RUNTIME_DIR": str(self.runtime),
                "DISPLAY": self.display,
                "DBUS_SESSION_BUS_ADDRESS": f"unix:path={self.runtime / 'bus'}",
                "SHELL": "/bin/bash",
                "PATH": f"{home / '.local' / 'bin'}:{os.environ.get('PATH', os.defpath)}",
                "PYTHONDONTWRITEBYTECODE": "1",
                # No accessibility bus in the container: keep GTK from asking for one.
                "NO_AT_BRIDGE": "1",
                SESSION_MARKER: self.session_id,
            }
        )
        env.update({name: value.replace("{home}", str(home)) for name, value in self.desktop.session_env.items()})
        return env

    def expand(self, argv: Sequence[str]) -> list[str]:
        """``argv`` with ``{home}`` replaced by the session's home."""
        return [arg.replace("{home}", str(self.home)) for arg in argv]

    # --- processes --------------------------------------------------------------------------------

    def run(
        self, argv: Sequence[str], *, timeout: float = 60, check: bool = True, cwd: Path | None = None,
        extra: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Run ``argv`` in the session and wait; a failure fails the test when ``check``."""
        result = subprocess.run(
            self.expand(argv), env={**self.env, **(extra or {})}, cwd=str(cwd or self.home),
            capture_output=True, text=True, timeout=timeout, check=False,
        )
        if check and result.returncode != 0:
            raise SessionError(
                f"{shlex.join(argv)} exited {result.returncode}:\n{result.stdout}\n{result.stderr}"
            )
        return result

    def spawn(
        self, name: str, argv: Sequence[str], *, cwd: Path | None = None, extra: Mapping[str, str] | None = None,
    ) -> subprocess.Popen[bytes]:
        """Start ``argv`` in the background (its own session), output in ``logs/<name>.log``."""
        with open(self.logs / f"{name}.log", "ab") as log:
            process = subprocess.Popen(
                self.expand(argv), env={**self.env, **(extra or {})}, cwd=str(cwd or self.home),
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
            )
        self._started.append(process)
        return process

    def session_pids(self) -> list[int]:
        """PIDs of every process of this session (marker or display in its environment), this one excluded."""
        found = []
        for pid in _all_pids():
            environ = _environ(pid)
            if any(owned in environ for owned in self._owned) and _comm_and_state(pid)[1] not in ("", "Z"):
                found.append(pid)
        return found

    # --- start / stop -------------------------------------------------------------------------------

    def start(self) -> None:
        """Xvfb, the session bus, then the desktop's settings daemon, window manager and file manager."""
        self.spawn("xvfb", ["Xvfb", self.display, "-screen", "0", SCREEN, "-dpi", "96", "-nolisten", "tcp"])
        wait_for(lambda: self.run(["xdpyinfo"], check=False, timeout=10).returncode == 0, START_TIMEOUT, "Xvfb")
        bus = self.runtime / "bus"
        if bus.exists():
            bus.unlink()
        self.spawn("dbus", ["dbus-daemon", "--session", "--nofork", "--nopidfile", f"--address=unix:path={bus}"])
        wait_for(lambda: self._dbus("org.freedesktop.DBus.GetId") is not None, START_TIMEOUT, "the session bus")
        self.spawn("settings-daemon", self.desktop.settings_daemon)
        wait_for(lambda: self.run(self.desktop.settings_ready, check=False).returncode == 0, START_TIMEOUT,
                 f"{self.desktop.settings_daemon[0]} to publish its settings")
        self.spawn("window-manager", self.desktop.window_manager)
        wait_for(lambda: self.window_manager_name() == self.desktop.wm_name, START_TIMEOUT,
                 f"the window manager {self.desktop.wm_name}")
        self.spawn("file-manager", self.desktop.file_manager_service)
        if self.desktop.provides_file_manager1:
            wait_for(lambda: self.name_has_owner(FILE_MANAGER1), START_TIMEOUT, f"{FILE_MANAGER1} on the bus")

    def stop(self) -> None:
        """End every process of the session: TERM, then KILL what is left after 5 s."""
        for sig, grace in ((signal.SIGTERM, 5.0), (signal.SIGKILL, 2.0)):
            pids = self.session_pids()
            for pid in pids:
                try:
                    os.kill(pid, sig)
                except ProcessLookupError:
                    continue
            deadline = time.monotonic() + grace
            while self.session_pids() and time.monotonic() < deadline:
                time.sleep(POLL)
        for process in self._started:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
        self._started.clear()

    def restart(self) -> None:
        """A fresh session on the same home (settings changed while it ran take effect from the start)."""
        self.stop()
        self.start()

    # --- D-Bus and X ---------------------------------------------------------------------------------

    def _dbus(self, method: str, *args: str) -> str | None:
        """A call on the bus daemon itself; its printed reply, or None on failure."""
        result = self.run(
            ["gdbus", "call", "--session", "--dest", "org.freedesktop.DBus", "--object-path", "/org/freedesktop/DBus",
             "--method", method, *args],
            check=False, timeout=10,
        )
        return result.stdout.strip() if result.returncode == 0 else None

    def name_has_owner(self, name: str) -> bool:
        """True when a process owns ``name`` on the session bus."""
        return self._dbus("org.freedesktop.DBus.NameHasOwner", name) == "(true,)"

    def name_activatable(self, name: str) -> bool:
        """True when the session bus can start a service for ``name``."""
        listed = self._dbus("org.freedesktop.DBus.ListActivatableNames") or ""
        return f"'{name}'" in listed

    def window_manager_name(self) -> str:
        """``_NET_WM_NAME`` of the window manager's check window ('' without a window manager)."""
        root = self.run(["xprop", "-root", "_NET_SUPPORTING_WM_CHECK"], check=False).stdout
        wid = root.rsplit("#", 1)[-1].strip() if "#" in root else ""
        if not wid:
            return ""
        name = self.run(["xprop", "-id", wid, "_NET_WM_NAME"], check=False).stdout
        _key, sep, value = name.partition("=")
        return value.strip().strip('"') if sep else ""

    def xdotool(self, *args: str, check: bool = True) -> str:
        """``xdotool ARGS`` on the session's display; its output."""
        return self.run(["xdotool", *args], check=check, timeout=30).stdout

    def window(self, wid: str) -> Window | None:
        """The visible window ``wid``, or None when it is gone or has no geometry."""
        geometry = _geometry(self.xdotool("getwindowgeometry", "--shell", wid, check=False))
        if not {"x", "y", "width", "height"} <= geometry.keys():
            return None
        name = self.xdotool("getwindowname", wid, check=False).strip()
        wm_class = _wm_class(self.run(["xprop", "-id", wid, "WM_CLASS"], check=False).stdout)
        pid_text = self.xdotool("getwindowpid", wid, check=False).strip()
        return Window(
            id=wid, name=name, wm_class=wm_class, pid=int(pid_text) if pid_text.isdigit() else None,
            x=geometry["x"], y=geometry["y"], width=geometry["width"], height=geometry["height"],
        )

    def windows(self) -> list[Window]:
        """Every visible top-level window that has a name."""
        found = []
        for wid in self.xdotool("search", "--onlyvisible", "--name", ".", check=False).split():
            window = self.window(wid)
            if window is not None:
                found.append(window)
        return found

    def wait_window(self, match: Callable[[Window], bool], what: str, timeout: float = START_TIMEOUT) -> Window:
        """The first visible window ``match`` accepts, waiting up to ``timeout``."""
        def probe() -> Window | None:
            return next((window for window in self.windows() if match(window)), None)

        try:
            return wait_for(probe, timeout, what)
        except SessionError as exc:
            self.screenshot(f"timeout-{what.replace(' ', '-')[:40]}")
            raise SessionError(f"{exc}; windows: {[(w.name, w.wm_class) for w in self.windows()]}") from None

    def parent(self, wid: str) -> str:
        """The X parent of window ``wid`` (``xwininfo -tree``), as a hex id."""
        tree = self.run(["xwininfo", "-tree", "-id", wid]).stdout
        for line in tree.splitlines():
            if "Parent window id:" in line:
                return line.split(":", 1)[1].split()[0]
        raise SessionError(f"xwininfo shows no parent for {wid}")

    def root_window(self) -> str:
        """The root window's id (hex)."""
        for line in self.run(["xwininfo", "-root"]).stdout.splitlines():
            if "Window id:" in line:
                return line.split(":", 2)[2].split()[0]
        raise SessionError("xwininfo shows no root window id")

    def activate(self, window: Window) -> None:
        """Raise and focus ``window`` the way the window manager does for a click."""
        self.xdotool("windowactivate", "--sync", window.id, check=False)
        time.sleep(0.5)

    def keys(self, keys: Iterable[str], delay: float = 0.4) -> None:
        """Press ``keys`` one after another on the focused window."""
        for key in keys:
            self.xdotool("key", key)
            time.sleep(delay)

    def screenshot(self, name: str, window: Window | None = None) -> Path:
        """The screen (or ``window``) as ``shots/<name>.png`` in the scratch root, never the repository."""
        target = self.shots / f"{name}.png"
        argv = ["magick", "import", "-display", self.display, "-window", "root"]
        if window is not None:
            argv += ["-crop", f"{window.width}x{window.height}+{window.x}+{window.y}"]
        self.run([*argv, str(target)], timeout=60)
        return target

    def image_stat(self, image: Path, expr: str) -> float:
        """An ImageMagick ``fx`` statistic of ``image`` (``mean``, ``standard_deviation``)."""
        out = self.run(["magick", str(image), "-format", f"%[fx:{expr}]", "info:"]).stdout
        return float(out.strip())


def cwd_of(pid: int) -> str:
    """The working directory of process ``pid`` ('' when it is gone)."""
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        return ""


def children_cwds(pid: int) -> list[str]:
    """Working directories of ``pid``'s direct children (a terminal's shells)."""
    try:
        children = Path("/proc", str(pid), "task", str(pid), "children").read_text(encoding="utf-8").split()
    except OSError:
        return []
    return [cwd_of(int(child)) for child in children]
