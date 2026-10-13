"""User interaction for setup and the desktop entry: terminal, zenity, kdialog or nothing.

:func:`choose` picks an implementation: a terminal when we run on one, a
zenity or kdialog dialog when started from the desktop, and :class:`NullUI`
(log only, confirmations answer their default) when nobody can be asked.
Every implementation offers the same small API (:class:`UI`): ``info``,
``notify`` (never waits for the user), ``warn``, ``error``, ``confirm`` and a
``progress`` context manager.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping
from types import TracebackType
from typing import IO, Any

from . import APP_ID, APP_NAME, sandbox
from .errors import Declined
from .procrun import Runner

log = logging.getLogger(__name__)

MODES = ("auto", "zenity", "kdialog", "terminal", "none")
DIALOG_WIDTH = "460"
# kdialog has no progress window we can feed from a pipe; notifications mark these steps.
MILESTONES = (25, 50, 75)
_YES = ("y", "yes")
_NO = ("n", "no")
_PROMPT_TRIES = 3
# Where questions are read from when we run on a terminal (stdin may be a pipe).
TTY = "/dev/tty"


def _percent(fraction: float) -> int:
    """``fraction`` (0.0-1.0) as a whole percentage clamped to 0..100."""
    return max(0, min(100, round(fraction * 100)))


class Progress:
    """A progress display; use it as a context manager (``with ui.progress(title) as p``)."""

    def __init__(self, title: str) -> None:
        self.title = title
        self.percent = 0
        self.step = ""

    def __enter__(self) -> Progress:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.finish(failed=exc_type is not None)

    def start(self) -> None:
        """Show the display (called on ``__enter__``)."""

    def finish(self, *, failed: bool) -> None:
        """Close the display (called on ``__exit__``; ``failed`` when an exception escapes)."""

    def update(self, fraction: float, text: str = "") -> None:
        """Move to ``fraction`` (0.0-1.0), optionally describing the current step."""
        self.percent = _percent(fraction)
        self.step = text

    def log(self, text: str) -> None:
        """Add a detail line without changing the percentage."""
        log.info("%s: %s", self.title, text)


class UI:
    """Base class and the interface every implementation provides."""

    interactive: bool = False

    def info(self, msg: str) -> None:
        """Tell the user something."""
        log.info("%s", msg)

    def notify(self, msg: str) -> None:
        """Tell the user something without waiting for them (no modal dialog)."""
        log.info("%s", msg)

    def warn(self, msg: str) -> None:
        """Warn the user."""
        log.warning("%s", msg)

    def error(self, msg: str, hint: str = "") -> None:
        """Report an error, with an optional hint on how to fix it."""
        log.error("%s%s", msg, f" (hint: {hint})" if hint else "")

    def confirm(self, title: str, text: str, *, default: bool = False) -> bool:
        """Ask a yes/no question; non-interactive implementations return ``default``."""
        log.info("%s: %s -> %s (not asked)", title, text, "yes" if default else "no")
        return default

    def progress(self, title: str) -> Progress:
        """A :class:`Progress` context manager for a long operation."""
        return Progress(title)


class NullUI(UI):
    """Nobody to ask: messages go to the log, confirmations answer their default."""


class _TerminalProgress(Progress):
    """``[ 42%] step`` lines on the terminal."""

    def __init__(self, title: str, write: Callable[[str], None]) -> None:
        super().__init__(title)
        self._write = write
        self._last: tuple[int, str] | None = None

    def start(self) -> None:
        self._write(f"{self.title}...")

    def finish(self, *, failed: bool) -> None:
        self._write(f"{self.title}: {'failed' if failed else 'done'}")

    def update(self, fraction: float, text: str = "") -> None:
        super().update(fraction, text)
        current = (self.percent, text)
        if current == self._last:
            return
        self._last = current
        self._write(f"[{self.percent:3d}%] {text}".rstrip())

    def log(self, text: str) -> None:
        self._write(f"       {text}")


class TerminalUI(UI):
    """Messages on stderr; questions read from ``/dev/tty`` (or stdin) when on a terminal."""

    def __init__(
        self,
        *,
        stream: IO[str] | None = None,
        input_stream: IO[str] | None = None,
        interactive: bool | None = None,
    ) -> None:
        self._stream = stream
        self._input = input_stream
        self.interactive = _stdin_is_tty() if interactive is None else interactive

    def _write(self, text: str) -> None:
        out = sys.stderr if self._stream is None else self._stream
        out.write(text + "\n")
        out.flush()

    def info(self, msg: str) -> None:
        self._write(msg)

    def notify(self, msg: str) -> None:
        self._write(msg)

    def warn(self, msg: str) -> None:
        self._write(f"warning: {msg}")

    def error(self, msg: str, hint: str = "") -> None:
        self._write(f"error: {msg}")
        if hint:
            self._write(f"hint: {hint}")

    def confirm(self, title: str, text: str, *, default: bool = False) -> bool:
        if not self.interactive:
            return default
        choices = "[Y/n]" if default else "[y/N]"
        self._write(f"{title}\n{text}")
        for _ in range(_PROMPT_TRIES):
            out = sys.stderr if self._stream is None else self._stream
            out.write(f"{choices} ")
            out.flush()
            answer = self._readline()
            if answer is None:
                return default
            answer = answer.strip().lower()
            if not answer:
                return default
            if answer in _YES:
                return True
            if answer in _NO:
                return False
            self._write("please answer yes or no")
        return default

    def _readline(self) -> str | None:
        """One line of the answer, or ``None`` at end of input."""
        if self._input is not None:
            line = self._input.readline()
            return line if line else None
        try:
            with open(TTY, encoding="utf-8", errors="replace") as tty:
                line = tty.readline()
        except OSError:
            line = sys.stdin.readline()
        return line if line else None

    def progress(self, title: str) -> Progress:
        return _TerminalProgress(title, self._write)


# What GTK / X11 print when zenity cannot reach the display at all.
_DISPLAY_ERRORS = ("cannot open display", "failed to open display", "unable to open display")
_MAX_ERRORS = 64 * 1024


def _zenity_text(text: str) -> str:
    """Escape ``text`` for zenity's progress label (Pango markup, C escapes, one line)."""
    escaped = text.replace("\\", "\\\\").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return escaped.replace("\r", " ").replace("\n", " ")


