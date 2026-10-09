"""Where the official Fork for Windows keeps its files inside our Wine prefix.

Fork is a Velopack app installed per user under ``%LOCALAPPDATA%\\Fork``
(spike S3): ``Update.exe``, ``current\\`` (``Fork.exe``, ``sq.version``, ...),
``packages\\Fork-<v>-full.nupkg``, ``settings.json``, ``logs\\fork.log``,
``velopack.log`` and ``gitInstance\\<git version>\\``. Its repository list lives in
``%LOCALAPPDATA%\\ForkData``. This module only computes paths and reads a few
of them; it never writes.
"""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path
from xml.parsers import expat

from . import versions
from .errors import UsageError
from .paths import Paths

# sq.version is a tiny nuspec; anything bigger is not Fork's and is never read whole.
SQ_VERSION_MAX_BYTES = 1024 * 1024
# A Velopack package version: dotted numbers with an optional semver suffix.
_VERSION = re.compile(r"\d{1,6}(?:\.\d{1,6}){0,3}(?:[-+][0-9A-Za-z.-]{1,64})?", re.ASCII)
_PACKAGE = re.compile(r"Fork-(\d{1,6}(?:\.\d{1,6}){0,3})-(?:full|delta)\.nupkg", re.ASCII)
# Characters that cannot appear in a Windows path component (plus our own separators).
_BAD_USER_CHARS = frozenset('/\\:*?"<>|\x00')
_NUSPEC_PATH = ["package", "metadata", "version"]


class _Refused(Exception):
    """A nuspec carried a DTD or entity declaration; it is not parsed further."""


def check_user(user: str) -> str:
    """Return ``user`` if it is usable as the Wine user directory name, else raise :class:`UsageError`."""
    if (
        not isinstance(user, str)
        or user in ("", ".", "..")
        or any(ch in _BAD_USER_CHARS or ord(ch) < 0x20 for ch in user)
    ):
        raise UsageError(
            f"cannot use {user!r} as the Wine user name",
            hint="run fork-linux from a normal user account with a plain $USER",
        )
    return user


def _local_name(tag: str) -> str:
    """``{uri}name`` / ``uri}name`` -> ``name`` (expat namespace mode uses ``}`` here)."""
    return tag.rsplit("}", 1)[-1]


def parse_nuspec_version(data: bytes) -> str | None:
    """The ``package/metadata/version`` text of a nuspec document, whatever its namespace.

    Documents with a DTD or entity declarations, malformed XML and versions that
    are not plain dotted numbers (optionally with a semver suffix) give None.
    """
    parser = expat.ParserCreate(namespace_separator="}")
    stack: list[str] = []
    text: list[str] = []
    found: list[str] = []

    def start(name: str, _attrs: object) -> None:
        stack.append(_local_name(name))

    def end(_name: str) -> None:
        if stack == _NUSPEC_PATH and not found:
            found.append("".join(text))
        stack.pop()

    def chars(data: str) -> None:
        if stack == _NUSPEC_PATH and not found:
            text.append(data)

    def refuse(*_args: object) -> None:
        raise _Refused

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = chars
    parser.StartDoctypeDeclHandler = refuse
    parser.EntityDeclHandler = refuse
    try:
        parser.Parse(data, True)
    except (expat.ExpatError, _Refused):
        return None
    if not found:
        return None
    version = found[0].strip()
    return version if _VERSION.fullmatch(version) else None


