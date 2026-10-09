"""``fork-linux git-bridge status|enable|disable``: the experimental native-git bridge."""

from __future__ import annotations

import argparse

from .. import bridge
from ..cli import AppContext


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Add ``git-bridge`` and its actions."""
    parser = subparsers.add_parser(
        "git-bridge",
        help="experimental: let Fork use the Linux git",
        description="Experimental: route Fork's git calls to the Linux git through the fork-linux bridge. "
        "Only the [git] bridge setting changes; Fork's settings.json is never touched, and "
        "'git-bridge disable' goes back to Fork's bundled git.",
    )
    actions = parser.add_subparsers(dest="bridge_action", metavar="ACTION", title="actions", required=True)
    actions.add_parser("status", help="is the bridge enabled and available").set_defaults(func=run_status)
    actions.add_parser("enable", help="use the bridge from the next Fork start").set_defaults(func=run_enable)
    actions.add_parser("disable", help="go back to Fork's bundled git").set_defaults(func=run_disable)


def _show(ctx: AppContext) -> None:
    """Print the bridge status."""
    info = bridge.status(ctx)
    if ctx.json:
        ctx.print_json(info)
        return
    print(f"enabled:   {'yes' if info['enabled'] else 'no'}")
    print(f"available: {'yes' if info['available'] else 'no'}")
    print(f"shims:     {info['shims_dir'] or 'not built'}")
    print(f"helper:    {info['helper']}")
    if info["reason"]:
        print(f"note:      {info['reason']}")


def run_status(args: argparse.Namespace, ctx: AppContext) -> int:
    """Show the status."""
    _show(ctx)
    return 0


def run_enable(args: argparse.Namespace, ctx: AppContext) -> int:
    """Enable the bridge (restart Fork to use it)."""
    bridge.enable(ctx)
    if not ctx.json:
        print("git bridge enabled (experimental); it takes effect the next time Fork starts")
    else:
        _show(ctx)
    return 0


def run_disable(args: argparse.Namespace, ctx: AppContext) -> int:
    """Disable the bridge."""
    bridge.disable(ctx)
    if not ctx.json:
        print("git bridge disabled; Fork uses its bundled git from its next start")
    else:
        _show(ctx)
    return 0
