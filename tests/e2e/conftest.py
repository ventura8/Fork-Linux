"""Real end-to-end tier: the official Fork under the managed Wine runtime, on an Xvfb display.

Containers only: run it with ``scripts/e2e-docker.sh pytest tests/e2e`` (CI:
``scripts/ci-e2e-wine.sh``). With ``FL_E2E_FORK=1`` outside a container (no ``/.dockerenv`` /
``/run/.containerenv``) pytest exits at once, before anything is created (AGENTS.md hard rule
18; ``tests/fixtures/real_tier.py``); there is no host override.

Gated: nothing here runs unless ``FL_E2E_FORK=1`` (each test module skips itself). The tier downloads (or
reuses) the pinned Wine runtime, winetricks verbs and the official Fork
installer, builds a real prefix (~2.5 GB, ~4-10 minutes) and drives Fork's
windows with ``xdotool`` (``fork_driver.py``). Everything lives under one root directory with a
fake ``HOME`` and XDG directories, so the real home and ``~/.wine`` are never
touched; screenshots stay in ``<root>/shots`` (they show Fork's logo: never
copy them into the repository).

The modules share one prefix and run in ``MODULE_ORDER`` (cheap error paths first, then the
main setup, Fork's UI, and the destructive ones last: setup variants, uninstall). Each test
function names the scenarios of ``tests/e2e/scenarios.json`` it covers on a docstring line
``Scenarios: ID, ID`` (``tests/test_e2e_scenarios.py`` checks both ways). A module that sets
``CHAIN = True`` is one scenario in steps: after a failed step its later tests fail fast.

Safety: tests only ever create or delete files inside the root. A test that makes Fork (a
Windows program, which sees the whole container through ``Z:``) delete files does so in a
repository under ``<root>/repos`` that holds no symlink pointing outside the root.

Environment:

* ``FL_E2E_ROOT``: the root directory (default: a new directory under the
  system temp dir). An existing root is reused: its ``home/.cache`` (verified
  downloads, winetricks cache) is kept and everything else is rebuilt. The root
  must be absolute and symlink-free, and must not be ``/`` or be, contain or lie
  inside a home directory; a non-empty root without the tier's marker is refused.
* ``FL_E2E_REUSE=1``: also keep the previous run's prefix, runtimes and settings (``home/.local``,
  ``home/.config``) so a developer can re-run one module without a new setup
  (``scripts/e2e-docker.sh --keep``); CI never sets it.
* ``FL_E2E_SEED``: a directory whose files (``wine-*.tar.xz``,
  ``Fork-*.exe``, ``winetricks-*``, ``*-notosans-*.ttf``) are copied into the download cache; our
  code re-verifies every one of them by size and sha256.
* ``FL_E2E_WINETRICKS_CACHE``: a winetricks cache to copy to
  ``$XDG_CACHE_HOME/winetricks`` (dotnet48 and corefonts installers); setup only reads
  that legacy folder and seeds ``$XDG_CACHE_HOME/fork-linux/winetricks`` (``W_CACHE``) from it.
* ``FL_E2E_DISPLAY``: the X display to use (default ``:98``); an Xvfb is
  started on it unless one already answers there.
* ``FL_E2E_FORK_VERSION``: the Fork version to install and test (default: the
  manifest's ``fork.default``); upstream-watch.yml sets it to the version it
  just recorded.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from fork_driver import Fork, Screen, fork_pids, fork_version
from fork_ui import ForkUI

from fixtures import real_tier


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config: pytest.Config) -> None:
    """Defence in depth (tests/conftest.py does the same): no real tier outside a container.

    Ends the session with status 2 before any fixture creates anything (AGENTS.md hard rule 18).
    """
    real_tier.enforce()


# The gate itself is a module-level skip in each test module: a skip raised while a
# conftest.py is imported ends the whole session under pytest 6.2 (Ubuntu 22.04) and
# aborts ``pytest tests/e2e`` under pytest 8. Without the flag these fixtures are never used.

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src"
ROOT_MARKER = ".fl-e2e-root"
TOOLS = ("Xvfb", "xdotool", "magick", "git")
SEED_PATTERNS = ("wine-*.tar.xz", "Fork-*.exe", "winetricks-*", "*-notosans-*.ttf")
SCREEN = "1600x1000x24"
# Host variables that would point the tier at the real session or another Wine.
DROPPED = ("WAYLAND_DISPLAY", "XAUTHORITY", "GNOME_SETUP_DISPLAY", "PYTHONPATH", "FORK_LINUX_LIBDIR",
           "DBUS_SESSION_BUS_ADDRESS", "GIT_EDITOR", "QT_QPA_PLATFORM", "SSH_AUTH_SOCK")
# The order the modules run in (one shared prefix; destructive modules last).
MODULE_ORDER = (
    "test_real_setup_errors.py",
    "test_real_fork.py",
    "test_real_launch.py",
    "test_real_git_history.py",
    "test_real_git_changes.py",
    "test_real_git_branches.py",
    "test_real_git_remotes.py",
    "test_git_ops_remote_ssh.py",
    "test_real_git_advanced.py",
    "test_git_ops_local.py",
    "test_real_integration.py",
    "test_real_display.py",
    "test_real_bridge.py",
    "test_git_ops_remote_bridge.py",
    "test_real_doctor.py",
    "test_real_update_guards.py",
    "test_real_snapshots.py",
    # Prefixes of their own (E2E.child), seeded from the session's caches before uninstall --purge.
    "test_fork_update.py",
    "test_wine_app_upgrade.py",
    "test_wine_pin_upgrade.py",
    "test_real_setup_variants.py",
    "test_real_uninstall.py",
)
SETUP_TIMEOUT = 3600.0
# Kept by FL_E2E_REUSE=1 (home/<name>); home/.cache is always kept.
REUSED = (".cache", ".local", ".config")


def _module_rank(item: pytest.Item) -> int:
    name = Path(str(item.fspath)).name
    return MODULE_ORDER.index(name) if name in MODULE_ORDER else len(MODULE_ORDER)


def pytest_collection_modifyitems(session: pytest.Session, config: pytest.Config, items: list[pytest.Item]) -> None:
    """Run the modules in ``MODULE_ORDER``; tests keep their file order inside a module."""
    order = {id(item): index for index, item in enumerate(items)}
    items.sort(key=lambda item: (_module_rank(item), order[id(item)]))


def _require_tools() -> None:
    missing = [tool for tool in TOOLS if shutil.which(tool) is None]
    if missing:
        pytest.skip(f"E2E tier needs {', '.join(missing)} on PATH")


def _prepare_root(root: Path) -> Path:
    """Create ``root`` or clean a previous run in it (keeping ``home/.cache``, more with FL_E2E_REUSE=1).

    ``real_tier.prepare_root`` refuses ``/``, home directories, symlink components and
    unmarked non-empty roots, and deletes only inside the root without following symlinks.
    """
    keep = REUSED if os.environ.get("FL_E2E_REUSE") == "1" else (".cache",)
    try:
        return real_tier.prepare_root(root, ROOT_MARKER, keep=keep)
    except real_tier.RefusedPath as exc:
        pytest.exit(f"E2E root refused: {exc}", returncode=2)


def _seed(home: Path) -> None:
    downloads = home / ".cache" / "fork-linux" / "downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    seed = os.environ.get("FL_E2E_SEED", "")
    if seed and Path(seed).is_dir():
        for pattern in SEED_PATTERNS:
            for source in sorted(Path(seed).glob(pattern)):
                target = downloads / source.name
                if source.is_file() and not target.exists():
                    shutil.copy2(source, target)
    winetricks_cache = os.environ.get("FL_E2E_WINETRICKS_CACHE", "")
    target_cache = home / ".cache" / "winetricks"
    if winetricks_cache and Path(winetricks_cache).is_dir() and not target_cache.exists():
        shutil.copytree(winetricks_cache, target_cache)


def _link_or_copy(source: str, target: str) -> None:
    """Hard-link ``source`` to ``target``, or copy it across file systems."""
    try:
        os.link(source, target)
    except OSError:
        shutil.copy2(source, target)


def _display_answers(display: str, env: dict[str, str]) -> bool:
    probe = subprocess.run(
        ["xdotool", "getdisplaygeometry"],
        env={**env, "DISPLAY": display},
        capture_output=True,
        timeout=10,
        check=False,
    )
    return probe.returncode == 0


def _clean_path(path: str, real_home: str) -> str:
    """``PATH`` without the real user's own bin directories (a developer install must not be found)."""
    keep = [entry for entry in path.split(os.pathsep) if entry and not entry.startswith(real_home + os.sep)]
    return os.pathsep.join(keep)


