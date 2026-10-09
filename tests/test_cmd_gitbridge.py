"""Tests for ``fork-linux git-bridge status|enable|disable|record``."""

from __future__ import annotations

from pathlib import Path

import pytest
from fixtures.cli_run import fork_running, isolate, paths, run_cli, run_json
from fixtures.setup_ctx import FakeUI

from fork_linux import bridge, resources
from fork_linux import ui as ui_mod
from fork_linux.config import Config


@pytest.fixture(autouse=True)
def _isolated(xdg: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    isolate(monkeypatch)
    fork_running(monkeypatch, False)
    monkeypatch.setenv(resources.SHIMS_ENV, str(tmp_path / "shims"))
    monkeypatch.setenv(resources.LIBEXEC_ENV, str(tmp_path / "libexec"))
    monkeypatch.setattr(resources, "bridge_build_script", lambda: None)
    _git(tmp_path, monkeypatch, "2.53.0")


def _git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: str) -> None:
    """A host ``git`` that reports ``version`` (one directory per version: the probe is cached by path)."""
    bin_dir = tmp_path / "git" / version
    bin_dir.mkdir(parents=True, exist_ok=True)
    (bin_dir / "git").write_text(f"#!/bin/sh\necho 'git version {version}'\n", encoding="utf-8")
    (bin_dir / "git").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")


def _build(tmp_path: Path) -> None:
    (tmp_path / "shims").mkdir(exist_ok=True)
    for exe in bridge.SHIM_EXES:
        (tmp_path / "shims" / exe).write_bytes(b"MZ")
    (tmp_path / "libexec").mkdir(exist_ok=True)
    (tmp_path / "libexec" / bridge.HELPER).write_bytes(b"\x7fELF")


def test_status_when_not_built(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    code, out, _err = run_cli(capsys, "git-bridge", "status")
    assert code == 0
    assert "enabled:   no" in out and "available: no" in out and "ready:     no" in out
    assert "git:       not checked (bridge off)" in out and "daemon:    not running" in out
    assert "note:      the native-git bridge is experimental" in out
    code, _out, err = run_cli(capsys, "git-bridge", "enable")
    assert code == 2 and "experimental" in err
    info = run_json(capsys, "git-bridge", "status")
    assert info["available"] is False and info["shims_dir"] == str(tmp_path / "shims")


def test_status_without_a_shims_dir(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(resources, "shims_dir", lambda: None)
    assert "shims:     not built" in run_cli(capsys, "git-bridge", "status")[1]


def test_enable_and_disable(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    _build(tmp_path)
    code, out, err = run_cli(capsys, "git-bridge", "enable")
    assert code == 0 and "enabled (experimental)" in out and "next time Fork starts" in out and err == ""
    assert Config.load(paths(), {}).getbool("git", "bridge") is True
    out = run_cli(capsys, "git-bridge", "status")[1]
    assert "enabled:   yes" in out and "git:       2.53.0" in out
    # Not installed in the prefix yet (setup's host_shims step does that before Fork starts).
    assert "ready:     no" in out and "not installed in the prefix" in out
    assert run_json(capsys, "git-bridge", "enable")["enabled"] is True
    code, out, _err = run_cli(capsys, "git-bridge", "disable")
    assert code == 0 and "bundled git" in out
    assert run_json(capsys, "git-bridge", "disable")["enabled"] is False
    assert Config.load(paths(), {}).getbool("git", "bridge") is False


def test_enable_with_old_or_missing_git(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _build(tmp_path)
    _git(tmp_path, monkeypatch, "2.34.1")
    code, _out, err = run_cli(capsys, "git-bridge", "enable")
    assert code == 2 and "older than 2.40" in err
    _git(tmp_path, monkeypatch, "2.45.2")
    code, out, err = run_cli(capsys, "git-bridge", "enable")
    assert code == 0 and "warning: the Linux git 2.45.2 is older than 2.50" in err
    assert "enabled (experimental)" in out
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    code, _out, err = run_cli(capsys, "git-bridge", "enable")
    assert code == 2 and "git is not installed" in err


def test_enable_while_fork_runs(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _build(tmp_path)
    fork_running(monkeypatch)
    out = run_cli(capsys, "git-bridge", "enable")[1]
    assert "Fork is running now and keeps its bundled git until it is restarted" in out


def _build_script(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, works: bool = True) -> Path:
    script = tmp_path / "build-bridge.sh"
    body = (
        f"mkdir -p '{tmp_path}/shims' '{tmp_path}/libexec'\n"
        f"printf MZ > '{tmp_path}/shims/fl-shim.exe'\nprintf MZ > '{tmp_path}/shims/fl-launch.exe'\n"
        f"printf ELF > '{tmp_path}/libexec/fl-bridge-helper'\necho built\n"
        if works
        else "echo 'meson: compiler not found' >&2\nexit 3\n"
    )
    script.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    script.chmod(0o755)
    monkeypatch.setattr(resources, "bridge_build_script", lambda: script)
    return script


def test_enable_offers_to_build_in_a_checkout(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _build_script(tmp_path, monkeypatch)
    answers = FakeUI(answer=False)
    monkeypatch.setattr(ui_mod, "choose", lambda *a, **k: answers)
    code, _out, err = run_cli(capsys, "git-bridge", "enable")
    assert code == 2 and "not built" in err
    assert answers.kinds("confirm")[0][0] == "Build the git bridge?"
    answers.answer = True
    code, out, err = run_cli(capsys, "git-bridge", "enable")
    assert code == 0 and "building the bridge with" in err and "enabled (experimental)" in out
    assert (paths().logs_dir / "build-bridge.log").read_text(encoding="utf-8").strip().endswith("built")
    # Built now: no question any more.
    answers.events.clear()
    assert run_cli(capsys, "git-bridge", "enable")[0] == 0
    assert answers.kinds("confirm") == []


def test_enable_build_flag_and_failing_build(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _build_script(tmp_path, monkeypatch, works=False)
    monkeypatch.setattr(ui_mod, "choose", lambda *a, **k: pytest.fail("--build must not ask"))
    code, _out, err = run_cli(capsys, "git-bridge", "enable", "--build")
    assert code == 1 and "building the bridge failed (exit 3)" in err and "compiler not found" in err
    _build_script(tmp_path, monkeypatch)
    assert run_cli(capsys, "git-bridge", "enable", "--build")[0] == 0


def test_record_mode(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _err = run_cli(capsys, "git-bridge", "record", "on")
    assert code == 0 and "FL_BRIDGE_MODE=record" in out
    assert Config.load(paths(), {}).get("git", "bridge_mode") == "record"
    assert "mode:      record" in run_cli(capsys, "git-bridge", "status")[1]
    code, out, _err = run_cli(capsys, "git-bridge", "record", "off")
    assert code == 0 and "runs the Linux git" in out
    assert run_json(capsys, "git-bridge", "record", "on")["mode"] == "record"
    assert run_cli(capsys, "git-bridge", "record", "maybe")[0] == 2


def test_status_ready_with_the_running_daemon(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _build(tmp_path)
    assert run_cli(capsys, "git-bridge", "enable")[0] == 0
    monkeypatch.setattr(bridge, "shims_problems", lambda paths: [])
    monkeypatch.setattr(bridge, "running_daemon", lambda paths, session, proc_root=None: {"pid": 7, "port": 4242})
    out = run_cli(capsys, "git-bridge", "status")[1]
    assert "ready:     yes" in out and "daemon:    pid 7, port 4242" in out and "note:" not in out
