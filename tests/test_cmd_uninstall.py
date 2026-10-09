"""Tests for ``fork-linux uninstall [--purge]``: only what fork-linux created is ever deleted."""

from __future__ import annotations

import argparse
import io
import os
from pathlib import Path
from typing import Any

import pytest

from fork_linux import desktop_integration, launcher, procs, wine_provider
from fork_linux.cli import AppContext
from fork_linux.commands import uninstall
from fork_linux.locking import FileLock
from fork_linux.paths import Paths
from fork_linux.procrun import RecordingRunner

from fixtures.cli_run import fork_running, isolate, make_prefix, paths, run_cli, run_json
from fixtures.setup_ctx import wine_info


@pytest.fixture(autouse=True)
def _isolated(xdg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    isolate(monkeypatch)
    fork_running(monkeypatch, False)
    monkeypatch.setattr(procs, "prefix_pids", lambda prefix, **_kw: [])


@pytest.fixture
def removed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[int]:
    calls: list[int] = []

    def remove(paths: Any, env: Any, **kwargs: Any) -> list[Path]:
        calls.append(1)
        return [tmp_path / "menu.desktop"]

    monkeypatch.setattr(desktop_integration, "remove", remove)
    return calls


@pytest.fixture
def ours(xdg: Path) -> Paths:
    """Every directory fork-linux owns, populated, plus a ~/.wine that must survive."""
    where = paths()
    make_prefix(where)
    for directory in (
        where.data_dir,
        where.cache_dir,
        where.state_dir,
        where.config_dir,
        where.downloads_dir,
        where.feeds_dir,
        where.logs_dir,
    ):
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "file").write_text("x", encoding="utf-8")
    where.runtime_dir.mkdir(parents=True, exist_ok=True)
    where.session_file.write_text("{}", encoding="utf-8")
    wine_home = xdg / ".wine"
    wine_home.mkdir()
    (wine_home / "E2E_SENTINEL").write_text("keep", encoding="utf-8")
    return where


def test_without_purge_only_removes_the_integration(
    capsys: pytest.CaptureFixture[str], removed: list[int], ours: Paths
) -> None:
    code, out, _err = run_cli(capsys, "uninstall")
    assert code == 0
    assert "removed 1 desktop integration file(s)" in out and "--purge" in out
    assert removed == [1]
    assert ours.prefix.is_dir() and ours.config_dir.is_dir()
    result = run_json(capsys, "uninstall")
    assert result["purged"] is False and len(result["integration_removed"]) == 1
    assert "removed" not in result


