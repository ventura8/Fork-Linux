"""Remotes through Fork's bundled git: fetch, pull and push to Linux paths, tags, HTTP with a
password, ssh with a key shared by ``ssh sync``, and the Clone dialog.

The bare remotes live under ``/tmp`` of the E2E container (a top-level Linux directory the git
overlay rewrites for Fork's bundled git, like ``/home`` on a desktop); the working copies under
``<root>/repos``. The HTTP and ssh servers listen on 127.0.0.1 inside the container with test
credentials. Gated by ``FL_E2E_FORK=1``; containers only.
"""

from __future__ import annotations

import base64
import os
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest
from fork_driver import git, make_repo, wait_until
from fork_ui import ForkUI
from git_servers import HttpGitServer, SshServer

from fixtures import real_tier

if os.environ.get("FL_E2E_FORK") != "1":
    pytest.skip("requires FL_E2E_FORK=1 (the official Fork under Wine)", allow_module_level=True)
real_tier.enforce()

REMOTES = Path("/tmp/fl-e2e-remotes")
GCM_TITLE = "Git Credential Manager"
# Inside Git Credential Manager's prompt (420x312): the user name and password fields.
GCM_USER_Y = 167
GCM_PASSWORD_Y = 208
# Inside Fork's Push dialog and Clone dialog.
PUSH_ALL_TAGS = (155, 158)
CLONE_BUTTON = (475, 218)
CLONE_SECONDS = 120.0


def _remote(e2e: Any, name: str) -> tuple[Path, Path]:
    """A bare remote ``REMOTES/<name>.git`` and the repository its first commit came from."""
    REMOTES.mkdir(parents=True, exist_ok=True)
    seed = make_repo(e2e.repos / f"{name}-seed", {".gitignore": "*.tmp\n", "a.txt": "a\n"})
    bare = REMOTES / f"{name}.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(seed), str(bare)], check=True, timeout=120)
    git(seed, "remote", "add", "origin", str(bare))
    return bare, seed


def _clone(url: str, target: Path, *extra: str) -> Path:
    subprocess.run(["git", *extra, "clone", "-q", url, str(target)], check=True, timeout=120)
    git(target, "config", "user.name", "E2E Test")
    git(target, "config", "user.email", "e2e@example.invalid")
    return target


def _remote_head(bare: Path, ref: str = "main") -> str:
    return subprocess.run(["git", "--git-dir", str(bare), "rev-parse", ref], capture_output=True, text=True,
                          check=False).stdout.strip()


def _new_commit(repo: Path, name: str, message: str) -> str:
    (repo / name).write_text(f"{message}\n", encoding="utf-8")
    git(repo, "add", "--", name)
    git(repo, "commit", "-qm", message)
    return git(repo, "rev-parse", "HEAD")


def _accept(ui: ForkUI, title: str) -> Any:
    window = ui.wait_dialog(title)
    ui.screen.key("Return", settle=1.0)
    return window


def _fetch_pull_push(ui: ForkUI, work: Path, seed: Path, bare: Path) -> None:
    """Fetch and pull a commit pushed by someone else, then push one of ours, all through Fork."""
    theirs = _new_commit(seed, "theirs.txt", "their work")
    git(seed, "push", "-q", "origin", "main")
    ui.open(work)
    ui.keys("ctrl+shift+f", settle=1.0)
    _accept(ui, "Fetch")
    wait_until(lambda: git(work, "rev-parse", "origin/main") == theirs, 90, "the fetch")
    ui.keys("ctrl+shift+l", settle=1.0)
    _accept(ui, "Pull")
    wait_until(lambda: git(work, "rev-parse", "main") == theirs, 90, "the pull")
    ours = _new_commit(work, "ours.txt", "our work")
    ui.refresh()
    ui.keys("ctrl+shift+p", settle=1.0)
    _accept(ui, "Push")
    wait_until(lambda: _remote_head(bare) == ours, 90, "the push")


def test_fetch_pull_push_with_a_file_url(ui: ForkUI, installed: Any) -> None:
    """Fetch, Pull and Push (Ctrl+Shift+F / L / P) to a ``file:///`` remote on the Linux side.

    Scenarios: G-FETCH-FILE, G-PULL-FILE, G-PUSH-FILE
    """
    bare, seed = _remote(installed, "fileurl")
    work = _clone(f"file://{bare}", installed.repos / "fileurl-work")
    _fetch_pull_push(ui, work, seed, bare)


