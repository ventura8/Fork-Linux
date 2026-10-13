"""Logging for the ``fork_linux`` logger tree: rotating private log file + terse stderr.

Every record passes through :class:`RedactFilter`, so access tokens,
``Authorization`` headers and URL passwords never reach a log file, a console
or a ``logs --bundle`` archive.
"""

from __future__ import annotations

import logging
import os
import re
import sys
from collections.abc import Callable
from logging.handlers import RotatingFileHandler
from typing import IO, Any

from .paths import Paths

LOGGER_NAME = "fork_linux"
LOG_FILE = "fork-linux.log"
MAX_BYTES = 1024 * 1024
BACKUPS = 5
MASK = "***"

_FILE_FORMAT = "%(asctime)s %(levelname)s %(name)s[%(process)d]: %(message)s"
_OWNED = "_fork_linux_handler"


def _mask_authorization(match: re.Match[str]) -> str:
    """Keep the header name and auth scheme, mask the credential."""
    scheme = match.group(2)
    return f"{match.group(1)}{scheme + ' ' if scheme else ''}{MASK}"


# (pattern, replacement) pairs applied in order.
_RULES: tuple[tuple[re.Pattern[str], str | Callable[[re.Match[str]], str]], ...] = (
    (
        re.compile(r"-----BEGIN ([A-Z0-9 ]*)PRIVATE KEY-----.*?-----END \1PRIVATE KEY-----", re.DOTALL),
        "[redacted private key]",
    ),
    (re.compile(r"\b(gh[pousr]_)[A-Za-z0-9]{16,}"), r"\1" + MASK),
    (re.compile(r"\b(github_pat_)(?a:\w{16,})"), r"\1" + MASK),
    (re.compile(r"\b(glpat-)[A-Za-z0-9_\-]{16,}"), r"\1" + MASK),
    (
        re.compile(r"(?i)\b(authorization\s*:\s*)(?:(basic|bearer|token|digest|negotiate)\s+)?[^\s'\",;]+"),
        _mask_authorization,
    ),
    # Greedy up to the last '@' of the authority, so a password containing '@' is masked whole.
    (re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://[^/\s:@'\"]+:)[^/\s'\"]+@"), r"\1" + MASK + "@"),
    (
        re.compile(r"(?i)\b(password|passwd|pwd|passphrase|secret|token|access_token|private_token)=[^&\s'\"]+"),
        r"\1=" + MASK,
    ),
)


def redact(text: str) -> str:
    """Mask access tokens, ``Authorization`` values and passwords in ``text``."""
    for pattern, replacement in _RULES:
        text = pattern.sub(replacement, text)
    return text


def _message(record: logging.LogRecord) -> str:
    """The formatted message; a call with mismatched ``%`` arguments still yields text."""
    try:
        return record.getMessage()
    except (TypeError, ValueError):
        return f"{record.msg} (unformattable arguments: {record.args!r})"


class RedactFilter(logging.Filter):
    """Rewrites each record's message (and traceback) through :func:`redact`."""

    _formatter = logging.Formatter()

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(_message(record))
        record.args = None
        if record.exc_info and not record.exc_text:
            record.exc_text = self._formatter.formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        if record.stack_info:
            record.stack_info = redact(record.stack_info)
        return True


class _ConsoleFormatter(logging.Formatter):
    """``fork-linux: warning: message``; tracebacks only when ``show_traceback``."""

    def __init__(self, show_traceback: bool) -> None:
        super().__init__()
        self.show_traceback = show_traceback

    def format(self, record: logging.LogRecord) -> str:
        text = f"fork-linux: {record.levelname.lower()}: {record.getMessage()}"
        if self.show_traceback and record.exc_text:
            text = f"{text}\n{record.exc_text}"
        return text


class _StderrHandler(logging.StreamHandler):
    """A stream handler that writes to whatever ``sys.stderr`` is at emit time."""

    def __init__(self) -> None:
        super().__init__(sys.stderr)

    @property
    def stream(self) -> IO[str]:
        """The live ``sys.stderr`` (pytest and the CLI may swap it)."""
        return sys.stderr

    @stream.setter
    def stream(self, value: Any) -> None:
        """Ignore: the stream is always the live ``sys.stderr``."""


class _PrivateRotatingFileHandler(RotatingFileHandler):
    """A rotating file handler whose files are created 0600 and never written through a symlink."""

    def _open(self) -> IO[str]:
        """Open the current log file for appending, creating it 0600."""
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_CLOEXEC | os.O_NOFOLLOW
        fd = os.open(self.baseFilename, flags, 0o600)
        return open(fd, self.mode, encoding=self.encoding, errors=self.errors)


def _console_level(verbose: int) -> int:
    """-1 and below: errors only; 0: warnings; 1: info; 2+: debug."""
    if verbose < 0:
        return logging.ERROR
    return (logging.WARNING, logging.INFO)[verbose] if verbose < 2 else logging.DEBUG


def reset() -> None:
    """Remove and close every handler :func:`configure` installed."""
    logger = logging.getLogger(LOGGER_NAME)
    owned = [handler for handler in logger.handlers if getattr(handler, _OWNED, False)]
    for handler in owned:
        logger.removeHandler(handler)
        handler.close()


def configure(paths: Paths, verbose: int = 0) -> logging.Logger:
    """Configure the ``fork_linux`` logger: DEBUG to the rotating log file, ``verbose`` to stderr.

    Safe to call repeatedly (previous handlers are replaced). If the log
    directory cannot be created the file handler is skipped with a warning.
    """
    reset()
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.DEBUG)
    redactor = RedactFilter()

    console = _StderrHandler()
    console.setLevel(_console_level(verbose))
    console.setFormatter(_ConsoleFormatter(show_traceback=verbose >= 2))
    console.addFilter(redactor)
    setattr(console, _OWNED, True)
    logger.addHandler(console)

    log_path = paths.logs_dir / LOG_FILE
    try:
        paths.logs_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        file_handler = _PrivateRotatingFileHandler(
            log_path, maxBytes=MAX_BYTES, backupCount=BACKUPS, encoding="utf-8"
        )
    except OSError as exc:
        logger.warning("cannot write the log file %s: %s", log_path, exc)
        return logger
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(_FILE_FORMAT))
    file_handler.addFilter(redactor)
    setattr(file_handler, _OWNED, True)
    logger.addHandler(file_handler)
    return logger
