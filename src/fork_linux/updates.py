"""Fork's own updates: notice version changes, report what is available, enforce a pin.

Fork updates itself through Velopack (spike S9, verified under Wine): it
downloads ``Fork-<v>-delta.nupkg`` (or the full package) into ``packages\\``,
rebuilds ``Fork-<v>-full.nupkg`` and, on "Restart and Update", runs
``Update.exe apply``, which replaces ``current\\`` and starts the new
``Fork.exe`` with the old process's environment. fork-linux never installs
updates itself; it records the version it last saw (so the launcher can offer
a rollback after a change), and when the user pinned a version it deletes
newer staged packages and turns Fork's own update check off
(:func:`sync_update_type`).
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import feeds, fork_settings, versions
from .errors import UsageError
from .fork_layout import ForkLayout
from .manifest import Manifest
from .state import State

LAST_SEEN_VERSION = "fork.last_seen_version"
# Fork's ApplicationUpdateType before a pin turned it off (restored when updates are allowed again).
SAVED_UPDATE_TYPE = "fork.update_type_before_pin"
UPDATE_TYPE_DEVELOP = 0  # Fork's default: every release
UPDATE_TYPE_STABLE = 1  # releases after a delay (its feed lags far behind)
UPDATE_TYPE_OFF = 2
UPDATE_TYPE_NAMES = {UPDATE_TYPE_DEVELOP: "develop", UPDATE_TYPE_STABLE: "stable", UPDATE_TYPE_OFF: "off"}


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


def _update_type(value: Any) -> int | None:
    """``value`` when it is one of Fork's ``ApplicationUpdateType`` values, else None."""
    if isinstance(value, int) and not isinstance(value, bool) and value in UPDATE_TYPE_NAMES:
        return value
    return None


def update_type_name(settings: dict[str, Any]) -> str:
    """Fork's update channel from ``settings.json`` data: develop (also when unset), stable, off or unknown."""
    value = fork_settings.get(settings, fork_settings.APPLICATION_UPDATE_TYPE, UPDATE_TYPE_DEVELOP)
    known = _update_type(value)
    return "unknown" if known is None else UPDATE_TYPE_NAMES[known]


def sync_update_type(layout: ForkLayout, *, pinned: bool, state: State, backup_dir: Path) -> str | None:
    """Make Fork's own update check follow ``[fork] update_policy``; return a note on what changed.

    Pinned: ``ApplicationUpdateType`` becomes 2 (Off), so Fork neither offers
    nor downloads updates, and the user's previous value is kept in ``state``.
    Allowed again: that value comes back, unless the user changed the setting
    meanwhile. A missing ``settings.json`` is left alone. The caller makes
    sure Fork is closed and saves ``state``.
    """
    if not layout.settings_file.exists():
        return None
    key = fork_settings.APPLICATION_UPDATE_TYPE
    current = fork_settings.get(fork_settings.load(layout.settings_file), key)
    if pinned:
        if current == UPDATE_TYPE_OFF:
            return None
        fork_settings.apply(layout, {key: UPDATE_TYPE_OFF}, backup_dir=backup_dir)
        previous = _update_type(current)
        state.set(SAVED_UPDATE_TYPE, UPDATE_TYPE_DEVELOP if previous is None else previous)
        return "Fork's own update check turned off while Fork is pinned"
    saved = state.get(SAVED_UPDATE_TYPE)
    if saved is None:
        return None
    state.set(SAVED_UPDATE_TYPE, None)
    if current != UPDATE_TYPE_OFF:
        return None
    value = _update_type(saved)
    value = UPDATE_TYPE_DEVELOP if value is None else value
    fork_settings.apply(layout, {key: value}, backup_dir=backup_dir)
    return f"Fork's own update check is back on ({UPDATE_TYPE_NAMES[value]})"