def test_purge_needs_confirmation(
    capsys: pytest.CaptureFixture[str], removed: list[int], ours: Paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(uninstall, "_ask", lambda prompt: None)
    code, _out, err = run_cli(capsys, "uninstall", "--purge")
    assert code == 18 and "Help > Activation > Deactivate" in err and "--yes" in err
    monkeypatch.setattr(uninstall, "_ask", lambda prompt: "yes")
    assert run_cli(capsys, "uninstall", "--purge")[0] == 18
    assert removed == [] and ours.prefix.is_dir()


def test_purge_deletes_everything_we_created(
    capsys: pytest.CaptureFixture[str], removed: list[int], ours: Paths, xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(uninstall, "_ask", lambda prompt: " purge\n")
    code, out, err = run_cli(capsys, "uninstall", "--purge")
    assert code == 0, err
    for directory in (ours.prefix, ours.data_dir, ours.cache_dir, ours.state_dir, ours.config_dir):
        assert not directory.exists(), directory
    assert not ours.session_file.exists()
    assert (xdg / ".wine" / "E2E_SENTINEL").read_text(encoding="utf-8") == "keep"
    assert f"deleted {ours.prefix}" in out and "~/.wine was never touched" in out


def test_purge_keep_downloads_and_json(capsys: pytest.CaptureFixture[str], removed: list[int], ours: Paths) -> None:
    result = run_json(capsys, "uninstall", "--purge", "--yes", "--keep-downloads")
    assert (ours.downloads_dir / "file").exists()
    assert not ours.feeds_dir.exists() and not (ours.cache_dir / "file").exists()
    assert result["kept"] == [str(ours.downloads_dir)]
    assert result["purged"] is True and result["wineserver_stopped"] is False
    assert str(ours.prefix) in result["removed"]


def test_purge_keep_downloads_text(capsys: pytest.CaptureFixture[str], removed: list[int], ours: Paths) -> None:
    out = run_cli(capsys, "uninstall", "--purge", "--yes", "--keep-downloads")[1]
    assert f"kept {ours.downloads_dir}" in out


def test_purge_keep_downloads_without_a_cache(
    capsys: pytest.CaptureFixture[str], removed: list[int], xdg: Path
) -> None:
    result = run_json(capsys, "uninstall", "--purge", "--yes", "--keep-downloads")
    assert result["kept"] == []


def test_purge_needs_fork_closed_and_the_lock(
    capsys: pytest.CaptureFixture[str], removed: list[int], ours: Paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    with FileLock(ours.lock_file, "setup"):
        assert run_cli(capsys, "uninstall", "--purge", "--yes")[0] == 16
    fork_running(monkeypatch, True)
    removed.clear()
    assert run_cli(capsys, "uninstall", "--purge", "--yes")[0] == 15
    assert ours.prefix.is_dir()
    # Nothing at all is removed while Fork runs, not even the desktop integration.
    assert removed == []


def _ctx(where: Paths, home: Path, runner: RecordingRunner | None = None) -> AppContext:
    args = argparse.Namespace(prefix=None, gui=False, offline=False, verbose=0, quiet=0, allow_root=False)
    ctx = AppContext(args, env={"HOME": str(home), "WINEDEBUG": "+all", "PATH": "/usr/bin"}, runner=runner)
    ctx._paths = where
    return ctx


def _purge_args() -> argparse.Namespace:
    return argparse.Namespace(purge=True, keep_downloads=False, yes=True)


def test_foreign_prefixes_are_kept(tmp_path: Path, xdg: Path) -> None:
    base = paths()
    elsewhere = tmp_path / "elsewhere-prefix"
    elsewhere.mkdir()
    (elsewhere / "user.reg").write_text("x", encoding="utf-8")
    custom = Paths(base.config_dir, base.data_dir, base.cache_dir, base.state_dir, base.runtime_dir, elsewhere)
    result = uninstall._purge(_ctx(custom, xdg), _purge_args())
    assert result["kept"] == [str(elsewhere)] and elsewhere.is_dir()
    wine_home = xdg / ".wine"
    (wine_home / ".fork-linux").mkdir(parents=True)
    (wine_home / ".fork-linux" / "created-by").write_text("forged", encoding="utf-8")
    custom = Paths(base.config_dir, base.data_dir, base.cache_dir, base.state_dir, base.runtime_dir, wine_home)
    result = uninstall._purge(_ctx(custom, xdg), _purge_args())
    assert result["kept"] == [str(wine_home)] and wine_home.is_dir()


def test_marked_prefix_outside_our_data_dir_is_deleted(tmp_path: Path, xdg: Path) -> None:
    base = paths()
    elsewhere = tmp_path / "marked-prefix"
    custom = Paths(base.config_dir, base.data_dir, base.cache_dir, base.state_dir, base.runtime_dir, elsewhere)
    make_prefix(custom)
    result = uninstall._purge(_ctx(custom, xdg), _purge_args())
    assert str(elsewhere) in result["removed"] and not elsewhere.exists()


def test_unmarked_prefix_inside_our_data_dir_goes_with_it(tmp_path: Path, xdg: Path) -> None:
    where = paths()
    where.prefix.mkdir(parents=True)
    result = uninstall._purge(_ctx(where, xdg), _purge_args())
    assert result["removed"] == [str(where.data_dir)] and not where.prefix.exists()


def test_running_wine_is_stopped_first(tmp_path: Path, xdg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    where = paths()
    make_prefix(where)
    runner = RecordingRunner()
    ctx = _ctx(where, xdg, runner)
    monkeypatch.setattr(procs, "prefix_pids", lambda prefix, **_kw: [77])
    # No wineserver known: nothing to ask.
    assert uninstall._stop_wine(ctx) is False
    managed = wine_provider.managed_root(where, "kron4ek-test") / "bin" / "wineserver"
    monkeypatch.setattr(wine_provider, "installed_builds", lambda paths: ["kron4ek-test"])
    managed.parent.mkdir(parents=True)
    managed.write_text("#!/bin/sh\n", encoding="utf-8")
    info = wine_info(tmp_path / "session-wine")
    info.wineserver.parent.mkdir(parents=True)
    info.wineserver.write_text("#!/bin/sh\n", encoding="utf-8")
    launcher.write_session(where, info, 5)
    assert uninstall._stop_wine(ctx) is True
    call = runner.calls[0]
    assert call["argv"] == [str(info.wineserver), "-k"]
    assert call["env"] == {"HOME": str(xdg), "PATH": "/usr/bin", "WINEPREFIX": str(where.prefix)}
    where.session_file.write_text('{"wineserver": 5}', encoding="utf-8")
    assert uninstall._stop_wine(ctx) is True
    assert runner.calls[1]["argv"] == [str(managed), "-k"]


def test_ask(monkeypatch: pytest.MonkeyPatch) -> None:
    class Tty(io.StringIO):
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr("sys.stdin", io.StringIO("purge\n"))
    assert uninstall._ask("? ") is None
    monkeypatch.setattr("sys.stdin", Tty("purge\n"))
    assert uninstall._ask("? ") == "purge"
    monkeypatch.setattr("sys.stdin", Tty(""))
    assert uninstall._ask("? ") is None


def test_never_deleted() -> None:
    home = Path(os.sep, "home", "u")
    assert uninstall._never_deleted(home / ".wine", home)
    assert uninstall._never_deleted(home / ".wine" / "sub", home)
    assert not uninstall._never_deleted(home / ".wine-other", home)
