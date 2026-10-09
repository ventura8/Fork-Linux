"""Tests for fork_linux.version: build-time version vs the VERSION file fallback."""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from fork_linux import version

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def no_build(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``fork_linux._build`` unimportable, as in a source checkout."""
    monkeypatch.setitem(sys.modules, "fork_linux._build", None)


def _fake_build(monkeypatch: pytest.MonkeyPatch, **attrs: str) -> None:
    module = types.ModuleType("fork_linux._build")
    for key, value in attrs.items():
        setattr(module, key, value)
    monkeypatch.setitem(sys.modules, "fork_linux._build", module)


def test_source_checkout_reads_version_file(no_build: None) -> None:
    expected = REPO_ROOT.joinpath("VERSION").read_text(encoding="utf-8").splitlines()[0].strip()
    assert version.get_version() == f"{expected}-dev"
    assert version.get_flavor() == "source"
    assert version.find_version_file() == REPO_ROOT / "VERSION"


def test_build_module_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_build(monkeypatch, VERSION="9.8.7", FLAVOR="deb")
    assert version.get_version() == "9.8.7"
    assert version.get_flavor() == "deb"


def test_incomplete_build_module_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_build(monkeypatch, FLAVOR="")
    assert version.get_version().endswith("-dev")
    assert version.get_flavor() == "source"
    _fake_build(monkeypatch, VERSION="")
    assert version.get_version().endswith("-dev")
    assert version.get_flavor() == "source"


def _isolated_package(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    pkg = tmp_path / "tree" / "src" / "fork_linux"
    pkg.mkdir(parents=True)
    monkeypatch.setattr(version, "_package_dir", lambda: pkg)
    return pkg


def test_version_file_found_upward(no_build: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolated_package(tmp_path, monkeypatch)
    (tmp_path / "tree" / "VERSION").write_text("\n1.2.3\n# trailing comment\n", encoding="utf-8")
    assert version.get_version() == "1.2.3-dev"
    assert version.find_version_file() == tmp_path / "tree" / "VERSION"


@pytest.mark.parametrize("content", ["", "   \n\n", "# 1.0.0\n"])
def test_invalid_version_file_gives_unknown(
    no_build: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: str
) -> None:
    _isolated_package(tmp_path, monkeypatch)
    path = tmp_path / "tree" / "VERSION"
    path.write_text(content, encoding="utf-8")
    assert version.get_version() == version.UNKNOWN
    with pytest.raises(ValueError):
        version.read_version_file(path)


def test_missing_version_file_gives_unknown(no_build: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(version, "find_version_file", lambda start=None: None)
    assert version.get_version() == version.UNKNOWN


def test_find_version_file_returns_none_when_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "a" / "b").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    assert version.find_version_file(Path("a/b")) is None
    Path("VERSION").write_text("2.0\n", encoding="utf-8")
    assert version.find_version_file(Path("a/b")) == Path("VERSION")


def test_version_directory_is_not_a_file(tmp_path: Path) -> None:
    start = tmp_path / "a" / "b"
    start.mkdir(parents=True)
    (tmp_path / "a" / "VERSION").mkdir()
    (tmp_path / "VERSION").write_text("4.5\n", encoding="utf-8")
    assert version.find_version_file(start) == tmp_path / "VERSION"
    assert version.read_version_file(tmp_path / "VERSION") == "4.5"
