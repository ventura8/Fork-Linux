"""Tests for the fonts, ui_font, font_replacements and display_dpi setup steps."""

from __future__ import annotations

import dataclasses
import struct
from pathlib import Path

import pytest
from fixtures.setup_ctx import FakeWine, fake_face, make_ctx, use_fake_ui_font

from fork_linux import bootstrap, font_names
from fork_linux.bootstrap import Ctx
from fork_linux.errors import DownloadFailed, ForkLinuxError, IntegrityFailed, SetupFailed
from fork_linux.procrun import Completed, RecordingRunner
from fork_linux.registry import RegBatch
from fork_linux.steps import display, fonts, runtime
from fork_linux.steps import prefix as prefix_steps

FC_LIST = "Noto Sans,Noto Sans Display\nDejaVu Sans Mono\nLiberation Sans\n"


def _ctx(extra: dict | None = None, **flags: object) -> tuple[Ctx, FakeWine]:
    ctx = make_ctx(**flags)
    fake = FakeWine(ctx.paths.prefix)
    ctx.runner = fake.runner(extra)
    ctx.cache[runtime._WINETRICKS_CACHE] = Path("/opt/winetricks")
    bootstrap.init_prefix_meta(ctx)
    return ctx, fake


# -- fonts ---------------------------------------------------------------------------------------


def test_fonts_installs_corefonts_once(xdg: Path) -> None:
    ctx, fake = _ctx()
    assert not fonts.verify_fonts(ctx)
    fonts.run_fonts(ctx)
    assert fake.verbs == ["corefonts"]
    assert fonts.verify_fonts(ctx)
    fonts.run_fonts(ctx)
    assert fake.verbs == ["corefonts"]


def test_fonts_are_found_case_insensitively(xdg: Path) -> None:
    ctx, _fake = _ctx()
    font_dir = ctx.paths.prefix / "drive_c" / "Windows" / "fonts"
    font_dir.mkdir(parents=True)
    (font_dir / "ARIAL.TTF").write_bytes(b"x")
    assert fonts.arial(ctx) == font_dir / "ARIAL.TTF"
    (font_dir / "ARIAL.TTF").unlink()
    (font_dir / "arial.ttf").mkdir()
    assert fonts.arial(ctx) is None


def test_fonts_missing_directory(xdg: Path) -> None:
    ctx, _fake = _ctx()
    (ctx.paths.prefix / "drive_c" / "windows").mkdir(parents=True)
    assert fonts.arial(ctx) is None


def test_fonts_failure(xdg: Path) -> None:
    ctx, fake = _ctx()
    fake.fail.add("corefonts")
    with pytest.raises(SetupFailed, match="corefonts") as caught:
        fonts.run_fonts(ctx)
    assert caught.value.step == "fonts"
    assert fake.verbs == ["corefonts"] * fonts.COREFONTS_ATTEMPTS


def test_fonts_retries_a_transient_failure(xdg: Path) -> None:
    # E2E: regedit sometimes fails to start under Wine staging halfway through corefonts.
    ctx, fake = _ctx()
    fake.fail_once.add("corefonts")
    fonts.run_fonts(ctx)
    assert fake.verbs == ["corefonts", "corefonts"]
    assert fonts.verify_fonts(ctx)


def test_fonts_do_not_retry_on_a_full_disk(xdg: Path) -> None:
    ctx, fake = _ctx(log_file=xdg / "setup.log")
    fake.disk_full.add("corefonts")
    with pytest.raises(prefix_steps.DiskFull):
        fonts.run_fonts(ctx)
    assert fake.verbs == ["corefonts"]


def test_fonts_partial_corefonts_is_not_done(xdg: Path) -> None:
    # E2E: an interrupted corefonts left arial.ttf behind; the step must not count as done.
    ctx, fake = _ctx()
    font_dir = ctx.paths.prefix / "drive_c" / "windows" / "Fonts"
    font_dir.mkdir(parents=True)
    (font_dir / "arial.ttf").write_bytes(b"x")
    (ctx.paths.prefix / "winetricks.log").write_text("andale\narial\n", encoding="utf-8")
    assert not fonts.verify_fonts(ctx)
    fonts.run_fonts(ctx)
    assert fake.verbs == ["corefonts"]
    assert fonts.verify_fonts(ctx)


