"""Tests for fork_linux.winecmd: Wine user, version parsing, environment and invocations."""

from __future__ import annotations

import json
import os
import stat
import types
from pathlib import Path

import pytest

from fork_linux import winecmd
from fork_linux.errors import ForkLinuxError, UsageError, WineUnavailable
from fork_linux.paths import Paths
from fork_linux.procrun import CommandError, Completed, RecordingRunner, Runner
from fork_linux.registry import RegBatch
from fork_linux.winecmd import WineInfo

FAKES_BIN = Path(__file__).resolve().parent / "fakes" / "bin"


def _info(root: Path) -> WineInfo:
    return WineInfo(
        provider="managed",
        build_id="kron4ek-11.0-staging-wow64",
        root=root,
        wine=root / "bin" / "wine",
        wineserver=root / "bin" / "wineserver",
        version="11.0",
        staging=True,
        wow64=True,
    )


@pytest.fixture
def paths(xdg: Path) -> Paths:
    return Paths.from_env(os.environ)


@pytest.fixture
def info(tmp_path: Path) -> WineInfo:
    return _info(tmp_path / "runtime")


# --------------------------------------------------------------------------- windows_user


def test_windows_user_prefers_user_then_logname() -> None:
    assert winecmd.windows_user({"USER": "alice", "LOGNAME": "bob"}) == "alice"
    assert winecmd.windows_user({"USER": "", "LOGNAME": "bob"}) == "bob"


def test_windows_user_falls_back_to_password_database(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(winecmd.pwd, "getpwuid", lambda uid: types.SimpleNamespace(pw_name="carol"))
    assert winecmd.windows_user({}) == "carol"


def test_windows_user_without_any_source(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(uid: int) -> object:
        raise KeyError(uid)

    monkeypatch.setattr(winecmd.pwd, "getpwuid", missing)
    with pytest.raises(UsageError, match="cannot determine the user name") as info:
        winecmd.windows_user({"USER": "", "LOGNAME": ""})
    assert "USER" in info.value.hint


@pytest.mark.parametrize("name", ["a/b", "dom\\user", ".", "..", " x", "x ", "a:b", "a*b", "a\x01b", "a\x7fb"])
def test_windows_user_refuses_unsafe_names(name: str) -> None:
    with pytest.raises(UsageError, match="cannot be used as a Windows user directory"):
        winecmd.windows_user({"USER": name})


def test_windows_user_checks_password_database_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(winecmd.pwd, "getpwuid", lambda uid: types.SimpleNamespace(pw_name="x/y"))
    with pytest.raises(UsageError):
        winecmd.windows_user({})


# --------------------------------------------------------------------------- versions


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("wine-11.0 (Staging)\n", ("11.0", True)),
        ("wine-10.0 (Ubuntu 10.0~repack)", ("10.0", False)),
        ("wine-9.0", ("9.0", False)),
        ("wine-8.0-rc1 (Staging)", ("8.0-rc1", True)),
        ("wine-10.20 (TkG staging)", ("10.20", True)),
        ("  wine-11.0\n", ("11.0", False)),
    ],
)
def test_parse_wine_version(text: str, expected: tuple[str, bool]) -> None:
    assert winecmd.parse_wine_version(text) == expected


@pytest.mark.parametrize("text", ["", "wine", "wine-", "Wine 11.0", "winex-1.0"])
def test_parse_wine_version_rejects_garbage(text: str) -> None:
    with pytest.raises(ValueError, match="not a wine --version output"):
        winecmd.parse_wine_version(text)


def test_query_version(tmp_path: Path) -> None:
    runner = RecordingRunner({"wine": "wine-11.0 (Staging)\n"})
    wine = tmp_path / "bin" / "wine"
    assert winecmd.query_version(runner, wine, {"PATH": "/usr/bin"}) == ("11.0", True)
    call = runner.calls[0]
    assert call["argv"] == [str(wine), "--version"]
    assert call["env"] == {"PATH": "/usr/bin"}
    assert call["timeout"] == winecmd.VERSION_TIMEOUT


def test_query_version_default_env_inherits(tmp_path: Path) -> None:
    runner = RecordingRunner({"wine": "wine-9.0\n"})
    assert winecmd.query_version(runner, tmp_path / "wine") == ("9.0", False)
    assert runner.calls[0]["env"] is None


