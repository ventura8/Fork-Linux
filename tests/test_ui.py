"""Tests for fork_linux.ui: terminal, zenity, kdialog and silent user interfaces."""

from __future__ import annotations

import io
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from fork_linux import APP_ID, APP_NAME, ui
from fork_linux.errors import Declined
from fork_linux.procrun import RecordingRunner


def _calls(log: Path) -> list[dict[str, Any]]:
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


# --- base classes --------------------------------------------------------------------


@pytest.mark.parametrize(("fraction", "percent"), [(-1.0, 0), (0.0, 0), (0.424, 42), (0.999, 100), (3.0, 100)])
def test_percent(fraction: float, percent: int) -> None:
    assert ui._percent(fraction) == percent


def test_null_ui_logs_and_answers_defaults(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="fork_linux.ui")
    null = ui.NullUI()
    assert not null.interactive
    null.info("hello")
    null.notify("updated")
    null.warn("careful")
    null.error("broken", "fix it")
    null.error("plain")
    assert null.confirm("Title", "Proceed?", default=True) is True
    assert null.confirm("Title", "Proceed?") is False
    with null.progress("Working") as progress:
        progress.update(0.5, "half")
        progress.log("detail")
        assert progress.percent == 50
    messages = [record.getMessage() for record in caplog.records]
    assert messages[:5] == ["hello", "updated", "careful", "broken (hint: fix it)", "plain"]
    assert "Working: detail" in messages


# --- terminal ------------------------------------------------------------------------------


def test_terminal_messages_go_to_the_stream() -> None:
    out = io.StringIO()
    term = ui.TerminalUI(stream=out, interactive=False)
    term.info("hello")
    term.notify("updated")
    term.warn("careful")
    term.error("broken", "fix it")
    term.error("plain")
    assert out.getvalue() == "hello\nupdated\nwarning: careful\nerror: broken\nhint: fix it\nerror: plain\n"


def test_terminal_defaults_to_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    term = ui.TerminalUI()
    assert term.interactive is False  # pytest's stdin is not a terminal
    term.info("to stderr")
    assert capsys.readouterr().err == "to stderr\n"


@pytest.mark.parametrize(
    ("answers", "default", "result"),
    [
        ("y\n", False, True),
        ("YES\n", False, True),
        ("n\n", True, False),
        ("no\n", True, False),
        ("\n", True, True),
        ("\n", False, False),
        ("", True, True),
        ("maybe\nyes\n", False, True),
        ("a\nb\nc\nyes\n", True, True),
        ("a\nb\nc\nyes\n", False, False),
    ],
)
def test_terminal_confirm(answers: str, default: bool, result: bool) -> None:
    out = io.StringIO()
    term = ui.TerminalUI(stream=out, input_stream=io.StringIO(answers), interactive=True)
    assert term.confirm("Download", "Download 200 MB?", default=default) is result
    text = out.getvalue()
    assert text.startswith("Download\nDownload 200 MB?\n")
    assert ("[Y/n] " if default else "[y/N] ") in text


def test_terminal_confirm_not_interactive() -> None:
    term = ui.TerminalUI(stream=io.StringIO(), input_stream=io.StringIO("y\n"), interactive=False)
    assert term.confirm("T", "x?") is False
    assert term.confirm("T", "x?", default=True) is True


def test_terminal_confirm_reads_the_tty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tty = tmp_path / "tty"
    tty.write_text("yes\n", encoding="utf-8")
    monkeypatch.setattr(ui, "TTY", str(tty))
    term = ui.TerminalUI(stream=io.StringIO(), interactive=True)
    assert term.confirm("T", "x?") is True
    tty.write_text("", encoding="utf-8")
    assert term.confirm("T", "x?", default=True) is True


def test_terminal_confirm_falls_back_to_stdin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ui, "TTY", str(tmp_path / "no-tty"))
    monkeypatch.setattr(sys, "stdin", io.StringIO("y\n"))
    term = ui.TerminalUI(stream=io.StringIO(), interactive=True)
    assert term.confirm("T", "x?") is True


def test_terminal_prompt_uses_stderr_by_default(capsys: pytest.CaptureFixture[str]) -> None:
    term = ui.TerminalUI(input_stream=io.StringIO("y\n"), interactive=True)
    assert term.confirm("T", "x?") is True
    assert capsys.readouterr().err == "T\nx?\n[y/N] "


