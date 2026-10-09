"""Tests for fork_linux.launcher: path resolution, the Wine environment, hooks, session and exec."""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
from pathlib import Path
from typing import Any

import pytest

from fork_linux import bootstrap, launcher, procs, snapshots, updates
from fork_linux.cli import AppContext
from fork_linux.errors import ForkLinuxError, IntegrityFailed, NotSetUpError, UsageError, WineUnavailable
from fork_linux.fork_layout import ForkLayout
from fork_linux.procrun import RecordingRunner

from fixtures.fork_tree import install_fork, write_settings
from fixtures.setup_ctx import USER, FakeUI, make_ctx


# -- fixtures ------------------------------------------------------------------------


def _make_prefix(ctx: bootstrap.Ctx) -> None:
    """Drive links like a real prefix: C: -> drive_c, Z: -> /."""
    dosdevices = ctx.paths.prefix / "dosdevices"
    dosdevices.mkdir(parents=True, exist_ok=True)
    (ctx.paths.prefix / "drive_c" / "users" / USER).mkdir(parents=True, exist_ok=True)
    (dosdevices / "c:").symlink_to("../drive_c")
    (dosdevices / "z:").symlink_to("/")


def _fake_wine(ctx: bootstrap.Ctx) -> None:
    """Create the fake Wine binaries the context points at."""
    info = ctx.wine()
    info.wine.parent.mkdir(parents=True, exist_ok=True)
    info.wine.write_text("#!/bin/sh\n", encoding="utf-8")
    info.wineserver.write_text("#!/bin/sh\n", encoding="utf-8")


