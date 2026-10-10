"""Tests for fork_linux.imaging: PNG writer/validator/reader, icon DIB decoder and area downscaler."""

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


# --- png_to_rgba ---------------------------------------------------------------


def _paeth(left: int, up: int, upper_left: int) -> int:
    guess = left + up - upper_left
    pa, pb_, pc = abs(guess - left), abs(guess - up), abs(guess - upper_left)
    if pa <= pb_ and pa <= pc:
        return left
    return up if pb_ <= pc else upper_left


def _filter_row(kind: int, line: bytes, prev: bytes, bpp: int) -> bytes:
    """Apply PNG filter ``kind`` to one row (the spec's encoder side, independent of the decoder)."""
    out = bytearray()
    for i, value in enumerate(line):
        left = line[i - bpp] if i >= bpp else 0
        up = prev[i]
        upper_left = prev[i - bpp] if i >= bpp else 0
        predictor = (0, left, up, (left + up) // 2, _paeth(left, up, upper_left))[kind]
        out.append((value - predictor) & 0xFF)
    return bytes(out)


def _make_png(
    width: int,
    height: int,
    samples: bytes,
    *,
    color_type: int = 6,
    depth: int = 8,
    filters: tuple[int, ...] = (0,),
    chunks: tuple[tuple[bytes, bytes], ...] = (),
    interlace: int = 0,
    idat: bytes | None = None,
    split: int = 0,
) -> bytes:
    """A PNG of raw ``samples`` (rows top-down), each row filtered with the next of ``filters``."""
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(color_type, 1)
    stride = width * channels * depth // 8
    bpp = max(1, channels * depth // 8)
    raw = bytearray()
    prev = bytes(stride)
    for y in range(height):
        line = samples[y * stride : (y + 1) * stride]
        kind = filters[y % len(filters)]
        raw += bytes((kind,)) + _filter_row(kind, line, prev, bpp)
        prev = line
    data = zlib.compress(bytes(raw)) if idat is None else idat
    header = struct.pack(">IIBBBBB", width, height, depth, color_type, 0, 0, interlace)
    body = b"".join(_chunk(kind, payload) for kind, payload in chunks)
    if split:
        image = b"".join(_chunk(b"IDAT", data[i : i + split]) for i in range(0, len(data), split))
    else:
        image = _chunk(b"IDAT", data)
    return imaging.PNG_SIGNATURE + _chunk(b"IHDR", header) + body + image + _chunk(b"IEND", b"")


def _pattern(width: int, height: int) -> bytes:
    return bytes(b for y in range(height) for x in range(width) for b in pb.pattern_rgba(x, y))


@pytest.mark.parametrize("filters", [(0,), (1,), (2,), (3,), (4,), (0, 1, 2, 3, 4)])
def test_png_to_rgba_undoes_every_filter(filters: tuple[int, ...]) -> None:
    width, height = 9, 7
    rgba = _pattern(width, height)
    png = _make_png(width, height, rgba, filters=filters, split=17)
    assert imaging.png_to_rgba(png) == (width, height, rgba)


def test_png_to_rgba_reads_what_png_encode_writes() -> None:
    rgba = _pattern(5, 3)
    assert imaging.png_to_rgba(imaging.png_encode(5, 3, rgba)) == (5, 3, rgba)


def test_png_to_rgba_rgb_and_gray() -> None:
    rgb = bytes((10, 20, 30, 40, 50, 60))
    assert imaging.png_to_rgba(_make_png(2, 1, rgb, color_type=2, filters=(4,))) == (
        2, 1, bytes((10, 20, 30, 255, 40, 50, 60, 255))
    )
    gray = bytes((0, 128, 255))
    assert imaging.png_to_rgba(_make_png(3, 1, gray, color_type=0, filters=(1,))) == (
        3, 1, bytes((0, 0, 0, 255, 128, 128, 128, 255, 255, 255, 255, 255))
    )
    gray_alpha = bytes((7, 0, 9, 200))
    assert imaging.png_to_rgba(_make_png(2, 1, gray_alpha, color_type=4, filters=(3,))) == (
        2, 1, bytes((7, 7, 7, 0, 9, 9, 9, 200))
    )


def test_png_to_rgba_color_keys() -> None:
    rgb = bytes((1, 2, 3, 4, 5, 6))
    key = struct.pack(">HHH", 4, 5, 6)
    assert imaging.png_to_rgba(_make_png(2, 1, rgb, color_type=2, chunks=((b"tRNS", key),)))[2] == bytes(
        (1, 2, 3, 255, 4, 5, 6, 0)
    )
    gray = bytes((9, 8))
    assert imaging.png_to_rgba(_make_png(2, 1, gray, color_type=0, chunks=((b"tRNS", struct.pack(">H", 8)),)))[
        2
    ] == bytes((9, 9, 9, 255, 8, 8, 8, 0))


def test_png_to_rgba_palette_with_partial_transparency() -> None:
    palette = bytes((255, 0, 0, 0, 255, 0, 0, 0, 255))
    indexes = bytes((0, 1, 2, 1))
    png = _make_png(2, 2, indexes, color_type=3, chunks=((b"PLTE", palette), (b"tRNS", b"\x00\x80")))
    assert imaging.png_to_rgba(png) == (
        2, 2, bytes((255, 0, 0, 0, 0, 255, 0, 128, 0, 0, 255, 255, 0, 255, 0, 128))
    )


def test_png_to_rgba_palette_index_out_of_range() -> None:
    png = _make_png(2, 1, bytes((0, 2)), color_type=3, chunks=((b"PLTE", bytes(6)),))
    with pytest.raises(IntegrityFailed, match="palette index out of range"):
        imaging.png_to_rgba(png)
    no_palette = _make_png(1, 1, b"\x00", color_type=3)
    with pytest.raises(IntegrityFailed, match="palette index out of range"):
        imaging.png_to_rgba(no_palette)


def test_png_to_rgba_reads_16_bit_samples_to_8() -> None:
    samples = struct.pack(">8H", 0x1234, 0x5678, 0x9ABC, 0xFFFF, 0x0001, 0x0203, 0x0405, 0x8000)
    png = _make_png(2, 1, samples, depth=16, filters=(4,))
    assert imaging.png_to_rgba(png) == (2, 1, bytes((0x12, 0x56, 0x9A, 0xFF, 0x00, 0x02, 0x04, 0x80)))


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"color_type": 5}, "unsupported PNG format"),
        ({"depth": 4, "color_type": 0}, "unsupported PNG format"),
        ({"depth": 16, "color_type": 3}, "unsupported PNG format"),
        ({"depth": 16, "color_type": 2, "chunks": ((b"tRNS", bytes(6)),)}, "unsupported PNG format"),
        ({"interlace": 1}, "unsupported PNG format"),
        ({"idat": b"not zlib"}, "PNG image data is corrupt"),
        ({"idat": zlib.compress(bytes(5))}, "PNG image data holds 5 bytes, not 9"),
        ({"idat": zlib.compress(bytes(20))}, "PNG image data holds 10 bytes, not 9"),
        ({"idat": zlib.compress(b"\x05" + bytes(8))}, "PNG row 0 has the unknown filter type 5"),
    ],
)
def test_png_to_rgba_rejects(kwargs: dict[str, object], message: str) -> None:
    png = _make_png(2, 1, bytes(8), **kwargs)
    with pytest.raises(IntegrityFailed, match=message):
        imaging.png_to_rgba(png)


