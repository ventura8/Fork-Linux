"""``fork-linux setup``: install (or resume installing) Wine, .NET and the official Fork.

Setup is idempotent and resumable: every step records a marker, so running it
again only does what is missing or changed. It never starts Fork itself.
``--reset`` deletes our Wine prefix first (after a warning about Fork's
license activations) and sets everything up from scratch.
"""

from __future__ import annotations

import argparse
import importlib
import os
from typing import Any

from .. import fork_install, fsutil, procs, wine_provider, winecmd
from .. import manifest as manifest_mod
from ..cli import AppContext
from ..errors import Declined, ForkLinuxError, UsageError
from ..locking import FileLock
from ..state import State

SCHEMA = 1
WINE_PROVIDERS = ("managed", "system", "flatpak")
DOTNET_CHOICES = ("auto", "dotnet48", "dotnet472")
RESET_TITLE = "Reset the Fork for Linux (unofficial) prefix"
# Consent survives a reset: the user already read and accepted Fork's license.
KEPT_ON_RESET = ("consent",)


def _wine_choice(value: str) -> str:
    """``managed``, ``system``, ``flatpak`` or an absolute path to a Wine root or binary."""
    if value in WINE_PROVIDERS or os.path.isabs(value):
        return value
    raise argparse.ArgumentTypeError(f"use {', '.join(WINE_PROVIDERS)} or an absolute path, not {value!r}")


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Add the ``setup`` command."""
    parser = subparsers.add_parser(
        "setup",
        help="install or repair Wine, .NET and the official Fork (resumable)",
        description=(
            "Download and set up everything Fork for Windows needs under Wine, then install the "
            "official Fork. Each step is recorded, so setup resumes where it stopped and only "
            "re-runs what changed. Setup never starts Fork."
        ),
    )
    version = parser.add_mutually_exclusive_group()
    version.add_argument("--fork-version", metavar="VERSION", help="install this Fork version")
    version.add_argument(
        "--latest", action="store_true", help="install the newest Fork release (verified after install)"
    )
    parser.add_argument(
        "--allow-untested", action="store_true", help="allow a Fork version the manifest does not pin"
    )
    parser.add_argument(
        "--wine",
        type=_wine_choice,
        metavar="{managed,system,flatpak,PATH}",
        help="which Wine to use (saved as wine.provider)",
    )
    parser.add_argument("--dotnet", choices=DOTNET_CHOICES, default="auto", help=".NET Framework verb to install")
    parser.add_argument(
        "--accept-fork-eula",
        action="store_true",
        help="accept Fork's license agreement (https://git-fork.com/license) without asking",
    )
    parser.add_argument(
        "--only", action="append", metavar="STEP", help="run only this step (repeatable; see --list-steps)"
    )
    parser.add_argument("--from-step", metavar="STEP", help="run this step and every step after it")
    parser.add_argument("--force", action="store_true", help="re-run steps even when they are done")
    parser.add_argument(
        "--reset", action="store_true", help="delete the Wine prefix (asks first) and set up from scratch"
    )
    parser.add_argument(
        "--no-launch", action="store_true", help="do not start Fork afterwards (the default; setup never does)"
    )
    parser.add_argument("--list-steps", action="store_true", help="list the setup steps and whether they are done")
    parser.set_defaults(func=run)


def _bootstrap() -> Any:
    """The bootstrap engine, imported on first use (a seam for tests)."""
    return importlib.import_module(f"{__package__.rpartition('.')[0]}.bootstrap")


def _make_ctx(args: argparse.Namespace, ctx: AppContext) -> Any:
    """The bootstrap context for ``args`` (``--wine`` is saved to the configuration)."""
    if args.wine and ctx.config.raw("wine", "provider") != args.wine:
        ctx.config.set("wine", "provider", args.wine)
    return _bootstrap().Ctx.from_app(
        ctx,
        accept_eula=args.accept_fork_eula,
        fork_version=args.fork_version,
        latest=args.latest,
        allow_untested=args.allow_untested,
        wine_choice=args.wine,
        dotnet=args.dotnet,
        force=args.force,
    )


def _status(boot: Any, bctx: Any, step: Any) -> str:
    if step.always:
        return "always"
    return "done" if boot.is_done(step, bctx) else "pending"


def _step_row(boot: Any, bctx: Any, step: Any) -> dict[str, Any]:
    """One step of ``--list-steps``.

    ``status`` also verifies the step's result; ``recorded`` only says whether its marker
    still matches this release's revision and inputs (``inputs_hash``), which is what a
    launch checks: after an upgrade the steps that are no longer recorded run again.
    """
    row = {
        "id": step.id,
        "title": step.title,
        "status": _status(boot, bctx, step),
        "needs_network": step.needs_network,
        "rev": step.rev,
        "inputs_hash": None,
        "recorded": None,
    }
    if not step.always:
        row["inputs_hash"] = boot.inputs_hash(step, bctx)
        row["recorded"] = boot.is_done(step, bctx, cheap=True)
    return row


def list_steps(ctx: AppContext, boot: Any, bctx: Any) -> int:
    """Print every step with its state."""
    rows = [_step_row(boot, bctx, step) for step in boot.default_steps()]
    if ctx.json:
        ctx.print_json(
            {
                "schema": SCHEMA,
                "complete": bctx.state.get("setup.complete") is True,
                "needs_setup": boot.needs_setup(bctx),
                "revisions": {"bootstrap": bctx.manifest.bootstrap_revision, "manifest": bctx.manifest.revision},
                "steps": rows,
            }
        )
    else:
        width = max(len(row["id"]) for row in rows)
        for row in rows:
            print(f"{row['status']:<8} {row['id']:<{width}}  {row['title']}")
    return 0


def _reset_text(bctx: Any) -> str:
    return (
        f"This deletes the Wine prefix {bctx.paths.prefix} - Fork, its settings and the .NET Framework "
        "installed in it - and sets everything up again.\n\n"
        "A new prefix looks like a new computer to Fork's license, which allows 3 activations. "
        "Deactivate this one first in Fork (Help > Activation) if you want to keep the activation.\n\n"
        "Delete the prefix and start over?"
    )


def _stop_prefix(bctx: Any) -> None:
    """End everything running in our prefix before it is deleted (a prefix without our marker: nothing).

    ``wineserver -k`` first; then what an interrupted setup left behind (a winetricks script that
    keeps starting installers) gets SIGTERM / SIGKILL, or deleting the prefix races it and fails
    with "Directory not empty".
    """
    paths = bctx.paths
    if paths.created_by_marker.is_symlink() or not paths.created_by_marker.exists():
        return
    try:
        winecmd.wineserver(bctx.runner, bctx.wine_env(), bctx.wine(), "-k", timeout=60)
    except ForkLinuxError as exc:
        bctx.ui.warn(f"could not stop Wine in the prefix: {exc}")
    stopped = procs.stop_prefix(paths.prefix, proc_root=bctx.proc_root)
    if stopped:
        bctx.ui.warn(f"stopped {len(stopped)} process(es) still running in {paths.prefix}")


def reset(bctx: Any) -> None:
    """Ask, stop the prefix's Wine, delete the prefix (only if ours) and start a fresh state."""
    if not bctx.ui.confirm(RESET_TITLE, _reset_text(bctx), default=False):
        raise Declined(
            "the prefix was not reset",
            hint="run 'fork-linux setup --reset' in a terminal and answer yes to delete the prefix",
        )
    paths = bctx.paths
    with FileLock(paths.lock_file, "setup"):
        procs.require_closed(paths.prefix, proc_root=bctx.proc_root)
        if paths.prefix.exists():
            _stop_prefix(bctx)
            fsutil.safe_rmtree(paths.prefix, marker=paths.created_by_marker)
    kept = {key: bctx.state.get(key) for key in KEPT_ON_RESET if bctx.state.get(key) is not None}
    bctx.state = State.load(paths.state_file)
    for key, value in kept.items():
        bctx.state.set(key, value)
    bctx.cache.clear()
    bctx.reset_wine()