def test_fetch_pull_push_with_a_linux_path(ui: ForkUI, installed: Any) -> None:
    """The same with a remote given as a bare Linux path, as ``git clone /path`` writes it.

    Scenarios: G-REMOTE-LINUX-PATH
    """
    bare, seed = _remote(installed, "barepath")
    work = _clone(str(bare), installed.repos / "barepath-work")
    assert git(work, "remote", "get-url", "origin") == str(bare)
    _fetch_pull_push(ui, work, seed, bare)


def test_push_tags(ui: ForkUI, installed: Any) -> None:
    """Push with "Push all tags" sends a tag made in Fork to the remote.

    Scenarios: G-TAG-PUSH
    """
    bare, _seed = _remote(installed, "tags")
    work = _clone(f"file://{bare}", installed.repos / "tags-work")
    ui.open(work)
    ui.keys("ctrl+shift+g", settle=1.0)
    ui.wait_dialog("Create Tag")
    ui.screen.type("v2.0.0")
    ui.screen.key("Return", settle=1.0)
    ui.wait_no_dialog()
    wait_until(lambda: git(work, "tag", "--list", "v2.0.0"), 60, "the local tag")
    ui.keys("ctrl+shift+p", settle=1.0)
    dialog = ui.wait_dialog("Push")
    ui.screen.click_in(dialog, *PUSH_ALL_TAGS)
    ui.screen.screenshot("push-all-tags", dialog)
    ui.screen.key("Return", settle=1.0)
    wait_until(lambda: _remote_head(bare, "refs/tags/v2.0.0") != "", 90, "the tag on the remote")
    # Fork remembers the box; leave it as Fork had it for the next tests.
    ui.keys("ctrl+shift+p", settle=1.0)
    dialog = ui.wait_dialog("Push")
    ui.screen.click_in(dialog, *PUSH_ALL_TAGS)
    ui.screen.key("Escape", settle=1.0)


def _answer_credentials(ui: ForkUI, user: str, password: str) -> None:
    """Git Credential Manager's own window (the overlay makes it Fork's git's helper) asks for the account.

    Its window takes no keyboard focus by itself (no window manager), so its fields are clicked.
    """
    window = wait_until(lambda: _any_window(ui, GCM_TITLE), 120, "Git Credential Manager's prompt")
    ui.screen.screenshot("gcm-prompt", window)
    ui.screen.click_in(window, window.width // 2, GCM_USER_Y)
    ui.screen.type(user)
    ui.screen.click_in(window, window.width // 2, GCM_PASSWORD_Y)
    ui.screen.type(password)
    ui.screen.key("Return", settle=2.0)


def _any_window(ui: ForkUI, title: str) -> Any:
    """A visible window of any program titled exactly ``title``."""
    return next((window for window in ui.screen.any_windows(f"^{title}$")), None)


def test_fetch_and_push_over_http_with_basic_auth(ui: ForkUI, installed: Any) -> None:
    """An HTTP remote with a password: Git Credential Manager asks once, keeps it (DPAPI), push works.

    Scenarios: G-HTTP-BASIC-AUTH, G-HTTP-CREDENTIALS-STORED
    """
    bare, seed = _remote(installed, "http")
    with HttpGitServer(REMOTES) as server:
        token = base64.b64encode(f"{server.user}:{server.password}".encode()).decode()
        work = _clone(server.url(bare.name), installed.repos / "http-work",
                      "-c", f"http.extraHeader=Authorization: Basic {token}")
        theirs = _new_commit(seed, "theirs.txt", "their work")
        git(seed, "push", "-q", "origin", "main")
        ui.open(work)
        ui.keys("ctrl+shift+f", settle=1.0)
        _accept(ui, "Fetch")
        _answer_credentials(ui, server.user, server.password)
        wait_until(lambda: git(work, "rev-parse", "origin/main") == theirs, 90, "the fetch over HTTP")
        ours = _new_commit(work, "ours.txt", "our work")
        ui.refresh()
        ui.keys("ctrl+shift+p", settle=1.0)
        _accept(ui, "Push")
        wait_until(lambda: _remote_head(bare) == ours, 90, "the push over HTTP")
        assert not _any_window(ui, GCM_TITLE), "the stored credentials must be used without asking again"
        assert any("git-receive-pack" in line and " 200 " in line for line in server.requests)


def _ssh_user_setup(e2e: Any, server: SshServer) -> str:
    """A key without passphrase and a Host alias in the tier's ~/.ssh; return the alias."""
    ssh_dir = e2e.home / ".ssh"
    ssh_dir.mkdir(mode=0o700, exist_ok=True)
    key = ssh_dir / "id_e2e"
    if not key.exists():
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "e2e", "-f", str(key)], check=True,
                       timeout=60)
    host_key = (server.base / "host_ed25519.pub").read_text(encoding="utf-8").split()
    (ssh_dir / "known_hosts").write_text(f"[127.0.0.1]:{server.port} {host_key[0]} {host_key[1]}\n",
                                         encoding="utf-8")
    (ssh_dir / "config").write_text(
        f"Host e2e-ssh\n    HostName 127.0.0.1\n    Port {server.port}\n    User {server.user}\n"
        f"    IdentityFile ~/.ssh/id_e2e\n    IdentitiesOnly yes\n", encoding="utf-8")
    return "e2e-ssh"


