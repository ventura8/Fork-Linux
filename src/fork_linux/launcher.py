"""Start Fork (or hand paths to the running Fork): resolve, prepare, then exec Wine.

``fork-linux run``, ``fork [PATH...]`` and the desktop entry end here.
:func:`run` resolves the paths, takes a fast path when Fork already runs in
our prefix (Fork's single-instance pipe forwards the paths to the open window,
spike S7), and otherwise makes sure setup is complete, runs the pre-launch
hooks (:func:`pre_launch`), records the session and replaces this process
with Wine (``os.execvpe``): the launcher never stays around as a parent.

With ``[git] bridge = on`` the native-git bridge daemon is started first
(:func:`bridge.start_daemon`, before the hooks so Fork's tool settings follow
whether it really runs): its ``--parent-pid`` is this process, which the exec
turns into the Wine process running Fork, so the daemon lives exactly as long
as Fork. The fast path reuses the running daemon recorded in ``session.json``.

Wine's own output goes to ``<logs>/wine-last.log`` (or, with ``--debug``, a
new ``wine-<UTC time>.log``; the newest :data:`DEBUG_LOGS_KEEP` are kept).
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import stat
import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import (
    bootstrap,
    bridge,
    fork_data,
    fork_settings,
    fork_tools,
    fsutil,
    gitconfig,
    procs,
    snapshots,
    ssh_sync,
    theme,
    updates,
    versions,
    wine_provider,
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
FORK_LOG_PREFIX = "fork-"
FORK_LOGS_KEEP = 10
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
    git_version: tuple[int, int, int] | None = None,
) -> LaunchSpec:
    """The :class:`LaunchSpec` for the given pieces (shared by the normal and the fast path).

    ``extra`` holds the bridge's variables; when they send git to the Linux
    git, the bundled-git-only ``GIT_CONFIG_*`` overrides are left out.
    """
    native = bridge.native_git(extra)
    overrides = {**gitconfig.env_overrides(config, git_version=git_version, native=native), **extra}
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
    bridge_env: Mapping[str, str] | None = None,
) -> LaunchSpec:
    """The launch for :class:`fork_linux.bootstrap.Ctx` ``ctx`` (its Wine, prefix, user and config).

    The environment is :func:`winecmd.build_env` (``WINEDEBUG`` from
    ``wine_debug``, the debug channels with ``debug``, else ``[wine] debug``)
    plus :func:`gitconfig.env_overrides` and ``bridge_env`` (:func:`bridge.launch_env`);
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
        extra=dict(bridge_env or {}),
        debug=debug,
        wine_debug=wine_debug,
        driver=driver,
        git_version=gitconfig.host_git_version(ctx.runner, ctx.env, ctx.paths.cache_dir),
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


def _pathmap(paths: Paths) -> PathMap | None:
    """The prefix's drive mapping, or None when the prefix has none yet."""
    try:
        return PathMap.from_prefix(paths.prefix)
    except ForkLinuxError:
        return None


def home_win(paths: Paths, env: Mapping[str, str]) -> str | None:
    """The Linux home as Fork sees it (``Z:\\home\\<u>``), or None when Wine cannot reach it."""
    pathmap = _pathmap(paths)
    if pathmap is None:
        return None
    try:
        return pathmap.unix_to_win(_host_home(env))
    except ForkLinuxError:
        return None


def desired_settings(
    *,
    paths: Paths,
    config: Config,
    env: Mapping[str, str],
    runner: Runner,
    user: str,
    layout: ForkLayout,
    bridge_active: bool = False,
) -> dict[str, object]:
    """The ``settings.json`` values fork-linux wants now (:func:`fork_settings.desired` with live probes).

    The desktop theme is probed only when ``[display] theme`` follows it;
    Fork's terminal / diff / merge settings come from :func:`fork_tools.wanted`
    (``bridge_active``: the launcher provides the ``fl-launch.exe`` daemon);
    the repository folder becomes the Linux home (``Z:\\...``).
    """
    home = _host_home(env)
    wanted_theme = theme.detect(env, runner, home) if config.get("display", "theme") == "follow" else None
    tools = fork_tools.wanted(
        bridge_active=bridge_active, fl_launch=fork_tools.fl_launch_installed(paths), pathmap=_pathmap(paths)
    )
    return fork_settings.desired(
        config,
        user=user,
        theme=wanted_theme,
        tools=tools,
        home_win=home_win(paths, env),
        current=fork_settings.load(layout.settings_file),
    )


