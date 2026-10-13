"""Tests for fork_linux.resources: installed vs source-checkout resource lookup."""

from __future__ import annotations

from pathlib import Path

import pytest

from fork_linux import resources

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _no_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(resources.SHIMS_ENV, raising=False)
    monkeypatch.delenv(resources.LIBEXEC_ENV, raising=False)


def _fake_module(monkeypatch: pytest.MonkeyPatch, module_file: Path) -> None:
    module_file.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(resources, "_here", lambda: module_file)


def test_real_checkout_is_detected_as_source_tree() -> None:
    assert resources.is_source_tree()
    assert resources.install_root() == REPO_ROOT
    assert resources.data_path("x", "y.json") == REPO_ROOT / "src/fork_linux/data/x/y.json"
    assert resources.manifest_path() == REPO_ROOT / "src/fork_linux/data/runtime-manifest.json"
    assert resources.libexec_dir() == REPO_ROOT / "libexec"
    assert resources.host_helper_path() == REPO_ROOT / "libexec/fork-linux-host"


def test_installed_layout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "usr"
    _fake_module(monkeypatch, root / "share/fork-linux/fork_linux/resources.py")
    assert not resources.is_source_tree()
    assert resources.install_root() == root
    assert resources.data_path("templates", "a.in") == root / "share/fork-linux/fork_linux/data/templates/a.in"
    assert resources.manifest_path() == root / "share/fork-linux/fork_linux/data/runtime-manifest.json"
    assert resources.shims_dir() == root / "lib/fork-linux/win64"
    assert resources.libexec_dir() == root / "lib/fork-linux"
    assert resources.host_helper_path() == root / "lib/fork-linux/fork-linux-host"


@pytest.mark.parametrize("marker", ["meson.build", "VERSION"])
def test_source_layout_detection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, marker: str) -> None:
    repo = tmp_path / "repo"
    _fake_module(monkeypatch, repo / "src/fork_linux/resources.py")
    (repo / marker).write_text("x\n", encoding="utf-8")
    assert resources.is_source_tree()
    assert resources.install_root() == repo
    assert resources.libexec_dir() == repo / "libexec"


def test_src_directory_without_markers_is_not_a_checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "opt"
    _fake_module(monkeypatch, root / "src/fork_linux/resources.py")
    assert not resources.is_source_tree()
    assert resources.install_root() == tmp_path


def test_shallow_module_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(resources, "_here", lambda: Path("/resources.py"))
    assert not resources.is_source_tree()
    assert resources.install_root() == Path("/")


def test_source_shims_dir_prefers_first_build_dir_with_bridge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    _fake_module(monkeypatch, repo / "src/fork_linux/resources.py")
    (repo / "meson.build").write_text("project('x')\n", encoding="utf-8")
    assert resources.shims_dir() is None
    (repo / "build-aaa").mkdir()
    assert resources.shims_dir() is None, "a build dir without bridge/ is skipped"
    (repo / "build-zzz/bridge").mkdir(parents=True)
    assert resources.shims_dir() == repo / "build-zzz/bridge"
    (repo / "build/bridge").mkdir(parents=True)
    assert resources.shims_dir() == repo / "build/bridge"


def test_environment_overrides(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(resources.SHIMS_ENV, str(tmp_path / "shims"))
    monkeypatch.setenv(resources.LIBEXEC_ENV, str(tmp_path / "libexec"))
    assert resources.shims_dir() == tmp_path / "shims"
    assert resources.libexec_dir() == tmp_path / "libexec"
    assert resources.host_helper_path() == tmp_path / "libexec" / "fork-linux-host"


def test_empty_overrides_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(resources.SHIMS_ENV, "")
    monkeypatch.setenv(resources.LIBEXEC_ENV, "")
    assert resources.libexec_dir() == REPO_ROOT / "libexec"


def test_bridge_helper_and_build_script_installed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "usr"
    _fake_module(monkeypatch, root / "share/fork-linux/fork_linux/resources.py")
    assert resources.bridge_helper_path() == root / "lib/fork-linux/fl-bridge-helper"
    assert resources.bridge_build_script() is None


def test_bridge_helper_and_build_script_in_a_checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    _fake_module(monkeypatch, repo / "src/fork_linux/resources.py")
    (repo / "VERSION").write_text("0.1.0\n", encoding="utf-8")
    # Not built yet: where scripts/build-bridge.sh puts it.
    assert resources.bridge_helper_path() == repo / "build-bridge/bridge/fl-bridge-helper"
    assert resources.bridge_build_script() is None
    (repo / "scripts").mkdir()
    (repo / "scripts/build-bridge.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    assert resources.bridge_build_script() == repo / "scripts/build-bridge.sh"
    # build-bridge/ wins over other build directories; the helper sits next to the shims.
    (repo / "build/bridge").mkdir(parents=True)
    assert resources.bridge_helper_path() == repo / "build/bridge/fl-bridge-helper"
    (repo / "build-bridge/bridge").mkdir(parents=True)
    assert resources.shims_dir() == repo / "build-bridge/bridge"
    assert resources.bridge_helper_path() == repo / "build-bridge/bridge/fl-bridge-helper"
    monkeypatch.setenv(resources.LIBEXEC_ENV, str(tmp_path / "lx"))
    assert resources.bridge_helper_path() == tmp_path / "lx/fl-bridge-helper"