# -- font_replacements ---------------------------------------------------------------------------


def test_parse_families() -> None:
    assert fonts.parse_families("A,B\n\n C \nD\\-E\n") == {"A", "B", "C", "D-E"}


UI_REPLACEMENTS = {
    "Segoe UI": "FL UI Sans",
    "Segoe UI Semibold": ["FL UI Sans Semibold", "FL UI Sans"],
    "Segoe UI Semilight": "FL UI Sans",
    "Segoe UI Light": ["FL UI Sans Light", "FL UI Sans"],
}


def test_replacements_use_the_interface_font_then_installed_fonts(xdg: Path) -> None:
    ctx, _fake = _ctx({"fc-list": FC_LIST})
    chosen = fonts.replacements(ctx)
    assert chosen == {
        **UI_REPLACEMENTS,
        # DejaVu Sans is preferred for symbols but missing here: Noto Sans is next.
        "Segoe UI Symbol": "Noto Sans",
        "Consolas": "DejaVu Sans Mono",
    }
    assert fonts.replacement_inputs(ctx) == {"replacements": chosen, "system_font": "Segoe UI", "height": -12}
    # fc-list runs once per context.
    fonts.replacements(ctx)
    assert [call["argv"] for call in ctx.runner.calls] == [["fc-list", ":", "family"]]


def test_symbols_prefer_dejavu_sans(xdg: Path) -> None:
    # Noto Sans has no U+2713 check mark; DejaVu Sans does.
    ctx, _fake = _ctx({"fc-list": "Noto Sans\nDejaVu Sans\nNoto Sans Mono\n"})
    chosen = fonts.replacements(ctx)
    assert chosen["Segoe UI Symbol"] == "DejaVu Sans"
    assert chosen["Consolas"] == "Noto Sans Mono"


@pytest.mark.parametrize(
    "response",
    [Completed([], 127, "", "not found"), "Some Other Font\n"],
)
def test_replacements_without_known_fonts(xdg: Path, response: object) -> None:
    ctx, _fake = _ctx({"fc-list": response})
    chosen = fonts.replacements(ctx)
    # Segoe UI never depends on fc-list: the interface font is installed into the prefix.
    assert chosen["Segoe UI"] == "FL UI Sans"
    assert chosen["Segoe UI Symbol"] == fonts.SYMBOL_FALLBACK
    assert chosen["Consolas"] == fonts.MONO_FALLBACK


def test_pick() -> None:
    assert fonts.pick({"B", "C"}, ("A", "B", "C"), "Z") == "B"
    assert fonts.pick(set(), ("A",), "Z") == "Z"


def test_segoe_ui_weights_without_a_matching_face(xdg: Path) -> None:
    ctx, _fake = _ctx()
    files = tuple(item for item in ctx.manifest.ui_font.files if item.weight in ("Regular", "Light"))
    font = dataclasses.replace(ctx.manifest.ui_font, files=files)
    assert fonts.segoe_ui_replacements(font) == {
        "Segoe UI": "FL UI Sans",
        "Segoe UI Semibold": "FL UI Sans",
        "Segoe UI Semilight": "FL UI Sans",
        "Segoe UI Light": ["FL UI Sans Light", "FL UI Sans"],
    }


def test_replacements_when_fc_list_cannot_run(xdg: Path) -> None:
    def broken(argv: list[str]) -> Completed:
        raise ForkLinuxError("timed out")

    ctx, _fake = _ctx({"fc-list": broken})
    assert fonts.installed_families(ctx) == set()


