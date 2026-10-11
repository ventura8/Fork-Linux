"""Remotes through the native-git bridge: the user actions of ``test_real_git_remotes.py`` (Fork's
bundled git) once more with Linux git behind Fork's shims. Fetch, pull and push to a ``file://``
URL and to a bare Linux path, tags, an HTTP remote with a password (Fork's own prompt through the
bridge's askpass, the account kept by the user's Linux credential helper), and File > Clone of a
``file://`` URL and of a public HTTPS repository.

Fork runs with ``--debug``, so the shims record every git call: each test also checks that its
operations ran as bridge calls (Linux git), not through the bundled git. The ssh cases of the bridge
(agent, passphrase prompt) are in ``test_real_bridge.py``.

Needs the bridge built in the checkout (``scripts/build-bridge.sh``). Gated by ``FL_E2E_FORK=1``;
containers only.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fork_driver import bridge_calls, fork_pids, git, make_repo, wait_until
from fork_ui import ForkUI
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

REPO = Path(__file__).resolve().parents[2]
DEBUG = ("--debug",)


@pytest.fixture(scope="module")
def bridge_ui(installed: Any) -> Iterator[ForkUI]:
    """The bridge enabled for this module (Fork restarted without old tabs); disabled afterwards."""
    assert (REPO / "build-bridge" / "bridge" / "fl-shim.exe").is_file(), \
        "the bridge is not built: run scripts/build-bridge.sh before the E2E tier"
    installed.fork.ensure_closed()
    installed.fork.forget_tabs()
    installed.cli("git-bridge", "enable")
    yield ForkUI(installed.fork)
    installed.fork.ensure_closed()
    installed.cli("git-bridge", "disable")


def _show(ui: ForkUI, repo: Path) -> None:
    """Show ``repo``: a new tab of the running Fork, or a Fork started for it with ``--debug``."""
    if fork_pids(ui.fork.prefix):
        ui.open(repo)
        return
    ui.fork.focus(ui.fork.start(repo, args=DEBUG))


def _assert_bridged(e2e: Any, repo: Path, *verbs: str) -> None:
    """Every git ``verb`` Fork ran in ``repo`` went to Linux git (a bridge call), and there was one."""
    for verb in verbs:
        calls = [call for call in bridge_calls(e2e.home)
                 if verb in call.get("argv", []) and repo.name in _where(call)]
        assert calls, f"no 'git {verb}' call recorded for {repo.name}"
        assert all(call.get("mode") == "bridge" for call in calls), calls[-3:]


def _where(call: dict[str, Any]) -> str:
    """The folder a recorded call ran in and its arguments (a clone names its target there)."""
    return " ".join([str(call.get("cwd", "")), *map(str, call.get("argv", []))])


def test_fetch_pull_push_with_a_file_url(bridge_ui: ForkUI, installed: Any) -> None:
    """Fetch, Pull and Push (Ctrl+Shift+F / L / P) to a ``file:///`` remote, run by Linux git.

    Scenarios: G-FETCH-FILE, G-PULL-FILE, G-PUSH-FILE
    """
    bare, seed = bare_remote(installed, "bridge-fileurl")
    work = clone(f"file://{bare}", installed.repos / "bridge-fileurl-work")
    fetch_pull_push(bridge_ui, work, seed, bare, open_work=lambda repo: _show(bridge_ui, repo))
    _assert_bridged(installed, work, "fetch", "pull", "push")


def test_fetch_pull_push_with_a_linux_path(bridge_ui: ForkUI, installed: Any) -> None:
    """The same with a remote given as a bare Linux path (``git clone /path``), run by Linux git.

    Scenarios: G-REMOTE-LINUX-PATH
    """
    bare, seed = bare_remote(installed, "bridge-barepath")
    work = clone(str(bare), installed.repos / "bridge-barepath-work")
    assert git(work, "remote", "get-url", "origin") == str(bare)
    fetch_pull_push(bridge_ui, work, seed, bare, open_work=lambda repo: _show(bridge_ui, repo))
    _assert_bridged(installed, work, "fetch", "pull", "push")


def test_push_tags(bridge_ui: ForkUI, installed: Any) -> None:
    """A tag made in Fork reaches the remote with Push's "Push all tags", through Linux git.

    Scenarios: G-TAG-PUSH
    """
    bare, _seed = bare_remote(installed, "bridge-tags")
    work = clone(f"file://{bare}", installed.repos / "bridge-tags-work")
    _show(bridge_ui, work)
    tag_and_push_all_tags(bridge_ui, work, bare, "v3.0.0")
    _assert_bridged(installed, work, "tag", "push")


def test_fetch_and_push_over_http_with_basic_auth(bridge_ui: ForkUI, installed: Any) -> None:
    """An HTTP remote with a password: Linux git asks through Fork's own prompt (the bridge's askpass runs
    Fork.AskPass), and the user's Linux credential helper keeps the account: the pull and the push that
    follow ask nothing.

    Scenarios: G-HTTP-BASIC-AUTH
    """
    store = installed.root / "bridge-http-credentials"

    def use_a_linux_helper(work: Path) -> None:
        # The user's own helper (any Linux one: libsecret, cache, store); store keeps it in a test file.
        git(work, "config", "credential.helper", f"store --file={store}")

    asked_by = http_fetch_pull_push(bridge_ui, installed, "bridge-http", configure=use_a_linux_helper,
                                    start=lambda work: _show(bridge_ui, work))
    assert asked_by == "fork", "with the bridge, Fork's own prompt asks (Git Credential Manager is not used)"
    assert "e2e-user" in store.read_text(encoding="utf-8")
    _assert_bridged(installed, installed.repos / "bridge-http-work", "fetch", "pull", "push")


def test_clone_a_file_url(bridge_ui: ForkUI, installed: Any) -> None:
    """File > Clone (Ctrl+N) of a ``file://`` URL with Linux git: the clone lands in the Linux home.

    Scenarios: G-CLONE-FILE
    """
    bare, _seed = bare_remote(installed, "bridge-clonefile")
    _show(bridge_ui, clone(f"file://{bare}", installed.repos / "bridge-clone-anchor"))
    clone_through_the_dialog(bridge_ui, f"file://{bare}")
    target = installed.home / "bridge-clonefile"
    wait_cloned(target, remote_head(bare))
    assert git(target, "config", "--bool", "core.symlinks", check=False) != "false", \
        "Linux git made the clone, not Git for Windows (which writes core.symlinks=false)"
    _assert_bridged(installed, target, "clone")


def test_clone_a_public_https_repository(bridge_ui: ForkUI, installed: Any) -> None:
    """Clone of a public HTTPS repository (GitHub) with Linux git and the host's TLS.

    Scenarios: G-CLONE-HTTPS
    """
    target = installed.home / "Hello-World"
    if target.exists():
        # The bundled-git module cloned it already: Fork's dialog would refuse an existing folder.
        real_tier.remove_inside(target, installed.root)
    _show(bridge_ui, make_repo(installed.repos / "bridge-https-anchor", {".gitignore": "*.tmp\n"}))
    started = time.monotonic()
    clone_through_the_dialog(bridge_ui, HELLO_WORLD)
    wait_until(lambda: (target / "README").is_file(), CLONE_SECONDS, "the HTTPS clone")
    assert git(target, "remote", "get-url", "origin") == HELLO_WORLD
    assert time.monotonic() - started < CLONE_SECONDS
    _assert_bridged(installed, target, "clone")
