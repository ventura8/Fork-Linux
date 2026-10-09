"""Tests for fork_linux.sandbox: packaging detection, host argv and environment cleaning."""

from __future__ import annotations

from pathlib import Path

import pytest

from fork_linux import sandbox


@pytest.fixture(autouse=True)
def _no_flatpak_info(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    marker = tmp_path / ".flatpak-info"
    monkeypatch.setattr(sandbox, "FLATPAK_INFO", marker)
    return marker


@pytest.mark.parametrize(
    ("env", "kind"),
    [
        ({}, "none"),
        ({"FLATPAK_ID": "io.github.ventura8.ForkLinux"}, "flatpak"),
        ({"SNAP": "/snap/fork-linux/x1"}, "snap"),
        ({"APPIMAGE": "/home/u/Fork.AppImage"}, "appimage"),
        ({"APPDIR": "/tmp/.mount_Fork"}, "appimage"),
        ({"SNAP": "", "APPIMAGE": "", "FLATPAK_ID": ""}, "none"),
        ({"SNAP": "/snap/x", "APPIMAGE": "/a"}, "snap"),
    ],
)
def test_detect_from_environment(env: dict[str, str], kind: str) -> None:
    assert sandbox.detect(env) == kind


def test_detect_flatpak_info_file(_no_flatpak_info: Path) -> None:
    _no_flatpak_info.write_text("[Application]\n", encoding="utf-8")
    assert sandbox.detect({}) == "flatpak"
    assert sandbox.detect({"SNAP": "/snap/x"}) == "flatpak"


def test_detect_defaults_to_os_environ(xdg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert sandbox.detect() == "none"
    monkeypatch.setenv("SNAP", "/snap/fork-linux/current")
    assert sandbox.detect() == "snap"


def test_host_argv(xdg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    argv = ["xdg-open", "/home/u/repo"]
    assert sandbox.host_argv(argv, "flatpak") == ["flatpak-spawn", "--host", "xdg-open", "/home/u/repo"]
    assert sandbox.host_argv(argv, "snap") == argv
    result = sandbox.host_argv(argv)
    assert result == argv and result is not argv
    monkeypatch.setenv("FLATPAK_ID", "io.github.ventura8.ForkLinux")
    assert sandbox.host_argv(argv)[:2] == ["flatpak-spawn", "--host"]


def test_clean_env_drops_injected_variables() -> None:
    env = {
        "PATH": "/usr/bin",
        "HOME": "/home/u",
        "LD_LIBRARY_PATH": "/snap/x/lib",
        "LD_PRELOAD": "libfoo.so",
        "PYTHONHOME": "/app",
        "PYTHONPATH": "/app/lib",
        "GTK_PATH": "/snap/gtk",
        "GIO_MODULE_DIR": "/snap/gio",
        "GDK_PIXBUF_MODULE_FILE": "/snap/loaders.cache",
        "SNAP": "/snap/fork-linux/x1",
        "SNAP_NAME": "fork-linux",
        "SNAP_REVISION": "x1",
        "SNAPPY": "kept: not a snap variable",
    }
    cleaned = sandbox.clean_env(env)
    assert cleaned == {"PATH": "/usr/bin", "HOME": "/home/u", "SNAPPY": "kept: not a snap variable"}
    assert "LD_LIBRARY_PATH" in env, "the input mapping is not modified"


def test_clean_env_restores_originals() -> None:
    env = {
        "LD_LIBRARY_PATH": "/tmp/.mount/usr/lib",
        "LD_LIBRARY_PATH_ORIG": "/home/u/lib",
        "PATH": "/tmp/.mount/usr/bin:/usr/bin",
        "PATH_ORIG": "/usr/bin",
        "XDG_DATA_DIRS": "/tmp/.mount/usr/share",
        "XDG_DATA_DIRS_ORIG": "",
        "PYTHONPATH_ORIG": "/home/u/py",
        "_ORIG": "odd but kept",
    }
    assert sandbox.clean_env(env) == {
        "LD_LIBRARY_PATH": "/home/u/lib",
        "PATH": "/usr/bin",
        "PYTHONPATH": "/home/u/py",
        "_ORIG": "odd but kept",
    }
