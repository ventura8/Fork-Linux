"""Give a copy of our own ``fl-launch.exe`` an icon (the Open in IDE stubs).

Fork's Open in menu shows the icon of the executable it found for an editor
(``Icon.ExtractAssociatedIcon``): a bare ``fl-launch.exe`` copy has none. This
module returns a copy of the image whose resource tree is the original one
(manifest, version) plus ``RT_ICON`` images and one ``RT_GROUP_ICON``, written
as a new last section; the resource data directory points at it and the old
``.rsrc`` stays as unused bytes. Only ever applied to ``fl-launch.exe``, which
we build (AGENTS.md hard rule 1: Fork's binaries are never patched).
"""

from __future__ import annotations

import struct
from collections.abc import Sequence
from typing import Any

from . import imaging
from .errors import IntegrityFailed
from .pe_resources import PE32_PLUS_MAGIC, RT_GROUP_ICON, RT_ICON, PEFile, ResourceKey

# Square sizes written as 32-bit DIBs (a 16 px menu icon at 100-400 % scaling).
ICON_SIZES = (16, 20, 24, 32, 40, 48, 64)
SECTION_NAME = b".flicon\x00"
GROUP_ID = 1
_SECTION_FLAGS = 0x40000040  # initialised data, readable
_HIGH_BIT = 0x80000000


def _align(value: int, alignment: int) -> int:
    return (value + alignment - 1) // alignment * alignment


