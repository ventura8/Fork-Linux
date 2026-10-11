"""Worktrees, Git Flow, Git LFS and hooks with Fork's bundled git.

Fork's own Git Flow "Start Feature" runs git-flow's shell scripts, which Fork's bundled git
cannot run under Wine: that part is covered with the native-git bridge in
``test_real_bridge.py``. Gated by ``FL_E2E_FORK=1``; containers only.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from fork_driver import git, wait_until
from fork_ui import STAGE_BUTTON, ForkUI, new_repo

from fixtures import real_tier

if os.environ.get("FL_E2E_FORK") != "1":
    pytest.skip("requires FL_E2E_FORK=1 (the official Fork under Wine)", allow_module_level=True)
real_tier.enforce()

# The sidebar of a repository with one worktree: the worktree's entry; the Repository menu.
WORKTREE_ENTRY = (120, 270)
WORKTREE_MENU_DELETE_FROM_BOTTOM = 53
REPOSITORY_MENU = (107, 16)
REPOSITORY_MENU_GIT_LFS = 269
# The Git Error report: its "Skip pre-commit hooks and commit" and Close buttons (from the bottom).
ERROR_CLOSE_FROM_RIGHT = 48
ERROR_CLOSE_FROM_BOTTOM = 33


def _repo(e2e: Any, name: str, files: dict[str, str] | None = None) -> Path:
    return new_repo(e2e.repos / name, files)


def test_worktree_create_open_remove(ui: ForkUI, installed: Any) -> None:
    """New Worktree creates a linked worktree Fork opens as a tab; native git agrees; it can be removed.

    Scenarios: G-WORKTREE
    """
    repo = _repo(installed, "worktrees")
    ui.open(repo)
    ui.quick_launch("Create Worktree")
    ui.wait_dialog("Create Worktree")
    ui.screen.type("wt-feature")
    ui.screen.key("Return", settle=3.0)
    target = installed.repos / "worktrees-worktrees" / "wt-feature"
    wait_until(lambda: (target / ".git").is_file(), 60, "the worktree")
    listed = git(repo, "worktree", "list", "--porcelain")
    assert f"worktree {target}" in listed, listed
    assert git(target, "branch", "--show-current") == "wt-feature", "native git must understand the worktree"
    ui.quick_launch("worktrees")
    ui.all_commits()
    before = ui.screen.windows()
    ui.click(WORKTREE_ENTRY, button=3)
    menu = ui.screen.new_window(before, timeout=10)
    ui.screen.screenshot("worktree-menu")
    ui.screen.click(menu.x + 60, menu.y + menu.height - WORKTREE_MENU_DELETE_FROM_BOTTOM, settle=1.5)
    if ui.dialogs():
        ui.screen.screenshot("worktree-remove-dialog")
        ui.screen.key("Return", settle=2.0)
    wait_until(lambda: f"worktree {target}" not in git(repo, "worktree", "list", "--porcelain"), 60,
               "the worktree to be removed")
    assert not target.exists()


def test_git_flow_initialize(ui: ForkUI, installed: Any) -> None:
    """Git Flow: Initialize creates develop and the gitflow settings with Fork's defaults.

    Scenarios: G-GITFLOW
    """
    repo = _repo(installed, "gitflow-init")
    ui.open(repo)
    ui.quick_launch("Git Flow")
    ui.wait_dialog("Initialize Git Flow")
    ui.screen.key("Return", settle=3.0)
    wait_until(lambda: git(repo, "branch", "--list", "develop"), 60, "the develop branch")
    assert git(repo, "config", "gitflow.branch.master") == "main"
    assert git(repo, "config", "gitflow.prefix.feature") == "feature/"


def test_lfs_files_are_committed_as_pointers(ui: ForkUI, installed: Any) -> None:
    """Repository > Git LFS > Initialize, then a tracked file committed in Fork is an LFS pointer with its
    content in the local LFS store (Fork's bundled git-lfs). Pushing it runs LFS's pre-push hook, which
    needs the git bridge (README, "Known Wine limitations"): ``test_real_bridge.py`` pushes it.

    Scenarios: G-LFS
    """
    repo = _repo(installed, "lfs-work")
    window = ui.open(repo)
    before = ui.screen.windows()
    ui.screen.click_in(window, *REPOSITORY_MENU)
    menu = ui.screen.new_window(before, timeout=10)
    ui.screen.hover(menu.x + 60, menu.y + REPOSITORY_MENU_GIT_LFS, settle=1.0)
    submenu = ui.screen.new_window([*before, menu], timeout=10)
    ui.screen.click(submenu.x + 60, submenu.y + 22, settle=3.0)
    ui.dismiss_dialogs()
    wait_until(lambda: "lfs" in (repo / ".git" / "hooks" / "pre-push").read_text(encoding="utf-8")
               if (repo / ".git" / "hooks" / "pre-push").is_file() else None, 60, "git lfs install's hooks")
    (repo / ".gitattributes").write_text("*.bin filter=lfs diff=lfs merge=lfs -text\n", encoding="utf-8")
    (repo / "data.bin").write_bytes(os.urandom(4096))
    ui.refresh()
    ui.local_changes()
    ui.stage_all_by_double_click(2)
    wait_until(lambda: "data.bin" in git(repo, "diff", "--cached", "--name-only") or None, 60, "data.bin staged")
    ui.commit("add an lfs file")
    wait_until(lambda: git(repo, "log", "-1", "--format=%s") == "add an lfs file" or None, 60, "the commit")
    pointer = git(repo, "show", "HEAD:data.bin")
    assert pointer.startswith("version https://git-lfs.github.com/spec/v1"), pointer[:80]
    oid = next(line.split(":", 1)[1] for line in pointer.splitlines() if line.startswith("oid sha256:"))
    assert (repo / ".git" / "lfs" / "objects" / oid[:2] / oid[2:4] / oid).is_file()


def test_failing_builtin_hook_blocks_the_commit_with_bundled_git(ui: ForkUI, installed: Any) -> None:
    """With bundled git a failing pre-commit hook made of shell builtins stops the commit; Fork shows
    the hook's message and offers to skip the hooks. (Hooks that start programs need the bridge:
    README, "Known Wine limitations"; ``doctor`` lists such repositories.)

    Scenarios: G-HOOK-BUNDLED
    """
    repo = _repo(installed, "hook-bundled", {"a.txt": "a\n"})
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text('#!/bin/sh\necho "builtin hook says no" >&2\nexit 1\n', encoding="utf-8")
    hook.chmod(0o755)
    head = git(repo, "rev-parse", "HEAD")
    (repo / "a.txt").write_text("a changed\n", encoding="utf-8")
    ui.open(repo)
    ui.local_changes()
    ui.click(ui.unstaged_row(0))
    ui.click(STAGE_BUTTON)
    ui.commit("must be blocked")
    error = ui.wait_dialog("Git Error", timeout=60)
    ui.screen.screenshot("hook-bundled-error", error)
    assert git(repo, "rev-parse", "HEAD") == head
    ui.screen.click_in(error, error.width - ERROR_CLOSE_FROM_RIGHT, error.height - ERROR_CLOSE_FROM_BOTTOM)
    ui.wait_no_dialog()
