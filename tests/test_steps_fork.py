"""Tests for the fork_download, fork_install and fork_settings setup steps."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from pathlib import Path
from typing import Any

import pytest
from fixtures.fork_tree import install_fork, write_settings
from fixtures.setup_ctx import make_ctx

from fork_linux import bootstrap, resources
from fork_linux import fork_settings as fork_settings_mod
from fork_linux.bootstrap import Ctx
from fork_linux.errors import (
    DownloadFailed,
    ForkLinuxError,
    IntegrityFailed,
    SetupFailed,
    UsageError,
)
from fork_linux.fork_install import InstallPlan
from fork_linux.procrun import RecordingRunner
from fork_linux.steps import fork as fork_steps

DEFAULT_SHA = "fee9b2bf84aca6297d7b7e10b29a09c624ac16a82486f2c04136f5ebaf8f079e"


def _nupkg_sha(version: str) -> str:
    return hashlib.sha256(b"PK placeholder " + version.encode()).hexdigest()


def _feed(*entries: tuple[str, str]) -> str:
    assets = [
        {
            "PackageId": "Fork",
            "Version": version,
            "Type": "Full",
            "FileName": f"Fork-{version}-full.nupkg",
            "SHA256": sha.upper(),
            "Size": 70_000_000,
        }
        for version, sha in entries
    ]
    return json.dumps({"Assets": assets})


@pytest.fixture
def feeds(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """URL -> feed text (or an exception); every fetch is recorded under ``calls``."""
    table: dict[str, Any] = {"calls": []}

    def fetch(url: str, cache_dir: Path, **kwargs: Any) -> str:
        table["calls"].append((url, kwargs.get("offline")))
        value = table.get(url)
        if value is None:
            raise DownloadFailed(f"no feed at {url}")
        if isinstance(value, BaseException):
            raise value
        return value

    monkeypatch.setattr(fork_steps.feeds, "fetch_feed", fetch)
    return table


@pytest.fixture
def downloads(monkeypatch: pytest.MonkeyPatch) -> list[InstallPlan]:
    """Fake installer downloads: an ``MZ`` file in the download cache."""
    seen: list[InstallPlan] = []

    def download(plan: InstallPlan, manifest: Any, paths: Any, **kwargs: Any) -> Path:
        seen.append(plan)
        path = paths.downloads_dir / plan.file_name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"MZ installer " + plan.version.encode())
        return path

    monkeypatch.setattr(fork_steps.fork_install, "download_installer", download)
    return seen


def _drives(ctx: Ctx) -> None:
    dosdevices = ctx.paths.prefix / "dosdevices"
    dosdevices.mkdir(parents=True, exist_ok=True)
    if not os.path.lexists(dosdevices / "c:"):
        (dosdevices / "c:").symlink_to("../drive_c")
        (dosdevices / "z:").symlink_to("/")


def _installer_runner(ctx: Ctx, *, rc: int = 0) -> RecordingRunner:
    def wine(argv: list[str]) -> int:
        if argv[-1] == "--silent" and rc == 0:
            version = Path(argv[1].replace("\\", "/")).stem.split("-", 1)[1]
            install_fork(ctx.layout, version)
            ctx.layout.desktop_lnk.parent.mkdir(parents=True, exist_ok=True)
            ctx.layout.desktop_lnk.write_bytes(b"lnk")
        return rc

    return RecordingRunner({"wine": wine})


# -- request ---------------------------------------------------------------------------------


def test_request_sources(xdg: Path) -> None:
    ctx = make_ctx()
    assert fork_steps.request(ctx) == {"source": "default"}
    assert fork_steps.inputs(ctx) == {"source": "default"}
    ctx.state.set(fork_steps.REQUEST_KEY, {"source": "requested", "version": "2.22.0"})
    assert fork_steps.request(ctx) == {"source": "requested", "version": "2.22.0"}
    ctx.state.set(fork_steps.REQUEST_KEY, {"source": "latest"})
    assert fork_steps.request(ctx) == {"source": "latest"}
    ctx.state.set(fork_steps.REQUEST_KEY, {"source": "requested", "version": 7})
    assert fork_steps.request(ctx) == {"source": "default"}
    ctx.fork_version = " 2.21.0 "
    assert fork_steps.request(ctx) == {"source": "requested", "version": "2.21.0"}
    ctx.latest = True
    assert fork_steps.request(ctx) == {"source": "latest"}


def test_request_from_config_channel(xdg: Path) -> None:
    ctx = make_ctx(env={**os.environ, "FORK_LINUX_FORK_CHANNEL": "latest"})
    assert fork_steps.request(ctx) == {"source": "latest"}


def test_describe_request(xdg: Path) -> None:
    ctx = make_ctx()
    assert fork_steps.describe_request(ctx) == ("2.23.2", 76278256)
    ctx.fork_version = "2.24.0"
    assert fork_steps.describe_request(ctx) == ("2.24.0", None)
    ctx.latest = True
    assert fork_steps.describe_request(ctx)[1] is None


# -- feeds and plans -------------------------------------------------------------------------


def test_feed_assets(xdg: Path, feeds: dict[str, Any]) -> None:
    ctx = make_ctx(offline=True)
    feeds[ctx.manifest.feed_url] = _feed(("2.23.2", _nupkg_sha("2.23.2")))
    assets = fork_steps.feed_assets(ctx)
    assert [asset.version for asset in assets] == ["2.23.2"]
    assert feeds["calls"] == [(ctx.manifest.feed_url, True)]
    feeds[ctx.manifest.legacy_feed_url] = "52376CB9A78706DF0E62CE21E9CC0BB9116C838D Fork-2.23.2-full.nupkg 73531413\n"
    legacy = fork_steps.feed_assets(ctx, legacy=True)
    assert legacy[0].sha256 is None
    feeds[ctx.manifest.feed_url] = "not json"
    assert fork_steps.feed_assets(ctx) is None


def test_plan_default_is_pinned_and_cached(xdg: Path, feeds: dict[str, Any]) -> None:
    ctx = make_ctx()
    first = fork_steps.plan(ctx)
    assert (first.version, first.sha256, first.tofu, first.source) == ("2.23.2", DEFAULT_SHA, False, "default")
    assert fork_steps.plan(ctx) is first
    assert feeds["calls"] == []


def test_plan_requested_untested_needs_permission(xdg: Path) -> None:
    ctx = make_ctx(fork_version="2.24.0")
    with pytest.raises(UsageError, match="not pinned"):
        fork_steps.plan(ctx)
    ctx.allow_untested = True
    assert fork_steps.plan(ctx).tofu


def test_plan_latest_from_feed_then_legacy(xdg: Path, feeds: dict[str, Any]) -> None:
    ctx = make_ctx(latest=True)
    feeds[ctx.manifest.legacy_feed_url] = "52376CB9A78706DF0E62CE21E9CC0BB9116C838D Fork-2.24.0-full.nupkg 73531413\n"
    chosen = fork_steps.plan(ctx)
    assert (chosen.version, chosen.tofu, chosen.source) == ("2.24.0", True, "latest")
    assert [url for url, _offline in feeds["calls"]] == [ctx.manifest.feed_url, ctx.manifest.legacy_feed_url]


def test_plan_latest_without_any_feed(xdg: Path, feeds: dict[str, Any]) -> None:
    with pytest.raises(DownloadFailed, match="release feed"):
        fork_steps.plan(make_ctx(latest=True))


# -- fork_download ---------------------------------------------------------------------------


def test_download_records_the_plan(xdg: Path, downloads: list[InstallPlan]) -> None:
    ctx = make_ctx()
    assert not fork_steps.verify_download(ctx)
    fork_steps.run_download(ctx)
    assert downloads[0].version == "2.23.2"
    recorded = ctx.state.get(fork_steps.PLAN_KEY)
    assert recorded["sha256"] == DEFAULT_SHA and recorded["request"] == {"source": "default"}
    assert ctx.state.get(fork_steps.REQUEST_KEY) is None
    assert fork_steps.verify_download(ctx)
    (ctx.paths.downloads_dir / "Fork-2.23.2.exe").unlink()
    assert not fork_steps.verify_download(ctx)


def test_download_of_an_unpinned_version_records_its_hash(xdg: Path, downloads: list[InstallPlan]) -> None:
    ctx = make_ctx(fork_version="2.24.0", allow_untested=True)
    fork_steps.run_download(ctx)
    recorded = ctx.state.get(fork_steps.PLAN_KEY)
    expected = hashlib.sha256(b"MZ installer 2.24.0").hexdigest()
    assert recorded["sha256"] == expected and recorded["tofu"] is True
    assert recorded["size"] == len(b"MZ installer 2.24.0")
    assert ctx.state.get(fork_steps.REQUEST_KEY) == {"source": "requested", "version": "2.24.0"}


def test_download_skipped_when_fork_is_installed(xdg: Path, downloads: list[InstallPlan]) -> None:
    ctx = make_ctx()
    install_fork(ctx.layout, "2.25.0")
    fork_steps.run_download(ctx)
    assert downloads == [] and fork_steps.verify_download(ctx)
    ctx.force = True
    assert not fork_steps.satisfied(ctx)


def test_satisfied_for_a_requested_version(xdg: Path) -> None:
    ctx = make_ctx(fork_version="2.23.2")
    assert not fork_steps.satisfied(ctx)
    install_fork(ctx.layout, "2.23.2")
    assert fork_steps.satisfied(ctx)
    ctx.fork_version = "2.22.0"
    assert not fork_steps.satisfied(ctx)
    ctx.fork_version = "garbage"
    assert not fork_steps.satisfied(ctx)


def test_recorded_plan_must_match_the_request(xdg: Path) -> None:
    ctx = make_ctx()
    ctx.state.set(fork_steps.PLAN_KEY, "broken")
    assert fork_steps._recorded_plan(ctx) is None
    ctx.state.set(fork_steps.PLAN_KEY, {"request": {"source": "default"}, "version": "2.23.2"})
    assert fork_steps._recorded_plan(ctx) is None
    ctx.state.set(fork_steps.PLAN_KEY, {"request": {"source": "latest"}})
    assert fork_steps._recorded_plan(ctx) is None


# -- fork_install ----------------------------------------------------------------------------


def test_install_runs_the_official_installer(xdg: Path, downloads: list[InstallPlan], feeds: dict[str, Any]) -> None:
    ctx = make_ctx()
    _drives(ctx)
    fork_steps.run_download(ctx)
    ctx.runner = _installer_runner(ctx)
    assert not fork_steps.verify_install(ctx)
    fork_steps.run_install(ctx)
    wine_call = ctx.runner.calls[0]
    assert wine_call["argv"][2] == "--silent"
    assert wine_call["argv"][1].startswith("Z:\\") and wine_call["argv"][1].endswith("Fork-2.23.2.exe")
    assert ctx.runner.calls[1]["argv"][1:] == ["-w"]
    assert fork_steps.verify_install(ctx)
    assert ctx.state.get("fork.version") == "2.23.2" and ctx.state.get("fork.source") == "default"
    assert ctx.state.get("fork.installer_sha256") == DEFAULT_SHA
    assert isinstance(ctx.state.get("fork.installed_at"), str)
    assert not ctx.layout.desktop_lnk.exists()
    assert feeds["calls"] == []
    # Fork updated itself: nothing is reinstalled, the new version is recorded.
    install_fork(ctx.layout, "2.26.0")
    calls = len(ctx.runner.calls)
    fork_steps.run_install(ctx)
    assert len(ctx.runner.calls) == calls and ctx.state.get("fork.version") == "2.26.0"


def test_install_failure(xdg: Path, downloads: list[InstallPlan]) -> None:
    ctx = make_ctx()
    _drives(ctx)
    ctx.runner = _installer_runner(ctx, rc=3)
    with pytest.raises(SetupFailed, match="exited with code 3"):
        fork_steps.run_install(ctx)


def _tofu_ctx(feeds: dict[str, Any], sha: str | None) -> Ctx:
    ctx = make_ctx(fork_version="2.24.0", allow_untested=True)
    _drives(ctx)
    if sha is not None:
        feeds[ctx.manifest.feed_url] = _feed(("2.24.0", sha), ("2.23.2", _nupkg_sha("2.23.2")))
    ctx.runner = _installer_runner(ctx)
    return ctx


def test_install_tofu_checks_the_feed(xdg: Path, downloads: list[InstallPlan], feeds: dict[str, Any]) -> None:
    ctx = _tofu_ctx(feeds, _nupkg_sha("2.24.0"))
    fork_steps.run_install(ctx)
    assert ctx.state.get("fork.version") == "2.24.0" and ctx.state.get("fork.source") == "requested"
    assert ctx.ui.kinds("warn") == []


def test_install_tofu_mismatch_is_an_integrity_failure(
    xdg: Path, downloads: list[InstallPlan], feeds: dict[str, Any]
) -> None:
    ctx = _tofu_ctx(feeds, "0" * 64)
    with pytest.raises(IntegrityFailed, match="does not match"):
        fork_steps.run_install(ctx)


def test_install_tofu_without_feed_warns(xdg: Path, downloads: list[InstallPlan], feeds: dict[str, Any]) -> None:
    ctx = _tofu_ctx(feeds, None)
    fork_steps.run_install(ctx)
    assert "could not be checked" in ctx.ui.kinds("warn")[0]


# -- fork_settings ---------------------------------------------------------------------------


def test_settings_are_seeded_before_the_first_start(xdg: Path) -> None:
    # E2E (Fork 2.23.2): a seeded settings.json is kept by Fork, so the step creates it.
    ctx = make_ctx(RecordingRunner())
    install_fork(ctx.layout)
    assert not fork_steps.verify_settings(ctx)
    fork_steps.run_settings(ctx)
    assert fork_steps.verify_settings(ctx)
    data = json.loads(ctx.layout.settings_file.read_text())
    assert str(uuid.UUID(data["Guid"])) == data["Guid"]
    assert data["UpdateSubmodulesOnCheckout"] is False and data["DisableHardwareAcceleration"] is True
    assert "settings_present" not in fork_steps.settings_inputs(ctx)
    assert not list(fork_settings_mod.default_backup_dir(ctx.paths).glob("settings.json.*"))


def test_settings_are_merged(xdg: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    shims = tmp_path / "shims"
    shims.mkdir()
    (shims / "fl-launch.exe").write_bytes(b"MZ")
    monkeypatch.setenv(resources.SHIMS_ENV, str(shims))
    ctx = make_ctx(RecordingRunner(), env={**os.environ, "GTK_THEME": "Adwaita:dark", "GDK_SCALE": "1.25"})
    _drives(ctx)
    install_fork(ctx.layout)
    write_settings(ctx.layout, {"Guid": "x", "UpdateSubmodulesOnCheckout": True, "ShellTool": None,
                                "RepositoryManager": {"SourceDirectories": [f"C:\\users\\{ctx.user}"]},
                                "Unknown": 1})
    assert fork_steps.settings_inputs(ctx)["shell_tool"]
    fork_steps.run_settings(ctx)
    data = json.loads(ctx.layout.settings_file.read_text())
    assert data["UpdateSubmodulesOnCheckout"] is False and data["DisableHardwareAcceleration"] is True
    assert data["Theme"] == 1 and data["FollowSystemTheme"] is False and data["LayoutScaling"] == 125
    assert data["ShellTool"] == fork_steps.SHELL_TOOL and data["Unknown"] == 1
    assert data["RepositoryManager"]["SourceDirectories"] == ["Z:" + str(ctx.host_home).replace("/", "\\")]
    assert fork_steps.verify_settings(ctx)
    assert list(fork_settings_mod.default_backup_dir(ctx.paths).iterdir())
    data["DisableHardwareAcceleration"] = False
    ctx.layout.settings_file.write_text(json.dumps(data))
    assert not fork_steps.verify_settings(ctx)
    ctx.layout.settings_file.write_text("{broken")
    assert not fork_steps.verify_settings(ctx)


def test_settings_without_shims_or_drives(xdg: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(resources.SHIMS_ENV, str(tmp_path / "none"))
    ctx = make_ctx(RecordingRunner())
    install_fork(ctx.layout)
    write_settings(ctx.layout, {"ShellTool": None})
    assert fork_steps.shell_tool() is None
    assert fork_steps._home_win(ctx) is None
    fork_steps.run_settings(ctx)
    assert json.loads(ctx.layout.settings_file.read_text())["ShellTool"] is None
    # A later context has nothing cached: a loadable file verifies.
    later = make_ctx(RecordingRunner())
    assert fork_steps.verify_settings(later)


def test_bootstrap_resumes_install_from_the_recorded_plan(
    xdg: Path, downloads: list[InstallPlan], feeds: dict[str, Any]
) -> None:
    ctx = make_ctx(latest=True)
    feeds[ctx.manifest.feed_url] = _feed(("2.24.0", _nupkg_sha("2.24.0")))
    bootstrap.init_prefix_meta(ctx)
    bootstrap.run_step(ctx, fork_steps.FORK_DOWNLOAD)
    # A new run (no per-run cache, no flags): the recorded request and plan are used.
    later = make_ctx()
    _drives(later)
    later.runner = _installer_runner(later)
    assert later.state.get(fork_steps.REQUEST_KEY) == {"source": "latest"}
    assert bootstrap.is_done(fork_steps.FORK_DOWNLOAD, later)
    plan = fork_steps._current_plan(later)
    assert plan.version == "2.24.0" and plan.sha256 == hashlib.sha256(b"MZ installer 2.24.0").hexdigest()
    bootstrap.run_step(later, fork_steps.FORK_INSTALL)
    assert later.state.get("fork.version") == "2.24.0"
    assert downloads[-1].sha256 == plan.sha256


def test_shell_tool_needs_fl_launch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(resources.SHIMS_ENV, str(tmp_path))
    assert fork_steps.shell_tool() is None
    (tmp_path / "fl-launch.exe").write_bytes(b"MZ")
    tool = fork_steps.shell_tool()
    assert tool == fork_steps.SHELL_TOOL and tool is not fork_steps.SHELL_TOOL


def test_errors_module_unchanged() -> None:
    assert issubclass(IntegrityFailed, ForkLinuxError)
