"""Contract tests for docker/ (AGENTS.md §4.8): pinned bases, explicit image tags, one Dockerfile
per stage / cell, and scripts that use exactly the tags the Dockerfiles declare."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOCKER = ROOT / "docker"
DOCKERFILES = sorted(DOCKER.glob("Dockerfile*"))
BASES = {
    "ubuntu:26.04",
    "ubuntu:24.04",
    "ubuntu:22.04",
    "debian:13",
    "fedora:44",
    "opensuse/leap:16.0",
}
TOOL_IMAGES = {"rhysd/actionlint:1.7.12", "hadolint/hadolint:v2.14.0"}
ARCH_BASE = re.compile(r"archlinux:base(?:-devel)?-\d{8}\.0\.\d+")
EXPECTED = {
    # stages
    "Dockerfile.ci.lint": "fork-linux-ci-lint:26.04",
    "Dockerfile.ci.coverage": "fork-linux-ci-coverage:26.04",
    "Dockerfile.ci.bridge": "fork-linux-ci-bridge:26.04",
    # compat cells
    "Dockerfile.ci": "fork-linux-ci-resolute:26.04",
    "Dockerfile.ci.jammy": "fork-linux-ci-jammy:22.04",
    "Dockerfile.ci.noble": "fork-linux-ci-noble:24.04",
    "Dockerfile.ci.debian13": "fork-linux-ci-debian13:13",
    "Dockerfile.ci.fedora44": "fork-linux-ci-fedora44:44",
    "Dockerfile.ci.leap16": "fork-linux-ci-leap16:16.0",
    "Dockerfile.ci.arch": "fork-linux-ci-arch:base-20260906.0.587075",
    # packaging + E2E
    "Dockerfile.ppa": "fork-linux-ci-ppa:26.04",
    "Dockerfile.ppa.jammy": "fork-linux-ci-ppa-jammy:22.04",
    "Dockerfile.rpm.fedora": "fork-linux-ci-rpm-fedora:44",
    "Dockerfile.rpm.opensuse": "fork-linux-ci-rpm-opensuse:16.0",
    "Dockerfile.arch": "fork-linux-ci-arch-pkg:base-devel-20260906.0.587075",
    "Dockerfile.release": "fork-linux-ci-release:26.04",
    "Dockerfile.snap": "fork-linux-ci-snap:24.04",
    "Dockerfile.e2e.wine": "fork-linux-ci-e2e-wine:26.04",
}
SCRIPTS = [
    "scripts/ci-docker.sh",
    "scripts/ci-packaging-cell.sh",
    "scripts/ci-packaging-matrix.sh",
    "scripts/ci-snap-build.sh",
    "scripts/ci-e2e-wine.sh",
    "scripts/build-bridge.sh",
]


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _froms(text: str) -> list[str]:
    return re.findall(r"^FROM\s+(\S+)", text, re.MULTILINE)


def _image_tag(text: str) -> str:
    match = re.search(r"^# Image tag: (\S+)$", text, re.MULTILINE)
    assert match is not None, "missing '# Image tag:' header"
    return match.group(1)


def test_every_expected_dockerfile_exists_and_nothing_else() -> None:
    assert sorted(path.name for path in DOCKERFILES) == sorted(EXPECTED)


@pytest.mark.parametrize("path", DOCKERFILES, ids=lambda p: p.name)
def test_buildkit_frontend_is_pinned(path: Path) -> None:
    assert _text(path).splitlines()[0] == "# syntax=docker/dockerfile:1.27.0"


@pytest.mark.parametrize("path", DOCKERFILES, ids=lambda p: p.name)
def test_bases_are_pinned(path: Path) -> None:
    froms = _froms(_text(path))
    assert froms
    for image in froms:
        assert "latest" not in image
        assert "@sha256" not in image
        assert image in BASES | TOOL_IMAGES or ARCH_BASE.fullmatch(image), f"{path.name}: unpinned base {image}"
    assert froms[-1] not in TOOL_IMAGES, "the final stage must be a distribution base"


@pytest.mark.parametrize("path", DOCKERFILES, ids=lambda p: p.name)
def test_image_tag_suffix_equals_the_base_tag(path: Path) -> None:
    text = _text(path)
    tag = _image_tag(text)
    assert tag == EXPECTED[path.name]
    name, suffix = tag.rsplit(":", 1)
    assert name.startswith("fork-linux-ci-")
    assert suffix == _froms(text)[-1].rsplit(":", 1)[1]


@pytest.mark.parametrize("path", DOCKERFILES, ids=lambda p: p.name)
def test_no_unpinned_installers(path: Path) -> None:
    text = _text(path)
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    assert not re.search(r"curl[^\n|]*\|\s*(?:ba)?sh", code)
    pip_blocks = re.findall(r"pip3 install --break-system-packages \\\n((?:\s+\S+(?: \\)?\n)+)", text)
    for block in pip_blocks:
        for item in block.split():
            if item != "\\":
                assert "==" in item, f"{path.name}: pip package {item} is not pinned with =="


def test_no_dockerfiles_outside_docker_dir() -> None:
    listed = subprocess.run(
        ["git", "-c", f"safe.directory={ROOT}", "-C", str(ROOT), "ls-files", "-co", "--exclude-standard"],
        capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    stray = [rel for rel in listed if Path(rel).name.startswith("Dockerfile") and not rel.startswith("docker/")]
    assert stray == []


def test_scripts_use_the_declared_tags() -> None:
    declared = set(EXPECTED.values())
    for rel in SCRIPTS:
        for tag in re.findall(r"fork-linux-ci-[\w.-]+:[\w.-]+", _text(ROOT / rel)):
            assert tag in declared, f"{rel} uses {tag}, which no Dockerfile declares"
    used = {tag for rel in SCRIPTS for tag in re.findall(r"fork-linux-ci-[\w.-]+:[\w.-]+", _text(ROOT / rel))}
    assert declared - {"fork-linux-ci-e2e-wine:26.04"} <= used | {"fork-linux-ci-e2e-wine:26.04"}
    assert "fork-linux-ci-e2e-wine:26.04" in used


def test_ci_docker_maps_every_stage_and_cell() -> None:
    text = _text(ROOT / "scripts" / "ci-docker.sh")
    for stage, dockerfile in (
        ("lint", "Dockerfile.ci.lint"),
        ("coverage", "Dockerfile.ci.coverage"),
        ("bridge", "Dockerfile.ci.bridge"),
    ):
        assert re.search(rf"{stage}\)\n\s+DOCKERFILE=\"docker/{re.escape(dockerfile)}\"", text)
    for cell in ("jammy", "noble", "resolute", "debian13", "fedora44", "leap16", "arch"):
        assert re.search(rf"^\s+{cell}\) DOCKERFILE=\"docker/Dockerfile\.ci(?:\.\w+)?\"", text, re.MULTILINE)
    assert "--cache-from \"type=gha" in text
    assert "DOCKER_BUILDKIT=1" in text
    assert '-v "${ROOT}:${WORKDIR}:rw"' in text


def test_ci_images_bind_mount_instead_of_copying_the_tree() -> None:
    for path in DOCKERFILES:
        assert not re.search(r"^COPY \. ", _text(path), re.MULTILINE), f"{path.name} copies the whole tree"


def test_lint_stage_runs_every_linter() -> None:
    text = _text(ROOT / "scripts" / "ci-docker.sh")
    lint = text[text.index("\t\tlint)\n\t\t\tmeson_build") :]
    lint = lint[: lint.index(";;")]
    for step in (
        "meson_build --werror",
        "--suite data",
        "run_clang_tidy",
        "run_cppcheck",
        "run_py_compile",
        "run_ruff",
        "run_no_suppressions",
        "run_gen_data_check",
        "run_shellcheck",
        "run_actionlint",
        "run_yamllint",
        "run_hadolint",
        "run_xmllint",
        "run_completion_syntax",
    ):
        assert step in lint, f"the lint stage does not run {step}"
    for tool in ("ruff==", "yamllint==", "clang-tidy", "cppcheck", "shellcheck", "libxml2-utils", "zsh", "fish"):
        assert tool in _text(DOCKER / "Dockerfile.ci.lint")
    assert "shellcheck -x" in text
    assert "bash -n data/completions" in text
    assert "zsh -n" in text


def test_coverage_stage_gates() -> None:
    text = _text(ROOT / "scripts" / "ci-docker.sh")
    assert "--fail-under=90" in text
    assert "--fail-under=100" in text
    assert "--cov-branch" in text
    critical = re.search(r"local -a critical=\((.*?)\)", text, re.DOTALL)
    assert critical is not None
    modules = critical.group(1).split()
    assert modules == [
        "download", "manifest", "feeds", "fsutil", "locking", "state", "config_edit", "registry", "pathmap",
        "fork_settings", "snapshots", "bootstrap", "pe_resources", "imaging", "ssh_sync", "gitconfig", "errors",
    ]
    assert "artifacts/coverage/coverage.xml" in text
    assert "-Db_sanitize=address,undefined" in text
    assert "scripts/ci-c-coverage.sh" in text


def test_clang_tidy_config_errors_on_every_enabled_check() -> None:
    config = _text(ROOT / ".clang-tidy")
    assert "WarningsAsErrors: '*'" in config
    checks = re.search(r"Checks: >-\n((?:  .*\n)+)", config)
    assert checks is not None
    entries = [entry.strip().rstrip(",") for entry in checks.group(1).splitlines()]
    assert entries[0] == "-*"
    assert all(not entry.startswith("-") for entry in entries[1:]), "individual checks must not be disabled"
    assert "cert-env33-c" in entries
