"""Bounded, pure-Python reader for the headers and resources of a Windows PE image.

Fork.exe is untrusted input. Every read is bounds-checked; the resource tree is
limited to three levels, 4096 entries per directory, a visited-offset set and
64 MiB of resource data; icon groups are capped and each icon image is read
once. Any malformation raises :class:`IntegrityFailed`.
Only what icon and version extraction needs is parsed; nothing is executed,
patched or written back.
"""

from __future__ import annotations

import os
import stat
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple

from .errors import IntegrityFailed
from .imaging import PNG_SIGNATURE

MAX_FILE_SIZE = 512 * 1024 * 1024
MAX_DEPTH = 3
MAX_ENTRIES = 4096
MAX_TOTAL_ENTRIES = 65536
MAX_GROUP_ENTRIES = 65536  # icon images listed by all RT_GROUP_ICONs together
MAX_RESOURCE_BYTES = 64 * 1024 * 1024
MAX_SECTIONS = 96

MACHINE_I386 = 0x014C
MACHINE_AMD64 = 0x8664
PE32_MAGIC = 0x10B
PE32_PLUS_MAGIC = 0x20B

RT_ICON = 3
RT_GROUP_ICON = 14
RT_VERSION = 16

ResourceKey = int | str

_DATA_DIRECTORY_OFFSET = {PE32_MAGIC: 96, PE32_PLUS_MAGIC: 112}
_SUBSYSTEM_OFFSET = 68
_DIR_RESOURCE = 2
_HIGH_BIT = 0x80000000
_LOW_BITS = 0x7FFFFFFF
_FIXED_SIGNATURE = 0xFEEF04BD
_HINT = "The file is not a well-formed Windows executable; re-run 'fork-linux setup' to reinstall Fork."


def _fail(what: str) -> IntegrityFailed:
    return IntegrityFailed(f"malformed PE image: {what}", hint=_HINT)


def _refuse(path: Path | str, why: str) -> IntegrityFailed:
    return IntegrityFailed(f"{path} {why}; refusing to parse it", hint=_HINT)


def _unpack(fmt: str, data: bytes | memoryview, offset: int, what: str) -> tuple[int, ...]:
    """``struct.unpack_from`` that raises IntegrityFailed instead of reading out of bounds."""
    if offset < 0 or offset + struct.calcsize(fmt) > len(data):
        raise _fail(f"{what} at offset {offset} is out of bounds")
    return struct.unpack_from(fmt, data, offset)


def _align4(value: int) -> int:
    return (value + 3) & ~3


class Section(NamedTuple):
    """One entry of the section table."""

    name: str
    virtual_address: int
    virtual_size: int
    raw_pointer: int
    raw_size: int


class IconEntry(NamedTuple):
    """One GRPICONDIRENTRY of an RT_GROUP_ICON resource (0 width/height means 256)."""

    width: int
    height: int
    color_count: int
    planes: int
    bit_count: int
    size: int
    icon_id: int


@dataclass(frozen=True)
class GroupIcon:
    """An RT_GROUP_ICON resource: its id (or name) and the icon images it lists."""

    id_or_name: ResourceKey
    entries: list[IconEntry] = field(default_factory=list)


class IconImage(NamedTuple):
    """The raw RT_ICON bytes of one chosen icon size: a PNG or an icon DIB."""

    width: int
    height: int
    data: bytes
    is_png: bool


class _Leaf(NamedTuple):
    path: tuple[ResourceKey, ...]  # (type, name, language)
    rva: int
    size: int


