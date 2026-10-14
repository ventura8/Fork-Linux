"""Fork for Linux (unofficial) upgrades with real Wine and the official Fork (docs/qa/UPDATE-SCENARIOS.md, A*).

One prefix of its own (``<root>/app-upgrade``, never another module's), set up by the release
under test, "N-1" (this tree), with Fork 2.23.1 (trust on first use against Fork's feed) so that
Fork can change its own version later. An app upgrade
is a copy of this tree's ``fork_linux`` package with a higher ``VERSION`` and the changes the
scenario names (a step revision, the manifest's revisions), started through the same
``fork-linux`` command on ``PATH`` and with the same helper directory, like a package that
replaces its files in place. The tests run in order and build on each other:

* A3: N bumps ``display_dpi`` and the manifest's revisions: its first launch re-runs that step
  alone, downloads nothing and records the new revisions;
* A5: N+1 (another step revision) is installed while Fork runs: ``fork <repo>`` hands over to
  the running Fork without touching the prefix; the step runs on the next launch after Fork closed;
* A4: the managed Wine build changes to another Kron4ek build (the previous pin, 11.0 staging, still
  listed in the manifest): the old build's wineserver is stopped with its own binary, the prefix is
  updated by the new build (``host_integration`` follows) and Fork runs on it; going back downloads nothing;
* A11: ``uninstall`` without ``--purge``, then the next launch: only the icon and the menu
  entry come back; Fork, its settings and its first-run answers stay;
* A6: with the git bridge on, an upgrade replaces ``fl-bridge-helper`` and the shims while the
  daemon serves the running Fork: the daemon (the old file) keeps answering Fork's git, and
  after Fork closed the next launch copies the new shims and starts the new helper. It needs
  two bridge builds of different versions in ``FL_E2E_BRIDGE_BUILDS`` (``<dir>/1/`` and
  ``<dir>/2/``: ``scripts/build-bridge.sh --out``; the image has no MinGW) and is skipped without;
* A12: while Fork runs, N+2 (another step revision) is installed and Fork updates itself to
  2.23.2 through its own updater (File > Check for Updates..., Restart and Update); the next
  launch re-runs the bumped step and the icon only, never reinstalls the requested 2.23.1, reports
  the version change once and snapshots 2.23.2.

Gated by ``FL_E2E_FORK=1`` and ``FL_E2E_APP_UPGRADE=1``; containers only
(``scripts/e2e-docker.sh --name NAME pytest tests/e2e/test_wine_app_upgrade.py``).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from fixtures import fork_ui as ui
from fixtures import real_tier
from fork_linux import resources, wine_provider, winecmd
from fork_linux import manifest as manifest_mod
from fork_linux.paths import Paths

if os.environ.get("FL_E2E_FORK") != "1" or os.environ.get("FL_E2E_APP_UPGRADE") != "1":
    pytest.skip(
        "requires FL_E2E_FORK=1 and FL_E2E_APP_UPGRADE=1 (real Wine, the official Fork 2.23.1 and 2.23.2, "
        "a second Kron4ek Wine build, Xvfb and xdotool)",
        allow_module_level=True,
    )
# Containers only (AGENTS.md hard rule 18).
real_tier.enforce()

# One scenario in steps that build on each other: after a failed step the later ones fail fast.
CHAIN = True
REPO = Path(__file__).resolve().parents[2]
FORK_OLD = "2.23.1"
FORK_NEW = manifest_mod.load().fork_default
SETUP = ("setup", "--accept-fork-eula", "--no-gui", "--fork-version", FORK_OLD, "--allow-untested")
SETUP_TIMEOUT = 3600.0
FAST_PATH_SECONDS = 30.0
# A second Fork.exe gives its paths to the running one through Fork's single-instance pipe, then exits.
HAND_OVER_SECONDS = 30.0
UPDATE_TIMEOUT = 180.0
# Fork's update dialogs (measured with Fork 2.23.1 / 2.23.2 on 1600x1000, tests/e2e/test_fork_update.py).
READY_TITLE = "Fork update is ready to be installed"
FILE_MENU_CHECK_FOR_UPDATES = (60, 260)  # inside the File menu popup (Alt+F)
READY_RESTART = (91, -35)  # "Restart and Update" (negative: from the far edge)
WINE_A = manifest_mod.load().wine_default
# The other build of A4: the previous default, still listed in the manifest (so going there needs no
# override). Kron4ek 11.19, the newest build when the pin moved to 11.18, is not one Fork runs on:
# Fork 2.23.1 and 2.23.2 exit at start (System.IO.FileLoadException 0x80131047 in WPF, 2026-10-11,
# tests/e2e/RESULTS.md). (11.1 hung for 5 minutes updating the same prefix: a "This application could
# not be started" .NET dialog under wine.inf's Wow64Install.)
WINE_B_ID = "kron4ek-11.0-staging-wow64"
WINE_B = manifest_mod.load().wine_build(WINE_B_ID)
# Steps that download or install something big: a Wine pin change never re-runs them.
HEAVY_STEPS = {"winetricks", "dotnet", "fonts", "ui_font", "fork_download", "fork_install"}
BRIDGE_FILES = ("fl-bridge-helper", "fl-shim.exe", "fl-launch.exe")
DAEMON_EXIT_SECONDS = 30.0
# One Python in every release: like /usr/bin/fork-linux, it stays where it is across upgrades.
LAUNCHER = '#!/bin/sh\nexec python3 -m fork_linux "$@"\n'


class Releases:
    """The release under test and its upgrades: the environment each one runs with."""

    def __init__(self, e2e: Any) -> None:
        self.e2e = e2e
        self.base = e2e.root / "releases"
        bin_dir = e2e.root / "bin"
        bin_dir.mkdir(exist_ok=True)
        launcher = bin_dir / "fork-linux"
        launcher.write_text(LAUNCHER, encoding="utf-8")
        launcher.chmod(0o755)
        common = {"PATH": f"{bin_dir}:{e2e.env['PATH']}", "FORK_LINUX_LIBEXEC_DIR": str(resources.libexec_dir())}
        shims = resources.shims_dir()
        if shims is not None:
            common["FORK_LINUX_SHIMS_DIR"] = str(shims)
        self.current = common

    def make(self, name: str, version: str, *, bump: tuple[str, ...] = (), revisions: bool = False) -> dict[str, str]:
        """A copy of the current release named ``name``: ``VERSION`` ``version``, step ``bump`` revisions + 1."""
        source = Path(self.current.get("PYTHONPATH", str(REPO / "src")))
        root = self.base / name
        target = root / "src" / "fork_linux"
        shutil.copytree(source / "fork_linux", target, ignore=shutil.ignore_patterns("__pycache__"))
        (root / "VERSION").write_text(f"{version}\n", encoding="utf-8")
        for step_id in bump:
            _bump_step(root / "src" / "fork_linux" / "steps", step_id)
        if revisions:
            data = root / "src" / "fork_linux" / "data" / "runtime-manifest.json"
            document = json.loads(data.read_text(encoding="utf-8"))
            document["bootstrap_revision"] += 1
            year, month, number = document["revision"].split(".")
            document["revision"] = f"{year}.{month}.{int(number) + 1}"
            data.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        # The new package's code in the same place: the helpers and shims stay where they are.
        return {**self.current, "PYTHONPATH": str(root / "src")}

    def upgrade(self, env: dict[str, str]) -> None:
        """From now on, ``env``'s release is the installed one."""
        self.current = env

    def cli(self, *args: str, timeout: float = 600, check: bool = True) -> subprocess.CompletedProcess[str]:
        return self.e2e.cli(*args, extra=self.current, timeout=timeout, check=check)

    def json(self, *args: str) -> Any:
        return json.loads(self.cli(*args, "--json").stdout)

    def launch(self, *targets: Path, log_name: str = "") -> subprocess.Popen[bytes]:
        return ui.launch(self.e2e, *targets, extra=self.current, log_name=log_name)


