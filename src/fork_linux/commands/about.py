"""``fork-linux about`` (alias ``credits``): who makes Fork, where to buy it, and the disclaimer."""

from __future__ import annotations

import argparse

from .. import APP_NAME, credits, sandbox
from ..cli import AppContext

# zenity --info exit codes that mean "the dialog was shown" (OK, or closed).
_SHOWN = (0, 1)


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Add the ``about`` command and its ``credits`` alias."""
    parser = subparsers.add_parser(
        "about",
        aliases=["credits"],
        help="credits, disclaimer and where to buy Fork",
        description="Show who makes Fork, where to buy a license, and the unofficial-project disclaimer.",
    )
    parser.set_defaults(func=run)


def _show_dialog(ctx: AppContext, text: str) -> bool:
    """Show ``text`` with ``zenity --info``; False if zenity is missing or failed."""
    zenity = ctx.runner.which("zenity")
    if zenity is None:
        return False
    argv = [zenity, "--info", "--no-markup", "--width=640", f"--title=About {APP_NAME}", f"--text={text}"]
    return ctx.runner.run(argv, env=sandbox.clean_env(ctx.env)).returncode in _SHOWN


def run(args: argparse.Namespace, ctx: AppContext) -> int:
    """Print the credits (``--json``: structured; ``--gui``: a dialog when zenity is available)."""
    if ctx.json:
        ctx.print_json(
            {
                "app": APP_NAME,
                "developers": list(credits.DEVELOPERS),
                "links": dict(credits.LINKS),
                "prior_art": [{"name": n, "url": u, "note": note} for n, u, note in credits.PRIOR_ART],
                "disclaimer": credits.disclaimer(),
            }
        )
        return 0
    text = credits.render_text()
    if ctx.gui and _show_dialog(ctx, text):
        return 0
    print(text, end="")
    return 0
