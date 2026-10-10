"""``fork-linux doctor``: diagnose the host, Wine, the prefix, Fork and our integrations.

Every check is a :class:`Check` whose ``run(ctx)`` returns a :class:`Result`
(``ok``, ``info``, ``warn`` or ``fail``) with a short detail and, when
something is wrong, a hint saying what to do. Checks only read: Wine is
resolved without installing anything, the registry is read straight from the
hive files and Fork's files are never touched. ``deep`` checks start Fork's
bundled git under Wine; ``network`` checks send HEAD requests to the hosts we
download from.

:func:`fix` repairs what it can: it re-runs the setup steps named in a failing
check's ``fix_steps`` (through :func:`bootstrap.run_steps`, under the setup
lock) or calls the check's own ``fixer``, then checks again.
"""

from __future__ import annotations

import logging
import os
import platform
import re
import stat
import sys
import tempfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import (
    APP_NAME,
    bootstrap,
    bridge,
    desktop_integration,
    download,
    feeds,
    fork_data,
    fork_tools,
    fsutil,
    gitconfig,
    hostdeps,
    launcher,
    procs,
    repos,
    resources,
    sandbox,
    snapshots,
    ssh_sync,
    steps,
    updates,
    versions,
    wine_provider,
    winecmd,
)
from . import display as display_mod
from . import fork_settings as settings_mod
from . import manifest as manifest_mod
from . import ui as ui_mod
from .config import Config
from .errors import ForkLinuxError, UsageError
from .fork_layout import ForkLayout
from .manifest import Manifest
from .pathmap import PathMap
from .paths import Paths
from .procrun import Runner, tail
from .state import FORK_VERSION, State
from .steps import display as display_step
from .steps import dotnet as dotnet_step
from .steps import fonts as fonts_step
from .steps import integration as integration_step
from .steps import prefix as prefix_step
from .steps import preflight as preflight_step
from .winecmd import WineInfo

log = logging.getLogger(__name__)

SCHEMA = 1
STATUSES = ("ok", "info", "warn", "fail")
NOT_OK = ("warn", "fail")

_NO_WINE = "no usable Wine"
_NOT_INSTALLED = "Fork is not installed"
_NOTHING_TO_DO = "nothing to do"
_RUN_FIX = "run 'fork-linux doctor --fix'"
X86_64 = ("x86_64", "amd64")
MIN_PYTHON = (3, 10)
FC_LIST_TIMEOUT = 30.0
# The ClearType gamma range Windows accepts (SPI_SETFONTSMOOTHINGCONTRAST).
GAMMA_MIN = 1000
GAMMA_MAX = 2200
GIT_TIMEOUT = 120.0
VALIDATE_TIMEOUT = 30.0
HEAD_TIMEOUT = 10.0
LOG_TAIL_BYTES = 512 * 1024
CLI_PROBE_BYTES = 64 * 1024
# Text our launchers (and development wrappers around them) contain.
OUR_MARKERS = (b"fork_linux", b"fork-linux", APP_NAME.encode("utf-8"))
ARCH_LINE = "#arch=win64"
HIVE_HEAD_BYTES = 64 * 1024
NO_PREFIX = "the Wine prefix does not exist yet"
GUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
MACHINE_GUID_KEY = r"HKLM\Software\Microsoft\Cryptography"
MENUBUILDER_OFF_VALUES = ("", "d", "disabled")
HOOK_LIMITATION = (
    "git hooks that start other programs cannot run under Wine (msys bash cannot spawn children "
    "on Wine staging and hangs on other builds; Wine bug 55138)"
)
_WINESERVER = b"WINESERVER="

# (needles that must all be present, alternatives of which one must be present, hint)
LOG_SIGNATURES: tuple[tuple[tuple[str, ...], tuple[str, ...], str, str], ...] = (
    (
        ("0x88980406",),
        (),
        "WPF render thread failure (0x88980406)",
        (
            "switch Fork's Direct3D renderer: 'fork-linux config set wine.renderer gdi' (or gl), "
            "then 'fork-linux setup --only registry'"
        ),
    ),
    (
        ("TypeLoadException",),
        ("WinRT", "Windows.UI"),
        "Fork tried to load Windows 10 (WinRT) types",
        "Fork.exe must run as Windows 7 (AppDefaults): run 'fork-linux setup --only registry'",
    ),
    (
        (),
        ("NotificationManager", "Windows.Data.Xml.Dom"),
        "Windows notifications are unavailable under Wine (fork-dev/TrackerWin#2862)",
        (
            "disconnect the GitHub account in Fork and sign in with a Personal Access Token instead of "
            "GitHub's browser login"
        ),
    ),
    (
        (),
        ("SSL", "schannel"),
        "TLS (SSL/schannel) errors",
        "check the host's GnuTLS library ('fork-linux doctor --check host.libs'), the system clock and any HTTPS proxy",
    ),
)


@dataclass(frozen=True)
class Result:
    """The outcome of one check: ``status`` is ``ok``, ``info``, ``warn`` or ``fail``."""

    status: str
    detail: str
    hint: str = ""


@dataclass(frozen=True)
class Check:
    """One diagnostic: ``run(ctx)`` -> :class:`Result`.

    ``deep`` checks run only with ``--deep``, ``network`` checks only with
    ``--network`` (both run when named with ``--check``). ``fix_steps`` are
    setup steps whose re-run repairs a failure; ``fixer(ctx)`` is a custom
    repair returning a description of what it did.
    """

    id: str
    title: str
    group: str
    run: Callable[[DoctorCtx], Result]
    deep: bool = False
    network: bool = False
    fix_steps: tuple[str, ...] = ()
    fixer: Callable[[DoctorCtx], str] | None = None

    @property
    def fixable(self) -> bool:
        """True when ``--fix`` can do something about this check."""
        return bool(self.fix_steps) or self.fixer is not None


@dataclass
class DoctorCtx:
    """What the checks look at. The Wine is resolved lazily and never installed."""

    paths: Paths
    config: Config
    manifest: Manifest
    runner: Runner
    env: dict[str, str]
    state: State
    user: str
    offline: bool = False
    proc_root: Path = procs.PROC
    ui: ui_mod.UI = field(default_factory=ui_mod.NullUI)
    lib_loader: Callable[[str], object] | None = None
    state_error: str = ""
    user_error: str = ""
    allow_root: bool = False
    deep: bool = False
    network: bool = False
    _wine: WineInfo | None = field(default=None, repr=False)
    _wine_error: str | None = field(default=None, repr=False)
    _layout: ForkLayout | None = field(default=None, repr=False)
    _boot: bootstrap.Ctx | None = field(default=None, repr=False)
    _repos: list[repos.RepoReport] | None = field(default=None, repr=False)

    @classmethod
    def from_app(cls, app_ctx: Any, **kwargs: Any) -> DoctorCtx:
        """A context for the CLI's :class:`~fork_linux.cli.AppContext`.

        A corrupt ``state.json`` or an unusable user name does not stop the
        doctor: they are remembered and reported by the checks.
        """
        paths: Paths = app_ctx.paths
        env = dict(app_ctx.env)
        state_error = ""
        try:
            state = State.load(paths.state_file)
        except ForkLinuxError as exc:
            state_error = exc.message
            state = State(paths.state_file, is_new=True)
        user_error = ""
        try:
            user = winecmd.windows_user(env)
        except UsageError as exc:
            user_error = exc.message
            user = ""
        kwargs.setdefault("offline", bool(app_ctx.offline))
        kwargs.setdefault(
            "allow_root", bool(getattr(app_ctx.args, "allow_root", False)) or env.get(bootstrap.ALLOW_ROOT_ENV) == "1"
        )
        return cls(
            paths=paths,
            config=app_ctx.config,
            manifest=manifest_mod.load(override=paths.manifest_override),
            runner=app_ctx.runner,
            env=env,
            state=state,
            user=user,
            state_error=state_error,
            user_error=user_error,
            **kwargs,
        )

    @property
    def host_home(self) -> Path:
        """The user's Linux home directory."""
        return Path(self.env.get("HOME") or Path.home())

    @property
    def data_home(self) -> Path:
        """``$XDG_DATA_HOME`` (the parent of our data directory)."""
        return self.paths.data_dir.parent

    @property
    def layout(self) -> ForkLayout:
        """Fork's files for our user (:class:`UsageError` without a usable user name)."""
        if self._layout is None:
            if self.user_error:
                raise UsageError(self.user_error, hint="set USER to your plain user name")
            self._layout = ForkLayout(self.paths, self.user)
        return self._layout

    @property
    def pathmap(self) -> PathMap:
        """The prefix's drive mapping."""
        return PathMap.from_prefix(self.paths.prefix)

    def wine(self) -> WineInfo | None:
        """The configured Wine, or None when it is not available (see :meth:`wine_error`)."""
        if self._wine is None and self._wine_error is None:
            try:
                self._wine = wine_provider.resolve(
                    self.config,
                    self.manifest,
                    self.paths,
                    self.runner,
                    self.env,
                    install=False,
                    offline=self.offline,
                )
            except ForkLinuxError as exc:
                self._wine_error = exc.message + (f" ({exc.hint})" if exc.hint else "")
        return self._wine

    def wine_error(self) -> str:
        """Why :meth:`wine` returned None ("" when it did not)."""
        self.wine()
        return self._wine_error or ""

    def reset(self) -> None:
        """Forget cached answers (after a fix changed things)."""
        self._wine = None
        self._wine_error = None
        self._layout = None
        self._boot = None
        self._repos = None

    @property
    def boot(self) -> bootstrap.Ctx:
        """A setup context sharing our state (step helpers and :func:`fix` use it)."""
        if self._boot is None:
            self._boot = bootstrap.Ctx(
                paths=self.paths,
                config=self.config,
                manifest=self.manifest,
                runner=self.runner,
                env=self.env,
                ui=self.ui,
                state=self.state,
                user=self.user,
                offline=self.offline,
                allow_root=self.allow_root,
                proc_root=self.proc_root,
            )
            info = self.wine()
            if info is not None:
                self._boot.set_wine(info)
        return self._boot


