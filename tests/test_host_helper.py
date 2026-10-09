"""Tests for fork_linux.host_helper and the libexec/fork-linux-host launcher."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from fork_linux import host_helper as hh
from fork_linux import sandbox
from fork_linux.errors import ForkLinuxError, NotFound, UsageError
from fork_linux.procrun import Completed, RecordingRunner

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "libexec" / "fork-linux-host.in"
PACKAGE = ROOT / "src" / "fork_linux"


@pytest.fixture(autouse=True)
def _no_flatpak(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sandbox, "FLATPAK_INFO", tmp_path / "no-flatpak-info")


class _Popen:
    """Records detached spawns; optionally raises."""

    def __init__(self, error: OSError | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.error = error

    def __call__(self, argv: list[str], **kwargs: Any) -> object:
        self.calls.append({"argv": argv, **kwargs})
        if self.error is not None:
            raise self.error
        return object()

    @property
    def argvs(self) -> list[list[str]]:
        return [call["argv"] for call in self.calls]


def _host(
    env: dict[str, str] | None = None,
    installed: tuple[str, ...] = (),
    responses: dict[str, Any] | None = None,
    popen: _Popen | None = None,
) -> tuple[hh.Host, RecordingRunner, _Popen]:
    names = {name for name, _args in hh.TERMINALS} | {name for name, _d, _m in hh.DIFF_TOOLS}
    names |= {"xdg-terminal-exec", "gdbus", "dbus-send", "xdg-open", "my-term", "weird-term"}
    which = {name: (f"/usr/bin/{name}" if name in installed else None) for name in names}
    runner = RecordingRunner(responses=responses or {}, which_map=which)
    spawner = popen or _Popen()
    environ = {"PATH": "/usr/bin", "HOME": "/home/u", FORK_TERMINAL_CONFIG: "auto"}
    environ.update(env or {})
    return hh.Host(environ, runner=runner, popen=spawner), runner, spawner


FORK_TERMINAL_CONFIG = "FORK_LINUX_INTEGRATION_TERMINAL"


def _prefix(tmp_path: Path) -> Path:
    """A prefix whose C: is drive_c and Z: is /."""
    prefix = tmp_path / "prefix"
    (prefix / "drive_c").mkdir(parents=True)
    (prefix / "dosdevices").mkdir()
    os.symlink("../drive_c", prefix / "dosdevices" / "c:")
    os.symlink("/", prefix / "dosdevices" / "z:")
    return prefix


# --- small helpers -------------------------------------------------------------------------


def test_fill_is_a_single_pass() -> None:
    assert hh.fill(["--x={left}", "{right}", "{other}"], {"left": "{right}", "right": "R"}) == [
        "--x={right}",
        "R",
        "{other}",
    ]


def test_file_uri_encodes_everything_unsafe() -> None:
    assert hh.file_uri("/a b/it's,\"x\"") == "file:///a%20b/it%27s%2C%22x%22"
    assert hh.file_uri(os.fsdecode(b"/caf\xe9")) == "file:///caf%E9"


def test_tool_env_drops_wine_and_bundle_variables() -> None:
    host, _runner, _popen = _host(
        {"WINEPREFIX": "/p", "WINEDEBUG": "-all", "WINE_X": "1", "LD_LIBRARY_PATH": "/bundle", "KEEP": "1"}
    )
    assert "KEEP" in host.tool_env
    assert not any(key.startswith("WINE") for key in host.tool_env)
    assert "LD_LIBRARY_PATH" not in host.tool_env
    assert host.env["WINEPREFIX"] == "/p"


def test_host_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FL_HOST_TEST", "1")
    host = hh.Host()
    assert host.env["FL_HOST_TEST"] == "1"
    assert isinstance(host.runner, hh.Runner)
    assert host.popen is subprocess.Popen


# --- lookup and spawning ---------------------------------------------------------------------


def test_which_inside_flatpak() -> None:
    answers = [Completed([], 0, "/usr/bin/konsole\n"), Completed([], 1, ""), Completed([], 0, "  ")]
    host, runner, _popen = _host({"FLATPAK_ID": "x"}, responses={"flatpak-spawn": answers})
    assert host.kind == sandbox.FLATPAK
    assert host.which("konsole") == "/usr/bin/konsole"
    assert host.which("nope") is None
    assert host.which("blank") is None
    assert runner.argvs[0] == ["flatpak-spawn", "--host", "sh", "-c", 'command -v -- "$1"', "sh", "konsole"]


def test_spawn(tmp_path: Path) -> None:
    host, _runner, popen = _host()
    assert host.spawn(["xdg-open", "/x"], cwd=str(tmp_path)) == 0
    call = popen.calls[0]
    assert call["argv"] == ["xdg-open", "/x"]
    assert call["cwd"] == str(tmp_path)
    assert call["start_new_session"] is True
    assert call["stdin"] == subprocess.DEVNULL and call["stdout"] == subprocess.DEVNULL
    assert call["env"] is host.tool_env


def test_spawn_inside_flatpak() -> None:
    host, _runner, popen = _host({"FLATPAK_ID": "x"})
    host.spawn(["xdg-open", "/x"])
    assert popen.argvs == [["flatpak-spawn", "--host", "xdg-open", "/x"]]


def test_spawn_failures() -> None:
    host, _runner, _popen = _host(popen=_Popen(FileNotFoundError("x")))
    with pytest.raises(hh.ToolNotFound):
        host.spawn(["xdg-open", "/x"])
    host, _runner, _popen = _host(popen=_Popen(PermissionError(13, "Permission denied")))
    with pytest.raises(ForkLinuxError, match="cannot start xdg-open: Permission denied"):
        host.spawn(["xdg-open", "/x"])


def test_wait() -> None:
    missing = Completed([], 127, "", "fork-linux: cannot execute meld: not found\n")
    host, runner, _popen = _host(responses={"meld": [3, Completed([], 127, "", "inner"), missing]})
    assert host.wait(["meld", "a", "b"]) == 3
    assert host.wait(["meld", "a", "b"]) == 127
    with pytest.raises(hh.ToolNotFound):
        host.wait(["meld", "a", "b"])
    assert runner.calls[0]["env"] == host.tool_env


# --- path translation ---------------------------------------------------------------------------


def test_path_translation(tmp_path: Path) -> None:
    prefix = _prefix(tmp_path)
    host, _runner, _popen = _host({"WINEPREFIX": str(prefix)})
    assert host.path("Z:\\home\\u\\repo") == "/home/u/repo"
    assert host.path("C:\\users\\u") == str(prefix / "drive_c" / "users" / "u")
    assert host.path("\\\\?\\unix\\srv\\x") == "/srv/x"
    assert host.path("/already/unix") == "/already/unix"
    assert host.path("https://example.com") == "https://example.com"
    with pytest.raises(UsageError):
        host.path("Q:\\unmapped")


def test_path_without_a_usable_prefix(tmp_path: Path) -> None:
    host, _runner, _popen = _host()
    assert host.path("Z:\\x") == "Z:\\x"
    host, _runner, _popen = _host({"WINEPREFIX": str(tmp_path / "not-a-prefix")})
    assert host.path("Z:\\x") == "Z:\\x"


def test_existing(tmp_path: Path) -> None:
    host, _runner, _popen = _host()
    assert host.existing(str(tmp_path)) == str(tmp_path)
    with pytest.raises(UsageError, match="no such file or directory"):
        host.existing(str(tmp_path / "missing"))


# --- terminal ---------------------------------------------------------------------------------------


def test_terminal_from_fork_linux_terminal_env(tmp_path: Path) -> None:
    host, _runner, popen = _host({hh.TERMINAL_ENV: "my-term --cd={dir} --title 'Fork repo'"})
    assert host.terminal(str(tmp_path)) == 0
    assert popen.calls[0]["argv"] == ["my-term", f"--cd={tmp_path}", "--title", "Fork repo"]
    assert popen.calls[0]["cwd"] == str(tmp_path)


def test_terminal_without_a_directory_uses_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fork's Console runs ``fl-launch.exe terminal`` in the repository, with no argument."""
    monkeypatch.chdir(tmp_path)
    host, _runner, popen = _host({hh.TERMINAL_ENV: "my-term --cd={dir}"})
    assert hh.main(["terminal"], host=host) == 0
    assert popen.calls[0]["argv"] == ["my-term", f"--cd={tmp_path}"] and popen.calls[0]["cwd"] == str(tmp_path)
    assert hh.main(["terminal", "a", "b"], host=host) == hh.EXIT_USAGE


