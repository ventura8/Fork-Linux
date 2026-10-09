"""The setup engine: an ordered list of resumable steps, each with a completion marker.

A :class:`Step` knows how to ``run`` itself, how to ``verify`` that its effect
is in place, and which ``inputs`` it depends on. After a step runs and
verifies, :func:`run_steps` records a marker (step revision + hash of its
inputs) in the prefix's ``state.json``; a later run skips the step while the
marker matches and ``verify`` still passes, so a failed setup resumes at the
failed step and a changed input (a new Wine build, another DPI) re-runs exactly
the steps that depend on it.

Steps live in :mod:`fork_linux.steps` (imported lazily: they import this
module). Setup holds the ``setup`` lock for its whole duration and refuses to
change the prefix while Fork runs in it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import fsutil, procs, wine_provider, winecmd
from . import manifest as manifest_mod
from . import ui as ui_mod
from .config import Config
from .errors import ExitCode, ForkLinuxError, NotSetUpError, SetupFailed, UsageError
from .fork_layout import ForkLayout
from .locking import FileLock
from .manifest import Manifest
from .pathmap import PathMap
from .paths import Paths
from .procrun import Runner
from .state import SETUP_COMPLETE, State
from .version import get_version
from .winecmd import WineInfo

log = logging.getLogger(__name__)

LOCK_PURPOSE = "setup"
PROGRESS_TITLE = "Setting up Fork for Linux (unofficial)"
ALLOW_ROOT_ENV = "FORK_LINUX_ALLOW_ROOT"

LAST_ERROR = "setup.last_error"
BOOTSTRAP_REVISION = "setup.bootstrap_revision"
MANIFEST_REVISION = "setup.manifest_revision"
COMPLETED_AT = "setup.completed_at"


def now() -> str:
    """Local time with offset, to the second."""
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _no_inputs(_ctx: Ctx) -> dict[str, Any]:
    """Inputs of a step that depends on nothing but its revision."""
    return {}


@dataclass
class Ctx:
    """Everything a step needs: where things live, what to run, whom to ask.

    The Wine to use is resolved lazily (:meth:`wine`) once the runtime step
    installed it; :meth:`reset_wine` forgets the cached answer. ``cache`` is
    scratch space steps share within one run (never persisted).
    """

    paths: Paths
    config: Config
    manifest: Manifest
    runner: Runner
    env: dict[str, str]
    ui: ui_mod.UI
    state: State
    user: str
    offline: bool = False
    accept_eula: bool = False
    fork_version: str | None = None
    latest: bool = False
    allow_untested: bool = False
    wine_choice: str | None = None
    dotnet: str = "auto"
    force: bool = False
    log_file: Path | None = None
    allow_root: bool = False
    proc_root: Path = procs.PROC
    cache: dict[str, Any] = field(default_factory=dict)
    _wine: WineInfo | None = field(default=None, repr=False)
    _layout: ForkLayout | None = field(default=None, repr=False)

    @classmethod
    def from_app(cls, app_ctx: Any, **flags: Any) -> Ctx:
        """A context for the CLI's :class:`~fork_linux.cli.AppContext` plus setup ``flags``.

        ``flags`` are field values (``accept_eula=True``, ``fork_version=...``);
        ``ui`` and ``log_file`` may be given too, otherwise the UI comes from
        :func:`ui.choose` and Wine's output goes to ``logs/setup-<time>.log``.
        """
        paths: Paths = app_ctx.paths
        config: Config = app_ctx.config
        env = dict(app_ctx.env)
        chosen_ui = flags.pop("ui", None)
        if chosen_ui is None:
            chosen_ui = ui_mod.choose(
                env, gui=app_ctx.gui, mode=config.get("ui", "progress"), runner=app_ctx.runner
            )
        if flags.get("log_file") is None:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            flags["log_file"] = paths.logs_dir / f"setup-{stamp}.log"
        allow_root = bool(getattr(app_ctx.args, "allow_root", False)) or env.get(ALLOW_ROOT_ENV) == "1"
        flags.setdefault("allow_root", allow_root)
        flags.setdefault("offline", bool(app_ctx.offline))
        return cls(
            paths=paths,
            config=config,
            manifest=manifest_mod.load(override=paths.manifest_override),
            runner=app_ctx.runner,
            env=env,
            ui=chosen_ui,
            state=State.load(paths.state_file),
            user=winecmd.windows_user(env),
            **flags,
        )

    @property
    def host_home(self) -> Path:
        """The user's Linux home directory (``$HOME``)."""
        return Path(self.env.get("HOME") or Path.home())

    def wine_provider_choice(self) -> str:
        """The provider asked for: ``setup --wine`` over ``[wine] provider``."""
        return self.wine_choice or self.config.get("wine", "provider")

    def wine(self) -> WineInfo:
        """The resolved Wine (never installs anything; cached until :meth:`reset_wine`)."""
        if self._wine is None:
            self._wine = wine_provider.resolve(
                self.config,
                self.manifest,
                self.paths,
                self.runner,
                self.env,
                choice=self.wine_choice,
                install=False,
                offline=self.offline,
            )
        return self._wine

    def set_wine(self, info: WineInfo) -> None:
        """Remember ``info`` as the Wine to use (the runtime step just installed it)."""
        self._wine = info

    def reset_wine(self) -> None:
        """Forget the resolved Wine; the next :meth:`wine` resolves it again."""
        self._wine = None

    def wine_env(self, **kwargs: Any) -> dict[str, str]:
        """:func:`winecmd.build_env` for our prefix, user and Wine (``kwargs`` passed on)."""
        return winecmd.build_env(self.paths, self.wine(), user=self.user, base_env=self.env, **kwargs)

    @property
    def layout(self) -> ForkLayout:
        """Fork's files in our prefix for this user."""
        if self._layout is None:
            self._layout = ForkLayout(self.paths, self.user)
        return self._layout

    @property
    def pathmap(self) -> PathMap:
        """The prefix's drive mapping (read fresh: the prefix may have been rebuilt)."""
        return PathMap.from_prefix(self.paths.prefix)


