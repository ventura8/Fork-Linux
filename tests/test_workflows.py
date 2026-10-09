"""Contract tests for the GitHub Actions workflows (AGENTS.md §4.8) and the release gate.

The workflows are read as text (stdlib only, no YAML parser): each test pins one rule the
agents' guide makes mandatory, so a change that breaks it fails here before CI does.
"""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
CHECK = (WORKFLOWS / "check.yml").read_text(encoding="utf-8")
RELEASE = (WORKFLOWS / "release.yml").read_text(encoding="utf-8")
E2E = (WORKFLOWS / "e2e-wine.yml").read_text(encoding="utf-8")
UPSTREAM = (WORKFLOWS / "upstream-watch.yml").read_text(encoding="utf-8")
ALL = {"check.yml": CHECK, "release.yml": RELEASE, "e2e-wine.yml": E2E, "upstream-watch.yml": UPSTREAM}
COMPAT_CELLS = ["jammy", "noble", "resolute", "debian13", "fedora44", "leap16", "arch"]
FORMATS = ["deb", "deb-jammy", "rpm-fedora", "rpm-opensuse", "arch", "snap", "appimage", "flatpak", "tarball"]
FORK_PR_GUARD = (
    "github.event_name != 'pull_request' || github.event.pull_request.head.repo.full_name == github.repository"
)
VERSION = (ROOT / "VERSION").read_text(encoding="utf-8").strip()


def _job(text: str, name: str) -> str:
    """The text of one job (from ``  name:`` to the next top-level job)."""
    match = re.search(rf"^  {re.escape(name)}:\n(.*?)(?=^  [\w-]+:\n|\Z)", text, re.MULTILINE | re.DOTALL)
    assert match is not None, f"job {name} not found"
    return match.group(1)


def _matrix(job: str, key: str) -> list[str]:
    match = re.search(rf"^\s+{key}: \[(.*?)\]", job, re.MULTILINE)
    assert match is not None, f"matrix {key} not found"
    return [item.strip() for item in match.group(1).split(",")]


def test_all_four_workflows_exist() -> None:
    assert sorted(path.name for path in WORKFLOWS.glob("*.yml")) == sorted(ALL)


@pytest.mark.parametrize("name", sorted(ALL))
def test_actions_are_pinned_to_explicit_tags(name: str) -> None:
    uses = re.findall(r"uses:\s*(\S+)", ALL[name])
    assert uses
    for ref in uses:
        assert re.fullmatch(r"[\w.-]+/[\w.-]+@v\d+\.\d+\.\d+", ref), f"{name}: {ref} is not pinned to vN.N.N"


@pytest.mark.parametrize("name", sorted(ALL))
def test_runners_are_the_baseline(name: str) -> None:
    runners = re.findall(r"runs-on:\s*(\S+)", ALL[name])
    assert runners
    assert set(runners) == {"ubuntu-26.04"}


def test_runner_label_is_declared_for_actionlint() -> None:
    assert "ubuntu-26.04" in (ROOT / ".github" / "actionlint.yaml").read_text(encoding="utf-8")


def test_check_triggers_and_concurrency() -> None:
    assert re.search(r"^  push:\n    branches: \[main\]", CHECK, re.MULTILINE)
    assert re.search(r"^  pull_request:", CHECK, re.MULTILINE)
    assert re.search(r"^  workflow_dispatch:", CHECK, re.MULTILINE)
    assert "cancel-in-progress: true" in CHECK


def test_check_has_no_job_gates() -> None:
    assert not re.search(r"^\s+needs:", CHECK, re.MULTILINE)


def test_check_jobs_and_matrices() -> None:
    jobs = re.findall(r"^  ([\w-]+):\n    (?:if|runs-on|strategy)", CHECK, re.MULTILINE)
    assert jobs == ["lint", "coverage", "bridge", "compat", "packaging"]
    compat = _job(CHECK, "compat")
    assert _matrix(compat, "cell") == COMPAT_CELLS
    assert "fail-fast: false" in compat
    packaging = _job(CHECK, "packaging")
    assert _matrix(packaging, "format") == FORMATS
    assert "fail-fast: false" in packaging
    assert f"if: {FORK_PR_GUARD}" in packaging
    assert "./scripts/ci-packaging-cell.sh" in packaging
    for stage in ("lint", "coverage", "bridge"):
        assert f"FL_CI_STAGE: {stage}" in _job(CHECK, stage)


def test_coverage_job_runs_c_coverage_then_sonar_like_ubuntu_hello() -> None:
    coverage = _job(CHECK, "coverage")
    assert "fetch-depth: 0" in coverage
    c_cov = coverage.index("./scripts/ci-c-coverage.sh")
    check = coverage.index("./scripts/ci-sonar.sh --check-token")
    scan = coverage.index("uses: SonarSource/sonarqube-scan-action@v8.2.2")
    assert c_cov < check < scan
    assert coverage.count("SONAR_TOKEN: ${{ secrets.SONAR_TOKEN }}") == 2
    assert coverage.count(f"if: {FORK_PR_GUARD}") >= 2


