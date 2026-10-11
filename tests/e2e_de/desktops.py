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
MENU_KSYCOCA = "kbuildsycoca"
MENU_READERS = (MENU_GARCON, MENU_KSYCOCA)

# How the desktop picks the web browser that xdg-open hands http(s) links to.
BROWSER_XFCE_HELPERS = "xfce-helpers"
BROWSER_XDG_MIME = "xdg-mime"
# KDE: System Settings > Default Applications also names the browser in kdeglobals, which KIO asks first.
BROWSER_KDE = "kde-browser-application"
BROWSER_KINDS = (BROWSER_XFCE_HELPERS, BROWSER_XDG_MIME, BROWSER_KDE)

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
    # The command (the .desktop file appended) that starts a menu entry as the desktop's menu does.
    menu_launcher: Command = ("gio", "launch")
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

def _kconfig(file: str, group: str, key: str, value: str | None) -> Command:
    """``kwriteconfig6`` of one key (``None`` deletes it), as KDE's settings modules write it."""
    action = ("--delete",) if value is None else (value,)
    return ("kwriteconfig6", "--file", file, "--group", group, "--key", key, *action)


def _plasma_global_theme(package: str) -> Command:
    """System Settings > Colors & Themes > Global Theme (writes ~/.config/kdedefaults)."""
    return ("plasma-apply-lookandfeel", "--apply", package)


# What System Settings' Text & Fonts and Display Configuration modules do on X11 after writing
# kcmfonts' forceFontDPI (and what kcminit does at the next login): publish it as Xft.dpi.
_KDE_FONTS_INIT = ("kcminit", "kcm_fonts_init")


def _kde_global_scale(factor: str, dpi: str) -> tuple[Command, ...]:
    """Display Configuration > Global scale, as kcm_kscreen applies it on X11."""
    return (
        _kconfig("kdeglobals", "KScreen", "ScaleFactor", factor),
        _kconfig("kdeglobals", "KScreen", "ScreenScaleFactors", f"screen={factor};"),
        _kconfig("kcmfonts", "General", "forceFontDPI", dpi),
        _KDE_FONTS_INIT,
    )


KDE = Desktop(
    key="kde",
    title="Kubuntu 26.04 (KDE Plasma 6.6 on X11: kwin_x11, kded6, Dolphin 25.12, Konsole, Breeze)",
    # What sddm + startplasma-x11 export for the "Plasma (X11)" session, with Kubuntu's defaults
    # (kubuntu-settings-desktop: /etc/xdg/plasma-workspace/env/env.sh adds its kf5-settings, where
    # LookAndFeelPackage=org.kubuntu.desktop); startplasma puts ~/.config/kdedefaults first, where
    # a Global Theme writes its colour scheme.
    session_env={
        "XDG_CURRENT_DESKTOP": "KDE",
        "DESKTOP_SESSION": "plasmax11",
        "XDG_SESSION_DESKTOP": "KDE",
        "XDG_SESSION_TYPE": "x11",
        "XDG_MENU_PREFIX": "plasma-",
        "XDG_CONFIG_DIRS": "{home}/.config/kdedefaults:/etc/xdg:/usr/share/kubuntu-default-settings/kf5-settings",
        "XDG_DATA_DIRS": "/usr/local/share/:/usr/share/:/var/lib/snapd/desktop",
        "KDE_FULL_SESSION": "true",
        "KDE_SESSION_VERSION": "6",
        "KDE_APPLICATIONS_AS_SCOPE": "1",
    },
    # plasma_session runs kcminit's startup modules (Xft.* and the widget style), then kded6.
    settings_daemon=("sh", "-c", "kcminit kcm_fonts_init kcm_style_init && exec kded6"),
    window_manager=("kwin_x11", "--replace"),
    wm_name="KWin",
    # D-Bus activation of org.freedesktop.FileManager1 runs "dolphin --daemon".
    file_manager="dolphin",
    file_manager_service=("dolphin", "--daemon"),
    file_manager_class="dolphin",
    provides_file_manager1=True,
    integration=DOLPHIN_SERVICEMENU,
    menu_reader=MENU_KSYCOCA,
    terminal="konsole",
    terminal_class="konsole",
    # Global Theme: Kubuntu's default and Kubuntu Dark.
    themes={
        LIGHT: (_plasma_global_theme("org.kubuntu.desktop"),),
        DARK: (_plasma_global_theme("org.kubuntudark.desktop"),),
    },
    # Display Configuration > Global scale; Text & Fonts > Force font DPI.
    scales=(
        ScaleCase("default", 100),
        ScaleCase("global-scale-200", 200, _kde_global_scale("2", "192")),
        ScaleCase("global-scale-150", 150, _kde_global_scale("1.5", "144")),
        ScaleCase("force-font-dpi-192", 200, (_kconfig("kcmfonts", "General", "forceFontDPI", "192"), _KDE_FONTS_INIT)),
    ),
    reset=(
        _plasma_global_theme("org.kubuntu.desktop"),
        _kconfig("kdeglobals", "General", "ColorScheme", None),
        _kconfig("kdeglobals", "KScreen", "ScaleFactor", None),
        _kconfig("kdeglobals", "KScreen", "ScreenScaleFactors", None),
        _kconfig("kcmfonts", "General", "forceFontDPI", None),
        _KDE_FONTS_INIT,
    ),
    browser=BROWSER_KDE,
    menu_launcher=("kioclient", "exec"),
    # Dolphin's context menu of the folder it shows: Create New, Open Folder With..., Add to Places,
    # Sort By, Change View Mode, Open Terminal Here, Open in Fork (our TopLevel service menu).
    open_in_fork_keys=("Menu", *("Down",) * 7, "Return"),
    # kcminit has published Xft.dpi and kded6 serves its modules on the bus.
    settings_ready=(
        "sh", "-c",
        "xrdb -query | grep -q '^Xft.dpi:' && gdbus introspect --session --dest org.kde.kded6 --object-path /kded",
    ),
    packages=(
        "kwin-x11", "kded6", "plasma-workspace", "plasma-session-x11", "dolphin", "konsole", "kde-cli-tools",
        "libkf6config-bin", "libkf6service-bin", "breeze", "kubuntu-settings-desktop", "xdg-desktop-portal-kde",
    ),
)


DESKTOPS: dict[str, Desktop] = {desktop.key: desktop for desktop in (XFCE, KDE)}
