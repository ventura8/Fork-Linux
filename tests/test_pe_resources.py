"""Tests for fork_linux.pe_resources: bounded PE header and resource parsing."""

from __future__ import annotations

import os
import shutil
import struct
import subprocess
import tracemalloc
from collections.abc import Callable
from pathlib import Path

import pytest
from fixtures import pe_builder as pb

from fork_linux import pe_resources
from fork_linux.errors import ExitCode, IntegrityFailed
from fork_linux.pe_resources import GroupIcon, IconEntry, IconImage, PEFile

LANG = pb.LANG_EN_US


def _icon_tree(icons: dict[int, bytes], entries: list[tuple[int, int, int, int, int, int, int]]) -> pb.Tree:
    return {
        pb.RT_ICON: {icon_id: {LANG: data} for icon_id, data in icons.items()},
        pb.RT_GROUP_ICON: {pb.APP_ICON_GROUP: {LANG: pb.make_group(entries)}},
    }


def _version_only(blob: bytes) -> PEFile:
    return PEFile(pb.build_pe({pb.RT_VERSION: {1: {LANG: blob}}}))


# --- headers -------------------------------------------------------------------


def test_pe32_plus_headers() -> None:
    pe = PEFile(pb.build_pe())
    assert pe.machine == pe_resources.MACHINE_AMD64
    assert pe.is_pe32_plus is True
    assert pe.subsystem == 2
    assert pe.characteristics == 0x0022
    assert [section.name for section in pe.sections] == [".text", ".rsrc"]
    assert pe.sections[1].virtual_address == pb.RSRC_RVA
    assert len(pe.data_directories) == 16


def test_pe32_headers_and_resources() -> None:
    pe = PEFile(pb.build_pe(pe32_plus=False, subsystem=3, characteristics=0x0102))
    assert pe.machine == pe_resources.MACHINE_I386
    assert pe.is_pe32_plus is False
    assert pe.subsystem == 3
    assert pe.characteristics == 0x0102
    assert [image.width for image in pe.best_icons()] == [16, 32, 256]


def test_accepts_bytearray_and_memoryview() -> None:
    data = pb.build_pe()
    assert PEFile(bytearray(data)).machine == PEFile(memoryview(data)).machine == pb.MACHINE_AMD64


def test_fewer_data_directories_means_no_resources() -> None:
    pe = PEFile(pb.build_pe(rva_and_sizes=2))
    assert len(pe.data_directories) == 2
    assert pe.group_icons() == []
    assert pe.version_info() == {}


def test_image_without_resource_section() -> None:
    pe = PEFile(pb.build_pe(with_resources=False))
    assert [section.name for section in pe.sections] == [".text"]
    assert pe.group_icons() == []
    assert pe.best_icons() == []
    assert pe.version_info() == {}


@pytest.mark.parametrize("name", sorted(pb.MALFORMED))
def test_malformed_images_raise_integrity_failed(name: str) -> None:
    data = pb.MALFORMED[name]()
    with pytest.raises(IntegrityFailed, match="malformed PE image") as caught:
        pe = PEFile(data)
        pe.group_icons()
        pe.best_icons()
        pe.version_info()
    assert caught.value.exit_code == ExitCode.INTEGRITY_FAILED
    assert "fork-linux setup" in caught.value.hint


@pytest.mark.parametrize(
    ("name", "message"),
    [
        ("bad-mz", "missing MZ signature"),
        ("bad-pe-signature", "missing PE signature"),
        ("mz-only", "e_lfanew"),
        ("bad-optional-magic", "unknown optional header magic 0x999"),
        ("optional-header-too-small", "optional header is truncated"),
        ("too-many-sections", "section table of 200 entries"),
        ("rsrc-directory-out-of-bounds", "resource directory at RVA 0x900000 is outside every section"),
        ("resource-loop", "contains a loop"),
        ("resource-too-deep", "nested deeper than 3 levels"),
        ("huge-count", "too many entries"),
        ("icon-rva-out-of-bounds", "outside every section"),
        ("subdir-out-of-bounds", "resource directory at offset"),
        ("truncated-resource-data", "runs past the end of the file"),
    ],
)
def test_malformed_messages(name: str, message: str) -> None:
    with pytest.raises(IntegrityFailed, match=message):
        pe = PEFile(pb.MALFORMED[name]())
        pe.best_icons()
        pe.version_info()


