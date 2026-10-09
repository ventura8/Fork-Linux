"""Is the desktop dark or light? Probes ported from Ubuntu-Hello's ``theme_detect.py``.

The desktop is classified from ``XDG_CURRENT_DESKTOP`` / ``DESKTOP_SESSION``
and asked in its own terms:

* GNOME, Budgie (and unknown desktops): ``color-scheme``, then the
  ``gtk-theme`` name, of ``org.gnome.desktop.interface`` (dconf, then gsettings);
* Cinnamon: the same keys of ``org.cinnamon.desktop.interface``, then GNOME's;
* KDE Plasma: ``kreadconfig6``/``kreadconfig5`` ``ColorScheme`` and
  ``LookAndFeelPackage`` of ``kdeglobals``, else the ``kdeglobals`` file;
* XFCE: ``xfconf-query`` ``xsettings`` ``/Net/ThemeName``;
* MATE: ``org.mate.interface`` ``gtk-theme``;
* LXQt: ``theme=`` in ``lxqt/lxqt.conf`` or ``lxqt/session.conf``.

When the desktop's own probe has no answer the GNOME probe is tried, then
GTK's ``settings.ini`` (``gtk-application-prefer-dark-theme`` or a theme name
containing "dark"). A ``GTK_THEME`` containing ``dark`` (``Adwaita:dark``)
wins over everything. Unlike the original, nothing runs as another user
(fork-linux never runs as root), and ``None`` means "no idea".
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path

from . import display
from .procrun import Runner

DARK = "dark"
LIGHT = "light"
SCHEMA_GNOME = "org.gnome.desktop.interface"
SCHEMA_CINNAMON = "org.cinnamon.desktop.interface"
SCHEMA_MATE = "org.mate.interface"
GTK_SETTINGS_DIRS = ("gtk-4.0", "gtk-3.0")
LXQT_FILES = ("lxqt.conf", "session.conf")

# (desktop, names that identify it in XDG_CURRENT_DESKTOP / DESKTOP_SESSION), checked in order.
_DESKTOPS = (
    ("kde", ("kde", "plasma")),
    ("xfce", ("xfce", "xubuntu")),
    ("cinnamon", ("cinnamon",)),
    ("mate", ("mate",)),
    ("budgie", ("budgie",)),
    ("lxqt", ("lxqt", "lubuntu")),
    ("gnome", ("gnome", "ubuntu", "unity", "pop")),
)
_GNOME_LIKE = ("gnome", "budgie", "unknown")
_TRUE = ("1", "true", "yes")


def desktop(env: Mapping[str, str]) -> str:
    """``kde``, ``xfce``, ``cinnamon``, ``mate``, ``budgie``, ``lxqt``, ``gnome`` or ``unknown``."""
    raw = (env.get("XDG_CURRENT_DESKTOP") or env.get("DESKTOP_SESSION") or "").lower()
    joined = " ".join(token for token in re.split(r"[:\s;,]+", raw) if token)
    for name, markers in _DESKTOPS:
        if any(marker in joined for marker in markers):
            return name
    return "unknown"


def _query(runner: Runner, env: Mapping[str, str], argv: list[str]) -> str:
    """A setting printed by a command, without whitespace or GVariant quotes."""
    return display.probe(runner, env, argv).strip("'\"")


def _by_name(name: str) -> str | None:
    """Dark if a theme/scheme name says so, light for any other name, None for no name."""
    if not name:
        return None
    return DARK if DARK in name.lower() else LIGHT


def _read(path: Path) -> str:
    """A small config file's text, or "" if it cannot be read."""
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def _ini_values(content: str) -> dict[str, str]:
    """``key=value`` lines as a dict (keys lowercased, values unquoted)."""
    values = {}
    for line in content.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            values[key.strip().lower()] = value.strip().strip("'\"")
    return values


def _gnome_family(runner: Runner, env: Mapping[str, str], schema: str) -> str | None:
    """``color-scheme`` (GNOME 42+), then the ``gtk-theme`` name, of ``schema``."""
    dconf_dir = "/" + schema.replace(".", "/")
    for argv in (["dconf", "read", f"{dconf_dir}/color-scheme"], ["gsettings", "get", schema, "color-scheme"]):
        value = _query(runner, env, argv)
        if value == "prefer-dark":
            return DARK
        if value == "prefer-light":
            return LIGHT
    for argv in (["dconf", "read", f"{dconf_dir}/gtk-theme"], ["gsettings", "get", schema, "gtk-theme"]):
        result = _by_name(_query(runner, env, argv))
        if result is not None:
            return result
    return None


