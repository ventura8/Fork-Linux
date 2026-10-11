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


MENU_READERS: dict[str, Callable[[dekit.Session], list[dict[str, object]]]] = {
    desktops.MENU_GARCON: _garcon_entries,
    desktops.MENU_KSYCOCA: _ksycoca_entries,
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


INTEGRATION_CHECKS: dict[str, Callable[[dekit.Session, dekit.Installed], None]] = {
    desktops.THUNAR_UCA: _check_thunar_uca,
    desktops.DOLPHIN_SERVICEMENU: _check_dolphin_servicemenu,
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
    assert entries[0]["path"] == ["Development"], entries
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
    session.activate(window)
    time.sleep(1)
    session.keys(desktop.open_in_fork_keys)
    askers = _decline_consent(session)
    assert any(OPEN_ACTION in cmdline and str(repo) in cmdline for cmdline in askers), askers


@pytest.mark.parametrize("wanted", [desktops.LIGHT, desktops.DARK])
def test_theme_detection_follows_the_desktop(
    session: dekit.Session, desktop: desktops.Desktop, installed: dekit.Installed, wanted: str
) -> None:
    for command in (*desktop.reset, *desktop.themes[wanted]):
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
    time.sleep(1)  # the settings daemon republishes Xft.dpi
    code = "import os; from fork_linux import display; from fork_linux.procrun import Runner\n"
    code += "print(display.scale_percent(os.environ, Runner()))"
    try:
        assert _installed_python(session, installed, code) == str(case.expected)
    finally:
        for command in desktop.reset:
            session.run(command)


def test_terminal_opens_in_the_directory(
    session: dekit.Session, desktop: desktops.Desktop, installed: dekit.Installed
) -> None:
    target = _git_repo(session.root / "term" / "term-repo", session)
    session.run([str(installed.host_helper), "terminal", str(target)])
    window = session.wait_window(lambda w: w.has_class(desktop.terminal_class), f"a {desktop.terminal} window")
    assert window.pid is not None
    dekit.wait_for(
        lambda: str(target) in dekit.children_cwds(window.pid or 0), 30, f"a shell of {desktop.terminal} in {target}"
    )
    assert dekit.pids_of(desktop.terminal), f"{desktop.terminal} is not the terminal that opened"
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