def test_release_triggers_and_dry_run_input() -> None:
    assert re.search(r"^  push:\n    tags:\n      - 'v\*'", RELEASE, re.MULTILINE)
    assert "workflow_dispatch:" in RELEASE
    assert re.search(r"dry_run:\n\s+description: .+\n\s+type: boolean\n\s+default: true", RELEASE)


def test_release_verifies_tag_and_notes_before_building() -> None:
    verify = _job(RELEASE, "verify")
    assert "./scripts/release-verify-tag-version.sh" in verify
    for job in ("ppa", "build", "github-release"):
        assert re.search(r"needs: (?:\[)?(?:verify|\[verify)", _job(RELEASE, job)), job


def test_release_builds_every_cell_and_ppa_series() -> None:
    build = _job(RELEASE, "build")
    assert _matrix(build, "format") == FORMATS
    assert "./scripts/ci-packaging-cell.sh" in build
    ppa = _job(RELEASE, "ppa")
    assert _matrix(ppa, "series") == ["jammy", "noble", "resolute"]
    assert "--dry-run" in ppa
    assert "UPLOAD_PPA=1" in ppa
    assert "vars.PPA_UPLOAD == 'true'" in ppa


def test_release_creates_the_github_release() -> None:
    job = _job(RELEASE, "github-release")
    assert "contents: write" in job
    assert "SHA256SUMS" in job
    assert "SHA256SUMS.asc" in job
    assert "cp install.sh uninstall.sh release-artifacts/" in job
    step = job[job.index("uses: softprops/action-gh-release@") :]
    assert re.search(r"softprops/action-gh-release@v\d+\.\d+\.\d+", job)
    assert "name: v${{ needs.verify.outputs.version }}" in step
    assert "body_path: docs/releases/v${{ needs.verify.outputs.version }}_github_description.md" in step
    assert "prerelease: false" in step
    assert "draft: false" in step
    create = job[job.rindex("- name:", 0, job.index("softprops")) :]
    assert "if: env.DRY_RUN != 'true' && github.event_name == 'push'" in create


def test_release_store_publishing_is_gated() -> None:
    assert "if: github.event_name == 'push' && vars.SNAP_PUBLISH == 'true'" in _job(RELEASE, "snap-publish")
    assert "if: github.event_name == 'push' && vars.AUR_PUBLISH == 'true'" in _job(RELEASE, "aur-publish")


def test_e2e_wine_uploads_logs_only() -> None:
    assert "schedule:" in E2E
    assert "workflow_dispatch:" in E2E
    assert "./scripts/ci-e2e-wine.sh" in E2E
    paths = re.findall(r"path: (\S+)", E2E)
    assert paths == ["logs/e2e-wine/"]
    assert "actions/cache" not in E2E


def test_upstream_watch_flow() -> None:
    assert "schedule:" in UPSTREAM
    order = [
        "scripts/check-upstream-fork.py --github-output",
        "https://cdn.fork.dev/win/Fork-${FORK_VERSION}.exe",
        "./scripts/ci-e2e-wine.sh",
        "peter-evans/create-pull-request@",
        "gh issue create",
    ]
    positions = [UPSTREAM.index(item) for item in order]
    assert positions == sorted(positions)
    assert "fails under Wine" in UPSTREAM
    assert "actions/cache" not in UPSTREAM
    assert "upload-artifact" in UPSTREAM
    assert "path: logs/e2e-wine/" in UPSTREAM


@pytest.mark.parametrize("name", sorted(ALL))
def test_no_floating_refs_or_suppressions(name: str) -> None:
    text = ALL[name]
    assert "@main" not in text
    assert "@master" not in text
    assert ":latest" not in text
    assert "-ignore" not in text


def test_actionlint_clean() -> None:
    if shutil.which("actionlint") is None:
        pytest.skip("actionlint is not installed (the lint stage runs it)")
    subprocess.run(["actionlint"], cwd=ROOT, check=True)


def test_local_runners_mirror_the_matrices() -> None:
    matrix = (ROOT / "scripts" / "ci-matrix.sh").read_text(encoding="utf-8")
    assert f"CELLS=({' '.join(COMPAT_CELLS)})" in matrix
    packaging = (ROOT / "scripts" / "ci-packaging-matrix.sh").read_text(encoding="utf-8")
    assert f"FORMATS=({' '.join(FORMATS)})" in packaging
    pipeline = (ROOT / "scripts" / "ci-pipeline.sh").read_text(encoding="utf-8")
    stages = re.findall(r"^stage (\w+)", pipeline, re.MULTILINE)
    assert stages == ["lint", "coverage", "bridge", "matrix", "packaging"]
    assert "set -euo pipefail" in pipeline
    assert "tee" in pipeline


# --------------------------------------------------------------------- release-verify-tag-version.sh


