"""Tests for ``fork-linux run`` / ``open`` and the ``fork`` shortcut that delegates to them."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from fork_linux import fork_cli, launcher

from fixtures.cli_run import isolate, run_cli


@pytest.fixture(autouse=True)
def _isolated(xdg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    isolate(monkeypatch)


@pytest.fixture
def launched(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Record launcher.run calls (nothing is executed)."""
    calls: list[dict[str, Any]] = []

    def fake_run(ctx: Any, targets: list[str], **kwargs: Any) -> int:
        calls.append({"targets": list(targets), "prefix": ctx.paths.prefix, **kwargs})
        return 0

    monkeypatch.setattr(launcher, "run", fake_run)
    return calls


def test_run_defaults(capsys: pytest.CaptureFixture[str], launched: list[dict[str, Any]]) -> None:
    assert run_cli(capsys, "run")[0] == 0
    assert launched == [
        {
            "targets": [],
            "prefix": launched[0]["prefix"],
            "debug": False,
            "wine_debug": None,
            "driver": None,
            "no_setup": False,
            "no_hooks": False,
            "from_file_manager": False,
        }
    ]


def test_run_every_option(capsys: pytest.CaptureFixture[str], launched: list[dict[str, Any]]) -> None:
    argv = ["run", "--debug", "--wine-debug", "+relay", "--wayland", "--no-setup", "--no-hooks"]
    assert run_cli(capsys, *argv, "--from-file-manager", "a", "b")[0] == 0
    call = launched[0]
    assert call["targets"] == ["a", "b"]
    assert call["debug"] and call["no_setup"] and call["no_hooks"] and call["from_file_manager"]
    assert (call["wine_debug"], call["driver"]) == ("+relay", "wayland")
    assert run_cli(capsys, "run", "--x11")[0] == 0
    assert launched[1]["driver"] == "x11"


def test_x11_and_wayland_exclude_each_other(capsys: pytest.CaptureFixture[str], launched: list[dict[str, Any]]) -> None:
    code, _out, err = run_cli(capsys, "run", "--x11", "--wayland")
    assert code == 2
    assert "not allowed with" in err
    assert launched == []


def test_open_needs_a_path(capsys: pytest.CaptureFixture[str], launched: list[dict[str, Any]]) -> None:
    assert run_cli(capsys, "open")[0] == 2
    assert run_cli(capsys, "open", "repo", "--from-file-manager")[0] == 0
    assert launched[0]["targets"] == ["repo"] and launched[0]["from_file_manager"] is True


def test_fork_shortcut_reaches_run(
    capsys: pytest.CaptureFixture[str], launched: list[dict[str, Any]], tmp_path: Path
) -> None:
    prefix = tmp_path / "pfx"
    (prefix / ".fork-linux").mkdir(parents=True)
    (prefix / ".fork-linux" / "created-by").write_text("fork-linux\n", encoding="utf-8")
    assert fork_cli.main(["--prefix", str(prefix), "repo", "-q"]) == 0
    assert launched[0]["targets"] == ["repo"]
    assert launched[0]["prefix"] == prefix
    assert fork_cli.main(["open", "--", "-dash"]) == 0
    assert launched[1]["targets"] == ["-dash"]


def test_launch_errors_map_to_exit_codes(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    code, _out, err = run_cli(capsys, "run", str(tmp_path / "missing"))
    assert code == 2
    assert "no such file or directory" in err
