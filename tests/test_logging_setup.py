"""Tests for fork_linux.logging_setup: redaction, handlers, levels and rotation."""

from __future__ import annotations

import io
import logging
import stat
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from fork_linux import logging_setup
from fork_linux.logging_setup import RedactFilter, configure, redact, reset
from fork_linux.paths import Paths

GHP = "ghp_" + "a1B2" * 9
PAT = "github_pat_" + "11ABCDEFG0" + "x" * 40
GLPAT = "glpat-" + "Ab3_-" * 4


@pytest.fixture(autouse=True)
def _clean_handlers() -> Iterator[None]:
    reset()
    yield
    reset()


@pytest.fixture
def paths(xdg: Path) -> Paths:
    return Paths.from_env()


def _log_text(paths: Paths) -> str:
    for handler in logging.getLogger("fork_linux").handlers:
        handler.flush()
    return (paths.logs_dir / "fork-linux.log").read_text(encoding="utf-8")


# --------------------------------------------------------------------------- redact


@pytest.mark.parametrize(
    ("raw", "secret", "kept"),
    [
        (f"token {GHP} end", GHP, "ghp_***"),
        ("gho_" + "Z" * 36, "Z" * 36, "gho_***"),
        ("ghu_" + "Y" * 36, "Y" * 36, "ghu_***"),
        ("ghs_" + "X" * 36, "X" * 36, "ghs_***"),
        (f"pat={PAT}", PAT[11:], "github_pat_***"),
        (f"gitlab {GLPAT}", GLPAT[6:], "glpat-***"),
        ("Authorization: Bearer abc.def.ghi", "abc.def.ghi", "Authorization: Bearer ***"),
        ("http.extraHeader=Authorization: Basic dXNlcjpwYXNz", "dXNlcjpwYXNz", "Authorization: Basic ***"),
        ("authorization:rawsecret", "rawsecret", "authorization:***"),
        ("https://user:hunter2@example.com/repo.git", "hunter2", "https://user:***@example.com"),
        ("ssh://git:pw@host:22/x", ":pw@", "ssh://git:***@host"),
        ("https://user:p@ss@host:8080/x", "ss@host", "https://user:***@host:8080/x"),
        ("https://example.com/cb?password=letmein&x=1", "letmein", "password=***&x=1"),
        ("token=abcdef secret=xyz", "abcdef", "token=*** secret=***"),
        (
            "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaA\n-----END OPENSSH PRIVATE KEY-----",
            "b3BlbnNzaA",
            "[redacted private key]",
        ),
    ],
)
def test_redact_masks_secrets(raw: str, secret: str, kept: str) -> None:
    cleaned = redact(raw)
    assert secret not in cleaned
    assert kept in cleaned


@pytest.mark.parametrize(
    "text",
    [
        "wine C:\\users\\me\\AppData\\Local\\Fork\\current\\Fork.exe",
        "http://localhost:8080/path@x",
        "ghp_short",
        "git@github.com:ventura8/Fork-Linux.git",
        "",
    ],
)
def test_redact_leaves_ordinary_text_alone(text: str) -> None:
    assert redact(text) == text


def test_filter_survives_mismatched_format_arguments(paths: Paths) -> None:
    logger = configure(paths)
    logger.warning("two %s %s", GHP)
    logger.warning("number %d", "not-a-number")
    text = _log_text(paths)
    assert "two %s %s (unformattable arguments:" in text
    assert "number %d (unformattable arguments: ('not-a-number',))" in text
    assert GHP not in text
    assert "ghp_***" in text


# --------------------------------------------------------------------------- configure


