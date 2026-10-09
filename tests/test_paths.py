"""Tests for fork_linux.paths: XDG resolution, derived locations and the prefix safety policy."""

from __future__ import annotations

import dataclasses
import os
import stat
from pathlib import Path

import pytest

from fork_linux import paths as paths_mod
from fork_linux.errors import ExitCode, UsageError
from fork_linux.paths import Paths, is_forbidden_prefix


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _mark(prefix: Path) -> None:
    (prefix / ".fork-linux").mkdir(parents=True)
    (prefix / ".fork-linux" / "created-by").write_text("fork-linux\n", encoding="utf-8")


def test_from_env_uses_xdg_variables(xdg: Path) -> None:
    p = Paths.from_env()
    assert p.config_dir == xdg / ".config" / "fork-linux"
    assert p.data_dir == xdg / ".local/share" / "fork-linux"
    assert p.cache_dir == xdg / ".cache" / "fork-linux"
    assert p.state_dir == xdg / ".local/state" / "fork-linux"
    assert p.runtime_dir == xdg / "run" / "fork-linux"
    assert p.prefix == p.data_dir / "prefix"


def test_from_env_standard_fallbacks_under_home(tmp_path: Path) -> None:
    home = tmp_path / "h"
    p = Paths.from_env({"HOME": str(home)})
    assert p.config_dir == home / ".config/fork-linux"
    assert p.data_dir == home / ".local/share/fork-linux"
    assert p.cache_dir == home / ".cache/fork-linux"
    assert p.state_dir == home / ".local/state/fork-linux"
    assert p.runtime_dir == p.state_dir / "run"
    assert p.prefix == home / ".local/share/fork-linux/prefix"


def test_relative_xdg_values_are_ignored(tmp_path: Path) -> None:
    home = tmp_path / "h"
    env = {
        "HOME": str(home),
        "XDG_CONFIG_HOME": "relative/config",
        "XDG_DATA_HOME": "",
        "XDG_RUNTIME_DIR": "run",
    }
    p = Paths.from_env(env)
    assert p.config_dir == home / ".config/fork-linux"
    assert p.data_dir == home / ".local/share/fork-linux"
    assert p.runtime_dir == home / ".local/state/fork-linux/run"


def test_missing_home_falls_back_to_path_home(xdg: Path) -> None:
    p = Paths.from_env({})
    assert p.config_dir == xdg / ".config" / "fork-linux"


def test_from_env_defaults_to_os_environ(xdg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(xdg / "elsewhere"))
    assert Paths.from_env().cache_dir == xdg / "elsewhere" / "fork-linux"


def test_derived_locations(tmp_path: Path) -> None:
    p = Paths.from_env({"HOME": str(tmp_path)})
    assert p.config_file == p.config_dir / "config.ini"
    assert p.manifest_override == p.config_dir / "manifest.local.json"
    assert p.runtimes_dir == p.data_dir / "runtimes"
    assert p.snapshots_dir == p.data_dir / "snapshots"
    assert p.integrations_file == p.data_dir / "integrations.json"
    assert p.downloads_dir == p.cache_dir / "downloads"
    assert p.feeds_dir == p.cache_dir / "feeds"
    assert p.logs_dir == p.state_dir / "logs"
    assert p.lock_file == p.runtime_dir / "setup.lock"
    assert p.session_file == p.runtime_dir / "session.json"
    assert p.prefix_meta_dir == p.prefix / ".fork-linux"
    assert p.state_file == p.prefix / ".fork-linux" / "state.json"
    assert p.created_by_marker == p.prefix / ".fork-linux" / "created-by"
    assert p.fork_linux_win_dir == p.prefix / "drive_c" / "fork-linux"


def test_wine_user_locations(tmp_path: Path) -> None:
    p = Paths.from_env({"HOME": str(tmp_path)})
    users = p.prefix / "drive_c" / "users"
    assert p.wine_user_dir("ana") == users / "ana"
    assert p.fork_local_dir("ana") == users / "ana" / "AppData" / "Local" / "Fork"
    assert p.fork_current_dir("ana") == users / "ana" / "AppData" / "Local" / "Fork" / "current"


def test_paths_is_frozen(tmp_path: Path) -> None:
    p = Paths.from_env({"HOME": str(tmp_path)})
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(p, "prefix", tmp_path)


def test_prefix_under_data_dir_is_accepted(xdg: Path) -> None:
    custom = xdg / ".local/share/fork-linux/other-prefix"
    assert Paths.from_env(prefix=custom).prefix == custom


