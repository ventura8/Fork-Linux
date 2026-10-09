"""Tests for fork_linux.updates: version-change detection, update status and pin enforcement."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from fixtures.fork_tree import install_fork, make_layout, write_sq_version
from fork_linux import manifest as manifest_mod
from fork_linux import updates
from fork_linux.errors import UsageError
from fork_linux.feeds import FeedAsset
from fork_linux.fork_layout import ForkLayout
from fork_linux.manifest import Manifest
from fork_linux.state import State
from fork_linux.updates import VersionChange


@pytest.fixture
def layout(tmp_path: Path) -> ForkLayout:
    return make_layout(tmp_path)


@pytest.fixture
def state(tmp_path: Path) -> State:
    return State.load(tmp_path / "state.json")


@pytest.fixture
def manifest() -> Manifest:
    data = json.loads(manifest_mod.DEFAULT_PATH.read_text(encoding="utf-8"))
    data["fork"]["versions"]["2.24.0"] = {
        "size": 77000000,
        "sha256": "PENDING",
        "full_nupkg_sha256": "a" * 64,
        "status": "testing",
        "tested_with": [],
    }
    data["fork"]["known_bad"] = {"2.26.0": "breaks the repository view"}
    return Manifest(data)


def _asset(version: str, kind: str = "Full") -> FeedAsset:
    return FeedAsset(version, kind, f"Fork-{version}-{kind.lower()}.nupkg", "d" * 64, None, 1000)


# -- observe_version ------------------------------------------------------------------


def test_observe_version_not_installed(state: State, layout: ForkLayout) -> None:
    assert updates.observe_version(state, layout) is None
    assert state.get(updates.LAST_SEEN_VERSION) is None


def test_observe_version_tracks_changes(state: State, layout: ForkLayout) -> None:
    write_sq_version(layout, "2.23.2")
    assert updates.observe_version(state, layout) == VersionChange(old=None, new="2.23.2")
    assert state.get("fork.last_seen_version") == "2.23.2"
    assert updates.observe_version(state, layout) is None
    write_sq_version(layout, "2.24.0")
    assert updates.observe_version(state, layout) == VersionChange(old="2.23.2", new="2.24.0")
    assert state.get(updates.LAST_SEEN_VERSION) == "2.24.0"


def test_observe_version_compares_numerically(state: State, layout: ForkLayout) -> None:
    state.set(updates.LAST_SEEN_VERSION, "2.23.2.0")
    write_sq_version(layout, "2.23.2")
    assert updates.observe_version(state, layout) is None
    assert state.get(updates.LAST_SEEN_VERSION) == "2.23.2.0"


@pytest.mark.parametrize(("stored", "old"), [(42, None), ("garbage", "garbage")])
def test_observe_version_with_odd_stored_value(state: State, layout: ForkLayout, stored: Any, old: Any) -> None:
    state.set(updates.LAST_SEEN_VERSION, stored)
    write_sq_version(layout, "2.23.2")
    assert updates.observe_version(state, layout) == VersionChange(old=old, new="2.23.2")


# -- check ----------------------------------------------------------------------------------


def test_check_not_installed_without_feed(manifest: Manifest, layout: ForkLayout) -> None:
    assert updates.check(manifest, layout, None) == {
        "installed": None,
        "default": manifest.fork_default,
        "latest": None,
        "known_good": False,
        "known_bad": None,
        "update_available": False,
        "staged": [],
    }


def test_check_known_good_with_newer_feed_and_staged(manifest: Manifest, layout: ForkLayout) -> None:
    install_fork(layout, "2.23.2")
    for name in ("Fork-2.24.0-full.nupkg", "Fork-2.24.0-delta.nupkg", "Fork-2.25.0-full.nupkg"):
        (layout.packages_dir / name).write_bytes(b"PK")
    feed = [_asset("2.23.2"), _asset("2.25.0"), _asset("2.25.0", "Delta"), _asset("2.27.0", "Delta")]
    assert updates.check(manifest, layout, feed) == {
        "installed": "2.23.2",
        "default": manifest.fork_default,
        "latest": "2.25.0",
        "known_good": True,
        "known_bad": None,
        "update_available": True,
        "staged": ["2.24.0", "2.25.0"],
    }


def test_check_latest_known_bad_is_not_offered(manifest: Manifest, layout: ForkLayout) -> None:
    install_fork(layout, "2.24.0")
    status = updates.check(manifest, layout, [_asset("2.26.0")])
    assert status["latest"] == "2.26.0"
    assert status["update_available"] is False
    assert status["known_good"] is False


def test_check_up_to_date_and_unknown_version(manifest: Manifest, layout: ForkLayout) -> None:
    install_fork(layout, "2.25.0")
    status = updates.check(manifest, layout, [_asset("2.25.0"), _asset("2.20.0")])
    assert status["update_available"] is False
    assert status["known_good"] is False
    assert status["known_bad"] is None


def test_check_installed_known_bad(manifest: Manifest, layout: ForkLayout) -> None:
    install_fork(layout, "2.26.0")
    status = updates.check(manifest, layout, [])
    assert status["known_bad"] == "breaks the repository view"
    assert status["known_good"] is False


def test_check_known_good_entry_marked_bad(layout: ForkLayout) -> None:
    data = json.loads(manifest_mod.DEFAULT_PATH.read_text(encoding="utf-8"))
    data["fork"]["versions"]["2.20.0"] = {
        "size": 1,
        "sha256": "e" * 64,
        "full_nupkg_sha256": "f" * 64,
        "status": "known-good",
        "tested_with": [],
    }
    data["fork"]["known_bad"] = {"2.20.0": "regressed"}
    install_fork(layout, "2.20.0")
    status = updates.check(Manifest(data), layout, None)
    assert status["known_good"] is False
    assert status["known_bad"] == "regressed"


# -- enforce_pin ----------------------------------------------------------------------------


def test_enforce_pin_deletes_newer_staged_packages(layout: ForkLayout) -> None:
    install_fork(layout, "2.24.0")
    pkgs = layout.packages_dir
    for name in ("Fork-2.23.2-full.nupkg", "Fork-2.25.0-full.nupkg", "Fork-2.25.0-delta.nupkg", ".velopack_lock"):
        (pkgs / name).write_bytes(b"PK")
    deleted = updates.enforce_pin(layout, "2.23.2")
    assert deleted == [
        pkgs / "Fork-2.24.0-full.nupkg",
        pkgs / "Fork-2.25.0-delta.nupkg",
        pkgs / "Fork-2.25.0-full.nupkg",
    ]
    assert sorted(p.name for p in pkgs.iterdir()) == [".velopack_lock", "Fork-2.23.2-full.nupkg"]
    assert updates.enforce_pin(layout, "2.23.2") == []


@pytest.mark.parametrize("pinned", ["", "latest", None])
def test_enforce_pin_rejects_invalid_versions(layout: ForkLayout, pinned: Any) -> None:
    with pytest.raises(UsageError, match="not a Fork version"):
        updates.enforce_pin(layout, pinned)