def test_query_version_nonzero_exit_with_output(tmp_path: Path) -> None:
    runner = RecordingRunner({"wine": Completed([], 127, "", "error while loading shared libraries: libgnutls\n")})
    with pytest.raises(WineUnavailable, match="exit code 127: error while loading") as info:
        winecmd.query_version(runner, tmp_path / "wine")
    assert "fork-linux doctor" in info.value.hint


def test_query_version_nonzero_exit_without_output(tmp_path: Path) -> None:
    runner = RecordingRunner({"wine": 1})
    with pytest.raises(WineUnavailable) as info:
        winecmd.query_version(runner, tmp_path / "wine")
    assert str(info.value).endswith("failed with exit code 1")


def test_query_version_unparsable(tmp_path: Path) -> None:
    runner = RecordingRunner({"wine": "hello\n"})
    with pytest.raises(WineUnavailable, match="is not a usable Wine"):
        winecmd.query_version(runner, tmp_path / "wine")


def test_query_version_timeout(tmp_path: Path) -> None:
    def hang(argv: list[str]) -> Completed:
        raise ForkLinuxError("command timed out after 30s")

    with pytest.raises(WineUnavailable, match="did not finish: command timed out"):
        winecmd.query_version(RecordingRunner({"wine": hang}), tmp_path / "wine")


def test_query_version_with_fake_wine(fake_bin: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FL_FAKE_WINE_VERSION", "wine-10.0 (Ubuntu 10.0~repack)")
    assert winecmd.query_version(Runner(), FAKES_BIN / "wine", dict(os.environ)) == ("10.0", False)
    logged = [json.loads(line) for line in fake_bin.read_text(encoding="utf-8").splitlines()]
    assert logged[0]["argv"] == ["wine", "--version"]


# --------------------------------------------------------------------------- build_env


def test_build_env_sets_every_wine_variable(paths: Paths, info: WineInfo) -> None:
    base = {
        "PATH": "/usr/local/bin:/usr/bin",
        "HOME": "/home/alice",
        "DISPLAY": ":0",
        "WINEPREFIX": "/home/alice/.wine",
        "WINEDLLPATH": "/opt/other-wine/lib",
        "WINEDEBUG": "+all",
        "WINEESYNC": "1",
        "WINEFSYNC": "0",
        "LD_LIBRARY_PATH": "/snap/lib",
        "SNAP": "/snap/fork-linux/1",
        "SNAP_NAME": "fork-linux",
    }
    env = winecmd.build_env(paths, info, user="alice", base_env=base)
    assert env["WINEPREFIX"] == str(paths.prefix)
    assert env["WINEARCH"] == "win64"
    assert env["WINESERVER"] == str(info.wineserver)
    assert env["WINELOADER"] == str(info.wine)
    assert env["PATH"] == f"{info.root / 'bin'}:/usr/local/bin:/usr/bin"
    assert env["WINEDEBUG"] == "-all"
    assert env["WINEDLLOVERRIDES"] == "winemenubuilder.exe=d"
    assert env["WINEHOME"] == "C:\\users\\alice"
    assert env["FL_WINE"] == str(info.wine)
    assert env["WINEESYNC"] == "1"
    assert env["WINEFSYNC"] == "0"
    assert env["HOME"] == "/home/alice"
    assert env["DISPLAY"] == ":0"
    for dropped in ("WINEDLLPATH", "LD_LIBRARY_PATH", "SNAP", "SNAP_NAME"):
        assert dropped not in env
    assert base["WINEPREFIX"] == "/home/alice/.wine"


def test_build_env_without_path_uses_default_search_path(paths: Paths, info: WineInfo) -> None:
    env = winecmd.build_env(paths, info, user="alice", base_env={})
    assert env["PATH"] == f"{info.root / 'bin'}:{os.defpath}"


def test_build_env_debug_and_extra(paths: Paths, info: WineInfo) -> None:
    env = winecmd.build_env(
        paths,
        info,
        user="bob",
        base_env={"PATH": "/usr/bin"},
        debug="+seh",
        extra={"WINEDEBUG": "+relay", "GIT_CONFIG_COUNT": "1"},
    )
    assert env["WINEDEBUG"] == "+relay"
    assert env["GIT_CONFIG_COUNT"] == "1"


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ("winemenubuilder.exe=d", "winemenubuilder.exe=d"),
        ("mscoree,mshtml=;winemenubuilder.exe=d", "mscoree,mshtml=;winemenubuilder.exe=d"),
        ("dwrite=n,b", "dwrite=n,b;winemenubuilder.exe=d"),
        ("winemenubuilder.exe=n;d3d9=b", "d3d9=b;winemenubuilder.exe=d"),
        ("mscoree,WineMenuBuilder=n", "mscoree=n;winemenubuilder.exe=d"),
        ("", "winemenubuilder.exe=d"),
        ("d3d9=b;;", "d3d9=b;winemenubuilder.exe=d"),
        ("mshtml", "mshtml;winemenubuilder.exe=d"),
    ],
)
def test_build_env_always_disables_winemenubuilder(
    paths: Paths, info: WineInfo, overrides: str, expected: str
) -> None:
    env = winecmd.build_env(paths, info, user="alice", base_env={}, dll_overrides=overrides)
    assert env["WINEDLLOVERRIDES"] == expected


