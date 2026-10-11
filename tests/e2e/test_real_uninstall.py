"""Uninstall, last: the plain uninstall keeps Fork, ``--purge`` asks first (license activations),
refuses while Fork runs, can keep the downloads, and deletes only what fork-linux created -
never ``~/.wine``, the user's clones or winetricks' own cache.

Gated by ``FL_E2E_FORK=1``; containers only.
"""

from __future__ import annotations

import json
import os
import pty
import select
import subprocess
import sys
import time
from typing import Any

import pytest
from fork_driver import fork_pids, make_repo

from fixtures import real_tier

if os.environ.get("FL_E2E_FORK") != "1":
    pytest.skip("requires FL_E2E_FORK=1 (the real install in a container)", allow_module_level=True)
real_tier.enforce()

CHAIN = True
FORK_RUNNING = 15
DECLINED = 18
APP_ID = "io.github.ventura8.ForkLinux"


def test_uninstall_keeps_the_prefix_and_fork(installed: Any) -> None:
    """``uninstall`` removes the menu entry and actions; Fork, its prefix and settings stay and still start.

    Scenarios: U-KEEP-PREFIX
    """
    e2e = installed
    e2e.fork.ensure_closed()
    e2e.cli("desktop", "install", "--file-managers", "all")
    entry = e2e.home / ".local" / "share" / "applications" / f"{APP_ID}.desktop"
    assert entry.is_file()
    result = json.loads(e2e.cli("uninstall", "--json").stdout)
    assert result["purged"] is False
    assert str(entry) in result["integration_removed"]
    assert not entry.exists()
    assert (e2e.prefix / ".fork-linux" / "created-by").is_file()
    assert e2e.fork.start(make_repo(e2e.repos / "after-uninstall", {".gitignore": "*.tmp\n"})).is_main


def test_purge_refuses_while_fork_runs(installed: Any) -> None:
    """``uninstall --purge --yes`` while Fork runs exits 15 and deletes nothing.

    Scenarios: U-FORK-RUNNING
    """
    e2e = installed
    if not fork_pids(e2e.prefix):
        e2e.fork.start(make_repo(e2e.repos / "purge-running", {".gitignore": "*.tmp\n"}))
    result = e2e.cli("uninstall", "--purge", "--yes", check=False)
    assert result.returncode == FORK_RUNNING, result.stderr
    assert e2e.prefix.is_dir()
    assert (e2e.home / ".local" / "share" / "fork-linux" / "runtimes").is_dir()
    e2e.fork.close()


def test_purge_shows_the_license_warning_and_needs_the_word(installed: Any) -> None:
    """``uninstall --purge`` warns about license activations and deletes nothing unless "purge" is typed.

    Scenarios: U-LICENSE-WARNING
    """
    e2e = installed
    master, slave = pty.openpty()
    process = subprocess.Popen([sys.executable, "-m", "fork_linux", "uninstall", "--purge"], env=e2e.env,
                               cwd=str(e2e.root), stdin=slave, stdout=slave, stderr=slave, close_fds=True,
                               start_new_session=True)
    os.close(slave)
    output = b""
    deadline = time.monotonic() + 60
    answered = False
    while process.poll() is None and time.monotonic() < deadline:
        ready, _, _ = select.select([master], [], [], 0.5)
        if ready:
            try:
                output += os.read(master, 4096)
            except OSError:
                break
        if not answered and b"Type 'purge'" in output:
            os.write(master, b"no\n")
            answered = True
    status = process.wait(timeout=60)
    os.close(master)
    text = output.decode("utf-8", errors="replace")
    assert answered, text
    assert status == DECLINED, text
    assert "activation" in text and "Deactivate" in text
    assert e2e.prefix.is_dir()


def test_purge_keep_downloads_keeps_the_caches(e2e: Any) -> None:
    """``--purge --keep-downloads`` deletes the rest but keeps the downloaded Wine, Fork and .NET.

    Scenarios: U-KEEP-DOWNLOADS
    """
    user = e2e.variant("keep-downloads")
    user.cli("setup", "--accept-fork-eula", "--no-gui", "--only", "consent")
    downloads = user.home / ".cache" / "fork-linux" / "downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    (downloads / "wine-e2e.tar.xz").write_bytes(b"cached download\n")
    winetricks = user.home / ".cache" / "fork-linux" / "winetricks"
    winetricks.mkdir(parents=True, exist_ok=True)
    (winetricks / "dotnet48.exe").write_bytes(b"cached installer\n")
    result = json.loads(user.cli("uninstall", "--purge", "--keep-downloads", "--yes", "--json").stdout)
    assert result["purged"] is True
    assert (downloads / "wine-e2e.tar.xz").is_file()
    assert (winetricks / "dotnet48.exe").is_file()
    assert not (user.home / ".local" / "share" / "fork-linux").exists()


def test_purge_deletes_only_what_we_created(installed: Any) -> None:
    """``uninstall --purge --yes`` deletes the prefixes, runtimes, snapshots, caches, logs and config of
    fork-linux, and nothing else: ``~/.wine``, winetricks' own cache and the user's clones stay.

    Scenarios: U-PURGE, U-NEVER-WINE
    """
    e2e = installed
    e2e.fork.ensure_closed()
    clone = e2e.home / "my-project"
    make_repo(clone, {".gitignore": "*.tmp\n", "README.md": "mine\n"})
    winetricks_cache = e2e.home / ".cache" / "winetricks"
    had_winetricks = winetricks_cache.is_dir()
    prefix2 = e2e.root / "prefix2"
    extra = {"FORK_LINUX_PREFIX": str(prefix2), "FORK_LINUX_ADOPT_PREFIX": "1"}
    if not prefix2.is_dir():
        prefix2.mkdir(mode=0o700)
        e2e.cli("setup", "--accept-fork-eula", "--no-gui", "--only", "consent", extra=extra)
    e2e.cli("uninstall", "--purge", "--yes", extra=extra)
    assert not prefix2.exists()
    e2e.cli("uninstall", "--purge", "--yes")
    for gone in (e2e.home / ".local" / "share" / "fork-linux", e2e.home / ".cache" / "fork-linux",
                 e2e.home / ".local" / "state" / "fork-linux", e2e.home / ".config" / "fork-linux"):
        assert not gone.exists(), f"{gone} is still there"
    assert (e2e.home / ".wine" / "SENTINEL").is_dir(), "~/.wine must never be touched"
    assert (clone / "README.md").read_text(encoding="utf-8") == "mine\n"
    assert winetricks_cache.is_dir() == had_winetricks
