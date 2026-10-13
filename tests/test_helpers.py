"""fork_linux.helpers: stable helper paths inside an AppImage, and AppRun's ``--libexec`` that serves them.

A fake AppDir under ``tmp_path`` stands for the AppImage mount (``resources.install_root`` is
pointed into it) and a small shell script stands for the AppImage file; nothing is mounted.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from fork_linux import helpers, resources
from fork_linux.paths import Paths

ROOT = Path(__file__).resolve().parents[1]
APPRUN = ROOT / "packaging" / "appimage" / "AppRun"
RECORD = '#!/bin/sh\nprintf "%s\\n" "$(basename "$0")" "$FORK_LINUX_HELPER_DIR" "$@" >> "$RECORD_LOG"\n'


@pytest.fixture
def appdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An AppDir whose usr/ is this fork-linux's install root, with every helper as a recording stub."""
    root = tmp_path / "mount" / "usr"
    libexec = root / "lib" / "fork-linux"
    libexec.mkdir(parents=True)
    for name in helpers.NAMES:
        (libexec / name).write_text(RECORD, encoding="utf-8")
        (libexec / name).chmod(0o755)
    monkeypatch.setattr(resources, "install_root", lambda: root)
    monkeypatch.setattr(resources, "libexec_dir", lambda: libexec)
    return root.parent


@pytest.fixture
def image(tmp_path: Path) -> Path:
    """The 'AppImage' file: records how it was started."""
    path = tmp_path / "Applications" / "fork-linux.AppImage"
    path.parent.mkdir()
    path.write_text(RECORD, encoding="utf-8")
    path.chmod(0o755)
    return path


def _env(xdg: Path, appdir: Path, image: Path) -> dict[str, str]:
    return {**os.environ, "APPIMAGE": str(image), "APPDIR": str(appdir)}


# --------------------------------------------------------------------- appimage()


def test_appimage_needs_the_runtime_variables_and_this_package_inside_the_mount(
    xdg: Path, appdir: Path, image: Path, tmp_path: Path
) -> None:
    env = _env(xdg, appdir, image)
    assert helpers.appimage(env) == image
    assert helpers.appimage({**env, "APPDIR": str(appdir / "usr")}) == image  # the root itself
    assert helpers.appimage({}) is None
    assert helpers.appimage({**env, "APPIMAGE": "fork-linux.AppImage"}) is None
    assert helpers.appimage({**env, "APPIMAGE": str(tmp_path / "gone.AppImage")}) is None
    assert helpers.appimage({**env, "APPDIR": "relative"}) is None
    other = tmp_path / "other-mount"
    other.mkdir()
    assert helpers.appimage({**env, "APPDIR": str(other)}) is None


def test_appimage_defaults_to_os_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("APPIMAGE", raising=False)
    assert helpers.appimage() is None


# --------------------------------------------------------------------- path()


def test_outside_an_appimage_the_installed_helper_is_used(xdg: Path, appdir: Path) -> None:
    assert helpers.path("fork-linux-terminal", env={}) == resources.libexec_dir() / "fork-linux-terminal"
    assert not (Paths.from_env().data_dir / helpers.HELPERS_DIR).exists()


def test_inside_an_appimage_a_stable_launcher_is_written_once(xdg: Path, appdir: Path, image: Path) -> None:
    env = _env(xdg, appdir, image)
    directory = Paths.from_env(env).data_dir / helpers.HELPERS_DIR
    launcher = helpers.path("fork-linux-open-url", env=env)
    assert launcher == directory / "fork-linux-open-url"
    assert launcher.read_text(encoding="utf-8") == helpers.launcher_text("fork-linux-open-url", image, directory)
    assert launcher.stat().st_mode & 0o777 == 0o700
    assert directory.stat().st_mode & 0o777 == 0o700
    stamp = launcher.stat().st_mtime_ns
    os.utime(launcher, ns=(stamp - 10**9, stamp - 10**9))
    assert helpers.path("fork-linux-open-url", env=env) == launcher
    assert launcher.stat().st_mtime_ns == stamp - 10**9, "an unchanged launcher is not rewritten"


def test_a_moved_appimage_rewrites_the_launcher_at_the_same_path(
    xdg: Path, appdir: Path, image: Path, tmp_path: Path
) -> None:
    """A9: the stored path stays; only the launcher's target follows the AppImage."""
    first = helpers.path("fork-linux-terminal", paths=Paths.from_env(), env=_env(xdg, appdir, image))
    moved = tmp_path / "elsewhere" / "Fork Linux.AppImage"
    moved.parent.mkdir()
    shutil.move(str(image), moved)
    second = helpers.path("fork-linux-terminal", env=_env(xdg, appdir, moved))
    assert second == first
    assert "'" + str(moved) + "'" in second.read_text(encoding="utf-8")


def test_launchers_replace_links_and_unreadable_files(xdg: Path, appdir: Path, image: Path, tmp_path: Path) -> None:
    env = _env(xdg, appdir, image)
    directory = Paths.from_env(env).data_dir / helpers.HELPERS_DIR
    directory.mkdir(parents=True)
    (tmp_path / "elsewhere").write_text("not ours\n", encoding="utf-8")
    (directory / "fork-linux-host").symlink_to(tmp_path / "elsewhere")
    host = helpers.path("fork-linux-host", env=env)
    assert not host.is_symlink()
    assert (tmp_path / "elsewhere").read_text(encoding="utf-8") == "not ours\n"
    (directory / "fork-linux-explorer").mkdir()
    with pytest.raises(OSError):
        helpers.path("fork-linux-explorer", env=env)


