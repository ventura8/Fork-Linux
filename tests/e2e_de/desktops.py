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
MENU_LXQT_PANEL = "lxqt-panel"
MENU_GNOME_SHELL = "gnome-shell"
MENU_BUDGIE = "budgie-appindexer"
MENU_READERS = (MENU_GARCON, MENU_KSYCOCA, MENU_LXQT_PANEL, MENU_GNOME_SHELL, MENU_BUDGIE)

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
    # Variables the session manager (the settings daemon) exports to the applications it starts,
    # taken from its running children into the session's environment after each start.
    session_exports: tuple[str, ...] = ()
    # The scale is read once, at login: each scale case restarts the session.
    scale_restarts_session: bool = False
    # The folder of the desktop's menu that lists our entry (Categories=Development;...).
    menu_folder: tuple[str, ...] = ("Development",)
    # The command (the .desktop file appended) that starts a menu entry as the desktop's menu does.
    menu_launcher: Command = ("gio", "launch")
    # The window manager is a Wayland compositor with its own rootless Xwayland (mutter 50 has no
    # X11 mode): it starts right after the session bus instead of Xvfb, and its command writes
    # DISPLAY, XAUTHORITY and WAYLAND_DISPLAY to $XDG_RUNTIME_DIR/session.env (dekit.SESSION_ENV).
    wayland_compositor: bool = False
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


def _plasma_colors(scheme: str) -> Command:
    """System Settings > Colors & Themes > Colors."""
    return ("plasma-apply-colorscheme", scheme)


