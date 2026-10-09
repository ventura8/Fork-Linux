"""``fork [PATH...]`` and ``fork open PATH...``: open repositories in Fork.

``fork`` is a symlink to the ``fork-linux`` launcher; :func:`fork_linux.cli.main`
recognises the program name and calls :func:`main`, which forwards to
``fork-linux run``. Global options (``--prefix PATH``, ``--gui``/``--no-gui``,
``--offline``, ``-v``, ``-q``, ``--allow-root``) may appear anywhere before
``--``; everything else is a path.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence

from . import cli, credits
from .errors import ExitCode

OPEN = "open"
_VALUE_OPTIONS = frozenset({"--prefix"})

USAGE = f"""\
usage: fork [OPTIONS] [PATH...]
       fork open [OPTIONS] PATH...
       fork --help | --version

Open each PATH (a repository, or a file or folder inside one) in Fork.
With no PATH, start Fork. This is a shortcut for 'fork-linux run'.

OPTIONS are fork-linux's global options: --prefix PATH, --gui, --no-gui,
--offline, -v/--verbose, -q/--quiet, --allow-root. Use -- before paths
that start with '-'. See 'fork-linux --help' for everything else.

{credits.short_footer()}"""


def split_args(argv: Sequence[str]) -> tuple[list[str], list[str]]:
    """Separate global options from paths; everything after ``--`` is a path."""
    options: list[str] = []
    paths: list[str] = []
    items = iter(argv)
    for arg in items:
        if arg == "--":
            paths.extend(items)
            break
        if arg.startswith("-") and arg != "-":
            options.append(arg)
            value = next(items, None) if arg in _VALUE_OPTIONS else None
            if value is not None:
                options.append(value)
        else:
            paths.append(arg)
    return options, paths


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point of ``fork``; returns the exit status of ``fork-linux run``."""
    args = list(sys.argv[1:] if argv is None else argv)
    is_open = bool(args) and args[0] == OPEN
    options, paths = split_args(args[1:] if is_open else args)
    if "-h" in options or "--help" in options:
        print(USAGE)
        return int(ExitCode.OK)
    if "-V" in options or "--version" in options:
        print(cli.version_line())
        return int(ExitCode.OK)
    if is_open and not paths:
        print(USAGE, file=sys.stderr)
        cli.print_error("fork open: missing PATH")
        return int(ExitCode.USAGE)
    return cli.main(["run", *options, "--", *paths], prog=cli.PROG)