def test_terminal_configured_names(tmp_path: Path) -> None:
    host, _runner, _popen = _host({FORK_TERMINAL_CONFIG: "konsole"})
    assert host.terminal_argv("/r") == ["konsole", "--workdir", "/r"]
    host, _runner, _popen = _host({FORK_TERMINAL_CONFIG: "/opt/kitty/bin/kitty"})
    assert host.terminal_argv("/r") == ["/opt/kitty/bin/kitty", "--directory", "/r"]
    host, _runner, _popen = _host({FORK_TERMINAL_CONFIG: "weird-term -e bash"})
    assert host.terminal_argv("/r") == ["weird-term", "-e", "bash"]
    host, _runner, _popen = _host({FORK_TERMINAL_CONFIG: "weird-term 'unbalanced"})
    assert host.terminal_argv("/r") == ["weird-term", "'unbalanced"]


def test_terminal_reads_config_ini(xdg: Path) -> None:
    config = xdg / ".config" / "fork-linux" / "config.ini"
    config.parent.mkdir(parents=True)
    config.write_text("[integration]\nterminal = gnome-terminal\n", encoding="utf-8")
    env = {key: os.environ[key] for key in ("HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_RUNTIME_DIR")}
    host = hh.Host(env, runner=RecordingRunner(which_map={}), popen=_Popen())
    assert host.terminal_argv("/r") == ["gnome-terminal", "--working-directory=/r"]
    config.write_text("not an ini file\n", encoding="utf-8")
    host = hh.Host(env, runner=RecordingRunner(which_map={"xdg-terminal-exec": "/x"}), popen=_Popen())
    assert host.terminal_argv("/r") == ["xdg-terminal-exec", "--dir=/r"]


