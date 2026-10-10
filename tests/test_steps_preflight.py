"""Tests for the preflight, consent and finalize setup steps."""

from __future__ import annotations

from pathlib import Path

import pytest
from fixtures.setup_ctx import FakeUI, make_ctx

from fork_linux import APP_NAME, credits
from fork_linux.bootstrap import Ctx
from fork_linux.errors import Declined, ForkLinuxError, UnsupportedEnvironment
from fork_linux.procrun import RecordingRunner
from fork_linux.steps import consent, finalize, preflight

TOOLS = {"cabextract": "/usr/bin/cabextract", "unzip": "/usr/bin/unzip"}


@pytest.fixture
def host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(preflight, "_machine", lambda: "x86_64")
    monkeypatch.setattr(preflight, "_python", lambda: (3, 10))
    monkeypatch.setattr(preflight, "_euid", lambda: 1000)
    monkeypatch.setattr(preflight.fsutil, "disk_free", lambda path: 10 * preflight.GiB)


def _ctx(which: dict[str, str | None] | None = None, **flags: object) -> Ctx:
    return make_ctx(RecordingRunner(which_map=TOOLS if which is None else which), **flags)


def test_seams_report_the_real_host() -> None:
    assert preflight._machine()
    assert preflight._python() >= (3, 10)
    assert isinstance(preflight._euid(), int)


def test_preflight_passes_and_creates_directories(xdg: Path, host: None) -> None:
    ctx = _ctx()
    preflight.run(ctx)
    assert ctx.paths.logs_dir.is_dir()
    assert ctx.paths.runtime_dir.is_dir()
    assert preflight.verify(ctx)
    assert preflight.PREFLIGHT.always
    assert not preflight.PREFLIGHT.requires_fork_closed


@pytest.mark.parametrize("machine", ["aarch64", ""])
def test_preflight_refuses_other_architectures(xdg: Path, host: None, monkeypatch: pytest.MonkeyPatch,
                                              machine: str) -> None:
    monkeypatch.setattr(preflight, "_machine", lambda: machine)
    ctx = _ctx()
    with pytest.raises(UnsupportedEnvironment, match="x86_64 only"):
        preflight.run(ctx)


