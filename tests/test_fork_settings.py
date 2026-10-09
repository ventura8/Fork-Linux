"""Tests for fork_linux.fork_settings: Fork's settings.json, merged safely while Fork is closed."""

from __future__ import annotations

import json
import logging
import os
import stat
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from fixtures.fork_tree import USER, make_layout, write_settings
from fork_linux import fork_settings as fs
from fork_linux.config import Config
from fork_linux.errors import ForkLinuxError, IntegrityFailed
from fork_linux.fork_layout import ForkLayout

OUR_SHELL = {"Type": "Custom", "ApplicationPath": "C:\\fork-linux\\fl-launch.exe", "Arguments": "terminal"}


@pytest.fixture
def layout(tmp_path: Path) -> ForkLayout:
    return make_layout(tmp_path)


@pytest.fixture
def backups(tmp_path: Path) -> Path:
    return tmp_path / "backups"


def _config(tmp_path: Path, **values: str) -> Config:
    file_values = {tuple(key.split("__", 1)): value for key, value in values.items()}
    return Config(tmp_path / "config.ini", file_values, {})


def _clock(monkeypatch: pytest.MonkeyPatch, *moments: datetime) -> None:
    queue = list(moments)
    monkeypatch.setattr(fs, "_utcnow", lambda: queue.pop(0) if len(queue) > 1 else queue[0])


def test_enforced_defaults_and_keys() -> None:
    assert fs.ENFORCED_DEFAULTS == {"UpdateSubmodulesOnCheckout": False, "DisableHardwareAcceleration": True}
    assert fs.SOURCE_DIRECTORIES == "RepositoryManager.SourceDirectories"
    assert (fs.THEME, fs.FOLLOW_SYSTEM_THEME, fs.LAYOUT_SCALING, fs.SHELL_TOOL) == (
        "Theme",
        "FollowSystemTheme",
        "LayoutScaling",
        "ShellTool",
    )


def test_default_backup_dir(layout: ForkLayout) -> None:
    assert fs.default_backup_dir(layout.paths) == layout.paths.data_dir / "settings-backups"


def test_utcnow_is_aware() -> None:
    assert fs._utcnow().tzinfo is timezone.utc


# -- load ----------------------------------------------------------------------------


def test_load_missing_gives_empty(tmp_path: Path) -> None:
    assert fs.load(tmp_path / "settings.json") == {}


def test_load_reads_object_with_bom(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps({"Theme": 1, "Name": "\u00e9"}).encode("utf-8"))
    assert fs.load(path) == {"Theme": 1, "Name": "\u00e9"}


@pytest.mark.parametrize(
    "raw",
    [b"{not json", b"[1, 2]", b"\xff\xfe\x00garbage", b'"text"', b""],
)
def test_load_corrupt_raises_with_restore_hint(tmp_path: Path, raw: bytes) -> None:
    path = tmp_path / "settings.json"
    path.write_bytes(raw)
    with pytest.raises(IntegrityFailed) as info:
        fs.load(path)
    assert "fork-linux settings restore" in info.value.hint


