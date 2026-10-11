"""Fork's own user interface for the git scenarios: where things are and how a user reaches them.

Measured with the official Fork 2.23.2 on the tier's display: the main window is seeded at
1500x940 (``fork_driver.MAIN_GEOMETRY``) and every offset below is inside that window. The
repositories the tests create carry a ``.gitignore``, so Fork shows no "Consider adding a
.gitignore" banner and the layout does not shift. Wherever Fork offers one, a keyboard
path is used (Quick Launch commands, shortcuts, the keyboard context menu); offsets are the
fallback for controls that have none.

Every effect is verified with native git by the tests, never by reading pixels alone.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

from fork_driver import DriverError, Fork, Window, fork_pids, git, wait_until

# Commit list (All Commits): rows from the top of the list.
COMMIT_ROW_FIRST = 108
COMMIT_ROW_PITCH = 23
COMMIT_ROW_X = 1000
# Local Changes: the Unstaged and Staged lists, their header buttons, the commit box.
UNSTAGED_FIRST = 157
STAGED_FIRST = 564
FILE_ROW_PITCH = 20
FILE_ROW_X = 420
STAGE_BUTTON = (521, 127)
UNSTAGE_BUTTON = (514, 533)
COMMIT_SUBJECT = (1020, 854)
COMMIT_DESCRIPTION = (1020, 876)
AMEND_BOX = (577, 911)
COMMIT_BUTTON = (1405, 911)
# The hover buttons of a diff hunk (shown while the pointer is inside the hunk).
HUNK_STAGE = (1369, 158)
HUNK_DISCARD = (1430, 158)
# Line staging: a click in the diff's gutter selects that line; the change block's Stage button
# (bottom right of the block) then stages the selected lines only.
DIFF_GUTTER_X = 575
# The sidebar: its search tab and the filter box above the branch tree.
SIDEBAR_SEARCH_TAB = (171, 181)
SIDEBAR_FILTER = (150, 214)
TAB_SETTLE = 2.5
# The commit context menu: its items' distance above the popup's bottom edge (the popup
# window includes Fork's shadow margin). The submenu of "Interactive Rebase" starts with
# "Interactively Rebase '<branch>' to Here...".
MENU_FROM_BOTTOM = {
    "copy_info": 19,
    "copy_sha": 42,
    "compare": 75,
    "save_patch": 108,
    "revert": 131,
    "cherry_pick": 154,
    "checkout_commit": 177,
    "reset": 209,
    "interactive_rebase": 242,
}
MENU_TEXT_X = 60
SUBMENU_FIRST_ITEM = 21
# The lower pane of All Commits: its Changes tab.
CHANGES_TAB = (450, 534)
# Double-clicks on an unstaged row before giving up on staging it.
STAGE_ATTEMPTS = 3
# Fork's AskPass window (520x185, titled with the repository's name): its text field.
ASKPASS_FIELD = (335, 91)


class ForkUI:
    """A user at Fork's main window."""

    def __init__(self, fork: Fork) -> None:
        self.fork = fork
        self.screen = fork.screen

    # -- windows and tabs -----------------------------------------------------------------------

    def window(self) -> Window:
        """The main window (fails when Fork is not open)."""
        window = self.fork.main_window()
        if window is None:
            raise DriverError("Fork's main window is not open")
        return window

    def open(self, repo: Path) -> Window:
        """Show ``repo``: start Fork on it, or hand it to the running Fork (it becomes the active tab)."""
        if not fork_pids(self.fork.prefix):
            window = self.fork.start(repo)
        else:
            self.dismiss_dialogs()
            forwarded = self.fork.launch(repo)
            if forwarded.wait(timeout=60) != 0:
                raise DriverError(f"fork-linux run {repo} failed while Fork runs")
            time.sleep(TAB_SETTLE)
            window = self.window()
        self.fork.focus(window)
        return window

    def dialogs(self) -> list[Window]:
        """Fork's windows other than the main window (dialogs, popups, Blame, History...)."""
        return [window for window in self.screen.windows() if not window.is_main]

    def dismiss_dialogs(self) -> None:
        """Close leftover dialogs with Escape; kill Fork when one will not close."""
        for _attempt in range(3):
            if not self.dialogs():
                return
            self.screen.key("Escape", settle=0.8)
        for window in self.dialogs():
            self.screen.click_in(window, window.width - 16, 14)
        time.sleep(1)
        if self.dialogs():
            self.fork.kill()

    def wait_dialog(self, title: str, timeout: float = 30) -> Window:
        """Wait for a dialog titled ``title``."""
        return self.screen.wait_window(title, timeout=timeout)

    def wait_no_dialog(self, timeout: float = 60) -> None:
        """Wait until every dialog has closed (the operation was accepted)."""
        wait_until(lambda: not self.dialogs(), timeout, "Fork's dialog to close")

    # -- keyboard paths ---------------------------------------------------------------------------

    def keys(self, *keys: str, settle: float = 0.5) -> None:
        """Press keys in the main window."""
        self.fork.focus(self.window())
        self.screen.key(*keys, settle=settle)

    def quick_launch(self, command: str, pick: str | None = None) -> None:
        """Run a Quick Launch command (Ctrl+P); ``pick`` answers its second step (a branch, a file)."""
        self.keys("ctrl+p", settle=0.8)
        self.screen.type(command, settle=0.8)
        self.screen.key("Return", settle=1.0)
        if pick is not None:
            self.screen.type(pick, settle=0.8)
            self.screen.key("Return", settle=1.0)

    def all_commits(self) -> None:
        """View > Show All Commits (Ctrl+2)."""
        self.keys("ctrl+2", settle=1.0)

    def local_changes(self) -> None:
        """View > Show Uncommitted Changes (Ctrl+1)."""
        self.keys("ctrl+1", settle=1.5)

    def refresh(self) -> None:
        """Repository > Refresh (F5)."""
        self.keys("F5", settle=1.5)

    # -- the commit list --------------------------------------------------------------------------

    def select_commit(self, row: int) -> None:
        """Click a row of the commit list (0 = the top row)."""
        window = self.window()
        self.screen.click_in(window, COMMIT_ROW_X, COMMIT_ROW_FIRST + row * COMMIT_ROW_PITCH)

    def commit_menu(self, row: int, item: str) -> None:
        """Open a commit's context menu (Shift+F10) and click one of its items (``MENU_FROM_BOTTOM``).

        The menu's last items keep their order whatever branch labels the commit carries, so
        they are found from the bottom of the popup; a click cannot be lost like an arrow key
        pressed before the popup has the keyboard.
        """
        self.select_commit(row)
        self.selection_menu(item)

    def selection_menu(self, item: str) -> None:
        """The context menu of the selected commit: click ``item``."""
        before = self.screen.windows()
        self.screen.key("shift+F10", settle=0.2)
        menu = self.screen.new_window(before, timeout=10)
        time.sleep(0.4)
        if item != "interactive_rebase":
            self.screen.click(menu.x + MENU_TEXT_X, menu.y + menu.height - MENU_FROM_BOTTOM[item], settle=1.0)
            return
        self.screen.hover(menu.x + MENU_TEXT_X, menu.y + menu.height - MENU_FROM_BOTTOM[item], settle=1.0)
        submenu = self.screen.new_window([*before, menu], timeout=10)
        self.screen.click(submenu.x + MENU_TEXT_X, submenu.y + SUBMENU_FIRST_ITEM, settle=1.0)

    def checkout_selected(self, repo: Path) -> str:
        """Checkout Commit... on the selected commit (its context menu); the commit HEAD then is.

        This is how the tests read which commit a row (or a search result) shows: Fork's own
        Create Branch shortcut always starts at HEAD, but the context menu acts on the selection.
        """
        self.selection_menu("checkout_commit")
        self.wait_dialog("Checkout Commit")
        self.screen.key("Return", settle=1.0)
        self.wait_no_dialog()
        return wait_until(lambda: git(repo, "symbolic-ref", "-q", "HEAD", check=False) == ""
                          and git(repo, "rev-parse", "HEAD"), 60, f"a detached HEAD in {repo.name}")

    def commit_at(self, repo: Path, row: int) -> str:
        """The commit a row of the commit list shows (checked out, detached)."""
        self.select_commit(row)
        return self.checkout_selected(repo)

    # -- local changes ----------------------------------------------------------------------------

    def unstaged_row(self, index: int) -> tuple[int, int]:
        """Offset of an Unstaged list row."""
        return FILE_ROW_X, UNSTAGED_FIRST + index * FILE_ROW_PITCH

    def staged_row(self, index: int) -> tuple[int, int]:
        """Offset of a Staged list row."""
        return FILE_ROW_X, STAGED_FIRST + index * FILE_ROW_PITCH

    def click(self, offset: tuple[int, int], *, button: int = 1, repeat: int = 1) -> None:
        """Click at an offset inside the main window."""
        self.screen.click_in(self.window(), *offset, button=button, repeat=repeat)

    def askpass(self, repo: str, timeout: float = 90) -> Window:
        """Fork's own prompt (AskPass: a user name, a password, a key's passphrase) for repository ``repo``.

        It is titled with the repository's name; a Fetch or Push dialog that is still closing is not it.
        """
        pattern = f"^{re.escape(repo)}$"
        return wait_until(lambda: next(iter(self.screen.any_windows(pattern)), None), timeout,
                          f"Fork's prompt for {repo}")

    def answer_askpass(self, repo: str, answer: str, timeout: float = 90) -> None:
        """Type ``answer`` into Fork's prompt for ``repo`` and confirm it.

        The prompt does not take the keyboard by itself (no window manager): its field is clicked first.
        """
        window = self.askpass(repo, timeout)
        self.screen.click_in(window, *ASKPASS_FIELD, settle=0.8)
        self.screen.type(answer)
        self.screen.key("Return", settle=1.0)
        self.screen.wait_gone(window)

    def stage_all_by_double_click(self, repo: Path, count: int) -> None:
        """Stage ``count`` unstaged files: a double-click stages the top one, the next moves up.

        Each one is confirmed by git; a double-click that lands while Fork still redraws the list after
        the previous one only selects the row, so it is repeated (at most :data:`STAGE_ATTEMPTS` times).
        """
        for staged in range(1, count + 1):
            self._double_click_until_staged(repo, staged)

    def _double_click_until_staged(self, repo: Path, staged: int) -> None:
        for attempt in range(1, STAGE_ATTEMPTS + 1):
            self.click(self.unstaged_row(0), repeat=2)
            try:
                wait_until(lambda: len(git(repo, "diff", "--cached", "--name-only").split()) >= staged or None,
                           20, f"{staged} staged file(s)")
                return
            except DriverError:
                if attempt == STAGE_ATTEMPTS:
                    raise

    def commit_subject_and_commit(self) -> None:
        """Commit with the message Fork has filled in (a template, a merge message): Ctrl+Enter.

        Fork fills the commit box when it gets the focus, so it is clicked first.
        """
        self.click(COMMIT_SUBJECT)
        time.sleep(0.8)
        self.screen.key("ctrl+Return", settle=1.5)

    def commit(self, subject: str, description: str = "", *, amend: bool = False) -> None:
        """Type a commit message in the Local Changes view and commit (Ctrl+Enter)."""
        if amend:
            self.click(AMEND_BOX)
        self.click(COMMIT_SUBJECT)
        self.screen.key("ctrl+a")
        self.screen.type(subject)
        if description:
            self.click(COMMIT_DESCRIPTION)
            self.screen.type(description)
        self.screen.key("ctrl+Return", settle=1.5)


