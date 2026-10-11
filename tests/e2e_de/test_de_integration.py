"""Desktop integration tier (no Wine): fork-linux from the release tarball in a real desktop session.

Gated by ``FL_E2E_DE=<desktop>``; containers only (``scripts/ci-e2e-de.sh <desktop>``). Each test
asks the desktop's own programs: its menu reader lists our entry, its file manager runs our
"Open in Fork" action from its context menu, its settings switch the theme and scale fork-linux
reads, ``fork-linux-host`` opens its terminal in a directory and shows a file in its file
manager, and xdg-open hands files and links to the default applications the desktop chose.
Nothing here downloads or starts Wine: a launch is proved by fork-linux's first-run license
question (zenity), which is then declined.
"""

from __future__ import annotations

import configparser
import json
import os
import shlex
import textwrap
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from pathlib import Path

import dekit
import desktops
import pytest

from fixtures import real_tier

if not os.environ.get("FL_E2E_DE"):
    pytest.skip(
        "requires FL_E2E_DE=<desktop> (scripts/ci-e2e-de.sh: a desktop session in a container)",
        allow_module_level=True,
    )
# Containers only (AGENTS.md hard rule 18).
real_tier.enforce()

REPO = Path(__file__).resolve().parents[2]
APP_ID = "io.github.ventura8.ForkLinux"
CONSENT_TITLE = "Fork license agreement"
OPEN_ACTION = "open --from-file-manager"
STUB_MIME = "text/x-fl-e2e"
STUB_SUFFIX = ".fle2e"
STUB_URL = "https://example.invalid/fl-e2e?from=fork-linux-host"
LAUNCH_TIMEOUT = 60.0
# What `fork-linux doctor` must fail on before setup ran: Wine, the prefix and Fork itself.
NOT_SET_UP = {"wine.present", "prefix.exists", "fork.installed"}


# -- helpers ---------------------------------------------------------------------------------


def _installed_python(session: dekit.Session, installed: dekit.Installed, code: str) -> str:
    """Run ``code`` with the installed fork_linux package (not the source tree) first on sys.path."""
    prelude = f"import sys; sys.path.insert(0, {str(installed.package_dir)!r})\n"
    return session.run(["python3", "-I", "-c", prelude + code]).stdout.strip()


def _consent_window(window: dekit.Window) -> bool:
    return window.name == CONSENT_TITLE


