"""The real thing: setup, doctor, launch, tabs, settings, rollback and purge with the official Fork.

The tests run in file order and build on each other (one prefix, one Fork
session at a time); after a failed step ``conftest.py`` makes the later ones
fail fast. Gated by ``FL_E2E_FORK=1`` in ``conftest.py``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tarfile
import time
from pathlib import Path
from typing import Any

import pytest

from fixtures import fork_ui as ui
from fixtures import real_tier
from fork_linux import manifest, versions

if os.environ.get("FL_E2E_FORK") != "1":
    pytest.skip(
        "requires FL_E2E_FORK=1 (real Wine + the official Fork download, ~2.5 GB disk, Xvfb and xdotool)",
        allow_module_level=True,
    )
# Containers only (AGENTS.md hard rule 18): scripts/e2e-docker.sh pytest tests/e2e.
real_tier.enforce()



def _target_version() -> str:
    """``FL_E2E_FORK_VERSION`` (upstream-watch: the version it just found), else the manifest default."""
    wanted = os.environ.get("FL_E2E_FORK_VERSION", "").strip()
    if not wanted:
        return manifest.load().fork_default
    if not versions.is_valid(wanted):
        raise pytest.UsageError(f"FL_E2E_FORK_VERSION={wanted!r} is not a Fork version")
    return wanted


FORK_VERSION = _target_version()
# Every setup call names the version, so a re-run sees the same inputs.
SETUP = ("setup", "--accept-fork-eula", "--no-gui", "--fork-version", FORK_VERSION)
SETUP_TIMEOUT = 3600.0
NOOP_SETUP_SECONDS = 10.0
FAST_PATH_SECONDS = 30.0
MIN_STDDEV = 0.02
DARK_MEAN = 0.35

# -- the scenario --------------------------------------------------------------------------------


def test_01_setup_completes_and_resumes(e2e: Any) -> None:
    started = time.monotonic()
    e2e.cli(*SETUP, timeout=SETUP_TIMEOUT)
    (e2e.logs / "timing-setup.txt").write_text(f"{time.monotonic() - started:.1f}\n", encoding="utf-8")
    listed = json.loads(e2e.cli("setup", "--list-steps", "--json").stdout)
    assert listed["complete"] is True
    assert {row["status"] for row in listed["steps"]} <= {"done", "always"}
    started = time.monotonic()
    report = json.loads(e2e.cli(*SETUP, "--json").stdout)
    assert report["ran"] == ["preflight", "finalize"]
    assert time.monotonic() - started < NOOP_SETUP_SECONDS


def test_02_doctor_has_no_failures(e2e: Any) -> None:
    report = json.loads(e2e.cli("doctor", "--deep", "--json").stdout)
    failed = [check for check in report["checks"] if check["status"] == "fail"]
    assert failed == []
    warned = {check["id"] for check in report["checks"] if check["status"] == "warn"}
    assert warned == set(), f"unexpected warnings: {warned}"
    # The image's Noto fonts are mapped in the copy of WPF's composite font .NET 4.8 reads.
    fallback = next(check for check in report["checks"] if check["id"] == "prefix.font_fallback")
    assert "(WPF\\; " in fallback["detail"]
    assert "CJK: Noto Sans CJK JP first" in fallback["detail"]


def test_03_first_launch_opens_the_repository(e2e: Any) -> None:
    demo = ui.make_repo(e2e.repos / "demo")
    ui.make_repo(e2e.repos / "second")
    ui.launch(e2e, demo)
    window = ui.wait_main_window(e2e, first_run=True)
    time.sleep(5)
    shot = ui.screenshot(e2e, "03-main")
    assert ui.image_stat(e2e, shot, "standard_deviation") > MIN_STDDEV
    assert ui.fork_pids(e2e.prefix)
    # settings.json was seeded before the first start and Fork kept the Wine-safe values.
    settings = ui.settings(e2e, e2e.prefix)
    assert settings["UpdateSubmodulesOnCheckout"] is False
    assert settings["DisableHardwareAcceleration"] is True
    assert window["width"] >= ui.MAIN_MIN_WIDTH


def test_04_second_tab_and_fork_alias(e2e: Any) -> None:
    started = time.monotonic()
    second = ui.launch(e2e, e2e.repos / "second")
    assert second.wait(timeout=FAST_PATH_SECONDS) == 0
    assert time.monotonic() - started < FAST_PATH_SECONDS
    alias = subprocess.run(
        [sys.executable, "-c",
         "import sys; from fork_linux.cli import main; raise SystemExit(main([sys.argv[1]], prog='fork'))",
         str(e2e.repos / "demo")],
        env=e2e.env, cwd=str(e2e.root), capture_output=True, timeout=FAST_PATH_SECONDS, check=False,
    )
    assert alias.returncode == 0, alias.stderr
    assert len(ui.fork_pids(e2e.prefix)) == 1, "the second launches must hand over to the running Fork"


def test_05_relaunch_applies_dark_theme(e2e: Any) -> None:
    ui.close_fork(e2e, e2e.prefix)
    ui.launch(e2e, e2e.repos / "demo", extra={"FORK_LINUX_DISPLAY_THEME": "dark"})
    window = ui.wait_main_window(e2e)
    settings = ui.settings(e2e, e2e.prefix)
    assert settings["Theme"] == 1
    assert settings["FollowSystemTheme"] is False
    assert settings["UpdateSubmodulesOnCheckout"] is False
    assert settings["DisableHardwareAcceleration"] is True
    assert isinstance(settings["LayoutScaling"], int)
    time.sleep(5)
    shot = ui.screenshot(e2e, "05-dark", window)
    assert ui.image_stat(e2e, shot, "mean") < DARK_MEAN


def test_07_snapshot_and_rollback(e2e: Any) -> None:
    listing = e2e.cli("snapshot", "list", "--json")
    assert FORK_VERSION in listing.stdout
    refused = e2e.cli("rollback", "--to-version", FORK_VERSION, "--no-pin", check=False)
    assert refused.returncode == 15, "rollback must refuse while Fork runs"
    ui.close_fork(e2e, e2e.prefix)
    e2e.cli("snapshot", "create")
    e2e.cli("rollback", "--to-version", FORK_VERSION, "--no-pin")
    ui.launch(e2e, e2e.repos / "demo")
    window = ui.wait_main_window(e2e)
    assert window["width"] >= ui.MAIN_MIN_WIDTH
    ui.close_fork(e2e, e2e.prefix)


def test_10_logs_bundle_and_purge_second_prefix(e2e: Any) -> None:
    prefix2 = e2e.root / "prefix2"
    prefix2.mkdir(mode=0o700)
    extra = {"FORK_LINUX_PREFIX": str(prefix2), "FORK_LINUX_ADOPT_PREFIX": "1"}
    # Only the host-side steps: enough for fork-linux to mark the prefix as its own.
    e2e.cli("setup", "--accept-fork-eula", "--no-gui", "--only", "consent", extra=extra)
    assert (prefix2 / ".fork-linux" / "created-by").is_file()

    user = os.environ.get("USER") or Path.home().name
    fork_dir = e2e.prefix / "drive_c" / "users" / user / "AppData" / "Local" / "Fork"
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

    e2e.cli("uninstall", extra=extra)
    assert not (e2e.home / ".local" / "share" / "applications" / "io.github.ventura8.ForkLinux.desktop").exists()
    e2e.cli("uninstall", "--purge", "--yes", extra=extra)
    assert not prefix2.exists()
    assert not (e2e.home / ".local" / "share" / "fork-linux").exists()
    assert (e2e.home / ".wine" / "SENTINEL").is_dir(), "~/.wine must never be touched"
    assert (e2e.home / ".cache" / "winetricks").is_dir() or not os.environ.get("FL_E2E_WINETRICKS_CACHE")
