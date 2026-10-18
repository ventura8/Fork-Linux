"""Fork's "Open Git Bash" under Wine: the Linux terminal in the repository instead.

Fork's Open in menu starts its bundled Git for Windows ``bin\\bash.exe`` with the
repository as the working directory and no arguments (``ShellExecute``, traced
with ``WINEDEBUG=+process``). Under Wine that msys bash dies after its first
child process (S6 / functionality report 7.4), and Git's own ``/etc/bash.bashrc``
forks a subshell on its second line: nothing ever reaches a prompt. Fork's files
are never changed (AGENTS.md hard rules 1 and 3), so the hook is the shell's
own startup: with ``POSIXLY_CORRECT`` set an interactive bash reads only the
file named by ``ENV`` — :data:`SCRIPT_NAME` under ``C:\\fork-linux``, ours — and
that file makes exactly one call: ``start /unix`` of ``fork-linux-terminal``
(the script behind "Open In Shell") with the folder as a Windows path, worked out
with parameter expansion only (no child process before it).

Non-interactive shells (Git's hooks, scripts) never read ``ENV``. The two
variables go only to Fork's environment while it uses its bundled git (with the
git bridge, Fork's ``bash.exe`` is our shim), and ``fork-linux-host`` drops them
before starting any Linux program.
"""

from __future__ import annotations

from pathlib import Path

from . import fork_tools, fsutil
from .paths import Paths

SCRIPT_NAME = "git-bash.sh"
SCRIPT_MSYS = "/c/fork-linux/" + SCRIPT_NAME
START_MSYS = "/c/windows/system32/start.exe"
ENV_VARS = ("POSIXLY_CORRECT", "ENV")


def script_path(paths: Paths) -> Path:
    """``C:\\fork-linux\\git-bash.sh`` in the prefix."""
    return paths.fork_linux_win_dir / SCRIPT_NAME


def _single_quoted(text: str) -> str:
    return "'" + text.replace("'", "'\\''") + "'"


def script_text(terminal: Path) -> str:
    """The ``ENV`` file that opens ``terminal`` (``fork-linux-terminal``) in the shell's folder."""
    return (
        "# Written by fork-linux: Fork's \"Open Git Bash\" opens the Linux terminal (see git_bash.py).\n"
        "# Under Wine msys bash dies after its first child: one exec, no command before it.\n"
        "fl_dir=${PWD#/}\n"
        "fl_drive=${fl_dir%%/*}\n"
        "case $fl_dir in */*) fl_rest=${fl_dir#*/} ;; *) fl_rest= ;; esac\n"
        f"MSYS2_ARG_CONV_EXCL='*' exec {START_MSYS} /b /unix {_single_quoted(str(terminal))} "
        '"${fl_drive^^}:/$fl_rest"\n'
    )


def wanted_text() -> str | None:
    """The script this installation wants, or None when it has no ``fork-linux-terminal``."""
    terminal = fork_tools.script(fork_tools.TERMINAL_SCRIPT)
    return None if terminal is None else script_text(terminal)


def install(paths: Paths) -> bool:
    """Write (or, without a terminal script, remove) :func:`script_path`; True when it is in place."""
    target = script_path(paths)
    text = wanted_text()
    if text is None:
        if target.is_file() and not target.is_symlink():
            target.unlink()
        return False
    fsutil.ensure_dir(target.parent)
    fsutil.atomic_write(target, text.encode("utf-8"), mode=0o644)
    return True


def installed(paths: Paths) -> bool:
    """The script is in place with the content this installation wants (or none is wanted)."""
    text = wanted_text()
    target = script_path(paths)
    if text is None:
        return not target.exists()
    try:
        return not target.is_symlink() and target.read_text(encoding="utf-8") == text
    except OSError:
        return False


def launch_env(paths: Paths, *, native_git: bool) -> dict[str, str]:
    """``POSIXLY_CORRECT`` and ``ENV`` for Fork when its bundled bash is in use and the script exists."""
    if native_git or not script_path(paths).is_file():
        return {}
    return {"POSIXLY_CORRECT": "1", "ENV": SCRIPT_MSYS}
