"""Steps ``fork_download``, ``fork_install`` and ``fork_settings``: the official Fork for Windows.

Which Fork to install is the *request*: the manifest's tested default,
a version named with ``setup --fork-version`` or the newest release
(``--latest`` / ``[fork] channel = latest``). An explicit request is remembered
in the state so later runs (the launcher) keep honouring it; the default is
not pinned to a version because Fork updates itself after installation.

The installer comes only from the manifest's URL template (AGENTS.md hard
rule 2). A pinned version is verified by size + sha256; a trust-on-first-use
install must afterwards have the full nupkg the feed describes.
"""

from __future__ import annotations

import dataclasses
import logging
from typing import Any

from .. import (
    bridge,
    display,
    feeds,
    fork_data,
    fork_install,
    fork_settings,
    fork_tools,
    fsutil,
    resources,
    theme,
    versions,
    winecmd,
)
from ..bootstrap import Ctx, Step, now
from ..errors import DownloadFailed, ForkLinuxError, IntegrityFailed
from ..fork_install import InstallPlan
from ..pathmap import PathMap

log = logging.getLogger(__name__)

REQUEST_KEY = "fork.request"
PLAN_KEY = "fork.plan"
DEFAULT = "default"
REQUESTED = "requested"
LATEST = "latest"
_PLAN_CACHE = "fork_plan"
_WANTED_CACHE = "fork_settings_wanted"
_PLAN_FIELDS = ("version", "url", "sha256", "size", "tofu", "source")


# -- the request ---------------------------------------------------------------------------


def request(ctx: Ctx) -> dict[str, Any]:
    """``{"source": ...}`` (+ ``version`` for an explicit one): flags, then the recorded request, then config."""
    if ctx.latest:
        return {"source": LATEST}
    if ctx.fork_version is not None:
        return {"source": REQUESTED, "version": ctx.fork_version.strip()}
    recorded = ctx.state.get(REQUEST_KEY)
    if isinstance(recorded, dict) and recorded.get("source") == LATEST:
        return {"source": LATEST}
    if isinstance(recorded, dict) and recorded.get("source") == REQUESTED and isinstance(recorded.get("version"), str):
        return {"source": REQUESTED, "version": recorded["version"]}
    if ctx.config.get("fork", "channel") == LATEST:
        return {"source": LATEST}
    return {"source": DEFAULT}


def describe_request(ctx: Ctx) -> tuple[str, int | None]:
    """``(version text, installer size or None)`` for the consent dialog (no network)."""
    wanted = request(ctx)
    if wanted["source"] == LATEST:
        return "(newest release from Fork's update feed)", None
    entry = ctx.manifest.installer_entry(wanted.get("version"))
    return entry.version, entry.size


def inputs(ctx: Ctx) -> dict[str, Any]:
    """The request (Fork's own updates never re-trigger the install)."""
    return request(ctx)


def feed_assets(ctx: Ctx, *, legacy: bool = False) -> list[feeds.FeedAsset] | None:
    """Fork's release feed (JSON, or the legacy ``RELEASES`` file), or None when unavailable."""
    url = ctx.manifest.legacy_feed_url if legacy else ctx.manifest.feed_url
    parse = feeds.parse_releases_legacy if legacy else feeds.parse_releases_json
    try:
        text = feeds.fetch_feed(url, ctx.paths.feeds_dir, offline=ctx.offline, validate=parse)
        return parse(text)
    except ForkLinuxError as exc:
        log.warning("Fork's release feed %s is unavailable: %s", url, exc)
        return None


def plan(ctx: Ctx) -> InstallPlan:
    """The install plan for the request (the feed is read only for ``latest``; cached per run)."""
    cached = ctx.cache.get(_PLAN_CACHE)
    if isinstance(cached, InstallPlan):
        return cached
    wanted = request(ctx)
    if wanted["source"] == LATEST:
        assets = feed_assets(ctx) or feed_assets(ctx, legacy=True)
        if assets is None:
            raise DownloadFailed(
                "cannot read Fork's release feed to find the latest version",
                hint="check your network connection, or install the tested default without --latest",
            )
        chosen = fork_install.plan(ctx.manifest, latest=True, feed_assets=assets)
    else:
        chosen = fork_install.plan(
            ctx.manifest, requested=wanted.get("version"), allow_untested=ctx.allow_untested
        )
    ctx.cache[_PLAN_CACHE] = chosen
    return chosen