def test_fetch_and_push_over_ssh_with_a_shared_key(ui: ForkUI, installed: Any) -> None:
    """ssh remotes with the user's own key and config, shared with Fork's bundled ssh by ``ssh sync``.

    Scenarios: G-SSH-KEY, I-SSH-SYNC
    """
    bare, seed = _remote(installed, "ssh")
    server = SshServer(installed.root / "sshd")
    authorized = installed.root / "sshd-authorized_keys"
    try:
        server.start(authorized)
        alias = _ssh_user_setup(installed, server)
        authorized.write_text((installed.home / ".ssh" / "id_e2e.pub").read_text(encoding="utf-8"), encoding="utf-8")
        synced = installed.cli("ssh", "sync")
        assert synced.returncode == 0
        status = installed.cli("ssh", "status", "--json").stdout
        assert "id_e2e" in status
        work = _clone(f"file://{bare}", installed.repos / "ssh-work")
        git(work, "remote", "set-url", "origin", f"{alias}:{bare}")
        _fetch_pull_push(ui, work, seed, bare)
    finally:
        server.stop()


def test_clone_a_file_url(ui: ForkUI, installed: Any) -> None:
    """File > Clone (Ctrl+N) clones into the Linux home by default, never into the Wine prefix.

    Scenarios: G-CLONE-FILE
    """
    bare, _seed = _remote(installed, "clonefile")
    ui.open(_clone(f"file://{bare}", installed.repos / "clone-anchor"))
    _clone_through_the_dialog(ui, f"file://{bare}")
    target = installed.home / "clonefile"
    wait_until(lambda: (target / ".git" / "HEAD").is_file(), CLONE_SECONDS, "the clone in the Linux home")
    wait_until(lambda: git(target, "rev-parse", "HEAD", check=False) == _remote_head(bare), CLONE_SECONDS,
               "the cloned commit")


def _clone_through_the_dialog(ui: ForkUI, url: str) -> None:
    ui.keys("ctrl+n", settle=1.5)
    dialog = ui.wait_dialog("Clone")
    ui.screen.type(url, settle=1.5)
    ui.screen.screenshot("clone-dialog", dialog)
    ui.screen.click_in(dialog, *CLONE_BUTTON, settle=2.0)


def test_clone_a_public_https_repository(ui: ForkUI, installed: Any) -> None:
    """Clone of a public HTTPS repository (GitHub) through Fork's bundled git and its TLS.

    Scenarios: G-CLONE-HTTPS
    """
    ui.open(make_repo(installed.repos / "https-anchor", {".gitignore": "*.tmp\n"}))
    started = time.monotonic()
    _clone_through_the_dialog(ui, "https://github.com/octocat/Hello-World.git")
    target = installed.home / "Hello-World"
    wait_until(lambda: (target / "README").is_file(), CLONE_SECONDS, "the HTTPS clone")
    assert git(target, "remote", "get-url", "origin") == "https://github.com/octocat/Hello-World.git"
    assert time.monotonic() - started < CLONE_SECONDS
