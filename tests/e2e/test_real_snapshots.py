"""Snapshots of the installed Fork and rollback: create, list, delete, prune, rollback by id with
settings, and the pin that turns Fork's own update check off until the user unpins.

Fork stays closed (snapshot / rollback need it closed). Gated by ``FL_E2E_FORK=1``;
containers only.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from typing import Any

import pytest
from fork_driver import fork_version, make_repo

from fixtures import real_tier

if os.environ.get("FL_E2E_FORK") != "1":
    pytest.skip("requires FL_E2E_FORK=1 (the real install in a container)", allow_module_level=True)
real_tier.enforce()

UPDATES_OFF = 2


@pytest.fixture
def unpinned(closed: Any) -> Iterator[Any]:
    """Fork closed; the update policy back to auto afterwards (a rollback pins it)."""
    yield closed
    closed.cli("config", "set", "fork.update_policy", "auto")


def _snapshots(e2e: Any) -> list[dict[str, Any]]:
    return json.loads(e2e.cli("snapshot", "list", "--json").stdout)


def test_snapshot_create_list_delete_prune(unpinned: Any) -> None:
    """``snapshot create`` / ``list`` / ``delete`` / ``prune`` (keeps the newest ``[snapshots] keep``).

    Scenarios: R-SNAPSHOT-MANAGE
    """
    e2e = unpinned
    first = json.loads(e2e.cli("snapshot", "create", "--json", "--reason", "e2e one").stdout)
    second = json.loads(e2e.cli("snapshot", "create", "--json", "--reason", "e2e two").stdout)
    third = json.loads(e2e.cli("snapshot", "create", "--json", "--reason", "e2e three").stdout)
    assert first["fork_version"] == fork_version()
    ids = [snap["id"] for snap in _snapshots(e2e)]
    assert {first["id"], second["id"], third["id"]} <= set(ids)
    assert ids.index(third["id"]) < ids.index(first["id"]), "newest first"
    e2e.cli("snapshot", "delete", second["id"])
    assert second["id"] not in [snap["id"] for snap in _snapshots(e2e)]
    pruned = json.loads(e2e.cli("snapshot", "prune", "--json").stdout)["deleted"]
    remaining = [snap["id"] for snap in _snapshots(e2e)]
    assert remaining == [third["id"], first["id"]], remaining
    assert set(pruned).isdisjoint(remaining)


def test_rollback_with_settings_restores_settings(unpinned: Any) -> None:
    """``rollback ID --with-settings`` brings back Fork's settings.json of that snapshot.

    Scenarios: R-WITH-SETTINGS, R-ROLLBACK-ID
    """
    e2e = unpinned
    e2e.cli("settings", "set", "CommitSubjectHighLimit", "70")
    snap = json.loads(e2e.cli("snapshot", "create", "--json", "--reason", "e2e settings").stdout)
    e2e.cli("settings", "set", "CommitSubjectHighLimit", "99")
    restored = json.loads(e2e.cli("rollback", snap["id"], "--with-settings", "--no-pin", "--json").stdout)
    assert restored["restored"]["id"] == snap["id"]
    assert restored["with_settings"] is True
    assert e2e.fork.settings()["CommitSubjectHighLimit"] == 70
    window = e2e.fork.start(make_repo(e2e.repos / "rollback-settings", {".gitignore": "*.tmp\n"}))
    assert window.is_main


def test_rollback_pins_and_auto_brings_the_channel_back(unpinned: Any) -> None:
    """A rollback pins the version: Fork's update check is turned off; ``update_policy auto`` restores it.

    Scenarios: R-PIN
    """
    e2e = unpinned
    e2e.cli("settings", "set", "ApplicationUpdateType", "1")
    e2e.cli("rollback", "--to-version", fork_version())
    assert e2e.cli("config", "get", "fork.update_policy").stdout.strip() == "pinned"
    assert e2e.fork.settings()["ApplicationUpdateType"] == UPDATES_OFF
    repo = make_repo(e2e.repos / "rollback-pin", {".gitignore": "*.tmp\n"})
    e2e.fork.start(repo)
    assert e2e.fork.settings()["ApplicationUpdateType"] == UPDATES_OFF, "the pre-launch hook keeps updates off"
    e2e.fork.close()
    e2e.cli("config", "set", "fork.update_policy", "auto")
    e2e.fork.start(repo)
    assert e2e.fork.settings()["ApplicationUpdateType"] == 1, "the user's channel (Stable) comes back"
    check = json.loads(e2e.cli("--offline", "update", "--check", "--json").stdout)
    assert check["fork_channel"] == "stable"
    assert check["update_policy"] == "auto"
