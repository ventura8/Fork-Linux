"""Steps ``prefix_init``, ``shell_folders``, ``registry`` and ``winver``: our own Wine prefix.

The prefix is created by ``wineboot --init`` with Mono and Gecko disabled
(Fork needs the real .NET Framework, installed by the next steps), Wine's
``Desktop`` link to the Linux desktop is replaced by a real directory, and one
registry batch applies the settings Fork needs under Wine (spike S1).
"""

from __future__ import annotations

import logging
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .. import fsutil, helpers, procs, registry, wine_provider, winecmd, winetricks
from ..bootstrap import Ctx, Step
from ..errors import ForkLinuxError, SetupFailed
from ..registry import RegBatch
from . import runtime

log = logging.getLogger(__name__)

WINEBOOT_TIMEOUT = 600
BOOT_DLL_OVERRIDES = "mscoree,mshtml="
HIVES = ("system.reg", "user.reg")
ARCH_LINE = "#arch=win64"
HIVE_HEAD_BYTES = 64 * 1024

DLL_OVERRIDES_KEY = r"HKCU\Software\Wine\DllOverrides"
AVALON_KEY = r"HKCU\Software\Microsoft\Avalon.Graphics"
APPDEFAULTS_KEY = r"HKCU\Software\Wine\AppDefaults\Fork.exe"
WINEDBG_KEY = r"HKCU\Software\Wine\WineDbg"
DIRECT3D_KEY = r"HKCU\Software\Wine\Direct3D"
DESKTOP_KEY = r"HKCU\Control Panel\Desktop"
WINE_KEY = r"HKCU\Software\Wine"
DEFAULT_RENDERER = "default"
# Windows' default ClearType gamma; Wine reads it for GDI text (0, wine.inf's value, means "unset").
FONT_SMOOTHING_GAMMA = 1400
# What a full disk or a full disk quota makes winetricks, Wine and the installers print (strerror).
DISK_FULL_MARKERS = ("No space left on device", "Disk quota exceeded")
DISK_FULL_TAIL_BYTES = 256 * 1024
# Less free space than this after a failure: the disk filled up (whatever language the messages were in).
DISK_LOW_BYTES = 64 * 1024 * 1024


class DiskFull(SetupFailed):
    """winetricks failed because the disk, or the user's disk quota, is full: retrying cannot help."""


# -- shared helpers (also used by the dotnet, fonts and display steps) -----------------


def _hive_parts(key: str) -> tuple[str, str]:
    """``HKCU\\Software\\Wine`` -> ``("HKCU", "Software\\Wine")``."""
    hive, _sep, rest = key.partition("\\")
    return hive, rest


def reg_value(ctx: Ctx, key: str, name: str) -> object | None:
    """A value read straight from the prefix's hives (None when absent or unreadable)."""
    hive, rest = _hive_parts(key)
    try:
        return registry.query(ctx.paths.prefix, hive, rest, name)
    except (ForkLinuxError, OSError, ValueError):
        return None


def reg_values(ctx: Ctx, wanted: Sequence[tuple[str, str]]) -> list[object | None]:
    """:func:`reg_value` for every ``(key, name)``, each hive file read once (all None when unreadable)."""
    try:
        return registry.query_many(ctx.paths.prefix, [(*_hive_parts(key), name) for key, name in wanted])
    except (ForkLinuxError, OSError, ValueError):
        return [None] * len(wanted)


def reg_matches(ctx: Ctx, expected: Sequence[tuple[str, str, object]]) -> bool:
    """True if every ``(key, name, value)`` is in the prefix's registry (names case-insensitive)."""
    found = reg_values(ctx, [(key, name) for key, name, _value in expected])
    return all(value == want for value, (_key, _name, want) in zip(found, expected, strict=True))


def import_batch(ctx: Ctx, batch: RegBatch, name: str) -> None:
    """Import ``batch`` with ``wine regedit`` (a failure raises :class:`ForkLinuxError`)."""
    winecmd.import_reg(
        ctx.runner, ctx.wine_env(), ctx.wine(), batch, ctx.paths, ctx.user, name=name, log_file=ctx.log_file
    )


def run_verbs(ctx: Ctx, step_id: str, verbs: Sequence[str]) -> None:
    """``winetricks -q <verbs>`` with its cache in ours; a non-zero exit raises :class:`SetupFailed` for ``step_id``.

    Downloads already in winetricks' default cache are reused first (that cache is only read). A failure
    that its output blames on a full disk or quota (or with almost no space left) raises :class:`DiskFull`:
    the preflight sees free space only, not per-user quotas.
    """
    cache = ctx.paths.winetricks_cache_dir
    winetricks.seed_cache(cache, ctx.paths.legacy_winetricks_cache_dir)
    stop_leftovers(ctx)
    offset = _log_size(ctx.log_file)
    result = winetricks.run_verbs(
        ctx.runner, runtime.winetricks_path(ctx), ctx.wine_env(), list(verbs), log_file=ctx.log_file, cache=cache
    )
    if result.ok:
        return
    failed = f"winetricks {' '.join(verbs)} exited with code {result.returncode}"
    if _disk_full_since(ctx.log_file, offset) or fsutil.disk_free(ctx.paths.prefix) < DISK_LOW_BYTES:
        raise DiskFull(
            step_id,
            f"{failed}: the disk is full",
            hint=f"the disk holding {ctx.paths.prefix} (or your disk quota there) is full: free some space, "
            "then run 'fork-linux setup' again (it resumes)",
        )
    raise SetupFailed(step_id, failed, hint="check your network connection; winetricks' output is in the setup log")