def test_logfont_layout() -> None:
    value = fonts.logfont("Segoe UI")
    assert len(value) == fonts.LOGFONT_SIZE == 92
    height, width, escapement, orientation, weight = struct.unpack_from("<5i", value)
    assert (height, width, escapement, orientation, weight) == (-12, 0, 0, 0, 400)
    # lfItalic, lfUnderline, lfStrikeOut, lfCharSet, lfOutPrecision, lfClipPrecision, lfQuality, lfPitchAndFamily
    assert value[20:28] == bytes((0, 0, 0, 1, 0, 0, 0, 0))
    assert value[28:].decode("utf-16-le").rstrip("\0") == "Segoe UI"
    assert fonts.logfont_face(value) == "Segoe UI"
    bold = fonts.logfont("Selawik", height=-16, weight=700)
    assert struct.unpack_from("<i", bold)[0] == -16
    assert struct.unpack_from("<i", bold, 16)[0] == 700


def test_logfont_cuts_long_face_names_and_keeps_a_terminator() -> None:
    value = fonts.logfont("x" * 40)
    assert len(value) == fonts.LOGFONT_SIZE
    assert fonts.logfont_face(value) == "x" * 31
    assert value[-2:] == b"\0\0"


@pytest.mark.parametrize("value", [None, "Segoe UI", 1, b"", bytes(91), bytes(93)])
def test_logfont_face_rejects_other_values(value: object) -> None:
    assert fonts.logfont_face(value) is None


def test_system_fonts_are_segoe_ui() -> None:
    values = fonts.system_fonts()
    assert sorted(values) == sorted(fonts.METRICS_FONTS)
    assert set(values.values()) == {fonts.logfont("Segoe UI")}


def test_font_replacements_are_imported_and_verified(xdg: Path) -> None:
    ctx, _fake = _ctx({"fc-list": FC_LIST})
    assert not fonts.verify_replacements(ctx)
    fonts.run_replacements(ctx)
    assert fonts.verify_replacements(ctx)
    assert fonts.wrong_system_fonts(ctx) == []
    reg = (ctx.paths.fork_linux_win_dir / "tmp" / "font-replacements.reg").read_bytes().decode("utf-16")
    assert "[HKEY_CURRENT_USER\\Software\\Wine\\Fonts\\Replacements]" in reg
    assert '"Segoe UI"="FL UI Sans"' in reg
    assert '"Segoe UI Semibold"=hex(7):' in reg
    assert "[HKEY_CURRENT_USER\\Control Panel\\Desktop\\WindowMetrics]" in reg
    assert '"MessageFont"=hex:f4,ff,ff,ff,' in reg
    assert prefix_steps.reg_value(ctx, fonts.REPLACEMENTS_KEY, "Segoe UI Light") == ["FL UI Sans Light", "FL UI Sans"]


def test_system_fonts_left_on_tahoma_are_not_verified(xdg: Path) -> None:
    ctx, _fake = _ctx({"fc-list": FC_LIST})
    fonts.run_replacements(ctx)
    tahoma = RegBatch().set_binary(fonts.METRICS_KEY, "MenuFont", fonts.logfont("Tahoma"))
    tahoma.set_sz(fonts.METRICS_KEY, "StatusFont", "Segoe UI")
    prefix_steps.import_batch(ctx, tahoma, "tahoma")
    assert fonts.wrong_system_fonts(ctx) == ["MenuFont", "StatusFont"]
    assert not fonts.verify_replacements(ctx)


def test_system_fonts_unreadable_registry(xdg: Path) -> None:
    ctx, _fake = _ctx()
    assert fonts.wrong_system_fonts(ctx) == list(fonts.METRICS_FONTS)


# -- ui_font -------------------------------------------------------------------------------------


def test_ui_font_inputs(xdg: Path) -> None:
    ctx, _fake = _ctx()
    font = ctx.manifest.ui_font
    assert fonts.ui_font_inputs(ctx) == {"family": "FL UI Sans", "version": "2.015",
                                         "sha256": [item.sha256 for item in font.files]}


