"""Tests for ``fork-linux update --check | --fork | --wine``."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest

from fork_linux import bootstrap, feeds, fork_install, snapshots, updates, wine_provider
from fork_linux.commands import update as update_cmd
from fork_linux.errors import DownloadFailed, ForkLinuxError
from fork_linux.procrun import Completed, RecordingRunner
from fork_linux.state import FORK_VERSION, WINE_BUILD

from fixtures.cli_run import fork_running, isolate, layout, run_cli, run_json
from fixtures.fork_tree import install_fork, write_settings
from fixtures.setup_ctx import make_ctx


@pytest.fixture(autouse=True)
def _isolated(xdg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    isolate(monkeypatch)
    fork_running(monkeypatch, False)


def _feed(*entries: tuple[str, str]) -> str:
    """A releases.win.json document listing full packages ``(version, sha256)``."""
    return json.dumps(
        {
            "Assets": [
                {
                    "PackageId": "Fork",
                    "Version": version,
                    "Type": "Full",
                    "FileName": f"Fork-{version}-full.nupkg",
                    "SHA256": sha,
                    "Size": 100,
                }
                for version, sha in entries
            ]
        }
    )


@pytest.fixture
def feed(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Serve a feed from memory (``feed[0]`` is the document); records the offline flag."""
    doc = [_feed(("2.23.2", "a" * 64), ("2.24.0", "b" * 64))]
    seen: list[bool] = []

    def fetch(url: str, cache_dir: Path, *, offline: bool = False, validate: Any = None) -> str:
        seen.append(offline)
        if validate is not None:
            validate(doc[0])
        return doc[0]

    monkeypatch.setattr(feeds, "fetch_feed", fetch)
    doc.append(seen)  # feed[1]: the offline flag of every fetch
    return doc


# -- --check ------------------------------------------------------------------------------------


def test_check_offline_without_a_cache(capsys: pytest.CaptureFixture[str]) -> None:
    install_fork(layout(), "2.23.2")
    code, out, _err = run_cli(capsys, "--offline", "update")
    assert code == 0
    assert "Installed: 2.23.2" in out
    assert "Tested:    2.23.2" in out
    assert "Newest:    unknown (no cached copy" in out
    assert "Updates:   automatic" in out
    info = run_json(capsys, "--offline", "update", "--check")
    assert info["feed_error"].startswith("no cached copy")
    assert info["latest"] is None
    assert info["update_policy"] == "auto"


