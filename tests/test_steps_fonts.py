"""Tests for the fonts, font_replacements and display_dpi setup steps."""

from __future__ import annotations

from pathlib import Path

import pytest
from fixtures.setup_ctx import FakeWine, make_ctx

from fork_linux import bootstrap
from fork_linux.bootstrap import Ctx
from fork_linux.errors import ForkLinuxError, SetupFailed
from fork_linux.procrun import Completed, RecordingRunner
from fork_linux.steps import display, fonts, runtime

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


def test_replacements_prefer_noto_then_fallbacks(xdg: Path) -> None:
    ctx, _fake = _ctx({"fc-list": FC_LIST})
    chosen = fonts.replacements(ctx)
    assert chosen == {
        "Segoe UI": "Noto Sans",
        "Segoe UI Semibold": "Noto Sans",
        "Segoe UI Light": "Noto Sans",
        "Segoe UI Symbol": "Noto Sans",
        "Consolas": "DejaVu Sans Mono",
    }
    assert fonts.replacement_inputs(ctx) == {"replacements": chosen}
    # fc-list runs once per context.
    fonts.replacements(ctx)
    assert [call["argv"] for call in ctx.runner.calls] == [["fc-list", ":", "family"]]


@pytest.mark.parametrize(
    "response",
    [Completed([], 127, "", "not found"), "Some Other Font\n"],
)
def test_replacements_without_known_fonts(xdg: Path, response: object) -> None:
    ctx, _fake = _ctx({"fc-list": response})
    chosen = fonts.replacements(ctx)
    assert chosen["Segoe UI"] == fonts.SANS_FALLBACK
    assert chosen["Consolas"] == fonts.MONO_FALLBACK


def test_replacements_when_fc_list_cannot_run(xdg: Path) -> None:
    def broken(argv: list[str]) -> Completed:
        raise ForkLinuxError("timed out")

    ctx, _fake = _ctx({"fc-list": broken})
    assert fonts.installed_families(ctx) == set()


def test_font_replacements_are_imported_and_verified(xdg: Path) -> None:
    ctx, _fake = _ctx({"fc-list": FC_LIST})
    assert not fonts.verify_replacements(ctx)
    fonts.run_replacements(ctx)
    assert fonts.verify_replacements(ctx)
    reg = (ctx.paths.fork_linux_win_dir / "tmp" / "font-replacements.reg").read_bytes().decode("utf-16")
    assert "[HKEY_CURRENT_USER\\Software\\Wine\\Fonts\\Replacements]" in reg
    assert '"Segoe UI"="Noto Sans"' in reg


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
