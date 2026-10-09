"""Snapshots of Fork's install directory for rollback after a bad self-update.

Fork updates itself through Velopack, which replaces ``Fork\\current\\`` as a
whole. Before a launch we keep a copy of ``current\\`` (hard links when
possible: Velopack never edits those files in place, it swaps the directory)
plus copies of ``settings.json``, ``custom-commands.json`` and a small
``ForkData\\`` (never hard links: Fork rewrites those files in place).
``accounts.json`` is only included when explicitly asked for.

Layout: ``<snapshots_dir>/<fork_version>-<UTC %Y%m%dT%H%M%SZ>/`` with
``meta.json`` (version, time, method and a ``{relpath: [size, mtime_ns]}``
inventory), ``current/`` and ``data/``; directories are private (0700). A
snapshot is built under ``.partial-<id>`` and renamed into place when complete.

:func:`restore` always copies back (never hard links), so a later Velopack
swap can never mutate a snapshot's files. The caller makes sure Fork is closed
and holds the setup lock.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import shutil
import stat
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import fork_settings, fsutil, versions
from .errors import IntegrityFailed, NotFound, NotSetUpError, UsageError
from .fork_layout import ForkLayout
from .paths import Paths

log = logging.getLogger(__name__)

SCHEMA = 1
SNAPSHOT_META = "meta.json"
CURRENT = "current"
DATA = "data"
FORKDATA = "ForkData"
METHODS = ("hardlink", "copy")
FORKDATA_MAX_BYTES = 10 * 1024 * 1024
# meta.json lists every file of current\ (a few thousand); far below this.
META_MAX_BYTES = 64 * 1024 * 1024
PARTIAL_PREFIX = ".partial-"
STALE_PARTIAL_SECONDS = 3600
_PRIVATE = 0o700
_SAFE_ID = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+_-]{0,127}")


@dataclass(frozen=True)
class Snapshot:
    """One snapshot on disk."""

    id: str
    fork_version: str
    created: str
    method: str
    path: Path
    with_settings: bool


def _now() -> datetime:
    """The current UTC time (tests replace this)."""
    return datetime.now(timezone.utc)


def _damaged(directory: Path, why: str) -> IntegrityFailed:
    return IntegrityFailed(
        f"snapshot {directory.name} is damaged: {why}",
        hint=f"delete it with 'fork-linux snapshot delete {directory.name}' and pick another one",
    )


def _is_regular(path: Path) -> bool:
    try:
        return stat.S_ISREG(os.lstat(path).st_mode)
    except OSError:
        return False


def _real_dir(path: Path) -> bool:
    return path.is_dir() and not path.is_symlink()


def _inside(path: Path, root: Path) -> bool:
    return path == root or path.is_relative_to(root)


# -- creating ---------------------------------------------------------------------


def _free_id(root: Path, base: str) -> str:
    """``base``, or ``base-N`` when a snapshot (or a partial one) of that name exists."""
    candidate = base
    number = 1
    while os.path.lexists(root / candidate) or os.path.lexists(root / f"{PARTIAL_PREFIX}{candidate}"):
        number += 1
        candidate = f"{base}-{number}"
    return candidate


def _tree_size(root: Path, limit: int) -> int:
    """Total size of the regular files below ``root``; stops counting once past ``limit``."""
    total = 0
    for current, _dirs, names in os.walk(root):
        for name in names:
            try:
                info = os.lstat(os.path.join(current, name))
            except OSError:
                continue
            if stat.S_ISREG(info.st_mode):
                total += info.st_size
            if total > limit:
                return total
    return total


def _copy_data(layout: ForkLayout, data_dir: Path, *, with_accounts: bool) -> list[str]:
    """Copy Fork's settings files and a small ForkData into ``data_dir``; return what was skipped."""
    fsutil.ensure_dir(data_dir, _PRIVATE)
    sources = [layout.settings_file, layout.custom_commands_file]
    if with_accounts:
        sources.append(layout.accounts_file)
    for source in sources:
        if _is_regular(source):
            shutil.copy2(source, data_dir / source.name, follow_symlinks=False)
    skipped: list[str] = []
    forkdata = layout.forkdata_dir
    if _real_dir(forkdata):
        if _tree_size(forkdata, FORKDATA_MAX_BYTES) <= FORKDATA_MAX_BYTES:
            fsutil.clone_tree(forkdata, data_dir / FORKDATA, "copy")
        else:
            log.warning("%s is larger than %d bytes; not included in the snapshot", forkdata, FORKDATA_MAX_BYTES)
            skipped.append(FORKDATA)
    return skipped


