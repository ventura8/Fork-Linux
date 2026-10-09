"""``fork-linux config get|set|unset|list|path|edit``: fork-linux's own settings (config.ini)."""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
from collections.abc import Mapping, Sequence
from typing import Any

from .. import config as config_mod
from .. import fsutil, sandbox
from ..cli import AppContext
from ..config import Config
from ..errors import ForkLinuxError, UsageError

FALLBACK_EDITORS = ("sensible-editor", "editor", "nano", "vi")


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Add ``config`` and its actions."""
    parser = subparsers.add_parser(
        "config",
        help="show or change fork-linux settings",
        description="Show or change fork-linux's settings in ~/.config/fork-linux/config.ini. "
        "Settings are named SECTION.KEY, e.g. wine.provider; 'config list' shows them all. "
        "Environment variables FORK_LINUX_<SECTION>_<KEY> override the file.",
    )
    actions = parser.add_subparsers(dest="config_action", metavar="ACTION", title="actions", required=True)

    get = actions.add_parser("get", help="print the value of a setting")
    get.add_argument("key", metavar="SECTION.KEY")
    get.set_defaults(func=run_get)

    set_ = actions.add_parser("set", help="change a setting (comments in config.ini are kept)")
    set_.add_argument("key", metavar="SECTION.KEY")
    set_.add_argument("value", metavar="VALUE", nargs=argparse.REMAINDER, help="the new value (may start with -)")
    set_.set_defaults(func=run_set)

    unset = actions.add_parser("unset", help="remove a setting from config.ini (back to its default)")
    unset.add_argument("key", metavar="SECTION.KEY")
    unset.set_defaults(func=run_unset)

    list_ = actions.add_parser("list", help="show every setting, its value and where it comes from")
    list_.set_defaults(func=run_list)

    path = actions.add_parser("path", help="print the location of config.ini")
    path.set_defaults(func=run_path)

    edit = actions.add_parser("edit", help="open config.ini in your editor ($VISUAL, $EDITOR)")
    edit.set_defaults(func=run_edit)


def _entry(config: Config, section: str, key: str) -> dict[str, Any]:
    """JSON description of one setting."""
    return {
        "key": f"{section}.{key}",
        "value": config.raw(section, key),
        "source": config.source(section, key),
        "default": config_mod.SCHEMA[section][key].default,
    }


def _warn(message: str) -> None:
    """``fork-linux: warning: <message>`` on stderr."""
    print(f"fork-linux: warning: {message}", file=sys.stderr)


def run_get(args: argparse.Namespace, ctx: AppContext) -> int:
    """Print one value (invalid values are printed too, with a warning)."""
    section, key = config_mod.parse_key(args.key)
    config = ctx.config
    why = config_mod.problem(config_mod.SCHEMA[section][key], config.raw(section, key))
    if why is not None:
        _warn(f"{section}.{key} has an invalid value: {why}")
    if ctx.json:
        ctx.print_json(_entry(config, section, key))
    else:
        print(config.raw(section, key))
    return 0


def run_set(args: argparse.Namespace, ctx: AppContext) -> int:
    """Validate and store one value."""
    section, key = config_mod.parse_key(args.key)
    if not args.value:
        raise UsageError(f"missing VALUE for {section}.{key}", hint=f"fork-linux config set {section}.{key} VALUE")
    value = " ".join(args.value)
    config = ctx.config
    config.set(section, key, value)
    if config.source(section, key) == config_mod.SOURCE_ENV:
        _warn(f"${config_mod.env_var(section, key)} is set and overrides this value")
    if ctx.json:
        ctx.print_json(_entry(config, section, key))
    else:
        print(f"{section}.{key} = {config.raw(section, key)}")
    return 0


def run_unset(args: argparse.Namespace, ctx: AppContext) -> int:
    """Remove one value from config.ini."""
    section, key = config_mod.parse_key(args.key)
    config = ctx.config
    removed = config.unset(section, key)
    if ctx.json:
        ctx.print_json({**_entry(config, section, key), "removed": removed})
    elif removed:
        print(f"{section}.{key} is back to its default: {config.raw(section, key)!r}")
    else:
        print(f"{section}.{key} was not set in {config.path}; it is {config.raw(section, key)!r}")
    return 0


def run_list(args: argparse.Namespace, ctx: AppContext) -> int:
    """Show every setting with its source; problems go to stderr."""
    config = ctx.config
    for problem in config.problems(include_unknown=False):
        _warn(problem)
    if ctx.json:
        ctx.print_json([_entry(config, section, key) for section, key, _value, _source in config.items()])
        return 0
    rows = [(f"{section}.{key}", value, source) for section, key, value, source in config.items()]
    width = max(len(name) for name, _value, _source in rows)
    for name, value, source in rows:
        note = "" if source == config_mod.SOURCE_DEFAULT else f"  ({source})"
        print(f"{name:<{width}} = {value}{note}".rstrip())
    return 0


def run_path(args: argparse.Namespace, ctx: AppContext) -> int:
    """Print where config.ini lives."""
    path = ctx.paths.config_file
    if ctx.json:
        ctx.print_json({"path": str(path), "exists": path.is_file()})
    else:
        print(path)
    return 0


def editor_argv(env: Mapping[str, str], which: Any) -> list[str] | None:
    """The editor command: ``$VISUAL``, ``$EDITOR``, else the first fallback editor found."""
    for name in ("VISUAL", "EDITOR"):
        value = env.get(name, "").strip()
        if not value:
            continue
        try:
            return shlex.split(value)
        except ValueError as exc:
            raise UsageError(f"cannot parse ${name} ({value!r}): {exc}") from None
    for candidate in FALLBACK_EDITORS:
        found = which(candidate)
        if found:
            return [found]
    return None


def run_interactive(argv: Sequence[str], env: Mapping[str, str]) -> int:
    """Run an interactive program on the terminal (stdin/stdout not captured); its exit status."""
    try:
        return subprocess.run(list(argv), env=dict(env), check=False).returncode
    except FileNotFoundError:
        return 127
    except OSError:
        return 126


def run_edit(args: argparse.Namespace, ctx: AppContext) -> int:
    """Open config.ini (created from the commented defaults if missing) and check it afterwards."""
    path = ctx.paths.config_file
    argv = editor_argv(ctx.env, ctx.runner.which)
    if argv is None:
        raise UsageError("no text editor found", hint=f"set $EDITOR, or edit {path} with any editor")
    if not os.path.lexists(path):
        fsutil.atomic_write(path, config_mod.template(), mode=config_mod.FILE_MODE)
    command = sandbox.host_argv([*argv, str(path)], sandbox.detect(ctx.env))
    status = run_interactive(command, sandbox.clean_env(ctx.env))
    if status != 0:
        raise ForkLinuxError(f"the editor ({argv[0]}) exited with status {status}")
    for problem in Config.load(ctx.paths, ctx.env).problems(include_unknown=False):
        _warn(problem)
    return 0