def check_selection(boot: Any, args: argparse.Namespace) -> None:
    """Refuse unknown ``--only`` / ``--from-step`` ids before anything (``--reset``) is deleted."""
    ids = [step.id for step in boot.default_steps()]
    named = [*(args.only or []), *([args.from_step] if args.from_step else [])]
    unknown = [name for name in named if name not in ids]
    if unknown:
        raise UsageError(f"unknown setup step(s): {', '.join(unknown)}", hint=f"steps: {', '.join(ids)}")


def check_fork_request(args: argparse.Namespace, ctx: AppContext) -> None:
    """Refuse a malformed, known-bad or (without ``--allow-untested``) unpinned ``--fork-version`` first.

    The ``fork_download`` step checks it again; refusing it here means nothing (no Wine
    download, no prefix, no ``--reset``) happens for a request that can never succeed.
    """
    if args.fork_version is None or args.list_steps:
        return
    manifest = manifest_mod.load(override=ctx.paths.manifest_override)
    fork_install.plan(manifest, requested=args.fork_version, allow_untested=args.allow_untested)


def check_wine_choice(args: argparse.Namespace, ctx: AppContext) -> None:
    """Refuse a ``--wine`` choice that cannot work here before it is saved as ``wine.provider``.

    A saved choice is used by every later setup and launch, so a Flatpak provider outside the
    Flatpak, a missing system Wine or a wrong path would break them all. The managed Wine is
    downloaded by setup itself and needs no check.
    """
    if not args.wine or args.wine == "managed":
        return
    manifest = manifest_mod.load(override=ctx.paths.manifest_override)
    wine_provider.resolve(ctx.config, manifest, ctx.paths, ctx.runner, ctx.env, choice=args.wine)


def run(args: argparse.Namespace, ctx: AppContext) -> int:
    """Run the pending setup steps (or list them)."""
    boot = _bootstrap()
    check_selection(boot, args)
    check_fork_request(args, ctx)
    check_wine_choice(args, ctx)
    bctx = _make_ctx(args, ctx)
    if args.list_steps:
        return list_steps(ctx, boot, bctx)
    if args.reset:
        reset(bctx)
    ran = boot.run_steps(bctx, only=args.only, from_step=args.from_step, force=args.force)
    complete = bctx.state.get("setup.complete") is True
    if ctx.json:
        ctx.print_json(
            {
                "schema": SCHEMA,
                "ran": ran,
                "complete": complete,
                "fork_version": bctx.state.get("fork.version"),
                "log": None if bctx.log_file is None else str(bctx.log_file),
            }
        )
        return 0
    if complete:
        print("Setup is complete. Start Fork with 'fork' or from your applications menu.")
    else:
        print("The selected steps are done; run 'fork-linux setup' to finish the remaining ones.")
    return 0
