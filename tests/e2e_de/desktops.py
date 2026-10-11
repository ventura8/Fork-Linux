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
MENU_MATE_PANEL = "mate-panel"
MENU_CINNAMON = "cinnamon-cmenu"
MENU_READERS = (
    MENU_GARCON, MENU_KSYCOCA, MENU_LXQT_PANEL, MENU_GNOME_SHELL, MENU_BUDGIE, MENU_MATE_PANEL, MENU_CINNAMON,
)

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
    # The X server the session runs on, its display appended (empty: Xvfb at dekit.SCREEN, 96 DPI).
    # Xvfb's one RandR mode has no refresh rate, so a window manager that applies a scale through
    # its own monitor configuration (Cinnamon's muffin) needs a server with real modes.
    x_server: Command = ()
    # The desktop's own programs that must run once the session is up (executable or argv[0]
    # names): its shell or window manager, its settings daemon, a file-manager service that
    # stays up. dekit.Session checks them; logs/session-processes.txt lists what ran.
    components: tuple[str, ...] = ()
    # Components the desktop or the tier starts again on purpose while the tier runs (the session
    # checks at the end that every other component is still the process it started).
    restarted: tuple[str, ...] = ()
    # The xdg-desktop-portal backend the desktop's <desktop>-portals.conf picks for
    # org.freedesktop.portal.Settings (started on demand), which applications ask for the
    # desktop's colour scheme; "" when the desktop installs none that answers it (Lubuntu).
    portal: str = ""
    # The shell needs a system bus (gnome-shell aborts without one): a private one, no services.
    system_bus: bool = False
    # The desktop's menu watches only the application folders that existed at login (Cinnamon's
    # CMenu, MATE's libmate-menu): in the tier's new home install.sh creates
    # ~/.local/share/applications, says the entry shows after the next login, and the user logs
    # out and in (the session restarts) before the menu is read.
    menu_after_login: bool = False
    # Settings the tier's user has made before logging in (run once the session bus is up, before
    # the desktop's own programs start).
    prepare: tuple[Command, ...] = ()
    # The compositor's startup command writes dekit.SESSION_ENV; otherwise (gnome-shell, which
    # has no startup command) the session reads DISPLAY and XAUTHORITY from its Xwayland.
    hands_over_xwayland: bool = True


def _xsettings(prop: str, kind: str, value: str) -> Command:
    """``xfconf-query`` setting (creating if needed) one xsettings property, as XFCE's dialogs do."""
    return ("xfconf-query", "-c", "xsettings", "-p", prop, "-n", "-t", kind, "-s", value)


def _xft_dpi(dpi: int) -> Command:
    """Wait (30 s at most) until gsd-xsettings / csd-xsettings has published ``Xft.dpi`` ``dpi``.

    A scale setting reaches ``Xft.dpi`` asynchronously: a case asks fork-linux only once the
    desktop shows its new value, never the previous case's (MATE: :func:`_mate_xft_dpi`).
    """
    script = (
        'for _ in $(seq 300); do xrdb -query | grep -qE "^Xft\\.dpi:[[:space:]]+$1$" && exit 0; sleep 0.1; done;'
        ' echo "Xft.dpi is not $1: $(xrdb -query | grep Xft.dpi)" >&2; exit 1'
    )
    return ("sh", "-c", script, "sh", str(dpi))


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
    components=("xfsettingsd", "xfwm4", "thunar"),
    # Thunar quits when told to (`thunar -q`, as fork-linux tells a user after changing uca.xml).
    restarted=("thunar",),
    # xfce-portals.conf: Settings from xapp, else gtk; Xubuntu installs only xdg-desktop-portal-gtk.
    portal="xdg-desktop-portal-gtk",
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
    components=("kded6", "kwin_x11", "dolphin"),
    portal="xdg-desktop-portal-kde",
    packages=(
        "kwin-x11", "kded6", "plasma-workspace", "plasma-session-x11", "dolphin", "konsole", "kde-cli-tools",
        "libkf6config-bin", "libkf6service-bin", "breeze", "kubuntu-settings-desktop", "xdg-desktop-portal-kde",
    ),
)



# startplasma-wayland runs kwin_wayland_wrapper, which runs kwin_wayland with its own Xwayland. The
# tier runs kwin_wayland on its virtual backend (one 1920x1080 output, software rendering) on the
# Qt platform it brings itself (not the tier's xcb), and its --exit-with-session command hands the
# session its Xwayland (dekit.SESSION_ENV) and the D-Bus activation environment, as startplasma
# does. Xwayland drops the first XTEST event it gets: one Shift press warms it up first. kwin asks
# for the portal while it starts, before DISPLAY is in the activation environment, and
# xdg-desktop-portal-kde (on the tier's xcb) aborts without it, so the portal would offer no
# Settings for the whole session: the hand-over ends that early portal, and the next request starts
# it again with the session's environment, as in a Plasma session, where it starts after startplasma
# exported it.
_KWIN_WAYLAND = """
printf '%s\n' "$1" > "$XDG_RUNTIME_DIR/fl-e2e-hand-over.sh"
exec env -u QT_QPA_PLATFORM kwin_wayland --virtual --xwayland --width 1920 --height 1080 \\
    --exit-with-session "sh $XDG_RUNTIME_DIR/fl-e2e-hand-over.sh"
"""
_KWIN_HAND_OVER = (
    "xdotool key shift"
    ' && env | grep -E "^(DISPLAY|XAUTHORITY|WAYLAND_DISPLAY)=" > "$XDG_RUNTIME_DIR/session.env.new"'
    " && dbus-update-activation-environment DISPLAY XAUTHORITY WAYLAND_DISPLAY"
    " && { pkill -x xdg-desktop-por; true; }"
    ' && mv "$XDG_RUNTIME_DIR/session.env.new" "$XDG_RUNTIME_DIR/session.env" && exec sleep infinity'
)
# KWin's virtual output.
_KWIN_OUTPUT = "Virtual-0"


