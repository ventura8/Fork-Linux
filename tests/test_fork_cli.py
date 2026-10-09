"""Tests for fork_linux.fork_cli: ``fork [PATH...]`` / ``fork open PATH...`` -> ``fork-linux run``."""

from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

from fork_linux import cli, commands, credits, fork_cli


@pytest.fixture
def delegated(monkeypatch: pytest.MonkeyPatch) -> list[tuple[list[str], str | None]]:
    """Record what fork_cli forwards to cli.main instead of running it."""
    calls: list[tuple[list[str], str | None]] = []

    def fake_main(argv: Sequence[str] | None = None, prog: str | None = None) -> int:
        calls.append((list(argv or []), prog))
        return 42

    monkeypatch.setattr(cli, "main", fake_main)
    return calls


@pytest.mark.parametrize(
    ("argv", "forwarded"),
    [
        ([], ["run", "--"]),
        (["."], ["run", "--", "."]),
        (["repo", "other/file.txt"], ["run", "--", "repo", "other/file.txt"]),
        (["open", "repo"], ["run", "--", "repo"]),
        (["open", "a", "b"], ["run", "--", "a", "b"]),
        (["-v", "--no-gui", "."], ["run", "-v", "--no-gui", "--", "."]),
        ([".", "--offline"], ["run", "--offline", "--", "."]),
        (["--prefix", "/p", "repo"], ["run", "--prefix", "/p", "--", "repo"]),
        (["--prefix=/p", "repo"], ["run", "--prefix=/p", "--", "repo"]),
        (["--prefix"], ["run", "--prefix", "--"]),
        (["--", "-weird-name", "--help"], ["run", "--", "-weird-name", "--help"]),
        (["-"], ["run", "--", "-"]),
        (["open", "--gui", "--", "open"], ["run", "--gui", "--", "open"]),
        (["./open"], ["run", "--", "./open"]),
    ],
)
def test_delegates_to_run(
    delegated: list[tuple[list[str], str | None]], argv: list[str], forwarded: list[str]
) -> None:
    assert fork_cli.main(argv) == 42
    assert delegated == [(forwarded, "fork-linux")]


def test_reads_sys_argv_by_default(
    delegated: list[tuple[list[str], str | None]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "argv", ["/usr/bin/fork", "repo"])
    assert fork_cli.main() == 42
    assert delegated == [(["run", "--", "repo"], "fork-linux")]


@pytest.mark.parametrize("argv", [["--help"], ["-h"], ["repo", "--help"], ["open", "-h"]])
def test_help(capsys: pytest.CaptureFixture[str], delegated: list[object], argv: list[str]) -> None:
    assert fork_cli.main(argv) == 0
    out = capsys.readouterr().out
    assert out.startswith("usage: fork [OPTIONS] [PATH...]\n")
    assert "fork open [OPTIONS] PATH..." in out
    assert credits.short_footer() in out
    assert delegated == []


@pytest.mark.parametrize("argv", [["--version"], ["-V"]])
def test_version(capsys: pytest.CaptureFixture[str], delegated: list[object], argv: list[str]) -> None:
    assert fork_cli.main(argv) == 0
    assert capsys.readouterr().out.strip() == cli.version_line()
    assert delegated == []


def test_open_without_path_is_a_usage_error(capsys: pytest.CaptureFixture[str], delegated: list[object]) -> None:
    assert fork_cli.main(["open"]) == 2
    err = capsys.readouterr().err
    assert "usage: fork" in err
    assert "fork-linux: error: fork open: missing PATH" in err
    assert delegated == []


def test_split_args() -> None:
    assert fork_cli.split_args(["-q", "a", "--json", "--", "-b"]) == (["-q", "--json"], ["a", "-b"])
    assert fork_cli.split_args([]) == ([], [])


def test_without_a_run_command_the_real_cli_reports_usage(
    capsys: pytest.CaptureFixture[str], xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no 'run' command registered (phase 2), argparse rejects the delegation with exit 2."""
    monkeypatch.setattr(cli, "_is_root", lambda: False)
    monkeypatch.setattr(commands, "COMMANDS", [name for name in commands.COMMANDS if name != "run"])
    assert fork_cli.main(["."]) == 2
    assert "run" in capsys.readouterr().err
