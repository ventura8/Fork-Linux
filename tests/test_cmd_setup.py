"""Tests for ``fork-linux setup``: options, step listing, a full fake setup run, resume and --reset."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest
from fixtures.setup_ctx import USER, FakeUI, FakeWine

from fork_linux import bootstrap, cli
from fork_linux import ui as ui_mod
from fork_linux import winetricks as winetricks_mod
from fork_linux.commands import setup as setup_cmd
from fork_linux.errors import Declined, IntegrityFailed, UsageError, WineUnavailable
from fork_linux.fork_install import InstallPlan
from fork_linux.fork_layout import ForkLayout
from fork_linux.paths import Paths
from fork_linux.procrun import RecordingRunner
from fork_linux.state import State
from fork_linux.steps import STEP_IDS, preflight
from fork_linux.steps import fork as fork_steps

SCRIPT = b"#!/bin/sh\n# fake winetricks\n"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fork-linux")
    subparsers = parser.add_subparsers(dest="command", parser_class=cli._CommandParser)
    setup_cmd.register(subparsers)
    return parser


def _env(home: Path, tmp_path: Path) -> dict[str, str]:
    env = dict(os.environ)
    share = tmp_path / "system-share"
    share.mkdir(exist_ok=True)
    env.update({"USER": USER, "XDG_DATA_DIRS": str(share), "PATH": "/nonexistent"})
    return env


class Harness:
    """A fake host: Wine root, winetricks, downloads, disk space and a recording runner."""

    def __init__(self, home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.env = _env(home, tmp_path)
        self.paths = Paths.from_env(self.env)
        self.wine_root = tmp_path / "wine"
        (self.wine_root / "bin").mkdir(parents=True)
        for name in ("wine", "wineserver"):
            tool = self.wine_root / "bin" / name
            tool.write_text("#!/bin/sh\nexit 0\n")
            tool.chmod(0o755)
        self.fake = FakeWine(self.paths.prefix, layout=ForkLayout(self.paths, USER))
        self.runner = self.fake.runner()
        self.runner.which_map.update({"cabextract": "/usr/bin/cabextract", "unzip": "/usr/bin/unzip"})
        self.downloads: list[InstallPlan] = []
        self.ui = FakeUI()
        override = {"winetricks": {"sha256": hashlib.sha256(SCRIPT).hexdigest(), "size": len(SCRIPT)}}
        self.paths.config_dir.mkdir(parents=True, exist_ok=True)
        self.paths.manifest_override.write_text(json.dumps(override))
        shims = tmp_path / "no-shims"
        monkeypatch.setenv("FORK_LINUX_SHIMS_DIR", str(shims))
        monkeypatch.setattr(preflight.fsutil, "disk_free", lambda path: 50 * preflight.GiB)
        monkeypatch.setattr(preflight, "_euid", lambda: 1000)
        monkeypatch.setattr(preflight, "_machine", lambda: "x86_64")
        monkeypatch.setattr(winetricks_mod, "ensure", self._ensure)
        monkeypatch.setattr(fork_steps.fork_install, "download_installer", self._download)
        monkeypatch.setattr(ui_mod, "choose", lambda *args, **kwargs: self.ui)

    def _ensure(self, manifest: Any, paths: Paths, runner: Any, **kwargs: Any) -> Path:
        script = winetricks_mod.managed_path(manifest, paths)
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_bytes(SCRIPT)
        script.chmod(0o755)
        return script

    def _download(self, plan: InstallPlan, manifest: Any, paths: Paths, **kwargs: Any) -> Path:
        self.downloads.append(plan)
        path = paths.downloads_dir / plan.file_name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"MZ")
        return path

    def run(self, *argv: str) -> int:
        args = _parser().parse_args(["setup", *argv])
        ctx = cli.AppContext(args, env=self.env, runner=self.runner)
        return args.func(args, ctx)

    def state(self) -> State:
        return State.load(self.paths.state_file)


@pytest.fixture
def harness(xdg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Harness:
    return Harness(xdg, tmp_path, monkeypatch)


# -- options -------------------------------------------------------------------------------------


def test_wine_choice_validation(tmp_path: Path) -> None:
    for value in ("managed", "system", "flatpak", str(tmp_path)):
        assert setup_cmd._wine_choice(value) == value
    with pytest.raises(argparse.ArgumentTypeError):
        setup_cmd._wine_choice("relative/wine")
    with pytest.raises(SystemExit):
        _parser().parse_args(["setup", "--wine", "nope"])


def test_options_parse() -> None:
    args = _parser().parse_args(
        ["setup", "--fork-version", "2.23.2", "--only", "fonts", "--only", "icon", "--dotnet", "dotnet472",
         "--accept-fork-eula", "--no-launch", "--force", "--json"]
    )
    assert args.fork_version == "2.23.2" and args.only == ["fonts", "icon"] and args.dotnet == "dotnet472"
    assert args.accept_fork_eula and args.no_launch and args.force and args.json
    with pytest.raises(SystemExit):
        _parser().parse_args(["setup", "--fork-version", "2.23.2", "--latest"])
    with pytest.raises(SystemExit):
        _parser().parse_args(["setup", "--dotnet", "dotnet40"])


def test_bootstrap_seam() -> None:
    assert setup_cmd._bootstrap() is bootstrap


# -- full run ------------------------------------------------------------------------------------


def test_full_setup_then_resume(harness: Harness, capsys: pytest.CaptureFixture[str]) -> None:
    assert harness.run("--accept-fork-eula", "--wine", str(harness.wine_root)) == 0
    out = capsys.readouterr().out
    assert "Setup is complete" in out
    state = harness.state()
    assert state.get("setup.complete") is True
    assert set(state.data["steps"]) == set(STEP_IDS) - {"preflight", "finalize"}
    assert state.get("fork.version") == "2.23.2"
    assert state.get("wine.provider") == "custom" and state.get("dotnet.verb") == "dotnet48"
    assert state.get("consent.method") == "flag"
    assert harness.fake.verbs == ["dotnet48", "win10", "corefonts"]
    assert harness.paths.created_by_marker.read_text().startswith("fork-linux ")
    assert (harness.paths.config_file.read_text().count(str(harness.wine_root))) == 1
    assert [plan.version for plan in harness.downloads] == ["2.23.2", "2.23.2"]
    # Every Wine call has winemenubuilder disabled and our prefix.
    for call in harness.runner.calls:
        if call["argv"][0].endswith("/wine") and call["argv"][1:] != ["--version"]:
            assert "winemenubuilder.exe=d" in call["env"]["WINEDLLOVERRIDES"]
            assert call["env"]["WINEPREFIX"] == str(harness.paths.prefix)
    # Resume: nothing left to do but the always steps.
    calls = len(harness.runner.calls)
    assert harness.run("--json") == 0
    report = json.loads(capsys.readouterr().out)
    assert report["ran"] == ["preflight", "finalize"] and report["complete"] is True
    assert report["fork_version"] == "2.23.2" and report["log"].endswith(".log")
    assert [call["argv"][-1] for call in harness.runner.calls[calls:] if "wineserver" in call["argv"][0]] == ["-w"]


def test_list_steps(harness: Harness, capsys: pytest.CaptureFixture[str]) -> None:
    assert harness.run("--list-steps", "--wine", str(harness.wine_root)) == 0
    lines = capsys.readouterr().out.splitlines()
    assert [line.split()[1] for line in lines] == list(STEP_IDS)
    assert lines[0].startswith("always") and lines[1].startswith("pending")
    harness.run("--accept-fork-eula")
    capsys.readouterr()
    assert harness.run("--list-steps", "--json") == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["complete"] is True and listed["schema"] == 1
    statuses = {row["id"]: row["status"] for row in listed["steps"]}
    assert statuses["dotnet"] == "done" and statuses["finalize"] == "always"
    assert {row["id"]: row["needs_network"] for row in listed["steps"]}["fork_download"] is True


def test_non_interactive_without_consent_is_declined(harness: Harness) -> None:
    harness.ui.answer = False
    harness.ui.interactive = False
    with pytest.raises(Declined) as caught:
        harness.run("--wine", str(harness.wine_root))
    assert "--accept-fork-eula" in caught.value.hint
    assert harness.state().get("setup.last_error")["step"] == "consent"
    assert harness.downloads == [] and harness.fake.verbs == []


def test_only_selected_steps(harness: Harness, capsys: pytest.CaptureFixture[str]) -> None:
    assert harness.run("--only", "consent", "--accept-fork-eula") == 0
    assert "remaining ones" in capsys.readouterr().out
    assert harness.state().get("setup.complete") is False


# -- reset ---------------------------------------------------------------------------------------


def test_reset_requires_confirmation(harness: Harness) -> None:
    harness.ui.answer = False
    with pytest.raises(Declined, match="not reset"):
        harness.run("--reset", "--accept-fork-eula")
    title, text = harness.ui.kinds("confirm")[0]
    assert title == setup_cmd.RESET_TITLE and "3 activations" in text and "Help > Activation" in text


def test_reset_rebuilds_everything(harness: Harness, capsys: pytest.CaptureFixture[str]) -> None:
    harness.run("--accept-fork-eula", "--wine", str(harness.wine_root))
    stray = harness.paths.prefix / "drive_c" / "stray.txt"
    stray.write_text("old")
    harness.fake.verbs.clear()
    assert harness.run("--reset") == 0
    assert not stray.exists()
    state = harness.state()
    assert state.get("setup.complete") is True and state.get("consent.method") == "flag"
    assert harness.fake.verbs == ["dotnet48", "win10", "corefonts"]
    assert any(call["argv"][-1] == "-k" for call in harness.runner.calls)


def test_reset_on_a_fresh_machine(harness: Harness) -> None:
    assert harness.run("--reset", "--accept-fork-eula", "--wine", str(harness.wine_root)) == 0
    assert harness.state().get("setup.complete") is True


def test_reset_warns_when_wine_is_gone(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    harness.run("--accept-fork-eula", "--wine", str(harness.wine_root))

    def unavailable(self: Any) -> Any:
        raise WineUnavailable("gone")

    monkeypatch.setattr(bootstrap.Ctx, "wine", unavailable)
    args = _parser().parse_args(["setup", "--reset"])
    ctx = cli.AppContext(args, env=harness.env, runner=harness.runner)
    bctx = setup_cmd._make_ctx(args, ctx)
    setup_cmd.reset(bctx)
    assert "could not stop Wine" in harness.ui.kinds("warn")[0]
    assert not harness.paths.prefix.exists()
    assert bctx.state.get("consent.method") == "flag" and bctx.state.is_new


def test_reset_refuses_a_prefix_we_did_not_create(harness: Harness) -> None:
    harness.run("--accept-fork-eula", "--wine", str(harness.wine_root))
    harness.paths.created_by_marker.unlink()
    with pytest.raises(IntegrityFailed, match="marker"):
        harness.run("--reset")
    assert harness.paths.prefix.is_dir()


def test_reset_with_an_unknown_step_deletes_nothing(harness: Harness) -> None:
    harness.run("--accept-fork-eula", "--wine", str(harness.wine_root))
    for argv in (("--only", "nope"), ("--from-step", "nope")):
        with pytest.raises(UsageError, match="nope"):
            harness.run("--reset", *argv)
    assert harness.ui.kinds("confirm") == []
    assert harness.state().get("setup.complete") is True


def test_wine_choice_is_saved_only_when_it_changes(harness: Harness) -> None:
    harness.run("--only", "consent", "--accept-fork-eula", "--wine", "system")
    first = harness.paths.config_file.read_text()
    harness.run("--only", "consent", "--wine", "system")
    assert harness.paths.config_file.read_text() == first
    assert "provider = system" in first


def test_runner_type_is_recording(harness: Harness) -> None:
    assert isinstance(harness.runner, RecordingRunner)
