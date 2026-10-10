"""App-upgrade scenarios of docs/qa/UPDATE-SCENARIOS.md (A*) at the unit tier, with fakes.

Each test names its scenario. Nothing runs Wine: a fake ``/proc`` stands for the processes
serving the prefix, and the recording runner stands for Wine and wineserver.
"""

from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path

import pytest

from fork_linux import bootstrap, desktop_integration, launcher, resources, updates, wine_provider
from fork_linux.procrun import RecordingRunner
from fork_linux import version as version_mod
from fork_linux.bootstrap import Ctx
from fork_linux.errors import SetupFailed
from fork_linux.locking import FileLock
from fork_linux.state import WRITTEN_BY, State
from fork_linux.steps import integration, prefix

from fixtures.cli_run import fork_running, isolate, run_cli
from fixtures.fork_tree import install_fork
from fixtures.setup_ctx import FakeUI, make_ctx


def _managed_build(ctx: Ctx, build_id: str) -> Path:
    """A complete managed Wine build ``build_id`` in our data directory; returns its root."""
    root = wine_provider.managed_root(ctx.paths, build_id)
    (root / "bin").mkdir(parents=True)
    for name in ("wine", "wineserver"):
        (root / "bin" / name).write_text("#!/bin/sh\n", encoding="utf-8")
    (root / wine_provider.COMPLETE_MARKER).write_text("0" * 64 + "\n", encoding="utf-8")
    return root


def _wineserver_process(proc: Path, pid: int, prefix_dir: Path, exe: Path) -> None:
    directory = proc / str(pid)
    directory.mkdir(parents=True)
    (directory / "environ").write_bytes(f"WINEPREFIX={prefix_dir}\0".encode())
    (directory / "cmdline").write_bytes(b"/x/wineserver\0")
    (directory / "exe").symlink_to(exe)


@pytest.fixture
def proc(tmp_path: Path) -> Path:
    root = tmp_path / "proc"
    root.mkdir()
    return root


def test_a4_wine_pin_change_on_launch_stops_the_old_wineserver(xdg: Path, proc: Path, tmp_path: Path) -> None:
    """UPD-4: prefix_init stops the previous build's wineserver with that build's own binary."""
    ctx = make_ctx()
    ctx.proc_root = proc
    ctx.paths.prefix.mkdir(parents=True, exist_ok=True)
    old = _managed_build(ctx, "wine-10.0-old")
    new_info = ctx.wine()
    _wineserver_process(proc, 4001, ctx.paths.prefix, (old / "bin" / "wineserver").resolve())
    _wineserver_process(proc, 4002, ctx.paths.prefix, (old / "bin" / "wineserver").resolve())  # listed once
    _wineserver_process(proc, 4003, ctx.paths.prefix, new_info.wineserver)  # the current build: left alone
    foreign = tmp_path / "opt" / "wine" / "bin" / "wineserver"
    foreign.parent.mkdir(parents=True)
    foreign.write_text("#!/bin/sh\n", encoding="utf-8")
    _wineserver_process(proc, 4004, ctx.paths.prefix, foreign)  # not ours: never run
    _wineserver_process(proc, 4005, tmp_path / "other-prefix", (old / "bin" / "wineserver").resolve())
    stopped = prefix.stop_other_wineservers(ctx, new_info)
    assert stopped == [(old / "bin" / "wineserver").resolve()]
    (call,) = ctx.runner.calls
    assert call["argv"] == [str((old / "bin" / "wineserver").resolve()), "-k"]
    assert call["env"]["WINEPREFIX"] == str(ctx.paths.prefix)
    assert call["env"]["WINESERVER"] == call["argv"][0]
    assert call["env"]["WINELOADER"] == str(old.resolve() / "bin" / "wine")