def _bump_step(steps_dir: Path, step_id: str) -> None:
    """``rev=N`` -> ``rev=N+1`` in the ``Step(...)`` of ``step_id`` (exactly one match)."""
    pattern = re.compile(r'(id="' + re.escape(step_id) + r'",\s*title="[^"]*",\s*rev=)(\d+)')
    for module in steps_dir.glob("*.py"):
        text = module.read_text(encoding="utf-8")
        new, count = pattern.subn(lambda match: f"{match.group(1)}{int(match.group(2)) + 1}", text)
        if count:
            assert count == 1, f"{step_id} matched {count} times in {module}"
            module.write_text(new, encoding="utf-8")
            return
    pytest.fail(f"no Step {step_id} with a literal rev in {steps_dir}")


# -- state, files and processes ------------------------------------------------------------------------


def _state(e2e: Any) -> dict[str, Any]:
    return json.loads((e2e.prefix / ".fork-linux" / "state.json").read_text(encoding="utf-8"))


def _markers(e2e: Any) -> dict[str, dict[str, Any]]:
    return _state(e2e)["steps"]


def _rerun(before: dict[str, dict[str, Any]], after: dict[str, dict[str, Any]]) -> list[str]:
    """The steps whose marker was written again (or appeared) between two states."""
    return sorted(step for step, marker in after.items() if before.get(step) != marker)


