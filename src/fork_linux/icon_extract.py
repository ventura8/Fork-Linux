"""Extract Fork's own icon from the user's Fork.exe into a hicolor icon tree.

The icon is Fork's artwork, so it is never committed or redistributed: it is
read at runtime from the Fork.exe the user installed and written to
``<out_dir>/<N>x<N>/apps/<name>.png`` for each square size the exe contains.
PNG images are passed through after a structural check; DIBs are converted.
"""

from __future__ import annotations

from pathlib import Path

from . import fsutil
from .imaging import dib_to_png, image_size, png_validate
from .pe_resources import IconImage, PEFile

# An icon group stores 256 as 0, and that entry may hold a larger PNG (e.g. 512x512).
_LARGEST_LISTED = 256


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


def render_icons(exe_path: Path, min_size: int = 16) -> dict[int, bytes]:
    """Return ``{N: png_bytes}`` for every square icon size >= ``min_size`` in the exe.

    Entries whose image is not square, or whose real size differs from the
    size the icon group announces, are skipped without being decoded.
    """
    pngs: dict[int, bytes] = {}
    for icon in PEFile.from_path(exe_path).best_icons(min_size):
        size = _real_size(icon, min_size)
        if size:
            png = icon.data if icon.is_png else dib_to_png(icon.data)
            png_validate(png)
            pngs[size] = png
    return pngs


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
