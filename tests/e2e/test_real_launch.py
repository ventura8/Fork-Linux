"""Launch variants with the official Fork: paths, the file-manager form, drivers, logs and pending setup.

Every test leaves Fork closed. Gated by ``FL_E2E_FORK=1``; containers only.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest
from fork_driver import fork_pids, make_repo, processes, wait_until, windows_path

from fixtures import real_tier
from fork_linux import APP_NAME

if os.environ.get("FL_E2E_FORK") != "1":
    pytest.skip("requires FL_E2E_FORK=1 (the official Fork under Wine)", allow_module_level=True)
real_tier.enforce()

FAST_PATH_SECONDS = 30.0
# zenity's error dialog: its OK button's centre above the bottom edge (measured on the tier's Xvfb).
ERROR_DIALOG_OK_FROM_BOTTOM = 51
GITIGNORE = {".gitignore": "*.tmp\n"}


def _workspace_repositories(e2e: Any) -> list[str]:
    """Every repository of Fork's workspaces (Fork writes them when it closes)."""
    workspaces = e2e.fork.settings().get("Workspaces", {}).get("All", [])
    return [repo for workspace in workspaces for repo in workspace.get("Repositories", [])]


def _repo(e2e: Any, name: str) -> Path:
    return make_repo(e2e.repos / name, {**GITIGNORE, "README.md": f"# {name}\n", "docs/guide/notes.txt": "notes\n"})


def _log_dir(e2e: Any) -> Path:
    return e2e.home / ".local" / "state" / "fork-linux" / "logs"


def test_dot_in_a_subdirectory_opens_the_repository_root(closed: Any) -> None:
    """``fork .`` inside a repository's subdirectory opens the repository's root in the running Fork.

    Scenarios: L-DOT-SUBDIR
    """
    e2e = closed
    first = _repo(e2e, "launch-first")
    target = _repo(e2e, "launch-dot")
    e2e.fork.start(first)
    forwarded = e2e.fork.launch(".", argv0="fork", cwd=target / "docs" / "guide")
    assert forwarded.wait(timeout=FAST_PATH_SECONDS) == 0
    assert len(fork_pids(e2e.prefix)) == 1
    time.sleep(3)
    e2e.fork.close()
    repositories = _workspace_repositories(e2e)
    assert windows_path(target) in repositories
    assert not any(repo.startswith(windows_path(target / "docs")) for repo in repositories)


def test_file_manager_file_opens_its_repository(closed: Any) -> None:
    """``run --from-file-manager FILE`` (the file-manager action) opens the file's repository.

    Scenarios: L-FILE-MANAGER
    """
    e2e = closed
    target = _repo(e2e, "launch-file")
    e2e.fork.start(_repo(e2e, "launch-file-first"))
    forwarded = e2e.fork.launch(target / "docs" / "guide" / "notes.txt", args=("--from-file-manager",))
    assert forwarded.wait(timeout=FAST_PATH_SECONDS) == 0
    time.sleep(3)
    e2e.fork.close()
    assert windows_path(target) in _workspace_repositories(e2e)


def test_open_command_adds_a_tab(closed: Any) -> None:
    """``fork-linux open PATH`` opens a repository in the running Fork.

    Scenarios: L-OPEN
    """
    e2e = closed
    target = _repo(e2e, "launch-open")
    e2e.fork.start(_repo(e2e, "launch-open-first"))
    opened = e2e.cli("open", str(target), timeout=FAST_PATH_SECONDS)
    assert opened.returncode == 0
    assert len(fork_pids(e2e.prefix)) == 1
    time.sleep(3)
    e2e.fork.close()
    assert windows_path(target) in _workspace_repositories(e2e)


