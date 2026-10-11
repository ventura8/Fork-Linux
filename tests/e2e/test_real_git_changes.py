"""Local changes in Fork's UI: stage and unstage files, hunks and lines, discard, commit and amend.

Each test builds its own repository under ``<root>/repos`` (no symlinks: Fork discards and
rewrites files there) and checks the result with native git. Gated by ``FL_E2E_FORK=1``;
containers only.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from fork_driver import git, wait_until
from fork_ui import (
    DIFF_GUTTER_X,
    HUNK_DISCARD,
    HUNK_STAGE,
    STAGE_BUTTON,
    UNSTAGE_BUTTON,
    ForkUI,
    dated_env,
    new_repo,
)

from fixtures import real_tier

if os.environ.get("FL_E2E_FORK") != "1":
    pytest.skip("requires FL_E2E_FORK=1 (the official Fork under Wine)", allow_module_level=True)
real_tier.enforce()

FORTY = "".join(f"line {i}\n" for i in range(1, 41))
# Inside the diff of the selected file: a point in the first hunk; the lines of the diff in
# test_stage_one_line (" one", "+first added", "+second added", " two") and the Stage button of
# their change block.
FIRST_HUNK_POINT = (900, 160)
FIRST_ADDED_LINE_Y = 148
BLOCK_STAGE = (HUNK_STAGE[0], 172)


def _repo(e2e: Any, name: str, files: dict[str, str] | None = None) -> Path:
    return new_repo(e2e.repos / name, files)


def _cached(repo: Path) -> str:
    return git(repo, "diff", "--cached", "--name-only")


def _wait_cached(repo: Path, expected: str) -> None:
    wait_until(lambda: _cached(repo) == expected or None, 30, f"the index to hold {expected!r}")


def test_stage_and_unstage_a_file(ui: ForkUI, installed: Any) -> None:
    """Select a file in Unstaged and press Stage; select it in Staged and press Unstage.

    Scenarios: G-STAGE-FILE, G-UNSTAGE-FILE
    """
    repo = _repo(installed, "stagefile", {"a.txt": "a\n", "b.txt": "b\n"})
    (repo / "a.txt").write_text("a changed\n", encoding="utf-8")
    (repo / "b.txt").write_text("b changed\n", encoding="utf-8")
    ui.open(repo)
    ui.local_changes()
    ui.click(ui.unstaged_row(0))
    ui.click(STAGE_BUTTON)
    _wait_cached(repo, "a.txt")
    installed.screen.screenshot("stage-file")
    ui.click(ui.staged_row(0))
    ui.click(UNSTAGE_BUTTON)
    _wait_cached(repo, "")
    assert git(repo, "status", "--porcelain").splitlines() == [" M a.txt", " M b.txt"]


def test_stage_one_hunk(ui: ForkUI, installed: Any) -> None:
    """The Stage button of one hunk stages that hunk only.

    Scenarios: G-STAGE-HUNK
    """
    repo = _repo(installed, "stagehunk", {"a.txt": FORTY})
    (repo / "a.txt").write_text(FORTY.replace("line 3\n", "line 3 changed\n").replace("line 35\n", "line 35 changed\n"),
                                encoding="utf-8")
    window = ui.open(repo)
    ui.local_changes()
    ui.click(ui.unstaged_row(0))
    installed.screen.hover(*window.point(*FIRST_HUNK_POINT))
    installed.screen.screenshot("stage-hunk-hover")
    ui.click(HUNK_STAGE)
    wait_until(lambda: "line 3 changed" in git(repo, "diff", "--cached"), 30, "the first hunk to be staged")
    staged = git(repo, "diff", "--cached")
    assert "line 35 changed" not in staged
    assert "line 35 changed" in git(repo, "diff")


def test_stage_one_line(ui: ForkUI, installed: Any) -> None:
    """Selecting one added line in the diff and pressing its Stage button stages only that line.

    Scenarios: G-STAGE-LINE
    """
    repo = _repo(installed, "stageline", {"a.txt": "one\ntwo\n"})
    (repo / "a.txt").write_text("one\nfirst added\nsecond added\ntwo\n", encoding="utf-8")
    window = ui.open(repo)
    ui.local_changes()
    ui.click(ui.unstaged_row(0))
    ui.click((DIFF_GUTTER_X, FIRST_ADDED_LINE_Y))
    installed.screen.hover(*window.point(FIRST_HUNK_POINT[0], FIRST_ADDED_LINE_Y))
    installed.screen.screenshot("stage-line-selected")
    ui.click(BLOCK_STAGE)
    wait_until(lambda: "first added" in git(repo, "diff", "--cached"), 30, "the selected line to be staged")
    assert "second added" not in git(repo, "diff", "--cached")


def test_discard_a_hunk(ui: ForkUI, installed: Any) -> None:
    """A hunk's Discard button, confirmed, restores the committed text.

    Scenarios: G-DISCARD
    """
    repo = _repo(installed, "discard", {"a.txt": FORTY, "keep.txt": "keep\n"})
    (repo / "a.txt").write_text(FORTY.replace("line 3\n", "line 3 throw away\n"), encoding="utf-8")
    (repo / "keep.txt").write_text("keep changed\n", encoding="utf-8")
    window = ui.open(repo)
    ui.local_changes()
    ui.click(ui.unstaged_row(0))
    installed.screen.hover(*window.point(*FIRST_HUNK_POINT))
    ui.click(HUNK_DISCARD)
    installed.screen.screenshot("discard-confirm")
    installed.screen.key("Return", settle=1.5)
    wait_until(lambda: (repo / "a.txt").read_text(encoding="utf-8") == FORTY, 30, "a.txt to be restored")
    assert (repo / "keep.txt").read_text(encoding="utf-8") == "keep changed\n"


def test_commit_with_a_utf8_message(ui: ForkUI, installed: Any) -> None:
    """Stage, type a UTF-8 subject and description, commit (Ctrl+Enter): git has them byte for byte.

    Scenarios: G-COMMIT, G-COMMIT-UTF8
    """
    repo = _repo(installed, "commitutf8", {"a.txt": "a\n"})
    (repo / "a.txt").write_text("a changed\n", encoding="utf-8")
    ui.open(repo)
    ui.local_changes()
    ui.click(ui.unstaged_row(0))
    ui.click(STAGE_BUTTON)
    _wait_cached(repo, "a.txt")
    subject = "Grüße ümlaut ✓ 中文"
    ui.commit(subject, "Zweite Zeile: ß")
    wait_until(lambda: git(repo, "log", "-1", "--format=%s") == subject or None, 60, "the commit")
    assert git(repo, "log", "-1", "--format=%b") == "Zweite Zeile: ß"
    assert git(repo, "log", "-1", "--format=%an <%ae>") == "E2E Test <e2e@example.invalid>"
    assert git(repo, "status", "--porcelain") == ""


def test_amend_the_last_commit(ui: ForkUI, installed: Any) -> None:
    """Amend replaces the last commit (same parent, new message) instead of adding one.

    Scenarios: G-COMMIT-AMEND
    """
    repo = _repo(installed, "amend", {"a.txt": "a\n"})
    (repo / "a.txt").write_text("a changed\n", encoding="utf-8")
    git(repo, "commit", "-qam", "first message", env=dated_env(1))
    parent = git(repo, "rev-parse", "HEAD~1")
    ui.open(repo)
    ui.local_changes()
    ui.commit("amended message", amend=True)
    wait_until(lambda: git(repo, "log", "-1", "--format=%s") == "amended message" or None, 60, "the amend")
    assert git(repo, "rev-parse", "HEAD~1") == parent
    assert git(repo, "rev-list", "--count", "HEAD") == "2"
    assert "commit (amend)" in git(repo, "reflog", "-1")


def test_commit_template_prefills_the_message(ui: ForkUI, installed: Any) -> None:
    """A repository's commit.template fills the commit box.

    Scenarios: G-COMMIT-TEMPLATE
    """
    repo = _repo(installed, "template", {"a.txt": "a\n", ".gitmessage": "Template subject\n\nTemplate body\n"})
    git(repo, "config", "commit.template", ".gitmessage")
    (repo / "a.txt").write_text("a changed\n", encoding="utf-8")
    ui.open(repo)
    ui.local_changes()
    ui.click(ui.unstaged_row(0))
    ui.click(STAGE_BUTTON)
    _wait_cached(repo, "a.txt")
    ui.commit_subject_and_commit()
    installed.screen.screenshot("template-committed")
    wait_until(lambda: git(repo, "log", "-1", "--format=%s") == "Template subject" or None, 60,
               "the commit with the template's message")
