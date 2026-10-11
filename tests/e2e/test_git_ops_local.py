"""Local git operations in Fork's UI that the other git modules leave out: deleting a tag,
unstaging one hunk and aborting a merge that stopped on a conflict.

Each test builds its own repository under ``<root>/repos`` (no symlinks) and checks every effect
with native git. Gated by ``FL_E2E_FORK=1``; containers only.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from fork_driver import git
from fork_ui import HUNK_UNSTAGE, MERGE_BANNER_ABORT, ForkUI, conflicting_branches, linear_history, new_repo

from fixtures import real_tier

if os.environ.get("FL_E2E_FORK") != "1":
    pytest.skip("requires FL_E2E_FORK=1 (the official Fork under Wine)", allow_module_level=True)
real_tier.enforce()

FORTY = "".join(f"line {i}\n" for i in range(1, 41))
# Inside the diff of the selected file: a point in its first hunk.
FIRST_HUNK_POINT = (900, 160)


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
