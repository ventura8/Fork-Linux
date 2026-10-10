"""Stable paths for the helper programs Fork runs, also when fork-linux runs from an AppImage.

Wine's registry and Fork's ``settings.json`` keep the paths of ``fork-linux-terminal``,
``fork-linux-explorer`` and ``fork-linux-open-url``, and the git bridge daemon keeps the
path of ``fork-linux-host`` for as long as Fork runs. A package installs them at stable
paths. An AppImage, however, mounts itself at a new ``/tmp/.mount_*`` on every start: the
stored paths would change on every launch (re-running the setup steps that store them)
and stop working once that instance ends, for example when Fork's own updater restarts
Fork. Inside an AppImage the helpers are therefore reached through small launchers in
``<data_dir>/helpers/`` that run ``<AppImage> --libexec NAME``; a launcher is rewritten
when the AppImage file moves, so the stored paths never change (scenario A9 of
docs/qa/UPDATE-SCENARIOS.md).
"""

from __future__ import annotations

import os
import shlex
from collections.abc import Mapping
from pathlib import Path

from . import fsutil, resources
from .paths import Paths

HELPERS_DIR = "helpers"
# The helpers AppRun's --libexec runs (packaging/appimage/AppRun keeps the same list).
NAMES = ("fork-linux-terminal", "fork-linux-explorer", "fork-linux-open-url", "fork-linux-host")
# The launchers tell fork-linux-handoff where the stable fork-linux-host is.
HELPER_DIR_ENV = "FORK_LINUX_HELPER_DIR"
_LAUNCHER = (
    "#!/bin/sh\n"
    "# Written by fork-linux: runs {name} from the AppImage, wherever it was mounted.\n"
    "{env}={directory} exec {appimage} --libexec {name} \"$@\"\n"
)


def appimage(env: Mapping[str, str] | None = None) -> Path | None:
    """The AppImage file this fork-linux runs from, or None when it is not running from one.

    The AppImage runtime sets ``APPIMAGE`` (the file) and ``APPDIR`` (its mount); this
    package must live inside that mount, so a stray ``APPIMAGE`` never counts.
    """
    environ: Mapping[str, str] = os.environ if env is None else env
    image, appdir = environ.get("APPIMAGE", ""), environ.get("APPDIR", "")
    if not (os.path.isabs(image) and os.path.isabs(appdir) and os.path.isfile(image)):
        return None
    here = Path(os.path.realpath(resources.install_root()))
    mount = Path(os.path.realpath(appdir))
    return Path(image) if here == mount or mount in here.parents else None


def launcher_text(name: str, image: Path, directory: Path) -> str:
    """The launcher script of helper ``name`` for the AppImage ``image``."""
    return _LAUNCHER.format(
        name=name,
        env=HELPER_DIR_ENV,
        directory=shlex.quote(str(directory)),
        appimage=shlex.quote(str(image)),
    )


def path(name: str, *, paths: Paths | None = None, env: Mapping[str, str] | None = None) -> Path:
    """Where helper ``name`` is run from: a stable launcher inside an AppImage, else the installed file.

    The launcher is (re)written only when its text changed, i.e. on the first use or
    after the AppImage moved; a helper the AppImage does not ship stays the installed path.
    """
    installed = resources.libexec_dir() / name
    image = appimage(env)
    if image is None or name not in NAMES or not installed.is_file():
        return installed
    where = paths if paths is not None else Paths.from_env(env)
    directory = fsutil.ensure_dir(where.data_dir / HELPERS_DIR)
    launcher = directory / name
    text = launcher_text(name, image, directory)
    try:
        current = launcher.read_text(encoding="utf-8") if not launcher.is_symlink() else None
    except OSError:
        current = None
    if current != text:
        fsutil.atomic_write(launcher, text, mode=0o700)
    return launcher
