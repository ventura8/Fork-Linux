"""ssh with Fork's bundled git beyond a plain key: a key protected by a passphrase, and ssh-agent.

Under the pinned Wine (Kron4ek 11.18) Fork's bundled (msys) ssh starts its askpass program: Fork asks
for a key's passphrase in its own prompt, and the fetch goes through. It still cannot reach the Linux
agent's socket, so with ssh-agent it asks for the passphrase too. ``ssh sync`` and ``doctor`` name
such keys and point at the git bridge, whose Linux ssh uses the agent (``test_real_bridge.py``:
G-SSH-PASSPHRASE, G-SSH-AGENT). Under Wine 11.0 the bundled ssh could not ask at all (the fetch
failed with "Permission denied (publickey)"); these tests pin the current behaviour.

The sshd listens on 127.0.0.1 inside the container; keys and passphrases are made for the run.
Gated by ``FL_E2E_FORK=1``; containers only.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fork_driver import git, wait_until
from fork_ui import ForkUI
from git_servers import SshServer
from remote_ops import bare_remote, clone, credential_prompt, fetch, push_theirs

from fixtures import real_tier

if os.environ.get("FL_E2E_FORK") != "1":
    pytest.skip("requires FL_E2E_FORK=1 (the official Fork under Wine)", allow_module_level=True)
real_tier.enforce()

KEY = "id_e2e_passphrase"
PASSPHRASE = "e2e bundled passphrase"
ALIAS = "e2e-passphrase"
GIT_ERROR = "Git Error"
# How long the fetch gets to show Fork's passphrase prompt.
PROMPT_SECONDS = 90.0
FETCH_SECONDS = 90.0


class _Setup:
    """A passphrase key in the tier's ~/.ssh, an ssh config alias for it and an sshd that accepts it."""

    def __init__(self, e2e: Any) -> None:
        self.e2e = e2e
        self.ssh_dir = e2e.home / ".ssh"
        self.key = self.ssh_dir / KEY
        self.config = self.ssh_dir / "config"
        self.config_before: str | None = None
        self.server = SshServer(e2e.root / "sshd-passphrase")

    def start(self) -> None:
        self.ssh_dir.mkdir(mode=0o700, exist_ok=True)
        for old in (self.key, self.key.with_name(f"{KEY}.pub")):
            real_tier.remove_inside(old, self.e2e.root)
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", PASSPHRASE, "-C", "e2e", "-f", str(self.key)],
                       check=True, timeout=60)
        authorized = self.e2e.root / "sshd-passphrase-authorized_keys"
        authorized.write_text(self.key.with_name(f"{KEY}.pub").read_text(encoding="utf-8"), encoding="utf-8")
        self.server.start(authorized)
        host_key = (self.server.base / "host_ed25519.pub").read_text(encoding="utf-8").split()
        with open(self.ssh_dir / "known_hosts", "a", encoding="utf-8") as known:
            known.write(f"[127.0.0.1]:{self.server.port} {host_key[0]} {host_key[1]}\n")
        self.config_before = self.config.read_text(encoding="utf-8") if self.config.is_file() else None
        self.config.write_text(
            (self.config_before or "")
            + f"\nHost {ALIAS}\n    HostName 127.0.0.1\n    Port {self.server.port}\n    User {self.server.user}\n"
            f"    IdentityFile ~/.ssh/{KEY}\n    IdentitiesOnly yes\n",
            encoding="utf-8",
        )

    def stop(self) -> None:
        """Stop sshd and take the key and the alias out of ~/.ssh and the prefix again."""
        self.server.stop()
        for old in (self.key, self.key.with_name(f"{KEY}.pub")):
            real_tier.remove_inside(old, self.e2e.root)
        if self.config_before is None:
            real_tier.remove_inside(self.config, self.e2e.root)
        else:
            self.config.write_text(self.config_before, encoding="utf-8")
        self.e2e.cli("ssh", "sync")


@pytest.fixture(scope="module")
def passphrase_key(installed: Any) -> Iterator[_Setup]:
    """The passphrase key shared with Fork (``ssh sync``), Fork closed before and after; removed after."""
    installed.fork.ensure_closed()
    installed.fork.forget_tabs()
    setup = _Setup(installed)
    try:
        setup.start()
        yield setup
    finally:
        installed.fork.ensure_closed()
        setup.stop()