def test_terminal_detection_order() -> None:
    host, _runner, _popen = _host(installed=("xdg-terminal-exec", "konsole"))
    assert host.terminal_argv("/r") == ["xdg-terminal-exec", "--dir=/r"]
    host, _runner, _popen = _host({"TERMINAL": "alacritty"}, installed=("alacritty", "konsole"))
    assert host.terminal_argv("/r") == ["alacritty", "--working-directory", "/r"]
    host, _runner, _popen = _host({"TERMINAL": "missing-term"}, installed=("xterm", "gnome-terminal"))
    assert host.terminal_argv("/r") == ["gnome-terminal", "--working-directory=/r"]
    host, _runner, _popen = _host({"XDG_CURRENT_DESKTOP": "ubuntu:KDE"}, installed=("gnome-terminal", "konsole"))
    assert host.terminal_argv("/r") == ["konsole", "--workdir", "/r"]
    host, _runner, _popen = _host(installed=("x-terminal-emulator",))
    assert host.terminal_argv("/r") == ["x-terminal-emulator"]
    host, _runner, _popen = _host()
    with pytest.raises(hh.ToolNotFound, match="no terminal emulator found"):
        host.terminal_argv("/r")


@pytest.mark.parametrize(
    ("name", "args"),
    [
        ("ptyxis", ["--new-window", "-d", "/r"]),
        ("kgx", ["--working-directory=/r"]),
        ("tilix", ["--working-directory=/r"]),
        ("wezterm", ["start", "--cwd", "/r"]),
        ("ghostty", ["--working-directory=/r"]),
        ("foot", ["--working-directory=/r"]),
        ("xterm", []),
    ],
)
def test_terminal_table(name: str, args: list[str]) -> None:
    host, _runner, _popen = _host(installed=(name,))
    assert host.terminal_argv("/r") == [name, *args]


def test_terminal_with_a_file_opens_its_folder(tmp_path: Path) -> None:
    file = tmp_path / "README.md"
    file.write_text("x", encoding="utf-8")
    host, _runner, popen = _host(installed=("konsole",))
    host.terminal(str(file))
    assert popen.calls[0]["argv"] == ["konsole", "--workdir", str(tmp_path)]
    assert popen.calls[0]["cwd"] == str(tmp_path)


