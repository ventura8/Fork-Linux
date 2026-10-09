"""``fork-linux version``: the fork-linux version, its packaging flavour and the Fork credit line."""

from __future__ import annotations

import argparse

from .. import credits
from ..cli import AppContext
from ..version import get_flavor, get_version


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Add the ``version`` command."""
    parser = subparsers.add_parser(
        "version",
        help="show the fork-linux version",
        description="Show the fork-linux version and how it was packaged.",
    )
    parser.set_defaults(func=run)


def run(args: argparse.Namespace, ctx: AppContext) -> int:
    """Print ``fork-linux <version> (<flavor>)`` (or JSON) and the Fork credit line."""
    version, flavor = get_version(), get_flavor()
    if ctx.json:
        ctx.print_json({"fork_linux": version, "flavor": flavor})
        return 0
    print(f"fork-linux {version} ({flavor})")
    print(credits.short_footer())
    return 0