def _downloads(e2e: Any) -> dict[str, int]:
    """Verified downloads in the cache: name -> size (``.part`` files are not finished downloads)."""
    folder = e2e.home / ".cache" / "fork-linux" / "downloads"
    return {path.name: path.stat().st_size for path in folder.iterdir() if path.is_file() and path.suffix != ".part"}


def _runtimes(e2e: Any) -> list[str]:
    return wine_provider.installed_builds(Paths.from_env(e2e.env))


def _runtime_root(e2e: Any, build_id: str) -> Path:
    return wine_provider.managed_root(Paths.from_env(e2e.env), build_id)


def _wineservers(e2e: Any) -> dict[int, Path]:
    """wineserver processes serving our prefix: pid -> executable."""
    found = {}
    for pid in ui.prefix_pids(e2e.prefix, b"wineserver"):
        try:
            found[pid] = Path(os.readlink(f"/proc/{pid}/exe"))
        except OSError:
            continue
    return found


def _wait_until(condition: Any, timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            pytest.fail(f"timed out after {timeout:.0f} s waiting for {what}")
        time.sleep(0.5)


def _fork_env(pid: int) -> dict[str, str]:
    raw = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
    return dict(item.decode(errors="replace").split("=", 1) for item in raw if b"=" in item)


def _not_due(releases: Releases) -> None:
    """Fork closed: its own automatic update check is not due (``LastUpdateCheck`` now).

    Fork checks by itself about a minute after it starts when the last check is old enough, and on
    its Develop channel downloads 2.23.2 and applies it at the next start (scenarios F23, F24): that
    must not change Fork's version in the middle of these scenarios, only where A12 asks for it.
    """
    releases.cli("settings", "set", "LastUpdateCheck", datetime.now(timezone.utc).isoformat())


def _open_fork(releases: Releases, *targets: Path, name: str) -> dict[str, Any]:
    """Launch the installed release on ``targets`` (Fork closed) and wait for Fork's main window."""
    _not_due(releases)
    releases.launch(*targets, log_name=name)
    window = ui.wait_main_window(releases.e2e)
    time.sleep(3)
    ui.screenshot(releases.e2e, name, window)
    return window


def _log_text(e2e: Any) -> str:
    """Every fork-linux and launch log of this run."""
    folders = [e2e.logs, e2e.home / ".local" / "state" / "fork-linux" / "logs"]
    return "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for folder in folders
        if folder.is_dir()
        for path in sorted(folder.glob("*.log"))
    )


@pytest.fixture(scope="module")
def e2e(e2e: Any) -> Iterator[Any]:
    """This module's own environment in ``<root>/app-upgrade``: a fresh home and prefix with Fork 2.23.1,
    never the prefix another module of the session set up (seeded with the session's verified caches)."""
    env = e2e.child("app-upgrade")
    try:
        yield env
    finally:
        env.stop_wine()


@pytest.fixture(scope="module")
def releases(e2e: Any) -> Releases:
    return Releases(e2e)


# -- the scenario ------------------------------------------------------------------------------------------


