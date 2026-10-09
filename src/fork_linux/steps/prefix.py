"""Steps ``prefix_init``, ``shell_folders``, ``registry`` and ``winver``: our own Wine prefix.

The prefix is created by ``wineboot --init`` with Mono and Gecko disabled
(Fork needs the real .NET Framework, installed by the next steps), Wine's
``Desktop`` link to the Linux desktop is replaced by a real directory, and one
registry batch applies the settings Fork needs under Wine (spike S1).
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .. import registry, resources, winecmd, winetricks
from ..bootstrap import Ctx, Step
from ..errors import ForkLinuxError, SetupFailed
from ..registry import RegBatch
from . import runtime

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


def reg_matches(ctx: Ctx, expected: Sequence[tuple[str, str, object]]) -> bool:
    """True if every ``(key, name, value)`` is in the prefix's registry (names case-insensitive)."""
    return all(reg_value(ctx, key, name) == value for key, name, value in expected)


def import_batch(ctx: Ctx, batch: RegBatch, name: str) -> None:
    """Import ``batch`` with ``wine regedit`` (a failure raises :class:`ForkLinuxError`)."""
    winecmd.import_reg(
        ctx.runner, ctx.wine_env(), ctx.wine(), batch, ctx.paths, ctx.user, name=name, log_file=ctx.log_file
    )


def run_verbs(ctx: Ctx, step_id: str, verbs: Sequence[str]) -> None:
    """``winetricks -q <verbs>``; a non-zero exit raises :class:`SetupFailed` for ``step_id``."""
    result = winetricks.run_verbs(
        ctx.runner, runtime.winetricks_path(ctx), ctx.wine_env(), list(verbs), log_file=ctx.log_file
    )
    if not result.ok:
        raise SetupFailed(
            step_id,
            f"winetricks {' '.join(verbs)} exited with code {result.returncode}",
            hint="check your network connection; winetricks' output is in the setup log",
        )


# -- prefix_init -------------------------------------------------------------------------


def prefix_inputs(ctx: Ctx) -> dict[str, Any]:
    """The Wine the prefix was booted with."""
    return {"wine": runtime.wine_inputs(ctx)}


def run_prefix_init(ctx: Ctx) -> None:
    """``wineboot --init`` (Mono/Gecko off), then wait for the server to write the hives."""
    env = ctx.wine_env(dll_overrides=BOOT_DLL_OVERRIDES)
    info = ctx.wine()
    result = winecmd.run(
        ctx.runner, env, info, ["wineboot", "--init"], timeout=WINEBOOT_TIMEOUT, log_file=ctx.log_file
    )
    if not result.ok:
        raise SetupFailed(
            "prefix_init",
            f"wineboot exited with code {result.returncode}",
            hint="run 'fork-linux doctor' to check the Wine runtime and host libraries",
        )
    winecmd.wineserver(ctx.runner, env, info, "-w")
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
    handler = resources.libexec_dir() / URL_HANDLER
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
    rev=2,
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
