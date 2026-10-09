"""``fork-linux doctor``: check the host, Wine, the prefix, Fork and the integrations.

``--fix`` repairs what it can (re-running setup steps), ``--deep`` adds the
bundled-git self-test under Wine, ``--network`` the download-host checks,
``--check ID|GROUP`` runs only the named checks, ``--json`` prints the report
(schema 1) and ``--gui`` also shows the summary in a zenity window. The exit
code is 17 (``ChecksFailed``) when any check fails.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from .. import doctor, sandbox
from .. import ui as ui_mod
from ..cli import AppContext
from ..errors import ChecksFailed

LABELS = {"ok": "ok", "info": "info", "warn": "WARN", "fail": "FAIL"}
GUI_TITLE = "Fork for Linux (unofficial) - doctor"


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Add the ``doctor`` command."""
    parser = subparsers.add_parser(
        "doctor",
        help="diagnose problems (and fix what can be fixed)",
        description="Check the host, Wine, the Wine prefix, Fork and the desktop integration. "
        "Exits with code 17 when a check fails.",
    )
    parser.add_argument("--fix", action="store_true", help="repair what can be repaired, then check again")
    parser.add_argument("--deep", action="store_true", help="also run Fork's bundled git under Wine (slow)")
    parser.add_argument("--network", action="store_true", help="also check the download hosts and Fork's feed")
    parser.add_argument(
        "--check",
        metavar="ID",
        dest="only",
        action="extend",
        nargs="+",
        help="run only these checks (ids such as wine.present, or groups such as wine)",
    )
    parser.set_defaults(func=run, only=None)


def render_text(results: Sequence[tuple[doctor.Check, doctor.Result]], *, verbose: bool = False) -> str:
    """The human-readable report, one line per check, hints indented below."""
    lines = []
    group = None
    for check, result in results:
        if check.group != group:
            group = check.group
            lines.append(f"{group}:")
        lines.append(f"  [{LABELS[result.status]:>4}] {check.id:<26} {result.detail}")
        if result.hint and (result.status in doctor.NOT_OK or verbose):
            lines.append(f"{'':9}hint: {result.hint}")
    counts = doctor.summary(results)
    lines.append(f"{counts['ok']} ok, {counts['warn']} warning(s), {counts['fail']} failed, {counts['info']} info")
    return "\n".join(lines) + "\n"


def _render_fix(report: doctor.FixReport) -> str:
    lines = [f"fixed: {action}" for action in report.actions]
    lines.extend(f"fix failed: {error}" for error in report.errors)
    return "".join(line + "\n" for line in lines)


def show_gui(ctx: AppContext, text: str) -> bool:
    """Show ``text`` in a zenity text window when zenity and a display exist; True if shown."""
    env = sandbox.clean_env(ctx.env)
    if not (env.get("DISPLAY") or env.get("WAYLAND_DISPLAY")):
        return False
    zenity = ctx.runner.which("zenity", env.get("PATH"))
    if zenity is None:
        return False
    ctx.runner.run(
        [zenity, "--text-info", f"--title={GUI_TITLE}", "--width=760", "--height=520", "--font=monospace"],
        env=env,
        input=text,
    )
    return True


def run(args: argparse.Namespace, ctx: AppContext) -> int:
    """Run the checks, optionally fix, print the report; raise :class:`ChecksFailed` on failures."""
    dctx = doctor.DoctorCtx.from_app(ctx)
    results = doctor.run_checks(dctx, deep=args.deep, network=args.network, only=args.only)
    report = None
    if args.fix:
        dctx.ui = ui_mod.choose(ctx.env, gui=ctx.gui, mode=ctx.config.get("ui", "progress"), runner=ctx.runner)
        dctx.reset()
        report = doctor.fix(dctx, results)
        results = report.results
    if ctx.json:
        data = doctor.report_json(results)
        if report is not None:
            data["fix"] = {"actions": report.actions, "errors": report.errors}
        ctx.print_json(data)
    else:
        text = (_render_fix(report) if report is not None else "") + render_text(results, verbose=ctx.verbosity > 0)
        print(text, end="")
        if ctx.gui:
            show_gui(ctx, text)
    sys.stdout.flush()
    failing = doctor.failed(results)
    if failing:
        raise ChecksFailed(
            f"{len(failing)} check(s) failed: {', '.join(failing)}",
            hint="see the hints above; 'fork-linux doctor --fix' repairs what it can",
        )
    return 0
