"""Tests for ``fork-linux logs``: tail, follow, locate and the redacted bug-report bundle."""

from __future__ import annotations

import io
import os
import tarfile
from pathlib import Path

import pytest

from fork_linux.commands import logs as logs_cmd

from fixtures.cli_run import isolate, layout, paths, run_cli, run_json, write_json

SECRET = "ghp_" + "a" * 36


@pytest.fixture(autouse=True)
def _isolated(xdg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    isolate(monkeypatch)


def _write(path: Path, text: str, mtime: int | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


@pytest.fixture
def some_logs() -> dict[str, Path]:
    logs = paths().logs_dir
    found = {
        "wine-old": _write(logs / "wine-20200101T000000Z.log", "old wine\n", 1000),
        "wine-last": _write(logs / "wine-last.log", "".join(f"line {i}\n" for i in range(300)), 3000),
        "wine-debug": _write(logs / "wine-20210101T000000Z.log", "debug\n", 2000),
        "wine-ancient": _write(logs / "wine-20190101T000000Z.log", "ancient\n", 500),
        "own": _write(logs / "fork-linux.log", f"token={SECRET}\n", 3000),
        "own-rotated": _write(logs / "fork-linux.log.1", "rotated\n", 100),
        "setup": _write(logs / "setup-20260101T000000Z.log", "setup\n", 50),
        "other": _write(logs / "unrelated.txt", "x\n"),
        "fork": _write(layout().fork_log, "fork says hi\n"),
        "velopack": _write(layout().velopack_log, "velopack\n"),
        "accounts": _write(layout().accounts_file, f'{{"token": "{SECRET}"}}'),
    }
    write_json(layout().settings_file, {"Theme": 1})
    write_json(paths().state_file, {"schema": 1, "steps": {}})
    _write(paths().config_file, "[wine]\nprovider = managed\n")
    return found


def test_no_logs_yet(capsys: pytest.CaptureFixture[str]) -> None:
    assert logs_cmd.wine_logs(paths()) == [] and logs_cmd.own_logs(paths()) == []
    code, _out, err = run_cli(capsys, "logs")
    assert code == 20 and "no wine log yet" in err
    located = run_json(capsys, "logs", "--path", "--all")
    assert (located["wine"], located["fork"], located["velopack"]) == ([], [], [])
    # The CLI itself logs to fork-linux.log.
    assert [Path(item).name for item in located["setup"]] == ["fork-linux.log"]
    out = run_cli(capsys, "logs", "--path", "--all")[1]
    assert out.splitlines() == located["setup"]


def test_default_tail_of_the_newest_wine_log(capsys: pytest.CaptureFixture[str], some_logs: dict[str, Path]) -> None:
    code, out, _err = run_cli(capsys, "logs")
    lines = out.splitlines()
    assert code == 0 and len(lines) == logs_cmd.TAIL_LINES
    assert lines[-1] == "line 299" and lines[0] == "line 100"


def test_sources_and_paths(capsys: pytest.CaptureFixture[str], some_logs: dict[str, Path]) -> None:
    assert run_cli(capsys, "logs", "--fork")[1] == "fork says hi\n"
    assert run_cli(capsys, "logs", "--velopack")[1] == "velopack\n"
    assert run_cli(capsys, "logs", "--setup")[1] == f"token={SECRET}\n"
    assert run_cli(capsys, "logs", "--wine", "--path")[1] == f"{some_logs['wine-last']}\n"
    located = run_json(capsys, "logs", "--all", "--path")
    assert located["fork"] == [str(some_logs["fork"])]
    out = run_cli(capsys, "logs", "--all", "--path")[1]
    assert out.splitlines() == [str(some_logs[key]) for key in ("wine-last", "fork", "velopack", "own")]
    out = run_cli(capsys, "logs", "--all")[1]
    assert f"==> fork: {some_logs['fork']} <==" in out
    assert "==> velopack:" in out and "==> setup:" in out and "==> wine:" in out


def test_all_with_some_missing(capsys: pytest.CaptureFixture[str]) -> None:
    _write(layout().fork_log, "only fork\n")
    out = run_cli(capsys, "logs", "--all")[1]
    assert "==> wine:" not in out and "==> velopack:" not in out
    assert "only fork\n" in out and "==> setup:" in out


def test_follow(
    capsys: pytest.CaptureFixture[str], some_logs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    code, _out, err = run_cli(capsys, "logs", "--all", "--follow")
    assert code == 2 and "one log" in err
    target = some_logs["fork"]
    steps = iter(["append", "truncate", "append2", "stop"])

    def sleep(_seconds: float) -> None:
        action = next(steps)
        if action == "append":
            with open(target, "a", encoding="utf-8") as handle:
                handle.write("new line\n")
        elif action == "truncate":
            target.write_text("", encoding="utf-8")
        elif action == "append2":
            target.write_text("after truncation\n", encoding="utf-8")
        else:
            raise KeyboardInterrupt

    monkeypatch.setattr(logs_cmd, "_sleep", sleep)
    code, out, _err = run_cli(capsys, "logs", "--fork", "--follow")
    assert code == 0
    assert out == "new line\nafter truncation\n"


def test_real_sleep_is_time_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    slept: list[float] = []
    monkeypatch.setattr(logs_cmd.time, "sleep", slept.append)
    logs_cmd._sleep(0.25)
    assert slept == [0.25]


def test_bundle(
    capsys: pytest.CaptureFixture[str], some_logs: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    monkeypatch.chdir(out_dir)
    monkeypatch.setattr(logs_cmd, "OS_RELEASE", _write(tmp_path / "os-release", 'NAME="Test"\n'))
    monkeypatch.setattr(logs_cmd, "_utc_stamp", lambda: "20260101T000000Z")
    code, out, _err = run_cli(capsys, "logs", "--bundle")
    assert code == 0
    assert "review before sharing" in out
    bundle = out_dir / "fork-linux-logs-20260101T000000Z.tar.gz"
    assert bundle.stat().st_mode & 0o777 == 0o600
    with tarfile.open(bundle) as tar:
        names = tar.getnames()
        contents = {}
        for member in tar.getmembers():
            handle = tar.extractfile(member)
            assert handle is not None
            contents[member.name] = handle.read().decode()
    root = logs_cmd.BUNDLE_ROOT
    assert f"{root}/wine/wine-last.log" in names
    assert f"{root}/wine/wine-20210101T000000Z.log" in names
    assert f"{root}/wine/wine-20200101T000000Z.log" in names
    assert f"{root}/wine/wine-20190101T000000Z.log" not in names
    for name in (
        "fork/fork.log",
        "fork/velopack.log",
        "fork/settings.json",
        "fork-linux/state.json",
        "fork-linux/config.ini",
        "system/os-release",
        "system/uname",
        "fork-linux/fork-linux.log",
        "fork-linux/fork-linux.log.1",
        "fork-linux/setup-20260101T000000Z.log",
    ):
        assert f"{root}/{name}" in names, name
    assert not any("accounts" in name for name in names)
    assert all(SECRET not in text for text in contents.values())
    # A second bundle in the same second would overwrite: refused.
    code, _out, err = run_cli(capsys, "logs", "--bundle")
    assert code == 2 and "already exists" in err
    monkeypatch.setattr(logs_cmd, "_utc_stamp", lambda: "20260101T000001Z")
    result = run_json(capsys, "logs", "--bundle")
    assert result["bundle"].endswith("20260101T000001Z.tar.gz")
    assert "system/uname" in result["files"]


def test_bundle_skips_unreadable_and_accounts(tmp_path: Path) -> None:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        assert logs_cmd._add_file(tar, "x", tmp_path / "missing.log") is False
        accounts = _write(tmp_path / "accounts.json", "{}")
        assert logs_cmd._add_file(tar, "a", accounts) is False
        assert tar.getnames() == []


def test_newest_skips_vanished_files(tmp_path: Path) -> None:
    present = _write(tmp_path / "a", "x")
    assert logs_cmd._newest([tmp_path / "gone", present]) == [present]


def test_read_tail_reads_only_the_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(logs_cmd, "MAX_READ", 10)
    path = _write(tmp_path / "big", "0123456789abcdefghij\nend\n")
    assert logs_cmd._read_tail(path, 100) == "fghij\nend\n"


def test_bundle_with_almost_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(logs_cmd, "OS_RELEASE", tmp_path / "no-os-release")
    target, included = logs_cmd.bundle(paths(), layout(), tmp_path)
    assert included == ["system/uname"]
    assert target.name.startswith("fork-linux-logs-") and len(logs_cmd._utc_stamp()) == 16


def test_bundle_never_follows_links_inside_the_prefix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(logs_cmd, "OS_RELEASE", tmp_path / "no-os-release")
    key = _write(tmp_path / "id_ed25519", "PRIVATE KEY MATERIAL\n")
    fork_layout = layout()
    fork_layout.fork_log.parent.mkdir(parents=True, exist_ok=True)
    fork_layout.fork_log.symlink_to(key)
    os.mkfifo(fork_layout.velopack_log)
    out = tmp_path / "out"
    out.mkdir()
    target, included = logs_cmd.bundle(paths(), fork_layout, out)
    assert "fork/fork.log" not in included and "fork/velopack.log" not in included
    with tarfile.open(target) as tar:
        for member in tar.getmembers():
            handle = tar.extractfile(member)
            assert handle is not None and b"PRIVATE KEY" not in handle.read()
    # Outside the prefix links are fine (/etc/os-release usually is one).
    assert logs_cmd._read_tail(fork_layout.fork_log, 5) == "PRIVATE KEY MATERIAL\n"
    with pytest.raises(OSError):
        logs_cmd._read_tail(fork_layout.fork_log, 5, follow_symlinks=False)
