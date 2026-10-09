"""The desktop's session type and UI scale, mapped to Fork's ``LayoutScaling`` and Wine's DPI.

Scale sources, first match wins:

1. ``GDK_SCALE`` x ``GDK_DPI_SCALE`` (either alone counts), then ``QT_SCALE_FACTOR``;
2. GNOME ``org.gnome.desktop.interface`` ``scaling-factor`` x ``text-scaling-factor``
   (only when not both at their defaults);
3. ``xrdb -query`` ``Xft.dpi`` / 96 (GNOME and most X11 desktops publish it);
4. KDE ``kcmfonts`` ``forceFontDPI`` (kreadconfig6, then kreadconfig5);
5. XFCE ``xsettings`` ``/Xft/DPI``.

The result is rounded to the nearest 25 % and clamped to 100..300; with no
source it is 100. Every probe is a short command run through the runner
(2 s timeout); a missing tool or a failure just means "no answer". Tools that
talk to the D-Bus session bus (``xfconf-query``) are not started at all when
the session has no bus: they would wait for D-Bus' own (25 s) timeout or try to
autolaunch a bus.
"""

from __future__ import annotations

import logging
import math
import os
import re
import stat
from collections.abc import Callable, Mapping, Sequence

from . import sandbox
from .errors import ForkLinuxError
from .procrun import Runner

log = logging.getLogger(__name__)

PROBE_TIMEOUT = 2.0
# Probe tools that need the D-Bus session bus.
BUS_TOOLS = frozenset({"xfconf-query"})
_UNIX_PATH = "unix:path="
BASE_DPI = 96
MIN_SCALE = 100
MAX_SCALE = 300
SCALE_STEP = 25
GNOME_INTERFACE = "org.gnome.desktop.interface"

_NUMBER = re.compile(r"[-+]?\d+(?:\.\d+)?", re.ASCII)
# (?a:...) keeps the digits ASCII-only while \s keeps its Unicode meaning.
_XFT_DPI = re.compile(r"^\s*Xft\.dpi\s*:\s*((?a:\d+(?:\.\d+)?))\s*$", re.MULTILINE)


def _is_socket(path: str) -> bool:
    try:
        return stat.S_ISSOCK(os.stat(path).st_mode)
    except OSError:
        return False


def session_bus_available(env: Mapping[str, str]) -> bool:
    """True when ``env`` points at a D-Bus session bus that can be reached.

    ``DBUS_SESSION_BUS_ADDRESS`` counts unless every address in it is a
    ``unix:path=`` socket that does not exist; without it, the systemd user
    bus ``$XDG_RUNTIME_DIR/bus`` must exist.
    """
    address = env.get("DBUS_SESSION_BUS_ADDRESS", "").strip()
    if address:
        for entry in address.split(";"):
            if not entry.startswith(_UNIX_PATH):
                return True
            if _is_socket(entry[len(_UNIX_PATH):].split(",", 1)[0]):
                return True
        return False
    runtime_dir = env.get("XDG_RUNTIME_DIR", "")
    return os.path.isabs(runtime_dir) and _is_socket(os.path.join(runtime_dir, "bus"))


def probe(runner: Runner, env: Mapping[str, str], argv: Sequence[str], timeout: float = PROBE_TIMEOUT) -> str:
    """Stripped stdout of ``argv`` if it is installed and exits 0, else ``""`` (never raises).

    The timeout is capped at :data:`PROBE_TIMEOUT`; a :data:`BUS_TOOLS` probe is
    skipped when the session has no D-Bus session bus.
    """
    if argv[0] in BUS_TOOLS and not session_bus_available(env):
        log.debug("no D-Bus session bus; skipping %s", argv[0])
        return ""
    if runner.which(argv[0], path=env.get("PATH")) is None:
        return ""
    try:
        completed = runner.run(list(argv), env=sandbox.clean_env(env), timeout=min(timeout, PROBE_TIMEOUT))
    except ForkLinuxError:
        return ""
    return completed.stdout.strip() if completed.ok else ""


