"""Reading history in Fork's UI: commit list, graph, search, filter, blame, file history, diffs, tabs.

Each test builds its own repository under ``<root>/repos`` (dated commits, a ``.gitignore``)
and opens it as a tab of one running Fork. What a row shows is read back by creating a branch
at it (New Branch) and asking native git where the branch points. Gated by ``FL_E2E_FORK=1``;
containers only.
"""

from __future__ import annotations

import os
import struct
import subprocess
import time
import zlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fork_driver import git, wait_until
from fork_ui import CHANGES_TAB, COMMIT_ROW_FIRST, COMMIT_ROW_PITCH, ForkUI, dated_env, linear_history, new_repo

from fixtures import real_tier

if os.environ.get("FL_E2E_FORK") != "1":
    pytest.skip("requires FL_E2E_FORK=1 (the official Fork under Wine)", allow_module_level=True)
real_tier.enforce()

LARGE_COMMITS = 5000
LARGE_FILES = 3000
LARGE_OPEN_SECONDS = 90.0
# The graph column of the commit list and the lower (commit details) pane, inside the main window.
GRAPH_AREA = (300, 100, 70, 160)
DETAILS_AREA = (330, 560, 1150, 360)
# A diff that does not show is looked at once more (Fork's git process may fail to start).
DIFF_ATTEMPTS = 2


def _repo(e2e: Any, name: str, files: dict[str, str] | None = None) -> Path:
    return new_repo(e2e.repos / name, files)


def _png(path: Path, rgb: tuple[int, int, int], size: int = 96) -> None:
    """A solid-colour PNG (stdlib only)."""
    row = b"\x00" + bytes(rgb) * size
    raw = row * size

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw))
                     + chunk(b"IEND", b""))


def _colours(e2e: Any, image: Path, area: tuple[int, int, int, int], window: Any) -> dict[tuple[int, int, int], int]:
    """Pixel counts per colour of an area of the main window in a screenshot."""
    x, y, width, height = area
    ppm = subprocess.run(["magick", str(image), "-crop", f"{width}x{height}+{window.x + x}+{window.y + y}",
                          "+repage", "ppm:-"], capture_output=True, timeout=60, check=True).stdout
    header_end = 0
    for _field in range(4):
        while ppm[header_end:header_end + 1].isspace():
            header_end += 1
        while not ppm[header_end:header_end + 1].isspace():
            header_end += 1
    pixels = ppm[header_end + 1:]
    counts: dict[tuple[int, int, int], int] = {}
    for index in range(0, len(pixels) - 2, 3):
        rgb = (pixels[index], pixels[index + 1], pixels[index + 2])
        counts[rgb] = counts.get(rgb, 0) + 1
    return counts


def _saturated(counts: dict[tuple[int, int, int], int], minimum: int = 20) -> list[tuple[int, int, int]]:
    """Clearly coloured (not grey) colours with at least ``minimum`` pixels."""
    return [rgb for rgb, count in counts.items() if count >= minimum and max(rgb) - min(rgb) > 80]


def test_history_lists_the_commits_newest_first(ui: ForkUI, installed: Any) -> None:
    """The commit list shows the repository's history, newest first (read back with Checkout Commit).

    Scenarios: G-HISTORY, G-CHECKOUT-COMMIT
    """
    repo = _repo(installed, "history")
    shas = linear_history(repo, 6)
    ui.open(repo)
    ui.all_commits()
    installed.screen.screenshot("history-list")
    assert ui.commit_at(repo, 2) == shas[-3]
    assert ui.commit_at(repo, 6) == git(repo, "rev-list", "--max-parents=0", "HEAD")
    assert ui.commit_at(repo, 0) == shas[-1]


def test_graph_draws_the_merge(ui: ForkUI, installed: Any) -> None:
    """A merge shows as a merge commit with two coloured lanes in the graph.

    Scenarios: G-GRAPH
    """
    repo = _repo(installed, "graph")
    linear_history(repo, 2, "main.txt")
    git(repo, "checkout", "-q", "-b", "side", "HEAD~1")
    with open(repo / "side.txt", "w", encoding="utf-8") as handle:
        handle.write("side\n")
    git(repo, "add", "side.txt")
    git(repo, "commit", "-qm", "side work", env=dated_env(10))
    git(repo, "checkout", "-q", "main")
    git(repo, "merge", "-q", "--no-ff", "side", "-m", "merge side", env=dated_env(20))
    window = ui.open(repo)
    ui.all_commits()
    shot = installed.screen.screenshot("graph")
    lanes = _saturated(_colours(installed, shot, GRAPH_AREA, window))
    assert len(lanes) >= 2, f"the graph shows {len(lanes)} coloured lane(s): {lanes}"
    merge = ui.commit_at(repo, 0)
    assert merge == git(repo, "rev-parse", "main")
    assert git(repo, "rev-list", "--parents", "-n", "1", merge).count(" ") == 2


