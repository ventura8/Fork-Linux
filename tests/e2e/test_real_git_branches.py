"""Branches and history rewriting in Fork's UI: branches, merge, rebase (plain and interactive),
cherry-pick, revert, reset, tags, stashes and Fork's own merge-conflict tool.

Each test builds its own repository under ``<root>/repos`` (no symlinks) and checks every
effect with native git. Gated by ``FL_E2E_FORK=1``; containers only.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from fork_driver import git, wait_until
from fork_ui import ForkUI, conflicting_branches, dated_env, linear_history, new_repo, wait_ref

from fixtures import real_tier

if os.environ.get("FL_E2E_FORK") != "1":
    pytest.skip("requires FL_E2E_FORK=1 (the official Fork under Wine)", allow_module_level=True)
real_tier.enforce()

# The sidebar of a repository with only "main" and no remotes or tags: its Stashes entries.
FIRST_STASH = (80, 356)
STASH_MENU_APPLY = 24
STASH_MENU_DELETE_FROM_BOTTOM = 21
APPLY_DELETE_AFTER_BOX = (203, 107)
# With a merge in progress Fork shows a banner: the Unstaged list moves down by its height.
CONFLICTED_FILE = (420, 186)
OPEN_IN_MERGER = (1026, 374)
# Inside Fork's merger window ("Merge"): the box that takes the current branch's side, Resolve.
MERGER_OURS = (533, 73)
MERGER_RESOLVE = (899, 689)
# Inside the Interactive Rebase window: its rows (newest first) and the Rebase button.
REBASE_ROW_FIRST = 97
REBASE_ROW_PITCH = 22
REBASE_ROW_X = 600
REBASE_BUTTON = (901, 687)


def _repo(e2e: Any, name: str, files: dict[str, str] | None = None) -> Path:
    return new_repo(e2e.repos / name, files)


def _commit(repo: Path, name: str, content: str, message: str, index: int) -> str:
    (repo / name).write_text(content, encoding="utf-8")
    git(repo, "add", "--", name)
    git(repo, "commit", "-qm", message, env=dated_env(index))
    return git(repo, "rev-parse", "HEAD")


def _current(repo: Path) -> str:
    return git(repo, "branch", "--show-current")


def _checkout(ui: ForkUI, repo: Path, branch: str) -> None:
    """Checkout Branch (Quick Launch); Fork asks first only when there are local changes."""

    def start() -> None:
        ui.quick_launch("Checkout Branch", branch)
        try:
            ui.wait_dialog("Checkout Branch", timeout=5)
        except AssertionError:
            return
        ui.accept("Checkout Branch")

    ui.operate(start, lambda: _current(repo) == branch, f"the checkout of {branch}")


def _has(repo: Path, ref: str) -> bool:
    return bool(git(repo, "rev-parse", "--verify", "--quiet", ref, check=False))


def test_branch_create_checkout_rename_delete(ui: ForkUI, installed: Any) -> None:
    """New Branch, Checkout Branch, Rename Branch and Delete Branch, each through Fork's dialogs.

    Scenarios: G-BRANCH-CREATE, G-BRANCH-CHECKOUT, G-BRANCH-RENAME, G-BRANCH-DELETE
    """
    repo = _repo(installed, "branches")
    head = linear_history(repo, 2)[-1]
    ui.open(repo)
    ui.operate(lambda: ui.shortcut_dialog("ctrl+shift+b", "Create Branch", "topic"),
               lambda: _has(repo, "refs/heads/topic"), "the branch topic")
    assert wait_ref(repo, "refs/heads/topic") == head
    assert _current(repo) == "main"
    _checkout(ui, repo, "topic")
    ui.operate(lambda: ui.quick_dialog("Rename Branch", "topic", "Rename Local Branch", "renamed", replace=True),
               lambda: _current(repo) == "renamed", "the rename of topic")
    _checkout(ui, repo, "main")
    ui.operate(lambda: ui.quick_dialog("Delete Branch", "renamed", "Delete Branch"),
               lambda: not git(repo, "branch", "--list", "renamed"), "the deletion of renamed")


def test_merge_a_branch(ui: ForkUI, installed: Any) -> None:
    """Merge (Quick Launch) creates a merge commit of two diverged branches.

    Scenarios: G-MERGE
    """
    repo = _repo(installed, "merging")
    git(repo, "checkout", "-q", "-b", "feature")
    feature = _commit(repo, "feature.txt", "feature\n", "feature work", 1)
    git(repo, "checkout", "-q", "main")
    main = _commit(repo, "main.txt", "main\n", "main work", 2)
    ui.open(repo)
    ui.operate(lambda: ui.quick_dialog("Merge", "feature", "Merge Branch"),
               lambda: git(repo, "rev-parse", "HEAD") != main, "the merge commit")
    parents = git(repo, "rev-list", "--parents", "-n", "1", "HEAD").split()
    assert parents[1:] == [main, feature]
    assert (repo / "feature.txt").is_file()


def test_rebase_onto_a_branch(ui: ForkUI, installed: Any) -> None:
    """Rebase (Quick Launch) moves the current branch's commits onto another branch.

    Scenarios: G-REBASE
    """
    repo = _repo(installed, "rebasing")
    main = _commit(repo, "main.txt", "main\n", "main work", 1)
    git(repo, "checkout", "-q", "-b", "topic", "HEAD~1")
    _commit(repo, "topic.txt", "topic\n", "topic work", 2)
    ui.open(repo)
    rebasing = repo / ".git" / "rebase-merge"
    ui.operate(lambda: ui.quick_dialog("Rebase", "main", "Rebase"),
               lambda: not rebasing.exists() and git(repo, "rev-parse", "topic~1") == main, "topic to sit on main")
    assert git(repo, "log", "-1", "--format=%s", "topic") == "topic work"
    assert _current(repo) == "topic"


def test_interactive_rebase_squash_and_drop(ui: ForkUI, installed: Any) -> None:
    """Fork's Interactive Rebase window: squash one commit into its parent and drop another.

    Scenarios: G-REBASE-INTERACTIVE
    """
    repo = _repo(installed, "irebase")
    linear_history(repo, 4)
    base = git(repo, "rev-parse", "HEAD~3")
    ui.open(repo)
    ui.all_commits()
    # Rows: entry 3, entry 2, entry 1, entry 0, initial; rebase what follows "entry 0".
    before = ui.screen.windows()
    ui.commit_menu(3, "interactive_rebase")
    # Fork's Interactive Rebase window has no X title; it is the new 1020x720 window.
    window = ui.screen.new_window(before, timeout=30, min_width=900)
    rows = [REBASE_ROW_FIRST + index * REBASE_ROW_PITCH for index in range(3)]
    ui.screen.click_in(window, REBASE_ROW_X, rows[1])
    ui.screen.key("s")
    ui.screen.click_in(window, REBASE_ROW_X, rows[0])
    ui.screen.key("d")
    ui.screen.screenshot("interactive-rebase", window)
    ui.screen.click_in(window, *REBASE_BUTTON, settle=2.0)
    # Halfway (entry 1 reworded, entry 2 not squashed yet) the history has 3 commits too: wait for the end.
    rebasing = repo / ".git" / "rebase-merge"
    wait_until(lambda: not rebasing.exists() and git(repo, "rev-list", "--count", "HEAD") == "3", 90,
               "the rebased history")
    assert git(repo, "rev-parse", "HEAD~1") == base
    assert (repo / "log.txt").read_text(encoding="utf-8") == "entry 0\nentry 1\nentry 2\n"


def test_cherry_pick_a_commit(ui: ForkUI, installed: Any) -> None:
    """Cherry-pick Commit (commit menu) applies another branch's commit on the current branch.

    Scenarios: G-CHERRY-PICK
    """
    repo = _repo(installed, "cherry")
    main = _commit(repo, "main.txt", "main\n", "main work", 1)
    git(repo, "checkout", "-q", "-b", "side", "HEAD~1")
    _commit(repo, "picked.txt", "picked\n", "commit to pick", 2)
    git(repo, "checkout", "-q", "main")
    ui.open(repo)
    ui.all_commits()
    ui.screen.screenshot("cherry-list")
    ui.operate(lambda: _menu_dialog(ui, 0, "cherry_pick", "Cherry Pick"),
               lambda: git(repo, "rev-parse", "HEAD~1") == main, "the cherry-picked commit")
    assert git(repo, "log", "-1", "--format=%s") == "commit to pick"
    assert (repo / "picked.txt").read_text(encoding="utf-8") == "picked\n"


def test_revert_a_commit(ui: ForkUI, installed: Any) -> None:
    """Revert Commit (commit menu) adds a commit that undoes it.

    Scenarios: G-REVERT
    """
    repo = _repo(installed, "reverting")
    added = _commit(repo, "added.txt", "added\n", "add a file", 1)
    ui.open(repo)
    ui.all_commits()
    ui.operate(lambda: _menu_dialog(ui, 0, "revert", "Revert"),
               lambda: git(repo, "rev-parse", "HEAD~1") == added, "the revert commit")
    assert git(repo, "log", "-1", "--format=%s").startswith("Revert")
    assert not (repo / "added.txt").exists()


def test_reset_the_branch(ui: ForkUI, installed: Any) -> None:
    """Reset '<branch>' to Here (mixed) moves the branch back and keeps the changes in the files.

    Scenarios: G-RESET
    """
    repo = _repo(installed, "resetting")
    shas = linear_history(repo, 3)
    ui.open(repo)
    ui.all_commits()
    ui.operate(lambda: _menu_dialog(ui, 2, "reset", "Reset Current Branch to Revision"),
               lambda: git(repo, "rev-parse", "main") == shas[0], "main to move back")
    assert (repo / "log.txt").read_text(encoding="utf-8") == "entry 0\nentry 1\nentry 2\n"
    assert git(repo, "status", "--porcelain") == " M log.txt"


def test_create_a_tag(ui: ForkUI, installed: Any) -> None:
    """New Tag (Ctrl+Shift+G) tags the current commit.

    Scenarios: G-TAG-CREATE
    """
    repo = _repo(installed, "tagging")
    head = linear_history(repo, 1)[-1]
    ui.open(repo)
    ui.operate(lambda: ui.shortcut_dialog("ctrl+shift+g", "Create Tag", "v1.0.0"),
               lambda: _has(repo, "v1.0.0^{commit}"), "the tag")
    assert git(repo, "rev-parse", "v1.0.0^{commit}") == head


def _stash_menu(ui: ForkUI, from_top: int | None = None, from_bottom: int | None = None) -> None:
    window = ui.window()
    before = ui.screen.windows()
    ui.screen.click_in(window, *FIRST_STASH, button=3)
    menu = ui.screen.new_window(before, timeout=10)
    y = menu.y + from_top if from_top is not None else menu.y + menu.height - (from_bottom or 0)
    ui.screen.click(menu.x + 40, y, settle=1.0)


def _menu_dialog(ui: ForkUI, row: int, item: str, title: str) -> None:
    """A commit menu item whose dialog ``title`` is accepted."""
    ui.commit_menu(row, item)
    ui.accept(title)


def _save_stash(ui: ForkUI, repo: Path, message: str) -> None:
    ui.operate(lambda: ui.shortcut_dialog("ctrl+shift+h", "Save stash", message),
               lambda: message in git(repo, "stash", "list"), f"the stash {message!r}")


def test_stash_save_apply_pop_drop(ui: ForkUI, installed: Any) -> None:
    """Save Stash (Ctrl+Shift+H), Apply, Delete, and Apply with "delete after applying" (pop).

    Scenarios: G-STASH-SAVE, G-STASH-APPLY, G-STASH-DROP, G-STASH-POP
    """
    repo = _repo(installed, "stashing", {"a.txt": "a\n"})
    (repo / "a.txt").write_text("stashed change\n", encoding="utf-8")
    stashed = repo / "a.txt"
    ui.open(repo)
    _save_stash(ui, repo, "first stash")
    assert git(repo, "status", "--porcelain") == ""
    ui.all_commits()

    def apply() -> None:
        _stash_menu(ui, from_top=STASH_MENU_APPLY)
        ui.screen.screenshot("stash-apply-dialog")
        ui.accept("Apply Stash")

    ui.operate(apply, lambda: stashed.read_text(encoding="utf-8") == "stashed change\n", "the applied stash")
    assert "first stash" in git(repo, "stash", "list")
    git(repo, "checkout", "--", "a.txt")

    def drop() -> None:
        _stash_menu(ui, from_bottom=STASH_MENU_DELETE_FROM_BOTTOM)
        ui.screen.screenshot("stash-delete-dialog")
        ui.accept("Delete Stash")

    ui.operate(drop, lambda: git(repo, "stash", "list") == "", "the stash to be dropped")
    stashed.write_text("stashed change\n", encoding="utf-8")
    _save_stash(ui, repo, "second stash")
    tick_delete_after = [True]

    def pop() -> None:
        _stash_menu(ui, from_top=STASH_MENU_APPLY)
        dialog = ui.wait_dialog("Apply Stash")
        if tick_delete_after:
            # Ticked once: a retried pop finds the box as the first attempt left it.
            ui.screen.click_in(dialog, *APPLY_DELETE_AFTER_BOX)
            tick_delete_after.clear()
        ui.screen.screenshot("stash-pop-dialog", dialog)
        ui.accept("Apply Stash")

    ui.operate(pop, lambda: git(repo, "stash", "list") == "", "the popped stash to be gone")
    assert (repo / "a.txt").read_text(encoding="utf-8") == "stashed change\n"


def test_conflict_resolution_in_forks_merger(ui: ForkUI, installed: Any) -> None:
    """A conflicting merge is resolved in Fork's own merger (take ours), then committed.

    Scenarios: G-CONFLICT-MERGER
    """
    repo = _repo(installed, "conflict", {"shared.txt": "line 1\nline 2\nline 3\n"})
    main, other = conflicting_branches(repo)
    ui.open(repo)
    ui.merge_into_conflict("other")
    assert (repo / ".git" / "MERGE_HEAD").is_file()
    ui.local_changes()
    ui.click(CONFLICTED_FILE)
    ui.click(OPEN_IN_MERGER)
    merger = ui.wait_dialog("Merge")
    ui.screen.click_in(merger, *MERGER_OURS)
    ui.screen.screenshot("merger-ours", merger)
    ui.screen.click_in(merger, *MERGER_RESOLVE, settle=1.5)
    ui.wait_no_dialog()
    wait_until(lambda: "UU" not in git(repo, "status", "--porcelain"), 60, "the conflict to be resolved")
    ui.operate(ui.commit_subject_and_commit, lambda: not (repo / ".git" / "MERGE_HEAD").exists(), "the merge commit")
    assert git(repo, "rev-list", "--parents", "-n", "1", "HEAD").split()[1:] == [main, other]
    assert (repo / "shared.txt").read_text(encoding="utf-8") == "line 1\nline 2 main\nline 3\n"