def test_build_env_refuses_unsafe_user(paths: Paths, info: WineInfo) -> None:
    with pytest.raises(UsageError):
        winecmd.build_env(paths, info, user="..\\x", base_env={})
    with pytest.raises(UsageError):
        winecmd.build_env(paths, info, user="", base_env={})


# --------------------------------------------------------------------------- run / wineserver


def test_run_passes_everything_through(info: WineInfo, tmp_path: Path) -> None:
    runner = RecordingRunner()
    result = winecmd.run(
        runner, {"A": "1"}, info, ["wineboot", "--init"], cwd=tmp_path, timeout=60, log_file=None, check=True
    )
    assert result.ok
    call = runner.calls[0]
    assert call["argv"] == [str(info.wine), "wineboot", "--init"]
    assert call["env"] == {"A": "1"}
    assert call["cwd"] == tmp_path
    assert call["timeout"] == 60
    assert call["check"] is True


def test_run_defaults(info: WineInfo) -> None:
    runner = RecordingRunner()
    winecmd.run(runner, {}, info, ["--help"])
    call = runner.calls[0]
    assert (call["cwd"], call["timeout"], call["log_file"], call["check"]) == (None, None, None, False)


def test_wineserver_wait(info: WineInfo) -> None:
    runner = RecordingRunner()
    assert winecmd.wineserver(runner, {"B": "2"}, info).ok
    call = runner.calls[0]
    assert call["argv"] == [str(info.wineserver), "-w"]
    assert call["check"] is True
    assert call["timeout"] == 300
    assert call["env"] == {"B": "2"}


def test_wineserver_wait_failure_raises(info: WineInfo) -> None:
    with pytest.raises(CommandError):
        winecmd.wineserver(RecordingRunner({"wineserver": 1}), {}, info, "-w", timeout=5)


def test_wineserver_kill_tolerates_failure(info: WineInfo) -> None:
    runner = RecordingRunner({"wineserver": 1})
    result = winecmd.wineserver(runner, {}, info, "-k", timeout=10)
    assert result.returncode == 1
    assert runner.calls[0]["check"] is False
    assert runner.calls[0]["timeout"] == 10


def test_wineserver_rejects_other_flags(info: WineInfo) -> None:
    with pytest.raises(ValueError, match="unsupported wineserver flag"):
        winecmd.wineserver(RecordingRunner(), {}, info, "-p")


# --------------------------------------------------------------------------- import_reg


def _batch() -> RegBatch:
    return RegBatch().set_dword("HKCU\\Software\\Microsoft\\Avalon.Graphics", "DisableHWAcceleration", 1)


def test_import_reg_writes_imports_and_waits(paths: Paths, info: WineInfo) -> None:
    runner = RecordingRunner()
    env = {"WINEPREFIX": str(paths.prefix)}
    reg = winecmd.import_reg(runner, env, info, _batch(), paths, "alice")
    assert reg == paths.prefix / "drive_c" / "fork-linux" / "tmp" / "fork-linux.reg"
    data = reg.read_bytes()
    assert data.startswith(b"\xff\xfe")
    assert "DisableHWAcceleration" in data.decode("utf-16")
    assert stat.S_IMODE(reg.parent.stat().st_mode) == 0o700
    assert runner.argvs == [
        [str(info.wine), "regedit", "/S", "C:\\fork-linux\\tmp\\fork-linux.reg"],
        [str(info.wineserver), "-w"],
    ]
    assert runner.calls[0]["env"]["WINEHOME"] == "C:\\users\\alice"
    assert runner.calls[1]["env"]["WINEHOME"] == "C:\\users\\alice"
    assert "WINEHOME" not in env