class _ResourceWalker:
    """Depth-, count-, loop- and size-limited walk of a resource directory tree."""

    def __init__(self, rsrc: memoryview) -> None:
        self.rsrc = rsrc
        self.visited: set[int] = set()
        self.entries = 0
        self.total_bytes = 0
        self.leaves: list[_Leaf] = []

    def walk(self) -> list[_Leaf]:
        self._directory(0, ())
        return self.leaves

    def _directory(self, offset: int, path: tuple[ResourceKey, ...]) -> None:
        if len(path) >= MAX_DEPTH:
            raise _fail(f"resource tree is nested deeper than {MAX_DEPTH} levels")
        if offset in self.visited:
            raise _fail("resource tree contains a loop")
        self.visited.add(offset)
        named, ids = _unpack("<HH", self.rsrc, offset + 12, "resource directory")
        count = named + ids
        self.entries += count
        if count > MAX_ENTRIES or self.entries > MAX_TOTAL_ENTRIES:
            raise _fail(f"resource directory lists too many entries ({count})")
        for index in range(count):
            name, target = _unpack("<II", self.rsrc, offset + 16 + 8 * index, "resource entry")
            entry_path = path + (self._key(name),)
            if target & _HIGH_BIT:
                self._directory(target & _LOW_BITS, entry_path)
            elif len(entry_path) == MAX_DEPTH:
                self._leaf(target, entry_path)
            # A data entry above the language level is not a valid resource: skip it.

    def _leaf(self, offset: int, path: tuple[ResourceKey, ...]) -> None:
        rva, size = _unpack("<II", self.rsrc, offset, "resource data entry")
        self.total_bytes += size
        if self.total_bytes > MAX_RESOURCE_BYTES:
            raise _fail(f"resources exceed {MAX_RESOURCE_BYTES} bytes")
        self.leaves.append(_Leaf(path, rva, size))

    def _key(self, name: int) -> ResourceKey:
        if not name & _HIGH_BIT:
            return name & 0xFFFF
        offset = name & _LOW_BITS
        (length,) = _unpack("<H", self.rsrc, offset, "resource name")
        raw = self.rsrc[offset + 2 : offset + 2 + 2 * length]
        if len(raw) != 2 * length:
            raise _fail("resource name is out of bounds")
        return bytes(raw).decode("utf-16-le", "replace")


class _VersionBlock(NamedTuple):
    key: str
    value: bytes
    children: int
    end: int


def _utf16_nul(data: bytes, start: int, end: int) -> int:
    """Return the offset of the UTF-16 NUL that ends a key starting at ``start``."""
    for offset in range(start, end - 1, 2):
        if data[offset : offset + 2] == b"\x00\x00":
            return offset
    raise _fail("version resource key is not terminated")


def _version_block(data: bytes, offset: int, limit: int) -> _VersionBlock:
    """Parse one VS_VERSIONINFO-style block (length, value length, type, key, value)."""
    length, value_length, value_type = _unpack("<HHH", data, offset, "version block")
    end = offset + length
    if length < 6 or end > limit:
        raise _fail("version resource block has a bad length")
    key_end = _utf16_nul(data, offset + 6, end)
    value_start = _align4(key_end + 2)
    value_size = value_length * 2 if value_type == 1 else value_length
    value_end = min(value_start + value_size, end)
    key = data[offset + 6 : key_end].decode("utf-16-le", "replace")
    return _VersionBlock(key, data[value_start:value_end], _align4(value_end), end)


def _version_children(data: bytes, block: _VersionBlock) -> list[_VersionBlock]:
    children = []
    offset = block.children
    # Some linkers pad the parent with zeros after its last child.
    while offset < block.end and data[offset : offset + 2] != b"\x00\x00":
        child = _version_block(data, offset, block.end)
        children.append(child)
        offset = _align4(child.end)
    return children


def _dotted(most: int, least: int) -> str:
    return f"{most >> 16}.{most & 0xFFFF}.{least >> 16}.{least & 0xFFFF}"


def _parse_group(data: bytes) -> list[IconEntry]:
    reserved, kind, count = _unpack("<HHH", data, 0, "icon group header")
    if reserved != 0 or kind != 1:
        raise _fail("icon group has a bad header")
    entries = []
    for index in range(count):
        width, height, colors, _, planes, bits, size, icon_id = _unpack(
            "<BBBBHHIH", data, 6 + 14 * index, "icon group entry"
        )
        entries.append(IconEntry(width or 256, height or 256, colors, planes, bits, size, icon_id))
    return entries


