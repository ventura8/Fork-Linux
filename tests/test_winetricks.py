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
    with pytest.raises(IntegrityFailed, match="changed after it was verified"):
        winetricks.ensure(pinned, paths, RecordingRunner())
    assert not winetricks.managed_path(pinned, paths).exists()


def test_ensure_system(paths: Paths, pinned: Manifest) -> None:
    runner = RecordingRunner(which_map={"winetricks": "/usr/bin/winetricks"})
    assert winetricks.ensure(pinned, paths, runner, mode="system") == Path("/usr/bin/winetricks")


def test_ensure_system_missing(paths: Paths, pinned: Manifest) -> None:
    runner = RecordingRunner(which_map={"winetricks": None})
    with pytest.raises(WineUnavailable, match="winetricks is not installed"):
        winetricks.ensure(pinned, paths, runner, mode="system")


def test_ensure_unknown_mode(paths: Paths, pinned: Manifest) -> None:
    with pytest.raises(UsageError, match="unknown winetricks mode"):
        winetricks.ensure(pinned, paths, RecordingRunner(), mode="latest")


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
    with pytest.raises(ValueError, match="no winetricks verbs"):
        winetricks.run_verbs(RecordingRunner(), tmp_path / "winetricks", {}, [])


@pytest.mark.parametrize("verb", ["", "-q", "--force", "a b", "$(id)", "dotnet48;rm", "x" * 65])
def test_run_verbs_rejects_suspicious_verbs(tmp_path: Path, verb: str) -> None:
    with pytest.raises(ValueError, match="not a winetricks verb"):
        winetricks.run_verbs(RecordingRunner(), tmp_path / "winetricks", {}, ["corefonts", verb])


# --------------------------------------------------------------------------- installed_verbs


def test_installed_verbs_without_log(tmp_path: Path) -> None:
    assert winetricks.installed_verbs(tmp_path) == set()


def test_installed_verbs(tmp_path: Path) -> None:
    (tmp_path / "winetricks.log").write_bytes(
        b"remove_mono internal\n\ndotnet48\n  corefonts  \n-q\n# comment\nwin10\n\xff\xfe\n"
    )
    assert winetricks.installed_verbs(tmp_path) == {"remove_mono", "dotnet48", "corefonts", "win10", "��"}


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
