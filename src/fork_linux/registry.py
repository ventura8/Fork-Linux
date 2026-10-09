"""Wine registry: write ``.reg`` batches for ``regedit`` and read hives without Wine.

:class:`RegBatch` renders a REGEDIT5 file (UTF-16LE with BOM, CRLF line endings)
that ``wine regedit /S`` imports in one go. :func:`parse_wine_reg` reads Wine's
own on-disk format (``user.reg``, ``system.reg``, ``userdef.reg``) so checks such
as "is .NET 4.8 installed?" never have to start a Wine process.
"""

from __future__ import annotations

import contextlib
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from . import fsutil
from .errors import ForkLinuxError

REG_NONE = 0
REG_SZ = 1
REG_EXPAND_SZ = 2
REG_BINARY = 3
REG_DWORD = 4
REG_DWORD_BIG_ENDIAN = 5
REG_LINK = 6
REG_MULTI_SZ = 7
REG_QWORD = 11

REGEDIT_HEADER = "Windows Registry Editor Version 5.00"
WINE_HEADER = "WINE REGISTRY Version 2"

HIVE_ALIASES = {
    "HKCU": "HKEY_CURRENT_USER",
    "HKLM": "HKEY_LOCAL_MACHINE",
    "HKCR": "HKEY_CLASSES_ROOT",
    "HKU": "HKEY_USERS",
    "HKCC": "HKEY_CURRENT_CONFIG",
}
_HIVE_LOOKUP = {alias.lower(): full for alias, full in HIVE_ALIASES.items()}
_HIVE_LOOKUP.update({full.lower(): full for full in HIVE_ALIASES.values()})

_HEX_LINE_WIDTH = 80
_STRING_TYPES = (REG_SZ, REG_EXPAND_SZ, REG_LINK)
_INT_LAYOUTS = {REG_DWORD: (4, "little"), REG_DWORD_BIG_ENDIAN: (4, "big"), REG_QWORD: (8, "little")}
_SIMPLE_ESCAPES = {"a": "\a", "b": "\b", "e": "\x1b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v"}
_HEXDIGITS = frozenset("0123456789abcdefABCDEF")
_OCTDIGITS = frozenset("01234567")
_HEADER_RE = re.compile(r"^WINE REGISTRY Version (\d+)", re.ASCII)
_HEX_RE = re.compile(r"^hex(?:\(([0-9a-fA-F]+)\))?:")
_SURROGATE_RE = re.compile("[\ud800-\udfff]")
_DWORD_RE = re.compile(r"^dword:([0-9a-fA-F]{1,8})")
_TIMESTAMP_RE = re.compile(r"\d+", re.ASCII)
_ROOT_MARKER = ";; All keys relative to "
# A dotnet48 prefix's system.reg is tens of MiB; anything far beyond is not a hive.
_MAX_HIVE_BYTES = 256 * 1024 * 1024
_STRING_TAGS = (('"', REG_SZ), ('str:"', REG_SZ), ('str(2):"', REG_EXPAND_SZ), ('str(7):"', REG_MULTI_SZ))


class RegistryFormatError(ForkLinuxError):
    """A Wine hive file is not in the ``WINE REGISTRY Version 2`` format."""


# --------------------------------------------------------------------------- writer


def _check_text(text: str, what: str, *, allow_control: bool) -> None:
    """Reject text that a .reg file cannot carry (non-str, lone surrogates, control chars)."""
    if not isinstance(text, str):
        raise ValueError(f"{what} must be a string, not {type(text).__name__}: {text!r}")
    try:
        text.encode("utf-16-le")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{what} is not valid Unicode: {text!r}") from exc
    if not allow_control and any(ord(ch) < 0x20 for ch in text):
        raise ValueError(f"{what} contains a control character: {text!r}")


def normalize_key(key: str) -> str:
    """Return ``key`` with its hive spelled out in full (``HKCU`` -> ``HKEY_CURRENT_USER``)."""
    if not isinstance(key, str):
        raise ValueError(f"registry key must be a string, not {type(key).__name__}: {key!r}")
    parts = [part for part in key.split("\\") if part]
    hive = _HIVE_LOOKUP.get(parts[0].lower()) if parts else None
    if hive is None:
        raise ValueError(f"registry key must start with a hive such as HKCU or HKLM: {key!r}")
    if len(parts) < 2:
        raise ValueError(f"registry key needs a path below the hive: {key!r}")
    for part in parts[1:]:
        _check_text(part, "registry key", allow_control=False)
    return "\\".join([hive, *parts[1:]])


