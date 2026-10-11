"""Fork talking to the Linux desktop: Help links, Show in / Open In File Explorer, Open, the
terminal, Preferences, the ``H:`` drive, Fork's settings from the CLI, the menu entry with
Fork's icon and the file-manager actions.

The container has no desktop, so the desktop's programs are stand-ins on ``PATH`` that record
how they were called (``xdg-open``, ``gdbus`` answering for the portal and FileManager1, a
terminal): what is checked is that a click in Fork reaches the right Linux program with the
right path or URL. Gated by ``FL_E2E_FORK=1``; containers only.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fork_driver import fork_pids, git, make_repo, wait_until, windows_path
from fork_ui import ForkUI

from fixtures import real_tier

if os.environ.get("FL_E2E_FORK") != "1":
    pytest.skip("requires FL_E2E_FORK=1 (the official Fork under Wine)", allow_module_level=True)
real_tier.enforce()

APP_ID = "io.github.ventura8.ForkLinux"
STUBS = ("xdg-open", "gdbus", "e2e-terminal")
# Offsets inside popups and dialogs (from their top-left corner).
HELP_MENU = (220, 16)
HELP_WEBSITE = 78
HELP_ISSUES = 111
FILE_MENU_OPEN = 24
FILE_MENU_SHOW = 47
PREFS_SHOW_MARKS = (189, 326)
CREATE_PATCH_SAVE = (475, 205)


def _stub_dir(e2e: Any) -> tuple[Path, Path]:
    """Stand-ins for the desktop's programs; each appends ``name<TAB>args<TAB>cwd`` to calls.log."""
    stubs = e2e.root / "stubs"
    stubs.mkdir(exist_ok=True)
    log = e2e.root / "stub-calls.log"
    for name in STUBS:
        script = stubs / name
        script.write_text(
            "#!/bin/sh\n"
            f'printf "%s\\t%s\\t%s\\n" "{name}" "$*" "$(pwd)" >>"{log}"\n'
            "exit 0\n",
            encoding="utf-8",
        )
        script.chmod(0o755)
    return stubs, log


def _calls(log: Path, name: str) -> list[tuple[str, str]]:
    """(arguments, working directory) of every recorded call of ``name``."""
    if not log.is_file():
        return []
    found = []
    for line in log.read_text(encoding="utf-8").splitlines():
        parts = line.split("\t")
        if len(parts) == 3 and parts[0] == name:
            found.append((parts[1], parts[2]))
    return found


@pytest.fixture(scope="module")
def desk(installed: Any) -> Iterator[tuple[ForkUI, Path, dict[str, str]]]:
    """Fork started with the stand-ins first on PATH and the stub terminal configured."""
    stubs, log = _stub_dir(installed)
    extra = {
        "PATH": f"{stubs}:{installed.env['PATH']}",
        "FORK_LINUX_TERMINAL": f"{stubs / 'e2e-terminal'} {{dir}}",
        "FORK_LINUX_HANDOFF": "direct",
    }
    installed.fork.ensure_closed()
    installed.fork.forget_tabs()
    ui = ForkUI(installed.fork)
    ui.fork.start(make_repo(installed.repos / "desk-first", {".gitignore": "*.tmp\n"}), extra=extra)
    yield ui, log, extra
    installed.fork.ensure_closed()


def _repo(e2e: Any, name: str) -> Path:
    path = make_repo(e2e.repos / name, {".gitignore": "*.tmp\n", "notes.txt": "notes\n", "src/main.c": "int x;\n"})
    (path / "notes.txt").write_text("notes changed\n", encoding="utf-8")
    return path


def _popup_item(ui: ForkUI, opener: Any, from_top: int) -> None:
    """Open a popup menu with ``opener()`` and click the item ``from_top`` px below its top."""
    before = ui.screen.windows()
    opener()
    menu = ui.screen.new_window(before, timeout=10)
    time.sleep(0.4)
    ui.screen.click(menu.x + 60, menu.y + from_top, settle=1.0)


