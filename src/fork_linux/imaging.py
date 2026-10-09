"""Pure-Python PNG writer and Windows icon DIB decoder (stdlib only, no Pillow).

Icon images come out of Fork.exe, which is untrusted input: every header field
is range-checked, dimensions are capped and malformed data raises
:class:`IntegrityFailed` instead of producing a garbage image.
"""

from __future__ import annotations

import struct
import zlib

from .errors import IntegrityFailed

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
MAX_DIMENSION = 1024
BI_RGB = 0

_IHDR_PREFIX = b"\x00\x00\x00\x0dIHDR"
_SUPPORTED_BITS = (1, 4, 8, 24, 32)
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
