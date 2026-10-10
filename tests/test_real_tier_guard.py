"""Tests for tests/fixtures/real_tier.py: the real tiers run only in a container, and the E2E
root helpers never delete outside their root, never follow symlinks and refuse ``/`` and homes."""

from __future__ import annotations

import os
import pwd
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from fixtures import real_tier

REPO = Path(__file__).resolve().parents[1]
MARKER = ".fl-e2e-root"


@pytest.fixture
def homes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """A fake ``$HOME`` and passwd home, both outside the scratch area ``tmp_path/scratch``."""
    home = tmp_path / "home"
    pw_home = tmp_path / "pwhome"
    for directory in (home, pw_home, tmp_path / "scratch"):
        directory.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(pwd, "getpwuid", lambda uid: SimpleNamespace(pw_dir=str(pw_home)))
    return {"home": home, "pw": pw_home, "scratch": tmp_path / "scratch"}


# -- the container gate ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({}, []),
        ({"FL_E2E_FORK": "1"}, ["FL_E2E_FORK"]),
        ({"FL_REAL_WINE": "1", "FL_E2E_FORK": "1"}, ["FL_E2E_FORK", "FL_REAL_WINE"]),
        ({"FL_E2E_FORK": "0", "FL_REAL_WINE": ""}, []),
        ({"FL_E2E_DE": "yes"}, ["FL_E2E_DE"]),
        ({"FL_E2E_ROOT": "/e2e/root", "FL_E2E_SEED": "/seed", "FL_E2E_SLOTS": "1", "FL_E2E_DISPLAY": ":98"}, []),
        ({"FL_E2E_FORK_VERSION": "2.23.2", "FL_E2E_WINETRICKS_CACHE": "/seed/winetricks"}, []),
        ({"FL_CI_STAGE": "bridge", "WINEPREFIX": "/x"}, []),
    ],
)
def test_active_gates(env: dict[str, str], expected: list[str]) -> None:
    assert real_tier.active_gates(env) == expected


def test_in_container_looks_for_docker_and_podman_markers(tmp_path: Path) -> None:
    missing = (str(tmp_path / "dockerenv"), str(tmp_path / "containerenv"))
    assert real_tier.in_container(missing) is False
    (tmp_path / "containerenv").write_text("", encoding="utf-8")
    assert real_tier.in_container(missing) is True
    assert real_tier.CONTAINER_MARKERS == ("/.dockerenv", "/run/.containerenv")


def test_refusal_only_for_a_real_tier_outside_a_container(tmp_path: Path) -> None:
    host = (str(tmp_path / "none"),)
    marker = tmp_path / "dockerenv"
    marker.write_text("", encoding="utf-8")
    assert real_tier.refusal({}, host) is None
    assert real_tier.refusal({"FL_E2E_ROOT": "/x"}, host) is None
    assert real_tier.refusal({"FL_E2E_FORK": "1"}, (str(marker),)) is None
    reason = real_tier.refusal({"FL_E2E_FORK": "1", "FL_REAL_WINE": "1"}, host)
    assert reason is not None
    assert "FL_E2E_FORK=1, FL_REAL_WINE=1" in reason
    assert "scripts/e2e-docker.sh" in reason
    assert "2026-10-10" in reason
    assert "hard rule 18" in reason


def test_enforce_exits_with_status_2(tmp_path: Path) -> None:
    with pytest.raises(pytest.exit.Exception) as caught:
        real_tier.enforce({"FL_REAL_WINE": "1"}, (str(tmp_path / "none"),))
    assert caught.value.returncode == 2
    assert "only inside a container" in str(caught.value)
    real_tier.enforce({}, (str(tmp_path / "none"),))


def test_enforce_reads_the_process_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FL_E2E_FORK", "1")
    with pytest.raises(pytest.exit.Exception):
        real_tier.enforce(markers=(str(tmp_path / "none"),))
    monkeypatch.delenv("FL_E2E_FORK")
    real_tier.enforce(markers=(str(tmp_path / "none"),))


def test_pytest_on_the_e2e_tier_refuses_on_the_host_and_creates_nothing(tmp_path: Path) -> None:
    """The real tests/conftest.py: on the host the session stops in pytest_configure with status 2;
    inside a container (CI's jammy cell) collection goes ahead. Either way nothing is created."""
    root = tmp_path / "root"
    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir()
    env = {key: value for key, value in os.environ.items() if not key.startswith(("FL_E2E_", "FL_REAL_"))}
    env.update(
        {
            "FL_E2E_FORK": "1",
            "FL_E2E_ROOT": str(root),
            "TMPDIR": str(tmpdir),
            "HOME": str(tmp_path / "home"),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-o", "addopts=", "--collect-only", "-q",
         "tests/e2e"],
        cwd=str(REPO), env=env, capture_output=True, text=True, timeout=120, check=False,
    )
    if real_tier.in_container():
        assert result.returncode == 0, result.stdout + result.stderr
        assert "test_01_setup_completes_and_resumes" in result.stdout
    else:
        assert result.returncode == 2, result.stdout + result.stderr
        assert "refusing to run the real Wine / Fork tier on the host (FL_E2E_FORK=1)" in result.stderr
        assert "scripts/e2e-docker.sh" in result.stderr
    assert not root.exists()
    assert not (tmp_path / "home").exists()
    assert list(tmpdir.iterdir()) == []


