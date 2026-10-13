"""Tests for fork_linux.icon_extract: Fork.exe icon -> hicolor PNG tree (+ scaled sizes, scalable SVG)."""

from __future__ import annotations

import base64
import os
import shutil
import stat
import subprocess
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path

import pytest
from fixtures import pe_builder as pb

from fork_linux import APP_ID, fsutil, icon_extract, imaging
from fork_linux.errors import IntegrityFailed

LANG = pb.LANG_EN_US


def _write_exe(tmp_path: Path, data: bytes | None = None) -> Path:
    exe = tmp_path / "Fork.exe"
    exe.write_bytes(pb.build_pe() if data is None else data)
    return exe


def _icon_exe(tmp_path: Path, icons: dict[int, bytes], entries: list[tuple[int, int, int, int, int, int, int]]) -> Path:
    tree: pb.Tree = {
        pb.RT_ICON: {icon_id: {LANG: data} for icon_id, data in icons.items()},
        pb.RT_GROUP_ICON: {1: {LANG: pb.make_group(entries)}},
    }
    return _write_exe(tmp_path, pb.build_pe(tree))


def _tree_files(root: Path) -> list[str]:
    return sorted(str(path.relative_to(root)) for path in root.rglob("*") if path.is_file())


# The default exe holds 16, 32 and 256 px images; every other hicolor size below 256 is scaled down.
DEFAULT_SIZES = (16, 22, 24, 32, 36, 48, 64, 72, 96, 128, 192, 256)


def _sizes(written: list[Path]) -> list[int]:
    return [int(path.parent.parent.name.split("x")[0]) for path in written]


def test_extracts_every_size_into_hicolor_layout(tmp_path: Path) -> None:
    exe = _write_exe(tmp_path)
    out = tmp_path / "icons" / "hicolor"
    written = icon_extract.extract_icons(exe, out, APP_ID)
    assert written == [out / f"{n}x{n}" / "apps" / f"{APP_ID}.png" for n in DEFAULT_SIZES]
    assert _tree_files(out) == sorted(f"{n}x{n}/apps/{APP_ID}.png" for n in DEFAULT_SIZES)
    for size, path in zip(DEFAULT_SIZES, written, strict=True):
        width, height, _ = pb.decode_png(path.read_bytes())
        assert (width, height) == (size, size)
        assert stat.S_IMODE(path.stat().st_mode) == 0o644


def test_png_entries_are_passed_through_and_dibs_converted(tmp_path: Path) -> None:
    png, dib32, dib8 = pb.default_images()
    extracted = icon_extract.extract_icons(_write_exe(tmp_path), tmp_path / "out", "app")
    written = dict(zip(DEFAULT_SIZES, extracted, strict=True))
    assert written[256].read_bytes() == png
    assert pb.decode_png(written[32].read_bytes()) == imaging.dib_to_rgba(dib32)
    assert pb.decode_png(written[16].read_bytes()) == imaging.dib_to_rgba(dib8)


def test_min_size_and_str_paths(tmp_path: Path) -> None:
    exe = _write_exe(tmp_path)
    written = icon_extract.extract_icons(str(exe), str(tmp_path / "out"), "app", min_size=32)
    assert _sizes(written) == [size for size in DEFAULT_SIZES if size >= 32]


def test_overwrites_previous_extraction_without_leftovers(tmp_path: Path) -> None:
    exe = _write_exe(tmp_path)
    out = tmp_path / "out"
    target = out / "32x32" / "apps" / "app.png"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"old")
    icon_extract.extract_icons(exe, out, "app")
    icon_extract.extract_icons(exe, out, "app")
    assert target.read_bytes().startswith(imaging.PNG_SIGNATURE)
    assert _tree_files(out) == sorted(f"{n}x{n}/apps/app.png" for n in DEFAULT_SIZES)


def test_exe_without_icons_writes_nothing(tmp_path: Path) -> None:
    exe = _write_exe(tmp_path, pb.build_pe({pb.RT_VERSION: {1: {LANG: pb.make_version()}}}))
    out = tmp_path / "out"
    assert icon_extract.extract_icons(exe, out, "app") == []
    assert not out.exists()