def test_help_links_open_in_the_linux_browser(desk: Any, installed: Any) -> None:
    """Help > Visit Fork Website and Issue Tracker reach the desktop's URL handler (the portal).

    Scenarios: I-HELP-LINKS
    """
    ui, log, _extra = desk
    window = ui.open(_repo(installed, "help-links"))
    _popup_item(ui, lambda: ui.screen.click_in(window, *HELP_MENU), HELP_WEBSITE)
    wait_until(lambda: _opened_urls(log), 60, "the website link")
    _popup_item(ui, lambda: ui.screen.click_in(window, *HELP_MENU), HELP_ISSUES)
    urls = wait_until(lambda: len(_opened_urls(log)) >= 2 and _opened_urls(log), 60, "the issue tracker link")
    assert urls[0].startswith("https://fork.dev"), urls
    assert urls[1].startswith("https://github.com/"), urls


def _opened_urls(log: Path) -> list[str]:
    """URLs handed to the desktop portal's OpenURI (in call order)."""
    return [word for args, _cwd in _calls(log, "gdbus") if "portal.OpenURI.OpenURI" in args
            for word in args.split() if word.startswith("https://")]


def test_open_in_file_explorer_uses_the_linux_file_manager(desk: Any, installed: Any) -> None:
    """Repository > Open In File Explorer (Ctrl+Alt+O) opens the folder in the Linux file manager.

    Scenarios: I-OPEN-IN-EXPLORER
    """
    ui, log, _extra = desk
    repo = _repo(installed, "open-explorer")
    ui.open(repo)
    ui.keys("ctrl+alt+o", settle=2.0)
    wait_until(lambda: any(args.rstrip("/") == str(repo) for args, _ in _calls(log, "xdg-open")), 60,
               "xdg-open on the repository folder")


def test_show_in_file_explorer_selects_the_file(desk: Any, installed: Any) -> None:
    """A file's "Show in File Explorer" selects it in the Linux file manager (FileManager1).

    Scenarios: I-SHOW-IN-EXPLORER
    """
    ui, log, _extra = desk
    repo = _repo(installed, "show-explorer")
    ui.open(repo)
    ui.local_changes()
    ui.click(ui.unstaged_row(0))
    _popup_item(ui, lambda: ui.click(ui.unstaged_row(0), button=3), FILE_MENU_SHOW)
    uri = f"file://{repo / 'notes.txt'}"
    wait_until(lambda: any("FileManager1.ShowItems" in args and uri in args for args, _ in _calls(log, "gdbus")),
               60, "FileManager1.ShowItems for notes.txt")


def test_open_file_uses_the_linux_default_app(desk: Any, installed: Any) -> None:
    """A file's "Open" hands it to the Linux default application (xdg-open), not a Wine program.

    Scenarios: I-OPEN-FILE
    """
    ui, log, _extra = desk
    repo = _repo(installed, "open-file")
    ui.open(repo)
    ui.local_changes()
    ui.click(ui.unstaged_row(0))
    _popup_item(ui, lambda: ui.click(ui.unstaged_row(0), button=3), FILE_MENU_OPEN)
    wait_until(lambda: any(str(repo / "notes.txt") in args for args, _ in _calls(log, "xdg-open")), 60,
               "xdg-open on notes.txt")


def test_open_in_shell_runs_the_linux_terminal(desk: Any, installed: Any) -> None:
    """Repository > Open In Shell (Ctrl+Alt+T) and the Shell button open the Linux terminal there.

    Scenarios: I-CONSOLE-TERMINAL
    """
    ui, log, _extra = desk
    repo = _repo(installed, "shell")
    ui.open(repo)
    ui.keys("ctrl+alt+t", settle=2.0)
    wait_until(lambda: any(args == str(repo) or cwd == str(repo) for args, cwd in _calls(log, "e2e-terminal")),
               60, "the terminal in the repository")


def test_home_drive_in_a_file_dialog(desk: Any, installed: Any) -> None:
    """Fork's file dialogs reach the Linux home as ``H:``: Save as Patch to ``H:\\patches\\...``.

    Scenarios: I-HOME-DRIVE
    """
    ui, _log, _extra = desk
    repo = _repo(installed, "home-drive")
    (installed.home / "patches").mkdir(exist_ok=True)
    ui.open(repo)
    ui.all_commits()
    ui.commit_menu(0, "save_patch")
    dialog = ui.wait_dialog("Create Patch")
    ui.screen.click_in(dialog, *CREATE_PATCH_SAVE, settle=2.0)
    ui.screen.screenshot("save-patch-dialog")
    ui.screen.key("ctrl+a")
    ui.screen.type("H:\\patches\\initial.patch")
    ui.screen.key("Return", settle=2.0)
    patch = installed.home / "patches" / "initial.patch"
    wait_until(lambda: patch.is_file() and patch.stat().st_size > 0, 60, "the patch in ~/patches")
    assert git(repo, "rev-parse", "HEAD")[:7] in patch.read_text(encoding="utf-8", errors="replace")


