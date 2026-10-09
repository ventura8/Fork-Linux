"""Build synthetic PE32/PE32+ images with icon and version resources, plus malformed variants.

Pure Python and independent of fork_linux (so tests do not check the code
against itself). No real Fork.exe and no Fork artwork: the icons are generated
geometric patterns.
"""

from __future__ import annotations

import functools
import struct
import zlib
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

RT_ICON = 3
RT_GROUP_ICON = 14
RT_VERSION = 16
MACHINE_I386 = 0x014C
MACHINE_AMD64 = 0x8664
LANG_EN_US = 0x409
APP_ICON_GROUP = 32512
FILE_ALIGN = 0x200
SECTION_ALIGN = 0x1000
HEADERS_SIZE = 0x400
TEXT_RVA = 0x1000
RSRC_RVA = 0x2000
HIGH_BIT = 0x80000000

Key = int | str
Path3 = tuple[Key, ...]
Tree = dict[Key, dict[Key, dict[Key, bytes]]]
Rgba = tuple[int, int, int, int]

DEFAULT_STRINGS = {
    "CompanyName": "Example Vendor",
    "FileDescription": "Synthetic test executable",
    "FileVersion": "9.9.9",
    "ProductName": "Synthetic",
}


def _align(value: int, alignment: int) -> int:
    return (value + alignment - 1) // alignment * alignment


# --- pixel patterns ---------------------------------------------------------


def pattern_rgba(x: int, y: int) -> Rgba:
    """Deterministic RGBA test pattern with varying alpha."""
    return (x * 7 % 256, y * 5 % 256, (x + y) * 3 % 256, (x * y + 40) % 256)


def pattern_index(x: int, y: int, bits: int = 8) -> int:
    """Deterministic palette index for an image with ``bits`` bits per pixel."""
    return (x + 2 * y) % (1 << bits)


def pattern_mask(x: int, y: int) -> bool:
    """AND-mask pattern: True (transparent) on a diagonal band and the left column."""
    return x == 0 or (x + y) % 5 == 0