# --- reveal, open, edit -------------------------------------------------------------------------------


def test_reveal_uses_filemanager1(tmp_path: Path) -> None:
    target = tmp_path / "a b.txt"
    target.write_text("x", encoding="utf-8")
    host, runner, popen = _host(installed=("gdbus", "dbus-send", "xdg-open"))
    assert host.reveal(str(target)) == 0
    uri = hh.file_uri(target)
    assert runner.argvs == [
        [
            "gdbus", "call", "--session", "--dest", hh.FILE_MANAGER1, "--object-path", hh.FILE_MANAGER1_PATH,
            "--method", f"{hh.FILE_MANAGER1}.ShowItems", f"['{uri}']", "",
        ]
    ]
    assert runner.calls[0]["timeout"] == hh.DBUS_TIMEOUT
    assert popen.calls == []


def test_reveal_falls_back(tmp_path: Path) -> None:
    host, runner, popen = _host(installed=("gdbus", "dbus-send", "xdg-open"), responses={"gdbus": 1})
    assert host.reveal(str(tmp_path)) == 0
    uri = hh.file_uri(tmp_path)
    assert runner.argvs[1] == [
        "dbus-send", "--session", "--print-reply", f"--dest={hh.FILE_MANAGER1}", hh.FILE_MANAGER1_PATH,
        f"{hh.FILE_MANAGER1}.ShowItems", f"array:string:{uri}", "string:",
    ]
    assert popen.calls == []

    def timeout(argv: list[str]) -> Completed:
        raise ForkLinuxError("timed out")

    host, runner, popen = _host(installed=("dbus-send",), responses={"dbus-send": timeout})
    assert host.reveal(str(tmp_path)) == 0
    assert popen.argvs == [["xdg-open", str(tmp_path.parent)]]


@pytest.mark.parametrize(
    "url", ["https://github.com/fork-dev/TrackerWin", "http://localhost:8080/x", "mailto:dev@example.com"]
)
def test_open_urls(url: str) -> None:
    host, _runner, popen = _host()
    assert host.open(url) == 0
    assert popen.argvs == [["xdg-open", url]]


@pytest.mark.parametrize("url", ["javascript:alert(1)", "file:///etc/passwd", "steam://run/1", "smb://host/share"])
def test_open_refuses_other_schemes(url: str) -> None:
    host, _runner, popen = _host()
    with pytest.raises(UsageError, match="refusing to open"):
        host.open(url)
    assert popen.calls == []


def test_open_files_and_windows_paths(tmp_path: Path) -> None:
    prefix = _prefix(tmp_path)
    target = prefix / "drive_c" / "notes.txt"
    target.write_text("x", encoding="utf-8")
    host, _runner, popen = _host({"WINEPREFIX": str(prefix)})
    assert host.open("C:\\notes.txt") == 0
    assert host.open(str(target)) == 0
    assert popen.argvs == [["xdg-open", str(target)], ["xdg-open", str(target)]]
    with pytest.raises(UsageError):
        host.open("C:\\missing.txt")


def test_edit(tmp_path: Path) -> None:
    target = tmp_path / "f.py"
    target.write_text("x", encoding="utf-8")
    host, runner, _popen = _host(responses={"xdg-open": 4})
    assert host.edit(str(target)) == 4
    assert host.edit(str(target), "12") == 4
    assert runner.argvs == [["xdg-open", str(target)]] * 2
    with pytest.raises(UsageError, match="not a line number"):
        host.edit(str(target), "twelve")


# --- diff and merge -------------------------------------------------------------------------------------


def _files(tmp_path: Path, *names: str) -> list[str]:
    paths = []
    for name in names:
        path = tmp_path / name
        path.write_text(name, encoding="utf-8")
        paths.append(str(path))
    return paths