def _inventory(root: Path) -> dict[str, list[int]]:
    """``{relpath: [size, mtime_ns]}`` of every regular file below ``root`` (POSIX separators)."""
    files: dict[str, list[int]] = {}
    for current, dirs, names in os.walk(root):
        dirs.sort()
        for name in sorted(names):
            full = os.path.join(current, name)
            info = os.lstat(full)
            if stat.S_ISREG(info.st_mode):
                files[os.path.relpath(full, root).replace(os.sep, "/")] = [info.st_size, info.st_mtime_ns]
    return files


def create(
    paths: Paths,
    layout: ForkLayout,
    *,
    method: str = "auto",
    reason: str = "",
    with_accounts: bool = False,
) -> Snapshot:
    """Snapshot the installed Fork (``current\\`` + data files); ``accounts.json`` only with ``with_accounts``."""
    if method not in fsutil.CLONE_METHODS:
        raise UsageError(f"unknown snapshot method {method!r}", hint="use auto, hardlink or copy")
    version = layout.installed_version()
    if version is None or not _real_dir(layout.current_dir):
        raise NotSetUpError(
            "Fork is not installed, so there is nothing to snapshot", hint="run 'fork-linux setup' first"
        )
    root = fsutil.ensure_dir(paths.snapshots_dir, _PRIVATE)
    moment = _now()
    snap_id = _free_id(root, f"{version}-{moment.strftime('%Y%m%dT%H%M%SZ')}")
    staging = root / f"{PARTIAL_PREFIX}{snap_id}"
    final = root / snap_id
    try:
        fsutil.ensure_dir(staging, _PRIVATE)
        used = fsutil.clone_tree(layout.current_dir, staging / CURRENT, method)
        skipped = _copy_data(layout, staging / DATA, with_accounts=with_accounts)
        with_settings = _is_regular(staging / DATA / fork_settings.SETTINGS_NAME)
        meta: dict[str, Any] = {
            "schema": SCHEMA,
            "id": snap_id,
            "fork_version": version,
            "created": moment.isoformat(timespec="seconds"),
            "method": used,
            "reason": reason,
            "with_settings": with_settings,
            "with_accounts": with_accounts,
            "skipped": skipped,
            "files": _inventory(staging),
        }
        fsutil.atomic_write(staging / SNAPSHOT_META, json.dumps(meta, indent=2, sort_keys=True) + "\n")
        os.rename(staging, final)
    except BaseException:
        fsutil.safe_rmtree(staging, forbidden=(root,))
        raise
    return Snapshot(
        id=snap_id,
        fork_version=version,
        created=meta["created"],
        method=used,
        path=final,
        with_settings=with_settings,
    )


# -- listing ----------------------------------------------------------------------


def _read_meta(directory: Path) -> dict[str, Any] | None:
    """``meta.json`` of ``directory`` as a dict, or None if it is missing, oversized or not JSON."""
    try:
        fd = os.open(directory / SNAPSHOT_META, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        with os.fdopen(fd, "rb", closefd=False) as handle:
            raw = handle.read(META_MAX_BYTES + 1)
    finally:
        os.close(fd)
    if len(raw) > META_MAX_BYTES:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError):
        return None
    return data if isinstance(data, dict) else None


def _parse_created(value: Any) -> datetime | None:
    """An ISO timestamp with a UTC offset, or None."""
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else None


def _load(directory: Path) -> Snapshot | None:
    """The snapshot in ``directory``, or None if its metadata is malformed."""
    meta = _read_meta(directory)
    if meta is None:
        return None
    version = meta.get("fork_version")
    with_settings = meta.get("with_settings")
    if (
        meta.get("schema") != SCHEMA
        or _SAFE_ID.fullmatch(directory.name) is None
        or meta.get("id") != directory.name
        or not isinstance(version, str)
        or not versions.is_valid(version)
        or _parse_created(meta.get("created")) is None
        or meta.get("method") not in METHODS
        or not isinstance(with_settings, bool)
        or not isinstance(meta.get("files"), dict)
    ):
        return None
    return Snapshot(
        id=directory.name,
        fork_version=version,
        created=meta["created"],
        method=meta["method"],
        path=directory,
        with_settings=with_settings,
    )


def list_snapshots(paths: Paths) -> list[Snapshot]:
    """Every well-formed snapshot, newest first; malformed and partial ones are skipped."""
    try:
        entries = list(os.scandir(paths.snapshots_dir))
    except OSError:
        return []
    found = []
    for entry in entries:
        if entry.name.startswith(".") or not entry.is_dir(follow_symlinks=False):
            continue
        snap = _load(Path(entry.path))
        if snap is not None:
            found.append(snap)
    found.sort(key=lambda snap: (datetime.fromisoformat(snap.created), snap.id), reverse=True)
    return found


