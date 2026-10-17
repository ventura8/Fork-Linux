"""Tests for :mod:`fork_linux.pe_icon` (an icon for copies of our own fl-launch.exe)."""

from __future__ import annotations

import struct

import pytest

from fixtures.pe_builder import build_pe, make_version
from fork_linux import imaging, pe_icon
from fork_linux.errors import IntegrityFailed
from fork_linux.pe_resources import RT_GROUP_ICON, RT_ICON, RT_VERSION, PEFile


def _square(size: int, rgba: tuple[int, int, int, int] = (10, 20, 30, 255)) -> bytes:
    return bytes(rgba) * (size * size)


def test_icon_dib_is_a_bottom_up_bgra_bitmap_with_an_empty_mask() -> None:
    top, bottom = (1, 2, 3, 255), (4, 5, 6, 128)
    dib = pe_icon.icon_dib(2, bytes(top) * 2 + bytes(bottom) * 2)
    assert struct.unpack_from("<IiiHH", dib) == (40, 2, 4, 1, 32)
    assert dib[40:48] == bytes((6, 5, 4, 128)) * 2  # the bottom row first, BGRA
    assert dib[48:56] == bytes((3, 2, 1, 255)) * 2
    assert dib[56:] == b"\x00" * 8  # 2 rows of a 32-bit aligned AND mask
    width, height, rgba = imaging.dib_to_rgba(dib)
    assert (width, height, rgba[:4]) == (2, 2, bytes(top))
    with pytest.raises(IntegrityFailed, match="do not match"):
        pe_icon.icon_dib(2, b"\x00")


def test_icon_images_cover_every_size_the_source_can_fill() -> None:
    assert [size for size, _ in pe_icon.icon_images(1024, 1024, _square(1024))] == list(pe_icon.ICON_SIZES)
    assert [size for size, _ in pe_icon.icon_images(32, 32, _square(32))] == [16, 20, 24, 32]
    with pytest.raises(IntegrityFailed, match="not square"):
        pe_icon.icon_images(32, 16, _square(32)[: 32 * 16 * 4])
    with pytest.raises(IntegrityFailed, match="too small"):
        pe_icon.icon_images(8, 8, _square(8))


def test_group_icon_lists_the_images() -> None:
    group = pe_icon.group_icon([(16, b"a" * 5), (256, b"b" * 7)], 3)
    assert struct.unpack_from("<HHH", group) == (0, 1, 2)
    assert struct.unpack_from("<BBBBHHIH", group, 6) == (16, 16, 0, 0, 1, 32, 5, 3)
    assert struct.unpack_from("<BBBBHHIH", group, 20) == (0, 0, 0, 0, 1, 32, 7, 4)


def test_with_icon_keeps_the_other_resources_and_reads_back() -> None:
    version = make_version()
    image = build_pe({RT_VERSION: {1: {1033: version}}, 24: {"MANIFEST": {1033: b"<xml/>"}}})
    out = pe_icon.with_icon(image, 64, 64, _square(64, (200, 100, 50, 255)))
    pe = PEFile(out)
    resources = dict(pe.resources())
    assert resources[(RT_VERSION, 1, 1033)] == version
    assert resources[(24, "MANIFEST", 1033)] == b"<xml/>"
    assert [entry.width for entry in pe.group_icons()[0].entries] == list(pe_icon.ICON_SIZES)
    best = {image.width: image for image in pe.best_icons()}
    _w, _h, rgba = imaging.dib_to_rgba(best[16].data)
    assert rgba[:4] == bytes((200, 100, 50, 255))
    assert pe.sections[-1].name == ".flicon"
    assert pe.data_directories[2][0] == pe.sections[-1].virtual_address
    # Applied again (an icon replaced): the old icons go, one group stays.
    again = PEFile(pe_icon.with_icon(out + b"\x00", 16, 16, _square(16)))
    assert [entry.width for entry in again.group_icons()[0].entries] == [16]
    assert sum(1 for path, _ in again.resources() if path[0] == RT_ICON) == 1
    assert sum(1 for path, _ in again.resources() if path[0] == RT_GROUP_ICON) == 1


def test_with_icon_on_a_pe_without_resources_and_a_pe32() -> None:
    plain = PEFile(pe_icon.with_icon(build_pe(with_resources=False), 16, 16, _square(16)))
    assert [entry.width for entry in plain.group_icons()[0].entries] == [16]
    pe32 = PEFile(pe_icon.with_icon(build_pe(pe32_plus=False), 16, 16, _square(16)))
    assert not pe32.is_pe32_plus
    assert pe32.group_icons()[0].entries[0].width == 16


def test_with_icon_needs_room_for_a_section_header() -> None:
    image = bytearray(build_pe())
    (pe_offset,) = struct.unpack_from("<I", image, 0x3C)
    struct.pack_into("<I", image, pe_offset + 24 + 60, pe_offset + 24 + 112 + 128 + 2 * 40)  # SizeOfHeaders
    with pytest.raises(IntegrityFailed, match="no room"):
        pe_icon.with_icon(bytes(image), 16, 16, _square(16))


def test_checksum_matches_the_reference_algorithm() -> None:
    image = bytearray(b"\x01\x02\x03\x04\x05\x06\x07")  # odd length
    total = sum(struct.unpack("<HHH", bytes(image[:6]))) + 7
    total = (total & 0xFFFF) + (total >> 16)
    assert pe_icon._checksum(image, 100) == (total & 0xFFFF) + len(image)
