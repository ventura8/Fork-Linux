"""Bootstrap state of one Wine prefix: ``<prefix>/.fork-linux/state.json``.

The file records which setup steps completed (each with a revision and a hash
of its inputs, so ``setup`` resumes and re-runs exactly the steps whose inputs
changed) plus a few facts other commands read, under well-known dotted keys:

``setup.complete`` (bool), ``fork.version`` (str), ``wine.provider`` (str),
``wine.build`` (str).

Writes are atomic and the file is private (0600). A corrupt file is never
silently replaced: :meth:`State.load` raises :class:`IntegrityFailed`.
"""

from __future__ import annotations

import errno
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import fsutil
from .errors import ForkLinuxError, IntegrityFailed

SCHEMA = 1
MODE = 0o600
# state.json holds a few markers; anything bigger is not ours (and is never read whole).
MAX_BYTES = 4 * 1024 * 1024

SETUP_COMPLETE = "setup.complete"
FORK_VERSION = "fork.version"
WINE_PROVIDER = "wine.provider"
WINE_BUILD = "wine.build"

_STEPS = "steps"


def _fresh() -> dict[str, Any]:
    """The document of a prefix that has no state yet."""
    return {"schema": SCHEMA, _STEPS: {}}


def _split(dotted_key: str) -> list[str]:
    """``"a.b.c"`` -> ``["a", "b", "c"]``; empty components are rejected."""
    parts = dotted_key.split(".")
    if not all(parts):
        raise ValueError(f"invalid state key: {dotted_key!r}")
    return parts


def _now() -> str:
    """Local time with offset, to the second."""
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _corrupt(path: Path, why: str) -> IntegrityFailed:
    """The error raised for an unusable state file."""
    return IntegrityFailed(
        f"the setup state file {path} is unusable: {why}",
        hint=f"move {path} aside and run 'fork-linux setup' again; every step is re-checked",
    )


def _read(path: Path) -> bytes | None:
    """Raw bytes of ``path`` (never through a symlink), or ``None`` if it does not exist."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise _corrupt(path, "it is a symbolic link") from None
        raise _unreadable(path, exc) from exc
    try:
        with os.fdopen(fd, "rb") as handle:
            data = handle.read(MAX_BYTES + 1)
    except OSError as exc:
        raise _unreadable(path, exc) from exc
    if len(data) > MAX_BYTES:
        raise _corrupt(path, f"it is larger than {MAX_BYTES} bytes")
    return data


def _unreadable(path: Path, exc: OSError) -> ForkLinuxError:
    """The error raised when ``path`` exists but cannot be read."""
    return ForkLinuxError(f"cannot read {path}: {exc.strerror or exc}")


class State:
    """In-memory view of ``state.json``; call :meth:`save` to persist changes."""

    def __init__(self, path: Path, data: dict[str, Any] | None = None, *, is_new: bool = False) -> None:
        self.path = Path(path)
        self.data: dict[str, Any] = _fresh() if data is None else data
        self.is_new = is_new

    @classmethod
    def load(cls, path: Path) -> State:
        """Read ``path``; a missing file gives a fresh state, a corrupt one raises."""
        path = Path(path)
        raw = _read(path)
        if raw is None:
            return cls(path, is_new=True)
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise _corrupt(path, f"not valid JSON ({exc})") from None
        if not isinstance(data, dict):
            raise _corrupt(path, "the top level is not a JSON object")
        schema = data.get("schema")
        if not isinstance(schema, int) or isinstance(schema, bool) or schema < 1:
            raise _corrupt(path, "it has no valid schema number")
        if schema > SCHEMA:
            raise IntegrityFailed(
                f"the setup state file {path} uses schema {schema}, newer than this fork-linux ({SCHEMA})",
                hint="update fork-linux, or move the file aside to set up again with this version",
            )
        steps = data.setdefault(_STEPS, {})
        if not isinstance(steps, dict):
            raise _corrupt(path, "'steps' is not a JSON object")
        return cls(path, data)

    def get(self, dotted_key: str, default: Any = None) -> Any:
        """The value at ``dotted_key`` (``"fork.version"``), or ``default``."""
        node: Any = self.data
        for part in _split(dotted_key):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def set(self, dotted_key: str, value: Any) -> None:
        """Store ``value`` at ``dotted_key``, creating intermediate objects."""
        *parents, leaf = _split(dotted_key)
        node = self.data
        for part in parents:
            child = node.setdefault(part, {})
            if not isinstance(child, dict):
                raise TypeError(f"state key {part!r} in {dotted_key!r} is not an object")
            node = child
        node[leaf] = value

    def _steps(self) -> dict[str, Any]:
        """The ``steps`` object (recreated if something replaced it)."""
        steps = self.data.get(_STEPS)
        if not isinstance(steps, dict):
            steps = self.data[_STEPS] = {}
        return steps

    def step_marker(self, step_id: str) -> dict[str, Any] | None:
        """A copy of the completion marker of ``step_id``, or ``None``."""
        marker = self._steps().get(step_id)
        return dict(marker) if isinstance(marker, dict) else None

    def set_step_marker(self, step_id: str, rev: int, inputs_hash: str) -> dict[str, Any]:
        """Record that ``step_id`` (revision ``rev``) completed with ``inputs_hash``."""
        marker = {"rev": rev, "inputs_hash": inputs_hash, "completed": _now()}
        self._steps()[step_id] = marker
        return dict(marker)

    def clear_step_marker(self, step_id: str) -> None:
        """Forget that ``step_id`` completed (no-op if it never did)."""
        self._steps().pop(step_id, None)

    def save(self) -> None:
        """Write the state atomically with mode 0600."""
        text = json.dumps(self.data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        fsutil.atomic_write(self.path, text, mode=MODE)
        self.is_new = False
