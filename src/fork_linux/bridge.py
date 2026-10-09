"""The experimental native-git bridge: checks, the daemon and the environment Fork gets.

The bridge lets Fork run the host's ``git`` (and Linux terminals and diff /
merge tools) instead of its bundled Git for Windows. Its pieces are the MinGW
shims ``fl-shim.exe`` / ``fl-launch.exe`` (copied into ``C:\\fork-linux\\`` by
the ``host_shims`` setup step, :data:`SHIM_LAYOUT`) and the native
``fl-bridge-helper`` daemon; ``bridge/README.md`` is the specification this
module follows.

* :func:`check` / :func:`status` / :func:`host_actions_active` only look:
  they read the configuration, the files and the (cached) host git version,
  and never start anything.
* :func:`start_daemon` starts ``fl-bridge-helper --daemon`` from the
  launcher's main thread before it execs Wine (token in a fresh ``0600`` file,
  ``--parent-pid`` = the launcher, which becomes the Wine process running
  Fork), reads its ``FL_BRIDGE_PORT=<n>`` line and returns a :class:`Daemon`.
  Any failure is a warning: Fork then starts with its bundled git.
* :func:`launch_env` is what Fork's environment gets (``FORKGITINSTANCE``,
  ``FL_BRIDGE_*``, ``FL_WINE``, ``WINEPREFIX``); :func:`session_record` /
  :func:`session_env` let a second ``fork`` while Fork runs reuse the running
  daemon instead of starting another.

Enabling it records ``[git] bridge = on`` only (never touching Fork's
``settings.json``); ``[git] bridge_mode = record`` makes the shims forward to
Fork's bundled git unchanged (corpus capture).
"""

from __future__ import annotations

import logging
import os
import re
import secrets
import selectors
import shlex
import stat
import subprocess
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Any

from . import fsutil, gitconfig, resources, winecmd
from .errors import ForkLinuxError, UsageError
from .paths import Paths

log = logging.getLogger(__name__)

SECTION = "git"
KEY = "bridge"
MODE_KEY = "bridge_mode"
MODE_BRIDGE = "bridge"
MODE_RECORD = "record"
SHIM = "fl-shim.exe"
LAUNCH = "fl-launch.exe"
SHIM_EXES = (SHIM, LAUNCH)
HELPER = resources.BRIDGE_HELPER
_GIT_EXE = "git.exe"
_PROC = Path("/proc")
PERSONAS = ("fl-winexec", "fl-askpass", "fl-ssh-askpass")
HOST_WRAPPER = resources.HOST_HELPER
EXPERIMENTAL = "the native-git bridge is experimental"
GIT_INSTANCE_WIN = "C:\\fork-linux\\gitInstance"
# Where the host_shims step puts each shim, relative to C:\fork-linux (bridge/README.md).
SHIM_LAYOUT: tuple[tuple[tuple[str, ...], str], ...] = (
    (("gitInstance", "cmd", _GIT_EXE), SHIM),
    (("gitInstance", "bin", _GIT_EXE), SHIM),
    (("gitInstance", "mingw64", "bin", _GIT_EXE), SHIM),
    (("gitInstance", "bin", "bash.exe"), SHIM),
    (("gitInstance", "bin", "sh.exe"), SHIM),
    (("gitInstance", "usr", "bin", "bash.exe"), SHIM),
    (("gitInstance", "usr", "bin", "sh.exe"), SHIM),
    (("bin", LAUNCH), LAUNCH),
)
# Fork passes `rebase --update-refs` (2.38); 2.40 is the oldest release tested here, 2.50 matches
# the Git for Windows Fork 2.23.2 bundles.
MIN_GIT = (2, 40)
RECOMMENDED_GIT = (2, 50)

PORT_ENV = "FL_BRIDGE_PORT"
TOKEN_ENV = "FL_BRIDGE_TOKEN"
WINEXEC_ENV = "FL_BRIDGE_WINEXEC"
ASKPASS_ENV = "FL_BRIDGE_ASKPASS"
SSH_ASKPASS_ENV = "FL_BRIDGE_SSH_ASKPASS"
LOG_ENV = "FL_BRIDGE_LOG"
MODE_ENV = "FL_BRIDGE_MODE"
BUNDLED_GIT_ENV = "FL_BRIDGE_BUNDLED_GIT"
INSTANCE_ENV = "FORKGITINSTANCE"
PERSONA_ENVS = {"fl-winexec": WINEXEC_ENV, "fl-askpass": ASKPASS_ENV, "fl-ssh-askpass": SSH_ASKPASS_ENV}

