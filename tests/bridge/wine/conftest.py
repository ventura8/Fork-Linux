"""Bridge Wine tier (tests/bridge/wine): report header.

The gate itself is the module-level ``pytest.skip("requires FL_REAL_WINE=1",
allow_module_level=True)`` at the top of each test module (AGENTS.md hard rule 16). It is
not raised here: a skip raised while a conftest is imported skips its whole directory
(which would hide the native tests if this lived in tests/bridge/) and is a hard error
when the directory is given on the command line.
"""

from __future__ import annotations

import os

import pytest

from .fl_winetier import wine_candidates


def pytest_report_header(config: pytest.Config) -> str:
    if os.environ.get("FL_REAL_WINE") != "1":
        return "bridge Wine tier: off (set FL_REAL_WINE=1)"
    return "bridge Wine tier: wines = " + (
        ", ".join(wine_candidates()) or "<none found>"
    )
