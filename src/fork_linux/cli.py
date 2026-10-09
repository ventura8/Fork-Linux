"""The ``fork-linux`` command line: global options, subcommand routing, errors -> exit codes.

Subcommands live in :mod:`fork_linux.commands`; each module listed in
``commands.COMMANDS`` exposes ``register(subparsers)``, which adds its
parser(s) and sets ``func=handler`` with ``handler(args, ctx) -> int``.
Global options are accepted before the command and after it (they are added
to every subcommand parser automatically, so commands must not re-add them).

Invoked as ``fork`` (the symlink), :func:`main` hands over to :mod:`fork_cli`.
"""

from __future__ import annotations

import argparse
import contextvars
import importlib
import json
import logging
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, NoReturn

from . import APP_NAME, commands, credits, logging_setup
from .config import Config
from .errors import ExitCode, ForkLinuxError, UnsupportedEnvironment
from .paths import Paths
from .procrun import Runner
from .version import get_flavor, get_version

log = logging.getLogger(__name__)

PROG = "fork-linux"
FORK_PROG = "fork"
ALLOW_ROOT_ENV = "FORK_LINUX_ALLOW_ROOT"


def version_line() -> str:
    """``fork-linux <version> (<flavor>)``."""
    return f"{PROG} {get_version()} ({get_flavor()})"


def print_error(message: str, hint: str = "") -> None:
    """``fork-linux: error: <message>`` and ``hint: <hint>`` on stderr."""
    print(f"{PROG}: error: {message}", file=sys.stderr)
    if hint:
        print(f"hint: {hint}", file=sys.stderr)


class AppContext:
    """What a command handler needs; paths, config and logging are created lazily."""

    def __init__(
        self,
        args: argparse.Namespace,
        *,
        env: Mapping[str, str] | None = None,
        runner: Runner | None = None,
    ) -> None:
        self.args = args
        self.env: dict[str, str] = dict(os.environ if env is None else env)
        self.runner = Runner() if runner is None else runner
        self._paths: Paths | None = None
        self._config: Config | None = None

    @property
    def json(self) -> bool:
        """``--json``: machine-readable output."""
        return bool(getattr(self.args, "json", False))

    @property
    def gui(self) -> bool | None:
        """``--gui`` (True), ``--no-gui`` (False) or automatic (None)."""
        return getattr(self.args, "gui", None)

    @property
    def offline(self) -> bool:
        """``--offline``: never use the network."""
        return bool(getattr(self.args, "offline", False))

    @property
    def verbosity(self) -> int:
        """``-v`` count minus ``-q`` count."""
        return int(getattr(self.args, "verbose", 0) or 0) - int(getattr(self.args, "quiet", 0) or 0)

    @property
    def paths(self) -> Paths:
        """XDG paths for this user and the selected prefix (``--prefix``)."""
        if self._paths is None:
            self._paths = Paths.from_env(self.env, prefix=getattr(self.args, "prefix", None))
        return self._paths

    @property
    def config(self) -> Config:
        """Effective settings (``config.ini`` + ``FORK_LINUX_*`` overrides)."""
        if self._config is None:
            self._config = Config.load(self.paths, self.env)
        return self._config

    def print_json(self, data: Any) -> None:
        """Print ``data`` as indented JSON on stdout."""
        print(json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False))


def _add_global_options(parser: argparse.ArgumentParser, *, suppress: bool) -> None:
    """Add the global options; on subcommand parsers their defaults are suppressed."""

    def default(value: Any) -> Any:
        return argparse.SUPPRESS if suppress else value

    group = parser.add_argument_group("global options")
    group.add_argument("--json", action="store_true", default=default(False), help="machine-readable JSON output")
    group.add_argument(
        "--prefix",
        metavar="PATH",
        type=Path,
        default=default(None),
        help="use this Wine prefix instead of the default one",
    )
    group.add_argument(
        "--gui",
        action=argparse.BooleanOptionalAction,
        default=default(None),
        help="always (or never) use graphical dialogs; default: automatic",
    )
    group.add_argument("--offline", action="store_true", default=default(False), help="never use the network")
    group.add_argument(
        "-v", "--verbose", action="count", default=default(0), help="more output (repeat for debug output)"
    )
    group.add_argument("-q", "--quiet", action="count", default=default(0), help="less output")
    group.add_argument(
        "--allow-root",
        action="store_true",
        default=default(False),
        help=f"allow running as root (containers and CI only; or {ALLOW_ROOT_ENV}=1)",
    )


class _ParserExit(Exception):
    """argparse finished early: ``--help``, ``--version`` or a usage error (``status`` is the exit status)."""

    def __init__(self, status: int) -> None:
        super().__init__(status)
        self.status = status


# True while :func:`_dispatch` parses: early exits then raise :class:`_ParserExit` instead of SystemExit.
_TRAP_EXITS: contextvars.ContextVar[bool] = contextvars.ContextVar("fork_linux_trap_parser_exits", default=False)


