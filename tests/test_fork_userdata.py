"""fork_userdata: the user's files in Fork's install directory survive the official installer (F2, F3, F20)."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from fork_linux import fork_userdata, launcher
from fork_linux.errors import ForkLinuxError
from fork_linux.fork_layout import ForkLayout

from fixtures.fork_tree import install_fork, make_layout, run_velopack_setup
from fixtures.setup_ctx import make_ctx

USER_FILES = {
    "settings.json": b'\xef\xbb\xbf{"Theme": 1, "Unknown": {"kept": true}}',
    "accounts.json": b'{"accounts": ["secret"]}',
    "custom-commands.json": b'[{"name": "mine"}]',
    "logs/fork.log": b"the user's session log\n",
    "logs/fork-2026-10-01.log": b"an older log\n",
    "gitInstance/2.50.1/cmd/git.exe": b"MZ bundled git",
}


def _layout(tmp_path: Path) -> ForkLayout:
    layout = make_layout(tmp_path)
    layout.paths.prefix_meta_dir.mkdir(parents=True)
    install_fork(layout, "2.23.1")
    for rel, data in USER_FILES.items():
        target = layout.local_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return layout


def _user_files(layout: ForkLayout) -> dict[str, bytes]:
    found = {}
    for rel in USER_FILES:
        path = layout.local_dir / rel
        found[rel] = path.read_bytes() if path.is_file() else b"<missing>"
    return found


def test_entries_are_everything_but_velopacks_own(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    (layout.local_dir / "VeloPack.log").write_text("case does not matter", encoding="utf-8")
    assert fork_userdata.user_entries(layout) == [
        "accounts.json",
        "custom-commands.json",
        "gitInstance",
        "logs",
        "settings.json",
    ]
    assert fork_userdata.stash_dir(layout.paths) == layout.paths.prefix / ".fork-linux" / "fork-user-data"


def test_no_entries_without_a_real_install_directory(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    assert fork_userdata.user_entries(layout) == []
    real = tmp_path / "elsewhere"
    (real / "settings.json").parent.mkdir(parents=True)
    (real / "settings.json").write_text("{}", encoding="utf-8")
    layout.local_dir.parent.mkdir(parents=True)
    layout.local_dir.symlink_to(real)
    assert fork_userdata.user_entries(layout) == []
    with fork_userdata.kept(layout) as moved:
        assert moved == []
    assert (real / "settings.json").is_file()


def test_the_installers_wipe_keeps_the_users_bytes(tmp_path: Path) -> None:
    """The real installer deletes AppData\\Local\\Fork; every user file comes back byte for byte."""
    layout = _layout(tmp_path)
    before = _user_files(layout)
    with fork_userdata.kept(layout) as moved:
        assert moved == ["accounts.json", "custom-commands.json", "gitInstance", "logs", "settings.json"]
        assert not layout.settings_file.exists()
        run_velopack_setup(layout, "2.23.2")
    assert _user_files(layout) == before
    assert layout.installed_version() == "2.23.2"
    assert layout.velopack_log.read_text(encoding="utf-8") == "Velopack setup of Fork 2.23.2\n"
    assert not fork_userdata.stash_dir(layout.paths).exists()


def test_merged_directories_keep_the_installers_other_files(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    with fork_userdata.kept(layout):
        run_velopack_setup(layout, "2.23.2")
        (layout.logs_dir / "squirrel.log").write_text("installer", encoding="utf-8")
        (layout.local_dir / "settings.json").write_text("{}", encoding="utf-8")
    assert (layout.logs_dir / "squirrel.log").read_text(encoding="utf-8") == "installer"
    assert layout.fork_log.read_bytes() == USER_FILES["logs/fork.log"]
    assert layout.settings_file.read_bytes() == USER_FILES["settings.json"]


def test_a_directory_replaces_what_the_installer_wrote_in_its_place(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    with fork_userdata.kept(layout):
        (layout.local_dir / "gitInstance").write_text("not a directory", encoding="utf-8")
        (layout.local_dir / "accounts.json").mkdir()
    assert _user_files(layout) == USER_FILES


def test_links_are_moved_as_links(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    outside = tmp_path / "outside.json"
    outside.write_text("never followed", encoding="utf-8")
    link = layout.local_dir / "linked.json"
    link.symlink_to(outside)
    with fork_userdata.kept(layout):
        assert not os.path.lexists(link)
        (layout.local_dir / "linked.json").symlink_to(tmp_path / "installer-link")
    assert os.readlink(link) == str(outside)
    assert outside.read_text(encoding="utf-8") == "never followed"


def test_the_files_come_back_when_the_installer_fails(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    with fork_userdata.kept(layout):
        run_velopack_setup(layout, "2.23.2")
        with pytest.raises(RuntimeError, match="installer crashed"):
            raise RuntimeError("installer crashed")
    assert _user_files(layout) == USER_FILES


def test_a_stash_left_by_an_interrupted_run_is_put_back_first(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    stash = fork_userdata.stash_dir(layout.paths)
    stash.mkdir()
    os.rename(layout.settings_file, stash / "settings.json")
    layout.settings_file.write_text('{"seeded": "by fork_settings"}', encoding="utf-8")
    with fork_userdata.kept(layout) as moved:
        assert "settings.json" in moved
    assert _user_files(layout) == USER_FILES
    assert not stash.exists()


def test_restore_without_a_stash_does_nothing(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    assert fork_userdata.restore(layout) == []


def test_restore_recreates_a_vanished_install_directory(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    stash = fork_userdata.stash_dir(layout.paths)
    stash.mkdir()
    os.rename(layout.accounts_file, stash / "accounts.json")
    shutil.rmtree(layout.local_dir)
    assert fork_userdata.restore(layout) == ["accounts.json"]
    assert layout.accounts_file.read_bytes() == USER_FILES["accounts.json"]


def test_restore_refuses_a_linked_install_directory_and_keeps_the_stash(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    stash = fork_userdata.stash_dir(layout.paths)
    stash.mkdir(parents=True)
    (stash / "accounts.json").write_bytes(USER_FILES["accounts.json"])
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    layout.local_dir.parent.mkdir(parents=True)
    layout.local_dir.symlink_to(elsewhere)
    with pytest.raises(fork_userdata.UserDataError) as caught:
        fork_userdata.restore(layout)
    assert "is a symbolic link" in caught.value.message
    assert str(stash) in caught.value.hint
    assert (stash / "accounts.json").is_file()
    assert list(elsewhere.iterdir()) == []


def test_a_failed_move_aside_puts_everything_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    layout = _layout(tmp_path)
    real_rename = os.rename
    calls: list[str] = []

    def rename(source: str | Path, target: str | Path) -> None:
        calls.append(Path(source).name)
        if Path(source).name == "logs" and Path(target).parent.name == fork_userdata.STASH:
            raise OSError(18, "Invalid cross-device link")
        real_rename(source, target)

    monkeypatch.setattr(os, "rename", rename)
    entered = []
    with pytest.raises(fork_userdata.UserDataError) as caught, fork_userdata.kept(layout):
        entered.append(True)
    assert entered == []
    assert "Invalid cross-device link" in caught.value.message
    assert "nothing was installed" in caught.value.hint
    monkeypatch.setattr(os, "rename", real_rename)
    assert _user_files(layout) == USER_FILES
    assert not fork_userdata.stash_dir(layout.paths).exists()


def test_the_launcher_puts_back_a_leftover_stash_before_fork_starts(xdg: Path) -> None:
    ctx = make_ctx()
    install_fork(ctx.layout, "2.23.2")
    ctx.paths.prefix_meta_dir.mkdir(parents=True, exist_ok=True)
    stash = fork_userdata.stash_dir(ctx.paths)
    stash.mkdir()
    (stash / "accounts.json").write_bytes(USER_FILES["accounts.json"])
    notes: list[str] = []
    launcher._user_data(ctx, notes)
    assert ctx.layout.accounts_file.read_bytes() == USER_FILES["accounts.json"]
    assert notes == ["Fork's own files put back after an interrupted install: accounts.json"]
    launcher._user_data(ctx, notes)
    assert len(notes) == 1


def test_the_launcher_leaves_the_stash_alone_while_fork_runs(xdg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = make_ctx()
    stash = fork_userdata.stash_dir(ctx.paths)
    stash.mkdir(parents=True)
    (stash / "accounts.json").write_bytes(b"{}")
    monkeypatch.setattr(launcher.procs, "fork_running", lambda prefix: True)
    notes: list[str] = []
    launcher._user_data(ctx, notes)
    assert (stash / "accounts.json").is_file()
    assert notes == []


def test_user_data_error_is_a_fork_linux_error() -> None:
    assert issubclass(fork_userdata.UserDataError, ForkLinuxError)
