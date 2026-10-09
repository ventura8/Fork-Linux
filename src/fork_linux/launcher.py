"""Start Fork (or hand paths to the running Fork): resolve, prepare, then exec Wine.

``fork-linux run``, ``fork [PATH...]`` and the desktop entry end here.
:func:`run` resolves the paths, takes a fast path when Fork already runs in
our prefix (Fork's single-instance pipe forwards the paths to the open window,
spike S7), and otherwise makes sure setup is complete, runs the pre-launch
hooks (:func:`pre_launch`), records the session and replaces this process
with Wine (``os.execvpe``): the launcher never stays around as a parent.

Wine's own output goes to ``<logs>/wine-last.log`` (or, with ``--debug``, a
new ``wine-<UTC time>.log``; the newest :data:`DEBUG_LOGS_KEEP` are kept).
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import (
    bootstrap,
    bridge,
    display,
    fork_settings,
    fsutil,
    gitconfig,
    procs,
    snapshots,
    ssh_sync,
    theme,
    updates,
    versions,
    winecmd,
)
from .cli import AppContext
from .config import Config
from .errors import ForkLinuxError, NotSetUpError, UsageError, WineUnavailable
from .fork_layout import ForkLayout
from .pathmap import PathMap
from .paths import Paths
from .procrun import Runner

log = logging.getLogger(__name__)

DEBUG_CHANNELS = "err+all,fixme-all"
QUIET_CHANNELS = winecmd.DEFAULT_DEBUG
LAST_LOG = "wine-last.log"
DEBUG_LOG_PREFIX = "wine-"
DEBUG_LOGS_KEEP = 10
WAYLAND = "wayland"
PINNED = "pinned"
PINNED_VERSION = "fork.pinned_version"
APPLIED_SSH = "applied.ssh_sync"
APPLIED_GITCONFIG = "applied.gitconfig"
SESSION_SCHEMA = 1
SETUP_HINT = "run 'fork-linux setup' to finish setting up, then try again"
LAUNCH_EXE = ("bin", "fl-launch.exe")
LAUNCH_EXE_WIN = "C:\\fork-linux\\bin\\fl-launch.exe"
_LOG_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_CLOEXEC | os.O_NOFOLLOW


@dataclasses.dataclass
class LaunchSpec:
    """Everything needed to exec Wine: argv, environment, working directory and log file."""

    argv: list[str]
    env: dict[str, str]
    cwd: Path
    log_file: Path | None


# -- paths -------------------------------------------------------------------------


def _repo_root(path: Path) -> Path:
    """The nearest directory above file ``path`` holding ``.git`` (a dir or a file), else its parent."""
    parent = path.parent
    for candidate in (parent, *parent.parents):
        if os.path.lexists(candidate / ".git"):
            return candidate
    return parent


def resolve_targets(args: Sequence[str], cwd: Path, *, from_file_manager: bool = False) -> list[Path]:
    """Absolute paths (``abspath``, symlinks kept) of ``args`` relative to ``cwd``.

    A path that does not exist raises :class:`UsageError`. With
    ``from_file_manager``, a file becomes the repository it belongs to (the
    nearest parent holding ``.git``) or, outside a repository, its directory.
    Duplicates are dropped, order is kept.
    """
    targets: list[Path] = []
    for raw in args:
        if not raw:
            raise UsageError("empty path", hint="pass a repository directory or a file inside one")
        path = Path(os.path.abspath(os.path.join(os.fspath(cwd), raw)))
        if not path.exists():
            raise UsageError(f"no such file or directory: {raw}", hint="pass an existing repository path")
        if from_file_manager and not path.is_dir():
            path = _repo_root(path)
        if path not in targets:
            targets.append(path)
    return targets


# -- environment and argv ------------------------------------------------------------


def _utc_stamp() -> str:
    """The current UTC time for log names (tests replace this)."""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _debug_log(logs_dir: Path) -> Path:
    """A new ``wine-<UTC time>.log``; older debug logs beyond :data:`DEBUG_LOGS_KEEP` are deleted."""
    stamp = _utc_stamp()
    target = logs_dir / f"{DEBUG_LOG_PREFIX}{stamp}.log"
    counter = 0
    while os.path.lexists(target):
        counter += 1
        target = logs_dir / f"{DEBUG_LOG_PREFIX}{stamp}-{counter}.log"
    old = sorted(
        entry
        for entry in os.listdir(logs_dir)
        if entry.startswith(DEBUG_LOG_PREFIX) and entry.endswith(".log") and entry != LAST_LOG
    )
    for name in old[: max(0, len(old) - (DEBUG_LOGS_KEEP - 1))]:
        (logs_dir / name).unlink()
    return target


def _channels(config: Config, debug: bool, wine_debug: str | None) -> str:
    """``WINEDEBUG``: ``--wine-debug``, else the debug set, else ``[wine] debug``."""
    if wine_debug:
        return wine_debug
    if debug:
        return DEBUG_CHANNELS
    return config.get("wine", "debug").strip() or QUIET_CHANNELS


def _spec(
    *,
    paths: Paths,
    config: Config,
    env: Mapping[str, str],
    user: str,
    info: winecmd.WineInfo,
    layout: ForkLayout,
    targets: Sequence[Path],
    extra: Mapping[str, str],
    debug: bool,
    wine_debug: str | None,
    driver: str | None,
) -> LaunchSpec:
    """The :class:`LaunchSpec` for the given pieces (shared by the normal and the fast path)."""
    overrides = {**gitconfig.env_overrides(config), **extra}
    dll_overrides = config.get("wine", "extra_dll_overrides").strip()
    wine_env = winecmd.build_env(
        paths,
        info,
        user=user,
        base_env=env,
        debug=_channels(config, debug, wine_debug),
        dll_overrides=dll_overrides or winecmd.DEFAULT_DLL_OVERRIDES,
        extra=overrides,
    )
    chosen = driver or config.get("wine", "driver")
    if chosen == WAYLAND:
        wine_env.pop("DISPLAY", None)
    pathmap = PathMap.from_prefix(paths.prefix) if targets else None
    win_targets = [pathmap.unix_to_win(target) for target in targets] if pathmap is not None else []
    logs_dir = fsutil.ensure_dir(paths.logs_dir)
    log_file = _debug_log(logs_dir) if debug else logs_dir / LAST_LOG
    return LaunchSpec(
        argv=[str(info.wine), layout.win_exe, *win_targets],
        env=wine_env,
        cwd=layout.current_dir,
        log_file=log_file,
    )


def build_spec(
    ctx: bootstrap.Ctx,
    targets: Sequence[Path],
    *,
    debug: bool = False,
    wine_debug: str | None = None,
    driver: str | None = None,
) -> LaunchSpec:
    """The launch for :class:`fork_linux.bootstrap.Ctx` ``ctx`` (its Wine, prefix, user and config).

    The environment is :func:`winecmd.build_env` (``WINEDEBUG`` from
    ``wine_debug``, the debug channels with ``debug``, else ``[wine] debug``)
    plus :func:`gitconfig.env_overrides` and :func:`bridge.launch_env`;
    ``driver`` (or ``[wine] driver``) ``wayland`` drops ``DISPLAY`` so Wine
    uses its Wayland driver. Paths become Windows paths through the prefix's
    drive mapping.
    """
    return _spec(
        paths=ctx.paths,
        config=ctx.config,
        env=ctx.env,
        user=ctx.user,
        info=ctx.wine(),
        layout=ctx.layout,
        targets=targets,
        extra=bridge.launch_env(ctx),
        debug=debug,
        wine_debug=wine_debug,
        driver=driver,
    )


# -- pre-launch hooks ----------------------------------------------------------------


def _mtime(path: Path) -> int | None:
    """``st_mtime_ns`` of ``path`` (symlinks followed), or None when it does not exist."""
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return None


def _host_home(env: Mapping[str, str]) -> Path:
    """The user's Linux home directory."""
    return Path(env.get("HOME") or Path.home())


