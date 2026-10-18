"""Tests for :mod:`fork_linux.git_bash` (Fork's "Open Git Bash" opens the Linux terminal)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from fork_linux import git_bash, resources
from fork_linux.paths import Paths


@pytest.fixture
def paths(xdg: Path) -> Paths:
    return Paths.from_env({"HOME": str(xdg), "XDG_DATA_HOME": str(xdg / "data")})


@pytest.fixture
def terminal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    libexec = tmp_path / "lib dir's"
    libexec.mkdir()
    monkeypatch.setenv(resources.LIBEXEC_ENV, str(libexec))
    script = libexec / "fork-linux-terminal"
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    script.chmod(0o755)
    return script


def test_script_makes_one_call_with_the_folder_as_a_windows_path(terminal: Path, tmp_path: Path) -> None:
    text = git_bash.script_text(terminal)
    commands = [line for line in text.splitlines() if line and not line.startswith("#")]
    assert commands[-1].startswith("MSYS2_ARG_CONV_EXCL='*' exec /c/windows/system32/start.exe /b /unix ")
    assert sum("exec" in line for line in commands) == 1
    assert "$(" not in text and "`" not in text  # no command substitution: no child before the exec
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is not installed")
    # Run the script with exec turned into echo: the folder becomes Z:/…, the quoted path survives.
    probe = text.replace("exec ", "echo ")
    for pwd, expected in (("/z/home/ada/repo", "Z:/home/ada/repo"), ("/h", "H:/")):
        out = subprocess.run([bash, "-c", f"PWD={pwd}; {probe}"], capture_output=True, text=True, check=True,
                             cwd=tmp_path).stdout.split()
        assert out[-1] == expected
        assert " ".join(out[3:-1]) == str(terminal)


def test_install_writes_and_verifies_the_script(paths: Paths, terminal: Path) -> None:
    assert not git_bash.installed(paths)
    assert git_bash.launch_env(paths, native_git=False) == {}
    assert git_bash.install(paths)
    target = git_bash.script_path(paths)
    assert target.read_text(encoding="utf-8") == git_bash.script_text(terminal)
    assert target.stat().st_mode & 0o777 == 0o644
    assert git_bash.installed(paths)
    assert git_bash.launch_env(paths, native_git=False) == {"POSIXLY_CORRECT": "1", "ENV": "/c/fork-linux/git-bash.sh"}
    assert git_bash.launch_env(paths, native_git=True) == {}
    target.write_text("old\n", encoding="utf-8")
    assert not git_bash.installed(paths)
    target.unlink()
    target.symlink_to(paths.prefix / "elsewhere")
    assert not git_bash.installed(paths)


def test_without_a_terminal_script_nothing_is_installed(paths: Paths, tmp_path: Path,
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(resources.LIBEXEC_ENV, str(tmp_path / "none"))
    assert git_bash.wanted_text() is None
    assert git_bash.installed(paths)
    target = git_bash.script_path(paths)
    target.parent.mkdir(parents=True)
    target.write_text("stale\n", encoding="utf-8")
    assert not git_bash.installed(paths)
    assert not git_bash.install(paths)
    assert not target.exists()
    assert git_bash.install(paths) is False  # nothing to remove the second time


def test_unreadable_script_is_not_installed(paths: Paths, terminal: Path) -> None:
    git_bash.script_path(paths).mkdir(parents=True)
    assert not git_bash.installed(paths)