START_TIMEOUT = 5.0
STOP_TIMEOUT = 3.0
LOG_PREFIX = "bridge-"
LOGS_KEEP = 10
CACHE_KEY = "bridge.daemon"
_PORT_RE = re.compile(rb"^FL_BRIDGE_PORT=(\d{1,5})\n$")
_TOKEN_RE = re.compile(r"^[0-9a-f]{64}$")
_VERSION_DIR_RE = re.compile(r"^\d+(?:\.\d+){1,3}$", re.ASCII)
# Repository-location variables of the launcher's own environment must not reach native git.
_DAEMON_DROP = frozenset(
    {
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_NAMESPACE",
        "GIT_PREFIX",
        "GIT_CONFIG_PARAMETERS",
        "GIT_CONFIG_COUNT",
        "WINEHOME",
        "WINEARCH",
    }
)
_LOG_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_CLOEXEC | os.O_NOFOLLOW
_UNSET = object()


# -- what is there ---------------------------------------------------------------------------


def helper_path() -> Path:
    """Where this installation keeps ``fl-bridge-helper``."""
    return resources.bridge_helper_path()


def shim_sources() -> dict[str, Path]:
    """``{name: path}`` of the built shims (:data:`SHIM_EXES`) this installation ships."""
    shims = resources.shims_dir()
    if shims is None:
        return {}
    return {name: shims / name for name in SHIM_EXES if (shims / name).is_file()}


def _missing() -> tuple[Path | None, Path, list[str]]:
    """``(shims_dir, helper, missing)``: what this installation lacks to run the bridge."""
    sources = shim_sources()
    helper = helper_path()
    missing = [exe for exe in SHIM_EXES if exe not in sources]
    if not helper.is_file():
        missing.append(HELPER)
    return resources.shims_dir(), helper, missing


def shim_targets(paths: Paths) -> list[tuple[Path, Path]]:
    """``(installed copy, source)`` for every :data:`SHIM_LAYOUT` entry whose shim is built."""
    sources = shim_sources()
    return [
        (paths.fork_linux_win_dir.joinpath(*rel), sources[name]) for rel, name in SHIM_LAYOUT if name in sources
    ]


def shims_problems(paths: Paths) -> list[str]:
    """Installed shims that are missing, links, or differ from the built ones (empty: all in place)."""
    problems = []
    for target, source in shim_targets(paths):
        rel = target.relative_to(paths.fork_linux_win_dir)
        if target.is_symlink() or not target.is_file():
            problems.append(f"{rel} is missing")
        elif fsutil.sha256_file(target) != fsutil.sha256_file(source):
            problems.append(f"{rel} differs from the built {source.name}")
    return problems


def _enabled(ctx: Any) -> tuple[bool, str]:
    """``(enabled, problem)`` from ``[git] bridge``; an invalid value counts as off."""
    try:
        return ctx.config.getbool(SECTION, KEY), ""
    except ForkLinuxError as exc:
        return False, exc.message


def mode(ctx: Any) -> str:
    """``[git] bridge_mode``: :data:`MODE_BRIDGE` (native git) or :data:`MODE_RECORD` (bundled git)."""
    return MODE_RECORD if ctx.config.get(SECTION, MODE_KEY) == MODE_RECORD else MODE_BRIDGE


def git_version(ctx: Any) -> tuple[int, int, int] | None:
    """The host ``git --version`` (cached probe; None without git)."""
    return gitconfig.host_git_version(ctx.runner, ctx.env, ctx.paths.cache_dir)


def _format_version(version: tuple[int, ...] | None) -> str | None:
    return None if version is None else ".".join(str(part) for part in version)


def _git_problem(version: tuple[int, int, int] | None) -> str:
    """Why this host git cannot serve Fork (empty when it can)."""
    if version is None:
        return "git is not installed on this computer"
    if version[:2] < MIN_GIT:
        return f"the Linux git {_format_version(version)} is older than {_format_version(MIN_GIT)}"
    return ""


@dataclass
class Readiness:
    """What :func:`check` found: is the bridge on, and can the launcher start it."""

    enabled: bool
    problems: list[str]
    missing: list[str]
    git: tuple[int, int, int] | None
    setting_problem: str = ""

    @property
    def ready(self) -> bool:
        """On, built, installed into the prefix and a usable host git."""
        return self.enabled and not self.problems