def test_png_to_rgba_rejects_compression_and_filter_methods() -> None:
    png = bytearray(_make_png(1, 1, bytes(4)))
    for offset in (26, 27):  # IHDR compression method, filter method
        broken = bytearray(png)
        broken[offset] = 1
        header = bytes(broken[12:29])
        broken[29:33] = struct.pack(">I", zlib.crc32(header) & 0xFFFFFFFF)
        with pytest.raises(IntegrityFailed, match="unsupported PNG format"):
            imaging.png_to_rgba(bytes(broken))


def test_png_to_rgba_validates_first() -> None:
    with pytest.raises(IntegrityFailed, match="not a PNG"):
        imaging.png_to_rgba(b"GIF89a" + bytes(30))


# --- downscale_rgba --------------------------------------------------------------


def test_area_taps_are_exact() -> None:
    assert imaging._area_taps(4, 2) == [((0, 2), (1, 2)), ((2, 2), (3, 2))]
    assert imaging._area_taps(3, 2) == [((0, 2), (1, 1)), ((1, 1), (2, 2))]
    assert imaging._area_taps(2, 2) == [((0, 2),), ((1, 2),)]
    for src, dst in ((256, 24), (256, 22), (48, 36), (512, 72), (7, 3)):
        taps = imaging._area_taps(src, dst)
        assert len(taps) == dst
        assert all(sum(weight for _index, weight in tap) == src for tap in taps)
        assert all(0 < weight <= dst for tap in taps for _index, weight in tap)


