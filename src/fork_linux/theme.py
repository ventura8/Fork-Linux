"""Is the desktop dark or light? Probes ported from Ubuntu-Hello's ``theme_detect.py``.

The desktop is classified from ``XDG_CURRENT_DESKTOP`` / ``DESKTOP_SESSION``
and asked in its own terms:

* GNOME, Budgie (and unknown desktops): ``color-scheme``, then the
  ``gtk-theme`` name, of ``org.gnome.desktop.interface`` (dconf, then gsettings);
* Cinnamon: the "Dark mode" preference of Cinnamon 6 (``color-scheme`` of
  ``org.x.apps.portal``, what the XApp portal hands applications), then the
  ``gtk-theme`` name of ``org.cinnamon.desktop.interface``, then GNOME's keys;
* KDE Plasma: ``kreadconfig6``/``kreadconfig5``: the window background colour of
  ``kdeglobals`` (grey below 192 is dark, as Plasma's portal says), then the
  ``ColorScheme`` and ``LookAndFeelPackage`` names, else the ``kdeglobals`` file;
* XFCE: ``xfconf-query`` ``xsettings`` ``/Net/ThemeName``;
* MATE: ``org.mate.interface`` ``gtk-theme``;
* LXQt: what colours its applications. Under the ``kvantum`` Qt style (Lubuntu's
  default) that is the Kvantum theme's ``window.color``; under any other style, the
  widget palette's ``window_color`` of ``lxqt.conf``; else the ``theme=`` entry of
  ``lxqt/lxqt.conf`` or ``lxqt/session.conf`` (LXQt's own theme only styles the panel
  and the desktop). LXQt reads ``lxqt.conf`` from ``$XDG_CONFIG_HOME`` over each of
  ``$XDG_CONFIG_DIRS`` key by key (Lubuntu: ``/etc/xdg/xdg-Lubuntu``), and so does
  this probe, so a distribution's defaults count too.

When the desktop's own probe has no answer the GNOME probe is tried, then
GTK's ``settings.ini`` (``gtk-application-prefer-dark-theme`` or a theme name
containing "dark"). A theme name is dark when it contains "dark" or is one of
:data:`DARK_THEMES` (dark themes whose names do not say so, such as MATE's
stock ``BlackMATE``). A ``GTK_THEME`` containing ``dark`` (``Adwaita:dark``)
wins over everything. Unlike the original, nothing runs as another user
(fork-linux never runs as root), and ``None`` means "no idea".
"""

from __future__ import annotations

import os
import re
import string
from collections.abc import Mapping
from pathlib import Path

from . import display
from .display import desktop
from .procrun import Runner

DARK = "dark"
LIGHT = "light"
SCHEMA_GNOME = "org.gnome.desktop.interface"
SCHEMA_CINNAMON = "org.cinnamon.desktop.interface"
# Cinnamon 6's Themes > Settings > Dark mode (and the Light / Dark buttons of Themes).
SCHEMA_XAPP_PORTAL = "org.x.apps.portal"
SCHEMA_MATE = "org.mate.interface"
GTK_SETTINGS_DIRS = ("gtk-4.0", "gtk-3.0")
LXQT_FILES = ("lxqt.conf", "session.conf")
LXQT_CONF = Path("lxqt", "lxqt.conf")
KVANTUM_CONF = Path("Kvantum", "kvantum.kvconfig")
KVANTUM_STYLE = "kvantum"
# Kvantum's built-in theme, used when no kvantum.kvconfig names one (light).
KVANTUM_DEFAULT = "Default"
DEFAULT_CONFIG_DIRS = "/etc/xdg"
DEFAULT_DATA_DIRS = "/usr/local/share:/usr/share"
# QSettings files: keys before the first [section] belong to [General].
INI_GENERAL = "General"
# Perceived brightness (0..255, ITU-R BT.601 weights) below which a colour is dark.
DARK_BELOW = 128
# Plasma's line between dark and light window backgrounds (grey 0-255, xdg-desktop-portal-kde).
KDE_DARK_GRAY = 192
_KDE_RGB = re.compile(r"^(\d{1,3}),(\d{1,3}),(\d{1,3})(?:,\d{1,3})?$", re.ASCII)
# Dark themes without "dark" in their names (lowercased): MATE's stock dark theme and
# the inverse high-contrast theme MATE ships (mate-themes).
DARK_THEMES = frozenset({"blackmate", "highcontrastinverse"})