def test_a4_prefix_init_stops_the_old_server_before_wineboot(xdg: Path, proc: Path) -> None:
    ctx = make_ctx()
    ctx.proc_root = proc
    ctx.paths.prefix.mkdir(parents=True, exist_ok=True)
    old = _managed_build(ctx, "wine-10.0-old")
    _wineserver_process(proc, 4101, ctx.paths.prefix, (old / "bin" / "wineserver").resolve())
    prefix.run_prefix_init(ctx)
    first, second = ctx.runner.calls[:2]
    assert first["argv"] == [str((old / "bin" / "wineserver").resolve()), "-k"]
    assert second["argv"][1:] == ["wineboot", "--init"]


def test_a4_nothing_to_stop(xdg: Path, proc: Path) -> None:
    ctx = make_ctx()
    ctx.proc_root = proc
    assert prefix.stop_other_wineservers(ctx, ctx.wine()) == []  # no prefix yet
    ctx.paths.prefix.mkdir(parents=True, exist_ok=True)
    old = _managed_build(ctx, "wine-10.0-old")
    odd = old / "lib" / "wineserver"
    odd.parent.mkdir()
    odd.write_text("#!/bin/sh\n", encoding="utf-8")
    _wineserver_process(proc, 4201, ctx.paths.prefix, odd.resolve())  # not <root>/bin/wineserver
    assert prefix.stop_other_wineservers(ctx, ctx.wine()) == []
    assert ctx.runner.calls == []
    assert os.path.isdir(ctx.paths.prefix)


# -- A8: downgrade N -> N-1 ------------------------------------------------------------------


def _downgrade_warnings(ctx: Ctx) -> list[str]:
    assert isinstance(ctx.ui, FakeUI)
    return [message for message in ctx.ui.kinds("warn") if "set this prefix up last" in message]


