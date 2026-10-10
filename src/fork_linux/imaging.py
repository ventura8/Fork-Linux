"""Pure-Python PNG writer and reader, Windows icon DIB decoder and area downscaler (stdlib only, no Pillow).

Icon images come out of Fork.exe, which is untrusted input: every header field
is range-checked, dimensions are capped and malformed data raises
:class:`IntegrityFailed` instead of producing a garbage image.
"""

from __future__ import annotations

import struct
import zlib
from itertools import accumulate

from .errors import IntegrityFailed

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
MAX_DIMENSION = 1024
BI_RGB = 0

_IHDR_PREFIX = b"\x00\x00\x00\x0dIHDR"
_SUPPORTED_BITS = (1, 4, 8, 24, 32)
# PNG color type -> samples per pixel (gray, RGB, palette, gray + alpha, RGBA).
_PNG_CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
_HINT = "The icon inside Fork.exe could not be decoded; the placeholder icon stays in place."


def _fail(what: str) -> IntegrityFailed:
    return IntegrityFailed(f"malformed icon image: {what}", hint=_HINT)


def _chunk(kind: bytes, payload: bytes) -> bytes:
    """Return one PNG chunk: length, type, payload and CRC-32 of type+payload."""
    crc = zlib.crc32(kind + payload) & 0xFFFFFFFF
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", crc)