def test_01_setup_with_the_previous_fork(e2e: Any, releases: Releases) -> None:
    """N-1 sets up with Fork 2.23.1 (verified against Fork's feed); Fork's first run is answered.

    Scenarios: A3
    """
    releases.cli(*SETUP, timeout=SETUP_TIMEOUT)
    listed = releases.json("setup", "--list-steps")
    assert listed["needs_setup"] is False
    assert _state(e2e)["fork"]["version"] == FORK_OLD
    demo = ui.make_repo(e2e.repos / "demo")
    ui.make_repo(e2e.repos / "second")
    _not_due(releases)
    releases.launch(demo, log_name="01-first-launch")
    ui.wait_main_window(e2e, first_run=True)
    ui.close_fork(e2e, e2e.prefix)
    assert FORK_OLD in releases.cli("snapshot", "list", "--json").stdout


def test_02_a3_upgrade_reruns_only_the_bumped_step(e2e: Any, releases: Releases) -> None:
    """Scenarios: A3"""
    downloads, before = _downloads(e2e), _markers(e2e)
    releases.upgrade(releases.make("n", "1.0.1", bump=("display_dpi",), revisions=True))
    listed = releases.json("setup", "--list-steps")
    assert listed["needs_setup"] is True
    assert [row["id"] for row in listed["steps"] if row["recorded"] is False] == ["display_dpi"]
    _open_fork(releases, e2e.repos / "demo", name="02-a3-after-upgrade")
    after = _markers(e2e)
    assert _rerun(before, after) == ["display_dpi"]
    assert after["display_dpi"]["rev"] == before["display_dpi"]["rev"] + 1
    state = _state(e2e)
    assert state["setup"]["bootstrap_revision"] == listed["revisions"]["bootstrap"]
    assert state["setup"]["manifest_revision"] == listed["revisions"]["manifest"]
    assert state["written_by"] == "1.0.1-dev"
    assert _downloads(e2e) == downloads, "an app upgrade must not download anything again"
    assert releases.json("setup", "--list-steps")["needs_setup"] is False


def test_03_a5_upgrade_while_fork_runs(e2e: Any, releases: Releases) -> None:
    """Scenarios: A5"""
    (pid,) = ui.fork_pids(e2e.prefix)
    before = _markers(e2e)
    releases.upgrade(releases.make("n1", "1.0.2", bump=("fork_settings",)))
    started = time.monotonic()
    handed_over = releases.launch(e2e.repos / "second", log_name="03-a5-fast-path")
    assert handed_over.wait(timeout=FAST_PATH_SECONDS) == 0
    assert time.monotonic() - started < FAST_PATH_SECONDS
    _wait_until(lambda: ui.fork_pids(e2e.prefix) == [pid], HAND_OVER_SECONDS, "the second Fork to hand over and exit")
    assert _markers(e2e) == before, "nothing in the prefix changes while Fork runs"
    ui.close_fork(e2e, e2e.prefix)
    _open_fork(releases, e2e.repos / "demo", name="03-a5-after-close")
    after = _markers(e2e)
    assert _rerun(before, after) == ["fork_settings"]
    assert after["fork_settings"]["rev"] == before["fork_settings"]["rev"] + 1
    ui.close_fork(e2e, e2e.prefix)


