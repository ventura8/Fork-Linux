"""Tests for the font_fallback setup step (WPF's composite font -> Linux fonts for non-Latin scripts)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fixtures.setup_ctx import FakeUI, FakeWine, composite_font_text, make_ctx, write_composite_font

from fork_linux import bootstrap, steps
from fork_linux.bootstrap import Ctx
from fork_linux.procrun import RecordingRunner
from fork_linux.steps import font_fallback as ff

FC_LIST = (
    "Noto Sans,Noto Sans Display\nDejaVu Sans\nNoto Sans CJK JP,Noto Sans CJK JP Regular\n"
    "Noto Sans CJK KR\nNoto Sans CJK SC\nNoto Sans CJK TC\nNoto Sans CJK HK\nNoto Sans Hebrew\n"
)
CJK = ("Noto Sans CJK JP", "Noto Sans CJK KR", "Noto Sans CJK SC", "Noto Sans CJK TC", "Noto Sans CJK HK")
BOM = b"\xef\xbb\xbf"


def _ctx(fc_list: str = FC_LIST) -> Ctx:
    ctx = make_ctx(RecordingRunner({"fc-list": fc_list}), ui=FakeUI())
    bootstrap.init_prefix_meta(ctx)
    return ctx


def _wpf(ctx: Ctx) -> Path:
    return ctx.paths.prefix / "drive_c" / "windows" / "Microsoft.NET" / "Framework64" / "v4.0.30319" / "WPF"


def _original(newline: str = "\r\n", bom: bool = True) -> bytes:
    return (BOM if bom else b"") + composite_font_text().replace("\n", newline).encode("utf-8")


def _warnings(ctx: Ctx) -> list[str]:
    ui = ctx.ui
    assert isinstance(ui, FakeUI)
    return ui.kinds("warn")


# -- the maps ------------------------------------------------------------------------------------


def test_family_maps_use_only_installed_fonts() -> None:
    maps = ff.family_maps({"Noto Sans", "Liberation Sans", "Noto Sans Thai Looped"})
    assert [(item.script, item.targets) for item in maps] == [
        ("Greek", ("Noto Sans", "Liberation Sans")),
        ("Cyrillic", ("Noto Sans", "Liberation Sans")),
        ("Hebrew", ("Liberation Sans",)),
        ("Thai", ("Noto Sans Thai Looped",)),
    ]
    assert ff.family_maps(set()) == []


def test_han_maps_follow_the_language_and_default_to_japanese() -> None:
    maps = [item for item in ff.family_maps(set(CJK)) if item.script == ff.HAN]
    firsts = {item.language: item.targets[0] for item in maps}
    assert firsts == {
        "zh-Hans": "Noto Sans CJK SC",
        "zh-HK": "Noto Sans CJK HK",
        "zh-Hant": "Noto Sans CJK TC",
        "ja": "Noto Sans CJK JP",
        "ko": "Noto Sans CJK KR",
        "": "Noto Sans CJK JP",
    }
    # zh-HK must come before its parent zh-Hant, and the generic map last.
    assert [item.language for item in maps] == ["zh-Hans", "zh-HK", "zh-Hant", "ja", "ko", ""]
    assert all(sorted(item.targets) == sorted(CJK) for item in maps)
    hangul = next(item for item in ff.family_maps(set(CJK)) if item.script == ff.HANGUL)
    assert hangul.targets[0] == "Noto Sans CJK KR"
    assert ff.han_first(ff.family_maps(set(CJK))) == "Noto Sans CJK JP"
    assert ff.han_first([]) is None


def test_script_table_is_safe_xml_and_well_formed() -> None:
    for script in ff.SCRIPTS:
        text = " ".join((script.name, script.unicode, script.language, *script.candidates))
        assert not set(text) & set("<>&\"'"), script
        assert script.candidates
        for part in script.unicode.split(", "):
            low, high = (int(value, 16) for value in part.split("-"))
            assert 0 <= low < high <= 0x10FFFF, part
    assert set(ff.KEY_SCRIPTS) <= {script.name for script in ff.SCRIPTS}


def test_render_and_script_summaries() -> None:
    plain = ff.FamilyMap("Greek", "0370-03FF", ("Noto Sans", "DejaVu Sans"))
    assert plain.render() == '<FontFamilyMap Unicode="0370-03FF" Target="Noto Sans, DejaVu Sans" />'
    japanese = ff.FamilyMap(ff.HAN, "3000-30FF", ("Noto Sans CJK JP",), "ja")
    assert japanese.render() == '<FontFamilyMap Unicode="3000-30FF" Language="ja" Target="Noto Sans CJK JP" />'
    maps = ff.family_maps({"DejaVu Sans", *CJK})
    assert ff.mapped_scripts(maps) == ["Greek", "Cyrillic", "Armenian", "Hebrew", "Arabic", "Georgian", "Hangul", "Han"]
    assert ff.unmapped_key_scripts(maps) == []
    assert ff.unmapped_key_scripts(ff.family_maps({"DejaVu Sans"})) == ["Hangul", "Han"]
    assert ff.render_block([], "  ", "\n") == ""


# -- finding the copies --------------------------------------------------------------------------


def test_locations_and_windows_paths() -> None:
    assert ff.COMPOSITE_FONT_LOCATIONS == ((), ("Fonts",))
    assert ff.WPF_DIR_PARTS == ("windows", "Microsoft.NET", "Framework64", "v4.0.30319", "WPF")
    assert ff.composite_font_wins() == [
        "C:\\windows\\Microsoft.NET\\Framework64\\v4.0.30319\\WPF\\GlobalUserInterface.CompositeFont",
        "C:\\windows\\Microsoft.NET\\Framework64\\v4.0.30319\\WPF\\Fonts\\GlobalUserInterface.CompositeFont",
    ]
    assert [ff.folder_name(sub) for sub in ff.COMPOSITE_FONT_LOCATIONS] == ["WPF\\", "WPF\\Fonts\\"]


def test_no_copies_without_the_framework(xdg: Path) -> None:
    ctx = _ctx()
    assert ff.wpf_dir(ctx) is None
    assert ff.composite_fonts(ctx) == []
    (ctx.paths.prefix / "drive_c" / "windows").mkdir(parents=True)
    assert ff.composite_fonts(ctx) == []


def test_copies_are_found_case_insensitively_in_both_folders(xdg: Path) -> None:
    ctx = _ctx()
    fonts_copy = write_composite_font(ctx.paths.prefix, "Fonts")
    assert ff.composite_fonts(ctx) == [("WPF\\Fonts\\", fonts_copy)]
    wpf_copy = write_composite_font(ctx.paths.prefix)
    assert ff.composite_fonts(ctx) == [("WPF\\", wpf_copy), ("WPF\\Fonts\\", fonts_copy)]
    upper = wpf_copy.with_name("GLOBALUSERINTERFACE.COMPOSITEFONT")
    wpf_copy.rename(upper)
    windows = ctx.paths.prefix / "drive_c" / "windows"
    windows.rename(windows.with_name("Windows"))
    found = ff.composite_fonts(ctx)
    assert [folder for folder, _path in found] == ["WPF\\", "WPF\\Fonts\\"]
    assert found[0][1].name == "GLOBALUSERINTERFACE.COMPOSITEFONT"
    assert found[0][1].parent.parent.parent.parent.parent.name == "Windows"


def test_symlinks_and_directories_are_never_copies(xdg: Path, tmp_path: Path) -> None:
    ctx = _ctx()
    wpf_copy = write_composite_font(ctx.paths.prefix)
    outside = tmp_path / "outside.CompositeFont"
    outside.write_bytes(_original())
    wpf_copy.unlink()
    wpf_copy.symlink_to(outside)
    (_wpf(ctx) / "Fonts" / ff.COMPOSITE_FONT_NAME).mkdir(parents=True)
    assert ff.composite_fonts(ctx) == []


def test_a_symlinked_wpf_folder_is_refused(xdg: Path, tmp_path: Path) -> None:
    ctx = _ctx()
    real = tmp_path / "real-wpf"
    real.mkdir()
    (real / ff.COMPOSITE_FONT_NAME).write_bytes(_original())
    _wpf(ctx).parent.mkdir(parents=True)
    _wpf(ctx).symlink_to(real, target_is_directory=True)
    assert ff.wpf_dir(ctx) is None
    assert ff.composite_fonts(ctx) == []
    ff.run(ctx)
    assert (real / ff.COMPOSITE_FONT_NAME).read_bytes() == _original()


def test_symlinked_drive_c_and_a_file_named_wpf(xdg: Path, tmp_path: Path) -> None:
    ctx = _ctx()
    parent = _wpf(ctx).parent
    parent.mkdir(parents=True)
    (parent / "WPF").write_bytes(b"not a folder")
    assert ff.wpf_dir(ctx) is None
    drive_c = ctx.paths.prefix / "drive_c"
    drive_c.rename(tmp_path / "drive_c")
    drive_c.symlink_to(tmp_path / "drive_c", target_is_directory=True)
    assert ff.wpf_dir(ctx) is None


# -- patching ------------------------------------------------------------------------------------


def test_dotnet472_only_copy_under_fonts_is_patched(xdg: Path) -> None:
    ctx = _ctx()
    path = write_composite_font(ctx.paths.prefix, "Fonts", mode=0o640)
    assert not ff.verify(ctx)
    ff.run(ctx)
    data = path.read_bytes()
    assert data.startswith(BOM)
    text = data[len(BOM):].decode("utf-8")
    assert text.count(ff.BEGIN) == 2  # one block per FamilyMaps section
    assert text.count(ff.END) == 2
    assert "\r\n" in text and "\n" not in text.replace("\r\n", "")
    assert 'Target="Noto Sans CJK JP, Noto Sans CJK SC, Noto Sans CJK TC, Noto Sans CJK HK, Noto Sans CJK KR"' in text
    assert (path.stat().st_mode & 0o777) == 0o640
    # The block comes first in each section, indented one level deeper than the FamilyMaps tag.
    lines = text.split("\r\n")
    tag = lines.index("    <FontFamily.FamilyMaps>")
    assert lines[tag + 1].startswith("        " + ff.BEGIN)
    assert ff.verify(ctx)
    assert not (_wpf(ctx) / ff.COMPOSITE_FONT_NAME).exists()
    # Stripping the block gives back the original file byte for byte.
    assert BOM + ff.strip_blocks(text, path).encode("utf-8") == _original()


def test_both_copies_are_patched_and_only_stale_ones_rewritten(xdg: Path) -> None:
    ctx = _ctx()
    wpf_copy = write_composite_font(ctx.paths.prefix)
    fonts_copy = write_composite_font(ctx.paths.prefix, "Fonts")
    ff.run(ctx)
    patched = wpf_copy.read_bytes()
    assert fonts_copy.read_bytes() == patched != _original()
    inode = wpf_copy.stat().st_ino
    fonts_copy.write_bytes(_original())
    assert not ff.verify(ctx)
    ff.run(ctx)
    assert wpf_copy.stat().st_ino == inode  # current: not rewritten
    assert fonts_copy.read_bytes() == patched
    assert ff.verify(ctx)
    before = fonts_copy.stat().st_ino
    ff.run(ctx)  # idempotent: nothing is stale
    assert fonts_copy.stat().st_ino == before


def test_without_bom_and_with_lf_line_endings(xdg: Path) -> None:
    ctx = _ctx()
    path = write_composite_font(ctx.paths.prefix, bom=False, newline="\n")
    ff.run(ctx)
    data = path.read_bytes()
    assert not data.startswith(BOM)
    assert b"\r" not in data
    assert ff.BEGIN.encode() in data
    assert ff.strip_blocks(data.decode(), path).encode() == _original("\n", bom=False)


def test_fonts_changing_rewrites_or_removes_the_block(xdg: Path) -> None:
    ctx = _ctx()
    path = write_composite_font(ctx.paths.prefix)
    ff.run(ctx)
    ctx.cache.clear()
    ctx.runner = RecordingRunner({"fc-list": "DejaVu Sans\n"})
    assert not ff.verify(ctx)
    ff.run(ctx)
    text = path.read_bytes().decode("utf-8-sig")
    assert "Noto" not in text
    assert 'Target="DejaVu Sans"' in text
    ctx.cache.clear()
    ctx.runner = RecordingRunner({"fc-list": ""})
    ff.run(ctx)
    assert path.read_bytes() == _original()
    assert ff.verify(ctx)


def test_inputs_follow_the_installed_fonts(xdg: Path) -> None:
    ctx = _ctx("DejaVu Sans\n")
    assert ff.inputs(ctx) == {
        "maps": [
            ["Greek", "", "0370-03FF, 1F00-1FFF", ["DejaVu Sans"]],
            ["Cyrillic", "", "0400-052F, 1C80-1C8F, 2DE0-2DFF, A640-A69F", ["DejaVu Sans"]],
            ["Armenian", "", "0530-058F, FB13-FB17", ["DejaVu Sans"]],
            ["Hebrew", "", "0590-05FF, FB1D-FB4F", ["DejaVu Sans"]],
            ["Arabic", "", "0600-06FF, 0750-077F, 0870-08FF, FB50-FDFF, FE70-FEFF", ["DejaVu Sans"]],
            ["Georgian", "", "10A0-10FF, 1C90-1CBF, 2D00-2D2F", ["DejaVu Sans"]],
        ]
    }


def test_missing_composite_font_only_warns(xdg: Path) -> None:
    ctx = _ctx()
    ff.run(ctx)
    warnings = _warnings(ctx)
    assert len(warnings) == 1
    assert "WPF's composite font is missing" in warnings[0]
    assert "WPF\\Fonts\\GlobalUserInterface.CompositeFont do not exist" in warnings[0]
    assert ff.verify(ctx)


def test_one_damaged_copy_means_no_writes(xdg: Path) -> None:
    ctx = _ctx()
    good = write_composite_font(ctx.paths.prefix)
    bad = write_composite_font(ctx.paths.prefix, "Fonts", text="<FontFamilyCollection>\n")
    ff.run(ctx)
    assert good.read_bytes() == _original()
    assert "is damaged (not well-formed XML" in _warnings(ctx)[0]
    assert str(bad) in _warnings(ctx)[0]
    assert ff.verify(ctx)


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        ('<!DOCTYPE x [<!ENTITY a "b">]>\n<x/>\n', "it declares a DTD"),
        (composite_font_text().replace("FamilyMaps>", "FamilyMaps >"), None),
        (composite_font_text().replace("    <FontFamily.FamilyMaps>\n", "    <FontFamily.FamilyMaps><!-- x -->\n", 1),
         "2 FamilyMaps sections, 1 found on lines of their own"),
        ('<FontFamilyCollection xmlns="x"/>\n', "0 FamilyMaps sections, 0 found on lines of their own"),
        (f"<a>\n{ff.BEGIN} -->\n<b/>\n</a>\n", "a fork-linux block is not closed"),
        (f"<a>\n{ff.BEGIN} -->\n{ff.BEGIN} -->\n{ff.END}\n</a>\n", "a fork-linux block is not closed"),
        (f"<a>\n{ff.END}\n</a>\n", "a fork-linux block end without its beginning"),
    ],
)
def test_damaged_files_are_refused(xdg: Path, text: str, problem: str | None) -> None:
    ctx = _ctx()
    path = write_composite_font(ctx.paths.prefix, text=text)
    before = path.read_bytes()
    if problem is None:
        # Whitespace inside the tag is still a FamilyMaps line: patched normally.
        ff.run(ctx)
        assert path.read_bytes().count(ff.BEGIN.encode()) == 2
        return
    with pytest.raises(ff.Unpatchable) as caught:
        ff.patched_composite_fonts(ctx)
    assert f"({problem})" in caught.value.message
    assert "setup --reset" in caught.value.hint
    ff.run(ctx)
    assert path.read_bytes() == before


def test_not_utf8_and_too_large(xdg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _ctx()
    path = write_composite_font(ctx.paths.prefix)
    path.write_bytes(BOM + b"<a>\xff</a>")
    with pytest.raises(ff.Unpatchable, match="not UTF-8"):
        ff.patched_composite_fonts(ctx)
    path.write_bytes(_original())
    monkeypatch.setattr(ff, "MAX_BYTES", 100)
    with pytest.raises(ff.Unpatchable, match="not a regular file of at most 100 bytes"):
        ff.patched_composite_fonts(ctx)


def test_reading_never_follows_a_symlink_or_reads_a_directory(xdg: Path, tmp_path: Path) -> None:
    target = tmp_path / "real.CompositeFont"
    target.write_bytes(_original())
    link = tmp_path / "link.CompositeFont"
    link.symlink_to(target)
    with pytest.raises(ff.Unpatchable, match="cannot open it"):
        ff.patch_file(link, [])
    with pytest.raises(ff.Unpatchable, match="not a regular file"):
        ff.patch_file(tmp_path, [])


def test_a_result_that_is_not_xml_is_never_written(xdg: Path) -> None:
    ctx = _ctx()
    path = write_composite_font(ctx.paths.prefix)
    broken = [ff.FamilyMap("Greek", "0370-03FF", ("A<B",))]
    with pytest.raises(ff.Unpatchable, match="not well-formed XML"):
        ff.patched_composite_fonts(ctx, broken)
    assert path.read_bytes() == _original()


# -- the step ------------------------------------------------------------------------------------


def test_step_order_and_revision(xdg: Path) -> None:
    assert ff.STEPS == (ff.FONT_FALLBACK,)
    assert ff.FONT_FALLBACK.rev == 2
    assert not ff.FONT_FALLBACK.needs_network
    ids = steps.STEP_IDS
    assert ids.index("font_fallback") == ids.index("font_replacements") + 1
    ctx = _ctx()
    write_composite_font(ctx.paths.prefix, "Fonts")
    bootstrap.run_step(ctx, ff.FONT_FALLBACK)
    assert bootstrap.is_done(ff.FONT_FALLBACK, ctx)


@pytest.mark.parametrize(
    ("verb", "folders"), [("dotnet48", ["WPF\\", "WPF\\Fonts\\"]), ("dotnet472", ["WPF\\Fonts\\"])]
)
def test_after_the_dotnet_fallback_the_fonts_copy_is_patched(xdg: Path, verb: str, folders: list[str]) -> None:
    ctx = _ctx()
    fake = FakeWine(ctx.paths.prefix)
    fake.winetricks(["winetricks", "-q", verb])
    ff.run(ctx)
    patches = ff.patched_composite_fonts(ctx)
    assert [patch.folder for patch in patches] == folders
    assert all(ff.BEGIN.encode() in patch.current and not patch.stale for patch in patches)
