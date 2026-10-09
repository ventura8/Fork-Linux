"""``fork-linux settings ...``: read and edit Fork's own ``settings.json``.

Keys are dotted paths (``RepositoryManager.SourceDirectories``). Every write
needs Fork to be closed (Fork rewrites the file on exit) and backs the file up
first; unknown keys are always preserved. Fork creates ``settings.json``
itself after its first completed start, so commands that change a key refuse
to create it.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .. import fork_settings, procs, winecmd
from ..cli import AppContext
from ..errors import NotFound, UsageError
from ..fork_layout import ForkLayout

_MISSING = object()
_NO_FILE_HINT = "start Fork once and finish its welcome dialog; Fork creates settings.json then"


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Add ``settings`` and its actions."""
    parser = subparsers.add_parser(
        "settings",
        help="show or change Fork's own settings (settings.json)",
        description="Show or change Fork's settings.json. Keys are dotted paths such as "
        "RepositoryManager.SourceDirectories. Changes need Fork to be closed; the file is backed up first.",
    )
    actions = parser.add_subparsers(dest="settings_action", metavar="ACTION", title="actions", required=True)
    actions.add_parser("show", help="print the whole file").set_defaults(func=run_show)
    get = actions.add_parser("get", help="print one value as JSON")
    get.add_argument("key", metavar="KEY")
    get.set_defaults(func=run_get)
    set_ = actions.add_parser("set", help="change one value (JSON; plain text is taken as a string)")
    set_.add_argument("key", metavar="KEY")
    set_.add_argument("value", metavar="VALUE")
    set_.add_argument("--string", action="store_true", help="store VALUE as a string even if it looks like JSON")
    set_.set_defaults(func=run_set)
    unset = actions.add_parser("unset", help="remove one key (Fork falls back to its default)")
    unset.add_argument("key", metavar="KEY")
    unset.set_defaults(func=run_unset)
    actions.add_parser(
        "apply-defaults", help="apply fork-linux's Wine-safe values, theme and scaling now"
    ).set_defaults(func=run_apply_defaults)
    actions.add_parser("backup", help="back up settings.json now").set_defaults(func=run_backup)
    restore = actions.add_parser("restore", help="restore a backup (default: the newest)")
    restore.add_argument("file", metavar="FILE", nargs="?", type=Path)
    restore.set_defaults(func=run_restore)
    actions.add_parser("path", help="print the location of settings.json").set_defaults(func=run_path)


def _layout(ctx: AppContext) -> ForkLayout:
    """Fork's files for the current user."""
    return ForkLayout(ctx.paths, winecmd.windows_user(ctx.env))


def _existing(layout: ForkLayout) -> dict[str, Any]:
    """The parsed settings; :class:`NotFound` when Fork has not written them yet."""
    if not layout.settings_file.exists():
        raise NotFound(f"Fork has not created {layout.settings_file} yet", hint=_NO_FILE_HINT)
    return fork_settings.load(layout.settings_file)


def _backup_dir(ctx: AppContext) -> Path:
    return fork_settings.default_backup_dir(ctx.paths)


def _print_value(ctx: AppContext, value: Any) -> None:
    """Print ``value`` as JSON (always indented for ``--json``; strings bare otherwise)."""
    if isinstance(value, str) and not ctx.json:
        print(value)
    else:
        print(json.dumps(value, indent=2, ensure_ascii=False))


def run_show(args: argparse.Namespace, ctx: AppContext) -> int:
    """Print the whole file."""
    _print_value(ctx, _existing(_layout(ctx)))
    return 0


def run_get(args: argparse.Namespace, ctx: AppContext) -> int:
    """Print one value."""
    value = fork_settings.get(_existing(_layout(ctx)), args.key, _MISSING)
    if value is _MISSING:
        raise NotFound(f"Fork's settings have no {args.key!r}", hint="see them all with 'fork-linux settings show'")
    _print_value(ctx, value)
    return 0


def _parse_value(text: str, as_string: bool) -> Any:
    """``text`` as JSON (``true``, ``125``, ``{"a": 1}``), or the plain string."""
    if as_string:
        return text
    try:
        return json.loads(text)
    except ValueError:
        return text