class PEFile:
    """A parsed PE32/PE32+ image held in memory; resources are parsed on first use."""

    def __init__(self, data: bytes) -> None:
        self._data = bytes(data)
        self._leaves: list[_Leaf] | None = None
        self._icons: dict[ResourceKey, _Leaf] | None = None
        self.machine = 0
        self.characteristics = 0
        self.is_pe32_plus = False
        self.subsystem = 0
        self.data_directories: list[tuple[int, int]] = []
        self.sections: list[Section] = []
        self._parse_headers()

    @classmethod
    def from_path(cls, path: Path | str, max_size: int = MAX_FILE_SIZE) -> PEFile:
        """Read and parse the regular file ``path``; refuse anything larger than ``max_size`` bytes."""
        # O_NONBLOCK: a FIFO planted where the exe should be must not hang the caller.
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise _refuse(path, "is not a regular file")
            if info.st_size > max_size:
                raise _refuse(path, f"is larger than {max_size} bytes")
            handle = os.fdopen(fd, "rb")
        except BaseException:
            os.close(fd)
            raise
        with handle:
            # Size the read from fstat: read(max_size + 1) would allocate max_size up front.
            data = handle.read(info.st_size + 1)
        if len(data) != info.st_size:
            raise _refuse(path, "changed while it was being read")
        return cls(data)

    def _parse_headers(self) -> None:
        data = self._data
        if data[:2] != b"MZ":
            raise _fail("missing MZ signature")
        (pe_offset,) = _unpack("<I", data, 0x3C, "e_lfanew")
        if data[pe_offset : pe_offset + 4] != b"PE\x00\x00":
            raise _fail("missing PE signature")
        coff = pe_offset + 4
        machine, n_sections, _, _, _, opt_size, characteristics = _unpack("<HHIIIHH", data, coff, "COFF header")
        opt = coff + 20
        (magic,) = _unpack("<H", data, opt, "optional header")
        dirs_rel = _DATA_DIRECTORY_OFFSET.get(magic)
        if dirs_rel is None:
            raise _fail(f"unknown optional header magic 0x{magic:x}")
        if opt_size < dirs_rel or opt + opt_size > len(data):
            raise _fail("optional header is truncated")
        (subsystem,) = struct.unpack_from("<H", data, opt + _SUBSYSTEM_OFFSET)
        (n_rva,) = struct.unpack_from("<I", data, opt + dirs_rel - 4)
        n_dirs = min(n_rva, 16, (opt_size - dirs_rel) // 8)
        table = opt + opt_size
        if n_sections > MAX_SECTIONS or table + 40 * n_sections > len(data):
            raise _fail(f"section table of {n_sections} entries is too large or truncated")
        self.machine = machine
        self.characteristics = characteristics
        self.is_pe32_plus = magic == PE32_PLUS_MAGIC
        self.subsystem = subsystem
        self.data_directories = [struct.unpack_from("<II", data, opt + dirs_rel + 8 * index) for index in range(n_dirs)]
        for index in range(n_sections):
            raw_name, vsize, vaddr, raw_size, raw_ptr = struct.unpack_from("<8sIIII", data, table + 40 * index)
            name = raw_name.rstrip(b"\x00").decode("ascii", "replace")
            self.sections.append(Section(name, vaddr, vsize, raw_ptr, raw_size))

    def _section_for(self, rva: int, size: int) -> Section | None:
        for section in self.sections:
            if section.virtual_address <= rva and rva + size <= section.virtual_address + section.raw_size:
                return section
        return None

    def _rva_slice(self, rva: int, size: int) -> bytes:
        section = self._section_for(rva, size)
        if section is None:
            raise _fail(f"resource data at RVA 0x{rva:x} (+{size}) is outside every section")
        start = section.raw_pointer + rva - section.virtual_address
        if start + size > len(self._data):
            raise _fail(f"resource data at RVA 0x{rva:x} runs past the end of the file")
        return self._data[start : start + size]

    def _resource_leaves(self) -> list[_Leaf]:
        if self._leaves is None:
            self._leaves = self._parse_resources()
        return self._leaves

    def _parse_resources(self) -> list[_Leaf]:
        rva, _ = (self.data_directories + [(0, 0)] * 3)[_DIR_RESOURCE]
        if rva == 0:
            return []
        # Like the Windows loader, ignore the declared size (some linkers round it
        # past the raw data): only the root header must be mapped, and the walk
        # is bounded by the section's raw data anyway.
        section = self._section_for(rva, 16)
        if section is None:
            raise _fail(f"resource directory at RVA 0x{rva:x} is outside every section")
        start = section.raw_pointer + rva - section.virtual_address
        end = min(section.raw_pointer + section.raw_size, len(self._data))
        # A view, not a copy: the section may be large and is only read sparsely.
        return _ResourceWalker(memoryview(self._data)[start:end]).walk()

    def _leaves_of(self, resource_type: int) -> list[_Leaf]:
        return [leaf for leaf in self._resource_leaves() if leaf.path[0] == resource_type]

    def _group_leaves(self) -> list[_Leaf]:
        """One RT_GROUP_ICON leaf per id or name (its first language), in directory order."""
        leaves = []
        seen: set[ResourceKey] = set()
        for leaf in self._leaves_of(RT_GROUP_ICON):
            if leaf.path[1] not in seen:
                seen.add(leaf.path[1])
                leaves.append(leaf)
        return leaves

    def group_icons(self) -> list[GroupIcon]:
        """Every RT_GROUP_ICON resource, in directory order (the first is the app icon)."""
        groups = []
        total = 0
        for leaf in self._group_leaves():
            entries = _parse_group(self._rva_slice(leaf.rva, leaf.size))
            # Groups may alias one another's data: bound the objects, not just the bytes.
            total += len(entries)
            if total > MAX_GROUP_ENTRIES:
                raise _fail(f"icon groups list more than {MAX_GROUP_ENTRIES} images")
            groups.append(GroupIcon(leaf.path[1], entries))
        return groups

    def _icon_leaves(self) -> dict[ResourceKey, _Leaf]:
        """RT_ICON leaves by id (first language wins), indexed once for O(1) lookups."""
        if self._icons is None:
            icons: dict[ResourceKey, _Leaf] = {}
            for leaf in self._leaves_of(RT_ICON):
                icons.setdefault(leaf.path[1], leaf)
            self._icons = icons
        return self._icons

    def icon_image(self, entry: IconEntry) -> bytes:
        """The RT_ICON bytes (PNG or icon DIB) that a group entry refers to."""
        leaf = self._icon_leaves().get(entry.icon_id)
        if leaf is None:
            raise _fail(f"icon {entry.icon_id} listed in the icon group is missing")
        return self._rva_slice(leaf.rva, leaf.size)

    def best_icons(self, min_size: int = 16) -> list[IconImage]:
        """One image per size of the first icon group, highest bit depth wins, smallest first.

        Only the first group is parsed, and images are read once per icon id, so
        a group that lists one large image under thousands of sizes costs no
        more memory than the image itself.
        """
        leaves = self._group_leaves()
        if not leaves:
            return []
        best: dict[tuple[int, int], IconEntry] = {}
        for entry in _parse_group(self._rva_slice(leaves[0].rva, leaves[0].size)):
            if entry.width < min_size or entry.height < min_size:
                continue
            current = best.get((entry.width, entry.height))
            if current is None or entry.bit_count > current.bit_count:
                best[(entry.width, entry.height)] = entry
        images = []
        read: dict[int, bytes] = {}
        for size in sorted(best):
            icon_id = best[size].icon_id
            if icon_id not in read:
                read[icon_id] = self.icon_image(best[size])
            data = read[icon_id]
            images.append(IconImage(size[0], size[1], data, data.startswith(PNG_SIGNATURE)))
        return images

    def version_info(self) -> dict[str, str]:
        """FileVersion/ProductVersion from VS_FIXEDFILEINFO plus the string table entries."""
        leaves = self._leaves_of(RT_VERSION)
        if not leaves:
            return {}
        data = self._rva_slice(leaves[0].rva, leaves[0].size)
        root = _version_block(data, 0, len(data))
        if root.key != "VS_VERSION_INFO":
            raise _fail(f"version resource has the unexpected key {root.key!r}")
        info: dict[str, str] = {}
        if len(root.value) >= 52:
            signature, _, file_ms, file_ls, product_ms, product_ls = struct.unpack_from("<6I", root.value)
            if signature == _FIXED_SIGNATURE:
                info["FileVersion"] = _dotted(file_ms, file_ls)
                info["ProductVersion"] = _dotted(product_ms, product_ls)
        for child in _version_children(data, root):
            if child.key != "StringFileInfo":
                continue
            for table in _version_children(data, child):
                for string in _version_children(data, table):
                    text = string.value.decode("utf-16-le", "replace").split("\x00", 1)[0]
                    info.setdefault(string.key, text)
        return info


def pe_machine(path: Path | str) -> int:
    """The COFF machine type of the PE file at ``path`` (e.g. 0x8664 for x86-64)."""
    return PEFile.from_path(path).machine


def is_pe32_plus_x86_64(path: Path | str) -> bool:
    """True when ``path`` is a well-formed PE32+ image for x86-64 (OSError if unreadable)."""
    try:
        pe = PEFile.from_path(path)
    except IntegrityFailed:
        return False
    return pe.is_pe32_plus and pe.machine == MACHINE_AMD64