def test_ui_font_registry_lists_every_face(xdg: Path) -> None:
    ctx, _fake = _ctx()
    assert fonts.ui_font_registry(ctx.manifest.ui_font) == [
        (fonts.SYSTEM_FONTS_KEY, "FL UI Sans (TrueType)", "flui.ttf"),
        (fonts.SYSTEM_FONTS_KEY, "FL UI Sans Bold (TrueType)", "fluib.ttf"),
        (fonts.SYSTEM_FONTS_KEY, "FL UI Sans Semibold (TrueType)", "fluisb.ttf"),
        (fonts.SYSTEM_FONTS_KEY, "FL UI Sans Light (TrueType)", "fluil.ttf"),
    ]


def _family(font: bytes) -> dict[int, str]:
    """Windows name records (ID -> text) of a TrueType file."""
    count = struct.unpack_from(">H", font, 4)[0]
    for index in range(count):
        tag, _sum, offset, _length = struct.unpack_from(">4sIII", font, 12 + 16 * index)
        if tag == b"name":
            _fmt, records, strings = struct.unpack_from(">HHH", font, offset)
            found = {}
            for row in range(records):
                platform, _enc, _lang, name_id, size, at = struct.unpack_from(">6H", font, offset + 6 + 12 * row)
                if platform == 3:
                    start = offset + strings + at
                    found[name_id] = font[start : start + size].decode("utf-16-be")
            return found
    return {}


def test_ui_font_is_installed_from_the_cache_renamed_and_registered(xdg: Path) -> None:
    ctx, _fake = _ctx(offline=True)
    font = use_fake_ui_font(ctx)
    assert not fonts.verify_ui_font(ctx)
    assert fonts.missing_faces(ctx) == [name for name, _face in font.faces]
    fonts.run_ui_font(ctx)
    assert fonts.verify_ui_font(ctx)
    folder = ctx.paths.prefix / "drive_c" / "windows" / "Fonts"
    assert sorted(path.name for path in folder.iterdir()) == sorted(name for name, _face in font.faces)
    assert _family((folder / "fluisb.ttf").read_bytes())[1] == "FL UI Sans Semibold"
    assert _family((folder / "fluisb.ttf").read_bytes())[16] == "FL UI Sans"
    assert _family((folder / "fluib.ttf").read_bytes())[2] == "Bold"
    assert all((folder / name).stat().st_mode & 0o777 == 0o644 for name, _face in font.faces)
    reg = (ctx.paths.fork_linux_win_dir / "tmp" / "ui-font.reg").read_bytes().decode("utf-16")
    assert "[HKEY_LOCAL_MACHINE\\Software\\Microsoft\\Windows NT\\CurrentVersion\\Fonts]" in reg
    assert '"FL UI Sans Semibold (TrueType)"="fluisb.ttf"' in reg