def _write(ctx: AppContext, layout: ForkLayout, data: dict[str, Any]) -> Path | None:
    """Save ``data`` (Fork must be closed); return the backup made first."""
    return fork_settings.save(layout.settings_file, data, backup_dir=_backup_dir(ctx))


def _report(ctx: AppContext, changed: list[str], backup: Path | None) -> None:
    """Say what changed."""
    if ctx.json:
        ctx.print_json({"changed": changed, "backup": None if backup is None else str(backup)})
    elif changed:
        print(f"changed: {', '.join(changed)}" + (f" (backup: {backup})" if backup else ""))
    else:
        print("nothing to change")


def run_set(args: argparse.Namespace, ctx: AppContext) -> int:
    """Change one value."""
    layout = _layout(ctx)
    procs.require_closed(ctx.paths.prefix)
    data = _existing(layout)
    try:
        changed = fork_settings.merge_set(data, args.key, _parse_value(args.value, args.string))
    except ValueError as exc:
        raise UsageError(str(exc), hint="keys are dotted paths such as RepositoryManager.SourceDirectories") from None
    backup = _write(ctx, layout, data) if changed else None
    _report(ctx, [args.key] if changed else [], backup)
    return 0


def _remove(data: dict[str, Any], dotted: str) -> bool:
    """Delete ``dotted`` from ``data``; False when it is not there."""
    *parents, leaf = dotted.split(".")
    node: Any = data
    for part in parents:
        node = node.get(part) if isinstance(node, dict) else None
    if not isinstance(node, dict) or leaf not in node:
        return False
    del node[leaf]
    return True


def run_unset(args: argparse.Namespace, ctx: AppContext) -> int:
    """Remove one key."""
    layout = _layout(ctx)
    procs.require_closed(ctx.paths.prefix)
    data = _existing(layout)
    if not _remove(data, args.key):
        raise NotFound(f"Fork's settings have no {args.key!r}", hint="see them all with 'fork-linux settings show'")
    _report(ctx, [args.key], _write(ctx, layout, data))
    return 0


def run_apply_defaults(args: argparse.Namespace, ctx: AppContext) -> int:
    """Apply what the launcher applies before every start."""
    from .. import bridge, launcher

    layout = _layout(ctx)
    procs.require_closed(ctx.paths.prefix)
    _existing(layout)
    wanted = launcher.desired_settings(
        paths=ctx.paths,
        config=ctx.config,
        env=ctx.env,
        runner=ctx.runner,
        user=layout.user,
        layout=layout,
        bridge_active=bridge.host_actions_active(ctx),
    )
    changed = fork_settings.apply(layout, wanted, backup_dir=_backup_dir(ctx))
    backups = fork_settings.list_backups(_backup_dir(ctx))
    _report(ctx, changed, backups[0] if changed and backups else None)
    return 0


def run_backup(args: argparse.Namespace, ctx: AppContext) -> int:
    """Back up the file now."""
    layout = _layout(ctx)
    _existing(layout)
    saved = fork_settings.backup(layout.settings_file, backup_dir=_backup_dir(ctx))
    if ctx.json:
        ctx.print_json({"backup": str(saved)})
    else:
        print(f"backed up to {saved}")
    return 0


def run_restore(args: argparse.Namespace, ctx: AppContext) -> int:
    """Put a backup back (the current file is backed up first)."""
    layout = _layout(ctx)
    procs.require_closed(ctx.paths.prefix)
    source = args.file
    if source is None:
        backups = fork_settings.list_backups(_backup_dir(ctx))
        if not backups:
            raise NotFound("there is no backup of Fork's settings", hint="make one with 'fork-linux settings backup'")
        source = backups[0]
    if not source.is_file():
        raise NotFound(f"no such backup: {source}")
    data = fork_settings.load(source)
    backup = _write(ctx, layout, data)
    if ctx.json:
        ctx.print_json({"restored": str(source), "backup": None if backup is None else str(backup)})
    else:
        print(f"restored {layout.settings_file} from {source}")
    return 0


def run_path(args: argparse.Namespace, ctx: AppContext) -> int:
    """Print where the file lives."""
    path = _layout(ctx).settings_file
    if ctx.json:
        ctx.print_json({"path": str(path), "exists": path.exists()})
    else:
        print(path)
    return 0