def _quote(text: str) -> str:
    """Quote ``text`` for a .reg file: escape backslashes and double quotes."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _value_name(name: str) -> str:
    """Render a value name; the empty name is the key's default value (``@``)."""
    if name == "":
        return "@"
    _check_text(name, "value name", allow_control=False)
    return _quote(name)


def _hex_lines(lead: str, data: bytes) -> list[str]:
    """Render ``lead`` + comma-separated hex bytes, wrapped regedit-style with ``\\``."""
    lines: list[str] = []
    current = lead
    on_line = 0
    last = len(data) - 1
    for index, byte in enumerate(data):
        token = f"{byte:02x}" + ("," if index < last else "")
        if on_line and len(current) + len(token) + 1 > _HEX_LINE_WIDTH:
            lines.append(current + "\\")
            current = "  "
            on_line = 0
        current += token
        on_line += 1
    lines.append(current)
    return lines


def _utf16z(text: str) -> bytes:
    """Encode ``text`` as NUL-terminated UTF-16LE."""
    return text.encode("utf-16-le") + b"\x00\x00"


class RegBatch:
    """An ordered set of registry edits rendered as one REGEDIT5 ``.reg`` file."""

    def __init__(self) -> None:
        self._blocks: list[tuple[str, list[str]]] = []

    def _add(self, key: str, lines: list[str]) -> RegBatch:
        header = f"[{normalize_key(key)}]"
        if self._blocks and self._blocks[-1][0].lower() == header.lower():
            self._blocks[-1][1].extend(lines)
        else:
            self._blocks.append((header, list(lines)))
        return self

    def set_sz(self, key: str, name: str, value: str) -> RegBatch:
        """Set a REG_SZ value (control characters force the ``hex(1)`` form)."""
        lead = _value_name(name) + "="
        _check_text(value, "string value", allow_control=True)
        if "\x00" in value:
            raise ValueError(f"string value contains NUL: {value!r}")
        if any(ord(ch) < 0x20 for ch in value):
            return self._add(key, _hex_lines(lead + "hex(1):", _utf16z(value)))
        return self._add(key, [lead + _quote(value)])

    def set_default(self, key: str, value: str) -> RegBatch:
        """Set the key's default (``@``) value as REG_SZ."""
        return self.set_sz(key, "", value)

    def set_expand_sz(self, key: str, name: str, value: str) -> RegBatch:
        """Set a REG_EXPAND_SZ value (always written as ``hex(2)``)."""
        lead = _value_name(name) + "="
        _check_text(value, "string value", allow_control=True)
        if "\x00" in value:
            raise ValueError(f"string value contains NUL: {value!r}")
        return self._add(key, _hex_lines(lead + "hex(2):", _utf16z(value)))

    def set_dword(self, key: str, name: str, value: int) -> RegBatch:
        """Set a REG_DWORD value (0 .. 0xFFFFFFFF)."""
        if not isinstance(value, int) or not 0 <= value <= 0xFFFFFFFF:
            raise ValueError(f"DWORD out of range: {value!r}")
        return self._add(key, [f"{_value_name(name)}=dword:{value:08x}"])

    def set_multi_sz(self, key: str, name: str, values: Iterable[str]) -> RegBatch:
        """Set a REG_MULTI_SZ value (written as ``hex(7)``); items must be non-empty."""
        if isinstance(values, str):
            raise ValueError("multi-string values take a list of strings, not a string")
        items = list(values)
        for item in items:
            _check_text(item, "multi-string item", allow_control=True)
            if item == "" or "\x00" in item:
                raise ValueError(f"multi-string item must be non-empty and NUL-free: {item!r}")
        data = b"".join(_utf16z(item) for item in items) + b"\x00\x00"
        return self._add(key, _hex_lines(_value_name(name) + "=hex(7):", data))

    def delete_value(self, key: str, name: str) -> RegBatch:
        """Delete one value (the empty name deletes the default value)."""
        return self._add(key, [f"{_value_name(name)}=-"])

    def delete_key(self, key: str) -> RegBatch:
        """Delete a key and everything below it."""
        header = f"[-{normalize_key(key)}]"
        if not self._blocks or self._blocks[-1][0].lower() != header.lower():
            self._blocks.append((header, []))
        return self

    def is_empty(self) -> bool:
        """True when no edit has been added."""
        return not self._blocks

    def render_text(self) -> str:
        """The batch as text with CRLF line endings (no BOM)."""
        lines = [REGEDIT_HEADER, ""]
        for header, body in self._blocks:
            lines.append(header)
            lines.extend(body)
            lines.append("")
        return "\r\n".join(lines) + "\r\n"

    def render(self) -> bytes:
        """The batch as regedit expects it: UTF-16LE with a BOM."""
        return ("\ufeff" + self.render_text()).encode("utf-16-le")

    def write(self, path: Path | str) -> Path:
        """Atomically write the rendered batch to ``path`` (parents created) and return it.

        The data lands in a fresh temporary file that then replaces ``path``, so a
        symlink already sitting at ``path`` is replaced, never followed.
        """
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        fsutil.atomic_write(target, self.render())
        return target


