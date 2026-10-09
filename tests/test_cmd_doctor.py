"""Tests for ``fork-linux doctor``: output, exit codes, --json, --fix, --check and --gui."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from fork_linux import doctor
from fork_linux.commands import doctor as doctor_cmd
from fork_linux.doctor import Check, Result
from fork_linux.procrun import RecordingRunner

from fixtures.cli_run import isolate, run_cli


@pytest.fixture(autouse=True)
def _isolated(xdg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    isolate(monkeypatch)


def _check(check_id: str, status: str, detail: str = "", hint: str = "", **kwargs: Any) -> Check:
    group = check_id.split(".")[0]
    return Check(check_id, check_id.title(), group, lambda _ctx: Result(status, detail, hint), **kwargs)


@pytest.fixture
def fake_checks(monkeypatch: pytest.MonkeyPatch) -> list[Check]:
    checks = [
        _check("env.arch", "ok", "x86_64", "never shown"),
        _check("env.note", "info", "fyi", "info hint"),
        _check("wine.present", "warn", "old wine", "upgrade it", fix_steps=("wine_runtime",)),
        _check("git.selftest", "ok", "deep one", deep=True),
        _check("network.reach", "ok", "online", network=True),
    ]
    monkeypatch.setattr(doctor, "CHECKS", checks)
    return checks


def test_text_report(capsys: pytest.CaptureFixture[str], fake_checks: list[Check]) -> None:
    code, out, err = run_cli(capsys, "doctor")
    assert code == 0 and err == ""
    lines = out.splitlines()
    assert lines[0] == "env:"
    assert lines[1] == f"  [  ok] {'env.arch':<26} x86_64"
    assert "  [info] env.note" in lines[2]
    assert lines[3] == "wine:"
    assert "  [WARN] wine.present" in lines[4] and lines[5].strip() == "hint: upgrade it"
    assert "never shown" not in out and "info hint" not in out and "deep one" not in out
    assert lines[-1] == "1 ok, 1 warning(s), 0 failed, 1 info"


def test_verbose_shows_every_hint(capsys: pytest.CaptureFixture[str], fake_checks: list[Check]) -> None:
    out = run_cli(capsys, "doctor", "-v")[1]
    assert "never shown" in out and "info hint" in out


def test_deep_and_network_flags(capsys: pytest.CaptureFixture[str], fake_checks: list[Check]) -> None:
    out = run_cli(capsys, "doctor", "--deep", "--network")[1]
    assert "git.selftest" in out and "network.reach" in out


def test_check_selection(capsys: pytest.CaptureFixture[str], fake_checks: list[Check]) -> None:
    out = run_cli(capsys, "doctor", "--check", "env.arch", "--check", "network")[1]
    assert "env.arch" in out and "network.reach" in out and "wine.present" not in out
    code, _out, err = run_cli(capsys, "doctor", "--check", "bogus")
    assert code == 2 and "unknown doctor check(s): bogus" in err


def test_failure_exit_code(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "CHECKS", [_check("env.arch", "fail", "arm64", "buy x86"), _check("env.b", "ok")])
    code, out, err = run_cli(capsys, "doctor")
    assert code == 17
    assert "[FAIL] env.arch" in out and "hint: buy x86" in out
    assert "1 check(s) failed: env.arch" in err and "doctor --fix" in err


def test_json_report(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "CHECKS", [_check("env.arch", "fail", "arm64", "buy x86")])
    code, out, _err = run_cli(capsys, "--json", "doctor")
    assert code == 17
    data = json.loads(out)
    assert data["schema"] == 1 and data["summary"] == {"ok": 0, "warn": 0, "fail": 1, "info": 0}
    assert data["checks"][0]["id"] == "env.arch" and "fix" not in data


def test_fix(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, fake_checks: list[Check]) -> None:
    seen: list[Any] = []

    def fake_fix(dctx: doctor.DoctorCtx, results: list[tuple[Check, Result]]) -> doctor.FixReport:
        seen.append((dctx.ui, [check.id for check, _r in results]))
        fixed = [(check, Result("ok", "fixed now")) for check, _result in results]
        return doctor.FixReport(results=fixed, actions=["re-ran setup steps: wine_runtime"], errors=["x: oops"])

    monkeypatch.setattr(doctor, "fix", fake_fix)
    code, out, _err = run_cli(capsys, "doctor", "--fix")
    assert code == 0 and seen[0][1] == ["env.arch", "env.note", "wine.present"]
    assert out.startswith("fixed: re-ran setup steps: wine_runtime\nfix failed: x: oops\n")
    assert out.splitlines()[-1] == "3 ok, 0 warning(s), 0 failed, 0 info"
    code, out, _err = run_cli(capsys, "--json", "doctor", "--fix")
    data = json.loads(out)
    assert data["fix"] == {"actions": ["re-ran setup steps: wine_runtime"], "errors": ["x: oops"]}


def test_gui_flag_shows_the_report(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, fake_checks: list[Check]
) -> None:
    shown: list[str] = []
    monkeypatch.setattr(doctor_cmd, "show_gui", lambda _ctx, text: shown.append(text) or True)
    out = run_cli(capsys, "doctor", "--gui")[1]
    assert shown == [out]
    run_cli(capsys, "doctor")
    assert len(shown) == 1


class _Ctx:
    def __init__(self, env: dict[str, str], runner: RecordingRunner) -> None:
        self.env = env
        self.runner = runner


def test_show_gui() -> None:
    runner = RecordingRunner(which_map={"zenity": "/usr/bin/zenity"})
    assert doctor_cmd.show_gui(_Ctx({"PATH": "/usr/bin"}, runner), "report") is False
    assert doctor_cmd.show_gui(_Ctx({"DISPLAY": ":0", "PATH": "/usr/bin"}, runner), "report") is True
    call = runner.calls[0]
    assert call["argv"][:2] == ["/usr/bin/zenity", "--text-info"]
    assert call["input"] == "report" and call["env"]["DISPLAY"] == ":0"
    missing = RecordingRunner(which_map={"zenity": None})
    assert doctor_cmd.show_gui(_Ctx({"WAYLAND_DISPLAY": "wayland-0"}, missing), "report") is False
    assert missing.calls == []


def test_registered_after_logs() -> None:
    from fork_linux import commands

    assert commands.COMMANDS.index("doctor") == commands.COMMANDS.index("logs") + 1


def test_real_checks_run_offline(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _err = run_cli(capsys, "--json", "--offline", "doctor", "--check", "env", "prefix", "fork")
    data = json.loads(out)
    assert code == 17 and data["schema"] == 1
    ids = {item["id"]: item["status"] for item in data["checks"]}
    assert ids["prefix.exists"] == "fail" and ids["fork.installed"] == "fail" and ids["env.python"] == "ok"
