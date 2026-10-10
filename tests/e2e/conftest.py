"""Real end-to-end tier: the official Fork under the managed Wine runtime, on an Xvfb display.

Containers only: run it with ``scripts/e2e-docker.sh pytest tests/e2e`` (CI:
``scripts/ci-e2e-wine.sh``). With ``FL_E2E_FORK=1`` outside a container (no ``/.dockerenv`` /
``/run/.containerenv``) pytest exits at once, before anything is created (AGENTS.md hard rule
18; ``tests/fixtures/real_tier.py``); there is no host override.

Gated: nothing here runs unless ``FL_E2E_FORK=1`` (each test module skips itself). The tier downloads (or
reuses) the pinned Wine runtime, winetricks verbs and the official Fork
installer, builds a real prefix (~2.5 GB, ~4-10 minutes) and drives Fork's
windows with ``xdotool``. Everything lives under one root directory with a
fake ``HOME`` and XDG directories, so the real home and ``~/.wine`` are never
touched; screenshots stay in ``<root>/shots`` (they show Fork's logo: never
copy them into the repository).

Environment:

* ``FL_E2E_ROOT``: the root directory (default: a new directory under the
  system temp dir). An existing root is reused: its ``home/.cache`` (verified
  downloads, winetricks cache) is kept and everything else is rebuilt. The root
  must be absolute and symlink-free, and must not be ``/`` or be, contain or lie
  inside a home directory; a non-empty root without the tier's marker is refused.
* ``FL_E2E_SEED``: a directory whose files (``wine-*.tar.xz``,
  ``Fork-*.exe``, ``winetricks-*``, ``selawik-*.zip``) are copied into the download cache; our
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

import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

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
SEED_PATTERNS = ("wine-*.tar.xz", "Fork-*.exe", "winetricks-*", "selawik-*.zip")
SCREEN = "1600x1000x24"
# Host variables that would point the tier at the real session or another Wine.
DROPPED = ("WAYLAND_DISPLAY", "XAUTHORITY", "GNOME_SETUP_DISPLAY", "PYTHONPATH", "FORK_LINUX_LIBDIR")


def _require_tools() -> None:
    missing = [tool for tool in TOOLS if shutil.which(tool) is None]
    if missing:
        pytest.skip(f"E2E tier needs {', '.join(missing)} on PATH")


def _prepare_root(root: Path) -> Path:
    """Create ``root`` or clean a previous run in it (keeping ``home/.cache``).

    ``real_tier.prepare_root`` refuses ``/``, home directories, symlink components and
    unmarked non-empty roots, and deletes only inside the root without following symlinks.
    """
    try:
        return real_tier.prepare_root(root, ROOT_MARKER, keep=(".cache",))
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


_FAILED: list[str] = []


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]) -> Iterator[None]:
    """Remember the first failed step: the scenario's steps build on each other."""
    outcome = yield
    report = outcome.get_result()
    if report.when == "call" and report.failed:
        _FAILED.append(item.name)


@pytest.fixture(autouse=True)
def _in_order() -> None:
    """Fail fast once an earlier step of the scenario has failed."""
    if _FAILED:
        pytest.fail(f"an earlier E2E step failed ({_FAILED[0]})")


class E2E:
    """The isolated environment of one tier run."""

    def __init__(self, root: Path, display: str) -> None:
        self.root = root
        self.home = root / "home"
        self.display = display
        self.shots = root / "shots"
        self.logs = root / "logs"
        self.repos = root / "repos"
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
                "XDG_RUNTIME_DIR": str(root / "run"),
                "PYTHONPATH": str(SRC),
                "PYTHONDONTWRITEBYTECODE": "1",
                "DISPLAY": display,
                "GDK_BACKEND": "x11",
                "PATH": _clean_path(env.get("PATH", os.defpath), real_home),
            }
        )
        self.env = env

    @property
    def prefix(self) -> Path:
        return self.home / ".local" / "share" / "fork-linux" / "prefix"

    def cli(self, *args: str, extra: dict[str, str] | None = None, timeout: float = 600, check: bool = True,
            cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        """``python3 -m fork_linux ARGS`` in the isolated environment."""
        result = subprocess.run(
            [sys.executable, "-m", "fork_linux", *args],
            env={**self.env, **(extra or {})},
            cwd=str(cwd or self.root),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if check and result.returncode != 0:
            pytest.fail(f"fork-linux {' '.join(args)} exited {result.returncode}:\n{result.stdout}\n{result.stderr}")
        return result


@pytest.fixture(scope="session")
def e2e() -> Iterator[E2E]:
    """The isolated root, seeded caches and an X display; Wine and Xvfb are stopped afterwards."""
    _require_tools()
    root = _prepare_root(Path(os.environ.get("FL_E2E_ROOT") or tempfile.mkdtemp(prefix="fork-linux-e2e-")))
    display = os.environ.get("FL_E2E_DISPLAY", ":98")
    env = E2E(root, display)
    for sub in (".config", ".local/share", ".cache", ".local/state"):
        (env.home / sub).mkdir(parents=True, exist_ok=True)
    (root / "run").mkdir(mode=0o700, exist_ok=True)
    os.chmod(root / "run", 0o700)
    for directory in (env.shots, env.logs, env.repos):
        directory.mkdir(exist_ok=True)
    # ~/.wine must survive everything, including uninstall --purge.
    (env.home / ".wine" / "SENTINEL").mkdir(parents=True, exist_ok=True)
    _seed(env.home)
    xvfb = None
    if not _display_answers(display, env.env):
        xvfb = subprocess.Popen(
            ["Xvfb", display, "-screen", "0", SCREEN, "-nolisten", "tcp"],
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
        _stop_all_wine(env)
        if xvfb is not None:
            xvfb.terminate()
            xvfb.wait(timeout=10)


def _stop_all_wine(env: E2E) -> None:
    """``wineserver -k`` for every prefix of this run, with the managed runtime that served it."""
    runtimes = sorted((env.home / ".local" / "share" / "fork-linux" / "runtimes" / "wine").glob("*/bin/wineserver"))
    prefixes = [env.prefix, env.root / "prefix2"]
    for prefix in prefixes:
        if not prefix.is_dir():
            continue
        for server in runtimes:
            subprocess.run(
                [str(server), "-k"],
                env={**env.env, "WINEPREFIX": str(prefix)},
                capture_output=True,
                timeout=60,
                check=False,
            )