def test_configure_creates_private_log_file(paths: Paths) -> None:
    logger = configure(paths)
    assert logger.name == "fork_linux"
    assert logger.level == logging.DEBUG
    logging.getLogger("fork_linux.download").debug("fetching %s", "x")
    log_file = paths.logs_dir / "fork-linux.log"
    assert stat.S_IMODE(log_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(paths.logs_dir.stat().st_mode) == 0o700
    text = _log_text(paths)
    assert "DEBUG fork_linux.download[" in text
    assert "fetching x" in text


@pytest.mark.parametrize(
    ("verbose", "level"),
    [(-1, logging.ERROR), (0, logging.WARNING), (1, logging.INFO), (2, logging.DEBUG), (5, logging.DEBUG)],
)
def test_console_level_follows_verbosity(paths: Paths, verbose: int, level: int) -> None:
    logger = configure(paths, verbose)
    console = [h for h in logger.handlers if not hasattr(h, "baseFilename")]
    assert [h.level for h in console] == [level]


def test_console_format_and_redaction(paths: Paths, capsys: pytest.CaptureFixture[str]) -> None:
    configure(paths, verbose=0)
    log = logging.getLogger("fork_linux.cli")
    log.info("hidden at default verbosity")
    log.warning("cloning https://me:%s@example.com with %s", "pw123", GHP)
    err = capsys.readouterr().err
    assert "hidden" not in err
    assert err.startswith("fork-linux: warning: cloning https://me:***@example.com with ghp_***")
    assert "pw123" not in err
    assert GHP not in err
    assert "pw123" not in _log_text(paths)
    assert GHP not in _log_text(paths)


def test_console_follows_replaced_stderr(paths: Paths, monkeypatch: pytest.MonkeyPatch) -> None:
    configure(paths)
    first, second = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stderr", first)
    logging.getLogger("fork_linux").error("one")
    monkeypatch.setattr(sys, "stderr", second)
    logging.getLogger("fork_linux").error("two")
    assert first.getvalue() == "fork-linux: error: one\n"
    assert second.getvalue() == "fork-linux: error: two\n"


def _log_exception(message: str) -> None:
    try:
        raise RuntimeError(f"bad token {GHP}")
    except RuntimeError:
        logging.getLogger("fork_linux.x").exception(message)


def test_tracebacks_are_redacted_and_only_verbose_on_console(paths: Paths, capsys: pytest.CaptureFixture[str]) -> None:
    configure(paths, verbose=0)
    _log_exception("failed")
    err = capsys.readouterr().err
    assert err == "fork-linux: error: failed\n"
    text = _log_text(paths)
    assert "Traceback" in text
    assert "RuntimeError: bad token ghp_***" in text
    assert GHP not in text

    configure(paths, verbose=2)
    _log_exception("failed again")
    err = capsys.readouterr().err
    assert "Traceback" in err
    assert GHP not in err


def test_filter_redacts_preformatted_exc_text_and_stack_info() -> None:
    record = logging.LogRecord("fork_linux", logging.ERROR, __file__, 1, "msg %s", (GHP,), None)
    record.exc_text = f"Traceback ... {GHP}"
    record.stack_info = f"Stack ... {GHP}"
    assert RedactFilter().filter(record)
    assert record.getMessage() == "msg ghp_***"
    assert record.args is None
    assert GHP not in record.exc_text
    assert GHP not in record.stack_info


def test_configure_is_idempotent_and_reset_removes_handlers(paths: Paths) -> None:
    root_logger = logging.getLogger("fork_linux")
    foreign = logging.NullHandler()
    root_logger.addHandler(foreign)
    try:
        configure(paths)
        configure(paths, verbose=1)
        owned = [h for h in root_logger.handlers if getattr(h, "_fork_linux_handler", False)]
        assert len(owned) == 2
        reset()
        assert root_logger.handlers == [foreign], "handlers we did not add are left alone"
    finally:
        root_logger.removeHandler(foreign)


def test_unwritable_log_dir_falls_back_to_console(
    xdg: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    blocker = tmp_path / "state-is-a-file"
    blocker.write_text("x", encoding="utf-8")
    p = Paths.from_env({"HOME": str(xdg), "XDG_STATE_HOME": str(blocker)})
    logger = configure(p)
    assert len(logger.handlers) == 1
    assert "cannot write the log file" in capsys.readouterr().err


def test_symlinked_log_file_is_never_written_through(
    paths: Paths, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    victim = tmp_path / "victim.txt"
    victim.write_text("keep\n", encoding="utf-8")
    paths.logs_dir.mkdir(parents=True)
    (paths.logs_dir / "fork-linux.log").symlink_to(victim)
    logger = configure(paths)
    logger.error("must not land in the victim")
    assert len(logger.handlers) == 1
    assert "cannot write the log file" in capsys.readouterr().err
    assert victim.read_text(encoding="utf-8") == "keep\n"


def test_log_rotation_keeps_private_backups(paths: Paths, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(logging_setup, "MAX_BYTES", 300)
    configure(paths)
    log = logging.getLogger("fork_linux.rot")
    for index in range(40):
        log.debug("line %03d %s", index, "x" * 40)
    rotated = paths.logs_dir / "fork-linux.log.1"
    assert rotated.exists()
    assert stat.S_IMODE(rotated.stat().st_mode) == 0o600
    assert stat.S_IMODE((paths.logs_dir / "fork-linux.log").stat().st_mode) == 0o600
    assert not (paths.logs_dir / "fork-linux.log.6").exists()
