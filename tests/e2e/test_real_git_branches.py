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
from fork_ui import ForkUI, dated_env, linear_history, new_repo, wait_ref

from fixtures import real_tier

if os.environ.get("FL_E2E_FORK") != "1":
    pytest.skip("requires FL_E2E_FORK=1 (the official Fork under Wine)", allow_module_level=True)
real_tier.enforce()

# The sidebar of a repository with only "main" and no remotes or tags: its Stashes entries.
FIRST_STASH = (80, 356)
STASH_MENU_APPLY = 24
STASH_MENU_DELETE_FROM_BOTTOM = 21
APPLY_DELETE_AFTER_BOX = (203, 107)
# The Close button of the Merge Conflict report, from its bottom-right corner.
CLOSE_FROM_RIGHT = 48
CLOSE_FROM_BOTTOM = 33
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


def _accept(ui: ForkUI, title: str) -> None:
    """Wait for Fork's dialog ``title``, press its default button and wait for that dialog to close."""
    dialog = ui.wait_dialog(title)
    ui.screen.key("Return", settle=1.0)
    ui.screen.wait_gone(dialog, timeout=60)


def _accept_if_asked(ui: ForkUI, title: str) -> None:
    """Fork asks before a checkout only when there are local changes; answer when it does."""
    try:
        ui.wait_dialog(title, timeout=5)
    except AssertionError:
        return
    ui.screen.key("Return", settle=1.0)
    ui.wait_no_dialog()


def test_branch_create_checkout_rename_delete(ui: ForkUI, installed: Any) -> None:
    """New Branch, Checkout Branch, Rename Branch and Delete Branch, each through Fork's dialogs.

    Scenarios: G-BRANCH-CREATE, G-BRANCH-CHECKOUT, G-BRANCH-RENAME, G-BRANCH-DELETE
    """
    repo = _repo(installed, "branches")
    head = linear_history(repo, 2)[-1]
    ui.open(repo)
    ui.keys("ctrl+shift+b", settle=1.0)
    ui.wait_dialog("Create Branch")
    ui.screen.type("topic")
    ui.screen.key("Return", settle=1.0)
    ui.wait_no_dialog()
    assert wait_ref(repo, "refs/heads/topic") == head
    assert _current(repo) == "main"
    ui.quick_launch("Checkout Branch", "topic")
    _accept_if_asked(ui, "Checkout Branch")
    wait_until(lambda: _current(repo) == "topic", 60, "the checkout of topic")
    ui.quick_launch("Rename Branch", "topic")
    ui.wait_dialog("Rename Local Branch")
    ui.screen.key("ctrl+a")
    ui.screen.type("renamed")
    ui.screen.key("Return", settle=1.0)
    ui.wait_no_dialog()
    wait_until(lambda: _current(repo) == "renamed", 60, "the rename of topic")
    ui.quick_launch("Checkout Branch", "main")
    _accept_if_asked(ui, "Checkout Branch")
    wait_until(lambda: _current(repo) == "main", 60, "the checkout of main")
    ui.quick_launch("Delete Branch", "renamed")
    _accept(ui, "Delete Branch")
    wait_until(lambda: not git(repo, "branch", "--list", "renamed"), 60, "the deletion of renamed")


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
    ui.quick_launch("Merge", "feature")
    _accept(ui, "Merge Branch")
    wait_until(lambda: git(repo, "rev-parse", "HEAD") != main, 60, "the merge commit")
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
    ui.quick_launch("Rebase", "main")
    _accept(ui, "Rebase")
    wait_until(lambda: git(repo, "rev-parse", "topic~1") == main, 60, "topic to sit on main")
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
    wait_until(lambda: git(repo, "rev-list", "--count", "HEAD") == "3", 90, "the rebased history")
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
    ui.commit_menu(0, "cherry_pick")
    _accept(ui, "Cherry Pick")
    wait_until(lambda: git(repo, "rev-parse", "HEAD~1") == main, 60, "the cherry-picked commit")
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
    ui.commit_menu(0, "revert")
    _accept(ui, "Revert")
    wait_until(lambda: git(repo, "rev-parse", "HEAD~1") == added, 60, "the revert commit")
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
    ui.commit_menu(2, "reset")
    _accept(ui, "Reset Current Branch to Revision")
    wait_until(lambda: git(repo, "rev-parse", "main") == shas[0], 60, "main to move back")
    assert (repo / "log.txt").read_text(encoding="utf-8") == "entry 0\nentry 1\nentry 2\n"
    assert git(repo, "status", "--porcelain") == " M log.txt"


def test_create_a_tag(ui: ForkUI, installed: Any) -> None:
    """New Tag (Ctrl+Shift+G) tags the current commit.

    Scenarios: G-TAG-CREATE
    """
    repo = _repo(installed, "tagging")
    head = linear_history(repo, 1)[-1]
    ui.open(repo)
    ui.keys("ctrl+shift+g", settle=1.0)
    ui.wait_dialog("Create Tag")
    ui.screen.type("v1.0.0")
    ui.screen.key("Return", settle=1.0)
    ui.wait_no_dialog()
    assert wait_until(lambda: git(repo, "rev-parse", "--verify", "--quiet", "v1.0.0^{commit}", check=False), 60,
                      "the tag") == head