_FAILED: dict[str, str] = {}


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]) -> Iterator[None]:
    """Remember the first failed step of each module (``CHAIN = True`` modules fail fast after it)."""
    outcome = yield
    report = outcome.get_result()
    if report.when == "call" and report.failed:
        _FAILED.setdefault(Path(str(item.fspath)).name, item.name)
        _screenshot_failure(item)


def _screenshot_failure(item: pytest.Item) -> None:
    """Keep the screen of a failed test in the shots directory (never the repository)."""
    funcargs = getattr(item, "funcargs", {})
    tier = funcargs.get("e2e") or funcargs.get("installed")
    if tier is None:
        return
    try:
        tier.screen.screenshot(f"failed-{item.name}")
    except (OSError, subprocess.SubprocessError):
        return


@pytest.fixture(autouse=True)
def _in_order(request: pytest.FixtureRequest) -> None:
    """In a ``CHAIN = True`` module, fail fast once an earlier step has failed."""
    module = Path(str(request.node.fspath)).name
    if getattr(request.module, "CHAIN", False) and module in _FAILED:
        pytest.fail(f"an earlier step of this scenario failed ({_FAILED[module]})")


class E2E:
    """The isolated environment of one tier run (or of one variant ``home`` inside it)."""

    def __init__(self, root: Path, display: str, home: Path | None = None) -> None:
        self.root = root
        self.home = home or root / "home"
        self.display = display
        self.shots = root / "shots"
        self.logs = root / "logs"
        self.repos = root / "repos"
        self.run_dir = self.home.parent / "run" if home is not None else root / "run"
        real_home = os.environ.get("HOME", str(Path.home()))
        env = {key: value for key, value in os.environ.items() if not key.startswith(("WINE", "FORK_LINUX_"))}
        for key in DROPPED:
            env.pop(key, None)
        env.update(
            {
                "HOME": str(self.home),
                "XDG_CONFIG_HOME": str(self.home / ".config"),
                "XDG_DATA_HOME": str(self.home / ".local" / "share"),
                "XDG_CACHE_HOME": str(self.home / ".cache"),
                "XDG_STATE_HOME": str(self.home / ".local" / "state"),
                "XDG_RUNTIME_DIR": str(self.run_dir),
                "PYTHONPATH": str(SRC),
                "PYTHONDONTWRITEBYTECODE": "1",
                "DISPLAY": display,
                "GDK_BACKEND": "x11",
                "PATH": _clean_path(env.get("PATH", os.defpath), real_home),
            }
        )
        self.env = env
        self.screen = Screen(env, self.shots)
        self.fork = Fork(self.screen, env, self.prefix, self.logs, root)

    def make_dirs(self) -> None:
        """The XDG directories, a private 0700 runtime directory and the tier's own folders."""
        for sub in (".config", ".local/share", ".cache", ".local/state"):
            (self.home / sub).mkdir(parents=True, exist_ok=True)
        self.run_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.run_dir, 0o700)
        for directory in (self.shots, self.logs, self.repos):
            directory.mkdir(parents=True, exist_ok=True)
        # Existing E2E file-dialog cases drive Wine's dialogs with xdotool. The product
        # default is on; dedicated scenarios turn this back on with a stub picker.
        config = self.home / ".config" / "fork-linux" / "config.ini"
        if not config.is_file():
            config.parent.mkdir(parents=True, exist_ok=True)
            config.write_text(
                "[integration]\nsystem_file_dialogs = false\n",
                encoding="utf-8",
            )

    @property
    def prefix(self) -> Path:
        return self.home / ".local" / "share" / "fork-linux" / "prefix"

    def child(self, name: str) -> E2E:
        """A fresh environment of its own in ``<root>/<name>`` (home, prefix, logs, shots) on the same display.

        For a scenario that must not share the session's prefix: its downloads cache and winetricks cache
        start as hard links (or copies) of the session's verified files.
        """
        child = E2E(self.root / name, self.display)
        child.make_dirs()
        # ~/.wine must survive everything there too, including uninstall --purge.
        (child.home / ".wine" / "SENTINEL").mkdir(parents=True, exist_ok=True)
        for rel in (Path(".cache") / "fork-linux" / "downloads", Path(".cache") / "winetricks"):
            if (self.home / rel).is_dir():
                shutil.copytree(self.home / rel, child.home / rel, copy_function=_link_or_copy, dirs_exist_ok=True)
        return child

    def stop_wine(self) -> None:
        """``wineserver -k`` for every prefix of this environment and its variants, with every managed
        runtime any of their homes installed."""
        homes = [self.home, *sorted((self.root / "variants").glob("*/home"))]
        runtimes = [
            server
            for home in homes
            for server in sorted((home / ".local" / "share" / "fork-linux" / "runtimes" / "wine").glob(
                "*/bin/wineserver"))
        ]
        prefixes = [home / ".local" / "share" / "fork-linux" / "prefix" for home in homes]
        prefixes.append(self.root / "prefix2")
        for prefix in prefixes:
            if not prefix.is_dir():
                continue
            for server in runtimes:
                subprocess.run(
                    [str(server), "-k"],
                    env={**self.env, "WINEPREFIX": str(prefix)},
                    capture_output=True,
                    timeout=60,
                    check=False,
                )

    def cli(self, *args: str, extra: dict[str, str] | None = None, timeout: float = 600, check: bool = True,
            cwd: Path | None = None, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
        """``python3 -m fork_linux ARGS`` in the isolated environment."""
        result = self.fork.cli(*args, extra=extra, timeout=timeout, cwd=cwd or self.root, stdin=stdin)
        if check and result.returncode != 0:
            pytest.fail(f"fork-linux {' '.join(args)} exited {result.returncode}:\n{result.stdout}\n{result.stderr}")
        return result

    def variant(self, name: str) -> E2E:
        """A second, empty user (own HOME and XDG directories) under ``<root>/variants/<name>``."""
        base = self.root / "variants" / name
        if base.exists():
            real_tier.remove_inside(base, self.root)
        variant = E2E(self.root, self.display, home=base / "home")
        variant.make_dirs()
        return variant

    def setup_args(self) -> tuple[str, ...]:
        """The setup command line every run uses (the tested Fork version, no dialogs)."""
        return ("setup", "--accept-fork-eula", "--no-gui", "--fork-version", fork_version())

    def setup_complete(self) -> bool:
        """True when no setup step of the main prefix would run (``setup.complete`` is only the last run's)."""
        listed = self.cli("setup", "--list-steps", "--json", check=False)
        if listed.returncode != 0:
            return False
        return all(row["status"] in ("done", "always") for row in json.loads(listed.stdout)["steps"])


@pytest.fixture(scope="session")
def e2e() -> Iterator[E2E]:
    """The isolated root, seeded caches and an X display; Wine and Xvfb are stopped afterwards."""
    _require_tools()
    root = _prepare_root(Path(os.environ.get("FL_E2E_ROOT") or tempfile.mkdtemp(prefix="fork-linux-e2e-")))
    display = os.environ.get("FL_E2E_DISPLAY", ":98")
    env = E2E(root, display)
    env.make_dirs()
    # ~/.wine must survive everything, including uninstall --purge.
    (env.home / ".wine" / "SENTINEL").mkdir(parents=True, exist_ok=True)
    _seed(env.home)
    xvfb = None
    if not _display_answers(display, env.env):
        xvfb = subprocess.Popen(
            ["Xvfb", display, "-screen", "0", SCREEN, "-nolisten", "tcp", "-noreset"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        deadline = time.monotonic() + 15
        while not _display_answers(display, env.env):
            if time.monotonic() > deadline or xvfb.poll() is not None:
                pytest.fail(f"Xvfb did not start on {display}")
            time.sleep(0.3)
    try:
        yield env
    finally:
        env.stop_wine()
        if xvfb is not None:
            xvfb.terminate()
            xvfb.wait(timeout=10)


@pytest.fixture(scope="session")
def installed(e2e: E2E) -> E2E:
    """The main prefix, set up (``test_real_fork.py`` times the first setup; later modules reuse it)."""
    if not e2e.setup_complete():
        e2e.cli(*e2e.setup_args(), timeout=SETUP_TIMEOUT)
    return e2e


@pytest.fixture(scope="module")
def ui(installed: E2E) -> Iterator[ForkUI]:
    """One Fork session for a module's UI tests (each opens its repositories as tabs, starting from none);
    closed after."""
    installed.fork.ensure_closed()
    installed.fork.forget_tabs()
    yield ForkUI(installed.fork)
    installed.fork.ensure_closed()


@pytest.fixture
def closed(installed: E2E) -> Iterator[E2E]:
    """The installed tier with Fork closed before and after the test."""
    installed.fork.ensure_closed()
    yield installed
    installed.fork.ensure_closed()


@pytest.fixture(autouse=True)
def _no_leftover_dialogs(request: pytest.FixtureRequest) -> Iterator[None]:
    """After each test, close what a failed step left open in Fork (a dialog, a menu)."""
    yield
    if "e2e" not in request.fixturenames:
        return
    tier = request.getfixturevalue("e2e")
    if fork_pids(tier.prefix):
        ForkUI(tier.fork).dismiss_dialogs()
