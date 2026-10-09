"""Tests for ``fork-linux repo check|fix|undo`` (real host git on throw-away repositories)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from fork_linux import fork_data, repos
from fork_linux import ui as ui_mod
from fork_linux.procrun import RecordingRunner

from fixtures.cli_run import isolate, layout, make_prefix, paths, run_cli, run_json


@pytest.fixture(autouse=True)
def _isolated(xdg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    isolate(monkeypatch)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


def _git(repo: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@x"}
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=env).stdout


@pytest.fixture
def repo(xdg: Path) -> Path:
    path = xdg / "src" / "app"
    path.mkdir(parents=True)
    _git(path, "init", "-q")
    (path / "t").write_text("t\n", encoding="utf-8")
    (path / "link").symlink_to("t")
    _git(path, "add", "-A")
    _git(path, "commit", "-qm", "init")
    _git(path, "config", "core.filemode", "true")
    return path


class Yes:
    """A UI that answers every question with ``answer``."""

    def __init__(self, answer: bool) -> None:
        self.answer = answer
        self.asked: list[str] = []

    def confirm(self, title: str, text: str, *, default: bool = False) -> bool:
        self.asked.append(text)
        return self.answer


def _ui(monkeypatch: pytest.MonkeyPatch, answer: bool) -> Yes:
    ui = Yes(answer)
    monkeypatch.setattr(ui_mod, "choose", lambda *args, **kwargs: ui)
    return ui


def test_check_named_and_known(capsys: pytest.CaptureFixture[str], repo: Path, xdg: Path) -> None:
    code, out, _err = run_cli(capsys, "repo", "check", str(repo))
    assert code == 0
    assert "symbolic links Fork shows as modified: link" in out
    assert "core.filemode = true" in out
    plain = xdg / "plain"
    plain.mkdir()
    assert "not a git repository" in run_cli(capsys, "repo", "check", str(plain))[1]
    make_prefix(paths())
    assert "no repositories" in run_cli(capsys, "repo", "check")[1]
    forkdata = layout().forkdata_dir
    forkdata.mkdir(parents=True)
    win = "Z:" + str(repo).replace("/", "\\")
    fork_data.path(forkdata).write_text(f"[[repository]]\npath = '{win}'\n", encoding="utf-8")
    data = run_json(capsys, "repo", "check")
    assert data["repositories"][0]["path"] == str(repo)
    assert data["repositories"][0]["symlinks"] == ["link"]
    assert data["repositories"][0]["filemode"] is True


def test_check_lines_cover_every_problem() -> None:
    from fork_linux.commands import repo as repo_cmd

    report = repos.RepoReport(Path("/r"), hooks=["pre-commit"], skipped_links=["l"], submodules=True,
                              linux_remotes=["/srv/r.git"])
    lines = "\n".join(repo_cmd._lines(report))
    assert "pre-commit" in lines
    assert "skip-worktree" in lines
    assert "submodules" in lines
    assert "/srv/r.git" in lines
    assert repo_cmd._lines(repos.RepoReport(Path("/r"))) == ["  ok"]


def test_fix_asks_and_undo_reverts(capsys: pytest.CaptureFixture[str], repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    declined = _ui(monkeypatch, False)
    code, out, _err = run_cli(capsys, "repo", "fix", str(repo))
    assert code == 1
    assert "nothing changed" in out
    assert "set core.filemode = false" in declined.asked[0]
    assert "skip-worktree for link" in declined.asked[0]
    _ui(monkeypatch, True)
    code, out, _err = run_cli(capsys, "repo", "fix", str(repo))
    assert code == 0
    assert "core.filemode = false" in out
    assert _git(repo, "config", "core.filemode").strip() == "false"
    assert run_cli(capsys, "repo", "fix", "--yes", str(repo))[1] == "nothing to change\n"
    code, out, _err = run_cli(capsys, "repo", "undo", str(repo))
    assert code == 0
    assert "skip-worktree removed from link" in out
    assert "nothing to undo" in run_cli(capsys, "repo", "undo", str(repo))[1]


def test_fix_options(capsys: pytest.CaptureFixture[str], repo: Path) -> None:
    code, out, _err = run_cli(capsys, "repo", "fix", "--yes", "--no-symlinks", str(repo))
    assert code == 0
    assert "skip-worktree" not in out
    assert "core.filemode = false" in out
    code, out, _err = run_cli(capsys, "repo", "fix", "--yes", "--no-filemode", str(repo))
    assert code == 0
    assert "skip-worktree for link" in out


def test_errors(capsys: pytest.CaptureFixture[str], xdg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    code, _out, err = run_cli(capsys, "repo", "fix", str(xdg / "missing"))
    assert code != 0
    assert "no such directory" in err
    from fork_linux import cli

    original = cli.AppContext.__init__

    def no_git(self: Any, *args: Any, **kwargs: Any) -> None:
        original(self, *args, **kwargs)
        self.runner = RecordingRunner(which_map={"git": None})

    monkeypatch.setattr(cli.AppContext, "__init__", no_git)
    code, _out, err = run_cli(capsys, "repo", "undo", str(xdg))
    assert code != 0
    assert "git is not installed" in err