def find(paths: Paths, ref: str) -> Snapshot:
    """The snapshot with id ``ref``, else the newest one of Fork version ``ref``; :class:`NotFound` otherwise."""
    snaps = list_snapshots(paths)
    for snap in snaps:
        if snap.id == ref:
            return snap
    if isinstance(ref, str) and versions.is_valid(ref):
        wanted = versions.Version(ref)
        for snap in snaps:
            if versions.Version(snap.fork_version) == wanted:
                return snap
    raise NotFound(f"no snapshot matches {ref!r}", hint="list them with 'fork-linux snapshot list'")


# -- deleting ---------------------------------------------------------------------


def delete(paths: Paths, snap_id: str) -> None:
    """Delete snapshot ``snap_id``; :class:`NotFound` if there is no such snapshot directory."""
    if not isinstance(snap_id, str) or _SAFE_ID.fullmatch(snap_id) is None:
        raise UsageError(f"not a snapshot id: {snap_id!r}", hint="list them with 'fork-linux snapshot list'")
    directory = paths.snapshots_dir / snap_id
    if not _real_dir(directory):
        raise NotFound(f"no snapshot {snap_id!r}", hint="list them with 'fork-linux snapshot list'")
    fsutil.safe_rmtree(
        directory,
        marker=directory / SNAPSHOT_META,
        forbidden=(paths.snapshots_dir, paths.data_dir, paths.prefix),
    )


def _remove_stale_partials(paths: Paths) -> None:
    """Delete ``.partial-*`` directories left behind by an interrupted :func:`create`."""
    root = paths.snapshots_dir
    try:
        entries = list(os.scandir(root))
    except OSError:
        return
    cutoff = time.time() - STALE_PARTIAL_SECONDS
    for entry in entries:
        if (
            entry.name.startswith(PARTIAL_PREFIX)
            and entry.is_dir(follow_symlinks=False)
            and entry.stat(follow_symlinks=False).st_mtime < cutoff
        ):
            fsutil.safe_rmtree(Path(entry.path), forbidden=(root,))


def prune(paths: Paths, keep: int, protect_versions: set[str]) -> list[str]:
    """Keep the newest ``keep`` snapshots plus the newest one of each protected version; return deleted ids."""
    if keep < 0:
        raise ValueError("keep must not be negative")
    snaps = list_snapshots(paths)
    kept = {snap.id for snap in snaps[:keep]}
    protected = {versions.Version(item) for item in protect_versions if versions.is_valid(item)}
    for snap in snaps:
        version = versions.Version(snap.fork_version)
        if version in protected:
            kept.add(snap.id)
            protected.discard(version)
    deleted = []
    for snap in snaps:
        if snap.id not in kept:
            delete(paths, snap.id)
            deleted.append(snap.id)
    _remove_stale_partials(paths)
    return deleted


def ensure_current(paths: Paths, layout: ForkLayout, *, keep: int, method: str = "auto") -> Snapshot | None:
    """Snapshot the installed version unless one exists, then prune; ``keep`` 0 disables snapshots."""
    if keep <= 0:
        return None
    version = layout.installed_version()
    if version is None:
        return None
    wanted = versions.Version(version)
    created = None
    if not any(versions.Version(snap.fork_version) == wanted for snap in list_snapshots(paths)):
        created = create(paths, layout, method=method, reason="before launch")
    prune(paths, keep, {version})
    return created


# -- restoring --------------------------------------------------------------------


def _safe_rel(rel: Any) -> bool:
    """True for a relative POSIX path inside ``current/`` or ``data/`` without ``.``/``..``."""
    if not isinstance(rel, str) or "\0" in rel or rel.startswith("/"):
        return False
    parts = rel.split("/")
    return parts[0] in (CURRENT, DATA) and len(parts) > 1 and all(part not in ("", ".", "..") for part in parts)


def _valid_info(info: Any) -> bool:
    return (
        isinstance(info, list)
        and len(info) == 2
        and all(isinstance(item, int) and not isinstance(item, bool) and item >= 0 for item in info)
    )


def _verified_sizes(directory: Path) -> dict[str, int]:
    """``{relpath: size}`` from ``meta.json``, after checking every listed file still has that size."""
    meta = _read_meta(directory)
    files = None if meta is None else meta.get("files")
    if not isinstance(files, dict):
        raise _damaged(directory, "its meta.json is missing or unreadable")
    sizes: dict[str, int] = {}
    for rel, info in files.items():
        if not _safe_rel(rel) or not _valid_info(info):
            raise _damaged(directory, f"meta.json lists an invalid entry {rel!r}")
        try:
            found = os.lstat(directory / rel)
        except OSError:
            raise _damaged(directory, f"{rel} is missing") from None
        if not stat.S_ISREG(found.st_mode) or found.st_size != info[0]:
            raise _damaged(directory, f"{rel} has changed since the snapshot was taken")
        sizes[rel] = info[0]
    return sizes


