"""Update guards with the official Fork on the main prefix (docs/qa/UPDATE-SCENARIOS.md F8, F12, F13, A20).

What a user sees when ``update`` cannot reach Fork's feed, when Fork is newer than the version it is
pinned to, when ``update --fork`` must refuse, and when their manifest override no longer fits:
the real CLI and launcher against the installed Fork, which must stay exactly as it was and keep
starting. Every test leaves Fork closed and the prefix as it found it.

Gated by ``FL_E2E_FORK=1``; containers only (``scripts/e2e-docker.sh pytest
tests/e2e/test_real_update_guards.py``, AGENTS.md hard rule 18).
"""

from __future__ import annotations

import json
import os
import signal
from collections.abc import Iterator
from typing import Any

import pytest
from fork_driver import fork_pids, fork_version, make_repo, wait_until

from fixtures import real_tier
from fork_linux import APP_NAME, locking, updates
from fork_linux.paths import Paths
from fork_linux.state import State

if os.environ.get("FL_E2E_FORK") != "1":
    pytest.skip("requires FL_E2E_FORK=1 (the official Fork under Wine)", allow_module_level=True)
real_tier.enforce()

FAST_SECONDS = 60.0
# Fork's previous release: never installed here, only named (a pin to it, a request for it).
OLDER = "2.23.1"
# Every HTTPS request goes to a proxy nobody listens on: Fork's feed is unreachable.
UNREACHABLE = {"https_proxy": "http://127.0.0.1:9", "HTTPS_PROXY": "http://127.0.0.1:9", "no_proxy": "",
               "NO_PROXY": ""}
# zenity's dialogs: the OK button's centre above the bottom edge (tests/e2e/test_real_launch.py).
DIALOG_OK_FROM_BOTTOM = 51
PINNED_VERSION = "fork.pinned_version"


def _paths(e2e: Any) -> Paths:
    return Paths.from_env(e2e.env)


def _check(e2e: Any, *args: str, extra: dict[str, str] | None = None) -> dict[str, Any]:
    return json.loads(e2e.cli("--json", "update", "--check", *args, extra=extra).stdout)


def _unchanged(e2e: Any) -> tuple[Any, ...]:
    """What an update that must change nothing could change: the installed Fork, its files, the snapshots."""
    current = e2e.fork.local_dir / "Fork" / "current"
    files = sorted((str(path.relative_to(current)), path.stat().st_size, path.stat().st_mtime_ns)
                   for path in current.rglob("*") if path.is_file())
    snapshots = json.loads(e2e.cli("--json", "snapshot", "list").stdout)
    return _check(e2e, extra=UNREACHABLE)["installed"], files, snapshots


def _starts(e2e: Any, name: str) -> None:
    """Fork still starts (and is closed again)."""
    repo = e2e.repos / name
    e2e.fork.start(repo if repo.is_dir() else make_repo(repo, {".gitignore": "*.tmp\n", "README.md": f"# {name}\n"}))
    assert fork_pids(e2e.prefix)
    e2e.fork.close()


@pytest.fixture
def no_feed_cache(closed: Any) -> Iterator[Any]:
    """Fork's cached update feed moved aside for the test, then put back."""
    feeds = _paths(closed).feeds_dir
    aside = feeds.with_name(feeds.name + ".e2e-aside")
    if feeds.exists():
        feeds.rename(aside)
    try:
        yield closed
    finally:
        if feeds.exists():
            real_tier.remove_inside(feeds, closed.root)
        if aside.exists():
            aside.rename(feeds)


def test_f8_update_without_fork_s_feed_changes_nothing(no_feed_cache: Any) -> None:
    """Offline, or with Fork's feed unreachable: ``update --check`` still reports (newest unknown, exit 0);
    ``update --fork --latest`` fails with exit 12 and changes nothing; Fork starts as before.

    Scenarios: F8
    """
    e2e = no_feed_cache
    before = _unchanged(e2e)
    offline = e2e.cli("--offline", "update", "--check")
    assert f"Installed: {fork_version()}" in offline.stdout
    assert "Newest:    unknown (no cached copy" in offline.stdout
    unreachable = e2e.cli("update", "--check", extra=UNREACHABLE)
    assert "Newest:    unknown (fetching https://" in unreachable.stdout, unreachable.stdout
    for args, extra, said in (
        (("--offline", "update", "--fork", "--latest"), None, "no cached copy"),
        (("update", "--fork", "--latest"), UNREACHABLE, "fetching https://"),
    ):
        result = e2e.cli(*args, extra=extra, check=False)
        assert result.returncode == 12, result.stdout + result.stderr
        assert said in result.stderr
    assert _unchanged(e2e) == before
    _starts(e2e, "f8")