def test_search_finds_a_commit_by_message(ui: ForkUI, installed: Any) -> None:
    """The sidebar's Search Commits finds a commit by its message and selects it.

    Scenarios: G-SEARCH
    """
    repo = _repo(installed, "search")
    linear_history(repo, 3)
    (repo / "needle.txt").write_text("needle\n", encoding="utf-8")
    git(repo, "add", "needle.txt")
    git(repo, "commit", "-qm", "the needle commit", env=dated_env(30))
    needle = git(repo, "rev-parse", "HEAD")
    linear_history(repo, 3, "after.txt")
    window = ui.open(repo)
    ui.all_commits()
    installed.screen.click_in(window, *SIDEBAR_SEARCH)
    installed.screen.type("needle", settle=1.0)
    installed.screen.key("Return", settle=3.0)
    shot = installed.screen.screenshot("search-results")
    # Rows: after 2, after 1, after 0, the needle commit, entry 2, ...: the search selects row 3.
    assert _selected_rows(installed, shot, window, 8) == [3]
    assert ui.commit_at(repo, 3) == needle


def _selected_rows(e2e: Any, shot: Path, window: Any, rows: int) -> list[int]:
    """The commit-list rows drawn with the (focused, blue) selection colour."""
    selected = []
    for row in range(rows):
        y = COMMIT_ROW_FIRST + row * COMMIT_ROW_PITCH
        colours = _colours(e2e, shot, (SELECTION_PROBE_X, y - 3, 40, 6), window)
        blue = sum(count for (red, green, blue_value), count in colours.items() if blue_value > 150 and red < 110)
        if blue > 120:
            selected.append(row)
    return selected


def test_sidebar_filter_keeps_matching_refs(ui: ForkUI, installed: Any) -> None:
    """The sidebar filter narrows the branch tree; the match can be checked out from there.

    Scenarios: G-FILTER
    """
    repo = _repo(installed, "filter")
    for branch in ("alpha", "bravo", "needle-branch", "zulu"):
        git(repo, "branch", branch)
    window = ui.open(repo)
    installed.screen.click_in(window, *SIDEBAR_FILTER_BOX)
    installed.screen.type("needle", settle=1.5)
    installed.screen.screenshot("filter-typed")
    installed.screen.click_in(window, *FILTER_FIRST_BRANCH, repeat=2)
    installed.screen.screenshot("filter-checkout")
    wait_until(lambda: git(repo, "branch", "--show-current") == "needle-branch", 60, "the filtered branch checkout")


def _open_tool_window(ui: ForkUI, command: str, file_name: str, pattern: str) -> Any:
    ui.quick_launch(command, file_name)
    return ui.screen.wait_window(pattern=pattern, timeout=60)


def test_blame_window_opens_for_a_file(ui: ForkUI, installed: Any) -> None:
    """Quick Launch > Blame opens the Blame window of a file; each line names the commit that last
    changed it (read back with the line's Copy Commit SHA).

    Scenarios: G-BLAME
    """
    repo = _repo(installed, "annotated", {"code.py": "def hello():\n    return 1\n# end\n"})
    first = git(repo, "rev-parse", "HEAD")
    (repo / "code.py").write_text("def hello():\n    return 2\n# end\n", encoding="utf-8")
    git(repo, "commit", "-qam", "return two", env=dated_env(5))
    ui.open(repo)
    blame = _open_tool_window(ui, "Blame", "code.py", r"^code\.py - Blame$")
    time.sleep(2)
    installed.screen.screenshot("blame", blame)
    assert ui.copy_revision_sha(blame, BLAME_ANNOTATION_X, BLAME_LINE_1_Y) == first
    assert ui.copy_revision_sha(blame, BLAME_ANNOTATION_X, BLAME_LINE_2_Y) == git(repo, "rev-parse", "HEAD")