def stop_leftovers(ctx: Ctx) -> None:
    """End what still runs in the prefix before winetricks or a rebuild changes it.

    The steps that call this need Fork closed, so anything left is an interrupted setup's (a
    winetricks script that keeps starting installers would break the next install, and made
    deleting the prefix fail with "Directory not empty"): ``wineserver -k``, then a signal.
    """
    if not procs.prefix_pids(ctx.paths.prefix):
        return
    winecmd.wineserver(ctx.runner, ctx.wine_env(), ctx.wine(), "-k")
    stopped = procs.stop_prefix(ctx.paths.prefix)
    if stopped:
        log.warning("stopped %d process(es) an interrupted setup left in %s", len(stopped), ctx.paths.prefix)


def _log_size(log_file: Path | None) -> int:
    """The size of ``log_file`` now (0 when there is none yet)."""
    try:
        return 0 if log_file is None else log_file.stat().st_size
    except OSError:
        return 0


def _disk_full_since(log_file: Path | None, offset: int) -> bool:
    """True when what was appended to ``log_file`` after ``offset`` says the disk or the quota is full."""
    if log_file is None:
        return False
    try:
        with open(log_file, "rb") as handle:
            end = handle.seek(0, os.SEEK_END)
            handle.seek(max(offset, end - DISK_FULL_TAIL_BYTES))
            text = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return False
    return any(marker in text for marker in DISK_FULL_MARKERS)


# -- prefix_init -------------------------------------------------------------------------


def prefix_inputs(ctx: Ctx) -> dict[str, Any]:
    """The Wine the prefix was booted with."""
    return {"wine": runtime.wine_inputs(ctx)}


def stop_other_wineservers(ctx: Ctx, info: winecmd.WineInfo) -> list[Path]:
    """Stop a wineserver of another managed Wine build still serving the prefix; return the binaries used.

    After a Wine pin change (an app upgrade, ``config set wine.build``) Wine's services
    can keep the previous build's wineserver alive; every client of the new build would
    then fail with a protocol version mismatch. Only that build's own ``wineserver -k``
    can stop it, and only the managed builds in our data directory are trusted to run.
    """
    if not ctx.paths.prefix.is_dir():
        return []
    current = info.root.resolve()
    trusted = {
        wine_provider.managed_root(ctx.paths, build).resolve(): build
        for build in wine_provider.installed_builds(ctx.paths)
    }
    stopped = []
    for exe in procs.wineserver_exes(ctx.paths.prefix, proc_root=ctx.proc_root):
        root = exe.parent.parent
        if root == current or root not in trusted or exe != root / "bin" / exe.name:
            continue
        old = winecmd.WineInfo(
            provider="managed",
            build_id=trusted[root],
            root=root,
            wine=root / "bin" / "wine",
            wineserver=exe,
            version="",
            staging=False,
            wow64=False,
        )
        env = winecmd.build_env(ctx.paths, old, user=ctx.user, base_env=ctx.env)
        winecmd.wineserver(ctx.runner, env, old, "-k")
        stopped.append(exe)
    return stopped


def run_prefix_init(ctx: Ctx) -> None:
    """``wineboot --init`` (Mono/Gecko off), then wait for the server to write the hives.

    A wineserver of the previously used Wine build is stopped first (:func:`stop_other_wineservers`).
    """
    env = ctx.wine_env(dll_overrides=BOOT_DLL_OVERRIDES)
    info = ctx.wine()
    stop_other_wineservers(ctx, info)
    result = winecmd.run(
        ctx.runner, env, info, ["wineboot", "--init"], timeout=WINEBOOT_TIMEOUT, log_file=ctx.log_file
    )
    if not result.ok:
        raise SetupFailed(
            "prefix_init",
            f"wineboot exited with code {result.returncode}",
            hint="run 'fork-linux doctor' to check the Wine runtime and host libraries",
        )
    if not winecmd.settle(ctx.runner, env, info):
        ctx.ui.warn(winecmd.SETTLE_WARNING)
    os.chmod(ctx.paths.prefix, 0o700)


def _hive_ok(path: os.PathLike[str]) -> bool:
    """True if the hive exists and declares a 64-bit prefix near its top."""
    try:
        with open(path, "rb") as handle:
            head = handle.read(HIVE_HEAD_BYTES).decode("utf-8", errors="replace")
    except OSError:
        return False
    return any(line.strip() == ARCH_LINE for line in head.splitlines())


def verify_prefix_init(ctx: Ctx) -> bool:
    """``system.reg`` and ``user.reg`` exist and say ``#arch=win64``."""
    return all(_hive_ok(ctx.paths.prefix / name) for name in HIVES)