def test_check_with_a_feed(
    capsys: pytest.CaptureFixture[str], feed: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    install_fork(layout(), "2.23.2")
    layout().packages_dir.joinpath("Fork-2.24.0-full.nupkg").write_bytes(b"PK")
    monkeypatch.setenv("FORK_LINUX_FORK_UPDATE_POLICY", "pinned")
    code, out, _err = run_cli(capsys, "update", "--check")
    assert "Newest:    2.24.0" in out
    assert "Staged:    2.24.0" in out
    assert "pinned" in out
    assert "'fork-linux update --fork --latest' installs it" in out
    info = run_json(capsys, "--offline", "update")
    assert info["latest"] == "2.24.0"
    assert info["feed_error"] is None
    assert feed[1] == [False, True]
    assert "File > Check for Updates" not in out


def test_check_shows_forks_own_channel(capsys: pytest.CaptureFixture[str], feed: list[Any]) -> None:
    """Fork's ApplicationUpdateType is reported; on develop the in-app route is offered (spike S9)."""
    install_fork(layout(), "2.23.2")
    code, out, _err = run_cli(capsys, "update", "--check")
    assert "Channel:   develop (Fork's own updater)" in out
    assert "File > Check for Updates..., then Restart and Update" in out
    write_settings(layout(), {"ApplicationUpdateType": 1})
    code, out, _err = run_cli(capsys, "update", "--check")
    assert "Channel:   stable" in out
    assert "File > Check for Updates" not in out
    layout().settings_file.write_text("{broken", encoding="utf-8")
    assert run_json(capsys, "update", "--check")["fork_channel"] == "unknown"


def test_check_warns_about_known_bad(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    info = {
        "installed": None,
        "default": "2.23.2",
        "latest": None,
        "known_good": False,
        "known_bad": "breaks rendering",
        "update_available": False,
        "staged": [],
    }
    monkeypatch.setattr(updates, "check", lambda manifest, layout, assets: dict(info))
    monkeypatch.setattr(update_cmd, "_feed_assets", lambda *a, **kw: [])
    out = run_cli(capsys, "update")[1]
    assert "Installed: not installed" in out
    assert "Newest:    unknown\n" in out
    assert "known not to work well: breaks rendering" in out
    assert "is available" not in out


def test_version_flags_need_fork(capsys: pytest.CaptureFixture[str]) -> None:
    for flags in (["--latest"], ["--fork-version", "2.23.2"], ["--allow-untested"], ["--wine", "--latest"]):
        code, _out, err = run_cli(capsys, "update", *flags)
        assert code == 2, flags
        assert "go with --fork" in err
    assert run_cli(capsys, "update", "--fork", "--wine")[0] == 2


# -- --fork ---------------------------------------------------------------------------------------


@pytest.fixture
def boot(xdg: Path, monkeypatch: pytest.MonkeyPatch) -> bootstrap.Ctx:
    """``update`` gets this Ctx; the installer 'installs' the planned version; steps are recorded."""
    ctx = make_ctx()
    monkeypatch.setattr(bootstrap.Ctx, "from_app", classmethod(lambda cls, app: ctx))
    ctx.cache["steps"] = []

    def run_steps(c: bootstrap.Ctx, steps: Any = None, *, only: Any = None, **_kw: Any) -> list[str]:
        ctx.cache["steps"].append(None if only is None else list(only))
        return list(only or ["all"])

    monkeypatch.setattr(bootstrap, "run_steps", run_steps)
    return ctx


@pytest.fixture
def installer(boot: bootstrap.Ctx, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[str]:
    """Fake download + silent install: records the planned versions and lays out that version."""
    installed: list[str] = []
    planned: dict[str, fork_install.InstallPlan] = {}

    def download(plan: fork_install.InstallPlan, manifest: Any, paths: Any, *, offline: bool = False) -> Path:
        planned["plan"] = plan
        path = tmp_path / plan.file_name
        path.write_bytes(b"MZ")
        return path

    def run_installer(runner: Any, env: Any, wine: Path, path: Path, pathmap: Any, **_kw: Any) -> None:
        version = planned["plan"].version
        installed.append(version)
        install_fork(boot.layout, version)

    monkeypatch.setattr(fork_install, "download_installer", download)
    monkeypatch.setattr(fork_install, "run_installer", run_installer)
    return installed


def _prefix(boot: bootstrap.Ctx) -> None:
    dosdevices = boot.paths.prefix / "dosdevices"
    dosdevices.mkdir(parents=True, exist_ok=True)
    (dosdevices / "z:").symlink_to("/")


def test_fork_needs_an_installed_fork(capsys: pytest.CaptureFixture[str], boot: bootstrap.Ctx) -> None:
    code, _out, err = run_cli(capsys, "update", "--fork")
    assert code == 10
    assert "not installed" in err


def test_fork_already_on_the_default(
    capsys: pytest.CaptureFixture[str], boot: bootstrap.Ctx, installer: list[str]
) -> None:
    install_fork(boot.layout, "2.23.2")
    code, out, _err = run_cli(capsys, "update", "--fork")
    assert (code, out) == (0, "Fork 2.23.2 is already installed\n")
    assert installer == []
    assert boot.cache["steps"] == []
    assert snapshots.list_snapshots(boot.paths) == []


def test_fork_back_to_the_tested_default(
    capsys: pytest.CaptureFixture[str], boot: bootstrap.Ctx, installer: list[str]
) -> None:
    _prefix(boot)
    install_fork(boot.layout, "2.24.0")
    boot.state.set("fork.request", {"source": "latest"})
    code, out, _err = run_cli(capsys, "update", "--fork")
    assert code == 0
    assert out.startswith("Fork 2.24.0 -> 2.23.2 (snapshot 2.24.0-")
    assert "rollback --to-version 2.24.0" in out
    assert installer == ["2.23.2"]
    assert boot.layout.installed_version() == "2.23.2"
    assert boot.state.get(FORK_VERSION) == "2.23.2"
    assert boot.state.get(updates.LAST_SEEN_VERSION) == "2.23.2"
    assert boot.state.get("fork.request") is None
    assert boot.cache["steps"] == [list(update_cmd.FORK_STEPS)]
    assert [snap.fork_version for snap in snapshots.list_snapshots(boot.paths)] == ["2.24.0"]


def test_fork_requested_untested_version_is_verified_against_the_feed(
    capsys: pytest.CaptureFixture[str],
    boot: bootstrap.Ctx,
    installer: list[str],
    feed: list[Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prefix(boot)
    install_fork(boot.layout, "2.23.2")
    monkeypatch.setenv("FORK_LINUX_FORK_UPDATE_POLICY", "pinned")
    boot.config = type(boot.config).load(boot.paths, dict(os.environ))
    code, _out, err = run_cli(capsys, "update", "--fork", "--fork-version", "2.24.0")
    assert code == 2
    assert "--allow-untested" in err
    # The fake installer writes this nupkg; the feed must describe it.
    feed[0] = _feed(("2.24.0", hashlib.sha256(b"PK placeholder 2.24.0").hexdigest()))
    result = run_json(capsys, "update", "--fork", "--fork-version", "2.24.0", "--allow-untested")
    assert result["installed"] == "2.24.0"
    assert result["changed"] is True
    assert result["steps"] == list(update_cmd.FORK_STEPS)
    assert boot.state.get("fork.request") == {"source": "requested", "version": "2.24.0"}
    assert boot.state.get(update_cmd.PINNED_VERSION) == "2.24.0"


def test_fork_tofu_mismatch_is_an_integrity_failure(
    capsys: pytest.CaptureFixture[str], boot: bootstrap.Ctx, installer: list[str], feed: list[Any]
) -> None:
    _prefix(boot)
    install_fork(boot.layout, "2.23.2")
    code, _out, err = run_cli(capsys, "update", "--fork", "--latest")
    assert code == 13
    assert "does not match Fork's update feed" in err
    assert "rollback --to-version 2.23.2" in err
    assert boot.state.get(FORK_VERSION) is None


def test_fork_tofu_without_the_version_in_the_feed(
    capsys: pytest.CaptureFixture[str], boot: bootstrap.Ctx, installer: list[str], feed: list[Any]
) -> None:
    _prefix(boot)
    install_fork(boot.layout, "2.23.2")
    feed[0] = _feed(("2.23.2", "a" * 64))
    assert run_cli(capsys, "update", "--fork", "--fork-version", "2.24.0", "--allow-untested")[0] == 13


def test_fork_latest_records_the_request(
    capsys: pytest.CaptureFixture[str],
    boot: bootstrap.Ctx,
    installer: list[str],
    feed: list[Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prefix(boot)
    install_fork(boot.layout, "2.23.2")
    monkeypatch.setattr(fork_install, "verify_nupkg", lambda layout, version, sha: sha == "b" * 64)
    result = run_json(capsys, "update", "--fork", "--latest")
    assert result["installed"] == "2.24.0"
    assert boot.state.get("fork.request") == {"source": "latest"}


def test_fork_needs_fork_closed(
    capsys: pytest.CaptureFixture[str], boot: bootstrap.Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_fork(boot.layout, "2.24.0")
    fork_running(monkeypatch, True)
    assert run_cli(capsys, "update", "--fork")[0] == 15


def test_rerun_only_known_steps(boot: bootstrap.Ctx) -> None:
    assert update_cmd._rerun(boot, ("no-such-step",)) == []
    assert update_cmd._rerun(boot, ("icon", "no-such-step")) == ["icon"]
    assert update_cmd._rerun(boot, None) == ["all"]
    assert boot.cache["steps"] == [["icon"], None]


def test_feed_assets_uses_its_own_cache(xdg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Path] = []

    def fetch(url: str, cache_dir: Path, **kwargs: Any) -> str:
        seen.append(cache_dir)
        raise DownloadFailed("offline")

    monkeypatch.setattr(feeds, "fetch_feed", fetch)
    boot = make_ctx()
    with pytest.raises(DownloadFailed):
        update_cmd._feed_assets(boot.manifest, boot.paths, offline=True)
    assert seen == [boot.paths.feeds_dir / update_cmd.FEED_CACHE]


# -- --wine ---------------------------------------------------------------------------------------


@pytest.fixture
def managed(boot: bootstrap.Ctx, monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """install_managed returns the store root (recorded)."""
    roots: list[Path] = []

    def install(build: Any, paths: Any, runner: Any, *, offline: bool = False) -> Path:
        root = wine_provider.managed_root(paths, build.id)
        roots.append(root)
        return root

    monkeypatch.setattr(wine_provider, "install_managed", install)
    return roots


def test_wine_switch(capsys: pytest.CaptureFixture[str], boot: bootstrap.Ctx, managed: list[Path]) -> None:
    old = boot.wine()
    boot.config.set("wine", "build", boot.manifest.wine_default.id)
    code, out, _err = run_cli(capsys, "update", "--wine")
    assert code == 0, out
    default = boot.manifest.wine_default.id
    assert out == f"Wine switched to {default} (previous: {old.root}; it is kept)\n"
    argvs = boot.runner.argvs
    assert argvs[0] == [str(old.wineserver), "-k"]
    new_root = managed[0]
    assert argvs[1] == [str(new_root / "bin" / "wine"), "wineboot", "-u"]
    assert argvs[2] == [str(new_root / "bin" / "wineserver"), "-w"]
    assert boot.runner.calls[1]["env"]["WINEPREFIX"] == str(boot.paths.prefix)
    assert boot.config.raw("wine", "build") == ""
    assert boot.state.get(WINE_BUILD) == default
    assert boot.state.get("wine.provider") == "managed"
    assert boot.cache["steps"] == [None]


def test_wine_already_current(capsys: pytest.CaptureFixture[str], boot: bootstrap.Ctx, managed: list[Path]) -> None:
    target = boot.manifest.wine_default
    boot.set_wine(wine_provider.managed_info(target, wine_provider.managed_root(boot.paths, target.id)))
    result = run_json(capsys, "update", "--wine")
    assert result == {"previous": target.id, "build": target.id, "changed": False, "steps": ["all"]}
    assert boot.runner.argvs == []
    out = run_cli(capsys, "update", "--wine")[1]
    assert out == f"Wine {target.id} is already in use\n"


def test_wine_without_a_previous_wine(
    capsys: pytest.CaptureFixture[str], boot: bootstrap.Ctx, managed: list[Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    def missing() -> Any:
        raise DownloadFailed("no wine")

    boot.reset_wine()
    monkeypatch.setattr(boot, "wine", missing)
    called: list[Any] = []

    def wine_after_install() -> Any:
        return boot._wine

    def set_wine(info: Any) -> None:
        called.append(info)
        object.__setattr__(boot, "_wine", info)
        monkeypatch.setattr(boot, "wine", wine_after_install)

    monkeypatch.setattr(boot, "set_wine", set_wine)
    out = run_cli(capsys, "update", "--wine")[1]
    assert "(previous: none; it is kept)" in out
    assert [argv[1:] for argv in boot.runner.argvs] == [["wineboot", "-u"], ["-w"]]
    assert len(called) == 1


def test_wine_wineboot_failure(capsys: pytest.CaptureFixture[str], boot: bootstrap.Ctx, managed: list[Path]) -> None:
    boot.runner = RecordingRunner({"wine": Completed([], 3, "", "boom")})
    code, _out, err = run_cli(capsys, "update", "--wine")
    assert code == 1
    assert "wineboot -u failed with exit code 3" in err
    assert "previous Wine build is kept" in err
    assert boot.cache["steps"] == []


def test_wine_failure_hint_names_the_old_build(boot: bootstrap.Ctx, managed: list[Path]) -> None:
    other = boot.manifest.wine_default
    old = wine_provider.managed_info(other, boot.paths.data_dir / "old-wine")
    boot.set_wine(old)
    boot.runner = RecordingRunner({"wine": 1})
    with pytest.raises(ForkLinuxError) as info:
        update_cmd._switch_wine(boot)
    assert f"({other.id})" in info.value.hint


def test_wine_other_providers_are_not_updated(
    capsys: pytest.CaptureFixture[str], boot: bootstrap.Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    boot.config.set("wine", "provider", "system")
    code, _out, err = run_cli(capsys, "update", "--wine")
    assert code == 2
    assert "'system'" in err


def test_wine_needs_fork_closed(
    capsys: pytest.CaptureFixture[str], boot: bootstrap.Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    fork_running(monkeypatch, True)
    assert run_cli(capsys, "update", "--wine")[0] == 15
