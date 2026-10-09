"""``fork-linux run [PATH...]`` and ``fork-linux open PATH...``: start Fork or open repositories in it."""

from __future__ import annotations

import argparse

from ..cli import AppContext


def _add_options(parser: argparse.ArgumentParser) -> None:
    """Options shared by ``run`` and ``open``."""
    parser.add_argument(
        "--debug",
        action="store_true",
        help="log Wine errors and fixmes to a new wine-<time>.log (with -v: show them on the terminal)",
    )
    parser.add_argument("--wine-debug", metavar="CHANNELS", help="WINEDEBUG channels for this launch")
    driver = parser.add_mutually_exclusive_group()
    driver.add_argument(
        "--x11", dest="driver", action="store_const", const="x11", help="use Wine's X11 driver (XWayland)"
    )
    driver.add_argument(
        "--wayland",
        dest="driver",
        action="store_const",
        const="wayland",
        help="use Wine's experimental Wayland driver",
    )
    parser.add_argument("--no-setup", action="store_true", help="fail (exit 10) instead of running pending setup steps")
    parser.add_argument(
        "--no-hooks", action="store_true", help="skip the pre-launch snapshot, settings and ssh/git sync"
    )
    parser.add_argument(
        "--from-file-manager",
        action="store_true",
        help="a file opens the repository that contains it (used by file manager actions)",
    )
    parser.set_defaults(func=run, driver=None)


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Add ``run`` and ``open``."""
    run_parser = subparsers.add_parser(
        "run",
        help="start Fork, optionally opening repositories",
        description="Start Fork. Each PATH (a repository, or a file or folder inside one) opens in a tab; "
        "when Fork already runs, the paths go to its window.",
    )
    run_parser.add_argument("paths", metavar="PATH", nargs="*", help="repository to open")
    _add_options(run_parser)
    open_parser = subparsers.add_parser(
        "open",
        help="open repositories in Fork",
        description="Open each PATH (a repository, or a file or folder inside one) in Fork.",
    )
    open_parser.add_argument("paths", metavar="PATH", nargs="+", help="repository to open")
    _add_options(open_parser)


def run(args: argparse.Namespace, ctx: AppContext) -> int:
    """Hand over to :func:`fork_linux.launcher.run` (which replaces this process with Wine)."""
    from .. import launcher

    return launcher.run(
        ctx,
        args.paths,
        debug=args.debug,
        wine_debug=args.wine_debug,
        driver=args.driver,
        no_setup=args.no_setup,
        no_hooks=args.no_hooks,
        from_file_manager=args.from_file_manager,
    )