def gray_palette(bits: int) -> list[tuple[int, int, int]]:
    """A palette with distinct colors for every index of a ``bits``-bpp image."""
    count = 1 << bits
    return [(i * 255 // (count - 1), (i * 37) % 256, 255 - i * 255 // (count - 1)) for i in range(count)]


# --- images -----------------------------------------------------------------


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    crc = zlib.crc32(kind + payload) & 0xFFFFFFFF
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", crc)


def make_png(width: int, height: int, pixel: Callable[[int, int], Rgba] = pattern_rgba) -> bytes:
    """Encode an RGBA PNG (filter 0 rows) independently of fork_linux.imaging."""
    raw = bytearray()
    for y in range(height):
        raw.append(0)
        for x in range(width):
            raw += bytes(pixel(x, y))
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", zlib.compress(bytes(raw)))
        + _png_chunk(b"IEND", b"")
    )


def decode_png(png: bytes) -> tuple[int, int, bytes]:
    """Strictly decode an 8-bit RGBA, non-interlaced PNG whose rows all use filter 0.

    Checks the signature, every chunk CRC, IHDR fields and each row's filter
    byte, so tests can verify encoder output without trusting fork_linux.
    """
    assert png[:8] == b"\x89PNG\r\n\x1a\n", "bad PNG signature"
    offset = 8
    chunks = []
    while offset < len(png):
        (length,) = struct.unpack_from(">I", png, offset)
        kind = png[offset + 4 : offset + 8]
        payload = png[offset + 8 : offset + 8 + length]
        (crc,) = struct.unpack_from(">I", png, offset + 8 + length)
        assert crc == zlib.crc32(kind + payload) & 0xFFFFFFFF, f"bad CRC in {kind!r}"
        chunks.append((kind, payload))
        offset += 12 + length
    assert chunks[0][0] == b"IHDR" and chunks[-1] == (b"IEND", b""), "bad chunk order"
    width, height, depth, color_type, method, filtering, interlace = struct.unpack(">IIBBBBB", chunks[0][1])
    assert (depth, color_type, method, filtering, interlace) == (8, 6, 0, 0, 0), "not 8-bit RGBA"
    raw = zlib.decompress(b"".join(payload for kind, payload in chunks if kind == b"IDAT"))
    stride = width * 4 + 1
    assert len(raw) == stride * height, "IDAT size mismatch"
    rows = [raw[y * stride : (y + 1) * stride] for y in range(height)]
    assert all(row[0] == 0 for row in rows), "unexpected PNG filter type"
    return width, height, b"".join(row[1:] for row in rows)


def _pack_bits(values: list[int], bits: int, stride: int) -> bytes:
    row = bytearray(stride)
    for x, value in enumerate(values):
        bit = x * bits
        row[bit >> 3] |= (value & ((1 << bits) - 1)) << (8 - bits - (bit & 7))
    return bytes(row)


def _default_pixel(bits: int) -> Callable[[int, int], object]:
    def rgb(x: int, y: int) -> tuple[int, ...]:
        return pattern_rgba(x, y)[:3]

    def index(x: int, y: int) -> int:
        return pattern_index(x, y, bits)

    return {32: pattern_rgba, 24: rgb}.get(bits, index)


def make_dib(
    width: int,
    height: int,
    bits: int,
    pixel: Callable[[int, int], object] | None = None,
    *,
    mask: Callable[[int, int], bool] | None = pattern_mask,
    palette: list[tuple[int, int, int]] | None = None,
    colors_used: int = 0,
    header_size: int = 40,
    compression: int = 0,
    doubled_height: int | None = None,
    include_mask: bool = True,
    extra_palette: int = 0,
) -> bytes:
    """Build an icon DIB: BITMAPINFOHEADER, palette, bottom-up XOR rows, AND mask.

    ``pixel(x, y)`` returns (r, g, b, a) for 32 bpp, (r, g, b) for 24 bpp and a
    palette index otherwise; ``mask(x, y)`` is True where the icon is transparent.
    ``extra_palette`` appends that many unused RGBQUADs (for 24/32-bpp color tables).
    """
    if pixel is None:
        pixel = _default_pixel(bits)
    stride = (width * bits + 31) // 32 * 4
    mask_stride = (width + 31) // 32 * 4
    xor = bytearray()
    and_mask = bytearray()
    for file_row in range(height):
        y = height - 1 - file_row
        if bits == 32:
            row = b"".join(bytes((p[2], p[1], p[0], p[3])) for p in (pixel(x, y) for x in range(width)))
        elif bits == 24:
            row = b"".join(bytes((p[2], p[1], p[0])) for p in (pixel(x, y) for x in range(width)))
        else:
            row = _pack_bits([pixel(x, y) for x in range(width)], bits, stride)
        xor += row.ljust(stride, b"\x00")
        flags = [1 if (mask is not None and mask(x, y)) else 0 for x in range(width)]
        and_mask += _pack_bits(flags, 1, mask_stride)
    if palette is None and bits <= 8:
        palette = gray_palette(bits)
    table = b"".join(bytes((b, g, r, 0)) for (r, g, b) in (palette or []))
    table += b"\x00\x00\x00\x00" * extra_palette
    doubled = height * 2 if doubled_height is None else doubled_height
    header = struct.pack(
        "<IiiHHIIiiII", header_size, width, doubled, 1, bits, compression, len(xor), 0, 0, colors_used, 0
    ).ljust(header_size, b"\x00")
    return header + table + bytes(xor) + (bytes(and_mask) if include_mask else b"")


def make_group(entries: list[tuple[int, int, int, int, int, int, int]], *, reserved: int = 0, kind: int = 1) -> bytes:
    """RT_GROUP_ICON data from (width, height, colors, planes, bits, size, icon_id) tuples."""
    out = struct.pack("<HHH", reserved, kind, len(entries))
    for width, height, colors, planes, bits, size, icon_id in entries:
        out += struct.pack("<BBBBHHIH", width % 256, height % 256, colors, 0, planes, bits, size, icon_id)
    return out


def _pad4(data: bytes) -> bytes:
    return data + b"\x00" * (-len(data) % 4)


def version_block(key: str, value: bytes = b"", *, text: bool = False, children: tuple[bytes, ...] = ()) -> bytes:
    """One VS_VERSIONINFO-style block (children are 4-byte aligned)."""
    body = _pad4(b"\x00" * 6 + (key + "\x00").encode("utf-16-le")) + value
    if children:
        body = _pad4(body) + b"".join(_pad4(child) for child in children[:-1]) + children[-1]
    value_length = len(value) // 2 if text else len(value)
    return struct.pack("<HHH", len(body), value_length, 1 if text else 0) + body[6:]


def make_version(
    file_version: tuple[int, int, int, int] = (2, 23, 2, 0),
    product_version: tuple[int, int, int, int] = (2, 23, 2, 7),
    *,
    strings: dict[str, str] | None = None,
    fixed: bool = True,
    signature: int = 0xFEEF04BD,
    root_key: str = "VS_VERSION_INFO",
    lang: str = "040904B0",
    trailing_zero_padding: int = 0,
) -> bytes:
    """An RT_VERSION resource: VS_FIXEDFILEINFO, VarFileInfo and a StringFileInfo table."""
    fmaj, fmin, fpatch, fbuild = file_version
    pmaj, pmin, ppatch, pbuild = product_version
    fixed_info = b""
    if fixed:
        fixed_info = struct.pack(
            "<13I",
            signature,
            0x00010000,
            (fmaj << 16) | fmin,
            (fpatch << 16) | fbuild,
            (pmaj << 16) | pmin,
            (ppatch << 16) | pbuild,
            0x3F,
            0,
            0x40004,
            1,
            0,
            0,
            0,
        )
    table_strings = DEFAULT_STRINGS if strings is None else strings
    string_blocks = tuple(
        version_block(name, (value + "\x00").encode("utf-16-le"), text=True) for name, value in table_strings.items()
    )
    table = version_block(lang, text=True, children=string_blocks)
    string_info = version_block("StringFileInfo", text=True, children=(table,))
    translation = version_block("Translation", struct.pack("<HH", 0x409, 1200))
    var_info = version_block("VarFileInfo", text=True, children=(translation,))
    root = version_block(root_key, fixed_info, children=(var_info, string_info))
    if trailing_zero_padding:
        padded = _pad4(root) + b"\x00" * trailing_zero_padding
        root = struct.pack("<H", len(padded)) + padded[2:]
    return root


# --- resource section --------------------------------------------------------


@dataclass
class RsrcLayout:
    """A built .rsrc section plus the offsets tests need to corrupt it."""

    data: bytearray
    dirs: dict[Path3, int]
    entries: dict[Path3, int]
    data_entries: dict[Path3, int]
    blobs: dict[Path3, int]


def _sorted_keys(keys: Iterable[Key]) -> tuple[list[Key], int, int]:
    names = sorted(k for k in keys if isinstance(k, str))
    ids = sorted(k for k in keys if isinstance(k, int))
    return names + ids, len(names), len(ids)


def _children(tree: Tree, path: Path3) -> dict:
    node: dict = tree
    for key in path:
        node = node[key]
    return node


def build_rsrc(tree: Tree, base_rva: int = RSRC_RVA) -> RsrcLayout:
    """Lay out a three-level resource tree: directories, data entries, names, blobs."""
    level1 = [(t,) for t in _sorted_keys(tree)[0]]
    level2 = [(t, n) for (t,) in level1 for n in _sorted_keys(tree[t])[0]]
    leaves = [(t, n, lang) for (t, n) in level2 for lang in _sorted_keys(tree[t][n])[0]]
    dirs: dict[Path3, int] = {}
    offset = 0
    for path in [()] + level1 + level2:
        dirs[path] = offset
        offset += 16 + 8 * len(_children(tree, path))
    data_entries: dict[Path3, int] = {}
    for leaf in leaves:
        data_entries[leaf] = offset
        offset += 16
    strings: dict[str, int] = {}
    for path in level1 + level2 + leaves:
        key = path[-1]
        if isinstance(key, str) and key not in strings:
            strings[key] = offset
            offset = _align(offset + 2 + 2 * len(key), 4)
    blobs: dict[Path3, int] = {}
    for leaf in leaves:
        offset = _align(offset, 8)
        blobs[leaf] = offset
        offset += len(_children(tree, leaf[:2])[leaf[2]])
    buf = bytearray(offset)
    entries: dict[Path3, int] = {}
    for path, dir_offset in dirs.items():
        keys, named, ids = _sorted_keys(_children(tree, path))
        struct.pack_into("<IIHHHH", buf, dir_offset, 0, 0, 4, 0, named, ids)
        for index, key in enumerate(keys):
            child = path + (key,)
            name_field = strings[key] | HIGH_BIT if isinstance(key, str) else key
            target = dirs[child] | HIGH_BIT if child in dirs else data_entries[child]
            entries[child] = dir_offset + 16 + 8 * index
            struct.pack_into("<II", buf, entries[child], name_field, target)
    for key, name_offset in strings.items():
        struct.pack_into("<H", buf, name_offset, len(key))
        buf[name_offset + 2 : name_offset + 2 + 2 * len(key)] = key.encode("utf-16-le")
    for leaf in leaves:
        blob = _children(tree, leaf[:2])[leaf[2]]
        struct.pack_into("<IIII", buf, data_entries[leaf], base_rva + blobs[leaf], len(blob), 1252, 0)
        buf[blobs[leaf] : blobs[leaf] + len(blob)] = blob
    return RsrcLayout(buf, dirs, entries, data_entries, blobs)


# --- whole image -----------------------------------------------------------------


@dataclass
class BuiltPE:
    """A built image and the offsets of its headers, for targeted corruption."""

    data: bytearray
    pe_offset: int
    optional_offset: int
    data_dirs_offset: int
    sections_offset: int
    rsrc_raw_pointer: int
    layout: RsrcLayout | None

    def __bytes__(self) -> bytes:
        return bytes(self.data)


@functools.cache
def default_images() -> tuple[bytes, bytes, bytes]:
    """The default icon images (256x256 PNG, 32x32 32-bpp DIB, 16x16 8-bpp DIB), built once."""
    return make_png(256, 256), make_dib(32, 32, 32), make_dib(16, 16, 8)


def default_tree() -> Tree:
    """App icon group (256 PNG, 32x32 32-bpp DIB, 16x16 8-bpp DIB) and a version resource."""
    png, dib32, dib8 = default_images()
    group = make_group(
        [
            (256, 256, 0, 1, 32, len(png), 1),
            (32, 32, 0, 1, 32, len(dib32), 2),
            (16, 16, 0, 1, 8, len(dib8), 3),
        ]
    )
    return {
        RT_ICON: {1: {LANG_EN_US: png}, 2: {LANG_EN_US: dib32}, 3: {LANG_EN_US: dib8}},
        RT_GROUP_ICON: {APP_ICON_GROUP: {LANG_EN_US: group}},
        RT_VERSION: {1: {LANG_EN_US: make_version()}},
    }


def build_pe_ex(
    tree: Tree | None = None,
    *,
    with_resources: bool = True,
    pe32_plus: bool = True,
    machine: int | None = None,
    subsystem: int = 2,
    characteristics: int = 0x0022,
    rva_and_sizes: int = 16,
    patch_rsrc: Callable[[RsrcLayout], None] | None = None,
) -> BuiltPE:
    """Build a minimal two-section (.text, .rsrc) PE32+ or PE32 image."""
    if machine is None:
        machine = MACHINE_AMD64 if pe32_plus else MACHINE_I386
    layout = None
    rsrc = b""
    if with_resources:
        layout = build_rsrc(default_tree() if tree is None else tree)
        if patch_rsrc is not None:
            patch_rsrc(layout)
        rsrc = bytes(layout.data)
    n_dirs = 16
    dirs_rel = 112 if pe32_plus else 96
    opt_size = dirs_rel + 8 * n_dirs
    pe_offset = 0x80
    optional_offset = pe_offset + 24
    sections = [(b".text", TEXT_RVA, 0x10, FILE_ALIGN, HEADERS_SIZE, 0x60000020)]
    if with_resources:
        raw_size = _align(len(rsrc), FILE_ALIGN)
        sections.append((b".rsrc", RSRC_RVA, len(rsrc), raw_size, HEADERS_SIZE + FILE_ALIGN, 0x40000040))
    image_size = _align(RSRC_RVA + max(len(rsrc), 1), SECTION_ALIGN)
    opt = bytearray(opt_size)
    struct.pack_into("<H", opt, 0, 0x20B if pe32_plus else 0x10B)
    struct.pack_into("<I", opt, 16, TEXT_RVA)
    if pe32_plus:
        struct.pack_into("<Q", opt, 24, 0x140000000)
    else:
        struct.pack_into("<I", opt, 28, 0x400000)
    alignment_and_versions = (SECTION_ALIGN, FILE_ALIGN, 6, 0, 0, 0, 6, 0, 0)
    struct.pack_into("<IIHHHHHHIIII", opt, 32, *alignment_and_versions, image_size, HEADERS_SIZE, 0)
    struct.pack_into("<HH", opt, 68, subsystem, 0x8160)
    struct.pack_into("<I", opt, dirs_rel - 4, rva_and_sizes)
    if with_resources:
        struct.pack_into("<II", opt, dirs_rel + 8 * 2, RSRC_RVA, len(rsrc))
    head = bytearray(HEADERS_SIZE)
    head[0:2] = b"MZ"
    struct.pack_into("<I", head, 0x3C, pe_offset)
    head[pe_offset : pe_offset + 4] = b"PE\x00\x00"
    struct.pack_into("<HHIIIHH", head, pe_offset + 4, machine, len(sections), 0, 0, 0, opt_size, characteristics)
    head[optional_offset : optional_offset + opt_size] = opt
    sections_offset = optional_offset + opt_size
    for index, (name, vaddr, vsize, raw_size, raw_ptr, flags) in enumerate(sections):
        struct.pack_into(
            "<8sIIIIIIHHI", head, sections_offset + 40 * index, name, vsize, vaddr, raw_size, raw_ptr, 0, 0, 0, 0, flags
        )
    text = b"\xc3".ljust(FILE_ALIGN, b"\x00")
    data = bytearray(head + text + rsrc.ljust(_align(len(rsrc), FILE_ALIGN), b"\x00"))
    return BuiltPE(
        data=data,
        pe_offset=pe_offset,
        optional_offset=optional_offset,
        data_dirs_offset=optional_offset + dirs_rel,
        sections_offset=sections_offset,
        rsrc_raw_pointer=HEADERS_SIZE + FILE_ALIGN,
        layout=layout,
    )


def build_pe(tree: Tree | None = None, **kwargs: Any) -> bytes:
    """Bytes of :func:`build_pe_ex`."""
    return bytes(build_pe_ex(tree, **kwargs))


# --- malformed variants --------------------------------------------------------------


def _patched(patch: Callable[[RsrcLayout], None], tree: Tree | None = None) -> bytes:
    return build_pe(tree, patch_rsrc=patch)


def _resource_loop(layout: RsrcLayout) -> None:
    struct.pack_into("<I", layout.data, layout.entries[(RT_ICON,)] + 4, layout.dirs[()] | HIGH_BIT)


def _resource_too_deep(layout: RsrcLayout) -> None:
    target = layout.dirs[(RT_VERSION,)] | HIGH_BIT
    struct.pack_into("<I", layout.data, layout.entries[(RT_ICON, 1, LANG_EN_US)] + 4, target)


def _huge_count(layout: RsrcLayout) -> None:
    struct.pack_into("<HH", layout.data, layout.dirs[()] + 12, 0xFFFF, 0xFFFF)


def _icon_rva_out_of_bounds(layout: RsrcLayout) -> None:
    struct.pack_into("<I", layout.data, layout.data_entries[(RT_ICON, 2, LANG_EN_US)], 0x7FFF0000)


def _subdir_out_of_bounds(layout: RsrcLayout) -> None:
    struct.pack_into("<I", layout.data, layout.entries[(RT_ICON,)] + 4, 0x00FFFFF0 | HIGH_BIT)


def _with_header_patch(offset_of: Callable[[BuiltPE], int], fmt: str, value: int) -> bytes:
    built = build_pe_ex()
    struct.pack_into(fmt, built.data, offset_of(built), value)
    return bytes(built)


def bad_mz() -> bytes:
    data = bytearray(build_pe())
    data[0:2] = b"ZM"
    return bytes(data)


def bad_pe_signature() -> bytes:
    built = build_pe_ex()
    built.data[built.pe_offset + 1] = ord("X")
    return bytes(built)


def lfanew_out_of_bounds() -> bytes:
    return _with_header_patch(lambda b: 0x3C, "<I", 0x7FFFFFF0)


def bad_optional_magic() -> bytes:
    return _with_header_patch(lambda b: b.optional_offset, "<H", 0x999)


def optional_header_too_small() -> bytes:
    return _with_header_patch(lambda b: b.pe_offset + 20, "<H", 64)


def too_many_sections() -> bytes:
    return _with_header_patch(lambda b: b.pe_offset + 6, "<H", 200)


def rsrc_directory_out_of_bounds() -> bytes:
    return _with_header_patch(lambda b: b.data_dirs_offset + 16, "<I", 0x00900000)


def resource_loop() -> bytes:
    return _patched(_resource_loop)


def resource_too_deep() -> bytes:
    return _patched(_resource_too_deep)


def huge_count() -> bytes:
    return _patched(_huge_count)


def icon_rva_out_of_bounds() -> bytes:
    return _patched(_icon_rva_out_of_bounds)


def subdir_out_of_bounds() -> bytes:
    return _patched(_subdir_out_of_bounds)


def truncated(length: int) -> bytes:
    return build_pe()[:length]


MALFORMED: dict[str, Callable[[], bytes]] = {
    "empty": lambda: b"",
    "mz-only": lambda: b"MZ",
    "bad-mz": bad_mz,
    "bad-pe-signature": bad_pe_signature,
    "lfanew-out-of-bounds": lfanew_out_of_bounds,
    "truncated-coff": lambda: truncated(0x88),
    "truncated-optional": lambda: truncated(0x100),
    "truncated-sections": lambda: truncated(0x1A0),
    "truncated-rsrc": lambda: truncated(HEADERS_SIZE + FILE_ALIGN + 0x40),
    "truncated-resource-data": lambda: truncated(len(build_pe()) - 0x400),
    "bad-optional-magic": bad_optional_magic,
    "optional-header-too-small": optional_header_too_small,
    "too-many-sections": too_many_sections,
    "rsrc-directory-out-of-bounds": rsrc_directory_out_of_bounds,
    "resource-loop": resource_loop,
    "resource-too-deep": resource_too_deep,
    "huge-count": huge_count,
    "icon-rva-out-of-bounds": icon_rva_out_of_bounds,
    "subdir-out-of-bounds": subdir_out_of_bounds,
}