def test_explicit_prefix_beats_environment(xdg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = xdg / ".local/share/fork-linux"
    monkeypatch.setenv("FORK_LINUX_PREFIX", str(data / "from-env"))
    assert Paths.from_env().prefix == data / "from-env"
    assert Paths.from_env(prefix=data / "explicit").prefix == data / "explicit"


def test_prefix_tilde_and_relative_are_made_absolute(xdg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FORK_LINUX_PREFIX", "~/.local/share/fork-linux/p1")
    assert Paths.from_env().prefix == xdg / ".local/share/fork-linux/p1"
    monkeypatch.chdir(xdg / ".local/share")
    monkeypatch.setenv("FORK_LINUX_PREFIX", "fork-linux/p2")
    assert Paths.from_env().prefix == xdg / ".local/share/fork-linux/p2"


@pytest.mark.parametrize("adopt", ["0", "1"])
def test_dot_wine_is_always_refused(xdg: Path, monkeypatch: pytest.MonkeyPatch, adopt: str) -> None:
    wine = xdg / ".wine"
    _mark(wine)
    monkeypatch.setenv("FORK_LINUX_ADOPT_PREFIX", adopt)
    monkeypatch.setenv("FORK_LINUX_PREFIX", str(wine))
    with pytest.raises(UsageError) as info:
        Paths.from_env()
    assert info.value.exit_code == ExitCode.USAGE
    assert "~/.wine" in info.value.hint
    with pytest.raises(UsageError):
        Paths.from_env(prefix=wine / "drive_c")
    with pytest.raises(UsageError):
        Paths.from_env(prefix=Path("~/.wine"))


def test_symlink_to_dot_wine_target_is_refused(xdg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real = xdg / "real-wine"
    real.mkdir()
    (xdg / ".wine").symlink_to(real)
    monkeypatch.setenv("FORK_LINUX_ADOPT_PREFIX", "1")
    with pytest.raises(UsageError):
        Paths.from_env(prefix=real)


@pytest.mark.parametrize("which", ["home", "root", "parent"])
def test_home_root_and_ancestors_are_refused(xdg: Path, monkeypatch: pytest.MonkeyPatch, which: str) -> None:
    target = {"home": xdg, "root": Path("/"), "parent": xdg.parent}[which]
    monkeypatch.setenv("FORK_LINUX_ADOPT_PREFIX", "1")
    with pytest.raises(UsageError, match="refusing"):
        Paths.from_env(prefix=target)


@pytest.mark.parametrize("store", ["runtimes", "snapshots"])
def test_our_own_stores_are_never_a_prefix(xdg: Path, monkeypatch: pytest.MonkeyPatch, store: str) -> None:
    data = xdg / ".local/share/fork-linux"
    target = data / store / "wine" / "x"
    _mark(target)
    monkeypatch.setenv("FORK_LINUX_ADOPT_PREFIX", "1")
    for candidate in (data / store, target):
        with pytest.raises(UsageError, match="refusing") as info:
            Paths.from_env(prefix=candidate)
        assert store in info.value.hint
        assert is_forbidden_prefix(candidate, xdg, data_dir=data)
    assert not is_forbidden_prefix(data / f"{store}-prefix", xdg, data_dir=data)


def test_foreign_prefix_is_refused_without_adoption(xdg: Path, tmp_path: Path) -> None:
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    with pytest.raises(UsageError) as info:
        Paths.from_env(prefix=foreign)
    assert "not created by fork-linux" in info.value.message
    assert "FORK_LINUX_ADOPT_PREFIX=1" in info.value.hint


def test_missing_prefix_outside_data_dir_says_so(xdg: Path, tmp_path: Path) -> None:
    # E2E: FORK_LINUX_PREFIX=<new path> was refused as "not created by fork-linux".
    with pytest.raises(UsageError) as info:
        Paths.from_env(prefix=tmp_path / "new-prefix")
    assert "creates new prefixes only under" in info.value.message
    assert "FORK_LINUX_ADOPT_PREFIX=1" in info.value.hint


def test_adoption_accepts_existing_foreign_prefix(xdg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    monkeypatch.setenv("FORK_LINUX_ADOPT_PREFIX", "1")
    assert Paths.from_env(prefix=foreign).prefix == foreign
    with pytest.raises(UsageError):
        Paths.from_env(prefix=tmp_path / "does-not-exist")


def test_marked_prefix_outside_data_dir_is_accepted(xdg: Path, tmp_path: Path) -> None:
    ours = tmp_path / "scratch-prefix"
    _mark(ours)
    assert Paths.from_env(prefix=ours).prefix == ours


def test_is_forbidden_prefix_rules(xdg: Path, tmp_path: Path) -> None:
    data = xdg / ".local/share/fork-linux"
    assert is_forbidden_prefix(xdg / ".wine", xdg)
    assert is_forbidden_prefix(xdg, xdg)
    assert is_forbidden_prefix(Path("/"), xdg)
    assert is_forbidden_prefix(tmp_path / "x", xdg)
    assert is_forbidden_prefix(data, xdg), "the data dir itself is not a prefix"
    assert not is_forbidden_prefix(data / "prefix", xdg)
    assert not is_forbidden_prefix(data / "prefix", xdg, data_dir=data)
    marked = tmp_path / "marked"
    _mark(marked)
    assert not is_forbidden_prefix(marked, xdg)
    assert is_forbidden_prefix(tmp_path / "other" / "prefix", xdg, data_dir=tmp_path / "other2")


def test_is_forbidden_prefix_default_data_dir_follows_environment(
    xdg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    alt = tmp_path / "alt-data"
    monkeypatch.setenv("XDG_DATA_HOME", str(alt))
    assert not is_forbidden_prefix(alt / "fork-linux" / "prefix", xdg)
    assert is_forbidden_prefix(xdg / ".local/share/fork-linux/prefix", xdg)


def test_ensure_creates_directories_with_private_modes(xdg: Path) -> None:
    p = Paths.from_env()
    p.data_dir.mkdir(parents=True, mode=0o755)
    os.chmod(p.data_dir, 0o755)
    p.ensure()
    for directory in (p.config_dir, p.data_dir, p.cache_dir, p.state_dir, p.runtime_dir, p.logs_dir):
        assert directory.is_dir()
    assert _mode(p.data_dir) == 0o700
    assert _mode(p.runtime_dir) == 0o700
    assert _mode(p.logs_dir) == 0o700
    p.ensure()
    assert _mode(p.data_dir) == 0o700


def test_module_constants() -> None:
    assert paths_mod.APP_DIR == "fork-linux"
    assert paths_mod.PREFIX_ENV == "FORK_LINUX_PREFIX"
    assert paths_mod.ADOPT_ENV == "FORK_LINUX_ADOPT_PREFIX"
