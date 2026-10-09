"""Exit codes and the exception hierarchy shared by every fork-linux module.

Every user-facing failure is a :class:`ForkLinuxError` subclass carrying the
exit code the CLI returns. The table is mirrored in AGENTS.md §5; a test keeps
both in sync.
"""

from __future__ import annotations

import enum


class ExitCode(enum.IntEnum):
    """Process exit codes of ``fork-linux`` (AGENTS.md §5)."""

    OK = 0
    ERROR = 1
    USAGE = 2
    NOT_SET_UP = 10
    SETUP_FAILED = 11
    DOWNLOAD_FAILED = 12
    INTEGRITY_FAILED = 13
    WINE_UNAVAILABLE = 14
    FORK_RUNNING = 15
    LOCKED = 16
    CHECKS_FAILED = 17
    DECLINED = 18
    UNSUPPORTED_ENV = 19
    NOT_FOUND = 20
    INTERRUPTED = 130


class ForkLinuxError(Exception):
    """Base class: a failure with a user-facing message, hint and exit code."""

    exit_code: ExitCode = ExitCode.ERROR

    def __init__(self, message: str, *, hint: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint

    def __str__(self) -> str:
        return self.message


class UsageError(ForkLinuxError):
    """Bad arguments, or a path that does not exist."""

    exit_code = ExitCode.USAGE


class NotSetUpError(ForkLinuxError):
    """Setup is incomplete and the caller asked us not to run it."""

    exit_code = ExitCode.NOT_SET_UP


class SetupFailed(ForkLinuxError):
    """A bootstrap step failed; ``step`` names it so the next run resumes there."""

    exit_code = ExitCode.SETUP_FAILED

    def __init__(self, step: str, message: str, *, hint: str = "") -> None:
        super().__init__(f"setup step '{step}' failed: {message}", hint=hint)
        self.step = step


class DownloadFailed(ForkLinuxError):
    """Network/HTTP failure after retries, or a cache miss while offline."""

    exit_code = ExitCode.DOWNLOAD_FAILED


class IntegrityFailed(ForkLinuxError):
    """Size/sha256 mismatch, unsafe archive member, malformed PE, bad manifest."""

    exit_code = ExitCode.INTEGRITY_FAILED


class WineUnavailable(ForkLinuxError):
    """No usable Wine: missing, too old, or missing host libraries."""

    exit_code = ExitCode.WINE_UNAVAILABLE


class ForkRunning(ForkLinuxError):
    """The operation needs Fork to be closed first."""

    exit_code = ExitCode.FORK_RUNNING


class Locked(ForkLinuxError):
    """Another setup/update/rollback/uninstall holds the lock."""

    exit_code = ExitCode.LOCKED


class ChecksFailed(ForkLinuxError):
    """``doctor`` reported at least one failing check."""

    exit_code = ExitCode.CHECKS_FAILED


class Declined(ForkLinuxError):
    """The user declined a consent or confirmation prompt."""

    exit_code = ExitCode.DECLINED


class UnsupportedEnvironment(ForkLinuxError):
    """Not x86_64, running as root without --allow-root, or Python < 3.10."""

    exit_code = ExitCode.UNSUPPORTED_ENV


class NotFound(ForkLinuxError):
    """A snapshot, version or key that the user named does not exist."""

    exit_code = ExitCode.NOT_FOUND