@dataclass(frozen=True)
class Step:
    """One resumable setup step.

    ``rev`` is bumped when the step's behaviour changes (forcing a re-run);
    ``weight`` is its share of the progress bar; ``inputs`` must be cheap and
    offline (it runs on every launch). ``always`` steps keep no marker and
    run every time (preflight, finalize).
    """

    id: str
    title: str
    rev: int
    weight: int
    run: Callable[[Ctx], None]
    verify: Callable[[Ctx], bool]
    inputs: Callable[[Ctx], dict[str, Any]] = _no_inputs
    needs_network: bool = False
    requires_fork_closed: bool = True
    always: bool = False


def default_steps() -> list[Step]:
    """The ordered steps of :mod:`fork_linux.steps` (imported on first use)."""
    from . import steps

    return list(steps.STEPS)


def inputs_hash(step: Step, ctx: Ctx) -> str:
    """sha256 of the canonical JSON of the step's inputs and revision."""
    document = {"inputs": step.inputs(ctx), "rev": step.rev}
    text = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _verified(step: Step, ctx: Ctx) -> bool:
    """``step.verify(ctx)``; a verification that raises counts as failed."""
    try:
        return bool(step.verify(ctx))
    except ForkLinuxError as exc:
        log.info("verifying %s failed: %s", step.id, exc)
        return False


def is_done(step: Step, ctx: Ctx, *, cheap: bool = False) -> bool:
    """True if ``step`` has a current marker and (unless ``cheap``) still verifies."""
    if step.always:
        return False
    marker = ctx.state.step_marker(step.id)
    if marker is None or marker.get("rev") != step.rev:
        return False
    if marker.get("inputs_hash") != inputs_hash(step, ctx):
        return False
    return cheap or _verified(step, ctx)


def pending(ctx: Ctx, steps: Sequence[Step] | None = None) -> list[Step]:
    """The steps that would run now: ``always`` steps and those not done."""
    chosen = default_steps() if steps is None else list(steps)
    return [step for step in chosen if step.always or not is_done(step, ctx)]