def test_downscale_averages_whole_blocks() -> None:
    # 2x2 -> 1x1: the mean of four opaque pixels.
    rgba = bytes((0, 0, 0, 255, 100, 100, 100, 255, 200, 200, 200, 255, 100, 40, 20, 255))
    assert imaging.downscale_rgba(2, 2, rgba, 1, 1) == bytes((100, 85, 80, 255))


def test_downscale_uses_premultiplied_alpha() -> None:
    # A transparent black pixel must not darken its opaque white neighbour.
    rgba = bytes((0, 0, 0, 0, 255, 255, 255, 255))
    assert imaging.downscale_rgba(2, 1, rgba, 1, 1) == bytes((255, 255, 255, 128))
    clear = bytes(8)
    assert imaging.downscale_rgba(2, 1, clear, 1, 1) == bytes(4)


def test_downscale_fractional_ratio_weights_partial_pixels() -> None:
    # 3 -> 2: each output pixel covers one whole and half of the middle source pixel.
    rgba = bytes((0, 0, 0, 255, 90, 90, 90, 255, 180, 180, 180, 255))
    assert imaging.downscale_rgba(3, 1, rgba, 2, 1) == bytes((30, 30, 30, 255, 150, 150, 150, 255))


def test_downscale_same_size_is_identity() -> None:
    rgba = _pattern(6, 4)  # every pixel has some alpha, so no color is lost to premultiplication
    assert imaging.downscale_rgba(6, 4, rgba, 6, 4) == rgba


def test_downscale_matches_a_brute_force_area_average() -> None:
    width, height, new_width, new_height = 11, 7, 4, 3
    rgba = _pattern(width, height)
    got = imaging.downscale_rgba(width, height, rgba, new_width, new_height)
    for oy in range(new_height):
        for ox in range(new_width):
            weights = {}
            for y in range(height):
                wy = max(0, min((y + 1) * new_height, (oy + 1) * height) - max(y * new_height, oy * height))
                for x in range(width):
                    wx = max(0, min((x + 1) * new_width, (ox + 1) * width) - max(x * new_width, ox * width))
                    if wx and wy:
                        weights[(x, y)] = wx * wy
            covered = sum(pb.pattern_rgba(x, y)[3] * w for (x, y), w in weights.items())
            expected = [
                (2 * sum(pb.pattern_rgba(x, y)[c] * pb.pattern_rgba(x, y)[3] * w for (x, y), w in weights.items())
                 + covered) // (2 * covered)
                for c in range(3)
            ]
            expected.append((2 * covered + width * height) // (2 * width * height))
            index = (oy * new_width + ox) * 4
            assert list(got[index : index + 4]) == expected


@pytest.mark.parametrize(
    ("width", "height", "length", "new_width", "new_height"),
    [(2, 2, 15, 1, 1), (2, 2, 16, 3, 1), (2, 2, 16, 1, 3), (2, 2, 16, 0, 1), (2, 2, 16, 1, 0)],
)
def test_downscale_rejects_bad_arguments(width: int, height: int, length: int, new_width: int, new_height: int) -> None:
    with pytest.raises(ValueError, match="cannot shrink"):
        imaging.downscale_rgba(width, height, bytes(length), new_width, new_height)