_GNOME_LIKE = ("gnome", "budgie", "unknown")
_TRUE = ("1", "true", "yes")


def _query(runner: Runner, env: Mapping[str, str], argv: list[str]) -> str:
    """A setting printed by a command, without whitespace or GVariant quotes."""
    return display.probe(runner, env, argv).strip("'\"")


def _by_name(name: str) -> str | None:
    """Dark if a theme/scheme name says so or is a known dark theme, light for any other name, None for no name."""
    if not name:
        return None
    lowered = name.lower()
    return DARK if DARK in lowered or lowered in DARK_THEMES else LIGHT


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


def _ini_section(content: str, section: str) -> dict[str, str]:
    """``key=value`` lines of one ``[section]`` (QSettings: keys before any section are ``[General]``)."""
    values: dict[str, str] = {}
    current = INI_GENERAL
    for line in content.splitlines():
        text = line.strip()
        if text.startswith("[") and text.endswith("]"):
            current = text[1:-1]
            continue
        key, sep, value = text.partition("=")
        if sep and current == section:
            values.setdefault(key.strip().lower(), value.strip().strip("'\""))
    return values


def _search_dirs(env: Mapping[str, str], name: str, default: str) -> list[Path]:
    """The absolute directories of the ``name`` search path (``default`` when it is unset or empty)."""
    return [Path(entry) for entry in (env.get(name) or default).split(":") if os.path.isabs(entry)]


def _setting(dirs: list[Path], relative: Path, section: str, key: str) -> str:
    """The first non-empty ``key`` of ``[section]`` in ``dir/relative`` over ``dirs`` (as QSettings merges them)."""
    for base in dirs:
        value = _ini_section(_read(base / relative), section).get(key, "")
        if value:
            return value
    return ""


def _by_color(color: str) -> str | None:
    """Dark or light for a ``#rrggbb`` colour by its perceived brightness, None for anything else."""
    digits = color.strip().removeprefix("#")
    if len(digits) != 6 or not set(digits) <= set(string.hexdigits):
        return None
    red, green, blue = (int(digits[index : index + 2], 16) for index in (0, 2, 4))
    return DARK if (299 * red + 587 * green + 114 * blue) / 1000 < DARK_BELOW else LIGHT


def _color_scheme(runner: Runner, env: Mapping[str, str], schema: str) -> str | None:
    """The ``color-scheme`` key of ``schema`` (dconf, then gsettings): None for ``default`` or unset."""
    dconf_dir = "/" + schema.replace(".", "/")
    for argv in (["dconf", "read", f"{dconf_dir}/color-scheme"], ["gsettings", "get", schema, "color-scheme"]):
        value = _query(runner, env, argv)
        if value == "prefer-dark":
            return DARK
        if value == "prefer-light":
            return LIGHT
    return None


def _gnome_family(runner: Runner, env: Mapping[str, str], schema: str) -> str | None:
    """``color-scheme`` (GNOME 42+), then the ``gtk-theme`` name, of ``schema``."""
    result = _color_scheme(runner, env, schema)
    if result is not None:
        return result
    dconf_dir = "/" + schema.replace(".", "/")
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


def _by_window_background(value: str) -> str | None:
    """Dark or light from a kdeglobals colour ``R,G,B[,A]`` as Plasma decides it, None for no colour.

    xdg-desktop-portal-kde reports ``prefer-dark`` when the window background's grey (Qt's
    ``qGray``: (11 R + 16 G + 5 B) / 32) is below :data:`KDE_DARK_GRAY`.
    """
    match = _KDE_RGB.match(value)
    if match is None:
        return None
    red, green, blue = (int(part) for part in match.groups())
    return DARK if (red * 11 + green * 16 + blue * 5) // 32 < KDE_DARK_GRAY else LIGHT