def test_a8_state_from_a_newer_release_warns_and_keeps_working(xdg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """UPD-5: state.json names its last writer; an older release warns once and goes on."""
    ctx = make_ctx()
    install_fork(ctx.layout, "2.23.2")
    monkeypatch.setattr(version_mod, "get_version", lambda: "9.9.9")
    ctx.state.save()
    assert json.loads(ctx.state.path.read_text(encoding="utf-8"))[WRITTEN_BY] == "9.9.9"
    monkeypatch.setattr(version_mod, "get_version", lambda: "0.9.9")
    ctx.state = State.load(ctx.state.path)
    assert ctx.state.previous_writer == "9.9.9"
    notes = launcher.pre_launch(ctx)
    (warning,) = _downgrade_warnings(ctx)
    assert warning == (
        "fork-linux 0.9.9 is older than 9.9.9, which set this prefix up last; it keeps working, "
        "but install fork-linux 9.9.9 or newer again if something misbehaves"
    )
    assert warning in notes
    assert any("snapshot" in note for note in notes), "the other hooks still ran"
    ctx.state = State.load(ctx.state.path)
    assert ctx.state.previous_writer == "0.9.9"
    launcher.pre_launch(ctx)
    assert len(_downgrade_warnings(ctx)) == 1, "warned once only"


@pytest.mark.parametrize(
    ("writer", "ours"),
    [(None, "1.0.0"), ("1.0.0", "1.0.0"), ("0.9.9", "1.0.0"), ("1.0.0", "1.0.0-dev"), ("garbage", "1.0.0"),
     ("9.9.9", version_mod.UNKNOWN), (version_mod.UNKNOWN, "1.0.0")],
)
def test_a8_no_warning_without_a_downgrade(
    xdg: Path, monkeypatch: pytest.MonkeyPatch, writer: str | None, ours: str
) -> None:
    ctx = make_ctx()
    ctx.state.previous_writer = writer
    monkeypatch.setattr(version_mod, "get_version", lambda: ours)
    notes: list[str] = []
    launcher._note_downgrade(ctx, notes)
    assert notes == []
    assert _downgrade_warnings(ctx) == []


# -- A3: a step revision bump between N-1 and N ----------------------------------------------


def test_a3_revision_bump_reruns_only_changed_steps(xdg: Path) -> None:
    """Markers written by N-1; N bumps one step's revision: only that step is pending, nothing else."""
    ctx = make_ctx()
    install_fork(ctx.layout, "2.23.2")
    released = [step for step in bootstrap.default_steps() if not step.always]
    for step in released:
        ctx.state.set_step_marker(step.id, step.rev, bootstrap.inputs_hash(step, ctx))
    ctx.state.set(bootstrap.SETUP_COMPLETE, True)
    ctx.state.set(bootstrap.BOOTSTRAP_REVISION, ctx.manifest.bootstrap_revision)
    ctx.state.set(bootstrap.MANIFEST_REVISION, ctx.manifest.revision)
    assert [step.id for step in released if not bootstrap.is_done(step, ctx, cheap=True)] == []
    bumped = [dataclasses.replace(step, rev=step.rev + 1) if step.id == "host_shims" else step for step in released]
    assert [step.id for step in bumped if not bootstrap.is_done(step, ctx, cheap=True)] == ["host_shims"]
    assert bootstrap.needs_setup(ctx, bumped)
    assert not bootstrap.needs_setup(ctx, [step for step in bumped if step.id != "host_shims"])


# -- A11: uninstall (no purge), then reinstall ------------------------------------------------


def _set_up(ctx: Ctx, fork_version: str = "2.23.2") -> list[bootstrap.Step]:
    """Fork ``fork_version`` and markers for every step, as a completed setup leaves them; returns the steps."""
    install_fork(ctx.layout, fork_version)
    released = [step for step in bootstrap.default_steps() if not step.always]
    for step in released:
        ctx.state.set_step_marker(step.id, step.rev, bootstrap.inputs_hash(step, ctx))
    ctx.state.set(bootstrap.SETUP_COMPLETE, True)
    ctx.state.set(bootstrap.BOOTSTRAP_REVISION, ctx.manifest.bootstrap_revision)
    ctx.state.set(bootstrap.MANIFEST_REVISION, ctx.manifest.revision)
    ctx.state.save()
    return released


def test_a11_uninstall_reinstall_keeps_the_prefix(
    xdg: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The prefix and Fork stay; only the integration steps run again after the reinstall."""
    isolate(monkeypatch)
    fork_running(monkeypatch, False)
    ctx = make_ctx()
    released = _set_up(ctx)
    exe = ctx.layout.exe.read_bytes()
    code, out, _err = run_cli(capsys, "uninstall")
    assert code == 0, out
    assert "Fork, its Wine prefix and your settings were kept" in out
    after = make_ctx()  # the reinstalled release reads the same prefix
    assert after.layout.exe.read_bytes() == exe
    assert after.state.get(bootstrap.SETUP_COMPLETE) is True
    pending = [step.id for step in released if not bootstrap.is_done(step, after, cheap=True)]
    assert pending == list(bootstrap.INTEGRATION_STEPS)
    assert all(not step.requires_fork_closed for step in released if step.id in pending)


def test_forget_markers_edge_cases(xdg: Path, caplog: pytest.LogCaptureFixture) -> None:
    ctx = make_ctx()
    assert bootstrap.forget_markers(ctx.paths) == []  # no state file yet
    ctx.state.save()
    before = ctx.paths.state_file.read_bytes()
    assert bootstrap.forget_markers(ctx.paths) == []  # no markers: nothing written
    assert ctx.paths.state_file.read_bytes() == before
    _set_up(ctx)
    with FileLock(ctx.paths.lock_file, "setup"):
        assert bootstrap.forget_markers(ctx.paths) == []
    assert "could not reset the setup steps icon, desktop_entry" in caplog.text
    assert bootstrap.forget_markers(ctx.paths, ["icon"]) == ["icon"]
    assert State.load(ctx.paths.state_file).step_marker("desktop_entry") is not None


# -- A5 / A6: the package is upgraded while Fork runs ----------------------------------------


@pytest.fixture
def chosen(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """The steps each ensure_ready hands to run_steps (which runs the pending ones among them)."""
    seen: list[list[str]] = []

    def run_steps(ctx: Ctx, steps: list[bootstrap.Step] | None = None, **_kw: object) -> list[str]:
        seen.append([step.id for step in steps or []])
        return []

    monkeypatch.setattr(bootstrap, "run_steps", run_steps)
    return seen


def _running(monkeypatch: pytest.MonkeyPatch, value: bool) -> None:
    monkeypatch.setattr(bootstrap.procs, "fork_running", lambda prefix, **_kw: value)


def test_a5_upgrade_while_fork_runs(xdg: Path, monkeypatch: pytest.MonkeyPatch, chosen: list[list[str]]) -> None:
    """N bumps a step that needs Fork closed and one that does not: the first waits for Fork to close."""
    ctx = make_ctx()
    released = _set_up(ctx)
    bumped = [
        dataclasses.replace(step, rev=step.rev + 1) if step.id in ("host_shims", "desktop_entry") else step
        for step in released
    ]
    assert next(step for step in bumped if step.id == "host_shims").requires_fork_closed
    _running(monkeypatch, True)
    bootstrap.ensure_ready(ctx, steps=bumped)
    assert "host_shims" not in chosen[-1]
    assert "desktop_entry" in chosen[-1]
    _running(monkeypatch, False)
    bootstrap.ensure_ready(ctx, steps=bumped)
    assert {"host_shims", "desktop_entry"} <= set(chosen[-1])


def test_a6_new_shims_rerun_host_shims_after_fork_closes(
    xdg: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, chosen: list[list[str]]
) -> None:
    """New bridge binaries in the package change host_shims' inputs; the running Fork keeps the old ones."""
    shipped = tmp_path / "shims"
    shipped.mkdir()
    (shipped / "fl-shim.exe").write_bytes(b"MZ shim 0.9.9")
    (shipped / "fl-launch.exe").write_bytes(b"MZ launch 0.9.9")
    monkeypatch.setenv(resources.SHIMS_ENV, str(shipped))
    ctx = make_ctx()
    _set_up(ctx)
    host_shims = next(step for step in bootstrap.default_steps() if step.id == "host_shims")
    assert bootstrap.is_done(host_shims, ctx, cheap=True)
    (shipped / "fl-shim.exe").write_bytes(b"MZ shim 1.0.0")  # the package manager replaced it
    assert not bootstrap.is_done(host_shims, ctx, cheap=True)
    _running(monkeypatch, True)
    bootstrap.ensure_ready(ctx)
    assert "host_shims" not in chosen[-1]
    _running(monkeypatch, False)
    bootstrap.ensure_ready(ctx)
    assert "host_shims" in chosen[-1]


# -- A7: a state.json written by a newer schema ----------------------------------------------


def test_a7_launch_with_a_newer_state_schema_changes_nothing(
    xdg: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    isolate(monkeypatch)
    fork_running(monkeypatch, False)
    ctx = make_ctx()
    install_fork(ctx.layout, "2.23.2")
    ctx.paths.state_file.parent.mkdir(parents=True, exist_ok=True)
    ctx.paths.state_file.write_text(json.dumps({"schema": 99, "steps": {}}), encoding="utf-8")
    before = ctx.paths.state_file.read_bytes()
    code, _out, err = run_cli(capsys, "run")
    assert code == 13
    assert "uses schema 99, newer than this fork-linux" in err
    assert "update fork-linux" in err
    assert ctx.paths.state_file.read_bytes() == before
    assert not ctx.paths.snapshots_dir.exists()
    assert not ctx.paths.session_file.exists()


# -- A12: our upgrade and Fork's self-update before the same launch --------------------------


def test_a12_app_upgrade_and_fork_self_update_on_one_launch(
    xdg: Path, monkeypatch: pytest.MonkeyPatch, chosen: list[list[str]]
) -> None:
    ctx = make_ctx()
    released = _set_up(ctx, "2.23.1")  # N-1 set up with Fork 2.23.1 ...
    install_fork(ctx.layout, "2.23.2")  # ... then Fork updated itself
    ctx.state.set(updates.LAST_SEEN_VERSION, "2.23.1")
    bumped = [dataclasses.replace(s, rev=s.rev + 1) if s.id == "git_overlay" else s for s in released]
    _running(monkeypatch, False)
    bootstrap.ensure_ready(ctx, steps=bumped)
    assert "git_overlay" in chosen[-1]
    notes = launcher.pre_launch(ctx)
    assert "Fork updated 2.23.1 -> 2.23.2; 'fork-linux rollback' restores 2.23.1" in notes
    assert any(note.startswith("snapshot 2.23.2-") for note in notes)
    assert launcher.pre_launch(ctx).count("Fork updated 2.23.1 -> 2.23.2; 'fork-linux rollback' restores 2.23.1") == 0


# -- A22: a package upgrade lands in the middle of a long first setup ------------------------


def test_a22_step_failing_on_new_modules_is_resumed_by_the_new_release(xdg: Path) -> None:
    """N's files replaced N-1's under a running setup: the step fails cleanly and N resumes it."""
    ctx = make_ctx()
    attempts: list[str] = []

    def mixed_versions(_ctx: Ctx) -> None:
        attempts.append("n-1")
        raise ImportError("cannot import name 'new_helper' from 'fork_linux.fsutil'")

    step = bootstrap.Step(id="fonts", title="Fonts", rev=1, weight=1, run=mixed_versions, verify=lambda _c: True)
    with pytest.raises(SetupFailed, match="ImportError: cannot import name 'new_helper'") as exc:
        bootstrap.run_steps(ctx, [step])
    assert exc.value.step == "fonts"
    assert ctx.state.step_marker("fonts") is None
    fixed = dataclasses.replace(step, run=lambda _c: attempts.append("n"))
    assert bootstrap.run_steps(make_ctx(), [fixed]) == ["fonts"]
    assert attempts == ["n-1", "n"]


# -- A9: the AppImage file is moved ----------------------------------------------------------


def test_a9_moved_appimage_repoints_the_menu_entry_links_and_actions(xdg: Path, tmp_path: Path) -> None:
    """desktop_entry's input is the command; when the AppImage moves, everything recorded follows it."""
    old_image = tmp_path / "Downloads" / "fork-linux.AppImage"
    new_image = tmp_path / "Applications" / "Fork Linux.AppImage"
    for image in (old_image, new_image):
        image.parent.mkdir(parents=True)
        image.write_text("#!/bin/sh\n", encoding="utf-8")
        image.chmod(0o755)
    env = {**os.environ, "PATH": str(tmp_path / "empty-path"), desktop_integration.APPIMAGE_ENV: str(old_image)}
    runner = RecordingRunner(which_map={"update-desktop-database": None, "gtk-update-icon-cache": None})
    ctx = make_ctx(runner, env=env)
    desktop_integration.install(ctx.paths, env, file_managers="nautilus,dolphin", cli_alias=True, runner=runner)
    dirs = desktop_integration._Dirs.from_env(env)
    actions = [dirs.kind_path(desktop_integration.NAUTILUS_SCRIPT), dirs.kind_path(desktop_integration.DOLPHIN)]
    before = integration.desktop_inputs(ctx)
    moved = {**env, desktop_integration.APPIMAGE_ENV: str(new_image)}
    ctx = make_ctx(runner, env=moved)
    assert integration.desktop_inputs(ctx) != before, "the step re-runs after the move"
    integration.run_desktop(ctx)
    for path in [dirs.menu_file, *actions]:
        text = path.read_text(encoding="utf-8")
        assert str(old_image) not in text, path
        assert "Fork Linux.AppImage" in text, path
    for name in desktop_integration.CLI_ALIASES:
        assert os.readlink(dirs.bin / name) == str(new_image)
