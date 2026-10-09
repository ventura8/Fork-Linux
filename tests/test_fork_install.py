"""Tests for fork_linux.fork_install: choosing, fetching, running and verifying the Fork installer."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from fixtures.fork_tree import install_fork, make_layout, make_paths, write_sq_version
from fork_linux import fork_install as fi
from fork_linux import manifest as manifest_mod
from fork_linux.errors import ExitCode, ForkLinuxError, IntegrityFailed, SetupFailed, UsageError
from fork_linux.feeds import FeedAsset
from fork_linux.fork_install import InstallPlan
from fork_linux.manifest import Manifest
from fork_linux.pathmap import PathMap
from fork_linux.procrun import Completed, RecordingRunner

PINNED_SHA = "fee9b2bf84aca6297d7b7e10b29a09c624ac16a82486f2c04136f5ebaf8f079e"
NUPKG_SHA = "b70e7be92b73baeab9fee700ad352ae9e261dafcded0d10e5c94133f639b1c1f"


def _data() -> dict[str, Any]:
    data = json.loads(manifest_mod.DEFAULT_PATH.read_text(encoding="utf-8"))
    fork = data["fork"]
    fork["default"] = "2.23.2"
    fork["versions"] = {
        "2.23.2": {
            "size": 76278256,
            "sha256": PINNED_SHA,
            "full_nupkg_sha256": NUPKG_SHA.upper(),
            "status": "known-good",
            "tested_with": ["kron4ek-11.0-staging-wow64"],
        },
        "2.24.0": {
            "size": 77000000,
            "sha256": "PENDING",
            "full_nupkg_sha256": "a" * 64,
            "status": "testing",
            "tested_with": [],
        },
        "2.21.0": {
            "size": 70000000,
            "sha256": "b" * 64,
            "full_nupkg_sha256": "c" * 64,
            "status": "known-bad",
            "tested_with": [],
        },
    }
    fork["known_bad"] = {"2.22.0": "crashes on start under Wine"}
    return data


@pytest.fixture
def manifest() -> Manifest:
    return Manifest(_data())


def _asset(version: str, kind: str = "Full") -> FeedAsset:
    return FeedAsset(
        version=version,
        type=kind,
        filename=f"Fork-{version}-{kind.lower()}.nupkg",
        sha256="d" * 64,
        sha1=None,
        size=1000,
    )


# -- plan -------------------------------------------------------------------------------


def test_plan_default_is_pinned(manifest: Manifest) -> None:
    plan = fi.plan(manifest)
    assert plan == InstallPlan(
        version="2.23.2",
        url="https://cdn.fork.dev/win/Fork-2.23.2.exe",
        sha256=PINNED_SHA,
        size=76278256,
        tofu=False,
        source="default",
    )
    assert plan.file_name == "Fork-2.23.2.exe"


def test_plan_default_with_pending_sha_needs_allow_untested() -> None:
    data = _data()
    data["fork"]["default"] = "2.24.0"
    manifest = Manifest(data)
    with pytest.raises(UsageError, match="not pinned") as info:
        fi.plan(manifest)
    assert "--allow-untested" in info.value.hint
    plan = fi.plan(manifest, allow_untested=True)
    assert (plan.version, plan.sha256, plan.size, plan.tofu, plan.source) == ("2.24.0", None, 77000000, True, "default")


@pytest.mark.parametrize("requested", ["2.23.2", "v2.23.2", "2.23.2.0", " 2.23.2 "])
def test_plan_requested_known_version_is_pinned_and_canonical(manifest: Manifest, requested: str) -> None:
    plan = fi.plan(manifest, requested=requested)
    assert (plan.version, plan.url, plan.sha256, plan.tofu, plan.source) == (
        "2.23.2",
        "https://cdn.fork.dev/win/Fork-2.23.2.exe",
        PINNED_SHA,
        False,
        "requested",
    )


def test_plan_requested_unknown_needs_allow_untested(manifest: Manifest) -> None:
    with pytest.raises(UsageError) as info:
        fi.plan(manifest, requested="2.30.1")
    assert "--allow-untested" in info.value.hint
    plan = fi.plan(manifest, requested="2.30.1", allow_untested=True)
    assert plan == InstallPlan(
        version="2.30.1",
        url="https://cdn.fork.dev/win/Fork-2.30.1.exe",
        sha256=None,
        size=None,
        tofu=True,
        source="requested",
    )


def test_plan_requested_pending_keeps_known_size(manifest: Manifest) -> None:
    with pytest.raises(UsageError):
        fi.plan(manifest, requested="2.24.0")
    plan = fi.plan(manifest, requested="2.24.0", allow_untested=True)
    assert (plan.sha256, plan.size, plan.tofu) == (None, 77000000, True)


@pytest.mark.parametrize(
    ("requested", "reason"),
    [("2.22.0", "crashes on start under Wine"), ("2.21.0", "marked known-bad")],
)
def test_plan_refuses_known_bad(manifest: Manifest, requested: str, reason: str) -> None:
    with pytest.raises(UsageError, match=reason) as info:
        fi.plan(manifest, requested=requested, allow_untested=True)
    assert "2.23.2" in info.value.hint


@pytest.mark.parametrize("requested", ["abc", "", "../2.23.2", 123])
def test_plan_rejects_invalid_versions(manifest: Manifest, requested: Any) -> None:
    with pytest.raises(UsageError, match="not a Fork version"):
        fi.plan(manifest, requested=requested, allow_untested=True)


def test_plan_rejects_unplain_unknown_versions(manifest: Manifest) -> None:
    with pytest.raises(UsageError, match="not a Fork version"):
        fi.plan(manifest, requested="2.30.0-beta", allow_untested=True)


def test_plan_requested_and_latest_conflict(manifest: Manifest) -> None:
    with pytest.raises(UsageError, match="not both"):
        fi.plan(manifest, requested="2.23.2", latest=True)


def test_plan_latest_is_tofu_for_unknown_versions(manifest: Manifest) -> None:
    assets = [_asset("2.23.2"), _asset("2.25.0"), _asset("2.26.0", "Delta"), _asset("2.24.0")]
    plan = fi.plan(manifest, latest=True, feed_assets=assets)
    assert plan == InstallPlan(
        version="2.25.0",
        url="https://cdn.fork.dev/win/Fork-2.25.0.exe",
        sha256=None,
        size=None,
        tofu=True,
        source="latest",
    )


def test_plan_latest_known_version_stays_pinned(manifest: Manifest) -> None:
    plan = fi.plan(manifest, latest=True, feed_assets=[_asset("2.23.2"), _asset("2.20.0")])
    assert (plan.version, plan.sha256, plan.tofu, plan.source) == ("2.23.2", PINNED_SHA, False, "latest")


@pytest.mark.parametrize("assets", [None, [], [_asset("2.25.0", "Delta")]])
def test_plan_latest_without_full_package(manifest: Manifest, assets: Any) -> None:
    with pytest.raises(IntegrityFailed, match="no full package"):
        fi.plan(manifest, latest=True, feed_assets=assets)


def test_plan_latest_known_bad(manifest: Manifest) -> None:
    with pytest.raises(UsageError, match="crashes on start"):
        fi.plan(manifest, latest=True, feed_assets=[_asset("2.22.0")])


# -- download_installer ------------------------------------------------------------------


class _FakeFetch:
    """Stands in for download.fetch: records the call and writes ``body`` into the cache."""

    def __init__(self, body: bytes = b"MZ" + b"\0" * 64, size: int | None = None) -> None:
        self.body = body
        self.size = size
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def __call__(self, url: str, dest_name: str, **kwargs: Any) -> Path:
        self.calls.append(((url, dest_name), kwargs))
        dest = Path(kwargs["cache_dir"]) / dest_name
        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(dest, "wb") as handle:
            handle.write(self.body)
            if self.size is not None:
                handle.truncate(self.size)
        return dest


@pytest.fixture
def fake_fetch(monkeypatch: pytest.MonkeyPatch) -> _FakeFetch:
    fake = _FakeFetch()
    monkeypatch.setattr(fi.download, "fetch", fake)
    return fake


def test_download_installer_passes_pins(manifest: Manifest, tmp_path: Path, fake_fetch: _FakeFetch) -> None:
    paths = make_paths(tmp_path)
    plan = fi.plan(manifest)

    def progress(done: int, total: int | None) -> None:
        return None

    path = fi.download_installer(plan, manifest, paths, offline=True, progress=progress)
    assert path == paths.downloads_dir / "Fork-2.23.2.exe"
    ((args, kwargs),) = fake_fetch.calls
    assert args == ("https://cdn.fork.dev/win/Fork-2.23.2.exe", "Fork-2.23.2.exe")
    assert kwargs == {
        "cache_dir": paths.downloads_dir,
        "sha256": PINNED_SHA,
        "size": 76278256,
        "allowed_hosts": manifest.allowed_hosts,
        "max_size": 300 * 1024 * 1024,
        "offline": True,
        "progress": progress,
    }


def test_download_installer_rejects_non_pe(manifest: Manifest, tmp_path: Path, fake_fetch: _FakeFetch) -> None:
    fake_fetch.body = b"<html>not an exe</html>"
    paths = make_paths(tmp_path)
    with pytest.raises(IntegrityFailed, match="no MZ header") as info:
        fi.download_installer(fi.plan(manifest), manifest, paths)
    assert info.value.exit_code == ExitCode.INTEGRITY_FAILED
    assert not (paths.downloads_dir / "Fork-2.23.2.exe").exists()


@pytest.mark.parametrize(
    ("size", "ok"),
    [(1024, False), (20 * 1024 * 1024 - 1, False), (20 * 1024 * 1024, True), (76 * 1024 * 1024, True),
     (300 * 1024 * 1024, True), (300 * 1024 * 1024 + 1, False)],
)
def test_download_installer_tofu_size_sanity(
    manifest: Manifest, tmp_path: Path, fake_fetch: _FakeFetch, size: int, ok: bool
) -> None:
    fake_fetch.size = size
    paths = make_paths(tmp_path)
    plan = fi.plan(manifest, requested="2.30.1", allow_untested=True)
    target = paths.downloads_dir / "Fork-2.30.1.exe"
    if ok:
        assert fi.download_installer(plan, manifest, paths) == target
        assert fake_fetch.calls[0][1]["sha256"] is None
    else:
        with pytest.raises(IntegrityFailed, match="implausible size"):
            fi.download_installer(plan, manifest, paths)
        assert not target.exists()


def test_download_installer_pinned_skips_size_window(
    manifest: Manifest, tmp_path: Path, fake_fetch: _FakeFetch
) -> None:
    fake_fetch.size = 1024
    assert fi.download_installer(fi.plan(manifest), manifest, make_paths(tmp_path)).stat().st_size == 1024


@pytest.mark.parametrize(
    "plan",
    [
        InstallPlan("2.23.2", "https://evil.example/Fork-2.23.2.exe", None, None, True, "requested"),
        InstallPlan("2.23.2", "https://cdn.fork.dev/win/Fork-2.23.3.exe", None, None, True, "requested"),
        InstallPlan("../x", "https://cdn.fork.dev/win/Fork-../x.exe", None, None, True, "requested"),
    ],
)
def test_download_installer_refuses_foreign_urls(
    manifest: Manifest, tmp_path: Path, fake_fetch: _FakeFetch, plan: InstallPlan
) -> None:
    with pytest.raises(IntegrityFailed, match="refusing installer URL"):
        fi.download_installer(plan, manifest, make_paths(tmp_path))
    assert fake_fetch.calls == []


# -- run_installer --------------------------------------------------------------------------


@pytest.fixture
def installer(tmp_path: Path) -> Path:
    path = tmp_path / "cache" / "Fork-2.23.2.exe"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"MZ")
    return path


def test_run_installer_runs_silent_install(installer: Path, tmp_path: Path) -> None:
    runner = RecordingRunner({"wine": 0})
    env = {"WINEPREFIX": str(tmp_path / "prefix"), "WINEDEBUG": "-all"}
    wine = tmp_path / "runtime" / "bin" / "wine"
    fi.run_installer(runner, env, wine, installer, PathMap.with_drives({"z": "/"}))
    (call,) = runner.calls
    assert call["argv"] == [str(wine), "Z:" + str(installer).replace("/", "\\"), "--silent"]
    assert call["env"] == env
    assert call["cwd"] == installer.parent
    assert call["timeout"] == 900
    assert call["log_file"] is None


def test_run_installer_failure_is_a_setup_failure(installer: Path, tmp_path: Path) -> None:
    runner = RecordingRunner({"wine": Completed([], 3, "", "line1\nwine: installer crashed\n")})
    log_file = tmp_path / "logs" / "wine.log"
    with pytest.raises(SetupFailed) as info:
        fi.run_installer(
            runner, {}, Path("/w/wine"), installer, PathMap.with_drives({"z": "/"}), timeout=5, log_file=log_file
        )
    assert info.value.step == "fork_install"
    assert "exited with code 3" in info.value.message
    assert runner.calls[0]["timeout"] == 5
    assert str(log_file) in info.value.hint


def test_run_installer_failure_with_stderr(installer: Path) -> None:
    runner = RecordingRunner({"wine": Completed([], 3, "", "wine: installer crashed\n")})
    with pytest.raises(SetupFailed, match="installer crashed") as info:
        fi.run_installer(runner, {}, Path("/w/wine"), installer, PathMap.with_drives({"z": "/"}))
    assert info.value.hint == "run 'fork-linux setup' again to retry"


def test_run_installer_failure_without_output(installer: Path) -> None:
    runner = RecordingRunner({"wine": 1})
    with pytest.raises(SetupFailed) as info:
        fi.run_installer(runner, {}, Path("/w/wine"), installer, PathMap.with_drives({"z": "/"}))
    assert info.value.message.endswith("the Fork installer exited with code 1")


def test_run_installer_timeout_becomes_setup_failure(installer: Path) -> None:
    def hang(argv: list[str]) -> None:
        raise ForkLinuxError("command timed out after 900s: wine")

    runner = RecordingRunner({"wine": hang})
    with pytest.raises(SetupFailed, match="did not finish: command timed out") as info:
        fi.run_installer(runner, {}, Path("/w/wine"), installer, PathMap.with_drives({"z": "/"}))
    assert info.value.exit_code == ExitCode.SETUP_FAILED


def test_run_installer_unreachable_path(installer: Path) -> None:
    with pytest.raises(UsageError, match="not reachable"):
        fi.run_installer(RecordingRunner(), {}, Path("/w/wine"), installer, PathMap.with_drives({"c": "/nowhere"}))


# -- finish_install -------------------------------------------------------------------------


def _plan(version: str = "2.23.2") -> InstallPlan:
    return InstallPlan(version, f"https://cdn.fork.dev/win/Fork-{version}.exe", None, None, False, "default")


def test_finish_install_removes_desktop_shortcut(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    install_fork(layout)
    layout.desktop_lnk.parent.mkdir(parents=True)
    layout.desktop_lnk.write_bytes(b"L\0\0\0")
    layout.startmenu_lnk.parent.mkdir(parents=True)
    layout.startmenu_lnk.write_bytes(b"L\0\0\0")
    assert fi.finish_install(layout, _plan()) == "2.23.2"
    assert not layout.desktop_lnk.exists()
    assert layout.startmenu_lnk.exists()


def test_finish_install_without_shortcut(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    write_sq_version(layout, "2.23.2.0")
    assert fi.finish_install(layout, _plan("2.23.2")) == "2.23.2.0"


def test_finish_install_never_follows_symlinks(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    install_fork(layout)
    real = tmp_path / "linux-desktop"
    real.mkdir()
    victim = real / "Fork.lnk"
    victim.write_bytes(b"L")
    layout.desktop_lnk.parent.mkdir(parents=True)
    layout.desktop_lnk.symlink_to(victim)
    fi.finish_install(layout, _plan())
    assert victim.exists() and layout.desktop_lnk.is_symlink()


def test_finish_install_keeps_shortcut_outside_prefix(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    install_fork(layout)
    real = tmp_path / "home" / "Desktop"
    real.mkdir(parents=True)
    (real / "Fork.lnk").write_bytes(b"L")
    layout.desktop_lnk.parent.parent.mkdir(parents=True, exist_ok=True)
    layout.desktop_lnk.parent.symlink_to(real)
    fi.finish_install(layout, _plan())
    assert (real / "Fork.lnk").exists()


def test_finish_install_keeps_non_files(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    install_fork(layout)
    layout.desktop_lnk.mkdir(parents=True)
    fi.finish_install(layout, _plan())
    assert layout.desktop_lnk.is_dir()


@pytest.mark.parametrize("installed", [None, "2.20.0"])
def test_finish_install_detects_failed_install(tmp_path: Path, installed: str | None) -> None:
    layout = make_layout(tmp_path)
    if installed is not None:
        write_sq_version(layout, installed)
    with pytest.raises(SetupFailed, match="was not installed") as info:
        fi.finish_install(layout, _plan())
    assert info.value.step == "fork_install"


# -- verify_nupkg -------------------------------------------------------------------------------


def test_verify_nupkg(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    install_fork(layout)
    package = layout.packages_dir / "Fork-2.23.2-full.nupkg"
    digest = hashlib.sha256(package.read_bytes()).hexdigest()
    assert fi.verify_nupkg(layout, "2.23.2", digest) is True
    assert fi.verify_nupkg(layout, "2.23.2", f" {digest.upper()} ") is True
    assert fi.verify_nupkg(layout, "2.23.2", "0" * 64) is False
    assert fi.verify_nupkg(layout, "2.24.0", digest) is False
    assert fi.verify_nupkg(layout, "../2.23.2", digest) is False
    assert fi.verify_nupkg(layout, None, digest) is False


def test_verify_nupkg_refuses_symlinks(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    install_fork(layout)
    real = tmp_path / "elsewhere.nupkg"
    real.write_bytes(b"PK")
    package = layout.packages_dir / "Fork-2.25.0-full.nupkg"
    package.symlink_to(real)
    assert fi.verify_nupkg(layout, "2.25.0", hashlib.sha256(b"PK").hexdigest()) is False