def png_encode(width: int, height: int, rgba: bytes) -> bytes:
    """Encode 8-bit RGBA pixels (top-down rows) as a PNG with color type 6."""
    if width <= 0 or height <= 0 or len(rgba) != width * height * 4:
        raise ValueError(f"an RGBA buffer of {len(rgba)} bytes does not match {width}x{height}")
    stride = width * 4
    raw = b"".join(b"\x00" + rgba[row * stride : (row + 1) * stride] for row in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return PNG_SIGNATURE + _chunk(b"IHDR", header) + _chunk(b"IDAT", zlib.compress(raw, 9)) + _chunk(b"IEND", b"")


def png_size(png: bytes) -> tuple[int, int]:
    """Return ``(width, height)`` from a PNG's IHDR chunk."""
    if len(png) < 24 or not png.startswith(PNG_SIGNATURE) or png[8:16] != _IHDR_PREFIX:
        raise _fail("not a PNG image")
    width, height = struct.unpack_from(">II", png, 16)
    return width, height


def png_validate(png: bytes) -> tuple[int, int]:
    """Check a PNG's chunk structure, CRCs, image data and size cap; return ``(width, height)``."""
    width, height = png_size(png)
    if not (0 < width <= MAX_DIMENSION and 0 < height <= MAX_DIMENSION):
        raise _fail(f"PNG size {width}x{height} is outside 1..{MAX_DIMENSION}")
    offset = len(PNG_SIGNATURE)
    has_data = False
    while offset + 12 <= len(png):
        (length,) = struct.unpack_from(">I", png, offset)
        end = offset + 12 + length
        body = png[offset + 4 : end - 4]
        crc = struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
        if end > len(png) or png[end - 4 : end] != crc:
            raise _fail("PNG chunk is truncated or corrupt")
        if body[:4] == b"IEND":
            if not has_data:
                raise _fail("PNG image has no IDAT chunk")
            return width, height
        has_data = has_data or body[:4] == b"IDAT"
        offset = end
    raise _fail("PNG image has no IEND chunk")


def _mask_alpha(mask_row: bytes, width: int) -> bytes:
    """Turn one row of a 1-bpp AND mask (1 = transparent) into 8-bit alpha values."""
    return bytes(0 if (mask_row[x >> 3] >> (7 - (x & 7))) & 1 else 255 for x in range(width))


def _palette_length(bits: int, colors_used: int) -> int:
    """Number of RGBQUADs between the header and the pixels."""
    if bits > 8:
        return min(colors_used, 256)  # optional color table of a true-color DIB
    return min(colors_used or (1 << bits), 1 << bits)


def _dib_header(dib: bytes) -> tuple[int, int, int, int, int]:
    """Validate a BITMAPINFOHEADER; return (header size, width, height, bpp, colors used)."""
    if len(dib) < 40:
        raise _fail("DIB header is truncated")
    header_size, width, doubled, _, bits, compression, _, _, _, colors_used, _ = struct.unpack_from(
        "<IiiHHIIiiII", dib, 0
    )
    height = doubled // 2  # the XOR bitmap and the AND mask are stacked
    if header_size < 40 or not (0 < width <= MAX_DIMENSION and 0 < height <= MAX_DIMENSION):
        raise _fail(f"unsupported DIB header (size {header_size}, {width}x{doubled})")
    if compression != BI_RGB or bits not in _SUPPORTED_BITS:
        raise _fail(f"unsupported DIB format ({bits} bpp, compression {compression})")
    return header_size, width, height, bits, colors_used


def dib_size(dib: bytes) -> tuple[int, int]:
    """Return ``(width, height)`` of an icon DIB from its header, without decoding pixels."""
    _, width, height, _, _ = _dib_header(dib)
    return width, height


def image_size(data: bytes) -> tuple[int, int]:
    """Return ``(width, height)`` of an RT_ICON image, which is either a PNG or an icon DIB."""
    return png_size(data) if data.startswith(PNG_SIGNATURE) else dib_size(data)


def dib_to_rgba(dib: bytes) -> tuple[int, int, bytes]:
    """Decode an icon DIB (BITMAPINFOHEADER + XOR bitmap + AND mask) to top-down RGBA.

    Supports BI_RGB at 32, 24, 8, 4 and 1 bpp. A 32-bpp image whose alpha
    channel is entirely zero takes its alpha from the AND mask, like Windows.
    """
    header_size, width, height, bits, colors_used = _dib_header(dib)
    palette_off = header_size
    pixels_off = palette_off + 4 * _palette_length(bits, colors_used)
    stride = (width * bits + 31) // 32 * 4
    mask_stride = (width + 31) // 32 * 4
    mask_off = pixels_off + stride * height
    if mask_off > len(dib):
        raise _fail("DIB pixel data is truncated")
    pixels = dib[pixels_off:mask_off]
    # A missing AND mask means "nothing is transparent".
    mask = dib[mask_off : mask_off + mask_stride * height].ljust(mask_stride * height, b"\x00")
    masks = [mask[y * mask_stride : (y + 1) * mask_stride] for y in range(height)]
    rows = [pixels[y * stride : (y + 1) * stride] for y in range(height)]
    if bits == 32:
        out = _rows_32(rows, masks, width)
    elif bits == 24:
        out = _rows_24(rows, masks, width)
    else:
        palette = dib[palette_off:pixels_off].ljust(4 << bits, b"\x00")
        out = _rows_paletted(rows, masks, width, bits, palette)
    # DIB rows are stored bottom-up.
    return width, height, b"".join(reversed(out))


def _rows_32(rows: list[bytes], masks: list[bytes], width: int) -> list[bytes]:
    use_mask = not any(any(row[3::4]) for row in rows)
    out = []
    for row, mask_row in zip(rows, masks, strict=True):
        rgba = bytearray(width * 4)
        rgba[0::4] = row[2::4]
        rgba[1::4] = row[1::4]
        rgba[2::4] = row[0::4]
        rgba[3::4] = _mask_alpha(mask_row, width) if use_mask else row[3::4]
        out.append(bytes(rgba))
    return out


def _rows_24(rows: list[bytes], masks: list[bytes], width: int) -> list[bytes]:
    out = []
    for row, mask_row in zip(rows, masks, strict=True):
        bgr = row[: width * 3]
        rgba = bytearray(width * 4)
        rgba[0::4] = bgr[2::3]
        rgba[1::4] = bgr[1::3]
        rgba[2::4] = bgr[0::3]
        rgba[3::4] = _mask_alpha(mask_row, width)
        out.append(bytes(rgba))
    return out


def _rows_paletted(rows: list[bytes], masks: list[bytes], width: int, bits: int, palette: bytes) -> list[bytes]:
    colors = [bytes((palette[i + 2], palette[i + 1], palette[i])) for i in range(0, len(palette), 4)]
    index_mask = (1 << bits) - 1
    out = []
    for row, mask_row in zip(rows, masks, strict=True):
        alpha = _mask_alpha(mask_row, width)
        rgba = bytearray()
        for x in range(width):
            bit = x * bits
            index = (row[bit >> 3] >> (8 - bits - (bit & 7))) & index_mask
            rgba += colors[index]
            rgba.append(alpha[x])
        out.append(bytes(rgba))
    return out


def dib_to_png(dib: bytes) -> bytes:
    """Convert an icon DIB (as stored in an RT_ICON resource) to PNG bytes."""
    width, height, rgba = dib_to_rgba(dib)
    return png_encode(width, height, rgba)


# --- PNG reader -------------------------------------------------------------------


def _png_chunks(png: bytes) -> dict[bytes, bytes]:
    """``{type: payload}`` of a PNG that passed :func:`png_validate`; the IDAT payloads are joined."""
    chunks: dict[bytes, bytes] = {}
    offset = len(PNG_SIGNATURE)
    while offset + 12 <= len(png):
        (length,) = struct.unpack_from(">I", png, offset)
        kind = png[offset + 4 : offset + 8]
        payload = png[offset + 8 : offset + 8 + length]
        chunks[kind] = chunks.get(kind, b"") + payload if kind == b"IDAT" else chunks.get(kind, payload)
        offset += 12 + length
    return chunks


def _unfilter(raw: bytes, height: int, stride: int, bpp: int) -> bytes:
    """Undo the per-row PNG filters (None, Sub, Up, Average, Paeth) of non-interlaced image data."""
    out = bytearray()
    prev = bytes(stride)
    for y in range(height):
        start = y * (stride + 1)
        kind = raw[start]
        line = bytearray(raw[start + 1 : start + 1 + stride])
        if kind == 1:
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 0xFF
        elif kind == 2:
            line = bytearray((a + b) & 0xFF for a, b in zip(line, prev, strict=True))
        elif kind == 3:
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif kind == 4:
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                up = prev[i]
                upper_left = prev[i - bpp] if i >= bpp else 0
                guess = left + up - upper_left
                pa, pb, pc = abs(guess - left), abs(guess - up), abs(guess - upper_left)
                pred = left if pa <= pb and pa <= pc else up if pb <= pc else upper_left
                line[i] = (line[i] + pred) & 0xFF
        elif kind != 0:
            raise _fail(f"PNG row {y} has the unknown filter type {kind}")
        out += line
        prev = bytes(line)
    return bytes(out)


def _expand(samples: bytes, color_type: int, chunks: dict[bytes, bytes]) -> bytes:
    """8-bit samples of ``color_type`` as RGBA, applying a palette and ``tRNS`` transparency."""
    if color_type == 6:
        return samples
    if color_type == 3:
        palette = chunks.get(b"PLTE", b"")
        alpha = chunks.get(b"tRNS", b"")
        table = [
            palette[i * 3 : i * 3 + 3] + bytes((alpha[i] if i < len(alpha) else 255,)) for i in range(len(palette) // 3)
        ]
        if max(samples, default=0) >= len(table):
            raise _fail("PNG palette index out of range")
        return b"".join(table[index] for index in samples)
    gray_alpha = color_type == 4
    channels = 3 if color_type == 2 else 2 if gray_alpha else 1
    count = len(samples) // channels
    rgba = bytearray(count * 4)
    if color_type == 2:
        rgba[0::4], rgba[1::4], rgba[2::4] = samples[0::3], samples[1::3], samples[2::3]
    else:
        gray = samples[0::channels]
        rgba[0::4] = rgba[1::4] = rgba[2::4] = gray
    rgba[3::4] = samples[1::2] if gray_alpha else b"\xff" * count
    key = chunks.get(b"tRNS")
    if key is not None and not gray_alpha:
        # A color key: 16-bit samples per channel (only the low byte matters at 8 bits per sample).
        wanted = bytes(key[1::2][:channels])
        for index in range(count):
            if samples[index * channels : (index + 1) * channels] == wanted:
                rgba[index * 4 + 3] = 0
    return bytes(rgba)


def png_to_rgba(png: bytes) -> tuple[int, int, bytes]:
    """Decode a PNG to ``(width, height, top-down RGBA)``.

    Supports non-interlaced gray, RGB, palette, gray + alpha and RGBA images
    with 8 bits per sample (and 16, read to 8, without ``tRNS``) - what icon
    resources use. Anything else raises :class:`IntegrityFailed`.
    """
    width, height = png_validate(png)
    depth, color_type, compression, filtering, interlace = struct.unpack_from(">BBBBB", png, 24)
    channels = _PNG_CHANNELS.get(color_type)
    chunks = _png_chunks(png)
    wide = depth == 16 and color_type != 3 and b"tRNS" not in chunks
    if channels is None or not (depth == 8 or wide) or compression or filtering or interlace:
        raise _fail(f"unsupported PNG format (color type {color_type}, {depth} bits, interlace {interlace})")
    bpp = channels * depth // 8
    stride = width * bpp
    expected = height * (stride + 1)
    try:
        raw = zlib.decompressobj().decompress(chunks.get(b"IDAT", b""), expected + 1)
    except zlib.error as exc:
        raise _fail(f"PNG image data is corrupt ({exc})") from None
    if len(raw) != expected:
        raise _fail(f"PNG image data holds {len(raw)} bytes, not {expected}")
    samples = _unfilter(raw, height, stride, bpp)
    if depth == 16:
        samples = samples[0::2]  # the high byte of each big-endian sample
    return width, height, _expand(samples, color_type, chunks)


# --- area downscaling -------------------------------------------------------------


def _area_taps(src: int, dst: int) -> list[tuple[tuple[int, int], ...]]:
    """For each of ``dst`` output pixels, ``(source index, overlap)`` of an area (box) filter.

    Positions are counted in 1/``dst`` of a source pixel, so the overlaps are
    exact integers and those of one output pixel add up to ``src``.
    """
    taps = []
    for i in range(dst):
        start, end = i * src, (i + 1) * src
        taps.append(
            tuple((j, min((j + 1) * dst, end) - max(j * dst, start)) for j in range(start // dst, (end - 1) // dst + 1))
        )
    return taps


def _shrink_rows(rows: list[list[int]], taps: list[tuple[tuple[int, int], ...]]) -> list[list[int]]:
    """One output row per tap list: the rows it covers, weighted by their overlap."""
    out = []
    for tap in taps:
        (first, weight), *rest = tap
        acc = [value * weight for value in rows[first]]
        for index, weight in rest:
            acc = [total + value * weight for total, value in zip(acc, rows[index], strict=True)]
        out.append(acc)
    return out


def _shrink_row(row: list[int], ends: list[tuple[int, int, int, int]], inner: int) -> list[int]:
    """One row through the horizontal pass: partial first and last pixels, whole ones in between (prefix sums)."""
    prefix = [0, *accumulate(row)]
    return [
        row[first] * head + (row[last] * tail + (prefix[last] - prefix[first + 1]) * inner if last != first else 0)
        for first, head, last, tail in ends
    ]


def downscale_rgba(width: int, height: int, rgba: bytes, new_width: int, new_height: int) -> bytes:
    """Shrink top-down RGBA pixels with an area (box) filter on premultiplied alpha.

    Each output pixel is the exact area-weighted average of the source pixels
    it covers, at any ratio, so edges stay smooth and transparent pixels do not
    darken their neighbours. The arithmetic is integer, so the result is the
    same everywhere.
    """
    if len(rgba) != width * height * 4 or not (0 < new_width <= width and 0 < new_height <= height):
        raise ValueError(f"cannot shrink {width}x{height} ({len(rgba)} bytes) to {new_width}x{new_height}")
    alpha = rgba[3::4]
    planes = [[value * a for value, a in zip(rgba[channel::4], alpha, strict=True)] for channel in range(3)]
    planes.append(list(alpha))
    # A whole source pixel overlaps an output pixel by new_width units; only the two ends are partial.
    ends = [(*tap[0], *tap[-1]) for tap in _area_taps(width, new_width)]
    ys = _area_taps(height, new_height)
    sums = []
    for plane in planes:
        rows = [_shrink_row(plane[base : base + width], ends, new_width) for base in range(0, width * height, width)]
        sums.append([value for row in _shrink_rows(rows, ys) for value in row])
    # Rounded half up: color = sum(c * a * w) / sum(a * w) (at most 255), alpha = sum(a * w) / area.
    area = width * height
    coverage = sums[3]
    out = bytearray(new_width * new_height * 4)
    for channel in range(3):
        out[channel::4] = bytes(
            (2 * total + covered) // (2 * covered) if covered else 0
            for total, covered in zip(sums[channel], coverage, strict=True)
        )
    out[3::4] = bytes((2 * covered + area) // (2 * area) for covered in coverage)
    return bytes(out)