def _count_decodes(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Record the width of every DIB that icon_extract decodes."""
    calls: list[int] = []
    real = icon_extract.dib_to_png

    def counting(dib: bytes) -> bytes:
        calls.append(imaging.dib_size(dib)[0])
        return real(dib)

    monkeypatch.setattr(icon_extract, "dib_to_png", counting)
    return calls


def test_skips_non_square_small_and_mismatched_images(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    icons = {
        1: pb.make_dib(48, 24, 32),  # announced square, really 48x24
        2: pb.make_dib(16, 16, 32),  # announced as 64x64 but really 16x16 (< min_size)
        3: pb.make_dib(32, 32, 32),
        4: pb.make_dib(32, 32, 8),  # announced as 40x40, really 32x32
        5: pb.make_png(48, 48),  # announced as 128x128, really a 48x48 PNG
        6: pb.make_png(24, 24),  # announced as 96x96, really 24x24
        7: pb.make_dib(80, 40, 32),  # announced non-square: never even looked at
    }
    entries = [
        (48, 48, 0, 1, 32, 0, 1),
        (64, 64, 0, 1, 32, 0, 2),
        (32, 32, 0, 1, 32, 0, 3),
        (40, 40, 0, 1, 8, 0, 4),
        (128, 128, 0, 1, 32, 0, 5),
        (96, 96, 0, 1, 32, 0, 6),
        (80, 40, 0, 1, 32, 0, 7),
    ]
    decoded = _count_decodes(monkeypatch)
    exe = _icon_exe(tmp_path, icons, entries)
    written = icon_extract.extract_icons(exe, tmp_path / "out", "app", min_size=24)
    # 32 is the only usable image; 24 (>= min_size) is shrunk from it.
    assert _sizes(written) == [24, 32]
    assert pb.decode_png(written[1].read_bytes()) == imaging.dib_to_rgba(icons[3])
    assert decoded == [32]


def test_hostile_group_decodes_each_size_at_most_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # One 64x64 image listed under every square and many non-square sizes: only
    # the entry whose size matches the image is decoded.
    entries = [(n, n, 0, 1, 32, 0, 1) for n in range(1, 257)]
    entries += [(n, 64, 0, 1, 32, 0, 1) for n in range(1, 256) if n != 64]
    exe = _icon_exe(tmp_path, {1: pb.make_dib(64, 64, 8)}, entries)
    decoded = _count_decodes(monkeypatch)
    written = icon_extract.extract_icons(exe, tmp_path / "out", "app")
    assert _sizes(written) == [16, 22, 24, 32, 36, 48, 64]
    assert decoded == [64]


def test_256_entry_may_hold_a_larger_png(tmp_path: Path) -> None:
    big = pb.make_png(512, 512, lambda x, y: (x % 256, y % 256, 0, 255))
    exe = _icon_exe(tmp_path, {1: pb.make_dib(16, 16, 32), 2: big}, [(16, 16, 0, 1, 32, 0, 1), (0, 0, 0, 1, 32, 0, 2)])
    written = icon_extract.extract_icons(exe, tmp_path / "out", "app", min_size=128)
    assert _sizes(written) == [128, 192, 256, 512]
    assert written[-1].read_bytes() == big
    assert imaging.png_validate(written[2].read_bytes()) == (256, 256)


def test_256_entry_smaller_than_256_is_skipped(tmp_path: Path) -> None:
    exe = _icon_exe(tmp_path, {1: pb.make_png(128, 128)}, [(256, 256, 0, 1, 32, 0, 1)])
    assert icon_extract.extract_icons(exe, tmp_path / "out", "app") == []
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("name", ["", ".", "..", "a/b", "../app", "a\x00b"])
def test_rejects_unsafe_names(tmp_path: Path, name: str) -> None:
    exe = _write_exe(tmp_path)
    with pytest.raises(ValueError, match="invalid icon name"):
        icon_extract.extract_icons(exe, tmp_path / "out", name)
    assert not (tmp_path / "out").exists()


def test_malformed_exe_leaves_output_untouched(tmp_path: Path) -> None:
    bad_png = bytearray(pb.make_png(48, 48))
    bad_png[-5] ^= 0xFF  # corrupt the IEND CRC
    icons = {1: pb.make_dib(16, 16, 32), 2: bytes(bad_png)}
    exe = _icon_exe(tmp_path, icons, [(16, 16, 0, 1, 32, 0, 1), (48, 48, 0, 1, 32, 0, 2)])
    out = tmp_path / "out"
    with pytest.raises(IntegrityFailed, match="truncated or corrupt"):
        icon_extract.extract_icons(exe, out, "app")
    assert not out.exists()


def test_not_a_pe_file(tmp_path: Path) -> None:
    exe = _write_exe(tmp_path, b"not a PE file")
    with pytest.raises(IntegrityFailed, match="missing MZ signature"):
        icon_extract.extract_icons(exe, tmp_path / "out", "app")


def test_missing_exe_raises_oserror(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        icon_extract.extract_icons(tmp_path / "missing.exe", tmp_path / "out", "app")


def test_render_icons_returns_png_bytes_by_size(tmp_path: Path) -> None:
    pngs = icon_extract.render_icons(_write_exe(tmp_path))
    assert sorted(pngs) == list(DEFAULT_SIZES)
    assert all(imaging.png_validate(png) == (size, size) for size, png in pngs.items())


def test_scaled_sizes_are_shrunk_from_the_largest_image(tmp_path: Path) -> None:
    # A 64 px image of four solid quadrants: a 32 px copy has the same quadrants.
    def quadrants(x: int, y: int) -> tuple[int, int, int, int]:
        return ((255, 0, 0, 255), (0, 255, 0, 255), (0, 0, 255, 255), (0, 0, 0, 0))[(y >= 32) * 2 + (x >= 32)]

    exe = _icon_exe(tmp_path, {1: pb.make_png(64, 64, quadrants)}, [(64, 64, 0, 1, 32, 0, 1)])
    pngs = icon_extract.render_icons(exe, min_size=32)
    assert sorted(pngs) == [32, 36, 48, 64]
    width, height, rgba = pb.decode_png(pngs[32])
    assert (width, height) == (32, 32)
    assert rgba[:4] == bytes((255, 0, 0, 255))
    assert rgba[31 * 4 : 32 * 4] == bytes((0, 255, 0, 255))
    assert rgba[-4:] == bytes(4)


def test_nothing_to_scale_below_the_smallest_hicolor_size(tmp_path: Path) -> None:
    exe = _icon_exe(tmp_path, {1: pb.make_dib(16, 16, 32)}, [(16, 16, 0, 1, 32, 0, 1)])
    assert sorted(icon_extract.render_icons(exe)) == [16]


def test_an_undecodable_largest_png_keeps_the_exes_own_sizes(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # Valid structure, but interlaced: png_validate accepts it, png_to_rgba does not.
    png = bytearray(pb.make_png(48, 48))
    png[28] = 1
    png[29:33] = zlib.crc32(bytes(png[12:29])).to_bytes(4, "big")
    entries = [(16, 16, 0, 1, 32, 0, 1), (48, 48, 0, 1, 32, 0, 2)]
    exe = _icon_exe(tmp_path, {1: pb.make_dib(16, 16, 32), 2: bytes(png)}, entries)
    with caplog.at_level("WARNING", logger="fork_linux.icon_extract"):
        assert sorted(icon_extract.render_icons(exe)) == [16, 48]
    assert "not scaling Fork's 48 px icon to 22, 24, 32, 36 px" in caplog.text


def test_scalable_svg_embeds_the_png(tmp_path: Path) -> None:
    png = pb.make_png(24, 16)
    svg = icon_extract.scalable_svg(png)
    root = ET.fromstring(svg)
    assert root.tag == "{http://www.w3.org/2000/svg}svg"
    assert (root.get("width"), root.get("height"), root.get("viewBox")) == ("24", "16", "0 0 24 16")
    image = root.find("{http://www.w3.org/2000/svg}image")
    assert image is not None
    href = image.get("{http://www.w3.org/1999/xlink}href")
    assert href == "data:image/png;base64," + base64.b64encode(png).decode("ascii")
    with pytest.raises(IntegrityFailed):
        icon_extract.scalable_svg(b"not a png")


def test_hicolor_sizes_are_the_theme_directories() -> None:
    assert icon_extract.HICOLOR_SIZES == tuple(sorted(icon_extract.HICOLOR_SIZES))
    assert {16, 22, 24, 32, 48, 64, 128, 256, 512} <= set(icon_extract.HICOLOR_SIZES)


def test_atomic_write_cleans_up_on_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken_replace(src: object, dst: object) -> None:
        raise OSError("disk on fire")

    monkeypatch.setattr(fsutil.os, "replace", broken_replace)
    out = tmp_path / "out"
    exe = _write_exe(tmp_path)
    with pytest.raises(OSError, match="disk on fire"):
        icon_extract.extract_icons(exe, out, "app")
    assert _tree_files(out) == []


def test_atomic_write_creates_parents_and_replaces(tmp_path: Path) -> None:
    target = tmp_path / "a" / "b" / "file.png"
    icon_extract._atomic_write(target, b"one")
    icon_extract._atomic_write(target, b"two")
    assert target.read_bytes() == b"two"
    assert os.listdir(target.parent) == ["file.png"]


def test_imagemagick_identifies_extracted_icons(tmp_path: Path) -> None:
    identify = shutil.which("identify")
    if identify is None:
        pytest.skip("ImageMagick is not installed")
    written = icon_extract.extract_icons(_write_exe(tmp_path), tmp_path / "out", "app")
    for path in written:
        size = path.parent.parent.name.split("x")[0]
        result = subprocess.run(
            [identify, "-format", "%m %w %h", str(path)], check=True, capture_output=True, text=True
        )
        assert result.stdout == f"PNG {size} {size}"
