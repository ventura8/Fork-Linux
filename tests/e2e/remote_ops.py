"""The steps the remote scenarios share in both git modes (Fork's bundled git and the native-git
bridge): bare remotes on the Linux side, Fetch / Pull / Push through Fork's dialogs, tags, a
password asked by Git Credential Manager or by Fork's own prompt, and File > Clone.

The bare remotes live under :func:`fork_driver.remotes_dir` (``/tmp`` of the E2E container, a
top-level Linux directory the git overlay rewrites for Fork's bundled git, like ``/home`` on a
desktop); the working copies under ``<root>/repos``. The HTTP server listens on 127.0.0.1 inside
the container with test credentials generated for the run. Every effect is checked with native git.
"""

from __future__ import annotations

import base64
import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fork_driver import DriverError, Window, commit_file, git, make_repo, remotes_dir, wait_until
from fork_ui import ForkUI
from git_servers import HttpGitServer

GCM_TITLE = "Git Credential Manager"
# Inside Git Credential Manager's prompt (420x312): the user name and password fields.
GCM_USER_Y = 167
GCM_PASSWORD_Y = 208
# Inside Fork's Push dialog and Clone dialog.
PUSH_ALL_TAGS = (164, 154)
# The inside of that box (x, y, width, height) and the mean red level below which it is ticked.
PUSH_ALL_TAGS_BOX = (159, 149, 10, 9)
# A ticked box is filled blue; an empty one is grey: white in the light theme, dark grey in the dark one.
TICKED_BLUE = 0.2
BOX_CLICKS = 3
CLONE_BUTTON = (475, 218)
CLONE_SECONDS = 120.0
NETWORK_SECONDS = 90.0
HELLO_WORLD = "https://github.com/octocat/Hello-World.git"


# -- remotes and working copies (native git) ----------------------------------------------------------


def bare_remote(e2e: Any, name: str) -> tuple[Path, Path]:
    """A bare remote ``<remotes>/<name>.git`` and the repository its first commit came from (its origin)."""
    seed = make_repo(e2e.repos / f"{name}-seed", {".gitignore": "*.tmp\n", "a.txt": "a\n"})
    bare = remotes_dir() / f"{name}.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(seed), str(bare)], check=True, timeout=120)
    git(seed, "remote", "add", "origin", str(bare))
    return bare, seed


def clone(url: str, target: Path, *options: str) -> Path:
    """``git [OPTIONS] clone URL TARGET`` with the test identity."""
    subprocess.run(["git", *options, "clone", "-q", url, str(target)], check=True, timeout=120)
    git(target, "config", "user.name", "E2E Test")
    git(target, "config", "user.email", "e2e@example.invalid")
    return target


def remote_head(bare: Path, ref: str = "main") -> str:
    """The commit ``ref`` points at in a bare remote ('' when it does not exist)."""
    return subprocess.run(["git", "--git-dir", str(bare), "rev-parse", "--verify", "--quiet", ref],
                          capture_output=True, text=True, check=False).stdout.strip()


def push_theirs(seed: Path) -> str:
    """Someone else's commit, pushed to the remote; its SHA."""
    theirs = commit_file(seed, "theirs.txt", "their work\n", "their work")
    git(seed, "push", "-q", "origin", "main")
    return theirs


# -- Fork's remote dialogs ------------------------------------------------------------------------------


# How long an accepted dialog may stay before Return is pressed once more.
ACCEPT_SECONDS = 10.0


def accept(ui: ForkUI, title: str) -> Window:
    """Wait for Fork's dialog ``title`` and accept it as it is (Return), then wait until it closes.

    A Return the X server lost (the flaky-UI-spot decision: logged retries, never root-caused) leaves
    the dialog open: after :data:`ACCEPT_SECONDS` it is pressed once more, with a screenshot named
    ``<title>-return-retry`` that marks the retry.
    """
    window = ui.wait_dialog(title)
    ui.screen.key("Return", settle=1.0)
    try:
        ui.screen.wait_gone(window, timeout=ACCEPT_SECONDS)
    except DriverError:
        ui.screen.screenshot(f"{title.lower()}-return-retry")
        ui.screen.key("Return", settle=1.0)
        ui.screen.wait_gone(window, timeout=60)
    return window


def fetch(ui: ForkUI) -> None:
    """Repository > Fetch (Ctrl+Shift+F), accepted."""
    ui.keys("ctrl+shift+f", settle=1.0)
    accept(ui, "Fetch")


