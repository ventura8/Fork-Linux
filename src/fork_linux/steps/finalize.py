"""Step ``finalize``: let Wine settle and mark setup complete when every step is done."""

from __future__ import annotations

import logging

from .. import procs, winecmd
from ..bootstrap import Ctx, Step, now, update_completion
from ..errors import ForkLinuxError

log = logging.getLogger(__name__)

FINALIZED_AT = "setup.finalized_at"


def run(ctx: Ctx) -> None:
    """Wait for the prefix's wineserver to go idle (best effort) and update ``setup.complete``.

    While Fork runs in the prefix the server never goes idle (``-w`` would
    block until Fork is closed), so the wait is skipped then.
    """
    if procs.fork_running(ctx.paths.prefix, proc_root=ctx.proc_root):
        log.info("Fork is running in %s; not waiting for its wineserver", ctx.paths.prefix)
    else:
        try:
            winecmd.wineserver(ctx.runner, ctx.wine_env(), ctx.wine(), "-w")
        except ForkLinuxError as exc:
            log.info("wineserver -w: %s", exc)
    ctx.state.set(FINALIZED_AT, now())
    update_completion(ctx)


def verify(_ctx: Ctx) -> bool:
    """Nothing to verify."""
    return True


FINALIZE = Step(
    id="finalize",
    title="Finishing",
    rev=1,
    weight=1,
    run=run,
    verify=verify,
    requires_fork_closed=False,
    always=True,
)

STEPS = (FINALIZE,)
