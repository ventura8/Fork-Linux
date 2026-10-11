"""Tests for the libexec shell scripts Wine starts for Fork (explorer, terminal, links, hand-off).

Each script runs for real with stub programs first on PATH (systemd-run,
gdbus, xdg-open, the host helper) that only record their arguments.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

LIBEXEC = Path(__file__).resolve().parent.parent / "libexec"
SCRIPTS = ("fork-linux-handoff", "fork-linux-explorer", "fork-linux-terminal", "fork-linux-open-url")
RECORDER = '#!/bin/sh\nprintf "%s\\n" "$(basename "$0")" "$@" "@@" >> "$STUB_LOG"\nexit {code}\n'


def _stub(directory: Path, name: str, code: int = 0) -> Path:
    path = directory / name
    path.write_text(RECORDER.format(code=code), encoding="utf-8")
    path.chmod(0o755)
    return path


@pytest.fixture
def box(tmp_path: Path) -> dict[str, Path]:
    """A libexec copy (with a recording host helper) and a stub bin directory."""
    libexec = tmp_path / "libexec"
    libexec.mkdir()
    for name in SCRIPTS:
        shutil.copy2(LIBEXEC / name, libexec / name)
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    return {"libexec": libexec, "stubs": stubs, "log": tmp_path / "calls.log"}


def _run(box: dict[str, Path], script: str, *args: str, **env: str) -> tuple[int, list[list[str]]]:
    environ = {
        "PATH": f"{box['stubs']}:/usr/bin:/bin",
        "STUB_LOG": str(box["log"]),
        "HOME": "/home/ada",
        "WINEPREFIX": "/home/ada/.local/share/fork-linux/prefix",
        "WINESERVER": "/opt/wine/bin/wineserver",
        "DISPLAY": ":5",
        **env,
    }
    completed = subprocess.run([str(box["libexec"] / script), *args], env=environ, capture_output=True, text=True)
    calls: list[list[str]] = []
    if box["log"].exists():
        current: list[str] = []
        for line in box["log"].read_text(encoding="utf-8").splitlines():
            if line == "@@":
                calls.append(current)
                current = []
            else:
                current.append(line)
    return completed.returncode, calls


def test_scripts_are_executable_posix_sh() -> None:
    for name in SCRIPTS:
        path = LIBEXEC / name
        assert os.access(path, os.X_OK), name
        assert path.read_text(encoding="utf-8").startswith("#!/bin/sh\n"), name


# -- fork-linux-handoff ----------------------------------------------------------------------------


def test_handoff_runs_the_helper_in_a_systemd_unit(box: dict[str, Path]) -> None:
    _stub(box["libexec"], "fork-linux-host")
    _stub(box["stubs"], "systemd-run")
    code, calls = _run(box, "fork-linux-handoff", "terminal", "Z:\\home\\ada\\src")
    assert code == 0
    assert len(calls) == 1
    argv = calls[0]
    assert argv[:6] == ["systemd-run", "--user", "--quiet", "--collect", "--no-block", "--property=KillMode=process"]
    assert "--setenv=WINEPREFIX=/home/ada/.local/share/fork-linux/prefix" in argv
    assert "--setenv=DISPLAY=:5" in argv
    assert not any(arg.startswith("--setenv=WINESERVER") for arg in argv)
    assert argv[argv.index("--") + 1 :] == [str(box["libexec"] / "fork-linux-host"), "terminal", "Z:\\home\\ada\\src"]


def test_handoff_prefers_the_stable_helper_launcher(box: dict[str, Path], tmp_path: Path) -> None:
    """From an AppImage the systemd unit must not name the mount, which ends before the unit starts."""
    _stub(box["libexec"], "fork-linux-host")
    stable = tmp_path / "helpers"
    stable.mkdir()
    _stub(stable, "fork-linux-host")
    _stub(box["stubs"], "systemd-run")
    code, calls = _run(box, "fork-linux-handoff", "open", "/x", FORK_LINUX_HELPER_DIR=str(stable))
    assert code == 0
    assert calls[0][calls[0].index("--") + 1 :] == [str(stable / "fork-linux-host"), "open", "/x"]
    box["log"].unlink()
    code, calls = _run(box, "fork-linux-handoff", "open", "/x", FORK_LINUX_HELPER_DIR=str(tmp_path / "none"))
    assert calls[0][calls[0].index("--") + 1 :] == [str(box["libexec"] / "fork-linux-host"), "open", "/x"]


def test_handoff_without_a_systemd_user_manager_asks_the_daemon(box: dict[str, Path]) -> None:
    """systemd-run fails (no user manager): ``fork-linux-host --handoff`` (the launcher's daemon)."""
    _stub(box["libexec"], "fork-linux-host")
    _stub(box["stubs"], "systemd-run", code=1)
    code, calls = _run(box, "fork-linux-handoff", "reveal", "/home/ada/x")
    assert code == 0
    assert calls[0][0] == "systemd-run"
    assert calls[1] == ["fork-linux-host", "--handoff", "reveal", "/home/ada/x"]
    assert len(calls) == 2


def test_handoff_without_systemd_run_asks_the_daemon(box: dict[str, Path], tmp_path: Path) -> None:
    _stub(box["libexec"], "fork-linux-host", code=3)
    tools = tmp_path / "tools"  # what the script and the stubs need, and no systemd-run
    tools.mkdir()
    for name in ("dirname", "basename"):
        found = shutil.which(name)
        assert found is not None
        (tools / name).symlink_to(found)
    code, calls = _run(
        box, "fork-linux-handoff", "open-url", "file:///home/ada/a%20b.txt", PATH=f"{box['stubs']}:{tools}"
    )
    assert code == 3, "the helper's status"
    assert calls == [["fork-linux-host", "--handoff", "open-url", "file:///home/ada/a%20b.txt"]]


def test_handoff_direct_runs_the_helper_here(box: dict[str, Path]) -> None:
    _stub(box["libexec"], "fork-linux-host")
    _stub(box["stubs"], "systemd-run")
    code, calls = _run(box, "fork-linux-handoff", "open", "/home/ada/x", FORK_LINUX_HANDOFF="direct")
    assert code == 0
    assert calls == [["fork-linux-host", "open", "/home/ada/x"]]


def test_handoff_uses_the_source_template_with_python(box: dict[str, Path]) -> None:
    (box["libexec"] / "fork-linux-host.in").write_text("#!@PYTHON@ -I\n", encoding="utf-8")
    python = _stub(box["stubs"], "fake-python")
    code, calls = _run(
        box, "fork-linux-handoff", "open", "/x", FORK_LINUX_HANDOFF="direct", FORK_LINUX_PYTHON=str(python)
    )
    assert code == 0
    assert calls == [["fake-python", "-I", str(box["libexec"] / "fork-linux-host.in"), "open", "/x"]]
    box["log"].unlink()
    _stub(box["stubs"], "systemd-run", code=1)
    _run(box, "fork-linux-handoff", "terminal", "/x", FORK_LINUX_PYTHON=str(python))
    template = str(box["libexec"] / "fork-linux-host.in")
    assert calls_after_systemd(box) == [["fake-python", "-I", template, "--handoff", "terminal", "/x"]]


def calls_after_systemd(box: dict[str, Path]) -> list[list[str]]:
    """The recorded calls after systemd-run's."""
    lines = box["log"].read_text(encoding="utf-8").split("@@\n")
    return [line.splitlines() for line in lines if line and not line.startswith("systemd-run")]


@pytest.mark.parametrize(
    "args", [(), ("terminal",), ("terminal", ""), ("rm", "/x"), ("open", "/x", "extra"), ("diff", "/x")]
)
def test_handoff_rejects_bad_arguments(box: dict[str, Path], args: tuple[str, ...]) -> None:
    code, calls = _run(box, "fork-linux-handoff", *args)
    assert code == 2
    assert calls == []


def test_handoff_without_helper(box: dict[str, Path]) -> None:
    code, calls = _run(box, "fork-linux-handoff", "open", "/x")
    assert code == 127
    assert calls == []


# -- fork-linux-explorer / fork-linux-terminal -----------------------------------------------------


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (("/select,", "Z:\\home\\ada\\a.txt"), ["reveal", "Z:\\home\\ada\\a.txt"]),
        (("/select,Z:\\home\\ada\\a.txt",), ["reveal", "Z:\\home\\ada\\a.txt"]),
        (("/SELECT", "Z:\\a"), ["reveal", "Z:\\a"]),
        (("Z:\\home\\ada\\repo",), ["open", "Z:\\home\\ada\\repo"]),
        (("/e,Z:\\home",), ["open", "Z:\\home"]),
        (("/root,", "C:\\users"), ["open", "C:\\users"]),
        (("/n", "/separate", "/home/ada/x"), ["open", "/home/ada/x"]),
        ((), ["open", "/home/ada"]),
    ],
)
def test_explorer_arguments(box: dict[str, Path], args: tuple[str, ...], expected: list[str]) -> None:
    _stub(box["libexec"], "fork-linux-handoff")
    code, calls = _run(box, "fork-linux-explorer", *args)
    assert code == 0
    assert calls == [["fork-linux-handoff", *expected]]


