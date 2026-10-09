"""Fork's ``ForkData\\repositories.toml``: the repository list and the default source folder.

Fork 2.23 keeps its Repository Manager state in
``%LOCALAPPDATA%\\ForkData\\repositories.toml``::

    source_dirs = ['C:\\users\\u\\']
    scan_depth = 5
    ignore = []

    [[repository]]
    path = 'Z:\\home\\u\\src\\app'
    opened = 1791549485

``source_dirs`` is the folder Preferences > General, the welcome dialog and
the Clone dialog offer (the ``settings.json`` key ``RepositoryManager.
SourceDirectories`` is no longer read). Its default is inside the Wine prefix,
so clones would land in the prefix (and ``uninstall --purge`` would delete
them). :func:`ensure_source_dirs` replaces that default with the Linux home.

There is no TOML parser in Python 3.10's standard library, so this module
reads only what it needs, line by line, and edits only the top-level
``source_dirs = [...]`` line (when it is a single line), keeping every other
byte. The file is edited only while Fork is closed (the caller makes sure),
after a backup, and never through a symbolic link (AGENTS.md hard rule 3).
"""

from __future__ import annotations

import os
import re
import stat
from datetime import datetime, timezone
from pathlib import Path

from . import fsutil
from .errors import IntegrityFailed

NAME = "repositories.toml"
BACKUP_PREFIX = NAME + "."
DEFAULT_KEEP = 3
MAX_BYTES = 4 * 1024 * 1024
FILE_MODE = 0o644
BACKUP_MODE = 0o600
SOURCE_DIRS = "source_dirs"

_KEY_NAME = re.compile(r"[A-Za-z0-9_-]+")
_TABLE = re.compile(r"^\s*\[")
_LITERAL = re.compile(r"'([^'\n]*)'")
_BASIC = re.compile(r'"((?:[^"\\\n]|\\.)*)"')
_BASIC_ESCAPES = {"\\": "\\", '"': '"', "b": "\b", "t": "\t", "n": "\n", "f": "\f", "r": "\r"}


def path(forkdata_dir: Path) -> Path:
    """``…\\ForkData\\repositories.toml``."""
    return Path(forkdata_dir) / NAME


def _utcnow() -> datetime:
    """The current UTC time (tests replace this)."""
    return datetime.now(timezone.utc)


def read_text(file: Path) -> str | None:
    """The file's text (None when missing); a symlink, non-regular or huge file raises :class:`IntegrityFailed`."""
    try:
        mode = os.lstat(file).st_mode
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(mode):
        raise IntegrityFailed(
            f"refusing to read {file}: it is not a regular file",
            hint="fork-linux only edits Fork's own repositories.toml; replace it with a regular file",
        )
    with open(file, "rb") as handle:
        raw = handle.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise IntegrityFailed(f"{file} is larger than {MAX_BYTES} bytes")
    return raw.decode("utf-8", errors="surrogateescape")


def _unbasic(body: str) -> str | None:
    """A TOML basic string body unescaped (None for escapes this reader does not know)."""
    out: list[str] = []
    index = 0
    while index < len(body):
        char = body[index]
        if char != "\\":
            out.append(char)
            index += 1
            continue
        nxt = body[index + 1]
        if nxt not in _BASIC_ESCAPES:
            return None
        out.append(_BASIC_ESCAPES[nxt])
        index += 2
    return "".join(out)


def parse_strings(value: str) -> list[str] | None:
    """The strings of a one-line TOML array of strings (``['a', "b"]``), else None."""
    text = value.strip()
    if not (text.startswith("[") and text.endswith("]")):
        return None
    inner = text[1:-1].strip()
    items: list[str] = []
    while inner:
        literal = _LITERAL.match(inner)
        basic = _BASIC.match(inner)
        if literal is not None:
            items.append(literal.group(1))
            inner = inner[literal.end():]
        elif basic is not None:
            unescaped = _unbasic(basic.group(1))
            if unescaped is None:
                return None
            items.append(unescaped)
            inner = inner[basic.end():]
        else:
            return None
        inner = inner.strip()
        if inner.startswith(","):
            inner = inner[1:].strip()
        elif inner:
            return None
    return items


def quote(text: str) -> str:
    """``text`` as a TOML string: literal (``'…'``) when possible, else an escaped basic string."""
    if "'" not in text and not any(ord(ch) < 0x20 or ch == "\x7f" for ch in text):
        return f"'{text}'"
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    escaped = "".join(ch if ord(ch) >= 0x20 and ch != "\x7f" else f"\\u{ord(ch):04x}" for ch in escaped)
    return f'"{escaped}"'