def test_preferences_survive_a_restart(desk: Any, installed: Any) -> None:
    """A change in File > Preferences is kept by Fork across a restart.

    Scenarios: I-PREFERENCES-PERSIST
    """
    ui, _log, extra = desk
    repo = _repo(installed, "prefs")
    ui.open(repo)
    before = ui.fork.settings().get("DiffShowChangeMarks", False)
    ui.keys("ctrl+comma", settle=2.0)
    prefs = ui.wait_dialog("Preferences")
    ui.screen.click_in(prefs, *PREFS_SHOW_MARKS)
    ui.screen.screenshot("preferences-changed", prefs)
    ui.screen.key("Escape", settle=1.0)
    ui.dismiss_dialogs()
    ui.fork.close()
    assert ui.fork.settings()["DiffShowChangeMarks"] is (not before)
    ui.fork.start(repo, extra=extra)
    assert ui.fork.settings()["DiffShowChangeMarks"] is (not before)


def test_fork_settings_from_the_command_line(desk: Any, installed: Any) -> None:
    """``settings set`` refuses while Fork runs and works when it is closed, with a backup to restore.

    Scenarios: I-SETTINGS-CLI
    """
    ui, _log, extra = desk
    refused = installed.cli("settings", "set", "CommitSubjectLowLimit", "40", check=False)
    assert refused.returncode == 15, refused.stderr
    ui.fork.close()
    installed.cli("settings", "set", "CommitSubjectLowLimit", "40")
    assert json.loads(installed.cli("settings", "get", "CommitSubjectLowLimit").stdout) == 40
    installed.cli("settings", "restore")
    assert json.loads(installed.cli("settings", "get", "CommitSubjectLowLimit").stdout) != 40
    installed.cli("settings", "set", "DisableHardwareAcceleration", "false")
    installed.cli("settings", "apply-defaults")
    assert json.loads(installed.cli("settings", "get", "DisableHardwareAcceleration").stdout) is True
    ui.fork.start(_repo(installed, "settings-cli"), extra=extra)


def _desktop_exec(entry: Path) -> list[str]:
    exec_line = next(line for line in entry.read_text(encoding="utf-8").splitlines() if line.startswith("Exec="))
    return [word for word in shlex.split(exec_line[len("Exec="):]) if not word.startswith("%")]


