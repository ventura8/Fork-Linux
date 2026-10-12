"""How Fork looks: the light or dark theme of the desktop, Wine's DPI applied once (96, 144, 192)
and Selawik as the interface font.

Gated by ``FL_E2E_FORK=1``; containers only.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
from fork_driver import fork_pids, make_repo, wait_until
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


def test_interface_text_is_drawn_in_selawik(closed: Any) -> None:
    """Fork draws its interface with Selawik (installed by setup), not Wine's Tahoma, and its diff in the
    host's monospace font that replaces Consolas (no missing-font fallback).

    Scenarios: I-FONT-SELAWIK
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
    assert "selawk.ttf" in names, sorted(names)
    assert not {"tahoma.ttf", "tahomabd.ttf"} & names, sorted(names)
    report = json.loads(e2e.cli("doctor", "--json", "--check", "prefix.ui_font", "prefix.font_replacements").stdout)
    assert {check["status"] for check in report["checks"]} == {"ok"}
    (repo / "README.md").write_text("# display\n", encoding="utf-8")
