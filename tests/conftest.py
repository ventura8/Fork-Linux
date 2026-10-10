"""Shared pytest setup: import fork_linux from src/ and isolate every test from the real home."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from fixtures import real_tier

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src"
FAKES_BIN = Path(__file__).resolve().parent / "fakes" / "bin"

# Shared fixture modules under tests/fixtures/ (registered once, not imported per test file).
pytest_plugins = ["fixtures.http_server"]

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config: pytest.Config) -> None:
    """Before anything else: a real Wine / Fork tier never runs on the host (AGENTS.md hard rule 18).

    ``FL_E2E_FORK=1``, ``FL_REAL_WINE=1`` (any ``FL_E2E_*`` / ``FL_REAL_*`` switch) run only inside a
    container (scripts/e2e-docker.sh, scripts/ci-*.sh). ``pytest.exit`` here ends the session with
    status 2 before any test, fixture or directory exists; there is no host override.
    """
    real_tier.enforce()


@pytest.fixture
def xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point HOME and every XDG base directory at a fresh temp tree; return the fake HOME."""
    home = tmp_path / "home"
    for sub in (".config", ".local/share", ".cache", ".local/state", "run"):
        (home / sub).mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / ".local/share"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(home / ".cache"))
    monkeypatch.setenv("XDG_STATE_HOME", str(home / ".local/state"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(home / "run"))
    for var in ("FORK_LINUX_PREFIX", "WINEPREFIX", "FLATPAK_ID", "SNAP", "APPIMAGE", "APPDIR"):
        monkeypatch.delenv(var, raising=False)
    return home


@pytest.fixture
def fake_bin(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Put tests/fakes/bin first on PATH; fakes append JSON lines to $FL_FAKE_LOG."""
    log = tmp_path / "fake-calls.jsonl"
    monkeypatch.setenv("FL_FAKE_LOG", str(log))
    monkeypatch.setenv("PATH", f"{FAKES_BIN}:{Path(sys.executable).parent}:/usr/bin:/bin")
    return log
