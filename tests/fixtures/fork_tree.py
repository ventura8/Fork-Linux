"""Synthetic Fork for Windows install trees inside a fake Wine prefix.

Nothing here is a real Fork file: executables are a few placeholder bytes, the
nuspec is hand-written. Used by the fork_layout / fork_install / fork_settings /
snapshots / updates tests.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fork_linux.fork_layout import ForkLayout
from fork_linux.paths import Paths

USER = "tester"
NUSPEC_NS = "http://schemas.microsoft.com/packaging/2010/07/nuspec.xsd"
NUSPEC = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    f'<package xmlns="{NUSPEC_NS}">\n'
    "  <metadata>\n"
    "    <id>Fork</id>\n"
    "    <version>{version}</version>\n"
    "    <authors>placeholder</authors>\n"
    "  </metadata>\n"
    "</package>\n"
)


def make_paths(root: Path) -> Paths:
    """A Paths tree under ``root`` (the prefix lives inside data_dir, as by default)."""
    base = Path(root) / "xdg"
    data_dir = base / "data" / "fork-linux"
    return Paths(
        config_dir=base / "config" / "fork-linux",
        data_dir=data_dir,
        cache_dir=base / "cache" / "fork-linux",
        state_dir=base / "state" / "fork-linux",
        runtime_dir=base / "run" / "fork-linux",
        prefix=data_dir / "prefix",
    )


def make_layout(root: Path, user: str = USER) -> ForkLayout:
    """A ForkLayout over :func:`make_paths` (nothing is created on disk)."""
    return ForkLayout(make_paths(root), user)


def write_sq_version(layout: ForkLayout, version: str) -> None:
    """Write ``current/sq.version`` naming ``version``."""
    layout.current_dir.mkdir(parents=True, exist_ok=True)
    layout.sq_version_file.write_text(NUSPEC.format(version=version), encoding="utf-8")


def install_fork(layout: ForkLayout, version: str = "2.23.2") -> None:
    """Lay out a Velopack-style Fork install of ``version`` with placeholder files."""
    current = layout.current_dir
    current.mkdir(parents=True, exist_ok=True)
    layout.exe.write_bytes(b"MZ placeholder Fork.exe " + version.encode())
    layout.ri_exe.write_bytes(b"MZ placeholder Fork.RI.exe")
    layout.askpass_exe.write_bytes(b"MZ placeholder Fork.AskPass.exe")
    write_sq_version(layout, version)
    lib = current / "lib" / "net"
    lib.mkdir(parents=True, exist_ok=True)
    (lib / "Fork.Core.dll").write_bytes(b"MZ" + bytes(range(256)) * 4)
    layout.update_exe.write_bytes(b"MZ placeholder Update.exe")
    layout.packages_dir.mkdir(parents=True, exist_ok=True)
    (layout.packages_dir / f"Fork-{version}-full.nupkg").write_bytes(b"PK placeholder " + version.encode())


def write_settings(layout: ForkLayout, data: dict[str, Any]) -> None:
    """Write Fork's settings.json the way Fork does (indented JSON)."""
    layout.settings_file.parent.mkdir(parents=True, exist_ok=True)
    layout.settings_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
