"""Step ``preflight``: can this machine run Fork under Wine at all?

Runs every time (it keeps no marker): x86_64, Python >= 3.10, not root (unless
explicitly allowed), enough free disk space next to the prefix and the host
tools winetricks needs. It also creates our XDG directories.
"""

from __future__ import annotations

import os
import platform
import sys

from .. import fsutil, hostdeps
from ..bootstrap import Ctx, Step
from ..errors import ForkLinuxError, UnsupportedEnvironment

GiB = 1024 * 1024 * 1024
MiB = 1024 * 1024
# A fresh setup needs ~2 GB of prefix + 0.7 GB of runtime + downloads (spike S1).
MIN_FREE = 3 * GiB
# Re-running setup on an existing prefix only adds a little.
MIN_FREE_RESUME = 512 * MiB
X86_64 = ("x86_64", "amd64")
MIN_PYTHON = (3, 10)


def _machine() -> str:
    """The CPU architecture (a seam for tests)."""
    return platform.machine()


def _python() -> tuple[int, int]:
    """The running Python's ``(major, minor)`` (a seam for tests)."""
    return (sys.version_info[0], sys.version_info[1])


def _euid() -> int:
    """The effective user id (a seam for tests)."""
    return os.geteuid()


def _gib(size: int) -> str:
    return f"{size / GiB:.1f} GiB"


def run(ctx: Ctx) -> None:
    """Check the host; raise :class:`UnsupportedEnvironment` or :class:`ForkLinuxError` with a hint."""
    machine = _machine().lower()
    if machine not in X86_64:
        raise UnsupportedEnvironment(
            f"Fork for Windows is x86_64 only; this machine is {machine or 'unknown'}",
            hint="Fork for Linux (unofficial) supports x86_64 (amd64) machines only",
        )
    if _python() < MIN_PYTHON:
        raise UnsupportedEnvironment(
            f"Python {'.'.join(map(str, _python()))} is too old; 3.10 or newer is required",
            hint="install Python 3.10 or newer",
        )
    if _euid() == 0 and not ctx.allow_root:
        raise UnsupportedEnvironment(
            "refusing to set up Wine as root",
            hint="run setup as your normal user (Wine prefixes are per user); --allow-root is for containers and CI",
        )
    ctx.paths.ensure()
    fresh = not (ctx.paths.prefix / "system.reg").exists()
    needed = MIN_FREE if fresh else MIN_FREE_RESUME
    free = fsutil.disk_free(ctx.paths.prefix.parent)
    if free < needed:
        raise ForkLinuxError(
            f"not enough free disk space for {ctx.paths.prefix}: {_gib(free)} free, {_gib(needed)} needed",
            hint="free some space (Wine, .NET and Fork take about 3 GB), or choose another prefix with --prefix",
        )
    missing = hostdeps.missing_tools(ctx.runner, hostdeps.REQUIRED_TOOLS)
    if missing:
        family = hostdeps.distro().family
        raise ForkLinuxError(
            f"required host tools are missing: {', '.join(missing)}",
            hint=hostdeps.install_hint(family, missing, []),
        )


def verify(_ctx: Ctx) -> bool:
    """Preflight has nothing to verify once it passed."""
    return True


PREFLIGHT = Step(
    id="preflight",
    title="Checking this computer",
    rev=1,
    weight=1,
    run=run,
    verify=verify,
    requires_fork_closed=False,
    always=True,
)

STEPS = (PREFLIGHT,)
