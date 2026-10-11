"""The real thing: setup, doctor, first launch, tabs, dark theme, rollback and the logs bundle.

The tests run in file order and build on each other (one prefix, one Fork session at a
time): ``CHAIN = True`` makes the later ones fail fast after a failed step. Gated by
``FL_E2E_FORK=1`` and containers only (``scripts/e2e-docker.sh pytest tests/e2e``).
"""

from __future__ import annotations

import json
import os
import tarfile
import time
from typing import Any

import pytest
from fork_driver import fork_pids, fork_version, make_repo, windows_path

from fixtures import real_tier

if os.environ.get("FL_E2E_FORK") != "1":
    pytest.skip(
        "requires FL_E2E_FORK=1 (real Wine + the official Fork download, ~2.5 GB disk, Xvfb and xdotool)",
        allow_module_level=True,
    )
# Containers only (AGENTS.md hard rule 18): scripts/e2e-docker.sh pytest tests/e2e.
real_tier.enforce()

CHAIN = True
SETUP_TIMEOUT = 3600.0
NOOP_SETUP_SECONDS = 10.0
FAST_PATH_SECONDS = 30.0
MIN_STDDEV = 0.02
DARK_MEAN = 0.35


def test_01_setup_completes_and_resumes(e2e: Any) -> None:
    """A fresh managed setup completes; a second run is a quick no-op.

    Scenarios: S-FRESH-MANAGED, S-IDEMPOTENT, S-FORK-VERSION
    """
    started = time.monotonic()
    e2e.cli(*e2e.setup_args(), timeout=SETUP_TIMEOUT)
    (e2e.logs / "timing-setup.txt").write_text(f"{time.monotonic() - started:.1f}\n", encoding="utf-8")
    listed = json.loads(e2e.cli("setup", "--list-steps", "--json").stdout)
    assert listed["complete"] is True
    assert {row["status"] for row in listed["steps"]} <= {"done", "always"}
    status = json.loads(e2e.cli("status", "--json").stdout)
    assert status["fork_version"] == fork_version()
    assert status["wine_provider"] == "managed"
    started = time.monotonic()
    report = json.loads(e2e.cli(*e2e.setup_args(), "--json").stdout)
    assert report["ran"] == ["preflight", "finalize"]
    assert time.monotonic() - started < NOOP_SETUP_SECONDS


def test_02_doctor_has_no_failures(e2e: Any) -> None:
    """``doctor --deep`` on a fresh install: no failure and no warning.

    Scenarios: D-HEALTHY
    """
    report = json.loads(e2e.cli("doctor", "--deep", "--json").stdout)
    failed = [check for check in report["checks"] if check["status"] == "fail"]
    assert failed == []
    warned = {check["id"] for check in report["checks"] if check["status"] == "warn"}
    assert warned == set(), f"unexpected warnings: {warned}"


def test_03_first_launch_opens_the_repository(e2e: Any) -> None:
    """The first start answers Fork's first-run dialogs and shows the repository.

    Scenarios: L-FIRST-WELCOME
    """
    demo = make_repo(e2e.repos / "demo", {".gitignore": "*.tmp\n", "README.md": "# demo\n"})
    make_repo(e2e.repos / "second", {".gitignore": "*.tmp\n", "README.md": "# second\n"})
    window = e2e.fork.start(demo)
    shot = e2e.screen.screenshot("03-main")
    assert e2e.screen.stat(shot, "standard_deviation") > MIN_STDDEV
    assert fork_pids(e2e.prefix)
    # settings.json was seeded before the first start and Fork kept the Wine-safe values.
    settings = e2e.fork.settings()
    assert settings["UpdateSubmodulesOnCheckout"] is False
    assert settings["DisableHardwareAcceleration"] is True
    assert window.width >= 800
    # Fork's default folder for new clones is the Linux home from the very first start.
    toml = (e2e.fork.local_dir / "ForkData" / "repositories.toml").read_text(encoding="utf-8")
    line = next(line for line in toml.splitlines() if line.startswith("source_dirs"))
    assert windows_path(e2e.home) in line


