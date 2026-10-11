"""Setup beyond the first run: the winetricks cache, repairing one step, ``--reset`` (declined, then
rebuilt offline from the cache and interrupted in the .NET step), a resumed download, the
``custom`` / ``system`` / ``flatpak`` Wine providers, a second prefix and ``--latest``.

These change the main prefix, so they run after every UI module and before uninstall.
Gated by ``FL_E2E_FORK=1``; containers only.
"""

from __future__ import annotations

import json
import os
import pty
import re
import select
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from fork_driver import fork_pids, fork_version, make_repo, wait_until

from fixtures import real_tier

if os.environ.get("FL_E2E_FORK") != "1":
    pytest.skip("requires FL_E2E_FORK=1 (the real setup in a container)", allow_module_level=True)
real_tier.enforce()

DECLINED = 18
SETUP_TIMEOUT = 3600.0


def _files(directory: Path) -> dict[str, tuple[int, int]]:
    """``relative path -> (size, mtime_ns)`` of every file below ``directory``."""
    return {str(path.relative_to(directory)): (path.stat().st_size, path.stat().st_mtime_ns)
            for path in sorted(directory.rglob("*")) if path.is_file()}


def test_winetricks_cache_is_reused_and_left_alone(closed: Any) -> None:
    """Setup reused the .NET and font installers of ~/.cache/winetricks and never changed that folder.

    Scenarios: S-WINETRICKS-CACHE
    """
    e2e = closed
    legacy = e2e.home / ".cache" / "winetricks"
    ours = e2e.home / ".cache" / "fork-linux" / "winetricks"
    if not os.environ.get("FL_E2E_WINETRICKS_CACHE"):
        pytest.skip("no winetricks cache was seeded (FL_E2E_WINETRICKS_CACHE)")
    seeded = _files(Path(os.environ["FL_E2E_WINETRICKS_CACHE"]))
    assert {name: size for name, (size, _mtime) in _files(legacy).items()} == \
        {name: size for name, (size, _mtime) in seeded.items()}
    assert any(name.startswith("dotnet48") for name in _files(ours)), "W_CACHE has no dotnet48 installer"


def test_setup_repairs_one_step(closed: Any) -> None:
    """``setup --only STEP --force`` re-runs exactly that step (and the always-run ones).

    Scenarios: S-STEP-REPAIR
    """
    report = json.loads(closed.cli(*closed.setup_args(), "--only", "fork_settings", "--force", "--json").stdout)
    assert "fork_settings" in report["ran"]
    assert not {"wine_runtime", "dotnet", "fork_install"} & set(report["ran"])
    assert report["complete"] is True


class _Terminal:
    """fork-linux on a pseudo-terminal (the reset question needs a person to answer it)."""

    def __init__(self, user: Any, args: tuple[str, ...]) -> None:
        self.master, slave = pty.openpty()
        self.process = subprocess.Popen([sys.executable, "-m", "fork_linux", *args], env=user.env,
                                        cwd=str(user.root), stdin=slave, stdout=slave, stderr=slave,
                                        close_fds=True, start_new_session=True)
        os.close(slave)
        self.output = b""

    def pump(self, timeout: float = 0.5) -> None:
        ready, _, _ = select.select([self.master], [], [], timeout)
        if ready:
            try:
                self.output += os.read(self.master, 65536)
            except OSError:
                pass

    def wait_for(self, text: bytes, timeout: float = 120) -> None:
        deadline = time.monotonic() + timeout
        while text not in self.output:
            if time.monotonic() > deadline or self.process.poll() is not None:
                self.pump(0.1)
                if text in self.output:
                    return
                raise AssertionError(f"{text!r} never appeared: {self.output.decode(errors='replace')[-2000:]}")
            self.pump()

    def send(self, data: bytes) -> None:
        os.write(self.master, data)

    def close(self) -> str:
        while self.process.poll() is None:
            self.pump()
        self.pump(0.1)
        os.close(self.master)
        return self.output.decode("utf-8", errors="replace")


def test_reset_answered_no_keeps_the_prefix(closed: Any) -> None:
    """``setup --reset`` warns about Fork's license activations; answering no deletes nothing.

    Scenarios: S-RESET
    """
    e2e = closed
    marker = e2e.prefix / "drive_c" / "e2e-reset-marker"
    marker.write_text("keep me\n", encoding="utf-8")
    terminal = _Terminal(e2e, ("setup", "--reset", "--accept-fork-eula"))
    terminal.wait_for(b"[y/N]")
    assert b"3 activations" in terminal.output
    terminal.send(b"n\n")
    output = terminal.close()
    assert terminal.process.returncode == DECLINED, output
    assert marker.is_file()
    marker.unlink()


