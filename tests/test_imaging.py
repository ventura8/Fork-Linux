"""Tests for fork_linux.imaging: PNG writer/validator and icon DIB decoder."""

from __future__ import annotations

import shutil
import struct
import subprocess
import zlib
from pathlib import Path

import pytest
from fixtures import pe_builder as pb

from fork_linux import imaging
from fork_linux.errors import ExitCode, IntegrityFailed


def _chunk(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)


def _png_with_size(width: int, height: int, *, idat: bool = True) -> bytes:
    """A structurally valid PNG claiming ``width`` x ``height`` (the IDAT is a stub)."""
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    data = _chunk(b"IDAT", zlib.compress(b"")) if idat else b""
    return imaging.PNG_SIGNATURE + _chunk(b"IHDR", header) + data + _chunk(b"IEND", b"")


def _pixels(rgba: bytes, width: int) -> list[list[tuple[int, ...]]]:
    """Split a top-down RGBA buffer into rows of (r, g, b, a) tuples."""
    stride = width * 4
    return [
        [tuple(rgba[y * stride + x * 4 : y * stride + x * 4 + 4]) for x in range(width)]
        for y in range(len(rgba) // stride)
    ]


def _mask_alpha(x: int, y: int) -> int:
    return 0 if pb.pattern_mask(x, y) else 255


# --- png_encode ---------------------------------------------------------------


def test_png_encode_round_trips_pixels_with_filter_zero_rows() -> None:
    width, height = 7, 5
    rgba = bytes(b for y in range(height) for x in range(width) for b in pb.pattern_rgba(x, y))
    png = imaging.png_encode(width, height, rgba)
    assert pb.decode_png(png) == (width, height, rgba)
    assert imaging.png_size(png) == (width, height)
    assert imaging.png_validate(png) == (width, height)


def test_png_encode_writes_ihdr_rgba_8bit() -> None:
    png = imaging.png_encode(1, 1, b"\x01\x02\x03\x04")
    assert png[12:16] == b"IHDR"
    assert struct.unpack(">IIBBBBB", png[16:29]) == (1, 1, 8, 6, 0, 0, 0)
    assert png.endswith(_chunk(b"IEND", b""))


@pytest.mark.parametrize(
    ("width", "height", "length"),
    [(0, 1, 0), (1, 0, 0), (-1, 1, 4), (2, 2, 15), (2, 2, 17)],
)
def test_png_encode_rejects_mismatched_buffers(width: int, height: int, length: int) -> None:
    data = bytes(length)
    with pytest.raises(ValueError, match="does not match"):
        imaging.png_encode(width, height, data)


# --- png_size / png_validate ---------------------------------------------------------


@pytest.mark.parametrize(
    "data",
    [
        b"",
        imaging.PNG_SIGNATURE,
        b"GIF89a" + bytes(30),
        imaging.PNG_SIGNATURE + _chunk(b"IDAT", bytes(13)),
        imaging.PNG_SIGNATURE + struct.pack(">I", 12) + b"IHDR" + bytes(12),
    ],
)
def test_png_size_rejects_non_png(data: bytes) -> None:
    with pytest.raises(IntegrityFailed, match="not a PNG image") as caught:
        imaging.png_size(data)
    assert caught.value.exit_code == ExitCode.INTEGRITY_FAILED
    assert caught.value.hint


def test_png_size_reads_ihdr() -> None:
    assert imaging.png_size(pb.make_png(3, 9)) == (3, 9)


@pytest.mark.parametrize(("width", "height"), [(0, 4), (4, 0), (1025, 4), (4, 1025)])
def test_png_validate_rejects_out_of_range_sizes(width: int, height: int) -> None:
    png = _png_with_size(width, height)
    with pytest.raises(IntegrityFailed, match="outside 1..1024"):
        imaging.png_validate(png)


def test_png_validate_accepts_maximum_size_and_trailing_bytes() -> None:
    assert imaging.png_validate(_png_with_size(1024, 1024) + b"trailing") == (1024, 1024)


def test_png_validate_rejects_bad_crc() -> None:
    png = bytearray(pb.make_png(4, 4))
    png[40] ^= 0xFF  # inside the IDAT payload
    data = bytes(png)
    with pytest.raises(IntegrityFailed, match="truncated or corrupt"):
        imaging.png_validate(data)


def test_png_validate_rejects_chunk_running_past_end() -> None:
    png = pb.make_png(4, 4)
    with pytest.raises(IntegrityFailed, match="truncated or corrupt"):
        imaging.png_validate(png[:-14])


def test_png_validate_requires_image_data() -> None:
    png = _png_with_size(4, 4, idat=False)
    with pytest.raises(IntegrityFailed, match="no IDAT chunk"):
        imaging.png_validate(png)
    # An ancillary chunk is not image data either.
    no_data = _png_with_size(4, 4, idat=False)
    with_text = no_data[:33] + _chunk(b"tEXt", b"k\x00v") + no_data[33:]
    with pytest.raises(IntegrityFailed, match="no IDAT chunk"):
        imaging.png_validate(with_text)


def test_png_validate_requires_iend() -> None:
    png = pb.make_png(4, 4)
    assert png.endswith(_chunk(b"IEND", b""))
    with pytest.raises(IntegrityFailed, match="no IEND"):
        imaging.png_validate(png[:-12])
    with pytest.raises(IntegrityFailed, match="no IEND"):
        imaging.png_validate(png[:-12] + b"\x00\x00\x00")


# --- dib_to_rgba ---------------------------------------------------------------


def test_dib_32bpp_uses_its_alpha_channel_and_flips_rows() -> None:
    width, height, rgba = imaging.dib_to_rgba(pb.make_dib(6, 4, 32))
    assert (width, height) == (6, 4)
    assert _pixels(rgba, width) == [[pb.pattern_rgba(x, y) for x in range(6)] for y in range(4)]


def test_dib_32bpp_with_zero_alpha_takes_alpha_from_and_mask() -> None:
    def opaque_less(x: int, y: int) -> tuple[int, int, int, int]:
        r, g, b, _ = pb.pattern_rgba(x, y)
        return (r, g, b, 0)

    width, height, rgba = imaging.dib_to_rgba(pb.make_dib(9, 3, 32, opaque_less))
    expected = [[opaque_less(x, y)[:3] + (_mask_alpha(x, y),) for x in range(9)] for y in range(3)]
    assert _pixels(rgba, width) == expected


def test_dib_32bpp_with_color_table_and_v5_header() -> None:
    dib = pb.make_dib(4, 4, 32, colors_used=2, extra_palette=2, header_size=124)
    width, _, rgba = imaging.dib_to_rgba(dib)
    assert _pixels(rgba, width) == [[pb.pattern_rgba(x, y) for x in range(4)] for y in range(4)]


@pytest.mark.parametrize("width", [1, 5, 8, 33])
def test_dib_24bpp_with_row_padding_and_mask(width: int) -> None:
    out_width, height, rgba = imaging.dib_to_rgba(pb.make_dib(width, 3, 24))
    expected = [[pb.pattern_rgba(x, y)[:3] + (_mask_alpha(x, y),) for x in range(width)] for y in range(3)]
    assert (out_width, height) == (width, 3)
    assert _pixels(rgba, width) == expected


def test_dib_without_and_mask_is_opaque() -> None:
    width, _, rgba = imaging.dib_to_rgba(pb.make_dib(5, 2, 24, include_mask=False))
    assert all(pixel[3] == 255 for row in _pixels(rgba, width) for pixel in row)


@pytest.mark.parametrize(("bits", "width"), [(8, 7), (4, 9), (1, 13), (8, 32), (4, 16), (1, 40)])
def test_dib_paletted_maps_indexes_and_mask(bits: int, width: int) -> None:
    palette = pb.gray_palette(bits)
    width_out, height, rgba = imaging.dib_to_rgba(pb.make_dib(width, 5, bits, palette=palette))
    expected = [[palette[pb.pattern_index(x, y, bits)] + (_mask_alpha(x, y),) for x in range(width)] for y in range(5)]
    assert (width_out, height) == (width, 5)
    assert _pixels(rgba, width) == expected


def test_dib_8bpp_with_short_palette_and_out_of_range_index() -> None:
    palette = [(10, 20, 30), (40, 50, 60), (70, 80, 90)]

    def index(x: int, y: int) -> int:
        return (x + y) % 4  # index 3 is past the 3-color palette -> black

    width, _, rgba = imaging.dib_to_rgba(pb.make_dib(6, 2, 8, index, palette=palette, colors_used=3, mask=None))
    colors = palette + [(0, 0, 0)]
    assert _pixels(rgba, width) == [[colors[index(x, y)] + (255,) for x in range(6)] for y in range(2)]


def test_dib_colors_used_is_capped_to_palette_size() -> None:
    dib = pb.make_dib(3, 2, 1, palette=pb.gray_palette(1), colors_used=2, mask=None)
    patched = bytearray(dib)
    struct.pack_into("<I", patched, 32, 99)  # biClrUsed larger than a 1-bpp palette
    assert imaging.dib_to_rgba(bytes(patched)) == imaging.dib_to_rgba(dib)


def _header(size: int = 40, width: int = 4, doubled: int = 8, bits: int = 32, compression: int = 0) -> bytes:
    """A BITMAPINFOHEADER followed by enough zero bytes for a 4x4 32-bpp image."""
    return struct.pack("<IiiHHIIiiII", size, width, doubled, 1, bits, compression, 0, 0, 0, 0, 0) + bytes(128)


@pytest.mark.parametrize(
    ("dib", "message"),
    [
        (bytes(39), "header is truncated"),
        (_header(size=12), "unsupported DIB header"),
        (_header(width=0), "unsupported DIB header"),
        (_header(width=-4), "unsupported DIB header"),
        (_header(width=1025), "unsupported DIB header"),
        (_header(doubled=1), "unsupported DIB header"),
        (_header(doubled=-8), "unsupported DIB header"),
        (_header(doubled=2050), "unsupported DIB header"),
        (_header(compression=3), "unsupported DIB format"),
        (_header(bits=16), "unsupported DIB format"),
        (_header(bits=0), "unsupported DIB format"),
        (pb.make_dib(4, 4, 32)[:100], "pixel data is truncated"),
        (pb.make_dib(4, 4, 8)[: 40 + 1024 + 15], "pixel data is truncated"),
    ],
)
def test_dib_rejects_malformed_input(dib: bytes, message: str) -> None:
    with pytest.raises(IntegrityFailed, match=message) as caught:
        imaging.dib_to_rgba(dib)
    assert caught.value.exit_code == ExitCode.INTEGRITY_FAILED


def test_dib_accepts_header_and_pixels_without_mask() -> None:
    width, height, rgba = imaging.dib_to_rgba(_header(bits=32)[: 40 + 64])
    assert (width, height) == (4, 4)
    assert rgba == bytes((0, 0, 0, 255)) * 16


@pytest.mark.parametrize(("width", "height", "bits"), [(32, 32, 32), (48, 16, 24), (7, 3, 8), (1, 1, 1)])
def test_dib_size_reads_only_the_header(width: int, height: int, bits: int) -> None:
    dib = pb.make_dib(width, height, bits)
    assert imaging.dib_size(dib) == (width, height)
    # The pixels are not needed (nor checked): the header alone answers.
    assert imaging.dib_size(dib[:40]) == (width, height)


@pytest.mark.parametrize("dib", [bytes(20), _header(bits=16), _header(doubled=1)])
def test_dib_size_rejects_bad_headers(dib: bytes) -> None:
    with pytest.raises(IntegrityFailed, match="DIB"):
        imaging.dib_size(dib)


def test_image_size_handles_png_and_dib() -> None:
    assert imaging.image_size(pb.make_png(20, 10)) == (20, 10)
    assert imaging.image_size(pb.make_dib(12, 6, 4)) == (12, 6)
    with pytest.raises(IntegrityFailed, match="DIB header is truncated"):
        imaging.image_size(b"GIF89a")


def test_dib_to_png_matches_decoded_pixels() -> None:
    dib = pb.make_dib(16, 16, 8)
    png = imaging.dib_to_png(dib)
    assert imaging.png_validate(png) == (16, 16)
    assert pb.decode_png(png) == imaging.dib_to_rgba(dib)


def test_imagemagick_reads_generated_png(tmp_path: Path) -> None:
    identify = shutil.which("identify")
    convert = shutil.which("convert") or shutil.which("magick")
    if identify is None or convert is None:
        pytest.skip("ImageMagick is not installed")
    width, height, rgba = imaging.dib_to_rgba(pb.make_dib(20, 12, 32))
    target = tmp_path / "icon.png"
    target.write_bytes(imaging.png_encode(width, height, rgba))
    shape = subprocess.run(
        [identify, "-format", "%m %w %h", str(target)], check=True, capture_output=True, text=True
    ).stdout
    assert shape == "PNG 20 12"
    raw = subprocess.run([convert, str(target), "-depth", "8", "rgba:-"], check=True, capture_output=True).stdout
    assert raw == rgba