def check(ctx: Any) -> Readiness:
    """Whether the launcher would start the bridge for ``ctx`` (no side effects).

    ``ctx`` is anything with ``config``, ``paths``, ``env`` and ``runner``
    (the CLI's ``AppContext``, the bootstrap ``Ctx``, the doctor's context).
    The only write is the host git version cache of :func:`gitconfig.host_git_version`.
    """
    enabled, setting_problem = _enabled(ctx)
    _shims, _helper, missing = _missing()
    problems = []
    if missing:
        problems.append(f"{EXPERIMENTAL} and not built in this installation (missing: {', '.join(missing)})")
    else:
        installed = shims_problems(ctx.paths)
        if installed:
            more = f" and {len(installed) - 1} more" if len(installed) > 1 else ""
            problems.append(f"the shims are not installed in the prefix ({installed[0]}{more})")
    version = git_version(ctx) if enabled else None
    problem = _git_problem(version) if enabled else ""
    if problem:
        problems.append(problem)
    return Readiness(enabled, problems, missing, version, setting_problem)


def host_actions_active(ctx: Any) -> bool:
    """True when Fork's terminal / diff / merge tools should point at ``fl-launch.exe``.

    During a launch this is whether :func:`start_daemon` actually started the
    daemon (it records the outcome in ``ctx.cache``); elsewhere (setup,
    ``settings``, ``doctor``) it is :func:`check` ``.ready``: the launcher
    will provide the daemon. It never starts anything.
    """
    started = getattr(ctx, "cache", {}).get(CACHE_KEY, _UNSET)
    if started is not _UNSET:
        return started is not None
    return check(ctx).ready


# -- the daemon ------------------------------------------------------------------------------


@dataclass
class Daemon:
    """A running ``fl-bridge-helper --daemon`` and the variables Fork needs to reach it."""

    pid: int
    port: int
    env: dict[str, str]
    log_file: Path | None = None
    proc: subprocess.Popen[bytes] | None = field(default=None, repr=False)

    def stop(self) -> None:
        """SIGTERM the daemon (SIGKILL after :data:`STOP_TIMEOUT`); used when the launch is abandoned."""
        if self.proc is None:
            return
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=STOP_TIMEOUT)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self.proc = None


class _StartError(ForkLinuxError):
    """The daemon could not be started (the launch goes on with bundled git)."""


def _private_dir(path: Path) -> Path:
    """Create ``path`` (0700) or check that an existing one is a directory of ours with no group/other access."""
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise _StartError(f"{path} is not a directory owned by you")
    if stat.S_IMODE(info.st_mode) & 0o077:
        os.chmod(path, 0o700)
    return path


def personas_dir(paths: Paths) -> Path:
    """Our private directory for the persona links (when the installation ships none)."""
    return paths.data_dir / "bridge" / "bin"


def _link(target: Path, link: Path) -> None:
    """Make ``link`` a symlink to ``target`` (replaced atomically when it points elsewhere)."""
    if link.is_symlink() and os.readlink(link) == str(target):
        return
    tmp = link.with_name(f".{link.name}.{secrets.token_hex(4)}")
    tmp.symlink_to(target)
    os.replace(tmp, link)


def ensure_personas(paths: Paths, helper: Path) -> dict[str, Path]:
    """``{persona: path}``: the installation's own links, else links in :func:`personas_dir`."""
    real = os.path.realpath(helper)
    shipped = {name: helper.parent / name for name in PERSONAS}
    if all(path.is_symlink() and os.path.realpath(path) == real for path in shipped.values()):
        return shipped
    directory = _private_dir(personas_dir(paths))
    found = {}
    for name in PERSONAS:
        _link(Path(real), directory / name)
        found[name] = directory / name
    return found


def host_helper(paths: Paths) -> Path | None:
    """The ``fork-linux-host`` the daemon runs for ``fl-launch.exe`` (None when missing).

    A source checkout has only the template ``fork-linux-host.in``; a small
    wrapper in :func:`personas_dir` runs it with this Python.
    """
    installed = resources.host_helper_path()
    if installed.is_file() and os.access(installed, os.X_OK):
        return installed
    template = installed.with_name(installed.name + ".in")
    if not template.is_file():
        return None
    wrapper = _private_dir(personas_dir(paths)) / HOST_WRAPPER
    text = f"#!/bin/sh\nexec {shlex.quote(sys.executable)} -I {shlex.quote(str(template))} \"$@\"\n"
    if not wrapper.is_file() or wrapper.read_text(encoding="utf-8") != text:
        fsutil.atomic_write(wrapper, text, mode=0o700)
    return wrapper


