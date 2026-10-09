"""Tests for ``fork-linux snapshot create|list|delete|prune`` and ``fork-linux rollback``."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from fork_linux import snapshots, updates
from fork_linux.commands import snapshot as snapshot_cmd
from fork_linux.config import Config
from fork_linux.locking import FileLock
from fork_linux.state import FORK_VERSION, State

from fixtures.cli_run import fork_running, isolate, layout, paths, run_cli, run_json
from fixtures.fork_tree import install_fork, write_settings


@pytest.fixture(autouse=True)
def _isolated(xdg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    isolate(monkeypatch)
    fork_running(monkeypatch, False)
    clock = [datetime(2026, 1, 1, tzinfo=timezone.utc)]

    def now() -> datetime:
        clock[0] += timedelta(minutes=1)
        return clock[0]

    monkeypatch.setattr(snapshots, "_now", now)


def _snapshot(version: str) -> snapshots.Snapshot:
    """Install ``version`` and snapshot it."""
    install_fork(layout(), version)
    return snapshots.create(paths(), layout(), method="copy", reason="test")


def test_create_needs_an_installed_fork(capsys: pytest.CaptureFixture[str]) -> None:
    code, _out, err = run_cli(capsys, "snapshot", "create")
    assert code == 10
    assert "not installed" in err


def test_create_list_delete(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _err = run_cli(capsys, "snapshot", "list")
    assert (code, out) == (0, "no snapshots\n")
    install_fork(layout(), "2.23.2")
    code, out, _err = run_cli(capsys, "snapshot", "create")
    assert code == 0
    assert out.startswith("created snapshot 2.23.2-")
    made = run_json(capsys, "snapshot", "create", "--reason", "x")
    assert made["fork_version"] == "2.23.2"
    assert set(made) == {"id", "fork_version", "created", "method", "path", "with_settings"}
    listed = run_json(capsys, "snapshot", "list")
    assert [item["id"] for item in listed][0] == made["id"]
    assert len(listed) == 2
    code, out, _err = run_cli(capsys, "snapshot", "list")
    assert out.splitlines()[0].startswith("ID")
    assert made["id"] in out
    code, out, _err = run_cli(capsys, "snapshot", "delete", made["id"])
    assert (code, out) == (0, f"deleted snapshot {made['id']}\n")
    other = listed[1]["id"]
    assert run_json(capsys, "snapshot", "delete", other) == {"deleted": [other]}
    code, _out, err = run_cli(capsys, "snapshot", "delete", other)
    assert code == 20


def test_create_respects_the_lock(capsys: pytest.CaptureFixture[str]) -> None:
    install_fork(layout(), "2.23.2")
    with FileLock(paths().lock_file, "setup"):
        assert run_cli(capsys, "snapshot", "create")[0] == 16


def test_prune(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    for version in ("2.21.0", "2.22.0", "2.23.0"):
        _snapshot(version)
    monkeypatch.setenv("FORK_LINUX_SNAPSHOTS_KEEP", "1")
    code, out, _err = run_cli(capsys, "snapshot", "prune")
    assert code == 0
    assert out.startswith("deleted 2 snapshot(s): ")
    assert run_json(capsys, "snapshot", "prune") == {"deleted": []}
    code, out, _err = run_cli(capsys, "snapshot", "prune")
    assert out == "deleted 0 snapshot(s)\n"
    layout().sq_version_file.unlink()
    monkeypatch.setenv("FORK_LINUX_SNAPSHOTS_KEEP", "0")
    assert len(run_json(capsys, "snapshot", "prune")["deleted"]) == 1


def test_rollback_to_the_previous_version_pins_it(capsys: pytest.CaptureFixture[str]) -> None:
    old = _snapshot("2.23.1")
    _snapshot("2.23.2")
    layout().packages_dir.joinpath("Fork-2.24.0-full.nupkg").write_bytes(b"PK")
    code, out, _err = run_cli(capsys, "rollback")
    assert code == 0
    assert f"restored Fork 2.23.1 from snapshot {old.id}" in out
    assert "pinned to 2.23.1" in out
    assert layout().installed_version() == "2.23.1"
    state = State.load(paths().state_file)
    assert state.get(snapshot_cmd.PINNED_VERSION) == "2.23.1"
    assert state.get(FORK_VERSION) == "2.23.1"
    assert state.get(updates.LAST_SEEN_VERSION) == "2.23.1"
    assert Config.load(paths(), {}).get("fork", "update_policy") == "pinned"
    assert not layout().packages_dir.joinpath("Fork-2.24.0-full.nupkg").exists()


def test_rollback_by_id_version_and_options(capsys: pytest.CaptureFixture[str]) -> None:
    first = _snapshot("2.23.1")
    _snapshot("2.23.2")
    write_settings(layout(), {"Theme": 1})
    result = run_json(capsys, "rollback", first.id, "--no-pin", "--with-settings")
    assert result["restored"]["id"] == first.id
    assert result["previous_version"] == "2.23.2"
    assert result["pinned"] is False
    assert result["with_settings"] is True
    assert Config.load(paths(), {}).get("fork", "update_policy") == "auto"
    code, out, _err = run_cli(capsys, "rollback", "--to-version", "2.23.2", "--no-pin")
    assert code == 0
    assert "may update itself again" in out
    assert layout().installed_version() == "2.23.2"
    code, _out, err = run_cli(capsys, "rollback", "--to-version", "x.y")
    assert code == 2
    assert "not a Fork version" in err
    code, _out, _err = run_cli(capsys, "rollback", "--to-version", "1.0")
    assert code == 20
    code, _out, err = run_cli(capsys, "rollback", first.id, "--to-version", "2.23.1")
    assert code == 2


def test_rollback_without_another_version(capsys: pytest.CaptureFixture[str]) -> None:
    _snapshot("2.23.2")
    code, _out, err = run_cli(capsys, "rollback")
    assert code == 20
    assert "no snapshot of another Fork version" in err


def test_rollback_when_nothing_is_installed_takes_the_newest(capsys: pytest.CaptureFixture[str]) -> None:
    snap = _snapshot("2.23.1")
    layout().sq_version_file.unlink()
    assert run_json(capsys, "rollback", "--no-pin")["restored"]["id"] == snap.id
    assert layout().installed_version() == "2.23.1"


def test_rollback_needs_fork_closed(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    _snapshot("2.23.1")
    _snapshot("2.23.2")
    fork_running(monkeypatch, True)
    code, _out, err = run_cli(capsys, "rollback")
    assert code == 15
    assert layout().installed_version() == "2.23.2"


def test_rollback_saves_an_unsnapshotted_installed_version(capsys: pytest.CaptureFixture[str]) -> None:
    old = _snapshot("2.23.1")
    # Fork updated itself and no launch has snapshotted the new version yet.
    install_fork(layout(), "2.23.2")
    code, out, _err = run_cli(capsys, "rollback", "--no-pin")
    assert code == 0
    assert f"restored Fork 2.23.1 from snapshot {old.id}" in out
    assert "Fork 2.23.2 was saved first as snapshot 2.23.2-" in out
    assert layout().installed_version() == "2.23.1"
    # ... so the newer version can be restored again.
    result = run_json(capsys, "rollback", "--to-version", "2.23.2", "--no-pin")
    assert result["saved"] is None
    assert layout().installed_version() == "2.23.2"
    # Restoring the installed version itself (a repair) saves nothing either.
    assert run_json(capsys, "rollback", result["restored"]["id"], "--no-pin")["saved"] is None
    assert len(snapshots.list_snapshots(paths())) == 2
