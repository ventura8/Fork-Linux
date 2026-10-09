"""Locate files that ship with fork-linux, relative to where it is installed.

Two layouts are supported:

* installed: ``<root>/share/fork-linux/fork_linux/resources.py`` with shims in
  ``<root>/lib/fork-linux/win64`` and helpers in ``<root>/lib/fork-linux``;
* source checkout: ``<repo>/src/fork_linux/resources.py`` with helpers in
  ``<repo>/libexec`` and freshly built shims in ``<repo>/build*/bridge``.
"""

from __future__ import annotations

import os
from pathlib import Path

SHIMS_ENV = "FORK_LINUX_SHIMS_DIR"
LIBEXEC_ENV = "FORK_LINUX_LIBEXEC_DIR"
HOST_HELPER = "fork-linux-host"


def _here() -> Path:
    """Absolute path of this module (a seam for tests)."""
    return Path(__file__).resolve()


def is_source_tree() -> bool:
    """True when running from a repository checkout (``<repo>/src/fork_linux``)."""
    parents = _here().parents
    if len(parents) < 3 or parents[1].name != "src":
        return False
    repo = parents[2]
    return (repo / "meson.build").exists() or (repo / "VERSION").exists()


def install_root() -> Path:
    """The installation prefix (e.g. ``/usr``), or the repository root in a checkout."""
    parents = _here().parents
    if is_source_tree():
        return parents[2]
    return parents[min(3, len(parents) - 1)]


def data_path(*parts: str) -> Path:
    """A file under the package data directory (``fork_linux/data``)."""
    return _here().parent.joinpath("data", *parts)


def manifest_path() -> Path:
    """The packaged runtime manifest."""
    return data_path("runtime-manifest.json")


def shims_dir() -> Path | None:
    """Directory holding the Windows shims, or ``None`` if none were built."""
    override = os.environ.get(SHIMS_ENV, "")
    if override:
        return Path(override)
    root = install_root()
    if not is_source_tree():
        return root / "lib" / "fork-linux" / "win64"
    for build in sorted(root.glob("build*")):
        candidate = build / "bridge"
        if candidate.is_dir():
            return candidate
    return None


def libexec_dir() -> Path:
    """Directory holding private helper executables."""
    override = os.environ.get(LIBEXEC_ENV, "")
    if override:
        return Path(override)
    root = install_root()
    if is_source_tree():
        return root / "libexec"
    return root / "lib" / "fork-linux"


def host_helper_path() -> Path:
    """The ``fork-linux-host`` helper (terminal/reveal/open/diff/merge)."""
    return libexec_dir() / HOST_HELPER