def _stash_menu(ui: ForkUI, from_top: int | None = None, from_bottom: int | None = None) -> None:
    window = ui.window()
    before = ui.screen.windows()
    ui.screen.click_in(window, *FIRST_STASH, button=3)
    menu = ui.screen.new_window(before, timeout=10)
    y = menu.y + from_top if from_top is not None else menu.y + menu.height - (from_bottom or 0)
    ui.screen.click(menu.x + 40, y, settle=1.0)


def test_stash_save_apply_pop_drop(ui: ForkUI, installed: Any) -> None:
    """Save Stash (Ctrl+Shift+H), Apply, Delete, and Apply with "delete after applying" (pop).

    Scenarios: G-STASH-SAVE, G-STASH-APPLY, G-STASH-DROP, G-STASH-POP
    """
    repo = _repo(installed, "stashing", {"a.txt": "a\n"})
    (repo / "a.txt").write_text("stashed change\n", encoding="utf-8")
    ui.open(repo)
    ui.keys("ctrl+shift+h", settle=1.0)
    ui.wait_dialog("Save stash")
    ui.screen.type("first stash")
    ui.screen.key("Return", settle=1.0)
    ui.wait_no_dialog()
    wait_until(lambda: "first stash" in git(repo, "stash", "list"), 60, "the stash")
    assert git(repo, "status", "--porcelain") == ""
    ui.all_commits()
    _stash_menu(ui, from_top=STASH_MENU_APPLY)
    ui.screen.screenshot("stash-apply-dialog")
    _accept(ui, "Apply Stash")
    wait_until(lambda: (repo / "a.txt").read_text(encoding="utf-8") == "stashed change\n", 60, "the applied stash")
    assert "first stash" in git(repo, "stash", "list")
    git(repo, "checkout", "--", "a.txt")
    _stash_menu(ui, from_bottom=STASH_MENU_DELETE_FROM_BOTTOM)
    ui.screen.screenshot("stash-delete-dialog")
    ui.screen.key("Return", settle=1.0)
    ui.wait_no_dialog()
    wait_until(lambda: git(repo, "stash", "list") == "", 60, "the stash to be dropped")
    (repo / "a.txt").write_text("stashed change\n", encoding="utf-8")
    ui.keys("ctrl+shift+h", settle=1.0)
    ui.wait_dialog("Save stash")
    ui.screen.type("second stash")
    ui.screen.key("Return", settle=1.0)
    ui.wait_no_dialog()
    wait_until(lambda: "second stash" in git(repo, "stash", "list"), 60, "the second stash")
    _stash_menu(ui, from_top=STASH_MENU_APPLY)
    dialog = ui.wait_dialog("Apply Stash")
    ui.screen.click_in(dialog, *APPLY_DELETE_AFTER_BOX)
    ui.screen.screenshot("stash-pop-dialog", dialog)
    ui.screen.key("Return", settle=1.0)
    ui.wait_no_dialog()
    wait_until(lambda: git(repo, "stash", "list") == "", 60, "the popped stash to be gone")
    assert (repo / "a.txt").read_text(encoding="utf-8") == "stashed change\n"


def test_conflict_resolution_in_forks_merger(ui: ForkUI, installed: Any) -> None:
    """A conflicting merge is resolved in Fork's own merger (take ours), then committed.

    Scenarios: G-CONFLICT-MERGER
    """
    repo = _repo(installed, "conflict", {"shared.txt": "line 1\nline 2\nline 3\n"})
    git(repo, "checkout", "-q", "-b", "other")
    other = _commit(repo, "shared.txt", "line 1\nline 2 other\nline 3\n", "other edits line 2", 1)
    git(repo, "checkout", "-q", "main")
    main = _commit(repo, "shared.txt", "line 1\nline 2 main\nline 3\n", "main edits line 2", 2)
    ui.open(repo)
    ui.quick_launch("Merge", "other")
    _accept(ui, "Merge Branch")
    # The Merge Conflict report keeps the focus in its text: its Close button is clicked.
    report = ui.wait_dialog("Merge Conflict", timeout=60)
    ui.screen.click_in(report, report.width - CLOSE_FROM_RIGHT, report.height - CLOSE_FROM_BOTTOM)
    ui.wait_no_dialog()
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
    ui.commit_subject_and_commit()
    wait_until(lambda: not (repo / ".git" / "MERGE_HEAD").exists(), 60, "the merge commit")
    assert git(repo, "rev-list", "--parents", "-n", "1", "HEAD").split()[1:] == [main, other]
    assert (repo / "shared.txt").read_text(encoding="utf-8") == "line 1\nline 2 main\nline 3\n"