class _Parser(argparse.ArgumentParser):
    """An argument parser whose early exits can be trapped (see :data:`_TRAP_EXITS`)."""

    def exit(self, status: int = 0, message: str | None = None) -> NoReturn:
        if message:
            self._print_message(message, sys.stderr)
        if _TRAP_EXITS.get():
            raise _ParserExit(status)
        sys.exit(status)


class _CommandParser(_Parser):
    """A subcommand parser that also accepts the global options."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("conflict_handler", "resolve")
        super().__init__(*args, **kwargs)
        _add_global_options(self, suppress=True)


def _epilog() -> str:
    """Credits footer shown under ``--help``."""
    return (
        f"{APP_NAME} is NOT affiliated with the Fork developers.\n"
        f"Report problems at {credits.LINKS['project_issues']}.\n"
        f"{credits.short_footer()}"
    )


def build_parser() -> argparse.ArgumentParser:
    """The top-level parser with every command of ``commands.COMMANDS`` registered."""
    parser = _Parser(
        prog=PROG,
        description=f"{APP_NAME}: run the official Fork for Windows git client under Wine.",
        epilog=_epilog(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("-V", "--version", action="version", version=version_line())
    _add_global_options(parser, suppress=False)
    subparsers = parser.add_subparsers(
        dest="command", metavar="COMMAND", title="commands", parser_class=_CommandParser
    )
    for name in commands.COMMANDS:
        module = importlib.import_module(f"{commands.__name__}.{name}")
        module.register(subparsers)
    return parser


def _is_root() -> bool:
    """True when running with effective uid 0 (a seam for tests)."""
    return os.geteuid() == 0


def check_root(ctx: AppContext) -> None:
    """Refuse to run as root unless explicitly allowed (:class:`UnsupportedEnvironment`)."""
    if not _is_root() or getattr(ctx.args, "allow_root", False) or ctx.env.get(ALLOW_ROOT_ENV) == "1":
        return
    raise UnsupportedEnvironment(
        "refusing to run as root",
        hint="run fork-linux as your normal user (Wine prefixes are per user); "
        f"use --allow-root or {ALLOW_ROOT_ENV}=1 only in containers and CI",
    )


def _configure_logging(ctx: AppContext) -> Path | None:
    """Set up logging; returns the log file, or None when paths are unusable."""
    try:
        paths = ctx.paths
    except ForkLinuxError:
        return None
    logger = logging_setup.configure(paths, ctx.verbosity)
    for handler in logger.handlers:
        if isinstance(handler, logging.FileHandler):
            return Path(handler.baseFilename)
    return None


def _exit_status(code: object) -> int:
    """Exit status carried by a ``SystemExit`` (argparse uses 0 and 2)."""
    if code is None:
        return int(ExitCode.OK)
    return code if isinstance(code, int) else int(ExitCode.ERROR)


def _parse(parser: argparse.ArgumentParser, argv: list[str]) -> argparse.Namespace:
    """``parser.parse_args(argv)`` with early exits raised as :class:`_ParserExit`."""
    token = _TRAP_EXITS.set(True)
    try:
        return parser.parse_args(argv)
    finally:
        _TRAP_EXITS.reset(token)


def _dispatch(argv: list[str]) -> int:
    """Parse ``argv`` and run the selected command, mapping failures to exit codes."""
    parser = build_parser()
    try:
        args = _parse(parser, argv)
    except _ParserExit as exc:
        return _exit_status(exc.status)
    handler = getattr(args, "func", None)
    if handler is None:
        parser.print_help(sys.stderr)
        return int(ExitCode.USAGE)
    ctx = AppContext(args)
    log_file: Path | None = None
    try:
        check_root(ctx)
        log_file = _configure_logging(ctx)
        result = handler(args, ctx)
        return int(ExitCode.OK if result is None else result)
    except ForkLinuxError as exc:
        log.debug("%s failed", args.command, exc_info=True)
        print_error(exc.message, exc.hint)
        return int(exc.exit_code)
    except Exception as exc:
        log.debug("unexpected error in %s", args.command, exc_info=True)
        where = f"the details are in {log_file}; " if log_file is not None else ""
        print_error(
            f"unexpected {type(exc).__name__}: {exc}",
            f"{where}please report it at {credits.LINKS['project_issues']}",
        )
        return int(ExitCode.ERROR)
    finally:
        logging_setup.reset()


def main(argv: Sequence[str] | None = None, prog: str | None = None) -> int:
    """Entry point of ``fork-linux`` (and of ``fork``, chosen by the program name)."""
    if os.path.basename(prog or sys.argv[0]) == FORK_PROG:
        from . import fork_cli

        return fork_cli.main(None if argv is None else list(argv))
    try:
        return _dispatch(list(sys.argv[1:] if argv is None else argv))
    except KeyboardInterrupt:
        print(f"{PROG}: interrupted", file=sys.stderr)
        return int(ExitCode.INTERRUPTED)