def _settings(ctx: bootstrap.Ctx, notes: list[str]) -> None:
    """Keep Fork's ``settings.json`` and default source folder as fork-linux wants (Fork closed only).

    A missing ``settings.json`` is seeded (see :func:`fork_settings.seed`);
    ``ForkData\\repositories.toml`` gets the Linux home as its source folder
    (:func:`fork_data.ensure_source_dirs`).
    """
    paths: Paths = ctx.paths
    if procs.fork_running(paths.prefix):
        return
    wanted = desired_settings(
        paths=paths,
        config=ctx.config,
        env=ctx.env,
        runner=ctx.runner,
        user=ctx.user,
        layout=ctx.layout,
        bridge_active=bridge.host_actions_active(ctx),
    )
    backup_dir = fork_settings.default_backup_dir(paths)
    changed = fork_settings.seed(ctx.layout, wanted, backup_dir=backup_dir)
    if changed:
        notes.append(f"Fork settings updated: {', '.join(changed)}")
    home = home_win(paths, ctx.env)
    if home is not None and fork_data.ensure_source_dirs(
        ctx.layout.forkdata_dir, user=ctx.user, home_win=home, backup_dir=backup_dir
    ):
        notes.append(f"Fork's default source folder set to {home}")


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
    signature = [
        gitconfig.OVERLAY_REVISION,
        *(_mtime(home.joinpath(*rel)) for rel in gitconfig.HOST_FILES),
        *gitconfig.rewrite_tops(),
    ]
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


# -- Fork's own log ----------------------------------------------------------------------


