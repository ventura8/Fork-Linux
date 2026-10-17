"""Tests for :mod:`fork_linux.font_names` (the interface font installed under its own family)."""

from __future__ import annotations

import struct
from collections.abc import Callable

import pytest
from fixtures.setup_ctx import fake_face

from fork_linux import font_names
from fork_linux.errors import IntegrityFailed


def _records(font: bytes) -> dict[tuple[int, int], str]:
    """(platform, name ID) -> text of every name record."""
    count = struct.unpack_from(">H", font, 4)[0]
    for index in range(count):
        tag, _sum, offset, _length = struct.unpack_from(">4sIII", font, 12 + 16 * index)
        if tag == b"name":
            _fmt, total, strings = struct.unpack_from(">HHH", font, offset)
            found = {}
            for row in range(total):
                platform, _enc, _lang, name_id, size, at = struct.unpack_from(">6H", font, offset + 6 + 12 * row)
                raw = font[offset + strings + at : offset + strings + at + size]
                found[(platform, name_id)] = raw.decode("utf-16-be" if platform == 3 else "ascii")
            return found
    raise AssertionError("no name table")


def _checksum(data: bytes) -> int:
    data += b"\x00" * (-len(data) % 4)
    return sum(struct.unpack(f">{len(data) // 4}I", data)) & 0xFFFFFFFF


def test_names_follow_segoe_uis_layout() -> None:
    assert font_names.names("FL UI Sans", "Regular") == {1: "FL UI Sans", 2: "Regular", 4: "FL UI Sans",
                                                         6: "FLUISans-Regular"}
    assert font_names.names("FL UI Sans", "Semibold") == {1: "FL UI Sans Semibold", 2: "Regular",
                                                          4: "FL UI Sans Semibold", 6: "FLUISans-Semibold",
                                                          16: "FL UI Sans", 17: "Semibold"}


def test_rename_rewrites_the_names_and_keeps_the_rest() -> None:
    renamed = font_names.rename(fake_face("Noto Sans"), "FL UI Sans", "Light")
    records = _records(renamed)
    assert records[(3, 1)] == records[(1, 1)] == "FL UI Sans Light"
    assert records[(3, 16)] == "FL UI Sans"
    assert (3, 21) not in records
    assert _checksum(renamed) == 0xB1B0AFBA  # whole-file checksum after checkSumAdjustment
    # Records the rename does not own (here a copyright, ID 0) are kept.
    font = bytearray(fake_face("x"))
    name_at = struct.unpack_from(">I", font, 12 + 16 + 8)[0]
    struct.pack_into(">H", font, name_at + 6 + 6, 0)  # the one record becomes name ID 0
    assert _records(font_names.rename(bytes(font), "A", "Bold"))[(3, 0)] == "x"


@pytest.mark.parametrize(
    ("damage", "message"),
    [
        (lambda font: font[:8], "truncated header"),
        (lambda font: b"OTTO" + font[4:], "not a TrueType font"),
        (lambda font: font[:4] + struct.pack(">H", 65) + font[6:], "too large or truncated"),
        (lambda font: font[:12 + 8] + struct.pack(">I", 1 << 20) + font[12 + 12 :], "runs past the end"),
        (lambda font: font[:12] + b"xxxx" + font[16:], "no head table"),
        (lambda font: font[:12 + 12] + struct.pack(">I", 4) + font[12 + 16 :], "head table is truncated"),
    ],
)
def test_malformed_fonts_are_refused(damage: Callable[[bytes], bytes], message: str) -> None:
    with pytest.raises(IntegrityFailed, match=message):
        font_names.rename(damage(fake_face("x")), "A", "Regular")


def _name_damaged(patch: Callable[[bytearray, int], None]) -> bytes:
    font = bytearray(fake_face("x"))
    name_at = struct.unpack_from(">I", font, 12 + 16 + 8)[0]
    patch(font, name_at)
    return bytes(font)


def test_malformed_name_tables_are_refused() -> None:
    def short(font: bytearray, _at: int) -> None:
        struct.pack_into(">I", font, 12 + 16 + 12, 4)

    def many(font: bytearray, at: int) -> None:
        struct.pack_into(">H", font, at + 2, font_names.MAX_NAME_RECORDS + 1)

    def outside(font: bytearray, at: int) -> None:
        struct.pack_into(">H", font, at + 6 + 8, 4000)

    for patch, message in ((short, "name table is truncated"), (many, "too large or truncated"),
                           (outside, "runs past the name table")):
        with pytest.raises(IntegrityFailed, match=message):
            font_names.rename(_name_damaged(patch), "A", "Regular")


def test_limits() -> None:
    with pytest.raises(IntegrityFailed, match="larger than"):
        font_names.rename(b"\x00" * (font_names.MAX_FONT_BYTES + 1), "A", "Regular")
    with pytest.raises(IntegrityFailed, match="ASCII"):
        font_names.rename(fake_face("x"), "Ä", "Regular")
