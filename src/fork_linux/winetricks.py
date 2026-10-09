"""Winetricks: the pinned script (managed) or the distribution's (system), and running its verbs.

The managed copy is the manifest-pinned script, downloaded with a sha256 check
into ``runtimes/winetricks/<version>/winetricks``. Winetricks' own downloads
(Microsoft .NET, core fonts) are sha256-checked by winetricks itself and cached
in ``$XDG_CACHE_HOME/winetricks``.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from collections.abc import Iterable, Mapping
from pathlib import Path

from . import download, fsutil
from .errors import IntegrityFailed, UsageError, WineUnavailable
from .manifest import Manifest
from .paths import Paths
from .procrun import Completed, Runner
from .wine_provider import COMPLETE_MARKER, Progress

log = logging.getLogger(__name__)

MODES = ("managed", "system")
STORE = "winetricks"
SCRIPT = "winetricks"
LOG_NAME = "winetricks.log"
DEFAULT_TIMEOUT = 2700
# The pinned script's host (AGENTS.md hard rule 7).
WINETRICKS_DOWNLOAD_HOSTS = ("raw.githubusercontent.com",)

# A verb or setting (``dotnet48``, ``win10``, ``renderer=gdi``); never an option.
_VERB_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+=-]{0,63}")


def managed_path(manifest: Manifest, paths: Paths) -> Path:
    """Where the pinned winetricks script lives once installed."""
    return paths.runtimes_dir / STORE / manifest.winetricks.version / SCRIPT


def _verified(script: Path, sha256: str) -> bool:
    """True if ``script`` is a regular executable file with the pinned sha256."""
    if script.is_symlink() or not script.is_file() or not os.access(script, os.X_OK):
        return False
    return fsutil.sha256_file(script) == sha256


def ensure(
    manifest: Manifest,
    paths: Paths,
    runner: Runner,
    *,
    mode: str = "managed",
    offline: bool = False,
    progress: Progress | None = None,
) -> Path:
    """Return a winetricks to run, installing the pinned copy first if needed.

    ``managed`` downloads the manifest's script (verified size + sha256) and
    installs it 0755 next to a complete marker; an already verified copy is
    reused. ``system`` uses ``winetricks`` from ``PATH`` or raises
    :class:`WineUnavailable`.
    """
    if mode == "system":
        found = runner.which("winetricks")
        if found is None:
            raise WineUnavailable(
                "winetricks is not installed",
                hint="install your distribution's winetricks package, or use the pinned copy (the default)",
            )
        return Path(found)
    if mode != "managed":
        raise UsageError(f"unknown winetricks mode {mode!r}", hint=f"use {' or '.join(MODES)}")
    pinned = manifest.winetricks
    target = managed_path(manifest, paths)
    if _verified(target, pinned.sha256):
        return target
    fetched = download.fetch(
        pinned.url,
        f"winetricks-{pinned.version}",
        cache_dir=paths.downloads_dir,
        sha256=pinned.sha256,
        size=pinned.size,
        offline=offline,
        progress=progress,
        allowed_hosts=WINETRICKS_DOWNLOAD_HOSTS,
    )
    data = fetched.read_bytes()
    if hashlib.sha256(data).hexdigest() != pinned.sha256:
        raise IntegrityFailed(
            f"{fetched} changed after it was verified",
            hint="delete the download cache and run setup again",
        )
    fsutil.ensure_dir(target.parent)
    fsutil.atomic_write(target, data, mode=0o755)
    fsutil.atomic_write(target.parent / COMPLETE_MARKER, pinned.sha256 + "\n", mode=0o644)
    log.info("installed winetricks %s in %s", pinned.version, target)
    return target


def run_verbs(
    runner: Runner,
    winetricks: Path,
    env: Mapping[str, str],
    verbs: Iterable[str],
    *,
    log_file: Path | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> Completed:
    """Run ``winetricks -q <verbs>`` unattended against the Wine in ``env``.

    ``env`` comes from :func:`winecmd.build_env`; winetricks additionally gets
    ``WINE`` (= ``WINELOADER``), ``W_OPT_UNATTENDED=1`` and its update check
    disabled. Verbs are validated so none can be read as an option.
    """
    verb_list = [verbs] if isinstance(verbs, str) else list(verbs)
    if not verb_list:
        raise ValueError("no winetricks verbs given")
    for verb in verb_list:
        if _VERB_RE.fullmatch(verb) is None:
            raise ValueError(f"not a winetricks verb: {verb!r}")
    call_env = dict(env)
    call_env["WINETRICKS_LATEST_VERSION_CHECK"] = "disabled"
    call_env["W_OPT_UNATTENDED"] = "1"
    loader = env.get("WINELOADER")
    if loader:
        call_env["WINE"] = loader
    return runner.run([str(winetricks), "-q", *verb_list], env=call_env, timeout=timeout, log_file=log_file)


def installed_verbs(prefix: Path) -> set[str]:
    """Verbs winetricks recorded as installed in ``<prefix>/winetricks.log`` (one per line)."""
    try:
        text = (prefix / LOG_NAME).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return set()
    verbs = set()
    for line in text.splitlines():
        words = line.split()
        if words and not words[0].startswith(("-", "#")):
            verbs.add(words[0])
    return verbs