def test_menu_entry_with_forks_icon_starts_fork(desk: Any, installed: Any) -> None:
    """``desktop install``: the menu entry starts Fork; Fork's own icon is there at every size.

    Scenarios: I-DESKTOP-MENU
    """
    ui, _log, extra = desk
    installed.cli("desktop", "install", "--file-managers", "none")
    applications = installed.home / ".local" / "share" / "applications"
    entry = applications / f"{APP_ID}.desktop"
    assert entry.is_file()
    assert "(unofficial)" in entry.read_text(encoding="utf-8")
    icons = sorted((installed.home / ".local" / "share" / "icons" / "hicolor").glob(f"*/apps/{APP_ID}.png"))
    sizes = {int(path.parent.parent.name.split("x")[0]) for path in icons}
    assert {16, 32, 48, 64, 128, 256} <= sizes, sizes
    assert all(path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n" for path in icons)
    assert (installed.home / ".local" / "share" / "icons" / "hicolor" / "scalable" / "apps" / f"{APP_ID}.svg").is_file()
    ui.fork.close()
    subprocess.Popen(_desktop_exec(entry), env={**installed.env, **extra}, cwd=str(installed.home),
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
    assert ui.fork.wait_main(timeout=180).is_main


def _action_commands(e2e: Any, target: Path) -> dict[str, list[str]]:
    """The command each installed file-manager action runs for ``target``."""
    share = e2e.home / ".local" / "share"
    commands: dict[str, list[str]] = {}
    for kind, pattern in (("nemo", "nemo/actions/*.nemo_action"), ("dolphin", "kio/servicemenus/*.desktop"),
                          ("fma", "file-manager/actions/*.desktop")):
        for path in sorted(share.glob(pattern)):
            text = path.read_text(encoding="utf-8")
            line = next(item for item in text.splitlines() if item.startswith("Exec="))
            commands[kind] = [str(target) if word in ("%f", "%F") else word
                              for word in shlex.split(line[len("Exec="):])]
    uca = e2e.home / ".config" / "Thunar" / "uca.xml"
    match = re.search(r"<command>([^<]*--from-file-manager[^<]*)</command>", uca.read_text(encoding="utf-8"))
    assert match, "no Thunar action"
    commands["thunar"] = [str(target) if word == "%f" else word for word in shlex.split(match.group(1))]
    scripts = sorted((share / "nautilus" / "scripts").iterdir())
    assert scripts, "no Nautilus script"
    commands["nautilus"] = [str(scripts[0]), str(target)]
    return commands


def test_file_manager_actions_open_the_files_repository(desk: Any, installed: Any) -> None:
    """``desktop install --file-managers all``: every "Open in Fork" action opens the file's repository.

    Scenarios: I-FILE-MANAGER-ACTIONS
    """
    ui, _log, extra = desk
    installed.cli("desktop", "install", "--file-managers", "all")
    if not ui.fork.main_window():
        ui.fork.start(_repo(installed, "actions-first"), extra=extra)
    opened = []
    for kind in ("nemo", "dolphin", "fma", "thunar", "nautilus"):
        repo = _repo(installed, f"action-{kind}")
        command = _action_commands(installed, repo / "src" / "main.c")[kind]
        result = subprocess.run(command, env={**installed.env, **extra}, cwd=str(installed.home),
                                capture_output=True, text=True, timeout=60, check=False)
        assert result.returncode == 0, f"{kind}: {result.stderr}"
        opened.append(repo)
        time.sleep(2)
    assert len(fork_pids(installed.prefix)) == 1
    ui.fork.close()
    workspaces = installed.fork.settings()["Workspaces"]["All"]
    known = {repo for workspace in workspaces for repo in workspace["Repositories"]}
    missing = [repo.name for repo in opened if windows_path(repo) not in known]
    assert not missing, f"not opened in Fork: {missing}"
    ui.fork.start(opened[0], extra=extra)


def test_desktop_remove_deletes_exactly_what_was_installed(desk: Any, installed: Any) -> None:
    """``desktop remove`` deletes the menu entry, icons and actions it installed (and the folders it had to
    create for them), nothing else.

    Scenarios: I-DESKTOP-REMOVE
    """
    _ui, _log, _extra = desk
    share = installed.home / ".local" / "share"
    keep = share / "applications" / "someone-else.desktop"
    keep.parent.mkdir(parents=True, exist_ok=True)
    keep.write_text("[Desktop Entry]\nType=Application\nName=Other\nExec=true\n", encoding="utf-8")
    installed.cli("desktop", "remove")
    folders = _folders(installed)
    installed.cli("desktop", "install", "--file-managers", "all")
    before = json.loads(installed.cli("desktop", "status", "--json").stdout)
    files = [Path(entry["path"]) for entry in before["entries"]]
    assert before["installed"] is True and before["ok"] is True
    assert {entry["kind"] for entry in before["entries"]} >= {"menu", "icon"}, before["entries"]
    assert all(path.exists() for path in files)
    installed.cli("desktop", "remove")
    after = json.loads(installed.cli("desktop", "status", "--json").stdout)
    assert after["installed"] is False and after["entries"] == []
    assert not [path for path in files if path.exists()], "desktop remove left files behind"
    assert keep.is_file()
    assert sorted(_folders(installed) - folders) == [], "desktop remove left the folders it created behind"


def _folders(e2e: Any) -> set[Path]:
    """The folders under the data and config homes, without fork-linux's own (Fork, the prefix)."""
    roots = (e2e.home / ".local" / "share", e2e.home / ".config", e2e.home / ".local" / "bin")
    return {path for root in roots if root.is_dir() for path in root.rglob("*")
            if path.is_dir() and path.relative_to(root).parts[0] != "fork-linux"}