def test_preflight_accepts_amd64(xdg: Path, host: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(preflight, "_machine", lambda: "AMD64")
    preflight.run(_ctx())


def test_preflight_refuses_old_python(xdg: Path, host: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(preflight, "_python", lambda: (3, 9))
    ctx = _ctx()
    with pytest.raises(UnsupportedEnvironment, match="Python 3.9"):
        preflight.run(ctx)


def test_preflight_refuses_root_unless_allowed(xdg: Path, host: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(preflight, "_euid", lambda: 0)
    ctx = _ctx()
    with pytest.raises(UnsupportedEnvironment, match="root"):
        preflight.run(ctx)
    preflight.run(_ctx(allow_root=True))


def test_preflight_needs_disk_space(xdg: Path, host: None, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Path] = []

    def free(path: Path) -> int:
        seen.append(path)
        return 1 * preflight.GiB

    monkeypatch.setattr(preflight.fsutil, "disk_free", free)
    ctx = _ctx()
    with pytest.raises(ForkLinuxError, match="not enough free disk space") as caught:
        preflight.run(ctx)
    assert "3.0 GiB needed" in caught.value.message
    assert "--prefix" in caught.value.hint
    assert seen == [ctx.paths.prefix.parent]
    # An existing prefix only needs room to grow.
    ctx.paths.prefix.mkdir(parents=True)
    (ctx.paths.prefix / "system.reg").write_text("x")
    preflight.run(ctx)


def test_preflight_names_missing_tools_with_a_hint(xdg: Path, host: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        preflight.hostdeps, "distro", lambda: preflight.hostdeps.DistroInfo("ubuntu", (), "debian", "Ubuntu")
    )
    ctx = _ctx({"cabextract": None, "unzip": "/usr/bin/unzip"})
    with pytest.raises(ForkLinuxError, match="cabextract") as caught:
        preflight.run(ctx)
    assert caught.value.hint.startswith("sudo apt install cabextract")


# -- consent -----------------------------------------------------------------------------------


def test_consent_text_credits_the_developers_and_lists_downloads(xdg: Path) -> None:
    ctx = make_ctx()
    text = consent.text(ctx)
    for needed in (APP_NAME, "Dan Pristupov and Tanya Pristupova", credits.LINKS["license"], credits.LINKS["buy"],
                   "NOT affiliated", "Wine 11.0 (staging)", "75 MB", "winetricks 20260125", "0.8 MB",
                   "Fork 2.23.2 installer from cdn.fork.dev: 76 MB", ".NET Framework 4.8", "core fonts",
                   "about 3 GB", "minutes"):
        assert needed in text
    assert "Selawik 1.01 interface font (OFL-1.1) from GitHub (Microsoft): 0.5 MB" in consent.downloads(ctx)


def test_consent_text_for_other_providers_and_latest(xdg: Path) -> None:
    ctx = make_ctx(wine_choice="system", latest=True)
    lines = consent.downloads(ctx)
    assert lines[0] == "Wine: your system Wine is used, nothing to download"
    assert "newest release" in lines[-1]
    assert "size not pinned" in lines[-1]


def test_consent_flag_skips_the_question(xdg: Path) -> None:
    ui = FakeUI(answer=False)
    ctx = make_ctx(ui=ui, accept_eula=True)
    assert not consent.verify(ctx)
    consent.run(ctx)
    assert consent.verify(ctx)
    assert ctx.state.get(consent.METHOD_KEY) == "flag"
    assert ui.kinds("confirm") == []


def test_consent_prompt_accepted(xdg: Path) -> None:
    ui = FakeUI(answer=True)
    ctx = make_ctx(ui=ui)
    consent.run(ctx)
    assert ctx.state.get(consent.METHOD_KEY) == "prompt"
    title, text = ui.kinds("confirm")[0]
    assert title == consent.TITLE
    assert credits.LINKS["license"] in text
    # Accepted once: never asked again.
    consent.run(ctx)
    assert len(ui.kinds("confirm")) == 1


def test_consent_declined_interactively(xdg: Path) -> None:
    ctx = make_ctx(ui=FakeUI(answer=False, interactive=True))
    with pytest.raises(Declined, match="declined"):
        consent.run(ctx)
    assert not consent.verify(ctx)


def test_consent_without_anyone_to_ask(xdg: Path) -> None:
    ctx = make_ctx(ui=FakeUI(answer=False, interactive=False))
    with pytest.raises(Declined) as caught:
        consent.run(ctx)
    assert "--accept-fork-eula" in caught.value.hint
    assert credits.LINKS["license"] in caught.value.hint


# -- finalize ----------------------------------------------------------------------------------


def test_finalize_waits_for_wine_and_updates_completion(xdg: Path, tmp_path: Path) -> None:
    runner = RecordingRunner({"wineserver": 1})
    proc = tmp_path / "proc"
    proc.mkdir()
    ctx = make_ctx(runner, proc_root=proc)
    finalize.run(ctx)
    assert runner.argvs[-1][-1] == "-w"
    assert ctx.state.get("setup.complete") is False
    assert isinstance(ctx.state.get(finalize.FINALIZED_AT), str)
    assert finalize.verify(ctx)
    assert finalize.FINALIZE.always


def test_finalize_does_not_wait_while_fork_runs(xdg: Path, tmp_path: Path) -> None:
    # 'wineserver -w' would block until Fork is closed (setup re-run with Fork open).
    runner = RecordingRunner()
    proc = tmp_path / "proc"
    entry = proc / "4242"
    entry.mkdir(parents=True)
    ctx = make_ctx(runner, proc_root=proc)
    (entry / "environ").write_bytes(b"WINEPREFIX=" + str(ctx.paths.prefix).encode() + b"\0")
    (entry / "cmdline").write_bytes(b"wine\0C:\\users\\tester\\AppData\\Local\\Fork\\current\\Fork.exe\0")
    finalize.run(ctx)
    assert runner.argvs == []
    assert isinstance(ctx.state.get(finalize.FINALIZED_AT), str)