def _note_version(ctx: bootstrap.Ctx, notes: list[str]) -> None:
    """Tell the user when Fork updated itself, and warn about a known-bad version."""
    change = updates.observe_version(ctx.state, ctx.layout)
    if change is not None and change.old is not None:
        message = f"Fork updated {change.old} -> {change.new}; 'fork-linux rollback' restores {change.old}"
        ctx.ui.info(message)
        notes.append(message)
    installed = change.new if change is not None else ctx.layout.installed_version()
    if installed is not None:
        reason = ctx.manifest.known_bad_reason(installed)
        if reason is not None:
            message = f"Fork {installed} is known not to work well under Wine: {reason}"
            ctx.ui.warn(message + "; 'fork-linux rollback' goes back to the previous version")
            notes.append(message)


def _snapshot(ctx: bootstrap.Ctx, notes: list[str]) -> None:
    """Keep a snapshot of the installed version for rollback."""
    keep = ctx.config.getint("snapshots", "keep")
    created = snapshots.ensure_current(ctx.paths, ctx.layout, keep=keep, method=ctx.config.get("snapshots", "method"))
    if created is not None:
        notes.append(f"snapshot {created.id} created")


def _pin(ctx: bootstrap.Ctx, notes: list[str]) -> None:
    """With ``[fork] update_policy = pinned``, drop staged packages newer than the pinned version."""
    if ctx.config.get("fork", "update_policy") != PINNED:
        return
    pinned = ctx.state.get(PINNED_VERSION)
    if not isinstance(pinned, str) or not versions.is_valid(pinned):
        pinned = ctx.layout.installed_version()
    if pinned is None:
        return
    for package in updates.enforce_pin(ctx.layout, pinned):
        notes.append(f"removed staged update {package.name} (Fork is pinned to {pinned})")
    installed = ctx.layout.installed_version()
    if installed is not None and versions.Version(installed) > versions.Version(pinned):
        # Fork applied an update it had already downloaded while it ran; the pin cannot undo that by itself.
        message = f"Fork {installed} is installed although Fork is pinned to {pinned}"
        ctx.ui.warn(f"{message}; 'fork-linux rollback --to-version {pinned}' goes back")
        notes.append(message)