def _setup_steps_running(e2e: Any) -> set[str]:
    state = e2e.prefix / ".fork-linux" / "state.json"
    try:
        return set(json.loads(state.read_text(encoding="utf-8")).get("steps", {}))
    except (OSError, ValueError):
        return set()


def test_reset_rebuilds_offline_and_resumes_after_an_interrupted_dotnet(closed: Any) -> None:
    """``--offline setup --reset`` rebuilds the prefix from the cache alone; interrupted in the .NET
    step, the next run resumes there instead of starting over.

    Scenarios: S-RESET, S-OFFLINE-CACHE, S-RESUME-DOTNET
    """
    e2e = closed
    terminal = _Terminal(e2e, ("--offline", "setup", "--reset", "--accept-fork-eula", "--no-gui",
                               "--fork-version", fork_version()))
    terminal.wait_for(b"[y/N]")
    terminal.send(b"y\n")
    wait_until(lambda: "winetricks" in _setup_steps_running(e2e) and "dotnet" not in _setup_steps_running(e2e)
               and _winetricks_dotnet_running(), 900, "the .NET installation to start")
    time.sleep(20)
    terminal.process.send_signal(signal.SIGINT)
    output = terminal.close()
    assert terminal.process.returncode != 0, output
    assert "dotnet" not in _setup_steps_running(e2e)
    assert "wine_runtime" in _setup_steps_running(e2e)
    resumed = e2e.cli("--offline", *e2e.setup_args(), "--json", timeout=SETUP_TIMEOUT)
    ran = json.loads(resumed.stdout)["ran"]
    assert "dotnet" in ran
    assert "wine_runtime" not in ran and "consent" not in ran, ran
    assert e2e.setup_complete()
    report = json.loads(e2e.cli("doctor", "--json").stdout)
    assert not [check for check in report["checks"] if check["status"] == "fail"]
    assert e2e.fork.start(make_repo(e2e.repos / "after-reset", {".gitignore": "*.tmp\n"})).is_main


def _winetricks_dotnet_running() -> bool:
    from fork_driver import processes

    return bool(processes(lambda cmd: "winetricks" in cmd and "dotnet" in cmd))


def test_interrupted_wine_download_resumes(e2e: Any) -> None:
    """A Wine download stopped half way resumes from where it stopped (HTTP Range).

    Scenarios: S-RESUME-DOWNLOAD
    """
    user = e2e.variant("resume-download")
    downloads = user.home / ".cache" / "fork-linux" / "downloads"
    process = subprocess.Popen([sys.executable, "-m", "fork_linux", "setup", "--accept-fork-eula", "--no-gui",
                                "--only", "wine_runtime"], env=user.env, cwd=str(user.root),
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               start_new_session=True)
    try:
        part = wait_until(lambda: next((path for path in downloads.glob("wine-*.part")
                                        if path.stat().st_size > 4 * 1024 * 1024), None), 300,
                          "a partial Wine download")
        process.send_signal(signal.SIGINT)
        process.wait(timeout=60)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=30)
    kept = part.stat().st_size
    assert kept > 0 and part.is_file(), "the partial download must be kept"
    user.cli("setup", "--accept-fork-eula", "--no-gui", "--only", "wine_runtime", timeout=1200)
    log = "\n".join(path.read_text(encoding="utf-8", errors="replace")
                    for path in (user.home / ".local" / "state" / "fork-linux" / "logs").glob("*.log"))
    assert re.search(r"resuming wine-\S+\.part at byte [1-9]", log), "the partial download was not resumed"
    assert not list(downloads.glob("wine-*.part"))
    assert list((user.home / ".local" / "share" / "fork-linux" / "runtimes" / "wine").glob("*/.complete"))


def _managed_wine(e2e: Any) -> Path:
    return next(iter(sorted((e2e.home / ".local" / "share" / "fork-linux" / "runtimes" / "wine").glob("*/bin/wine"))))


def _fork_wine_binary(e2e: Any) -> str:
    """The wine loader the running Fork.exe was started by (its WINELOADER)."""
    pid = fork_pids(e2e.prefix)[0]
    environ = Path("/proc", str(pid), "environ").read_bytes().split(b"\0")
    return next((item.split(b"=", 1)[1].decode() for item in environ if item.startswith(b"WINELOADER=")), "")