def icon_dib(size: int, rgba: bytes) -> bytes:
    """A 32-bit ``RT_ICON`` image: BITMAPINFOHEADER (double height), BGRA rows bottom-up, empty AND mask."""
    if len(rgba) != size * size * 4:
        raise IntegrityFailed(f"icon pixels do not match {size}x{size}")
    header = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0, 0, 0, 0, 0, 0)
    rows = []
    for y in range(size - 1, -1, -1):
        row = bytearray(rgba[y * size * 4 : (y + 1) * size * 4])
        row[0::4], row[2::4] = row[2::4], row[0::4]
        rows.append(bytes(row))
    mask = b"\x00" * (_align(size, 32) // 8 * size)
    return header + b"".join(rows) + mask


def icon_images(width: int, height: int, rgba: bytes) -> list[tuple[int, bytes]]:
    """``(size, RT_ICON data)`` for every :data:`ICON_SIZES` entry the square source can fill."""
    if width != height:
        raise IntegrityFailed(f"icon is not square ({width}x{height})")
    base, base_rgba = width, rgba
    if base > 128:  # one big step first: every size below comes from 128 px
        base_rgba = imaging.downscale_rgba(base, base, rgba, 128, 128)
        base = 128
    images = []
    for size in ICON_SIZES:
        if size > base:
            continue
        pixels = base_rgba if size == base else imaging.downscale_rgba(base, base, base_rgba, size, size)
        images.append((size, icon_dib(size, pixels)))
    if not images:
        raise IntegrityFailed(f"icon is too small ({width}x{height})")
    return images


def group_icon(images: Sequence[tuple[int, bytes]], first_id: int) -> bytes:
    """An ``RT_GROUP_ICON`` (GRPICONDIR) listing ``images`` as ids ``first_id``, ``first_id + 1``, …"""
    data = struct.pack("<HHH", 0, 1, len(images))
    for index, (size, image) in enumerate(images):
        data += struct.pack("<BBBBHHIH", size % 256, size % 256, 0, 0, 1, 32, len(image), first_id + index)
    return data


def _sort_key(key: ResourceKey) -> tuple[int, str, int]:
    """Names before ids, names by text, ids ascending (the order Windows requires)."""
    return (0, key, 0) if isinstance(key, str) else (1, "", key)


class _Layout:
    """The directory part of a resource tree being written: tables, leaf slots and name slots."""

    def __init__(self) -> None:
        self.dirs = bytearray()
        self.leaves: list[tuple[int, bytes]] = []  # (offset of the entry's target field, data)
        self.names: list[tuple[int, str]] = []  # (offset of the entry's name field, name)

    def directory(self, children: dict[ResourceKey, Any]) -> tuple[int, list[tuple[int, Any]]]:
        """Write one directory table; return its offset and ``(target field offset, child)`` per entry."""
        offset = len(self.dirs)
        keys = sorted(children, key=_sort_key)
        named = sum(isinstance(key, str) for key in keys)
        self.dirs.extend(struct.pack("<IIHHHH", 0, 0, 0, 0, named, len(keys) - named))
        targets = []
        for key in keys:
            if isinstance(key, str):
                self.names.append((len(self.dirs), key))
            self.dirs.extend(struct.pack("<II", 0 if isinstance(key, str) else key, 0))
            targets.append((len(self.dirs) - 4, children[key]))
        return offset, targets

    def tables(self, tree: dict[ResourceKey, Any]) -> None:
        """Every directory table, breadth-first (the root first)."""
        queue: list[tuple[int | None, dict[ResourceKey, Any]]] = [(None, tree)]
        while queue:
            slot, children = queue.pop(0)
            offset, targets = self.directory(children)
            if slot is not None:
                struct.pack_into("<I", self.dirs, slot, offset | _HIGH_BIT)
            for field_offset, child in targets:
                if isinstance(child, dict):
                    queue.append((field_offset, child))
                else:
                    self.leaves.append((field_offset, child))


def resource_section(resources: Sequence[tuple[tuple[ResourceKey, ...], bytes]], rva: int) -> bytes:
    """A three-level resource tree for ``resources`` laid out to be mapped at ``rva``.

    Layout: directory tables, data entries, names (length-prefixed UTF-16), then the data (8-aligned).
    """
    tree: dict[ResourceKey, dict[ResourceKey, dict[ResourceKey, bytes]]] = {}
    for (kind, name, lang), data in resources:
        tree.setdefault(kind, {}).setdefault(name, {})[lang] = data
    layout = _Layout()
    layout.tables(tree)
    entries_at = len(layout.dirs)
    strings_at = entries_at + 16 * len(layout.leaves)
    strings = bytearray()
    for field_offset, name in layout.names:
        struct.pack_into("<I", layout.dirs, field_offset, (strings_at + len(strings)) | _HIGH_BIT)
        encoded = name.encode("utf-16-le")
        strings.extend(struct.pack("<H", len(encoded) // 2) + encoded)
    blob_at = _align(strings_at + len(strings), 8)
    table = bytearray()
    blobs = bytearray()
    for index, (field_offset, data) in enumerate(layout.leaves):
        struct.pack_into("<I", layout.dirs, field_offset, entries_at + 16 * index)
        table.extend(struct.pack("<IIII", rva + blob_at + len(blobs), len(data), 0, 0))
        blobs.extend(data + b"\x00" * (_align(len(data), 8) - len(data)))
    head = bytes(layout.dirs) + bytes(table) + bytes(strings)
    return head + b"\x00" * (blob_at - len(head)) + bytes(blobs)


def _checksum(image: bytearray, checksum_at: int) -> int:
    """The PE image checksum (16-bit folded sum of the words, plus the length)."""
    padded = bytes(image) + b"\x00" * (len(image) % 2)
    total = 0
    for index, (word,) in enumerate(struct.iter_unpack("<H", padded)):
        if checksum_at // 2 <= index < checksum_at // 2 + 2:
            continue
        total += word
        total = (total & 0xFFFF) + (total >> 16)
    return (total & 0xFFFF) + len(image)


def with_icon(image: bytes, width: int, height: int, rgba: bytes) -> bytes:
    """A copy of the PE ``image`` whose application icon is the square RGBA picture ``rgba``."""
    pe = PEFile(image)
    icons = icon_images(width, height, rgba)
    kept = [(path, data) for path, data in pe.resources() if path[0] not in (RT_ICON, RT_GROUP_ICON)]
    added = [((RT_ICON, index + 1, 0), data) for index, (_size, data) in enumerate(icons)]
    added.append(((RT_GROUP_ICON, GROUP_ID, 0), group_icon(icons, 1)))
    out = bytearray(image)
    (pe_offset,) = struct.unpack_from("<I", out, 0x3C)
    coff = pe_offset + 4
    (n_sections,) = struct.unpack_from("<H", out, coff + 2)
    (opt_size,) = struct.unpack_from("<H", out, coff + 16)
    opt = coff + 20
    (magic,) = struct.unpack_from("<H", out, opt)
    section_align, file_align = struct.unpack_from("<II", out, opt + 32)
    (size_of_headers,) = struct.unpack_from("<I", out, opt + 60)
    table_end = opt + opt_size + 40 * n_sections
    first_raw = min((s.raw_pointer for s in pe.sections if s.raw_pointer), default=size_of_headers)
    if table_end + 40 > min(size_of_headers, first_raw):
        raise IntegrityFailed("no room for another section header")
    rva = _align(max(s.virtual_address + max(s.virtual_size, s.raw_size) for s in pe.sections), section_align)
    section = resource_section(kept + added, rva)
    raw_size = _align(len(section), file_align)
    raw_pointer = _align(len(out), file_align)
    out.extend(b"\x00" * (raw_pointer - len(out)) + section + b"\x00" * (raw_size - len(section)))
    struct.pack_into("<8sIIIIIIHHI", out, table_end, SECTION_NAME, len(section), rva, raw_size, raw_pointer,
                     0, 0, 0, 0, _SECTION_FLAGS)
    struct.pack_into("<H", out, coff + 2, n_sections + 1)
    struct.pack_into("<I", out, opt + 56, _align(rva + len(section), section_align))  # SizeOfImage
    dirs_at = opt + (112 if magic == PE32_PLUS_MAGIC else 96)
    struct.pack_into("<II", out, dirs_at + 8 * 2, rva, len(section))
    struct.pack_into("<I", out, opt + 64, _checksum(out, opt + 64))
    return bytes(out)
