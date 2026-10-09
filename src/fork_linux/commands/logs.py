"""``fork-linux logs``: show, follow or bundle the logs that help diagnose a problem.

``--bundle`` writes ``fork-linux-logs-<UTC time>.tar.gz`` into the current
directory with secrets redacted: fork-linux's own logs, the last Wine logs,
the tails of Fork's ``fork.log`` and ``velopack.log``, Fork's
``settings.json``, our ``state.json`` and ``config.ini``, ``/etc/os-release``
and ``uname``. Fork's ``accounts.json`` is never included.
"""

from __future__ import annotations

import argparse
import io
import os
import platform
import stat
import sys
import tarfile
import time
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import IO

from .. import logging_setup, winecmd
from ..cli import AppContext
from ..errors import NotFound, UsageError
from ..fork_layout import ForkLayout
from ..paths import Paths

TAIL_LINES = 200
BUNDLE_TAIL_LINES = 2000
BUNDLE_WINE_LOGS = 3
# Never read more than this from one file (logs can grow large).
MAX_READ = 8 * 1024 * 1024
FOLLOW_INTERVAL = 0.5
OS_RELEASE = Path("/etc/os-release")
BUNDLE_ROOT = "fork-linux-logs"
SOURCES = ("wine", "fork", "velopack", "setup")
NEVER_BUNDLED = ("accounts.json",)


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Add ``logs``."""
    parser = subparsers.add_parser(
        "logs",
        help="show, follow or bundle logs",
        description="Show the end of a log (default: Wine's output of the last Fork start), follow it, "
        "print its location, or bundle the logs for a bug report (secrets redacted).",
    )
    which = parser.add_mutually_exclusive_group()
    which.add_argument("--wine", dest="source", action="store_const", const="wine", help="Wine's output (default)")
    which.add_argument("--fork", dest="source", action="store_const", const="fork", help="Fork's own fork.log")
    which.add_argument("--velopack", dest="source", action="store_const", const="velopack", help="Fork's updater log")
    which.add_argument(
        "--setup", dest="source", action="store_const", const="setup", help="fork-linux's own log (setup, launches)"
    )
    which.add_argument("--all", dest="source", action="store_const", const="all", help="all of the above")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--follow", "-f", action="store_true", help="keep printing new lines (Ctrl+C stops)")
    mode.add_argument("--path", action="store_true", help="print the log file location(s) only")
    mode.add_argument("--bundle", action="store_true", help="write a redacted tar.gz for a bug report")
    parser.set_defaults(func=run, source=None)


def _newest(paths: Iterable[Path]) -> list[Path]:
    """Existing regular files, newest first."""
    found = []
    for path in paths:
        try:
            found.append((path.stat().st_mtime_ns, path.name, path))
        except OSError:
            continue
    found.sort(reverse=True)
    return [path for _mtime, _name, path in found]


def wine_logs(paths: Paths) -> list[Path]:
    """``wine-last.log`` and the ``--debug`` logs, newest first."""
    try:
        names = os.listdir(paths.logs_dir)
    except OSError:
        return []
    return _newest(paths.logs_dir / name for name in names if name.startswith("wine-") and name.endswith(".log"))


def own_logs(paths: Paths) -> list[Path]:
    """fork-linux's own logs (``fork-linux.log`` and its rotations, ``setup-*.log``), newest first."""
    try:
        names = os.listdir(paths.logs_dir)
    except OSError:
        return []
    return _newest(
        paths.logs_dir / name
        for name in names
        if name.startswith(logging_setup.LOG_FILE) or (name.startswith("setup-") and name.endswith(".log"))
    )


def _source_files(source: str, paths: Paths, layout: ForkLayout) -> list[Path]:
    """The file(s) for one source, newest first (may be empty)."""
    if source == "wine":
        return wine_logs(paths)[:1]
    if source == "fork":
        return _newest([layout.fork_log])
    if source == "velopack":
        return _newest([layout.velopack_log])
    return own_logs(paths)[:1]


def _read_tail(path: Path, lines: int, *, follow_symlinks: bool = True) -> str:
    """The last ``lines`` lines of regular file ``path`` (reads at most :data:`MAX_READ` bytes).

    Anything but a regular file raises ``OSError`` (a FIFO never blocks); with
    ``follow_symlinks=False`` a symbolic link does too.
    """
    flags = os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC | (0 if follow_symlinks else os.O_NOFOLLOW)
    fd = os.open(path, flags)
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(f"{path} is not a regular file")
        size = handle.seek(0, os.SEEK_END)
        handle.seek(max(0, size - MAX_READ))
        data = handle.read()
    text = data.decode("utf-8", errors="replace")
    return "".join(text.splitlines(keepends=True)[-lines:])


def _follow(path: Path, out: IO[str]) -> None:
    """Print what is appended to ``path`` until interrupted (a truncated file starts over)."""
    with open(path, "rb") as handle:
        handle.seek(0, os.SEEK_END)
        while True:
            chunk = handle.read()
            if chunk:
                out.write(chunk.decode("utf-8", errors="replace"))
                out.flush()
            elif handle.tell() > os.fstat(handle.fileno()).st_size:
                handle.seek(0)
            else:
                _sleep(FOLLOW_INTERVAL)