def test_declared_resource_size_past_the_section_is_tolerated() -> None:
    # Windows ignores DataDirectory[RESOURCE].Size; so do we (the walk stays inside .rsrc).
    built = pb.build_pe_ex()
    struct.pack_into("<I", built.data, built.data_dirs_offset + 8 * 2 + 4, 0x7FFFFFFF)
    assert [image.width for image in PEFile(bytes(built)).best_icons()] == [16, 32, 256]


def test_resource_directory_header_must_be_mapped() -> None:
    # The root directory starts 8 bytes before the end of .rsrc's raw data.
    built = pb.build_pe_ex()
    (raw_size,) = struct.unpack_from("<I", built.data, built.sections_offset + 40 + 16)
    struct.pack_into("<I", built.data, built.data_dirs_offset + 8 * 2, pb.RSRC_RVA + raw_size - 8)
    with pytest.raises(IntegrityFailed, match="resource directory at RVA .* is outside every section"):
        PEFile(bytes(built)).group_icons()


def test_headers_parse_even_when_resources_are_broken() -> None:
    pe = PEFile(pb.resource_loop())
    assert pe.machine == pb.MACHINE_AMD64
    with pytest.raises(IntegrityFailed, match="loop"):
        pe.group_icons()


# --- resource tree limits ---------------------------------------------------------


def test_total_entry_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pe_resources, "MAX_TOTAL_ENTRIES", 5)
    with pytest.raises(IntegrityFailed, match="too many entries"):
        PEFile(pb.build_pe()).group_icons()


def test_resource_byte_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pe_resources, "MAX_RESOURCE_BYTES", 1000)
    with pytest.raises(IntegrityFailed, match="resources exceed 1000 bytes"):
        PEFile(pb.build_pe()).group_icons()


def test_data_entry_above_language_level_is_ignored() -> None:
    def shallow(layout: pb.RsrcLayout) -> None:
        leaf = layout.data_entries[(pb.RT_VERSION, 1, LANG)]
        struct.pack_into("<I", layout.data, layout.entries[(pb.RT_VERSION,)] + 4, leaf)

    pe = PEFile(pb.build_pe(patch_rsrc=shallow))
    assert pe.version_info() == {}
    assert len(pe.best_icons()) == 3


def test_named_resources_and_languages() -> None:
    group = pb.make_group([(32, 32, 0, 1, 32, 0, 7)])
    other = pb.make_group([(16, 16, 0, 1, 32, 0, 7)])
    tree: pb.Tree = {
        pb.RT_ICON: {7: {LANG: pb.make_dib(32, 32, 32)}},
        # Languages are stored in ascending order and the first one wins.
        pb.RT_GROUP_ICON: {"MAINICON": {LANG: other, 0x407: group}, 2: {LANG: other}},
    }
    groups = PEFile(pb.build_pe(tree)).group_icons()
    assert [g.id_or_name for g in groups] == ["MAINICON", 2]
    assert groups[0] == GroupIcon("MAINICON", [IconEntry(32, 32, 0, 1, 32, 0, 7)])


def _named_group_pe(patch_entry: Callable[[pb.RsrcLayout, int], None]) -> bytes:
    tree: pb.Tree = {pb.RT_GROUP_ICON: {"APP": {LANG: pb.make_group([])}}}

    def patch(layout: pb.RsrcLayout) -> None:
        patch_entry(layout, layout.entries[(pb.RT_GROUP_ICON, "APP")])

    return pb.build_pe(tree, patch_rsrc=patch)


def test_resource_name_length_out_of_bounds() -> None:
    def huge_length(layout: pb.RsrcLayout, entry: int) -> None:
        (name_field,) = struct.unpack_from("<I", layout.data, entry)
        struct.pack_into("<H", layout.data, name_field & 0x7FFFFFFF, 0xFFFF)

    with pytest.raises(IntegrityFailed, match="resource name is out of bounds"):
        PEFile(_named_group_pe(huge_length)).group_icons()


def test_resource_name_offset_out_of_bounds() -> None:
    def far_name(layout: pb.RsrcLayout, entry: int) -> None:
        struct.pack_into("<I", layout.data, entry, 0x00100000 | pb.HIGH_BIT)

    with pytest.raises(IntegrityFailed, match="resource name at offset 1048576 is out of bounds"):
        PEFile(_named_group_pe(far_name)).group_icons()


# --- icons -------------------------------------------------------------------------