def _utc_stamp() -> str:
    """The current UTC time for file names (tests replace this)."""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _rotate(logs_dir: Path, prefix: str, suffix: str) -> None:
    """Keep the newest :data:`LOGS_KEEP` - 1 ``<prefix>*<suffix>`` files, making room for a new one."""
    old = sorted(name for name in os.listdir(logs_dir) if name.startswith(prefix) and name.endswith(suffix))
    for name in old[: max(0, len(old) - (LOGS_KEEP - 1))]:
        (logs_dir / name).unlink()


def _new_log(paths: Paths, prefix: str, suffix: str) -> Path:
    """A fresh ``<logs>/<prefix><UTC time><suffix>`` after rotating the older ones."""
    logs_dir = fsutil.ensure_dir(paths.logs_dir)
    _rotate(logs_dir, prefix, suffix)
    return logs_dir / f"{prefix}{_utc_stamp()}{suffix}"


def write_token(paths: Paths) -> tuple[Path, str]:
    """A new 256-bit token in a fresh ``0600`` file (``O_EXCL``) in the private runtime directory."""
    token = secrets.token_hex(32)
    directory = _private_dir(paths.runtime_dir)
    path = directory / f"bridge-token-{secrets.token_hex(8)}"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        os.write(fd, (token + "\n").encode("ascii"))
    finally:
        os.close(fd)
    return path, token


def daemon_env(wine_env: Mapping[str, str], host_env: Mapping[str, str]) -> dict[str, str]:
    """The daemon's Unix-clean environment: ``wine_env`` (from :func:`winecmd.build_env`) minus
    Windows-only and repository-location variables and every ``FL_BRIDGE_*``, with the host ``PATH``."""
    env = {
        key: value
        for key, value in wine_env.items()
        if key not in _DAEMON_DROP and not key.startswith("FL_BRIDGE_") and not key.startswith("GIT_CONFIG_")
    }
    env["PATH"] = host_env.get("PATH") or os.defpath
    return env


def _read_port(proc: subprocess.Popen[bytes], stdout: IO[bytes], timeout: float) -> int:
    """The port from the daemon's single ``FL_BRIDGE_PORT=<n>`` line on ``stdout`` (within ``timeout``)."""
    deadline = time.monotonic() + timeout
    data = b""
    with selectors.DefaultSelector() as selector:
        selector.register(stdout, selectors.EVENT_READ)
        while not data.endswith(b"\n") and len(data) < 64:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                raise _StartError(f"{HELPER} did not report its port within {timeout:g} s")
            chunk = os.read(stdout.fileno(), 64 - len(data))
            if not chunk:
                code = proc.wait(timeout=STOP_TIMEOUT)
                raise _StartError(f"{HELPER} exited ({code}) before reporting its port")
            data += chunk
    match = _PORT_RE.match(data)
    port = int(match.group(1)) if match else 0
    if not 1 <= port <= 65535:
        raise _StartError(f"{HELPER} reported an invalid port line: {data[:64]!r}")
    return port


def bundled_git_win(paths: Paths, user: str) -> str | None:
    """Fork's newest bundled ``gitInstance\\<version>\\cmd\\git.exe`` as a Windows path (record mode)."""
    root = paths.fork_local_dir(user) / "gitInstance"
    try:
        names = [name for name in os.listdir(root) if _VERSION_DIR_RE.match(name)]
    except OSError:
        return None
    for name in sorted(names, key=lambda text: tuple(int(part) for part in text.split(".")), reverse=True):
        if (root / name / "cmd" / _GIT_EXE).is_file():
            return f"C:\\users\\{user}\\AppData\\Local\\Fork\\gitInstance\\{name}\\cmd\\git.exe"
    return None


def _fork_env(ctx: Any, port: int, token: str, personas: Mapping[str, Path], wine: Path) -> dict[str, str]:
    """``FORKGITINSTANCE`` and the ``FL_*`` variables of bridge/README.md for Fork's environment."""
    env = {
        INSTANCE_ENV: GIT_INSTANCE_WIN,
        PORT_ENV: str(port),
        TOKEN_ENV: token,
        "FL_WINE": str(wine),
        "WINEPREFIX": str(ctx.paths.prefix),
    }
    for name, path in personas.items():
        env[PERSONA_ENVS[name]] = str(path)
    if mode(ctx) == MODE_RECORD:
        bundled = bundled_git_win(ctx.paths, ctx.user)
        if bundled is None:
            _warn(ctx, "git bridge record mode needs Fork's bundled git, which was not found; using native git")
        else:
            env[MODE_ENV] = MODE_RECORD
            env[BUNDLED_GIT_ENV] = bundled
    return env


