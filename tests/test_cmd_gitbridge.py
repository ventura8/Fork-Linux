"""Tests for ``fork-linux git-bridge status|enable|disable``."""

from __future__ import annotations

from pathlib import Path

import pytest

from fork_linux import bridge, resources
from fork_linux.config import Config

from fixtures.cli_run import isolate, paths, run_cli, run_json


@pytest.fixture(autouse=True)
def _isolated(xdg: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    isolate(monkeypatch)
    monkeypatch.setenv(resources.SHIMS_ENV, str(tmp_path / "shims"))
    monkeypatch.setenv(resources.LIBEXEC_ENV, str(tmp_path / "libexec"))


def _build(tmp_path: Path) -> None:
    for exe in bridge.SHIM_EXES:
        (tmp_path / "shims").mkdir(exist_ok=True)
        (tmp_path / "shims" / exe).write_bytes(b"MZ")
    (tmp_path / "libexec").mkdir()
    (tmp_path / "libexec" / bridge.HELPER).write_bytes(b"\x7fELF")


def test_status_when_not_built(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    code, out, _err = run_cli(capsys, "git-bridge", "status")
    assert code == 0
    assert "enabled:   no" in out and "available: no" in out
    assert "note:      the native-git bridge is experimental" in out
    code, _out, err = run_cli(capsys, "git-bridge", "enable")
    assert code == 2 and "experimental" in err
    info = run_json(capsys, "git-bridge", "status")
    assert info["available"] is False and info["shims_dir"] == str(tmp_path / "shims")


def test_status_without_a_shims_dir(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(resources, "shims_dir", lambda: None)
    assert "shims:     not built" in run_cli(capsys, "git-bridge", "status")[1]


def test_enable_and_disable(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    _build(tmp_path)
    code, out, _err = run_cli(capsys, "git-bridge", "enable")
    assert code == 0 and "enabled (experimental)" in out
    assert Config.load(paths(), {}).getbool("git", "bridge") is True
    out = run_cli(capsys, "git-bridge", "status")[1]
    assert "enabled:   yes" in out and "note:" not in out
    assert run_json(capsys, "git-bridge", "enable")["enabled"] is True
    code, out, _err = run_cli(capsys, "git-bridge", "disable")
    assert code == 0 and "bundled git" in out
    assert run_json(capsys, "git-bridge", "disable")["enabled"] is False
    assert Config.load(paths(), {}).getbool("git", "bridge") is False
