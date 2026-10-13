"""Keep the user's files in Fork's install directory across the official installer.

Fork keeps its user data next to the Velopack-managed install, in
``AppData\\Local\\Fork``: ``settings.json``, ``accounts.json`` (credentials),
``custom-commands.json``, ``logs\\``, the unpacked bundled git (``gitInstance\\``)...
The official installer (Velopack's setup, ``--silent``) replaces an existing
installation by deleting that whole directory first: with the real installer,
``update --fork`` 2.23.1 -> 2.23.2 lost all of them, and Fork started with its
first-run dialogs again (E2E, F2).

So before the installer runs on an existing installation, every entry that is
not Velopack's own (:data:`VELOPACK_ENTRIES`) is *moved* (renamed, never
copied: ``accounts.json`` holds credentials) into
``<prefix>/.fork-linux/fork-user-data`` and moved back afterwards, replacing
what the installer wrote under the same name. Nothing is read, merged or
changed: the user's bytes come back as they were. A stash an interrupted run
left behind is put back first (and by the launcher before Fork starts).
Symbolic links are moved as links, never followed.
"""

from __future__ import annotations

import contextlib
import os
import stat
from collections.abc import Iterator
from pathlib import Path

from . import fsutil
from .errors import ForkLinuxError
from .fork_layout import ForkLayout
from .paths import Paths

STASH = "fork-user-data"
# Velopack's own entries in AppData\Local\Fork (compared case-insensitively): the installer
# writes these, everything else there is the user's.
VELOPACK_ENTRIES = frozenset({"current", "packages", "update.exe", "velopack.log"})


class UserDataError(ForkLinuxError):
    """Fork's user data could not be moved aside or back (the message says where it is)."""


def stash_dir(paths: Paths) -> Path:
    """Where the user's entries wait while the installer runs (inside our metadata directory)."""
    return paths.prefix_meta_dir / STASH


def _real_dir(path: Path) -> bool:
    """True for a directory that is not a symbolic link."""
    try:
        return stat.S_ISDIR(os.lstat(path).st_mode)
    except OSError:
        return False


def user_entries(layout: ForkLayout) -> list[str]:
    """The names in Fork's install directory that are not Velopack's (empty when it is missing or a link)."""
    local = layout.local_dir
    if not _real_dir(local):
        return []
    return sorted(name for name in os.listdir(local) if name.lower() not in VELOPACK_ENTRIES)


def _remove(path: Path) -> None:
    """Delete what the installer left at ``path`` (a link or file is unlinked, a real directory removed)."""
    if _real_dir(path):
        fsutil.safe_rmtree(path)
    else:
        path.unlink()


def _put_back(source: Path, target: Path) -> None:
    """Move ``source`` to ``target``: directories are merged, the user's entry wins over the installer's."""
    if _real_dir(source) and _real_dir(target):
        for child in sorted(os.listdir(source)):
            _put_back(source / child, target / child)
        os.rmdir(source)
        return
    if os.path.lexists(target):
        _remove(target)
    os.rename(source, target)


def restore(layout: ForkLayout) -> list[str]:
    """Put the stashed entries back into Fork's install directory; return their names.

    Raises :class:`UserDataError` (and keeps the stash) when that directory is a
    symbolic link or an entry cannot be moved back.
    """
    stash = stash_dir(layout.paths)
    if not _real_dir(stash):
        return []
    local = layout.local_dir
    names = sorted(os.listdir(stash))
    try:
        if os.path.islink(local):
            raise OSError(f"{local} is a symbolic link")
        local.mkdir(parents=True, exist_ok=True)
        for name in names:
            _put_back(stash / name, local / name)
        os.rmdir(stash)
    except OSError as exc:
        raise UserDataError(
            f"cannot put Fork's user data back into {local}: {exc}",
            hint=f"your files are kept in {stash}; move them back into {local} by hand",
        ) from exc
    return names


@contextlib.contextmanager
def kept(layout: ForkLayout) -> Iterator[list[str]]:
    """Move the user's entries out of Fork's install directory for the duration of the block.

    Yields the moved names; they are put back when the block ends, also when it
    raises. A failure while moving them aside puts back what was moved and raises
    :class:`UserDataError` before the block runs.
    """
    restore(layout)
    names = user_entries(layout)
    if names:
        stash = fsutil.ensure_dir(stash_dir(layout.paths))
        try:
            for name in names:
                os.rename(layout.local_dir / name, stash / name)
        except OSError as exc:
            restore(layout)
            raise UserDataError(
                f"cannot move Fork's user data aside before installing: {exc}",
                hint="nothing was installed; check the permissions of the Wine prefix and try again",
            ) from exc
    try:
        yield names
    finally:
        restore(layout)