def test_04_a4_wine_pin_change_and_revert(e2e: Any, releases: Releases) -> None:
    """Scenarios: A4"""
    releases.cli("config", "set", "wine.build", WINE_B_ID)
    old_root = _runtime_root(e2e, WINE_A.id)
    # Wine's services can keep the previous build's server alive after Fork closed: a persistent one.
    server = old_root / "bin" / "wineserver"
    server_env = {**e2e.env, "WINEPREFIX": str(e2e.prefix)}
    subprocess.run([str(server), "-w"], env=server_env, timeout=120, check=True)  # the one Fork left lingering
    subprocess.run([str(server), "-p"], env=server_env, timeout=60, check=True)
    _wait_until(lambda: server.resolve() in _wineservers(e2e).values(), 30, "the old build's wineserver")
    downloads = _downloads(e2e)
    report = releases.json("setup", "--no-gui")
    # wine.inf takes .js back for its JSFile: host_integration follows every Wine change.
    assert report["ran"] == ["preflight", "wine_runtime", "prefix_init", "host_integration", "finalize"]
    assert server.resolve() not in _wineservers(e2e).values(), "the old build's wineserver was stopped"
    # Downloaded now, or verified again from the cache of an earlier run (--keep keeps home/.cache).
    assert _downloads(e2e).get(Path(WINE_B.url).name) == WINE_B.size
    assert set(_downloads(e2e)) - set(downloads) <= {Path(WINE_B.url).name}
    complete = _runtime_root(e2e, WINE_B_ID) / wine_provider.COMPLETE_MARKER
    assert complete.read_text(encoding="utf-8").strip() == WINE_B.sha256
    assert _runtimes(e2e) == sorted([WINE_A.id, WINE_B_ID]), "the previous build stays on disk"
    new_root = _runtime_root(e2e, WINE_B_ID).resolve()
    assert releases.json("status")["wine_build"] == WINE_B_ID
    # The prefix now runs on B: B's own server answers in it (an old one would refuse B's clients).
    b_env = {**e2e.env, "WINEPREFIX": str(e2e.prefix), "WINEDEBUG": "-all", "WINEDLLOVERRIDES": "winemenubuilder.exe=d"}
    on_b = subprocess.run([str(new_root / "bin" / "wine"), "cmd", "/c", "ver"], env=b_env,
                          capture_output=True, text=True, timeout=120, check=False)
    assert on_b.returncode == 0, on_b.stderr
    subprocess.run([str(new_root / "bin" / "wineserver"), "-k"], env=b_env, timeout=60, check=False)
    _open_fork(releases, e2e.repos / "demo", name="04-a4-on-the-other-build")
    (pid,) = ui.fork_pids(e2e.prefix)
    assert Path(_fork_env(pid)["WINESERVER"]).resolve().parent.parent == new_root
    ui.close_fork(e2e, e2e.prefix)

    releases.cli("config", "unset", "wine.build")
    downloads = _downloads(e2e)
    report = releases.json("setup", "--no-gui")
    # Wine's update back to the older build may reset registry values of ours: the steps whose
    # result no longer verifies run again too, but nothing that downloads or reinstalls.
    assert report['ran'][:3] == ['preflight', 'wine_runtime', 'prefix_init']
    assert report['ran'][-1] == 'finalize'
    assert not set(report["ran"]) & HEAVY_STEPS, report["ran"]
    assert _downloads(e2e) == downloads, "going back to the default build downloads nothing"
    _open_fork(releases, e2e.repos / "demo", name="04-a4-back-on-the-default")
    (pid,) = ui.fork_pids(e2e.prefix)
    assert Path(_fork_env(pid)["WINESERVER"]).resolve().parent.parent == old_root.resolve()
    ui.close_fork(e2e, e2e.prefix)


def test_05_a11_uninstall_then_reinstall_keeps_the_prefix(e2e: Any, releases: Releases) -> None:
    """Scenarios: A11"""
    menu = e2e.home / ".local" / "share" / "applications" / "io.github.ventura8.ForkLinux.desktop"
    assert menu.is_file()
    settings = ui.settings_file(e2e.prefix).read_bytes()
    before = _markers(e2e)
    releases.cli("uninstall")
    assert not menu.exists()
    assert e2e.prefix.is_dir()
    assert ui.settings_file(e2e.prefix).read_bytes() == settings
    assert sorted(set(before) - set(_markers(e2e))) == ["desktop_entry", "icon"]
    listed = releases.json("setup", "--list-steps")
    assert [row["id"] for row in listed["steps"] if row["recorded"] is False] == ["icon", "desktop_entry"]
    # No first-run dialogs: Fork's own data (and its license state) survived the uninstall.
    _open_fork(releases, e2e.repos / "demo", name="05-a11-after-reinstall")
    assert _rerun(before, _markers(e2e)) == ["desktop_entry", "icon"]
    assert menu.is_file()
    ui.close_fork(e2e, e2e.prefix)


def _bridge_builds() -> Path:
    """The two bridge builds of A6 (``FL_E2E_BRIDGE_BUILDS``), or a skip when they are not there."""
    builds = Path(os.environ.get("FL_E2E_BRIDGE_BUILDS") or "/nonexistent")
    if not all((builds / build / name).is_file() for build in ("1", "2") for name in BRIDGE_FILES):
        pytest.skip(
            "A6 needs two bridge builds of different versions in FL_E2E_BRIDGE_BUILDS (<dir>/1, <dir>/2: "
            "scripts/build-bridge.sh --out; the E2E image has no MinGW)"
        )
    return builds


