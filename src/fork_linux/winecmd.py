"""Run Wine for our prefix: the resolved build, its environment and the common invocations.

:class:`WineInfo` describes one usable Wine (whichever provider found it);
:func:`build_env` turns it into the environment every Wine call uses, and the
helpers below run ``wine``, ``wineserver`` and ``regedit`` through a
:class:`~fork_linux.procrun.Runner`, so tests never start a real Wine.

Every environment built here disables winemenubuilder (AGENTS.md hard rule
12) and points ``WINEPREFIX`` at our own prefix, never at ``~/.wine``.
"""

from __future__ import annotations

import os
import pwd
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from . import fsutil, sandbox
from .errors import ForkLinuxError, UsageError, WineUnavailable
from .paths import Paths
from .procrun import Completed, Runner, format_argv, tail
from .registry import RegBatch

PROVIDERS = ("managed", "system", "flatpak", "custom")
DEFAULT_DEBUG = "-all"
MENUBUILDER_OFF = "winemenubuilder.exe=d"
DEFAULT_DLL_OVERRIDES = MENUBUILDER_OFF
# Inherited WINE* variables we keep: user tuning that does not point at another Wine.
KEEP_WINE_VARS = ("WINEESYNC", "WINEFSYNC")
VERSION_TIMEOUT = 30.0
WINESERVER_FLAGS = ("-w", "-k")
REG_TMP_DIR = "tmp"

# (?a:...) keeps the digits ASCII-only while \b and \s keep their Unicode meaning.
_VERSION_RE = re.compile(r"\bwine-((?a:\d+(?:\.\d+)*)[^\s()]*)")
_STAGING_RE = re.compile(r"staging", re.IGNORECASE)
_FORBIDDEN_USER_CHARS = frozenset('/\\<>:"|?*')
_REG_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_MENUBUILDER_NAMES = frozenset({"winemenubuilder", "winemenubuilder.exe"})


@dataclass(frozen=True)
class WineInfo:
    """One usable Wine installation.

    ``provider`` is ``managed``, ``system``, ``flatpak`` or ``custom``;
    ``build_id`` names the manifest build for ``managed`` (else ``None``);
    ``root`` is the directory holding ``bin/wine``. ``wow64`` is True for a
    new-style WoW64 build (32-bit Windows programs run without 32-bit host
    libraries).
    """

    provider: str
    build_id: str | None
    root: Path
    wine: Path
    wineserver: Path
    version: str
    staging: bool
    wow64: bool


def _check_user(name: str) -> str:
    """Return ``name`` if it is usable as ``C:\\users\\<name>``; :class:`UsageError` otherwise."""
    if (
        name in ("", ".", "..")
        or any(ch in _FORBIDDEN_USER_CHARS or ord(ch) < 0x20 or ch == "\x7f" for ch in name)
        or name != name.strip()
    ):
        raise UsageError(
            f"the user name {name!r} cannot be used as a Windows user directory",
            hint="set USER to a plain name without path separators or the characters <>:\"|?*",
        )
    return name


def windows_user(env: Mapping[str, str]) -> str:
    """The Wine user name: ``$USER``, else ``$LOGNAME``, else the password database.

    Wine names the profile directory ``C:\\users\\<name>`` after the Unix
    user, so this is also the directory name inside the prefix. Names with
    path separators (or other characters Windows forbids) raise
    :class:`UsageError`.
    """
    for var in ("USER", "LOGNAME"):
        value = env.get(var, "")
        if value:
            return _check_user(value)
    try:
        name = pwd.getpwuid(os.getuid()).pw_name
    except KeyError:
        raise UsageError(
            f"cannot determine the user name for uid {os.getuid()}",
            hint="set the USER environment variable",
        ) from None
    return _check_user(name)


def parse_wine_version(text: str) -> tuple[str, bool]:
    """``('11.0', True)`` for ``wine-11.0 (Staging)``; ``ValueError`` without a ``wine-N`` token."""
    match = _VERSION_RE.search(text)
    if match is None:
        raise ValueError(f"not a wine --version output: {text.strip()!r}")
    return match.group(1), _STAGING_RE.search(text) is not None


def query_version(runner: Runner, wine: Path, env: Mapping[str, str] | None = None) -> tuple[str, bool]:
    """Run ``wine --version`` and parse it; :class:`WineUnavailable` if Wine does not run."""
    argv = [str(wine), "--version"]
    try:
        completed = runner.run(argv, env=env, timeout=VERSION_TIMEOUT)
    except ForkLinuxError as exc:
        raise WineUnavailable(f"{wine} --version did not finish: {exc}", hint=_wine_hint(wine)) from exc
    if not completed.ok:
        detail = tail(completed.stderr or completed.stdout, 5)
        raise WineUnavailable(
            f"{wine} --version failed with exit code {completed.returncode}" + (f": {detail}" if detail else ""),
            hint=_wine_hint(wine),
        )
    try:
        return parse_wine_version(completed.stdout)
    except ValueError as exc:
        raise WineUnavailable(f"{wine} is not a usable Wine: {exc}", hint=_wine_hint(wine)) from None


def _wine_hint(wine: Path) -> str:
    """Hint for a Wine binary that does not run."""
    return (
        f"check that {wine} is a complete Wine installation; 'fork-linux doctor' lists missing host "
        "libraries, and 'fork-linux setup' reinstalls the managed Wine runtime"
    )