def _top_level_lines(text: str) -> list[tuple[int, str]]:
    """``(index, line)`` of the lines before the first table header."""
    found = []
    for index, line in enumerate(text.splitlines(keepends=True)):
        if _TABLE.match(line):
            break
        found.append((index, line))
    return found


def _key_line(line: str) -> tuple[str, str, str] | None:
    """``(indent, key, value)`` of a ``key = value`` line without line breaks, else None."""
    body = line.lstrip()
    key = _KEY_NAME.match(body)
    if key is None:
        return None
    rest = body[key.end():].lstrip()
    if not rest.startswith("="):
        return None
    return line[: len(line) - len(body)], key.group(), rest[1:].strip()


def source_dirs(text: str) -> list[str] | None:
    """The top-level ``source_dirs`` (None when absent or not a one-line array of strings)."""
    for _index, line in _top_level_lines(text):
        match = _key_line(line.rstrip("\r\n"))
        if match is not None and match[1] == SOURCE_DIRS:
            return parse_strings(match[2])
    return None


def repositories(text: str) -> list[str]:
    """The ``path`` of every ``[[repository]]`` table, in file order."""
    paths: list[str] = []
    in_repository = False
    for line in text.splitlines():
        if _TABLE.match(line):
            in_repository = line.strip() == "[[repository]]"
            continue
        match = _key_line(line)
        if in_repository and match is not None and match[1] == "path":
            parsed = parse_strings(f"[{match[2]}]")
            if parsed is not None and len(parsed) == 1:
                paths.append(parsed[0])
    return paths


def is_default(dirs: list[str] | None, user: str) -> bool:
    """True for Fork's default ``['C:\\users\\<user>\\']`` (with or without the trailing backslash)."""
    if dirs is None or len(dirs) != 1:
        return False
    return dirs[0].rstrip("\\").lower() == f"c:\\users\\{user}".lower()


def with_source_dirs(text: str, dirs: list[str]) -> str:
    """``text`` with the top-level ``source_dirs`` line replaced.

    :class:`ValueError` when there is no one-line ``source_dirs`` array to replace.
    """
    lines = text.splitlines(keepends=True)
    for index, line in _top_level_lines(text):
        body = line.rstrip("\r\n")
        match = _key_line(body)
        if match is not None and match[1] == SOURCE_DIRS:
            if parse_strings(match[2]) is None:
                break
            ending = line[len(body):]
            lines[index] = f"{match[0]}{SOURCE_DIRS} = [{', '.join(quote(item) for item in dirs)}]{ending}"
            return "".join(lines)
    raise ValueError("no one-line source_dirs array to replace")


def list_backups(backup_dir: Path) -> list[Path]:
    """Backups of ``repositories.toml`` in ``backup_dir``, newest first."""
    try:
        names = os.listdir(backup_dir)
    except OSError:
        return []
    return sorted((Path(backup_dir) / name for name in names if name.startswith(BACKUP_PREFIX)), reverse=True)


def backup(text: str, *, backup_dir: Path, keep: int = DEFAULT_KEEP) -> Path:
    """Save ``text`` (the current content of ``repositories.toml``) as ``repositories.toml.<UTC time>``; keep ``keep``."""
    fsutil.ensure_dir(backup_dir, 0o700)
    stamp = _utcnow().strftime("%Y%m%dT%H%M%S.%fZ")
    target = Path(backup_dir) / f"{BACKUP_PREFIX}{stamp}"
    fsutil.atomic_write(target, text.encode("utf-8", errors="surrogateescape"), mode=BACKUP_MODE)
    for old in list_backups(backup_dir)[keep:]:
        old.unlink()
    return target


def ensure_source_dirs(forkdata_dir: Path, *, user: str, home_win: str, backup_dir: Path) -> bool:
    """Point Fork's default source folder at ``home_win``; True if the file was written.

    A missing file is created with just ``source_dirs`` (Fork adds the other
    keys on its first start). An existing file is changed only when
    ``source_dirs`` is still Fork's default ``C:\\users\\<user>``; a folder the
    user chose is kept. The caller makes sure Fork is closed.
    """
    file = path(forkdata_dir)
    text = read_text(file)
    if text is None:
        fsutil.atomic_write(file, f"{SOURCE_DIRS} = [{quote(home_win)}]\n", mode=FILE_MODE)
        return True
    if not is_default(source_dirs(text), user):
        return False
    new = with_source_dirs(text, [home_win])
    backup(text, backup_dir=backup_dir)
    fsutil.atomic_write(file, new.encode("utf-8", errors="surrogateescape"), mode=FILE_MODE)
    return True