@pytest.fixture
def not_running(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    """Fork is not running (flip ``state[0]`` to pretend it is)."""
    state = [False]
    monkeypatch.setattr(procs, "fork_running", lambda prefix, **_kw: state[0])
    return state


@pytest.fixture
def ctx(xdg: Path, not_running: list[bool]) -> bootstrap.Ctx:
    """A Ctx with a fake prefix, Fork 2.23.2 installed and quiet desktop probes."""
    env = dict(os.environ)
    for var in ("DISPLAY", "WAYLAND_DISPLAY", "GTK_THEME", "GDK_SCALE", "GDK_DPI_SCALE", "QT_SCALE_FACTOR"):
        env.pop(var, None)
    env["XDG_CURRENT_DESKTOP"] = "none"
    made = make_ctx(env=env)
    _make_prefix(made)
    _fake_wine(made)
    install_fork(made.layout, "2.23.2")
    return made


def _notes_kinds(ctx: bootstrap.Ctx, kind: str) -> list[Any]:
    assert isinstance(ctx.ui, FakeUI)
    return ctx.ui.kinds(kind)


# -- resolve_targets ------------------------------------------------------------------


def test_resolve_targets_relative_absolute_and_duplicates(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    found = launcher.resolve_targets(["repo", str(other), "./repo"], tmp_path)
    assert found == [repo, other]


def test_resolve_targets_keeps_symlinks(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "link").symlink_to(real)
    assert launcher.resolve_targets(["link"], tmp_path) == [tmp_path / "link"]


def test_resolve_targets_rejects_missing_and_empty(tmp_path: Path) -> None:
    with pytest.raises(UsageError, match="no such file"):
        launcher.resolve_targets(["missing"], tmp_path)
    with pytest.raises(UsageError, match="empty path"):
        launcher.resolve_targets([""], tmp_path)


def test_resolve_targets_from_file_manager(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    nested = repo / "src" / "deep"
    nested.mkdir(parents=True)
    (nested / "file.txt").write_text("x", encoding="utf-8")
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / ".git").write_text("gitdir: /elsewhere\n", encoding="utf-8")
    (worktree / "a.txt").write_text("x", encoding="utf-8")
    loose = tmp_path / "loose"
    loose.mkdir()
    (loose / "b.txt").write_text("x", encoding="utf-8")
    found = launcher.resolve_targets(
        ["repo/src/deep/file.txt", "wt/a.txt", "loose/b.txt", "repo/src"], tmp_path, from_file_manager=True
    )
    # A directory stays as it is; files become their repository (or their folder outside one).
    assert found == [repo, worktree, loose, repo / "src"]
    # Without the flag a file is passed through unchanged.
    assert launcher.resolve_targets(["loose/b.txt"], tmp_path) == [loose / "b.txt"]


def test_repo_root_without_any_git_dir_is_the_parent(tmp_path: Path) -> None:
    file = tmp_path / "f"
    file.write_text("x", encoding="utf-8")
    assert launcher._repo_root(file) == tmp_path


# -- build_spec ---------------------------------------------------------------------------


def test_build_spec_defaults(ctx: bootstrap.Ctx, tmp_path: Path) -> None:
    repo = tmp_path / "my repo"
    repo.mkdir()
    spec = launcher.build_spec(ctx, [repo])
    info = ctx.wine()
    win_repo = "Z:\\" + str(repo).strip("/").replace("/", "\\")
    assert spec.argv == [str(info.wine), f"C:\\users\\{USER}\\AppData\\Local\\Fork\\current\\Fork.exe", win_repo]
    assert spec.cwd == ctx.layout.current_dir
    assert spec.log_file == ctx.paths.logs_dir / launcher.LAST_LOG
    env = spec.env
    assert env["WINEPREFIX"] == str(ctx.paths.prefix)
    assert env["WINEDEBUG"] == "-all"
    assert env["WINEDLLOVERRIDES"] == "winemenubuilder.exe=d"
    assert env["WINEHOME"] == f"C:\\users\\{USER}"
    assert env["GIT_CONFIG_COUNT"] == "2"
    assert env["GIT_CONFIG_KEY_0"] == "core.filemode"
    assert env["GIT_CONFIG_VALUE_0"] == "false"
    assert env["HOME"] == ctx.env["HOME"]


def test_build_spec_without_targets_reads_no_drive_map(ctx: bootstrap.Ctx) -> None:
    for link in (ctx.paths.prefix / "dosdevices").iterdir():
        link.unlink()
    spec = launcher.build_spec(ctx, [])
    assert spec.argv[2:] == []


def test_build_spec_debug_channels_and_rotating_logs(ctx: bootstrap.Ctx, monkeypatch: pytest.MonkeyPatch) -> None:
    logs = ctx.paths.logs_dir
    logs.mkdir(parents=True, exist_ok=True)
    for index in range(12):
        (logs / f"wine-20200101T0000{index:02d}Z.log").write_text("old", encoding="utf-8")
    (logs / launcher.LAST_LOG).write_text("last", encoding="utf-8")
    (logs / "fork-linux.log").write_text("ours", encoding="utf-8")
    monkeypatch.setattr(launcher, "_utc_stamp", lambda: "20300101T000000Z")
    spec = launcher.build_spec(ctx, [], debug=True)
    assert spec.env["WINEDEBUG"] == launcher.DEBUG_CHANNELS
    assert spec.log_file == logs / "wine-20300101T000000Z.log"
    spec.log_file.write_text("new", encoding="utf-8")
    second = launcher.build_spec(ctx, [], debug=True, wine_debug="+relay")
    assert second.env["WINEDEBUG"] == "+relay"
    assert second.log_file == logs / "wine-20300101T000000Z-1.log"
    second.log_file.write_text("newer", encoding="utf-8")
    debug_logs = sorted(name for name in os.listdir(logs) if name.startswith("wine-2"))
    assert len(debug_logs) == launcher.DEBUG_LOGS_KEEP
    assert "wine-20300101T000000Z.log" in debug_logs
    assert (logs / launcher.LAST_LOG).exists()
    assert (logs / "fork-linux.log").exists()


def test_build_spec_config_debug_overrides_and_drivers(ctx: bootstrap.Ctx, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx.config.set("wine", "debug", "err+all")
    ctx.config.set("wine", "extra_dll_overrides", "dwrite=n,b")
    ctx.env["DISPLAY"] = ":0"
    spec = launcher.build_spec(ctx, [], wine_debug="")
    assert spec.env["WINEDEBUG"] == "err+all"
    assert spec.env["WINEDLLOVERRIDES"] == "dwrite=n,b;winemenubuilder.exe=d"
    assert spec.env["DISPLAY"] == ":0"
    assert "DISPLAY" not in launcher.build_spec(ctx, [], driver="wayland").env
    ctx.config.set("wine", "driver", "wayland")
    assert "DISPLAY" not in launcher.build_spec(ctx, []).env
    assert launcher.build_spec(ctx, [], driver="x11").env["DISPLAY"] == ":0"
    ctx.config.set("wine", "debug", "")
    assert launcher.build_spec(ctx, []).env["WINEDEBUG"] == launcher.QUIET_CHANNELS


def test_build_spec_includes_bridge_env(ctx: bootstrap.Ctx, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(launcher.bridge, "launch_env", lambda _ctx: {"FORKGITINSTANCE": "C:\\fork-linux\\g"})
    assert launcher.build_spec(ctx, []).env["FORKGITINSTANCE"] == "C:\\fork-linux\\g"


# -- pre_launch -----------------------------------------------------------------------------


def test_pre_launch_first_run_records_version_snapshot_and_syncs(ctx: bootstrap.Ctx) -> None:
    home = ctx.host_home
    (home / ".ssh").mkdir(mode=0o700)
    (home / ".ssh" / "id_ed25519").write_text("KEY", encoding="utf-8")
    (home / ".gitconfig").write_text("[user]\n\tname = T\n", encoding="utf-8")
    write_settings(ctx.layout, {"UpdateSubmodulesOnCheckout": True, "Other": 1})
    notes = launcher.pre_launch(ctx)
    assert ctx.state.get(updates.LAST_SEEN_VERSION) == "2.23.2"
    assert any(note.startswith("snapshot 2.23.2-") for note in notes)
    assert "ssh keys and config shared with Fork" in notes
    assert "git configuration translated for Fork" in notes
    assert any(note.startswith("Fork settings updated:") for note in notes)
    settings = json.loads(ctx.layout.settings_file.read_text(encoding="utf-8"))
    assert settings["UpdateSubmodulesOnCheckout"] is False
    assert settings["DisableHardwareAcceleration"] is True
    assert settings["Other"] == 1
    assert _notes_kinds(ctx, "info") == []
    # Saved to disk.
    saved = json.loads(ctx.paths.state_file.read_text(encoding="utf-8"))
    assert saved["applied"]["ssh_sync"] == ctx.state.get(launcher.APPLIED_SSH)
    # A second launch with nothing changed does nothing.
    assert launcher.pre_launch(ctx) == []


def test_pre_launch_reports_update_and_known_bad(ctx: bootstrap.Ctx) -> None:
    ctx.state.set(updates.LAST_SEEN_VERSION, "2.23.1")

    class BadManifest:
        def known_bad_reason(self, version: str) -> str | None:
            return "crashes on start" if version == "2.23.2" else None

    ctx.manifest = BadManifest()  # type-compatible stand-in for the two calls made
    notes = launcher.pre_launch(ctx)
    expected = "Fork updated 2.23.1 -> 2.23.2; 'fork-linux rollback' restores 2.23.1"
    assert expected in notes
    assert _notes_kinds(ctx, "info") == [expected]
    assert any("crashes on start" in msg for msg in _notes_kinds(ctx, "warn"))
    # Unchanged version: still warned about, from the installed version.
    ctx.ui.events.clear()
    launcher.pre_launch(ctx)
    assert any("crashes on start" in msg for msg in _notes_kinds(ctx, "warn"))


def test_note_version_without_fork_installed(ctx: bootstrap.Ctx) -> None:
    ctx.layout.sq_version_file.unlink()
    notes: list[str] = []
    launcher._note_version(ctx, notes)
    assert notes == []


def test_snapshot_hook_disabled(ctx: bootstrap.Ctx) -> None:
    ctx.config.set("snapshots", "keep", "0")
    notes: list[str] = []
    launcher._snapshot(ctx, notes)
    assert notes == []
    assert snapshots.list_snapshots(ctx.paths) == []


def _stage(layout: ForkLayout, version: str) -> Path:
    package = layout.packages_dir / f"Fork-{version}-full.nupkg"
    package.write_bytes(b"PK")
    return package


def test_pin_hook(ctx: bootstrap.Ctx) -> None:
    newer = _stage(ctx.layout, "2.24.0")
    notes: list[str] = []
    launcher._pin(ctx, notes)
    assert newer.exists() and notes == []
    ctx.config.set("fork", "update_policy", "pinned")
    ctx.state.set(launcher.PINNED_VERSION, "2.23.2")
    launcher._pin(ctx, notes)
    assert not newer.exists()
    assert notes == ["removed staged update Fork-2.24.0-full.nupkg (Fork is pinned to 2.23.2)"]
    # No (valid) pinned version: the installed one counts.
    newer = _stage(ctx.layout, "2.25.0")
    ctx.state.set(launcher.PINNED_VERSION, "garbage")
    launcher._pin(ctx, notes)
    assert not newer.exists()
    # Nothing installed and nothing pinned: nothing to enforce.
    ctx.state.set(launcher.PINNED_VERSION, None)
    ctx.layout.sq_version_file.unlink()
    newer = _stage(ctx.layout, "2.26.0")
    launcher._pin(ctx, notes)
    assert newer.exists()


def test_pin_hook_warns_when_fork_already_updated_past_the_pin(ctx: bootstrap.Ctx) -> None:
    ctx.config.set("fork", "update_policy", "pinned")
    ctx.state.set(launcher.PINNED_VERSION, "2.23.0")
    notes: list[str] = []
    launcher._pin(ctx, notes)
    assert notes[-1] == "Fork 2.23.2 is installed although Fork is pinned to 2.23.0"
    warnings = _notes_kinds(ctx, "warn")
    assert any("rollback --to-version 2.23.0" in str(item) for item in warnings)


def test_settings_hook_skipped_while_fork_runs(ctx: bootstrap.Ctx, not_running: list[bool]) -> None:
    write_settings(ctx.layout, {"UpdateSubmodulesOnCheckout": True})
    not_running[0] = True
    notes: list[str] = []
    launcher._settings(ctx, notes)
    assert notes == []
    assert json.loads(ctx.layout.settings_file.read_text(encoding="utf-8"))["UpdateSubmodulesOnCheckout"] is True


def test_settings_hook_seeds_a_missing_settings_file(ctx: bootstrap.Ctx) -> None:
    notes: list[str] = []
    launcher._settings(ctx, notes)
    assert len(notes) == 1 and notes[0].startswith("Fork settings updated: Guid, ")
    data = json.loads(ctx.layout.settings_file.read_text(encoding="utf-8"))
    assert data["UpdateSubmodulesOnCheckout"] is False and "Guid" in data


def test_desired_settings_probes_and_shell_tool(ctx: bootstrap.Ctx, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(launcher.theme, "detect", lambda env, runner, home: calls.append("theme") or "dark")
    monkeypatch.setattr(launcher.display, "scale_percent", lambda env, runner: calls.append("scale") or 150)
    write_settings(ctx.layout, {"ShellTool": None, "RepositoryManager": {"SourceDirectories": [f"C:\\users\\{USER}"]}})

    def desired() -> dict[str, object]:
        return launcher.desired_settings(
            paths=ctx.paths, config=ctx.config, env=ctx.env, runner=ctx.runner, user=ctx.user, layout=ctx.layout
        )

    wanted = desired()
    assert calls == ["theme", "scale"]
    assert wanted["Theme"] == 1 and wanted["LayoutScaling"] == 150
    assert "ShellTool" not in wanted
    home_win = "Z:\\" + str(ctx.host_home).strip("/").replace("/", "\\")
    assert wanted["RepositoryManager.SourceDirectories"] == [home_win]
    launch = ctx.paths.fork_linux_win_dir / "bin" / "fl-launch.exe"
    launch.parent.mkdir(parents=True)
    launch.write_bytes(b"MZ")
    ctx.config.set("display", "theme", "light")
    ctx.config.set("display", "dpi", "120")
    calls.clear()
    wanted = desired()
    assert calls == []
    assert wanted["ShellTool"] == {
        "Type": "Custom",
        "ApplicationPath": "C:\\fork-linux\\bin\\fl-launch.exe",
        "Arguments": "terminal",
    }
    assert wanted["Theme"] == 0 and wanted["LayoutScaling"] == 125
    # Without drive links the home directory cannot be mapped: left alone.
    for link in (ctx.paths.prefix / "dosdevices").iterdir():
        link.unlink()
    assert "RepositoryManager.SourceDirectories" not in desired()


def test_ssh_hook_modes(ctx: bootstrap.Ctx, monkeypatch: pytest.MonkeyPatch) -> None:
    synced: list[str] = []
    monkeypatch.setattr(launcher.ssh_sync, "sync", lambda paths, user, home, mode: synced.append(mode))
    notes: list[str] = []
    launcher._ssh(ctx, notes)  # auto without ~/.ssh
    assert synced == []
    ctx.config.set("ssh", "sync", "on")
    ctx.config.set("ssh", "mode", "copy")
    launcher._ssh(ctx, notes)
    assert synced == ["copy"]
    assert ctx.state.get(launcher.APPLIED_SSH) == [None, None]
    launcher._ssh(ctx, notes)  # unchanged
    assert synced == ["copy"]
    ssh_dir = ctx.host_home / ".ssh"
    ssh_dir.mkdir()
    (ssh_dir / "config").write_text("Host x\n", encoding="utf-8")
    launcher._ssh(ctx, notes)
    assert synced == ["copy", "copy"]
    ctx.config.set("ssh", "sync", "off")
    (ssh_dir / "config").write_text("Host y\n", encoding="utf-8")
    os.utime(ssh_dir / "config", ns=(1, 1))
    launcher._ssh(ctx, notes)
    assert synced == ["copy", "copy"]


def test_git_hook_modes(ctx: bootstrap.Ctx, monkeypatch: pytest.MonkeyPatch) -> None:
    synced: list[Path] = []
    monkeypatch.setattr(launcher.gitconfig, "sync", lambda paths, user, home, pathmap: synced.append(home))
    notes: list[str] = []
    ctx.config.set("git", "config_overlay", "off")
    launcher._git(ctx, notes)
    assert synced == []
    ctx.config.set("git", "config_overlay", "translate")
    launcher._git(ctx, notes)
    launcher._git(ctx, notes)
    assert synced == [ctx.host_home]
    (ctx.host_home / ".gitconfig").write_text("[core]\n", encoding="utf-8")
    launcher._git(ctx, notes)
    assert len(synced) == 2


def test_pre_launch_continues_after_a_failing_hook(ctx: bootstrap.Ctx, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*_args: Any, **_kwargs: Any) -> None:
        raise IntegrityFailed("snapshot store damaged")

    def oserror(*_args: Any, **_kwargs: Any) -> None:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(launcher.snapshots, "ensure_current", broken)
    monkeypatch.setattr(launcher.gitconfig, "sync", oserror)
    notes = launcher.pre_launch(ctx)
    assert "snapshot skipped: snapshot store damaged" in notes
    assert any(note.startswith("git configuration skipped:") for note in notes)
    assert len(_notes_kinds(ctx, "warn")) == 2
    assert ctx.paths.state_file.exists()


def test_mtime_and_host_home(tmp_path: Path) -> None:
    assert launcher._mtime(tmp_path / "missing") is None
    assert launcher._mtime(tmp_path) == tmp_path.stat().st_mtime_ns
    assert launcher._host_home({"HOME": str(tmp_path)}) == tmp_path
    assert launcher._host_home({}) == Path.home()


# -- session -------------------------------------------------------------------------------------


def test_session_round_trip_and_bad_files(ctx: bootstrap.Ctx) -> None:
    paths = ctx.paths
    assert launcher.read_session(paths) is None
    path = launcher.write_session(paths, ctx.wine(), 4242)
    data = launcher.read_session(paths)
    assert data is not None
    assert data["pid"] == 4242
    assert data["wine_root"] == str(ctx.wine().root)
    assert data["prefix"] == str(paths.prefix)
    assert {"started", "wine", "wineserver", "provider", "schema"} <= set(data)
    assert path.stat().st_mode & 0o777 == 0o600
    path.write_text("[1, 2]", encoding="utf-8")
    assert launcher.read_session(paths) is None
    path.write_text("{not json", encoding="utf-8")
    assert launcher.read_session(paths) is None


def _write_raw_session(ctx: bootstrap.Ctx, **data: Any) -> None:
    ctx.paths.runtime_dir.mkdir(parents=True, exist_ok=True)
    ctx.paths.session_file.write_text(json.dumps(data), encoding="utf-8")


def test_session_wine(ctx: bootstrap.Ctx) -> None:
    paths = ctx.paths
    info = ctx.wine()
    root = str(info.root)
    assert launcher._session_wine(paths) is None
    _write_raw_session(ctx, prefix="/elsewhere", wine_root=root)
    assert launcher._session_wine(paths) is None
    _write_raw_session(ctx, prefix=str(paths.prefix), wine_root="relative/root")
    assert launcher._session_wine(paths) is None
    _write_raw_session(ctx, prefix=str(paths.prefix), wine_root=7)
    assert launcher._session_wine(paths) is None
    _write_raw_session(ctx, prefix=str(paths.prefix), wine_root="/nonexistent-wine-root")
    assert launcher._session_wine(paths) is None
    _write_raw_session(ctx, prefix=str(paths.prefix), wine_root=root, provider=5, wine="rel", wineserver=None)
    found = launcher._session_wine(paths)
    assert found is not None
    assert (found.provider, found.wine, found.wineserver) == ("session", info.wine, info.wineserver)
    launcher.write_session(paths, info, 1)
    found = launcher._session_wine(paths)
    assert found is not None and found.provider == info.provider and found.root == info.root


# -- exec ----------------------------------------------------------------------------------


class Exec:
    """Records the exec instead of replacing the test process."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str], dict[str, str], str]] = []

    def __call__(self, file: str, argv: list[str], env: dict[str, str]) -> None:
        self.calls.append((file, list(argv), dict(env), os.getcwd()))


class _OsProxy:
    """``os`` for the launcher module, with ``dup2`` recorded (pytest's own capture needs the real one)."""

    def __init__(self, calls: list[tuple[int, int]]) -> None:
        self.calls = calls

    def __getattr__(self, name: str) -> Any:
        return getattr(os, name)

    def dup2(self, fd: int, target: int) -> None:
        self.calls.append((fd, target))


@pytest.fixture
def dup2(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, int]]:
    """Record the launcher's dup2 calls instead of redirecting pytest's own stdout/stderr."""
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(launcher, "os", _OsProxy(calls))
    return calls


def test_exec_redirects_and_truncates(
    ctx: bootstrap.Ctx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dup2: list[tuple[int, int]]
) -> None:
    monkeypatch.chdir(tmp_path)
    log_file = tmp_path / "wine.log"
    log_file.write_text("old output\n", encoding="utf-8")
    spec = launcher.LaunchSpec(argv=["/bin/wine", "x"], env={"A": "1"}, cwd=tmp_path, log_file=log_file)
    fake = Exec()
    assert launcher._exec(spec, truncate=False, execvpe=fake) == 0
    assert [target for _fd, target in dup2] == [1, 2]
    assert log_file.read_text(encoding="utf-8") == "old output\n"
    assert fake.calls == [("/bin/wine", ["/bin/wine", "x"], {"A": "1"}, str(tmp_path))]
    launcher._exec(spec, truncate=True, execvpe=fake)
    assert log_file.read_text(encoding="utf-8") == ""
    dup2.clear()
    launcher._exec(launcher.LaunchSpec(["/bin/wine"], {}, tmp_path, None), truncate=True, execvpe=fake)
    assert dup2 == []


def test_redirect_creates_a_private_log(tmp_path: Path, dup2: list[tuple[int, int]]) -> None:
    log_file = tmp_path / "new.log"
    launcher._redirect(log_file, truncate=True)
    assert log_file.stat().st_mode & 0o777 == 0o600
    assert len(dup2) == 2


def test_redirect_closes_the_descriptor_when_dup2_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    closed: list[int] = []
    real_close = os.close

    class Failing(_OsProxy):
        def dup2(self, fd: int, target: int) -> None:
            raise OSError(9, "Bad file descriptor")

        def close(self, fd: int) -> None:
            closed.append(fd)
            real_close(fd)

    monkeypatch.setattr(launcher, "os", Failing([]))
    with pytest.raises(OSError):
        launcher._redirect(tmp_path / "x.log", truncate=False)
    assert len(closed) == 1


# -- run -----------------------------------------------------------------------------------------


def _app(env: dict[str, str], verbose: int = 0) -> AppContext:
    args = argparse.Namespace(prefix=None, gui=False, offline=False, verbose=verbose, quiet=0, allow_root=False)
    return AppContext(args, env=env, runner=RecordingRunner())


@pytest.fixture
def flow(
    ctx: bootstrap.Ctx, monkeypatch: pytest.MonkeyPatch, dup2: list[tuple[int, int]], tmp_path: Path
) -> dict[str, Any]:
    """Wire launcher.run to ``ctx``: from_app returns it, ensure_ready records its arguments."""
    seen: dict[str, Any] = {"ensure": [], "hooks": 0}
    monkeypatch.setattr(bootstrap.Ctx, "from_app", classmethod(lambda cls, app: ctx))

    def ensure_ready(c: bootstrap.Ctx, *, allow: bool = True) -> None:
        seen["ensure"].append(allow)

    def pre_launch(c: bootstrap.Ctx) -> list[str]:
        seen["hooks"] += 1
        return []

    monkeypatch.setattr(bootstrap, "ensure_ready", ensure_ready)
    monkeypatch.setattr(launcher, "pre_launch", pre_launch)
    cwd = tmp_path / "cwd"
    (cwd / "repo").mkdir(parents=True)
    monkeypatch.chdir(cwd)
    env = dict(ctx.env)
    seen["app"] = _app(env)
    seen["exec"] = Exec()
    seen["cwd"] = cwd
    return seen


def test_run_normal_path(ctx: bootstrap.Ctx, flow: dict[str, Any], dup2: list[tuple[int, int]]) -> None:
    rc = launcher.run(flow["app"], ["repo"], execvpe=flow["exec"])
    assert rc == 0
    assert flow["ensure"] == [True]
    assert flow["hooks"] == 1
    file, argv, env, cwd = flow["exec"].calls[0]
    assert file == str(ctx.wine().wine)
    assert argv[-1].endswith("\\cwd\\repo")
    assert cwd == str(ctx.layout.current_dir)
    assert env["WINEPREFIX"] == str(ctx.paths.prefix)
    session = launcher.read_session(ctx.paths)
    assert session is not None and session["pid"] == os.getpid()
    assert len(dup2) == 2


def test_run_no_setup_no_hooks_and_terminal_debug(
    ctx: bootstrap.Ctx, flow: dict[str, Any], dup2: list[tuple[int, int]]
) -> None:
    app = _app(dict(ctx.env), verbose=1)
    launcher.run(app, [], debug=True, no_setup=True, no_hooks=True, execvpe=flow["exec"])
    assert flow["ensure"] == [False]
    assert flow["hooks"] == 0
    assert dup2 == []
    assert flow["exec"].calls[0][2]["WINEDEBUG"] == launcher.DEBUG_CHANNELS


def test_run_not_set_up_gets_a_setup_hint(
    ctx: bootstrap.Ctx, flow: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(c: bootstrap.Ctx, *, allow: bool = True) -> None:
        raise NotSetUpError("not set up", hint="")

    monkeypatch.setattr(bootstrap, "ensure_ready", refuse)
    with pytest.raises(NotSetUpError) as info:
        launcher.run(flow["app"], [], no_setup=True, execvpe=flow["exec"])
    assert info.value.hint == launcher.SETUP_HINT
    assert info.value.exit_code == 10

    def refuse_with_hint(c: bootstrap.Ctx, *, allow: bool = True) -> None:
        raise NotSetUpError("not set up", hint="run 'fork-linux setup'")

    monkeypatch.setattr(bootstrap, "ensure_ready", refuse_with_hint)
    with pytest.raises(NotSetUpError) as info:
        launcher.run(flow["app"], [], execvpe=flow["exec"])
    assert info.value.hint == "run 'fork-linux setup'"
    assert flow["exec"].calls == []


def test_run_fast_path_when_fork_runs(
    ctx: bootstrap.Ctx, flow: dict[str, Any], not_running: list[bool], dup2: list[tuple[int, int]]
) -> None:
    launcher.write_session(ctx.paths, ctx.wine(), 99)
    not_running[0] = True
    launcher.run(flow["app"], ["repo"], execvpe=flow["exec"])
    assert flow["ensure"] == [] and flow["hooks"] == 0
    file, argv, env, _cwd = flow["exec"].calls[0]
    assert file == str(ctx.wine().wine)
    assert argv[1] == ctx.layout.win_exe
    assert env["WINEPREFIX"] == str(ctx.paths.prefix)
    # Appended to the running session's log, never truncated.
    assert len(dup2) == 2
    session = launcher.read_session(ctx.paths)
    assert session is not None and session["pid"] == 99
    # -v --debug: Wine's output stays on the terminal.
    dup2.clear()
    launcher.run(_app(dict(ctx.env), verbose=1), [], debug=True, execvpe=flow["exec"])
    assert dup2 == []


def test_run_running_without_session_takes_the_normal_path_without_hooks(
    ctx: bootstrap.Ctx, flow: dict[str, Any], not_running: list[bool]
) -> None:
    not_running[0] = True
    launcher.run(flow["app"], [], execvpe=flow["exec"])
    assert flow["ensure"] == [True]
    assert flow["hooks"] == 0
    assert len(flow["exec"].calls) == 1


def test_run_missing_path_is_a_usage_error(flow: dict[str, Any]) -> None:
    with pytest.raises(UsageError):
        launcher.run(flow["app"], ["nope"], execvpe=flow["exec"])
    assert flow["exec"].calls == []


def test_run_default_execvpe_is_os_execvpe() -> None:
    assert launcher.run.__kwdefaults__["execvpe"] is os.execvpe


def test_exec_missing_install_dir_is_not_set_up(tmp_path: Path, dup2: list[tuple[int, int]]) -> None:
    spec = launcher.LaunchSpec(["/bin/wine"], {}, tmp_path / "gone", tmp_path / "wine.log")
    fake = Exec()
    with pytest.raises(NotSetUpError, match="install directory"):
        launcher._exec(spec, truncate=True, execvpe=fake)
    assert fake.calls == [] and dup2 == []


def test_exec_unwritable_log_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dup2: list[tuple[int, int]]
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "target").write_text("", encoding="utf-8")
    log_file = tmp_path / "wine.log"
    log_file.symlink_to(tmp_path / "target")
    fake = Exec()
    with pytest.raises(ForkLinuxError, match="cannot write Wine's log"):
        launcher._exec(launcher.LaunchSpec(["/bin/wine"], {}, tmp_path, log_file), truncate=True, execvpe=fake)
    assert fake.calls == [] and dup2 == []


def test_exec_failure_restores_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dup2: list[tuple[int, int]]
) -> None:
    monkeypatch.chdir(tmp_path)

    def missing(file: str, argv: list[str], env: dict[str, str]) -> None:
        raise FileNotFoundError(2, "No such file or directory")

    spec = launcher.LaunchSpec(["/nope/wine"], {}, tmp_path, tmp_path / "wine.log")
    with pytest.raises(WineUnavailable, match="cannot start /nope/wine: No such file"):
        launcher._exec(spec, truncate=True, execvpe=missing)
    # Redirected to the log (1, 2), then back to the saved descriptors (1, 2).
    assert [target for _fd, target in dup2] == [1, 2, 1, 2]
    saved = [fd for fd, _target in dup2[2:]]
    for fd in saved:
        with pytest.raises(OSError):
            os.fstat(fd)
    dup2.clear()
    with pytest.raises(WineUnavailable):
        launcher._exec(dataclasses.replace(spec, log_file=None), truncate=True, execvpe=missing)
    assert dup2 == []

