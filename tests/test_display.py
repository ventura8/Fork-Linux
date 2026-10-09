"""Tests for fork_linux.display: session type, desktop scale probes and the derived DPI."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from fork_linux import display
from fork_linux.errors import ForkLinuxError
from fork_linux.procrun import Completed, RecordingRunner, Runner

FAKES_BIN = Path(__file__).resolve().parent / "fakes" / "bin"
TOOLS = ("gsettings", "xrdb", "kreadconfig6", "kreadconfig5", "xfconf-query")
GNOME_SCALE = ("gsettings", "get", "org.gnome.desktop.interface", "scaling-factor")
GNOME_TEXT = ("gsettings", "get", "org.gnome.desktop.interface", "text-scaling-factor")
XRDB = ("xrdb", "-query")
KDE6 = ("kreadconfig6", "--file", "kcmfonts", "--group", "General", "--key", "forceFontDPI")
KDE5 = ("kreadconfig5", "--file", "kcmfonts", "--group", "General", "--key", "forceFontDPI")
XFCE = ("xfconf-query", "-c", "xsettings", "-p", "/Xft/DPI")


def _runner(outputs: dict[tuple[str, ...], Any], installed: tuple[str, ...] | None = None) -> RecordingRunner:
    """A runner answering ``outputs`` per argv; tools not in ``installed`` are absent from PATH."""
    present = {argv[0] for argv in outputs} if installed is None else set(installed)

    def respond(argv: list[str]) -> Any:
        answer = outputs.get(tuple(argv), Completed(argv, 1, "", "no such key\n"))
        return answer(argv) if callable(answer) else answer

    return RecordingRunner(respond, {tool: f"/usr/bin/{tool}" if tool in present else None for tool in TOOLS})


@pytest.fixture
def isolated_path(tmp_path: Path, fake_bin: Path) -> str:
    """A PATH holding only our fakes and python3, so no host desktop tool can run."""
    bin_dir = tmp_path / "isolated-bin"
    bin_dir.mkdir()
    for fake in FAKES_BIN.iterdir():
        (bin_dir / fake.name).symlink_to(fake)
    (bin_dir / "python3").symlink_to(sys.executable)
    return str(bin_dir)


# --------------------------------------------------------------------------- session type


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({"XDG_SESSION_TYPE": "wayland", "DISPLAY": ":0"}, "wayland"),
        ({"XDG_SESSION_TYPE": "X11"}, "x11"),
        ({"XDG_SESSION_TYPE": "tty", "WAYLAND_DISPLAY": "wayland-0"}, "wayland"),
        ({"WAYLAND_DISPLAY": "wayland-0", "DISPLAY": ":0"}, "wayland"),
        ({"DISPLAY": ":1"}, "x11"),
        ({"XDG_SESSION_TYPE": "tty"}, "tty"),
        ({}, "tty"),
    ],
)
def test_session_type(env: dict[str, str], expected: str) -> None:
    assert display.session_type(env) == expected


# --------------------------------------------------------------------------- scale sources


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({"GDK_SCALE": "2"}, 200),
        ({"GDK_DPI_SCALE": "1.25"}, 125),
        ({"GDK_SCALE": "2", "GDK_DPI_SCALE": "1.5"}, 300),
        ({"GDK_SCALE": "3", "GDK_DPI_SCALE": "1.5"}, 300),
        ({"GDK_SCALE": "0", "QT_SCALE_FACTOR": "1.5"}, 150),
        ({"GDK_SCALE": "abc", "QT_SCALE_FACTOR": "1.75"}, 175),
        ({"QT_SCALE_FACTOR": "0.5"}, 100),
        ({"GDK_DPI_SCALE": "1.1"}, 100),
        ({"GDK_DPI_SCALE": "1.13"}, 125),
    ],
)
def test_scale_from_environment(env: dict[str, str], expected: int) -> None:
    runner = _runner({})
    assert display.scale_percent(env, runner) == expected
    assert runner.calls == []


def test_scale_from_gnome_integer_scaling() -> None:
    runner = _runner({GNOME_SCALE: "uint32 2\n", GNOME_TEXT: "1.0\n"})
    assert display.scale_percent({}, runner) == 200


def test_scale_from_gnome_text_scaling() -> None:
    runner = _runner({GNOME_SCALE: "uint32 0\n", GNOME_TEXT: "1.25\n"})
    assert display.scale_percent({}, runner) == 125


def test_gnome_defaults_fall_through_to_xrdb() -> None:
    runner = _runner({GNOME_SCALE: "uint32 0\n", GNOME_TEXT: "1.0\n", XRDB: "Xft.antialias:\t1\nXft.dpi:\t144\n"})
    assert display.scale_percent({}, runner) == 150
    assert [argv[0] for argv in runner.argvs] == ["gsettings", "gsettings", "xrdb"]


def test_xrdb_without_dpi_falls_through_to_kde() -> None:
    runner = _runner({XRDB: "Xft.antialias:\t1\n", KDE6: "120\n"})
    assert display.scale_percent({}, runner) == 125


def test_kde_falls_back_to_kreadconfig5() -> None:
    runner = _runner({KDE6: "0\n", KDE5: "192\n"})
    assert display.scale_percent({}, runner) == 200


def test_kde_unset_falls_through_to_xfce() -> None:
    runner = _runner({KDE6: "\n", KDE5: "\n", XFCE: "120\n"})
    assert display.scale_percent({}, runner) == 125


def test_xfce_unset_means_default() -> None:
    assert display.scale_percent({}, _runner({XFCE: "-1\n"})) == 100


def test_no_tools_means_default_and_nothing_runs() -> None:
    runner = _runner({}, installed=())
    assert display.scale_percent({}, runner) == 100
    assert display.detect_dpi({}, runner) == 96
    assert runner.calls == []


def test_failing_and_hanging_probes_are_ignored() -> None:
    def hang(argv: list[str]) -> Completed:
        raise ForkLinuxError("command timed out after 2s")

    runner = _runner({GNOME_SCALE: hang, GNOME_TEXT: Completed([], 1, "2.0", ""), XRDB: "Xft.dpi: 120\n"})
    assert display.scale_percent({}, runner) == 125


def test_probe_uses_env_path_and_cleans_env() -> None:
    seen: list[tuple[str, str | None]] = []

    class Recorder(RecordingRunner):
        def which(self, name: str, path: str | None = None) -> str | None:
            seen.append((name, path))
            return "/usr/bin/" + name

    runner = Recorder({"xrdb": "Xft.dpi: 96\n"})
    env = {"PATH": "/opt/bin", "LD_LIBRARY_PATH": "/snap/lib", "DISPLAY": ":0"}
    assert display.probe(runner, env, ["xrdb", "-query"]) == "Xft.dpi: 96"
    assert seen == [("xrdb", "/opt/bin")]
    assert runner.calls[0]["env"] == {"PATH": "/opt/bin", "DISPLAY": ":0"}
    assert runner.calls[0]["timeout"] == display.PROBE_TIMEOUT


@pytest.mark.parametrize(
    ("percent", "expected"),
    [(50, 100), (112.4, 100), (112.5, 125), (137.5, 150), (162, 150), (250, 250), (299, 300), (1000, 300)],
)
def test_normalize(percent: float, expected: int) -> None:
    assert display.normalize(percent) == expected


@pytest.mark.parametrize(
    ("scale", "dpi"), [("1", 96), ("1.25", 120), ("1.5", 144), ("1.75", 168), ("2", 192), ("3", 288)]
)
def test_detect_dpi(scale: str, dpi: int) -> None:
    assert display.detect_dpi({"QT_SCALE_FACTOR": scale}, _runner({})) == dpi


# --------------------------------------------------------------------------- with the fake tools


def test_scale_with_fake_gsettings(isolated_path: str, fake_bin: Path) -> None:
    env = {
        "PATH": isolated_path,
        "FL_FAKE_LOG": str(fake_bin),
        "FL_FAKE_GSETTINGS": json.dumps({"org.gnome.desktop.interface text-scaling-factor": "1.5"}),
    }
    assert display.scale_percent(env, Runner()) == 150
    logged = [json.loads(line)["argv"] for line in fake_bin.read_text(encoding="utf-8").splitlines()]
    assert logged == [list(GNOME_SCALE), list(GNOME_TEXT)]


def test_scale_with_fake_xrdb_kde_and_xfce(isolated_path: str, fake_bin: Path) -> None:
    base = {"PATH": isolated_path, "FL_FAKE_LOG": str(fake_bin)}
    assert display.scale_percent({**base, "FL_FAKE_XRDB": "Xft.dpi:\t120\n"}, Runner()) == 125
    kde = {**base, "FL_FAKE_KREADCONFIG": json.dumps({"kcmfonts/General/forceFontDPI": "144"})}
    assert display.scale_percent(kde, Runner()) == 150
    xfce = {**base, "FL_FAKE_XFCONF": json.dumps({"xsettings:/Xft/DPI": "192"})}
    assert display.detect_dpi(xfce, Runner()) == 192
    assert display.scale_percent(base, Runner()) == 100