def session_type(env: Mapping[str, str]) -> str:
    """``wayland``, ``x11`` or ``tty`` for the session ``env`` belongs to."""
    declared = env.get("XDG_SESSION_TYPE", "").strip().lower()
    if declared in ("wayland", "x11"):
        return declared
    if env.get("WAYLAND_DISPLAY"):
        return "wayland"
    if env.get("DISPLAY"):
        return "x11"
    return "tty"


def _number(text: str) -> float | None:
    """The last number in ``text`` (``uint32 2`` -> 2.0), or None."""
    found = _NUMBER.findall(text)
    return float(found[-1]) if found else None


def _positive(value: str | None) -> float | None:
    """``value`` as a positive float, or None."""
    number = _number(value or "")
    return number if number is not None and number > 0 else None


def _from_env(env: Mapping[str, str], _runner: Runner) -> float | None:
    """GDK_SCALE x GDK_DPI_SCALE, else QT_SCALE_FACTOR, as a percentage."""
    gdk_scale = _positive(env.get("GDK_SCALE"))
    gdk_dpi = _positive(env.get("GDK_DPI_SCALE"))
    if gdk_scale is not None or gdk_dpi is not None:
        return (gdk_scale or 1.0) * (gdk_dpi or 1.0) * 100
    qt_scale = _positive(env.get("QT_SCALE_FACTOR"))
    return None if qt_scale is None else qt_scale * 100


def _from_gnome(env: Mapping[str, str], runner: Runner) -> float | None:
    """GNOME integer scaling x text scaling, unless both are at their defaults."""
    scaling = _positive(probe(runner, env, ["gsettings", "get", GNOME_INTERFACE, "scaling-factor"]))
    text = _positive(probe(runner, env, ["gsettings", "get", GNOME_INTERFACE, "text-scaling-factor"]))
    factor = (scaling or 1.0) * (text or 1.0)
    return None if math.isclose(factor, 1.0) else factor * 100


def _from_xrdb(env: Mapping[str, str], runner: Runner) -> float | None:
    """``Xft.dpi`` from the X resource database."""
    match = _XFT_DPI.search(probe(runner, env, ["xrdb", "-query"]))
    dpi = _positive(match.group(1)) if match else None
    return None if dpi is None else dpi * 100 / BASE_DPI


def _from_kde(env: Mapping[str, str], runner: Runner) -> float | None:
    """KDE's forced font DPI (0 means 'not forced')."""
    for tool in ("kreadconfig6", "kreadconfig5"):
        argv = [tool, "--file", "kcmfonts", "--group", "General", "--key", "forceFontDPI"]
        dpi = _positive(probe(runner, env, argv))
        if dpi is not None:
            return dpi * 100 / BASE_DPI
    return None


def _from_xfce(env: Mapping[str, str], runner: Runner) -> float | None:
    """XFCE's ``/Xft/DPI`` xsettings value (-1 or missing means unset)."""
    dpi = _positive(probe(runner, env, ["xfconf-query", "-c", "xsettings", "-p", "/Xft/DPI"]))
    return None if dpi is None else dpi * 100 / BASE_DPI


PROBES: tuple[Callable[[Mapping[str, str], Runner], float | None], ...] = (
    _from_env,
    _from_gnome,
    _from_xrdb,
    _from_kde,
    _from_xfce,
)


def normalize(percent: float) -> int:
    """Round ``percent`` to the nearest :data:`SCALE_STEP` and clamp it to 100..300."""
    rounded = int(percent / SCALE_STEP + 0.5) * SCALE_STEP
    return max(MIN_SCALE, min(MAX_SCALE, rounded))


def scale_percent(env: Mapping[str, str], runner: Runner) -> int:
    """The desktop's UI scale in percent (100, 125, ... 300); 100 when nothing says otherwise."""
    for source in PROBES:
        percent = source(env, runner)
        if percent is not None:
            return normalize(percent)
    return MIN_SCALE


def detect_dpi(env: Mapping[str, str], runner: Runner) -> int:
    """The DPI matching :func:`scale_percent` (96 at 100 %, 120 at 125 %, 192 at 200 %)."""
    return round(BASE_DPI * scale_percent(env, runner) / 100)
