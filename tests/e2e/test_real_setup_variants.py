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
import shutil
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
# .NET Framework release numbers: 4.7.2 is 461808 (461814 on older Windows), 4.8 starts at 528040.
DOTNET_472 = 461808
DOTNET_48 = 528040


def _files(directory: Path) -> dict[str, tuple[int, int]]:
    """``relative path -> (size, mtime_ns)`` of every file below ``directory``."""
    return {str(path.relative_to(directory)): (path.stat().st_size, path.stat().st_mtime_ns)
            for path in sorted(directory.rglob("*")) if path.is_file()}


def test_winetricks_cache_is_reused_and_left_alone(closed: Any) -> None:
    """Setup reuses the installers of ~/.cache/winetricks (hard links into its own cache) and never changes
    that folder. With a seeded cache (``FL_E2E_WINETRICKS_CACHE``, e2e-docker.sh) the session's setup is
    checked; without one (CI's fresh downloads) the font installers setup downloaded become the user's own
    winetricks cache and the ``winver`` step runs winetricks again, which first takes what that cache has.

    Scenarios: S-WINETRICKS-CACHE
    """
    e2e = closed
    legacy = e2e.home / ".cache" / "winetricks"
    ours = e2e.home / ".cache" / "fork-linux" / "winetricks"
    if os.environ.get("FL_E2E_WINETRICKS_CACHE"):
        seeded = _files(Path(os.environ["FL_E2E_WINETRICKS_CACHE"]))
        assert {name: size for name, (size, _mtime) in _files(legacy).items()} == \
            {name: size for name, (size, _mtime) in seeded.items()}
        assert any(name.startswith("dotnet48") for name in _files(ours)), "W_CACHE has no dotnet48 installer"
        return
    installers = sorted(path for path in (ours / "corefonts").iterdir() if path.is_file() and path.stat().st_size)
    assert installers, "setup downloaded no corefonts installer into W_CACHE"
    (legacy / "corefonts").mkdir(parents=True)
    for path in installers:
        shutil.copy2(path, legacy / "corefonts" / path.name)
        path.unlink()
    before = _files(legacy)
    report = json.loads(e2e.cli(*e2e.setup_args(), "--only", "winver", "--force", "--json").stdout)
    assert "winver" in report["ran"]
    assert _files(legacy) == before, "~/.cache/winetricks was changed"
    for path in installers:
        assert os.path.samefile(path, legacy / "corefonts" / path.name), f"{path.name} was not reused"


def test_setup_repairs_one_step(closed: Any) -> None:
    """``setup --only STEP --force`` re-runs exactly that step (and the always-run ones).

    Scenarios: S-STEP-REPAIR
    """
    report = json.loads(closed.cli(*closed.setup_args(), "--only", "fork_settings", "--force", "--json").stdout)
    assert "fork_settings" in report["ran"]
    assert not {"wine_runtime", "dotnet", "fork_install"} & set(report["ran"])
    assert report["complete"] is True