def _work(e2e: Any, name: str) -> tuple[Path, str]:
    """A working copy whose origin is reached through the ssh alias; the commit waiting to be fetched."""
    bare, seed = bare_remote(e2e, name)
    work = clone(f"file://{bare}", e2e.repos / f"{name}-work")
    git(work, "remote", "set-url", "origin", f"{ALIAS}:{bare}")
    return work, push_theirs(seed)


def _fetch_asks_for_the_passphrase(ui: ForkUI, work: Path, theirs: str, extra: dict[str, str] | None = None) -> None:
    """Fork's Fetch over ssh asks for the key's passphrase in Fork's own prompt; with it the fetch goes through."""
    ui.fork.ensure_closed()
    ui.fork.focus(ui.fork.start(work, extra=extra))
    fetch(ui)
    prompt = wait_until(lambda: ui.screen.find(GIT_ERROR) or credential_prompt(ui, work.name), PROMPT_SECONDS,
                        "Fork's passphrase prompt (or its Git Error dialog)")
    ui.screen.screenshot(f"bundled-ssh-{work.name}")
    assert isinstance(prompt, tuple) and prompt[0] == "fork", f"no passphrase prompt from the bundled ssh: {prompt}"
    ui.answer_askpass(work.name, PASSPHRASE)
    wait_until(lambda: git(work, "rev-parse", "--verify", "--quiet", "origin/main", check=False) == theirs,
               FETCH_SECONDS, "the fetch after the passphrase")
    ui.fork.ensure_closed()


def test_a_passphrase_key_is_named_and_bundled_ssh_asks_for_it(passphrase_key: _Setup, installed: Any) -> None:
    """``ssh sync`` and ``doctor`` name a key with a passphrase and point at the git bridge for ssh-agent;
    Fork's bundled ssh asks for the passphrase in Fork's prompt and fetches.

    Scenarios: G-SSH-PASSPHRASE-BUNDLED
    """
    synced = installed.cli("ssh", "sync").stdout
    assert f"needs a passphrase: {KEY}\n" in synced, synced
    assert "git-bridge enable" in synced
    status = json.loads(installed.cli("ssh", "status", "--json").stdout)
    assert status["passphrase_keys"] == [KEY]
    report = json.loads(installed.cli("doctor", "--json", "--check", "ssh.passphrase", check=False).stdout)
    check = next(item for item in report["checks"] if item["id"] == "ssh.passphrase")
    assert check["status"] == "info", check
    assert KEY in check["detail"]
    work, theirs = _work(installed, "ssh-passphrase")
    _fetch_asks_for_the_passphrase(ForkUI(installed.fork), work, theirs)


def test_bundled_ssh_cannot_use_the_agent(passphrase_key: _Setup, installed: Any) -> None:
    """The same key loaded into the Linux ssh-agent: Fork's bundled ssh cannot reach the agent's socket, so it
    asks for the passphrase anyway.

    Scenarios: G-SSH-AGENT-BUNDLED
    """
    agent = installed.root / "bundled-agent.sock"
    real_tier.remove_inside(agent, installed.root)
    process = subprocess.Popen(["ssh-agent", "-D", "-a", str(agent)], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        wait_until(agent.exists, 15, "the ssh agent")
        askpass = installed.root / "bundled-askpass.sh"
        askpass.write_text(f"#!/bin/sh\necho '{PASSPHRASE}'\n", encoding="utf-8")
        askpass.chmod(0o755)
        subprocess.run(["ssh-add", str(passphrase_key.key)], check=True, timeout=30, stdin=subprocess.DEVNULL,
                       env={**os.environ, "SSH_AUTH_SOCK": str(agent), "SSH_ASKPASS": str(askpass),
                            "SSH_ASKPASS_REQUIRE": "force"})
        work, theirs = _work(installed, "ssh-agent-bundled")
        _fetch_asks_for_the_passphrase(ForkUI(installed.fork), work, theirs, {"SSH_AUTH_SOCK": str(agent)})
    finally:
        process.send_signal(signal.SIGTERM)
        process.wait(timeout=30)