def _shell_tool(paths: Paths) -> dict[str, Any] | None:
    """Fork's terminal setting for our ``fl-launch.exe``, when the shims are installed."""
    if not paths.fork_linux_win_dir.joinpath(*LAUNCH_EXE).is_file():
        return None
    return {"Type": "Custom", "ApplicationPath": LAUNCH_EXE_WIN, "Arguments": "terminal"}


def desired_settings(
    *, paths: Paths, config: Config, env: Mapping[str, str], runner: Runner, user: str, layout: ForkLayout
) -> dict[str, object]:
    """The ``settings.json`` values fork-linux wants now (:func:`fork_settings.desired` with live probes).

    The desktop theme and scale are probed only when ``[display]`` asks to
    follow them; the terminal setting is offered only when our shims are
    installed; the repository folder becomes the Linux home (``Z:\\...``).
    """
    home = _host_home(env)
    wanted_theme = theme.detect(env, runner, home) if config.get("display", "theme") == "follow" else None
    scale = display.scale_percent(env, runner) if config.get("display", "dpi") == "auto" else None
    try:
        home_win: str | None = PathMap.from_prefix(paths.prefix).unix_to_win(home)
    except ForkLinuxError:
        home_win = None
    return fork_settings.desired(
        config,
        user=user,
        theme=wanted_theme,
        scale=scale,
        shell_tool=_shell_tool(paths),
        home_win=home_win,
        current=fork_settings.load(layout.settings_file),
    )


def _settings(ctx: bootstrap.Ctx, notes: list[str]) -> None:
    """Keep Fork's ``settings.json`` at the values fork-linux wants (only while Fork is closed).

    A missing file is seeded (see :func:`fork_settings.seed`).
    """
    paths: Paths = ctx.paths
    if procs.fork_running(paths.prefix):
        return
    wanted = desired_settings(
        paths=paths, config=ctx.config, env=ctx.env, runner=ctx.runner, user=ctx.user, layout=ctx.layout
    )
    changed = fork_settings.seed(ctx.layout, wanted, backup_dir=fork_settings.default_backup_dir(paths))
    if changed:
        notes.append(f"Fork settings updated: {', '.join(changed)}")


def _ssh(ctx: bootstrap.Ctx, notes: list[str]) -> None:
    """Re-share ``~/.ssh`` when it changed since the last sync (``[ssh] sync``)."""
    mode = ctx.config.get("ssh", "sync")
    home = _host_home(ctx.env)
    ssh_dir = home / ".ssh"
    if mode == "off" or (mode == "auto" and not ssh_dir.is_dir()):
        return
    signature = [_mtime(ssh_dir), _mtime(ssh_dir / "config")]
    if ctx.state.get(APPLIED_SSH) == signature:
        return
    ssh_sync.sync(ctx.paths, ctx.user, home, mode=ctx.config.get("ssh", "mode"))
    ctx.state.set(APPLIED_SSH, signature)
    notes.append("ssh keys and config shared with Fork")