def pull(ui: ForkUI) -> None:
    """Repository > Pull (Ctrl+Shift+L), accepted."""
    ui.keys("ctrl+shift+l", settle=1.0)
    accept(ui, "Pull")


def push(ui: ForkUI) -> None:
    """Repository > Push (Ctrl+Shift+P), accepted."""
    ui.keys("ctrl+shift+p", settle=1.0)
    accept(ui, "Push")


def wait_fetched(work: Path, commit: str, what: str) -> None:
    """Wait until ``origin/main`` of ``work`` is ``commit``."""
    wait_until(lambda: git(work, "rev-parse", "origin/main", check=False) == commit, NETWORK_SECONDS, what)


def wait_pulled(work: Path, commit: str, what: str) -> None:
    """Wait until ``main`` of ``work`` is ``commit``."""
    wait_until(lambda: git(work, "rev-parse", "main", check=False) == commit, NETWORK_SECONDS, what)


def wait_pushed(bare: Path, commit: str, what: str) -> None:
    """Wait until ``main`` of the remote is ``commit``."""
    wait_until(lambda: remote_head(bare) == commit, NETWORK_SECONDS, what)


def fetch_pull_push(ui: ForkUI, work: Path, seed: Path, bare: Path, *, open_work: Callable[[Path], Any]) -> None:
    """Fetch and pull a commit pushed by someone else, then push one of ours, all through Fork.

    ``open_work`` shows ``work`` in Fork (as a tab of the running Fork, or a Fork started for it).
    """
    theirs = push_theirs(seed)
    open_work(work)
    fetch(ui)
    wait_fetched(work, theirs, "the fetch")
    pull(ui)
    wait_pulled(work, theirs, "the pull")
    ours = commit_file(work, "ours.txt", "our work\n", "our work")
    ui.refresh()
    push(ui)
    wait_pushed(bare, ours, "the push")


def push_all_tags_ticked(ui: ForkUI, dialog: Window) -> bool:
    """Whether Push's "Push all tags" box is ticked: a ticked box is filled blue (far more blue than red), an
    empty one grey (as much red as blue) in either theme."""
    shot = ui.screen.screenshot("push-dialog", dialog)
    return ui.screen.stat(shot, "mean.b-mean.r", PUSH_ALL_TAGS_BOX) > TICKED_BLUE


def push_with_all_tags(ui: ForkUI, ticked: bool) -> None:
    """Repository > Push with "Push all tags" set to ``ticked``.

    Fork remembers the box from the last Push it ran (a cancelled dialog keeps nothing), so its state is
    read from the screen before it is clicked.
    """
    ui.keys("ctrl+shift+p", settle=1.0)
    dialog = ui.wait_dialog("Push")
    # A click that lands while the dialog still settles is lost: the box is clicked until it shows the state.
    for _attempt in range(BOX_CLICKS):
        if push_all_tags_ticked(ui, dialog) == ticked:
            break
        ui.screen.click_in(dialog, *PUSH_ALL_TAGS, settle=1.0)
    assert push_all_tags_ticked(ui, dialog) == ticked, f"Push all tags did not become {'ticked' if ticked else 'empty'}"
    ui.screen.key("Return", settle=1.0)
    ui.wait_no_dialog(timeout=NETWORK_SECONDS)


def tag_and_push_all_tags(ui: ForkUI, work: Path, bare: Path, tag: str) -> None:
    """Repository > Create Tag (Ctrl+Shift+G) on HEAD, then Push with "Push all tags" ticked.

    A second Push (nothing new to send) unticks the box again, so the next Push of the run starts the way
    Fork had it.
    """
    ui.keys("ctrl+shift+g", settle=1.0)
    ui.wait_dialog("Create Tag")
    ui.screen.type(tag)
    ui.screen.key("Return", settle=1.0)
    ui.wait_no_dialog()
    wait_until(lambda: git(work, "tag", "--list", tag), 60, "the local tag")
    push_with_all_tags(ui, True)
    tagged = git(work, "rev-parse", f"{tag}^{{commit}}")
    wait_until(lambda: remote_head(bare, f"refs/tags/{tag}^{{commit}}") == tagged, NETWORK_SECONDS,
               "the tag on the remote")
    push_with_all_tags(ui, False)


# -- credentials ------------------------------------------------------------------------------------------