def keep_fork_log(paths: Paths, layout: ForkLayout) -> Path | None:
    """Copy the previous session's ``fork.log`` to ``<logs>/fork-<UTC time>.log``; return the copy.

    Fork overwrites ``logs\\fork.log`` at every start, so it is copied before
    Fork starts again. The name comes from the log's own modification time,
    so a log already kept is not copied twice; the newest
    :data:`FORK_LOGS_KEEP` copies are kept. Fork's file is only read.
    """
    source = layout.fork_log
    try:
        fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size == 0:
            return None
        with os.fdopen(fd, "rb", closefd=False) as handle:
            data = handle.read()
    finally:
        os.close(fd)
    stamp = datetime.fromtimestamp(info.st_mtime, timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    logs_dir = fsutil.ensure_dir(paths.logs_dir)
    target = logs_dir / f"{FORK_LOG_PREFIX}{stamp}.log"
    if os.path.lexists(target):
        return None
    fsutil.atomic_write(target, data)
    kept = sorted(name for name in os.listdir(logs_dir) if _is_fork_copy(name))
    for name in kept[: max(0, len(kept) - FORK_LOGS_KEEP)]:
        (logs_dir / name).unlink()
    return target


def _is_fork_copy(name: str) -> bool:
    """True for ``fork-<UTC time>.log`` (not ``fork-linux.log``)."""
    return name.startswith(FORK_LOG_PREFIX) and name.endswith(".log") and name[len(FORK_LOG_PREFIX) : -4].endswith("Z")


def fork_log_history(paths: Paths) -> list[Path]:
    """The kept copies of earlier ``fork.log`` files, newest first."""
    try:
        names = os.listdir(paths.logs_dir)
    except OSError:
        return []
    return [paths.logs_dir / name for name in sorted((n for n in names if _is_fork_copy(n)), reverse=True)]


# -- session ---------------------------------------------------------------------------


def write_session(
    paths: Paths, info: winecmd.WineInfo, pid: int, bridge_record: Mapping[str, Any] | None = None
) -> Path:
    """Record the running session (the Wine in use, the bridge daemon) in ``session.json``; return its path.

    The file is private (0600 in the 0700 runtime directory): ``bridge_record``
    (:func:`bridge.session_record`) carries the daemon's token.
    """
    data: dict[str, Any] = {
        "schema": SESSION_SCHEMA,
        "pid": pid,
        "wine_root": str(info.root),
        "wine": str(info.wine),
        "wineserver": str(info.wineserver),
        "provider": info.provider,
        "prefix": str(paths.prefix),
        "started": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    if bridge_record is not None:
        data["bridge"] = dict(bridge_record)
    fsutil.ensure_dir(paths.runtime_dir, mode=0o700)
    fsutil.atomic_write(paths.session_file, json.dumps(data, indent=2) + "\n", mode=0o600)
    return paths.session_file


def read_session(paths: Paths) -> dict[str, Any] | None:
    """``session.json`` as a dict, or None when it is missing, unreadable or not a JSON object."""
    try:
        with open(paths.session_file, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


@dataclasses.dataclass(frozen=True)
class WineCandidate:
    """A Wine the running session may use: found from our own data or configuration, never from the session."""

    root: Path
    wine: Path
    wineservers: tuple[Path, ...]


def _candidate(root: Path, wine: Path, *servers: Path) -> WineCandidate:
    """A candidate whose wineserver sits next to ``wine``, in ``root/bin`` or at one of ``servers``."""
    return WineCandidate(root, wine, (wine.parent / "wineserver", root / "bin" / "wineserver", *servers))


def trusted_wines(app_ctx: AppContext) -> list[WineCandidate]:
    """Every Wine a session of ours can have recorded.

    The managed builds installed in our data directory, the Flatpak runtime, the ``wine`` on
    ``PATH`` (system provider) and an absolute ``[wine] provider`` (custom).
    """
    paths: Paths = app_ctx.paths
    candidates = [
        _candidate(root, root / "bin" / "wine")
        for root in (wine_provider.managed_root(paths, build) for build in wine_provider.installed_builds(paths))
    ]
    flatpak = wine_provider.FLATPAK_ROOT
    candidates.append(_candidate(flatpak, flatpak / "bin" / "wine"))
    search = app_ctx.env.get("PATH")
    found = app_ctx.runner.which("wine", path=search)
    if found is not None:
        server = app_ctx.runner.which("wineserver", path=search)
        extra = wine_provider.SYSTEM_WINESERVER_FALLBACKS + ((Path(server),) if server is not None else ())
        candidates.append(_candidate(Path(found).parent.parent, Path(found), *extra))
    provider = app_ctx.config.get("wine", "provider")
    if os.path.isabs(provider):
        location = Path(provider)
        if location.is_dir():
            candidates.append(_candidate(location, location / "bin" / "wine"))
        else:
            candidates.append(_candidate(location.parent.parent, location))
    return candidates


def _recorded(value: Any, default: str) -> str:
    """An absolute path recorded in the session, else ``default``."""
    return value if isinstance(value, str) and os.path.isabs(value) else default


def _select(recorded: str, choices: Sequence[Path]) -> Path | None:
    """The trusted path among ``choices`` spelled exactly like the ``recorded`` one, else None."""
    return next((choice for choice in choices if str(choice) == recorded), None)


def _session_wine(paths: Paths, candidates: Sequence[WineCandidate] = ()) -> winecmd.WineInfo | None:
    """The Wine recorded for the running session of this prefix, or None if unusable.

    The session only selects among ``candidates`` (see :func:`trusted_wines`): the recorded
    paths are compared with them and never used themselves.
    """
    data = read_session(paths)
    if data is None or data.get("prefix") != str(paths.prefix):
        return None
    root = data.get("wine_root")
    if not isinstance(root, str) or not os.path.isabs(root):
        return None
    wine = _recorded(data.get("wine"), os.path.join(root, "bin", "wine"))
    server = _recorded(data.get("wineserver"), os.path.join(root, "bin", "wineserver"))
    chosen = next((c for c in candidates if str(c.root) == root and str(c.wine) == wine), None)
    if chosen is None or not chosen.wine.is_file():
        return None
    provider = data.get("provider")
    return winecmd.WineInfo(
        provider=provider if isinstance(provider, str) else "session",
        build_id=None,
        root=chosen.root,
        wine=chosen.wine,
        wineserver=_select(server, chosen.wineservers) or chosen.root / "bin" / "wineserver",
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
    """The launch while Fork already runs: the session's Wine and bridge daemon, no setup, no hooks.

    None when the session is unknown.
    """
    paths: Paths = app_ctx.paths
    info = _session_wine(paths, trusted_wines(app_ctx))
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
        extra=bridge.session_env(app_ctx, read_session(paths)),
        debug=debug,
        wine_debug=wine_debug,
        driver=driver,
        git_version=gitconfig.host_git_version(app_ctx.runner, app_ctx.env, paths.cache_dir),
    )


def _maybe_to_terminal(spec: LaunchSpec, terminal: bool) -> LaunchSpec:
    """``spec`` without its log file when Wine's output stays on the terminal."""
    return dataclasses.replace(spec, log_file=None) if terminal else spec


def _ready_ctx(app_ctx: AppContext, *, no_setup: bool, running: bool) -> bootstrap.Ctx:
    """The setup context once setup is complete; keeps the previous fork.log when Fork is not running."""
    ctx = bootstrap.Ctx.from_app(app_ctx)
    try:
        bootstrap.ensure_ready(ctx, allow=not no_setup)
    except NotSetUpError as exc:
        hint = exc.hint if "fork-linux setup" in exc.hint else SETUP_HINT
        raise NotSetUpError(exc.message, hint=hint) from exc
    if not running:
        try:
            keep_fork_log(ctx.paths, ctx.layout)
        except (ForkLinuxError, OSError) as exc:
            log.warning("could not keep the previous fork.log: %s", exc)
    return ctx


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
    with the session's Wine and bridge daemon (Fork forwards the paths to its
    window); 3. make sure setup is complete (``no_setup``:
    :class:`NotSetUpError` instead of running it); 4. start the git bridge
    daemon when it is on (Fork not running yet); 5. pre-launch hooks unless
    ``no_hooks``; 6. record the session, send Wine's output to the log and
    exec. When anything fails before the exec the daemon is stopped again.
    """
    targets = resolve_targets(raw_targets, Path(os.getcwd()), from_file_manager=from_file_manager)
    terminal = _to_terminal(app_ctx, debug)
    running = procs.fork_running(app_ctx.paths.prefix)
    fast = _fast_spec(app_ctx, targets, debug=debug, wine_debug=wine_debug, driver=driver) if running else None
    if fast is not None:
        return _exec(_maybe_to_terminal(fast, terminal), truncate=False, execvpe=execvpe)
    ctx = _ready_ctx(app_ctx, no_setup=no_setup, running=running)
    daemon = None if running else bridge.start_daemon(ctx, debug=debug)
    try:
        if not no_hooks and not running:
            pre_launch(ctx)
        spec = build_spec(
            ctx, targets, debug=debug, wine_debug=wine_debug, driver=driver, bridge_env=bridge.launch_env(ctx, daemon)
        )
        spec = _maybe_to_terminal(spec, terminal)
        write_session(ctx.paths, ctx.wine(), os.getpid(), bridge.session_record(daemon))
        return _exec(spec, truncate=not running and not debug, execvpe=execvpe)
    except BaseException:
        if daemon is not None:
            daemon.stop()
        raise