def _git(ctx: bootstrap.Ctx, notes: list[str]) -> None:
    """Re-translate the host git configuration when it changed (``[git] config_overlay``)."""
    if ctx.config.get("git", "config_overlay") != "translate":
        return
    home = _host_home(ctx.env)
    signature = [_mtime(home.joinpath(*rel)) for rel in gitconfig.HOST_FILES]
    if ctx.state.get(APPLIED_GITCONFIG) == signature:
        return
    gitconfig.sync(ctx.paths, ctx.user, home, PathMap.from_prefix(ctx.paths.prefix))
    ctx.state.set(APPLIED_GITCONFIG, signature)
    notes.append("git configuration translated for Fork")


HOOKS: tuple[tuple[str, Callable[[bootstrap.Ctx, list[str]], None]], ...] = (
    ("version check", _note_version),
    ("snapshot", _snapshot),
    ("update pin", _pin),
    ("Fork settings", _settings),
    ("ssh sync", _ssh),
    ("git configuration", _git),
)


def pre_launch(ctx: bootstrap.Ctx) -> list[str]:
    """Run the pre-launch hooks and save the state; return notes on what they did.

    A failing hook never stops Fork from starting: it is reported through
    ``ctx.ui.warn`` (and the notes) and the next hook runs.
    """
    notes: list[str] = []
    for name, hook in HOOKS:
        try:
            hook(ctx, notes)
        except (ForkLinuxError, OSError) as exc:
            message = f"{name} skipped: {exc}"
            log.debug("pre-launch hook %s failed", name, exc_info=True)
            ctx.ui.warn(message)
            notes.append(message)
    ctx.state.save()
    return notes


# -- session ---------------------------------------------------------------------------


