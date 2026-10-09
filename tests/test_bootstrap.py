"""Tests for the setup engine (fork_linux.bootstrap): markers, resume, selection, locking, failures."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

import pytest
from fixtures.setup_ctx import USER, FakeUI, make_ctx, wine_info

from fork_linux import bootstrap, steps
from fork_linux import manifest as manifest_mod
from fork_linux import state as state_mod
from fork_linux import ui as ui_mod
from fork_linux.bootstrap import Ctx, Step
from fork_linux.errors import (
    DownloadFailed,
    ForkLinuxError,
    ForkRunning,
    Locked,
    NotSetUpError,
    SetupFailed,
    UsageError,
)
from fork_linux.fork_layout import ForkLayout
from fork_linux.locking import FileLock
from fork_linux.pathmap import PathMap
from fork_linux.paths import Paths
from fork_linux.procrun import RecordingRunner
from fork_linux.winecmd import WineInfo


@pytest.fixture
def ctx(xdg: Path, tmp_path: Path) -> Ctx:
    proc = tmp_path / "proc"
    proc.mkdir()
    return make_ctx(proc_root=proc)


class Recorder:
    """Synthetic steps that log their runs; ``verify`` follows ``ok``."""

    def __init__(self) -> None:
        self.runs: list[str] = []
        self.ok: dict[str, bool] = {}
        self.values: dict[str, Any] = {}

    def step(self, step_id: str, *, weight: int = 1, always: bool = False, closed: bool = True,
             error: BaseException | None = None, rev: int = 1) -> Step:
        def run(_ctx: Ctx) -> None:
            self.runs.append(step_id)
            if error is not None:
                raise error

        return Step(
            id=step_id,
            title=f"Doing {step_id}",
            rev=rev,
            weight=weight,
            run=run,
            verify=lambda _ctx: self.ok.get(step_id, True),
            inputs=lambda _ctx: {"value": self.values.get(step_id)},
            requires_fork_closed=closed,
            always=always,
        )


@pytest.fixture
def rec() -> Recorder:
    return Recorder()


def _ids(steps_list: list[Step]) -> list[str]:
    return [step.id for step in steps_list]


# -- Ctx -------------------------------------------------------------------------------------


def _app(xdg: Path, *, args: dict[str, Any] | None = None, env: dict[str, str] | None = None) -> Any:
    from fork_linux import cli

    namespace = argparse.Namespace(json=False, gui=False, offline=False, verbose=0, quiet=0, prefix=None)
    for key, value in (args or {}).items():
        setattr(namespace, key, value)
    environ = dict(os.environ)
    environ["USER"] = USER
    environ.update(env or {})
    return cli.AppContext(namespace, env=environ, runner=RecordingRunner())


def test_from_app_builds_everything(xdg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    chosen: list[dict[str, Any]] = []

    def choose(env: Any, **kwargs: Any) -> ui_mod.UI:
        chosen.append(kwargs)
        return ui_mod.NullUI()

    monkeypatch.setattr(ui_mod, "choose", choose)
    app = _app(xdg, args={"allow_root": True, "offline": True})
    ctx = Ctx.from_app(app, accept_eula=True, fork_version="2.23.2")
    assert isinstance(ctx.ui, ui_mod.NullUI)
    assert chosen[0]["mode"] == "auto" and chosen[0]["gui"] is False
    assert ctx.user == USER and ctx.accept_eula and ctx.fork_version == "2.23.2"
    assert ctx.allow_root is True and ctx.offline is True
    assert ctx.log_file is not None and ctx.log_file.parent == ctx.paths.logs_dir
    assert ctx.log_file.name.startswith("setup-")
    assert ctx.state.is_new
    assert ctx.manifest.fork_default == manifest_mod.load().fork_default


def test_from_app_keeps_given_ui_and_log_and_reads_root_env(xdg: Path, tmp_path: Path) -> None:
    given = FakeUI()
    app = _app(xdg, env={bootstrap.ALLOW_ROOT_ENV: "1"})
    ctx = Ctx.from_app(app, ui=given, log_file=tmp_path / "x.log")
    assert ctx.ui is given and ctx.log_file == tmp_path / "x.log"
    assert ctx.allow_root is True
    plain = Ctx.from_app(_app(xdg), ui=given)
    assert plain.allow_root is False


def test_host_home(ctx: Ctx, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    assert ctx.host_home == Path(os.environ["HOME"])
    ctx.env.pop("HOME")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert ctx.host_home == tmp_path


def test_wine_is_resolved_lazily_and_cached(ctx: Ctx, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []
    info = wine_info(Path("/opt/wine"))

    def resolve(*args: Any, **kwargs: Any) -> WineInfo:
        calls.append(kwargs)
        return info

    monkeypatch.setattr(bootstrap.wine_provider, "resolve", resolve)
    ctx.reset_wine()
    ctx.wine_choice = "system"
    assert ctx.wine() is info and ctx.wine() is info
    assert len(calls) == 1 and calls[0]["install"] is False and calls[0]["choice"] == "system"
    assert ctx.wine_provider_choice() == "system"
    ctx.wine_choice = None
    assert ctx.wine_provider_choice() == "managed"
    other = wine_info(Path("/opt/other"))
    ctx.set_wine(other)
    assert ctx.wine() is other


def test_wine_env_layout_and_pathmap(ctx: Ctx) -> None:
    env = ctx.wine_env(dll_overrides="mscoree=")
    assert env["WINEPREFIX"] == str(ctx.paths.prefix)
    assert env["WINEDLLOVERRIDES"] == "mscoree=;winemenubuilder.exe=d"
    layout = ctx.layout
    assert isinstance(layout, ForkLayout) and ctx.layout is layout and layout.user == USER
    dosdevices = ctx.paths.prefix / "dosdevices"
    dosdevices.mkdir(parents=True)
    (dosdevices / "z:").symlink_to("/")
    assert isinstance(ctx.pathmap, PathMap)
    assert ctx.pathmap.unix_to_win("/tmp") == "Z:\\tmp"


# -- markers -------------------------------------------------------------------------------


def test_default_steps_are_the_steps_package() -> None:
    assert _ids(bootstrap.default_steps()) == list(steps.STEP_IDS)
    assert steps.get("dotnet").id == "dotnet"
    with pytest.raises(KeyError):
        steps.get("nope")


def test_inputs_hash_depends_on_inputs_and_rev(ctx: Ctx, rec: Recorder) -> None:
    one = rec.step("a")
    first = bootstrap.inputs_hash(one, ctx)
    assert first == bootstrap.inputs_hash(one, ctx) and len(first) == 64
    rec.values["a"] = 5
    assert bootstrap.inputs_hash(one, ctx) != first
    assert bootstrap.inputs_hash(rec.step("a", rev=2), ctx) != bootstrap.inputs_hash(one, ctx)
    plain = Step(id="p", title="p", rev=1, weight=1, run=lambda c: None, verify=lambda c: True)
    assert plain.inputs(ctx) == {}


def test_is_done_checks_marker_rev_inputs_and_verify(ctx: Ctx, rec: Recorder) -> None:
    step = rec.step("a")
    assert not bootstrap.is_done(step, ctx)
    ctx.state.set_step_marker("a", 2, bootstrap.inputs_hash(step, ctx))
    assert not bootstrap.is_done(step, ctx)
    ctx.state.set_step_marker("a", 1, "0" * 64)
    assert not bootstrap.is_done(step, ctx)
    bootstrap.mark_done(step, ctx)
    assert bootstrap.is_done(step, ctx)
    rec.ok["a"] = False
    assert not bootstrap.is_done(step, ctx)
    assert bootstrap.is_done(step, ctx, cheap=True)
    assert not bootstrap.is_done(rec.step("b", always=True), ctx)


def test_verify_that_raises_counts_as_not_done(ctx: Ctx) -> None:
    def boom(_ctx: Ctx) -> bool:
        raise ForkLinuxError("cannot tell")

    step = Step(id="x", title="x", rev=1, weight=1, run=lambda c: None, verify=boom)
    bootstrap.mark_done(step, ctx)
    assert not bootstrap.is_done(step, ctx)


def test_mark_done_with_digest_and_always(ctx: Ctx, rec: Recorder) -> None:
    bootstrap.mark_done(rec.step("a"), ctx, "f" * 64)
    assert ctx.state.step_marker("a")["inputs_hash"] == "f" * 64
    bootstrap.mark_done(rec.step("b", always=True), ctx)
    assert ctx.state.step_marker("b") is None


def test_pending_and_clear_markers(ctx: Ctx, rec: Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    listed = [rec.step("pre", always=True), rec.step("a"), rec.step("b")]
    bootstrap.mark_done(listed[1], ctx)
    assert _ids(bootstrap.pending(ctx, listed)) == ["pre", "b"]
    monkeypatch.setattr(bootstrap, "default_steps", lambda: listed)
    assert _ids(bootstrap.pending(ctx)) == ["pre", "b"]
    bootstrap.mark_done(listed[2], ctx)
    bootstrap.clear_markers(ctx, keep=["b"])
    assert ctx.state.step_marker("a") is None and ctx.state.step_marker("b") is not None


def test_init_prefix_meta_creates_private_prefix_and_marker(ctx: Ctx) -> None:
    bootstrap.init_prefix_meta(ctx)
    assert ctx.paths.created_by_marker.read_text().startswith("fork-linux ")
    assert (ctx.paths.prefix.stat().st_mode & 0o777) == 0o700
    ctx.paths.created_by_marker.write_text("fork-linux 0.0.1\n")
    bootstrap.init_prefix_meta(ctx)
    assert ctx.paths.created_by_marker.read_text() == "fork-linux 0.0.1\n"


# -- run_step -------------------------------------------------------------------------------


def test_run_step_success_records_marker_and_saves(ctx: Ctx, rec: Recorder) -> None:
    bootstrap.init_prefix_meta(ctx)
    bootstrap.run_step(ctx, rec.step("a"))
    saved = json.loads(ctx.paths.state_file.read_text())
    assert saved["steps"]["a"]["rev"] == 1
    bootstrap.run_step(ctx, rec.step("pre", always=True))
    assert "pre" not in json.loads(ctx.paths.state_file.read_text())["steps"]
    assert rec.runs == ["a", "pre"]


@pytest.mark.parametrize(
    ("error", "expected", "same"),
    [
        (SetupFailed("a", "inner"), SetupFailed, True),
        (DownloadFailed("offline"), DownloadFailed, True),
        (ForkLinuxError("plain", hint="do this"), SetupFailed, False),
        (OSError("disk on fire"), SetupFailed, False),
    ],
)
def test_run_step_failures_record_last_error(
    ctx: Ctx, rec: Recorder, error: BaseException, expected: type, same: bool
) -> None:
    bootstrap.init_prefix_meta(ctx)
    ctx.log_file = ctx.paths.logs_dir / "setup.log"
    with pytest.raises(expected) as caught:
        bootstrap.run_step(ctx, rec.step("a", error=error))
    assert (caught.value is error) is same
    saved = state_mod.State.load(ctx.paths.state_file)
    last = saved.get(bootstrap.LAST_ERROR)
    assert last["step"] == "a" and last["log"] == str(ctx.log_file)
    assert saved.get("setup.complete") is False
    assert saved.step_marker("a") is None
    if expected is SetupFailed and not same:
        assert caught.value.step == "a"
        assert "setup.log" in caught.value.hint and "resume" in caught.value.hint
    if isinstance(error, ForkLinuxError) and not same:
        assert caught.value.hint.startswith("do this")


def test_run_step_verify_failure(ctx: Ctx, rec: Recorder) -> None:
    bootstrap.init_prefix_meta(ctx)
    rec.ok["a"] = False
    with pytest.raises(SetupFailed, match="could not be verified") as caught:
        bootstrap.run_step(ctx, rec.step("a"))
    assert caught.value.hint == "run 'fork-linux setup' again to resume at this step"
    assert ctx.state.get(bootstrap.LAST_ERROR)["log"] is None


def test_failure_is_recorded_even_when_saving_fails(ctx: Ctx, rec: Recorder) -> None:
    # A directory where state.json belongs: saving fails, the original error still wins.
    ctx.paths.state_file.mkdir(parents=True)
    rec.ok["a"] = False
    with pytest.raises(SetupFailed, match="could not be verified"):
        bootstrap.run_step(ctx, rec.step("a"))
    assert ctx.paths.state_file.is_dir()


def test_keyboard_interrupt_leaves_no_marker(ctx: Ctx, rec: Recorder) -> None:
    bootstrap.init_prefix_meta(ctx)
    with pytest.raises(KeyboardInterrupt):
        bootstrap.run_step(ctx, rec.step("a", error=KeyboardInterrupt()))
    assert ctx.state.step_marker("a") is None


# -- run_steps ------------------------------------------------------------------------------


def test_run_steps_runs_pending_in_order_with_progress(ctx: Ctx, rec: Recorder) -> None:
    listed = [rec.step("pre", always=True), rec.step("a", weight=3), rec.step("b", weight=6)]
    ran = bootstrap.run_steps(ctx, listed)
    assert ran == ["pre", "a", "b"]
    assert ctx.state.get("setup.complete") is True
    assert ctx.state.get(bootstrap.BOOTSTRAP_REVISION) == ctx.manifest.bootstrap_revision
    assert ctx.state.get(bootstrap.MANIFEST_REVISION) == ctx.manifest.revision
    assert ctx.state.get(bootstrap.LAST_ERROR) is None
    updates = ctx.ui.kinds("update")
    assert updates == [(0.0, "Doing pre"), (0.1, "Doing a"), (0.4, "Doing b"), (1.0, "done")]
    assert ctx.ui.kinds("finish") == [False]
    assert ctx.paths.created_by_marker.is_file()
    # Resume: nothing but the always step runs again.
    rec.runs.clear()
    assert bootstrap.run_steps(ctx, listed) == ["pre"]


def test_run_steps_resumes_at_the_failed_step(ctx: Ctx, rec: Recorder) -> None:
    good, bad = rec.step("a"), rec.step("b", error=ForkLinuxError("nope"))
    with pytest.raises(SetupFailed):
        bootstrap.run_steps(ctx, [good, bad])
    assert ctx.ui.kinds("finish") == [True]
    fixed = rec.step("b")
    rec.runs.clear()
    assert bootstrap.run_steps(ctx, [good, fixed]) == ["b"]


def test_run_steps_without_work_still_completes(ctx: Ctx, rec: Recorder) -> None:
    listed = [rec.step("a")]
    bootstrap.mark_done(listed[0], ctx)
    assert bootstrap.run_steps(ctx, listed) == []
    assert ctx.ui.kinds("update") == [(1.0, "done")]
    assert ctx.state.get("setup.complete") is True


def test_run_steps_selection(ctx: Ctx, rec: Recorder) -> None:
    listed = [rec.step("pre", always=True), rec.step("a"), rec.step("b"), rec.step("c")]
    for step in listed[1:]:
        bootstrap.mark_done(step, ctx)
    assert bootstrap.run_steps(ctx, listed, only=["b"]) == ["pre", "b"]
    assert bootstrap.run_steps(ctx, listed, from_step="b") == ["pre", "b", "c"]
    assert bootstrap.run_steps(ctx, listed, force=True) == ["pre", "a", "b", "c"]
    ctx.force = True
    assert bootstrap.run_steps(ctx, listed) == ["pre", "a", "b", "c"]
    with pytest.raises(UsageError, match="unknown setup step"):
        bootstrap.run_steps(ctx, listed, only=["zzz"])
    with pytest.raises(UsageError, match="yyy"):
        bootstrap.run_steps(ctx, listed, from_step="yyy")


def test_run_steps_partial_selection_is_not_complete(ctx: Ctx, rec: Recorder) -> None:
    listed = [rec.step("a"), rec.step("b")]
    assert bootstrap.run_steps(ctx, listed, only=["a"]) == ["a"]
    assert ctx.state.get("setup.complete") is False


def test_run_steps_reruns_a_step_whose_marker_was_cleared(ctx: Ctx, rec: Recorder) -> None:
    later = rec.step("later")
    bootstrap.mark_done(later, ctx)

    def rebuild(c: Ctx) -> None:
        rec.runs.append("rebuild")
        bootstrap.clear_markers(c)

    first = Step(id="rebuild", title="rebuild", rev=1, weight=1, run=rebuild, verify=lambda c: True)
    assert bootstrap.run_steps(ctx, [first, later]) == ["rebuild", "later"]


def _fork_process(proc: Path, prefix: Path) -> None:
    entry = proc / "4242"
    entry.mkdir()
    (entry / "environ").write_bytes(b"WINEPREFIX=" + str(prefix).encode() + b"\0")
    (entry / "cmdline").write_bytes(b"wine\0C:\\users\\tester\\AppData\\Local\\Fork\\current\\Fork.exe\0")


def test_run_steps_refuses_while_fork_runs(ctx: Ctx, rec: Recorder) -> None:
    ctx.paths.prefix.mkdir(parents=True)
    _fork_process(ctx.proc_root, ctx.paths.prefix)
    with pytest.raises(ForkRunning):
        bootstrap.run_steps(ctx, [rec.step("a")])
    assert rec.runs == []
    # Steps that do not need Fork closed still run.
    assert bootstrap.run_steps(ctx, [rec.step("b", closed=False)]) == ["b"]


def test_run_steps_holds_the_lock(ctx: Ctx, rec: Recorder) -> None:
    with FileLock(ctx.paths.lock_file, "update"), pytest.raises(Locked):
        bootstrap.run_steps(ctx, [rec.step("a")])

    seen: list[bool] = []

    def probe(c: Ctx) -> None:
        try:
            FileLock(c.paths.lock_file, "other").acquire()
        except Locked:
            seen.append(True)

    step = Step(id="probe", title="probe", rev=1, weight=1, run=probe, verify=lambda c: True)
    bootstrap.run_steps(ctx, [step])
    assert seen == [True]


# -- needs_setup / ensure_ready -------------------------------------------------------------------


def _complete(ctx: Ctx, rec: Recorder) -> list[Step]:
    listed = [rec.step("pre", always=True), rec.step("a")]
    bootstrap.run_steps(ctx, listed)
    return listed


def test_needs_setup(ctx: Ctx, rec: Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    listed = [rec.step("pre", always=True), rec.step("a")]
    assert bootstrap.needs_setup(ctx, listed)
    bootstrap.run_steps(ctx, listed)
    assert not bootstrap.needs_setup(ctx, listed)
    rec.values["a"] = "changed"
    assert bootstrap.needs_setup(ctx, listed)
    rec.values["a"] = None
    ctx.state.set(bootstrap.MANIFEST_REVISION, "2000.1.1")
    assert bootstrap.needs_setup(ctx, listed)
    ctx.state.set(bootstrap.MANIFEST_REVISION, ctx.manifest.revision)
    ctx.state.set(bootstrap.BOOTSTRAP_REVISION, 0)
    assert bootstrap.needs_setup(ctx, listed)
    ctx.state.set(bootstrap.BOOTSTRAP_REVISION, ctx.manifest.bootstrap_revision)
    monkeypatch.setattr(bootstrap, "default_steps", lambda: listed)
    assert not bootstrap.needs_setup(ctx)


def test_ensure_ready(ctx: Ctx, rec: Recorder) -> None:
    listed = [rec.step("a")]
    with pytest.raises(NotSetUpError, match="not set up") as caught:
        bootstrap.ensure_ready(ctx, allow=False, steps=listed)
    assert "fork-linux setup" in caught.value.hint
    bootstrap.ensure_ready(ctx, steps=listed)
    assert rec.runs == ["a"]
    bootstrap.ensure_ready(ctx, allow=False, steps=listed)
    assert rec.runs == ["a"]


def test_ensure_ready_defers_closed_steps_while_fork_runs(ctx: Ctx, rec: Recorder) -> None:
    listed = [rec.step("pre", always=True, closed=False), rec.step("a"), rec.step("b", closed=False)]
    bootstrap.run_steps(ctx, listed)
    # Fork is closed: nothing is deferred.
    rec.values["b"] = "first"
    bootstrap.ensure_ready(ctx, steps=listed)
    assert rec.runs == ["pre", "a", "b", "pre", "b"]
    rec.runs.clear()
    rec.values["a"] = "changed"
    rec.values["b"] = "changed"
    _fork_process(ctx.proc_root, ctx.paths.prefix)
    bootstrap.ensure_ready(ctx, steps=listed)
    assert rec.runs == ["pre", "b"]
    assert not bootstrap.is_done(listed[1], ctx, cheap=True)
    assert bootstrap.needs_setup(ctx, listed)
    # Nothing that needs Fork closed is pending: the normal path.
    rec.runs.clear()
    rec.values["a"] = None
    rec.values["b"] = "again"
    bootstrap.ensure_ready(ctx, steps=listed)
    assert rec.runs == ["pre", "b"]


def test_ensure_ready_before_setup_completed_refuses_while_fork_runs(ctx: Ctx, rec: Recorder) -> None:
    ctx.paths.prefix.mkdir(parents=True)
    _fork_process(ctx.proc_root, ctx.paths.prefix)
    with pytest.raises(ForkRunning):
        bootstrap.ensure_ready(ctx, steps=[rec.step("a")])


def test_update_completion_uses_default_steps(ctx: Ctx, rec: Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    listed = [rec.step("a")]
    monkeypatch.setattr(bootstrap, "default_steps", lambda: listed)
    assert bootstrap.update_completion(ctx) is False
    bootstrap.mark_done(listed[0], ctx)
    assert bootstrap.update_completion(ctx) is True
    assert bootstrap.run_steps(ctx) == []


def test_ctx_dataclass_paths_are_xdg(ctx: Ctx) -> None:
    assert isinstance(ctx.paths, Paths)
    assert ctx.paths.prefix == Path(os.environ["XDG_DATA_HOME"]) / "fork-linux" / "prefix"