# --------------------------------------------------------------------------- reader


@dataclass(frozen=True)
class RegValue:
    """One registry value; ``name`` is ``""`` for the default value."""

    name: str
    type: int
    data: object


@dataclass
class RegKey:
    """A key from a hive file: original-case ``path`` and values by lower-case name."""

    path: str
    values: dict[str, RegValue] = field(default_factory=dict)
    timestamp: int | None = None

    def get(self, name: str) -> RegValue | None:
        """Look up a value case-insensitively (``""`` is the default value)."""
        return self.values.get(name.lower())


def _key_id(path: str) -> str:
    """Case-insensitive lookup id of a key path; empty path components are dropped."""
    return "\\".join(part for part in path.split("\\") if part).lower()


class RegHive(dict[str, RegKey]):
    """Keys of one hive file, looked up case-insensitively by path."""

    def __init__(self) -> None:
        super().__init__()
        self.root = ""
        self.arch = ""

    def __getitem__(self, key: str) -> RegKey:
        return super().__getitem__(_key_id(key))

    def __setitem__(self, key: str, value: RegKey) -> None:
        super().__setitem__(_key_id(key), value)

    def __contains__(self, key: object) -> bool:
        return isinstance(key, str) and super().__contains__(_key_id(key))

    def get(self, key: str, default: RegKey | None = None) -> RegKey | None:
        """Return the key at ``key`` or ``default``."""
        return super().get(_key_id(key), default)


def _decode_escape(text: str, index: int) -> tuple[str, int]:
    """Decode the escape whose letter sits at ``index``; return (text, next index)."""
    esc = text[index]
    if esc in _SIMPLE_ESCAPES:
        return _SIMPLE_ESCAPES[esc], index + 1
    if esc == "x":
        stop = index + 1
        while stop < len(text) and stop < index + 5 and text[stop] in _HEXDIGITS:
            stop += 1
        return (chr(int(text[index + 1:stop], 16)) if stop > index + 1 else "x"), stop
    if esc in _OCTDIGITS:
        stop = index + 1
        while stop < len(text) and stop < index + 3 and text[stop] in _OCTDIGITS:
            stop += 1
        return chr(int(text[index:stop], 8)), stop
    return esc, index + 1


def _unescape(text: str, start: int, end_char: str) -> tuple[str, int] | None:
    """Decode a Wine-escaped string from ``start`` up to ``end_char``.

    Returns the decoded text and the index just past ``end_char``, or None when
    the terminator is missing. Mirrors ``parse_strW`` in Wine's server; UTF-16
    surrogate pairs written as two ``\\x`` escapes are recombined.
    """
    end = text.find(end_char, start)
    if end != -1:
        segment = text[start:end]
        # Fast path (nearly every key line: separators are written as "\\\\"): only
        # escaped backslashes, so the terminator found is the real one.
        if "\\" not in segment.replace("\\\\", ""):
            return segment.replace("\\\\", "\\"), end + 1
    out: list[str] = []
    index = start
    while index < len(text):
        char = text[index]
        if char == end_char:
            decoded = "".join(out)
            if _SURROGATE_RE.search(decoded):
                decoded = decoded.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")
            return decoded, index + 1
        if char != "\\":
            out.append(char)
            index += 1
        elif index + 1 < len(text):
            piece, index = _decode_escape(text, index + 1)
            out.append(piece)
        else:
            return None
    return None


def _split_multi(text: str) -> list[str]:
    """Split a NUL-separated multi-string, dropping the trailing terminators."""
    parts = text.split("\x00")
    while parts and parts[-1] == "":
        parts.pop()
    return parts


def _decode_binary(reg_type: int, data: bytes) -> object:
    """Turn ``hex(N):`` bytes into str / list[str] / int where the type allows it."""
    if reg_type in _STRING_TYPES:
        return data.decode("utf-16-le", "replace").split("\x00", 1)[0]
    if reg_type == REG_MULTI_SZ:
        return _split_multi(data.decode("utf-16-le", "replace"))
    layout = _INT_LAYOUTS.get(reg_type)
    if layout is not None and len(data) == layout[0]:
        return int.from_bytes(data, layout[1])
    return data


