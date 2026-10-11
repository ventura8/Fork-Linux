"""Local git operations in Fork's UI that the other git modules leave out: deleting a tag,
unstaging one hunk, aborting a merge that stopped on a conflict, applying a patch, a new
repository and a new remote.

Each test builds its own repository under ``<root>/repos`` (no symlinks) and checks every effect
with native git. Gated by ``FL_E2E_FORK=1``; containers only.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest
from fork_driver import git, wait_until
from fork_ui import HUNK_UNSTAGE, MERGE_BANNER_ABORT, ForkUI, conflicting_branches, linear_history, new_repo

from fixtures import real_tier

if os.environ.get("FL_E2E_FORK") != "1":
    pytest.skip("requires FL_E2E_FORK=1 (the official Fork under Wine)", allow_module_level=True)
real_tier.enforce()

FORTY = "".join(f"line {i}\n" for i in range(1, 41))
# Inside the diff of the selected file: a point in its first hunk.
FIRST_HUNK_POINT = (900, 160)
# The sidebar's "Remotes" header of a repository with one branch (All Commits), and the first item of
# its context menu ("Add New Remote..."), from the popup's top.
SIDEBAR_REMOTES = (60, 290)
ADD_REMOTE_ITEM = 22


def _repo(e2e: Any, name: str, files: dict[str, str] | None = None) -> Path:
    return new_repo(e2e.repos / name, files)


def test_delete_a_tag(ui: ForkUI, installed: Any) -> None:
    """Delete Tag (Quick Launch) deletes the chosen tag, confirmed, and keeps the others.

    Scenarios: G-TAG-DELETE
    """
    repo = _repo(installed, "tag-delete")
    linear_history(repo, 2)
    git(repo, "tag", "v1", "HEAD~1")
    git(repo, "tag", "-a", "v2", "-m", "release two")
    ui.open(repo)
    ui.all_commits()
    ui.operate(lambda: ui.quick_dialog("Delete Tag", "v1", "Delete Tag"), lambda: git(repo, "tag", "--list") == "v2",
               "only v2 to be left")
    assert git(repo, "cat-file", "-t", "v2") == "tag"


def test_unstage_one_hunk(ui: ForkUI, installed: Any) -> None:
    """The Unstage button of one hunk of a staged file unstages that hunk only.

    Scenarios: G-UNSTAGE-HUNK
    """
    repo = _repo(installed, "unstage-hunk", {"a.txt": FORTY})
    (repo / "a.txt").write_text(FORTY.replace("line 3\n", "line 3 changed\n").replace("line 35\n", "line 35 changed\n"),
                                encoding="utf-8")
    git(repo, "add", "a.txt")
    window = ui.open(repo)
    ui.local_changes()

    def unstage() -> None:
        ui.click(ui.staged_row(0))
        installed.screen.hover(*window.point(*FIRST_HUNK_POINT))
        installed.screen.screenshot("unstage-hunk-hover")
        ui.click(HUNK_UNSTAGE)

    ui.operate(unstage, lambda: "line 3 changed" in git(repo, "diff"), "the first hunk to be unstaged")
    assert "line 3 changed" not in git(repo, "diff", "--cached")
    assert "line 35 changed" in git(repo, "diff", "--cached")


def test_abort_a_conflicted_merge(ui: ForkUI, installed: Any) -> None:
    """A merge started in Fork stops on a conflict; the banner's Abort, confirmed, puts the branch and its
    files back as they were.

    Scenarios: G-MERGE-ABORT
    """
    repo = _repo(installed, "merge-abort", {"shared.txt": "line 1\nline 2\nline 3\n"})
    main, _other = conflicting_branches(repo)
    ui.open(repo)
    ui.merge_into_conflict("other")
    assert (repo / ".git" / "MERGE_HEAD").is_file()
    ui.local_changes()
    installed.screen.screenshot("merge-abort-banner")

    def abort() -> None:
        ui.click(MERGE_BANNER_ABORT)
        ui.accept("Do you want to abort the merge?")

    ui.operate(abort, lambda: not (repo / ".git" / "MERGE_HEAD").exists(), "the merge to be aborted")
    assert git(repo, "rev-parse", "HEAD") == main
    assert git(repo, "status", "--porcelain") == ""
    assert (repo / "shared.txt").read_text(encoding="utf-8") == "line 1\nline 2 main\nline 3\n"


def test_apply_a_patch_from_the_home_drive(ui: ForkUI, installed: Any) -> None:
    """Repository > Apply Patch applies a patch picked as ``H:\\...`` to a repository whose index Linux git
    wrote (bundled git under Wine sees no inode or owner: ``core.checkStat=minimal`` keeps such files clean).

    Scenarios: G-APPLY-PATCH
    """
    repo = _repo(installed, "apply-patch", {"a.txt": FORTY})
    (repo / "a.txt").write_text(FORTY.replace("line 3\n", "line 3 patched\n"), encoding="utf-8")
    patches = installed.home / "patches"
    patches.mkdir(exist_ok=True)
    (patches / "apply-patch.patch").write_text(git(repo, "diff") + "\n", encoding="utf-8")
    git(repo, "checkout", "--", "a.txt")
    ui.open(repo)
    ui.local_changes()

    def apply() -> None:
        ui.quick_launch("Apply Patch")
        picker = ui.wait_dialog("Select patch")
        ui.screen.type("H:\\patches\\apply-patch.patch", settle=0.5)
        ui.screen.key("Return", settle=1.5)
        ui.screen.wait_gone(picker)
        ui.accept("Apply Patch")

    ui.operate(apply, lambda: "line 3 patched" in (repo / "a.txt").read_text(encoding="utf-8"), "the patched a.txt")
    assert "line 3 patched" in git(repo, "diff", "HEAD")


def test_init_a_new_repository_in_the_home(ui: ForkUI, installed: Any) -> None:
    """File > Init New Repository (Ctrl+Shift+N) on an empty folder picked as ``H:\\...`` makes a repository
    that native git uses.

    Scenarios: G-INIT-REPO
    """
    target = installed.home / "init-new"
    target.mkdir()
    ui.open(_repo(installed, "init-anchor"))
    ui.keys("ctrl+shift+n", settle=1.5)
    picker = ui.wait_dialog("Select repository")
    ui.screen.type("H:\\init-new", settle=0.5)
    ui.screen.key("Return", settle=2.0)
    ui.screen.screenshot("init-new-after")
    ui.screen.wait_gone(picker)
    wait_until(lambda: (target / ".git" / "HEAD").is_file(), 60, "the new repository in ~/init-new")
    assert git(target, "rev-parse", "--is-inside-work-tree") == "true"
    (target / "first.txt").write_text("first\n", encoding="utf-8")
    git(target, "add", "first.txt")
    git(target, "-c", "user.name=E2E", "-c", "user.email=e2e@example.invalid", "commit", "-qm", "first")
    assert git(target, "log", "--format=%s") == "first"


def test_add_a_remote_from_the_sidebar(ui: ForkUI, installed: Any) -> None:
    """The sidebar's Remotes > Add New Remote adds a remote given as a Linux path; native git fetches from it.

    Scenarios: G-REMOTE-ADD
    """
    repo = _repo(installed, "remote-add")
    bare = installed.repos / "remote-add-origin.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(repo), str(bare)], capture_output=True, check=True)
    window = ui.open(repo)
    ui.all_commits()
    before = ui.screen.windows()
    ui.screen.click_in(window, *SIDEBAR_REMOTES, button=3, settle=0.5)
    menu = ui.screen.new_window(before, timeout=10)
    time.sleep(0.4)
    ui.screen.click(menu.x + 60, menu.y + ADD_REMOTE_ITEM, settle=1.5)
    dialog = ui.screen.new_window(before, timeout=30, min_width=300)
    ui.screen.screenshot("remote-add-dialog", dialog)
    ui.screen.type("e2e-origin", settle=0.3)
    ui.screen.key("Tab", settle=0.3)
    ui.screen.type(str(bare), settle=0.5)
    ui.screen.screenshot("remote-add-typed", dialog)
    ui.screen.key("Return", settle=2.0)
    wait_until(lambda: git(repo, "remote", "get-url", "e2e-origin", check=False) == str(bare), 60,
               "the new remote in .git/config")
    git(repo, "fetch", "-q", "e2e-origin")
    assert git(repo, "rev-parse", "e2e-origin/main") == git(repo, "rev-parse", "HEAD")
