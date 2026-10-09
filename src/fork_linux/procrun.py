"""Run external commands: list argv only, never a shell, UTF-8 text with replacement.

:class:`Runner` is the production implementation; :class:`RecordingRunner`
records calls and returns canned results so tests never spawn real tools.
"""

from __future__ import annotations

import inspect
import logging
import os
import shlex
import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Union

from .errors import ForkLinuxError
from .logging_setup import redact

log = logging.getLogger(__name__)

TAIL_LINES = 20
# Bytes of a logged command's output read back to build the error message.
TAIL_BYTES = 64 * 1024


@dataclass
class Completed:
    """Result of one command."""

    argv: list[str]
    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        """True if the command exited with status 0."""
        return self.returncode == 0


class CommandError(ForkLinuxError):
    """A command run with ``check=True`` failed or could not be started."""

    def __init__(self, message: str, *, completed: Completed, hint: str = "") -> None:
        super().__init__(message, hint=hint)
        self.completed = completed


def format_argv(argv: Sequence[str]) -> str:
    """Shell-quoted, secret-redacted rendering of ``argv`` for messages and logs."""
    return redact(shlex.join(argv))


def tail(text: str, lines: int = TAIL_LINES) -> str:
    """The last ``lines`` lines of ``text`` (trailing whitespace dropped)."""
    return "\n".join(text.rstrip().splitlines()[-lines:])


def _normalise(argv: Sequence[str]) -> list[str]:
    """Validate ``argv`` and coerce path-like items to ``str``."""
    if isinstance(argv, (str, bytes)):
        raise TypeError("argv must be a list of arguments, not a string")
    items = [os.fspath(item) for item in argv]
    if not items:
        raise ValueError("argv must not be empty")
    return items


def _raise_for_status(completed: Completed, output: str) -> None:
    """Raise :class:`CommandError` naming the command, with the tail of ``output``."""
    if completed.ok:
        return
    message = f"command failed with exit code {completed.returncode}: {format_argv(completed.argv)}"
    detail = redact(tail(output))
    if detail:
        message = f"{message}\n{detail}"
    raise CommandError(message, completed=completed, hint="see the log for the full output")


def _spawn(args: list[str], **options: Any) -> Completed:
    """``subprocess.run`` mapped onto :class:`Completed`; timeouts raise, exec failures don't."""
    try:
        proc = subprocess.run(args, **options)
    except subprocess.TimeoutExpired as exc:
        raise ForkLinuxError(
            f"command timed out after {exc.timeout:g}s: {format_argv(args)}",
            hint="the program may be stuck or waiting for input; see the log",
        ) from exc
    except OSError as exc:
        code = 127 if isinstance(exc, FileNotFoundError) else 126
        return Completed(args, code, "", f"fork-linux: cannot execute {args[0]}: {exc}\n")
    return Completed(args, proc.returncode, proc.stdout or "", proc.stderr or "")


