"""Dotted version parsing and comparison (``2.23.2``, ``11.0``, ``1.2.3-rc1``).

Only the leading numeric components take part in ordering; any suffix after the
first non-numeric character is kept for display but ignored for comparison, so
``2.23.2-beta`` == ``2.23.2``. Missing components count as zero (``11`` == ``11.0``).
"""

from __future__ import annotations

import re
from functools import total_ordering

_NUMERIC = re.compile(r"^\s*v?(\d+(?:\.\d+)*)")


@total_ordering
class Version:
    """A comparable dotted version."""

    __slots__ = ("raw", "parts")

    def __init__(self, raw: str) -> None:
        match = _NUMERIC.match(raw)
        if match is None:
            raise ValueError(f"not a version: {raw!r}")
        self.raw = raw.strip()
        parts = [int(p) for p in match.group(1).split(".")]
        while len(parts) > 1 and parts[-1] == 0:
            parts.pop()
        self.parts = tuple(parts)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Version):
            return NotImplemented
        return self.parts == other.parts

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, Version):
            return NotImplemented
        return self.parts < other.parts

    def __hash__(self) -> int:
        return hash(self.parts)

    def __repr__(self) -> str:
        return f"Version({self.raw!r})"

    def __str__(self) -> str:
        return self.raw


def parse(raw: str) -> Version:
    """Parse ``raw`` into a :class:`Version`; raises ``ValueError`` if it has no leading number."""
    return Version(raw)


def is_valid(raw: str) -> bool:
    """True if ``raw`` starts with a dotted number."""
    return _NUMERIC.match(raw) is not None


def compare(a: str, b: str) -> int:
    """Return -1, 0 or 1 as ``a`` is older than, equal to, or newer than ``b``."""
    va, vb = Version(a), Version(b)
    return (va > vb) - (va < vb)