def _parse_hex(text: str) -> bytes | None:
    """Parse ``aa,bb,cc`` (whitespace tolerated); None when malformed."""
    tokens = [tok for tok in "".join(text.split()).split(",") if tok]
    out = bytearray()
    for tok in tokens:
        if len(tok) > 2 or not set(tok) <= _HEXDIGITS:
            return None
        out.append(int(tok, 16))
    return bytes(out)


def _parse_string(text: str, start: int, reg_type: int) -> tuple[int, object] | None:
    """Parse quoted string data starting just after its opening quote."""
    parsed = _unescape(text, start, '"')
    if parsed is None:
        return None
    return reg_type, _split_multi(parsed[0]) if reg_type == REG_MULTI_SZ else parsed[0]


def _parse_hex_data(text: str) -> tuple[int, object] | None:
    """Parse ``hex:aa,bb`` / ``hex(N):aa,bb`` data."""
    match = _HEX_RE.match(text)
    if match is None:
        return None
    reg_type = int(match.group(1), 16) if match.group(1) else REG_BINARY
    data = _parse_hex(text[match.end():])
    if data is None:
        return None
    return reg_type, _decode_binary(reg_type, data)


def _parse_data(text: str) -> tuple[int, object] | None:
    """Parse the right-hand side of a value line into (type, decoded data)."""
    for tag, reg_type in _STRING_TAGS:
        if text.startswith(tag):
            return _parse_string(text, len(tag), reg_type)
    match = _DWORD_RE.match(text)
    if match:
        return REG_DWORD, int(match.group(1), 16)
    return _parse_hex_data(text)


def _parse_value(line: str) -> RegValue | None:
    """Parse a complete (continuations joined) value line; None when malformed."""
    if line.startswith("@"):
        name, rest = "", line[1:]
    else:
        parsed = _unescape(line, 1, '"')
        if parsed is None:
            return None
        name, rest = parsed[0], line[parsed[1]:]
    rest = rest.lstrip()
    if not rest.startswith("="):
        return None
    result = _parse_data(rest[1:].lstrip())
    if result is None:
        return None
    return RegValue(name, result[0], result[1])


def _parse_key_line(line: str) -> tuple[str, int | None] | None:
    """Parse ``[path] 1696000000`` into (path, timestamp); None when malformed."""
    parsed = _unescape(line, 1, "]")
    if parsed is None:
        return None
    path, end = parsed
    stamp = line[end:].strip()
    # ASCII digits only: str.isdigit() also accepts e.g. "²", which int() rejects.
    return path, int(stamp) if _TIMESTAMP_RE.fullmatch(stamp) else None


def _check_header(first_line: str) -> None:
    """Raise :class:`RegistryFormatError` unless ``first_line`` is Wine's version 2 header."""
    header = first_line.lstrip("\ufeff").rstrip("\r")
    match = _HEADER_RE.match(header)
    if match is None or match.group(1) != "2":
        raise RegistryFormatError(
            f"not a Wine registry file (expected {WINE_HEADER!r}, got {header[:40]!r})",
            hint="the Wine prefix may be damaged; run 'fork-linux doctor'",
        )


def _open_key(hive: RegHive, line: str, wanted: set[str] | None) -> RegKey | None:
    """The key a ``[path] stamp`` line starts (created on first sight); None if malformed or not wanted."""
    parsed_key = _parse_key_line(line)
    if parsed_key is None or (wanted is not None and _key_id(parsed_key[0]) not in wanted):
        return None
    current = hive.get(parsed_key[0])
    if current is None:
        current = RegKey(parsed_key[0], timestamp=parsed_key[1])
        hive[parsed_key[0]] = current
    return current


def _join_continued(lines: list[str], index: int, line: str) -> tuple[str, int]:
    """``line`` joined with its backslash-continued lines from ``lines[index]`` on; the next index."""
    # Collect continuation lines in a list: repeated str concatenation
    # is quadratic on large binary values.
    pieces = [line]
    while pieces[-1].endswith("\\") and index < len(lines):
        pieces[-1] = pieces[-1][:-1]
        pieces.append(lines[index].strip())
        index += 1
    return "".join(pieces), index


