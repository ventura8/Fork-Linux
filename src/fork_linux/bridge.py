"""The experimental native-git bridge: interface used by the CLI and the launcher.

The bridge lets Fork run the host's ``/usr/bin/git`` (and Linux terminals,
file managers and diff tools) instead of its bundled Git for Windows. Its
pieces are the MinGW shims ``fl-shim.exe`` / ``fl-launch.exe`` (installed into
``C:\\fork-linux\\``) and the native ``fl-bridge-helper`` daemon; see
``bridge/win/README.md`` and ``bridge/unix/README.md``.

This module is only the interface for now. Enabling it records
``[git] bridge = on`` (never touching Fork's ``settings.json``), and only when
this installation actually ships the shims and the helper. :func:`launch_env`
is where the launcher will start the daemon and export ``FORKGITINSTANCE``
(phase 7); until then it returns no variables, so enabling the bridge does not
change how Fork runs yet.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import resources
from .errors import ForkLinuxError, UsageError

SECTION = "git"
KEY = "bridge"
SHIM_EXES = ("fl-shim.exe", "fl-launch.exe")
HELPER = "fl-bridge-helper"
EXPERIMENTAL = "the native-git bridge is experimental"
PORT_ENV = "FL_BRIDGE_PORT"


def helper_path() -> Path:
    """Where this installation keeps ``fl-bridge-helper``."""
    return resources.libexec_dir() / HELPER


def _missing() -> tuple[Path | None, Path, list[str]]:
    """``(shims_dir, helper, missing)``: what this installation lacks to run the bridge."""
    shims = resources.shims_dir()
    helper = helper_path()
    missing = []
    for exe in SHIM_EXES:
        if shims is None or not (shims / exe).is_file():
            missing.append(exe)
    if not helper.is_file():
        missing.append(HELPER)
    return shims, helper, missing


def _enabled(ctx: Any) -> tuple[bool, str]:
    """``(enabled, problem)`` from ``[git] bridge``; an invalid value counts as off."""
    try:
        return ctx.config.getbool(SECTION, KEY), ""
    except ForkLinuxError as exc:
        return False, exc.message


def status(ctx: Any) -> dict[str, Any]:
    """``{enabled, available, reason, shims_dir, helper}`` for ``git-bridge status`` and ``doctor``.

    ``ctx`` is anything with a ``config`` (:class:`~fork_linux.config.Config`):
    the CLI's ``AppContext`` or the bootstrap context. ``available`` says
    whether this installation ships the shims and the helper; ``reason``
    explains why not (or why the setting is unusable), empty otherwise.
    """
    shims, helper, missing = _missing()
    enabled, problem = _enabled(ctx)
    reasons = []
    if missing:
        reasons.append(f"{EXPERIMENTAL} and not built in this installation (missing: {', '.join(missing)})")
    if problem:
        reasons.append(problem)
    return {
        "enabled": enabled,
        "available": not missing,
        "reason": "; ".join(reasons),
        "shims_dir": None if shims is None else str(shims),
        "helper": str(helper),
    }


def enable(ctx: Any) -> None:
    """Set ``[git] bridge = on``; :class:`UsageError` when the bridge is not built in this install."""
    _shims, _helper, missing = _missing()
    if missing:
        raise UsageError(
            f"{EXPERIMENTAL} and not built in this installation (missing: {', '.join(missing)})",
            hint="install a fork-linux package built with the bridge, or build it from source "
            "(bridge/win and bridge/unix); Fork keeps using its bundled git meanwhile",
        )
    ctx.config.set(SECTION, KEY, "on")


def disable(ctx: Any) -> None:
    """Set ``[git] bridge = off`` (Fork goes back to its bundled git on the next start)."""
    ctx.config.set(SECTION, KEY, "off")


def launch_env(ctx: Any) -> dict[str, str]:
    """Environment variables the launcher adds for the bridge: none yet.

    Phase 7 starts ``fl-bridge-helper --daemon`` here when ``[git] bridge`` is
    on, and returns ``FL_BRIDGE_PORT`` / the token file location plus
    ``FORKGITINSTANCE=C:\\fork-linux\\gitInstance``. Until then the bridge
    has no effect on a launch, whatever the setting says.
    """
    del ctx
    return {}


def host_actions_active(ctx: Any) -> bool:
    """True when the launcher provides the daemon ``fl-launch.exe`` needs (:data:`PORT_ENV`).

    Fork's terminal, diff and merge tools are pointed at ``fl-launch.exe``
    only then; without the daemon ``fl-launch.exe`` exits at once and Fork's
    buttons do nothing. Phase 7 must keep :func:`launch_env` cheap and free of
    side effects for this check (or split the daemon start out of it).
    """
    return PORT_ENV in launch_env(ctx)
