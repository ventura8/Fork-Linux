"""Steps ``wine_runtime`` and ``winetricks``: the Wine that runs Fork and the pinned winetricks.

``wine_runtime`` resolves (and for the managed provider downloads, verifies
and extracts) the Wine build and records what it found in the state.
``winetricks`` installs the manifest-pinned winetricks script.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .. import fsutil, wine_provider, winetricks
from ..bootstrap import Ctx, Step
from ..errors import ForkLinuxError

WINETRICKS_MODE = "managed"
_WINETRICKS_CACHE = "winetricks_path"


def wine_inputs(ctx: Ctx) -> dict[str, Any]:
    """The provider, and for the managed one the pinned build id and sha256."""
    provider = ctx.wine_provider_choice()
    if provider != "managed":
        return {"provider": provider}
    build = ctx.manifest.wine_build(ctx.config.get("wine", "build") or ctx.manifest.wine_default.id)
    return {"provider": provider, "build": build.id, "sha256": build.sha256}


def run_wine(ctx: Ctx) -> None:
    """Resolve the Wine, installing the managed build if needed, and record it."""
    info = wine_provider.resolve(
        ctx.config,
        ctx.manifest,
        ctx.paths,
        ctx.runner,
        ctx.env,
        choice=ctx.wine_choice,
        install=True,
        offline=ctx.offline,
    )
    ctx.set_wine(info)
    ctx.state.set("wine.provider", info.provider)
    ctx.state.set("wine.build", info.build_id)
    ctx.state.set("wine.version", info.version)
    ctx.state.set("wine.staging", info.staging)
    ctx.state.set("wine.root", str(info.root))


def verify_wine(ctx: Ctx) -> bool:
    """True when the chosen Wine resolves without installing anything."""
    ctx.reset_wine()
    try:
        ctx.wine()
    except ForkLinuxError:
        return False
    return True


def winetricks_inputs(ctx: Ctx) -> dict[str, Any]:
    """The pinned winetricks version and sha256, and the mode."""
    pinned = ctx.manifest.winetricks
    return {"mode": WINETRICKS_MODE, "version": pinned.version, "sha256": pinned.sha256}


def winetricks_path(ctx: Ctx) -> Path:
    """The verified winetricks script (installed on first use, then cached in ``ctx``)."""
    cached = ctx.cache.get(_WINETRICKS_CACHE)
    if isinstance(cached, Path):
        return cached
    path = winetricks.ensure(ctx.manifest, ctx.paths, ctx.runner, mode=WINETRICKS_MODE, offline=ctx.offline)
    ctx.cache[_WINETRICKS_CACHE] = path
    return path


def run_winetricks(ctx: Ctx) -> None:
    """Install the pinned winetricks."""
    ctx.cache.pop(_WINETRICKS_CACHE, None)
    winetricks_path(ctx)


def verify_winetricks(ctx: Ctx) -> bool:
    """True when the pinned script is installed and intact."""
    pinned = ctx.manifest.winetricks
    script = winetricks.managed_path(ctx.manifest, ctx.paths)
    if script.is_symlink() or not script.is_file() or not os.access(script, os.X_OK):
        return False
    return fsutil.sha256_file(script) == pinned.sha256


WINE_RUNTIME = Step(
    id="wine_runtime",
    title="Installing the Wine runtime",
    rev=1,
    weight=20,
    run=run_wine,
    verify=verify_wine,
    inputs=wine_inputs,
    needs_network=True,
    requires_fork_closed=False,
)

WINETRICKS = Step(
    id="winetricks",
    title="Installing winetricks",
    rev=1,
    weight=2,
    run=run_winetricks,
    verify=verify_winetricks,
    inputs=winetricks_inputs,
    needs_network=True,
    requires_fork_closed=False,
)

STEPS = (WINE_RUNTIME, WINETRICKS)