def any_window(ui: ForkUI, title: str) -> Window | None:
    """A visible window of any program titled exactly ``title`` (a regular expression)."""
    return next(iter(ui.screen.any_windows(f"^{title}$")), None)


def credential_prompt(ui: ForkUI, repo: str) -> tuple[str, Window] | None:
    """The credential prompt on screen, if any: ``("gcm", window)`` or ``("fork", window)``."""
    gcm = any_window(ui, GCM_TITLE)
    if gcm is not None:
        return "gcm", gcm
    fork = any_window(ui, re.escape(repo))
    return ("fork", fork) if fork is not None else None


def answer_credentials(ui: ForkUI, repo: str, user: str, password: str) -> str:
    """Answer the first credential prompt of an HTTP remote; return which one asked (``gcm`` or ``fork``).

    With Fork's bundled git, Git Credential Manager (the overlay makes it the helper) asks in its own
    window. Under Wine it sometimes ends without one on the first prompt after Fork starts (about 1 in 6
    fresh starts, never in a running session); git then asks through Fork's own prompt (one for the user
    name, one for the password) and still hands the account to GCM to keep. With the native-git bridge,
    Linux git asks through Fork's prompt (the bridge's askpass). Neither window takes the keyboard by
    itself (no window manager), so their fields are clicked.
    """
    kind, window = wait_until(lambda: credential_prompt(ui, repo), 120, "a credential prompt")
    ui.screen.screenshot(f"{kind}-credential-prompt-{repo}", window)
    if kind == "gcm":
        ui.screen.click_in(window, window.width // 2, GCM_USER_Y)
        ui.screen.type(user)
        ui.screen.click_in(window, window.width // 2, GCM_PASSWORD_Y)
        ui.screen.type(password)
        ui.screen.key("Return", settle=2.0)
        return kind
    for answer in (user, password):
        ui.answer_askpass(repo, answer, timeout=60)
    return kind


def http_fetch_pull_push(ui: ForkUI, e2e: Any, name: str, *, start: Callable[[Path], Any],
                         configure: Callable[[Path], None] | None = None) -> str:
    """Fetch, pull and push through Fork to an HTTP remote behind basic auth (test credentials).

    ``start`` starts Fork on the working copy; ``configure`` adjusts the working copy first (a
    credential helper). Only the first fetch may ask for the account, the pull and the push must
    not; return which prompt asked (``gcm`` or ``fork``).
    """
    bare, seed = bare_remote(e2e, name)
    with HttpGitServer(remotes_dir()) as server:
        token = base64.b64encode(f"{server.user}:{server.password}".encode()).decode()
        work = clone(server.url(bare.name), e2e.repos / f"{name}-work",
                     "-c", f"http.extraHeader=Authorization: Basic {token}")
        if configure is not None:
            configure(work)
        theirs = push_theirs(seed)
        e2e.fork.ensure_closed()
        start(work)
        fetch(ui)
        asked_by = answer_credentials(ui, work.name, server.user, server.password)
        wait_fetched(work, theirs, "the fetch over HTTP")
        pull(ui)
        wait_pulled(work, theirs, "the pull over HTTP")
        ours = commit_file(work, "ours.txt", "our work\n", "our work")
        ui.refresh()
        push(ui)
        wait_pushed(bare, ours, "the push over HTTP")
        assert credential_prompt(ui, work.name) is None, "the kept credentials must be used without asking again"
        assert any("git-receive-pack" in line and " 200 " in line for line in server.requests)
    e2e.fork.ensure_closed()
    return asked_by


# -- File > Clone -------------------------------------------------------------------------------------------


def clone_through_the_dialog(ui: ForkUI, url: str) -> None:
    """File > Clone (Ctrl+N): type the URL, keep the default folder and name, click Clone."""
    ui.keys("ctrl+n", settle=1.5)
    dialog = ui.wait_dialog("Clone")
    ui.screen.type(url, settle=1.5)
    ui.screen.screenshot("clone-dialog", dialog)
    ui.screen.click_in(dialog, *CLONE_BUTTON, settle=2.0)


def wait_cloned(target: Path, commit: str) -> None:
    """Wait until ``target`` is a clone whose HEAD is ``commit``."""
    wait_until(lambda: (target / ".git" / "HEAD").is_file(), CLONE_SECONDS, f"the clone in {target}")
    wait_until(lambda: git(target, "rev-parse", "HEAD", check=False) == commit, CLONE_SECONDS, "the cloned commit")

