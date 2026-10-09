"""Steps ``fonts`` and ``font_replacements``: core fonts, and Linux fonts standing in for Segoe UI.

Fork's WPF interface asks for Segoe UI and Consolas, which Wine does not have.
``corefonts`` brings Arial & co.; the replacements map the missing families to
the best installed Linux font (``fc-list``), so text renders with real glyph
metrics instead of Wine's fallback.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from .. import sandbox, winetricks
from ..bootstrap import Ctx, Step
from ..errors import ForkLinuxError, SetupFailed
from ..registry import RegBatch
from . import prefix as prefix_steps

REPLACEMENTS_KEY = r"HKCU\Software\Wine\Fonts\Replacements"
FC_LIST_TIMEOUT = 30.0
SANS_FAMILIES = ("Segoe UI", "Segoe UI Semibold", "Segoe UI Light", "Segoe UI Symbol")
MONO_FAMILIES = ("Consolas",)
SANS_CHOICES = ("Noto Sans", "DejaVu Sans", "Liberation Sans")
MONO_CHOICES = ("Noto Sans Mono", "DejaVu Sans Mono", "Liberation Mono")
# Always available: Wine ships its own Tahoma, corefonts brings Courier New.
SANS_FALLBACK = "Tahoma"
MONO_FALLBACK = "Courier New"
_FC_CACHE = "fc_list_families"
COREFONTS = "corefonts"
# corefonts is resumable (finished fonts are skipped); one retry absorbs the
# occasional "Application could not be started" of regedit under Wine staging.
COREFONTS_ATTEMPTS = 2

log = logging.getLogger(__name__)


# -- fonts ---------------------------------------------------------------------------------


def _find_ci(root: Path, parts: tuple[str, ...]) -> Path | None:
    """``root/<parts>`` matching each component case-insensitively, or None."""
    current = root
    for part in parts:
        try:
            names = os.listdir(current)
        except OSError:
            return None
        match = next((name for name in sorted(names) if name.lower() == part.lower()), None)
        if match is None:
            return None
        current = current / match
    return current


def arial(ctx: Ctx) -> Path | None:
    """``C:\\windows\\Fonts\\arial.ttf`` (any case), if installed."""
    found = _find_ci(ctx.paths.prefix / "drive_c", ("windows", "Fonts", "arial.ttf"))
    return found if found is not None and found.is_file() else None


def run_fonts(ctx: Ctx) -> None:
    """``winetricks -q corefonts`` unless it already finished; one retry after a failure.

    Arial alone is not proof: corefonts installs its fonts one by one, so an
    interrupted run leaves Arial without Times New Roman or Verdana.
    """
    if verify_fonts(ctx):
        return
    for _attempt in range(COREFONTS_ATTEMPTS - 1):
        try:
            prefix_steps.run_verbs(ctx, "fonts", [COREFONTS])
            return
        except SetupFailed:
            log.warning("winetricks %s failed; trying once more", COREFONTS)
    prefix_steps.run_verbs(ctx, "fonts", [COREFONTS])


def verify_fonts(ctx: Ctx) -> bool:
    """Arial is installed and winetricks recorded the whole ``corefonts`` verb as done."""
    return arial(ctx) is not None and COREFONTS in winetricks.installed_verbs(ctx.paths.prefix)


# -- font_replacements -------------------------------------------------------------------


def parse_families(output: str) -> set[str]:
    """Family names in ``fc-list : family`` output (one font per line, aliases comma separated)."""
    families = set()
    for line in output.splitlines():
        for name in line.split(","):
            name = name.strip().replace("\\-", "-")
            if name:
                families.add(name)
    return families


def installed_families(ctx: Ctx) -> set[str]:
    """Font families fontconfig knows (empty when ``fc-list`` is missing or fails)."""
    cached = ctx.cache.get(_FC_CACHE)
    if isinstance(cached, set):
        return cached
    try:
        result = ctx.runner.run(["fc-list", ":", "family"], env=sandbox.clean_env(ctx.env), timeout=FC_LIST_TIMEOUT)
    except ForkLinuxError:
        families: set[str] = set()
    else:
        families = parse_families(result.stdout) if result.ok else set()
    ctx.cache[_FC_CACHE] = families
    return families


def _pick(families: set[str], choices: tuple[str, ...], fallback: str) -> str:
    return next((choice for choice in choices if choice in families), fallback)


def replacements(ctx: Ctx) -> dict[str, str]:
    """Windows family -> installed Linux family."""
    families = installed_families(ctx)
    sans = _pick(families, SANS_CHOICES, SANS_FALLBACK)
    mono = _pick(families, MONO_CHOICES, MONO_FALLBACK)
    chosen = dict.fromkeys(SANS_FAMILIES, sans)
    chosen.update(dict.fromkeys(MONO_FAMILIES, mono))
    return chosen


def replacement_inputs(ctx: Ctx) -> dict[str, Any]:
    """The chosen replacements (derived from the ``fc-list`` result)."""
    return {"replacements": replacements(ctx)}


def run_replacements(ctx: Ctx) -> None:
    """Write ``HKCU\\Software\\Wine\\Fonts\\Replacements``."""
    batch = RegBatch()
    for name, target in replacements(ctx).items():
        batch.set_sz(REPLACEMENTS_KEY, name, target)
    prefix_steps.import_batch(ctx, batch, "font-replacements")


def verify_replacements(ctx: Ctx) -> bool:
    """Every replacement is in the registry."""
    return prefix_steps.reg_matches(
        ctx, [(REPLACEMENTS_KEY, name, target) for name, target in replacements(ctx).items()]
    )


FONTS = Step(
    id="fonts",
    title="Installing Microsoft core fonts",
    rev=1,
    weight=48,
    run=run_fonts,
    verify=verify_fonts,
    needs_network=True,
)

FONT_REPLACEMENTS = Step(
    id="font_replacements",
    title="Choosing replacement fonts",
    rev=1,
    weight=3,
    run=run_replacements,
    verify=verify_replacements,
    inputs=replacement_inputs,
)

STEPS = (FONTS, FONT_REPLACEMENTS)
