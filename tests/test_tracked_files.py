"""Every build input on disk must be committed: an over-broad .gitignore pattern once hid
docker/Dockerfile.snap (matched by '*.snap'), which only broke CI on GitHub."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
TRACKED_DIRS = ("docker", "packaging", "debian", "scripts", ".github/workflows", "data", "libexec", "bin")


def _ignored(paths: list[str]) -> set[str]:
    if not paths:
        return set()
    result = subprocess.run(
        ["git", "-C", str(REPO), "check-ignore", "--no-index", "--stdin"],
        input="\n".join(paths), capture_output=True, text=True, check=False,
    )
    return set(result.stdout.split())


def test_no_build_input_is_gitignored() -> None:
    if not (REPO / ".git").exists():
        pytest.skip("not a git checkout")
    candidates = [
        str(path.relative_to(REPO))
        for directory in TRACKED_DIRS
        for path in (REPO / directory).rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and ".craft" not in path.parts
    ]
    assert _ignored(candidates) == set()


def test_every_dockerfile_matches_no_ignore_pattern() -> None:
    if not (REPO / ".git").exists():
        pytest.skip("not a git checkout")
    dockerfiles = [str(p.relative_to(REPO)) for p in (REPO / "docker").glob("Dockerfile*")]
    assert dockerfiles
    assert _ignored(dockerfiles) == set()