def test_04_second_tab_and_fork_alias(e2e: Any) -> None:
    """``fork-linux run PATH`` and ``fork PATH`` hand the path to the running Fork.

    Scenarios: L-FORWARD-PATH, L-FORK-ALIAS
    """
    started = time.monotonic()
    second = e2e.fork.launch(e2e.repos / "second")
    assert second.wait(timeout=FAST_PATH_SECONDS) == 0
    assert time.monotonic() - started < FAST_PATH_SECONDS
    alias = e2e.fork.launch(e2e.repos / "demo", argv0="fork")
    assert alias.wait(timeout=FAST_PATH_SECONDS) == 0
    assert len(fork_pids(e2e.prefix)) == 1, "the second launches must hand over to the running Fork"


def test_05_relaunch_applies_dark_theme(e2e: Any) -> None:
    """Closing and starting Fork again applies the pre-launch hooks (here: a dark desktop theme).

    Scenarios: L-RELAUNCH, I-THEME-DARK
    """
    e2e.fork.close()
    window = e2e.fork.start(e2e.repos / "demo", extra={"FORK_LINUX_DISPLAY_THEME": "dark"})
    settings = e2e.fork.settings()
    assert settings["Theme"] == 1
    assert settings["FollowSystemTheme"] is False
    assert settings["UpdateSubmodulesOnCheckout"] is False
    assert settings["DisableHardwareAcceleration"] is True
    assert isinstance(settings["LayoutScaling"], int)
    time.sleep(2)
    shot = e2e.screen.screenshot("05-dark", window)
    assert e2e.screen.stat(shot, "mean") < DARK_MEAN


def test_07_snapshot_and_rollback(e2e: Any) -> None:
    """Rollback is refused while Fork runs, then restores the version and Fork starts again.

    Scenarios: R-SNAPSHOT-AUTO, R-REFUSE-RUNNING, R-ROLLBACK-VERSION
    """
    listing = e2e.cli("snapshot", "list", "--json")
    assert fork_version() in listing.stdout
    refused = e2e.cli("rollback", "--to-version", fork_version(), "--no-pin", check=False)
    assert refused.returncode == 15, "rollback must refuse while Fork runs"
    e2e.fork.close()
    e2e.cli("snapshot", "create")
    e2e.cli("rollback", "--to-version", fork_version(), "--no-pin")
    window = e2e.fork.start(e2e.repos / "demo", extra={"FORK_LINUX_DISPLAY_THEME": "light"})
    assert window.width >= 800
    e2e.fork.close()


def test_10_logs_bundle_is_redacted(e2e: Any) -> None:
    """``logs --bundle`` leaves out accounts.json and redacts secrets in every log.

    Scenarios: D-LOGS-BUNDLE
    """
    fork_dir = e2e.fork.local_dir / "Fork"
    (fork_dir / "accounts.json").write_text('{"token": "ghp_E2EACCOUNTSJSON000000000000000000"}', encoding="utf-8")
    with open(fork_dir / "logs" / "fork.log", "a", encoding="utf-8") as handle:
        handle.write("E2E https://user:supersecretpw@example.invalid/x.git ghp_abcdefghijklmnopqrstuvwxyz0123456789\n")
    e2e.cli("logs", "--bundle")
    bundles = sorted(e2e.root.glob("fork-linux-logs-*.tar.gz"))
    assert bundles
    with tarfile.open(bundles[-1]) as archive:
        names = archive.getnames()
        assert not any(name.endswith("accounts.json") for name in names)
        text = b"".join(
            archive.extractfile(member).read() for member in archive.getmembers() if member.isfile()
        )
    assert b"supersecretpw" not in text
    assert b"ghp_abcdefghijklmnopqrstuvwxyz" not in text
    (fork_dir / "accounts.json").unlink()