def test_file_history_window_opens(ui: ForkUI, installed: Any) -> None:
    """Quick Launch > File History opens the history window of a file: every commit that changed it,
    newest first (rows read back with Copy Commit SHA).

    Scenarios: G-FILE-HISTORY
    """
    repo = _repo(installed, "versions", {"notes.txt": "one\n"})
    for index in range(3):
        (repo / "notes.txt").write_text(f"version {index}\n", encoding="utf-8")
        git(repo, "commit", "-qam", f"notes {index}", env=dated_env(index + 1))
    ui.open(repo)
    history = _open_tool_window(ui, "File History", "notes.txt", r"^notes\.txt - (File )?History$")
    time.sleep(2)
    installed.screen.screenshot("file-history", history)
    revisions = git(repo, "log", "--format=%H", "--", "notes.txt").split()
    assert len(revisions) == 4
    for row in (0, 1, 3):
        y = HISTORY_ROW_FIRST + row * HISTORY_ROW_PITCH
        assert ui.copy_revision_sha(history, HISTORY_ROW_X, y) == revisions[row], f"File History row {row}"


def _show_changes_of_top_commit(ui: ForkUI) -> Any:
    window = ui.window()
    ui.all_commits()
    ui.select_commit(0)
    ui.screen.click_in(window, *CHANGES_TAB, settle=2.0)
    return window


def _diff_pixels(ui: ForkUI, e2e: Any, name: str, kinds: Callable[[dict[tuple[int, int, int], int]], tuple[int, int]],
                 enough: int) -> tuple[int, int]:
    """Show the top commit's changes and count two kinds of pixels in the diff (``kinds``).

    Under Wine Fork now and then fails to start the git process behind the diff and shows
    "Win32Exception ... StartWithCreateProcess" instead; a user would look again, so does the test (once).
    """
    found = (0, 0)
    for attempt in range(DIFF_ATTEMPTS):
        if attempt:
            ui.refresh()
        window = _show_changes_of_top_commit(ui)
        time.sleep(2)
        found = kinds(_colours(e2e, e2e.screen.screenshot(name), DETAILS_AREA, window))
        if min(found) > enough:
            break
    return found


def test_text_diff_of_a_commit(ui: ForkUI, installed: Any) -> None:
    """Selecting a commit shows its text diff with removed and added lines.

    Scenarios: G-DIFF-TEXT
    """
    repo = _repo(installed, "textdiff", {"poem.txt": "".join(f"line {i}\n" for i in range(12))})
    (repo / "poem.txt").write_text("".join(f"line {i}\n" for i in range(12)).replace("line 5\n", "LINE FIVE\n"),
                                   encoding="utf-8")
    git(repo, "commit", "-qam", "shout line five", env=dated_env(3))
    ui.open(repo)

    def added_removed(colours: dict[tuple[int, int, int], int]) -> tuple[int, int]:
        greens = sum(count for rgb, count in colours.items() if rgb[1] > rgb[0] + 15 and rgb[1] > rgb[2] + 15)
        reds = sum(count for rgb, count in colours.items() if rgb[0] > rgb[1] + 15 and rgb[0] > rgb[2] + 15)
        return greens, reds

    greens, reds = _diff_pixels(ui, installed, "text-diff", added_removed, 500)
    assert greens > 500 and reds > 500, f"diff colours: {greens} green, {reds} red pixels"


def test_image_diff_of_a_changed_png(ui: ForkUI, installed: Any) -> None:
    """A changed PNG shows as an image diff: the old and the new picture.

    Scenarios: G-DIFF-IMAGE
    """
    repo = _repo(installed, "imagediff")
    _png(repo / "picture.png", (255, 0, 0))
    git(repo, "add", "picture.png")
    git(repo, "commit", "-qm", "red picture", env=dated_env(1))
    _png(repo / "picture.png", (0, 0, 255))
    git(repo, "commit", "-qam", "blue picture", env=dated_env(2))
    ui.open(repo)

    def old_new(colours: dict[tuple[int, int, int], int]) -> tuple[int, int]:
        red = sum(count for rgb, count in colours.items() if rgb[0] > 200 and rgb[1] < 60 and rgb[2] < 60)
        blue = sum(count for rgb, count in colours.items() if rgb[2] > 200 and rgb[0] < 60 and rgb[1] < 60)
        return red, blue

    red, blue = _diff_pixels(ui, installed, "image-diff", old_new, 1000)
    assert red > 1000 and blue > 1000, f"image diff: {red} red, {blue} blue pixels"