def _verify(tmp_path: Path, tag: str, *, notes: bool = True, changelog: str | None = None) -> int:
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "debian").mkdir()
    for name in ("release-verify-tag-version.sh", "read-version.py"):
        shutil.copy2(ROOT / "scripts" / name, repo / "scripts" / name)
    (repo / "VERSION").write_text(f"{VERSION}\n", encoding="utf-8")
    top = changelog or VERSION
    (repo / "debian" / "changelog").write_text(f"fork-linux ({top}) resolute; urgency=medium\n", encoding="utf-8")
    if notes:
        (repo / "docs" / "releases").mkdir(parents=True)
        (repo / "docs" / "releases" / f"v{VERSION}.md").write_text("notes\n", encoding="utf-8")
        (repo / "docs" / "releases" / f"v{VERSION}_github_description.md").write_text("body\n", encoding="utf-8")
    result = subprocess.run(
        ["bash", str(repo / "scripts" / "release-verify-tag-version.sh"), tag],
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": "/usr/bin:/bin"},
    )
    return result.returncode


def test_release_gate_accepts_the_current_version(tmp_path: Path) -> None:
    assert _verify(tmp_path, f"v{VERSION}") == 0


@pytest.mark.parametrize("tag", ["v0.0.1", VERSION, f"v{VERSION}-rc1", ""])
def test_release_gate_rejects_bad_tags(tmp_path: Path, tag: str) -> None:
    assert _verify(tmp_path, tag) != 0


def test_release_gate_rejects_missing_notes(tmp_path: Path) -> None:
    assert _verify(tmp_path, f"v{VERSION}", notes=False) != 0


def test_release_gate_rejects_a_stale_changelog(tmp_path: Path) -> None:
    assert _verify(tmp_path, f"v{VERSION}", changelog="0.0.1") != 0


def test_release_gate_passes_on_this_repository() -> None:
    subprocess.run(["bash", str(ROOT / "scripts" / "release-verify-tag-version.sh"), f"v{VERSION}"], check=True)


# --------------------------------------------------------------------- check-upstream-fork.py


def _upstream() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_upstream_fork", ROOT / "scripts" / "check-upstream-fork.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upstream_status_with_the_fixture_feed() -> None:
    upstream = _upstream()
    manifest = upstream.load_manifest()
    feed = (ROOT / "tests" / "fixtures" / "releases.win.json").read_text(encoding="utf-8")
    result = upstream.status(feed, manifest)
    assert result["latest"] == manifest["fork"]["default"]
    assert result["new"] is False


def test_upstream_detects_a_newer_version() -> None:
    upstream = _upstream()
    manifest = upstream.load_manifest()
    manifest["fork"]["versions"] = {"1.0.0": {}}
    manifest["fork"]["default"] = "1.0.0"
    feed = (ROOT / "tests" / "fixtures" / "releases.win.json").read_text(encoding="utf-8")
    assert upstream.status(feed, manifest)["new"] is True


def test_upstream_add_version_records_size_and_sha(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    upstream = _upstream()
    manifest = tmp_path / "manifest.json"
    shutil.copy2(upstream.MANIFEST, manifest)
    monkeypatch.setattr(upstream, "MANIFEST", manifest)
    monkeypatch.chdir(tmp_path)
    installer = tmp_path / "Fork.exe"
    installer.write_bytes(b"MZ" + b"\0" * 100)
    entry = upstream.add_version("9.9.9", installer, "ab" * 32)
    data = json.loads(manifest.read_text(encoding="utf-8"))
    assert data["fork"]["versions"]["9.9.9"] == entry
    assert entry["size"] == 102
    assert entry["status"] == "known-good"
    assert entry["full_nupkg_sha256"] == ("AB" * 32)


def test_upstream_add_version_rejects_bad_input(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    upstream = _upstream()
    manifest = tmp_path / "manifest.json"
    shutil.copy2(upstream.MANIFEST, manifest)
    monkeypatch.setattr(upstream, "MANIFEST", manifest)
    monkeypatch.chdir(tmp_path)
    installer = tmp_path / "Fork.exe"
    installer.write_bytes(b"MZ")
    with pytest.raises(SystemExit):
        upstream.add_version("../9", installer, None)
    installer.write_bytes(b"not a PE")
    with pytest.raises(SystemExit):
        upstream.add_version("9.9.8", installer, None)
    with pytest.raises(SystemExit):
        upstream.confined("/etc/passwd")


def test_upstream_cli_with_feed_file(tmp_path: Path) -> None:
    out = tmp_path / "gh-output"
    subprocess.run(
        [
            "python3",
            str(ROOT / "scripts" / "check-upstream-fork.py"),
            "--feed-file",
            str(ROOT / "tests" / "fixtures" / "releases.win.json"),
            "--github-output",
            str(out),
        ],
        check=True,
        capture_output=True,
    )
    assert out.read_text(encoding="utf-8").startswith("new=false\nversion=")
