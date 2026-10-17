"""Host IDEs that Fork's "Open in" menu should offer under Wine.

Fork for Windows lists an editor only when its executable exists at one of a
few fixed Windows paths (Fork 2.23.2, traced with ``WINEDEBUG=+file`` while the
toolbar's "Open in" menu opened): ``%LOCALAPPDATA%\\Programs\\<App>\\<App>.exe``
for VS Code, Cursor, Zed, Fleet, Antigravity, CodeBuddy; ``Programs\\<IDE>\\bin\\
<ide>64.exe`` for the JetBrains IDEs; ``%LOCALAPPDATA%\\atom\\atom.exe``;
``C:\\Program Files\\Sublime Text\\sublime_text.exe``. Under Wine those installs do
not exist, so fork-linux copies ``fl-launch.exe`` there. Each copy's basename
makes ``fl-launch`` call ``fork-linux-host run <id> …`` (detached) with the
repository or file path Fork passed.

Only editors found on the host are installed: on ``PATH`` (snaps included),
as Flatpak apps (``exports/bin``) or as JetBrains Toolbox scripts. Removing
one drops its stubs on the next ``host_integration`` run. Visual Studio (also
in Fork's menu) has no Linux build and is never offered.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any

from . import fsutil
from .paths import Paths

# Stub roots: %LOCALAPPDATA%\Programs, %LOCALAPPDATA% and C:\Program Files.
PROGRAMS = "programs"
LOCAL = "local"
PROGRAM_FILES = "program_files"


@dataclass(frozen=True)
class Editor:
    """One entry of Fork's Open in menu and the host programs that provide it."""

    id: str
    label: str
    stubs: tuple[tuple[str, str], ...]
    commands: tuple[str, ...]

    @property
    def win_names(self) -> tuple[str, ...]:
        """The stub basenames (``fl-launch.exe`` maps each to :attr:`id`)."""
        return tuple(dict.fromkeys(PureWindowsPath(rel).name for _root, rel in self.stubs))


def _jetbrains(editor_id: str, label: str, folder: str, exe: str, commands: tuple[str, ...]) -> Editor:
    return Editor(editor_id, label, ((PROGRAMS, rf"{folder}\bin\{exe}"),), commands)


# id (allowlisted by fork_linux.host_helper.Host.run), Fork's label, where Fork
# looks, host commands (PATH names, Flatpak app ids, Toolbox script names) in
# order of preference. Keep the basenames in sync with bridge/win/fl_launch.c.
EDITORS: tuple[Editor, ...] = (
    Editor("code", "Visual Studio Code", ((PROGRAMS, r"Microsoft VS Code\Code.exe"),),
           ("code", "com.visualstudio.code")),
    Editor("code-insiders", "Visual Studio Code Insiders",
           ((PROGRAMS, r"Microsoft VS Code Insiders\Code - Insiders.exe"),), ("code-insiders",)),
    Editor("cursor", "Cursor", ((PROGRAMS, r"cursor\Cursor.exe"),), ("cursor",)),
    _jetbrains("idea", "IntelliJ IDEA", "IntelliJ IDEA", "idea64.exe",
               ("idea", "intellij-idea", "intellij-idea-ultimate", "intellij-idea-community",
                "com.jetbrains.IntelliJ-IDEA-Ultimate", "com.jetbrains.IntelliJ-IDEA-Community")),
    _jetbrains("pycharm", "PyCharm", "PyCharm", "pycharm64.exe",
               ("pycharm", "pycharm-professional", "pycharm-community",
                "com.jetbrains.PyCharm-Professional", "com.jetbrains.PyCharm-Community")),
    _jetbrains("webstorm", "WebStorm", "WebStorm", "webstorm64.exe", ("webstorm", "com.jetbrains.WebStorm")),
    _jetbrains("phpstorm", "PhpStorm", "PhpStorm", "phpstorm64.exe", ("phpstorm", "com.jetbrains.PhpStorm")),
    _jetbrains("rider", "Rider", "Rider", "rider64.exe", ("rider", "com.jetbrains.Rider")),
    _jetbrains("goland", "GoLand", "GoLand", "goland64.exe", ("goland", "com.jetbrains.GoLand")),
    Editor("fleet", "Fleet", ((PROGRAMS, r"Fleet\Fleet.exe"),), ("fleet",)),
    Editor("sublime", "Sublime Text", ((PROGRAM_FILES, r"Sublime Text\sublime_text.exe"),),
           ("subl", "sublime-text.subl", "sublime_text", "com.sublimetext.three")),
    Editor("atom", "Atom", ((LOCAL, r"atom\atom.exe"),), ("atom",)),
    Editor("zed", "Zed", ((PROGRAMS, r"Zed\Zed.exe"),), ("zed", "zeditor", "zedit", "dev.zed.Zed")),
    Editor("antigravity", "Antigravity", ((PROGRAMS, r"Antigravity\Antigravity.exe"),),
           ("antigravity", "com.google.Antigravity")),
    Editor("antigravity-ide", "Antigravity IDE", ((PROGRAMS, r"Antigravity IDE\Antigravity IDE.exe"),),
           ("antigravity-ide", "com.google.AntigravityIDE")),
    Editor("codebuddy", "CodeBuddy", ((PROGRAMS, r"CodeBuddy\CodeBuddy.exe"),), ("codebuddy",)),
)