def test_terminal_progress() -> None:
    out = io.StringIO()
    term = ui.TerminalUI(stream=out, interactive=True)
    with term.progress("Setting up") as progress:
        progress.update(0.1, "Downloading")
        progress.update(0.1, "Downloading")
        progress.update(0.5)
        progress.log("detail")
    with pytest.raises(RuntimeError), term.progress("Again"):
        raise RuntimeError("boom")
    assert out.getvalue().splitlines() == [
        "Setting up...",
        "[ 10%] Downloading",
        "[ 50%]",
        "       detail",
        "Setting up: done",
        "Again...",
        "Again: failed",
    ]


# --- zenity ------------------------------------------------------------------------------------


def test_zenity_dialogs() -> None:
    runner = RecordingRunner(responses={"zenity": [0, 0, 0, 0, 1, 5, 5]})
    dialog = ui.ZenityUI(runner=runner, env={"PATH": "/usr/bin", "LD_LIBRARY_PATH": "/bundle"})
    assert dialog.interactive
    dialog.info("hello")
    dialog.warn("careful")
    dialog.error("broken", "fix it")
    dialog.error("plain")
    assert dialog.confirm("Q", "Proceed?") is False
    assert dialog.confirm("Q", "Proceed?", default=True) is True
    assert dialog.confirm("Q", "Proceed?") is False
    common = ["--no-markup", f"--width={ui.DIALOG_WIDTH}"]
    assert runner.argvs == [
        ["zenity", "--info", f"--title={APP_NAME}", "--text=hello", *common],
        ["zenity", "--warning", f"--title={APP_NAME}", "--text=careful", *common],
        ["zenity", "--error", f"--title={APP_NAME}", "--text=broken\n\nfix it", *common],
        ["zenity", "--error", f"--title={APP_NAME}", "--text=plain", *common],
        ["zenity", "--question", "--title=Q", "--text=Proceed?", *common, "--default-cancel"],
        ["zenity", "--question", "--title=Q", "--text=Proceed?", *common],
        ["zenity", "--question", "--title=Q", "--text=Proceed?", *common, "--default-cancel"],
    ]
    assert runner.calls[0]["env"] == {"PATH": "/usr/bin"}


def test_zenity_confirm_yes() -> None:
    dialog = ui.ZenityUI(runner=RecordingRunner(responses={"zenity": 0}), env={})
    assert dialog.confirm("Q", "Proceed?") is True


def test_zenity_text_escaping() -> None:
    assert ui._zenity_text("a & <b>\\n\nnext\r") == "a &amp; &lt;b&gt;\\\\n next "


