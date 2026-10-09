"""``fork-linux desktop install|remove|status``: menu entry, icon and file-manager actions."""

from __future__ import annotations

import argparse
from pathlib import Path

from .. import desktop_integration, winecmd
from ..cli import AppContext
from ..fork_layout import ForkLayout


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Add ``desktop`` and its actions."""
    parser = subparsers.add_parser(
        "desktop",
        help="add or remove the menu entry and 'Open in Fork' actions",
        description="Manage the per-user desktop integration: the application menu entry, its icon "
        "(extracted from your own Fork.exe), 'Open in Fork' actions for file managers and the "
        "fork / fork-linux command aliases in ~/.local/bin. Only files fork-linux wrote are ever removed.",
    )
    actions = parser.add_subparsers(dest="desktop_action", metavar="ACTION", title="actions", required=True)
    install = actions.add_parser("install", help="install (or refresh) the integration")
    install.add_argument(
        "--file-managers",
        metavar="LIST",
        default="all",
        help="comma separated: " + ", ".join(desktop_integration.FILE_MANAGERS) + ", all or none (default: all)",
    )
    install.add_argument("--no-menu", action="store_true", help="no application menu entry")
    install.add_argument("--no-icons", action="store_true", help="do not install icons")
    install.add_argument("--cli-alias", action="store_true", help="also link fork and fork-linux into ~/.local/bin")
    install.set_defaults(func=run_install)
    actions.add_parser("remove", help="remove everything fork-linux installed").set_defaults(func=run_remove)
    actions.add_parser("status", help="show what is installed").set_defaults(func=run_status)


def _print_paths(ctx: AppContext, key: str, items: list[Path], empty: str, verb: str) -> None:
    """Print the paths touched (or JSON ``{key: [...]}``)."""
    if ctx.json:
        ctx.print_json({key: [str(item) for item in items]})
        return
    if not items:
        print(empty)
    for item in items:
        print(f"{verb} {item}")


def run_install(args: argparse.Namespace, ctx: AppContext) -> int:
    """Install the integration."""
    layout = ForkLayout(ctx.paths, winecmd.windows_user(ctx.env))
    installed = desktop_integration.install(
        ctx.paths,
        ctx.env,
        fork_exe=layout.exe if layout.exe.is_file() else None,
        menu=not args.no_menu,
        icons=not args.no_icons,
        file_managers=args.file_managers,
        cli_alias=args.cli_alias,
        runner=ctx.runner,
    )
    _print_paths(ctx, "installed", installed, "nothing installed", "installed")
    return 0


def run_remove(args: argparse.Namespace, ctx: AppContext) -> int:
    """Remove the integration."""
    removed = desktop_integration.remove(ctx.paths, ctx.env, runner=ctx.runner)
    _print_paths(ctx, "removed", removed, "nothing to remove", "removed")
    return 0


def run_status(args: argparse.Namespace, ctx: AppContext) -> int:
    """Show what is installed."""
    info = desktop_integration.status(ctx.paths, ctx.env)
    if ctx.json:
        ctx.print_json(info)
        return 0
    print(f"launcher: {' '.join(info['launcher'])}")
    if info["system_desktop"]:
        print(f"menu entry installed by a package: {info['system_desktop']}")
    if not info["entries"]:
        print("not installed - run 'fork-linux desktop install'")
    for entry in info["entries"]:
        print(f"{entry['state']:<9} {entry['kind']:<16} {entry['path']}")
    return 0
