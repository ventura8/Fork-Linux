"""``fork-linux update --check | --fork | --wine``: Fork and Wine updates under fork-linux's control.

* ``--check`` (the default) compares the installed Fork with the version this
  fork-linux release tested and the newest one in Fork's update feed.
* ``--fork`` snapshots the installed Fork, then installs the tested default
  (or ``--fork-version V`` / ``--latest``) with the official installer and
  refreshes what depends on Fork's files (the menu icon).
* ``--wine`` switches to the manifest's default managed Wine build (the old
  build is kept so it can be reverted), updates the prefix with
  ``wineboot -u`` and re-runs the setup steps that depend on Wine.

Fork's own updater (Velopack) is not involved: ``rollback`` undoes either.
"""

from __future__ import annotations

import argparse
from typing import Any

from .. import bootstrap, feeds, fork_install, launcher, procs, snapshots, updates, winecmd, wine_provider
from .. import manifest as manifest_mod
from ..cli import AppContext
from ..errors import ForkLinuxError, IntegrityFailed, NotSetUpError, UsageError
from ..fork_layout import ForkLayout
from ..locking import FileLock
from ..manifest import Manifest
from ..paths import Paths
from ..state import FORK_VERSION, WINE_BUILD, WINE_PROVIDER

FEED_CACHE = "releases-win-json"
# Re-run after a Fork update: the download/install markers record the new request, the icon is re-extracted.
FORK_STEPS = ("fork_download", "fork_install", "icon")
REQUEST_KEY = "fork.request"
WINEBOOT_TIMEOUT = 900.0
PINNED = launcher.PINNED
PINNED_VERSION = launcher.PINNED_VERSION
MANAGED = "managed"


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Add ``update``."""
    parser = subparsers.add_parser(
        "update",
        help="check for or install Fork and Wine updates",
        description="Check for updates (default), update Fork with a snapshot first, or switch to the "
        "default managed Wine build. 'fork-linux rollback' goes back to the previous Fork.",
    )
    what = parser.add_mutually_exclusive_group()
    what.add_argument("--check", action="store_true", help="show installed, tested and newest versions (default)")
    what.add_argument("--fork", action="store_true", help="install a Fork version (default: the tested one)")
    what.add_argument("--wine", action="store_true", help="switch to the default managed Wine build")
    version = parser.add_mutually_exclusive_group()
    version.add_argument("--fork-version", metavar="V", help="with --fork: install Fork version V")
    version.add_argument("--latest", action="store_true", help="with --fork: the newest version in Fork's feed")
    parser.add_argument(
        "--allow-untested",
        action="store_true",
        help="with --fork: allow a version this release has no pinned checksum for (trust on first use)",
    )
    parser.set_defaults(func=run)


def _feed_assets(manifest: Manifest, paths: Paths, *, offline: bool) -> list[feeds.FeedAsset]:
    """The packages listed in Fork's update feed (cached; ``offline`` uses only the cache)."""
    text = feeds.fetch_feed(
        manifest.feed_url,
        paths.feeds_dir / FEED_CACHE,
        offline=offline,
        validate=feeds.parse_releases_json,
    )
    return feeds.parse_releases_json(text)


def _check(ctx: AppContext) -> int:
    """``--check``: installed vs tested vs newest."""
    paths = ctx.paths
    manifest = manifest_mod.load(override=paths.manifest_override)
    layout = ForkLayout(paths, winecmd.windows_user(ctx.env))
    feed_error = None
    try:
        assets: list[feeds.FeedAsset] | None = _feed_assets(manifest, paths, offline=ctx.offline)
    except ForkLinuxError as exc:
        assets = None
        feed_error = exc.message
    info: dict[str, Any] = dict(updates.check(manifest, layout, assets))
    info["feed_error"] = feed_error
    info["update_policy"] = ctx.config.get("fork", "update_policy")
    if ctx.json:
        ctx.print_json(info)
    else:
        _print_check(info, feed_error)
    return 0


def _print_check(info: dict[str, Any], feed_error: str | None) -> None:
    """Print the ``--check`` report as text."""
    rows = [
        ("Installed", info["installed"] or "not installed"),
        ("Tested", info["default"]),
        ("Newest", info["latest"] or (f"unknown ({feed_error})" if feed_error else "unknown")),
        ("Staged", ", ".join(info["staged"]) or "none"),
        ("Updates", "pinned (Fork will not update itself)" if info["update_policy"] == PINNED else "automatic"),
    ]
    for label, value in rows:
        print(f"{label + ':':<10} {value}")
    if info["known_bad"]:
        print(f"warning: the installed version is known not to work well: {info['known_bad']}")
    if info["update_available"]:
        print(f"Fork {info['latest']} is available: 'fork-linux update --fork --latest' installs it")