def test_ui_font_download_is_pinned(xdg: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    ctx, _fake = _ctx()
    face = tmp_path / "face.ttf"
    face.write_bytes(fake_face("x"))
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def fetch(*args: object, **kwargs: object) -> Path:
        calls.append((args, kwargs))
        return face

    monkeypatch.setattr(fonts.download, "fetch", fetch)
    fonts.run_ui_font(ctx)
    assert calls == [
        (
            (item.url, item.cache_name),
            {
                "cache_dir": ctx.paths.downloads_dir,
                "sha256": item.sha256,
                "size": item.size,
                "offline": False,
                "allowed_hosts": ("raw.githubusercontent.com",),
                "max_size": item.size,
            },
        )
        for item in ctx.manifest.ui_font.files
    ]


def test_ui_font_keeps_the_existing_fonts_folder_spelling(xdg: Path) -> None:
    ctx, _fake = _ctx(offline=True)
    use_fake_ui_font(ctx)
    folder = ctx.paths.prefix / "drive_c" / "Windows" / "fonts"
    folder.mkdir(parents=True)
    assert fonts.fonts_dir(ctx) == folder
    fonts.run_ui_font(ctx)
    assert (folder / "flui.ttf").is_file()
    assert not (ctx.paths.prefix / "drive_c" / "windows").exists()


def test_ui_font_faces_must_be_real_non_empty_files(xdg: Path) -> None:
    ctx, _fake = _ctx(offline=True)
    use_fake_ui_font(ctx)
    fonts.run_ui_font(ctx)
    folder = fonts.fonts_dir(ctx)
    (folder / "flui.ttf").write_bytes(b"")
    (folder / "fluib.ttf").unlink()
    (folder / "fluib.ttf").symlink_to(folder / "fluil.ttf")
    (folder / "fluil.ttf").unlink()
    (folder / "fluil.ttf").mkdir()
    assert fonts.missing_faces(ctx) == ["flui.ttf", "fluib.ttf", "fluil.ttf"]
    assert not fonts.verify_ui_font(ctx)


def test_ui_font_unregistered_is_not_verified(xdg: Path) -> None:
    ctx, fake = _ctx(offline=True)
    use_fake_ui_font(ctx)
    fake.fail.add("regedit")
    with pytest.raises(ForkLinuxError):
        fonts.run_ui_font(ctx)
    assert fonts.missing_faces(ctx) == []
    assert not fonts.verify_ui_font(ctx)


def test_ui_font_bad_downloads_raise_integrity_failed(xdg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx, _fake = _ctx(offline=True)
    use_fake_ui_font(ctx, {"fluib.ttf": b"not a font at all"})
    with pytest.raises(IntegrityFailed, match="not a TrueType font") as caught:
        fonts.run_ui_font(ctx)
    assert caught.value.hint == "delete the download cache and run setup again"
    use_fake_ui_font(ctx)
    monkeypatch.setattr(fonts, "MAX_FACE_BYTES", 4)
    with pytest.raises(IntegrityFailed, match="implausibly large"):
        fonts.run_ui_font(ctx)


def test_ui_font_unreadable_or_unwritable_fails_the_step(xdg: Path, tmp_path: Path) -> None:
    ctx, _fake = _ctx()
    font = ctx.manifest.ui_font
    item = font.files[0]
    source = tmp_path / "face.ttf"
    source.write_bytes(fake_face("x"))
    blocker = tmp_path / "file"
    blocker.write_bytes(b"")
    with pytest.raises(SetupFailed, match="cannot write") as caught:
        fonts.install_face(source, font, item, blocker / "Fonts")
    assert caught.value.step == "ui_font"
    with pytest.raises(SetupFailed, match="cannot read"):
        fonts.install_face(tmp_path / "missing.ttf", font, item, tmp_path)


def test_ui_font_offline_without_a_cached_face(xdg: Path) -> None:
    ctx, _fake = _ctx(offline=True)
    with pytest.raises(DownloadFailed, match="offline"):
        fonts.run_ui_font(ctx)


def test_installed_faces_render_with_the_new_names(tmp_path: Path) -> None:
    renamed = font_names.rename(fake_face("Noto Sans"), "FL UI Sans", "Bold")
    assert _family(renamed) == {1: "FL UI Sans", 2: "Bold", 4: "FL UI Sans Bold", 6: "FLUISans-Bold"}


def test_steps_order_and_revisions() -> None:
    assert fonts.STEPS == (fonts.FONTS, fonts.UI_FONT, fonts.FONT_REPLACEMENTS)
    assert fonts.UI_FONT.needs_network
    assert (fonts.UI_FONT.rev, fonts.FONT_REPLACEMENTS.rev) == (1, 2)
    assert not fonts.FONT_REPLACEMENTS.needs_network


# -- display_dpi ---------------------------------------------------------------------------------


def test_dpi_auto_follows_the_desktop(xdg: Path) -> None:
    ctx, _fake = _ctx(env={"GDK_SCALE": "2", "PATH": "/usr/bin"})
    assert display.dpi(ctx) == 192
    assert display.inputs(ctx) == {"dpi": 192}
    assert not display.verify(ctx)
    display.run(ctx)
    assert display.verify(ctx)


def test_dpi_from_config(xdg: Path) -> None:
    ctx, _fake = _ctx(env={"FORK_LINUX_DISPLAY_DPI": "144", "PATH": "/usr/bin"})
    assert display.dpi(ctx) == 144


def test_dpi_default_without_scaling(xdg: Path) -> None:
    ctx = make_ctx(RecordingRunner(), env={"PATH": "/usr/bin"})
    assert display.dpi(ctx) == 96