def _verify_copy(target: Path, sizes: dict[str, int], rel_prefix: str) -> None:
    """Check that every file of the snapshot below ``rel_prefix`` arrived in ``target`` intact."""
    lead = rel_prefix + "/"
    for rel, size in sizes.items():
        if not rel.startswith(lead):
            continue
        copied = target / rel[len(lead):]
        if not _is_regular(copied) or copied.stat().st_size != size:
            raise IntegrityFailed(
                f"restoring {rel} into {target} failed: the copy does not match the snapshot",
                hint="check free disk space and try again; nothing was replaced",
            )


def _replace_dir(
    source: Path,
    target: Path,
    sizes: dict[str, int],
    rel_prefix: str,
    stamp: str,
    *,
    guard: Path,
    prefix: Path,
) -> None:
    """Replace directory ``target`` (directly inside ``guard``, inside ``prefix``) with a copy of ``source``.

    The copy is made and verified next to ``target`` first; ``target`` is then
    renamed aside, the copy renamed into place and the old tree deleted.
    """
    guard_real = guard.resolve()
    if not _inside(guard_real, prefix.resolve()) or target.parent.resolve() != guard_real:
        raise IntegrityFailed(
            f"refusing to replace {target}: it is not inside {guard} within the Wine prefix",
            hint="fork-linux only restores into its own Wine prefix",
        )
    if target.is_symlink():
        raise IntegrityFailed(
            f"refusing to replace {target}: it is a symbolic link",
            hint="fork-linux never follows links out of the Wine prefix",
        )
    fresh = target.with_name(f"{target.name}.fork-linux-new-{stamp}")
    old = target.with_name(f"{target.name}.fork-linux-old-{stamp}")
    try:
        fsutil.clone_tree(source, fresh, "copy")
        _verify_copy(fresh, sizes, rel_prefix)
    except BaseException:
        fsutil.safe_rmtree(fresh, forbidden=(guard_real,))
        raise
    had_old = os.path.lexists(target)
    if had_old:
        os.rename(target, old)
    try:
        os.rename(fresh, target)
    except OSError:
        if had_old:
            os.rename(old, target)
        fsutil.safe_rmtree(fresh, forbidden=(guard_real,))
        raise
    if had_old:
        fsutil.safe_rmtree(old, forbidden=(guard_real, prefix))


def _restore_data(paths: Paths, layout: ForkLayout, directory: Path, sizes: dict[str, int], stamp: str) -> None:
    """Put back the data files the snapshot holds; ``settings.json`` is backed up first."""
    data = directory / DATA
    for target in (layout.settings_file, layout.custom_commands_file, layout.accounts_file):
        source = data / target.name
        if not _is_regular(source):
            continue
        if target == layout.settings_file:
            fork_settings.backup(target, backup_dir=fork_settings.default_backup_dir(paths))
        fsutil.atomic_write(target, source.read_bytes(), mode=stat.S_IMODE(os.lstat(source).st_mode))
    forkdata = data / FORKDATA
    if _real_dir(forkdata):
        _replace_dir(
            forkdata,
            layout.forkdata_dir,
            sizes,
            f"{DATA}/{FORKDATA}",
            stamp,
            guard=layout.forkdata_dir.parent,
            prefix=paths.prefix,
        )


def restore(paths: Paths, layout: ForkLayout, snap: Snapshot, *, with_settings: bool = False) -> None:
    """Put the snapshot's ``current\\`` back, drop newer staged packages, optionally restore data files.

    Raises :class:`IntegrityFailed` (and changes nothing) when the snapshot
    does not match its ``meta.json`` or lies outside the snapshots directory.
    """
    directory = Path(snap.path)
    if directory.is_symlink() or directory.resolve().parent != paths.snapshots_dir.resolve():
        raise IntegrityFailed(
            f"refusing to restore {directory}: it is not a snapshot in {paths.snapshots_dir}",
            hint="pick a snapshot from 'fork-linux snapshot list'",
        )
    sizes = _verified_sizes(directory)
    if not _real_dir(directory / CURRENT):
        raise _damaged(directory, "its current/ directory is missing")
    stamp = _now().strftime("%Y%m%dT%H%M%S%fZ")
    _replace_dir(
        directory / CURRENT,
        layout.current_dir,
        sizes,
        CURRENT,
        stamp,
        guard=layout.local_dir,
        prefix=paths.prefix,
    )
    for _version, package in layout.staged_packages(than=snap.fork_version):
        with contextlib.suppress(FileNotFoundError):
            package.unlink()
    if with_settings:
        _restore_data(paths, layout, directory, sizes, stamp)
