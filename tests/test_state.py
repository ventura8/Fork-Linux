"""Tests for fork_linux.state: the per-prefix setup state file."""

from __future__ import annotations

import errno
import json
import os
import stat
from pathlib import Path

import pytest

from fork_linux import state as state_mod
from fork_linux.errors import ExitCode, ForkLinuxError, IntegrityFailed
from fork_linux.state import SCHEMA, State


@pytest.fixture
def path(tmp_path: Path) -> Path:
    return tmp_path / "prefix" / ".fork-linux" / "state.json"


def _write(path: Path, content: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, str):
        content = content.encode("utf-8")
    path.write_bytes(content)


def test_missing_file_gives_fresh_state(path: Path) -> None:
    state = State.load(path)
    assert state.is_new is True
    assert state.data == {"schema": SCHEMA, "steps": {}}
    assert state.path == path
    assert not path.exists()


def test_save_round_trip_is_private_and_atomic(path: Path) -> None:
    state = State.load(path)
    state.set("fork.version", "2.23.2")
    state.set(state_mod.SETUP_COMPLETE, True)
    state.save()
    assert state.is_new is False
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert [p.name for p in path.parent.iterdir()] == ["state.json"]
    loaded = State.load(path)
    assert loaded.is_new is False
    assert loaded.get(state_mod.FORK_VERSION) == "2.23.2"
    assert loaded.get("setup.complete") is True
    assert json.loads(path.read_text(encoding="utf-8"))["schema"] == SCHEMA
    assert path.read_text(encoding="utf-8").endswith("}\n")


def test_constructor_defaults() -> None:
    state = State(Path("/nonexistent/state.json"))
    assert state.data == {"schema": SCHEMA, "steps": {}}
    assert state.is_new is False


def test_get_dotted_keys_and_defaults(path: Path) -> None:
    state = State.load(path)
    state.set("a.b.c", 1)
    assert state.get("a.b.c") == 1
    assert state.get("a.b") == {"c": 1}
    assert state.get("a.missing") is None
    assert state.get("a.missing", "dflt") == "dflt"
    assert state.get("a.b.c.d", 5) == 5  # walking into a non-object
    assert state.get("schema") == SCHEMA


def test_set_overwrites_and_rejects_non_object_parents(path: Path) -> None:
    state = State.load(path)
    state.set("wine.provider", "managed")
    state.set("wine.provider", "system")
    assert state.get("wine.provider") == "system"
    with pytest.raises(TypeError):
        state.set("wine.provider.sub", 1)


@pytest.mark.parametrize("key", ["", ".", "a.", ".a", "a..b"])
def test_invalid_keys(path: Path, key: str) -> None:
    state = State.load(path)
    with pytest.raises(ValueError):
        state.get(key)
    with pytest.raises(ValueError):
        state.set(key, 1)