def test_group_icons_of_default_image() -> None:
    tree = pb.default_tree()
    groups = PEFile(pb.build_pe(tree)).group_icons()
    assert len(groups) == 1
    assert groups[0].id_or_name == pb.APP_ICON_GROUP
    sizes = [len(tree[pb.RT_ICON][i][LANG]) for i in (1, 2, 3)]
    assert groups[0].entries == [
        IconEntry(256, 256, 0, 1, 32, sizes[0], 1),
        IconEntry(32, 32, 0, 1, 32, sizes[1], 2),
        IconEntry(16, 16, 0, 1, 8, sizes[2], 3),
    ]


def test_icon_image_returns_exact_resource_bytes() -> None:
    tree = pb.default_tree()
    pe = PEFile(pb.build_pe(tree))
    for entry in pe.group_icons()[0].entries:
        assert pe.icon_image(entry) == tree[pb.RT_ICON][entry.icon_id][LANG]


def test_icon_image_missing_id() -> None:
    pe = PEFile(pb.build_pe())
    with pytest.raises(IntegrityFailed, match="icon 99 listed in the icon group is missing"):
        pe.icon_image(IconEntry(16, 16, 0, 1, 32, 0, 99))


def test_best_icons_sorted_with_png_flag() -> None:
    tree = pb.default_tree()
    images = PEFile(pb.build_pe(tree)).best_icons()
    assert [(i.width, i.height, i.is_png) for i in images] == [(16, 16, False), (32, 32, False), (256, 256, True)]
    assert images[2] == IconImage(256, 256, tree[pb.RT_ICON][1][LANG], True)


def test_best_icons_min_size_filter() -> None:
    pe = PEFile(pb.build_pe())
    assert [i.width for i in pe.best_icons(min_size=32)] == [32, 256]
    assert pe.best_icons(min_size=512) == []


def test_best_icons_prefers_highest_bit_depth() -> None:
    icons = {
        2: pb.make_dib(32, 32, 8),
        3: pb.make_dib(32, 32, 32),
        4: pb.make_dib(32, 32, 4),
        5: pb.make_dib(48, 16, 32),
    }
    entries = [
        (32, 32, 0, 1, 8, 0, 2),
        (32, 32, 0, 1, 32, 0, 3),
        (32, 32, 16, 1, 4, 0, 4),
        (48, 16, 0, 1, 32, 0, 5),
    ]
    images = PEFile(pb.build_pe(_icon_tree(icons, entries))).best_icons()
    # Non-square sizes are kept here; icon_extract decides what to install.
    assert [(i.width, i.height, i.data) for i in images] == [(32, 32, icons[3]), (48, 16, icons[5])]


def test_best_icons_reads_each_image_once() -> None:
    # A hostile group listing one image under every size from 16 to 255 must not
    # make a copy of that image per size.
    image = pb.make_dib(64, 64, 32)
    entries = [(n, n, 0, 1, 32, 0, 1) for n in range(16, 256)] + [(64, 32, 0, 1, 32, 0, 2)]
    images = PEFile(pb.build_pe(_icon_tree({1: image, 2: pb.make_dib(64, 32, 32)}, entries))).best_icons()
    assert len(images) == 241
    shared = [i.data for i in images if i.width == i.height]
    assert len(shared) == 240
    assert all(data is shared[0] for data in shared)
    assert shared[0] == image


def test_icon_image_uses_first_language_of_an_icon() -> None:
    first, second = pb.make_dib(16, 16, 32), pb.make_dib(16, 16, 8)
    tree = _icon_tree({}, [(16, 16, 0, 1, 32, 0, 5)])
    tree[pb.RT_ICON] = {5: {0x407: second, LANG: first}}
    pe = PEFile(pb.build_pe(tree))
    # Languages are stored in ascending id order: 0x407 comes first.
    assert pe.icon_image(IconEntry(16, 16, 0, 1, 32, 0, 5)) == second
    assert pe.best_icons()[0].data == second


def test_icon_group_entries_are_capped_across_groups(monkeypatch: pytest.MonkeyPatch) -> None:
    tree = pb.default_tree()  # the app group lists 3 images
    tree[pb.RT_GROUP_ICON][40000] = {LANG: pb.make_group([(48, 48, 0, 1, 32, 0, 2)] * 2)}
    pe = PEFile(pb.build_pe(tree))
    monkeypatch.setattr(pe_resources, "MAX_GROUP_ENTRIES", 4)
    with pytest.raises(IntegrityFailed, match="icon groups list more than 4 images"):
        pe.group_icons()
    # best_icons parses the first group only, so the other one cannot inflate it.
    assert [image.width for image in pe.best_icons()] == [16, 32, 256]
    monkeypatch.setattr(pe_resources, "MAX_GROUP_ENTRIES", 5)
    assert [len(group.entries) for group in pe.group_icons()] == [3, 2]


