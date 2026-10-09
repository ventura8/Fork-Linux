#!/usr/bin/env python3
"""Print the Fork for Linux (unofficial) version from the repo VERSION file (single source of truth)."""

from __future__ import annotations

import sys
from pathlib import Path


def find_version_file() -> Path:
    """Return this script's repo VERSION, else the nearest VERSION above the cwd.

    The script's own repo root wins, so running it from another project's directory
    (one with its own VERSION) still prints Fork for Linux's version.
    """
    own = Path(__file__).resolve().parent.parent / "VERSION"
    if own.is_file():
        return own
    cwd = Path.cwd().resolve()
    for candidate in [cwd, *cwd.parents]:
        path = candidate / "VERSION"
        if path.is_file():
            return path
    raise FileNotFoundError("VERSION file not found (searched the repo root and upward from cwd)")


def read_version() -> str:
    """Return the first line of VERSION, stripped; raise ValueError when it is empty."""
    text = find_version_file().read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError("VERSION file is empty")
    # First line only; ignore comments/blank trailing lines.
    line = text.splitlines()[0].strip()
    if not line or line.startswith("#"):
        raise ValueError("VERSION file missing a version on the first line")
    return line


def main() -> int:
    """Print the version; exit 1 with a message on stderr when it cannot be read."""
    try:
        print(read_version())
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
