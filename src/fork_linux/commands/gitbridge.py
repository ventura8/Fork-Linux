"""``fork-linux git-bridge status|enable|disable|record``: the experimental native-git bridge."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .. import bridge, launcher, procs, resources
from .. import ui as ui_mod
from ..cli import AppContext
from ..errors import ForkLinuxError
from ..procrun import tail

BUILD_TIMEOUT = 3600.0
RESTART_HINT = "it takes effect the next time Fork starts (close Fork, then start it with 'fork' or the menu entry)"


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
    actions.add_parser("status", help="is the bridge enabled and ready").set_defaults(func=run_status)
    enable = actions.add_parser("enable", help="use the bridge from the next Fork start")
    enable.add_argument(
        "--build",
        action="store_true",
        help="in a source checkout, build the bridge with scripts/build-bridge.sh without asking when it is missing",
    )
    enable.set_defaults(func=run_enable)
    actions.add_parser("disable", help="go back to Fork's bundled git").set_defaults(func=run_disable)
    record = actions.add_parser(
        "record", help="debugging: the bridge's git.exe forwards to Fork's bundled git (FL_BRIDGE_MODE=record)"
    )
    record.add_argument("state", choices=("on", "off"), help="on: forward to bundled git; off: run the Linux git")
    record.set_defaults(func=run_record)


def _show(ctx: AppContext) -> None:
    """Print the bridge status."""
    info = bridge.status(ctx, launcher.read_session(ctx.paths))
    if ctx.json:
        ctx.print_json(info)
        return
    daemon = info["daemon"]
    print(f"enabled:   {'yes' if info['enabled'] else 'no'}")
    print(f"available: {'yes' if info['available'] else 'no'}")
    print(f"ready:     {'yes' if info['ready'] else 'no'}")
    print(f"mode:      {info['mode']}")
    print(f"git:       {info['git_version'] or ('not found' if info['enabled'] else 'not checked (bridge off)')}")
    print(f"shims:     {info['shims_dir'] or 'not built'}")
    print(f"helper:    {info['helper']}")
    print(f"daemon:    {'pid {pid}, port {port}'.format(**daemon) if daemon else 'not running'}")
    if info["reason"]:
        print(f"note:      {info['reason']}")


def run_status(args: argparse.Namespace, ctx: AppContext) -> int:
    """Show the status."""
    _show(ctx)
    return 0


def _build(ctx: AppContext, script: Path) -> None:
    """Run ``scripts/build-bridge.sh`` (meson in Docker); its output goes to a log file."""
    log_file = ctx.paths.logs_dir / "build-bridge.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    print(f"building the bridge with {script} (log: {log_file}) ...", file=sys.stderr)
    completed = ctx.runner.run([str(script)], timeout=BUILD_TIMEOUT, log_file=log_file)
    if not completed.ok:
        output = log_file.read_text(encoding="utf-8", errors="replace") if log_file.is_file() else ""
        raise ForkLinuxError(
            f"building the bridge failed (exit {completed.returncode})",
            hint=f"see {log_file}: {tail(output, 5) or 'no output'}",
        )


def _offer_build(args: argparse.Namespace, ctx: AppContext) -> None:
    """In a source checkout without a bridge build, build it (``--build``, or after asking)."""
    script = resources.bridge_build_script()
    if script is None or not bridge.check(ctx).missing:
        return
    if not args.build:
        ui = ui_mod.choose(ctx.env, gui=ctx.gui, mode=ctx.config.get("ui", "progress"), runner=ctx.runner)
        text = f"The git bridge is not built in this source checkout.\n\nBuild it now with {script}?"
        if not ui.confirm("Build the git bridge?", text, default=False):
            return
    _build(ctx, script)


def run_enable(args: argparse.Namespace, ctx: AppContext) -> int:
    """Enable the bridge (restart Fork to use it)."""
    _offer_build(args, ctx)
    for warning in bridge.enable(ctx):
        print(f"warning: {warning}", file=sys.stderr)
    if ctx.json:
        _show(ctx)
    else:
        print(f"git bridge enabled (experimental); {RESTART_HINT}")
        if procs.fork_running(ctx.paths.prefix):
            print("Fork is running now and keeps its bundled git until it is restarted")
    return 0


def run_disable(args: argparse.Namespace, ctx: AppContext) -> int:
    """Disable the bridge."""
    bridge.disable(ctx)
    if ctx.json:
        _show(ctx)
    else:
        print(f"git bridge disabled; Fork uses its bundled git again: {RESTART_HINT}")
    return 0


def run_record(args: argparse.Namespace, ctx: AppContext) -> int:
    """Switch the shims between the Linux git and record mode (forward to Fork's bundled git)."""
    bridge.set_mode(ctx, args.state == "on")
    if ctx.json:
        _show(ctx)
    elif args.state == "on":
        print(
            "record mode on: the bridge's git.exe forwards every call unchanged to Fork's bundled git "
            f"(FL_BRIDGE_MODE=record); {RESTART_HINT}"
        )
    else:
        print(f"record mode off: the bridge runs the Linux git; {RESTART_HINT}")
    return 0