def _install_bridge(build: Path, libexec: Path) -> None:
    """Put ``build``'s helper and shims in place the way a package manager does: new file, then rename."""
    for name in BRIDGE_FILES:
        target = libexec / name if name == "fl-bridge-helper" else libexec / "win64" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        staged = target.with_name(f".{name}.new")
        shutil.copy2(build / name, staged)
        os.replace(staged, target)


def _daemon(releases: Releases) -> dict[str, Any]:
    return releases.json("git-bridge", "status")["daemon"]


def _git_through_the_bridge(e2e: Any, fork_pid: int, repo: Path) -> str:
    """``git rev-parse HEAD`` in ``repo`` through the installed shim, with the running Fork's environment."""
    env = _fork_env(fork_pid)
    shim = e2e.prefix / "drive_c" / "fork-linux" / "gitInstance" / "cmd" / "git.exe"
    result = subprocess.run(
        [env["WINELOADER"], str(shim), "rev-parse", "HEAD"],
        env=env, cwd=str(repo), capture_output=True, text=True, timeout=120, check=False,
    )
    assert result.returncode == 0, f"git through the bridge failed: {result.stdout}{result.stderr}"
    return result.stdout.strip()


def test_06_a6_bridge_binaries_replaced_while_the_daemon_runs(e2e: Any, releases: Releases) -> None:
    """Scenarios: A6"""
    builds = _bridge_builds()
    libexec = e2e.root / "bridge-install" / "lib"
    shutil.copytree(Path(releases.current["FORK_LINUX_LIBEXEC_DIR"]), libexec)
    _install_bridge(builds / "1", libexec)
    shims = {"FORK_LINUX_LIBEXEC_DIR": str(libexec), "FORK_LINUX_SHIMS_DIR": str(libexec / "win64")}
    releases.upgrade({**releases.current, **shims})
    releases.cli("git-bridge", "enable")
    releases.cli("setup", "--no-gui")
    demo = e2e.repos / "demo"
    _open_fork(releases, demo, name="06-a6-bridge-on")
    (fork_pid,) = ui.fork_pids(e2e.prefix)
    daemon = _daemon(releases)
    assert Path(os.readlink(f"/proc/{daemon['pid']}/exe")) == (libexec / "fl-bridge-helper").resolve()
    head = subprocess.run(["git", "-C", str(demo), "rev-parse", "HEAD"], capture_output=True, text=True,
                          timeout=60, check=True).stdout.strip()
    assert _git_through_the_bridge(e2e, fork_pid, demo) == head
    before = _markers(e2e)

    _install_bridge(builds / "2", libexec)  # the upgrade, while Fork and the daemon run
    assert _daemon(releases) == daemon, "the running daemon keeps its port"
    assert os.readlink(f"/proc/{daemon['pid']}/exe").endswith(" (deleted)"), "it still runs the old file"
    assert _git_through_the_bridge(e2e, fork_pid, demo) == head, "Fork's git still goes through the old daemon"
    handed_over = releases.launch(e2e.repos / "second", log_name="06-a6-fast-path")
    assert handed_over.wait(timeout=FAST_PATH_SECONDS) == 0
    _wait_until(lambda: ui.fork_pids(e2e.prefix) == [fork_pid], HAND_OVER_SECONDS, "the second Fork to hand over")
    assert _markers(e2e) == before

    ui.close_fork(e2e, e2e.prefix)
    _wait_until(lambda: not Path(f"/proc/{daemon['pid']}").exists(), DAEMON_EXIT_SECONDS, "the old daemon to exit")
    _open_fork(releases, demo, name="06-a6-new-bridge")
    assert _rerun(before, _markers(e2e)) == ["host_shims"]
    shim = e2e.prefix / "drive_c" / "fork-linux" / "gitInstance" / "cmd" / "git.exe"
    assert shim.read_bytes() == (builds / "2" / "fl-shim.exe").read_bytes()
    restarted = _daemon(releases)
    assert restarted["pid"] != daemon["pid"]
    assert Path(os.readlink(f"/proc/{restarted['pid']}/exe")) == (libexec / "fl-bridge-helper").resolve()
    (fork_pid,) = ui.fork_pids(e2e.prefix)
    assert _git_through_the_bridge(e2e, fork_pid, demo) == head
    ui.close_fork(e2e, e2e.prefix)
    releases.cli("git-bridge", "disable")
    releases.cli("setup", "--no-gui")


