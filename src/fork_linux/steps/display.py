"""Step ``display_dpi``: Wine's DPI (``LogPixels``) from the desktop's scaling or ``[display] dpi``."""

from __future__ import annotations

from typing import Any

from .. import display
from ..bootstrap import Ctx, Step
from ..registry import RegBatch
from . import prefix as prefix_steps

DESKTOP_KEY = r"HKCU\Control Panel\Desktop"
LOG_PIXELS = "LogPixels"
_DPI_CACHE = "dpi"


def dpi(ctx: Ctx) -> int:
    """``[display] dpi`` as a number, or the desktop's DPI for ``auto`` (cached per run)."""
    cached = ctx.cache.get(_DPI_CACHE)
    if isinstance(cached, int):
        return cached
    configured = ctx.config.get("display", "dpi").strip().lower()
    value = display.detect_dpi(ctx.env, ctx.runner) if configured == "auto" else int(configured)
    ctx.cache[_DPI_CACHE] = value
    return value


def inputs(ctx: Ctx) -> dict[str, Any]:
    """The DPI to apply."""
    return {"dpi": dpi(ctx)}


def run(ctx: Ctx) -> None:
    """Write ``HKCU\\Control Panel\\Desktop`` ``LogPixels``."""
    prefix_steps.import_batch(ctx, RegBatch().set_dword(DESKTOP_KEY, LOG_PIXELS, dpi(ctx)), "display-dpi")


def verify(ctx: Ctx) -> bool:
    """``LogPixels`` holds the DPI."""
    return prefix_steps.reg_value(ctx, DESKTOP_KEY, LOG_PIXELS) == dpi(ctx)


DISPLAY_DPI = Step(
    id="display_dpi",
    title="Matching your display scaling",
    rev=1,
    weight=3,
    run=run,
    verify=verify,
    inputs=inputs,
)

STEPS = (DISPLAY_DPI,)
