"""Set or remove one option in an INI file while preserving comments and layout.

``configparser.write()`` drops every comment, so ``fork-linux config set`` and
``unset`` edit ``config.ini`` line by line instead (ported from Ubuntu-Hello's
``config_edit.py``). Lines are classified exactly as ``configparser`` reads
them, so an edit can never create a duplicate option or glue lines onto the
wrong value:

* blank lines and full-line ``#``/``;`` comments (at any indentation) are
  skipped and never end a value;
* a line indented deeper than the current option's line continues its value;
* otherwise the line is a section header (``[name]``, case-sensitive) or an
  option (``name = value`` or ``name: value``; names are case-insensitive).

A replaced option keeps its indentation and the comments inside its old value.
A new option is placed right after its commented-out default (``# key = ...``,
as written by ``data/defaults.ini``) when there is one, otherwise at the end of
its section; a missing section is appended to the file.
"""

from __future__ import annotations

import configparser
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path

from . import fsutil

DEFAULT_MODE = 0o600

_SECTCRE = configparser.RawConfigParser.SECTCRE
_OPTCRE = configparser.RawConfigParser.OPTCRE
_COMMENT_PREFIXES = ("#", ";")
_LINE_RE = re.compile(r"[^\n]*\n|[^\n]+\Z")
_KEY_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]*$")


@dataclass
class _Option:
    """One option: its lower-cased name and the line span of its whole value."""

    name: str
    start: int
    stop: int


@dataclass
class _Section:
    """One section: its name, the line span of its body and its options."""

    name: str
    first: int
    end: int
    options: list[_Option] = field(default_factory=list)


def _commented_key_re(key: str) -> re.Pattern[str]:
    """A commented-out option line for ``key`` (``# key = default``)."""
    return re.compile(rf"^[#;][ \t]*{re.escape(key)}[ \t]*[=:]", re.IGNORECASE)


def _indent(line: str) -> int:
    """Width of the leading whitespace of ``line``."""
    return len(line) - len(line.lstrip())


def _is_skipped(line: str) -> bool:
    """A blank or full-line comment line (``configparser`` ignores both)."""
    stripped = line.strip()
    return not stripped or stripped.startswith(_COMMENT_PREFIXES)


def _is_comment(line: str) -> bool:
    """A full-line ``#``/``;`` comment, at any indentation."""
    return line.strip().startswith(_COMMENT_PREFIXES)


def _validate(section: str, key: str, value: str | None = None) -> None:
    """Reject names and values that would corrupt the file (``ValueError``)."""
    if not section or section != section.strip() or any(ch in section for ch in "[]\r\n"):
        raise ValueError(f"invalid section name: {section!r}")
    if not _KEY_NAME_RE.match(key):
        raise ValueError(f"invalid option name: {key!r}")
    if value is not None and ("\n" in value or "\r" in value):
        raise ValueError(f"option values must be a single line: {key}")


def split_lines(text: str) -> list[str]:
    """Split ``text`` into lines that keep their ``\\n`` (only ``\\n`` separates lines)."""
    return _LINE_RE.findall(text)


def _parse(lines: list[str]) -> list[_Section]:
    """Sections and option spans of ``lines``, following ``configparser``'s rules."""
    sections: list[_Section] = []
    section: _Section | None = None
    option: _Option | None = None
    level = 0
    for index, line in enumerate(lines):
        if _is_skipped(line):
            continue
        indent = _indent(line)
        if option is not None and indent > level:
            option.stop = index + 1
            continue
        level = indent
        option = None
        header = _SECTCRE.match(line.strip())
        if header is not None:
            if section is not None:
                section.end = index
            section = _Section(header.group("header"), index + 1, len(lines))
            sections.append(section)
            continue
        match = _OPTCRE.match(line.strip()) if section is not None else None
        if match is not None and match.group("option").strip():
            option = _Option(match.group("option").rstrip().lower(), index, index + 1)
            section.options.append(option)
    return sections


def _find_section(sections: list[_Section], name: str) -> _Section | None:
    """The first section called ``name``."""
    return next((section for section in sections if section.name == name), None)


