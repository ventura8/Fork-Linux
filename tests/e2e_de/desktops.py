"""Per-desktop recipes of the desktop E2E tier (``tests/e2e_de``): how each desktop starts and is set.

One :class:`Desktop` per ``docker/Dockerfile.e2e.de.<key>`` image (``tests/test_e2e_de_recipes.py``
keeps the two in step). A recipe names only the desktop's own programs: its settings daemon, its
window manager, its file manager (and whether that provides ``org.freedesktop.FileManager1``), its
terminal, the session variables its display manager would set, and the commands its own settings
dialogs run to switch the theme and the scale. ``dekit.Session`` starts the session from it; the
tests in ``test_de_integration.py`` and ``test_de_fork.py`` read nothing desktop-specific from
anywhere else.

Commands are argv tuples run in the session's environment; ``{home}`` is replaced by the
session's home directory. Pure data: importing this module starts nothing.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

Command = tuple[str, ...]

# How a file manager loads our "Open in Fork" action (one checker per kind in test_de_integration.py).
THUNAR_UCA = "thunar-uca"
NAUTILUS_EXTENSION = "nautilus-python"
DOLPHIN_SERVICEMENU = "dolphin-servicemenu"
NEMO_ACTION = "nemo-action"
CAJA_FMA = "caja-fma"
PCMANFM_QT_FMA = "pcmanfm-qt-fma"
INTEGRATION_KINDS = (THUNAR_UCA, NAUTILUS_EXTENSION, DOLPHIN_SERVICEMENU, NEMO_ACTION, CAJA_FMA, PCMANFM_QT_FMA)

# How the desktop's menu is read back (one reader per kind in test_de_integration.py).
MENU_GARCON = "garcon"
MENU_READERS = (MENU_GARCON,)

# How the desktop picks the web browser that xdg-open hands http(s) links to.
BROWSER_XFCE_HELPERS = "xfce-helpers"
BROWSER_XDG_MIME = "xdg-mime"
BROWSER_KINDS = (BROWSER_XFCE_HELPERS, BROWSER_XDG_MIME)

LIGHT = "light"
DARK = "dark"


@dataclass(frozen=True)
class ScaleCase:
    """One scale setting: the commands that set it and the percentage fork-linux must read."""

    id: str
    expected: int
    commands: tuple[Command, ...] = ()


@dataclass(frozen=True)
class Desktop:
    """How one desktop environment runs inside its E2E image."""

    key: str
    title: str
    # Variables the desktop's display manager / session script sets.
    session_env: Mapping[str, str]
    settings_daemon: Command
    window_manager: Command
    # _NET_WM_NAME of the window manager's _NET_SUPPORTING_WM_CHECK window.
    wm_name: str
    # The file manager's program (opens a window on a folder) and the command that starts its
    # D-Bus service; the session waits for FileManager1 when provides_file_manager1.
    file_manager: str
    file_manager_service: Command
    file_manager_class: str
    provides_file_manager1: bool
    integration: str
    menu_reader: str
    # The terminal fork-linux-host must pick on this desktop, and its window class.
    terminal: str
    terminal_class: str
    themes: Mapping[str, tuple[Command, ...]]
    scales: tuple[ScaleCase, ...]
    # Commands that bring theme and scale back to the desktop's defaults before each case.
    reset: tuple[Command, ...]
    browser: str
    # Keys that run "Open in Fork" from the file manager's context menu of the folder it shows.
    open_in_fork_keys: tuple[str, ...]
    # The window manager reparents (frames) Fork's undecorated main window.
    frames_undecorated: bool = True
    # A command whose success means the settings daemon has published its settings.
    settings_ready: Command = ("true",)
    packages: tuple[str, ...] = field(default=())


def _xsettings(prop: str, kind: str, value: str) -> Command:
    """``xfconf-query`` setting (creating if needed) one xsettings property, as XFCE's dialogs do."""
    return ("xfconf-query", "-c", "xsettings", "-p", prop, "-n", "-t", kind, "-s", value)


XFCE = Desktop(
    key="xfce",
    title="Xubuntu 26.04 (XFCE 4.20: xfwm4, xfsettingsd, Thunar, xfce4-terminal)",
    # What lightdm + /etc/X11/Xsession.d (55xfce4-session, 56xubuntu-session, 60x11-common_xdg_path)
    # and startxfce4 export for the "Xubuntu Session".
    session_env={
        "XDG_CURRENT_DESKTOP": "XFCE",
        "DESKTOP_SESSION": "xubuntu",
        "XDG_SESSION_DESKTOP": "xubuntu",
        "XDG_SESSION_TYPE": "x11",
        "XDG_MENU_PREFIX": "xfce-",
        "XDG_CONFIG_DIRS": "/etc/xdg/xdg-xubuntu:/etc/xdg",
        "XDG_DATA_DIRS": "/usr/share/xubuntu:/usr/share/xfce4:/usr/local/share/:/usr/share/",
        "GTK_OVERLAY_SCROLLING": "0",
        "QT_QPA_PLATFORMTHEME": "gtk2",
        "CLUTTER_BACKEND": "x11",
    },
    settings_daemon=("xfsettingsd", "--no-daemon", "--replace"),
    window_manager=("xfwm4", "--replace"),
    wm_name="Xfwm4",
    # xfce4-session starts "Thunar --daemon", which owns org.freedesktop.FileManager1.
    file_manager="thunar",
    file_manager_service=("thunar", "--daemon"),
    file_manager_class="Thunar",
    provides_file_manager1=True,
    integration=THUNAR_UCA,
    menu_reader=MENU_GARCON,
    terminal="xfce4-terminal",
    terminal_class="Xfce4-terminal",
    # Settings > Appearance > Style (Xubuntu ships Greybird and Greybird-dark).
    themes={
        LIGHT: (_xsettings("/Net/ThemeName", "string", "Greybird"),),
        DARK: (_xsettings("/Net/ThemeName", "string", "Greybird-dark"),),
    },
    # Settings > Appearance > Settings: Window Scaling, Custom DPI.
    scales=(
        ScaleCase("default", 100),
        ScaleCase("window-scaling-2x", 200, (_xsettings("/Gdk/WindowScalingFactor", "int", "2"),)),
        ScaleCase("custom-dpi-192", 200, (_xsettings("/Xft/DPI", "int", "192"),)),
    ),
    reset=(
        _xsettings("/Net/ThemeName", "string", "Greybird"),
        _xsettings("/Gdk/WindowScalingFactor", "int", "1"),
        _xsettings("/Xft/DPI", "int", "96"),
    ),
    browser=BROWSER_XFCE_HELPERS,
    # Thunar's folder context menu: Create Folder, Create Document, (Paste, disabled),
    # Open Terminal Here, Find in this folder, Open in Fork.
    open_in_fork_keys=("Menu", "Down", "Down", "Down", "Down", "Down", "Return"),
    settings_ready=("sh", "-c", "xrdb -query | grep -q '^Xft.dpi:'"),
    packages=("xfwm4", "xfce4-settings", "xfconf", "thunar", "xfce4-terminal", "exo-utils", "xubuntu-default-settings"),
)

DESKTOPS: dict[str, Desktop] = {desktop.key: desktop for desktop in (XFCE,)}