class _ZenityProgress(Progress):
    """``zenity --progress --auto-close`` fed ``NN`` and ``# text`` lines on its stdin."""

    def __init__(self, title: str, argv: list[str], env: Mapping[str, str], popen: Callable[..., Any]) -> None:
        super().__init__(title)
        self._argv = argv
        self._env = dict(env)
        self._popen = popen
        self._proc: Any = None
        self._errors: Any = None

    def start(self) -> None:
        # zenity's stderr goes to a file (a pipe could fill up and block it) so that a
        # dialog that never opened can be told apart from one the user cancelled.
        self._errors = tempfile.TemporaryFile()
        try:
            self._proc = self._popen(
                self._argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=self._errors,
                env=self._env,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except OSError as exc:
            log.warning("cannot show the zenity progress dialog: %s", exc)
            self._proc = None
            self._take_errors()

    def _take_errors(self) -> str:
        """What zenity wrote to stderr (lower case); the file is closed."""
        errors, self._errors = self._errors, None
        if errors is None:
            return ""
        with errors:
            errors.seek(0)
            return errors.read(_MAX_ERRORS).decode("utf-8", "replace").lower()

    def _ended(self, status: int) -> None:
        """zenity exited with ``status``: :class:`Declined` if the user cancelled it.

        A dialog that could not open the display (a stale ``DISPLAY``) exits
        non-zero too; then the work goes on without a dialog.
        """
        errors = self._take_errors()
        if status == 0:
            return
        if any(marker in errors for marker in _DISPLAY_ERRORS):
            log.warning("the zenity progress dialog cannot open the display; continuing without it")
            return
        raise _cancelled(self.title)

    def _send(self, line: str) -> None:
        """Write one line to zenity; :class:`Declined` once the user has cancelled.

        A dialog that closed itself successfully (``--auto-close`` at 100%)
        is simply no longer fed.
        """
        if self._proc is None:
            return
        status = self._proc.poll()
        if status is None:
            try:
                self._proc.stdin.write(line + "\n")
                self._proc.stdin.flush()
                return
            except (BrokenPipeError, ValueError):
                status = self._proc.wait()
        self._proc = None
        self._ended(status)

    def update(self, fraction: float, text: str = "") -> None:
        super().update(fraction, text)
        if text:
            self._send(f"# {_zenity_text(text)}")
        self._send(str(self.percent))

    def log(self, text: str) -> None:
        super().log(text)
        self._send(f"# {_zenity_text(text)}")

    def finish(self, *, failed: bool) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            self._take_errors()
            return
        if failed:
            proc.terminate()
            proc.wait()
            self._take_errors()
            return
        try:
            proc.stdin.write("100\n")
            proc.stdin.close()
        except (BrokenPipeError, ValueError):
            log.debug("zenity closed its input before the end")
        self._ended(proc.wait())


def _cancelled(title: str) -> Declined:
    """The error raised when the user cancels a progress dialog."""
    return Declined(f"{title}: cancelled", hint="run the command again to continue where it stopped")


class _DialogUI(UI):
    """Common base of the dialog implementations: a runner and a host environment."""

    interactive = True
    program = ""

    def __init__(
        self,
        *,
        runner: Runner | None = None,
        env: Mapping[str, str] | None = None,
        popen: Callable[..., Any] | None = None,
    ) -> None:
        self.runner = Runner() if runner is None else runner
        self.env = sandbox.clean_env(os.environ if env is None else env)
        self._popen = subprocess.Popen if popen is None else popen

    def _run(self, *args: str) -> int:
        """Run the dialog program with ``args``; return its exit status."""
        return self.runner.run([self.program, *args], env=self.env).returncode

    def _passive(self, msg: str) -> list[str]:
        """The dialog program's own non-modal notification for ``msg``."""
        raise NotImplementedError

    def notify(self, msg: str) -> None:
        """A desktop notification started in the background: never waits for it or for the user.

        ``notify-send`` when installed, else the dialog program's own passive
        notification; a program that cannot start leaves the message in the log only.
        """
        super().notify(msg)
        if self.runner.which("notify-send", self.env.get("PATH")) is not None:
            argv = ["notify-send", f"--app-name={APP_NAME}", f"--icon={APP_ID}", APP_NAME, msg]
        else:
            argv = self._passive(msg)
        try:
            self._popen(
                argv,
                env=self.env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                close_fds=True,
            )
        except OSError as exc:
            log.warning("cannot show a desktop notification: %s", exc)


class ZenityUI(_DialogUI):
    """GTK dialogs through ``zenity``."""

    program = "zenity"

    def _passive(self, msg: str) -> list[str]:
        return [self.program, "--notification", f"--text={APP_NAME}: {msg}"]

    def _message(self, kind: str, text: str) -> None:
        self._run(f"--{kind}", f"--title={APP_NAME}", f"--text={text}", "--no-markup", f"--width={DIALOG_WIDTH}")

    def info(self, msg: str) -> None:
        super().info(msg)
        self._message("info", msg)

    def warn(self, msg: str) -> None:
        super().warn(msg)
        self._message("warning", msg)

    def error(self, msg: str, hint: str = "") -> None:
        super().error(msg, hint)
        self._message("error", f"{msg}\n\n{hint}" if hint else msg)

    def confirm(self, title: str, text: str, *, default: bool = False) -> bool:
        args = ["--question", f"--title={title}", f"--text={text}", "--no-markup", f"--width={DIALOG_WIDTH}"]
        if not default:
            args.append("--default-cancel")
        status = self._run(*args)
        if status in (0, 1):
            return status == 0
        return default

    def progress(self, title: str) -> Progress:
        argv = [
            self.program,
            "--progress",
            f"--title={APP_NAME}",
            # The label is Pango markup (no --no-markup for progress text updates): escape it.
            f"--text={_zenity_text(title)}",
            "--percentage=0",
            "--auto-close",
            f"--width={DIALOG_WIDTH}",
        ]
        return _ZenityProgress(title, argv, self.env, self._popen)


class _NotifyProgress(Progress):
    """Desktop notifications at the start, at each milestone and at the end."""

    def __init__(self, title: str, notify: Callable[[str, str], None]) -> None:
        super().__init__(title)
        self._notify = notify
        self._next = 0

    def start(self) -> None:
        self._notify(self.title, "Started")

    def update(self, fraction: float, text: str = "") -> None:
        super().update(fraction, text)
        reached = [mark for mark in MILESTONES[self._next:] if self.percent >= mark]
        if reached:
            self._next = MILESTONES.index(reached[-1]) + 1
            self._notify(self.title, f"{self.percent}% {text}".rstrip())

    def finish(self, *, failed: bool) -> None:
        self._notify(self.title, "Failed" if failed else "Done")


class KDialogUI(_DialogUI):
    """KDE dialogs through ``kdialog``; progress through ``notify-send`` milestones."""

    program = "kdialog"

    def _passive(self, msg: str) -> list[str]:
        return [self.program, "--title", APP_NAME, "--passivepopup", msg, "10"]

    def info(self, msg: str) -> None:
        super().info(msg)
        self._run("--title", APP_NAME, "--msgbox", msg)

    def warn(self, msg: str) -> None:
        super().warn(msg)
        self._run("--title", APP_NAME, "--sorry", msg)

    def error(self, msg: str, hint: str = "") -> None:
        super().error(msg, hint)
        self._run("--title", APP_NAME, "--error", f"{msg}\n\n{hint}" if hint else msg)

    def confirm(self, title: str, text: str, *, default: bool = False) -> bool:
        status = self._run("--title", title, "--yesno", text)
        if status in (0, 1):
            return status == 0
        return default

    def _notify(self, title: str, body: str) -> None:
        """A desktop notification; silently skipped when ``notify-send`` is missing."""
        if self.runner.which("notify-send", self.env.get("PATH")) is None:
            return
        self.runner.run(
            ["notify-send", f"--app-name={APP_NAME}", f"--icon={APP_ID}", title, body], env=self.env
        )

    def progress(self, title: str) -> Progress:
        return _NotifyProgress(title, self._notify)


def _stdin_is_tty() -> bool:
    """True when both stdin and stderr are terminals (a person is watching)."""
    try:
        return sys.stdin.isatty() and sys.stderr.isatty()
    except (AttributeError, ValueError):
        return False


def _has_display(env: Mapping[str, str]) -> bool:
    """True when an X11 or Wayland display is available."""
    return bool(env.get("DISPLAY") or env.get("WAYLAND_DISPLAY"))


_DIALOGS: dict[str, type[_DialogUI]] = {"zenity": ZenityUI, "kdialog": KDialogUI}


def choose(
    env: Mapping[str, str] | None = None,
    *,
    gui: bool | None = None,
    mode: str = "auto",
    runner: Runner | None = None,
    isatty: bool | None = None,
) -> UI:
    """Pick the UI for this process.

    ``mode`` comes from ``[ui] progress``: ``zenity`` / ``kdialog`` force that
    dialog (when installed), ``terminal`` the terminal, ``none`` silence.
    ``auto`` uses the terminal when stdin/stderr are a TTY (unless ``gui`` is
    True), else a dialog when a display and zenity or kdialog exist, else
    :class:`NullUI`. ``gui=False`` (``--no-gui``) never shows dialogs.
    """
    environ: Mapping[str, str] = os.environ if env is None else env
    run = Runner() if runner is None else runner
    tty = _stdin_is_tty() if isatty is None else isatty
    if mode not in MODES:
        log.warning("unknown progress mode %r; using auto", mode)
        mode = "auto"
    if mode == "none":
        return NullUI()
    if mode == "terminal":
        return TerminalUI(interactive=tty)
    want_dialog = gui is not False and (gui is True or not tty or mode in _DIALOGS)
    dialog = _dialog_ui(environ, run, mode, tty) if want_dialog and _has_display(environ) else None
    if dialog is not None:
        return dialog
    if tty:
        return TerminalUI(interactive=True)
    return NullUI()


def _dialog_ui(environ: Mapping[str, str], run: Runner, mode: str, tty: bool) -> UI | None:
    """The dialog UI for ``mode`` (``auto``: zenity, else kdialog) when installed, else None."""
    order = [mode] if mode in _DIALOGS else list(_DIALOGS)
    path = sandbox.clean_env(environ).get("PATH")
    for name in order:
        if run.which(name, path) is not None:
            return _DIALOGS[name](runner=run, env=environ)
    if mode in _DIALOGS:
        log.warning("%s is not installed; falling back to %s", mode, "the terminal" if tty else "the log")
    return None
