"""Step ``dotnet``: Microsoft .NET Framework 4.8 (or 4.7.2) through winetricks.

Fork for Windows is a .NET Framework 4.7.2+ WPF application. The verbs come
from ``setup --dotnet`` (``auto``: the manifest's list, in order). Success is
judged by the registry (``NDP\\v4\\Full`` ``Release``), not by winetricks'
exit code alone. A failed winetricks run is retried once in place: .NET's
installers fail now and then under Wine (the 4.6.2 one with status 67 while
installing 4.7.2), and winetricks skips the parts already installed. A verb
that still fails leaves a half-installed .NET that is not repairable, so before
Fork is installed the prefix (ours: it carries the created-by marker) is
deleted, booted again and the next verb is tried; once Fork is installed we
never delete the prefix behind the user's back.
"""

from __future__ import annotations

import logging
from typing import Any

from .. import fsutil, winecmd
from ..bootstrap import Ctx, Step, clear_markers, init_prefix_meta, run_step
from ..errors import SetupFailed
from . import prefix as prefix_steps

log = logging.getLogger(__name__)

NDP_KEY = r"HKLM\Software\Microsoft\NET Framework Setup\NDP\v4\Full"
VERB_KEY = "dotnet.verb"
RESET_HINT = (
    "Fork is already installed, so the prefix is not rebuilt automatically; "
    "run 'fork-linux setup --reset' to start over with a fresh prefix"
)
# Markers that survive a prefix rebuild: they describe things outside the prefix.
HOST_STEPS = ("consent", "wine_runtime", "winetricks")
# winetricks runs per verb: a transient installer failure gets one more try in the same prefix.
VERB_ATTEMPTS = 2


def verbs(ctx: Ctx) -> list[str]:
    """The winetricks verbs to try, in order."""
    if ctx.dotnet == "auto":
        return list(ctx.manifest.dotnet_verbs)
    return [ctx.dotnet]


def release(ctx: Ctx) -> int | None:
    """The installed .NET Framework 4.x ``Release`` number, or None."""
    value = prefix_steps.reg_value(ctx, NDP_KEY, "Release")
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def inputs(ctx: Ctx) -> dict[str, Any]:
    """The minimum release Fork needs (the verb only matters while installing)."""
    return {"min_release": ctx.manifest.dotnet_min_release}


def verify(ctx: Ctx) -> bool:
    """``Release`` is at least the manifest's minimum."""
    found = release(ctx)
    return found is not None and found >= ctx.manifest.dotnet_min_release


def rebuild_prefix(ctx: Ctx) -> None:
    """Delete our prefix and boot it again (state and consent are kept)."""
    log.warning("rebuilding the Wine prefix %s after a failed .NET installation", ctx.paths.prefix)
    ctx.ui.warn("the .NET Framework installation failed; rebuilding the Wine prefix and trying again")
    winecmd.wineserver(ctx.runner, ctx.wine_env(), ctx.wine(), "-k")
    prefix_steps.stop_leftovers(ctx)
    fsutil.safe_rmtree(ctx.paths.prefix, marker=ctx.paths.created_by_marker)
    init_prefix_meta(ctx)
    clear_markers(ctx, keep=HOST_STEPS)
    ctx.state.save()
    for step in prefix_steps.BOOT_STEPS:
        run_step(ctx, step)


def _install(ctx: Ctx, verb: str) -> str | None:
    """Run winetricks for ``verb`` (twice at most); None when it succeeded, else why it failed."""
    problem = None
    for attempt in range(VERB_ATTEMPTS):
        if attempt:
            log.warning("winetricks %s failed (%s); trying once more", verb, problem)
        try:
            prefix_steps.run_verbs(ctx, "dotnet", [verb])
            return None
        except prefix_steps.DiskFull:
            raise
        except SetupFailed as exc:
            problem = exc.message
    return problem


def _attempt(ctx: Ctx, verb: str) -> str | None:
    """Install ``verb``; None on success, else why it failed."""
    problem = _install(ctx, verb)
    if problem is not None:
        return problem
    if verify(ctx):
        return None
    found = release(ctx)
    return (
        f"after winetricks {verb} the .NET Framework release is {found if found is not None else 'missing'}, "
        f"below the required {ctx.manifest.dotnet_min_release}"
    )


def run(ctx: Ctx) -> None:
    """Install the first verb that works (see the module docstring)."""
    if verify(ctx):
        return
    tried: list[str] = []
    for index, verb in enumerate(verbs(ctx)):
        if index:
            rebuild_prefix(ctx)
        problem = _attempt(ctx, verb)
        if problem is None:
            ctx.state.set(VERB_KEY, verb)
            return
        tried.append(f"{verb}: {problem}")
        if ctx.layout.is_installed():
            raise SetupFailed("dotnet", problem, hint=RESET_HINT)
    raise SetupFailed(
        "dotnet",
        "no .NET Framework could be installed (" + "; ".join(tried) + ")",
        hint="check your network connection and the setup log, or try 'fork-linux setup --dotnet dotnet472'",
    )


DOTNET = Step(
    id="dotnet",
    title="Installing Microsoft .NET Framework (this takes a few minutes)",
    rev=1,
    weight=160,
    run=run,
    verify=verify,
    inputs=inputs,
    needs_network=True,
)

STEPS = (DOTNET,)