# Stubs earlier releases wrote where Fork never looks; removed on the next run.
LEGACY_STUBS: tuple[tuple[str, str], ...] = (
    (PROGRAMS, r"IntelliJ IDEA\idea64.exe"), (PROGRAMS, r"IntelliJ IDEA\idea.exe"),
    (PROGRAMS, r"PyCharm\pycharm64.exe"), (PROGRAMS, r"PyCharm\pycharm.exe"),
    (PROGRAMS, r"WebStorm\webstorm64.exe"), (PROGRAMS, r"WebStorm\webstorm.exe"),
    (PROGRAMS, r"PhpStorm\phpstorm64.exe"), (PROGRAMS, r"PhpStorm\phpstorm.exe"),
    (PROGRAMS, r"Rider\rider64.exe"), (PROGRAMS, r"Rider\rider.exe"),
    (PROGRAMS, r"Android Studio\studio64.exe"), (PROGRAMS, r"Android Studio\studio.exe"),
    (PROGRAMS, r"Sublime Text\sublime_text.exe"), (PROGRAMS, r"Sublime Text\subl.exe"),
    (PROGRAMS, r"atom\atom.exe"),
)
LEGACY_NAMES = ("idea.exe", "pycharm.exe", "webstorm.exe", "phpstorm.exe", "rider.exe", "studio64.exe",
                "studio.exe", "subl.exe")

# Allowlist for ``fork-linux-host run`` (ids above only).
ALLOWED_IDS = frozenset(item.id for item in EDITORS)

# System-wide launcher folders searched after PATH (Flatpak's system installation).
SYSTEM_DIRS: tuple[str, ...] = ("/var/lib/flatpak/exports/bin",)

EDITORS_DIR = ("editors",)
SHIM_MODE = 0o755
SHIM_DIR_MODE = 0o755


@dataclass(frozen=True)
class FoundEditor:
    """A host IDE that is installed and should appear in Fork's Open in menu."""

    id: str
    command: str
    win_names: tuple[str, ...]
    stubs: tuple[tuple[str, str], ...] = ()
    label: str = ""


def _which(name: str, dirs: Sequence[str]) -> str | None:
    """Absolute path of ``name`` in ``dirs``, or None.

    The path is kept as found, not resolved: ``/snap/bin/code`` is a symlink to
    ``/usr/bin/snap``, which picks the app from ``argv[0]``.
    """
    for directory in dirs:
        if not directory:
            continue
        candidate = Path(directory) / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate.absolute())
    return None


def search_dirs(env: Mapping[str, str] | None) -> list[str]:
    """``PATH``, then Flatpak's exported launchers and the JetBrains Toolbox scripts."""
    env = env or {}
    dirs = (env.get("PATH") or os.defpath).split(os.pathsep)
    home = env.get("HOME", "")
    data = env.get("XDG_DATA_HOME") or (os.path.join(home, ".local", "share") if home else "")
    if data:
        dirs.append(os.path.join(data, "flatpak", "exports", "bin"))
    dirs += SYSTEM_DIRS
    if data:
        dirs.append(os.path.join(data, "JetBrains", "Toolbox", "scripts"))
    return dirs