def _note_meta(hive: RegHive, line: str) -> None:
    """Record the ``;; All keys relative to`` root or the ``#arch=`` of the hive."""
    if line.startswith(_ROOT_MARKER):
        hive.root = line[len(_ROOT_MARKER):].strip().replace("\\\\", "\\")
    elif line.startswith("#arch="):
        hive.arch = line[len("#arch="):].strip()


def parse_wine_reg(text: str, *, keys: Iterable[str] | None = None) -> RegHive:
    """Parse Wine's on-disk registry format.

    ``keys`` limits decoding to those key paths (much faster on ``system.reg``).
    Malformed lines are skipped, as Wine itself does; a missing or unknown
    header raises :class:`RegistryFormatError`.
    """
    lines = text.split("\n")
    _check_header(lines[0])
    wanted = None if keys is None else {_key_id(k) for k in keys}
    hive = RegHive()
    current: RegKey | None = None
    index = 1
    while index < len(lines):
        line = lines[index].rstrip("\r").lstrip()
        index += 1
        if line.startswith("["):
            current = _open_key(hive, line, wanted)
        elif line.startswith(("@", '"')):
            joined, index = _join_continued(lines, index, line)
            value = None if current is None else _parse_value(joined)
            if current is not None and value is not None:
                current.values[value.name.lower()] = value
        else:
            _note_meta(hive, line)
    return hive


def read_hive(path: Path | str, *, keys: Iterable[str] | None = None) -> RegHive:
    """Read and parse a hive file such as ``<prefix>/system.reg`` (size-capped)."""
    with open(path, "rb") as handle:
        data = handle.read(_MAX_HIVE_BYTES + 1)
    if len(data) > _MAX_HIVE_BYTES:
        raise RegistryFormatError(
            f"registry hive is larger than {_MAX_HIVE_BYTES} bytes: {path}",
            hint="the Wine prefix may be damaged; run 'fork-linux doctor'",
        )
    return parse_wine_reg(data.decode("utf-8", "replace"), keys=keys)


def _query_targets(hive: str, key: str) -> list[tuple[str, str]]:
    """Map (hive, key) to the hive files and file-relative key paths to search."""
    full = _HIVE_LOOKUP.get(hive.lower())
    if full == "HKEY_CURRENT_USER":
        return [("user.reg", key)]
    if full == "HKEY_LOCAL_MACHINE":
        return [("system.reg", key)]
    if full == "HKEY_CLASSES_ROOT":
        classes = "Software\\Classes\\" + key
        return [("user.reg", classes), ("system.reg", classes)]
    raise ValueError(f"query supports HKCU, HKLM and HKCR, not {hive!r}")


def query_many(prefix: Path | str, wanted: Iterable[tuple[str, str, str]]) -> list[object | None]:
    """:func:`query` for several ``(hive, key, name)`` at once; each hive file is parsed once.

    Returns the values in the order asked (None when absent).
    """
    items = [(_query_targets(hive, key), name) for hive, key, name in wanted]
    keys_by_file: dict[str, list[str]] = {}
    for targets, _name in items:
        for filename, key_path in targets:
            keys_by_file.setdefault(filename, []).append(key_path)
    hives: dict[str, RegHive] = {}
    for filename, keys in keys_by_file.items():
        with contextlib.suppress(FileNotFoundError):
            hives[filename] = read_hive(Path(prefix) / filename, keys=keys)
    return [_first_value(hives, targets, name) for targets, name in items]


def _first_value(hives: dict[str, RegHive], targets: list[tuple[str, str]], name: str) -> object | None:
    """The data of value ``name`` under the first ``(hive file, key)`` target that has it, else None."""
    for filename, key_path in targets:
        regkey = hives[filename].get(key_path) if filename in hives else None
        value = regkey.get(name) if regkey is not None else None
        if value is not None:
            return value.data
    return None


def query(prefix: Path | str, hive: str, key: str, name: str) -> object | None:
    """Read one value straight from a prefix's hive files, without starting Wine.

    ``key`` is relative to the hive (``Software\\Wine\\DllOverrides``); ``name``
    ``""`` is the default value. Returns int (dword/qword), str (sz/expand_sz),
    list[str] (multi_sz), bytes (binary), or None when absent. ``HKCR`` looks in
    ``user.reg`` then ``system.reg`` under ``Software\\Classes``, value by value.
    """
    for filename, key_path in _query_targets(hive, key):
        try:
            parsed = read_hive(Path(prefix) / filename, keys=[key_path])
        except FileNotFoundError:
            continue
        regkey = parsed.get(key_path)
        value = regkey.get(name) if regkey is not None else None
        if value is not None:
            return value.data
    return None