def _kreadconfig(runner: Runner, env: Mapping[str, str], group: str, key: str) -> str:
    """``key`` of ``group`` in the kdeglobals cascade (kreadconfig6, then kreadconfig5), or ""."""
    for tool in ("kreadconfig6", "kreadconfig5"):
        value = _query(runner, env, [tool, "--file", "kdeglobals", "--group", group, "--key", key])
        if value:
            return value
    return ""


def _kde(runner: Runner, env: Mapping[str, str], config_dir: Path) -> str | None:
    """Plasma: the colour scheme's window background (kreadconfig6/5), then the ``ColorScheme`` /
    ``LookAndFeelPackage`` names, else the ``kdeglobals`` file.

    The colours come first: a scheme named without "dark" (Nordic, Dracula, a user's own) is
    dark when Plasma says so.
    """
    result = _by_window_background(_kreadconfig(runner, env, "Colors:Window", "BackgroundNormal"))
    for group, key in (("General", "ColorScheme"), ("KDE", "LookAndFeelPackage")):
        if result is None:
            result = _by_name(_kreadconfig(runner, env, group, key))
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


def _kvantum(env: Mapping[str, str], config_dirs: list[Path]) -> str:
    """The Kvantum theme's window colour: the user's copy in ``Kvantum/``, else the installed one."""
    name = _setting(config_dirs, KVANTUM_CONF, INI_GENERAL, "theme") or KVANTUM_DEFAULT
    theme_file = Path("Kvantum", name, f"{name}.kvconfig")
    for base in (config_dirs[0], *_search_dirs(env, "XDG_DATA_DIRS", DEFAULT_DATA_DIRS)):
        result = _by_color(_ini_section(_read(base / theme_file), "GeneralColors").get("window.color", ""))
        if result is not None:
            return result
    return _by_name(name) or LIGHT


def _lxqt_theme_entry(config_dir: Path) -> str | None:
    """The ``theme=`` entry of the user's LXQt configuration (``lxqt.conf``, then ``session.conf``)."""
    for name in LXQT_FILES:
        for line in _read(config_dir / "lxqt" / name).splitlines():
            key, _sep, value = line.partition("=")
            if key.strip().lower() == "theme":
                result = _by_name(value.strip())
                if result is not None:
                    return result
    return None


def _lxqt(_runner: Runner, env: Mapping[str, str], config_dir: Path) -> str | None:
    """What colours LXQt's applications: the Kvantum theme, the widget palette, else LXQt's theme."""
    config_dirs = [config_dir, *_search_dirs(env, "XDG_CONFIG_DIRS", DEFAULT_CONFIG_DIRS)]
    if _setting(config_dirs, LXQT_CONF, "Qt", "style").lower() == KVANTUM_STYLE:
        return _kvantum(env, config_dirs)
    result = _by_color(_setting(config_dirs, LXQT_CONF, "Palette", "window_color"))
    if result is None:
        result = _by_name(_setting(config_dirs, LXQT_CONF, INI_GENERAL, "theme"))
    return result if result is not None else _lxqt_theme_entry(config_dir)


def _cinnamon(runner: Runner, env: Mapping[str, str], _config_dir: Path) -> str | None:
    """Cinnamon's dark-mode preference, then its own interface schema (GNOME's is the generic fallback).

    Cinnamon 6 keeps "Dark mode" in ``org.x.apps.portal``; ``org.cinnamon.desktop.interface``
    has no ``color-scheme`` key, so a light theme with "Prefer dark mode" is dark only there.
    """
    result = _color_scheme(runner, env, SCHEMA_XAPP_PORTAL)
    if result is not None:
        return result
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