def _warn(ctx: Any, message: str) -> None:
    """Log ``message`` and show it through ``ctx.ui`` (the launch's bootstrap context)."""
    log.warning("%s", message)
    ctx.ui.warn(message)


def _spawn(argv: list[str], env: Mapping[str, str], stderr: int) -> subprocess.Popen[bytes]:
    """Start the daemon (argument list, no shell; stdout is the port pipe)."""
    return subprocess.Popen(  # the daemon must outlive this call, so procrun's run() does not fit
        argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=stderr, env=dict(env), close_fds=True
    )


def _start(ctx: Any, *, debug: bool) -> Daemon:
    """Start the daemon as bridge/README.md describes; :class:`_StartError` / ``OSError`` on failure."""
    paths: Paths = ctx.paths
    helper = helper_path()
    info = ctx.wine()
    personas = ensure_personas(paths, helper)
    wine_env = winecmd.build_env(paths, info, user=ctx.user, base_env=ctx.env)
    log_file = _new_log(paths, LOG_PREFIX, ".log")
    token_file, token = write_token(paths)
    try:
        argv = [str(helper), "--daemon", "--token-file", str(token_file), "--parent-pid", str(os.getpid())]
        host = host_helper(paths)
        if host is not None:
            argv += ["--host-helper", str(host)]
        argv += ["--log", str(log_file)]
        fd = os.open(log_file, _LOG_FLAGS, 0o600)
        try:
            proc = _spawn(argv, daemon_env(wine_env, ctx.env), fd)
        finally:
            os.close(fd)
        stdout = proc.stdout
        assert stdout is not None
        try:
            port = _read_port(proc, stdout, START_TIMEOUT)
        except BaseException:
            Daemon(proc.pid, 0, {}, proc=proc).stop()
            raise
        finally:
            stdout.close()
    finally:
        # The daemon unlinks the file once it read the token; never leave it behind.
        if os.path.lexists(token_file):
            token_file.unlink()
    env = _fork_env(ctx, port, token, personas, info.wine)
    if debug:
        env[LOG_ENV] = str(_new_log(paths, LOG_PREFIX + "calls-", ".jsonl"))
    return Daemon(proc.pid, port, env, log_file=log_file, proc=proc)


def start_daemon(ctx: Any, *, debug: bool = False) -> Daemon | None:
    """Start the bridge daemon for a launch when ``[git] bridge`` is on and everything is there.

    Must run on the launcher's main thread right before it execs Wine
    (``--parent-pid`` and the daemon's ``PDEATHSIG`` then track the Wine
    process that runs Fork). Returns None, after a warning when the bridge is
    on, if the bridge is off or cannot start: Fork then uses its bundled git.
    The outcome is recorded in ``ctx.cache`` for :func:`host_actions_active`.
    """
    ctx.cache[CACHE_KEY] = None
    readiness = check(ctx)
    if not readiness.enabled:
        return None
    if readiness.problems:
        _warn(ctx, f"git bridge not started ({'; '.join(readiness.problems)}); Fork uses its bundled git")
        return None
    try:
        daemon = _start(ctx, debug=debug)
    except (ForkLinuxError, OSError, subprocess.SubprocessError) as exc:
        _warn(ctx, f"git bridge not started ({exc}); Fork uses its bundled git")
        return None
    log.info("git bridge daemon pid %d on 127.0.0.1:%d", daemon.pid, daemon.port)
    ctx.cache[CACHE_KEY] = daemon
    return daemon


def launch_env(ctx: Any, daemon: Daemon | None) -> dict[str, str]:
    """The variables Fork's environment gets for the bridge: none unless it is on and ``daemon`` runs."""
    enabled, _problem = _enabled(ctx)
    if not enabled or daemon is None:
        return {}
    return dict(daemon.env)


def native_git(env: Mapping[str, str]) -> bool:
    """True when the bridge env sends Fork's git calls to native git (not record mode)."""
    return INSTANCE_ENV in env and env.get(MODE_ENV) != MODE_RECORD


# -- the running session ---------------------------------------------------------------------


def session_record(daemon: Daemon | None) -> dict[str, Any] | None:
    """What ``session.json`` (0600) keeps so a second launch can reuse the daemon."""
    if daemon is None:
        return None
    return {"pid": daemon.pid, "port": daemon.port, "env": dict(daemon.env)}