# A dark colour scheme whose name does not say so, as one from Get New Colors (Nordic, Dracula,
# ...): Breeze Dark's colours saved as the user's own "Nightfall".
_NIGHTFALL_COLORS = (
    "sh", "-c",
    "mkdir -p {home}/.local/share/color-schemes && sed 's/^Name=Breeze Dark$/Name=Nightfall/' "
    "/usr/share/color-schemes/BreezeDark.colors > {home}/.local/share/color-schemes/Nightfall.colors",
)


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
    # Global Theme (Kubuntu's default, Kubuntu Dark, Breeze Dark) and Colors on their own: an
    # explicit colour scheme wins over the global theme's, also one whose name says nothing.
    themes={
        LIGHT: (_plasma_global_theme("org.kubuntu.desktop"),),
        DARK: (_plasma_global_theme("org.kubuntudark.desktop"),),
        "dark:breeze-dark": (_plasma_global_theme("org.kde.breezedark.desktop"),),
        "dark:colors": (_plasma_colors("BreezeDark"),),
        "light:colors": (_plasma_global_theme("org.kubuntudark.desktop"), _plasma_colors("BreezeLight")),
        "dark:custom-colors": (_NIGHTFALL_COLORS, _plasma_colors("Nightfall")),
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



# One INI file of LXQt (or Kvantum) changed as its settings dialogs save it through QSettings:
# FILE, then SECTION KEY VALUE triples; an empty VALUE removes the key (back to the default
# LXQt merges in from XDG_CONFIG_DIRS, here Lubuntu's /etc/xdg/xdg-Lubuntu).
_LXQT_INI = """
import configparser, sys
from pathlib import Path
path, args = Path(sys.argv[1]), sys.argv[2:]
ini = configparser.RawConfigParser(strict=False, delimiters=("=",))
ini.optionxform = str
ini.read(path, encoding="utf-8")
for section, key, value in zip(args[0::3], args[1::3], args[2::3]):
    if value:
        if not ini.has_section(section):
            ini.add_section(section)
        ini.set(section, key, value)
    elif ini.has_section(section):
        ini.remove_option(section, key)
path.parent.mkdir(parents=True, exist_ok=True)
with path.open("w", encoding="utf-8") as out:
    ini.write(out, space_around_delimiters=False)
"""
_LXQT_CONF = "{home}/.config/lxqt/lxqt.conf"
_LXQT_SESSION_CONF = "{home}/.config/lxqt/session.conf"
_KVANTUM_CONF = "{home}/.config/Kvantum/kvantum.kvconfig"
# LXQt Appearance > Widget Style > Qt Palette > Load Palette: "Dark" (/usr/share/lxqt/palettes/Dark).
_DARK_PALETTE = {
    "base_color": "#282828",
    "highlight_color": "#640b0c",
    "highlighted_text_color": "#ebfdff",
    "link_color": "#8c9bff",
    "link_visited_color": "#ffb3f7",
    "text_color": "#b8b8b8",
    "window_color": "#232323",
    "window_text_color": "#e1e6e6",
    "tooltip_base_color": "#232323",
    "tooltip_text_color": "#b8b8b8",
}


def _lxqt_ini(path: str, *settings: tuple[str, str, str]) -> Command:
    return ("python3", "-I", "-c", _LXQT_INI, path, *(word for setting in settings for word in setting))


def _kvantum_theme(name: str) -> Command:
    """Kvantum Manager > Change/Delete Theme > Use this theme (Lubuntu's Qt style is kvantum)."""
    return _lxqt_ini(_KVANTUM_CONF, ("General", "theme", name))


def _lxqt_theme(name: str) -> Command:
    """LXQt Appearance > LXQt Theme (the panel, the desktop and LXQt's own dialogs)."""
    return _lxqt_ini(_LXQT_CONF, ("General", "theme", name))


def _fusion_with_palette(palette: Mapping[str, str]) -> Command:
    """LXQt Appearance > Widget Style: Qt style Fusion, then Qt Palette > Load Palette."""
    colors = (("Palette", key, value) for key, value in palette.items())
    return _lxqt_ini(_LXQT_CONF, ("Qt", "style", "Fusion"), *colors)


def _global_scaling(factor: str) -> Command:
    """LXQt Session Settings > Basic Settings > Global Screen Scaling, applied at the next login."""
    return _lxqt_ini(
        _LXQT_SESSION_CONF, ("Environment", "GDK_SCALE", factor), ("Environment", "QT_SCALE_FACTOR", factor)
    )


# Every setting the cases change, removed: Lubuntu's defaults (/etc/xdg/xdg-Lubuntu) apply again.
_LXQT_DEFAULTS = (
    _lxqt_ini(_LXQT_CONF, ("General", "theme", ""), ("Qt", "style", ""), *(("Palette", k, "") for k in _DARK_PALETTE)),
    _lxqt_ini(_KVANTUM_CONF, ("General", "theme", "")),
    _lxqt_ini(_LXQT_SESSION_CONF, ("Environment", "GDK_SCALE", ""), ("Environment", "QT_SCALE_FACTOR", "")),
)

LXQT = Desktop(
    key="lxqt",
    title="Lubuntu 26.04 (LXQt 2.3: lxqt-session, openbox, pcmanfm-qt, qterminal, Kvantum)",
    # What sddm + /etc/X11/Xsession.d (60x11-common_xdg_path) and startlxqt export for the
    # "Lubuntu" session (startlxqt appends /etc and /usr/share to the XDG directories).
    session_env={
        "XDG_CURRENT_DESKTOP": "LXQt",
        "DESKTOP_SESSION": "Lubuntu",
        "XDG_SESSION_DESKTOP": "Lubuntu",
        "XDG_SESSION_TYPE": "x11",
        "XDG_MENU_PREFIX": "lxqt-",
        "XDG_CONFIG_DIRS": "/etc/xdg/xdg-Lubuntu:/etc/xdg:/etc:/usr/share",
        "XDG_DATA_DIRS": "/usr/share/Lubuntu:/usr/local/share/:/usr/share/:/usr/share",
        "QT_QPA_PLATFORMTHEME": "lxqt",
        "QT_PLATFORM_PLUGIN": "lxqt",
    },
    # startlxqt runs lxqt-session, which starts what session.conf and the autostart entries name:
    # openbox (Lubuntu's window_manager), pcmanfm-qt --desktop (its FileManager1 service), the
    # panel, the global shortcuts and the notification daemon.
    settings_daemon=("startlxqt",),
    settings_ready=("gdbus", "wait", "--session", "--timeout", "2", "org.lxqt.session"),
    window_manager=(),
    wm_name="Openbox",
    file_manager="pcmanfm-qt",
    file_manager_service=(),
    file_manager_class="pcmanfm-qt",
    provides_file_manager1=True,
    integration=PCMANFM_QT_FMA,
    # The panel's application menu (Lubuntu: fancymenu on lxqt-applications-compact.menu) files
    # Development under "Programming".
    menu_reader=MENU_LXQT_PANEL,
    menu_folder=("Programming",),
    terminal="qterminal",
    terminal_class="qterminal",
    # Lubuntu's Qt style is kvantum: Kvantum's theme colours the applications, LXQt's own theme
    # only the panel and the desktop, and LXQt's widget palette counts only for other Qt styles.
    themes={
        # Lubuntu as installed: Kvantum's "Lubuntu" theme, the "Lubuntu Arc" LXQt theme.
        LIGHT: _LXQT_DEFAULTS,
        "light:chosen-in-the-dialogs": (_kvantum_theme("Lubuntu"), _lxqt_theme("Lubuntu Arc")),
        DARK: (_kvantum_theme("KvArcDark"), _lxqt_theme("dark")),
        "dark:kvantum-dark-theme": (_kvantum_theme("KvArcDark"),),
        "dark:fusion-dark-palette": (_fusion_with_palette(_DARK_PALETTE),),
        "light:dark-panel-theme": (_lxqt_theme("dark"),),
    },
    scales=(
        ScaleCase("default", 100),
        ScaleCase("global-scaling-1.5", 150, (_global_scaling("1.5"),)),
        ScaleCase("global-scaling-2", 200, (_global_scaling("2"),)),
    ),
    reset=_LXQT_DEFAULTS,
    # lxqt-session exports session.conf's [Environment] (Global Screen Scaling) and marks the session.
    session_exports=("LXQT_SESSION_CONFIG", "QT_SCALE_FACTOR", "GDK_SCALE"),
    scale_restarts_session=True,
    browser=BROWSER_XDG_MIME,
    # pcmanfm-qt's folder context menu ends with our action and Folder Properties.
    open_in_fork_keys=("Menu", "Up", "Up", "Return"),
    packages=(
        "lxqt-session", "lxqt-config", "lxqt-panel", "lxqt-qtplugin", "lxqt-globalkeys", "lxqt-notificationd",
        "openbox", "pcmanfm-qt", "qterminal", "qtxdg-tools", "qt-style-kvantum", "lubuntu-default-settings",
        "gir1.2-gmenu-3.0",
    ),
)


def _gnome_interface(key: str, value: str) -> Command:
    """``gsettings set`` of one ``org.gnome.desktop.interface`` key, as GNOME's Settings writes it."""
    return ("gsettings", "set", "org.gnome.desktop.interface", key, value)


# Settings > Displays > Scale, then "Keep Changes": mutter's DisplayConfig applies the scale to its
# monitor persistently (~/.config/monitors.xml); gsd-xsettings then republishes Xft.dpi.
_MUTTER_SCALE = """
import sys, gi
gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib
NAME = PATH = "org.gnome.Mutter.DisplayConfig"
bus = Gio.bus_get_sync(Gio.BusType.SESSION)
def call(method, args=None):
    reply = bus.call_sync(NAME, "/" + PATH.replace(".", "/"), NAME, method, args, None, 0, -1, None)
    return reply.unpack()
serial, monitors, _logical, _properties = call("GetCurrentState")
connector, modes = monitors[0][0][0], monitors[0][1]
mode = next(item[0] for item in modes if item[6].get("is-current"))
config = [(0, 0, float(sys.argv[1]), 0, True, [(connector, mode, {})])]
call("ApplyMonitorsConfig", GLib.Variant("(uua(iiduba(ssa{sv}))a{sv})", (serial, 2, config, {})))
"""


def _mutter_scale(scale: str) -> Command:
    return ("python3", "-I", "-c", _MUTTER_SCALE, scale)


# What gnome-shell hands gnome-session once its Xwayland is up (the session's DISPLAY,
# XAUTHORITY, WAYLAND_DISPLAY), and the D-Bus activation environment gnome-session updates.
_MUTTER_HAND_OVER = (
    'env | grep -E "^(DISPLAY|XAUTHORITY|WAYLAND_DISPLAY)=" > "$XDG_RUNTIME_DIR/session.env.new"'
    ' && dbus-update-activation-environment DISPLAY XAUTHORITY WAYLAND_DISPLAY'
    ' && mv "$XDG_RUNTIME_DIR/session.env.new" "$XDG_RUNTIME_DIR/session.env" && exec sleep infinity'
)

GNOME = Desktop(
    key="gnome",
    title="Ubuntu 26.04 (GNOME 50: mutter + Xwayland, gsd-xsettings, Nautilus 50, Ptyxis, Yaru)",
    # What gdm + gnome-session export for the "Ubuntu" session (Wayland only since GNOME 49);
    # ubuntu-settings' gschema overrides (Yaru) apply to XDG_CURRENT_DESKTOP "ubuntu".
    # GDK_BACKEND=x11 is the tier's: GTK apps then open on Xwayland, where xdotool sees them.
    session_env={
        "XDG_CURRENT_DESKTOP": "ubuntu:GNOME",
        "DESKTOP_SESSION": "ubuntu",
        "XDG_SESSION_DESKTOP": "ubuntu",
        "XDG_SESSION_TYPE": "wayland",
        "XDG_MENU_PREFIX": "gnome-",
        "XDG_CONFIG_DIRS": "/etc/xdg/xdg-ubuntu:/etc/xdg",
        "XDG_DATA_DIRS": "/usr/share/ubuntu:/usr/local/share/:/usr/share/:/var/lib/snapd/desktop",
        "GNOME_SHELL_SESSION_MODE": "ubuntu",
        # gnome-session sets it for compatibility; xdg-open recognises GNOME by it (or by
        # gnome-session's org.gnome.SessionManager), as "ubuntu:GNOME" is not "GNOME*".
        "GNOME_DESKTOP_SESSION_ID": "this-is-deprecated",
        "GDK_BACKEND": "x11",
    },
    settings_daemon=("/usr/libexec/gsd-xsettings",),
    window_manager=(
        "mutter", "--wayland", "--headless", "--virtual-monitor", "1920x1080", "--", "sh", "-c", _MUTTER_HAND_OVER,
    ),
    wm_name="Mutter",
    wayland_compositor=True,
    # Nautilus's D-Bus service owns org.freedesktop.FileManager1 (and quits when idle).
    file_manager="nautilus",
    file_manager_service=("nautilus", "--gapplication-service"),
    file_manager_class="org.gnome.Nautilus",
    provides_file_manager1=True,
    integration=NAUTILUS_EXTENSION,
    # gnome-shell's app grid: every app that should be shown, in a folder only when
    # org.gnome.desktop.app-folders puts it in one (ours is on the top level).
    menu_reader=MENU_GNOME_SHELL,
    menu_folder=(),
    # Ubuntu 26.04's terminal, which xdg-terminal-exec picks.
    terminal="ptyxis",
    terminal_class="ptyxis",
    # Settings > Appearance > Style: Ubuntu's panel sets the color scheme and the Yaru variant.
    themes={
        LIGHT: (_gnome_interface("color-scheme", "default"), _gnome_interface("gtk-theme", "Yaru")),
        DARK: (_gnome_interface("color-scheme", "prefer-dark"), _gnome_interface("gtk-theme", "Yaru-dark")),
    },
    # Settings > Displays > Scale; Settings > Accessibility > Large Text (text scaling 1.25).
    scales=(
        ScaleCase("default", 100),
        ScaleCase("display-200", 200, (_mutter_scale("2"),)),
        ScaleCase("large-text-125", 125, (_gnome_interface("text-scaling-factor", "1.25"),)),
        ScaleCase(
            "display-200-large-text-250", 250, (_mutter_scale("2"), _gnome_interface("text-scaling-factor", "1.25"))
        ),
    ),
    reset=(
        _gnome_interface("color-scheme", "default"),
        _gnome_interface("gtk-theme", "Yaru"),
        _gnome_interface("text-scaling-factor", "1.0"),
        _mutter_scale("1"),
    ),
    browser=BROWSER_XDG_MIME,
    # Nautilus 50 opens the folder's context menu with the Menu key (not Shift+F10); "Open in Fork"
    # is the second item from the bottom, above Properties.
    open_in_fork_keys=("Menu", "Up", "Up", "Return"),
    # mutter reparents only decorated X windows: Fork's own title bar leaves it unframed.
    frames_undecorated=False,
    settings_ready=("sh", "-c", "xrdb -query | grep -q '^Xft.dpi:'"),
    packages=(
        "mutter", "xwayland", "gnome-settings-daemon", "ubuntu-settings", "yaru-theme-gtk", "nautilus",
        "python3-nautilus", "ptyxis", "xdg-terminal-exec", "xdg-desktop-portal-gnome",
    ),
)

# Budgie Desktop Settings > Style: the "Dark theme" switch (com.solus-project.budgie-panel
# dark-theme), which budgie-daemon turns into org.gnome.desktop.interface color-scheme, and
# Widgets (gtk-theme). The command returns once budgie-daemon has published the color scheme.
_BUDGIE_STYLE = """
import sys, time, gi
gi.require_version("Gio", "2.0")
from gi.repository import Gio
panel = Gio.Settings.new("com.solus-project.budgie-panel")
interface = Gio.Settings.new("org.gnome.desktop.interface")
dark, widgets = sys.argv[1], sys.argv[2]
for settings, key, value in ((panel, "dark-theme", dark), (interface, "gtk-theme", widgets)):
    if value == "default":
        settings.reset(key)
    elif key == "dark-theme":
        settings.set_boolean(key, value == "true")
    elif value:
        settings.set_string(key, value)
Gio.Settings.sync()
wanted = "prefer-dark" if panel.get_boolean("dark-theme") else "prefer-light"
deadline = time.monotonic() + 10
while interface.get_string("color-scheme") != wanted:
    if time.monotonic() > deadline:
        sys.exit(f"budgie-daemon did not set color-scheme to {wanted}")
    time.sleep(0.1)
"""


def _budgie_style(dark: str, widgets: str) -> Command:
    """Dark theme switch ("true", "false", "default") and Widgets theme ("" leaves it, "default")."""
    return ("python3", "-I", "-c", _BUDGIE_STYLE, dark, widgets)


def _budgie_interface(key: str, value: str) -> Command:
    """Budgie Desktop Settings > Fonts > Text scaling; Budgie Control Center > Accessibility > Large Text."""
    return ("gsettings", "set", "org.gnome.desktop.interface", key, value)


def _labwc_output_scale(scale: str) -> Command:
    """Budgie Control Center > Displays > Scale, as wdisplays / wlr-randr apply it to labwc's output."""
    return ("wlr-randr", "--output", "HEADLESS-1", "--scale", scale)


# startbudgielabwc: Budgie's own copy of labwc's configuration, then labwc. The tier runs it on
# the headless backend with one 1920x1080 output (pixman: no GPU in the container); its startup
# command hands the session its Xwayland (dekit.SESSION_ENV) as budgie-session's would see it.
_LABWC = """
config="$XDG_CONFIG_HOME/budgie-desktop/labwc"
mkdir -p "$config"
for file in rc.xml menu.xml; do
    [ -f "$config/$file" ] || cp /usr/share/budgie-desktop/labwc/"$file" "$config/$file"
done
# labwc splits its startup command into words itself (no shell): the hand-over is a script.
printf '%s\n' "$1" > "$config/fl-e2e-hand-over.sh"
WLR_BACKENDS=headless WLR_RENDERER=pixman WLR_LIBINPUT_NO_DEVICES=1 \
    exec labwc --config-dir "$config" -s "sh $config/fl-e2e-hand-over.sh"
"""
# Xwayland drops the first XTEST event it gets (xdotool's first key would be lost): one Shift
# press warms it up before the session uses it.
_LABWC_HAND_OVER = (
    "wlr-randr --output HEADLESS-1 --custom-mode 1920x1080 && xdotool key shift"
    ' && env | grep -E "^(DISPLAY|WAYLAND_DISPLAY)=" > "$XDG_RUNTIME_DIR/session.env.new"'
    " && dbus-update-activation-environment DISPLAY WAYLAND_DISPLAY"
    ' && mv "$XDG_RUNTIME_DIR/session.env.new" "$XDG_RUNTIME_DIR/session.env"'
)

BUDGIE = Desktop(
    key="budgie",
    title="Ubuntu Budgie 26.04 (Budgie 10.10: labwc + Xwayland, budgie-daemon, Nemo, xfce4-terminal, Pocillo)",
    # What sddm + startbudgielabwc export for the "Budgie Desktop" session (Wayland only since
    # 10.10; DesktopNames=Budgie, so budgie-desktop-environment's ":Budgie" gschema overrides
    # apply). GDK_BACKEND=x11 is the tier's (Budgie sets wayland,x11): GTK apps then open on
    # Xwayland, where xdotool sees them. fork-linux never reads GDK_BACKEND.
    session_env={
        "XDG_CURRENT_DESKTOP": "Budgie",
        "DESKTOP_SESSION": "budgie-desktop",
        "XDG_SESSION_DESKTOP": "budgie-desktop",
        "XDG_SESSION_TYPE": "wayland",
        "XDG_DATA_DIRS": "/usr/share/budgie-desktop:/usr/local/share/:/usr/share/",
        "QT_QPA_PLATFORMTHEME": "gtk3",
        "CLUTTER_BACKEND": "gdk",
        "GNOME_DESKTOP_SESSION_ID": "this-is-deprecated",
        "MOZ_ENABLE_WAYLAND": "1",
        "GDK_BACKEND": "x11",
        # Budgie's session runs with accessibility on (the tier reads Nemo's menu over AT-SPI).
        "NO_AT_BRIDGE": "0",
    },
    # budgie-session's BudgieDaemon component: notifications, OSD, screenshots, and the style
    # switch (dark-theme -> color-scheme). Budgie 10.10 runs no XSETTINGS daemon: Xwayland
    # programs get no Xft.dpi.
    settings_daemon=("env", "GDK_BACKEND=wayland", "budgie-daemon"),
    settings_ready=("gdbus", "wait", "--session", "--timeout", "2", "org.budgie_desktop.Notifications"),
    window_manager=("sh", "-c", _LABWC, "sh", _LABWC_HAND_OVER),
    wm_name="wlroots wm",
    wayland_compositor=True,
    # Nemo's D-Bus service (nemo.FileManager1.service) owns org.freedesktop.FileManager1.
    file_manager="nemo",
    file_manager_service=("nemo", "--no-default-window"),
    file_manager_class="Nemo",
    provides_file_manager1=True,
    integration=NEMO_ACTION,
    # The panel's menus (Budgie Menu, Ubuntu Budgie's AppMenu) group Gio's desktop entries by
    # their main category, through libbudgie-appindexer.
    menu_reader=MENU_BUDGIE,
    menu_folder=("Programming",),
    # Ubuntu Budgie's terminal (org.gnome.desktop.default-applications.terminal:Budgie).
    terminal="xfce4-terminal",
    terminal_class="Xfce4-terminal",
    themes={
        LIGHT: (_budgie_style("false", "Pocillo"),),
        # Ubuntu Budgie's defaults: Pocillo-dark with the Dark theme switch on (prefer-dark).
        DARK: (_budgie_style("default", "default"),),
    },
    scales=(
        ScaleCase("default", 100),
        # labwc scales Xwayland's programs up itself (their screen becomes 960x540): fork-linux
        # must not scale Fork again.
        ScaleCase("display-scale-2-xwayland-upscaled", 100, (_labwc_output_scale("2"),)),
        ScaleCase("large-text-125", 125, (_budgie_interface("text-scaling-factor", "1.25"),)),
        ScaleCase("text-scaling-2", 200, (_budgie_interface("text-scaling-factor", "2.0"),)),
    ),
    reset=(
        _budgie_style("default", "default"),
        ("gsettings", "reset", "org.gnome.desktop.interface", "text-scaling-factor"),
        _labwc_output_scale("1"),
    ),
    browser=BROWSER_XDG_MIME,
    # Nemo's context menu of the folder it shows (no item selected): Create New Folder, Create New
    # Document, then the actions, Budgie's (Change Desktop Background, Desktop Settings) and ours
    # in an order that varies with the order Nemo loaded their folders: found by label.
    open_in_fork_keys=("Menu", "menu-item:Open in Fork", "Return"),
    # labwc (wlroots' XWM) never reparents X windows: it draws server-side decorations itself.
    frames_undecorated=False,
    packages=(
        "labwc", "xwayland", "wlr-randr", "budgie-core", "budgie-desktop", "budgie-desktop-environment",
        "ubuntu-budgie-themes", "nemo", "xfce4-terminal", "dconf-gsettings-backend", "at-spi2-core",
        "gir1.2-atspi-2.0",
    ),
)


DESKTOPS: dict[str, Desktop] = {desktop.key: desktop for desktop in (XFCE, KDE, LXQT, GNOME, BUDGIE)}