def test_import_reg_keeps_existing_winehome_and_custom_name(paths: Paths, info: WineInfo, tmp_path: Path) -> None:
    runner = RecordingRunner()
    log_file = tmp_path / "wine.log"
    env = {"WINEHOME": "C:\\users\\other"}
    reg = winecmd.import_reg(
        runner, env, info, _batch(), paths, "alice", name="step-06.v2", timeout=30, log_file=log_file
    )
    assert reg.name == "step-06.v2.reg"
    assert runner.calls[0]["argv"][-1] == "C:\\fork-linux\\tmp\\step-06.v2.reg"
    assert runner.calls[0]["env"]["WINEHOME"] == "C:\\users\\other"
    assert runner.calls[0]["timeout"] == 30
    assert runner.calls[0]["log_file"] == log_file
    assert runner.calls[1]["timeout"] == 30


def test_import_reg_failure_with_output(paths: Paths, info: WineInfo) -> None:
    runner = RecordingRunner({"wine": Completed([], 1, "", "regedit: Unable to open the registry file\n")})
    with pytest.raises(ForkLinuxError, match="importing registry settings failed \\(exit code 1\\)") as excinfo:
        winecmd.import_reg(runner, {}, info, _batch(), paths, "alice")
    assert "Unable to open" in str(excinfo.value)
    assert "fork-linux.reg" in excinfo.value.hint
    assert len(runner.calls) == 1


def test_import_reg_failure_without_output(paths: Paths, info: WineInfo) -> None:
    runner = RecordingRunner({"wine": 3})
    with pytest.raises(ForkLinuxError) as excinfo:
        winecmd.import_reg(runner, {}, info, _batch(), paths, "alice")
    assert "\n" not in str(excinfo.value)


@pytest.mark.parametrize("name", ["", "../x", "a/b", ".hidden", "x" * 65, "a b"])
def test_import_reg_rejects_bad_names(paths: Paths, info: WineInfo, name: str) -> None:
    with pytest.raises(ValueError, match="plain file name"):
        winecmd.import_reg(RecordingRunner(), {}, info, _batch(), paths, "alice", name=name)


def test_import_reg_rejects_bad_user(paths: Paths, info: WineInfo) -> None:
    with pytest.raises(UsageError):
        winecmd.import_reg(RecordingRunner(), {}, info, _batch(), paths, "a/b")


def test_import_reg_with_fake_wine(paths: Paths, fake_bin: Path) -> None:
    info = _info(FAKES_BIN.parent)
    env = winecmd.build_env(paths, info, user="alice", base_env=dict(os.environ))
    runner = Runner()
    winecmd.run(runner, env, info, ["wineboot", "--init"], check=True)
    reg = winecmd.import_reg(runner, env, info, _batch(), paths, "alice")
    imported = (paths.prefix / "fake-regedit.log").read_text(encoding="utf-8")
    assert "[HKEY_CURRENT_USER\\Software\\Microsoft\\Avalon.Graphics]" in imported
    assert reg.is_file()
    assert (paths.prefix / "drive_c" / "users" / "alice").is_dir()
    logged = [json.loads(line) for line in fake_bin.read_text(encoding="utf-8").splitlines()]
    assert [entry["argv"] for entry in logged] == [
        ["wine", "wineboot", "--init"],
        ["wine", "regedit", "/S", "C:\\fork-linux\\tmp\\fork-linux.reg"],
        ["wineserver", "-w"],
    ]
    for entry in logged:
        assert entry["env"]["WINEPREFIX"] == str(paths.prefix)
        assert entry["env"]["WINEDLLOVERRIDES"] == "winemenubuilder.exe=d"
    assert logged[1]["env"]["WINEHOME"] == "C:\\users\\alice"


def test_import_reg_with_failing_fake_wine(paths: Paths, fake_bin: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FL_FAKE_WINE_RC", "5")
    info = _info(FAKES_BIN.parent)
    env = winecmd.build_env(paths, info, user="alice", base_env=dict(os.environ))
    with pytest.raises(ForkLinuxError, match="exit code 5") as excinfo:
        winecmd.import_reg(Runner(), env, info, _batch(), paths, "alice")
    assert "simulated failure" in str(excinfo.value)
