"""Remotes through Fork's bundled git: fetch, pull and push to Linux paths, tags, HTTP with a
password, ssh with a key shared by ``ssh sync``, and the Clone dialog.

The bare remotes live under ``/tmp`` of the E2E container (a top-level Linux directory the git
overlay rewrites for Fork's bundled git, like ``/home`` on a desktop); the working copies under
``<root>/repos``. The HTTP and ssh servers listen on 127.0.0.1 inside the container with test
credentials. Gated by ``FL_E2E_FORK=1``; containers only.
"""

from __future__ import annotations

import os
import subprocess
import time
from typing import Any

import pytest
from fork_driver import git, make_repo, wait_until, windows_path
from fork_ui import ForkUI
from git_servers import SshServer
from remote_ops import (
    CLONE_SECONDS,
    HELLO_WORLD,
    bare_remote,
    clone,
    clone_through_the_dialog,
    fetch_pull_push,
    http_fetch_pull_push,
    remote_head,
    tag_and_push_all_tags,
    wait_cloned,
)

from fixtures import real_tier

if os.environ.get("FL_E2E_FORK") != "1":
    pytest.skip("requires FL_E2E_FORK=1 (the official Fork under Wine)", allow_module_level=True)
real_tier.enforce()


def test_fetch_pull_push_with_a_file_url(ui: ForkUI, installed: Any) -> None:
    """Fetch, Pull and Push (Ctrl+Shift+F / L / P) to a ``file:///`` remote on the Linux side.

    Scenarios: G-FETCH-FILE, G-PULL-FILE, G-PUSH-FILE
    """
    bare, seed = bare_remote(installed, "fileurl")
    work = clone(f"file://{bare}", installed.repos / "fileurl-work")
    fetch_pull_push(ui, work, seed, bare, open_work=ui.open)


def test_fetch_pull_push_with_a_linux_path(ui: ForkUI, installed: Any) -> None:
    """The same with a remote given as a bare Linux path, as ``git clone /path`` writes it.

    Scenarios: G-REMOTE-LINUX-PATH
    """
    bare, seed = bare_remote(installed, "barepath")
    work = clone(str(bare), installed.repos / "barepath-work")
    assert git(work, "remote", "get-url", "origin") == str(bare)
    fetch_pull_push(ui, work, seed, bare, open_work=ui.open)


def test_push_tags(ui: ForkUI, installed: Any) -> None:
    """Push with "Push all tags" sends a tag made in Fork to the remote.

    Scenarios: G-TAG-PUSH
    """
    bare, _seed = bare_remote(installed, "tags")
    work = clone(f"file://{bare}", installed.repos / "tags-work")
    ui.open(work)
    tag_and_push_all_tags(ui, work, bare, "v2.0.0")


def _start_with(ui: ForkUI, extra: dict[str, str]) -> Any:
    """Start Fork on a working copy with ``extra`` in its environment and show it."""
    def start(work: Any) -> None:
        ui.fork.start(work, extra=extra)
        ui.open(work)
    return start


def test_fetch_and_push_over_http_with_basic_auth(ui: ForkUI, installed: Any) -> None:
    """An HTTP remote with a password: Git Credential Manager asks once (or Fork's own prompt, when GCM cannot
    open its window) and keeps the account (DPAPI): the pull and the push that follow ask nothing.

    Scenarios: G-HTTP-BASIC-AUTH, G-HTTP-CREDENTIALS-STORED
    """
    # Git Credential Manager's trace goes to the tier's logs: it shows what GCM did (and where it stopped).
    trace = {"GCM_TRACE": windows_path(installed.logs / "gcm-trace.log")}
    asked_by = http_fetch_pull_push(ui, installed, "http", start=_start_with(ui, trace))
    print(f"credential prompt: {asked_by}")


def test_forks_own_prompt_when_gcm_cannot_ask(ui: ForkUI, installed: Any) -> None:
    """When Git Credential Manager cannot show its window, Fork's own prompt asks for the account, and GCM
    still keeps it: the pull and the push that follow ask nothing.

    Scenarios: G-HTTP-FORK-PROMPT
    """
    # GCM_INTERACTIVE=never makes GCM refuse to prompt: what happens when its window cannot open under Wine.
    never = {"GCM_INTERACTIVE": "never"}
    assert http_fetch_pull_push(ui, installed, "http-fork-prompt", start=_start_with(ui, never)) == "fork"


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
    """ssh remotes with the user's own key and config, shared with Fork's bundled ssh by ``ssh sync`` (a dry run
    afterwards has nothing to do).

    Scenarios: G-SSH-KEY, I-SSH-SYNC
    """
    bare, seed = bare_remote(installed, "ssh")
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
        # Already shared: a dry run has nothing to do.
        dry = installed.cli("ssh", "sync", "--dry-run").stdout
        assert 'nothing would change' in dry, dry
        assert 'would link' not in dry, dry
        work = clone(f"file://{bare}", installed.repos / "ssh-work")
        git(work, "remote", "set-url", "origin", f"{alias}:{bare}")
        fetch_pull_push(ui, work, seed, bare, open_work=ui.open)
    finally:
        server.stop()


def test_clone_a_file_url(ui: ForkUI, installed: Any) -> None:
    """File > Clone (Ctrl+N) clones into the Linux home by default, never into the Wine prefix.

    Scenarios: G-CLONE-FILE
    """
    bare, _seed = bare_remote(installed, "clonefile")
    ui.open(clone(f"file://{bare}", installed.repos / "clone-anchor"))
    clone_through_the_dialog(ui, f"file://{bare}")
    wait_cloned(installed.home / "clonefile", remote_head(bare))


def test_clone_a_public_https_repository(ui: ForkUI, installed: Any) -> None:
    """Clone of a public HTTPS repository (GitHub) through Fork's bundled git and its TLS.

    Scenarios: G-CLONE-HTTPS
    """
    ui.open(make_repo(installed.repos / "https-anchor", {".gitignore": "*.tmp\n"}))
    started = time.monotonic()
    clone_through_the_dialog(ui, HELLO_WORLD)
    target = installed.home / "Hello-World"
    wait_until(lambda: (target / "README").is_file(), CLONE_SECONDS, "the HTTPS clone")
    assert git(target, "remote", "get-url", "origin") == HELLO_WORLD
    assert time.monotonic() - started < CLONE_SECONDS