def _forks_own_update(e2e: Any) -> None:
    """Fork's own updater, as a user drives it: File > Check for Updates..., then Restart and Update.

    Velopack downloads 2.23.2 from Fork's feed, replaces ``current\\`` and starts the new Fork.
    """
    main = ui.wait_main_window(e2e)
    ui.click(e2e, main, (main["width"] // 2, 8))  # the keyboard to Fork (no window manager)
    before = {window["id"] for window in ui.windows(e2e)}
    ui.xdotool(e2e, "key", "--delay", "90", "alt+f", check=True)
    menu = ui.wait_for(lambda: next((w for w in ui.windows(e2e) if w["id"] not in before), None), 10, "the File menu")
    ui.click(e2e, menu, FILE_MENU_CHECK_FOR_UPDATES)
    ready = ui.wait_for(lambda: ui.find_window(e2e, READY_TITLE), UPDATE_TIMEOUT, "Fork's update to be ready")
    ui.screenshot(e2e, "07-a12-update-ready")
    old = set(ui.fork_pids(e2e.prefix))
    mark = len(_velopack_log(e2e))
    ui.click(e2e, ready, READY_RESTART)
    try:
        ui.wait_for(lambda: set(ui.fork_pids(e2e.prefix)) - old, UPDATE_TIMEOUT, "the Fork Velopack restarted")
        ui.wait_main_window(e2e)
    except pytest.fail.Exception:
        # Velopack applied the update but Wine did not start the new Fork.exe ("Failed to start
        # application (OS Error 731)"): named, so scripts/ci-e2e-wine.sh re-runs the module once.
        failed = [line for line in _velopack_log(e2e)[mark:].splitlines() if "Apply error" in line]
        if failed:
            pytest.fail(f"Velopack could not start the updated Fork: {failed[-1]}")
        raise


def _velopack_log(e2e: Any) -> str:
    """Fork's ``velopack.log`` (empty while there is none)."""
    fork_dir = e2e.prefix / "drive_c" / "users" / winecmd.windows_user(e2e.env) / "AppData" / "Local" / "Fork"
    try:
        return (fork_dir / "velopack.log").read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return ""


def test_07_a12_app_upgrade_and_fork_self_update_on_one_launch(e2e: Any, releases: Releases) -> None:
    """While Fork runs, our package is upgraded and Fork updates itself; the next launch applies both.

    Scenarios: A12
    """
    fork_dir = e2e.prefix / "drive_c" / "users" / winecmd.windows_user(e2e.env) / "AppData" / "Local" / "Fork"
    _open_fork(releases, e2e.repos / "demo", name="07-a12-before")
    before = _markers(e2e)
    releases.upgrade(releases.make("n2", "1.0.3", bump=("host_integration",)))
    _forks_own_update(e2e)
    assert f"<version>{FORK_NEW}</version>" in (fork_dir / "current" / "sq.version").read_text(encoding="utf-8-sig")
    assert _state(e2e)["fork"]["version"] == FORK_OLD, "fork-linux took no part in Fork's update"
    ui.close_fork(e2e, e2e.prefix)
    assert _markers(e2e) == before
    notice = f"Fork updated {FORK_OLD} -> {FORK_NEW}"
    reported = _log_text(e2e).count(notice)
    _open_fork(releases, e2e.repos / "demo", name="07-a12-one-launch")
    after = _markers(e2e)
    assert _rerun(before, after) == ["host_integration", "icon"], "fork_install must not put 2.23.1 back"
    assert _state(e2e)["fork"]["last_seen_version"] == FORK_NEW
    assert _log_text(e2e).count(notice) > reported, "the next launch reports Fork's own update"
    reported = _log_text(e2e).count(notice)
    assert FORK_NEW in releases.cli("snapshot", "list", "--json").stdout
    ui.close_fork(e2e, e2e.prefix)
    _open_fork(releases, e2e.repos / "demo", name="07-a12-next-launch")
    assert _log_text(e2e).count(notice) == reported, "the change is reported once"
    assert _markers(e2e) == after
    ui.close_fork(e2e, e2e.prefix)
