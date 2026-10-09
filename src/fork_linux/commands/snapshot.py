"""``fork-linux snapshot create|list|delete|prune`` and ``fork-linux rollback``: undo a bad Fork update."""

from __future__ import annotations

import argparse
from typing import Any

from .. import launcher, procs, snapshots, updates, versions, winecmd
from ..cli import AppContext
from ..errors import NotFound, NotSetUpError, UsageError
from ..fork_layout import ForkLayout
from ..locking import FileLock
from ..state import FORK_VERSION, State

PINNED = launcher.PINNED
PINNED_VERSION = launcher.PINNED_VERSION


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Add ``snapshot`` (with its actions) and ``rollback``."""
    parser = subparsers.add_parser(
        "snapshot",
        help="manage snapshots of the installed Fork",
        description="Snapshots keep a copy of Fork's install directory and settings so 'fork-linux rollback' "
        "can undo a bad self-update. One is taken automatically before each launch of a new version.",
    )
    actions = parser.add_subparsers(dest="snapshot_action", metavar="ACTION", title="actions", required=True)
    create = actions.add_parser("create", help="snapshot the installed Fork now")
    create.add_argument("--reason", default="manual", help="note stored with the snapshot")
    create.set_defaults(func=run_create)
    list_ = actions.add_parser("list", help="list snapshots, newest first")
    list_.set_defaults(func=run_list)
    delete = actions.add_parser("delete", help="delete one snapshot")
    delete.add_argument("id", metavar="ID")
    delete.set_defaults(func=run_delete)
    prune = actions.add_parser("prune", help="keep only the newest [snapshots] keep snapshots")
    prune.set_defaults(func=run_prune)

    rollback = subparsers.add_parser(
        "rollback",
        help="go back to a snapshot of an earlier Fork version",
        description="Restore Fork's install directory from a snapshot (by default the newest one of a "
        "version other than the installed one) and pin that version so Fork does not update again.",
    )
    which = rollback.add_mutually_exclusive_group()
    which.add_argument("id", metavar="ID", nargs="?", help="snapshot id (see 'fork-linux snapshot list')")
    which.add_argument("--to-version", metavar="V", help="the newest snapshot of Fork version V")
    rollback.add_argument(
        "--with-settings", action="store_true", help="also restore settings.json and Fork's repository list"
    )
    rollback.add_argument("--no-pin", action="store_true", help="do not pin the restored version")
    rollback.set_defaults(func=run_rollback)


def _layout(ctx: AppContext) -> ForkLayout:
    """Fork's files for the current user."""
    return ForkLayout(ctx.paths, winecmd.windows_user(ctx.env))


def _as_dict(snap: snapshots.Snapshot) -> dict[str, Any]:
    """JSON form of a snapshot."""
    return {
        "id": snap.id,
        "fork_version": snap.fork_version,
        "created": snap.created,
        "method": snap.method,
        "path": str(snap.path),
        "with_settings": snap.with_settings,
    }


def run_create(args: argparse.Namespace, ctx: AppContext) -> int:
    """Snapshot the installed Fork."""
    layout = _layout(ctx)
    if layout.installed_version() is None:
        raise NotSetUpError("Fork is not installed, so there is nothing to snapshot", hint="run 'fork-linux setup'")
    with FileLock(ctx.paths.lock_file, "snapshot"):
        snap = snapshots.create(ctx.paths, layout, method=ctx.config.get("snapshots", "method"), reason=args.reason)
    if ctx.json:
        ctx.print_json(_as_dict(snap))
    else:
        print(f"created snapshot {snap.id} (Fork {snap.fork_version}, {snap.method})")
    return 0


def run_list(args: argparse.Namespace, ctx: AppContext) -> int:
    """List the snapshots."""
    snaps = snapshots.list_snapshots(ctx.paths)
    if ctx.json:
        ctx.print_json([_as_dict(snap) for snap in snaps])
    elif not snaps:
        print("no snapshots")
    else:
        print(f"{'ID':<34} {'VERSION':<10} {'METHOD':<9} CREATED")
        for snap in snaps:
            print(f"{snap.id:<34} {snap.fork_version:<10} {snap.method:<9} {snap.created}")
    return 0


