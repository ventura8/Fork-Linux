"""Safety guard of the real tiers: Wine and the official Fork run only inside a container.

On 2026-10-10 a developer's whole home directory was recursively deleted while agents ran
real-Fork tests on the host; the cause was never found. Since then every tier that starts
Wine, winetricks or Fork (``FL_E2E_FORK=1``, ``FL_REAL_WINE=1`` and any later ``FL_E2E_*`` /
``FL_REAL_*`` switch) refuses to run unless it is inside a container, where the only host
paths are the read-only source tree and a scratch root outside ``$HOME``
(``scripts/e2e-docker.sh``, ``scripts/ci-docker.sh``, ``scripts/ci-e2e-wine.sh``). There is no
host override.

The root helpers below are what ``tests/e2e/conftest.py`` uses to create and clean its
scratch root: they refuse ``/``, any home directory (``$HOME`` and the passwd entry's) and
any path with a symlink component, and they never follow a symlink while deleting.
"""

from __future__ import annotations

import os
import pwd
import shutil
import stat
from collections.abc import Iterable, Mapping
from pathlib import Path

import pytest

CONTAINER_MARKERS = ("/.dockerenv", "/run/.containerenv")
RUNNER = "scripts/e2e-docker.sh"
# FL_E2E_* / FL_REAL_* variables that configure a tier (paths, a version, a display, the
# runner's own knobs) rather than switch one on.
SETTINGS = frozenset(
    {
        "FL_E2E_ROOT",
        "FL_E2E_SEED",
        "FL_E2E_SEED_DIR",
        "FL_E2E_WINETRICKS_CACHE",
        "FL_E2E_DISPLAY",
        "FL_E2E_FORK_VERSION",
        "FL_E2E_SCRATCH",
        "FL_E2E_SLOTS",
        "FL_E2E_LOCK_DIR",
        "FL_E2E_IMAGE",
    }
)


class RefusedPath(Exception):
    """A scratch path the real tier must never create, clean or delete."""


def active_gates(env: Mapping[str, str]) -> list[str]:
    """The real-tier switches set in ``env`` (any ``FL_E2E_*`` / ``FL_REAL_*`` that is not a setting)."""
    return sorted(
        key
        for key, value in env.items()
        if key.startswith(("FL_E2E_", "FL_REAL_")) and key not in SETTINGS and value not in ("", "0")
    )


def in_container(markers: Iterable[str] = CONTAINER_MARKERS) -> bool:
    """True inside Docker (``/.dockerenv``) or Podman (``/run/.containerenv``)."""
    return any(os.path.lexists(marker) for marker in markers)


def refusal(env: Mapping[str, str], markers: Iterable[str] = CONTAINER_MARKERS) -> str | None:
    """Why this pytest run must stop at once, or None when it may go on."""
    gates = active_gates(env)
    if not gates or in_container(markers):
        return None
    return (
        f"refusing to run the real Wine / Fork tier on the host ({', '.join(f'{g}={env[g]}' for g in gates)}): "
        f"it runs only inside a container. Use {RUNNER} (e.g. '{RUNNER} pytest tests/e2e') or the "
        "scripts/ci-*.sh stages (FL_CI_STAGE=bridge ./scripts/ci-docker.sh). Nothing was created. "
        "Reason: on 2026-10-10 a home directory was deleted while real-Fork tests ran on the host "
        "(AGENTS.md hard rule 18)."
    )


def enforce(env: Mapping[str, str] | None = None, markers: Iterable[str] = CONTAINER_MARKERS) -> None:
    """End the pytest session (exit status 2) when a real tier is switched on outside a container."""
    reason = refusal(os.environ if env is None else env, markers)
    if reason is not None:
        pytest.exit(reason, returncode=2)


def home_dirs(env: Mapping[str, str]) -> list[Path]:
    """``$HOME`` and the passwd home of this uid, as given and resolved."""
    found: list[str] = []
    if env.get("HOME"):
        found.append(env["HOME"])
    try:
        found.append(pwd.getpwuid(os.getuid()).pw_dir)
    except KeyError:
        pass
    homes: list[Path] = []
    for home in found:
        for candidate in (Path(os.path.abspath(home)), Path(os.path.realpath(home))):
            if candidate not in homes:
                homes.append(candidate)
    return homes


def _no_symlink_components(path: Path, start: Path) -> None:
    """Refuse when ``path`` (below ``start``) has a symlink component; missing tails are fine."""
    current = start
    for part in path.relative_to(start).parts:
        current = current / part
        try:
            mode = os.lstat(current).st_mode
        except FileNotFoundError:
            return
        if stat.S_ISLNK(mode):
            raise RefusedPath(f"{path}: {current} is a symlink")


def check_root(root: Path, env: Mapping[str, str] | None = None) -> Path:
    """Validate an E2E scratch root and return it: absolute, symlink-free, not ``/``, not a home."""
    env = os.environ if env is None else env
    if not root.is_absolute():
        raise RefusedPath(f"{root}: the E2E root must be an absolute path")
    if ".." in root.parts:
        raise RefusedPath(f"{root}: the E2E root must not contain '..'")
    root = Path(os.path.normpath(root))
    if root == Path("/"):
        raise RefusedPath("/: the E2E root must not be the file system root")
    _no_symlink_components(root, Path("/"))
    for home in home_dirs(env):
        if root == home or home in root.parents or root in home.parents:
            raise RefusedPath(f"{root}: the E2E root must not be, contain or lie inside the home directory {home}")
    return root


def remove_inside(path: Path, root: Path) -> None:
    """Delete ``path``, which must lie strictly inside ``root``; symlinks are unlinked, never followed."""
    path = Path(os.path.abspath(path))
    if root not in path.parents:
        raise RefusedPath(f"{path}: outside the E2E root {root}")
    _no_symlink_components(path.parent, root)
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        return
    if stat.S_ISDIR(mode):
        if not shutil.rmtree.avoids_symlink_attacks:
            raise RefusedPath(f"{path}: this platform's rmtree may follow symlinks")
        shutil.rmtree(path)
    else:
        os.unlink(path)


def _write_marker(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)


def prepare_root(root: Path, marker: str, keep: Iterable[str] = (), env: Mapping[str, str] | None = None) -> Path:
    """Create ``root`` (0700) or clean a previous run in it, keeping ``home/<keep>`` entries.

    A non-empty root without ``marker`` (a regular file) was not created by the tier and is
    refused, as is every path :func:`check_root` refuses.
    """
    root = check_root(root, env)
    kept = set(keep)
    try:
        entries = sorted(os.scandir(root), key=lambda entry: entry.name)
    except FileNotFoundError:
        entries = []
    others = [entry for entry in entries if entry.name != marker]
    if others:
        try:
            marked = stat.S_ISREG(os.lstat(root / marker).st_mode)
        except FileNotFoundError:
            marked = False
        if not marked:
            raise RefusedPath(f"{root} is not empty and was not created by the E2E tier; refusing to clean it")
    for entry in others:
        if entry.name == "home" and entry.is_dir(follow_symlinks=False):
            for sub in sorted(os.scandir(entry.path), key=lambda item: item.name):
                if sub.name not in kept:
                    remove_inside(Path(sub.path), root)
        else:
            remove_inside(Path(entry.path), root)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    _write_marker(root / marker, "fork-linux E2E root\n")
    return root