def _menubuilder_disabled(overrides: str) -> str:
    """``overrides`` with any winemenubuilder entry replaced by ``winemenubuilder.exe=d`` (last)."""
    entries = []
    for entry in overrides.split(";"):
        names, sep, mode = entry.partition("=")
        kept = [name for name in names.split(",") if name.strip() and name.strip().lower() not in _MENUBUILDER_NAMES]
        if kept:
            entries.append(",".join(kept) + sep + mode)
    entries.append(MENUBUILDER_OFF)
    return ";".join(entries)


def build_env(
    paths: Paths,
    info: WineInfo,
    *,
    user: str,
    base_env: Mapping[str, str],
    debug: str = DEFAULT_DEBUG,
    dll_overrides: str = DEFAULT_DLL_OVERRIDES,
    extra: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """The environment for every Wine call on our prefix.

    Starts from :func:`sandbox.clean_env` of ``base_env`` and drops inherited
    ``WINE*`` variables (they belong to some other Wine setup) except
    :data:`KEEP_WINE_VARS`. Then sets ``WINEPREFIX``, ``WINEARCH=win64``,
    ``WINESERVER``, ``WINELOADER``, ``PATH`` (``<root>/bin`` first),
    ``WINEDEBUG``, ``WINEDLLOVERRIDES`` (winemenubuilder is always disabled,
    whatever ``dll_overrides`` says), ``WINEHOME`` (``%HOME%`` on the Windows
    side) and ``FL_WINE`` (for bridge callbacks). ``extra`` is applied last.
    The Unix ``HOME`` is never changed.
    """
    _check_user(user)
    env = {
        key: value
        for key, value in sandbox.clean_env(base_env).items()
        if not key.startswith("WINE") or key in KEEP_WINE_VARS
    }
    old_path = env.get("PATH") or os.defpath
    env.update(
        {
            "WINEPREFIX": str(paths.prefix),
            "WINEARCH": "win64",
            "WINESERVER": str(info.wineserver),
            "WINELOADER": str(info.wine),
            "PATH": f"{info.root / 'bin'}:{old_path}",
            "WINEDEBUG": debug,
            "WINEDLLOVERRIDES": _menubuilder_disabled(dll_overrides),
            "WINEHOME": "C:\\users\\" + user,
            "FL_WINE": str(info.wine),
        }
    )
    env.update(extra or {})
    return env


def run(
    runner: Runner,
    env: Mapping[str, str],
    info: WineInfo,
    args: Sequence[str],
    *,
    cwd: Path | None = None,
    timeout: float | None = None,
    log_file: Path | None = None,
    check: bool = False,
) -> Completed:
    """Run ``wine *args`` with ``env`` (from :func:`build_env`)."""
    return runner.run(
        [str(info.wine), *args], env=env, cwd=cwd, timeout=timeout, log_file=log_file, check=check
    )


def wineserver(
    runner: Runner, env: Mapping[str, str], info: WineInfo, flag: str = "-w", timeout: float = 300
) -> Completed:
    """``wineserver -w`` (wait for the prefix to go idle) or ``-k`` (kill it).

    A failing ``-w`` raises :class:`~fork_linux.procrun.CommandError`; ``-k``
    tolerates a non-zero exit (there may be no server to kill).
    """
    if flag not in WINESERVER_FLAGS:
        raise ValueError(f"unsupported wineserver flag {flag!r}; use one of {', '.join(WINESERVER_FLAGS)}")
    return runner.run([str(info.wineserver), flag], env=env, timeout=timeout, check=flag == "-w")


def import_reg(
    runner: Runner,
    env: Mapping[str, str],
    info: WineInfo,
    batch: RegBatch,
    paths: Paths,
    user: str,
    *,
    name: str = "fork-linux",
    timeout: float = 300,
    log_file: Path | None = None,
) -> Path:
    """Import ``batch`` with ``wine regedit /S`` and wait for the registry to reach disk.

    The ``.reg`` file is written to ``C:\\fork-linux\\tmp\\<name>.reg`` (kept
    for inspection) and its path returned. ``user`` is the Wine user whose
    ``HKEY_CURRENT_USER`` receives the HKCU keys; it sets ``WINEHOME`` when
    ``env`` lacks it. A failed import raises :class:`ForkLinuxError`.
    """
    if _REG_NAME_RE.fullmatch(name) is None:
        raise ValueError(f"registry batch name must be a plain file name, got {name!r}")
    _check_user(user)
    tmp_dir = fsutil.ensure_dir(paths.fork_linux_win_dir / REG_TMP_DIR)
    reg_file = batch.write(tmp_dir / f"{name}.reg")
    win_path = f"C:\\fork-linux\\{REG_TMP_DIR}\\{name}.reg"
    call_env = dict(env)
    call_env.setdefault("WINEHOME", "C:\\users\\" + user)
    completed = run(runner, call_env, info, ["regedit", "/S", win_path], timeout=timeout, log_file=log_file)
    if not completed.ok:
        detail = tail(completed.stderr or completed.stdout, 5)
        message = (
            f"importing registry settings failed (exit code {completed.returncode}): {format_argv(completed.argv)}"
        )
        raise ForkLinuxError(
            message + (f"\n{detail}" if detail else ""),
            hint=f"the batch is kept in {reg_file}; run 'fork-linux doctor' and see the log for Wine's output",
        )
    wineserver(runner, call_env, info, "-w", timeout=timeout)
    return reg_file