def run_delete(args: argparse.Namespace, ctx: AppContext) -> int:
    """Delete one snapshot."""
    with FileLock(ctx.paths.lock_file, "snapshot"):
        snapshots.delete(ctx.paths, args.id)
    if ctx.json:
        ctx.print_json({"deleted": [args.id]})
    else:
        print(f"deleted snapshot {args.id}")
    return 0


def run_prune(args: argparse.Namespace, ctx: AppContext) -> int:
    """Delete all but the newest ``[snapshots] keep`` (the installed version's newest is always kept)."""
    keep = ctx.config.getint("snapshots", "keep")
    installed = _layout(ctx).installed_version()
    with FileLock(ctx.paths.lock_file, "snapshot"):
        deleted = snapshots.prune(ctx.paths, keep, set() if installed is None else {installed})
    if ctx.json:
        ctx.print_json({"deleted": deleted})
    else:
        print(f"deleted {len(deleted)} snapshot(s)" + (": " + ", ".join(deleted) if deleted else ""))
    return 0


def _choose(ctx: AppContext, args: argparse.Namespace, installed: str | None) -> snapshots.Snapshot:
    """The snapshot to restore: by id, by version, or the newest of another version."""
    if args.id:
        return snapshots.find(ctx.paths, args.id)
    if args.to_version:
        if not versions.is_valid(args.to_version):
            raise UsageError(f"not a Fork version: {args.to_version!r}", hint="use a dotted number such as 2.23.2")
        return snapshots.find(ctx.paths, args.to_version)
    current = None if installed is None else versions.Version(installed)
    for snap in snapshots.list_snapshots(ctx.paths):
        if current is None or versions.Version(snap.fork_version) != current:
            return snap
    raise NotFound(
        "there is no snapshot of another Fork version to go back to",
        hint="list them with 'fork-linux snapshot list'",
    )


def _keep_installed(
    ctx: AppContext, layout: ForkLayout, installed: str | None, target: snapshots.Snapshot
) -> snapshots.Snapshot | None:
    """Snapshot the installed version before a rollback replaces it, unless a snapshot of it exists.

    Without this, rolling back right after Fork updated itself (before the next
    launch took its snapshot) would leave no way back to the newer version.
    """
    if installed is None:
        return None
    current = versions.Version(installed)
    if versions.Version(target.fork_version) == current:
        return None
    if any(versions.Version(snap.fork_version) == current for snap in snapshots.list_snapshots(ctx.paths)):
        return None
    return snapshots.create(ctx.paths, layout, method=ctx.config.get("snapshots", "method"), reason="before rollback")


def run_rollback(args: argparse.Namespace, ctx: AppContext) -> int:
    """Restore a snapshot (Fork must be closed) and, unless ``--no-pin``, pin its version."""
    paths = ctx.paths
    layout = _layout(ctx)
    procs.require_closed(paths.prefix)
    with FileLock(paths.lock_file, "rollback"):
        installed = layout.installed_version()
        snap = _choose(ctx, args, installed)
        saved = _keep_installed(ctx, layout, installed, snap)
        snapshots.restore(paths, layout, snap, with_settings=args.with_settings)
        state = State.load(paths.state_file)
        state.set(FORK_VERSION, snap.fork_version)
        state.set(updates.LAST_SEEN_VERSION, snap.fork_version)
        if not args.no_pin:
            ctx.config.set("fork", "update_policy", PINNED)
            state.set(PINNED_VERSION, snap.fork_version)
        state.save()
    result = {
        "restored": _as_dict(snap),
        "previous_version": installed,
        "pinned": not args.no_pin,
        "with_settings": args.with_settings,
        "saved": None if saved is None else saved.id,
    }
    if ctx.json:
        ctx.print_json(result)
    else:
        _print_rollback(snap, saved, pinned=not args.no_pin)
    return 0


def _print_rollback(snap: snapshots.Snapshot, saved: snapshots.Snapshot | None, *, pinned: bool) -> None:
    """Describe a finished rollback as text."""
    print(f"restored Fork {snap.fork_version} from snapshot {snap.id}")
    if saved is not None:
        print(f"Fork {saved.fork_version} was saved first as snapshot {saved.id}")
    if pinned:
        print(
            f"Fork is pinned to {snap.fork_version}; 'fork-linux config set fork.update_policy auto' "
            "allows updates again"
        )
    else:
        print("Fork may update itself again; 'fork-linux config set fork.update_policy pinned' stops that")