def _select(steps: list[Step], only: Iterable[str] | None, from_step: str | None) -> list[Step]:
    """The steps chosen by ``--only`` / ``--from-step`` (``always`` steps are kept)."""
    ids = [step.id for step in steps]
    wanted = list(only or [])
    unknown = [name for name in [*wanted, *([from_step] if from_step else [])] if name not in ids]
    if unknown:
        raise UsageError(
            f"unknown setup step(s): {', '.join(unknown)}",
            hint=f"steps: {', '.join(ids)}",
        )
    if wanted:
        return [step for step in steps if step.always or step.id in wanted]
    if from_step:
        start = ids.index(from_step)
        return [step for index, step in enumerate(steps) if step.always or index >= start]
    return steps


def init_prefix_meta(ctx: Ctx) -> None:
    """Create the prefix (0700) with our metadata directory and created-by marker."""
    fsutil.ensure_dir(ctx.paths.prefix)
    os.chmod(ctx.paths.prefix, 0o700)
    fsutil.ensure_dir(ctx.paths.prefix_meta_dir)
    marker = ctx.paths.created_by_marker
    if not os.path.lexists(marker):
        fsutil.atomic_write(marker, f"fork-linux {get_version()}\n", mode=0o644)


def clear_markers(ctx: Ctx, keep: Iterable[str] = ()) -> None:
    """Forget every step marker except those in ``keep`` (the prefix was rebuilt)."""
    kept = set(keep)
    stale = [step_id for step_id in ctx.state.data.get("steps", {}) if step_id not in kept]
    for step_id in stale:
        ctx.state.clear_step_marker(step_id)


def mark_done(step: Step, ctx: Ctx, digest: str | None = None) -> None:
    """Record ``step`` as done with ``digest`` (default: its current inputs hash)."""
    if not step.always:
        ctx.state.set_step_marker(step.id, step.rev, inputs_hash(step, ctx) if digest is None else digest)


def _save_quietly(ctx: Ctx) -> None:
    """Save the state, logging (not raising) a failure: we are already handling one."""
    try:
        ctx.state.save()
    except (OSError, ForkLinuxError) as exc:
        log.warning("cannot save the setup state: %s", exc)


def _record_failure(ctx: Ctx, step: Step, message: str) -> None:
    """Remember which step failed and why (``setup.last_error``)."""
    ctx.state.set(SETUP_COMPLETE, False)
    ctx.state.set(
        LAST_ERROR,
        {
            "step": step.id,
            "message": message,
            "log": None if ctx.log_file is None else str(ctx.log_file),
            "at": now(),
        },
    )
    _save_quietly(ctx)


def _failure_hint(ctx: Ctx, hint: str) -> str:
    """``hint`` plus where the log is and how to resume."""
    parts = [hint] if hint else []
    if ctx.log_file is not None:
        parts.append(f"details are in {ctx.log_file}")
    parts.append("run 'fork-linux setup' again to resume at this step")
    return "; ".join(parts)


def run_step(ctx: Ctx, step: Step) -> None:
    """Run and verify one step, then record its marker and save the state.

    Errors with their own exit code (downloads, integrity, declined consent,
    ...) propagate unchanged; generic failures become :class:`SetupFailed`
    naming the step. ``KeyboardInterrupt`` propagates and leaves no marker.
    """
    log.info("setup step %s: %s", step.id, step.title)
    digest = None if step.always else inputs_hash(step, ctx)
    try:
        step.run(ctx)
        ok = _verified(step, ctx)
    except SetupFailed as exc:
        _record_failure(ctx, step, exc.message)
        raise
    except ForkLinuxError as exc:
        _record_failure(ctx, step, exc.message)
        if exc.exit_code != ExitCode.ERROR:
            raise
        raise SetupFailed(step.id, exc.message, hint=_failure_hint(ctx, exc.hint)) from exc
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        _record_failure(ctx, step, message)
        raise SetupFailed(step.id, message, hint=_failure_hint(ctx, "")) from exc
    if not ok:
        message = "the step ran but its result could not be verified"
        _record_failure(ctx, step, message)
        raise SetupFailed(step.id, message, hint=_failure_hint(ctx, ""))
    mark_done(step, ctx, digest)
    ctx.state.save()