def _recorded_plan(ctx: Ctx) -> InstallPlan | None:
    """The plan ``fork_download`` recorded for the current request, if any."""
    data = ctx.state.get(PLAN_KEY)
    if not isinstance(data, dict) or data.get("request") != request(ctx):
        return None
    try:
        return InstallPlan(**{name: data[name] for name in _PLAN_FIELDS})
    except (KeyError, TypeError):
        return None


def _current_plan(ctx: Ctx) -> InstallPlan:
    return _recorded_plan(ctx) or plan(ctx)


def satisfied(ctx: Ctx) -> bool:
    """True when the installed Fork already fulfils the request (``--force`` never is)."""
    if ctx.force:
        return False
    installed = ctx.layout.installed_version()
    if installed is None:
        return False
    wanted = request(ctx)
    if wanted["source"] != REQUESTED:
        return True
    return versions.is_valid(wanted["version"]) and versions.Version(installed) == versions.Version(wanted["version"])


# -- fork_download -------------------------------------------------------------------------


def run_download(ctx: Ctx) -> None:
    """Download and verify the installer (skipped when Fork already fulfils the request)."""
    wanted = request(ctx)
    if wanted["source"] != DEFAULT:
        ctx.state.set(REQUEST_KEY, wanted)
    else:
        ctx.state.set(REQUEST_KEY, None)
    if satisfied(ctx):
        return
    chosen = plan(ctx)
    path = fork_install.download_installer(chosen, ctx.manifest, ctx.paths, offline=ctx.offline)
    if chosen.sha256 is None:
        chosen = dataclasses.replace(chosen, sha256=fsutil.sha256_file(path), size=path.stat().st_size)
    recorded = {name: getattr(chosen, name) for name in _PLAN_FIELDS}
    recorded["request"] = wanted
    ctx.state.set(PLAN_KEY, recorded)


def verify_download(ctx: Ctx) -> bool:
    """Fork fulfils the request, or the planned installer is in the download cache."""
    if satisfied(ctx):
        return True
    recorded = _recorded_plan(ctx)
    return recorded is not None and (ctx.paths.downloads_dir / recorded.file_name).is_file()


# -- fork_install --------------------------------------------------------------------------


def _verify_tofu(ctx: Ctx, chosen: InstallPlan) -> None:
    """For an unpinned install: the installed full nupkg must match the feed's sha256."""
    assets = feed_assets(ctx) or []
    expected = next(
        (
            asset.sha256
            for asset in assets
            if asset.type == "Full"
            and asset.sha256
            and versions.Version(asset.version) == versions.Version(chosen.version)
        ),
        None,
    )
    if expected is None:
        ctx.ui.warn(
            f"Fork {chosen.version} could not be checked against Fork's release feed "
            "(the feed is unavailable or does not list it)"
        )
        return
    if not fork_install.verify_nupkg(ctx.layout, chosen.version, expected):
        raise IntegrityFailed(
            f"the installed Fork {chosen.version} package does not match the sha256 in Fork's release feed",
            hint="run 'fork-linux setup --reset' to start over, and report this if it happens again",
        )


def run_install(ctx: Ctx) -> None:
    """Run the installer silently, check the result, record what is installed."""
    chosen = None
    if not satisfied(ctx):
        chosen = _current_plan(ctx)
        installer = fork_install.download_installer(chosen, ctx.manifest, ctx.paths, offline=ctx.offline)
        info = ctx.wine()
        env = ctx.wine_env()
        fork_install.run_installer(ctx.runner, env, info.wine, installer, ctx.pathmap, log_file=ctx.log_file)
        winecmd.wineserver(ctx.runner, env, info, "-w")
        fork_install.finish_install(ctx.layout, chosen)
        if chosen.tofu:
            _verify_tofu(ctx, chosen)
        ctx.state.set("fork.installed_at", now())
        ctx.state.set("fork.source", chosen.source)
        ctx.state.set("fork.installer_sha256", chosen.sha256)
    ctx.state.set("fork.version", ctx.layout.installed_version())


