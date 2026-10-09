"""Tests for bin/fork-linux.in: the relocatable, isolated-mode launcher and its ``fork`` symlink.

The template is rendered (``@PYTHON@`` -> ``sys.executable``) into temporary
trees laid out like an installation, a ``FORK_LINUX_LIBDIR`` setup and a
source checkout, then run through subprocesses without ``PYTHONPATH``.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "bin" / "fork-linux.in"
PACKAGE = ROOT / "src" / "fork_linux"


def _render(root: Path) -> Path:
    """Render the launcher into ``root/bin/fork-linux`` with a ``fork`` symlink beside it."""
    bin_dir = root / "bin"
    bin_dir.mkdir(parents=True)
    launcher = bin_dir / "fork-linux"
    launcher.write_text(TEMPLATE.read_text(encoding="utf-8").replace("@PYTHON@", sys.executable), encoding="utf-8")
    launcher.chmod(0o755)
    (bin_dir / "fork").symlink_to("fork-linux")
    return launcher


def _copy_package(dest: Path) -> None:
    """Copy the fork_linux package (without bytecode caches) to ``dest/fork_linux``."""
    shutil.copytree(PACKAGE, dest / "fork_linux", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))


def _env(home: Path, **extra: str) -> dict[str, str]:
    """A minimal environment: no PYTHONPATH, private HOME."""
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    env.update(extra)
    return env


def _run(
    executable: Path, *args: str, env: dict[str, str], cwd: Path | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(executable), *args],
        env=env,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _assert_works(root: Path, env: dict[str, str], version_prefix: str = "fork-linux ") -> None:
    """``fork-linux`` and ``fork`` both answer --version and --help."""
    launcher, alias = root / "bin" / "fork-linux", root / "bin" / "fork"
    for executable in (launcher, alias):
        result = _run(executable, "--version", env=env)
        assert result.returncode == 0, result.stderr
        assert result.stdout.startswith(version_prefix), result.stdout
    result = _run(launcher, "--help", env=env)
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("usage: fork-linux")
    result = _run(alias, "--help", env=env)
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("usage: fork [OPTIONS]")


def test_template_shape() -> None:
    text = TEMPLATE.read_text(encoding="utf-8")
    assert text.splitlines()[0] == "#!@PYTHON@ -I"
    assert text.count("@PYTHON@") == 1
    assert "FORK_LINUX_LIBDIR" in text
    assert '"share" / "fork-linux"' in text
    assert "main(prog=sys.argv[0])" in text
    compile(text.replace("@PYTHON@", sys.executable), str(TEMPLATE), "exec")


def test_installed_layout(tmp_path: Path) -> None:
    root = tmp_path / "usr"
    _render(root)
    _copy_package(root / "share" / "fork-linux")
    _assert_works(root, _env(tmp_path / "home"))


def test_libdir_override(tmp_path: Path) -> None:
    root = tmp_path / "opt"
    _render(root)
    _assert_works(root, _env(tmp_path / "home", FORK_LINUX_LIBDIR=str(ROOT / "src")))


def test_source_checkout_layout_uses_its_version_file(tmp_path: Path) -> None:
    root = tmp_path / "checkout"
    _render(root)
    _copy_package(root / "src")
    (root / "VERSION").write_text("9.8.7\n", encoding="utf-8")
    _assert_works(root, _env(tmp_path / "home"), version_prefix="fork-linux 9.8.7-dev (source)")


def _decoy_package(parent: Path) -> None:
    """A ``fork_linux`` package that fails loudly if it is ever imported."""
    (parent / "fork_linux").mkdir(parents=True)
    (parent / "fork_linux" / "__init__.py").write_text("raise SystemExit(99)\n", encoding="utf-8")


def test_installed_package_beats_a_stray_src_directory(tmp_path: Path) -> None:
    root = tmp_path / "usr"
    _render(root)
    _copy_package(root / "share" / "fork-linux")
    _decoy_package(root / "src")
    (root / "VERSION").write_text("6.6.6\n", encoding="utf-8")
    _assert_works(root, _env(tmp_path / "home"))


def test_src_directory_without_version_or_meson_is_not_a_checkout(tmp_path: Path) -> None:
    root = tmp_path / "local"
    launcher = _render(root)
    _decoy_package(root / "src")
    result = _run(launcher, "--version", env=_env(tmp_path / "home"))
    assert result.returncode != 99
    assert "fork_linux" in result.stderr


def test_meson_build_marks_a_checkout(tmp_path: Path) -> None:
    root = tmp_path / "checkout"
    _render(root)
    _copy_package(root / "src")
    (root / "meson.build").write_text("project('fork-linux')\n", encoding="utf-8")
    _assert_works(root, _env(tmp_path / "home"))


def test_symlinked_launcher_resolves_its_real_location(tmp_path: Path) -> None:
    root = tmp_path / "usr"
    _render(root)
    _copy_package(root / "share" / "fork-linux")
    links = tmp_path / "home" / ".local" / "bin"
    links.mkdir(parents=True)
    (links / "fork").symlink_to(root / "bin" / "fork")
    result = _run(links / "fork", "--version", env=_env(tmp_path / "home"))
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("fork-linux ")


def test_isolated_mode_ignores_pythonpath_and_cwd(tmp_path: Path) -> None:
    evil = tmp_path / "evil"
    (evil / "fork_linux").mkdir(parents=True)
    (evil / "fork_linux" / "__init__.py").write_text("raise SystemExit(99)\n", encoding="utf-8")
    root = tmp_path / "usr"
    _render(root)
    _copy_package(root / "share" / "fork-linux")
    env = _env(tmp_path / "home", PYTHONPATH=str(evil))
    result = _run(root / "bin" / "fork-linux", "--version", env=env, cwd=evil)
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("fork-linux ")


def test_missing_package_fails_loudly(tmp_path: Path) -> None:
    root = tmp_path / "broken"
    launcher = _render(root)
    result = _run(launcher, "--version", env=_env(tmp_path / "home", PYTHONPATH=str(ROOT / "src")))
    assert result.returncode != 0
    assert "fork_linux" in result.stderr
