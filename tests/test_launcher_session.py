"""The Wine recorded in ``session.json`` only selects among Wines found from trusted sources."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from fork_linux import launcher, wine_provider
from fork_linux.paths import Paths


class _Which:
    def __init__(self, found: dict[str, str]) -> None:
        self.found = found

    def which(self, name: str, path: str | None = None) -> str | None:
        return self.found.get(name)


class _Config:
    def __init__(self, provider: str) -> None:
        self.provider = provider

    def get(self, section: str, key: str) -> str:
        return self.provider


def _paths(tmp_path: Path) -> Paths:
    return Paths.from_env({"HOME": str(tmp_path / "home"), "XDG_RUNTIME_DIR": str(tmp_path / "run")})


def _app(paths: Paths, found: dict[str, str], provider: str = "managed") -> Any:
    return SimpleNamespace(paths=paths, runner=_Which(found), env={"PATH": "/nowhere"}, config=_Config(provider))


def _fake_wine(root: Path) -> Path:
    wine = root / "bin" / "wine"
    wine.parent.mkdir(parents=True)
    wine.write_text("#!/bin/sh\n", encoding="utf-8")
    return wine


def _session(paths: Paths, **data: Any) -> None:
    paths.runtime_dir.mkdir(parents=True, exist_ok=True)
    paths.session_file.write_text(json.dumps({"prefix": str(paths.prefix), **data}), encoding="utf-8")


def test_trusted_wines_list_every_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths = _paths(tmp_path)
    monkeypatch.setattr(wine_provider, "installed_builds", lambda p: ["build-1"])
    usr = tmp_path / "usr"
    custom = tmp_path / "custom"
    custom.mkdir()
    found = {"wine": str(usr / "bin" / "wine"), "wineserver": str(usr / "lib" / "wineserver")}
    candidates = launcher.trusted_wines(_app(paths, found, str(custom)))
    managed = wine_provider.managed_root(paths, "build-1")
    assert [(c.root, c.wine) for c in candidates] == [
        (managed, managed / "bin" / "wine"),
        (wine_provider.FLATPAK_ROOT, wine_provider.FLATPAK_ROOT / "bin" / "wine"),
        (usr, usr / "bin" / "wine"),
        (custom, custom / "bin" / "wine"),
    ]
    assert usr / "lib" / "wineserver" in candidates[2].wineservers
    assert set(wine_provider.SYSTEM_WINESERVER_FALLBACKS) <= set(candidates[2].wineservers)


def test_trusted_wines_custom_binary_and_no_system_wineserver(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    usr = tmp_path / "usr"
    binary = tmp_path / "opt" / "bin" / "wine"
    candidates = launcher.trusted_wines(_app(paths, {"wine": str(usr / "bin" / "wine")}, str(binary)))
    assert [(c.root, c.wine) for c in candidates[1:]] == [(usr, usr / "bin" / "wine"), (tmp_path / "opt", binary)]
    assert candidates[1].wineservers[2:] == wine_provider.SYSTEM_WINESERVER_FALLBACKS


def test_trusted_wines_without_system_or_custom_wine(tmp_path: Path) -> None:
    candidates = launcher.trusted_wines(_app(_paths(tmp_path), {}))
    assert [c.root for c in candidates] == [wine_provider.FLATPAK_ROOT]


def test_session_selects_a_trusted_wine(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    root = tmp_path / "system-wine"
    wine = _fake_wine(root)
    server = tmp_path / "elsewhere" / "wineserver"
    candidates = [launcher._candidate(root, wine, server)]
    _session(paths, wine_root=str(root), wine=str(wine), wineserver=str(server), provider="system")
    found = launcher._session_wine(paths, candidates)
    assert found is not None
    assert (found.provider, found.root, found.wine, found.wineserver) == ("system", root, wine, server)
    # A wineserver no candidate knows falls back to the root's.
    _session(paths, wine_root=str(root), wine=str(wine), wineserver="/tmp/evil")
    found = launcher._session_wine(paths, candidates)
    assert found is not None
    assert found.wineserver == root / "bin" / "wineserver"


def test_session_wine_must_match_a_candidate_that_exists(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    root = tmp_path / "wine"
    wine = _fake_wine(root)
    candidates = [launcher._candidate(root, wine)]
    _session(paths, wine_root=str(root), wine=str(tmp_path / "other" / "wine"))
    assert launcher._session_wine(paths, candidates) is None
    _session(paths, wine_root=str(tmp_path / "other"), wine=str(wine))
    assert launcher._session_wine(paths, candidates) is None
    _session(paths, wine_root=str(root), wine=str(wine))
    wine.unlink()
    assert launcher._session_wine(paths, candidates) is None
