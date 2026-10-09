"""Tests for fork_linux.procrun: the real Runner and the RecordingRunner test double."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path
from typing import Any

import pytest

from fork_linux import procrun
from fork_linux.errors import ExitCode, ForkLinuxError
from fork_linux.procrun import CommandError, Completed, RecordingRunner, Runner, format_argv, tail

PY = sys.executable
TOKEN = "ghp_" + "A" * 36


def _py(code: str) -> list[str]:
    return [PY, "-c", code]


# --------------------------------------------------------------------------- Completed / helpers


def test_completed_defaults_and_ok() -> None:
    done = Completed(["x"], 0)
    assert (done.stdout, done.stderr, done.ok) == ("", "", True)
    assert not Completed(["x"], 3, "", "").ok


def test_format_argv_quotes_and_redacts() -> None:
    assert format_argv(["echo", "a b", "c"]) == "echo 'a b' c"
    assert TOKEN not in format_argv(["git", "clone", f"https://{TOKEN}@github.com/x/y"])
    assert "s3cret" not in format_argv(["curl", "https://me:s3cret@example.com/"])


def test_tail_keeps_last_lines() -> None:
    text = "\n".join(str(i) for i in range(50)) + "\n\n"
    assert tail(text, 3) == "47\n48\n49"
    assert tail("") == ""


# --------------------------------------------------------------------------- Runner


def test_run_captures_stdout_and_stderr() -> None:
    done = Runner().run(_py("import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)"))
    assert done.returncode == 3
    assert done.stdout == "out\n"
    assert done.stderr == "err\n"
    assert done.argv[0] == PY


def test_run_decodes_invalid_utf8_with_replacement() -> None:
    done = Runner().run(_py("import sys; sys.stdout.buffer.write(b'ok\\xff\\n')"))
    assert done.stdout == "ok\ufffd\n"


def test_run_accepts_path_arguments(tmp_path: Path) -> None:
    script = tmp_path / "s.py"
    script.write_text("print('from file')\n", encoding="utf-8")
    assert Runner().run([Path(PY), script]).stdout == "from file\n"


def test_run_rejects_string_and_empty_argv() -> None:
    runner = Runner()
    with pytest.raises(TypeError):
        runner.run("echo hi")
    with pytest.raises(ValueError):
        runner.run([])


def test_run_stdin_is_devnull_unless_input_given() -> None:
    assert Runner().run(_py("import sys; print(repr(sys.stdin.read()))")).stdout == "''\n"
    assert Runner().run(_py("import sys; print(sys.stdin.read().upper())"), input="abc").stdout == "ABC\n"


def test_run_env_replaces_environment_and_cwd(tmp_path: Path) -> None:
    done = Runner().run(
        _py("import os; print(os.environ.get('FL_X'), os.environ.get('HOME'), os.getcwd())"),
        env={"FL_X": "1", "PATH": os.environ.get("PATH", "")},
        cwd=tmp_path,
    )
    assert done.stdout.split() == ["1", "None", str(tmp_path)]


def test_run_check_raises_with_command_and_stderr_tail() -> None:
    code = (
        "import sys\n"
        "for i in range(40): print('line', i, file=sys.stderr)\n"
        f"print('token {TOKEN}', file=sys.stderr)\n"
        "sys.exit(5)\n"
    )
    runner, argv = Runner(), _py(code)
    with pytest.raises(CommandError) as info:
        runner.run(argv, check=True)
    err = info.value
    assert err.exit_code == ExitCode.ERROR
    assert isinstance(err, ForkLinuxError)
    assert "exit code 5" in err.message
    assert PY in err.message
    assert "line 39" in err.message
    assert "line 5\n" not in err.message, "only the tail is included"
    assert TOKEN not in err.message
    assert err.completed.returncode == 5
    assert err.hint


def test_run_check_without_stderr() -> None:
    runner, argv = Runner(), _py("raise SystemExit(2)")
    with pytest.raises(CommandError) as info:
        runner.run(argv, check=True)
    assert info.value.message.endswith("-c 'raise SystemExit(2)'")


def test_run_check_success_returns_result() -> None:
    assert Runner().run(_py("print(1)"), check=True).stdout == "1\n"


def test_run_timeout_raises() -> None:
    runner, argv = Runner(), _py("import time; time.sleep(30)")
    with pytest.raises(ForkLinuxError, match="timed out after 0.2s") as info:
        runner.run(argv, timeout=0.2)
    assert not isinstance(info.value, CommandError)


def test_run_missing_executable_is_127(tmp_path: Path) -> None:
    missing = str(tmp_path / "no-such-tool")
    done = Runner().run([missing, "--version"])
    assert done.returncode == 127
    assert "cannot execute" in done.stderr
    runner = Runner()
    with pytest.raises(CommandError, match="exit code 127"):
        runner.run([missing], check=True)


def test_run_non_executable_is_126(tmp_path: Path) -> None:
    script = tmp_path / "not-exec.sh"
    script.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    script.chmod(0o644)
    assert Runner().run([str(script)]).returncode == 126


def test_run_log_file_appends_combined_output(tmp_path: Path) -> None:
    log_file = tmp_path / "logs" / "wine.log"
    runner = Runner()
    done = runner.run(_py("import sys; print('to-out'); print('to-err', file=sys.stderr)"), log_file=log_file)
    assert done.returncode == 0
    assert (done.stdout, done.stderr) == ("", "")
    runner.run(_py("print('second')"), log_file=log_file)
    text = log_file.read_text(encoding="utf-8")
    assert text.count("--- ") == 2
    assert "to-out" in text
    assert "to-err" in text
    assert "second" in text
    assert text.index("to-out") < text.index("second")


def test_run_log_file_check_uses_logged_tail(tmp_path: Path) -> None:
    log_file = tmp_path / "w.log"
    log_file.write_text("old noise that must not appear\n", encoding="utf-8")
    runner, argv = Runner(), _py("import sys; print('fatal' + ': broken', file=sys.stderr); sys.exit(9)")
    with pytest.raises(CommandError) as info:
        runner.run(argv, log_file=log_file, check=True)
    assert "fatal: broken" in info.value.message
    assert "old noise" not in info.value.message


def test_run_log_file_is_private_and_only_the_tail_is_read_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(procrun, "TAIL_BYTES", 64)
    log_file = tmp_path / "logs" / "big.log"
    code = (
        "import sys; print('HEAD' + '-MARKER'); print('x' * 5000, flush=True); "
        "print('fatal' + ': tail', file=sys.stderr); sys.exit(3)"
    )
    runner, argv = Runner(), _py(code)
    with pytest.raises(CommandError) as info:
        runner.run(argv, log_file=log_file, check=True)
    assert "fatal: tail" in info.value.message
    assert "HEAD-MARKER" not in info.value.message
    assert len(info.value.message) < 1000
    assert "HEAD-MARKER" in log_file.read_text(encoding="utf-8"), "the log itself keeps everything"
    assert stat.S_IMODE(log_file.stat().st_mode) == 0o600


def test_run_log_file_symlink_or_unwritable_is_a_fork_linux_error(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere.txt"
    target.write_text("keep\n", encoding="utf-8")
    link = tmp_path / "wine.log"
    link.symlink_to(target)
    runner, argv = Runner(), _py("print('x')")
    with pytest.raises(ForkLinuxError, match="cannot open the log file") as info:
        runner.run(argv, log_file=link)
    assert "symbolic link" in info.value.hint
    assert target.read_text(encoding="utf-8") == "keep\n"


def test_run_log_file_missing_executable(tmp_path: Path) -> None:
    log_file = tmp_path / "x.log"
    done = Runner().run([str(tmp_path / "nope")], log_file=log_file)
    assert done.returncode == 127
    assert "cannot execute" in done.stderr
    assert "cannot execute" in log_file.read_text(encoding="utf-8"), "exec failures are logged too"
    runner, argv = Runner(), [str(tmp_path / "nope")]
    with pytest.raises(CommandError) as info:
        runner.run(argv, log_file=log_file, check=True)
    assert info.value.message.count("cannot execute") == 1


def test_which(tmp_path: Path) -> None:
    runner = Runner()
    name = Path(PY).name
    assert runner.which(name, path=str(Path(PY).parent)) is not None
    assert runner.which("definitely-not-a-real-tool-xyz") is None
    assert runner.which(name, path=str(tmp_path)) is None


# --------------------------------------------------------------------------- RecordingRunner


def test_recording_runner_defaults_and_records(tmp_path: Path) -> None:
    runner = RecordingRunner()
    done = runner.run(["/opt/wine/bin/wine", Path("x.exe")], env={"A": "1"}, cwd=tmp_path, timeout=5)
    assert done == Completed(["/opt/wine/bin/wine", "x.exe"], 0, "", "")
    call = runner.calls[0]
    assert call["argv"] == ["/opt/wine/bin/wine", "x.exe"]
    assert call["env"] == {"A": "1"}
    assert call["cwd"] == tmp_path
    assert call["timeout"] == 5
    assert call["check"] is False
    assert call["input"] is None
    assert call["log_file"] is None
    assert runner.argvs == [["/opt/wine/bin/wine", "x.exe"]]


def test_recording_runner_response_kinds() -> None:
    runner = RecordingRunner(
        {
            "git": Completed(["ignored"], 0, "git version 2.50.0\n", ""),
            "wine": 3,
            "uname": "x86_64\n",
            "/usr/bin/exact": 7,
        }
    )
    git = runner.run(["/usr/bin/git", "--version"])
    assert git.argv == ["/usr/bin/git", "--version"]
    assert git.stdout == "git version 2.50.0\n"
    assert runner.run(["wine", "--version"]).returncode == 3
    assert runner.run(["uname", "-m"]).stdout == "x86_64\n"
    assert runner.run(["/usr/bin/exact"]).returncode == 7
    assert runner.run(["/other/exact"]).returncode == 0


def test_recording_runner_sequences() -> None:
    queue: list[Any] = [1, "ok\n"]
    runner = RecordingRunner({"wineserver": queue, "empty": []})
    assert runner.run(["wineserver", "-k"]).returncode == 1
    assert runner.run(["wineserver", "-k"]).stdout == "ok\n"
    assert runner.run(["wineserver", "-k"]).stdout == "ok\n", "the last response repeats"
    assert queue == [1, "ok\n"], "the caller's list is copied"
    assert runner.run(["empty"]).returncode == 0


def test_recording_runner_callables() -> None:
    seen: dict[str, Any] = {}

    def positional_only(argv: list[str]) -> Completed:
        return Completed(argv, 0, " ".join(argv), "")

    def with_env(argv: list[str], env: dict[str, str] | None = None) -> int:
        seen["env"] = env
        return 4

    def with_kwargs(argv: list[str], **kwargs: Any) -> str:
        seen.update(kwargs)
        return "kw\n"

    class Opaque:
        __signature__ = "not a signature"

        def __call__(self, argv: list[str]) -> int:
            return 11

    runner = RecordingRunner({"a": positional_only, "b": with_env, "c": with_kwargs, "d": Opaque()})
    assert runner.run(["a", "1"]).stdout == "a 1"
    assert runner.run(["b"], env={"K": "v"}).returncode == 4
    assert seen["env"] == {"K": "v"}
    assert runner.run(["c"], cwd=Path("/w"), input="in").stdout == "kw\n"
    assert seen["cwd"] == Path("/w")
    assert seen["input"] == "in"
    assert runner.run(["d"]).returncode == 11


def test_recording_runner_single_callable_answers_everything() -> None:
    def answer(argv: list[str], check: bool = False) -> Completed:
        return Completed(argv, 0 if argv[0] == "git" else 1, f"check={check}", "")

    runner = RecordingRunner(answer)
    assert runner.run(["git", "status"], check=True).stdout == "check=True"
    assert runner.run(["/usr/bin/wine"]).returncode == 1
    assert runner.argvs == [["git", "status"], ["/usr/bin/wine"]]


def test_recording_runner_rejects_unknown_response_type() -> None:
    runner = RecordingRunner({"x": 2.5})
    with pytest.raises(TypeError, match="unsupported"):
        runner.run(["x"])


def test_recording_runner_check() -> None:
    runner = RecordingRunner({"winetricks": Completed([], 1, "", "dotnet48 failed\n")})
    with pytest.raises(CommandError, match="dotnet48 failed"):
        runner.run(["winetricks", "-q", "dotnet48"], check=True)
    assert runner.calls[0]["check"] is True
    assert runner.run(["true"], check=True).ok


def test_recording_runner_log_file(tmp_path: Path) -> None:
    log_file = tmp_path / "logs" / "setup.log"
    runner = RecordingRunner({"wine": Completed([], 2, "out\n", "err\n")})
    done = runner.run(["wine", "x"], log_file=log_file)
    assert (done.returncode, done.stdout, done.stderr) == (2, "", "")
    assert log_file.read_text(encoding="utf-8") == "out\nerr\n"
    with pytest.raises(CommandError, match="err"):
        runner.run(["wine", "x"], log_file=log_file, check=True)


def test_recording_runner_which() -> None:
    runner = RecordingRunner(which_map={"zenity": "/usr/bin/zenity", "kdialog": None})
    assert runner.which("zenity") == "/usr/bin/zenity"
    assert runner.which("kdialog") is None
    assert runner.which(Path(PY).name, path=str(Path(PY).parent)) is not None
