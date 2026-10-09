"""Tests for ``fork-linux settings``: Fork's settings.json, edited only while Fork is closed."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from fork_linux import fork_settings

from fixtures.cli_run import USER, fork_running, isolate, layout, make_prefix, paths, run_cli, run_json
from fixtures.fork_tree import write_settings

ORIGINAL = {
    "Guid": "abc",
    "Theme": 0,
    "UpdateSubmodulesOnCheckout": True,
    "RepositoryManager": {"SourceDirectories": [f"C:\\users\\{USER}"], "Other": 1},
    "Unknown": {"Keep": [1, 2]},
}


@pytest.fixture(autouse=True)
def _isolated(xdg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    isolate(monkeypatch)
    fork_running(monkeypatch, False)
    clock = [datetime(2026, 1, 1, tzinfo=timezone.utc)]

    def now() -> datetime:
        clock[0] += timedelta(seconds=1)
        return clock[0]

    monkeypatch.setattr(fork_settings, "_utcnow", now)


@pytest.fixture
def settings() -> Path:
    write_settings(layout(), ORIGINAL)
    return layout().settings_file


def _read(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _backups() -> list[Path]:
    return fork_settings.list_backups(fork_settings.default_backup_dir(paths()))


def test_missing_file_is_not_found(capsys: pytest.CaptureFixture[str]) -> None:
    for argv in (["show"], ["get", "Theme"], ["set", "Theme", "1"], ["unset", "Theme"], ["apply-defaults"], ["backup"]):
        code, _out, err = run_cli(capsys, "settings", *argv)
        assert code == 20, argv
        assert "welcome dialog" in err


def test_show_get_and_path(capsys: pytest.CaptureFixture[str], settings: Path) -> None:
    code, out, _err = run_cli(capsys, "settings", "show")
    assert code == 0 and json.loads(out) == ORIGINAL
    assert run_json(capsys, "settings", "show") == ORIGINAL
    assert run_cli(capsys, "settings", "get", "Guid")[1] == "abc\n"
    assert run_json(capsys, "settings", "get", "Guid") == "abc"
    assert run_json(capsys, "settings", "get", "RepositoryManager.SourceDirectories") == [f"C:\\users\\{USER}"]
    assert run_cli(capsys, "settings", "get", "Theme")[1] == "0\n"
    code, _out, err = run_cli(capsys, "settings", "get", "Nope.Nested")
    assert code == 20 and "Nope.Nested" in err
    assert run_cli(capsys, "settings", "path")[1] == f"{settings}\n"
    assert run_json(capsys, "settings", "path") == {"path": str(settings), "exists": True}


def test_set_values_and_backups(capsys: pytest.CaptureFixture[str], settings: Path) -> None:
    code, out, _err = run_cli(capsys, "settings", "set", "Theme", "1")
    assert code == 0 and out.startswith("changed: Theme (backup: ")
    assert _read(settings)["Theme"] == 1
    assert len(_backups()) == 1
    assert run_cli(capsys, "settings", "set", "Theme", "1")[1] == "nothing to change\n"
    assert len(_backups()) == 1
    result = run_json(capsys, "settings", "set", "Theme", "1", "--string")
    assert result["changed"] == ["Theme"] and result["backup"]
    assert _read(settings)["Theme"] == "1"
    run_cli(capsys, "settings", "set", "New.Deep", "plain text")
    run_cli(capsys, "settings", "set", "Flag", "true")
    data = _read(settings)
    assert data["New"] == {"Deep": "plain text"} and data["Flag"] is True
    assert data["Unknown"] == {"Keep": [1, 2]}
    assert run_json(capsys, "settings", "set", "Flag", "true") == {"changed": [], "backup": None}
    code, _out, err = run_cli(capsys, "settings", "set", "a..b", "1")
    assert code == 2 and "invalid settings key" in err
    code, _out, _err = run_cli(capsys, "settings", "set", "Guid.Sub", "1")
    assert code == 13


def test_unset(capsys: pytest.CaptureFixture[str], settings: Path) -> None:
    assert run_cli(capsys, "settings", "unset", "RepositoryManager.Other")[0] == 0
    assert "Other" not in _read(settings)["RepositoryManager"]
    assert run_json(capsys, "settings", "unset", "Theme")["changed"] == ["Theme"]
    for key in ("Theme", "Missing.Key", "Guid.Sub"):
        assert run_cli(capsys, "settings", "unset", key)[0] == 20, key


def test_writes_need_fork_closed(
    capsys: pytest.CaptureFixture[str], settings: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fork_running(monkeypatch, True)
    for argv in (["set", "Theme", "1"], ["unset", "Theme"], ["apply-defaults"], ["restore"]):
        assert run_cli(capsys, "settings", *argv)[0] == 15, argv
    assert _read(settings) == ORIGINAL
    assert run_cli(capsys, "settings", "show")[0] == 0


def test_apply_defaults(capsys: pytest.CaptureFixture[str], settings: Path) -> None:
    make_prefix(paths())
    result = run_json(capsys, "settings", "apply-defaults")
    assert set(result["changed"]) >= {
        "UpdateSubmodulesOnCheckout",
        "DisableHardwareAcceleration",
        "Theme",
        "FollowSystemTheme",
        "LayoutScaling",
        "RepositoryManager.SourceDirectories",
    }
    assert result["backup"] is not None
    data = _read(settings)
    assert data["Theme"] == 1 and data["LayoutScaling"] == 100
    assert data["RepositoryManager"]["Other"] == 1
    code, out, _err = run_cli(capsys, "settings", "apply-defaults")
    assert (code, out) == (0, "nothing to change\n")


def test_backup_and_restore(capsys: pytest.CaptureFixture[str], settings: Path, tmp_path: Path) -> None:
    code, _out, err = run_cli(capsys, "settings", "restore")
    assert code == 20 and "no backup" in err
    code, out, _err = run_cli(capsys, "settings", "backup")
    assert code == 0 and out.startswith("backed up to ")
    saved = run_json(capsys, "settings", "backup")["backup"]
    run_cli(capsys, "settings", "set", "Theme", "1")
    code, out, _err = run_cli(capsys, "settings", "restore")
    # The newest backup is the one taken just before 'set'.
    assert code == 0 and out.startswith(f"restored {settings} from ")
    assert _read(settings)["Theme"] == 0
    result = run_json(capsys, "settings", "restore", saved)
    assert result["restored"] == saved and result["backup"]
    custom = tmp_path / "mine.json"
    custom.write_text('{"Theme": 1}', encoding="utf-8")
    assert run_cli(capsys, "settings", "restore", str(custom))[0] == 0
    assert _read(settings) == {"Theme": 1}
    assert run_cli(capsys, "settings", "restore", str(tmp_path / "nope.json"))[0] == 20
    custom.write_text("not json", encoding="utf-8")
    assert run_cli(capsys, "settings", "restore", str(custom))[0] == 13
    assert _read(settings) == {"Theme": 1}


def test_restore_without_a_settings_file(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    custom = tmp_path / "mine.json"
    custom.write_text('{"Theme": 1}', encoding="utf-8")
    result = run_json(capsys, "settings", "restore", str(custom))
    assert result["backup"] is None
    assert _read(layout().settings_file) == {"Theme": 1}
    assert run_json(capsys, "settings", "path")["exists"] is True