def _install_fork(ctx: bootstrap.Ctx, args: argparse.Namespace) -> dict[str, Any]:
    """Snapshot, download, install and verify the chosen Fork version (holding the lock)."""
    paths, layout, manifest = ctx.paths, ctx.layout, ctx.manifest
    installed = layout.installed_version()
    if installed is None:
        raise NotSetUpError("Fork is not installed yet", hint="run 'fork-linux setup'")
    assets = _feed_assets(manifest, paths, offline=ctx.offline) if args.latest else None
    plan = fork_install.plan(
        manifest,
        requested=args.fork_version,
        latest=args.latest,
        feed_assets=assets,
        allow_untested=args.allow_untested,
    )
    if plan.version == installed:
        return {"installed": installed, "previous": installed, "changed": False, "snapshot": None}
    if plan.tofu and assets is None:
        assets = _feed_assets(manifest, paths, offline=ctx.offline)
    snap = snapshots.create(paths, layout, method=ctx.config.get("snapshots", "method"), reason="before update")
    installer = fork_install.download_installer(plan, manifest, paths, offline=ctx.offline)
    info = ctx.wine()
    fork_install.run_installer(ctx.runner, ctx.wine_env(), info.wine, installer, ctx.pathmap, log_file=ctx.log_file)
    version = fork_install.finish_install(layout, plan)
    if plan.tofu:
        expected = next(
            (asset.sha256 for asset in assets or () if asset.type == "Full" and asset.version == version), None
        )
        if expected is None or not fork_install.verify_nupkg(layout, version, expected):
            raise IntegrityFailed(
                f"the installed Fork {version} package does not match Fork's update feed",
                hint=f"'fork-linux rollback --to-version {installed}' restores the previous version",
            )
    ctx.state.set(FORK_VERSION, version)
    ctx.state.set(updates.LAST_SEEN_VERSION, version)
    ctx.state.set(REQUEST_KEY, _request(plan))
    if ctx.config.get("fork", "update_policy") == PINNED:
        ctx.state.set(PINNED_VERSION, version)
    ctx.state.save()
    return {"installed": version, "previous": installed, "changed": True, "snapshot": snap.id}


def _request(plan: fork_install.InstallPlan) -> dict[str, str] | None:
    """What setup should keep installing from now on (the same record the setup steps keep)."""
    if plan.source == "latest":
        return {"source": "latest"}
    if plan.source == "requested":
        return {"source": "requested", "version": plan.version}
    return None


def _rerun(ctx: bootstrap.Ctx, wanted: tuple[str, ...] | None) -> list[str]:
    """Re-run the named setup steps that exist (``None``: every pending step)."""
    if wanted is None:
        return bootstrap.run_steps(ctx)
    known = {step.id for step in bootstrap.default_steps()}
    chosen = [step_id for step_id in wanted if step_id in known]
    return bootstrap.run_steps(ctx, only=chosen) if chosen else []


def _update_fork(ctx: bootstrap.Ctx, args: argparse.Namespace) -> dict[str, Any]:
    """``--fork``."""
    procs.require_closed(ctx.paths.prefix)
    with FileLock(ctx.paths.lock_file, "update"):
        result = _install_fork(ctx, args)
    result["steps"] = _rerun(ctx, FORK_STEPS) if result["changed"] else []
    return result


def _switch_wine(ctx: bootstrap.Ctx) -> dict[str, Any]:
    """Install the default managed build, stop the old wineserver and update the prefix (holding the lock)."""
    target = ctx.manifest.wine_default
    try:
        old: winecmd.WineInfo | None = ctx.wine()
    except ForkLinuxError:
        old = None
    if ctx.config.raw("wine", "build").strip():
        ctx.config.unset("wine", "build")
    ctx.reset_wine()
    root = wine_provider.install_managed(target, ctx.paths, ctx.runner, offline=ctx.offline)
    new = wine_provider.managed_info(target, root)
    ctx.set_wine(new)
    if old is not None and old.root == new.root:
        return {"previous": old.build_id, "build": target.id, "changed": False}
    if old is not None:
        old_env = winecmd.build_env(ctx.paths, old, user=ctx.user, base_env=ctx.env)
        winecmd.wineserver(ctx.runner, old_env, old, "-k")
    env = ctx.wine_env()
    completed = winecmd.run(ctx.runner, env, new, ["wineboot", "-u"], timeout=WINEBOOT_TIMEOUT, log_file=ctx.log_file)
    if not completed.ok:
        raise ForkLinuxError(
            f"wineboot -u failed with exit code {completed.returncode}",
            hint=f"see {ctx.log_file}; the previous Wine build is kept"
            + (f" ({old.build_id})" if old is not None and old.build_id else ""),
        )
    winecmd.wineserver(ctx.runner, env, new, "-w")
    ctx.state.set(WINE_PROVIDER, MANAGED)
    ctx.state.set(WINE_BUILD, target.id)
    ctx.state.set("wine.root", str(new.root))
    ctx.state.save()
    return {"previous": None if old is None else old.build_id or str(old.root), "build": target.id, "changed": True}


def _update_wine(ctx: bootstrap.Ctx) -> dict[str, Any]:
    """``--wine``."""
    if ctx.wine_provider_choice() != MANAGED:
        raise UsageError(
            f"Wine is provided by '{ctx.wine_provider_choice()}', which fork-linux does not update",
            hint="update that Wine with its own tools, or 'fork-linux config set wine.provider managed'",
        )
    procs.require_closed(ctx.paths.prefix)
    with FileLock(ctx.paths.lock_file, "update"):
        result = _switch_wine(ctx)
    result["steps"] = _rerun(ctx, None)
    return result


def run(args: argparse.Namespace, ctx: AppContext) -> int:
    """Dispatch on ``--check`` / ``--fork`` / ``--wine``."""
    if not args.fork and (args.fork_version or args.latest or args.allow_untested):
        raise UsageError("--fork-version, --latest and --allow-untested go with --fork")
    if not (args.fork or args.wine):
        return _check(ctx)
    boot = bootstrap.Ctx.from_app(ctx)
    result = _update_fork(boot, args) if args.fork else _update_wine(boot)
    if ctx.json:
        ctx.print_json(result)
    elif args.fork:
        if result["changed"]:
            print(f"Fork {result['previous']} -> {result['installed']} (snapshot {result['snapshot']})")
            print(f"'fork-linux rollback --to-version {result['previous']}' goes back")
        else:
            print(f"Fork {result['installed']} is already installed")
    elif result["changed"]:
        print(f"Wine switched to {result['build']} (previous: {result['previous'] or 'none'}; it is kept)")
    else:
        print(f"Wine {result['build']} is already in use")
    return 0