def write_session(paths: Paths, info: winecmd.WineInfo, pid: int) -> Path:
    """Record the running session (the Wine in use) in ``session.json``; return its path."""
    data = {
        "schema": SESSION_SCHEMA,
        "pid": pid,
        "wine_root": str(info.root),
        "wine": str(info.wine),
        "wineserver": str(info.wineserver),
        "provider": info.provider,
        "prefix": str(paths.prefix),
        "started": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    fsutil.ensure_dir(paths.runtime_dir)
    fsutil.atomic_write(paths.session_file, json.dumps(data, indent=2) + "\n")
    return paths.session_file


def read_session(paths: Paths) -> dict[str, Any] | None:
    """``session.json`` as a dict, or None when it is missing, unreadable or not a JSON object."""
    try:
        with open(paths.session_file, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _recorded(value: Any, default: Path) -> Path:
    """An absolute path recorded in the session, else ``default``."""
    return Path(value) if isinstance(value, str) and os.path.isabs(value) else default


def _session_wine(paths: Paths) -> winecmd.WineInfo | None:
    """The Wine recorded for the running session of this prefix, or None if unusable."""
    data = read_session(paths)
    if data is None or data.get("prefix") != str(paths.prefix):
        return None
    root = data.get("wine_root")
    if not isinstance(root, str) or not os.path.isabs(root):
        return None
    wine_path = _recorded(data.get("wine"), Path(root, "bin", "wine"))
    server_path = _recorded(data.get("wineserver"), Path(root, "bin", "wineserver"))
    if not wine_path.is_file():
        return None
    provider = data.get("provider")
    return winecmd.WineInfo(
        provider=provider if isinstance(provider, str) else "session",
        build_id=None,
        root=Path(root),
        wine=wine_path,
        wineserver=server_path,
        version="",
        staging=False,
        wow64=False,
    )


# -- run -------------------------------------------------------------------------------


def _redirect(log_file: Path, *, truncate: bool) -> None:
    """Point stdout and stderr at ``log_file`` (private, appended; emptied first with ``truncate``)."""
    sys.stdout.flush()
    sys.stderr.flush()
    fd = os.open(log_file, _LOG_FLAGS | (os.O_TRUNC if truncate else 0), 0o600)
    try:
        os.dup2(fd, 1)
        os.dup2(fd, 2)
    finally:
        os.close(fd)


def _restore_output(saved: Sequence[int]) -> None:
    """Point stdout and stderr back at the descriptors :func:`_exec` saved (if any)."""
    sys.stdout.flush()
    sys.stderr.flush()
    for target, fd in zip((1, 2), saved):
        os.dup2(fd, target)


def _exec(spec: LaunchSpec, *, truncate: bool, execvpe: Callable[..., Any]) -> int:
    """chdir, redirect output, then replace this process with Wine.

    When Wine cannot be executed, stdout and stderr go back to where they
    were so the error reaches the user instead of the log file.
    """
    try:
        os.chdir(spec.cwd)
    except OSError as exc:
        raise NotSetUpError(
            f"cannot enter Fork's install directory {spec.cwd}: {exc.strerror or exc}", hint=SETUP_HINT
        ) from None
    # Duplicates are close-on-exec, so Wine never inherits them.
    saved = [os.dup(1), os.dup(2)] if spec.log_file is not None else []
    try:
        if spec.log_file is not None:
            try:
                _redirect(spec.log_file, truncate=truncate)
            except OSError as exc:
                raise ForkLinuxError(f"cannot write Wine's log {spec.log_file}: {exc.strerror or exc}") from None
        log.debug("exec %s (cwd %s, log %s)", spec.argv, spec.cwd, spec.log_file)
        try:
            execvpe(spec.argv[0], spec.argv, spec.env)
        except OSError as exc:
            _restore_output(saved)
            raise WineUnavailable(
                f"cannot start {spec.argv[0]}: {exc.strerror or exc}",
                hint="'fork-linux doctor' checks the Wine installation; 'fork-linux setup' reinstalls "
                "the managed Wine runtime",
            ) from None
    finally:
        for fd in saved:
            os.close(fd)
    return 0


def _to_terminal(app_ctx: AppContext, debug: bool) -> bool:
    """``--debug`` with ``-v``: Wine's output stays on the terminal instead of a log file."""
    return debug and app_ctx.verbosity > 0


def _fast_spec(
    app_ctx: AppContext, targets: Sequence[Path], *, debug: bool, wine_debug: str | None, driver: str | None
) -> LaunchSpec | None:
    """The launch while Fork already runs: the session's Wine, no setup, no hooks (None if unknown)."""
    paths: Paths = app_ctx.paths
    info = _session_wine(paths)
    if info is None:
        return None
    user = winecmd.windows_user(app_ctx.env)
    return _spec(
        paths=paths,
        config=app_ctx.config,
        env=app_ctx.env,
        user=user,
        info=info,
        layout=ForkLayout(paths, user),
        targets=targets,
        extra=bridge.launch_env(app_ctx),
        debug=debug,
        wine_debug=wine_debug,
        driver=driver,
    )


def run(
    app_ctx: AppContext,
    raw_targets: Sequence[str],
    *,
    debug: bool = False,
    wine_debug: str | None = None,
    driver: str | None = None,
    no_setup: bool = False,
    no_hooks: bool = False,
    from_file_manager: bool = False,
    execvpe: Callable[..., Any] = os.execvpe,
) -> int:
    """Open ``raw_targets`` in Fork (or just start it); returns only when ``execvpe`` does (tests).

    1. resolve the paths; 2. Fork already running in our prefix: exec at once
    with the session's Wine (Fork forwards the paths to its window); 3. make
    sure setup is complete (``no_setup``: :class:`NotSetUpError` instead of
    running it); 4. pre-launch hooks unless ``no_hooks``; 5. record the
    session, send Wine's output to the log and exec.
    """
    targets = resolve_targets(raw_targets, Path(os.getcwd()), from_file_manager=from_file_manager)
    terminal = _to_terminal(app_ctx, debug)
    running = procs.fork_running(app_ctx.paths.prefix)
    if running:
        spec = _fast_spec(app_ctx, targets, debug=debug, wine_debug=wine_debug, driver=driver)
        if spec is not None:
            if terminal:
                spec = dataclasses.replace(spec, log_file=None)
            return _exec(spec, truncate=False, execvpe=execvpe)
    ctx = bootstrap.Ctx.from_app(app_ctx)
    try:
        bootstrap.ensure_ready(ctx, allow=not no_setup)
    except NotSetUpError as exc:
        hint = exc.hint if "fork-linux setup" in exc.hint else SETUP_HINT
        raise NotSetUpError(exc.message, hint=hint) from exc
    if not no_hooks and not running:
        pre_launch(ctx)
    spec = build_spec(ctx, targets, debug=debug, wine_debug=wine_debug, driver=driver)
    if terminal:
        spec = dataclasses.replace(spec, log_file=None)
    write_session(ctx.paths, ctx.wine(), os.getpid())
    return _exec(spec, truncate=not running and not debug, execvpe=execvpe)