def _kdeglobals_names_dark(content: str) -> bool:
    """True if kdeglobals names a dark colour scheme or look-and-feel package (in any group)."""
    for line in content.splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip().lower() in ("colorscheme", "lookandfeelpackage") and _by_name(value.strip()) == DARK:
            return True
    return False


def _kde(runner: Runner, env: Mapping[str, str], config_dir: Path) -> str | None:
    """Plasma: kreadconfig6/5 ``ColorScheme`` / ``LookAndFeelPackage``, else ``kdeglobals``."""
    for key, group in (("ColorScheme", "General"), ("LookAndFeelPackage", "KDE")):
        for tool in ("kreadconfig6", "kreadconfig5"):
            argv = [tool, "--file", "kdeglobals", "--group", group, "--key", key]
            result = _by_name(_query(runner, env, argv))
            if result is not None:
                return result
    content = _read(config_dir / "kdeglobals")
    if _kdeglobals_names_dark(content):
        return DARK
    return LIGHT if content else None


def _xfce(runner: Runner, env: Mapping[str, str], _config_dir: Path) -> str | None:
    """XFCE's xsettings theme name."""
    return _by_name(_query(runner, env, ["xfconf-query", "-c", "xsettings", "-p", "/Net/ThemeName"]))


def _mate(runner: Runner, env: Mapping[str, str], _config_dir: Path) -> str | None:
    """MATE's GTK theme name."""
    return _by_name(_query(runner, env, ["gsettings", "get", SCHEMA_MATE, "gtk-theme"]))


def _lxqt(_runner: Runner, _env: Mapping[str, str], config_dir: Path) -> str | None:
    """The ``theme=`` entry of LXQt's configuration."""
    for name in LXQT_FILES:
        for line in _read(config_dir / "lxqt" / name).splitlines():
            key, _sep, value = line.partition("=")
            if key.strip().lower() == "theme":
                result = _by_name(value.strip())
                if result is not None:
                    return result
    return None


def _cinnamon(runner: Runner, env: Mapping[str, str], _config_dir: Path) -> str | None:
    """Cinnamon's own interface schema (GNOME's is the generic fallback)."""
    return _gnome_family(runner, env, SCHEMA_CINNAMON)


def _gnome(runner: Runner, env: Mapping[str, str], _config_dir: Path) -> str | None:
    """GNOME's interface schema (also Budgie and unknown desktops)."""
    return _gnome_family(runner, env, SCHEMA_GNOME)


def _gtk_settings_ini(config_dir: Path) -> str | None:
    """GTK 4/3 ``settings.ini``: the prefer-dark flag, else the theme name."""
    for version in GTK_SETTINGS_DIRS:
        values = _ini_values(_read(config_dir / version / "settings.ini"))
        if values.get("gtk-application-prefer-dark-theme", "").lower() in _TRUE:
            return DARK
        result = _by_name(values.get("gtk-theme-name", ""))
        if result is not None:
            return result
    return None


_PROBES = {
    "gnome": _gnome,
    "budgie": _gnome,
    "unknown": _gnome,
    "cinnamon": _cinnamon,
    "kde": _kde,
    "xfce": _xfce,
    "mate": _mate,
    "lxqt": _lxqt,
}


def _config_dir(env: Mapping[str, str], home: Path) -> Path:
    """``$XDG_CONFIG_HOME`` if absolute, else ``home/.config``."""
    value = env.get("XDG_CONFIG_HOME", "")
    return Path(value) if value and os.path.isabs(value) else home / ".config"


def detect(env: Mapping[str, str], runner: Runner, home: Path) -> str | None:
    """``dark`` or ``light`` as the user's desktop prefers, or None if no probe answers."""
    if DARK in env.get("GTK_THEME", "").lower():
        return DARK
    config_dir = _config_dir(env, home)
    kind = desktop(env)
    result = _PROBES[kind](runner, env, config_dir)
    if result is None and kind not in _GNOME_LIKE:
        result = _gnome_family(runner, env, SCHEMA_GNOME)
    if result is None:
        result = _gtk_settings_ini(config_dir)
    return result