def test_zenity_progress_with_the_fake(fake_bin: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dialog = ui.ZenityUI(env=dict(os.environ))
    with dialog.progress("Setting up") as progress:
        progress.update(0.25, "Downloading <Wine>")
        progress.log("verified")
        progress.update(0.5)
    calls = _calls(fake_bin)
    assert calls[-1]["argv"] == [
        "zenity",
        "--progress",
        f"--title={APP_NAME}",
        "--text=Setting up",
        "--percentage=0",
        "--auto-close",
        f"--width={ui.DIALOG_WIDTH}",
    ]
    assert calls[-1]["stdin"] == ["# Downloading &lt;Wine&gt;", "25", "# verified", "50", "100"]


def _report_progress(dialog: ui.ZenityUI, title: str, fractions: tuple[float, ...]) -> None:
    with dialog.progress(title) as progress:
        for fraction in fractions:
            progress.update(fraction)


def test_zenity_progress_cancel_with_the_fake(fake_bin: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FL_FAKE_ZENITY_CANCEL_AT", "40")
    dialog = ui.ZenityUI(env=dict(os.environ))
    with pytest.raises(Declined, match="Setting up: cancelled"):
        _report_progress(dialog, "Setting up", (0.25, 0.5, 0.75))
    assert _calls(fake_bin)[-1]["cancelled"] is True


class _Stdin:
    def __init__(self, fail: type[Exception] | None = None, fail_on_close: type[Exception] | None = None) -> None:
        self.lines: list[str] = []
        self.fail = fail
        self.fail_on_close = fail_on_close
        self.closed = False

    def write(self, text: str) -> None:
        if self.fail is not None:
            raise self.fail("pipe")
        self.lines.append(text)

    def flush(self) -> None:
        """Nothing is buffered."""

    def close(self) -> None:
        if self.fail_on_close is not None:
            raise self.fail_on_close("pipe")
        self.closed = True


class _Proc:
    """A scripted stand-in for the zenity process."""

    def __init__(self, polls: list[int | None], code: int = 0, stdin: _Stdin | None = None) -> None:
        self.polls = polls
        self.code = code
        self.stdin = stdin or _Stdin()
        self.terminated = False

    def poll(self) -> int | None:
        return self.polls.pop(0) if len(self.polls) > 1 else self.polls[0]

    def wait(self) -> int:
        return self.code

    def terminate(self) -> None:
        self.terminated = True


def _zenity_with(proc: _Proc | Exception) -> tuple[ui.ZenityUI, list[dict[str, Any]]]:
    seen: list[dict[str, Any]] = []

    def popen(argv: list[str], **kwargs: Any) -> _Proc:
        seen.append({"argv": argv, **kwargs})
        if isinstance(proc, Exception):
            raise proc
        return proc

    return ui.ZenityUI(runner=RecordingRunner(), env={}, popen=popen), seen


def test_zenity_progress_closed_by_itself() -> None:
    proc = _Proc([None, 0])
    dialog, seen = _zenity_with(proc)
    with dialog.progress("T") as progress:
        progress.update(1.0)
        progress.update(1.0, "after the dialog closed")
    assert proc.stdin.lines == ["100\n"]
    assert seen[0]["stdin"] == subprocess.PIPE


@pytest.mark.parametrize("error", [BrokenPipeError, ValueError])
def test_zenity_progress_broken_pipe_means_cancelled(error: type[Exception]) -> None:
    dialog, _seen = _zenity_with(_Proc([None], code=1, stdin=_Stdin(fail=error)))
    with pytest.raises(Declined), dialog.progress("T") as progress:
        progress.update(0.3)


def test_zenity_progress_broken_pipe_after_success() -> None:
    proc = _Proc([None], code=0, stdin=_Stdin(fail=BrokenPipeError))
    dialog, _seen = _zenity_with(proc)
    with dialog.progress("T") as progress:
        progress.update(0.3)
        progress.update(0.4)


def test_zenity_progress_cancelled_before_a_write() -> None:
    dialog, _seen = _zenity_with(_Proc([1]))
    with pytest.raises(Declined), dialog.progress("T") as progress:
        progress.log("x")


@pytest.mark.parametrize(("code", "cancelled"), [(0, False), (1, True)])
def test_zenity_progress_finish_with_a_closed_pipe(code: int, cancelled: bool) -> None:
    proc = _Proc([None], code=code, stdin=_Stdin(fail_on_close=BrokenPipeError))
    dialog, _seen = _zenity_with(proc)
    if cancelled:
        with pytest.raises(Declined), dialog.progress("T"):
            pass
    else:
        with dialog.progress("T"):
            pass
    assert proc.stdin.lines == ["100\n"]


def test_zenity_progress_failure_terminates_the_dialog() -> None:
    proc = _Proc([None])
    dialog, _seen = _zenity_with(proc)
    with pytest.raises(KeyError), dialog.progress("T"):
        raise KeyError("x")
    assert proc.terminated


def _zenity_failing_with(message: bytes, code: int = 1) -> ui.ZenityUI:
    """A zenity that wrote ``message`` to its stderr and exited with ``code`` before any input."""

    def popen(argv: list[str], **kwargs: Any) -> _Proc:
        kwargs["stderr"].write(message)
        return _Proc([code], code=code)

    return ui.ZenityUI(runner=RecordingRunner(), env={}, popen=popen)


def test_zenity_progress_without_a_display_goes_on(caplog: pytest.LogCaptureFixture) -> None:
    # E2E: a stale DISPLAY made zenity exit 1 at once, and 'fork-linux run' reported "cancelled".
    dialog = _zenity_failing_with(b"(zenity:1): Gtk-WARNING **: Failed to open display\n")
    with dialog.progress("T") as progress:
        progress.update(0.5, "x")
        progress.update(0.6, "y")
    assert "cannot open the display" in caplog.text


def test_zenity_progress_without_a_display_at_finish() -> None:
    dialog = _zenity_failing_with(b"cannot open display: :98\n")
    with dialog.progress("T"):
        pass


def test_zenity_progress_cancel_is_still_declined() -> None:
    dialog = _zenity_failing_with(b"")
    with pytest.raises(Declined), dialog.progress("T") as progress:
        progress.update(0.5)


def test_zenity_progress_without_zenity(caplog: pytest.LogCaptureFixture) -> None:
    dialog, _seen = _zenity_with(FileNotFoundError("zenity"))
    with dialog.progress("T") as progress:
        progress.update(0.5, "x")
    assert "cannot show the zenity progress dialog" in caplog.text


# --- kdialog ------------------------------------------------------------------------------------


def test_kdialog_dialogs() -> None:
    runner = RecordingRunner(responses={"kdialog": [0, 0, 0, 0, 0, 1, 254]})
    dialog = ui.KDialogUI(runner=runner, env={"PATH": "/usr/bin"})
    dialog.info("hello")
    dialog.warn("careful")
    dialog.error("broken", "fix it")
    dialog.error("plain")
    assert dialog.confirm("Q", "Proceed?") is True
    assert dialog.confirm("Q", "Proceed?", default=True) is False
    assert dialog.confirm("Q", "Proceed?", default=True) is True
    assert runner.argvs == [
        ["kdialog", "--title", APP_NAME, "--msgbox", "hello"],
        ["kdialog", "--title", APP_NAME, "--sorry", "careful"],
        ["kdialog", "--title", APP_NAME, "--error", "broken\n\nfix it"],
        ["kdialog", "--title", APP_NAME, "--error", "plain"],
        ["kdialog", "--title", "Q", "--yesno", "Proceed?"],
        ["kdialog", "--title", "Q", "--yesno", "Proceed?"],
        ["kdialog", "--title", "Q", "--yesno", "Proceed?"],
    ]


def test_kdialog_progress_notifies_milestones() -> None:
    runner = RecordingRunner(which_map={"notify-send": "/usr/bin/notify-send"})
    dialog = ui.KDialogUI(runner=runner, env={})
    with dialog.progress("Setting up") as progress:
        progress.update(0.1, "a")
        progress.update(0.3, "b")
        progress.update(0.35, "c")
        progress.update(0.8, "d")
        progress.update(0.9, "e")
    with pytest.raises(OSError), dialog.progress("Again"):
        raise OSError("disk full")
    bodies = [argv[-1] for argv in runner.argvs]
    assert bodies == ["Started", "30% b", "80% d", "Done", "Started", "Failed"]
    assert runner.argvs[0][:3] == ["notify-send", f"--app-name={APP_NAME}", f"--icon={APP_ID}"]


def test_kdialog_progress_without_notify_send() -> None:
    runner = RecordingRunner(which_map={"notify-send": None})
    with ui.KDialogUI(runner=runner, env={}).progress("T") as progress:
        progress.update(1.0)
    assert runner.calls == []


def test_dialog_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FL_UI_TEST", "1")
    dialog = ui.KDialogUI()
    assert dialog.env["FL_UI_TEST"] == "1"
    assert isinstance(dialog.runner, ui.Runner)


# --- choose -----------------------------------------------------------------------------------------


def _runner(*installed: str) -> RecordingRunner:
    which = {name: (f"/usr/bin/{name}" if name in installed else None) for name in ("zenity", "kdialog")}
    return RecordingRunner(which_map=which)


DISPLAY = {"DISPLAY": ":0"}


@pytest.mark.parametrize(
    ("env", "gui", "mode", "installed", "tty", "expected"),
    [
        (DISPLAY, None, "auto", ("zenity", "kdialog"), True, ui.TerminalUI),
        (DISPLAY, None, "auto", ("zenity", "kdialog"), False, ui.ZenityUI),
        ({"WAYLAND_DISPLAY": "wayland-0"}, None, "auto", ("kdialog",), False, ui.KDialogUI),
        (DISPLAY, None, "auto", (), False, ui.NullUI),
        ({}, None, "auto", ("zenity",), False, ui.NullUI),
        (DISPLAY, True, "auto", ("zenity",), True, ui.ZenityUI),
        ({}, True, "auto", ("zenity",), True, ui.TerminalUI),
        (DISPLAY, False, "auto", ("zenity",), False, ui.NullUI),
        (DISPLAY, False, "auto", ("zenity",), True, ui.TerminalUI),
        (DISPLAY, None, "kdialog", ("zenity", "kdialog"), True, ui.KDialogUI),
        (DISPLAY, None, "zenity", ("kdialog",), True, ui.TerminalUI),
        (DISPLAY, None, "zenity", ("kdialog",), False, ui.NullUI),
        (DISPLAY, None, "terminal", ("zenity",), False, ui.TerminalUI),
        (DISPLAY, None, "none", ("zenity",), True, ui.NullUI),
        (DISPLAY, None, "bogus", ("zenity",), False, ui.ZenityUI),
    ],
)
def test_choose(
    env: dict[str, str], gui: bool | None, mode: str, installed: tuple[str, ...], tty: bool, expected: type
) -> None:
    chosen = ui.choose(env, gui=gui, mode=mode, runner=_runner(*installed), isatty=tty)
    assert type(chosen) is expected


def test_choose_terminal_mode_keeps_interactivity() -> None:
    assert ui.choose({}, mode="terminal", isatty=False).interactive is False
    assert ui.choose({}, mode="terminal", isatty=True).interactive is True


def test_choose_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    assert isinstance(ui.choose(), ui.NullUI)


class _NoTTY:
    def isatty(self) -> bool:
        raise ValueError("I/O operation on closed file")


def test_stdin_is_tty_handles_odd_streams(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "stdin", _NoTTY())
    assert ui._stdin_is_tty() is False
    monkeypatch.setattr(sys, "stdin", object())
    assert ui._stdin_is_tty() is False


def test_zenity_progress_title_is_escaped(fake_bin: Path) -> None:
    dialog = ui.ZenityUI(env=dict(os.environ))
    with dialog.progress("Fork & <Wine>") as progress:
        progress.update(1.0)
    assert "--text=Fork &amp; &lt;Wine&gt;" in _calls(fake_bin)[-1]["argv"]


# --- non-modal notifications ---------------------------------------------------------------


def _notifier(
    cls: type[ui.ZenityUI] | type[ui.KDialogUI], notify_send: str | None, error: OSError | None = None
) -> tuple[ui.ZenityUI | ui.KDialogUI, RecordingRunner, list[dict[str, Any]]]:
    seen: list[dict[str, Any]] = []

    def popen(argv: list[str], **kwargs: Any) -> object:
        seen.append({"argv": argv, **kwargs})
        if error is not None:
            raise error
        return object()

    runner = RecordingRunner(which_map={"notify-send": notify_send})
    return cls(runner=runner, env={"PATH": "/usr/bin"}, popen=popen), runner, seen


@pytest.mark.parametrize("cls", [ui.ZenityUI, ui.KDialogUI])
def test_notify_uses_notify_send_in_the_background(cls: type[ui.ZenityUI] | type[ui.KDialogUI]) -> None:
    dialog, runner, seen = _notifier(cls, "/usr/bin/notify-send")
    dialog.notify("Fork updated 1 -> 2")
    assert runner.argvs == []  # nothing is waited for
    assert seen[0]["argv"] == [
        "notify-send",
        f"--app-name={APP_NAME}",
        f"--icon={APP_ID}",
        APP_NAME,
        "Fork updated 1 -> 2",
    ]
    assert seen[0]["start_new_session"] is True
    assert seen[0]["stdin"] == subprocess.DEVNULL


@pytest.mark.parametrize(
    ("cls", "argv"),
    [
        (ui.ZenityUI, ["zenity", "--notification", f"--text={APP_NAME}: hi"]),
        (ui.KDialogUI, ["kdialog", "--title", APP_NAME, "--passivepopup", "hi", "10"]),
    ],
)
def test_notify_falls_back_to_the_passive_popup(
    cls: type[ui.ZenityUI] | type[ui.KDialogUI], argv: list[str]
) -> None:
    dialog, runner, seen = _notifier(cls, None)
    dialog.notify("hi")
    assert runner.argvs == []
    assert seen[0]["argv"] == argv


def test_notify_that_cannot_start_only_logs(caplog: pytest.LogCaptureFixture) -> None:
    dialog, _runner, _seen = _notifier(ui.ZenityUI, None, FileNotFoundError(2, "No such file"))
    dialog.notify("hi")
    assert "cannot show a desktop notification" in caplog.text


def test_dialog_base_has_no_passive_popup() -> None:
    dialog = ui._DialogUI(runner=RecordingRunner(), env={})
    with pytest.raises(NotImplementedError):
        dialog._passive("x")
