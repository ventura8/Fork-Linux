"""``fork-linux ssh sync|status``: share ``~/.ssh`` with Fork's bundled ssh."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from .. import ssh_sync, winecmd
from ..cli import AppContext


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Add ``ssh`` and its actions."""
    parser = subparsers.add_parser(
        "ssh",
        help="share your ssh keys and config with Fork",
        description="Share ~/.ssh with Fork's bundled ssh: keys are linked (or copied 0600) into the Wine "
        "user's own private .ssh directory and ~/.ssh/config is translated. This also runs before Fork "
        "starts whenever ~/.ssh changed ([ssh] sync).",
    )
    actions = parser.add_subparsers(dest="ssh_action", metavar="ACTION", title="actions", required=True)
    sync = actions.add_parser("sync", help="share ~/.ssh now")
    sync.add_argument("--dry-run", action="store_true", help="only show what would change")
    sync.add_argument("--mode", choices=ssh_sync.MODES, help="link or copy private keys (default: [ssh] mode)")
    sync.add_argument("--no-config", action="store_true", help="do not translate ~/.ssh/config")
    sync.set_defaults(func=run_sync)
    actions.add_parser("status", help="show the state of the shared ssh setup").set_defaults(func=run_status)


_LABELS = {
    "linked": "linked",
    "copied": "copied",
    "removed": "removed",
    "skipped": "skipped",
    "config_path": "wrote config",
}
_DRY_LABELS = {
    "linked": "would link",
    "copied": "would copy",
    "removed": "would remove",
    "skipped": "would skip",
    "config_path": "would write config",
}


def _home(ctx: AppContext) -> Path:
    return Path(ctx.env.get("HOME") or Path.home())


def _report_dict(report: ssh_sync.SyncReport) -> dict[str, Any]:
    """JSON form of a sync report."""
    return {
        "linked": [str(path) for path in report.linked],
        "copied": [str(path) for path in report.copied],
        "dropped_directives": [{"directive": item, "reason": why} for item, why in report.dropped_directives],
        "config_path": None if report.config_path is None else str(report.config_path),
        "skipped": [str(path) for path in report.skipped],
        "removed": [str(path) for path in report.removed],
    }


def run_sync(args: argparse.Namespace, ctx: AppContext) -> int:
    """Share ``~/.ssh`` (or show what would change with ``--dry-run``)."""
    report = ssh_sync.sync(
        ctx.paths,
        winecmd.windows_user(ctx.env),
        _home(ctx),
        mode=args.mode or ctx.config.get("ssh", "mode"),
        include_config=not args.no_config,
        dry_run=args.dry_run,
    )
    data = _report_dict(report)
    if ctx.json:
        ctx.print_json({"dry_run": args.dry_run, **data})
        return 0
    labels = _DRY_LABELS if args.dry_run else _LABELS
    for key in ("linked", "copied", "removed", "skipped"):
        for path in data[key]:
            print(f"{labels[key]}: {path}")
    if data["config_path"]:
        print(f"{labels['config_path']}: {data['config_path']}")
    for item in data["dropped_directives"]:
        print(f"commented out: {item['directive']} ({item['reason']})")
    if not any(data[key] for key in ("linked", "copied", "removed", "skipped", "config_path")):
        print("nothing to share" if not args.dry_run else "nothing would change")
    return 0


def run_status(args: argparse.Namespace, ctx: AppContext) -> int:
    """Show the state of the shared setup."""
    info = ssh_sync.status(ctx.paths, winecmd.windows_user(ctx.env), _home(ctx))
    if ctx.json:
        ctx.print_json(info)
        return 0
    print(f"host ~/.ssh: {info['host_dir']}" + ("" if info["host_present"] else " (missing)"))
    if not info["exists"]:
        print(f"Wine .ssh:   {info['wine_dir']} (not shared yet - run 'fork-linux ssh sync')")
        return 0
    print(f"Wine .ssh:   {info['wine_dir']} (mode {info['mode']}, {'ok' if info['perms_ok'] else 'NOT private'})")
    print(f"managed:     {len(info['managed'])} file(s)")
    print(f"config:      {info['config']}")
    for name in info["insecure_files"]:
        print(f"insecure:    {name}")
    for name in info["broken_links"]:
        print(f"broken link: {name}")
    return 0