def test_best_icons_uses_only_first_group() -> None:
    tree = _icon_tree({1: pb.make_dib(16, 16, 32), 2: pb.make_dib(48, 48, 32)}, [(16, 16, 0, 1, 32, 0, 1)])
    tree[pb.RT_GROUP_ICON][40000] = {LANG: pb.make_group([(48, 48, 0, 1, 32, 0, 2)])}
    images = PEFile(pb.build_pe(tree)).best_icons()
    assert [i.width for i in images] == [16]


@pytest.mark.parametrize(
    ("group", "message"),
    [
        (pb.make_group([], reserved=1), "bad header"),
        (pb.make_group([], kind=2), "bad header"),
        (pb.make_group([(16, 16, 0, 1, 32, 0, 1)] * 2)[:-3], "icon group entry"),
        (b"\x00\x00", "icon group header"),
    ],
)
def test_bad_icon_groups(group: bytes, message: str) -> None:
    tree: pb.Tree = {pb.RT_GROUP_ICON: {1: {LANG: group}}}
    with pytest.raises(IntegrityFailed, match=message):
        PEFile(pb.build_pe(tree)).group_icons()


# --- version info ---------------------------------------------------------------------


def test_version_info_defaults() -> None:
    assert PEFile(pb.build_pe()).version_info() == {
        "FileVersion": "2.23.2.0",
        "ProductVersion": "2.23.2.7",
        "CompanyName": "Example Vendor",
        "FileDescription": "Synthetic test executable",
        "ProductName": "Synthetic",
    }


def test_version_info_without_fixed_info_uses_strings() -> None:
    info = _version_only(pb.make_version(fixed=False)).version_info()
    assert info["FileVersion"] == "9.9.9"
    assert "ProductVersion" not in info


def test_version_info_ignores_fixed_info_with_bad_signature() -> None:
    info = _version_only(pb.make_version(signature=0x12345678, strings={"ProductName": "X"})).version_info()
    assert info == {"ProductName": "X"}


def test_version_info_without_strings_and_with_trailing_padding() -> None:
    info = _version_only(pb.make_version((65535, 1, 2, 65535), strings={}, trailing_zero_padding=8)).version_info()
    assert info == {"FileVersion": "65535.1.2.65535", "ProductVersion": "2.23.2.7"}


def test_version_info_first_string_table_wins() -> None:
    def table(lang: str, product: str) -> bytes:
        value = (product + "\x00").encode("utf-16-le")
        return pb.version_block(lang, text=True, children=(pb.version_block("ProductName", value, text=True),))

    tables = (table("040904B0", "A"), table("040704B0", "B"))
    string_info = pb.version_block("StringFileInfo", text=True, children=tables)
    root = pb.version_block("VS_VERSION_INFO", children=(string_info,))
    assert _version_only(root).version_info() == {"ProductName": "A"}


def test_version_info_wrong_root_key() -> None:
    with pytest.raises(IntegrityFailed, match="unexpected key 'NOT_VERSION'"):
        _version_only(pb.make_version(root_key="NOT_VERSION")).version_info()


@pytest.mark.parametrize(
    ("blob", "message"),
    [
        (b"\x00\x00", "version block at offset 0"),
        (struct.pack("<HHH", 4, 0, 0) + bytes(10), "bad length"),
        (struct.pack("<HHH", 400, 0, 0) + bytes(10), "bad length"),
        (struct.pack("<HHH", 16, 0, 0) + "VS_VERSI".encode("ascii") + b"AB", "not terminated"),
    ],
)
def test_version_info_malformed_blocks(blob: bytes, message: str) -> None:
    with pytest.raises(IntegrityFailed, match=message):
        _version_only(blob).version_info()


def test_version_info_child_overflowing_parent() -> None:
    child = pb.version_block("StringFileInfo", text=True)
    root = bytearray(pb.version_block("VS_VERSION_INFO", children=(child,)))
    struct.pack_into("<H", root, len(root) - len(child), len(child) + 8)
    with pytest.raises(IntegrityFailed, match="bad length"):
        _version_only(bytes(root)).version_info()


