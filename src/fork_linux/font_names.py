"""Give a downloaded TrueType face the family name fork-linux installs it under.

The interface font is Noto Sans (SIL OFL 1.1, no reserved font name), installed
in the prefix as its own family, :data:`fork_linux.manifest.UiFont.family`:
Wine also sees the desktop's own "Noto Sans" (often only Regular and Bold), and
WPF then drew Fork's semibold and bold text in the regular face. The new names
follow Segoe UI's (and Selawik's) layout: Regular and Bold are the family
itself, other weights are a family of their own (``<family> Semibold``) with
the typographic family (name IDs 16/17) pointing back at it.

Only the ``name`` table and the checksums change; every other table is copied
byte for byte. The input is a pinned, sha256-checked download, but it is still
parsed with bounds checks and limits.
"""

from __future__ import annotations

import struct

from .errors import IntegrityFailed

MAX_FONT_BYTES = 16 * 1024 * 1024
MAX_TABLES = 64
MAX_NAME_RECORDS = 4096
RIBBI = ("Regular", "Bold")
# Name IDs rewritten here: family, subfamily, full name, PostScript name, typographic
# family / subfamily, WWS family / subfamily. Every other record is kept.
_REPLACED = (1, 2, 4, 6, 16, 17, 21, 22)
_CHECKSUM_MAGIC = 0xB1B0AFBA
_WINDOWS = (3, 1, 0x409)  # Windows, Unicode BMP, en-US
_MAC = (1, 0, 0)  # Macintosh, Roman, English


def _fail(what: str) -> IntegrityFailed:
    return IntegrityFailed(f"font: {what}", hint="delete the download cache and run setup again")


def _checksum(data: bytes) -> int:
    padded = data + b"\x00" * (-len(data) % 4)
    return sum(struct.unpack(f">{len(padded) // 4}I", padded)) & 0xFFFFFFFF


def _tables(font: bytes) -> tuple[int, dict[bytes, bytes]]:
    """The sfnt version and every table's bytes."""
    if len(font) > MAX_FONT_BYTES:
        raise _fail(f"larger than {MAX_FONT_BYTES} bytes")
    if len(font) < 12:
        raise _fail("truncated header")
    version, count = struct.unpack_from(">IH", font)
    if version != 0x00010000:
        raise _fail(f"not a TrueType font (0x{version:08x})")
    if count > MAX_TABLES or 12 + 16 * count > len(font):
        raise _fail(f"table directory of {count} entries is too large or truncated")
    tables = {}
    for index in range(count):
        tag, _checksum_value, offset, length = struct.unpack_from(">4sIII", font, 12 + 16 * index)
        if offset + length > len(font):
            raise _fail(f"table {tag!r} runs past the end of the file")
        tables[tag] = font[offset : offset + length]
    for tag in (b"name", b"head"):
        if tag not in tables:
            raise _fail(f"no {tag.decode()} table")
    if len(tables[b"head"]) < 12:
        raise _fail("head table is truncated")
    return version, tables


def names(family: str, weight: str) -> dict[int, str]:
    """The name records (ID -> text) of ``weight`` in ``family``."""
    ribbi = weight in RIBBI
    full = family if weight == "Regular" else f"{family} {weight}"
    chosen = {
        1: family if ribbi else full,
        2: weight if ribbi else "Regular",
        4: full,
        6: f"{family}-{weight}".replace(" ", ""),
    }
    if not ribbi:
        chosen.update({16: family, 17: weight})
    return chosen


def _name_table(old: bytes, wanted: dict[int, str]) -> bytes:
    """``old`` without the :data:`_REPLACED` records, plus ``wanted`` for Windows and Macintosh."""
    if len(old) < 6:
        raise _fail("name table is truncated")
    _format, count, strings_at = struct.unpack_from(">HHH", old)
    if count > MAX_NAME_RECORDS or 6 + 12 * count > len(old):
        raise _fail(f"name table of {count} records is too large or truncated")
    records: list[tuple[tuple[int, int, int, int], bytes]] = []
    for index in range(count):
        platform, encoding, language, name_id, length, offset = struct.unpack_from(">6H", old, 6 + 12 * index)
        start = strings_at + offset
        if start + length > len(old):
            raise _fail("a name record runs past the name table")
        if name_id not in _REPLACED:
            records.append(((platform, encoding, language, name_id), old[start : start + length]))
    for name_id, text in wanted.items():
        records.append(((*_WINDOWS, name_id), text.encode("utf-16-be")))
        records.append(((*_MAC, name_id), text.encode("ascii")))
    records.sort(key=lambda record: record[0])
    heads = bytearray()
    strings = bytearray()
    for key, raw in records:
        heads.extend(struct.pack(">6H", *key, len(raw), len(strings)))
        strings.extend(raw)
    return struct.pack(">HHH", 0, len(records), 6 + len(heads)) + bytes(heads) + bytes(strings)


def rename(font: bytes, family: str, weight: str) -> bytes:
    """``font`` (a TrueType file) renamed to ``weight`` of ``family``, with fresh checksums."""
    if not family.isascii() or not weight.isascii():
        raise _fail("names must be ASCII")
    version, tables = _tables(font)
    tables[b"name"] = _name_table(tables[b"name"], names(family, weight))
    head = bytearray(tables[b"head"])
    head[8:12] = b"\x00\x00\x00\x00"  # checkSumAdjustment, recomputed below
    tables[b"head"] = bytes(head)
    tags = sorted(tables)
    selector = max(power for power in range(16) if 2**power <= len(tags))
    search = 2**selector * 16
    out = bytearray(struct.pack(">IHHHH", version, len(tags), search, selector, 16 * len(tags) - search))
    directory_at = len(out)
    out.extend(b"\x00" * 16 * len(tags))
    head_at = 0
    for index, tag in enumerate(tags):
        data = tables[tag]
        if tag == b"head":
            head_at = len(out)
        struct.pack_into(">4sIII", out, directory_at + 16 * index, tag, _checksum(data), len(out), len(data))
        out.extend(data + b"\x00" * (-len(data) % 4))
    struct.pack_into(">I", out, head_at + 8, (_CHECKSUM_MAGIC - _checksum(bytes(out))) & 0xFFFFFFFF)
    return bytes(out)
