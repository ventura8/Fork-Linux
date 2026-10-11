"""Real tier: an existing user's prefix follows a new Wine pin (scenario A24).

A user's prefix is set up by the previous release's pin (Kron4ek 11.0 staging, still listed in the
runtime manifest as known-good) and used: Fork's first run, a commit through Fork. Then the user
upgrades fork-linux, which moves the pin (this release's ``wine.default``). The next setup must
re-run only the Wine-dependent steps (never .NET, the fonts or Fork's installer), doctor stays
healthy, and Fork starts on the new build and commits again.

The previous release is simulated by a manifest override written before the first setup (the old
pin as ``wine.default`` and another manifest revision) and removed for the upgrade: the packaged
manifest of this release takes over. The prefix lives in a root of its own (``E2E.child``), seeded
with the session's verified downloads. ``CHAIN = True``: the steps build on each other.

Gated by ``FL_E2E_FORK=1``; containers only (``scripts/e2e-docker.sh pytest
tests/e2e/test_wine_pin_upgrade.py``, AGENTS.md hard rule 18).
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fork_driver import git, make_repo
from fork_ui import ForkUI

from fixtures import real_tier
from fork_linux import manifest, wine_provider
from fork_linux.paths import Paths

if os.environ.get("FL_E2E_FORK") != "1":
    pytest.skip("requires FL_E2E_FORK=1 (real Wine + the official Fork download in a container)",
                allow_module_level=True)
real_tier.enforce()

CHAIN = True
SETUP_TIMEOUT = 3600.0
# The pin of the release before this one; the manifest keeps it listed (known-good).
OLD_ID = "kron4ek-11.0-staging-wow64"
NEW_ID = manifest.load().wine_default.id
# A pin change never re-runs what downloads or installs something big.
HEAVY_STEPS = {"winetricks", "dotnet", "fonts", "ui_font", "fork_download", "fork_install"}
# What a Wine change re-runs (wine.inf takes .js back for its JSFile: host_integration follows).
WINE_STEPS = {"wine_runtime", "prefix_init", "host_integration"}


def _previous_revision(revision: str) -> str:
    """A manifest revision other than ``revision`` (the release before this one had its own)."""
    year, month, number = revision.split(".")
    return f"{year}.{month}.{int(number) + 1}" if number == "0" else f"{year}.{month}.{int(number) - 1}"


def _override(env: Any) -> Path:
    return env.home / ".config" / "fork-linux" / "manifest.local.json"


def _status(env: Any) -> dict[str, Any]:
    return json.loads(env.cli("status", "--json").stdout)


@pytest.fixture(scope="module")
def pin(e2e: Any) -> Iterator[Any]:
    """This module's own home and prefix in ``<root>/pin-upgrade``; Wine is stopped afterwards."""
    env = e2e.child("pin-upgrade")
    try:
        yield env
    finally:
        env.fork.ensure_closed()
        env.stop_wine()


@pytest.fixture(scope="module")
def pin_ui(pin: Any) -> ForkUI:
    return ForkUI(pin.fork)


def _commit_through_fork(env: Any, ui: ForkUI, repo: Path, name: str, subject: str) -> None:
    """Change ``name`` in ``repo``, stage it and commit it in Fork; git then has ``subject`` on top."""
    (repo / name).write_text(f"{subject}\n", encoding="utf-8")
    ui.open(repo)
    ui.local_changes()
    ui.stage_all_by_double_click(repo, 1)
    ui.commit_and_wait(repo, subject)
    assert git(repo, "log", "-1", "--format=%s") == subject
    env.fork.close()


def test_01_setup_on_the_previous_pin_and_commit(pin: Any, pin_ui: ForkUI) -> None:
    """The previous release sets the prefix up on its pin; Fork's first run and a commit through Fork.

    Scenarios: A24
    """
    _override(pin).parent.mkdir(parents=True, exist_ok=True)
    previous = {"revision": _previous_revision(manifest.load().revision), "wine": {"default": OLD_ID}}
    _override(pin).write_text(json.dumps(previous), encoding="utf-8")
    pin.cli(*pin.setup_args(), timeout=SETUP_TIMEOUT)
    assert _status(pin)["wine_build"] == OLD_ID
    repo = make_repo(pin.repos / "demo", {"README.md": "# demo\n"})
    _commit_through_fork(pin, pin_ui, repo, "on-old.txt", "committed on the previous pin")


def test_02_the_upgrade_moves_the_prefix_to_the_new_pin(pin: Any) -> None:
    """The upgrade's packaged manifest takes over: only the Wine-dependent steps re-run; doctor is healthy.

    Scenarios: A24
    """
    _override(pin).unlink()
    report = json.loads(pin.cli("setup", "--no-gui", "--json", timeout=SETUP_TIMEOUT).stdout)
    ran = set(report["ran"])
    assert WINE_STEPS <= ran, report["ran"]
    assert HEAVY_STEPS.isdisjoint(ran), report["ran"]
    assert _status(pin)["wine_build"] == NEW_ID
    # The previous build stays on disk (config set wine.build switches back without a download).
    assert sorted(wine_provider.installed_builds(Paths.from_env(pin.env))) == sorted([NEW_ID, OLD_ID])
    doctor = json.loads(pin.cli("doctor", "--deep", "--json", check=False).stdout)
    unhealthy = [(check["id"], check["status"], check["detail"]) for check in doctor["checks"]
                 if check["status"] in ("fail", "warn")]
    assert unhealthy == []


def test_03_fork_starts_and_commits_on_the_new_pin(pin: Any, pin_ui: ForkUI) -> None:
    """Fork starts on the new build and commits, twice (the second start reads what the first wrote).

    Scenarios: A24
    """
    repo = pin.repos / "demo"
    _commit_through_fork(pin, pin_ui, repo, "on-new.txt", "committed on the new pin")
    _commit_through_fork(pin, pin_ui, repo, "again.txt", "committed again on the new pin")
    assert git(repo, "rev-list", "--count", "HEAD") == "4"
    assert _status(pin)["wine_build"] == NEW_ID