# --- file helpers ------------------------------------------------------------------------


def test_from_path_and_helpers(tmp_path: Path) -> None:
    exe = tmp_path / "app.exe"
    exe.write_bytes(pb.build_pe())
    assert PEFile.from_path(exe).machine == pb.MACHINE_AMD64
    assert PEFile.from_path(str(exe)).is_pe32_plus
    assert pe_resources.pe_machine(exe) == pb.MACHINE_AMD64
    assert pe_resources.is_pe32_plus_x86_64(exe) is True


def test_from_path_size_cap(tmp_path: Path) -> None:
    exe = tmp_path / "big.exe"
    data = pb.build_pe()
    exe.write_bytes(data)
    assert PEFile.from_path(exe, max_size=len(data)).machine == pb.MACHINE_AMD64
    with pytest.raises(IntegrityFailed, match="larger than"):
        PEFile.from_path(exe, max_size=len(data) - 1)


@pytest.mark.parametrize("delta", [-100, 1])
def test_from_path_refuses_a_file_that_changed_after_fstat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, delta: int
) -> None:
    exe = tmp_path / "changing.exe"
    exe.write_bytes(pb.build_pe())
    real_fstat = os.fstat

    def stale_fstat(fd: int) -> os.stat_result:
        info = real_fstat(fd)
        # Pretend the file had another size when fstat() ran: it grew or shrank since.
        return os.stat_result(tuple(info[:6]) + (info.st_size - delta,) + tuple(info[7:10]))

    monkeypatch.setattr(pe_resources.os, "fstat", stale_fstat)
    try:
        with pytest.raises(IntegrityFailed, match="changed while it was being read"):
            PEFile.from_path(exe)
    finally:
        monkeypatch.undo()


def test_from_path_allocates_only_the_file_size(tmp_path: Path) -> None:
    exe = tmp_path / "app.exe"
    exe.write_bytes(pb.build_pe())
    tracemalloc.start()
    try:
        PEFile.from_path(exe)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    # The default cap is 512 MiB; reading must not reserve anywhere near that.
    assert peak < 16 * 1024 * 1024


def test_from_path_refuses_non_regular_files(tmp_path: Path) -> None:
    fifo = tmp_path / "Fork.exe"
    os.mkfifo(fifo)
    # Opened non-blocking: a FIFO with no writer must not hang the caller.
    with pytest.raises(IntegrityFailed, match="is not a regular file") as caught:
        PEFile.from_path(fifo)
    assert caught.value.exit_code == ExitCode.INTEGRITY_FAILED
    assert pe_resources.is_pe32_plus_x86_64(fifo) is False
    with pytest.raises(IntegrityFailed, match="is not a regular file"):
        PEFile.from_path(tmp_path)


def test_from_path_missing_file_raises_oserror(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        PEFile.from_path(tmp_path / "missing.exe")
    with pytest.raises(FileNotFoundError):
        pe_resources.is_pe32_plus_x86_64(tmp_path / "missing.exe")


def test_is_pe32_plus_x86_64_rejects_other_images(tmp_path: Path) -> None:
    cases = {
        "pe32.exe": pb.build_pe(pe32_plus=False),
        "arm64.exe": pb.build_pe(machine=0xAA64),
        "i386-plus.exe": pb.build_pe(machine=pb.MACHINE_I386),
        "text.exe": b"#!/bin/sh\necho not a PE\n",
    }
    for name, data in cases.items():
        (tmp_path / name).write_bytes(data)
        assert pe_resources.is_pe32_plus_x86_64(tmp_path / name) is False, name
    assert pe_resources.pe_machine(tmp_path / "arm64.exe") == 0xAA64


def test_pe_machine_raises_on_non_pe(tmp_path: Path) -> None:
    target = tmp_path / "plain.txt"
    target.write_text("hello")
    with pytest.raises(IntegrityFailed):
        pe_resources.pe_machine(target)


def test_fixture_is_accepted_by_binutils(tmp_path: Path) -> None:
    objdump = shutil.which("objdump")
    if objdump is None:
        pytest.skip("binutils objdump is not installed")
    exe = tmp_path / "app.exe"
    exe.write_bytes(pb.build_pe())
    out = subprocess.run([objdump, "-x", str(exe)], check=True, capture_output=True, text=True).stdout
    assert "pei-x86-64" in out
    assert "Resource Directory" in out