@dataclass
class FixReport:
    """What :func:`fix` did: the refreshed ``results``, the ``actions`` taken and the ``errors`` met."""

    results: list[tuple[Check, Result]]
    actions: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


# -- small helpers ---------------------------------------------------------------------------------


def _machine() -> str:
    """The CPU architecture (a seam for tests)."""
    return platform.machine()


def _python() -> tuple[int, int, int]:
    """The running Python version (a seam for tests)."""
    return (sys.version_info[0], sys.version_info[1], sys.version_info[2])


def _euid() -> int:
    """The effective user id (a seam for tests)."""
    return os.geteuid()


def _head_ok(url: str) -> bool:
    """HEAD ``url`` (a seam for tests)."""
    return download.head_ok(url, timeout=HEAD_TIMEOUT)


def _gib(size: int) -> str:
    return f"{size / preflight_step.GiB:.1f} GiB"


def _skip(why: str) -> Result:
    return Result("info", f"skipped: {why}")


def _hive_ok(path: Path) -> bool:
    """True if ``path`` exists and declares a 64-bit prefix near its top."""
    head = _read_text(path, HIVE_HEAD_BYTES) or ""
    return any(line.strip() == ARCH_LINE for line in head.splitlines())


def _has_prefix(ctx: DoctorCtx) -> bool:
    return (ctx.paths.prefix / "system.reg").is_file()


def _reg(ctx: DoctorCtx, key: str, name: str) -> object | None:
    return prefix_step.reg_value(ctx.boot, key, name)