def test_explorer_ignores_the_desktop(box: dict[str, Path]) -> None:
    _stub(box["libexec"], "fork-linux-handoff")
    code, calls = _run(box, "fork-linux-explorer", "/desktop=shell")
    assert code == 0
    assert calls == []


def test_terminal_uses_the_working_directory(box: dict[str, Path], tmp_path: Path) -> None:
    _stub(box["libexec"], "fork-linux-handoff")
    repo = tmp_path / "repo"
    repo.mkdir()
    environ = {"PATH": "/usr/bin:/bin", "STUB_LOG": str(box["log"])}
    subprocess.run([str(box["libexec"] / "fork-linux-terminal")], cwd=repo, env=environ, check=True)
    subprocess.run([str(box["libexec"] / "fork-linux-terminal"), "/elsewhere"], cwd=repo, env=environ, check=True)
    lines = box["log"].read_text(encoding="utf-8").split("@@\n")
    assert lines[0].splitlines() == ["fork-linux-handoff", "terminal", str(repo)]
    assert lines[1].splitlines() == ["fork-linux-handoff", "terminal", "/elsewhere"]


# -- fork-linux-open-url -----------------------------------------------------------------------------


def test_open_url_web_links_go_to_the_portal(box: dict[str, Path]) -> None:
    _stub(box["stubs"], "gdbus")
    code, calls = _run(box, "fork-linux-open-url", "https://git-fork.com")
    assert code == 0
    assert calls[0][0] == "gdbus"
    assert "https://git-fork.com" in calls[0]


