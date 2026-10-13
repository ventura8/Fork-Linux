"""Locate files that ship with fork-linux, relative to where it is installed.

Two layouts are supported:

* installed: ``<root>/share/fork-linux/fork_linux/resources.py`` with shims in
  ``<root>/lib/fork-linux/win64``, helpers in ``<root>/lib/fork-linux`` and
  the bridge daemon ``<root>/lib/fork-linux/fl-bridge-helper`` (with its
  persona symlinks ``fl-winexec`` / ``fl-askpass`` / ``fl-ssh-askpass``);
* source checkout: ``<repo>/src/fork_linux/resources.py`` with helpers in
  ``<repo>/libexec`` and the freshly built bridge (shims and daemon, no
  persona links) in ``<repo>/build-bridge/bridge`` (``scripts/build-bridge.sh``;
  another ``<repo>/build*/bridge`` is used when that one does not exist).
"""

from __future__ import annotations

import os
from pathlib import Path

SHIMS_ENV = "FORK_LINUX_SHIMS_DIR"
LIBEXEC_ENV = "FORK_LINUX_LIBEXEC_DIR"
HOST_HELPER = "fork-linux-host"
BRIDGE_HELPER = "fl-bridge-helper"
BRIDGE_BUILD = "build-bridge"
BUILD_SCRIPT = ("scripts", "build-bridge.sh")


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
    for build in [root / BRIDGE_BUILD, *sorted(root.glob("build*"))]:
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


def bridge_helper_path() -> Path:
    """``fl-bridge-helper``: next to the other helpers when installed, next to the shims in a checkout.

    ``FORK_LINUX_LIBEXEC_DIR`` overrides both. In a checkout without a bridge
    build the path points where ``scripts/build-bridge.sh`` would put it.
    """
    if os.environ.get(LIBEXEC_ENV, "") or not is_source_tree():
        return libexec_dir() / BRIDGE_HELPER
    shims = shims_dir()
    return (shims if shims is not None else install_root() / BRIDGE_BUILD / "bridge") / BRIDGE_HELPER


def bridge_build_script() -> Path | None:
    """``scripts/build-bridge.sh`` in a source checkout (None when installed or missing)."""
    if not is_source_tree():
        return None
    script = install_root().joinpath(*BUILD_SCRIPT)
    return script if script.is_file() else None