def wait_git(repo: Path, args: tuple[str, ...], expected: str, timeout: float = 60) -> str:
    """Wait until ``git ARGS`` in ``repo`` prints ``expected`` (Fork runs git in the background)."""
    return wait_until(lambda: git(repo, *args, check=False) == expected and expected or None, timeout,
                      f"git {' '.join(args)} == {expected!r} in {repo.name}")


def wait_ref(repo: Path, ref: str, timeout: float = 60) -> str:
    """Wait until ``ref`` exists in ``repo``; return its commit."""
    return wait_until(lambda: git(repo, "rev-parse", "--verify", "--quiet", ref, check=False), timeout,
                      f"{ref} in {repo.name}")


def new_repo(path: Path, files: dict[str, str] | None = None) -> Path:
    """A repository at ``path`` (``main``, the test identity, a ``.gitignore`` so Fork shows no banner)
    whose first commit carries the oldest date of :func:`dated_env`."""
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.name", "E2E Test")
    git(path, "config", "user.email", "e2e@example.invalid")
    for name, content in {".gitignore": "*.tmp\n", "README.md": f"# {path.name}\n", **(files or {})}.items():
        (path / name).parent.mkdir(parents=True, exist_ok=True)
        (path / name).write_text(content, encoding="utf-8")
    git(path, "add", "-A")
    git(path, "commit", "-qm", "initial", env=dated_env(0))
    return path


def dated_env(index: int) -> dict[str, str]:
    """Author and committer dates one minute apart, so Fork's date order is the commit order."""
    stamp = f"2026-01-01T10:{index // 60:02d}:{index % 60:02d}+00:00" if index < 3600 else "2026-01-02T00:00:00"
    return {"GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp}


def linear_history(repo: Path, count: int, name: str = "log.txt") -> list[str]:
    """``count`` commits on the current branch, one minute apart; their SHAs, oldest first."""
    shas = []
    for index in range(count):
        with open(repo / name, "a", encoding="utf-8") as handle:
            handle.write(f"entry {index}\n")
        git(repo, "add", "--", name)
        git(repo, "commit", "-qm", f"entry {index}", env=dated_env(index + 1))
        shas.append(git(repo, "rev-parse", "HEAD"))
    return shas