def _fork_linux_cmdlines() -> list[str]:
    """Command lines of running fork-linux processes (the launcher runs as ``python3 -I …/fork-linux``)."""
    found = []
    for pid in dekit.pids_of("python3"):
        try:
            argv = Path("/proc", str(pid), "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        words = [os.fsdecode(arg) for arg in argv if arg]
        if any(word.endswith("/fork-linux") for word in words):
            found.append(shlex.join(words))
    return found


def _decline_consent(session: dekit.Session) -> list[str]:
    """Wait for fork-linux's license question, note who asked, decline it and wait for fork-linux to end."""
    window = session.wait_window(_consent_window, "fork-linux's license question", LAUNCH_TIMEOUT)
    askers = _fork_linux_cmdlines()
    session.screenshot("consent")
    session.activate(window)
    session.keys(["Escape"])
    dekit.wait_for(lambda: not _fork_linux_cmdlines(), LAUNCH_TIMEOUT, "fork-linux to end after the refusal")
    return askers


def _git_repo(path: Path, session: dekit.Session) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    session.run(["git", "init", "-q", str(path)])
    return path


# -- default applications (a stub that records what it was asked to open) --------------------


def _write(path: Path, text: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(mode)


@pytest.fixture(scope="module")
def stub(session: dekit.Session, desktop: desktops.Desktop) -> Path:
    """A default application for ``text/x-fl-e2e`` files and for web links; returns its call log."""
    calls = session.root / "stub-calls.txt"
    program = session.home / ".local" / "libexec" / "fl-e2e-stub"
    _write(program, f'#!/bin/sh\nprintf "%s\\n" "$*" >> {shlex.quote(str(calls))}\n', 0o755)
    data = session.home / ".local" / "share"
    _write(
        data / "applications" / "fl-e2e-stub.desktop",
        textwrap.dedent(f"""\
            [Desktop Entry]
            Type=Application
            Name=FL E2E stub
            Exec={program} %u
            MimeType={STUB_MIME};x-scheme-handler/http;x-scheme-handler/https;
            Categories=Network;WebBrowser;
            NoDisplay=true
            """),
    )
    _write(
        data / "mime" / "packages" / "fl-e2e.xml",
        textwrap.dedent(f"""\
            <?xml version="1.0" encoding="UTF-8"?>
            <mime-info xmlns="http://www.freedesktop.org/standards/shared-mime-info">
              <mime-type type="{STUB_MIME}"><comment>FL E2E file</comment><glob pattern="*{STUB_SUFFIX}"/></mime-type>
            </mime-info>
            """),
    )
    session.run(["update-mime-database", str(data / "mime")])
    session.run(["update-desktop-database", str(data / "applications")])
    for mime in (STUB_MIME, "x-scheme-handler/http", "x-scheme-handler/https"):
        session.run(["xdg-mime", "default", "fl-e2e-stub.desktop", mime])
    if desktop.browser == desktops.BROWSER_XFCE_HELPERS:
        # Settings > Default Applications > Web Browser > Other: a custom helper in helpers.rc.
        _write(
            data / "xfce4" / "helpers" / "fl-e2e-browser.desktop",
            textwrap.dedent(f"""\
                [Desktop Entry]
                NoDisplay=true
                Version=1.0
                Type=X-XFCE-Helper
                Name=FL E2E stub
                X-XFCE-Category=WebBrowser
                X-XFCE-Commands={program};
                X-XFCE-CommandsWithParameter={program} "%s";
                """),
        )
        _write(session.home / ".config" / "xfce4" / "helpers.rc", "WebBrowser=fl-e2e-browser\n")
    if desktop.browser == desktops.BROWSER_KDE:
        # Default Applications > Web browser also names it in kdeglobals: KIO hands http(s) links to
        # BrowserApplication first (Kubuntu's default: the Firefox snap), else to mimeapps.list.
        session.run(["kwriteconfig6", "--file", "kdeglobals", "--group", "General", "--key", "BrowserApplication",
                     "fl-e2e-stub.desktop"])
    return calls


def _stub_called_with(calls: Path, text: str) -> bool:
    return calls.is_file() and any(text in line for line in calls.read_text(encoding="utf-8").splitlines())


# -- menu readers ------------------------------------------------------------------------------

GARCON_READER = """
import json, gi
gi.require_version("Garcon", "1.0")
from gi.repository import Garcon
menu = Garcon.Menu.new_applications()
menu.load(None)
found = []
def walk(node, path):
    for item in node.get_items():
        found.append({"path": path, "id": item.get_desktop_id(), "command": item.get_command()})
    for sub in node.get_menus():
        walk(sub, path + [sub.get_name()])
walk(menu, [])
print(json.dumps(found))
"""


def _garcon_entries(session: dekit.Session) -> list[dict[str, object]]:
    """Every entry of XFCE's applications menu as garcon (the panel's menu library) reads it."""
    return json.loads(session.run(["python3", "-I", "-c", GARCON_READER]).stdout)


def _desktop_entry_exec(session: dekit.Session, menu_id: str) -> str:
    """``Exec`` of the desktop entry ``menu_id`` that wins in the XDG data directories, read by KConfig."""
    for base in (session.env["XDG_DATA_HOME"], *session.env.get("XDG_DATA_DIRS", "/usr/share").split(":")):
        path = Path(base) / "applications" / menu_id
        if path.is_file():
            argv = ["kreadconfig6", "--file", str(path), "--group", "Desktop Entry", "--key", "Exec"]
            return session.run(argv).stdout.strip()
    return ""


def _ksycoca_entries(session: dekit.Session) -> list[dict[str, object]]:
    """Every entry of Plasma's application menu as KService builds it for Kickoff and Kicker from
    ``$XDG_MENU_PREFIX``applications.menu (``kbuildsycoca6 --menutest``: menu path, entry id)."""
    found: list[dict[str, object]] = []
    for line in session.run(["kbuildsycoca6", "--menutest"], timeout=120).stdout.splitlines():
        menu, sep, rest = line.partition("\t")
        menu_id = rest.split("\t", 1)[0]
        if sep and menu_id:
            path = [part for part in menu.split("/") if part]
            found.append({"path": path, "id": menu_id, "command": _desktop_entry_exec(session, menu_id)})
    return found


# lxqt-panel's application menu: the menu file its menu plugin (fancymenu or mainmenu) names in
# panel.conf (LXQt merges the user's file over XDG_CONFIG_DIRS key by key), else
# $XDG_MENU_PREFIX applications.menu, read with GMenu: the XDG menu specification the panel's
# libqtxdg implements (libqtxdg has no Python binding).
LXQT_PANEL_READER = """
import configparser, json, os, gi
gi.require_version("GMenu", "3.0")
from gi.repository import GMenu
dirs = [os.environ["XDG_CONFIG_HOME"], *os.environ.get("XDG_CONFIG_DIRS", "/etc/xdg").split(":")]
panel = configparser.RawConfigParser(strict=False, interpolation=None)
for base in reversed(dirs):
    path = os.path.join(base, "lxqt", "panel.conf")
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as conf:  # QSettings: keys before any section are [General]
            panel.read_string("[General]\\n" + conf.read(), path)
menus = [panel[name].get("menu_file") for name in panel.sections()
         if panel[name].get("type") in ("fancymenu", "mainmenu")]
menu_file = next((menu for menu in menus if menu), "")
if menu_file:
    tree = GMenu.Tree.new_for_path(menu_file, GMenu.TreeFlags.NONE)
else:
    tree = GMenu.Tree.new(os.environ.get("XDG_MENU_PREFIX", "") + "applications.menu", GMenu.TreeFlags.NONE)
tree.load_sync()
found = []
def walk(directory, path):
    items = directory.iter()
    kind = items.next()
    while kind != GMenu.TreeItemType.INVALID:
        if kind == GMenu.TreeItemType.ENTRY:
            entry = items.get_entry()
            found.append({"path": path, "id": entry.get_desktop_file_id(),
                          "command": entry.get_app_info().get_commandline()})
        elif kind == GMenu.TreeItemType.DIRECTORY:
            folder = items.get_directory()
            walk(folder, path + [folder.get_name()])
        kind = items.next()
walk(tree.get_root_directory(), [])
print(json.dumps(found))
"""


def _lxqt_panel_entries(session: dekit.Session) -> list[dict[str, object]]:
    """Every entry of the LXQt panel's application menu."""
    return json.loads(session.run(["python3", "-I", "-c", LXQT_PANEL_READER]).stdout)


# gnome-shell's app grid (Shell.AppSystem): every desktop entry Gio lists that should be shown; an
# app sits in a folder of org.gnome.desktop.app-folders when the folder names it or one of its
# categories (and does not exclude it), else on the top level.
GNOME_SHELL_READER = """
import json, gi
gi.require_version("Gio", "2.0")
from gi.repository import Gio
SCHEMA = "org.gnome.desktop.app-folders"
folders = []
for folder in Gio.Settings.new(SCHEMA).get_strv("folder-children"):
    settings = Gio.Settings.new_with_path(SCHEMA + ".folder", f"/org/gnome/desktop/app-folders/folders/{folder}/")
    folders.append((folder, settings.get_strv("apps"), settings.get_strv("categories"),
                    settings.get_strv("excluded-apps")))
found = []
for app in Gio.AppInfo.get_all():
    if not app.should_show():
        continue
    app_id, categories = app.get_id(), (app.get_categories() or "").split(";")
    path = [name for name, apps, wanted, excluded in folders
            if app_id in apps or (set(wanted) & set(categories) and app_id not in excluded)][:1]
    found.append({"path": path, "id": app_id, "command": app.get_commandline()})
print(json.dumps(found))
"""


def _gnome_shell_entries(session: dekit.Session) -> list[dict[str, object]]:
    """Every app of GNOME's app grid as gnome-shell finds it (Gio's desktop entries, app folders)."""
    return json.loads(session.run(["python3", "-I", "-c", GNOME_SHELL_READER]).stdout)


# Budgie's panel menus (Budgie Menu; Ubuntu Budgie's AppMenu groups the same way) list what
# libbudgie-appindexer indexes: Gio's desktop entries that should be shown, grouped by their main
# category (named after desktop-directories). The library has no GObject introspection data, so
# it is called through ctypes (its lists are Gee lists); it needs GTK for the icons it looks up.
BUDGIE_READER = """
import ctypes, json, gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk
Gtk.init(None)
indexer, gee = ctypes.CDLL("libbudgie-appindexer.so.0"), ctypes.CDLL("libgee-0.8.so.2")
def function(library, name, restype, *argtypes):
    found = getattr(library, name)
    found.restype, found.argtypes = restype, list(argtypes)
    return found
P, S = ctypes.c_void_p, ctypes.c_char_p
index = function(indexer, "budgie_app_index_get", P)()
categories = function(indexer, "budgie_app_index_get_categories", P, P)
category_name = function(indexer, "budgie_category_get_name", S, P)
category_apps = function(indexer, "budgie_category_get_apps", P, P)
desktop_id = function(indexer, "budgie_application_get_desktop_id", S, P)
command = function(indexer, "budgie_application_get_exec", S, P)
size, get = function(gee, "gee_collection_get_size", ctypes.c_int, P), function(gee, "gee_list_get", P, P, ctypes.c_int)
def items(gee_list):
    return [get(gee_list, position) for position in range(size(gee_list))]
found = [
    {"path": [category_name(category).decode()], "id": (desktop_id(app) or b"").decode(),
     "command": (command(app) or b"").decode()}
    for category in items(categories(index)) for app in items(category_apps(category))
]
print(json.dumps(found))
"""


def _budgie_entries(session: dekit.Session) -> list[dict[str, object]]:
    """Every entry of Budgie's panel menus, as libbudgie-appindexer files them."""
    return json.loads(session.run(["python3", "-I", "-c", BUDGIE_READER]).stdout)

# mate-panel's Applications menu: libmate-menu on mate-applications.menu (the panel names that file
# itself, whatever XDG_MENU_PREFIX says), as the panel shows it (directory names, not menu ids). The
# binding has no TreeEntry.get_app_info: the command comes from the entry's desktop file.
MATE_PANEL_READER = """
import json, gi
gi.require_version("MateMenu", "2.0")
from gi.repository import Gio, MateMenu
tree = MateMenu.Tree.new("mate-applications.menu", MateMenu.TreeFlags.NONE)
tree.load_sync()
found = []
def walk(directory, path):
    items = directory.iter()
    kind = items.next()
    while kind != MateMenu.TreeItemType.INVALID:
        if kind == MateMenu.TreeItemType.ENTRY:
            entry = items.get_entry()
            info = Gio.DesktopAppInfo.new_from_filename(entry.get_desktop_file_path())
            found.append({"path": path, "id": entry.get_desktop_file_id(), "command": info.get_commandline()})
        elif kind == MateMenu.TreeItemType.DIRECTORY:
            folder = items.get_directory()
            walk(folder, path + [folder.get_name()])
        kind = items.next()
walk(tree.get_root_directory(), [])
print(json.dumps(found))
"""


def _mate_panel_entries(session: dekit.Session) -> list[dict[str, object]]:
    """Every entry of mate-panel's Applications menu."""
    return json.loads(session.run(["python3", "-I", "-c", MATE_PANEL_READER]).stdout)


# Cinnamon's menu applet: Cinnamon.AppSystem loads cinnamon-applications.menu (by that name) with
# CMenu, Cinnamon's fork of GMenu; the applet lists each directory under its directory's name.
CINNAMON_READER = """
import json, gi
gi.require_version("CMenu", "3.0")
from gi.repository import CMenu
tree = CMenu.Tree.new("cinnamon-applications.menu", CMenu.TreeFlags.NONE)
tree.load_sync()
found = []
def collect(directory, names):
    walker = directory.iter()
    kind = walker.next()
    while kind != CMenu.TreeItemType.INVALID:
        if kind == CMenu.TreeItemType.DIRECTORY:
            child = walker.get_directory()
            collect(child, names + [child.get_name()])
        elif kind == CMenu.TreeItemType.ENTRY:
            entry = walker.get_entry()
            found.append({"path": names, "id": entry.get_desktop_file_id(),
                          "command": entry.get_app_info().get_commandline()})
        kind = walker.next()
collect(tree.get_root_directory(), [])
print(json.dumps(found))
"""


def _cinnamon_entries(session: dekit.Session) -> list[dict[str, object]]:
    """Every entry of Cinnamon's application menu, as its menu applet reads it."""
    return json.loads(session.run(["python3", "-I", "-c", CINNAMON_READER]).stdout)


MENU_READERS: dict[str, Callable[[dekit.Session], list[dict[str, object]]]] = {
    desktops.MENU_BUDGIE: _budgie_entries,
    desktops.MENU_GARCON: _garcon_entries,
    desktops.MENU_KSYCOCA: _ksycoca_entries,
    desktops.MENU_LXQT_PANEL: _lxqt_panel_entries,
    desktops.MENU_GNOME_SHELL: _gnome_shell_entries,
    desktops.MENU_MATE_PANEL: _mate_panel_entries,
    desktops.MENU_CINNAMON: _cinnamon_entries,
}


# -- file-manager integration checks -----------------------------------------------------------


def _uca_actions(path: Path) -> dict[str, str]:
    """``uca.xml`` actions as {unique-id: command}."""
    root = ET.parse(path).getroot()
    return {
        (action.findtext("unique-id") or "").strip(): (action.findtext("command") or "").strip()
        for action in root.iter("action")
    }


def _check_thunar_uca(session: dekit.Session, installed: dekit.Installed) -> None:
    """Our action sits in the user's uca.xml next to the desktop's own (seeded from XDG_CONFIG_DIRS)."""
    user = _uca_actions(session.home / ".config" / "Thunar" / "uca.xml")
    seeds = [
        Path(base) / "Thunar" / "uca.xml" for base in session.env["XDG_CONFIG_DIRS"].split(":")
        if (Path(base) / "Thunar" / "uca.xml").is_file()
    ]
    assert seeds, "the desktop ships no Thunar/uca.xml to seed from"
    assert set(_uca_actions(seeds[0])) <= set(user), "the desktop's own Thunar actions were lost"
    assert OPEN_ACTION in user["fork-linux-open"]
    # Thunar ran while install.sh changed uca.xml: fork-linux says to restart it, and so we do.
    assert "thunar -q" in installed.output
    session.run(["thunar", "-q"], check=False)
    dekit.wait_for(lambda: not dekit.pids_of("thunar"), 30, "Thunar to quit")


DOLPHIN_SERVICEMENU_FILE = (".local", "share", "kio", "servicemenus", "fork-linux-open.desktop")


def _check_dolphin_servicemenu(session: dekit.Session, _installed: dekit.Installed) -> None:
    """Our service menu is where KIO reads the user's service menus, executable (KIO ignores a user
    service menu that is not), offered for folders, and runs ``open --from-file-manager``. Dolphin
    reads the service menus each time it opens a context menu: no restart."""
    path = session.home.joinpath(*DOLPHIN_SERVICEMENU_FILE)
    assert path.is_file(), "fork-linux added no Dolphin service menu"
    assert os.access(path, os.X_OK), "KIO ignores a service menu that is not executable"

    def read(group: str, key: str) -> str:
        argv = ["kreadconfig6", "--file", str(path), "--group", group, "--key", key]
        return session.run(argv).stdout.strip()

    assert "KonqPopupMenu/Plugin" in read("Desktop Entry", "X-KDE-ServiceTypes")
    assert "inode/directory" in read("Desktop Entry", "MimeType").split(";")
    actions = [name for name in read("Desktop Entry", "Actions").split(";") if name]
    assert any(OPEN_ACTION in read(f"Desktop Action {name}", "Exec") for name in actions), actions
    assert dekit.pids_of("dolphin"), "Dolphin's service (FileManager1) is not running"


FMA_ACTION = (".local", "share", "file-manager", "actions", "fork-linux-open.desktop")


def _check_fma_action(session: dekit.Session, _installed: dekit.Installed) -> None:
    """Our FileManager-Actions entry is where PCManFM-Qt (libfm-qt) and caja-actions read it, and it
    runs ``open --from-file-manager`` (they read actions when they build a menu: no restart)."""
    path = session.home.joinpath(*FMA_ACTION)
    assert path.is_file(), f"fork-linux added no 'Open in Fork' action for {session.desktop.file_manager}"
    action = configparser.RawConfigParser(strict=False, interpolation=None)  # keys lower-cased
    action.read(path, encoding="utf-8")
    assert action["Desktop Entry"]["type"] == "Action"
    profiles = [name for name in action["Desktop Entry"]["profiles"].split(";") if name]
    assert any(OPEN_ACTION in action[f"X-Action-Profile {name}"]["exec"] for name in profiles), profiles


NAUTILUS_EXTENSIONS = Path("/usr/lib/x86_64-linux-gnu/nautilus/extensions-4/libnautilus-python.so")


def _check_nautilus_extension(session: dekit.Session, installed: dekit.Installed) -> None:
    """Our nautilus-python extension is installed where python3-nautilus loads it from, and a user
    who has Files open while fork-linux installs it is told to restart Nautilus, as is done here."""
    assert NAUTILUS_EXTENSIONS.is_file(), "python3-nautilus is not installed"
    extension = session.home / ".local" / "share" / "nautilus-python" / "extensions" / "fork_linux_nautilus.py"
    assert "--from-file-manager" in extension.read_text(encoding="utf-8")
    # Nautilus loads extensions when it starts: with a window open, a new extension stays
    # invisible until `nautilus -q`. Whether Nautilus ran during install.sh is up to D-Bus
    # activation, so the install is done again with a Files window open.
    session.spawn("nautilus-home", ["nautilus", "--new-window", str(session.home)])
    session.wait_window(lambda w: w.has_class(session.desktop.file_manager_class), "a Files window")
    session.run(["fork-linux", "desktop", "remove"])
    again = session.run(["fork-linux", "desktop", "install"])
    assert "nautilus -q" in again.stdout + again.stderr, "no notice to restart the running Nautilus"
    session.run(["nautilus", "-q"], check=False)
    dekit.wait_for(lambda: not dekit.pids_of("nautilus"), 30, "Nautilus to quit")


NEMO_ACTION_FILE = (".local", "share", "nemo", "actions", "fork-linux-open.nemo_action")


def _check_nemo_action(session: dekit.Session, _installed: dekit.Installed) -> None:
    """Our Nemo action is where Nemo reads the user's actions, active, and runs
    ``open --from-file-manager`` (Nemo watches its action folders: no restart)."""
    path = session.home.joinpath(*NEMO_ACTION_FILE)
    assert path.is_file(), "fork-linux added no Nemo action"
    action = configparser.RawConfigParser(strict=False, interpolation=None)  # keys lower-cased
    action.read(path, encoding="utf-8")
    assert action["Nemo Action"]["active"] == "true"
    assert OPEN_ACTION in action["Nemo Action"]["exec"]

# caja-actions' menu plugin: Caja loads it from its extensions directory when it starts.
CAJA_ACTIONS_MENU = b"/caja/extensions-2.0/libcaja-actions-menu.so"


def _loaded(pid: int, library: bytes) -> bool:
    try:
        return library in Path("/proc", str(pid), "maps").read_bytes()
    except OSError:
        return False


def _check_caja_fma(session: dekit.Session, installed: dekit.Installed) -> None:
    """Our FileManager-Actions entry, and the running Caja (started before install.sh wrote it)
    has caja-actions' menu plugin, which watches the actions directories: no restart."""
    _check_fma_action(session, installed)
    cajas = dekit.pids_of("caja")
    assert cajas, "Caja is not running"
    assert any(_loaded(pid, CAJA_ACTIONS_MENU) for pid in cajas), "Caja did not load caja-actions"


INTEGRATION_CHECKS: dict[str, Callable[[dekit.Session, dekit.Installed], None]] = {
    desktops.NEMO_ACTION: _check_nemo_action,
    desktops.THUNAR_UCA: _check_thunar_uca,
    desktops.DOLPHIN_SERVICEMENU: _check_dolphin_servicemenu,
    desktops.PCMANFM_QT_FMA: _check_fma_action,
    desktops.NAUTILUS_EXTENSION: _check_nautilus_extension,
    desktops.CAJA_FMA: _check_caja_fma,
}


# -- the tests -----------------------------------------------------------------------------------


def test_install_from_the_release_tarball(session: dekit.Session, installed: dekit.Installed) -> None:
    version = (REPO / "VERSION").read_text(encoding="utf-8").strip()
    reported = session.run(["fork-linux", "--version"]).stdout
    assert version in reported
    status = json.loads(session.run(["fork-linux", "--json", "desktop", "status"]).stdout)
    assert status["ok"] is True, status
    kinds = {entry["kind"] for entry in status["entries"]}
    assert {"menu", "icon"} <= kinds
    assert installed.host_helper.is_file()


def test_menu_entry_is_valid_listed_and_starts_fork_linux(
    session: dekit.Session, desktop: desktops.Desktop, installed: dekit.Installed
) -> None:
    menu_file = installed.menu_file
    session.run(["desktop-file-validate", str(menu_file)])
    entries = [entry for entry in MENU_READERS[desktop.menu_reader](session) if entry["id"] == menu_file.name]
    assert entries, f"{desktop.menu_reader} does not list {menu_file.name}"
    assert entries[0]["path"] == list(desktop.menu_folder), entries
    assert str(entries[0]["command"]).startswith(str(session.home / ".local" / "bin" / "fork-linux"))
    session.spawn("menu-launch", [*desktop.menu_launcher, str(menu_file)])
    askers = _decline_consent(session)
    assert any(" run" in cmdline for cmdline in askers), askers


def test_file_manager_runs_open_in_fork(
    session: dekit.Session, desktop: desktops.Desktop, installed: dekit.Installed
) -> None:
    INTEGRATION_CHECKS[desktop.integration](session, installed)
    repo = _git_repo(session.root / "fm" / "fm-repo", session)
    session.spawn("file-manager-window", [desktop.file_manager, str(repo)])
    window = session.wait_window(
        lambda w: w.has_class(desktop.file_manager_class) and repo.name in w.name, f"a {desktop.file_manager} window"
    )
    session.context_action(
        window,
        desktop.open_in_fork_keys,
        lambda: next((shown for shown in session.windows() if _consent_window(shown)), None),
        "fork-linux's license question after the file manager's \"Open in Fork\"",
    )
    askers = _decline_consent(session)
    assert any(OPEN_ACTION in cmdline and str(repo) in cmdline for cmdline in askers), askers


def _theme_ids(desktop_key: str) -> list[str]:
    """The recipe's theme cases: light and dark, plus ``light:<how>`` / ``dark:<how>`` for other ways
    the desktop's own settings make it light or dark."""
    return list(desktops.DESKTOPS[desktop_key].themes)


@pytest.mark.parametrize("case_id", _theme_ids(os.environ.get("FL_E2E_DE", "")))
def test_theme_detection_follows_the_desktop(
    session: dekit.Session, desktop: desktops.Desktop, installed: dekit.Installed, case_id: str
) -> None:
    wanted = case_id.partition(":")[0]
    for command in (*desktop.reset, *desktop.themes[case_id]):
        session.run(command)
    code = "import os; from pathlib import Path; from fork_linux import theme; from fork_linux.procrun import Runner\n"
    code += "print(theme.detect(os.environ, Runner(), Path.home()))"
    assert _installed_python(session, installed, code) == wanted


def _scale_ids(desktop_key: str) -> list[str]:
    return [case.id for case in desktops.DESKTOPS[desktop_key].scales]


@pytest.mark.parametrize("case_id", _scale_ids(os.environ.get("FL_E2E_DE", "")))
def test_scale_detection_follows_the_desktop(
    session: dekit.Session, desktop: desktops.Desktop, installed: dekit.Installed, case_id: str
) -> None:
    case = next(item for item in desktop.scales if item.id == case_id)
    for command in (*desktop.reset, *case.commands):
        session.run(command)
    if desktop.scale_restarts_session:
        session.restart()  # read at login only
    else:
        time.sleep(1)  # the settings daemon republishes Xft.dpi
    code = "import os; from fork_linux import display; from fork_linux.procrun import Runner\n"
    code += "print(display.scale_percent(os.environ, Runner()))"
    try:
        assert _installed_python(session, installed, code) == str(case.expected)
    finally:
        for command in desktop.reset:
            session.run(command)
        if desktop.scale_restarts_session and case.commands:
            session.restart()


def test_terminal_opens_in_the_directory(
    session: dekit.Session, desktop: desktops.Desktop, installed: dekit.Installed
) -> None:
    target = _git_repo(session.root / "term" / "term-repo", session)
    # A terminal window an earlier test left open (the file manager's own "Open in Terminal") is not the answer.
    before = {window.id for window in session.windows()}
    session.run([str(installed.host_helper), "terminal", str(target)])
    window = session.wait_window(
        lambda w: w.id not in before and w.has_class(desktop.terminal_class), f"a new {desktop.terminal} window"
    )
    assert window.pid is not None
    dekit.wait_for(
        lambda: str(target) in dekit.children_cwds(window.pid or 0), 30, f"a shell of {desktop.terminal} in {target}"
    )
    # The window's process is the terminal (gnome-terminal's windows belong to gnome-terminal-server).
    program = os.path.basename(os.readlink(f"/proc/{window.pid}/exe"))
    assert dekit.pids_of(desktop.terminal) or program.startswith(desktop.terminal), (
        f"{desktop.terminal} is not the terminal that opened ({program})"
    )
    session.screenshot("terminal", window)


def test_reveal_shows_the_file_in_the_file_manager(
    session: dekit.Session, desktop: desktops.Desktop, installed: dekit.Installed
) -> None:
    folder = session.root / "reveal" / "reveal-here"
    item = folder / "item.txt"
    _write(item, "reveal me\n")
    if desktop.provides_file_manager1:
        assert session.name_has_owner("org.freedesktop.FileManager1") or session.name_activatable(
            "org.freedesktop.FileManager1"
        )
    session.run([str(installed.host_helper), "reveal", str(item)])
    window = session.wait_window(
        lambda w: w.has_class(desktop.file_manager_class) and folder.name in w.name, "the file manager showing it"
    )
    session.screenshot("reveal", window)


def test_open_file_reaches_the_default_application(
    session: dekit.Session, installed: dekit.Installed, stub: Path
) -> None:
    document = session.root / "open" / f"document{STUB_SUFFIX}"
    _write(document, "open me\n")
    session.run([str(installed.host_helper), "open", str(document)])
    dekit.wait_for(lambda: _stub_called_with(stub, str(document)), 30, f"the default application to get {document}")


def test_open_url_reaches_the_web_browser(session: dekit.Session, installed: dekit.Installed, stub: Path) -> None:
    session.run([str(installed.host_helper), "open", STUB_URL])
    dekit.wait_for(lambda: _stub_called_with(stub, STUB_URL), 30, "the web browser to get the link")


def test_doctor_fails_only_for_what_setup_installs(session: dekit.Session, installed: dekit.Installed) -> None:
    result = session.run(["fork-linux", "doctor", "--json"], check=False)
    report = json.loads(result.stdout)
    failed = {check["id"] for check in report["checks"] if check["status"] == "fail"}
    warned = {check["id"] for check in report["checks"] if check["status"] == "warn"}
    assert failed == NOT_SET_UP, report
    assert warned == set(), report