def test_list_steps_and_from_step(closed: Any) -> None:
    """``setup --list-steps`` lists every step in order with its state; ``--from-step STEP`` re-runs that step
    and the ones after it; an unknown step is refused (exit 2) before anything runs.

    Scenarios: S-LIST-STEPS, S-FROM-STEP
    """
    e2e = closed
    listed = json.loads(e2e.cli("setup", "--list-steps", "--json").stdout)
    ids = [row["id"] for row in listed["steps"]]
    assert ids[:3] == ["preflight", "consent", "wine_runtime"] and ids[-1] == "finalize", ids
    assert listed["complete"] is True
    assert {row["status"] for row in listed["steps"]} <= {"done", "always"}, listed["steps"]
    text = e2e.cli("setup", "--list-steps").stdout.splitlines()
    assert [line.split()[1] for line in text] == ids, text
    unknown = e2e.cli("setup", "--only", "no_such_step", check=False)
    assert unknown.returncode == 2 and "unknown setup step(s): no_such_step" in unknown.stderr, unknown.stderr
    assert "ssh_sync" in unknown.stderr, "the refusal lists the steps"
    report = json.loads(e2e.cli(*e2e.setup_args(), "--from-step", "ssh_sync", "--no-launch", "--json").stdout)
    later = ids[ids.index("ssh_sync"):]
    assert set(later) <= set(report["ran"]), report
    assert not set(report["ran"]) & {"wine_runtime", "dotnet", "fork_install", "fork_settings"}, report
    assert report["complete"] is True
    assert not fork_pids(e2e.prefix), "setup never starts Fork"


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
    """``--offline setup --reset`` rebuilds the prefix from the cache alone, ending only Wine's processes
    (a shell or program started with our ``WINEPREFIX`` keeps running); interrupted in the .NET step,
    the next run resumes there instead of starting over.

    Scenarios: S-RESET, S-OFFLINE-CACHE, S-RESUME-DOTNET
    """
    e2e = closed
    # A shell and a program the user started with our WINEPREFIX exported are not Wine's: the reset
    # must not end them (only Wine's processes and winetricks are stopped before the prefix goes).
    bystanders = [
        subprocess.Popen(argv, env={**e2e.env, "WINEPREFIX": str(e2e.prefix)}, start_new_session=True)
        for argv in (["bash", "-c", "sleep 3600"], ["sleep", "3600"])
    ]
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
    try:
        assert [process.poll() for process in bystanders] == [None, None], "the reset ended a non-Wine process"
    finally:
        for process in bystanders:
            process.kill()
            process.wait(timeout=30)
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
    # HTTP 206 resumes the .part; GitHub's release CDN sometimes answers 200 and we restart
    # from zero (unit tests cover 206). Either path must appear after the interrupt.
    resumed = re.search(r"resuming wine-\S+\.part at byte [1-9]", log)
    restarted = re.search(r"restarting wine-\S+\.part \(server returned \d+, not 206\)", log)
    assert resumed or restarted, "the partial download was neither resumed nor restarted:\n" + log
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
    try:
        e2e.cli(*e2e.setup_args(), "--wine", str(custom / "bin" / "wine"), timeout=SETUP_TIMEOUT)
        assert json.loads(e2e.cli("status", "--json").stdout)["wine_provider"] == "custom"
        assert e2e.cli("config", "get", "wine.provider").stdout.strip() == str(custom / "bin" / "wine")
        e2e.fork.start(make_repo(e2e.repos / "custom-wine-repo", {".gitignore": "*.tmp\n"}))
        assert _fork_wine_binary(e2e).startswith(str(custom))
        e2e.fork.close()
    finally:
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
    assert _fork_wine_binary(e2e).startswith(str(bin_dir)), "Fork runs with the wine found on PATH"
    e2e.fork.close()
    e2e.cli(*e2e.setup_args(), "--wine", "managed", timeout=SETUP_TIMEOUT)


def test_flatpak_provider_outside_the_flatpak_is_refused(closed: Any) -> None:
    """``--wine flatpak`` outside the Flatpak build is refused with a clear message; nothing changes.

    Scenarios: S-WINE-FLATPAK
    """
    e2e = closed
    result = e2e.cli("setup", "--accept-fork-eula", "--no-gui", "--wine", "flatpak", check=False)
    assert result.returncode == 14, result.stderr
    assert "Flatpak" in result.stderr
    # The refused choice is not saved: setup and Fork keep working with the managed Wine.
    assert e2e.cli("config", "get", "wine.provider").stdout.strip() == "managed"
    assert e2e.setup_complete()


def test_a_second_prefix_is_adopted_and_kept_apart(closed: Any) -> None:
    """``FORK_LINUX_PREFIX`` with ``FORK_LINUX_ADOPT_PREFIX=1`` makes a prefix outside the data dir ours;
    ``--prefix DIR`` selects it as well.

    Scenarios: S-CUSTOM-PREFIX, S-PREFIX-OPTION
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
    # The global --prefix option selects a prefix too (and wins over FORK_LINUX_PREFIX); ~/.wine never.
    assert json.loads(e2e.cli("--prefix", str(prefix2), "status", "--json").stdout)["prefix"] == str(prefix2)
    chosen = e2e.cli("--prefix", str(e2e.prefix), "status", "--json", extra=extra)
    assert json.loads(chosen.stdout)["prefix"] == str(e2e.prefix)
    wine = e2e.cli("--prefix", str(e2e.home / ".wine"), "status", check=False)
    assert wine.returncode == 2 and "refusing to use" in wine.stderr, wine.stderr


def test_dotnet472_prefix_runs_fork(closed: Any) -> None:
    """``setup --dotnet dotnet472`` builds a prefix with .NET Framework 4.7.2 (not 4.8) in which Fork starts.

    Scenarios: S-DOTNET-472
    """
    user = closed.variant("dotnet472")
    user.cli(*user.setup_args(), "--dotnet", "dotnet472", timeout=SETUP_TIMEOUT)
    report = json.loads(user.cli("doctor", "--json", check=False).stdout)
    dotnet = next(check for check in report["checks"] if check["id"] == "prefix.dotnet")
    release = int(re.search(r"release (\d+)", dotnet["detail"]).group(1))
    assert DOTNET_472 <= release < DOTNET_48, dotnet
    try:
        assert user.fork.start(make_repo(user.repos / "dotnet472-repo", {".gitignore": "*.tmp\n"})).is_main
        user.screen.screenshot("dotnet472-fork")
    finally:
        user.fork.ensure_closed()


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
