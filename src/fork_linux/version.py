"""The fork-linux version: baked in at build time, else read from the repo's VERSION file.

Built packages ship a generated ``fork_linux/_build.py`` with ``VERSION`` and
``FLAVOR``. A source checkout has no such module, so the version comes from
the ``VERSION`` file (the single source of truth), found upward from the
package directory exactly like ``scripts/read-version.py``, with ``-dev``
appended.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from types import ModuleType

UNKNOWN = "0+unknown"
SOURCE_FLAVOR = "source"
_BUILD_MODULE = "fork_linux._build"


def _build_info() -> ModuleType | None:
    """The generated build module, or ``None`` in a source checkout."""
    try:
        return importlib.import_module(_BUILD_MODULE)
    except ImportError:
        return None


def _package_dir() -> Path:
    """Directory of the ``fork_linux`` package (a seam for tests)."""
    return Path(__file__).resolve().parent


def find_version_file(start: Path | None = None) -> Path | None:
    """The nearest ``VERSION`` file at or above ``start`` (default: the package dir)."""
    origin = _package_dir() if start is None else start
    for candidate in (origin, *origin.parents):
        path = candidate / "VERSION"
        if path.is_file():
            return path
    return None


def read_version_file(path: Path) -> str:
    """First line of a VERSION file; raises ``ValueError`` if it is empty or a comment."""
    text = path.read_text(encoding="utf-8").strip()
    line = text.splitlines()[0].strip() if text else ""
    if not line or line.startswith("#"):
        raise ValueError(f"{path} does not start with a version")
    return line


def get_version() -> str:
    """``VERSION`` from the build module, else ``<VERSION file>-dev``, else ``0+unknown``."""
    build = _build_info()
    if build is not None and getattr(build, "VERSION", ""):
        return str(build.VERSION)
    path = find_version_file()
    if path is None:
        return UNKNOWN
    try:
        return f"{read_version_file(path)}-dev"
    except (OSError, ValueError):
        return UNKNOWN


def get_flavor() -> str:
    """Packaging flavour baked in at build time (deb, rpm, flatpak, ...), else ``source``."""
    build = _build_info()
    flavor = getattr(build, "FLAVOR", "") if build is not None else ""
    return str(flavor) if flavor else SOURCE_FLAVOR
