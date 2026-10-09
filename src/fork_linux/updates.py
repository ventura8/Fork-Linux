"""Fork's own updates: notice version changes, report what is available, enforce a pin.

Fork updates itself through Velopack: it downloads ``Fork-<v>-full.nupkg`` into
``packages\\`` and swaps ``current\\`` on a later start. fork-linux never
installs updates itself; it records the version it last saw (so the launcher
can offer a rollback after a change) and, when the user pinned a version,
deletes newer staged packages so Velopack has nothing to apply.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import feeds, versions
from .errors import UsageError
from .fork_layout import ForkLayout
from .manifest import Manifest
from .state import State

LAST_SEEN_VERSION = "fork.last_seen_version"


@dataclass
class VersionChange:
    """The installed Fork version changed from ``old`` (None: first seen) to ``new``."""

    old: str | None
    new: str


def _same_version(a: str, b: str) -> bool:
    if versions.is_valid(a) and versions.is_valid(b):
        return versions.Version(a) == versions.Version(b)
    return a == b


def observe_version(state: State, layout: ForkLayout) -> VersionChange | None:
    """Compare the installed version with the one last seen; record it (the caller saves ``state``)."""
    installed = layout.installed_version()
    if installed is None:
        return None
    previous = state.get(LAST_SEEN_VERSION)
    if not isinstance(previous, str):
        previous = None
    if previous is not None and _same_version(previous, installed):
        return None
    state.set(LAST_SEEN_VERSION, installed)
    return VersionChange(old=previous, new=installed)


def check(manifest: Manifest, layout: ForkLayout, feed_assets: Iterable[feeds.FeedAsset] | None) -> dict[str, Any]:
    """Update status for ``update --check`` and ``doctor``.

    Keys: ``installed`` (str | None), ``default`` (the manifest's tested version),
    ``latest`` (newest full package in the feed, None without a feed),
    ``known_good`` (the installed version is known-good), ``known_bad`` (why the
    installed version is known-bad, or None), ``update_available`` (the feed has
    a newer version that is not known-bad) and ``staged`` (versions Velopack
    downloaded but has not applied yet).
    """
    installed = layout.installed_version()
    newest = feeds.latest_full(feed_assets or ())
    latest = None if newest is None else newest.version
    entry = None if installed is None else manifest.fork_version(installed)
    staged: list[str] = []
    for version, _path in layout.staged_packages():
        if version not in staged:
            staged.append(version)
    return {
        "installed": installed,
        "default": manifest.fork_default,
        "latest": latest,
        "known_good": entry is not None and entry.status == "known-good" and not manifest.is_known_bad(installed),
        "known_bad": None if installed is None else manifest.known_bad_reason(installed),
        "update_available": (
            installed is not None
            and latest is not None
            and versions.Version(latest) > versions.Version(installed)
            and not manifest.is_known_bad(latest)
        ),
        "staged": staged,
    }


def enforce_pin(layout: ForkLayout, pinned: str) -> list[Path]:
    """Delete staged packages newer than ``pinned``; return the deleted paths."""
    if not isinstance(pinned, str) or not versions.is_valid(pinned):
        raise UsageError(f"not a Fork version: {pinned!r}", hint="use a dotted number such as 2.23.2")
    deleted = []
    for _version, package in layout.staged_packages(than=pinned):
        with contextlib.suppress(FileNotFoundError):
            package.unlink()
            deleted.append(package)
    return deleted