@pytest.mark.parametrize("url", ["/home/ada/a.py", "file:///home/ada/a%20b.py", "FILE:///x"])
def test_open_url_files_skip_the_portal(box: dict[str, Path], url: str) -> None:
    _stub(box["stubs"], "gdbus")
    _stub(box["libexec"], "fork-linux-handoff")
    code, calls = _run(box, "fork-linux-open-url", url)
    assert code == 0
    assert calls == [["fork-linux-handoff", "open-url", url]]


def test_open_url_without_the_portal_hands_off(box: dict[str, Path]) -> None:
    _stub(box["stubs"], "gdbus", code=1)
    _stub(box["libexec"], "fork-linux-handoff")
    code, calls = _run(box, "fork-linux-open-url", "mailto:a@b.invalid")
    assert code == 0
    assert calls == [["gdbus", *calls[0][1:]], ["fork-linux-handoff", "open-url", "mailto:a@b.invalid"]]


def test_open_url_through_the_real_handoff(box: dict[str, Path]) -> None:
    """open-url, then fork-linux-handoff itself: systemd first, the helper's --handoff otherwise."""
    _stub(box["stubs"], "gdbus", code=1)
    _stub(box["stubs"], "systemd-run", code=1)
    _stub(box["libexec"], "fork-linux-host")
    code, calls = _run(box, "fork-linux-open-url", "https://git-fork.com/buy")
    assert code == 0
    assert [call[0] for call in calls] == ["gdbus", "systemd-run", "fork-linux-host"]
    assert calls[-1] == ["fork-linux-host", "--handoff", "open-url", "https://git-fork.com/buy"]


def test_open_url_windows_paths_go_to_the_helper(box: dict[str, Path]) -> None:
    _stub(box["libexec"], "fork-linux-handoff")
    code, calls = _run(box, "fork-linux-open-url", "Z:\\home\\ada\\a.py")
    assert code == 0
    assert calls == [["fork-linux-handoff", "open", "Z:\\home\\ada\\a.py"]]


def test_open_url_needs_an_argument(box: dict[str, Path]) -> None:
    assert _run(box, "fork-linux-open-url")[0] == 2
    assert _run(box, "fork-linux-open-url", "")[0] == 2
