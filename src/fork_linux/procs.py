"""Find Fork (and any other Wine process) running in our prefix by scanning ``/proc``.

Only processes of the current user are considered. A process belongs to a
prefix when its environment has ``WINEPREFIX`` equal to the prefix (both
resolved); Fork itself is recognised by ``fork.exe`` in its command line
(Wine shows the Windows path there), excluding Velopack's ``Update.exe``.
Entries that vanish or cannot be read while scanning are skipped.
"""

from __future__ import annotations

import os
from pathlib import Path

from .errors import ForkRunning

PROC = Path("/proc")
# /proc/<pid>/environ and cmdline are bounded by ARG_MAX in practice; never read more.
MAX_BYTES = 4 * 1024 * 1024
_WINEPREFIX = b"WINEPREFIX="


def _read(path: Path) -> bytes | None:
    """The contents of a /proc file, or None when it cannot be read."""
    try:
        with open(path, "rb") as handle:
            return handle.read(MAX_BYTES)
    except OSError:
        return None


def _wineprefix(environ: bytes) -> str | None:
    """``WINEPREFIX`` from a NUL-separated environment block, or None."""
    for item in environ.split(b"\0"):
        if item.startswith(_WINEPREFIX):
            return os.fsdecode(item[len(_WINEPREFIX):])
    return None


def _own_pids(proc_root: Path) -> list[int]:
    """Numeric /proc entries owned by the current user, except this process."""
    uid = os.getuid()
    me = os.getpid()
    try:
        entries = list(os.scandir(proc_root))
    except OSError:
        return []
    pids = []
    for entry in entries:
        if not (entry.name.isascii() and entry.name.isdigit()) or int(entry.name) == me:
            continue
        try:
            owner = entry.stat(follow_symlinks=False).st_uid
        except OSError:
            continue
        if owner == uid:
            pids.append(int(entry.name))
    return sorted(pids)


def _in_prefix(proc_root: Path, pid: int, prefix: str) -> bool:
    """True if process ``pid`` runs with ``WINEPREFIX`` resolving to ``prefix``."""
    environ = _read(proc_root / str(pid) / "environ")
    if environ is None:
        return False
    value = _wineprefix(environ)
    if value is None or not os.path.isabs(value):
        return False
    return os.path.realpath(value) == prefix


def prefix_pids(prefix: Path, *, proc_root: Path = PROC) -> list[int]:
    """PIDs of the current user's processes running with ``WINEPREFIX=<prefix>``."""
    wanted = os.path.realpath(prefix)
    return [pid for pid in _own_pids(proc_root) if _in_prefix(proc_root, pid, wanted)]


def _is_fork(cmdline: bytes) -> bool:
    """True for a command line naming ``fork.exe`` but not Velopack's ``Update.exe``."""
    text = os.fsdecode(cmdline.replace(b"\0", b" ")).lower()
    return "fork.exe" in text and "update.exe" not in text


def fork_pids(prefix: Path, *, proc_root: Path = PROC) -> list[int]:
    """PIDs of Fork processes running in ``prefix``."""
    found = []
    for pid in prefix_pids(prefix, proc_root=proc_root):
        cmdline = _read(proc_root / str(pid) / "cmdline")
        if cmdline is not None and _is_fork(cmdline):
            found.append(pid)
    return found


def fork_running(prefix: Path, *, proc_root: Path = PROC) -> bool:
    """True if Fork runs in ``prefix``."""
    return bool(fork_pids(prefix, proc_root=proc_root))


def require_closed(prefix: Path, *, proc_root: Path = PROC) -> None:
    """Raise :class:`ForkRunning` if Fork runs in ``prefix``."""
    pids = fork_pids(prefix, proc_root=proc_root)
    if pids:
        listed = ", ".join(str(pid) for pid in pids)
        raise ForkRunning(
            f"Fork is running in {prefix} (pid {listed})",
            hint="close Fork first, then run the command again",
        )
