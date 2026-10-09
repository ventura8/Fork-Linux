"""Tests for fork_linux.fork_layout: where Fork keeps its files inside the prefix."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from fixtures.fork_tree import NUSPEC, NUSPEC_NS, USER, install_fork, make_layout, make_paths, write_sq_version
from fork_linux import fork_layout
from fork_linux.errors import UsageError
from fork_linux.fork_layout import ForkLayout, check_user, parse_nuspec_version


@pytest.fixture
def layout(tmp_path: Path) -> ForkLayout:
    return make_layout(tmp_path)


def test_paths_follow_the_velopack_layout(layout: ForkLayout) -> None:
    user_dir = layout.paths.prefix / "drive_c" / "users" / USER
    local = user_dir / "AppData" / "Local" / "Fork"
    assert layout.user == USER
    assert layout.local_dir == local
    assert layout.current_dir == local / "current"
    assert layout.exe == local / "current" / "Fork.exe"
    assert layout.ri_exe == local / "current" / "Fork.RI.exe"
    assert layout.askpass_exe == local / "current" / "Fork.AskPass.exe"
    assert layout.update_exe == local / "Update.exe"
    assert layout.sq_version_file == local / "current" / "sq.version"
    assert layout.packages_dir == local / "packages"
    assert layout.settings_file == local / "settings.json"
    assert layout.accounts_file == local / "accounts.json"
    assert layout.custom_commands_file == local / "custom-commands.json"
    assert layout.logs_dir == local / "logs"
    assert layout.fork_log == local / "logs" / "fork.log"
    assert layout.velopack_log == local / "velopack.log"
    assert layout.gitinstance_dir == local / "gitInstance"
    assert layout.forkdata_dir == user_dir / "AppData" / "Local" / "ForkData"
    assert layout.desktop_lnk == user_dir / "Desktop" / "Fork.lnk"
    assert layout.startmenu_lnk == (
        user_dir / "AppData" / "Roaming" / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Fork.lnk"
    )
    assert layout.local_dir == layout.paths.fork_local_dir(USER)
    assert layout.current_dir == layout.paths.fork_current_dir(USER)


def test_win_exe_is_the_windows_path(layout: ForkLayout) -> None:
    assert layout.win_exe == "C:\\users\\tester\\AppData\\Local\\Fork\\current\\Fork.exe"


def test_repr_names_prefix_and_user(layout: ForkLayout) -> None:
    assert repr(layout) == f"ForkLayout(prefix={str(layout.paths.prefix)!r}, user='tester')"


@pytest.mark.parametrize("user", ["", ".", "..", "a/b", "a\\b", "a:b", "a*b", 'a"b', "a\x01b", "a\x00b"])
def test_check_user_rejects_unusable_names(tmp_path: Path, user: str) -> None:
    with pytest.raises(UsageError, match="Wine user name"):
        check_user(user)
    with pytest.raises(UsageError):
        ForkLayout(make_paths(tmp_path), user)


def test_check_user_rejects_non_strings() -> None:
    with pytest.raises(UsageError):
        check_user(None)


def test_check_user_accepts_plain_names() -> None:
    assert check_user("sergiu.alexandrescu-2") == "sergiu.alexandrescu-2"


# -- installed_version / parse_nuspec_version ------------------------------------


def test_installed_version_missing_file(layout: ForkLayout) -> None:
    assert layout.installed_version() is None


def test_installed_version_reads_the_nuspec(layout: ForkLayout) -> None:
    write_sq_version(layout, "2.23.2")
    assert layout.installed_version() == "2.23.2"


@pytest.mark.parametrize(
    ("document", "expected"),
    [
        ("<package><metadata><version>2.23.2</version></metadata></package>", "2.23.2"),
        (
            '<n:package xmlns:n="urn:other"><n:metadata><n:version> 2.24.0 </n:version></n:metadata></n:package>',
            "2.24.0",
        ),
        (
            f'<package xmlns="{NUSPEC_NS}"><metadata><version>2.25.0-beta.1</version></metadata></package>',
            "2.25.0-beta.1",
        ),
        (
            "<package><metadata><version>2.23.2</version><version>9.9.9</version></metadata></package>",
            "2.23.2",
        ),
        ("<package><version>2.23.2</version></package>", None),
        ("<package><metadata><id>Fork</id></metadata></package>", None),
        ("<other><metadata><version>2.23.2</version></metadata></other>", None),
        ("<package><metadata><version>not-a-version</version></metadata></package>", None),
        ("<package><metadata><version></version></metadata></package>", None),
        ("<package><metadata><version>2.23.2</metadata></package>", None),
        ("not xml at all", None),
        ("", None),
        (
            '<!DOCTYPE package [<!ENTITY v "2.23.2">]><package><metadata><version>&v;</version></metadata></package>',
            None,
        ),
        ('<!DOCTYPE package SYSTEM "http://example.invalid/x.dtd"><package/>', None),
    ],
)
def test_parse_nuspec_version(document: str, expected: str | None) -> None:
    assert parse_nuspec_version(document.encode("utf-8")) == expected


def test_parse_nuspec_version_handles_bom_and_utf16() -> None:
    text = NUSPEC.format(version="2.23.2").replace('encoding="utf-8"', 'encoding="utf-16"')
    assert parse_nuspec_version(text.encode("utf-16")) == "2.23.2"
    assert parse_nuspec_version(b"\xef\xbb\xbf" + NUSPEC.format(version="2.23.2").encode()) == "2.23.2"


def test_installed_version_rejects_oversized_file(layout: ForkLayout, monkeypatch: pytest.MonkeyPatch) -> None:
    write_sq_version(layout, "2.23.2")
    monkeypatch.setattr(fork_layout, "SQ_VERSION_MAX_BYTES", 16)
    assert layout.installed_version() is None


def test_installed_version_never_follows_symlinks(layout: ForkLayout, tmp_path: Path) -> None:
    target = tmp_path / "elsewhere.xml"
    target.write_text(NUSPEC.format(version="2.23.2"), encoding="utf-8")
    layout.current_dir.mkdir(parents=True)
    layout.sq_version_file.symlink_to(target)
    assert layout.installed_version() is None


def test_installed_version_ignores_directories_and_fifos(layout: ForkLayout) -> None:
    layout.sq_version_file.mkdir(parents=True)
    assert layout.installed_version() is None
    layout.sq_version_file.rmdir()
    os.mkfifo(layout.sq_version_file)
    assert layout.installed_version() is None


def test_installed_version_read_error_gives_none(layout: ForkLayout, monkeypatch: pytest.MonkeyPatch) -> None:
    write_sq_version(layout, "2.23.2")

    def broken_fstat(fd: int) -> os.stat_result:
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(fork_layout.os, "fstat", broken_fstat)
    assert layout.installed_version() is None


def test_is_installed(layout: ForkLayout) -> None:
    assert layout.is_installed() is False
    install_fork(layout)
    assert layout.is_installed() is True
    layout.sq_version_file.unlink()
    assert layout.is_installed() is False
    write_sq_version(layout, "2.23.2")
    layout.exe.unlink()
    assert layout.is_installed() is False


# -- git_instances -----------------------------------------------------------------


def test_git_instances_missing_dir(layout: ForkLayout) -> None:
    assert layout.git_instances() == []


def test_git_instances_lists_dirs_with_git_exe(layout: ForkLayout, tmp_path: Path) -> None:
    base = layout.gitinstance_dir
    for name in ("2.50.1", "2.45.0"):
        (base / name / "cmd").mkdir(parents=True)
        (base / name / "cmd" / "git.exe").write_bytes(b"MZ")
    (base / "2.40.0" / "cmd").mkdir(parents=True)  # no git.exe
    (base / "notes.txt").write_text("x")
    outside = tmp_path / "outside"
    (outside / "cmd").mkdir(parents=True)
    (outside / "cmd" / "git.exe").write_bytes(b"MZ")
    (base / "linked").symlink_to(outside)
    assert layout.git_instances() == ["2.45.0", "2.50.1"]


# -- staged_packages ---------------------------------------------------------------


def _touch_packages(layout: ForkLayout, *names: str) -> None:
    layout.packages_dir.mkdir(parents=True, exist_ok=True)
    for name in names:
        (layout.packages_dir / name).write_bytes(b"PK")


def test_staged_packages_missing_dir(layout: ForkLayout) -> None:
    write_sq_version(layout, "2.23.2")
    assert layout.staged_packages() == []


def test_staged_packages_newer_than_installed(layout: ForkLayout, tmp_path: Path) -> None:
    write_sq_version(layout, "2.23.2")
    _touch_packages(
        layout,
        "Fork-2.23.2-full.nupkg",
        "Fork-2.22.0-full.nupkg",
        "Fork-2.25.1-full.nupkg",
        "Fork-2.24.0-full.nupkg",
        "Fork-2.24.0-delta.nupkg",
        ".velopack_lock",
        ".betaId",
        "Fork-2.26.0-full.nupkg.partial",
        "Other-2.30.0-full.nupkg",
    )
    (layout.packages_dir / "Fork-3.0.0-full.nupkg").mkdir()
    real = tmp_path / "real.nupkg"
    real.write_bytes(b"PK")
    (layout.packages_dir / "Fork-3.1.0-full.nupkg").symlink_to(real)
    pkgs = layout.packages_dir
    assert layout.staged_packages() == [
        ("2.24.0", pkgs / "Fork-2.24.0-delta.nupkg"),
        ("2.24.0", pkgs / "Fork-2.24.0-full.nupkg"),
        ("2.25.1", pkgs / "Fork-2.25.1-full.nupkg"),
    ]
    assert layout.staged_packages(than="2.24.0") == [("2.25.1", pkgs / "Fork-2.25.1-full.nupkg")]
    assert layout.staged_packages(than="2.30") == []


def test_staged_packages_without_installed_version_lists_all(layout: ForkLayout) -> None:
    _touch_packages(layout, "Fork-2.24.0-full.nupkg", "Fork-2.23.2-full.nupkg")
    assert [version for version, _path in layout.staged_packages()] == ["2.23.2", "2.24.0"]