class ForkLayout:
    """Fork's files for one Wine user in one prefix."""

    def __init__(self, paths: Paths, user: str) -> None:
        self.paths = paths
        self.user = check_user(user)

    def __repr__(self) -> str:
        return f"ForkLayout(prefix={str(self.paths.prefix)!r}, user={self.user!r})"

    # -- Fork's Velopack root -------------------------------------------------

    @property
    def local_dir(self) -> Path:
        """``%LOCALAPPDATA%\\Fork``."""
        return self.paths.fork_local_dir(self.user)

    @property
    def current_dir(self) -> Path:
        """``Fork\\current``: the installed version, replaced wholesale by Velopack."""
        return self.local_dir / "current"

    @property
    def exe(self) -> Path:
        """``current\\Fork.exe``."""
        return self.current_dir / "Fork.exe"

    @property
    def ri_exe(self) -> Path:
        """``current\\Fork.RI.exe`` (Fork's elevated helper)."""
        return self.current_dir / "Fork.RI.exe"

    @property
    def askpass_exe(self) -> Path:
        """``current\\Fork.AskPass.exe``."""
        return self.current_dir / "Fork.AskPass.exe"

    @property
    def update_exe(self) -> Path:
        """``Fork\\Update.exe`` (Velopack)."""
        return self.local_dir / "Update.exe"

    @property
    def sq_version_file(self) -> Path:
        """``current\\sq.version``: nuspec XML naming the installed version."""
        return self.current_dir / "sq.version"

    @property
    def packages_dir(self) -> Path:
        """``Fork\\packages``: installed and staged ``.nupkg`` files."""
        return self.local_dir / "packages"

    def full_package(self, version: str) -> Path:
        """``Fork\\packages\\Fork-<version>-full.nupkg``: Velopack's full package of ``version``."""
        return self.packages_dir / f"Fork-{version}-full.nupkg"

    @property
    def settings_file(self) -> Path:
        """``Fork\\settings.json`` (written by Fork after its first completed launch)."""
        return self.local_dir / "settings.json"

    @property
    def accounts_file(self) -> Path:
        """``Fork\\accounts.json``: credentials, never snapshotted or bundled."""
        return self.local_dir / "accounts.json"

    @property
    def custom_commands_file(self) -> Path:
        """``Fork\\custom-commands.json``."""
        return self.local_dir / "custom-commands.json"

    @property
    def logs_dir(self) -> Path:
        """``Fork\\logs``."""
        return self.local_dir / "logs"

    @property
    def fork_log(self) -> Path:
        """``Fork\\logs\\fork.log``."""
        return self.logs_dir / "fork.log"

    @property
    def velopack_log(self) -> Path:
        """``Fork\\velopack.log``."""
        return self.local_dir / "velopack.log"

    @property
    def gitinstance_dir(self) -> Path:
        """``Fork\\gitInstance``: Fork's bundled Git for Windows builds."""
        return self.local_dir / "gitInstance"

    @property
    def forkdata_dir(self) -> Path:
        """``%LOCALAPPDATA%\\ForkData`` (``repositories.toml``)."""
        return self.local_dir.parent / "ForkData"

    # -- shortcuts written by the installer -----------------------------------

    @property
    def desktop_lnk(self) -> Path:
        """``C:\\users\\<u>\\Desktop\\Fork.lnk`` written by ``--silent`` installs."""
        return self.paths.wine_user_dir(self.user) / "Desktop" / "Fork.lnk"

    @property
    def startmenu_lnk(self) -> Path:
        """The Start Menu shortcut written by ``--silent`` installs."""
        return (
            self.paths.wine_user_dir(self.user)
            / "AppData"
            / "Roaming"
            / "Microsoft"
            / "Windows"
            / "Start Menu"
            / "Programs"
            / "Fork.lnk"
        )

    @property
    def win_local_dir(self) -> str:
        """Fork's install directory as Wine sees it, with a trailing backslash (``Update.exe`` lives here)."""
        return f"C:\\users\\{self.user}\\AppData\\Local\\Fork\\"

    @property
    def win_exe(self) -> str:
        """``Fork.exe`` as Wine sees it: ``C:\\users\\<u>\\AppData\\Local\\Fork\\current\\Fork.exe``."""
        return f"{self.win_local_dir}current\\Fork.exe"

    # -- probes ----------------------------------------------------------------

    def installed_version(self) -> str | None:
        """The version in ``current\\sq.version``, or None (missing, oversized or malformed)."""
        try:
            # O_NONBLOCK: a FIFO planted here must not hang the launcher.
            fd = os.open(self.sq_version_file, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        except OSError:
            return None
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                return None
            with os.fdopen(fd, "rb", closefd=False) as handle:
                data = handle.read(SQ_VERSION_MAX_BYTES + 1)
        except OSError:
            return None
        finally:
            os.close(fd)
        if len(data) > SQ_VERSION_MAX_BYTES:
            return None
        return parse_nuspec_version(data)

    def is_installed(self) -> bool:
        """True when ``Fork.exe`` exists and ``sq.version`` names a version."""
        return self.exe.is_file() and self.installed_version() is not None

    def git_instances(self) -> list[str]:
        """Sorted names of ``gitInstance`` subdirectories that contain ``cmd\\git.exe``."""
        try:
            entries = list(os.scandir(self.gitinstance_dir))
        except OSError:
            return []
        return sorted(
            entry.name
            for entry in entries
            if entry.is_dir(follow_symlinks=False) and (Path(entry.path) / "cmd" / "git.exe").is_file()
        )

    def staged_packages(self, than: str | None = None) -> list[tuple[str, Path]]:
        """``(version, path)`` of ``Fork-<v>-(full|delta).nupkg`` files newer than a baseline.

        The baseline is ``than`` or, by default, the installed version; with no
        baseline at all every package counts. Only regular files are listed,
        sorted by version, then name.
        """
        baseline_text = self.installed_version() if than is None else than
        baseline = None if baseline_text is None else versions.Version(baseline_text)
        try:
            entries = list(os.scandir(self.packages_dir))
        except OSError:
            return []
        found: list[tuple[versions.Version, str, Path]] = []
        for entry in entries:
            match = _PACKAGE.fullmatch(entry.name)
            if match is None or not entry.is_file(follow_symlinks=False):
                continue
            version = versions.Version(match.group(1))
            if baseline is None or version > baseline:
                found.append((version, entry.name, Path(entry.path)))
        found.sort(key=lambda item: (item[0], item[1]))
        return [(str(version), path) for version, _name, path in found]