def discover(env: Mapping[str, str] | None = None) -> list[FoundEditor]:
    """Host editors that are installed (one entry per id, first matching command)."""
    dirs = search_dirs(env)
    found: list[FoundEditor] = []
    for editor in EDITORS:
        for command in editor.commands:
            resolved = _which(command, dirs)
            if resolved is not None:
                found.append(FoundEditor(editor.id, resolved, editor.win_names, editor.stubs, editor.label))
                break
    return found


def editors_win_dir(paths: Paths) -> Path:
    """``C:\\fork-linux\\editors`` in the prefix."""
    return paths.fork_linux_win_dir.joinpath(*EDITORS_DIR)


def programs_root(paths: Paths, user: str) -> Path:
    """``C:\\users\\<user>\\AppData\\Local\\Programs``."""
    return local_root(paths, user) / "Programs"


def local_root(paths: Paths, user: str) -> Path:
    """``C:\\users\\<user>\\AppData\\Local``."""
    return paths.prefix / "drive_c" / "users" / user / "AppData" / "Local"


def stub_path(paths: Paths, user: str, stub: tuple[str, str]) -> Path:
    """The Unix path of one ``(root, Windows relative path)`` stub location."""
    root, rel = stub
    base = {
        PROGRAMS: programs_root(paths, user),
        LOCAL: local_root(paths, user),
        PROGRAM_FILES: paths.prefix / "drive_c" / "Program Files",
    }[root]
    return base.joinpath(*PureWindowsPath(rel).parts)


def stub_targets(paths: Paths, user: str, found: Sequence[FoundEditor]) -> list[Path]:
    """Every Unix path that should hold a copy of ``fl-launch.exe`` for ``found``."""
    targets: list[Path] = []
    root = editors_win_dir(paths)
    for editor in found:
        targets += [root / name for name in editor.win_names]
        targets += [stub_path(paths, user, stub) for stub in editor.stubs]
    return targets


def install(paths: Paths, user: str, launch: Path, found: Sequence[FoundEditor]) -> list[str]:
    """Copy ``launch`` (``fl-launch.exe``) to every stub path for ``found``; return installed ids.

    Stubs of editors that are no longer installed, and stubs earlier releases
    wrote where Fork never looks, are removed (empty folders we made go too).
    """
    targets = stub_targets(paths, user, found)
    fsutil.ensure_dir(editors_win_dir(paths), mode=SHIM_DIR_MODE)
    payload = launch.read_bytes()
    for target in targets:
        fsutil.ensure_dir(target.parent, mode=SHIM_DIR_MODE)
        fsutil.atomic_write(target, payload, mode=SHIM_MODE)
    _remove_stale(paths, user, {path.absolute() for path in targets})
    return [editor.id for editor in found]


def _known_stubs(paths: Paths, user: str) -> list[Path]:
    """Every stub path any release may have written."""
    root = editors_win_dir(paths)
    known = [root / name for editor in EDITORS for name in editor.win_names]
    known += [root / name for name in LEGACY_NAMES]
    known += [stub_path(paths, user, stub) for editor in EDITORS for stub in editor.stubs]
    known += [stub_path(paths, user, stub) for stub in LEGACY_STUBS]
    return known


def _remove_stale(paths: Paths, user: str, wanted: set[Path]) -> None:
    """Delete stubs we wrote that are no longer wanted, then the folders that became empty."""
    stops = {editors_win_dir(paths), programs_root(paths, user), local_root(paths, user),
             paths.prefix / "drive_c" / "Program Files"}
    for path in _known_stubs(paths, user):
        if path.absolute() in wanted or path.is_symlink() or not path.is_file():
            continue
        path.unlink()
        parent = path.parent
        while parent not in stops and parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
            parent = parent.parent


def app_path_entries(paths: Paths, found: Sequence[FoundEditor]) -> list[tuple[str, str]]:
    """``(Windows exe name, Windows path of stub)`` for App Paths registration."""
    root_win = "C:\\fork-linux\\editors"
    entries: list[tuple[str, str]] = []
    for editor in found:
        for name in editor.win_names:
            entries.append((name, f"{root_win}\\{name}"))
    return entries


def inputs_signature(found: Sequence[FoundEditor]) -> dict[str, Any]:
    """Stable inputs for the ``host_integration`` step marker."""
    return {"editors": [{"id": e.id, "command": e.command, "names": list(e.win_names),
                         "stubs": [list(stub) for stub in e.stubs]} for e in found]}
