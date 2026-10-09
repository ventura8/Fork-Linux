"""Unix <-> Windows path mapping from a prefix's ``dosdevices`` symlinks, without ``winepath``.

Wine exposes Unix directories as drive letters through ``<prefix>/dosdevices``
(``c:`` -> ``../drive_c``, ``z:`` -> ``/``). Converting a path is a
longest-prefix match over the drive targets, which is what ``winepath -w``
does but in microseconds and without starting a wineserver.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path

from .errors import NotSetUpError, UsageError

# Always used with fullmatch(): "$" would also accept a trailing newline ("c:\n").
_DRIVE_ENTRY = re.compile(r"[a-z]:")
_DRIVE_LETTER = re.compile(r"([A-Za-z]):?")
_WIN_DRIVE = re.compile(r"([A-Za-z]):(?:\\(.*))?", re.DOTALL)
_WIN_UNSAFE = frozenset('\\<>:"|?*')
_LONG_PREFIXES = ("\\\\?\\", "\\\\.\\", "\\??\\")
_UNIX_NAMESPACE = "unix\\"
# Typing shortcuts fork-linux adds (``h:`` -> the Linux home): never chosen for Linux-to-Windows
# conversion while another drive reaches the path, so Fork keeps seeing repositories under Z:.
ALIAS_DRIVES = frozenset({"h"})


def _norm(path: str) -> str:
    """Absolute, lexically normalised form of ``path`` (symlinks are NOT resolved).

    POSIX ``normpath`` keeps exactly two leading slashes; on Linux ``//x`` is
    ``/x``, so they are collapsed to keep the longest-prefix match exact.
    """
    norm = os.path.normpath(os.path.abspath(path))
    return "/" + norm.lstrip("/") if norm.startswith("//") else norm


def _is_under(path: str, root: str) -> bool:
    """True when ``path`` equals ``root`` or lies below it (both normalised)."""
    return path == root or path.startswith(root.rstrip("/") + "/")


def _check_component(part: str, path: str) -> None:
    """Refuse a path component that Windows cannot name."""
    if any("\ud800" <= ch <= "\udfff" for ch in part):
        raise UsageError(
            f"{path!r} is not valid UTF-8, so Windows cannot represent it",
            hint="rename it so that its name is valid UTF-8",
        )
    if any(ch in _WIN_UNSAFE or ord(ch) < 0x20 for ch in part):
        raise UsageError(
            f"{path!r} has a name Windows cannot represent: {part!r}",
            hint='rename it to avoid \\ < > : " | ? * and control characters',
        )


def _clean_parts(parts: list[str]) -> list[str]:
    """Drop empty and ``.`` components and apply ``..`` without climbing above the root."""
    out: list[str] = []
    for part in parts:
        if part == "..":
            if out:
                out.pop()
        elif part not in ("", "."):
            out.append(part)
    return out


class PathMap:
    """Drive-letter mapping of one Wine prefix."""

    def __init__(self, drives: Mapping[str, Path], targets: list[tuple[str, str]]) -> None:
        self._drives = dict(drives)
        # (normalised target, letter): alias drives last, then longest target first, then by letter.
        self._targets = sorted(set(targets), key=lambda item: (item[1] in ALIAS_DRIVES, -len(item[0]), item[1]))

    @classmethod
    def from_prefix(cls, prefix: Path | str) -> PathMap:
        """Read ``<prefix>/dosdevices``; device entries such as ``c::`` are ignored."""
        dosdevices = Path(prefix) / "dosdevices"
        try:
            entries = sorted(os.listdir(dosdevices))
        except OSError as exc:
            raise NotSetUpError(
                f"Wine prefix has no readable dosdevices directory: {dosdevices}",
                hint="run 'fork-linux setup' to create the prefix",
            ) from exc
        drives: dict[str, Path] = {}
        targets: list[tuple[str, str]] = []
        for entry in entries:
            if not _DRIVE_ENTRY.fullmatch(entry):
                continue
            try:
                link_target = os.readlink(dosdevices / entry)
            except OSError:
                continue  # not a symlink, or removed since listdir()
            lexical = _norm(os.path.join(dosdevices, link_target))
            resolved = os.path.realpath(lexical)
            drives[entry[0]] = Path(resolved)
            targets.extend([(lexical, entry[0]), (resolved, entry[0])])
        return cls(drives, targets)

    @classmethod
    def with_drives(cls, mapping: Mapping[str, Path | str]) -> PathMap:
        """Build a map directly from ``{"c": Path(...), "z": Path("/")}`` (tests, tooling)."""
        drives: dict[str, Path] = {}
        for key, target in mapping.items():
            match = _DRIVE_LETTER.fullmatch(key)
            if match is None:
                raise ValueError(f"not a drive letter: {key!r}")
            drives[match.group(1).lower()] = Path(_norm(os.fspath(target)))
        return cls(drives, [(str(path), letter) for letter, path in drives.items()])

    def drives(self) -> dict[str, Path]:
        """Drive letter (lower case) -> target directory."""
        return dict(self._drives)

    def unix_to_win(self, path: Path | str) -> str:
        """Convert a Unix path to ``X:\\...`` using the longest matching drive target."""
        raw = os.fspath(path)
        if not raw:
            raise UsageError("empty path")
        try:
            absolute = _norm(raw)
        except OSError as exc:  # relative path while the working directory is gone
            raise UsageError(
                f"cannot make {raw!r} absolute: {exc.strerror or exc}",
                hint="pass an absolute path or cd into an existing directory",
            ) from exc
        for target, letter in self._targets:
            if _is_under(absolute, target):
                rest = absolute[len(target):].strip("/")
                parts = rest.split("/") if rest else []
                for part in parts:
                    _check_component(part, absolute)
                return f"{letter.upper()}:\\" + "\\".join(parts)
        raise UsageError(
            f"{absolute!r} is not reachable from Wine: no drive maps it",
            hint="add a drive for it (e.g. restore the Z: drive with winecfg)",
        )

    def win_to_unix(self, winpath: str) -> Path:
        """Convert ``C:\\x``, ``c:/x``, ``\\\\?\\C:\\x`` or ``\\\\?\\unix\\home\\u`` to a Unix path."""
        if any(ord(ch) < 0x20 for ch in winpath):
            raise UsageError(f"Windows path contains a control character: {winpath!r}")
        text = winpath.replace("/", "\\")
        for long_prefix in _LONG_PREFIXES:
            if text.startswith(long_prefix):
                text = text[len(long_prefix):]
                if text[: len(_UNIX_NAMESPACE)].lower() == _UNIX_NAMESPACE:
                    rest = _clean_parts(text[len(_UNIX_NAMESPACE):].split("\\"))
                    return Path("/", *rest)
                break
        match = _WIN_DRIVE.fullmatch(text)
        if match is None:
            raise UsageError(
                f"not an absolute drive path: {winpath!r}",
                hint="UNC and relative paths are not supported; use a form like C:\\path",
            )
        letter = match.group(1).lower()
        root = self._drives.get(letter)
        if root is None:
            raise UsageError(f"drive {letter.upper()}: is not mapped in this Wine prefix: {winpath!r}")
        return root.joinpath(*_clean_parts((match.group(2) or "").split("\\")))

    def __repr__(self) -> str:
        inner = ", ".join(f"{k}: {str(v)!r}" for k, v in sorted(self._drives.items()))
        return f"PathMap({{{inner}}})"