def _open_log(log_file: Path) -> BinaryIO:
    """Open ``log_file`` for appending; a new file is private (0600), never through a symlink."""
    try:
        log_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(log_file, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    except OSError as exc:
        raise ForkLinuxError(
            f"cannot open the log file {log_file}: {exc.strerror or exc}",
            hint="check that the log directory is writable and the file is not a symbolic link",
        ) from exc
    return os.fdopen(fd, "ab")


def _spawn_logged(args: list[str], log_file: Path, options: dict[str, Any]) -> tuple[Completed, str]:
    """Run with stdout+stderr appended to ``log_file``; return the result and the tail of the new output.

    Only the last :data:`TAIL_BYTES` of the command's output are read back
    (for :func:`_raise_for_status`), however much it wrote to the log.
    """
    stamp = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    with _open_log(log_file) as handle:
        handle.write(f"--- {stamp} $ {format_argv(args)}\n".encode("utf-8"))
        handle.flush()
        start = handle.tell()
        completed = _spawn(args, stdout=handle, stderr=subprocess.STDOUT, **options)
        # Only an exec failure leaves text in ``stderr``; record it in the log too.
        handle.write(completed.stderr.encode("utf-8"))
        handle.flush()
        end = handle.tell()
    with open(log_file, "rb") as reader:
        reader.seek(max(start, end - TAIL_BYTES))
        output = reader.read(end - reader.tell()).decode("utf-8", errors="replace")
    return completed, output


class Runner:
    """Spawns real processes."""

    def run(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
        timeout: float | None = None,
        check: bool = False,
        input: str | None = None,
        log_file: Path | None = None,
    ) -> Completed:
        """Run ``argv`` and return a :class:`Completed`.

        ``env`` replaces the whole environment when given; stdin is
        ``/dev/null`` unless ``input`` is passed. With ``log_file``, stdout and
        stderr are appended to that file instead of being captured.
        ``check=True`` raises :class:`CommandError` on a non-zero exit; a
        timeout always raises :class:`ForkLinuxError`. A missing executable
        yields exit code 127 (126 if it cannot be executed).
        """
        args = _normalise(argv)
        log.debug("run: %s (cwd=%s)", format_argv(args), cwd)
        options: dict[str, Any] = {
            "env": None if env is None else dict(env),
            "cwd": cwd,
            "timeout": timeout,
            "input": input,
            "stdin": subprocess.DEVNULL if input is None else None,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "check": False,
        }
        if log_file is None:
            completed = _spawn(args, capture_output=True, **options)
            output = completed.stderr
        else:
            completed, output = _spawn_logged(args, Path(log_file), options)
        log.debug("exit %d: %s", completed.returncode, args[0])
        if check:
            _raise_for_status(completed, output)
        return completed

    def which(self, name: str, path: str | None = None) -> str | None:
        """Absolute path of executable ``name`` on ``path`` (default ``$PATH``), or ``None``."""
        return shutil.which(name, path=path)


Response = Union[Completed, int, str, None, Callable[..., Any], list[Any]]


class RecordingRunner(Runner):
    """Test double: records every call and answers from :attr:`responses`.

    ``responses`` maps the basename of ``argv[0]`` (or the full ``argv[0]``)
    to a :class:`Completed` (its ``argv`` is replaced by the real one), an
    ``int`` exit code, a ``str`` stdout, a list of those (consumed in order,
    the last one repeats) or a callable ``fn(argv, **kwargs)`` returning any
    of those; it receives only the keyword arguments it declares (``env``,
    ``cwd``, ``timeout``, ``check``, ``input``, ``log_file``). Unknown
    commands succeed with empty output. ``responses`` may also be a single
    callable that answers every command. :attr:`which_map` overrides
    :meth:`which` per name.
    """

    def __init__(
        self,
        responses: Mapping[str, Response] | Callable[..., Any] | None = None,
        which_map: Mapping[str, str | None] | None = None,
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self.responses: dict[str, Response] | Callable[..., Any]
        if callable(responses):
            self.responses = responses
        else:
            self.responses = {
                key: list(value) if isinstance(value, list) else value for key, value in (responses or {}).items()
            }
        self.which_map: dict[str, str | None] = dict(which_map or {})

    @property
    def argvs(self) -> list[list[str]]:
        """Just the argv of every recorded call."""
        return [call["argv"] for call in self.calls]

    def run(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
        timeout: float | None = None,
        check: bool = False,
        input: str | None = None,
        log_file: Path | None = None,
    ) -> Completed:
        """Record the call and return the canned response."""
        args = _normalise(argv)
        options: dict[str, Any] = {
            "env": None if env is None else dict(env),
            "cwd": cwd,
            "timeout": timeout,
            "check": check,
            "input": input,
            "log_file": log_file,
        }
        self.calls.append({"argv": args, **options})
        completed = self._respond(args, options)
        output = completed.stderr
        if log_file is not None:
            output = completed.stdout + completed.stderr
            path = Path(log_file)
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(output)
            completed = replace(completed, stdout="", stderr="")
        if check:
            _raise_for_status(completed, output)
        return completed

    def which(self, name: str, path: str | None = None) -> str | None:
        """Answer from :attr:`which_map`, else fall back to the real lookup."""
        if name in self.which_map:
            return self.which_map[name]
        return super().which(name, path)

    def _respond(self, args: list[str], options: dict[str, Any]) -> Completed:
        """Resolve the configured response for ``args``."""
        table = self.responses
        if callable(table):
            return _as_completed(_call_response(table, args, options), args)
        key = os.path.basename(args[0])
        response = table[key] if key in table else table.get(args[0])
        if isinstance(response, list):
            response = response.pop(0) if len(response) > 1 else next(iter(response), None)
        if callable(response):
            response = _call_response(response, args, options)
        return _as_completed(response, args)


def _call_response(fn: Callable[..., Any], args: list[str], options: dict[str, Any]) -> Any:
    """Call ``fn(argv, **kwargs)`` passing only the keyword arguments it accepts."""
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return fn(args)
    if any(param.kind is inspect.Parameter.VAR_KEYWORD for param in params.values()):
        return fn(args, **options)
    return fn(args, **{name: value for name, value in options.items() if name in params})


def _as_completed(response: Any, args: list[str]) -> Completed:
    """Coerce a canned response into a :class:`Completed` for ``args``."""
    if response is None:
        return Completed(list(args), 0, "", "")
    if isinstance(response, Completed):
        return Completed(list(args), response.returncode, response.stdout, response.stderr)
    if isinstance(response, int):
        return Completed(list(args), response, "", "")
    if isinstance(response, str):
        return Completed(list(args), 0, response, "")
    raise TypeError(f"unsupported RecordingRunner response: {response!r}")
