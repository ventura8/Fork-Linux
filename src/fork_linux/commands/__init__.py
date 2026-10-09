"""fork-linux subcommands.

Each module named in :data:`COMMANDS` exposes ``register(subparsers)``, which
adds its parser(s) and sets ``func=handler`` where ``handler(args, ctx) -> int``
(``ctx`` is a :class:`fork_linux.cli.AppContext`). The CLI imports them in
this order, which is also the order of ``fork-linux --help``.
"""

from __future__ import annotations

COMMANDS: list[str] = [
    "setup",
    "run",
    "update",
    "snapshot",
    "settings",
    "desktop",
    "ssh",
    "gitbridge",
    "logs",
    "doctor",
    "uninstall",
    "status",
    "version",
    "about",
    "config",
]