def test_f13_update_fork_guards(closed: Any) -> None:
    """``update --fork`` refuses what it must (Fork running, the lock held, an untested or invalid version,
    version flags without ``--fork``, no Fork installed) and says the tested version is already there;
    nothing changes and nothing is downloaded.

    Scenarios: F13
    """
    e2e = closed
    before = _unchanged(e2e)
    already = e2e.cli("update", "--fork")
    assert already.stdout == f"Fork {fork_version()} is already installed\n"
    for args, said in (
        (("update", "--fork-version", OLDER), "go with --fork"),
        (("update", "--latest"), "go with --fork"),
        (("update", "--fork", "--fork-version", OLDER), "--allow-untested"),
        (("update", "--fork", "--fork-version", "2.23.x"), "2.23.x"),
    ):
        result = e2e.cli(*args, check=False)
        assert result.returncode == 2, (args, result.stdout + result.stderr)
        assert said in result.stderr, (args, result.stderr)
    with locking.FileLock(_paths(e2e).lock_file, "setup"):
        locked = e2e.cli("update", "--fork", "--fork-version", OLDER, "--allow-untested", check=False)
    assert locked.returncode == 16, locked.stdout + locked.stderr
    e2e.fork.start(make_repo(e2e.repos / "f13", {".gitignore": "*.tmp\n", "README.md": "# f13\n"}))
    running = e2e.cli("update", "--fork", "--fork-version", OLDER, "--allow-untested", check=False)
    assert running.returncode == 15, running.stdout + running.stderr
    e2e.fork.close()
    assert _unchanged(e2e) == before
    nobody = e2e.variant("no-fork")
    missing = nobody.cli("update", "--fork", check=False)
    assert missing.returncode == 10, missing.stdout + missing.stderr
    assert "not installed" in missing.stderr
    _starts(e2e, "f13")


def _close_dialog(e2e: Any, launch: Any, shot: str) -> None:
    """Wait for the launch's zenity dialog (titled like our app), keep a screenshot, press its OK button."""
    dialog = wait_until(
        lambda: next((window for window in e2e.screen.any_windows("unofficial") if window.title == APP_NAME), None),
        FAST_SECONDS, "the launch's dialog",
    )
    e2e.screen.screenshot(shot)
    assert launch.poll() is None, "the launch waits until the dialog is closed"
    e2e.screen.click_in(dialog, dialog.width // 2, -DIALOG_OK_FROM_BOTTOM)


def _fork_linux_log(e2e: Any) -> str:
    try:
        return (_paths(e2e).logs_dir / "fork-linux.log").read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return ""


def test_f12_a_pin_older_than_the_installed_fork_warns_and_never_downgrades(closed: Any) -> None:
    """Pinned to an older version than the installed Fork (Fork updated itself before the pin, or the pin
    was set by hand): a launch from the menu warns in a dialog and names ``rollback --to-version``; Fork
    starts on the installed version, which is never changed by itself, and its own update check stays off.

    Scenarios: F12
    """
    e2e = closed
    paths = _paths(e2e)
    installed = fork_version()
    channel = e2e.fork.settings().get("ApplicationUpdateType")
    e2e.cli("config", "set", "fork.update_policy", "pinned")
    state = State.load(paths.state_file)
    state.set(PINNED_VERSION, OLDER)
    state.save()
    shown = len(_fork_linux_log(e2e))
    launch = e2e.fork.launch(make_repo(e2e.repos / "f12", {".gitignore": "*.tmp\n", "README.md": "# f12\n"}))
    try:
        _close_dialog(e2e, launch, "f12-pin-warning")
        e2e.fork.wait_main()
        assert fork_pids(e2e.prefix)
        warned = _fork_linux_log(e2e)[shown:]
        assert f"Fork {installed} is installed although Fork is pinned to {OLDER}" in warned, warned
        assert f"'fork-linux rollback --to-version {OLDER}' goes back" in warned
        e2e.fork.close()
        assert _check(e2e, extra=UNREACHABLE)["installed"] == installed, "the pin never downgrades by itself"
        assert e2e.fork.settings()["ApplicationUpdateType"] == 2, "Fork's own update check is off while pinned"
    finally:
        if launch.poll() is None and not fork_pids(e2e.prefix):
            os.killpg(launch.pid, signal.SIGKILL)
            launch.wait(timeout=10)
        e2e.fork.ensure_closed()
        e2e.cli("config", "set", "fork.update_policy", "auto")
        state = State.load(paths.state_file)
        state.get("fork", {}).pop("pinned_version", None)
        state.save()
    _starts(e2e, "f12")
    back = updates.UPDATE_TYPE_DEVELOP if channel is None else channel
    assert e2e.fork.settings().get("ApplicationUpdateType") == back, "unpinned: Fork's channel is back"
    e2e.cli("config", "unset", "fork.update_policy")
    assert State.load(paths.state_file).get(PINNED_VERSION) is None


def test_a20_a_manifest_override_that_no_longer_fits_is_named(closed: Any) -> None:
    """A user manifest override this release's manifest rejects: every command exits 13 naming the
    override file (hint: fix or remove it), not the packaged manifest; Fork is not started; once the file
    is removed, Fork starts as before.

    Scenarios: A20
    """
    e2e = closed
    override = _paths(e2e).manifest_override
    override.parent.mkdir(parents=True, exist_ok=True)
    override.write_text(json.dumps({"wine": {"default": "no-such-build"}}), encoding="utf-8")
    repo = make_repo(e2e.repos / "a20", {".gitignore": "*.tmp\n", "README.md": "# a20\n"})
    try:
        for args in (("run", "--no-gui", str(repo)), ("doctor",), ("update", "--check")):
            result = e2e.cli(*args, check=False, timeout=FAST_SECONDS)
            assert result.returncode == 13, (args, result.stdout + result.stderr)
            assert f"manifest override {override} does not fit this fork-linux's runtime manifest" in result.stderr
            assert "no-such-build" in result.stderr
            assert f"hint: fix or remove {override}" in result.stderr
        assert not fork_pids(e2e.prefix)
    finally:
        override.unlink()
    _starts(e2e, "a20")
    assert not override.exists()

