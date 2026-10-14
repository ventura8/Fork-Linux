"""Tests for :mod:`fork_linux.editors` (host IDE discovery and Wine stubs)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from fork_linux import editors
from fork_linux.paths import Paths


@pytest.fixture
def paths(xdg: Path) -> Paths:
    return Paths.from_env({"HOME": str(xdg), "XDG_DATA_HOME": str(xdg / "data")})


def _exe(directory: Path, name: str) -> Path:
    path = directory / name
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def test_discover_finds_ides_on_path(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _exe(bin_dir, "code")
    _exe(bin_dir, "cursor")
    _exe(bin_dir, "intellij-idea")
    found = editors.discover({"PATH": str(bin_dir)})
    assert [item.id for item in found] == ["code", "cursor", "idea"]
    assert found[0].win_names == ("Code.exe",)
    assert found[1].command.endswith("/cursor")


def test_discover_skips_missing(tmp_path: Path) -> None:
    assert editors.discover({"PATH": str(tmp_path)}) == []


def test_install_writes_and_removes_stubs(paths: Paths, tmp_path: Path) -> None:
    paths.prefix.mkdir(parents=True)
    launch = tmp_path / "fl-launch.exe"
    launch.write_bytes(b"MZ-launch")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _exe(bin_dir, "code")
    _exe(bin_dir, "cursor")
    env = {"PATH": str(bin_dir)}
    found = editors.discover(env)
    assert editors.install(paths, "ada", launch, found) == ["code", "cursor"]
    root = editors.editors_win_dir(paths)
    assert (root / "Code.exe").read_bytes() == b"MZ-launch"
    assert (root / "Cursor.exe").read_bytes() == b"MZ-launch"
    programs = editors.programs_root(paths, "ada")
    assert (programs / "Microsoft VS Code" / "Code.exe").is_file()
    assert (programs / "cursor" / "Cursor.exe").is_file()
    entries = editors.app_path_entries(paths, found)
    assert ("Code.exe", r"C:\fork-linux\editors\Code.exe") in entries
    # Drop cursor from PATH: its stubs go away, code stays.
    (bin_dir / "cursor").unlink()
    left = editors.discover(env)
    editors.install(paths, "ada", launch, left)
    assert (root / "Code.exe").is_file()
    assert not (root / "Cursor.exe").exists()
    assert not (programs / "cursor" / "Cursor.exe").exists()


def test_allowed_ids_match_catalog() -> None:
    assert editors.ALLOWED_IDS == frozenset(item[0] for item in editors.EDITORS)
