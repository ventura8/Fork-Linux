"""Desktop E2E tier: fork-linux, installed from the release tarball, inside a real desktop session.

Containers only: ``scripts/ci-e2e-de.sh <desktop> [--fork]`` builds the release tarball and the
desktop's image (``docker/Dockerfile.e2e.de.<desktop>``) and runs this directory there. With
``FL_E2E_DE`` (or ``FL_E2E_DE_FORK``) set outside a container pytest exits at once, before
anything is created (AGENTS.md hard rule 18; ``tests/fixtures/real_tier.py``).

Gated: each test module skips itself unless ``FL_E2E_DE=<desktop>`` names a recipe of
``desktops.py`` (the real-Fork module also needs ``FL_E2E_DE_FORK=1``). A skip raised while a
conftest.py is imported would end the whole session under pytest 6.2, so the gate lives in the
modules and these fixtures are only used once it passed.

Environment (set by scripts/ci-e2e-de.sh):

* ``FL_E2E_DE``: the desktop (a key of ``desktops.DESKTOPS``).
* ``FL_E2E_DE_TARBALL``: the release tarball (``fork-linux-VERSION-x86_64.tar.gz``) to install.
* ``FL_E2E_ROOT``: the scratch root (home, logs, screenshots); checked like the real tier's.
* ``FL_E2E_SEED`` / ``FL_E2E_WINETRICKS_CACHE``: read-only download seeds for the real-Fork tier.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

import dekit
import desktops
import pytest

from fixtures import real_tier


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config: pytest.Config) -> None:
    """Defence in depth (tests/conftest.py does the same): no desktop tier outside a container."""
    real_tier.enforce()


REPO = Path(__file__).resolve().parents[2]
ROOT_MARKER = ".fl-e2e-de-root"
TOOLS = ("Xvfb", "dbus-daemon", "gdbus", "xdotool", "xprop", "xwininfo", "magick", "git")
STEP_MODULE = "test_de_fork.py"


def _require_tools(desktop: desktops.Desktop) -> None:
    wanted = (*TOOLS, desktop.settings_daemon[0], *desktop.window_manager[:1], desktop.terminal)
    missing = [tool for tool in wanted if shutil.which(tool) is None]
    if missing:
        pytest.skip(f"the desktop tier needs {', '.join(missing)} on PATH (run it in docker/Dockerfile.e2e.de.*)")


@pytest.fixture(scope="session")
def desktop() -> desktops.Desktop:
    """The recipe ``FL_E2E_DE`` names."""
    key = os.environ.get("FL_E2E_DE", "")
    if key not in desktops.DESKTOPS:
        pytest.fail(f"FL_E2E_DE={key!r} is not one of {sorted(desktops.DESKTOPS)}")
    recipe = desktops.DESKTOPS[key]
    _require_tools(recipe)
    return recipe


@pytest.fixture(scope="session")
def root() -> Path:
    """The scratch root: ``FL_E2E_ROOT`` (or a new temp directory), cleaned and marked."""
    path = Path(os.environ.get("FL_E2E_ROOT") or tempfile.mkdtemp(prefix="fork-linux-e2e-de-"))
    try:
        return real_tier.prepare_root(path, ROOT_MARKER, keep=(".cache",))
    except real_tier.RefusedPath as exc:
        pytest.exit(f"E2E root refused: {exc}", returncode=2)


@pytest.fixture(scope="session")
def session(desktop: desktops.Desktop, root: Path) -> Iterator[dekit.Session]:
    """The running desktop session; every process it started is ended afterwards."""
    started = dekit.Session(desktop, root)
    try:
        started.start()
        yield started
    finally:
        started.stop()


@pytest.fixture(scope="session")
def installed(session: dekit.Session) -> dekit.Installed:
    """``install.sh --from-tarball FL_E2E_DE_TARBALL --no-deps`` in the session (per-user install)."""
    tarball = Path(os.environ.get("FL_E2E_DE_TARBALL", ""))
    if not tarball.is_file():
        pytest.fail(f"FL_E2E_DE_TARBALL={str(tarball)!r} is not a file (scripts/ci-e2e-de.sh builds it)")
    result = session.run(["bash", str(REPO / "install.sh"), "--from-tarball", str(tarball), "--no-deps"], timeout=300)
    prefix = session.home / ".local" / "opt" / "fork-linux"
    packages = sorted(prefix.glob("share/fork-linux/fork_linux/__init__.py"))
    assert packages, f"install.sh left no fork_linux package under {prefix}"
    return dekit.Installed(
        output=result.stdout + result.stderr,
        prefix=prefix,
        package_dir=packages[0].parent.parent,
        host_helper=prefix / "lib" / "fork-linux" / "fork-linux-host",
        menu_file=session.home / ".local" / "share" / "applications" / "io.github.ventura8.ForkLinux.desktop",
    )


# The real-Fork module's steps build on each other: after a failed step the later ones fail fast.
_FAILED: list[str] = []


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]) -> Iterator[None]:
    """Remember the first failed step of the real-Fork scenario."""
    outcome = yield
    report = outcome.get_result()
    if report.when == "call" and report.failed and item.nodeid.split("::")[0].endswith(STEP_MODULE):
        _FAILED.append(item.name)


@pytest.fixture
def in_order() -> None:
    """Fail fast once an earlier step of the real-Fork scenario failed."""
    if _FAILED:
        pytest.fail(f"an earlier step failed ({_FAILED[0]})")