def verify_install(ctx: Ctx) -> bool:
    """Fork.exe and ``sq.version`` are in place."""
    return ctx.layout.is_installed()


# -- fork_settings -------------------------------------------------------------------------


def tools(ctx: Ctx) -> dict[str, Any]:
    """Fork's terminal / diff / merge settings for this installation (:func:`fork_tools.wanted`).

    Setup runs before the shims are copied, so ``fl-launch.exe`` counts as
    installed when this installation ships it; the launcher re-checks the
    prefix before every start.
    """
    shims = resources.shims_dir()
    shipped = shims is not None and (shims / fork_tools.FL_LAUNCH[-1]).is_file()
    try:
        pathmap: PathMap | None = ctx.pathmap
    except ForkLinuxError:
        pathmap = None
    return fork_tools.wanted(
        bridge_active=bridge.host_actions_active(ctx),
        fl_launch=shipped or fork_tools.fl_launch_installed(ctx.paths),
        pathmap=pathmap,
    )


def settings_inputs(ctx: Ctx) -> dict[str, Any]:
    """The configuration that shapes ``settings.json`` (the file itself is seeded when missing)."""
    return {
        "theme": ctx.config.raw("display", "theme"),
        "dpi": ctx.config.raw("display", "dpi"),
        "enforce": ctx.config.getlist("fork", "enforce_settings"),
        "tools": tools(ctx),
        "home": _home_win(ctx),
    }


def _home_win(ctx: Ctx) -> str | None:
    try:
        return ctx.pathmap.unix_to_win(ctx.host_home)
    except ForkLinuxError:
        return None


def run_settings(ctx: Ctx) -> None:
    """Merge the Wine-safe values into ``settings.json``, creating it before Fork's first start.

    Seeding is safe (E2E, Fork 2.23.2): Fork keeps the seeded keys, so even
    the first session runs with the Wine-safe values and theme. A
    ``LayoutScaling`` an earlier fork-linux wrote goes back to 100 (Wine's
    ``LogPixels`` does the scaling now), and ``ForkData\\repositories.toml``
    gets the Linux home as the default source folder.
    """
    path = ctx.layout.settings_file
    current = fork_settings.load(path)
    home = _home_win(ctx)
    wanted = fork_settings.desired(
        ctx.config,
        user=ctx.user,
        theme=theme.detect(ctx.env, ctx.runner, ctx.host_home),
        tools=tools(ctx),
        home_win=home,
        current=current,
        reset_scaling=fork_settings.legacy_scaling(
            ctx.config.raw("display", "dpi").strip().lower(), display.scale_percent(ctx.env, ctx.runner)
        ),
    )
    backup_dir = fork_settings.default_backup_dir(ctx.paths)
    fork_settings.seed(ctx.layout, wanted, backup_dir=backup_dir)
    ctx.cache[_WANTED_CACHE] = wanted
    if home is not None:
        fork_data.ensure_source_dirs(ctx.layout.forkdata_dir, user=ctx.user, home_win=home, backup_dir=backup_dir)


def verify_settings(ctx: Ctx) -> bool:
    """``settings.json`` exists, loads and holds what this run applied."""
    path = ctx.layout.settings_file
    if not path.is_file():
        return False
    try:
        data = fork_settings.load(path)
    except ForkLinuxError:
        return False
    wanted = ctx.cache.get(_WANTED_CACHE) or {}
    return all(fork_settings.get(data, key) == value for key, value in wanted.items())


FORK_DOWNLOAD = Step(
    id="fork_download",
    title="Downloading the official Fork installer",
    rev=1,
    weight=10,
    run=run_download,
    verify=verify_download,
    inputs=inputs,
    needs_network=True,
    requires_fork_closed=False,
)

FORK_INSTALL = Step(
    id="fork_install",
    title="Installing Fork",
    rev=1,
    weight=5,
    run=run_install,
    verify=verify_install,
    inputs=inputs,
)

FORK_SETTINGS = Step(
    id="fork_settings",
    title="Adjusting Fork's settings for Wine",
    rev=2,
    weight=1,
    run=run_settings,
    verify=verify_settings,
    inputs=settings_inputs,
)

STEPS = (FORK_DOWNLOAD, FORK_INSTALL, FORK_SETTINGS)