def test_load_oversized(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "settings.json"
    path.write_text('{"a": 1}')
    monkeypatch.setattr(fs, "MAX_BYTES", 4)
    with pytest.raises(IntegrityFailed, match="larger than"):
        fs.load(path)


def test_load_non_regular_files(tmp_path: Path) -> None:
    folder = tmp_path / "settings.json"
    folder.mkdir()
    with pytest.raises(IntegrityFailed, match="not a regular file"):
        fs.load(folder)
    fifo = tmp_path / "fifo.json"
    os.mkfifo(fifo)
    with pytest.raises(IntegrityFailed, match="not a regular file"):
        fs.load(fifo)


def test_load_unreadable(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x")
    with pytest.raises(ForkLinuxError, match="cannot read") as info:
        fs.load(blocker / "settings.json")
    assert not isinstance(info.value, IntegrityFailed)


# -- get / merge_set --------------------------------------------------------------


def test_get_dotted() -> None:
    data = {"RepositoryManager": {"SourceDirectories": ["C:\\x"]}, "Theme": 0, "ShellTool": None}
    assert fs.get(data, "Theme") == 0
    assert fs.get(data, "RepositoryManager.SourceDirectories") == ["C:\\x"]
    assert fs.get(data, "Missing", "dflt") == "dflt"
    assert fs.get(data, "Theme.Sub", 5) == 5
    assert fs.get(data, "ShellTool", "dflt") is None
    with pytest.raises(ValueError, match="invalid settings key"):
        fs.get(data, "a..b")


def test_merge_set_reports_changes() -> None:
    data: dict[str, Any] = {"Theme": 0, "Flag": 1, "Scale": 100}
    assert fs.merge_set(data, "Theme", 0) is False
    assert fs.merge_set(data, "Theme", 1) is True
    assert fs.merge_set(data, "Flag", True) is True  # JSON true differs from 1
    assert fs.merge_set(data, "Scale", 100.0) is True
    assert fs.merge_set(data, "New", False) is True
    assert data == {"Theme": 1, "Flag": True, "Scale": 100.0, "New": False}


def test_merge_set_creates_intermediate_objects_and_copies() -> None:
    data: dict[str, Any] = {"RepositoryManager": None, "Other": {"Keep": 1}}
    value = ["Z:\\home\\tester"]
    assert fs.merge_set(data, "RepositoryManager.SourceDirectories", value) is True
    assert fs.merge_set(data, "Other.Deep.Key", 2) is True
    assert fs.merge_set(data, "Brand.New", 3) is True
    value.append("mutated")
    assert data == {
        "RepositoryManager": {"SourceDirectories": ["Z:\\home\\tester"]},
        "Other": {"Keep": 1, "Deep": {"Key": 2}},
        "Brand": {"New": 3},
    }
    assert fs.merge_set(data, "RepositoryManager.SourceDirectories", ["Z:\\home\\tester"]) is False


def test_merge_set_refuses_scalar_intermediate() -> None:
    with pytest.raises(IntegrityFailed, match="not an object"):
        fs.merge_set({"RepositoryManager": 5}, "RepositoryManager.SourceDirectories", [])


# -- backups ---------------------------------------------------------------------------


def test_list_backups(backups: Path) -> None:
    assert fs.list_backups(backups) == []
    backups.mkdir()
    names = [
        "settings.json.20261009T100000.000000Z",
        "settings.json.20261009T110000.000000Z",
        "settings.json.20261009T110000.000000Z-1",
        "settings.json.20261009T110000.000000Z-2",
        "settings.json.bogus",
        "notes.txt",
    ]
    for name in names:
        (backups / name).write_text("{}")
    (backups / "settings.json.20261009T120000.000000Z").mkdir()
    assert [p.name for p in fs.list_backups(backups)] == [
        "settings.json.20261009T110000.000000Z-2",
        "settings.json.20261009T110000.000000Z-1",
        "settings.json.20261009T110000.000000Z",
        "settings.json.20261009T100000.000000Z",
    ]


def test_backup_validates_keep(tmp_path: Path, backups: Path) -> None:
    with pytest.raises(ValueError, match="keep"):
        fs.backup(tmp_path / "settings.json", backup_dir=backups, keep=0)


def test_backup_missing_file(tmp_path: Path, backups: Path) -> None:
    assert fs.backup(tmp_path / "settings.json", backup_dir=backups) is None
    assert not backups.exists()


def test_backup_copies_privately_and_rotates(
    tmp_path: Path, backups: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "settings.json"
    stamps = [datetime(2026, 10, 9, 10, minute, 0, 5, tzinfo=timezone.utc) for minute in range(5)]
    _clock(monkeypatch, *stamps)
    made = []
    for number in range(5):
        path.write_text(json.dumps({"n": number}))
        made.append(fs.backup(path, backup_dir=backups, keep=2))
    assert made[0] is not None and made[0].name == "settings.json.20261009T100000.000005Z"
    assert stat.S_IMODE(backups.stat().st_mode) == 0o700
    remaining = fs.list_backups(backups)
    assert remaining == [made[4], made[3]]
    assert json.loads(remaining[0].read_text()) == {"n": 4}
    assert stat.S_IMODE(remaining[0].stat().st_mode) == 0o600


def test_backup_name_collision_gets_counter(tmp_path: Path, backups: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "settings.json"
    path.write_text("{}")
    _clock(monkeypatch, datetime(2026, 10, 9, 12, 0, 0, tzinfo=timezone.utc))
    first = fs.backup(path, backup_dir=backups)
    second = fs.backup(path, backup_dir=backups)
    third = fs.backup(path, backup_dir=backups)
    assert first is not None and second is not None and third is not None
    assert second.name == first.name + "-1"
    assert third.name == first.name + "-2"
    assert fs.list_backups(backups) == [third, second, first]


def test_backup_refuses_symlink_and_non_regular(tmp_path: Path, backups: Path) -> None:
    real = tmp_path / "real.json"
    real.write_text("{}")
    link = tmp_path / "settings.json"
    link.symlink_to(real)
    with pytest.raises(IntegrityFailed, match="symbolic link"):
        fs.backup(link, backup_dir=backups)
    folder = tmp_path / "dir.json"
    folder.mkdir()
    with pytest.raises(IntegrityFailed, match="not a regular file"):
        fs.backup(folder, backup_dir=backups)


# -- save --------------------------------------------------------------------------------


def test_save_new_file(tmp_path: Path, backups: Path) -> None:
    path = tmp_path / "Fork" / "settings.json"
    assert fs.save(path, {"Name": "\u00e9t\u00e9", "Theme": 1}, backup_dir=backups) is None
    text = path.read_text(encoding="utf-8")
    assert text == '{\n  "Name": "\u00e9t\u00e9",\n  "Theme": 1\n}\n'
    assert stat.S_IMODE(path.stat().st_mode) == 0o644


def test_save_backs_up_existing(tmp_path: Path, backups: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text('{"Theme": 0}')
    saved = fs.save(path, {"Theme": 1}, backup_dir=backups, keep=1)
    assert saved is not None and saved.read_text() == '{"Theme": 0}'
    assert json.loads(path.read_text()) == {"Theme": 1}


def test_save_refuses_symlink(tmp_path: Path, backups: Path) -> None:
    real = tmp_path / "real.json"
    real.write_text('{"Theme": 0}')
    link = tmp_path / "settings.json"
    link.symlink_to(real)
    with pytest.raises(IntegrityFailed):
        fs.save(link, {"Theme": 1}, backup_dir=backups)
    assert real.read_text() == '{"Theme": 0}'
    assert link.is_symlink()


# -- desired -----------------------------------------------------------------------------


def _desired(config: Config, **overrides: Any) -> dict[str, object]:
    options: dict[str, Any] = {
        "user": USER,
        "theme": None,
        "tools": {},
        "home_win": None,
        "current": {},
    }
    options.update(overrides)
    return fs.desired(config, **options)


def test_desired_defaults_enforce_wine_safe_keys(tmp_path: Path) -> None:
    assert _desired(_config(tmp_path)) == {"UpdateSubmodulesOnCheckout": False, "DisableHardwareAcceleration": True}


def test_desired_enforce_list_unknown_and_empty(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    config = _config(tmp_path, fork__enforce_settings="DisableHardwareAcceleration, Bogus")
    with caplog.at_level(logging.WARNING, logger="fork_linux.fork_settings"):
        assert _desired(config) == {"DisableHardwareAcceleration": True}
    assert "Bogus" in caplog.text
    assert _desired(_config(tmp_path, fork__enforce_settings="")) == {}


@pytest.mark.parametrize(
    ("mode", "theme", "expected"),
    [
        ("follow", "dark", {"Theme": 1, "FollowSystemTheme": False}),
        ("follow", "light", {"Theme": 0, "FollowSystemTheme": False}),
        ("follow", None, {}),
        ("follow", "purple", {}),
        ("dark", None, {"Theme": 1, "FollowSystemTheme": False}),
        ("Light", "dark", {"Theme": 0, "FollowSystemTheme": False}),
        ("off", "dark", {}),
        ("weird", "dark", {}),
    ],
)
def test_desired_theme(tmp_path: Path, mode: str, theme: str | None, expected: dict[str, object]) -> None:
    config = _config(tmp_path, fork__enforce_settings="", display__theme=mode)
    assert _desired(config, theme=theme) == expected


@pytest.mark.parametrize(
    ("dpi", "scale", "expected"),
    [
        ("auto", 125, 125),
        ("auto", 1000, 300),
        ("auto", 50, 100),
        ("auto", None, None),
        ("auto", 0, None),
        ("auto", True, None),
        ("120", None, 125),
        ("144", 200, 150),
        ("192", None, 200),
        ("110", None, 115),
        ("480", None, 300),
        ("72", None, 100),
        ("0", None, None),
        ("abc", 125, None),
        ("\u00b2", None, None),
        ("12345", None, None),
    ],
)
def test_legacy_scaling(dpi: str, scale: Any, expected: int | None) -> None:
    assert fs.legacy_scaling(dpi, scale) == expected


def test_desired_never_sets_layout_scaling_by_itself(tmp_path: Path) -> None:
    config = _config(tmp_path, fork__enforce_settings="", display__theme="off", display__dpi="144")
    assert _desired(config, current={"LayoutScaling": 100}) == {}


@pytest.mark.parametrize(
    ("current", "reset", "expected"),
    [
        ({"LayoutScaling": 150}, 150, {"LayoutScaling": 100}),
        ({"LayoutScaling": 125}, 150, {}),
        ({"LayoutScaling": 100}, 100, {}),
        ({}, 150, {}),
        ({"LayoutScaling": 150}, None, {}),
    ],
)
def test_desired_resets_an_old_layout_scaling(
    tmp_path: Path, current: dict[str, Any], reset: int | None, expected: dict[str, object]
) -> None:
    config = _config(tmp_path, fork__enforce_settings="", display__theme="off")
    assert _desired(config, current=current, reset_scaling=reset) == expected


TERMINAL = {"Type": "Custom", "ApplicationPath": "Z:\\opt\\fl\\fork-linux-terminal", "Arguments": ""}
DIFF = {"Type": "Custom", "ApplicationPath": "C:\\fork-linux\\bin\\fl-launch.exe", "Arguments": "diff"}
EMPTY_TOOL = {"Type": "Custom", "ApplicationPath": "", "Arguments": ""}


@pytest.mark.parametrize(
    ("current", "replaced"),
    [
        ({}, True),
        ({"ShellTool": None}, True),
        ({"ShellTool": {"Type": "CommandPrompt"}}, True),
        ({"ShellTool": {"Type": "Custom", "ApplicationPath": "c:\\FORK-LINUX\\old.exe", "Arguments": ""}}, True),
        ({"ShellTool": {"Type": "Custom", "ApplicationPath": "Z:\\x\\fork-linux-terminal", "Arguments": ""}}, True),
        ({"ShellTool": {"Type": "Custom", "ApplicationPath": "C:\\tools\\wt.exe", "Arguments": ""}}, False),
        ({"ShellTool": {"Type": "Custom", "ApplicationPath": None}}, False),
        ({"ShellTool": "cmd"}, False),
    ],
)
def test_desired_shell_tool(tmp_path: Path, current: dict[str, Any], replaced: bool) -> None:
    config = _config(tmp_path, fork__enforce_settings="", display__theme="off", display__dpi="0")
    wanted = _desired(config, tools={"ShellTool": TERMINAL}, current=current)
    assert wanted == ({"ShellTool": TERMINAL} if replaced and current.get("ShellTool") != TERMINAL else {})


@pytest.mark.parametrize(
    ("current", "expected"),
    [
        ({"ShellTool": OUR_SHELL}, {"ShellTool": None}),
        ({"ShellTool": TERMINAL}, {"ShellTool": None}),
        ({"ShellTool": {"Type": "CommandPrompt"}}, {}),
        ({"ShellTool": None}, {}),
        ({"MergeTool": DIFF}, {"MergeTool": EMPTY_TOOL}),
        ({"ExternalDiffTool": DIFF}, {"ExternalDiffTool": EMPTY_TOOL}),
        ({"ExternalDiffTool": {"Type": "Custom", "ApplicationPath": "C:\\bc\\bc.exe", "Arguments": ""}}, {}),
    ],
)
def test_desired_restores_our_dead_tools(
    tmp_path: Path, current: dict[str, Any], expected: dict[str, object]
) -> None:
    config = _config(tmp_path, fork__enforce_settings="", display__theme="off")
    assert _desired(config, tools={}, current=current) == expected


@pytest.mark.parametrize(
    ("current", "replaced"),
    [
        ({}, True),
        ({"MergeTool": EMPTY_TOOL}, True),
        ({"MergeTool": DIFF}, True),
        ({"MergeTool": {"Type": "Custom", "ApplicationPath": "C:\\km\\kdiff3.exe", "Arguments": ""}}, False),
        ({"MergeTool": {"Type": "BeyondCompare"}}, False),
        ({"MergeTool": "x"}, False),
    ],
)
def test_desired_merge_tool(tmp_path: Path, current: dict[str, Any], replaced: bool) -> None:
    config = _config(tmp_path, fork__enforce_settings="", display__theme="off")
    merge = {"Type": "Custom", "ApplicationPath": "C:\\fork-linux\\bin\\fl-launch.exe", "Arguments": "merge"}
    wanted = _desired(config, tools={"MergeTool": merge}, current=current)
    assert wanted == ({"MergeTool": merge} if replaced else {})


@pytest.mark.parametrize(
    ("dirs", "replaced"),
    [
        (["C:\\users\\tester"], True),
        (["c:\\Users\\Tester\\"], True),
        (["C:\\users\\someone"], False),
        (["C:\\users\\tester", "D:\\src"], False),
        ([5], False),
        ("C:\\users\\tester", False),
        (None, False),
    ],
)
def test_desired_source_directories(tmp_path: Path, dirs: Any, replaced: bool) -> None:
    config = _config(tmp_path, fork__enforce_settings="", display__theme="off", display__dpi="0")
    current = {"RepositoryManager": {"SourceDirectories": dirs, "Other": 1}}
    wanted = _desired(config, home_win="Z:\\home\\tester", current=current)
    assert wanted == ({"RepositoryManager.SourceDirectories": ["Z:\\home\\tester"]} if replaced else {})
    assert _desired(config, home_win=None, current=current) == {}


# -- apply ---------------------------------------------------------------------------------


def test_apply_leaves_missing_file_alone(layout: ForkLayout, backups: Path) -> None:
    assert fs.apply(layout, {"Theme": 1}, backup_dir=backups) == []
    assert not layout.settings_file.exists()


def test_apply_create(layout: ForkLayout, backups: Path) -> None:
    wanted = {"Theme": 1, "RepositoryManager.SourceDirectories": ["Z:\\"]}
    changed = fs.apply(layout, wanted, backup_dir=backups, create=True)
    assert changed == ["Theme", "RepositoryManager.SourceDirectories"]
    assert json.loads(layout.settings_file.read_text()) == {
        "Theme": 1,
        "RepositoryManager": {"SourceDirectories": ["Z:\\"]},
    }
    assert fs.list_backups(backups) == []


def test_seed_creates_the_file_with_a_guid(layout: ForkLayout, backups: Path) -> None:
    changed = fs.seed(layout, {"Theme": 1}, backup_dir=backups)
    assert changed == ["Guid", "Theme"]
    data = json.loads(layout.settings_file.read_text())
    assert data["Theme"] == 1 and str(uuid.UUID(data["Guid"])) == data["Guid"]
    # An existing file is merged as by apply(): its Guid is kept.
    assert fs.seed(layout, {"Theme": 0}, backup_dir=backups) == ["Theme"]
    again = json.loads(layout.settings_file.read_text())
    assert again == {"Guid": data["Guid"], "Theme": 0}
    assert len(fs.list_backups(backups)) == 1


def test_apply_merges_preserving_unknown_keys(layout: ForkLayout, backups: Path) -> None:
    original = {
        "Guid": "0f8fad5b-d9cb-469f-a165-70867728950e",
        "Theme": 0,
        "UpdateSubmodulesOnCheckout": True,
        "Workspaces": [{"Name": "Default", "Repos": [1, 2]}],
        "RepositoryManager": {"SourceDirectories": ["C:\\users\\tester"], "Depth": 3},
        "Future": {"Nested": [None, 1.5]},
    }
    write_settings(layout, original)
    wanted = {
        "Theme": 1,
        "UpdateSubmodulesOnCheckout": False,
        "DisableHardwareAcceleration": True,
        "RepositoryManager.SourceDirectories": ["Z:\\home\\tester"],
        "Guid": original["Guid"],
    }
    changed = fs.apply(layout, wanted, backup_dir=backups)
    assert changed == [
        "Theme",
        "UpdateSubmodulesOnCheckout",
        "DisableHardwareAcceleration",
        "RepositoryManager.SourceDirectories",
    ]
    result = json.loads(layout.settings_file.read_text())
    assert result["Workspaces"] == original["Workspaces"]
    assert result["Future"] == original["Future"]
    assert result["RepositoryManager"] == {"SourceDirectories": ["Z:\\home\\tester"], "Depth": 3}
    assert result["Theme"] == 1 and result["DisableHardwareAcceleration"] is True
    saved = fs.list_backups(backups)
    assert len(saved) == 1 and json.loads(saved[0].read_text()) == original


def test_apply_without_changes_does_not_write(layout: ForkLayout, backups: Path) -> None:
    write_settings(layout, {"Theme": 1})
    before = layout.settings_file.stat()
    assert fs.apply(layout, {"Theme": 1}, backup_dir=backups) == []
    after = layout.settings_file.stat()
    assert (before.st_ino, before.st_mtime_ns) == (after.st_ino, after.st_mtime_ns)
    assert not backups.exists()


def test_apply_refuses_dangling_symlink(layout: ForkLayout, backups: Path, tmp_path: Path) -> None:
    layout.local_dir.mkdir(parents=True)
    layout.settings_file.symlink_to(tmp_path / "nowhere.json")
    with pytest.raises(IntegrityFailed, match="symbolic link"):
        fs.apply(layout, {"Theme": 1}, backup_dir=backups)
    assert not (tmp_path / "nowhere.json").exists()
