"""Tests for fork_linux.file_picker."""

from __future__ import annotations

from pathlib import Path

import pytest

from fork_linux import file_picker as fp
from fork_linux.errors import ForkLinuxError
from fork_linux.procrun import RecordingRunner


def test_zenity_and_kdialog_argv(tmp_path: Path) -> None:
    start = str(tmp_path)
    assert "--directory" in fp.zenity_argv("dir", start, "")
    assert "--save" in fp.zenity_argv("save", start, "out.txt")
    assert fp.kdialog_argv("dir", start, "") == ["kdialog", "--getexistingdirectory", start]
    assert fp.kdialog_argv("file", start, "")[1] == "--getopenfilename"
    assert "out.txt" in fp.kdialog_argv("save", start, "out.txt")[-1]


def _which_zenity(name: str) -> str | None:
    return "/usr/bin/zenity" if name == "zenity" else None


def _which_none(_name: str) -> None:
    return None


def test_pick_zenity_success(tmp_path: Path) -> None:
    chosen = tmp_path / "repo"
    chosen.mkdir()
    runner = RecordingRunner(responses={"zenity": str(chosen) + "\n"})
    assert fp.pick("dir", str(tmp_path), env={}, runner=runner, which=_which_zenity) == str(chosen)
    assert runner.calls[0]["argv"][0] == "zenity"


def test_pick_kdialog_first_on_kde(tmp_path: Path) -> None:
    chosen = tmp_path / "f.txt"
    chosen.write_text("x", encoding="utf-8")
    runner = RecordingRunner(responses={"kdialog": str(chosen) + "\n"})

    def which(name: str) -> str | None:
        return f"/usr/bin/{name}" if name in ("kdialog", "zenity") else None

    path = fp.pick(
        "file",
        str(tmp_path),
        env={"XDG_CURRENT_DESKTOP": "KDE"},
        runner=runner,
        which=which,
    )
    assert path == str(chosen)
    assert runner.calls[0]["argv"][0] == "kdialog"


def test_pick_cancel_and_missing(tmp_path: Path) -> None:
    runner = RecordingRunner(responses={"zenity": 1})
    with pytest.raises(fp.PickCancelled):
        fp.pick("file", str(tmp_path), env={}, runner=runner, which=_which_zenity)
    with pytest.raises(fp.PickNotFound):
        fp.pick("dir", env={}, runner=RecordingRunner(), which=_which_none)
    with pytest.raises(ForkLinuxError, match="unknown picker kind"):
        fp.pick("nope", env={}, runner=RecordingRunner(), which=_which_none)


def test_start_dir_falls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert fp._start_dir("") == str(tmp_path.resolve())
    missing = tmp_path / "gone" / "x"
    assert fp._start_dir(str(missing)) == str(tmp_path.resolve())
