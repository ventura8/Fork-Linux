"""Setup's refusals, each for a new user (an empty HOME under ``<root>/variants``): no consent,
missing host tools or downloader, offline without a cache, a bad Fork version, a held lock.

These run first: none of them downloads anything or needs the main prefix. Gated by
``FL_E2E_FORK=1``; containers only.
"""

from __future__ import annotations

import os
import pty
import select
import shutil
import subprocess
import sys
import time
from typing import Any

import pytest

from fixtures import real_tier

if os.environ.get("FL_E2E_FORK") != "1":
    pytest.skip("requires FL_E2E_FORK=1 (the real setup in a container)", allow_module_level=True)
real_tier.enforce()

ACCEPT = ("setup", "--accept-fork-eula", "--no-gui")
DECLINED = 18
DOWNLOAD_FAILED = 12
LOCKED = 16
USAGE = 2
SETUP_FAILED = 11


def _nothing_downloaded(user: Any) -> None:
    """No runtime, no prefix and no download under the user's directories."""
    data = user.home / ".local" / "share" / "fork-linux"
    assert not (data / "runtimes").exists()
    assert not (data / "prefix" / "drive_c").exists()
    downloads = user.home / ".cache" / "fork-linux" / "downloads"
    assert not downloads.exists() or not any(downloads.iterdir())


def _tools_dir(user: Any, tools: tuple[str, ...]) -> str:
    """A PATH directory holding only ``tools`` (links to the container's own programs)."""
    target = user.home.parent / "bin"
    target.mkdir()
    for tool in tools:
        found = shutil.which(tool)
        assert found, f"{tool} is not installed in the E2E image"
        (target / tool).symlink_to(found)
    return str(target)


def test_no_consent_and_nobody_to_ask_downloads_nothing(e2e: Any) -> None:
    """Without --accept-fork-eula and with nobody to ask, setup stops before any download.

    Scenarios: S-CONSENT-REQUIRED
    """
    user = e2e.variant("no-consent")
    result = user.cli("setup", "--no-gui", check=False, timeout=120)
    assert result.returncode == DECLINED, result.stderr
    assert "license" in result.stderr.lower()
    assert "--accept-fork-eula" in result.stderr
    _nothing_downloaded(user)


class Terminal:
    """fork-linux on a pseudo-terminal: read what it prints, answer its prompts."""

    def __init__(self, user: Any, args: tuple[str, ...]) -> None:
        self.master, slave = pty.openpty()
        self.process = subprocess.Popen([sys.executable, "-m", "fork_linux", *args], env=user.env,
                                        cwd=str(user.root), stdin=slave, stdout=slave, stderr=slave,
                                        close_fds=True, start_new_session=True)
        os.close(slave)
        self.output = b""

    def _read(self, timeout: float) -> bool:
        ready, _, _ = select.select([self.master], [], [], timeout)
        if not ready:
            return self.process.poll() is None
        try:
            chunk = os.read(self.master, 4096)
        except OSError:
            return False
        self.output += chunk
        return bool(chunk)

    def wait_for(self, text: bytes, timeout: float = 120) -> None:
        """Read until ``text`` was printed."""
        deadline = time.monotonic() + timeout
        while text not in self.output:
            if time.monotonic() > deadline or not self._read(0.5):
                raise AssertionError(f"{text!r} never appeared; output: {self.text()}")

    def answer(self, line: bytes) -> tuple[int, str]:
        """Type ``line``; return the exit status and everything printed."""
        os.write(self.master, line)
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline and self._read(0.5):
            pass
        status = self.process.wait(timeout=60)
        os.close(self.master)
        return status, self.text()

    def text(self) -> str:
        return self.output.decode("utf-8", errors="replace")


def test_license_on_a_terminal_and_no_stops(e2e: Any) -> None:
    """On a terminal, setup shows Fork's license and the download list; answering no stops it.

    Scenarios: S-CONSENT-REQUIRED
    """
    user = e2e.variant("terminal-no")
    terminal = Terminal(user, ("setup",))
    terminal.wait_for(b"[y/N]")
    status, output = terminal.answer(b"n\n")
    assert status == DECLINED, output
    assert "https://git-fork.com/license" in output
    assert "cdn.fork.dev" in output
    assert "nothing was downloaded" in output
    _nothing_downloaded(user)


def test_missing_host_tools_are_named_with_their_packages(e2e: Any) -> None:
    """Without cabextract, setup stops at its first step and names the package to install.

    Scenarios: S-MISSING-TOOLS
    """
    user = e2e.variant("no-cabextract")
    path = _tools_dir(user, ("unzip", "curl", "git"))
    result = user.cli(*ACCEPT, extra={"PATH": path}, check=False, timeout=120)
    assert result.returncode == SETUP_FAILED, result.stderr
    assert "cabextract" in result.stderr
    assert "apt install" in result.stderr or "apt-get install" in result.stderr
    _nothing_downloaded(user)


def test_missing_downloader_is_named(e2e: Any) -> None:
    """Without curl, wget or aria2c (winetricks needs one), setup names curl.

    Scenarios: S-MISSING-DOWNLOADER
    """
    user = e2e.variant("no-downloader")
    path = _tools_dir(user, ("unzip", "cabextract", "git"))
    result = user.cli(*ACCEPT, extra={"PATH": path}, check=False, timeout=120)
    assert result.returncode == SETUP_FAILED, result.stderr
    assert "curl" in result.stderr
    _nothing_downloaded(user)


def test_offline_with_an_empty_cache_names_the_file(e2e: Any) -> None:
    """``--offline`` with nothing cached fails with exit 12 and names what it would download.

    Scenarios: S-OFFLINE-MISSING
    """
    user = e2e.variant("offline-empty")
    result = user.cli("--offline", *ACCEPT, check=False, timeout=300)
    assert result.returncode == DOWNLOAD_FAILED, result.stderr
    assert "offline" in result.stderr.lower()
    assert "wine-" in result.stderr


def test_unpinned_or_malformed_fork_version_is_refused_first(e2e: Any) -> None:
    """A malformed --fork-version is a usage error; an unpinned one needs --allow-untested; nothing runs.

    Scenarios: S-FORK-VERSION
    """
    user = e2e.variant("bad-version")
    malformed = user.cli(*ACCEPT, "--fork-version", "2.23.2; rm -rf /", check=False, timeout=120)
    assert malformed.returncode == USAGE, malformed.stderr
    unpinned = user.cli(*ACCEPT, "--fork-version", "9.98.7", check=False, timeout=120)
    assert unpinned.returncode != 0
    assert "--allow-untested" in unpinned.stderr
    _nothing_downloaded(user)
    # Refused before the first step: not even the consent was recorded.
    assert not (user.home / ".local" / "share" / "fork-linux" / "prefix" / ".fork-linux" / "state.json").exists()


def test_a_second_setup_while_one_runs_is_locked(e2e: Any) -> None:
    """While one setup waits at its license prompt (holding the setup lock), a second exits 16.

    Scenarios: S-LOCKED
    """
    user = e2e.variant("locked")
    first = Terminal(user, ("setup",))
    first.wait_for(b"[y/N]")
    second = user.cli(*ACCEPT, check=False, timeout=60)
    status, _output = first.answer(b"n\n")
    assert second.returncode == LOCKED, second.stderr
    assert "lock" in second.stderr.lower()
    assert status == DECLINED
    _nothing_downloaded(user)
