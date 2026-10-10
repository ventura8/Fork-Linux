"""Extract Fork's own icon from the user's Fork.exe into a hicolor icon tree.

The icon is Fork's artwork, so it is never committed or redistributed: it is
read at runtime from the Fork.exe the user installed and written to
``<out_dir>/<N>x<N>/apps/<name>.png`` for each square size the exe contains.
PNG images are passed through after a structural check; DIBs are converted.
Every other fixed size of the hicolor theme below the largest image is
downscaled from that image (area filter): GTK picks the scalable placeholder
SVG for any size without its own PNG (QA NUC-XFCE issue 7).
"""

from __future__ import annotations

import base64
import logging
from pathlib import Path

from . import fsutil
from .errors import IntegrityFailed
from .imaging import dib_to_png, downscale_rgba, image_size, png_encode, png_to_rgba, png_validate
from .pe_resources import IconImage, PEFile

log = logging.getLogger(__name__)

# An icon group stores 256 as 0, and that entry may hold a larger PNG (e.g. 512x512).
_LARGEST_LISTED = 256
# The fixed-size directories of the hicolor theme (its index.theme), smallest first.
HICOLOR_SIZES = (16, 22, 24, 32, 36, 48, 64, 72, 96, 128, 192, 256, 512)


def _check_name(name: str) -> None:
    if name in ("", ".", "..") or "/" in name or "\x00" in name:
        raise ValueError(f"invalid icon name: {name!r}")


def _atomic_write(path: Path, data: bytes) -> None:
    """Atomically write ``data`` to ``path`` (mode 0644); parents get the usual icon-theme modes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fsutil.atomic_write(path, data, mode=0o644)


def _real_size(icon: IconImage, min_size: int) -> int:
    """The edge of a square image whose header matches its group entry, else 0.

    Only the header is read, so a group that lists one huge image under
    hundreds of sizes is rejected before any pixel is decoded.
    """
    if icon.width != icon.height:
        return 0
    width, height = image_size(icon.data)
    if width != height or width < min_size:
        return 0
    if width == icon.width or (icon.width == _LARGEST_LISTED and width > _LARGEST_LISTED):
        return width
    return 0


def _scaled(pngs: dict[int, bytes], min_size: int) -> dict[int, bytes]:
    """PNGs for the hicolor sizes >= ``min_size`` the exe lacks, shrunk from its largest image.

    An image this module cannot decode leaves only the exe's own sizes (with a warning).
    """
    largest = max(pngs)
    missing = [size for size in HICOLOR_SIZES if min_size <= size < largest and size not in pngs]
    if not missing:
        return {}
    try:
        width, height, rgba = png_to_rgba(pngs[largest])
    except IntegrityFailed as exc:
        log.warning("not scaling Fork's %d px icon to %s px: %s", largest, ", ".join(map(str, missing)), exc)
        return {}
    return {size: png_encode(size, size, downscale_rgba(width, height, rgba, size, size)) for size in missing}


def render_icons(exe_path: Path, min_size: int = 16) -> dict[int, bytes]:
    """Return ``{N: png_bytes}`` for every square icon size >= ``min_size`` in the exe.

    Entries whose image is not square, or whose real size differs from the
    size the icon group announces, are skipped without being decoded. The
    hicolor sizes the exe lacks (below its largest image) are added, shrunk
    from the largest image.
    """
    pngs: dict[int, bytes] = {}
    for icon in PEFile.from_path(exe_path).best_icons(min_size):
        size = _real_size(icon, min_size)
        if size:
            png = icon.data if icon.is_png else dib_to_png(icon.data)
            png_validate(png)
            pngs[size] = png
    if pngs:
        pngs.update(_scaled(pngs, min_size))
    return pngs


def scalable_svg(png: bytes) -> bytes:
    """An SVG that shows ``png`` (Fork's largest icon) at any size, to shadow the scalable placeholder.

    GTK takes the scalable directory for every size without its own PNG, so a
    per-user copy of this file keeps Fork's icon at those sizes too.
    """
    width, height = png_validate(png)
    data = base64.b64encode(png).decode("ascii")
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
        f'width="{width}" height="{height}" viewBox="0 0 {width} {height}">\n'
        f'<image width="{width}" height="{height}" xlink:href="data:image/png;base64,{data}"/>\n'
        "</svg>\n"
    ).encode("ascii")


def extract_icons(exe_path: Path, out_dir: Path, name: str, *, min_size: int = 16) -> list[Path]:
    """Write ``out_dir/<N>x<N>/apps/<name>.png`` per icon size; return the paths, smallest first.

    Every image is decoded before anything is written, so a malformed exe
    (IntegrityFailed) leaves ``out_dir`` untouched.
    """
    _check_name(name)
    written = []
    for size, png in sorted(render_icons(Path(exe_path), min_size).items()):
        target = Path(out_dir) / f"{size}x{size}" / "apps" / f"{name}.png"
        _atomic_write(target, png)
        written.append(target)
    return written