def daemon_alive(pid: Any, proc_root: Path = _PROC) -> bool:
    """True when ``pid`` is a running ``fl-bridge-helper`` of ours."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        cmdline = (proc_root / str(pid) / "cmdline").read_bytes()
    except OSError:
        return False
    return HELPER.encode() in cmdline and b"--daemon" in cmdline


def session_env(ctx: Any, session: Mapping[str, Any] | None, proc_root: Path = _PROC) -> dict[str, str]:
    """The running daemon's variables from ``session``, when the bridge is on and that daemon still runs."""
    enabled, _problem = _enabled(ctx)
    record = session.get("bridge") if session is not None else None
    if not enabled or not isinstance(record, dict) or not daemon_alive(record.get("pid"), proc_root):
        return {}
    env = record.get("env")
    if not isinstance(env, dict) or not all(isinstance(v, str) for v in env.values()):
        return {}
    if not _TOKEN_RE.match(env.get(TOKEN_ENV, "")) or not env.get(PORT_ENV, "").isdigit():
        return {}
    return {str(key): value for key, value in env.items()}


def running_daemon(paths: Paths, session: Mapping[str, Any] | None, proc_root: Path = _PROC) -> dict[str, Any]:
    """``{pid, port}`` of the daemon recorded in ``session`` while it runs (empty otherwise)."""
    record = session.get("bridge") if session is not None else None
    if not isinstance(record, dict) or session.get("prefix") != str(paths.prefix):
        return {}
    if not daemon_alive(record.get("pid"), proc_root):
        return {}
    return {"pid": record["pid"], "port": record.get("port")}


# -- CLI --------------------------------------------------------------------------------------


def status(ctx: Any, session: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """The bridge's state for ``git-bridge status`` and ``doctor`` (no token, nothing started).

    ``available``: this installation ships the shims and the helper;
    ``ready``: enabled, built, installed in the prefix, with a usable host git
    (the next Fork start uses it); ``reason`` explains what is missing.
    """
    readiness = check(ctx)
    shims, helper, _missing_items = _missing()
    reasons = []
    if readiness.missing:
        reasons.append(readiness.problems[0])
    elif readiness.enabled:
        reasons += readiness.problems
    if readiness.setting_problem:
        reasons.append(readiness.setting_problem)
    version = readiness.git
    return {
        "enabled": readiness.enabled,
        "available": not readiness.missing,
        "ready": readiness.ready,
        "mode": mode(ctx),
        "reason": "; ".join(reasons),
        "shims_dir": None if shims is None else str(shims),
        "helper": str(helper),
        "git_version": _format_version(version),
        "git_recommended": version is None or version[:2] >= RECOMMENDED_GIT,
        "daemon": running_daemon(ctx.paths, session),
    }


def enable(ctx: Any) -> list[str]:
    """Set ``[git] bridge = on``; return warnings (an older git). :class:`UsageError` when unusable."""
    _shims, _helper, missing = _missing()
    if missing:
        raise UsageError(
            f"{EXPERIMENTAL} and not built in this installation (missing: {', '.join(missing)})",
            hint="install a fork-linux package built with the bridge, or build it from a source checkout "
            "with scripts/build-bridge.sh; Fork keeps using its bundled git meanwhile",
        )
    version = git_version(ctx)
    problem = _git_problem(version)
    if problem:
        raise UsageError(
            f"cannot enable the git bridge: {problem}",
            hint=f"install git {_format_version(MIN_GIT)} or newer (Fork needs rebase --update-refs); "
            "Fork keeps using its bundled git meanwhile",
        )
    ctx.config.set(SECTION, KEY, "on")
    warnings = []
    assert version is not None
    if version[:2] < RECOMMENDED_GIT:
        warnings.append(
            f"the Linux git {_format_version(version)} is older than {_format_version(RECOMMENDED_GIT)} "
            "(the version Fork bundles); some Fork features may not work"
        )
    return warnings


def disable(ctx: Any) -> None:
    """Set ``[git] bridge = off`` (Fork goes back to its bundled git on the next start)."""
    ctx.config.set(SECTION, KEY, "off")


def set_mode(ctx: Any, record: bool) -> None:
    """``[git] bridge_mode``: record (shims forward to Fork's bundled git) or bridge (native git)."""
    ctx.config.set(SECTION, MODE_KEY, MODE_RECORD if record else MODE_BRIDGE)