def _find_option(section: _Section, key: str) -> _Option | None:
    """The first option of ``section`` named ``key`` (case-insensitive)."""
    name = key.lower()
    return next((option for option in section.options if option.name == name), None)


def _kept_comments(lines: list[str], option: _Option) -> list[str]:
    """Comment lines inside the old value of ``option`` (they are kept)."""
    return [line for line in lines[option.start + 1 : option.stop] if _is_comment(line)]


def _end_of_content(lines: list[str], first: int, end: int) -> int:
    """Index just after the last non-blank line of a section body."""
    while end > first and not lines[end - 1].strip():
        end -= 1
    return end


def _indentation_before(lines: list[str], position: int) -> str:
    """Indentation for a new option inserted at ``position``: that of the next significant line.

    ``configparser`` reads an indented line as a continuation of the option
    above it. The next significant (non-blank, non-comment) line is not a
    continuation of the option current at ``position``, so it is indented no
    deeper than that option: copying its indentation makes the new line a new
    option without turning that line into the new option's continuation.
    """
    for line in lines[position:]:
        if not _is_skipped(line):
            return line[: _indent(line)]
    return ""


def _inside_value(section: _Section, index: int) -> bool:
    """True if line ``index`` lies within the value of one of the section's options."""
    return any(option.start <= index < option.stop for option in section.options)


def _insert_position(lines: list[str], section: _Section, key: str) -> int:
    """Where a new ``key`` line goes: after its commented-out default, else at the section end."""
    commented = _commented_key_re(key)
    for index in range(section.first, section.end):
        if commented.match(lines[index]) and not _inside_value(section, index):
            return index + 1
    return _end_of_content(lines, section.first, section.end)


def rewrite_set(lines: list[str], section: str, key: str, value: str) -> list[str]:
    """Return ``lines`` with ``[section] key = value`` set (see the module docstring)."""
    _validate(section, key, value)
    out = list(lines)
    if out and not out[-1].endswith("\n"):
        out[-1] += "\n"
    new_line = f"{key} = {value}\n"
    found = _find_section(_parse(out), section)
    if found is None:
        if out and out[-1].strip():
            out.append("\n")
        out.extend([f"[{section}]\n", new_line])
        return out
    option = _find_option(found, key)
    if option is not None:
        indentation = out[option.start][: _indent(out[option.start])]
        out[option.start : option.stop] = [indentation + new_line, *_kept_comments(out, option)]
        return out
    position = _insert_position(out, found, key)
    out.insert(position, _indentation_before(out, position) + new_line)
    return out


def rewrite_unset(lines: list[str], section: str, key: str) -> tuple[list[str], bool]:
    """Return ``lines`` without ``[section] key`` and whether it was present.

    The option's continuation lines go with it; comments inside its value stay.
    """
    _validate(section, key)
    found = _find_section(_parse(lines), section)
    option = None if found is None else _find_option(found, key)
    if option is None:
        return list(lines), False
    return [*lines[: option.start], *_kept_comments(lines, option), *lines[option.stop :]], True


def _target(path: Path) -> Path:
    """The file to edit: a symlinked config (dotfile managers) is edited in place."""
    path = Path(path)
    return path.resolve() if path.is_symlink() else path


def _read_lines(path: Path) -> list[str]:
    """The lines of ``path``, or no lines when it does not exist."""
    try:
        return split_lines(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []


def _write(path: Path, lines: list[str]) -> None:
    """Atomically replace ``path``, keeping its permission bits (new files: 0600)."""
    try:
        mode = stat.S_IMODE(os.stat(path).st_mode)
    except FileNotFoundError:
        mode = DEFAULT_MODE
    fsutil.atomic_write(path, "".join(lines), mode=mode)


def set_option(path: Path, section: str, key: str, value: str) -> None:
    """Set ``[section] key = value`` in ``path`` (created if missing), keeping comments and order."""
    target = _target(path)
    _write(target, rewrite_set(_read_lines(target), section, key, str(value)))


def unset_option(path: Path, section: str, key: str) -> bool:
    """Remove ``[section] key`` from ``path``; True if it was there (the file is then rewritten)."""
    target = _target(path)
    lines, removed = rewrite_unset(_read_lines(target), section, key)
    if removed:
        _write(target, lines)
    return removed