def test_unknown_or_missing_helpers_keep_their_installed_path(xdg: Path, appdir: Path, image: Path) -> None:
    env = _env(xdg, appdir, image)
    assert helpers.path("fork-linux-handoff", env=env) == resources.libexec_dir() / "fork-linux-handoff"
    (resources.libexec_dir() / "fork-linux-terminal").unlink()
    assert helpers.path("fork-linux-terminal", env=env) == resources.libexec_dir() / "fork-linux-terminal"


def test_launcher_starts_the_appimage_with_libexec(xdg: Path, appdir: Path, image: Path, tmp_path: Path) -> None:
    env = _env(xdg, appdir, image)
    launcher = helpers.path("fork-linux-explorer", env=env)
    log = tmp_path / "calls.log"
    subprocess.run([str(launcher), "/select,Z:\\x y"], env={**env, "RECORD_LOG": str(log)}, check=True)
    assert log.read_text(encoding="utf-8").splitlines() == [
        "fork-linux.AppImage",
        str(launcher.parent),
        "--libexec",
        "fork-linux-explorer",
        "/select,Z:\\x y",
    ]


# --------------------------------------------------------------------- AppRun --libexec


def _apprun(appdir: Path, tmp_path: Path, *args: str) -> tuple[int, list[str], str]:
    """Run a copy of AppRun at the AppDir root with the host's Python."""
    shutil.copy2(ROOT / "packaging" / "common" / "find-python.sh", appdir / "usr" / "lib" / "fork-linux")
    shutil.copy2(APPRUN, appdir / "AppRun")
    log = tmp_path / "apprun.log"
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path),
        "RECORD_LOG": str(log),
        "FORK_LINUX_HELPER_DIR": "/stable",
        "FORK_LINUX_PYTHON": sys.executable,
    }
    done = subprocess.run([str(appdir / "AppRun"), *args], env=env, capture_output=True, text=True, check=False)
    calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    return done.returncode, calls, done.stderr


@pytest.mark.parametrize("name", ["fork-linux-terminal", "fork-linux-explorer", "fork-linux-open-url"])
def test_apprun_libexec_runs_a_shipped_helper(appdir: Path, tmp_path: Path, name: str) -> None:
    code, calls, err = _apprun(appdir, tmp_path, "--libexec", name, "a b", "c")
    assert code == 0, err
    assert calls == [name, "/stable", "a b", "c"]


def test_apprun_libexec_runs_the_host_helper_with_python(appdir: Path, tmp_path: Path) -> None:
    host = appdir / "usr" / "lib" / "fork-linux" / "fork-linux-host"
    host.write_text(
        "import os, sys\n"
        "with open(os.environ['RECORD_LOG'], 'a') as log:\n"
        "    log.write('\\n'.join(['python', sys.flags.isolated and 'isolated' or 'plain', *sys.argv[1:]]) + '\\n')\n",
        encoding="utf-8",
    )
    code, calls, err = _apprun(appdir, tmp_path, "--libexec", "fork-linux-host", "open", "Z:\\x")
    assert code == 0, err
    assert calls == ["python", "isolated", "open", "Z:\\x"]


@pytest.mark.parametrize("args", [("--libexec",), ("--libexec", "fork-linux-handoff"), ("--libexec", "../../bin/sh")])
def test_apprun_libexec_refuses_anything_else(appdir: Path, tmp_path: Path, args: tuple[str, ...]) -> None:
    code, calls, err = _apprun(appdir, tmp_path, *args)
    assert code == 2
    assert "--libexec: unknown helper" in err
    assert calls == []


def test_apprun_and_helpers_list_the_same_helpers() -> None:
    text = APPRUN.read_text(encoding="utf-8")
    plain = [name for name in helpers.NAMES if name != "fork-linux-host"]
    assert f"\t\t\t{' | '.join(plain)}) ;;\n" in text
    assert "\t\t\tfork-linux-host)\n" in text


# --------------------------------------------------------------------- snap


@pytest.fixture
def snap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """/snap/fork-linux/x2 (the running revision) with current -> x2, under tmp_path."""
    root = tmp_path / "snap"
    revision = root / "fork-linux" / "x2"
    libexec = revision / "usr" / "lib" / "fork-linux"
    libexec.mkdir(parents=True)
    (libexec / "fork-linux-terminal").write_text("#!/bin/sh\n", encoding="utf-8")
    (root / "fork-linux" / "current").symlink_to("x2")
    monkeypatch.setattr(helpers, "SNAP_ROOT", root)
    monkeypatch.setattr(resources, "libexec_dir", lambda: libexec)
    return {"SNAP": str(revision), "SNAP_NAME": "fork-linux"}


def test_a10_snap_helpers_go_through_the_current_link(snap: dict[str, str]) -> None:
    """A snap refresh changes the revision directory; the stored paths name 'current' and stay valid."""
    terminal = helpers.path("fork-linux-terminal", env=snap)
    current = helpers.SNAP_ROOT / "fork-linux" / "current"
    assert terminal == current / "usr" / "lib" / "fork-linux" / "fork-linux-terminal"
    assert terminal.is_file()


def test_snap_current_leaves_other_paths_alone(snap: dict[str, str], tmp_path: Path) -> None:
    installed = resources.libexec_dir() / "fork-linux-terminal"
    assert helpers.snap_current(installed, {}) == installed
    assert helpers.snap_current(installed, {**snap, "SNAP_NAME": "a/b"}) == installed
    assert helpers.snap_current(installed, {**snap, "SNAP": "relative"}) == installed
    elsewhere = tmp_path / "usr" / "lib" / "fork-linux-terminal"
    assert helpers.snap_current(elsewhere, snap) == elsewhere
    (helpers.SNAP_ROOT / "fork-linux" / "current").unlink()
    (helpers.SNAP_ROOT / "fork-linux" / "current").symlink_to("x3")  # refreshed while this one runs
    assert helpers.snap_current(installed, snap) == installed
