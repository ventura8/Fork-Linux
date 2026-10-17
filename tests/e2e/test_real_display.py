"""How Fork looks: the light or dark theme of the desktop, Wine's DPI applied once (96, 144, 192)
and the interface font (Noto Sans installed as FL UI Sans), semibold included.

Gated by ``FL_E2E_FORK=1``; containers only.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from fork_driver import fork_pids, git, make_repo, wait_until
from fork_ui import ForkUI

from fixtures import real_tier
from fork_linux.steps import fonts as fonts_step

if os.environ.get("FL_E2E_FORK") != "1":
    pytest.skip("requires FL_E2E_FORK=1 (the official Fork under Wine)", allow_module_level=True)
real_tier.enforce()

LIGHT_MEAN = 0.6
DARK_MEAN = 0.35
# Fork's main window size in device-independent units for the DPI tests: wider than any dialog
# titled "Fork" at 96 DPI, and (nearly) inside the 1600x1000 display at 2x.
DIP_WIDTH = 820
DIP_HEIGHT = 480
SCALE_TOLERANCE = 0.03
# The image's only monospace family, which setup maps Consolas (Fork's diff font) to.


def _repo(e2e: Any, name: str) -> Path:
    path = e2e.repos / name
    return path if path.is_dir() else make_repo(path, {".gitignore": "*.tmp\n", "README.md": "# display\n"})


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_theme_follows_the_desktop(closed: Any, theme: str) -> None:
    """Fork's theme follows the desktop's light or dark preference at every start.

    Scenarios: I-THEME-LIGHT, I-THEME-DARK
    """
    e2e = closed
    window = e2e.fork.start(_repo(e2e, "theme"), extra={"FORK_LINUX_DISPLAY_THEME": theme})
    settings = e2e.fork.settings()
    assert settings["Theme"] == (1 if theme == "dark" else 0)
    assert settings["FollowSystemTheme"] is False
    shot = e2e.screen.screenshot(f"theme-{theme}", window)
    mean = e2e.screen.stat(shot, "mean")
    assert (mean < DARK_MEAN) if theme == "dark" else (mean > LIGHT_MEAN), f"{theme}: mean brightness {mean:.2f}"


def test_an_unknown_desktop_theme_hands_control_back_to_fork(closed: Any) -> None:
    """With ``theme=follow`` and no desktop probe answering, the launch turns Fork's own "follow system
    theme" back on instead of keeping the dark theme an earlier launch forced.

    Scenarios: I-THEME-UNKNOWN
    """
    e2e = closed
    # The image has no gsettings / dconf and the test home no GTK settings.ini: nothing can answer.
    assert shutil.which('gsettings') is None
    assert shutil.which('dconf') is None
    assert "GTK_THEME" not in e2e.fork.env
    e2e.fork.start(_repo(e2e, "theme"), extra={"FORK_LINUX_DISPLAY_THEME": "dark"})
    assert e2e.fork.settings()["FollowSystemTheme"] is False
    e2e.fork.ensure_closed()
    e2e.fork.start(_repo(e2e, "theme"), extra={"FORK_LINUX_DISPLAY_THEME": "follow"})
    settings = e2e.fork.settings()
    assert settings["FollowSystemTheme"] is True, settings
    assert fork_pids(e2e.prefix), "Fork must start with its own theme following on"


@pytest.mark.parametrize("dpi", [96, 144, 192])
def test_dpi_scales_fork_once(closed: Any, dpi: int) -> None:
    """``[display] dpi`` sets Wine's DPI; Fork is scaled by exactly that factor (never twice).

    Scenarios: I-HIDPI-96, I-HIDPI-144, I-HIDPI-192
    """
    e2e = closed
    e2e.cli("config", "set", "display.dpi", str(dpi))
    try:
        e2e.cli(*e2e.setup_args())
        assert e2e.fork.settings()["LayoutScaling"] == 100, "Fork's own scaling must stay at 100%"
        geometry = json.dumps({"Left": 0, "Top": 0, "Width": DIP_WIDTH, "Height": DIP_HEIGHT, "WindowState": 0})
        window = e2e.fork.start(_repo(e2e, "dpi"), geometry=geometry)
        e2e.screen.screenshot(f"dpi-{dpi}", window)
        scale = window.width / DIP_WIDTH
        assert abs(scale - dpi / 96) <= SCALE_TOLERANCE * dpi / 96, f"{dpi} DPI: Fork is {scale:.2f}x"
    finally:
        e2e.fork.ensure_closed()
        e2e.cli("config", "unset", "display.dpi")
        e2e.cli(*e2e.setup_args())


def test_a_layout_scaling_the_user_picked_is_kept(closed: Any) -> None:
    """A ``LayoutScaling`` the user chose in Fork stays, even when it equals the desktop's scale: setup
    after a ``[display] dpi`` change and the next launch leave it alone (Wine's DPI does the scaling).

    Scenarios: I-SCALE-USER-KEPT
    """
    e2e = closed
    e2e.cli("settings", "set", "LayoutScaling", "125")
    e2e.cli("config", "set", "display.dpi", "120")
    try:
        e2e.cli(*e2e.setup_args())
        assert e2e.fork.settings()["LayoutScaling"] == 125, "setup changed the user's LayoutScaling"
        e2e.fork.start(_repo(e2e, "user-scale"))
        e2e.fork.ensure_closed()
        assert e2e.fork.settings()["LayoutScaling"] == 125, "a launch changed the user's LayoutScaling"
    finally:
        e2e.fork.ensure_closed()
        e2e.cli("settings", "set", "LayoutScaling", "100")
        e2e.cli("config", "unset", "display.dpi")
        e2e.cli(*e2e.setup_args())


def _mono_font() -> str:
    """The font file (lower-case name) of the family setup maps Consolas to: the first of the
    ``font_replacements`` step's monospace choices that fontconfig knows (Noto Sans Mono in the image)."""
    families = subprocess.run(["fc-list", ":", "family"], capture_output=True, text=True, timeout=60,
                              check=True).stdout.splitlines()
    names = {name.strip() for line in families for name in line.split(",")}
    family = next(choice for choice in fonts_step.MONO_CHOICES if choice in names)
    found = subprocess.run(["fc-match", "-f", "%{file}", family], capture_output=True, text=True, timeout=60,
                           check=True).stdout
    return Path(found).name.lower()


def _mapped_fonts(pid: int) -> set[str]:
    """The font files (lower-case names) process ``pid`` has mapped: the faces Wine really draws with."""
    lines = Path("/proc", str(pid), "maps").read_text().splitlines()
    return {Path(line.split()[-1]).name.lower() for line in lines if line.lower().endswith((".ttf", ".ttc", ".otf"))}


# Commit subjects in Cyrillic and Greek (the interface font draws them) and in Hangul and Han (WPF's
# composite font falls back to the Linux Noto Sans CJK for them: font_fallback).
NON_LATIN_SUBJECTS = ("Привет, мир", "Καλημέρα κόσμε", "안녕하세요 세계", "你好，世界")


def _font_file(family: str) -> str:
    """The font file (lower-case name) fontconfig gives for ``family``."""
    found = subprocess.run(["fc-match", "-f", "%{file}", family], capture_output=True, text=True, timeout=60,
                           check=True).stdout
    return Path(found).name.lower()


def test_non_latin_text_is_drawn_with_the_linux_fonts(closed: Any) -> None:
    """Commit subjects and a branch name in Cyrillic, Greek, Korean and Chinese are drawn (not boxes, not left
    out): Cyrillic and Greek by the interface font itself, Korean and Chinese by the Linux Noto Sans CJK setup
    mapped into WPF's composite font. Fork maps those font files as it draws its history.

    Scenarios: FF-NON-LATIN
    """
    e2e = closed
    repo = e2e.repos / "non-latin"
    if not repo.is_dir():
        make_repo(repo, {".gitignore": "*.tmp\n", "README.md": "# non-latin\n"})
        for index, subject in enumerate(NON_LATIN_SUBJECTS):
            (repo / f"note-{index}.txt").write_text(f"{subject}\n", encoding="utf-8")
            git(repo, "add", "-A")
            git(repo, "commit", "-qm", subject)
        git(repo, "branch", "ветка-분기-分支")
    cjk = _font_file("Noto Sans CJK KR")
    report = json.loads(e2e.cli("doctor", "--json", "--check", "prefix.font_fallback").stdout)
    assert [check["status"] for check in report["checks"]] == ["ok"], report
    ui = ForkUI(e2e.fork)
    window = ui.open(repo)
    pid = fork_pids(e2e.prefix)[0]
    wait_until(lambda: {"flui.ttf", cjk} <= _mapped_fonts(pid) or None, 60,
               f"flui.ttf (the interface font) and {cjk} (the CJK fallback) in Fork's process")
    e2e.screen.screenshot("non-latin-history", window)


def test_interface_text_is_drawn_in_the_interface_font(closed: Any) -> None:
    """Fork draws its interface with the interface font setup installed (``flui.ttf``, and its semibold face
    ``fluisb.ttf`` for headings and the repository name), not Wine's Tahoma, and its diff in the host's
    monospace font that replaces Consolas (no missing-font fallback).

    Scenarios: I-FONT-UI
    """
    e2e = closed
    repo = _repo(e2e, "font")
    (repo / "README.md").write_text("# display\nint main(void) { return 0; }\n", encoding="utf-8")
    ui = ForkUI(e2e.fork)
    window = ui.open(repo)
    ui.refresh()
    ui.local_changes()
    ui.click(ui.unstaged_row(0))
    pid = fork_pids(e2e.prefix)[0]
    mono = _mono_font()
    wait_until(lambda: mono in _mapped_fonts(pid) or None, 30, f"{mono} (Consolas' replacement) in the diff")
    names = _mapped_fonts(pid)
    e2e.screen.screenshot("font-diff", window)
    assert {"flui.ttf", "fluisb.ttf"} <= names, sorted(names)
    assert not {"tahoma.ttf", "tahomabd.ttf", "selawk.ttf"} & names, sorted(names)
    report = json.loads(e2e.cli("doctor", "--json", "--check", "prefix.ui_font", "prefix.font_replacements").stdout)
    assert {check["status"] for check in report["checks"]} == {"ok"}
    (repo / "README.md").write_text("# display\n", encoding="utf-8")