@pytest.mark.parametrize(
    ("tool", "diff", "merge"),
    [
        ("meld", ["L", "R"], ["--output=M", "LO", "B", "RE"]),
        ("kdiff3", ["L", "R"], ["--auto", "B", "LO", "RE", "-o", "M"]),
        ("bcompare", ["L", "R"], ["LO", "RE", "B", "-mergeoutput=M"]),
        ("code", ["--wait", "--diff", "L", "R"], ["--wait", "--merge", "RE", "LO", "B", "M"]),
    ],
)
def test_diff_and_merge_templates(tmp_path: Path, tool: str, diff: list[str], merge: list[str]) -> None:
    left, right, base, local, remote = _files(tmp_path, "L", "R", "B", "LO", "RE")
    merged = str(tmp_path / "M")
    host, runner, _popen = _host(installed=(tool,), responses={tool: 1})
    assert host.diff(left, right) == 1
    assert host.merge(base, local, remote, merged) == 1
    names = {"L": left, "R": right, "B": base, "LO": local, "RE": remote, "M": merged}

    def expand(args: list[str]) -> list[str]:
        out = []
        for arg in args:
            for short in sorted(names, key=len, reverse=True):
                if arg == short or arg.endswith("=" + short):
                    arg = arg[: len(arg) - len(short)] + names[short]
                    break
            out.append(arg)
        return out

    assert runner.argvs == [[tool, *expand(diff)], [tool, *expand(merge)]]


def test_kompare_only_diffs(tmp_path: Path) -> None:
    left, right, base = _files(tmp_path, "L", "R", "B")
    host, runner, _popen = _host(installed=("kompare",))
    assert host.diff(left, right) == 0
    assert runner.argvs == [["kompare", left, right]]
    with pytest.raises(hh.ToolNotFound, match="no merge tool found"):
        host.merge(base, left, right, str(tmp_path / "out"))


def test_diff_prefers_the_first_installed_tool(tmp_path: Path) -> None:
    left, right = _files(tmp_path, "L", "R")
    host, runner, _popen = _host(installed=("code", "kdiff3"))
    host.diff(left, right)
    assert runner.argvs[0][0] == "kdiff3"
    host, _runner, _popen = _host()
    with pytest.raises(hh.ToolNotFound, match="no diff tool found"):
        host.diff(left, right)


# --- main -------------------------------------------------------------------------------------------------


def test_main_usage(capsys: pytest.CaptureFixture[str]) -> None:
    assert hh.main(["--help"]) == 0
    assert capsys.readouterr().out.startswith(f"usage: {hh.PROG} VERB")
    assert hh.main([]) == hh.EXIT_USAGE
    assert "expected a verb" in capsys.readouterr().err
    assert hh.main(["frobnicate"]) == hh.EXIT_USAGE
    assert "bad usage of 'frobnicate'" in capsys.readouterr().err
    assert hh.main(["diff", "only-one"]) == hh.EXIT_USAGE
    assert hh.main(["edit", "a", "1", "extra"]) == hh.EXIT_USAGE


def test_main_runs_the_verb(tmp_path: Path) -> None:
    host, runner, _popen = _host(installed=("meld",), responses={"meld": 7})
    left, right = _files(tmp_path, "L", "R")
    assert hh.main(["diff", left, right], host=host) == 7
    assert runner.argvs == [["meld", left, right]]


