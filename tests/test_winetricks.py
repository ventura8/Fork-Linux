"""Tests for fork_linux.winetricks: pinned install, system lookup, running verbs, the verb log."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path
from typing import Any

import pytest

from fork_linux import download, manifest, winetricks
from fork_linux.errors import IntegrityFailed, UsageError, WineUnavailable
from fork_linux.manifest import Manifest
from fork_linux.paths import Paths
from fork_linux.procrun import RecordingRunner, Runner
from fork_linux.wine_provider import COMPLETE_MARKER

SCRIPT = b"#!/bin/sh\n# winetricks (test copy)\necho winetricks\n"
VERSION = "20260125"
URL = f"https://raw.githubusercontent.com/Winetricks/winetricks/{VERSION}/src/winetricks"
FAKES_BIN = Path(__file__).resolve().parent / "fakes" / "bin"


@pytest.fixture
def paths(xdg: Path) -> Paths:
    return Paths.from_env(os.environ)


@pytest.fixture
def pinned() -> Manifest:
    data = manifest.load().as_dict()
    data["winetricks"] = {
        "version": VERSION,
        "url": URL,
        "sha256": hashlib.sha256(SCRIPT).hexdigest(),
        "size": len(SCRIPT),
    }
    return Manifest(data)


class FakeFetch:
    """Stands in for download.fetch: writes ``payload`` into the cache and records the call."""

    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.calls: list[dict[str, Any]] = []

    def __call__(self, url: str, dest_name: str, **kwargs: Any) -> Path:
        self.calls.append({"url": url, "dest_name": dest_name, **kwargs})
        dest = Path(kwargs["cache_dir"]) / dest_name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(self.payload)
        return dest


@pytest.fixture
def fetch(monkeypatch: pytest.MonkeyPatch) -> FakeFetch:
    fake = FakeFetch(SCRIPT)
    monkeypatch.setattr(download, "fetch", fake)
    return fake


# --------------------------------------------------------------------------- ensure


def test_managed_path(paths: Paths, pinned: Manifest) -> None:
    assert winetricks.managed_path(pinned, paths) == paths.runtimes_dir / "winetricks" / VERSION / "winetricks"


def test_ensure_managed_installs_the_pinned_script(paths: Paths, pinned: Manifest, fetch: FakeFetch) -> None:
    progress: list[Any] = []
    path = winetricks.ensure(pinned, paths, RecordingRunner(), offline=True, progress=progress.append)
    assert path == winetricks.managed_path(pinned, paths)
    assert path.read_bytes() == SCRIPT
    assert stat.S_IMODE(path.stat().st_mode) == 0o755
    assert (path.parent / COMPLETE_MARKER).read_text(encoding="utf-8") == pinned.winetricks.sha256 + "\n"
    assert fetch.calls == [
        {
            "url": URL,
            "dest_name": f"winetricks-{VERSION}",
            "cache_dir": paths.downloads_dir,
            "sha256": pinned.winetricks.sha256,
            "size": len(SCRIPT),
            "offline": True,
            "progress": progress.append,
            "allowed_hosts": ("raw.githubusercontent.com",),
        }
    ]


def test_ensure_managed_is_idempotent(paths: Paths, pinned: Manifest, fetch: FakeFetch) -> None:
    first = winetricks.ensure(pinned, paths, RecordingRunner())
    assert winetricks.ensure(pinned, paths, RecordingRunner()) == first
    assert len(fetch.calls) == 1


def test_ensure_managed_replaces_a_modified_copy(paths: Paths, pinned: Manifest, fetch: FakeFetch) -> None:
    target = winetricks.ensure(pinned, paths, RecordingRunner())
    target.write_bytes(b"#!/bin/sh\necho tampered\n")
    winetricks.ensure(pinned, paths, RecordingRunner())
    assert target.read_bytes() == SCRIPT
    assert len(fetch.calls) == 2


def test_ensure_managed_replaces_a_non_executable_copy(paths: Paths, pinned: Manifest, fetch: FakeFetch) -> None:
    target = winetricks.ensure(pinned, paths, RecordingRunner())
    target.chmod(0o644)
    winetricks.ensure(pinned, paths, RecordingRunner())
    assert stat.S_IMODE(target.stat().st_mode) == 0o755
    assert len(fetch.calls) == 2


def test_ensure_managed_replaces_a_symlink(paths: Paths, pinned: Manifest, fetch: FakeFetch, tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.write_bytes(SCRIPT)
    elsewhere.chmod(0o755)
    target = winetricks.managed_path(pinned, paths)
    target.parent.mkdir(parents=True)
    target.symlink_to(elsewhere)
    winetricks.ensure(pinned, paths, RecordingRunner())
    assert not target.is_symlink()
    assert len(fetch.calls) == 1


def test_ensure_managed_detects_a_swapped_download(
    paths: Paths, pinned: Manifest, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(download, "fetch", FakeFetch(b"#!/bin/sh\necho evil\n"))
    runner = RecordingRunner()
    with pytest.raises(IntegrityFailed, match="changed after it was verified"):
        winetricks.ensure(pinned, paths, runner)
    assert not winetricks.managed_path(pinned, paths).exists()


def test_ensure_system(paths: Paths, pinned: Manifest) -> None:
    runner = RecordingRunner(which_map={"winetricks": "/usr/bin/winetricks"})
    assert winetricks.ensure(pinned, paths, runner, mode="system") == Path("/usr/bin/winetricks")


def test_ensure_system_missing(paths: Paths, pinned: Manifest) -> None:
    runner = RecordingRunner(which_map={"winetricks": None})
    with pytest.raises(WineUnavailable, match="winetricks is not installed"):
        winetricks.ensure(pinned, paths, runner, mode="system")


def test_ensure_unknown_mode(paths: Paths, pinned: Manifest) -> None:
    runner = RecordingRunner()
    with pytest.raises(UsageError, match="unknown winetricks mode"):
        winetricks.ensure(pinned, paths, runner, mode="latest")


# --------------------------------------------------------------------------- run_verbs


def test_run_verbs(tmp_path: Path) -> None:
    runner = RecordingRunner()
    env = {"WINEPREFIX": "/p", "WINELOADER": "/r/bin/wine", "WINESERVER": "/r/bin/wineserver"}
    log_file = tmp_path / "winetricks.log"
    result = winetricks.run_verbs(runner, tmp_path / "winetricks", env, ["dotnet48", "corefonts"], log_file=log_file)
    assert result.ok
    call = runner.calls[0]
    assert call["argv"] == [str(tmp_path / "winetricks"), "-q", "dotnet48", "corefonts"]
    assert call["env"] == {
        **env,
        "WINE": "/r/bin/wine",
        "WINETRICKS_LATEST_VERSION_CHECK": "disabled",
        "W_OPT_UNATTENDED": "1",
    }
    assert call["timeout"] == 2700
    assert call["log_file"] == log_file
    assert "WINE" not in env


def test_run_verbs_single_string_and_no_loader(tmp_path: Path) -> None:
    runner = RecordingRunner({"winetricks": 1})
    result = winetricks.run_verbs(runner, tmp_path / "winetricks", {"WINEPREFIX": "/p"}, "renderer=gdi", timeout=60)
    assert result.returncode == 1
    call = runner.calls[0]
    assert call["argv"][1:] == ["-q", "renderer=gdi"]
    assert "WINE" not in call["env"]
    assert call["timeout"] == 60


def test_run_verbs_requires_verbs(tmp_path: Path) -> None:
    runner = RecordingRunner()
    with pytest.raises(ValueError, match="no winetricks verbs"):
        winetricks.run_verbs(runner, tmp_path / "winetricks", {}, [])


@pytest.mark.parametrize("verb", ["", "-q", "--force", "a b", "$(id)", "dotnet48;rm", "x" * 65])
def test_run_verbs_rejects_suspicious_verbs(tmp_path: Path, verb: str) -> None:
    runner = RecordingRunner()
    with pytest.raises(ValueError, match="not a winetricks verb"):
        winetricks.run_verbs(runner, tmp_path / "winetricks", {}, ["corefonts", verb])


# --------------------------------------------------------------------------- installed_verbs


def test_installed_verbs_without_log(tmp_path: Path) -> None:
    assert winetricks.installed_verbs(tmp_path) == set()


def test_installed_verbs(tmp_path: Path) -> None:
    (tmp_path / "winetricks.log").write_bytes(
        b"remove_mono internal\n\ndotnet48\n  corefonts  \n-q\n# comment\nwin10\n\xff\xfe\n"
    )
    assert winetricks.installed_verbs(tmp_path) == {"remove_mono", "dotnet48", "corefonts", "win10", "\ufffd\ufffd"}


def test_installed_verbs_unreadable(tmp_path: Path) -> None:
    (tmp_path / "winetricks.log").mkdir()
    assert winetricks.installed_verbs(tmp_path) == set()


def test_run_verbs_with_fake_winetricks(fake_bin: Path, tmp_path: Path) -> None:
    prefix = tmp_path / "prefix"
    prefix.mkdir()
    env = {**os.environ, "WINEPREFIX": str(prefix), "WINELOADER": "/r/bin/wine", "WINESERVER": "/r/bin/wineserver"}
    result = winetricks.run_verbs(Runner(), FAKES_BIN / "winetricks", env, ["dotnet48", "corefonts"])
    assert result.ok
    assert winetricks.installed_verbs(prefix) == {"dotnet48", "corefonts"}
    logged = fake_bin.read_text(encoding="utf-8")
    assert '"W_OPT_UNATTENDED": "1"' in logged
    assert '"WINE": "/r/bin/wine"' in logged


def test_run_verbs_with_our_cache(tmp_path: Path) -> None:
    runner = RecordingRunner()
    cache = tmp_path / "cache" / "fork-linux" / "winetricks"
    winetricks.run_verbs(runner, tmp_path / "winetricks", {"WINEPREFIX": "/p"}, ["corefonts"], cache=cache)
    assert runner.calls[0]["env"]["W_CACHE"] == str(cache)
    assert cache.is_dir()
    assert stat.S_IMODE(cache.stat().st_mode) == 0o755


def test_run_verbs_without_a_cache_leaves_winetricks_default(tmp_path: Path) -> None:
    runner = RecordingRunner()
    winetricks.run_verbs(runner, tmp_path / "winetricks", {"WINEPREFIX": "/p"}, ["corefonts"])
    assert "W_CACHE" not in runner.calls[0]["env"]


# --------------------------------------------------------------------------- seed_cache


def _legacy_tree(root: Path) -> Path:
    legacy = root / "winetricks"
    (legacy / "corefonts").mkdir(parents=True)
    (legacy / "corefonts" / "arial32.exe").write_bytes(b"arial")
    (legacy / "corefonts" / "times32.exe").write_bytes(b"times")
    (legacy / "corefonts" / "empty.exe").write_bytes(b"")
    (legacy / "corefonts" / ".hidden").write_bytes(b"x")
    (legacy / "corefonts" / "subdir").mkdir()
    (legacy / "corefonts" / "link.exe").symlink_to(legacy / "corefonts" / "arial32.exe")
    (legacy / "dotnet48").mkdir()
    (legacy / "dotnet48" / "ndp48-x86-x64-allos-enu.exe").write_bytes(b"dotnet")
    (legacy / "vcrun2019").mkdir()
    (legacy / "vcrun2019" / "vc_redist.x64.exe").write_bytes(b"vc")
    return legacy


def test_seed_cache_links_the_known_packages(tmp_path: Path) -> None:
    legacy = _legacy_tree(tmp_path)
    cache = tmp_path / "fork-linux" / "winetricks"
    added = winetricks.seed_cache(cache, legacy)
    assert added == [
        cache / "corefonts" / "arial32.exe",
        cache / "corefonts" / "times32.exe",
        cache / "dotnet48" / "ndp48-x86-x64-allos-enu.exe",
    ]
    assert os.path.samefile(cache / "corefonts" / "arial32.exe", legacy / "corefonts" / "arial32.exe")
    assert not (cache / "vcrun2019").exists()
    assert sorted(os.listdir(cache / "corefonts")) == ["arial32.exe", "times32.exe"]
    assert stat.S_IMODE((cache / "corefonts").stat().st_mode) == 0o755
    # The legacy cache is only read.
    assert sorted(os.listdir(legacy / "corefonts")) == [
        ".hidden", "arial32.exe", "empty.exe", "link.exe", "subdir", "times32.exe",
    ]
    # A second run adds nothing: every file is there already.
    assert winetricks.seed_cache(cache, legacy) == []


def test_seed_cache_keeps_our_own_files(tmp_path: Path) -> None:
    legacy = _legacy_tree(tmp_path)
    cache = tmp_path / "fork-linux" / "winetricks"
    (cache / "corefonts").mkdir(parents=True)
    (cache / "corefonts" / "arial32.exe").write_bytes(b"ours")
    (cache / "corefonts" / "times32.exe").symlink_to(tmp_path / "dangling")
    added = winetricks.seed_cache(cache, legacy, packages=("corefonts",))
    assert added == []
    assert (cache / "corefonts" / "arial32.exe").read_bytes() == b"ours"


def test_seed_cache_without_a_legacy_cache(tmp_path: Path) -> None:
    assert winetricks.seed_cache(tmp_path / "ours", tmp_path / "missing") == []
    assert not (tmp_path / "ours").exists()


def test_seed_cache_copies_across_file_systems(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    legacy = _legacy_tree(tmp_path)
    cache = tmp_path / "fork-linux" / "winetricks"

    def no_link(source: object, target: object) -> None:
        raise OSError(18, "Invalid cross-device link")

    monkeypatch.setattr(winetricks.os, "link", no_link)
    added = winetricks.seed_cache(cache, legacy, packages=("dotnet48",))
    target = cache / "dotnet48" / "ndp48-x86-x64-allos-enu.exe"
    assert added == [target]
    assert target.read_bytes() == b"dotnet"
    assert not os.path.samefile(target, legacy / "dotnet48" / "ndp48-x86-x64-allos-enu.exe")


def test_seed_cache_replaces_a_stale_temp_file(tmp_path: Path) -> None:
    legacy = _legacy_tree(tmp_path)
    cache = tmp_path / "fork-linux" / "winetricks"
    (cache / "dotnet48").mkdir(parents=True)
    stale = cache / "dotnet48" / ".ndp48-x86-x64-allos-enu.exe.fork-linux-tmp"
    stale.symlink_to(tmp_path / "elsewhere")
    added = winetricks.seed_cache(cache, legacy, packages=("dotnet48",))
    assert added == [cache / "dotnet48" / "ndp48-x86-x64-allos-enu.exe"]
    assert not os.path.lexists(stale)
    assert not (tmp_path / "elsewhere").exists()


def test_seed_cache_skips_a_file_it_cannot_take(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    legacy = _legacy_tree(tmp_path)
    cache = tmp_path / "fork-linux" / "winetricks"

    def no_link(source: object, target: object) -> None:
        raise OSError(18, "Invalid cross-device link")

    def no_copy(source: object, target: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(winetricks.os, "link", no_link)
    monkeypatch.setattr(winetricks.shutil, "copyfile", no_copy)
    assert winetricks.seed_cache(cache, legacy, packages=("dotnet48",)) == []
    assert os.listdir(cache / "dotnet48") == []


def test_seed_packages_cover_our_verbs() -> None:
    assert set(winetricks.SEED_PACKAGES) >= {"corefonts", *manifest.load().dotnet_verbs}