# -- shell_folders -------------------------------------------------------------------------


def run_shell_folders(ctx: Ctx) -> None:
    """Replace the ``Desktop`` link to the Linux desktop with a real directory (the link only)."""
    desktop = ctx.paths.wine_user_dir(ctx.user) / "Desktop"
    if desktop.is_symlink():
        desktop.unlink()
    elif os.path.lexists(desktop) and not desktop.is_dir():
        raise SetupFailed("shell_folders", f"{desktop} exists and is not a directory")
    desktop.mkdir(parents=True, exist_ok=True)


def verify_shell_folders(ctx: Ctx) -> bool:
    """``Desktop`` is a real directory."""
    desktop = ctx.paths.wine_user_dir(ctx.user) / "Desktop"
    return desktop.is_dir() and not desktop.is_symlink()


# -- registry --------------------------------------------------------------------------------


def _renderer(ctx: Ctx) -> str:
    return ctx.config.get("wine", "renderer")


WINEBROWSER_KEY = r"HKCU\Software\Wine\WineBrowser"
URL_HANDLER = "fork-linux-open-url"


def url_handler() -> Path | None:
    """Our link opener (escapes Wine's seccomp/NoNewPrivs via the desktop portal), if installed."""
    handler = helpers.path(URL_HANDLER)
    return handler if handler.is_file() and os.access(handler, os.X_OK) else None


def registry_inputs(ctx: Ctx) -> dict[str, Any]:
    """The Direct3D renderer and link handler (the rest is fixed by the step revision)."""
    handler = url_handler()
    return {"renderer": _renderer(ctx), "url_handler": str(handler) if handler else ""}


def registry_expected(ctx: Ctx) -> list[tuple[str, str, object]]:
    """``(key, name, value)`` the registry step sets."""
    expected: list[tuple[str, str, object]] = [
        (DLL_OVERRIDES_KEY, "winemenubuilder.exe", ""),
        (AVALON_KEY, "DisableHWAcceleration", 1),
        (APPDEFAULTS_KEY, "Version", "win7"),
        (WINEDBG_KEY, "ShowCrashDialog", 0),
        (DESKTOP_KEY, "FontSmoothing", "2"),
        (DESKTOP_KEY, "FontSmoothingType", 2),
        (DESKTOP_KEY, "FontSmoothingOrientation", 1),
        (DESKTOP_KEY, "FontSmoothingGamma", FONT_SMOOTHING_GAMMA),
    ]
    renderer = _renderer(ctx)
    if renderer != DEFAULT_RENDERER:
        expected.append((DIRECT3D_KEY, "renderer", renderer))
    handler = url_handler()
    if handler is not None:
        # winebrowser tries these comma-separated commands in order for links and mailto:.
        expected.append((WINEBROWSER_KEY, "Browsers", f"{handler},xdg-open"))
        expected.append((WINEBROWSER_KEY, "Mailers", f"{handler},xdg-email"))
    return expected


def registry_batch(ctx: Ctx) -> RegBatch:
    """The registry batch for :func:`registry_expected`."""
    batch = RegBatch()
    for key, name, value in registry_expected(ctx):
        if isinstance(value, int):
            batch.set_dword(key, name, value)
        else:
            batch.set_sz(key, name, str(value))
    return batch


def run_registry(ctx: Ctx) -> None:
    """Import the batch: no winemenubuilder, software WPF rendering, win7 for Fork.exe, ..."""
    import_batch(ctx, registry_batch(ctx), "registry")


def verify_registry(ctx: Ctx) -> bool:
    """Every value of the batch is in ``user.reg``."""
    return reg_matches(ctx, registry_expected(ctx))


# -- winver ------------------------------------------------------------------------------------


def run_winver(ctx: Ctx) -> None:
    """``winetricks -q win10``: Windows 10 for the whole prefix (Fork.exe keeps win7)."""
    run_verbs(ctx, "winver", ["win10"])


def verify_winver(ctx: Ctx) -> bool:
    """Best effort: the global Windows version, when Wine recorded one, is win10."""
    return reg_value(ctx, WINE_KEY, "Version") in (None, "win10")


PREFIX_INIT = Step(
    id="prefix_init",
    title="Creating the Wine prefix",
    rev=1,
    weight=10,
    run=run_prefix_init,
    verify=verify_prefix_init,
    inputs=prefix_inputs,
)

SHELL_FOLDERS = Step(
    id="shell_folders",
    title="Configuring Windows folders",
    rev=1,
    weight=1,
    run=run_shell_folders,
    verify=verify_shell_folders,
)

REGISTRY = Step(
    id="registry",
    title="Applying Wine settings for Fork",
    rev=3,
    weight=3,
    run=run_registry,
    verify=verify_registry,
    inputs=registry_inputs,
)

WINVER = Step(
    id="winver",
    title="Setting the Windows version",
    rev=1,
    weight=5,
    run=run_winver,
    verify=verify_winver,
)

# The steps that rebuild an empty prefix (re-run by the dotnet step after a reset).
BOOT_STEPS = (PREFIX_INIT, SHELL_FOLDERS, REGISTRY)