def _follow_until_interrupted(path: Path) -> int:
    """:func:`_follow` until Ctrl+C; exit status 0."""
    try:
        _follow(path, sys.stdout)
    except KeyboardInterrupt:
        pass
    return 0


def _sleep(seconds: float) -> None:
    """``time.sleep`` (tests replace this)."""
    time.sleep(seconds)


def _utc_stamp() -> str:
    """UTC time for the bundle name (tests replace this)."""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _add(tar: tarfile.TarFile, name: str, text: str) -> None:
    """Add ``text`` (redacted) as ``<root>/<name>``."""
    data = logging_setup.redact(text).encode("utf-8")
    info = tarfile.TarInfo(f"{BUNDLE_ROOT}/{name}")
    info.size = len(data)
    info.mtime = int(time.time())
    info.mode = 0o600
    tar.addfile(info, io.BytesIO(data))


def _add_file(
    tar: tarfile.TarFile, name: str, path: Path, lines: int | None = None, *, follow_symlinks: bool = True
) -> bool:
    """Add a redacted copy (or its last ``lines`` lines) of ``path``; False when it cannot be read."""
    if path.name in NEVER_BUNDLED:
        return False
    try:
        text = _read_tail(path, lines if lines is not None else sys.maxsize, follow_symlinks=follow_symlinks)
    except OSError:
        return False
    _add(tar, name, text)
    return True


def _uname() -> str:
    """``uname -a``-like text."""
    info = platform.uname()
    return f"{info.system} {info.node} {info.release} {info.version} {info.machine}\n"


def bundle(paths: Paths, layout: ForkLayout, directory: Path) -> tuple[Path, list[str]]:
    """Write the bug-report bundle into ``directory``; return its path and the names included."""
    target = directory / f"{BUNDLE_ROOT}-{_utc_stamp()}.tar.gz"
    if os.path.lexists(target):
        raise UsageError(f"{target} already exists", hint="wait a second and try again")
    included: list[str] = []
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
    with os.fdopen(fd, "wb") as raw, tarfile.open(fileobj=raw, mode="w:gz") as tar:
        candidates: list[tuple[str, Path, int | None]] = []
        candidates += [(f"fork-linux/{path.name}", path, None) for path in own_logs(paths)]
        candidates += [(f"wine/{path.name}", path, None) for path in wine_logs(paths)[:BUNDLE_WINE_LOGS]]
        candidates += [
            ("fork-linux/state.json", paths.state_file, None),
            ("fork-linux/config.ini", paths.config_file, None),
            ("system/os-release", OS_RELEASE, None),
        ]
        for name, path, lines in candidates:
            if _add_file(tar, name, path, lines):
                included.append(name)
        # Files inside the Wine prefix are written by Windows programs: a symbolic link there could
        # point at a private file (an ssh key), so links are never followed for them.
        for name, path, lines in (
            ("fork/fork.log", layout.fork_log, BUNDLE_TAIL_LINES),
            ("fork/velopack.log", layout.velopack_log, BUNDLE_TAIL_LINES),
            ("fork/settings.json", layout.settings_file, None),
        ):
            if _add_file(tar, name, path, lines, follow_symlinks=False):
                included.append(name)
        _add(tar, "system/uname", _uname())
        included.append("system/uname")
    return target, included


def _run_bundle(ctx: AppContext, paths: Paths, layout: ForkLayout) -> int:
    """``--bundle``: write the support bundle into the current directory."""
    target, included = bundle(paths, layout, Path(os.getcwd()))
    if ctx.json:
        ctx.print_json({"bundle": str(target), "files": included})
    else:
        print(f"wrote {target} ({len(included)} files)")
        print("review before sharing: secrets are redacted, but the logs name your files and repositories")
    return 0


def _print_paths(ctx: AppContext, files: dict[str, list[Path]]) -> int:
    """``--path``: print where each log lives."""
    if ctx.json:
        ctx.print_json({source: [str(path) for path in found] for source, found in files.items()})
    else:
        for found in files.values():
            for path in found:
                print(path)
    return 0


def _show(sources: tuple[str, ...], files: dict[str, list[Path]], *, follow: bool) -> int:
    """Print the tail of each existing log, or follow the single one."""
    existing = [(source, found[0]) for source, found in files.items() if found]
    if not existing:
        raise NotFound(f"no {' / '.join(sources)} log yet", hint="start Fork with 'fork-linux run' first")
    if follow:
        if len(existing) > 1:
            raise UsageError("--follow follows one log; pick --wine, --fork, --velopack or --setup")
        return _follow_until_interrupted(existing[0][1])
    for source, path in existing:
        if len(existing) > 1:
            print(f"==> {source}: {path} <==")
        sys.stdout.write(_read_tail(path, TAIL_LINES))
    return 0


def run(args: argparse.Namespace, ctx: AppContext) -> int:
    """Show, follow, locate or bundle logs."""
    paths = ctx.paths
    layout = ForkLayout(paths, winecmd.windows_user(ctx.env))
    if args.bundle:
        return _run_bundle(ctx, paths, layout)
    sources = SOURCES if args.source == "all" else (args.source or "wine",)
    files = {source: _source_files(source, paths, layout) for source in sources}
    if args.path:
        return _print_paths(ctx, files)
    return _show(sources, files, follow=args.follow)
