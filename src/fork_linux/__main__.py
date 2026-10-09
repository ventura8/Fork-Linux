"""``python3 -m fork_linux``: the same as the ``fork-linux`` command."""

from __future__ import annotations

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