def test_quick_launch_switches_repositories(ui: ForkUI, installed: Any) -> None:
    """Quick Launch (Ctrl+P) switches to another open repository by name.

    Scenarios: G-QUICK-LAUNCH
    """
    first = _repo(installed, "qlfirst")
    linear_history(first, 2)
    second = _repo(installed, "qlsecond")
    ui.open(first)
    ui.open(second)
    head = git(first, "rev-parse", "HEAD")
    ui.quick_launch("qlfirst")
    ui.all_commits()
    assert ui.commit_at(first, 1) == git(first, "rev-parse", "main~1")
    assert git(second, "symbolic-ref", "-q", "HEAD") == "refs/heads/main"
    assert head != git(first, "rev-parse", "HEAD")


def test_tabs_close_and_restore(ui: ForkUI, installed: Any) -> None:
    """File > Close Tab (Ctrl+W) and Restore Closed Tab (Ctrl+Shift+T).

    Scenarios: G-TABS
    """
    kept = _repo(installed, "tabkept")
    closed = _repo(installed, "tabclosed")
    ui.open(kept)
    ui.open(closed)
    linear_history(kept, 1)
    linear_history(closed, 1)
    ui.keys("ctrl+w", settle=1.5)
    ui.all_commits()
    assert ui.commit_at(kept, 1) == git(kept, "rev-parse", "main~1")
    ui.keys("ctrl+shift+t", settle=2.0)
    ui.all_commits()
    assert ui.commit_at(closed, 1) == git(closed, "rev-parse", "main~1")


def _large_repo(e2e: Any) -> tuple[Path, str]:
    """5000 commits over 3000 files, made with git fast-import (seconds, not minutes)."""
    path = e2e.repos / "large"
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    stream = [b"commit refs/heads/main\nmark :1\nauthor E2E <e2e@example.invalid> 1767225600 +0000\n"
              b"committer E2E <e2e@example.invalid> 1767225600 +0000\ndata 8\ninitial\n"
              b"M 100644 inline .gitignore\ndata 6\n*.tmp\n"]
    for index in range(LARGE_FILES):
        body = f"file {index}\n".encode()
        stream.append(f"M 100644 inline d{index // 100:02d}/f{index:04d}.txt\ndata {len(body)}\n".encode() + body)
    for index in range(1, LARGE_COMMITS):
        stamp = 1767225600 + index * 60
        message = f"commit {index}\n".encode()
        body = f"file {index % LARGE_FILES}\nrevision {index}\n".encode()
        stream.append(f"commit refs/heads/main\nmark :{index + 1}\n"
                      f"author E2E <e2e@example.invalid> {stamp} +0000\n"
                      f"committer E2E <e2e@example.invalid> {stamp} +0000\n".encode()
                      + f"data {len(message)}\n".encode() + message
                      + f"from :{index}\nM 100644 inline d{(index % LARGE_FILES) // 100:02d}/"
                        f"f{index % LARGE_FILES:04d}.txt\ndata {len(body)}\n".encode() + body)
    subprocess.run(["git", "-C", str(path), "fast-import", "--quiet"], input=b"".join(stream), check=True,
                   timeout=300)
    git(path, "reset", "-q", "--hard", "main")
    return path, git(path, "rev-list", "--max-parents=0", "HEAD")


def test_large_repository_opens_quickly(ui: ForkUI, installed: Any) -> None:
    """A repository with 5000 commits and 3000 files opens, and its whole history is listed.

    Scenarios: G-LARGE-REPO
    """
    repo, root_commit = _large_repo(installed)
    assert git(repo, "rev-list", "--count", "HEAD") == str(LARGE_COMMITS)
    started = time.monotonic()
    ui.open(repo)
    ui.all_commits()
    ui.select_commit(0)
    ui.screen.key("End", settle=2.0)
    installed.screen.screenshot("large-end")
    assert ui.checkout_selected(repo) == root_commit
    assert time.monotonic() - started < LARGE_OPEN_SECONDS


SIDEBAR_SEARCH = (171, 181)
# Inside the Blame window (1020x720) of test_blame_window_opens_for_a_file: the commit column of line 1
# and of line 2 (which the inline diff of the newest commit shows below its removed version).
BLAME_ANNOTATION_X = 250
BLAME_LINE_1_Y = 112
BLAME_LINE_2_Y = 145
# Inside the History window (1020x720): its revision rows (three lines each).
HISTORY_ROW_X = 200
HISTORY_ROW_FIRST = 125
HISTORY_ROW_PITCH = 62
SELECTION_PROBE_X = 760
SIDEBAR_FILTER_BOX = (150, 214)
FILTER_FIRST_BRANCH = (110, 270)
