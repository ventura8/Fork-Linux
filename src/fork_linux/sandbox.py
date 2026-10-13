"""Detect Flatpak/Snap/AppImage packaging and undo their environment changes for host tools.

Wine, git, terminals and editors must see the user's real environment: the
library paths, Python paths and GTK module paths a sandbox or bundle injects
for *our* process would break them.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

NONE = "none"
FLATPAK = "flatpak"
SNAP = "snap"
APPIMAGE = "appimage"

FLATPAK_INFO = Path("/.flatpak-info")
HOST_SPAWN = ("flatpak-spawn", "--host")

# Variables bundles inject for their own process that must not leak into host tools.
INJECTED = (
    "LD_LIBRARY_PATH",
    "LD_PRELOAD",
    "PYTHONHOME",
    "PYTHONPATH",
    "GTK_PATH",
    "GIO_MODULE_DIR",
    "GDK_PIXBUF_MODULE_FILE",
)
ORIG_SUFFIX = "_ORIG"


def detect(env: Mapping[str, str] | None = None) -> str:
    """``flatpak``, ``snap``, ``appimage`` or ``none`` for the current process."""
    environ: Mapping[str, str] = os.environ if env is None else env
    if FLATPAK_INFO.exists() or environ.get("FLATPAK_ID"):
        return FLATPAK
    if environ.get("SNAP"):
        return SNAP
    if environ.get("APPIMAGE") or environ.get("APPDIR"):
        return APPIMAGE
    return NONE


def host_argv(argv: list[str], kind: str | None = None) -> list[str]:
    """``argv`` to run a *host* program: prefixed with ``flatpak-spawn --host`` inside Flatpak."""
    sandbox = detect() if kind is None else kind
    if sandbox == FLATPAK:
        return [*HOST_SPAWN, *argv]
    return list(argv)


def clean_env(env: Mapping[str, str]) -> dict[str, str]:
    """A copy of ``env`` without bundle-injected variables, originals restored.

    Drops :data:`INJECTED`, ``SNAP`` and every ``SNAP_*`` variable, then
    restores ``VAR`` from ``VAR_ORIG`` (the convention used by our AppRun and
    by bundlers such as PyInstaller): a non-empty original is put back, an
    empty one means the variable was unset.
    """
    cleaned = {
        key: value
        for key, value in env.items()
        if key not in INJECTED and key != "SNAP" and not key.startswith("SNAP_")
    }
    for key in [name for name in cleaned if name.endswith(ORIG_SUFFIX) and len(name) > len(ORIG_SUFFIX)]:
        original = cleaned.pop(key)
        base = key[: -len(ORIG_SUFFIX)]
        if original:
            cleaned[base] = original
        else:
            cleaned.pop(base, None)
    return cleaned
