"""Linux file/folder pickers for Fork's dialogs (zenity / kdialog).

``fork-linux-host pick-dir|pick-file|save-file`` prints one absolute path on
stdout. Used by ``fl-launch.exe`` and ``fl-filedlg.dll`` through the bridge.
Cancel is exit status 1; no picker installed is 127.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence

from .errors import ForkLinuxError
from .procrun import Completed, Runner

EXIT_CANCELLED = 1
EXIT_NOT_FOUND = 127
PICK_TIMEOUT = 600.0


class PickCancelled(ForkLinuxError):
    """The user closed the dialog without choosing a path."""

    def __init__(self) -> None:
        super().__init__("cancelled")


class PickNotFound(ForkLinuxError):
    """No zenity or kdialog on PATH."""

    def __init__(self) -> None:
        super().__init__(
            "no file picker found",
            hint="install zenity or kdialog",
        )


def _desktops(env: Mapping[str, str]) -> list[str]:
    return [item.strip().upper() for item in env.get("XDG_CURRENT_DESKTOP", "").split(":") if item.strip()]


def _prefer_kdialog(env: Mapping[str, str]) -> bool:
    return any(name in ("KDE", "LXQT") for name in _desktops(env))


def _start_dir(path: str) -> str:
    """Directory to open in: an existing folder, else its parent, else cwd."""
    if not path:
        return os.path.abspath(os.getcwd())
    absolute = os.path.abspath(path)
    if os.path.isdir(absolute):
        return absolute
    parent = os.path.dirname(absolute)
    return parent if os.path.isdir(parent) else os.path.abspath(os.getcwd())


def zenity_argv(kind: str, start: str, name: str) -> list[str]:
    """``zenity --file-selection`` arguments for ``kind`` (dir / file / save)."""
    filename = os.path.join(start, name) if kind == "save" and name else start.rstrip("/") + "/"
    argv = ["zenity", "--file-selection", f"--filename={filename}"]
    if kind == "dir":
        argv.append("--directory")
    elif kind == "save":
        argv.extend(("--save", "--confirm-overwrite"))
    return argv


def kdialog_argv(kind: str, start: str, name: str) -> list[str]:
    """``kdialog`` arguments for ``kind`` (dir / file / save)."""
    if kind == "dir":
        return ["kdialog", "--getexistingdirectory", start]
    if kind == "save":
        return ["kdialog", "--getsavefilename", os.path.join(start, name) if name else start]
    return ["kdialog", "--getopenfilename", start]


def _parse_path(result: Completed) -> str | None:
    """Absolute path from a successful picker, or None when cancelled / empty."""
    if result.returncode != 0:
        return None
    line = result.stdout.strip().splitlines()[:1]
    if not line:
        return None
    path = line[0].strip()
    return os.path.abspath(path) if path else None


RunPick = Callable[[Sequence[str], Mapping[str, str]], Completed]


def _try(run: RunPick, argv: Sequence[str], env: Mapping[str, str]) -> str | None:
    """Run one picker command; None when the binary is missing at spawn time."""
    result = run(list(argv), env)
    if result.returncode == EXIT_NOT_FOUND:
        return None
    path = _parse_path(result)
    if path is not None:
        return path
    raise PickCancelled()


def pick(
    kind: str,
    start: str = "",
    name: str = "",
    *,
    env: Mapping[str, str] | None = None,
    runner: Runner | None = None,
    which: Callable[[str], str | None] | None = None,
    run: RunPick | None = None,
) -> str:
    """Show a picker and return an absolute path.

    ``kind`` is ``dir``, ``file`` or ``save``. Raises :class:`PickCancelled` or
    :class:`PickNotFound`. ``run`` overrides how the argv is spawned (Flatpak
    wraps with ``flatpak-spawn --host``).
    """
    if kind not in ("dir", "file", "save"):
        raise ForkLinuxError(f"unknown picker kind: {kind!r}")
    environ = dict(os.environ if env is None else env)
    tool = Runner() if runner is None else runner

    def lookup(program: str) -> str | None:
        return which(program) if which is not None else tool.which(program, environ.get("PATH"))

    def execute(argv: Sequence[str], env_map: Mapping[str, str]) -> Completed:
        if run is not None:
            return run(argv, env_map)
        return tool.run(list(argv), env=dict(env_map), timeout=PICK_TIMEOUT)

    folder = _start_dir(start)
    order = ("kdialog", "zenity") if _prefer_kdialog(environ) else ("zenity", "kdialog")
    builders = {"zenity": zenity_argv, "kdialog": kdialog_argv}
    saw_picker = False
    for program in order:
        if lookup(program) is None:
            continue
        saw_picker = True
        chosen = _try(execute, builders[program](kind, folder, name), environ)
        if chosen is not None:
            return chosen
        # _try returns None only when the binary vanished between which() and exec.
    if not saw_picker:
        raise PickNotFound()
    raise PickCancelled()