# -- home directories -----------------------------------------------------------------------------


def test_home_dirs_lists_home_and_passwd_home_resolved(homes: dict[str, Path], tmp_path: Path,
                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    link = tmp_path / "home-link"
    link.symlink_to(homes["home"])
    monkeypatch.setenv("HOME", str(link))
    assert real_tier.home_dirs(os.environ) == [link, homes["home"], homes["pw"]]


def test_home_dirs_without_home_or_passwd_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_entry(uid: int) -> None:
        raise KeyError(uid)

    monkeypatch.setattr(pwd, "getpwuid", no_entry)
    assert real_tier.home_dirs({}) == []
    assert real_tier.home_dirs({"HOME": ""}) == []


# -- check_root -----------------------------------------------------------------------------------


def test_check_root_accepts_a_plain_scratch_path(homes: dict[str, Path]) -> None:
    wanted = homes["scratch"] / "e2e" / "root"
    assert real_tier.check_root(homes["scratch"] / "e2e" / "." / "root") == wanted
    assert real_tier.check_root(homes["scratch"], {"HOME": str(homes["home"])}) == homes["scratch"]


@pytest.mark.parametrize(
    ("path", "message"),
    [
        (Path("relative/root"), "absolute"),
        (Path("/tmp/x/../root"), "'..'"),
        (Path("/"), "file system root"),
    ],
)
def test_check_root_refuses_bad_shapes(homes: dict[str, Path], path: Path, message: str) -> None:
    with pytest.raises(real_tier.RefusedPath, match=message):
        real_tier.check_root(path)


@pytest.mark.parametrize("which", ["home", "pw"])
def test_check_root_refuses_homes(homes: dict[str, Path], which: str) -> None:
    home = homes[which]
    for path in (home, home / ".cache" / "fl-e2e", home.parent):
        with pytest.raises(real_tier.RefusedPath, match="home directory"):
            real_tier.check_root(path)


def test_check_root_refuses_a_resolved_home(homes: dict[str, Path], tmp_path: Path,
                                            monkeypatch: pytest.MonkeyPatch) -> None:
    link = tmp_path / "scratch" / "home-link"
    link.symlink_to(homes["home"])
    monkeypatch.setenv("HOME", str(link))
    with pytest.raises(real_tier.RefusedPath, match="home directory"):
        real_tier.check_root(homes["home"] / "e2e")


def test_check_root_refuses_symlink_components(homes: dict[str, Path], tmp_path: Path) -> None:
    target = tmp_path / "elsewhere"
    target.mkdir()
    (homes["scratch"] / "link").symlink_to(target)
    with pytest.raises(real_tier.RefusedPath, match="is a symlink"):
        real_tier.check_root(homes["scratch"] / "link" / "root")
    with pytest.raises(real_tier.RefusedPath, match="is a symlink"):
        real_tier.check_root(homes["scratch"] / "link")


# -- remove_inside --------------------------------------------------------------------------------


def test_remove_inside_deletes_files_and_trees(homes: dict[str, Path]) -> None:
    root = homes["scratch"]
    (root / "tree" / "sub").mkdir(parents=True)
    (root / "tree" / "sub" / "f").write_text("x", encoding="utf-8")
    (root / "file").write_text("x", encoding="utf-8")
    real_tier.remove_inside(root / "tree", root)
    real_tier.remove_inside(root / "file", root)
    real_tier.remove_inside(root / "missing", root)
    assert list(root.iterdir()) == []


def test_remove_inside_unlinks_symlinks_without_following_them(homes: dict[str, Path]) -> None:
    root = homes["scratch"]
    precious = homes["home"] / "precious.txt"
    precious.write_text("keep me", encoding="utf-8")
    (root / "to-home").symlink_to(homes["home"])
    (root / "tree").mkdir()
    (root / "tree" / "to-home").symlink_to(homes["home"])
    real_tier.remove_inside(root / "to-home", root)
    real_tier.remove_inside(root / "tree", root)
    assert precious.read_text(encoding="utf-8") == "keep me"
    assert list(root.iterdir()) == []


@pytest.mark.parametrize("rel", ["", "..", "../home", "../scratch-sibling/x"])
def test_remove_inside_refuses_paths_outside_the_root(homes: dict[str, Path], rel: str) -> None:
    root = homes["scratch"]
    with pytest.raises(real_tier.RefusedPath, match="outside the E2E root"):
        real_tier.remove_inside(root / rel if rel else root, root)
    assert homes["home"].is_dir()


def test_remove_inside_refuses_a_symlinked_parent(homes: dict[str, Path]) -> None:
    root = homes["scratch"]
    (homes["home"] / "doc").write_text("keep", encoding="utf-8")
    (root / "via").symlink_to(homes["home"])
    with pytest.raises(real_tier.RefusedPath, match="is a symlink"):
        real_tier.remove_inside(root / "via" / "doc", root)
    assert (homes["home"] / "doc").is_file()


def test_remove_inside_needs_a_symlink_safe_rmtree(homes: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    root = homes["scratch"]
    (root / "tree").mkdir()
    monkeypatch.setattr(shutil.rmtree, "avoids_symlink_attacks", False)
    with pytest.raises(real_tier.RefusedPath, match="may follow symlinks"):
        real_tier.remove_inside(root / "tree", root)
    assert (root / "tree").is_dir()


# -- prepare_root ---------------------------------------------------------------------------------


def test_prepare_root_creates_a_private_marked_root(homes: dict[str, Path]) -> None:
    root = homes["scratch"] / "a" / "root"
    assert real_tier.prepare_root(root, MARKER) == root
    assert (root.stat().st_mode & 0o777) == 0o700
    assert (root / MARKER).read_text(encoding="utf-8") == "fork-linux E2E root\n"
    assert [entry.name for entry in root.iterdir()] == [MARKER]


def test_prepare_root_reuses_a_marked_root_and_keeps_the_cache(homes: dict[str, Path]) -> None:
    root = homes["scratch"] / "root"
    real_tier.prepare_root(root, MARKER)
    (root / "home" / ".cache" / "fork-linux").mkdir(parents=True)
    (root / "home" / ".cache" / "fork-linux" / "wine.tar.xz").write_text("x", encoding="utf-8")
    (root / "home" / ".local" / "share").mkdir(parents=True)
    (root / "home" / "file").write_text("x", encoding="utf-8")
    (root / "shots").mkdir()
    (root / "fork-linux-logs.tar.gz").write_text("x", encoding="utf-8")
    real_tier.prepare_root(root, MARKER, keep=(".cache",))
    assert sorted(entry.name for entry in root.iterdir()) == [MARKER, "home"]
    assert [entry.name for entry in (root / "home").iterdir()] == [".cache"]
    assert (root / "home" / ".cache" / "fork-linux" / "wine.tar.xz").is_file()


def test_prepare_root_unlinks_a_symlinked_home_without_touching_its_target(homes: dict[str, Path]) -> None:
    """The scenario the old _prepare_root got wrong: <root>/home -> the real home."""
    root = homes["scratch"] / "root"
    real_tier.prepare_root(root, MARKER)
    precious = homes["home"] / "Projects"
    precious.mkdir()
    (root / "home").symlink_to(homes["home"])
    real_tier.prepare_root(root, MARKER, keep=(".cache",))
    assert precious.is_dir()
    assert not (root / "home").exists()


def test_prepare_root_accepts_an_empty_unmarked_directory(homes: dict[str, Path]) -> None:
    root = homes["scratch"] / "root"
    root.mkdir(mode=0o755)
    real_tier.prepare_root(root, MARKER)
    assert (root.stat().st_mode & 0o777) == 0o700
    assert (root / MARKER).is_file()


def test_prepare_root_refuses_an_unmarked_non_empty_directory(homes: dict[str, Path]) -> None:
    root = homes["scratch"] / "root"
    root.mkdir()
    (root / "someone-elses.txt").write_text("x", encoding="utf-8")
    with pytest.raises(real_tier.RefusedPath, match="not created by the E2E tier"):
        real_tier.prepare_root(root, MARKER)
    assert (root / "someone-elses.txt").is_file()


def test_prepare_root_refuses_a_symlinked_marker(homes: dict[str, Path]) -> None:
    root = homes["scratch"] / "root"
    root.mkdir()
    (homes["home"] / "marker").write_text("x", encoding="utf-8")
    (root / MARKER).symlink_to(homes["home"] / "marker")
    (root / "data").write_text("x", encoding="utf-8")
    with pytest.raises(real_tier.RefusedPath, match="not created by the E2E tier"):
        real_tier.prepare_root(root, MARKER)


def test_prepare_root_never_writes_through_a_marker_symlink(homes: dict[str, Path]) -> None:
    root = homes["scratch"] / "root"
    root.mkdir()
    target = homes["home"] / "victim"
    target.write_text("keep", encoding="utf-8")
    (root / MARKER).symlink_to(target)
    with pytest.raises(OSError):
        real_tier.prepare_root(root, MARKER)
    assert target.read_text(encoding="utf-8") == "keep"


def test_prepare_root_refuses_home_and_slash(homes: dict[str, Path]) -> None:
    for root in (homes["home"], Path("/")):
        with pytest.raises(real_tier.RefusedPath):
            real_tier.prepare_root(root, MARKER)
    assert not (homes["home"] / MARKER).exists()