def _read_bytes(path: Path, limit: int, *, from_end: bool = False) -> bytes | None:
    """The first (or, ``from_end``, the last) ``limit`` bytes of a regular file, or None.

    Symbolic links are not followed and FIFOs never block.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return None
        if from_end:
            os.lseek(fd, max(0, info.st_size - limit), os.SEEK_SET)
        with os.fdopen(fd, "rb", closefd=False) as handle:
            return handle.read(limit)
    finally:
        os.close(fd)


def _read_text(path: Path, limit: int, *, from_end: bool = False) -> str | None:
    """:func:`_read_bytes` decoded as UTF-8 (invalid bytes replaced)."""
    data = _read_bytes(path, limit, from_end=from_end)
    return None if data is None else data.decode("utf-8", errors="replace")


def _install_hint(missing_tools: Sequence[str], missing_libs: Sequence[str]) -> str:
    return hostdeps.install_hint(hostdeps.distro().family, missing_tools, missing_libs)


# -- env -------------------------------------------------------------------------------------------


def check_arch(_ctx: DoctorCtx) -> Result:
    """x86_64 only."""
    machine = _machine().lower()
    if machine in X86_64:
        return Result("ok", machine)
    return Result(
        "fail",
        f"this machine is {machine or 'unknown'}; Fork for Windows is x86_64 only",
        "Fork for Linux (unofficial) supports x86_64 (amd64) machines only",
    )


def check_python(_ctx: DoctorCtx) -> Result:
    """Python 3.10 or newer."""
    version = _python()
    text = ".".join(str(part) for part in version)
    if version[:2] >= MIN_PYTHON:
        return Result("ok", f"Python {text}")
    return Result("fail", f"Python {text} is too old", "install Python 3.10 or newer")


def check_user(ctx: DoctorCtx) -> Result:
    """Not root; a user name usable as ``C:\\users\\<name>``."""
    if ctx.user_error:
        return Result("fail", ctx.user_error, "set USER to your plain user name")
    if _euid() == 0:
        if ctx.allow_root:
            return Result("warn", "running as root (allowed explicitly)", "use root only in containers and CI")
        return Result("fail", "running as root", "run fork-linux as your normal user: Wine prefixes are per user")
    return Result("ok", f"{ctx.user} (C:\\users\\{ctx.user})")


def check_display(ctx: DoctorCtx) -> Result:
    """A graphical session Wine's X11 driver can use (DISPLAY, or XWayland).

    The sockets named by ``DISPLAY`` / ``WAYLAND_DISPLAY`` decide, not
    ``XDG_SESSION_TYPE``: a shell started from a Wayland desktop keeps that
    variable while talking to another X server (Xvfb, ssh -X).
    """
    x_display = ctx.env.get("DISPLAY", "")
    wayland = bool(ctx.env.get("WAYLAND_DISPLAY"))
    if x_display:
        where = "wayland session, XWayland" if wayland else "X11"
        return Result("ok", f"{where} display {x_display}")
    if wayland:
        if ctx.config.get("wine", "driver") == "wayland":
            return Result("warn", "Wayland session without XWayland; using Wine's experimental Wayland driver")
        return Result(
            "fail",
            "Wayland session without XWayland (DISPLAY is not set)",
            "enable XWayland in your desktop, or try 'fork-linux config set wine.driver wayland' (experimental)",
        )
    session = display_mod.session_type(ctx.env)
    declared = f" ({session} declared by XDG_SESSION_TYPE)" if session != "tty" else ""
    return Result(
        "warn",
        f"no graphical session (DISPLAY and WAYLAND_DISPLAY are not set){declared}",
        "Fork needs a desktop session; setup and doctor work without one",
    )


def check_disk(ctx: DoctorCtx) -> Result:
    """Enough free space next to the prefix."""
    fresh = not _has_prefix(ctx)
    needed = preflight_step.MIN_FREE if fresh else preflight_step.MIN_FREE_RESUME
    free = fsutil.disk_free(ctx.paths.prefix.parent)
    detail = f"{_gib(free)} free for {ctx.paths.prefix}"
    if free < needed:
        return Result(
            "fail",
            f"{detail}; {_gib(needed)} needed",
            "free some space (Wine, .NET and Fork take about 3 GB), or choose another prefix with --prefix",
        )
    return Result("ok", detail)


# -- host ------------------------------------------------------------------------------------------


def check_tools(ctx: DoctorCtx) -> Result:
    """Required (and useful optional) host tools on PATH."""
    required = hostdeps.missing_required_tools(ctx.runner)
    optional = hostdeps.missing_tools(ctx.runner, hostdeps.OPTIONAL_TOOLS)
    if required:
        return Result("fail", f"missing: {', '.join(required)}", _install_hint(required, []))
    if optional:
        return Result(
            "info",
            f"required tools present; optional tools missing: {', '.join(optional)}",
            _install_hint(optional, []),
        )
    return Result("ok", "all required and optional tools are present")


def check_libs(ctx: DoctorCtx) -> Result:
    """Host libraries Wine loads, and an ``ldd`` scan of the managed runtime."""
    required = hostdeps.missing_libs(hostdeps.REQUIRED_LIBS, ctx.lib_loader)
    optional = hostdeps.missing_libs(hostdeps.OPTIONAL_LIBS, ctx.lib_loader)
    info = ctx.wine()
    unresolved: list[str] = []
    if info is not None and info.provider == "managed":
        unresolved = hostdeps.ldd_missing(ctx.runner, info.root)
    # ldd reports every optional Wine module (OpenCL, scanners, cameras, smart cards, USB,
    # GStreamer, Wayland, ...); a missing dependency there only disables that feature, so
    # only the libraries Wine itself needs (REQUIRED_LIBS) fail the check.
    blocking = [lib for lib in unresolved if lib in hostdeps.REQUIRED_LIBS and lib not in required]
    if required or blocking:
        parts = []
        if required:
            parts.append(f"missing libraries: {', '.join(required)}")
        if blocking:
            parts.append(f"the Wine runtime needs: {', '.join(blocking)}")
        return Result("fail", "; ".join(parts), _install_hint([], [*required, *blocking]))
    extra = [lib for lib in unresolved if lib not in hostdeps.REQUIRED_LIBS]
    if optional or extra:
        parts = []
        if optional:
            parts.append(f"optional libraries missing: {', '.join(optional)}")
        if extra:
            parts.append(f"optional Wine features unavailable (not used by Fork): {', '.join(extra)}")
        return Result("info", "; ".join(parts), _install_hint([], [*optional, *extra]))
    scanned = " (Wine runtime scanned with ldd)" if info is not None and info.provider == "managed" else ""
    return Result("ok", f"all {len(hostdeps.REQUIRED_LIBS)} required libraries load{scanned}")


def _families(ctx: DoctorCtx) -> set[str] | None:
    """Font families from ``fc-list``, or None when it is unavailable."""
    if ctx.runner.which("fc-list", sandbox.clean_env(ctx.env).get("PATH")) is None:
        return None
    try:
        result = ctx.runner.run(["fc-list", ":", "family"], env=sandbox.clean_env(ctx.env), timeout=FC_LIST_TIMEOUT)
    except ForkLinuxError:
        return None
    return fonts_step.parse_families(result.stdout) if result.ok else None


def check_fonts(ctx: DoctorCtx) -> Result:
    """Linux fonts that can stand in for Segoe UI Symbol and Consolas (Segoe UI itself is Selawik)."""
    families = _families(ctx)
    if families is None:
        return Result("warn", "fc-list is not available; cannot check the fonts", _install_hint(["fc-list"], []))
    symbol = next((name for name in fonts_step.SYMBOL_CHOICES if name in families), None)
    mono = next((name for name in fonts_step.MONO_CHOICES if name in families), None)
    if symbol and mono:
        return Result("ok", f"Segoe UI Symbol -> {symbol}, Consolas -> {mono}")
    missing = []
    if not symbol:
        choices = " / ".join(fonts_step.SYMBOL_CHOICES)
        missing.append(f"a Segoe UI Symbol replacement ({choices}; using {fonts_step.SYMBOL_FALLBACK})")
    if not mono:
        choices = " / ".join(fonts_step.MONO_CHOICES)
        missing.append(f"a monospace font ({choices}; using {fonts_step.MONO_FALLBACK})")
    return Result(
        "warn",
        "missing " + " and ".join(missing),
        "install DejaVu or Noto fonts (e.g. fonts-dejavu-core / dejavu-sans-fonts), "
        "then run 'fork-linux doctor --fix'",
    )


# -- wine ------------------------------------------------------------------------------------------


def _wine_label(info: WineInfo) -> str:
    flavor = " (Staging)" if info.staging else ""
    build = f" {info.build_id}" if info.build_id else ""
    return f"{info.provider}{build}: wine {info.version}{flavor} at {info.wine}"


def check_wine_present(ctx: DoctorCtx) -> Result:
    """The configured Wine is installed and runs."""
    info = ctx.wine()
    if info is None:
        return Result("fail", ctx.wine_error(), "run 'fork-linux setup' (or 'fork-linux doctor --fix')")
    return Result("ok", _wine_label(info))


def check_wine_version(ctx: DoctorCtx) -> Result:
    """Managed: the build is complete and pinned; others: at least the minimum version."""
    info = ctx.wine()
    if info is None:
        return _skip(_NO_WINE)
    if info.provider == "managed" and info.build_id is not None:
        build = ctx.manifest.wine_build(info.build_id)
        if not wine_provider.is_installed(ctx.paths, build):
            return Result(
                "fail",
                f"the managed Wine {build.id} is incomplete (its .complete sha256 does not match the manifest)",
                "run 'fork-linux doctor --fix' to reinstall it",
            )
        return Result("ok", f"{build.id} ({build.status}); sha256 matches the manifest")
    minimum = ctx.manifest.min_system_wine
    if not info.version or versions.Version(info.version) < versions.Version(minimum):
        return Result(
            "fail",
            f"wine {info.version or '?'} is older than the required {minimum}",
            "install a newer Wine, or use the managed Wine: 'fork-linux config set wine.provider managed'",
        )
    return Result("ok", f"wine {info.version} (>= {minimum})")


def check_wine_staging(ctx: DoctorCtx) -> Result:
    """A staging build: on other builds hooks and bash custom commands hang (Wine bug 55138)."""
    info = ctx.wine()
    if info is None:
        return _skip(_NO_WINE)
    if info.staging:
        return Result("ok", f"wine {info.version} is a staging build")
    return Result(
        "warn",
        f"wine {info.version} is not a staging build: git hooks and bash custom commands may hang (Wine bug 55138)",
        "use the managed Wine (a staging build): 'fork-linux config set wine.provider managed'",
    )


def _wineserver_of(environ: bytes) -> str | None:
    for item in environ.split(b"\0"):
        if item.startswith(_WINESERVER):
            return os.fsdecode(item[len(_WINESERVER) :])
    return None


def _read_proc(path: Path) -> bytes | None:
    try:
        with open(path, "rb") as handle:
            return handle.read(procs.MAX_BYTES)
    except OSError:
        return None


def _same_build(real: str, expected: str, homes: set[str]) -> bool:
    """True if the wineserver ``real`` is ``expected`` or lives in the same Wine tree.

    Distributions put a ``wineserver`` wrapper on PATH and the binary
    (``wineserver64``) elsewhere under the same root (``/usr/lib/wine``).
    """
    if real == expected:
        return True
    return any(real.startswith(home.rstrip(os.sep) + os.sep) for home in homes)


def foreign_wineservers(ctx: DoctorCtx, info: WineInfo) -> list[tuple[int, str]]:
    """``(pid, wineserver)`` of prefix processes started by another Wine build than ``info``."""
    expected = os.path.realpath(info.wineserver)
    homes = {os.path.dirname(expected), os.path.realpath(info.root)}
    found = []
    for pid in procs.prefix_pids(ctx.paths.prefix, proc_root=ctx.proc_root):
        base = ctx.proc_root / str(pid)
        candidates = []
        environ = _read_proc(base / "environ")
        server = None if environ is None else _wineserver_of(environ)
        if server and os.path.isabs(server):
            candidates.append(server)
        try:
            exe = os.readlink(base / "exe")
        except OSError:
            exe = ""
        if os.path.basename(exe).startswith("wineserver"):
            candidates.append(exe)
        for candidate in candidates:
            if not _same_build(os.path.realpath(candidate), expected, homes):
                found.append((pid, candidate))
                break
    return found


def check_wineserver(ctx: DoctorCtx) -> Result:
    """No process in our prefix runs with a wineserver from another Wine build."""
    info = ctx.wine()
    if info is None:
        return _skip(_NO_WINE)
    foreign = foreign_wineservers(ctx, info)
    if foreign:
        listed = ", ".join(f"pid {pid} ({server})" for pid, server in foreign)
        return Result(
            "fail",
            f"the prefix is in use by another Wine build: {listed}",
            "close Fork and every Wine program in this prefix (or run that build's 'wineserver -k' "
            f"with WINEPREFIX={ctx.paths.prefix}), then start Fork again",
        )
    return Result("ok", f"no Wine process of another build uses the prefix (expected {info.wineserver})")


# -- prefix ----------------------------------------------------------------------------------------


def check_prefix(ctx: DoctorCtx) -> Result:
    """The prefix exists, is ours, 64-bit, private, and its state file is readable."""
    prefix = ctx.paths.prefix
    if not prefix.is_dir():
        return Result("fail", f"{prefix} does not exist (not set up yet)", "run 'fork-linux setup'")
    problems = [f"{name} is missing or not 64-bit" for name in prefix_step.HIVES if not _hive_ok(prefix / name)]
    if problems:
        return Result("fail", "; ".join(problems), "run 'fork-linux doctor --fix' (or 'fork-linux setup')")
    if not ctx.paths.created_by_marker.is_file():
        return Result("warn", f"{prefix} has no created-by marker", "run 'fork-linux setup' to adopt it properly")
    if ctx.state_error:
        return Result("fail", ctx.state_error, "move the state file aside and run 'fork-linux setup'")
    mode = stat.S_IMODE(prefix.stat().st_mode)
    if mode & 0o077:
        return Result("warn", f"{prefix} is accessible by other users (mode {mode:04o})", f"chmod 700 '{prefix}'")
    return Result("ok", f"{prefix} (win64)")


def check_dotnet(ctx: DoctorCtx) -> Result:
    """.NET Framework 4.7.2+ (``NDP\\v4\\Full`` ``Release``)."""
    if not _has_prefix(ctx):
        return _skip(NO_PREFIX)
    found = dotnet_step.release(ctx.boot)
    minimum = ctx.manifest.dotnet_min_release
    if found is None:
        return Result("fail", ".NET Framework 4.x is not installed", _RUN_FIX)
    if found < minimum:
        return Result(
            "fail", f".NET Framework release {found} is older than {minimum}", _RUN_FIX
        )
    return Result("ok", f".NET Framework release {found} (>= {minimum})")


def check_corefonts(ctx: DoctorCtx) -> Result:
    """Microsoft core fonts are in the prefix."""
    if not _has_prefix(ctx):
        return _skip(NO_PREFIX)
    found = fonts_step.arial(ctx.boot)
    if found is None:
        return Result("fail", "Microsoft core fonts are not installed", _RUN_FIX)
    if not fonts_step.verify_fonts(ctx.boot):
        return Result(
            "warn", "Microsoft core fonts are only partly installed", _RUN_FIX
        )
    return Result("ok", f"core fonts installed ({found.name})")


def check_ui_font(ctx: DoctorCtx) -> Result:
    """The interface font (Selawik, standing in for Segoe UI) is installed and registered in the prefix."""
    if not _has_prefix(ctx):
        return _skip(NO_PREFIX)
    font = ctx.manifest.ui_font
    missing = fonts_step.missing_faces(ctx.boot)
    if missing:
        return Result("fail", f"{font.family} is not installed (missing {', '.join(missing)})", _RUN_FIX)
    unregistered = [
        name for key, name, value in fonts_step.ui_font_registry(font) if _reg(ctx, key, name) != value
    ]
    if unregistered:
        return Result("warn", f"{font.family} faces are not registered: {', '.join(unregistered)}", _RUN_FIX)
    return Result("ok", f"{font.family} {font.version} ({font.license}), {len(font.faces)} faces")


def _shown(target: str | list[str]) -> str:
    return " | ".join(target) if isinstance(target, list) else target


def check_font_replacements(ctx: DoctorCtx) -> Result:
    """Segoe UI / Consolas are mapped to installed fonts, and the system fonts ask for Segoe UI."""
    if not _has_prefix(ctx):
        return _skip(NO_PREFIX)
    wanted = fonts_step.replacements(ctx.boot)
    wrong = [name for name, target in wanted.items() if _reg(ctx, fonts_step.REPLACEMENTS_KEY, name) != target]
    if wrong:
        return Result(
            "warn", f"replacements missing or outdated for: {', '.join(wrong)}", _RUN_FIX
        )
    system = fonts_step.wrong_system_fonts(ctx.boot)
    if system:
        return Result(
            "warn",
            f"system fonts do not use {fonts_step.SEGOE_UI} (Wine's Tahoma is used instead): {', '.join(system)}",
            _RUN_FIX,
        )
    shown = ", ".join(f"{name} -> {_shown(target)}" for name, target in sorted(wanted.items()))
    return Result("ok", f"{shown}; system fonts: {fonts_step.SEGOE_UI}")


def _registry_result(ctx: DoctorCtx, key: str, name: str, expected: object, what: str) -> Result:
    if not _has_prefix(ctx):
        return _skip(NO_PREFIX)
    value = _reg(ctx, key, name)
    if value == expected:
        return Result("ok", what)
    return Result(
        "fail",
        f"{key}\\{name} is {value!r}, expected {expected!r}",
        _RUN_FIX,
    )


def check_font_smoothing(ctx: DoctorCtx) -> Result:
    """Font smoothing is on, with a ClearType gamma Windows accepts (1000-2200; Wine's 0 means unset)."""
    if not _has_prefix(ctx):
        return _skip(NO_PREFIX)
    key = prefix_step.DESKTOP_KEY
    smoothing, gamma = (_reg(ctx, key, name) for name in ("FontSmoothing", "FontSmoothingGamma"))
    if smoothing != "2":
        return Result("warn", f"font smoothing is off (FontSmoothing is {smoothing!r})", _RUN_FIX)
    if not isinstance(gamma, int) or not GAMMA_MIN <= gamma <= GAMMA_MAX:
        return Result(
            "warn", f"FontSmoothingGamma is {gamma!r}, expected {GAMMA_MIN}-{GAMMA_MAX}", _RUN_FIX
        )
    return Result("ok", f"font smoothing on, gamma {gamma}")


def check_avalon(ctx: DoctorCtx) -> Result:
    """WPF hardware acceleration is off (software rendering is what works under Wine)."""
    return _registry_result(ctx, prefix_step.AVALON_KEY, "DisableHWAcceleration", 1, "WPF hardware acceleration off")


def check_appdefaults(ctx: DoctorCtx) -> Result:
    """Fork.exe runs as Windows 7 (keeps it away from WinRT APIs Wine lacks)."""
    return _registry_result(ctx, prefix_step.APPDEFAULTS_KEY, "Version", "win7", "Fork.exe runs as Windows 7")


LNK_MAGIC = b"L\0\0\0\x01\x14\x02\0"


def _mtime(path: Path) -> float | None:
    try:
        return os.lstat(path).st_mtime
    except OSError:
        return None


def _our_menu_entry(ctx: DoctorCtx, path: Path) -> bool:
    """True if the menubuilder entry ``path`` launches a program of *our* prefix.

    Wine writes ``Exec=env WINEPREFIX="<prefix>" wine ...``; entries of other
    prefixes (a real Fork in the default Wine prefix, say) are the user's and left alone.
    """
    text = _read_text(path, HIVE_HEAD_BYTES)
    if text is None:
        return False
    return f'WINEPREFIX="{ctx.paths.prefix}"' in text.replace("\\", "")


def _our_desktop_lnk(ctx: DoctorCtx, path: Path) -> bool:
    """True if ``path`` is a shell link written no earlier than our prefix was created.

    Before the ``shell_folders`` step replaces the prefix's ``Desktop`` link,
    the installer's ``Fork.lnk`` lands on the Linux desktop; a link older
    than our prefix cannot be ours.
    """
    head = _read_bytes(path, len(LNK_MAGIC))
    created = _mtime(ctx.paths.created_by_marker)
    modified = _mtime(path)
    return head == LNK_MAGIC and created is not None and modified is not None and modified >= created


def menubuilder_leftovers(ctx: DoctorCtx) -> list[Path]:
    """Menu entries Wine's menubuilder wrote for Fork in our prefix, and the installer's ``~/Desktop/Fork.lnk``."""
    found: list[Path] = []
    wine_apps = ctx.data_home / "applications" / "wine"
    if wine_apps.is_dir() and not wine_apps.is_symlink():
        for path in sorted(wine_apps.rglob("Fork*.desktop")):
            if not path.is_symlink() and _our_menu_entry(ctx, path):
                found.append(path)
    lnk = ctx.host_home / "Desktop" / "Fork.lnk"
    if _our_desktop_lnk(ctx, lnk):
        found.append(lnk)
    return found


def _menubuilder_override(ctx: DoctorCtx) -> object | None:
    return _reg(ctx, prefix_step.DLL_OVERRIDES_KEY, "winemenubuilder.exe")


def check_menubuilder(ctx: DoctorCtx) -> Result:
    """winemenubuilder is disabled and left nothing on the Linux desktop."""
    leftovers = menubuilder_leftovers(ctx)
    if _has_prefix(ctx) and _menubuilder_override(ctx) not in MENUBUILDER_OFF_VALUES:
        return Result(
            "fail",
            "winemenubuilder is not disabled in the prefix (it would add Wine entries to your menu)",
            _RUN_FIX,
        )
    if leftovers:
        return Result(
            "warn",
            "Wine-generated Fork shortcuts on the Linux side: " + ", ".join(str(path) for path in leftovers),
            "run 'fork-linux doctor --fix' to delete exactly these files",
        )
    if not _has_prefix(ctx):
        return Result("info", f"no Wine menu leftovers ({NO_PREFIX})")
    return Result("ok", "winemenubuilder disabled; no leftovers")


def fix_menubuilder(ctx: DoctorCtx) -> str:
    """Delete the leftovers found by :func:`menubuilder_leftovers`; re-apply the registry batch if needed."""
    done = []
    for path in menubuilder_leftovers(ctx):
        path.unlink()
        done.append(f"deleted {path}")
    if _has_prefix(ctx) and _menubuilder_override(ctx) not in MENUBUILDER_OFF_VALUES:
        bootstrap.run_steps(ctx.boot, only=["registry"])
        done.append("re-ran setup step registry")
    return "; ".join(done) or _NOTHING_TO_DO


def check_dpi(ctx: DoctorCtx) -> Result:
    """Wine's ``LogPixels`` matches the desktop scaling (or ``[display] dpi``)."""
    if not _has_prefix(ctx):
        return _skip(NO_PREFIX)
    wanted = display_step.dpi(ctx.boot)
    value = _reg(ctx, display_step.DESKTOP_KEY, display_step.LOG_PIXELS)
    if value == wanted:
        return Result("ok", f"{wanted} DPI")
    return Result("warn", f"LogPixels is {value!r}, expected {wanted}", _RUN_FIX)


# -- fork ------------------------------------------------------------------------------------------


def check_fork_installed(ctx: DoctorCtx) -> Result:
    """Fork.exe and ``sq.version`` are in place."""
    layout = ctx.layout
    if layout.is_installed():
        return Result("ok", f"Fork {layout.installed_version()} in {layout.current_dir}")
    return Result(
        "fail", "Fork is not installed in the prefix", "run 'fork-linux setup' (or 'fork-linux doctor --fix')"
    )


def _feed_assets(ctx: DoctorCtx) -> list[feeds.FeedAsset] | None:
    url = ctx.manifest.feed_url
    try:
        text = feeds.fetch_feed(url, ctx.paths.feeds_dir, offline=ctx.offline, validate=feeds.parse_releases_json)
        return feeds.parse_releases_json(text)
    except ForkLinuxError as exc:
        log.info("Fork's release feed is unavailable: %s", exc)
        return None


def check_fork_version(ctx: DoctorCtx) -> Result:
    """The installed version is known-good (not known-bad); with --network: is a newer one out?"""
    installed = ctx.layout.installed_version()
    if installed is None:
        return _skip(_NOT_INSTALLED)
    reason = ctx.manifest.known_bad_reason(installed)
    if reason is not None:
        return Result(
            "fail",
            f"Fork {installed} is known-bad: {reason}",
            "go back to a working version: 'fork-linux rollback' or 'fork-linux update --fork'",
        )
    entry = ctx.manifest.fork_version(installed)
    newer = ""
    if ctx.network:
        assets = _feed_assets(ctx)
        info = updates.check(ctx.manifest, ctx.layout, assets)
        if assets is None:
            newer = "; Fork's release feed is unavailable"
        elif info["update_available"]:
            newer = f"; Fork {info['latest']} is available"
    if entry is not None and entry.status == "known-good":
        return Result("ok", f"Fork {installed} is known-good{newer}")
    status = entry.status if entry is not None else "untested"
    return Result(
        "warn",
        f"Fork {installed} is {status} with this fork-linux release{newer}",
        f"the tested version is {ctx.manifest.fork_default}; report problems to the fork-linux project",
    )


def check_fork_integrity(ctx: DoctorCtx) -> Result:
    """The installed full package matches the sha256 pinned in the manifest."""
    installed = ctx.layout.installed_version()
    if installed is None:
        return _skip(_NOT_INSTALLED)
    entry = ctx.manifest.fork_version(installed)
    if entry is None:
        return Result("info", f"no pinned package sha256 for Fork {installed}")
    package = ctx.layout.packages_dir / f"Fork-{installed}-full.nupkg"
    if not os.path.lexists(package):
        return Result("warn", f"{package.name} is missing; cannot verify Fork {installed}")
    if _verify_nupkg(ctx.layout, installed, entry.full_nupkg_sha256):
        return Result("ok", f"{package.name} matches the manifest's sha256")
    if _self_updated(ctx, installed):
        # Spike S9: Velopack rebuilds the full package from a delta, which gives other zip bytes.
        return Result(
            "info",
            f"{package.name} was rebuilt by Fork's own updater from a delta package, so its bytes "
            "differ from the published package the manifest pins (Velopack checked the download "
            "against Fork's feed)",
        )
    return Result(
        "fail",
        f"{package.name} does not match the sha256 pinned in the manifest",
        "reinstall Fork: 'fork-linux update --fork' (or 'fork-linux setup --reset')",
    )


def _self_updated(ctx: DoctorCtx, installed: str) -> bool:
    """True when Fork updated itself to ``installed``: fork-linux installed (or restored) another version."""
    ours = ctx.state.get(FORK_VERSION)
    if not isinstance(ours, str) or not versions.is_valid(ours):
        return False
    return versions.Version(ours) != versions.Version(installed)


def _verify_nupkg(layout: ForkLayout, version: str, sha256: str) -> bool:
    """:func:`fork_install.verify_nupkg` (imported lazily: it pulls in the downloader)."""
    from . import fork_install

    return fork_install.verify_nupkg(layout, version, sha256)


# Written to fork.log by every real start of Fork (not by the installer's Velopack hook).
FORK_STARTED_MARK = "Start IPC server"


def check_gitinstance(ctx: DoctorCtx) -> Result:
    """Fork's bundled Git for Windows is unpacked."""
    layout = ctx.layout
    if not layout.is_installed():
        return _skip(_NOT_INSTALLED)
    found = layout.git_instances()
    if found:
        return Result("ok", f"bundled git {', '.join(found)}")
    # settings.json is seeded before the first start, so only Fork's own log tells whether it ran.
    if FORK_STARTED_MARK not in (_read_text(layout.fork_log, LOG_TAIL_BYTES, from_end=True) or ""):
        return Result("info", "Fork unpacks its bundled git on its first start (not started yet)")
    return Result(
        "warn",
        f"no bundled git in {layout.gitinstance_dir}",
        "start Fork once; if it still has no git, reinstall it with 'fork-linux update --fork'",
    )


def check_fork_settings(ctx: DoctorCtx) -> Result:
    """``settings.json`` is a regular file with valid JSON, a Guid and the enforced values."""
    path = ctx.layout.settings_file
    if not os.path.lexists(path):
        return Result("info", "settings.json does not exist yet (Fork writes it after its first-run dialog)")
    if path.is_symlink():
        return Result(
            "fail", f"{path} is a symbolic link", "replace it with a regular file (fork-linux settings restore)"
        )
    try:
        data = settings_mod.load(path)
    except ForkLinuxError as exc:
        return Result("fail", exc.message, exc.hint or "fork-linux settings restore")
    problems = []
    guid = data.get(settings_mod.GUID)
    if not isinstance(guid, str) or GUID_RE.fullmatch(guid) is None:
        problems.append("no valid Guid")
    for key in ctx.config.getlist("fork", "enforce_settings"):
        if key in settings_mod.ENFORCED_DEFAULTS and settings_mod.get(data, key) != settings_mod.ENFORCED_DEFAULTS[key]:
            problems.append(
                f"{key} is {settings_mod.get(data, key)!r}, expected {settings_mod.ENFORCED_DEFAULTS[key]!r}"
            )
    if problems:
        return Result("warn", "; ".join(problems), "close Fork and run 'fork-linux doctor --fix'")
    return Result("ok", f"{len(data)} settings, enforced values in place")


def check_pending_update(ctx: DoctorCtx) -> Result:
    """Velopack has staged an update: is there a snapshot to roll back to?"""
    layout = ctx.layout
    installed = layout.installed_version()
    if installed is None:
        return _skip(_NOT_INSTALLED)
    staged = sorted({version for version, _path in layout.staged_packages()}, key=versions.Version)
    if not staged:
        return Result("ok", "no staged Fork update")
    listed = ", ".join(staged)
    if ctx.config.get("fork", "update_policy") == "pinned":
        return Result(
            "warn",
            f"Fork is pinned to {installed} but Velopack staged {listed}",
            "the next 'fork' launch deletes them; 'fork-linux config set fork.update_policy auto' allows updates",
        )
    have = any(
        versions.is_valid(snap.fork_version) and versions.Version(snap.fork_version) == versions.Version(installed)
        for snap in snapshots.list_snapshots(ctx.paths)
    )
    if have:
        return Result("info", f"Fork will update to {listed}; a snapshot of {installed} exists for rollback")
    return Result(
        "warn",
        f"Fork will update to {listed} but there is no snapshot of {installed}",
        "take one now: 'fork-linux snapshot create'",
    )


def scan_fork_log(text: str) -> list[tuple[str, str]]:
    """``(what, hint)`` for every known crash signature in ``text``."""
    found = []
    for needles, alternatives, what, hint in LOG_SIGNATURES:
        if all(needle in text for needle in needles) and (not alternatives or any(alt in text for alt in alternatives)):
            found.append((what, hint))
    return found


def check_log_signatures(ctx: DoctorCtx) -> Result:
    """Known problems in the tail of Fork's ``fork.log``."""
    text = _read_text(ctx.layout.fork_log, LOG_TAIL_BYTES, from_end=True)
    if text is None:
        return Result("info", "no fork.log yet")
    found = scan_fork_log(text)
    if not found:
        return Result("ok", "no known problems in fork.log")
    return Result(
        "warn",
        "fork.log shows: " + "; ".join(what for what, _hint in found),
        "; ".join(hint for _what, hint in found),
    )


def _settings_data(ctx: DoctorCtx) -> dict[str, Any] | None:
    """Fork's ``settings.json`` (None when missing or unusable)."""
    try:
        data = settings_mod.load(ctx.layout.settings_file)
    except ForkLinuxError:
        return None
    return data or None


def _dead_tools(ctx: DoctorCtx, data: Mapping[str, Any]) -> list[str]:
    """Fork tool settings that name ``fl-launch.exe`` while no bridge daemon will run."""
    active = bridge.host_actions_active(ctx)
    dead = [key for key in fork_tools.TOOL_KEYS if fork_tools.is_dead(data.get(key), bridge_active=active)]
    if not active:
        dead += [key for key in fork_tools.TOOL_LIST_KEYS if fork_tools.merged_list(data.get(key), None) is not None]
    return dead


def check_fork_tools(ctx: DoctorCtx) -> Result:
    """Fork's terminal, diff and merge settings do not name a program that cannot run."""
    data = _settings_data(ctx)
    if data is None:
        return Result("info", "settings.json does not exist yet")
    dead = _dead_tools(ctx, data)
    if dead:
        return Result(
            "warn",
            f"{', '.join(dead)} run(s) fl-launch.exe, but the native-git bridge daemon is not running: "
            "the button does nothing",
            "close Fork and run 'fork-linux doctor --fix' (Fork's defaults come back; the terminal "
            "uses fork-linux-terminal)",
        )
    shell = data.get(fork_tools.SHELL_TOOL)
    if isinstance(shell, dict) and shell.get("Type") == "GitBash":
        return Result(
            "warn",
            "the terminal is Git Bash, which exits at once under Wine (msys mintty)",
            "pick 'Custom' in Fork's Preferences > Integration, or 'fork-linux settings unset ShellTool' "
            "with Fork closed so fork-linux sets its own terminal",
        )
    if fork_tools.is_ours(shell):
        return Result("ok", f"terminal: {shell.get('ApplicationPath')}")
    return Result("ok", "no unusable tool configured")


def fix_fork_tools(ctx: DoctorCtx) -> str:
    """Put the dead tool settings back to Fork's defaults (Fork must be closed)."""
    procs.require_closed(ctx.paths.prefix, proc_root=ctx.proc_root)
    data = _settings_data(ctx) or {}
    wanted: dict[str, Any] = {}
    for key in _dead_tools(ctx, data):
        if key in fork_tools.TOOL_LIST_KEYS:
            wanted[key] = fork_tools.merged_list(data.get(key), None)
        else:
            wanted[key] = fork_tools.DEFAULTS[key]
    terminal = fork_tools.wanted(bridge_active=False, fl_launch=False, pathmap=ctx.pathmap)[fork_tools.SHELL_TOOL]
    if fork_tools.SHELL_TOOL in wanted and terminal is not None:
        wanted[fork_tools.SHELL_TOOL] = terminal
    changed = settings_mod.apply(ctx.layout, wanted, backup_dir=settings_mod.default_backup_dir(ctx.paths))
    return f"reset {', '.join(changed)}" if changed else _NOTHING_TO_DO


def _home_win(ctx: DoctorCtx) -> str:
    return ctx.pathmap.unix_to_win(ctx.host_home)


def check_source_dirs(ctx: DoctorCtx) -> Result:
    """Fork's default folder for new clones is the Linux home, not a folder inside the prefix."""
    file = fork_data.path(ctx.layout.forkdata_dir)
    text = fork_data.read_text(file)
    if text is None:
        return Result("info", f"{file.name} does not exist yet (fork-linux creates it before Fork starts)")
    dirs = fork_data.source_dirs(text)
    if fork_data.is_default(dirs, ctx.user):
        return Result(
            "warn",
            f"Fork's source folder is {dirs[0] if dirs else ''} (inside the Wine prefix: clones land there "
            "and 'uninstall --purge' deletes them)",
            "close Fork and run 'fork-linux doctor --fix' to use your Linux home",
        )
    return Result("ok", f"source folder: {', '.join(dirs or []) or '(not set)'}")


def fix_source_dirs(ctx: DoctorCtx) -> str:
    """Point Fork's source folder at the Linux home (Fork must be closed)."""
    procs.require_closed(ctx.paths.prefix, proc_root=ctx.proc_root)
    home = _home_win(ctx)
    written = fork_data.ensure_source_dirs(
        ctx.layout.forkdata_dir, user=ctx.user, home_win=home, backup_dir=settings_mod.default_backup_dir(ctx.paths)
    )
    return f"source folder set to {home}" if written else _NOTHING_TO_DO


def check_integration(ctx: DoctorCtx) -> Result:
    """"Show in File Explorer" and "Open" are redirected to the Linux desktop; ``H:`` maps the home."""
    if not _has_prefix(ctx):
        return _skip(NO_PREFIX)
    expected = integration_step.integration_expected(ctx.boot)
    found = prefix_step.reg_values(ctx.boot, [(key, name) for key, name, _value in expected])
    wrong = [key for (key, _name, value), have in zip(expected, found, strict=True) if have != value]
    if wrong:
        return Result(
            "warn",
            f"{len(wrong)} of {len(expected)} file-manager / open redirects are missing",
            _RUN_FIX,
        )
    explorer = any("App Paths" in key for key, _name, _value in expected)
    drive = integration_step.home_drive(ctx.boot)
    parts = [f"{len(expected)} redirects in place"]
    if not explorer:
        parts.append("fork-linux-explorer is not installed: 'Show in File Explorer' opens Wine's explorer")
    parts.append(f"H: -> {os.readlink(drive)}" if drive.is_symlink() else "no H: drive")
    return Result("ok" if explorer else "info", "; ".join(parts))


# -- repositories ----------------------------------------------------------------------------------

BRIDGE_HINT = (
    "or enable the git bridge ('fork-linux git-bridge enable', experimental): Fork then runs them with Linux git"
)
BRIDGE_HANDLES = "the git bridge runs Fork's git with Linux git"


def _bridge_ready(ctx: DoctorCtx) -> bool:
    """The git bridge is on and the next Fork start will use it."""
    return bridge.check(ctx).ready


def _repo_reports(ctx: DoctorCtx) -> list[repos.RepoReport] | Result:
    """The scan of Fork's repositories (cached), or the Result explaining why there is none."""
    if ctx._repos is not None:
        return ctx._repos
    git = repos.Git(ctx.runner, ctx.env)
    if not git.available:
        return Result("info", "skipped: git is not installed on this computer")
    known = repos.known(ctx.layout, ctx.pathmap) if _has_prefix(ctx) else []
    if not known:
        return Result("info", "no repositories opened in Fork yet")
    ctx._repos = repos.scan(git, known, repos.overlay_text(ctx.paths, ctx.user))
    return ctx._repos


def _repo_result(ctx: DoctorCtx, attr: str, problem: str, hint: str, *, bridge_fixes: bool = False) -> Result:
    reports = _repo_reports(ctx)
    if isinstance(reports, Result):
        return reports
    found = repos.describe(reports, attr)
    if found and bridge_fixes and _bridge_ready(ctx):
        return Result(
            "ok", f"{len(reports)} repositories checked; {attr} in {len(found)} of them work: {BRIDGE_HANDLES}"
        )
    if found:
        return Result("warn", f"{len(found)} of {len(reports)} repositories {problem}: {'; '.join(found)}", hint)
    return Result("ok", f"{len(reports)} repositories checked")


def check_repo_hooks(ctx: DoctorCtx) -> Result:
    """Executable hooks: Fork's bundled git skips hooks that run programs, and reports success."""
    return _repo_result(
        ctx,
        "hooks",
        "have executable hooks that Fork's bundled git silently skips under Wine",
        "commit and push from a Linux terminal in these repositories; " + BRIDGE_HINT,
        bridge_fixes=True,
    )


def check_repo_symlinks(ctx: DoctorCtx) -> Result:
    """Tracked symbolic links: Fork always shows them as modified (Wine hides Unix links)."""
    return _repo_result(
        ctx,
        "symlinks",
        "track symbolic links that Fork always lists as modified (they block rebase)",
        "'fork-linux repo fix PATH' marks them skip-worktree after asking ('fork-linux repo undo PATH' "
        "reverts); with --fix, doctor asks too",
    )


def check_repo_submodules(ctx: DoctorCtx) -> Result:
    """Submodules: Fork's "update submodules" does nothing with its bundled git under Wine."""
    return _repo_result(
        ctx,
        "submodules",
        "use submodules, which Fork's bundled git cannot update under Wine",
        "run 'git submodule update --init --recursive' in a Linux terminal; " + BRIDGE_HINT,
        bridge_fixes=True,
    )


def check_repo_remotes(ctx: DoctorCtx) -> Result:
    """Remotes given as Linux paths that the git overlay does not rewrite for Fork's git."""
    return _repo_result(
        ctx,
        "linux_remotes",
        "have remotes as Linux paths Fork's git cannot open",
        "run 'fork-linux doctor --fix' (the git overlay rewrites /home, /mnt, /media, /srv, /opt, /tmp, /var "
        "and /run); for other folders use a file:///Z:/... URL",
    )


def check_repo_filemode(ctx: DoctorCtx) -> Result:
    """``core.filemode = true``: Fork then lists every executable file as modified."""
    return _repo_result(
        ctx,
        "filemode",
        "have core.filemode = true, so Fork shows executable files as modified",
        "'git config core.filemode false' in each (or 'fork-linux repo fix PATH', undo with "
        "'fork-linux repo undo PATH'); with --fix, doctor asks first",
    )


def _fix_repos(ctx: DoctorCtx, *, filemode: bool, symlinks: bool, what: str) -> str:
    """Apply :func:`repos.fix` to the scanned repositories after the user confirmed."""
    reports = _repo_reports(ctx)
    if isinstance(reports, Result):
        return _NOTHING_TO_DO
    targets = [r for r in reports if (filemode and r.filemode) or (symlinks and r.symlinks)]
    if not targets:
        return _NOTHING_TO_DO
    listed = "\n".join(f"  {report.path}" for report in targets)
    if not ctx.ui.confirm(
        "Change your repositories?",
        f"{what} in:\n{listed}\nUndo with 'fork-linux repo undo PATH'.",
        default=False,
    ):
        return "skipped (not confirmed)"
    git = repos.Git(ctx.runner, ctx.env)
    done: list[str] = []
    for report in targets:
        done += repos.fix(git, report, filemode=filemode, symlinks=symlinks)
    ctx._repos = None
    return "; ".join(done)


def fix_repo_filemode(ctx: DoctorCtx) -> str:
    """``core.filemode = false`` in the repositories that have it on (asks first)."""
    return _fix_repos(ctx, filemode=True, symlinks=False, what="Set core.filemode = false")


def fix_repo_symlinks(ctx: DoctorCtx) -> str:
    """``skip-worktree`` for tracked links (asks first)."""
    return _fix_repos(ctx, filemode=False, symlinks=True, what="Mark tracked symbolic links skip-worktree")


# -- ssh / git / bridge ----------------------------------------------------------------------------


def _ssh_wanted(ctx: DoctorCtx) -> str | None:
    """Why ssh sharing is not wanted, or None when it is."""
    sync = ctx.config.get("ssh", "sync")
    if sync == "off":
        return "ssh sharing is off ([ssh] sync = off)"
    if sync == "auto" and not (ctx.host_home / ".ssh").is_dir():
        return "no ~/.ssh to share"
    return None


def check_ssh_dir(ctx: DoctorCtx) -> Result:
    """The Wine user's ``.ssh`` is a private real directory with intact links."""
    why = _ssh_wanted(ctx)
    if why:
        return Result("info", why)
    if not _has_prefix(ctx):
        return _skip(NO_PREFIX)
    info = ssh_sync.status(ctx.paths, ctx.user, ctx.host_home)
    hint = "run 'fork-linux doctor --fix' (or 'fork-linux ssh sync')"
    if not info["exists"]:
        return Result("warn", f"{info['wine_dir']} does not exist", hint)
    if info["is_symlink"]:
        return Result("warn", f"{info['wine_dir']} is a symbolic link, not a private directory", hint)
    problems = []
    if not info["perms_ok"]:
        problems.append(
            f"mode {info['mode']}"
            + (f"; readable by others: {', '.join(info['insecure_files'])}" if info["insecure_files"] else "")
        )
    if info["broken_links"]:
        problems.append(f"broken links: {', '.join(info['broken_links'])}")
    if problems:
        return Result("warn", "; ".join(problems), hint)
    return Result("ok", f"{len(info['managed'])} file(s) shared in {info['wine_dir']}")


def check_ssh_config(ctx: DoctorCtx) -> Result:
    """The translated ``~/.ssh/config`` is current."""
    why = _ssh_wanted(ctx)
    if why:
        return Result("info", why)
    if not _has_prefix(ctx):
        return _skip(NO_PREFIX)
    state = ssh_sync.status(ctx.paths, ctx.user, ctx.host_home)["config"]
    if state == "none":
        return Result("ok", "no ~/.ssh/config to translate")
    if state == "current":
        return Result("ok", "translated ssh config is current")
    if state == "foreign":
        return Result("info", "the Wine user's ssh config was not generated by fork-linux; left alone")
    return Result(
        "warn", f"translated ssh config is {state}", "run 'fork-linux doctor --fix' (or 'fork-linux ssh sync')"
    )


def check_git_overlay(ctx: DoctorCtx) -> Result:
    """Fork's git sees a translated ``~/.gitconfig``; the env overrides are valid."""
    try:
        overrides = gitconfig.env_overrides(
            ctx.config, git_version=gitconfig.host_git_version(ctx.runner, ctx.env, ctx.paths.cache_dir)
        )
    except ForkLinuxError as exc:
        return Result("fail", exc.message, exc.hint)
    count = overrides.get("GIT_CONFIG_COUNT", "0")
    pairs = ", ".join(
        f"{overrides[f'GIT_CONFIG_KEY_{n}']}={overrides[f'GIT_CONFIG_VALUE_{n}']}" for n in range(int(count))
    )
    env_note = f"; env overrides: {pairs}" if pairs else "; no env overrides"
    if ctx.config.get("git", "config_overlay") == "off":
        return Result("info", "git config overlay is off" + env_note)
    if not _has_prefix(ctx):
        return _skip(NO_PREFIX)
    overlay = gitconfig.overlay_path(ctx.paths, ctx.user)
    gitconfig_file = ctx.paths.wine_user_dir(ctx.user) / ".gitconfig"
    text = _read_text(gitconfig_file, HIVE_HEAD_BYTES) or ""
    if not overlay.is_file() or gitconfig.MARK_BEGIN not in text:
        return Result("warn", "the translated git config is not in place" + env_note, _RUN_FIX)
    if gitconfig.MANAGED_HEADER.strip() not in repos.overlay_text(ctx.paths, ctx.user):
        return Result(
            "warn",
            "the git overlay predates the remote-path and credential fixes" + env_note,
            _RUN_FIX,
        )
    return Result("ok", f"{overlay}{env_note}")


def _version_key(name: str) -> tuple[tuple[int, int | str], ...]:
    """Sort key for ``gitInstance`` names such as ``2.50.1.windows.1`` (numbers compare numerically)."""
    return tuple((0, int(part)) if part.isdigit() else (1, part) for part in re.split(r"[.-]", name))


def _git(ctx: DoctorCtx, info: WineInfo, env: Mapping[str, str], git_win: str, args: Sequence[str]) -> str:
    """Run Fork's bundled git under Wine; its stdout, or :class:`ForkLinuxError` on failure."""
    completed = winecmd.run(ctx.runner, env, info, [git_win, *args], timeout=GIT_TIMEOUT)
    if not completed.ok:
        detail = tail(completed.stderr or completed.stdout, 3)
        raise ForkLinuxError(
            f"git {' '.join(args)} exited with {completed.returncode}" + (f": {detail}" if detail else "")
        )
    return completed.stdout


def check_git_selftest(ctx: DoctorCtx) -> Result:
    """Fork's bundled git runs under Wine and a fresh repository is clean (deep)."""
    info = ctx.wine()
    if info is None:
        return _skip(_NO_WINE)
    instances = ctx.layout.git_instances()
    if not instances:
        return _skip("Fork's bundled git is not unpacked yet")
    pathmap = ctx.pathmap
    git_exe = ctx.layout.gitinstance_dir / max(instances, key=_version_key) / "cmd" / "git.exe"
    git_win = pathmap.unix_to_win(git_exe)
    env = winecmd.build_env(ctx.paths, info, user=ctx.user, base_env=ctx.env, extra=gitconfig.env_overrides(ctx.config))
    scratch = fsutil.ensure_dir(ctx.paths.cache_dir)
    repo = Path(tempfile.mkdtemp(prefix="doctor-git-", dir=scratch))
    try:
        repo_win = pathmap.unix_to_win(repo)
        version = _git(ctx, info, env, git_win, ["--version"]).strip()
        _git(ctx, info, env, git_win, ["init", "-q", repo_win])
        script = repo / "run.sh"
        script.write_text("#!/bin/sh\necho ok\n", encoding="utf-8")
        script.chmod(0o755)
        (repo / "text.txt").write_text("line one\nline two\n", encoding="utf-8")
        identity = ["-c", "user.name=fork-linux doctor", "-c", "user.email=doctor@localhost"]
        _git(ctx, info, env, git_win, ["-C", repo_win, "add", "-A"])
        _git(ctx, info, env, git_win, [*identity, "-C", repo_win, "commit", "-q", "-m", "doctor"])
        status = _git(ctx, info, env, git_win, ["-C", repo_win, "status", "--porcelain"]).strip()
    except ForkLinuxError as exc:
        return Result(
            "fail",
            f"Fork's bundled git does not work under Wine: {exc.message}",
            "run 'fork-linux logs --wine' and 'fork-linux doctor --check wine host'",
        )
    finally:
        fsutil.safe_rmtree(repo)
    if status:
        return Result(
            "warn",
            f"{version or 'git'}: a fresh repository shows changes ({status.splitlines()[0]})",
            "keep core.filemode=false and core.autocrlf=false in git.env_overrides; " + HOOK_LIMITATION,
        )
    level = "ok" if info.staging else "warn"
    return Result(level, f"{version or 'git'} works; a fresh repository is clean", HOOK_LIMITATION)


def check_bridge(ctx: DoctorCtx) -> Result:
    """The experimental native-git bridge: on, built, installed, a usable Linux git; the running daemon."""
    info = bridge.status(ctx, launcher.read_session(ctx.paths))
    if info["enabled"] and not info["available"]:
        return Result("fail", info["reason"], "run 'fork-linux git-bridge disable'")
    if info["enabled"] and not info["ready"]:
        return Result(
            "warn",
            f"git bridge enabled but Fork falls back to its bundled git: {info['reason']}",
            "run 'fork-linux setup' to install the shims, or 'fork-linux git-bridge disable'",
        )
    if info["enabled"]:
        daemon = info["daemon"]
        running = f"daemon running (pid {daemon['pid']}, port {daemon['port']})" if daemon else "starts with Fork"
        mode = "record mode: Fork's bundled git" if info["mode"] == bridge.MODE_RECORD else "Linux git"
        detail = f"native-git bridge enabled (experimental): {mode} {info['git_version']}; {running}"
        if not info["git_recommended"]:
            wanted = ".".join(str(part) for part in bridge.RECOMMENDED_GIT)
            return Result("warn", detail, f"git {wanted} or newer is recommended for Fork's features")
        return Result("ok", detail)
    if info["reason"] and info["available"]:
        return Result("warn", info["reason"], "run 'fork-linux git-bridge disable' to reset the setting")
    return Result("info", "native-git bridge off; Fork uses its bundled git")


# -- desktop ---------------------------------------------------------------------------------------


def _icons(ctx: DoctorCtx) -> list[Path]:
    recorded = ctx.state.get(integration_step.ICONS_KEY)
    if isinstance(recorded, list) and recorded:
        return [Path(item) for item in recorded if isinstance(item, str) and Path(item).is_file()]
    hicolor = ctx.data_home / "icons" / "hicolor"
    return sorted(hicolor.glob(f"*/apps/{desktop_integration.APP_ID}.png"))


def check_desktop_entry(ctx: DoctorCtx) -> Result:
    """A menu entry (packaged or personal), Fork's icon, and a valid desktop file."""
    info = desktop_integration.status(ctx.paths, ctx.env)
    entry = info["system_desktop"] or (info["menu_file"] if Path(info["menu_file"]).is_file() else None)
    if entry is None:
        return Result(
            "fail", "no menu entry is installed", "run 'fork-linux doctor --fix' (or 'fork-linux desktop install')"
        )
    notes = [f"menu entry {entry}"]
    level = "ok"
    hint = ""
    validator = ctx.runner.which("desktop-file-validate", sandbox.clean_env(ctx.env).get("PATH"))
    if validator is not None:
        result = ctx.runner.run([validator, str(entry)], env=sandbox.clean_env(ctx.env), timeout=VALIDATE_TIMEOUT)
        if not result.ok or result.stdout.strip():
            level = "warn"
            notes.append("desktop-file-validate: " + tail(result.stdout or result.stderr, 3).replace("\n", " "))
            hint = "run 'fork-linux desktop install' to rewrite it"
    layout_installed = ctx.layout.is_installed() if not ctx.user_error else False
    if layout_installed and not _icons(ctx):
        level = "warn"
        notes.append("Fork's icon is not installed")
        hint = _RUN_FIX
    return Result(level, "; ".join(notes), hint)


def fix_desktop(ctx: DoctorCtx) -> str:
    """Re-run the ``desktop_entry`` and ``icon`` steps."""
    bootstrap.run_steps(ctx.boot, only=["icon", "desktop_entry"])
    return "re-ran setup steps icon, desktop_entry"


def _ours(path: str) -> bool:
    """True if ``path`` is our ``fork`` launcher (or links to it)."""
    real = os.path.realpath(path)
    root = resources.install_root() / "bin"
    if real in {os.path.realpath(root / name) for name in ("fork", "fork-linux")}:
        return True
    if os.path.basename(real) in ("fork-linux", "fork-linux.in"):
        return True
    head = _read_bytes(Path(real), CLI_PROBE_BYTES)
    return head is not None and any(marker in head for marker in OUR_MARKERS)


def check_cli(ctx: DoctorCtx) -> Result:
    """``fork`` on PATH is our launcher."""
    path = sandbox.clean_env(ctx.env).get("PATH")
    found = ctx.runner.which("fork", path)
    if found is None:
        return Result(
            "info",
            "'fork' is not on PATH (use 'fork-linux')",
            "add it with 'fork-linux desktop install --cli-alias'",
        )
    if _ours(found):
        return Result("ok", f"'fork' is {found}")
    return Result(
        "warn",
        f"'fork' on PATH is another program: {found}",
        "use 'fork-linux run', or put fork-linux's 'fork' earlier on PATH",
    )


def check_license(ctx: DoctorCtx) -> Result:
    """Reminder: Fork license activations belong to this prefix."""
    guid = _reg(ctx, MACHINE_GUID_KEY, "MachineGuid") if _has_prefix(ctx) else None
    machine = f" (prefix MachineGuid {guid})" if isinstance(guid, str) and guid else ""
    return Result(
        "info",
        f"Fork license activations are tied to this Wine prefix{machine}",
        "deactivate your license in Fork before 'fork-linux setup --reset' or 'fork-linux uninstall --purge'",
    )


# -- network ---------------------------------------------------------------------------------------


def network_targets(ctx: DoctorCtx) -> list[tuple[str, str]]:
    """``(what, url)`` of the hosts setup downloads from."""
    return [
        ("Fork installer (cdn.fork.dev)", ctx.manifest.installer_url(ctx.manifest.fork_default)),
        ("Fork release feed (git-fork.com)", ctx.manifest.feed_url),
        ("managed Wine (github.com)", ctx.manifest.wine_default.url),
    ]


def check_network(ctx: DoctorCtx) -> Result:
    """The download hosts answer a HEAD request."""
    if ctx.offline:
        return _skip("--offline")
    failed = [what for what, url in network_targets(ctx) if not _head_ok(url)]
    if failed:
        return Result("warn", f"unreachable: {', '.join(failed)}", "check your network connection and proxy settings")
    return Result("ok", "cdn.fork.dev, git-fork.com and github.com are reachable")


CHECKS: list[Check] = [
    Check("env.arch", "CPU architecture", "env", check_arch),
    Check("env.python", "Python version", "env", check_python),
    Check("env.user", "User account", "env", check_user),
    Check("env.display", "Display session", "env", check_display),
    Check("env.disk", "Free disk space", "env", check_disk),
    Check("host.tools", "Host tools", "host", check_tools),
    Check("host.libs", "Host libraries", "host", check_libs),
    Check("host.fonts", "Host fonts", "host", check_fonts),
    Check("wine.present", "Wine", "wine", check_wine_present, fix_steps=("wine_runtime",)),
    Check("wine.version", "Wine version", "wine", check_wine_version, fix_steps=("wine_runtime",)),
    Check("wine.staging", "Wine staging", "wine", check_wine_staging),
    Check("wine.wineserver", "wineserver", "wine", check_wineserver),
    Check("prefix.exists", "Wine prefix", "prefix", check_prefix, fix_steps=("prefix_init",)),
    Check("prefix.dotnet", ".NET Framework", "prefix", check_dotnet, fix_steps=("dotnet",)),
    Check("prefix.corefonts", "Core fonts", "prefix", check_corefonts, fix_steps=("fonts",)),
    Check("prefix.ui_font", "Interface font", "prefix", check_ui_font, fix_steps=("ui_font",)),
    Check(
        "prefix.font_replacements",
        "Font replacements",
        "prefix",
        check_font_replacements,
        fix_steps=("font_replacements",),
    ),
    Check(
        "prefix.font_smoothing", "Font smoothing", "prefix", check_font_smoothing, fix_steps=("registry",)
    ),
    Check("prefix.avalon", "WPF rendering", "prefix", check_avalon, fix_steps=("registry",)),
    Check("prefix.appdefaults", "Fork.exe Windows version", "prefix", check_appdefaults, fix_steps=("registry",)),
    Check("prefix.menubuilder", "Wine menu entries", "prefix", check_menubuilder, fixer=fix_menubuilder),
    Check("prefix.dpi", "DPI", "prefix", check_dpi, fix_steps=("display_dpi",)),
    Check(
        "prefix.integration", "File manager and open redirects", "prefix", check_integration,
        fix_steps=("host_integration",),
    ),
    Check(
        "fork.installed", "Fork installed", "fork", check_fork_installed, fix_steps=("fork_download", "fork_install")
    ),
    Check("fork.version", "Fork version", "fork", check_fork_version),
    Check("fork.integrity", "Fork package integrity", "fork", check_fork_integrity),
    Check("fork.gitinstance", "Fork's bundled git", "fork", check_gitinstance),
    Check("fork.settings", "Fork settings", "fork", check_fork_settings, fix_steps=("fork_settings",)),
    Check("fork.pending_update", "Pending Fork update", "fork", check_pending_update),
    Check("fork.log_signatures", "Fork log", "fork", check_log_signatures),
    Check("fork.tools", "Fork terminal / diff / merge", "fork", check_fork_tools, fixer=fix_fork_tools),
    Check("fork.source_dirs", "Fork source folder", "fork", check_source_dirs, fixer=fix_source_dirs),
    Check("repo.hooks", "Repository hooks", "repo", check_repo_hooks),
    Check("repo.symlinks", "Repository symlinks", "repo", check_repo_symlinks, fixer=fix_repo_symlinks),
    Check("repo.submodules", "Repository submodules", "repo", check_repo_submodules),
    Check("repo.remotes", "Repository remotes", "repo", check_repo_remotes, fix_steps=("git_overlay",)),
    Check("repo.filemode", "Repository core.filemode", "repo", check_repo_filemode, fixer=fix_repo_filemode),
    Check("ssh.dir", "ssh keys", "ssh", check_ssh_dir, fix_steps=("ssh_sync",)),
    Check("ssh.config", "ssh config", "ssh", check_ssh_config, fix_steps=("ssh_sync",)),
    Check("git.overlay", "git config overlay", "git", check_git_overlay, fix_steps=("git_overlay",)),
    Check("git.selftest", "Bundled git self-test", "git", check_git_selftest, deep=True),
    Check("bridge.status", "Native-git bridge", "bridge", check_bridge),
    Check("desktop.entry", "Menu entry and icon", "desktop", check_desktop_entry, fixer=fix_desktop),
    Check("desktop.cli", "fork command", "desktop", check_cli),
    Check("license.reminder", "Fork license", "license", check_license),
    Check("network.reach", "Download hosts", "network", check_network, network=True),
]


def get(check_id: str) -> Check:
    """The check called ``check_id`` (``KeyError`` if there is none)."""
    for check in CHECKS:
        if check.id == check_id:
            return check
    raise KeyError(check_id)


def select(
    checks: Sequence[Check], *, deep: bool = False, network: bool = False, only: Iterable[str] | None = None
) -> list[Check]:
    """The checks to run: ``only`` (ids or group names; :class:`UsageError` for unknown ones), else by flags."""
    wanted = list(only or [])
    if wanted:
        names = {check.id for check in checks} | {check.group for check in checks}
        unknown = [name for name in wanted if name not in names]
        if unknown:
            raise UsageError(
                f"unknown doctor check(s): {', '.join(unknown)}",
                hint="checks: " + ", ".join(check.id for check in checks),
            )
        return [check for check in checks if check.id in wanted or check.group in wanted]
    return [check for check in checks if (deep or not check.deep) and (network or not check.network)]


def run_check(ctx: DoctorCtx, check: Check) -> Result:
    """Run one check; a check that raises is reported as failed (never propagated)."""
    try:
        result = check.run(ctx)
    except ForkLinuxError as exc:
        return Result("fail", exc.message, exc.hint)
    except Exception as exc:
        log.debug("doctor check %s raised", check.id, exc_info=True)
        return Result("fail", f"the check could not run: {type(exc).__name__}: {exc}")
    if result.status not in STATUSES:
        return Result("fail", f"the check returned an invalid status {result.status!r}")
    return result


def run_checks(
    ctx: DoctorCtx,
    *,
    deep: bool = False,
    network: bool = False,
    only: Iterable[str] | None = None,
    checks: Sequence[Check] | None = None,
) -> list[tuple[Check, Result]]:
    """Run the selected checks in order."""
    ctx.deep = deep
    ctx.network = network
    chosen = select(CHECKS if checks is None else checks, deep=deep, network=network, only=only)
    return [(check, run_check(ctx, check)) for check in chosen]


def summary(results: Iterable[tuple[Check, Result]]) -> dict[str, int]:
    """How many results have each status."""
    counts = dict.fromkeys(("ok", "warn", "fail", "info"), 0)
    for _check, result in results:
        counts[result.status] += 1
    return counts


def report_json(results: Sequence[tuple[Check, Result]]) -> dict[str, Any]:
    """The machine-readable report (schema 1)."""
    return {
        "schema": SCHEMA,
        "summary": summary(results),
        "checks": [
            {
                "id": check.id,
                "title": check.title,
                "group": check.group,
                "status": result.status,
                "detail": result.detail,
                "hint": result.hint,
                "fixable": check.fixable,
            }
            for check, result in results
        ],
    }


def failed(results: Iterable[tuple[Check, Result]]) -> list[str]:
    """Ids of the failing checks."""
    return [check.id for check, result in results if result.status == "fail"]


def _run_fixers(ctx: DoctorCtx, checks: Sequence[Check], report: FixReport) -> list[str]:
    """Run the custom fixers of ``checks``; return the setup steps the other checks want re-run."""
    step_ids: list[str] = []
    for check in checks:
        if check.fixer is not None:
            try:
                report.actions.append(f"{check.id}: {check.fixer(ctx)}")
            except (ForkLinuxError, OSError) as exc:
                report.errors.append(f"{check.id}: {exc}")
            continue
        for step_id in check.fix_steps:
            if step_id not in step_ids:
                step_ids.append(step_id)
    return step_ids


def _rerun_steps(ctx: DoctorCtx, step_ids: list[str], report: FixReport) -> None:
    """Re-run ``step_ids`` in setup order, recording the outcome in ``report``."""
    ordered = [step_id for step_id in steps.STEP_IDS if step_id in step_ids]
    try:
        ran = bootstrap.run_steps(ctx.boot, only=ordered)
        report.actions.append("re-ran setup steps: " + ", ".join(step for step in ran if step in ordered))
    except ForkLinuxError as exc:
        report.errors.append(exc.message + (f" ({exc.hint})" if exc.hint else ""))


def fix(ctx: DoctorCtx, results: Sequence[tuple[Check, Result]]) -> FixReport:
    """Repair the fixable checks that warn or fail, then check them again.

    Custom fixers run first; the setup steps of every other fixable check are
    re-run together, in setup order, through :func:`bootstrap.run_steps`
    (``only=...``: the setup lock is taken and steps that need Fork closed
    refuse to run while it is open). Failures are collected, not raised.
    """
    report = FixReport(results=list(results))
    targets = [(check, result) for check, result in results if result.status in NOT_OK and check.fixable]
    step_ids = _run_fixers(ctx, [check for check, _result in targets], report)
    if step_ids:
        _rerun_steps(ctx, step_ids, report)
    if targets:
        ctx.reset()
        fixed_ids = {check.id for check, _result in targets}
        report.results = [
            (check, run_check(ctx, check) if check.id in fixed_ids else result) for check, result in results
        ]
    return report