def test_missing_path_is_a_usage_error(closed: Any) -> None:
    """A path that does not exist is refused (exit 2) and no Fork starts: on stderr in a terminal, and from
    the menu or a file manager (no terminal) also in a dialog, which the launch waits for.

    Scenarios: L-BAD-PATH, L-ERROR-DIALOG, F21
    """
    e2e = closed
    missing = e2e.repos / "does-not-exist"
    result = e2e.cli("run", "--no-gui", str(missing), check=False, timeout=FAST_PATH_SECONDS)
    assert result.returncode == 2
    assert "does-not-exist" in result.stderr
    if shutil.which("zenity") is None:
        pytest.skip("zenity (the error dialog) is not installed")
    launch = e2e.fork.launch(missing)
    try:
        dialog = wait_until(
            lambda: next((window for window in e2e.screen.any_windows("unofficial") if window.title == APP_NAME), None),
            FAST_PATH_SECONDS, "the launch's error dialog",
        )
        e2e.screen.screenshot("bad-path-dialog")
        assert launch.poll() is None, "the launch waits until the dialog is closed"
        # The dialog's one button, OK, spans its width at the bottom (no window manager gives it the keyboard).
        e2e.screen.click_in(dialog, dialog.width // 2, -ERROR_DIALOG_OK_FROM_BOTTOM)
        assert launch.wait(timeout=FAST_PATH_SECONDS) == 2
    finally:
        if launch.poll() is None:
            # The launch runs in a session of its own: end it with its dialog.
            os.killpg(launch.pid, signal.SIGKILL)
            launch.wait(timeout=10)
    log = max((e2e.logs).glob("launch-*.log"), key=lambda path: path.stat().st_mtime)
    assert "does-not-exist" in log.read_text(encoding="utf-8", errors="replace")
    time.sleep(2)
    assert not fork_pids(e2e.prefix)


def test_previous_fork_log_is_kept(closed: Any) -> None:
    """Fork overwrites fork.log at every start; the launcher keeps the previous session's copy.

    Scenarios: L-FORK-LOG-HISTORY
    """
    e2e = closed
    repo = _repo(e2e, "launch-log")
    e2e.fork.start(repo)
    e2e.fork.close()
    marker = f"E2E previous session {time.time_ns()}"
    with open(e2e.fork.local_dir / "Fork" / "logs" / "fork.log", "a", encoding="utf-8") as handle:
        handle.write(marker + "\n")
    e2e.fork.start(repo)
    kept = sorted(_log_dir(e2e).glob("fork-2*.log"))
    assert kept, "no fork-<time>.log was kept"
    assert marker in kept[-1].read_text(encoding="utf-8", errors="replace")
    assert marker not in e2e.fork.fork_log()
    history = e2e.cli("logs", "--fork", "--path").stdout
    assert "fork.log" in history


def test_debug_launch_writes_a_wine_log(closed: Any) -> None:
    """``run --debug`` sends Wine's errors and fixmes to a new wine-<time>.log.

    Scenarios: L-DEBUG-LOGS
    """
    e2e = closed
    before = set(_log_dir(e2e).glob("wine-2*.log"))
    e2e.fork.start(_repo(e2e, "launch-debug"), args=("--debug", "--wine-debug", "err+all,fixme-all,+loaddll"))
    created = sorted(set(_log_dir(e2e).glob("wine-2*.log")) - before)
    assert created, "--debug wrote no wine-<time>.log"
    wait_until(lambda: created[-1].stat().st_size > 0, 30, "Wine debug output")
    pid = fork_pids(e2e.prefix)[0]
    environ = Path("/proc", str(pid), "environ").read_bytes().split(b"\0")
    assert b"WINEDEBUG=err+all,fixme-all,+loaddll" in environ
    assert "loaddll" in created[-1].read_text(encoding="utf-8", errors="replace")
    assert "wine" in e2e.cli("logs", "--wine", "--path").stdout


def test_x11_driver(closed: Any) -> None:
    """``--x11`` keeps Wine on the X display even when the session also offers Wayland.

    Scenarios: L-DRIVER-X11
    """
    e2e = closed
    window = e2e.fork.start(_repo(e2e, "launch-x11"), args=("--x11",),
                            extra={"WAYLAND_DISPLAY": "wayland-e2e-absent", "XDG_SESSION_TYPE": "wayland"})
    assert window.is_main
    pid = fork_pids(e2e.prefix)[0]
    environ = Path("/proc", str(pid), "environ").read_bytes().split(b"\0")
    assert f"DISPLAY={e2e.display}".encode() in environ


def test_wayland_driver(closed: Any) -> None:
    """``--wayland`` runs Fork with Wine's Wayland driver on a (headless) Wayland compositor.

    Scenarios: L-DRIVER-WAYLAND
    """
    e2e = closed
    if shutil.which("weston") is None:
        pytest.skip("weston (a Wayland compositor) is not installed")
    socket = "wayland-e2e"
    runtime = Path(e2e.env["XDG_RUNTIME_DIR"])
    with open(e2e.logs / "weston.log", "wb") as log:
        weston = subprocess.Popen(["weston", "--backend=headless", f"--socket={socket}", "--idle-time=0"],
                                  env=e2e.env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    try:
        wait_until(lambda: (runtime / socket).exists(), 30, "the Wayland socket")
        started = e2e.fork.launch(_repo(e2e, "launch-wayland"), args=("--wayland",),
                                  extra={"WAYLAND_DISPLAY": socket, "XDG_SESSION_TYPE": "wayland"})
        pids = wait_until(lambda: fork_pids(e2e.prefix), 120, "Fork.exe under the Wayland driver")
        environ = Path("/proc", str(pids[0]), "environ").read_bytes().split(b"\0")
        assert f"WAYLAND_DISPLAY={socket}".encode() in environ
        assert not any(item.startswith(b"DISPLAY=") for item in environ)
        wait_until(lambda: "Start IPC server" in e2e.fork.fork_log(), 120, "Fork to finish starting")
        assert not e2e.fork.main_window(), "Fork must not open an X11 window with --wayland"
        assert started.poll() in (None, 0)
    finally:
        e2e.fork.kill()
        weston.terminate()
        weston.wait(timeout=30)


def test_restart_replaces_a_fork_without_a_window(closed: Any) -> None:
    """A Fork that lost its window keeps the fast path busy (``run`` shows nothing); ``run --restart`` ends it
    and starts Fork again.

    Scenarios: L-RESTART
    """
    e2e = closed
    repo = _repo(e2e, "launch-restart")
    window = e2e.fork.start(repo)
    stuck = fork_pids(e2e.prefix)
    e2e.screen.xdotool("windowclose", window.id, check=True)
    wait_until(lambda: e2e.fork.main_window() is None, 30, "Fork's window to be gone")
    time.sleep(3)
    assert fork_pids(e2e.prefix) == stuck, "Fork.exe ended with its window"
    forwarded = e2e.fork.launch(repo)
    assert forwarded.wait(timeout=FAST_PATH_SECONDS) == 0
    time.sleep(5)
    assert e2e.fork.main_window() is None, "the fast path showed a window after all"
    e2e.fork.launch(repo, args=("--restart",))
    assert e2e.fork.wait_main().is_main
    restarted = fork_pids(e2e.prefix)
    assert restarted and not set(restarted) & set(stuck)


def test_no_setup_refuses_pending_steps(installed: Any) -> None:
    """``run --no-setup`` with an incomplete setup exits 10 instead of setting up (in a terminal: no
    dialog, see test_missing_path_is_a_usage_error for a desktop launch's).

    Scenarios: L-NO-SETUP
    """
    fresh = installed.variant("no-setup")
    result = fresh.cli("run", "--no-gui", "--no-setup", check=False, timeout=60)
    assert result.returncode == 10, result.stderr
    assert not (fresh.home / ".local" / "share" / "fork-linux" / "runtimes").exists()


def test_no_hooks_skips_the_pre_launch_hooks(closed: Any) -> None:
    """``run --no-hooks`` starts Fork without the snapshot, settings and ssh/git sync hooks.

    Scenarios: L-NO-HOOKS
    """
    e2e = closed
    e2e.cli("settings", "set", "DisableHardwareAcceleration", "false")
    snapshots = json.loads(e2e.cli("snapshot", "list", "--json").stdout)
    e2e.fork.start(_repo(e2e, "launch-nohooks"), args=("--no-hooks",))
    assert e2e.fork.settings()["DisableHardwareAcceleration"] is False
    e2e.fork.close()
    assert json.loads(e2e.cli("snapshot", "list", "--json").stdout) == snapshots
    e2e.fork.start(_repo(e2e, "launch-hooks"))
    assert e2e.fork.settings()["DisableHardwareAcceleration"] is True


def test_pending_setup_runs_behind_a_progress_dialog(closed: Any) -> None:
    """A launch with a pending setup step shows a graphical progress dialog, then starts Fork.

    Scenarios: L-SETUP-INCOMPLETE-GUI
    """
    e2e = closed
    if shutil.which("zenity") is None:
        pytest.skip("zenity (the progress dialog) is not installed")
    # A changed display setting makes its setup step pending again, like a config change by the user.
    e2e.cli("config", "set", "display.dpi", "120")
    try:
        assert not e2e.setup_complete()
        e2e.fork.launch(_repo(e2e, "launch-gui"), args=("--gui",))
        dialog = wait_until(lambda: [p for p in processes(lambda cmd: "zenity" in cmd and "--progress" in cmd)],
                            60, "zenity's progress dialog")
        assert dialog
        e2e.screen.screenshot("launch-setup-progress")
        e2e.fork.wait_main(timeout=180)
        assert e2e.setup_complete()
    finally:
        e2e.fork.ensure_closed()
        e2e.cli("config", "unset", "display.dpi")
        e2e.cli(*e2e.setup_args())
