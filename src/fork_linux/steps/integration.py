"""Steps ``host_shims``, ``git_overlay``, ``ssh_sync``, ``icon`` and ``desktop_entry``.

Everything here connects Fork to the Linux side: our Windows shims in
``C:\\fork-linux\\bin``, the translated git configuration and ssh keys, Fork's
own icon extracted from the user's ``Fork.exe`` and the personal menu entry.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .. import desktop_integration, fsutil, gitconfig, resources, ssh_sync
from ..bootstrap import Ctx, Step

log = logging.getLogger(__name__)

SHIM_MODE = 0o755
ICONS_KEY = "desktop.icons"


# -- host_shims ----------------------------------------------------------------------------


def shim_sources() -> list[Path]:
    """The built Windows shims (``*.exe``) shipped with fork-linux, sorted (empty if none)."""
    shims = resources.shims_dir()
    if shims is None or not shims.is_dir():
        return []
    return sorted(path for path in shims.glob("*.exe") if path.is_file())


def shims_dest(ctx: Ctx) -> Path:
    """``C:\\fork-linux\\bin``."""
    return ctx.paths.fork_linux_win_dir / "bin"


def shims_inputs(_ctx: Ctx) -> dict[str, Any]:
    """Name and sha256 of every shim."""
    return {"shims": {path.name: fsutil.sha256_file(path) for path in shim_sources()}}


def run_shims(ctx: Ctx) -> None:
    """Copy the shims into the prefix (each written to a temporary file, then renamed)."""
    sources = shim_sources()
    if not sources:
        log.info("no Windows shims were built; Fork keeps its own terminal and git")
        return
    dest = fsutil.ensure_dir(shims_dest(ctx))
    for source in sources:
        fsutil.atomic_write(dest / source.name, source.read_bytes(), mode=SHIM_MODE)


def verify_shims(ctx: Ctx) -> bool:
    """Every shim is in ``C:\\fork-linux\\bin`` with the same content."""
    dest = shims_dest(ctx)
    for source in shim_sources():
        target = dest / source.name
        if target.is_symlink() or not target.is_file():
            return False
        if fsutil.sha256_file(target) != fsutil.sha256_file(source):
            return False
    return True


# -- git_overlay ---------------------------------------------------------------------------


def _overlay_mode(ctx: Ctx) -> str:
    return ctx.config.get("git", "config_overlay")


def git_inputs(ctx: Ctx) -> dict[str, Any]:
    """The overlay mode."""
    return {"mode": _overlay_mode(ctx)}


def run_git(ctx: Ctx) -> None:
    """Give Fork's bundled git a translated copy of ``~/.gitconfig`` (unless turned off)."""
    if _overlay_mode(ctx) == "off":
        return
    gitconfig.sync(ctx.paths, ctx.user, ctx.host_home, ctx.pathmap)


def verify_git(ctx: Ctx) -> bool:
    """Turned off, or the overlay file exists."""
    return _overlay_mode(ctx) == "off" or gitconfig.overlay_path(ctx.paths, ctx.user).is_file()


# -- ssh_sync ------------------------------------------------------------------------------


def _ssh_wanted(ctx: Ctx) -> bool:
    """``[ssh] sync``: on, off, or auto (when ``~/.ssh`` exists)."""
    sync = ctx.config.get("ssh", "sync")
    if sync == "off":
        return False
    return sync == "on" or (ctx.host_home / ".ssh").is_dir()


def ssh_inputs(ctx: Ctx) -> dict[str, Any]:
    """The sync setting, the mode and whether there is anything to share."""
    return {
        "sync": ctx.config.get("ssh", "sync"),
        "mode": ctx.config.get("ssh", "mode"),
        "wanted": _ssh_wanted(ctx),
    }


def run_ssh(ctx: Ctx) -> None:
    """Share ``~/.ssh`` with Fork's ssh."""
    if not _ssh_wanted(ctx):
        return
    ssh_sync.sync(ctx.paths, ctx.user, ctx.host_home, mode=ctx.config.get("ssh", "mode"))


def verify_ssh(ctx: Ctx) -> bool:
    """Not wanted, or the Wine side ``.ssh`` is a private real directory."""
    if not _ssh_wanted(ctx):
        return True
    return bool(ssh_sync.status(ctx.paths, ctx.user, ctx.host_home)["dir_ok"])


# -- icon ----------------------------------------------------------------------------------


def icon_inputs(ctx: Ctx) -> dict[str, Any]:
    """sha256 of ``Fork.exe`` (a Fork update refreshes the icon)."""
    exe = ctx.layout.exe
    return {"exe_sha256": fsutil.sha256_file(exe) if exe.is_file() else None}


def run_icon(ctx: Ctx) -> None:
    """Extract Fork's icon from the user's own ``Fork.exe`` into the hicolor theme."""
    exe = ctx.layout.exe
    if not exe.is_file():
        ctx.state.set(ICONS_KEY, [])
        return
    installed = desktop_integration.install(
        ctx.paths, ctx.env, fork_exe=exe, menu=False, icons=True, file_managers=None, runner=ctx.runner
    )
    ctx.state.set(ICONS_KEY, [str(path) for path in installed])


def verify_icon(ctx: Ctx) -> bool:
    """Every icon recorded by the last run still exists (none: nothing to check)."""
    icons = ctx.state.get(ICONS_KEY)
    if not isinstance(icons, list):
        return False
    return all(isinstance(item, str) and Path(item).is_file() for item in icons)


# -- desktop_entry -------------------------------------------------------------------------


def desktop_inputs(ctx: Ctx) -> dict[str, Any]:
    """The command the menu entry starts."""
    return {"launcher": desktop_integration.launcher_command(ctx.env)}


def run_desktop(ctx: Ctx) -> None:
    """Install the personal menu entry (skipped by the module when a package installed one)."""
    desktop_integration.install(
        ctx.paths, ctx.env, menu=True, icons=False, file_managers=(), runner=ctx.runner
    )


def verify_desktop(ctx: Ctx) -> bool:
    """A packaged or a personal menu entry exists."""
    status = desktop_integration.status(ctx.paths, ctx.env)
    return status["system_desktop"] is not None or Path(status["menu_file"]).is_file()


HOST_SHIMS = Step(
    id="host_shims",
    title="Installing the Linux bridge programs",
    rev=1,
    weight=1,
    run=run_shims,
    verify=verify_shims,
    inputs=shims_inputs,
)

GIT_OVERLAY = Step(
    id="git_overlay",
    title="Sharing your git configuration",
    rev=1,
    weight=1,
    run=run_git,
    verify=verify_git,
    inputs=git_inputs,
    requires_fork_closed=False,
)

SSH_SYNC = Step(
    id="ssh_sync",
    title="Sharing your ssh keys",
    rev=1,
    weight=1,
    run=run_ssh,
    verify=verify_ssh,
    inputs=ssh_inputs,
    requires_fork_closed=False,
)

ICON = Step(
    id="icon",
    title="Adding Fork's icon",
    rev=1,
    weight=1,
    run=run_icon,
    verify=verify_icon,
    inputs=icon_inputs,
    requires_fork_closed=False,
)

DESKTOP_ENTRY = Step(
    id="desktop_entry",
    title="Adding the menu entry",
    rev=1,
    weight=1,
    run=run_desktop,
    verify=verify_desktop,
    inputs=desktop_inputs,
    requires_fork_closed=False,
)

STEPS = (HOST_SHIMS, GIT_OVERLAY, SSH_SYNC, ICON, DESKTOP_ENTRY)
