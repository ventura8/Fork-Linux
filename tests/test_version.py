"""Contract tests for VERSION (single source of truth) and scripts/read-version.py."""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "read-version.py"
SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("read_version", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rv = _load_script()


def test_version_file_is_single_semver_line() -> None:
    text = (ROOT / "VERSION").read_text(encoding="utf-8")
    assert text.endswith("\n")
    lines = text.splitlines()
    assert len(lines) == 1, "VERSION must hold exactly one line"
    assert SEMVER.match(lines[0]), f"VERSION is not N.N.N semver: {lines[0]!r}"


def test_read_version_script_matches_version_file() -> None:
    expected = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    out = subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout == f"{expected}\n"
    assert out.stderr == ""


def test_read_version_script_ignores_a_foreign_version_in_cwd(tmp_path: Path) -> None:
    """Run from another project's directory, the script still prints this repo's VERSION."""
    expected = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    (tmp_path / "VERSION").write_text("9.9.9\n", encoding="utf-8")
    out = subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout == f"{expected}\n"


def test_version_parses_with_fork_linux_versions() -> None:
    from fork_linux import versions

    raw = rv.read_version()
    assert versions.is_valid(raw)
    assert versions.compare(raw, raw) == 0


@pytest.fixture
def fake_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the loaded script at ``tmp_path/repo/scripts/read-version.py`` (no VERSION yet)."""
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    monkeypatch.setattr(rv, "__file__", str(repo / "scripts" / "read-version.py"))
    return repo


def test_find_version_file_prefers_script_repo(
    fake_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (fake_repo / "VERSION").write_text("1.0.0\n", encoding="utf-8")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "VERSION").write_text("9.8.7\n", encoding="utf-8")
    monkeypatch.chdir(elsewhere)
    assert rv.find_version_file() == fake_repo / "VERSION"
    assert rv.read_version() == "1.0.0"


def test_find_version_file_falls_back_to_cwd_walk_up(
    fake_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    (tmp_path / "VERSION").write_text("9.8.7\n", encoding="utf-8")
    monkeypatch.chdir(nested)
    assert rv.find_version_file() == tmp_path / "VERSION"
    assert rv.read_version() == "9.8.7"


def test_find_version_file_from_inside_the_repo(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(ROOT / "scripts")
    assert rv.find_version_file() == ROOT / "VERSION"


def test_find_version_file_missing(fake_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(rv.Path, "is_file", lambda self: False)
    with pytest.raises(FileNotFoundError, match="VERSION file not found"):
        rv.find_version_file()


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("", "empty"),
        ("\n\n", "empty"),
        ("# comment\n1.2.3\n", "missing a version"),
    ],
)
def test_read_version_rejects_bad_files(fake_repo: Path, content: str, message: str) -> None:
    (fake_repo / "VERSION").write_text(content, encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        rv.read_version()


def test_read_version_takes_first_line(fake_repo: Path) -> None:
    (fake_repo / "VERSION").write_text("  1.2.3  \n# trailing note\n", encoding="utf-8")
    assert rv.read_version() == "1.2.3"


def test_main_success(fake_repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (fake_repo / "VERSION").write_text("4.5.6\n", encoding="utf-8")
    assert rv.main() == 0
    assert capsys.readouterr().out == "4.5.6\n"


def test_main_error(fake_repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (fake_repo / "VERSION").write_text("", encoding="utf-8")
    assert rv.main() == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "error: VERSION file is empty\n"
