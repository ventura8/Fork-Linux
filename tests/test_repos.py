"""Tests for fork_linux.repos: Fork's repository list, the host-git scan and the opt-in repairs.

The scan runs the real host git on throw-away repositories under the test's
temporary HOME (no network, no Wine).
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from fork_linux import fork_data, gitconfig, repos
from fork_linux.errors import ForkLinuxError
from fork_linux.fork_layout import ForkLayout
from fork_linux.pathmap import PathMap
from fork_linux.paths import Paths
from fork_linux.procrun import Completed, RecordingRunner, Runner

USER = "tester"


def _git(repo: Path, *args: str) -> str:
    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x"}
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=env).stdout


def _repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q")
    return path


@pytest.fixture
def git(xdg: Path) -> repos.Git:
    return repos.Git(Runner(), {**os.environ, "GIT_CONFIG_NOSYSTEM": "1"})


@pytest.fixture
def messy(xdg: Path) -> Path:
    """A repository with a hook, a tracked link, .gitmodules, Linux remotes and core.filemode=true."""
    repo = _repo(xdg / "src" / "messy")
    (repo / "target.txt").write_text("t\n", encoding="utf-8")
    (repo / "link").symlink_to("target.txt")
    (repo / ".gitmodules").write_text("", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    hooks = repo / ".git" / "hooks"
    (hooks / "pre-commit").write_text("#!/bin/sh\n", encoding="utf-8")
    (hooks / "pre-commit").chmod(0o755)
    (hooks / "post-merge").write_text("#!/bin/sh\n", encoding="utf-8")  # not executable
    (hooks / "notes").mkdir()
    _git(repo, "remote", "add", "home", "file:///home/u/remote.git")
    _git(repo, "remote", "add", "bare", "/srv/remote.git")
    _git(repo, "remote", "add", "win", "file:///Z:/home/u/remote.git")
    _git(repo, "remote", "add", "web", "https://example.invalid/r.git")
    _git(repo, "config", "remote.web.pushurl", "/media/usb/r.git")
    _git(repo, "config", "core.filemode", "true")
    return repo


def test_linux_path_remote() -> None:
    assert repos.linux_path_remote("/home/u/r.git") == "home"
    assert repos.linux_path_remote("file:///mnt/r.git") == "mnt"
    assert repos.linux_path_remote("file:///Z:/home/r.git") is None
    assert repos.linux_path_remote("//server/share") is None
    assert repos.linux_path_remote("../r.git") is None
    assert repos.linux_path_remote("git@host:r.git") is None


def test_covered_tops() -> None:
    text = '[url "Z:/home/"]\n\tinsteadOf = /home/\n\tinsteadOf = file:///home/\n[x]\n\tinsteadOf=/srv/\nfoo = /bar/\n'
    assert repos.covered_tops(text) == {"home", "srv"}


def test_scan_finds_every_problem(git: repos.Git, messy: Path, xdg: Path) -> None:
    clean = _repo(xdg / "src" / "clean")
    not_repo = xdg / "src" / "plain"
    not_repo.mkdir()
    reports = repos.scan(git, [messy, clean, not_repo], '[url "Z:/home/"]\n\tinsteadOf = /home/\n')
    report = reports[0]
    assert report.name == "messy" and report.error == ""
    assert report.hooks == ["pre-commit"]
    assert report.symlinks == ["link"] and report.skipped_links == []
    assert report.submodules is True and report.filemode is True
    assert report.linux_remotes == ["/srv/remote.git", "/media/usb/r.git"]
    assert reports[1].hooks == [] and reports[1].symlinks == [] and not reports[1].submodules
    assert reports[2].error == "not a git repository"
    assert repos.describe(reports, "hooks") == ["messy (pre-commit)"]
    assert repos.describe(reports, "filemode") == ["messy", "clean"]  # git init on Linux writes it
    assert repos.describe(reports, "submodules") == ["messy"]


def test_scan_follows_core_hookspath(git: repos.Git, xdg: Path) -> None:
    repo = _repo(xdg / "src" / "hookspath")
    custom = repo / "githooks"
    custom.mkdir()
    (custom / "commit-msg").write_text("#!/bin/sh\n", encoding="utf-8")
    (custom / "commit-msg").chmod(0o755)
    _git(repo, "config", "core.hooksPath", str(custom))
    assert repos.scan_one(git, repo, set()).hooks == ["commit-msg"]
    _git(repo, "config", "core.hooksPath", "missing")
    assert repos.scan_one(git, repo, set()).hooks == []


def test_fix_and_undo(git: repos.Git, messy: Path) -> None:
    report = repos.scan_one(git, messy, set())
    done = repos.fix(git, report)
    assert done == [f"{messy}: core.filemode = false", f"{messy}: skip-worktree for link"]
    after = repos.scan_one(git, messy, set())
    assert not after.filemode and after.symlinks == [] and after.skipped_links == ["link"]
    assert "S link" in _git(messy, "ls-files", "-v")
    assert repos.fix(git, after) == []
    assert repos.undo(git, messy) == [
        f"{messy}: core.filemode = true again",
        f"{messy}: skip-worktree removed from link",
    ]
    restored = repos.scan_one(git, messy, set())
    assert restored.filemode and restored.symlinks == ["link"]
    assert "H link" in _git(messy, "ls-files", "-v")
    assert repos.undo(git, messy) == []
    assert repos.fix(git, restored, filemode=False, symlinks=False) == []


def test_fix_reports_git_failures(messy: Path, xdg: Path) -> None:
    runner = RecordingRunner({"git": Completed([], 1, "", "locked")}, which_map={"git": "/usr/bin/git"})
    report = repos.RepoReport(messy, filemode=True)
    with pytest.raises(ForkLinuxError, match="failed"):
        repos.fix(repos.Git(runner, {}), report)


def test_git_without_git() -> None:
    git = repos.Git(RecordingRunner(which_map={"git": None}), {"PATH": "/none"})
    assert not git.available
    with pytest.raises(ForkLinuxError, match="not installed"):
        git.run(Path("/"), "status")


def test_known_reads_repositories_and_workspaces(xdg: Path) -> None:
    paths = Paths.from_env(dict(os.environ))
    layout = ForkLayout(paths, USER)
    one = xdg / "src" / "one"
    two = xdg / "src" / "two"
    for repo in (one, two):
        repo.mkdir(parents=True)
    pathmap = PathMap.with_drives({"z": "/", "c": paths.prefix / "drive_c"})
    assert repos.known(layout, pathmap) == []
    layout.forkdata_dir.mkdir(parents=True)
    win = {repo: "Z:" + str(repo).replace("/", "\\") for repo in (one, two)}
    fork_data.path(layout.forkdata_dir).write_text(
        f"source_dirs = []\n[[repository]]\npath = '{win[one]}'\n[[repository]]\npath = 'Z:\\gone'\n"
        "[[repository]]\npath = 'relative'\n",
        encoding="utf-8",
    )
    layout.settings_file.parent.mkdir(parents=True, exist_ok=True)
    layout.settings_file.write_text(
        json.dumps(
            {
                "Workspaces": {
                    "All": [
                        {"Name": "Home", "Repositories": [win[one], win[two], 5]},
                        {"Name": "Empty", "Repositories": None},
                        "junk",
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    assert repos.known(layout, pathmap) == [one, two]
    layout.settings_file.write_text(json.dumps({"Workspaces": {"All": "x"}}), encoding="utf-8")
    assert repos.known(layout, pathmap) == [one]


def test_overlay_text(xdg: Path) -> None:
    paths = Paths.from_env(dict(os.environ))
    assert repos.overlay_text(paths, USER) == ""
    overlay = gitconfig.overlay_path(paths, USER)
    overlay.parent.mkdir(parents=True)
    overlay.write_text("x", encoding="utf-8")
    assert repos.overlay_text(paths, USER) == "x"
