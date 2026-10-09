"""Load ``scripts/gen-data.py`` (a script with a dash in its name) as a module, once."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[2]
NAME = "fork_linux_gen_data"


def load() -> ModuleType:
    """The ``scripts/gen-data.py`` module (registered in ``sys.modules`` for dataclasses)."""
    if NAME in sys.modules:
        return sys.modules[NAME]
    spec = importlib.util.spec_from_file_location(NAME, ROOT / "scripts" / "gen-data.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[NAME] = module
    spec.loader.exec_module(module)
    return module
