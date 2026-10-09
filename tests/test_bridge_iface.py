"""Tests for fork_linux.bridge: the experimental git bridge's interface (status / enable / disable)."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import pytest

from fork_linux import bridge, resources
from fork_linux.cli import AppContext
from fork_linux.errors import UsageError


def _ctx() -> AppContext:
    args = argparse.Namespace(prefix=None, gui=False, offline=False, verbose=0, quiet=0, allow_root=False)
    return AppContext(args, env=dict(os.environ))


@pytest.fixture
def built(xdg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An installation that ships the shims and the helper."""
    shims = tmp_path / "shims"
    shims.mkdir()
    for exe in bridge.SHIM_EXES:
        (shims / exe).write_bytes(b"MZ")
    libexec = tmp_path / "libexec"
    libexec.mkdir()
    (libexec / bridge.HELPER).write_bytes(b"\x7fELF")
    monkeypatch.setenv(resources.SHIMS_ENV, str(shims))
    monkeypatch.setenv(resources.LIBEXEC_ENV, str(libexec))
    return shims


def test_status_and_enable_when_built(built: Path) -> None:
    ctx = _ctx()
    info = bridge.status(ctx)
    assert info == {
        "enabled": False,
        "available": True,
        "reason": "",
        "shims_dir": str(built),
        "helper": str(bridge.helper_path()),
    }
    bridge.enable(ctx)
    assert ctx.config.getbool("git", "bridge") is True
    assert "bridge = on" in ctx.paths.config_file.read_text(encoding="utf-8")
    assert bridge.status(_ctx())["enabled"] is True
    bridge.disable(ctx)
    assert bridge.status(_ctx())["enabled"] is False


def test_enable_refused_when_not_built(xdg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(resources.LIBEXEC_ENV, str(tmp_path / "nowhere"))
    monkeypatch.setattr(resources, "shims_dir", lambda: None)
    ctx = _ctx()
    info = bridge.status(ctx)
    assert info["available"] is False
    assert info["shims_dir"] is None
    assert "experimental" in info["reason"]
    for name in (*bridge.SHIM_EXES, bridge.HELPER):
        assert name in info["reason"]
    with pytest.raises(UsageError, match="experimental") as excinfo:
        bridge.enable(ctx)
    assert "bundled git" in excinfo.value.hint
    assert not ctx.paths.config_file.exists()


def test_partial_build_lists_what_is_missing(built: Path) -> None:
    (built / "fl-launch.exe").unlink()
    info = bridge.status(_ctx())
    assert info["available"] is False
    assert "fl-launch.exe" in info["reason"] and "fl-shim.exe" not in info["reason"]


def test_invalid_setting_counts_as_off(built: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FORK_LINUX_GIT_BRIDGE", "maybe")
    info = bridge.status(_ctx())
    assert info["enabled"] is False
    assert info["available"] is True
    assert "maybe" in info["reason"]


def test_launch_env_is_empty_until_the_daemon_exists(built: Path) -> None:
    ctx = _ctx()
    bridge.enable(ctx)
    assert bridge.launch_env(ctx) == {}