def test_step_markers(path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(state_mod, "_now", lambda: "2026-10-09T08:00:00+00:00")
    state = State.load(path)
    assert state.step_marker("wine_runtime") is None
    marker = state.set_step_marker("wine_runtime", 2, "abc123")
    assert marker == {"rev": 2, "inputs_hash": "abc123", "completed": "2026-10-09T08:00:00+00:00"}
    got = state.step_marker("wine_runtime")
    assert got == marker
    got["rev"] = 99  # a copy: callers cannot corrupt the state by accident
    assert state.step_marker("wine_runtime")["rev"] == 2
    state.save()
    assert State.load(path).step_marker("wine_runtime") == marker
    state.clear_step_marker("wine_runtime")
    state.clear_step_marker("never_ran")
    assert state.step_marker("wine_runtime") is None


def test_step_marker_ignores_non_object_entries(path: Path) -> None:
    _write(path, json.dumps({"schema": 1, "steps": {"x": "garbage"}}))
    assert State.load(path).step_marker("x") is None


def test_steps_object_is_recreated_if_replaced(path: Path) -> None:
    state = State.load(path)
    state.data["steps"] = "oops"
    assert state.step_marker("a") is None
    state.set_step_marker("a", 1, "h")
    assert isinstance(state.data["steps"], dict)


def test_real_timestamp_format(path: Path) -> None:
    marker = State.load(path).set_step_marker("s", 1, "h")
    assert "T" in marker["completed"]
    assert marker["completed"][-6] in "+-"


def test_missing_steps_key_is_added(path: Path) -> None:
    _write(path, json.dumps({"schema": 1, "fork": {"version": "2.23.2"}}))
    state = State.load(path)
    assert state.data["steps"] == {}
    assert state.get("fork.version") == "2.23.2"


@pytest.mark.parametrize(
    ("content", "fragment"),
    [
        ("{not json", "not valid JSON"),
        (b"\xff\xfe\x00garbage", "not valid JSON"),
        ("[1, 2]", "not a JSON object"),
        ("{}", "no valid schema number"),
        ('{"schema": "1"}', "no valid schema number"),
        ('{"schema": true}', "no valid schema number"),
        ('{"schema": 0}', "no valid schema number"),
        ('{"schema": -3}', "no valid schema number"),
        ('{"schema": 1, "steps": []}', "'steps' is not a JSON object"),
    ],
)
def test_corrupt_files_raise_integrity_failed_with_hint(path: Path, content: str | bytes, fragment: str) -> None:
    _write(path, content)
    with pytest.raises(IntegrityFailed) as info:
        State.load(path)
    assert fragment in info.value.message
    assert str(path) in info.value.hint
    assert info.value.exit_code == ExitCode.INTEGRITY_FAILED


def test_oversized_file_is_refused_without_reading_it_whole(path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(state_mod, "MAX_BYTES", 64)
    _write(path, json.dumps({"schema": 1, "steps": {}, "pad": "x" * 100}))
    with pytest.raises(IntegrityFailed, match="larger than 64 bytes"):
        State.load(path)
    _write(path, json.dumps({"schema": 1, "steps": {}}))
    assert State.load(path).data == {"schema": 1, "steps": {}}


def test_newer_schema_is_refused(path: Path) -> None:
    _write(path, json.dumps({"schema": SCHEMA + 1, "steps": {}}))
    with pytest.raises(IntegrityFailed) as info:
        State.load(path)
    assert "newer" in info.value.message
    assert "update fork-linux" in info.value.hint


def test_symlinked_state_file_is_refused(path: Path, tmp_path: Path) -> None:
    target = tmp_path / "elsewhere.json"
    target.write_text('{"schema": 1, "steps": {}}', encoding="utf-8")
    path.parent.mkdir(parents=True)
    path.symlink_to(target)
    with pytest.raises(IntegrityFailed, match="symbolic link"):
        State.load(path)


def test_unopenable_file_raises_fork_linux_error(path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write(path, "{}")

    def denied(*args: object, **kwargs: object) -> int:
        raise PermissionError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(state_mod.os, "open", denied)
    with pytest.raises(ForkLinuxError) as info:
        State.load(path)
    assert not isinstance(info.value, IntegrityFailed)
    assert "Permission denied" in info.value.message


def test_unreadable_directory_raises_fork_linux_error(path: Path) -> None:
    path.mkdir(parents=True)
    with pytest.raises(ForkLinuxError) as info:
        State.load(path)
    assert not isinstance(info.value, IntegrityFailed)
    assert str(path) in info.value.message


def test_save_replaces_a_symlink_instead_of_following_it(path: Path, tmp_path: Path) -> None:
    target = tmp_path / "victim.json"
    target.write_text("untouched", encoding="utf-8")
    path.parent.mkdir(parents=True)
    path.symlink_to(target)
    state = State(path)
    state.save()
    assert not path.is_symlink()
    assert target.read_text(encoding="utf-8") == "untouched"
    assert os.stat(path).st_mode & 0o777 == 0o600
