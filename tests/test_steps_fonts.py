"""Tests for the fonts, ui_font, font_replacements and display_dpi setup steps."""

from __future__ import annotations

import dataclasses
import io
import struct
import zipfile
from pathlib import Path

import pytest
from fixtures.setup_ctx import FakeWine, make_ctx, ui_font_archive, use_fake_ui_font

from fork_linux import bootstrap
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


SELAWIK_REPLACEMENTS = {
    "Segoe UI": "Selawik",
    "Segoe UI Semibold": ["Selawik Semibold", "Selawik"],
    "Segoe UI Semilight": ["Selawik Semilight", "Selawik"],
    "Segoe UI Light": ["Selawik Light", "Selawik"],
}


def test_replacements_use_selawik_then_installed_fonts(xdg: Path) -> None:
    ctx, _fake = _ctx({"fc-list": FC_LIST})
    chosen = fonts.replacements(ctx)
    assert chosen == {
        **SELAWIK_REPLACEMENTS,
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
    # Segoe UI never depends on fc-list: Selawik is installed into the prefix.
    assert chosen["Segoe UI"] == "Selawik"
    assert chosen["Segoe UI Symbol"] == fonts.SYMBOL_FALLBACK
    assert chosen["Consolas"] == fonts.MONO_FALLBACK


def test_pick() -> None:
    assert fonts.pick({"B", "C"}, ("A", "B", "C"), "Z") == "B"
    assert fonts.pick(set(), ("A",), "Z") == "Z"


def test_segoe_ui_weights_without_a_matching_face(xdg: Path) -> None:
    ctx, _fake = _ctx()
    faces = (("selawk.ttf", "Selawik"), ("selawkl.ttf", "Selawik Light"))
    font = dataclasses.replace(ctx.manifest.ui_font, faces=faces)
    assert fonts.segoe_ui_replacements(font) == {
        "Segoe UI": "Selawik",
        "Segoe UI Semibold": "Selawik",
        "Segoe UI Semilight": "Selawik",
        "Segoe UI Light": ["Selawik Light", "Selawik"],
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
    assert '"Segoe UI"="Selawik"' in reg
    assert '"Segoe UI Semibold"=hex(7):' in reg
    assert "[HKEY_CURRENT_USER\\Control Panel\\Desktop\\WindowMetrics]" in reg
    assert '"MessageFont"=hex:f4,ff,ff,ff,' in reg
    assert prefix_steps.reg_value(ctx, fonts.REPLACEMENTS_KEY, "Segoe UI Light") == ["Selawik Light", "Selawik"]


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
    assert fonts.ui_font_inputs(ctx) == {"family": "Selawik", "version": "1.01", "sha256": font.sha256}


def test_ui_font_registry_lists_every_face(xdg: Path) -> None:
    ctx, _fake = _ctx()
    assert fonts.ui_font_registry(ctx.manifest.ui_font) == [
        (fonts.SYSTEM_FONTS_KEY, "Selawik (TrueType)", "selawk.ttf"),
        (fonts.SYSTEM_FONTS_KEY, "Selawik Bold (TrueType)", "selawkb.ttf"),
        (fonts.SYSTEM_FONTS_KEY, "Selawik Light (TrueType)", "selawkl.ttf"),
        (fonts.SYSTEM_FONTS_KEY, "Selawik Semibold (TrueType)", "selawksb.ttf"),
        (fonts.SYSTEM_FONTS_KEY, "Selawik Semilight (TrueType)", "selawksl.ttf"),
    ]


def test_ui_font_is_installed_from_the_cache_and_registered(xdg: Path) -> None:
    ctx, _fake = _ctx(offline=True)
    font = use_fake_ui_font(ctx)
    assert not fonts.verify_ui_font(ctx)
    assert fonts.missing_faces(ctx) == [name for name, _face in font.faces]
    fonts.run_ui_font(ctx)
    assert fonts.verify_ui_font(ctx)
    folder = ctx.paths.prefix / "drive_c" / "windows" / "Fonts"
    assert sorted(path.name for path in folder.iterdir()) == sorted(name for name, _face in font.faces)
    assert (folder / "selawkb.ttf").read_bytes() == b"\x00\x01\x00\x00Selawik Bold"
    assert all((folder / name).stat().st_mode & 0o777 == 0o644 for name, _face in font.faces)
    reg = (ctx.paths.fork_linux_win_dir / "tmp" / "ui-font.reg").read_bytes().decode("utf-16")
    assert "[HKEY_LOCAL_MACHINE\\Software\\Microsoft\\Windows NT\\CurrentVersion\\Fonts]" in reg
    assert '"Selawik Semibold (TrueType)"="selawksb.ttf"' in reg


def test_ui_font_download_is_pinned(xdg: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    ctx, _fake = _ctx()
    archive = tmp_path / "font.zip"
    archive.write_bytes(ui_font_archive(ctx.manifest.ui_font))
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def fetch(*args: object, **kwargs: object) -> Path:
        calls.append((args, kwargs))
        return archive

    monkeypatch.setattr(fonts.download, "fetch", fetch)
    fonts.run_ui_font(ctx)
    font = ctx.manifest.ui_font
    assert calls == [
        (
            (font.url, "selawik-1.01.zip"),
            {
                "cache_dir": ctx.paths.downloads_dir,
                "sha256": font.sha256,
                "size": font.size,
                "offline": False,
                "allowed_hosts": ("github.com",),
                "max_size": font.size,
            },
        )
    ]


def test_ui_font_keeps_the_existing_fonts_folder_spelling(xdg: Path) -> None:
    ctx, _fake = _ctx(offline=True)
    use_fake_ui_font(ctx)
    folder = ctx.paths.prefix / "drive_c" / "Windows" / "fonts"
    folder.mkdir(parents=True)
    assert fonts.fonts_dir(ctx) == folder
    fonts.run_ui_font(ctx)
    assert (folder / "selawk.ttf").is_file()
    assert not (ctx.paths.prefix / "drive_c" / "windows").exists()


def test_ui_font_faces_must_be_real_non_empty_files(xdg: Path) -> None:
    ctx, _fake = _ctx(offline=True)
    use_fake_ui_font(ctx)
    fonts.run_ui_font(ctx)
    folder = fonts.fonts_dir(ctx)
    (folder / "selawk.ttf").write_bytes(b"")
    (folder / "selawkb.ttf").unlink()
    (folder / "selawkb.ttf").symlink_to(folder / "selawkl.ttf")
    (folder / "selawkl.ttf").unlink()
    (folder / "selawkl.ttf").mkdir()
    assert fonts.missing_faces(ctx) == ["selawk.ttf", "selawkb.ttf", "selawkl.ttf"]
    assert not fonts.verify_ui_font(ctx)


def test_ui_font_unregistered_is_not_verified(xdg: Path) -> None:
    ctx, fake = _ctx(offline=True)
    use_fake_ui_font(ctx)
    fake.fail.add("regedit")
    with pytest.raises(ForkLinuxError):
        fonts.run_ui_font(ctx)
    assert fonts.missing_faces(ctx) == []
    assert not fonts.verify_ui_font(ctx)


def test_ui_font_extracts_only_the_listed_members(xdg: Path, tmp_path: Path) -> None:
    ctx, _fake = _ctx()
    font = ctx.manifest.ui_font
    archive = tmp_path / "font.zip"
    archive.write_bytes(ui_font_archive(font, {"../evil.ttf": b"x", "/abs.ttf": b"x", "readme.txt": b"x"}))
    dest = tmp_path / "Fonts"
    written = fonts.extract_faces(archive, font, dest)
    assert written == [dest / name for name, _face in font.faces]
    assert sorted(path.name for path in dest.iterdir()) == sorted(name for name, _face in font.faces)
    assert not (tmp_path / "evil.ttf").exists()


def _zip(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        for name, data in members.items():
            bundle.writestr(name, data)
    return buffer.getvalue()


def test_ui_font_archive_problems_raise_integrity_failed(
    xdg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, _fake = _ctx()
    font = ctx.manifest.ui_font
    archive = tmp_path / "font.zip"
    dest = tmp_path / "Fonts"
    archive.write_bytes(_zip({"selawk.ttf": b"x"}))
    with pytest.raises(IntegrityFailed, match="selawkb.ttf is missing from the archive") as caught:
        fonts.extract_faces(archive, font, dest)
    assert caught.value.hint == "delete the download cache and run setup again"
    archive.write_bytes(b"not a zip")
    with pytest.raises(IntegrityFailed, match="cannot unpack"):
        fonts.extract_faces(archive, font, dest)
    with pytest.raises(IntegrityFailed, match="cannot unpack"):
        fonts.extract_faces(tmp_path / "missing.zip", font, dest)
    monkeypatch.setattr(fonts, "MAX_FACE_BYTES", 4)
    archive.write_bytes(ui_font_archive(font))
    with pytest.raises(IntegrityFailed, match="selawk.ttf is implausibly large"):
        fonts.extract_faces(archive, font, dest)
    assert not dest.exists()


def test_ui_font_unwritable_folder_fails_the_step(xdg: Path, tmp_path: Path) -> None:
    ctx, _fake = _ctx()
    font = ctx.manifest.ui_font
    archive = tmp_path / "font.zip"
    archive.write_bytes(ui_font_archive(font))
    blocker = tmp_path / "file"
    blocker.write_bytes(b"")
    with pytest.raises(SetupFailed, match="cannot write") as caught:
        fonts.extract_faces(archive, font, blocker / "Fonts")
    assert caught.value.step == "ui_font"


def test_ui_font_offline_without_a_cached_archive(xdg: Path) -> None:
    ctx, _fake = _ctx(offline=True)
    with pytest.raises(DownloadFailed, match="offline"):
        fonts.run_ui_font(ctx)


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