def test_main_maps_errors(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    host, _runner, _popen = _host()
    assert hh.main(["terminal", str(tmp_path)], host=host) == hh.EXIT_NOT_FOUND
    err = capsys.readouterr().err
    assert "fork-linux-host: error: no terminal emulator found" in err and "hint: install one" in err
    assert hh.main(["reveal", str(tmp_path / "missing")], host=host) == hh.EXIT_USAGE

    class Failing(hh.Host):
        def open(self, target: str) -> int:
            raise NotFound("gone")

    failing = Failing({}, runner=RecordingRunner(), popen=_Popen())
    assert hh.main(["open", "x"], host=failing) == 20
    assert "error: gone" in capsys.readouterr().err

    class Interrupted(hh.Host):
        def open(self, target: str) -> int:
            raise KeyboardInterrupt

    assert hh.main(["open", "x"], host=Interrupted({}, runner=RecordingRunner(), popen=_Popen())) == 130


def test_main_defaults_to_sys_argv(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(sys, "argv", ["fork-linux-host", "help"])
    assert hh.main() == 0
    assert "verbs:" in capsys.readouterr().out


# --- with the fake host tools ---------------------------------------------------------------------------------


def _wait_for_calls(log: Path, count: int) -> list[dict[str, Any]]:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if log.exists():
            calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
            if len(calls) >= count:
                return calls
        time.sleep(0.02)
    raise AssertionError(f"expected {count} fake call(s) in {log}")


def test_fake_tools_end_to_end(xdg: Path, fake_bin: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    prefix = _prefix(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    for name in ("base", "local", "remote"):
        (repo / name).write_text(name, encoding="utf-8")
    monkeypatch.setenv("WINEPREFIX", str(prefix))
    monkeypatch.setenv("WINEDEBUG", "-all")
    monkeypatch.setenv(FORK_TERMINAL_CONFIG, "auto")
    monkeypatch.delenv("TERMINAL", raising=False)
    monkeypatch.delenv(hh.TERMINAL_ENV, raising=False)
    win_repo = "Z:" + str(repo).replace("/", "\\")
    # terminal: xdg-terminal-exec is a fake on PATH, started detached in the repository.
    assert hh.main(["terminal", win_repo]) == 0
    call = _wait_for_calls(fake_bin, 1)[0]
    assert call["argv"] == ["xdg-terminal-exec", f"--dir={repo}"]
    assert call["cwd"] == str(repo)
    assert "WINEPREFIX" not in call["env"] and "WINEDEBUG" not in call["env"]
    # reveal: the fake gdbus answers.
    assert hh.main(["reveal", win_repo + "\\base"]) == 0
    assert _wait_for_calls(fake_bin, 2)[1]["argv"][-2] == f"['{hh.file_uri(repo / 'base')}']"
    # merge: the fake meld writes the result and its status is returned.
    monkeypatch.setenv("FL_FAKE_MELD_RC", "3")
    merged = repo / "merged"
    assert hh.main(["merge", str(repo / "base"), str(repo / "local"), str(repo / "remote"), str(merged)]) == 3
    assert merged.read_text(encoding="utf-8") == "merged by fake meld\n"
    assert _wait_for_calls(fake_bin, 3)[2]["argv"] == [
        "meld", f"--output={merged}", str(repo / "local"), str(repo / "base"), str(repo / "remote"),
    ]


# --- the libexec launcher ---------------------------------------------------------------------------------------


def _render(target: Path) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(TEMPLATE.read_text(encoding="utf-8").replace("@PYTHON@", sys.executable), encoding="utf-8")
    target.chmod(0o755)
    return target


def _copy_package(dest: Path) -> None:
    shutil.copytree(PACKAGE, dest / "fork_linux", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))


def _run(launcher: Path, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
    environ = {"PATH": "/usr/bin:/bin", "HOME": str(launcher.parent), "PYTHONDONTWRITEBYTECODE": "1", **env}
    return subprocess.run([str(launcher), *args], capture_output=True, text=True, env=environ, check=False)


def test_launcher_template_shape() -> None:
    text = TEMPLATE.read_text(encoding="utf-8")
    assert text.startswith("#!@PYTHON@ -I\n")
    assert "from fork_linux.host_helper import main" in text
    assert "main(sys.argv[1:])" in text


def test_launcher_installed_layout(tmp_path: Path) -> None:
    root = tmp_path / "usr"
    launcher = _render(root / "lib" / "fork-linux" / "fork-linux-host")
    _copy_package(root / "share" / "fork-linux")
    result = _run(launcher, "--help")
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("usage: fork-linux-host")
    assert _run(launcher).returncode == 2


def test_launcher_source_checkout(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    launcher = _render(repo / "libexec" / "fork-linux-host")
    _copy_package(repo / "src")
    (repo / "VERSION").write_text("0.1.0\n", encoding="utf-8")
    result = _run(launcher, "help")
    assert result.returncode == 0, result.stderr
    # Without a VERSION/meson.build marker the checkout is not trusted.
    (repo / "VERSION").unlink()
    assert _run(launcher, "help").returncode != 0


def test_launcher_libdir_override(tmp_path: Path) -> None:
    launcher = _render(tmp_path / "anywhere" / "fork-linux-host")
    _copy_package(tmp_path / "lib")
    result = _run(launcher, "--help", FORK_LINUX_LIBDIR=str(tmp_path / "lib"))
    assert result.returncode == 0, result.stderr
