"""Host IDEs that Fork's "Open in" menu should offer under Wine.

Fork for Windows looks for known editors (VS Code, Cursor, IntelliJ, …) by
their Windows names. Under Wine those installs do not exist, so fork-linux
copies ``fl-launch.exe`` to the names Fork expects and points App Paths at
them. Each copy's basename makes ``fl-launch`` call ``fork-linux-host run
<id> …`` (detached) with the repository or file path Fork passed.

Only editors found on the host (PATH) are installed; removing one from PATH
drops its stubs on the next ``host_integration`` run.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import fsutil
from .bridge import LAUNCH
from .paths import Paths

# Windows basenames Fork's Open-in list recognises (Fork.exe strings), mapped to
# a short host id that :func:`fork_linux.host_helper.Host.run` allowlists.
EDITORS: tuple[tuple[str, tuple[str, ...], tuple[str, ...], str], ...] = (
    # id, Windows stub names, host commands to try, Local\\Programs subfolder
    ("code", ("Code.exe",), ("code",), "Microsoft VS Code"),
    ("code-insiders", ("Code - Insiders.exe",), ("code-insiders",), "Microsoft VS Code Insiders"),
    ("cursor", ("Cursor.exe",), ("cursor",), "cursor"),
    ("idea", ("idea64.exe", "idea.exe"), ("intellij-idea", "idea"), "IntelliJ IDEA"),
    ("pycharm", ("pycharm64.exe", "pycharm.exe"), ("pycharm", "pycharm-community", "pycharm-professional"), "PyCharm"),
    ("webstorm", ("webstorm64.exe", "webstorm.exe"), ("webstorm",), "WebStorm"),
    ("phpstorm", ("phpstorm64.exe", "phpstorm.exe"), ("phpstorm",), "PhpStorm"),
    ("rider", ("rider64.exe", "rider.exe"), ("rider",), "Rider"),
    ("android-studio", ("studio64.exe", "studio.exe"), ("android-studio",), "Android Studio"),
    ("sublime", ("sublime_text.exe", "subl.exe"), ("subl", "sublime-text.subl", "sublime_text"), "Sublime Text"),
    ("atom", ("atom.exe",), ("atom",), "atom"),
)

# Allowlist for ``fork-linux-host run`` (ids above only).
ALLOWED_IDS = frozenset(item[0] for item in EDITORS)

EDITORS_DIR = ("editors",)
SHIM_MODE = 0o755
SHIM_DIR_MODE = 0o755


@dataclass(frozen=True)
class FoundEditor:
    """A host IDE that is installed and should appear in Fork's Open in menu."""

    id: str
    command: str
    win_names: tuple[str, ...]
    programs_subdir: str


def _which(name: str, path: str | None) -> str | None:
    """Absolute path of ``name`` on ``path``, or None."""
    for directory in (path or os.defpath).split(os.pathsep):
        if not directory:
            continue
        candidate = Path(directory) / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate.resolve())
    return None


def discover(env: Mapping[str, str] | None = None) -> list[FoundEditor]:
    """Host editors found on ``PATH`` (one entry per id, first matching command)."""
    path = None if env is None else env.get("PATH")
    found: list[FoundEditor] = []
    for editor_id, win_names, commands, subdir in EDITORS:
        for command in commands:
            resolved = _which(command, path)
            if resolved is not None:
                found.append(FoundEditor(editor_id, resolved, win_names, subdir))
                break
    return found


def editors_win_dir(paths: Paths) -> Path:
    """``C:\\fork-linux\\editors`` in the prefix."""
    return paths.fork_linux_win_dir.joinpath(*EDITORS_DIR)


def programs_root(paths: Paths, user: str) -> Path:
    """``C:\\users\\<user>\\AppData\\Local\\Programs``."""
    return paths.prefix / "drive_c" / "users" / user / "AppData" / "Local" / "Programs"


def stub_targets(paths: Paths, user: str, found: Sequence[FoundEditor]) -> list[Path]:
    """Every Windows path that should hold a copy of ``fl-launch.exe`` for ``found``."""
    targets: list[Path] = []
    root = editors_win_dir(paths)
    programs = programs_root(paths, user)
    for editor in found:
        for name in editor.win_names:
            targets.append(root / name)
            targets.append(programs / editor.programs_subdir / name)
    return targets


def install(paths: Paths, user: str, launch: Path, found: Sequence[FoundEditor]) -> list[str]:
    """Copy ``launch`` (``fl-launch.exe``) to every stub path for ``found``; return installed ids.

    Stubs for editors that are no longer on PATH are removed from
    ``C:\\fork-linux\\editors`` and from our ``Local\\Programs`` folders.
    """
    wanted = {path.resolve() for path in stub_targets(paths, user, found)}
    root = editors_win_dir(paths)
    fsutil.ensure_dir(root, mode=SHIM_DIR_MODE)
    payload = launch.read_bytes()
    installed: list[str] = []
    for editor in found:
        for name in editor.win_names:
            for target in (root / name, programs_root(paths, user) / editor.programs_subdir / name):
                fsutil.ensure_dir(target.parent, mode=SHIM_DIR_MODE)
                fsutil.atomic_write(target, payload, mode=SHIM_MODE)
        installed.append(editor.id)
    _remove_stale(root, programs_root(paths, user), wanted)
    return installed


def _known_names() -> set[str]:
    """Every Windows stub basename we may write."""
    return {name for _id, names, _cmds, _sub in EDITORS for name in names}


def _unlink_unwanted(directory: Path, known_names: set[str], wanted: set[Path]) -> None:
    """Remove known stub files in ``directory`` that are not in ``wanted``."""
    if not directory.is_dir():
        return
    for path in directory.iterdir():
        if path.name in known_names and path.is_file() and path.resolve() not in wanted:
            path.unlink()


def _remove_stale(editors_dir: Path, programs: Path, wanted: set[Path]) -> None:
    """Delete editor stubs we wrote that are no longer wanted."""
    known = _known_names()
    _unlink_unwanted(editors_dir, known, wanted)
    known_subdirs = {subdir for _id, _names, _cmds, subdir in EDITORS}
    if not programs.is_dir():
        return
    for subdir in programs.iterdir():
        if subdir.name not in known_subdirs or not subdir.is_dir():
            continue
        _unlink_unwanted(subdir, known, wanted)
        if not any(subdir.iterdir()):
            subdir.rmdir()


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
    return {"editors": [{"id": e.id, "command": e.command, "names": list(e.win_names)} for e in found]}