def _kscreen_scale(scale: str) -> Command:
    """System Settings > Display Configuration > Scale on Wayland: libkscreen's KWin backend sets the
    output's scale (kscreen-doctor is libkscreen's own tool; it needs Qt's Wayland platform)."""
    return ("env", "QT_QPA_PLATFORM=wayland", "kscreen-doctor", f"output.{_KWIN_OUTPUT}.scale.{scale}")


KDE_WAYLAND = Desktop(
    key="kdewayland",
    title="Kubuntu 26.04 (KDE Plasma 6.6 on Wayland, its default session: kwin_wayland + Xwayland, Dolphin, Konsole)",
    # What sddm + startplasma-wayland export for the "Plasma" session (Kubuntu's default), with
    # Kubuntu's defaults as on X11. QT_QPA_PLATFORM=xcb and GDK_BACKEND=x11 are the tier's own:
    # Dolphin, Konsole and zenity then open on Xwayland, where xdotool sees them (Wine is an X
    # client anyway); fork-linux reads neither.
    session_env={
        "XDG_CURRENT_DESKTOP": "KDE",
        "DESKTOP_SESSION": "plasma",
        "XDG_SESSION_DESKTOP": "KDE",
        "XDG_SESSION_TYPE": "wayland",
        "XDG_MENU_PREFIX": "plasma-",
        "XDG_CONFIG_DIRS": "{home}/.config/kdedefaults:/etc/xdg:/usr/share/kubuntu-default-settings/kf5-settings",
        "XDG_DATA_DIRS": "/usr/local/share/:/usr/share/:/var/lib/snapd/desktop",
        "KDE_FULL_SESSION": "true",
        "KDE_SESSION_VERSION": "6",
        "KDE_APPLICATIONS_AS_SCOPE": "1",
        "QT_QPA_PLATFORM": "xcb",
        "GDK_BACKEND": "x11",
    },
    settings_daemon=KDE.settings_daemon,
    settings_ready=KDE.settings_ready,
    window_manager=("sh", "-c", _KWIN_WAYLAND, "sh", _KWIN_HAND_OVER),
    wm_name="KWin",
    wayland_compositor=True,
    file_manager="dolphin",
    file_manager_service=("dolphin", "--daemon"),
    file_manager_class="dolphin",
    provides_file_manager1=True,
    integration=DOLPHIN_SERVICEMENU,
    menu_reader=MENU_KSYCOCA,
    terminal="konsole",
    terminal_class="konsole",
    themes=KDE.themes,
    # Display Configuration > Scale: KWin publishes Xft.dpi = 96 x the output's scale to its
    # Xwayland ("Legacy applications (X11): Apply scaling themselves", Plasma's default), which
    # fork-linux reads; each case waits for it.
    scales=(
        ScaleCase("default", 100, (_xft_dpi(96),)),
        ScaleCase("display-scale-200", 200, (_kscreen_scale("2"), _xft_dpi(192))),
        ScaleCase("display-scale-150", 150, (_kscreen_scale("1.5"), _xft_dpi(144))),
    ),
    reset=(
        _plasma_global_theme("org.kubuntu.desktop"),
        _kconfig("kdeglobals", "General", "ColorScheme", None),
        _kscreen_scale("1"),
        _xft_dpi(96),
    ),
    browser=BROWSER_KDE,
    menu_launcher=KDE.menu_launcher,
    open_in_fork_keys=KDE.open_in_fork_keys,
    # On Wayland KWin draws the frames of X windows itself and does not reparent them (kwin_x11
    # does): Fork's main window is in _NET_CLIENT_LIST and stays a child of the root window.
    frames_undecorated=False,
    components=("kwin_wayland", "Xwayland", "kded6", "dolphin"),
    portal="xdg-desktop-portal-kde",
    packages=(
        "kwin-wayland", "xwayland", "kded6", "plasma-workspace", "plasma-session-wayland", "dolphin", "konsole",
        "kde-cli-tools", "libkf6config-bin", "libkf6service-bin", "libkscreen-bin", "breeze",
        "kubuntu-settings-desktop", "xdg-desktop-portal-kde",
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
    components=("lxqt-session", "openbox", "pcmanfm-qt", "lxqt-panel"),
    # lxqt-portals.conf picks lxqt, then kde, then gtk; Lubuntu installs only xdg-desktop-portal-lxqt,
    # a file chooser: no portal tells applications Lubuntu's colour scheme.
    portal="",
    packages=(
        "lxqt-session", "lxqt-config", "lxqt-panel", "lxqt-qtplugin", "lxqt-globalkeys", "lxqt-notificationd",
        "openbox", "pcmanfm-qt", "qterminal", "qtxdg-tools", "qt-style-kvantum", "lubuntu-default-settings",
        "gir1.2-gmenu-3.0", "featherpad", "lubuntu-update-notifier",
    ),
)


# A display panel's scale (GNOME Settings > Displays, Cinnamon's System Settings > Display): a new
# monitor configuration with that scale through mutter's / muffin's DisplayConfig, applied
# persistently (~/.config/monitors.xml or cinnamon-monitors.xml). The shell then asks "Keep these
# display settings?" and reverts them after a countdown: the command presses that dialog's Keep
# Changes, through the shell's own Eval (GNOME Shell with --unsafe-mode). The settings daemon
# (gsd- / csd-xsettings) then publishes Xft.dpi as 96 x the scale x the text scaling factor.
_DISPLAY_CONFIG_SCALE = """
import sys, time, gi
gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib
SHELLS = {  # display config bus name, shell bus name, the stage group the shell's dialogs join
    "gnome": ("org.gnome.Mutter.DisplayConfig", "org.gnome.Shell", "Main.layoutManager.modalDialogGroup"),
    "cinnamon": ("org.cinnamon.Muffin.DisplayConfig", "org.Cinnamon", "Main.uiGroup"),
}
config_name, shell_name, group = SHELLS[sys.argv[1]]
keep_changes = (f"const dialog = {group}.get_children().find(child => child._onSuccess);"
                " if (dialog) dialog._onSuccess(); Boolean(dialog)")
bus = Gio.bus_get_sync(Gio.BusType.SESSION)
def call(name, method, args=None):
    reply = bus.call_sync(name, "/" + name.replace(".", "/"), name, method, args, None, Gio.DBusCallFlags.NONE, -1,
                          None)
    return reply.unpack()
serial, monitors, logical_monitors, _properties = call(config_name, "GetCurrentState")
scale = float(sys.argv[2])
if logical_monitors[0][2] != scale:
    connector, modes = monitors[0][0][0], monitors[0][1]
    current = next(mode[0] for mode in modes if mode[6].get("is-current"))
    layout = [(0, 0, scale, 0, True, [(connector, current, {})])]
    call(config_name, "ApplyMonitorsConfig", GLib.Variant("(uua(iiduba(ssa{sv}))a{sv})", (serial, 2, layout, {})))
    deadline = time.monotonic() + 15
    while call(shell_name, "Eval", GLib.Variant("(s)", (keep_changes,))) != (True, "true"):
        if time.monotonic() > deadline:
            sys.exit(f"{shell_name} asked nothing about keeping the display settings")
        time.sleep(0.2)
"""


def _display_config_scale(shell: str, scale: str) -> Command:
    return ("python3", "-I", "-c", _DISPLAY_CONFIG_SCALE, shell, scale)


def _gnome_interface(key: str, value: str) -> Command:
    """``gsettings set`` of one ``org.gnome.desktop.interface`` key, as GNOME's Settings writes it."""
    return ("gsettings", "set", "org.gnome.desktop.interface", key, value)


def _displays_scale(scale: str) -> Command:
    """Settings > Displays > Scale, then "Keep Changes" (:func:`_display_config_scale`)."""
    return _display_config_scale("gnome", scale)


# The shells' own D-Bus Eval (GNOME Shell with --unsafe-mode, Cinnamon), by shell.
SHELL_BUSES = {"gnome": "org.gnome.Shell", "cinnamon": "org.Cinnamon"}
DESKTOP_ID = "{desktop_id}"


def shell_eval(shell: str, code: str, *, until_true: float = 0.0) -> str:
    """A Python program that runs the JavaScript ``code`` in the shell's Eval and prints the value of
    its last expression (JSON-decoded), or fails with the shell's error (message and stack).

    :data:`DESKTOP_ID` in ``code`` becomes the file name of ``argv[1]`` (a .desktop file), JSON-quoted.
    With ``until_true`` the code runs again every 0.5 s until it returns true, for that many seconds
    at most (a shell reloads its apps a moment after a new desktop file appears).
    """
    name = SHELL_BUSES[shell]
    return f"""
import json, sys, time, gi
gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib
code = {code!r}
if len(sys.argv) > 1:
    code = code.replace({DESKTOP_ID!r}, json.dumps(sys.argv[1].rsplit("/", 1)[-1]))
# A thrown Error serialises as {{}}: hand its text and stack over instead.
code = "(() => {{ try {{ return eval(" + json.dumps(code) + "); }} catch (e) {{ throw `${{e}}\\n${{e.stack}}`; }} }})()"
bus = Gio.bus_get_sync(Gio.BusType.SESSION)
deadline = time.monotonic() + {until_true!r}
while True:
    ok, value = bus.call_sync({name!r}, {"/" + name.replace(".", "/")!r}, {name!r}, "Eval",
                              GLib.Variant("(s)", (code,)), None, Gio.DBusCallFlags.NONE, -1, None).unpack()
    if not ok:
        sys.exit(f"{name}.Eval: {{json.loads(value) if value else value}}")
    result = json.loads(value) if value else ""
    if result is True or time.monotonic() >= deadline:
        break
    time.sleep(0.5)
if {until_true!r} and result is not True:
    sys.exit(f"{name}.Eval: still {{result!r}} after {until_true!r} s")
print(result)
"""


def _shell_launcher(shell: str, app_system: str) -> Command:
    """What a click on an app in the shell's own menu runs: the shell's ``App.open_new_window``
    (``false`` until the shell's AppSystem knows the desktop file)."""
    lookup = f"{app_system}.get_default().lookup_app({DESKTOP_ID})"
    code = f"(app => app ? (app.open_new_window(-1), true) : false)({lookup})"
    return ("python3", "-I", "-c", shell_eval(shell, code, until_true=30.0))


GNOME = Desktop(
    key="gnome",
    title="Ubuntu 26.04 (GNOME 50: GNOME Shell + Xwayland, gsd-xsettings, Nautilus 50, Ptyxis, Yaru)",
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
    # gnome-session's org.gnome.Shell@wayland unit runs gnome-shell; the tier runs it on mutter's
    # headless backend with one virtual 1920x1080 monitor, in Ubuntu's session mode (its dock,
    # desktop icons and other Ubuntu extensions), its Eval opened (--unsafe-mode) so the tier can
    # read the app grid and press the shell's own buttons.
    window_manager=(
        "gnome-shell", "--wayland", "--headless", "--virtual-monitor", "1920x1080", "--mode=ubuntu", "--unsafe-mode",
    ),
    wm_name="GNOME Shell",
    # Ubuntu's desktop icons (DING) run under the tier's GDK_BACKEND=x11 too, and would warn about it
    # in a window over the screen: its "Don't show this message anymore".
    prepare=(("gsettings", "set", "org.gnome.shell.extensions.ding", "check-x11wayland", "false"),),
    wayland_compositor=True,
    hands_over_xwayland=False,
    system_bus=True,
    # Nautilus's D-Bus service owns org.freedesktop.FileManager1 (and quits when idle).
    file_manager="nautilus",
    file_manager_service=("nautilus", "--gapplication-service"),
    file_manager_class="org.gnome.Nautilus",
    provides_file_manager1=True,
    integration=NAUTILUS_EXTENSION,
    # GNOME Shell's own app grid (ours is on the top level, in no folder), and its launcher.
    menu_reader=MENU_GNOME_SHELL,
    menu_folder=(),
    # Clicking our icon in GNOME Shell's app grid.
    menu_launcher=_shell_launcher("gnome", "Shell.AppSystem"),
    # Ubuntu 26.04's terminal, which xdg-terminal-exec picks.
    terminal="ptyxis",
    terminal_class="ptyxis",
    # Settings > Appearance > Style: Ubuntu's panel sets the color scheme and the Yaru variant.
    themes={
        LIGHT: (_gnome_interface("color-scheme", "default"), _gnome_interface("gtk-theme", "Yaru")),
        DARK: (_gnome_interface("color-scheme", "prefer-dark"), _gnome_interface("gtk-theme", "Yaru-dark")),
    },
    # Settings > Displays > Scale; Settings > Accessibility > Large Text (text scaling 1.25). Each
    # case waits for gsd-xsettings' Xft.dpi (96 x display scale x text scaling).
    scales=(
        ScaleCase("default", 100, (_xft_dpi(96),)),
        ScaleCase("display-200", 200, (_displays_scale("2"), _xft_dpi(192))),
        ScaleCase("large-text-125", 125, (_gnome_interface("text-scaling-factor", "1.25"), _xft_dpi(120))),
        ScaleCase(
            "display-200-large-text-250", 250,
            (_displays_scale("2"), _gnome_interface("text-scaling-factor", "1.25"), _xft_dpi(240)),
        ),
    ),
    reset=(
        _gnome_interface("color-scheme", "default"),
        _gnome_interface("gtk-theme", "Yaru"),
        _gnome_interface("text-scaling-factor", "1.0"),
        _displays_scale("1"),
        _xft_dpi(96),
    ),
    browser=BROWSER_XDG_MIME,
    # Nautilus 50 opens the folder's context menu with the Menu key (not Shift+F10); "Open in Fork"
    # is the second item from the bottom, above Properties.
    open_in_fork_keys=("Menu", "Up", "Up", "Return"),
    # GNOME Shell (mutter) reparents only decorated X windows: Fork's own title bar leaves it unframed.
    frames_undecorated=False,
    settings_ready=("sh", "-c", "xrdb -query | grep -q '^Xft.dpi:'"),
    components=("gnome-shell", "Xwayland", "gsd-xsettings"),
    portal="xdg-desktop-portal-gnome",
    packages=(
        "gnome-shell", "ubuntu-session", "xwayland", "gnome-settings-daemon", "ubuntu-settings", "yaru-theme-gtk",
        "nautilus", "python3-nautilus", "ptyxis", "xdg-terminal-exec", "xdg-desktop-portal-gnome",
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
    components=("labwc", "Xwayland", "budgie-daemon"),
    portal="xdg-desktop-portal-gtk",
    packages=(
        "labwc", "xwayland", "wlr-randr", "budgie-core", "budgie-desktop", "budgie-desktop-environment",
        "ubuntu-budgie-themes", "nemo", "xfce4-terminal", "dconf-gsettings-backend", "at-spi2-core",
        "gir1.2-atspi-2.0",
    ),
)


def _mate_set(schema: str, key: str, value: str) -> Command:
    """``gsettings set`` of one MATE key, as MATE's Control Center dialogs write it."""
    return ("gsettings", "set", schema, key, value)


def _mate_reset(schema: str, key: str) -> Command:
    """``gsettings reset``: the key back to its (Ubuntu MATE) default."""
    return ("gsettings", "reset", schema, key)


_MATE_INTERFACE = "org.mate.interface"
_MATE_FONTS = "org.mate.font-rendering"
_MARCO = "org.mate.Marco.general"


def _mate_theme(gtk: str, marco: str) -> tuple[Command, ...]:
    """Appearance > Theme: a theme sets the controls (GTK) and Marco's window border."""
    return (_mate_set(_MATE_INTERFACE, "gtk-theme", gtk), _mate_set(_MARCO, "theme", marco))


# Appearance > Fonts > Details > Window Scaling: org.mate.interface window-scaling-factor (0 = auto,
# 1 on the tier's 96 DPI screen). When the scale changes, mate-settings-daemon restarts the window
# manager ("marco --replace"), and the replaced marco never exits (marco 1.26.2's meta_quit finds
# no main loop: "g_main_loop_is_running: assertion 'loop != NULL' failed"), so stale marcos pile
# up. The command waits for the new marco (25 s) and ends the replaced ones, as their own exit
# would have. Now and then mate-settings-daemon then held the X server for minutes (see
# docs/qa/DE-E2E.md): when no window manager answers 10 s later, the settings daemon is ended
# (the session starts it again), then marco if need be (all within Session.run's 60 s).
_MATE_WINDOW_SCALING = """
import os, signal, subprocess, sys, time
KEY = ("org.mate.interface", "window-scaling-factor")
def scale():
    words = subprocess.run(["gsettings", "get", *KEY], capture_output=True, text=True, check=True).stdout.split()
    return max(int(words[-1]), 1)
def wm_check():
    try:
        root = subprocess.run(["xprop", "-root", "_NET_SUPPORTING_WM_CHECK"], capture_output=True, text=True,
                              timeout=3).stdout
    except subprocess.TimeoutExpired:
        return None
    return root.rpartition("#")[2].strip()
def marcos():
    found = []
    for pid in filter(str.isdigit, os.listdir("/proc")):
        try:
            with open(f"/proc/{pid}/stat", encoding="utf-8") as stat:
                text = stat.read()
        except OSError:
            continue
        fields = text[text.rfind(")") + 2 :].split()
        if text[text.find("(") + 1 : text.rfind(")")] == "marco" and fields[0] != "Z":
            found.append((int(fields[19]), int(pid)))
    return [pid for _started, pid in sorted(found)]
def settled(seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if wm_check() not in (None, "") and marcos():
            return True
        time.sleep(0.5)
    return False
before, check = scale(), wm_check()
subprocess.run(["gsettings", "set", *KEY, sys.argv[1]] if sys.argv[1:] else ["gsettings", "reset", *KEY], check=True)
deadline = time.monotonic() + 25
while scale() != before and wm_check() in (check, None) and time.monotonic() < deadline:
    time.sleep(0.2)
for pid in marcos()[:-1]:
    os.kill(pid, signal.SIGTERM)
if scale() != before and not settled(10):
    # mate-settings-daemon holds the X server (a grab): end it, the session starts it again
    # (see _MATE_SETTINGS_DAEMON); then a marco that still does not answer is started again,
    # as mate-session starts its window manager again. logs/settings-daemon-stalls.txt says so.
    with open(os.path.join(os.environ["HOME"], "..", "logs", "settings-daemon-stalls.txt"), "a",
              encoding="utf-8") as log:
        log.write(f"no window manager answered after window-scaling-factor {sys.argv[1:] or 'reset'}\\n")
    subprocess.run(["pkill", "-KILL", "-x", "mate-settings-d"], check=False)
    if not settled(15):
        for pid in marcos():
            os.kill(pid, signal.SIGKILL)
        subprocess.Popen(["marco", "--replace"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
        if not settled(15):
            sys.exit("no window manager answers after the window scaling change")
"""


# mate-session restarts mate-settings-daemon when it dies (X-MATE-AutoRestart=true in its autostart
# entry). In the container it now and then segfaulted in libmate-desktop (a NULL read at 0x78)
# right after a window-scaling change, and Xft.dpi then stayed where it was: the session restarts
# it the same way and says so in logs/settings-daemon.log.
_MATE_SETTINGS_DAEMON = (
    "while :; do /usr/libexec/mate-settings-daemon; echo \"mate-settings-daemon exited ($?):"
    " restarting it as mate-session does (X-MATE-AutoRestart)\" >&2; sleep 1; done"
)


# Wait (40 s at most) until mate-settings-daemon has published Xft.dpi ARGV[1]. Now and then the
# daemon held the X server (a grab) for minutes while it updated Xft.dpi, and xrdb got no answer:
# then it is ended and the session starts it again (_MATE_SETTINGS_DAEMON), which publishes the
# current settings; logs/settings-daemon-stalls.txt says so.
_MATE_XFT_DPI = """
import os, subprocess, sys, time
def published():
    try:
        out = subprocess.run(["xrdb", "-query"], capture_output=True, text=True, timeout=5).stdout
    except subprocess.TimeoutExpired:
        return None
    return next((line.split(":", 1)[1].strip() for line in out.splitlines() if line.startswith("Xft.dpi:")), "")
deadline, ended = time.monotonic() + 40, False
while time.monotonic() < deadline:
    value = published()
    if value == sys.argv[1]:
        sys.exit(0)
    if value is None and not ended:
        with open(os.path.join(os.environ["HOME"], "..", "logs", "settings-daemon-stalls.txt"), "a",
                  encoding="utf-8") as log:
            log.write(f"the X server did not answer xrdb while Xft.dpi was to become {sys.argv[1]}\\n")
        subprocess.run(["pkill", "-KILL", "-x", "mate-settings-d"], check=False)
        ended = True
    time.sleep(0.2)
sys.exit(f"Xft.dpi is not {sys.argv[1]}: {published()!r}")
"""


def _mate_xft_dpi(dpi: int) -> Command:
    """Wait until mate-settings-daemon has published ``Xft.dpi`` ``dpi`` (ending a stuck daemon)."""
    return ("python3", "-I", "-c", _MATE_XFT_DPI, str(dpi))


def _mate_window_scaling(*value: str) -> Command:
    """Set (or, without a value, reset) MATE's window scaling and settle marco's restart."""
    return ("python3", "-I", "-c", _MATE_WINDOW_SCALING, *value)


MATE = Desktop(
    key="mate",
    title="Ubuntu MATE 26.04 (MATE 1.26: marco, mate-settings-daemon, Caja + caja-actions, mate-terminal)",
    # What lightdm + /etc/X11/Xsession.d (60x11-common_xdg_path, 80mate-environment),
    # /etc/profile.d/mate-qt.sh and mate-session export for the "MATE" session.
    session_env={
        "XDG_CURRENT_DESKTOP": "MATE",
        "DESKTOP_SESSION": "mate",
        "XDG_SESSION_DESKTOP": "mate",
        "XDG_SESSION_TYPE": "x11",
        "XDG_MENU_PREFIX": "mate-",
        "XDG_CONFIG_DIRS": "/etc/xdg/xdg-mate:/etc/xdg",
        "XDG_DATA_DIRS": "/usr/share/mate:/usr/local/share/:/usr/share/",
        "GTK_OVERLAY_SCROLLING": "0",
        "COMPIZ_CONFIG_PROFILE": "mate",
        "QT_QPA_PLATFORMTHEME": "gtk2",
    },
    settings_daemon=("sh", "-c", _MATE_SETTINGS_DAEMON),
    window_manager=("marco", "--replace"),
    wm_name="Metacity (Marco)",
    # mate-session's file manager (and the FileManager1 D-Bus service Caja ships) is
    # "caja --no-default-window", which also draws the desktop.
    file_manager="caja",
    file_manager_service=("caja", "--no-default-window"),
    file_manager_class="Caja",
    provides_file_manager1=True,
    integration=CAJA_FMA,
    # mate-panel's Applications menu (libmate-menu on mate-applications.menu) files
    # Categories=Development under its "Development" menu, shown as "Programming".
    menu_reader=MENU_MATE_PANEL,
    menu_folder=("Programming",),
    menu_after_login=True,
    terminal="mate-terminal",
    terminal_class="Mate-terminal",
    # Control Center > Appearance > Theme: Ubuntu MATE's Yaru-MATE light and dark, and MATE's own
    # stock themes (mate-themes), whose dark ones do not say "dark" in their names.
    themes={
        LIGHT: _mate_theme("Yaru-MATE-light", "Yaru"),
        DARK: _mate_theme("Yaru-MATE-dark", "Yaru-dark"),
        "light:menta": _mate_theme("Menta", "Menta"),
        "dark:blackmate": _mate_theme("BlackMATE", "BlackMATE"),
        "dark:highcontrastinverse": _mate_theme("HighContrastInverse", "HighContrastInverse"),
    },
    # Control Center > Appearance > Fonts > Details: Resolution (dots per inch); Window Scaling
    # (org.mate.interface window-scaling-factor, 0 = auto).
    scales=(
        # Each case waits for mate-settings-daemon's Xft.dpi (font DPI x window scaling): once a
        # case ran on the previous case's 144 (fork-linux answered 150 for font-dpi-192).
        ScaleCase("default", 100, (_mate_xft_dpi(96),)),
        ScaleCase("window-scaling-2", 200, (_mate_window_scaling("2"), _mate_xft_dpi(192))),
        ScaleCase("font-dpi-144", 150, (_mate_set(_MATE_FONTS, "dpi", "144.0"), _mate_xft_dpi(144))),
        ScaleCase("font-dpi-192", 200, (_mate_set(_MATE_FONTS, "dpi", "192.0"), _mate_xft_dpi(192))),
    ),
    reset=(
        _mate_reset(_MATE_INTERFACE, "gtk-theme"),
        _mate_reset(_MARCO, "theme"),
        _mate_window_scaling(),
        _mate_reset(_MATE_FONTS, "dpi"),
        _mate_xft_dpi(96),
    ),
    browser=BROWSER_XDG_MIME,
    # Caja's folder context menu: Create Folder, Create Document, Caja-Actions actions (the submenu
    # caja-actions puts every action in), whose first item is "Open in Fork".
    open_in_fork_keys=("Menu", "Down", "Down", "Down", "Right", "Return"),
    # marco, like metacity, gives a window that asks for no decorations no frame: Fork's main
    # window is managed (client list, WM_STATE) but stays a child of the root window.
    frames_undecorated=False,
    settings_ready=("sh", "-c", "xrdb -query | grep -q '^Xft.dpi:'"),
    components=("mate-settings-daemon", "marco", "caja"),
    # A window-scaling change makes mate-settings-daemon start marco again; the tier ends a settings
    # daemon that holds the X server (see _MATE_XFT_DPI) and the session starts it again.
    restarted=("mate-settings-daemon", "marco"),
    portal="xdg-desktop-portal-gtk",
    packages=(
        "marco", "mate-settings-daemon", "mate-desktop-common", "caja", "caja-actions", "mate-terminal",
        "mate-menus", "gir1.2-matemenu-2.0", "mate-themes", "ubuntu-mate-default-settings", "ubuntu-mate-themes",
        "dconf-gsettings-backend",
    ),
)


_CINNAMON_INTERFACE = "org.cinnamon.desktop.interface"
_CINNAMON_PORTAL = "org.x.apps.portal"


def _cinnamon_set(schema: str, key: str, value: str) -> Command:
    """``gsettings set`` of one Cinnamon key, as Cinnamon's System Settings writes it."""
    return ("gsettings", "set", schema, key, value)


def _cinnamon_reset(schema: str, key: str) -> Command:
    """``gsettings reset``: the key back to its (Ubuntu Cinnamon) default."""
    return ("gsettings", "reset", schema, key)


def _ui_scale(scale: str) -> Command:
    """System Settings > Display > User interface scale, then "Keep this configuration"."""
    return _display_config_scale("cinnamon", scale)


def _text_scaling(factor: str) -> Command:
    """System Settings > Fonts > Text scaling factor."""
    return _cinnamon_set(_CINNAMON_INTERFACE, "text-scaling-factor", factor)


def _gtk_theme(name: str) -> Command:
    """System Settings > Themes > Applications."""
    return _cinnamon_set(_CINNAMON_INTERFACE, "gtk-theme", name)


# What cinnamon-session starts for the settings (its csd-* autostart entries: csd-settings-remap
# mirrors Cinnamon's interface keys into org.gnome.desktop.interface for GNOME applications,
# csd-xsettings publishes XSETTINGS and Xft.dpi), in the tier user's home as they left it: they
# ran Ubuntu Cinnamon's gnome-terminal once (which then writes itself into xdg-terminal-exec's
# lists), then picked Xfce Terminal in System Settings > Preferred Applications > Terminal
# (cs_default stores its desktop entry's Exec), and the light Yaru-cinnamon theme (Ubuntu Cinnamon
# ships Yaru-cinnamon-dark; the real-Fork scenario starts light).
_CINNAMON_SESSION = (
    'mkdir -p "$XDG_CONFIG_HOME"'
    ' && for list in x-cinnamon-xdg-terminals.list xdg-terminals.list; do'
    ' echo org.gnome.Terminal.desktop > "$XDG_CONFIG_HOME/$list"; done'
    " && gsettings set org.cinnamon.desktop.default-applications.terminal exec xfce4-terminal"
    " && gsettings set org.cinnamon.desktop.default-applications.terminal exec-arg -x"
    " && gsettings set org.cinnamon.desktop.interface gtk-theme Yaru-cinnamon"
    " && { /usr/bin/csd-settings-remap & exec /usr/bin/csd-xsettings; }"
)
# Xorg with the dummy driver (docker/Dockerfile.e2e.de.cinnamon writes its 1920x1080@60 config),
# run rootless like Xvfb: muffin needs a RandR mode with a refresh rate to apply a UI scale.
_XORG_DUMMY = (
    'mkdir -p "$XDG_DATA_HOME/xorg" && exec /usr/lib/xorg/Xorg "$@" -config /etc/X11/fl-e2e-dummy.conf'
    ' -noreset -nolisten tcp -dpi 96 -logfile "$XDG_DATA_HOME/xorg/Xorg.log"'
)

CINNAMON = Desktop(
    key="cinnamon",
    title="Ubuntu Cinnamon 26.04 (Cinnamon 6.4 on muffin, csd-xsettings, Nemo 6.4, xfce4-terminal, Yaru-cinnamon)",
    # What lightdm + /etc/X11/Xsession.d (60x11-common_xdg_path) and cinnamon-session export for
    # the "Cinnamon" session (cinnamon.session: DesktopName=X-Cinnamon; no XDG_MENU_PREFIX: the
    # menu applet reads cinnamon-applications.menu by name).
    session_env={
        "XDG_CURRENT_DESKTOP": "X-Cinnamon",
        "DESKTOP_SESSION": "cinnamon",
        "XDG_SESSION_DESKTOP": "cinnamon",
        "XDG_SESSION_TYPE": "x11",
        "XDG_DATA_DIRS": "/usr/share/cinnamon:/usr/local/share/:/usr/share/",
    },
    x_server=("sh", "-c", _XORG_DUMMY, "sh"),
    settings_daemon=("sh", "-c", _CINNAMON_SESSION),
    # cinnamon-session's cinnamon-launcher runs cinnamon: the shell (panel, menu applet, window
    # manager on libmuffin), which names itself after muffin.
    window_manager=("cinnamon", "--replace"),
    wm_name="Mutter (Muffin)",
    # Nemo's D-Bus services (nemo.FileManager1.service, nemo.service) run "nemo --no-default-window".
    file_manager="nemo",
    file_manager_service=("nemo", "--no-default-window"),
    file_manager_class="Nemo",
    provides_file_manager1=True,
    integration=NEMO_ACTION,
    # The menu applet (CMenu on cinnamon-applications.menu): Categories=Development is "Programming".
    menu_reader=MENU_CINNAMON,
    menu_folder=("Programming",),
    # Clicking our entry in the menu applet (Cinnamon's own AppSystem).
    menu_launcher=_shell_launcher("cinnamon", "imports.gi.Cinnamon.AppSystem"),
    menu_after_login=True,
    # The terminal of System Settings > Preferred Applications (exec of
    # org.cinnamon.desktop.default-applications.terminal), which Nemo's "Open in Terminal" starts.
    terminal="xfce4-terminal",
    terminal_class="Xfce4-terminal",
    themes={
        LIGHT: (_gtk_theme("Yaru-cinnamon"),),
        # Ubuntu Cinnamon's default theme.
        DARK: (_gtk_theme("Yaru-cinnamon-dark"),),
        # Themes > Settings > Dark mode: "Prefer dark mode" (org.x.apps.portal color-scheme, what
        # the XApp portal hands applications as their color scheme), with the light theme kept.
        "dark:prefer-dark-mode": (_cinnamon_set(_CINNAMON_PORTAL, "color-scheme", "prefer-dark"),),
    },
    scales=(
        ScaleCase("default", 100, (_xft_dpi(96),)),
        ScaleCase("ui-scale-200", 200, (_ui_scale("2"), _xft_dpi(192))),
        ScaleCase("text-scaling-150", 150, (_text_scaling("1.5"), _xft_dpi(144))),
        ScaleCase("ui-scale-200-text-scaling-125", 250, (_ui_scale("2"), _text_scaling("1.25"), _xft_dpi(240))),
    ),
    reset=(
        _gtk_theme("Yaru-cinnamon"),
        _cinnamon_reset(_CINNAMON_PORTAL, "color-scheme"),
        _cinnamon_reset(_CINNAMON_INTERFACE, "text-scaling-factor"),
        _ui_scale("1"),
        _xft_dpi(96),
    ),
    browser=BROWSER_XDG_MIME,
    # Nemo's context menu of the folder it shows (no item selected): Create New Folder, Create New
    # Document, then the actions for the folder (ours: Selection=none), then Open in Terminal.
    open_in_fork_keys=("Menu", "Down", "Down", "Down", "Return"),
    # muffin, like mutter, reparents only decorated windows: Fork draws its own title bar.
    frames_undecorated=False,
    settings_ready=("sh", "-c", "xrdb -query | grep -q '^Xft.dpi:'"),
    components=("cinnamon", "Xorg", "csd-xsettings", "csd-settings-remap"),
    portal="xdg-desktop-portal-xapp",
    packages=(
        "cinnamon", "cinnamon-settings-daemon", "nemo", "ubuntucinnamon-environment", "yaru-cinnamon-theme-gtk",
        "gir1.2-cmenu-3.0", "gnome-terminal", "xdg-terminal-exec", "xfce4-terminal", "xdg-desktop-portal-xapp",
        "xserver-xorg-core", "xserver-xorg-video-dummy", "dconf-cli",
    ),
)


DESKTOPS: dict[str, Desktop] = {
    desktop.key: desktop for desktop in (XFCE, KDE, KDE_WAYLAND, LXQT, GNOME, BUDGIE, MATE, CINNAMON)
}
