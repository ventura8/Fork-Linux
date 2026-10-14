"""Tests for fork_linux.bridge: checks, the daemon start (with a fake daemon), the environment and the session."""

from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fixtures import bridge_kit
from fixtures.setup_ctx import USER, FakeUI, make_ctx

from fork_linux import bootstrap, bridge, resources, sandbox
from fork_linux.cli import AppContext
from fork_linux.config import Config
from fork_linux.errors import ForkLinuxError, UsageError
from fork_linux.steps import integration

TOKEN = "ab" * 32


@pytest.fixture
def kit(xdg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """The fake bridge build; every fake daemon is killed afterwards."""
    made = bridge_kit.install(tmp_path, monkeypatch)
    yield made
    bridge_kit.kill_daemons(made)


def _ctx(kit: bridge_kit.Kit, version: str = "2.53.0", **flags: Any) -> bootstrap.Ctx:
    return make_ctx(bridge_kit.git_runner(kit, version), **flags)


def _ready(kit: bridge_kit.Kit, version: str = "2.53.0") -> bootstrap.Ctx:
    """A context with the bridge on and the shims installed in the prefix."""
    ctx = _ctx(kit, version)
    integration.run_shims(ctx)
    ctx.config.set("git", "bridge", "on")
    return ctx


def _app() -> AppContext:
    args = argparse.Namespace(prefix=None, gui=False, offline=False, verbose=0, quiet=0, allow_root=False)
    return AppContext(args, env=dict(os.environ))


# -- what is there ----------------------------------------------------------------------------


def test_status_enable_disable_when_built(kit: bridge_kit.Kit) -> None:
    ctx = _ctx(kit)
    info = bridge.status(ctx)
    assert info == {
        "enabled": False,
        "available": True,
        "ready": False,
        "mode": "bridge",
        "reason": "",
        "shims_dir": str(kit.shims),
        "helper": str(kit.helper),
        "git_version": None,
        "git_recommended": True,
        "daemon": {},
    }
    assert bridge.enable(ctx) == []
    assert ctx.config.getbool("git", "bridge") is True
    info = bridge.status(ctx)
    assert info["enabled"]
    assert not info["ready"]
    assert info["git_version"] == "2.53.0"
    assert "not installed in the prefix" in info["reason"]
    assert "gitInstance" in info["reason"]
    assert info["reason"].endswith(" is missing and 8 more)")
    integration.run_shims(ctx)
    info = bridge.status(ctx)
    assert info["ready"]
    assert info["reason"] == ""
    (ctx.paths.fork_linux_win_dir / "bin" / bridge.LAUNCH).unlink()
    assert (
        bridge.status(ctx)["reason"]
        == f"the shims are not installed in the prefix ({Path('bin/fl-launch.exe')} is missing)"
    )
    integration.run_shims(ctx)
    bridge.disable(ctx)
    assert bridge.status(ctx)["enabled"] is False


def test_enable_refused_when_not_built(xdg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(resources.LIBEXEC_ENV, str(tmp_path / "nowhere"))
    monkeypatch.setattr(resources, "shims_dir", lambda: None)
    ctx = make_ctx()
    info = bridge.status(ctx)
    assert info["available"] is False
    assert info["shims_dir"] is None
    for name in (*bridge.SHIM_EXES, bridge.HELPER):
        assert name in info["reason"]
    with pytest.raises(UsageError, match="experimental") as excinfo:
        bridge.enable(ctx)
    assert "build-bridge.sh" in excinfo.value.hint
    assert not ctx.paths.config_file.exists()
    assert bridge.shim_sources() == {}
    assert bridge.shim_targets(ctx.paths) == []


def test_enable_checks_the_host_git(kit: bridge_kit.Kit) -> None:
    ctx = _ctx(kit, "2.34.1")
    with pytest.raises(UsageError, match="2.34.1 is older than 2.40") as excinfo:
        bridge.enable(ctx)
    assert "rebase --update-refs" in excinfo.value.hint
    assert ctx.config.getbool("git", "bridge") is False
    ctx = _ctx(kit, "2.43.0")
    warnings = bridge.enable(ctx)
    assert len(warnings) == 1
    assert "2.43.0 is older than 2.50" in warnings[0]
    assert bridge.status(ctx)["git_recommended"] is False
    ctx = _ctx(kit)
    ctx.runner.which_map["git"] = None
    with pytest.raises(UsageError, match="git is not installed"):
        bridge.enable(ctx)


def test_partial_build_lists_what_is_missing(kit: bridge_kit.Kit) -> None:
    (kit.shims / bridge.LAUNCH).unlink()
    info = bridge.status(_ctx(kit))
    assert info["available"] is False
    assert "fl-launch.exe" in info["reason"]
    assert "fl-shim.exe" not in info["reason"]


def test_invalid_setting_counts_as_off(kit: bridge_kit.Kit, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FORK_LINUX_GIT_BRIDGE", "maybe")
    info = bridge.status(_ctx(kit, env=None))
    assert info["enabled"] is False
    assert info["available"] is True
    assert "maybe" in info["reason"]


def test_mode_and_set_mode(kit: bridge_kit.Kit) -> None:
    ctx = _ctx(kit)
    assert bridge.mode(ctx) == bridge.MODE_BRIDGE
    bridge.set_mode(ctx, True)
    assert bridge.mode(ctx) == bridge.MODE_RECORD
    bridge.set_mode(ctx, False)
    assert ctx.config.get("git", "bridge_mode") == "bridge"


def test_shim_layout_problems(kit: bridge_kit.Kit) -> None:
    ctx = _ctx(kit)
    targets = bridge.shim_targets(ctx.paths)
    assert len(targets) == len(bridge.SHIM_LAYOUT)
    assert (ctx.paths.fork_linux_win_dir / "gitInstance/bin/git.exe", kit.shims / bridge.SHIM) in targets
    assert len(bridge.shims_problems(ctx.paths)) == len(bridge.SHIM_LAYOUT)
    integration.run_shims(ctx)
    assert bridge.shims_problems(ctx.paths) == []
    sh = ctx.paths.fork_linux_win_dir / "gitInstance/usr/bin/sh.exe"
    sh.write_bytes(b"MZ other")
    assert bridge.shims_problems(ctx.paths) == [
        f"{Path('gitInstance/usr/bin/sh.exe')} differs from the built fl-shim.exe"
    ]
    sh.unlink()
    sh.symlink_to(kit.shims / bridge.SHIM)
    assert bridge.shims_problems(ctx.paths) == [f"{Path('gitInstance/usr/bin/sh.exe')} is missing"]


def test_check_only_probes_git_when_enabled(kit: bridge_kit.Kit) -> None:
    ctx = _ctx(kit, "1.0")
    readiness = bridge.check(ctx)
    assert readiness.git is None
    assert not readiness.ready
    assert ctx.runner.calls == []
    ctx.config.set("git", "bridge", "on")
    integration.run_shims(ctx)
    readiness = bridge.check(ctx)
    assert not readiness.ready
    assert readiness.problems == ["the Linux git 1.0.0 is older than 2.40"]


def test_host_actions_active(kit: bridge_kit.Kit) -> None:
    ctx = _ready(kit)
    assert bridge.host_actions_active(ctx) is True
    ctx.cache[bridge.CACHE_KEY] = None
    assert bridge.host_actions_active(ctx) is False
    ctx.cache[bridge.CACHE_KEY] = bridge.Daemon(1, 2, {})
    assert bridge.host_actions_active(ctx) is True
    app = _app()
    assert bridge.host_actions_active(app) is bridge.check(app).ready


# -- personas, host helper, token ---------------------------------------------------------------


def test_personas_shipped_or_private(kit: bridge_kit.Kit) -> None:
    ctx = _ctx(kit)
    found = bridge.ensure_personas(ctx.paths, kit.helper)
    private = bridge.personas_dir(ctx.paths)
    assert found == {name: private / name for name in bridge.PERSONAS}
    for path in found.values():
        assert os.readlink(path) == os.path.realpath(kit.helper)
    assert stat.S_IMODE(private.stat().st_mode) == 0o700
    # A stale link is replaced; a correct one is kept.
    stale = private / "fl-askpass"
    stale.unlink()
    stale.symlink_to("/elsewhere")
    private.chmod(0o755)
    bridge.ensure_personas(ctx.paths, kit.helper)
    assert os.readlink(stale) == os.path.realpath(kit.helper)
    assert stat.S_IMODE(private.stat().st_mode) == 0o700
    # An installation that ships its own links: they are used as they are.
    for name in bridge.PERSONAS:
        (kit.libexec / name).symlink_to(bridge.HELPER)
    assert bridge.ensure_personas(ctx.paths, kit.helper) == {name: kit.libexec / name for name in bridge.PERSONAS}


def test_private_dir_refuses_foreign_or_linked(
    kit: bridge_kit.Kit, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    with pytest.raises(bridge._StartError, match="not a directory owned by you"):
        bridge._private_dir(link)
    monkeypatch.setattr(os, "getuid", lambda: 12345)
    with pytest.raises(bridge._StartError):
        bridge._private_dir(real)


def test_host_helper_installed_template_or_missing(kit: bridge_kit.Kit, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _ctx(kit)
    assert bridge.host_helper(ctx.paths) == kit.libexec / resources.HOST_HELPER
    (kit.libexec / resources.HOST_HELPER).unlink()
    assert bridge.host_helper(ctx.paths) is None
    template = kit.libexec / (resources.HOST_HELPER + ".in")
    template.write_text("#!@PYTHON@ -I\n", encoding="utf-8")
    wrapper = bridge.host_helper(ctx.paths)
    assert wrapper == bridge.personas_dir(ctx.paths) / resources.HOST_HELPER
    text = wrapper.read_text(encoding="utf-8")
    assert text.startswith("#!/bin/sh\nexec ")
    assert str(template) in text
    assert " -I " in text
    assert stat.S_IMODE(wrapper.stat().st_mode) == 0o700
    mtime = wrapper.stat().st_mtime_ns
    time.sleep(0.01)
    assert bridge.host_helper(ctx.paths) == wrapper
    assert wrapper.stat().st_mtime_ns == mtime, "an up-to-date wrapper is not rewritten"


def test_handoff_missing_only_looks(kit: bridge_kit.Kit) -> None:
    ctx = _ctx(kit)
    assert bridge.handoff_missing() == []
    host = kit.libexec / resources.HOST_HELPER
    host.chmod(0o644)
    assert bridge.handoff_missing() == [resources.HOST_HELPER], "not executable"
    host.unlink()
    (kit.libexec / (resources.HOST_HELPER + ".in")).write_text("#!@PYTHON@ -I\n", encoding="utf-8")
    assert bridge.handoff_missing() == [], "a source checkout's template"
    assert not bridge.personas_dir(ctx.paths).exists(), "no wrapper is written"
    (kit.libexec / (resources.HOST_HELPER + ".in")).unlink()
    kit.helper.unlink()
    assert bridge.handoff_missing() == [bridge.HELPER, resources.HOST_HELPER]


def test_write_token(kit: bridge_kit.Kit) -> None:
    ctx = _ctx(kit)
    path, token = bridge.write_token(ctx.paths)
    other, token2 = bridge.write_token(ctx.paths)
    assert path != other
    assert token != token2
    assert path.parent == ctx.paths.runtime_dir
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert path.read_text(encoding="ascii") == token + "\n"
    assert bridge._TOKEN_RE.match(token)


def test_daemon_env_is_unix_clean() -> None:
    wine_env = {
        "PATH": "/wine/bin:/usr/bin",
        "HOME": "/home/u",
        "WINEPREFIX": "/p",
        "WINEHOME": "C:\\users\\u",
        "WINEARCH": "win64",
        "FL_BRIDGE_TOKEN": "secret",
        "FL_BRIDGE_PORT": "1",
        "GIT_DIR": "/x/.git",
        "GIT_CONFIG_KEY_0": "core.filemode",
        "GIT_AUTHOR_NAME": "Me",
    }
    env = bridge.daemon_env(wine_env, {"PATH": "/usr/bin"})
    assert env == {"PATH": "/usr/bin", "HOME": "/home/u", "WINEPREFIX": "/p", "GIT_AUTHOR_NAME": "Me"}
    assert bridge.daemon_env({}, {})["PATH"] == os.defpath


def test_new_log_rotates(kit: bridge_kit.Kit, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _ctx(kit)
    stamps = iter(f"2026010{n}T000000Z" if n < 10 else f"202601{n}T000000Z" for n in range(1, 13))
    monkeypatch.setattr(bridge, "_utc_stamp", lambda: next(stamps))
    for _ in range(12):
        bridge._new_log(ctx.paths, bridge.LOG_PREFIX, ".log").write_text("x", encoding="utf-8")
    kept = sorted(p.name for p in ctx.paths.logs_dir.iterdir())
    assert len(kept) == bridge.LOGS_KEEP
    assert kept[-1] == "bridge-20260112T000000Z.log"


def test_bundled_git_win(kit: bridge_kit.Kit) -> None:
    ctx = _ctx(kit)
    assert bridge.bundled_git_win(ctx.paths, USER) is None
    root = ctx.paths.fork_local_dir(USER) / "gitInstance"
    for version in ("2.9.0", "2.50.1", "2.60.0", "notaversion"):
        (root / version / "cmd").mkdir(parents=True)
    assert bridge.bundled_git_win(ctx.paths, USER) is None
    (root / "2.9.0/cmd/git.exe").write_bytes(b"MZ")
    (root / "2.50.1/cmd/git.exe").write_bytes(b"MZ")
    assert bridge.bundled_git_win(ctx.paths, USER) == (
        f"C:\\users\\{USER}\\AppData\\Local\\Fork\\gitInstance\\2.50.1\\cmd\\git.exe"
    )


# -- the daemon -----------------------------------------------------------------------------------


def test_start_daemon_off_or_not_ready(kit: bridge_kit.Kit) -> None:
    ctx = _ctx(kit)
    assert bridge.start_daemon(ctx) is None
    assert ctx.cache[bridge.CACHE_KEY] is None
    assert bridge.host_actions_active(ctx) is False
    ctx.config.set("git", "bridge", "on")
    assert bridge.start_daemon(ctx) is None
    assert isinstance(ctx.ui, FakeUI)
    assert "not installed in the prefix" in ctx.ui.kinds("warn")[0]
    assert kit.starts() == []


def test_start_daemon_success(kit: bridge_kit.Kit, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _ready(kit)
    ctx.env["GIT_DIR"] = "/somewhere/.git"
    ctx.env["FL_BRIDGE_TOKEN"] = "leaked"
    daemon = bridge.start_daemon(ctx)
    assert daemon is not None
    assert daemon.port == 4242
    assert ctx.cache[bridge.CACHE_KEY] is daemon
    assert bridge.host_actions_active(ctx)
    (start,) = kit.starts()
    argv = start["argv"]
    assert isinstance(argv, list)
    assert argv[0] == "--daemon"
    assert argv[argv.index("--parent-pid") + 1] == str(os.getpid())
    assert argv[argv.index("--watch-prefix") + 1] == str(ctx.paths.prefix)
    assert argv[argv.index("--watch-exe-dir") + 1] == f"C:\\users\\{ctx.user}\\AppData\\Local\\Fork\\"
    assert argv[argv.index("--host-helper") + 1] == str(kit.libexec / resources.HOST_HELPER)
    assert argv[argv.index("--log") + 1] == str(daemon.log_file)
    token_file = Path(argv[argv.index("--token-file") + 1])
    assert token_file.parent == ctx.paths.runtime_dir
    assert not token_file.exists()
    assert start["token_mode"] == 0o600
    assert start["token_regular"] is True
    env = start["env"]
    assert isinstance(env, dict)
    assert env["FL_BRIDGE_TOKEN"] is None
    assert env["GIT_DIR"] is None
    assert env["WINEHOME"] is None
    assert env["WINEPREFIX"] == str(ctx.paths.prefix)
    assert env["FL_WINE"] == str(ctx.wine().wine)
    assert env["PATH"] == ctx.env["PATH"]
    personas = bridge.personas_dir(ctx.paths)
    assert daemon.env == {
        "FORKGITINSTANCE": "C:\\fork-linux\\gitInstance",
        "FL_BRIDGE_PORT": "4242",
        "FL_BRIDGE_TOKEN": start["token"],
        "FL_WINE": str(ctx.wine().wine),
        "WINEPREFIX": str(ctx.paths.prefix),
        "FL_BRIDGE_WINEXEC": str(personas / "fl-winexec"),
        "FL_BRIDGE_ASKPASS": str(personas / "fl-askpass"),
        "FL_BRIDGE_SSH_ASKPASS": str(personas / "fl-ssh-askpass"),
    }
    assert bridge.launch_env(ctx, daemon) == daemon.env
    assert bridge.launch_env(ctx, None) == {}
    assert bridge.native_git(daemon.env)
    assert not bridge.native_git({})
    assert daemon.log_file is not None
    assert stat.S_IMODE(daemon.log_file.stat().st_mode) == 0o600
    # The daemon follows its parent: still alive now, stopped on request.
    assert daemon.proc is not None
    assert daemon.proc.poll() is None
    proc = daemon.proc
    daemon.stop()
    assert proc.returncode is not None
    assert daemon.proc is None
    daemon.stop()
    ctx.config.set("git", "bridge", "off")
    assert bridge.launch_env(ctx, daemon) == {}


def test_start_daemon_debug_record_and_no_host_helper(kit: bridge_kit.Kit) -> None:
    (kit.libexec / resources.HOST_HELPER).unlink()
    ctx = _ready(kit)
    bridge.set_mode(ctx, True)
    daemon = bridge.start_daemon(ctx, debug=True)
    assert daemon is not None
    argv = kit.starts()[0]["argv"]
    assert isinstance(argv, list)
    assert "--host-helper" not in argv
    assert daemon.env[bridge.LOG_ENV].endswith(".jsonl")
    assert "/bridge-calls-" in daemon.env[bridge.LOG_ENV]
    assert bridge.MODE_ENV not in daemon.env
    assert isinstance(ctx.ui, FakeUI)
    assert "record mode needs Fork's bundled git" in ctx.ui.kinds("warn")[0]
    daemon.stop()
    git_exe = ctx.paths.fork_local_dir(USER) / "gitInstance/2.50.1/cmd/git.exe"
    git_exe.parent.mkdir(parents=True)
    git_exe.write_bytes(b"MZ")
    daemon = bridge.start_daemon(ctx)
    assert daemon is not None
    assert daemon.env[bridge.MODE_ENV] == "record"
    assert daemon.env[bridge.BUNDLED_GIT_ENV].endswith("\\gitInstance\\2.50.1\\cmd\\git.exe")
    assert not bridge.native_git(daemon.env)
    daemon.stop()


@pytest.mark.parametrize(
    ("mode", "message"),
    [
        ("fail", "exited (1) before reporting its port"),
        ("bad", "invalid port line: b'garbage\\n'"),
        ("port0", "invalid port line"),
        ("noeol", "invalid port line"),
        ("silent", "did not report its port within 0.3 s"),
    ],
)
def test_start_daemon_failures_fall_back_to_bundled_git(
    kit: bridge_kit.Kit, monkeypatch: pytest.MonkeyPatch, mode: str, message: str
) -> None:
    monkeypatch.setattr(bridge, "START_TIMEOUT", 0.3)
    ctx = _ready(kit)
    ctx.env["FL_FAKE_DAEMON"] = mode
    assert bridge.start_daemon(ctx) is None
    assert ctx.cache[bridge.CACHE_KEY] is None
    assert isinstance(ctx.ui, FakeUI)
    (warning,) = ctx.ui.kinds("warn")
    assert message in warning
    assert "Fork uses its bundled git" in warning
    assert list(ctx.paths.runtime_dir.glob("bridge-token-*")) == []
    (start,) = kit.starts()
    pid = start["pid"]
    assert isinstance(pid, int)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_start_daemon_spawn_error_removes_the_token(kit: bridge_kit.Kit) -> None:
    kit.helper.chmod(0o644)
    ctx = _ready(kit)
    assert bridge.start_daemon(ctx) is None
    assert isinstance(ctx.ui, FakeUI)
    assert "Permission denied" in ctx.ui.kinds("warn")[0]
    assert list(ctx.paths.runtime_dir.glob("bridge-token-*")) == []


def test_stop_kills_a_stubborn_daemon(kit: bridge_kit.Kit, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bridge, "STOP_TIMEOUT", 0.3)
    ctx = _ready(kit)
    ctx.env["FL_FAKE_DAEMON"] = "stubborn"
    daemon = bridge.start_daemon(ctx)
    assert daemon is not None
    assert daemon.proc is not None
    proc = daemon.proc
    daemon.stop()
    assert proc.returncode == -9


def test_daemon_that_exits_after_reporting(kit: bridge_kit.Kit) -> None:
    ctx = _ready(kit)
    ctx.env["FL_FAKE_DAEMON"] = "exit"
    daemon = bridge.start_daemon(ctx)
    assert daemon is not None
    assert daemon.proc is not None
    daemon.proc.wait(timeout=5)
    daemon.stop()
    assert daemon.proc is None


def test_daemon_ends_with_its_parent(kit: bridge_kit.Kit, tmp_path: Path) -> None:
    """A launcher-like parent starts the daemon and exits: the daemon follows (--parent-pid)."""
    ctx = _ready(kit)
    script = tmp_path / "parent.py"
    script.write_text(
        "import os, subprocess, sys\n"
        "proc = subprocess.Popen([sys.argv[1], '--daemon', '--parent-pid', str(os.getpid())],"
        " stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)\n"
        "print(proc.stdout.readline().decode().strip(), proc.pid, flush=True)\n",
        encoding="utf-8",
    )
    out = subprocess.run(
        [sys.executable, str(script), str(kit.helper)], capture_output=True, text=True, check=True, env=ctx.env
    ).stdout.split()
    assert out[0] == "FL_BRIDGE_PORT=4242"
    pid = int(out[1])
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and bridge.daemon_alive(pid):
        time.sleep(0.05)
    assert not bridge.daemon_alive(pid)


# -- the hand-off daemon ----------------------------------------------------------------------------


def _systemd_socket(runtime: Path) -> Path:
    """A systemd user manager's private socket under ``runtime`` (a real AF_UNIX socket file)."""
    path = runtime / "systemd" / "private"
    path.parent.mkdir(parents=True, exist_ok=True)
    server = socket.socket(socket.AF_UNIX)
    server.bind(str(path))
    server.close()
    return path


def test_systemd_user_manager(tmp_path: Path) -> None:
    runtime = tmp_path / "run"
    runtime.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    systemd_run = bin_dir / "systemd-run"
    systemd_run.write_text("#!/bin/sh\n", encoding="utf-8")
    systemd_run.chmod(0o755)
    env = {"XDG_RUNTIME_DIR": str(runtime), "PATH": str(bin_dir)}
    assert not bridge.systemd_user_manager(env), "no manager socket"
    (runtime / "systemd").mkdir()
    (runtime / "systemd" / "private").write_text("", encoding="utf-8")
    assert not bridge.systemd_user_manager(env), "not a socket"
    (runtime / "systemd" / "private").unlink()
    _systemd_socket(runtime)
    assert bridge.systemd_user_manager(env)
    assert not bridge.systemd_user_manager({**env, "PATH": str(tmp_path)}), "no systemd-run"
    assert not bridge.systemd_user_manager({"PATH": str(bin_dir)}), "no XDG_RUNTIME_DIR"


def test_handoff_route(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sandbox, "FLATPAK_INFO", tmp_path / "no-flatpak-info")
    assert bridge.handoff_route({"FLATPAK_ID": "io.github.ventura8.ForkLinux"}) == "flatpak"
    monkeypatch.setattr(bridge, "systemd_user_manager", lambda env: True)
    assert bridge.handoff_route({}) == "systemd"
    monkeypatch.setattr(bridge, "systemd_user_manager", lambda env: False)
    assert bridge.handoff_route({}) == ""


def test_start_handoff(kit: bridge_kit.Kit, caplog: pytest.LogCaptureFixture) -> None:
    """Bridge off, no systemd user manager: a --handoff-only daemon; Fork gets its port and token only."""
    ctx = _ctx(kit)
    caplog.set_level("INFO", logger="fork_linux.bridge")
    daemon = bridge.start_handoff(ctx)
    assert daemon is not None
    assert daemon.handoff
    (start,) = kit.starts()
    argv = start["argv"]
    assert isinstance(argv, list)
    assert argv[argv.index("--handoff-only") + 1 : argv.index("--handoff-only") + 3] == [
        "--host-helper", str(kit.libexec / resources.HOST_HELPER)
    ]
    assert '--watch-prefix' in argv
    assert '--parent-pid' in argv
    assert start["env"]["WINEPREFIX"] == str(ctx.paths.prefix)
    assert daemon.env == {"FL_BRIDGE_PORT": "4242", "FL_BRIDGE_TOKEN": start["token"]}
    assert bridge.launch_env(ctx, daemon) == daemon.env, "with the bridge off too"
    assert bridge.host_actions_active(ctx) is False, "Fork's tools stay off fl-launch.exe"
    assert "hand-off daemon pid" in caplog.text
    record = bridge.session_record(daemon)
    assert record is not None
    assert record['handoff'] is True
    session = {"prefix": str(ctx.paths.prefix), "bridge": record}
    ctx.config.set("git", "bridge", "on")
    assert bridge.session_env(ctx, session) == {}, "never mistaken for the git bridge"
    assert bridge.running_daemon(ctx.paths, session) == {}
    daemon.stop()


def test_start_handoff_not_needed_or_not_possible(
    kit: bridge_kit.Kit, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    ctx = _ctx(kit)
    ctx.config.set("integration", "system_file_dialogs", "false")
    caplog.set_level("DEBUG", logger="fork_linux.bridge")
    _systemd_socket(Path(ctx.env["XDG_RUNTIME_DIR"]))
    monkeypatch.setattr(bridge.shutil, "which", lambda name, path=None: "/usr/bin/systemd-run")
    assert bridge.start_handoff(ctx) is None
    assert "desktop hand-off through systemd" in caplog.text
    monkeypatch.setattr(bridge, "systemd_user_manager", lambda env: False)
    (kit.libexec / resources.HOST_HELPER).unlink()
    assert bridge.start_handoff(ctx) is None
    assert "hand-off daemon not started (no fork-linux-host in this installation)" in caplog.text
    kit.helper.unlink()
    assert bridge.start_handoff(ctx) is None
    assert "no fl-bridge-helper in this installation" in caplog.text
    assert kit.starts() == []


def test_start_handoff_for_system_file_dialogs(kit: bridge_kit.Kit, monkeypatch: pytest.MonkeyPatch) -> None:
    """With systemd available, system file dialogs still need a host-helper daemon."""
    ctx = _ctx(kit)
    ctx.config.set("integration", "system_file_dialogs", "true")
    _systemd_socket(Path(ctx.env["XDG_RUNTIME_DIR"]))
    monkeypatch.setattr(bridge.shutil, "which", lambda name, path=None: "/usr/bin/systemd-run")
    daemon = bridge.start_handoff(ctx)
    assert daemon is not None
    assert daemon.handoff
    assert "--handoff-only" in kit.starts()[0]["argv"]
    daemon.stop()


def test_start_handoff_failure_is_only_logged(kit: bridge_kit.Kit, caplog: pytest.LogCaptureFixture) -> None:
    ctx = _ctx(kit)
    ctx.env["FL_FAKE_DAEMON"] = "fail"
    assert bridge.start_handoff(ctx) is None
    assert "hand-off daemon not started" in caplog.text
    assert isinstance(ctx.ui, FakeUI)
    assert ctx.ui.kinds("warn") == []
    assert list(ctx.paths.runtime_dir.glob("bridge-token-*")) == []


# -- the ssh-agent relay ---------------------------------------------------------------------------


@pytest.fixture
def agent_sock() -> Iterator[Path]:
    """A listening AF_UNIX socket standing in for the session's ssh-agent (short path: sun_path)."""
    directory = Path(tempfile.mkdtemp(prefix="fl-agent-"))
    path = directory / "agent.sock"
    server = socket.socket(socket.AF_UNIX)
    server.bind(str(path))
    server.listen(1)
    try:
        yield path
    finally:
        server.close()
        path.unlink()
        directory.rmdir()


def test_host_agent(tmp_path: Path, agent_sock: Path) -> None:
    assert bridge.host_agent({}) is None
    assert bridge.host_agent({"SSH_AUTH_SOCK": "relative.sock"}) is None
    assert bridge.host_agent({"SSH_AUTH_SOCK": str(tmp_path / "missing")}) is None
    (tmp_path / "file").write_text("", encoding="utf-8")
    assert bridge.host_agent({"SSH_AUTH_SOCK": str(tmp_path / "file")}) is None
    assert bridge.host_agent({"SSH_AUTH_SOCK": str(agent_sock)}) == agent_sock


def test_agent_socket_follows_the_setting(kit: bridge_kit.Kit, agent_sock: Path) -> None:
    ctx = _ctx(kit)
    assert bridge.agent_socket(ctx) is None, "no agent in the session"
    ctx.env["SSH_AUTH_SOCK"] = str(agent_sock)
    assert bridge.agent_relay_enabled(ctx)
    assert bridge.agent_socket(ctx) == agent_sock
    ctx.config.set("ssh", "agent_relay", "off")
    assert bridge.agent_socket(ctx) is None
    ctx.config.set("ssh", "agent_relay", "on")
    ctx.env["FORK_LINUX_SSH_AGENT_RELAY"] = "maybe"
    ctx.config = Config.load(ctx.paths, ctx.env)
    assert not bridge.agent_relay_enabled(ctx), "an invalid value counts as off"


def _agent_args(argv: list[str]) -> tuple[str, str]:
    return argv[argv.index("--ssh-agent") + 1], argv[argv.index("--ssh-agent-file") + 1]


def test_start_handoff_starts_for_the_relay(
    kit: bridge_kit.Kit, agent_sock: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With a systemd user manager no hand-off daemon is needed, but the relay starts one: Fork's
    SSH_AUTH_SOCK names the msys socket file the daemon wrote in our 0700 directory."""
    ctx = _ctx(kit)
    ctx.env["SSH_AUTH_SOCK"] = str(agent_sock)
    monkeypatch.setattr(bridge, "systemd_user_manager", lambda env: True)
    socket_file = ctx.paths.fork_linux_win_dir / "ssh-agent" / "agent.sock"
    socket_file.parent.mkdir(parents=True)
    socket_file.write_text("left by a daemon that crashed", encoding="utf-8")
    daemon = bridge.start_handoff(ctx)
    assert daemon is not None
    assert daemon.handoff
    assert daemon.agent
    (start,) = kit.starts()
    argv = start["argv"]
    assert isinstance(argv, list)
    assert _agent_args(argv) == (str(agent_sock), str(socket_file))
    assert stat.S_IMODE(socket_file.parent.stat().st_mode) == 0o700
    assert socket_file.read_bytes().startswith(b"!<socket >4243 s ")
    assert daemon.env["SSH_AUTH_SOCK"] == "C:/fork-linux/ssh-agent/agent.sock"
    assert bridge.launch_env(ctx, daemon)["SSH_AUTH_SOCK"] == bridge.AGENT_SOCK_WIN
    record = bridge.session_record(daemon)
    assert record is not None
    assert record['agent'] is True
    daemon.stop()


def test_relay_that_does_not_start_leaves_fork_without_it(
    kit: bridge_kit.Kit, agent_sock: Path, caplog: pytest.LogCaptureFixture
) -> None:
    ctx = _ctx(kit)
    ctx.env["SSH_AUTH_SOCK"] = str(agent_sock)
    ctx.env["FL_FAKE_DAEMON"] = "agentoff"
    ctx.paths.prefix.mkdir(parents=True)
    daemon = bridge.start_handoff(ctx)
    assert daemon is not None
    assert not daemon.agent
    assert "SSH_AUTH_SOCK" not in daemon.env
    assert "ssh-agent relay not started" in caplog.text
    daemon.stop()


@pytest.mark.parametrize("damage", ["linked-fork-linux", "linked-drive-c", "socket-is-a-directory", "dir-is-a-file"])
def test_a_damaged_relay_file_only_leaves_the_relay_off(
    kit: bridge_kit.Kit, agent_sock: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture, damage: str
) -> None:
    """A problem with only the relay's socket file never stops the daemon: hand-offs still leave
    Wine's tree, and nothing is created, changed or removed through a link in the prefix."""
    ctx = _ctx(kit)
    ctx.env["SSH_AUTH_SOCK"] = str(agent_sock)
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "ssh-agent").mkdir(parents=True)
    (elsewhere / "ssh-agent" / "agent.sock").write_text("not yours to delete", encoding="utf-8")
    (elsewhere / "ssh-agent").chmod(0o755)
    drive_c = ctx.paths.prefix / "drive_c"
    if damage == "linked-drive-c":
        ctx.paths.prefix.mkdir(parents=True)
        drive_c.symlink_to(tmp_path)
        (tmp_path / "fork-linux").symlink_to(elsewhere)
    elif damage == "linked-fork-linux":
        drive_c.mkdir(parents=True)
        ctx.paths.fork_linux_win_dir.symlink_to(elsewhere)
    elif damage == "socket-is-a-directory":
        (ctx.paths.fork_linux_win_dir / "ssh-agent" / "agent.sock").mkdir(parents=True)
    else:
        ctx.paths.fork_linux_win_dir.mkdir(parents=True)
        (ctx.paths.fork_linux_win_dir / "ssh-agent").write_text("", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="fork_linux.bridge"):
        daemon = bridge.start_handoff(ctx)
    assert daemon is not None
    assert daemon.handoff
    assert not daemon.agent
    assert "--ssh-agent" not in kit.starts()[-1]["argv"]
    assert "SSH_AUTH_SOCK" not in daemon.env
    assert "ssh-agent relay off: " in caplog.text
    daemon.stop()
    assert (elsewhere / "ssh-agent" / "agent.sock").read_text(encoding="utf-8") == "not yours to delete"
    assert stat.S_IMODE((elsewhere / "ssh-agent").stat().st_mode) == 0o755


def test_no_relay_never_deletes_through_a_link(kit: bridge_kit.Kit, tmp_path: Path) -> None:
    """Without a relay a leftover socket file is removed, but never through a linked directory."""
    ctx = _ctx(kit)
    paths = ctx.paths
    bridge._drop_agent_file(paths)  # nothing there: nothing to do
    (paths.prefix / "drive_c").mkdir(parents=True)
    bridge._drop_agent_file(paths)  # no C:\fork-linux yet
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "ssh-agent").mkdir(parents=True)
    (elsewhere / "ssh-agent" / "agent.sock").write_text("keep", encoding="utf-8")
    paths.fork_linux_win_dir.symlink_to(elsewhere)
    with pytest.raises(ForkLinuxError, match="is a link or not a directory"):
        bridge._drop_agent_file(paths)
    assert bridge._relay_file(paths, None) is None
    assert (elsewhere / "ssh-agent" / "agent.sock").read_text(encoding="utf-8") == "keep"


def test_git_bridge_daemon_relays_only_in_record_mode(kit: bridge_kit.Kit, agent_sock: Path) -> None:
    """Native git runs Linux ssh, which reaches the agent itself; record mode forwards to bundled git."""
    ctx = _ready(kit)
    ctx.env["SSH_AUTH_SOCK"] = str(agent_sock)
    leftover = ctx.paths.fork_linux_win_dir / "ssh-agent" / "agent.sock"
    leftover.parent.mkdir(parents=True)
    leftover.write_text("left by a daemon that crashed", encoding="utf-8")
    daemon = bridge.start_daemon(ctx)
    assert daemon is not None
    assert not daemon.agent
    assert not leftover.exists(), "a leftover file is removed when no relay starts too"
    assert "--ssh-agent" not in kit.starts()[-1]["argv"]
    assert "SSH_AUTH_SOCK" not in daemon.env
    daemon.stop()
    fork_git = ctx.paths.fork_local_dir(ctx.user) / "gitInstance" / "2.50.1" / "cmd"
    fork_git.mkdir(parents=True)
    (fork_git / "git.exe").write_bytes(b"MZ")
    ctx.config.set("git", "bridge_mode", "record")
    daemon = bridge.start_daemon(ctx)
    assert daemon is not None
    assert daemon.agent
    argv = kit.starts()[-1]["argv"]
    assert isinstance(argv, list)
    assert _agent_args(argv)[0] == str(agent_sock)
    assert daemon.env["SSH_AUTH_SOCK"] == bridge.AGENT_SOCK_WIN
    assert bridge.launch_env(ctx, daemon)["SSH_AUTH_SOCK"] == bridge.AGENT_SOCK_WIN
    daemon.stop()


def test_relay_failure_reads_the_newest_daemon_log(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    assert bridge.relay_failure(logs) is None, "no logs directory"
    logs.mkdir()
    assert bridge.relay_failure(logs) is None, "no daemon log"
    (logs / "bridge-calls-20990101T000000Z.log").write_text("ssh-agent relay off: not this one\n", encoding="utf-8")
    (logs / "bridge-20260101T000000Z.log").write_text(
        "x ssh-agent relay off: the agent path is not a socket\n", encoding="utf-8"
    )
    assert bridge.relay_failure(logs) == "the agent path is not a socket"
    (logs / "bridge-20260102T000000Z.log").write_text("x daemon start ssh-agent=on\n", encoding="utf-8")
    assert bridge.relay_failure(logs) is None
    (logs / "bridge-20260103T000000Z.log").mkdir()
    assert bridge.relay_failure(logs) is None, "unreadable"


def test_launch_env_without_a_daemon(kit: bridge_kit.Kit) -> None:
    ctx = _ready(kit)
    assert bridge.launch_env(ctx, None) == {}


# -- the session ------------------------------------------------------------------------------------


def _proc(root: Path, pid: int, cmdline: bytes) -> None:
    (root / str(pid)).mkdir(parents=True)
    (root / str(pid) / "cmdline").write_bytes(cmdline)


def test_session_record_and_env(kit: bridge_kit.Kit, tmp_path: Path) -> None:
    ctx = _ready(kit)
    proc = tmp_path / "proc"
    _proc(proc, 77, b"/usr/lib/fork-linux/fl-bridge-helper\0--daemon\0")
    _proc(proc, 78, b"/usr/bin/sleep\0--daemon\0")
    _proc(proc, 79, b"/usr/lib/fork-linux/fl-bridge-helper\0--version\0")
    assert bridge.session_record(None) is None
    env = {"FL_BRIDGE_PORT": "4242", "FL_BRIDGE_TOKEN": TOKEN, "FORKGITINSTANCE": "C:\\fork-linux\\gitInstance"}
    record = bridge.session_record(bridge.Daemon(77, 4242, env))
    assert record == {"pid": 77, "port": 4242, "env": env, "handoff": False, "agent": False}
    session: dict[str, Any] = {"prefix": str(ctx.paths.prefix), "bridge": record}
    assert bridge.session_env(ctx, session, proc) == env
    assert bridge.running_daemon(ctx.paths, session, proc) == {"pid": 77, "port": 4242}
    for pid in (78, 79, 80, 0, True, "77"):
        assert bridge.session_env(ctx, {"bridge": {**record, "pid": pid}}, proc) == {}
    for bad in ({**env, "FL_BRIDGE_TOKEN": "short"}, {**env, "FL_BRIDGE_PORT": "x"}, {"FL_BRIDGE_PORT": 1}, []):
        assert bridge.session_env(ctx, {"bridge": {**record, "env": bad}}, proc) == {}
    assert bridge.session_env(ctx, {"bridge": "nope"}, proc) == {}
    assert bridge.session_env(ctx, None, proc) == {}
    assert bridge.running_daemon(ctx.paths, None, proc) == {}
    assert bridge.running_daemon(ctx.paths, {**session, "prefix": "/other"}, proc) == {}
    assert bridge.running_daemon(ctx.paths, {**session, "bridge": {"pid": 78}}, proc) == {}
    ctx.config.set("git", "bridge", "off")
    assert bridge.session_env(ctx, session, proc) == {}
    assert bridge.daemon_alive(77) is False


def test_status_reports_the_running_daemon(kit: bridge_kit.Kit) -> None:
    ctx = _ready(kit)
    daemon = bridge.start_daemon(ctx)
    assert daemon is not None
    session = {"prefix": str(ctx.paths.prefix), "bridge": bridge.session_record(daemon)}
    assert bridge.status(ctx, session)["daemon"] == {"pid": daemon.pid, "port": 4242}
    assert json.dumps(bridge.status(ctx, session)).count(daemon.env["FL_BRIDGE_TOKEN"]) == 0
    daemon.stop()


def test_system_file_dialogs_enabled_defaults_on_when_invalid(kit: bridge_kit.Kit) -> None:
    ctx = _ctx(kit)
    assert bridge.system_file_dialogs_enabled(ctx) is True
    ctx.config.set("integration", "system_file_dialogs", "false")
    assert bridge.system_file_dialogs_enabled(ctx) is False
    path = ctx.paths.config_file
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[integration]\nsystem_file_dialogs = maybe\n", encoding="utf-8")
    ctx.config = type(ctx.config).load(ctx.paths, ctx.env)
    assert bridge.system_file_dialogs_enabled(ctx) is True