def test_custom_wine_path_and_back(closed: Any) -> None:
    """``setup --wine /path/to/wine`` (a custom provider) runs Fork with that Wine; ``managed`` goes back.

    Scenarios: S-WINE-CUSTOM
    """
    e2e = closed
    custom = e2e.root / "custom-wine"
    if not custom.exists():
        custom.symlink_to(_managed_wine(e2e).parent.parent, target_is_directory=True)
    e2e.cli(*e2e.setup_args(), "--wine", str(custom / "bin" / "wine"), timeout=SETUP_TIMEOUT)
    status = json.loads(e2e.cli("status", "--json").stdout)
    assert status["wine_provider"] == str(custom / "bin" / "wine")
    e2e.fork.start(make_repo(e2e.repos / "custom-wine-repo", {".gitignore": "*.tmp\n"}))
    assert _fork_wine_binary(e2e).startswith(str(custom))
    e2e.fork.close()
    e2e.cli(*e2e.setup_args(), "--wine", "managed", timeout=SETUP_TIMEOUT)
    assert json.loads(e2e.cli("status", "--json").stdout)["wine_provider"] == "managed"


def test_system_wine_provider(closed: Any) -> None:
    """``--wine system`` uses the Wine found on PATH (here a Wine 11 build installed under /opt-like dir).

    Scenarios: S-WINE-SYSTEM
    """
    e2e = closed
    bin_dir = e2e.root / "system-bin"
    bin_dir.mkdir(exist_ok=True)
    managed = _managed_wine(e2e).parent
    for name in ("wine", "wineserver", "wineboot", "winecfg", "wine64"):
        if (managed / name).exists() and not (bin_dir / name).exists():
            (bin_dir / name).symlink_to(managed / name)
    extra = {"PATH": f"{bin_dir}:{e2e.env['PATH']}"}
    e2e.cli(*e2e.setup_args(), "--wine", "system", extra=extra, timeout=SETUP_TIMEOUT)
    assert json.loads(e2e.cli("status", "--json", extra=extra).stdout)["wine_provider"] == "system"
    report = json.loads(e2e.cli("doctor", "--json", "--check", "wine", extra=extra, check=False).stdout)
    assert not [check for check in report["checks"] if check["status"] == "fail"], report
    e2e.fork.start(make_repo(e2e.repos / "system-wine-repo", {".gitignore": "*.tmp\n"}), extra=extra)
    assert _fork_wine_binary(e2e).startswith(str(bin_dir)) or _fork_wine_binary(e2e).startswith(str(managed))
    e2e.fork.close()
    e2e.cli(*e2e.setup_args(), "--wine", "managed", timeout=SETUP_TIMEOUT)


def test_flatpak_provider_outside_the_flatpak_is_refused(closed: Any) -> None:
    """``--wine flatpak`` outside the Flatpak build is refused with a clear message; nothing changes.

    Scenarios: S-WINE-FLATPAK
    """
    e2e = closed
    result = e2e.cli("setup", "--accept-fork-eula", "--no-gui", "--wine", "flatpak", check=False)
    assert result.returncode != 0
    assert "Flatpak" in result.stderr
    assert json.loads(e2e.cli("status", "--json").stdout)["wine_provider"] == "managed"


def test_a_second_prefix_is_adopted_and_kept_apart(closed: Any) -> None:
    """``FORK_LINUX_PREFIX`` with ``FORK_LINUX_ADOPT_PREFIX=1`` makes a prefix outside the data dir ours.

    Scenarios: S-CUSTOM-PREFIX
    """
    e2e = closed
    prefix2 = e2e.root / "prefix2"
    prefix2.mkdir(mode=0o700, exist_ok=True)
    extra = {"FORK_LINUX_PREFIX": str(prefix2), "FORK_LINUX_ADOPT_PREFIX": "1"}
    e2e.cli("setup", "--accept-fork-eula", "--no-gui", "--only", "consent", extra=extra)
    assert (prefix2 / ".fork-linux" / "created-by").is_file()
    refused = e2e.cli("status", extra={"FORK_LINUX_PREFIX": str(e2e.root / "not-ours")}, check=False)
    assert refused.returncode == 2
    assert json.loads(e2e.cli("status", "--json", extra=extra).stdout)["prefix"] == str(prefix2)
    assert json.loads(e2e.cli("status", "--json").stdout)["prefix"] == str(e2e.prefix)


def test_latest_installs_the_newest_fork(closed: Any) -> None:
    """``setup --latest`` installs the newest Fork of Fork's feed, verified against the feed (TOFU).

    Scenarios: S-LATEST
    """
    e2e = closed
    check = json.loads(e2e.cli("update", "--check", "--json").stdout)
    newest = check["latest"]
    assert newest, check
    report = json.loads(e2e.cli("setup", "--accept-fork-eula", "--no-gui", "--latest", "--allow-untested",
                                "--json", timeout=SETUP_TIMEOUT).stdout)
    assert report["fork_version"] == newest
    assert e2e.fork.start(make_repo(e2e.repos / "latest-repo", {".gitignore": "*.tmp\n"})).is_main