def update_completion(ctx: Ctx, steps: Sequence[Step] | None = None) -> bool:
    """Set ``setup.complete`` (and the revisions it was completed with) from the markers."""
    chosen = default_steps() if steps is None else list(steps)
    complete = all(is_done(step, ctx, cheap=True) for step in chosen if not step.always)
    ctx.state.set(SETUP_COMPLETE, complete)
    if complete:
        ctx.state.set(BOOTSTRAP_REVISION, ctx.manifest.bootstrap_revision)
        ctx.state.set(MANIFEST_REVISION, ctx.manifest.revision)
        ctx.state.set(COMPLETED_AT, now())
        ctx.state.set(LAST_ERROR, None)
    return complete


def run_steps(
    ctx: Ctx,
    steps: Sequence[Step] | None = None,
    *,
    only: Iterable[str] | None = None,
    from_step: str | None = None,
    force: bool = False,
) -> list[str]:
    """Run the pending steps (or the ``only`` / ``from_step`` selection) under the setup lock.

    ``force`` (or ``ctx.force``) re-runs every selected step; ``only`` and
    ``from_step`` always run what they select. A step whose marker vanished
    during this run (the .NET step rebuilds the prefix) runs again. Returns
    the ids of the steps that ran.
    """
    all_steps = default_steps() if steps is None else list(steps)
    selected = _select(all_steps, only, from_step)
    explicit = force or ctx.force or bool(only) or from_step is not None
    ran: list[str] = []
    with FileLock(ctx.paths.lock_file, LOCK_PURPOSE):
        init_prefix_meta(ctx)
        todo = [step for step in selected if explicit or step.always or not is_done(step, ctx)]
        if any(step.requires_fork_closed for step in todo):
            procs.require_closed(ctx.paths.prefix, proc_root=ctx.proc_root)
        total = sum(step.weight for step in todo) or 1
        done_weight = 0
        with ctx.ui.progress(PROGRESS_TITLE) as progress:
            for step in selected:
                planned = step in todo
                if not planned and is_done(step, ctx, cheap=True):
                    continue
                progress.update(done_weight / total, step.title)
                run_step(ctx, step)
                ran.append(step.id)
                if planned:
                    done_weight += step.weight
            update_completion(ctx, all_steps)
            ctx.state.save()
            progress.update(1.0, "done")
    return ran


def needs_setup(ctx: Ctx, steps: Sequence[Step] | None = None) -> bool:
    """True when setup is incomplete, was completed with other revisions, or an input changed."""
    if ctx.state.get(SETUP_COMPLETE) is not True:
        return True
    if ctx.state.get(BOOTSTRAP_REVISION) != ctx.manifest.bootstrap_revision:
        return True
    if ctx.state.get(MANIFEST_REVISION) != ctx.manifest.revision:
        return True
    chosen = default_steps() if steps is None else list(steps)
    return any(not step.always and not is_done(step, ctx, cheap=True) for step in chosen)


def ensure_ready(ctx: Ctx, *, allow: bool = True, steps: Sequence[Step] | None = None) -> None:
    """Make sure setup is complete before launching Fork (``allow=False``: only check)."""
    if not needs_setup(ctx, steps):
        return
    if not allow:
        raise NotSetUpError(
            "Fork for Linux (unofficial) is not set up (or its setup needs to be updated)",
            hint="run 'fork-linux setup'",
        )
    chosen = default_steps() if steps is None else list(steps)
    if ctx.state.get(SETUP_COMPLETE) is True and procs.fork_running(ctx.paths.prefix, proc_root=ctx.proc_root):
        # Opening another repository while Fork runs must not fail because, say, Fork just wrote
        # its settings.json: steps that need Fork closed wait for a launch with Fork closed.
        deferred = {
            step.id
            for step in chosen
            if step.requires_fork_closed and not step.always and not is_done(step, ctx, cheap=True)
        }
        if deferred:
            log.info("Fork is running; deferring setup steps %s", ", ".join(sorted(deferred)))
            chosen = [step for step in chosen if step.id not in deferred]
    run_steps(ctx, chosen)
