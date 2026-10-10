"""Tests for ``fork-linux desktop install|remove|status``."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from fork_linux import desktop_integration

from fixtures.cli_run import isolate, layout, run_cli, run_json
from fixtures.fork_tree import install_fork


@pytest.fixture(autouse=True)
def _isolated(xdg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    isolate(monkeypatch)


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[tuple[str, dict[str, Any]]]:
    """Record desktop_integration.install / remove calls."""
    seen: list[tuple[str, dict[str, Any]]] = []

    def install(paths: Any, env: Any, **kwargs: Any) -> list[Path]:
        seen.append(("install", kwargs))
        return [tmp_path / "a.desktop", tmp_path / "icon.png"]

    def remove(paths: Any, env: Any, **kwargs: Any) -> list[Path]:
        seen.append(("remove", kwargs))
        return []

    monkeypatch.setattr(desktop_integration, "install", install)
    monkeypatch.setattr(desktop_integration, "remove", remove)
    return seen


def test_install_defaults_and_options(
    capsys: pytest.CaptureFixture[str], calls: list[tuple[str, dict[str, Any]]], tmp_path: Path
) -> None:
    code, out, _err = run_cli(capsys, "desktop", "install")
    assert code == 0
    assert out == f"installed {tmp_path / 'a.desktop'}\ninstalled {tmp_path / 'icon.png'}\n"
    kwargs = calls[0][1]
    assert kwargs["fork_exe"] is None
    options = (kwargs["menu"], kwargs["icons"], kwargs["file_managers"], kwargs["cli_alias"])
    assert options == (True, True, "auto", False)
    install_fork(layout())
    result = run_json(
        capsys, "desktop", "install", "--file-managers", "nautilus,dolphin", "--no-menu", "--no-icons", "--cli-alias"
    )
    assert result == {"installed": [str(tmp_path / "a.desktop"), str(tmp_path / "icon.png")]}
    kwargs = calls[1][1]
    assert kwargs["fork_exe"] == layout().exe
    assert (kwargs["menu"], kwargs["icons"], kwargs["file_managers"], kwargs["cli_alias"]) == (
        False,
        False,
        "nautilus,dolphin",
        True,
    )


def test_remove(capsys: pytest.CaptureFixture[str], calls: list[tuple[str, dict[str, Any]]]) -> None:
    assert run_cli(capsys, "desktop", "remove")[1] == "nothing to remove\n"
    assert run_json(capsys, "desktop", "remove") == {"removed": []}
    assert [name for name, _kw in calls] == ["remove", "remove"]


def test_install_with_nothing_installed(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(desktop_integration, "install", lambda paths, env, **kw: [])
    assert run_cli(capsys, "desktop", "install")[1] == "nothing installed\n"


def test_status(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    info: dict[str, Any] = {
        "launcher": ["fork-linux"],
        "system_desktop": None,
        "entries": [],
        "installed": False,
        "ok": True,
    }
    monkeypatch.setattr(desktop_integration, "status", lambda paths, env: info)
    code, out, _err = run_cli(capsys, "desktop", "status")
    assert code == 0
    assert "launcher: fork-linux" in out
    assert "not installed" in out
    info["system_desktop"] = "/usr/share/applications/x.desktop"
    info["entries"] = [{"state": "ok", "kind": "menu", "path": "/p/menu.desktop"}]
    code, out, _err = run_cli(capsys, "desktop", "status")
    assert "installed by a package: /usr/share/applications/x.desktop" in out
    assert "ok        menu             /p/menu.desktop" in out
    assert run_json(capsys, "desktop", "status") == info


def test_real_status_round_trip(capsys: pytest.CaptureFixture[str], fake_bin: Path) -> None:
    result = run_json(capsys, "desktop", "status")
    assert result["installed"] is False
