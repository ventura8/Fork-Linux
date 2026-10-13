"""Tests for scripts/no-suppressions-lint.py (loaded via importlib: the name is hyphenated)."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "no-suppressions-lint.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("no_suppressions_lint", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


nsl = _load()

# Sample lines are assembled from fragments so this file never reads as a suppression
# to other tools (it is also skipped by the linter itself).
NQ = "no" + "qa"
TI = "type" + ": ignore"
NL = "NO" + "LINT"
NS = "NO" + "SONAR"
SC = "shell" + "check"
PNC = "pragma" + ": no cover"


def _rules(errors: list[str]) -> set[str]:
    return {err.split("[", 1)[1].split("]", 1)[0] for err in errors}


def _write(path: Path, body: str, mode: int | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    if mode is not None:
        path.chmod(mode)
    return path


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A minimal clean repo root (AGENTS.md present)."""
    root = tmp_path / "repo"
    _write(root / "AGENTS.md", "# rules\nNever use # " + NQ + "\n")
    _write(root / "src" / "fork_linux" / "__init__.py", '"""pkg."""\n')
    return root


# --- rules ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("line", "rule"),
    [
        ("x = 1  # " + NQ + ": E501", "python-noqa"),
        ("from x import y  # " + TI, "python-type-ignore"),
        ("y = 2  # pyright: ignore[reportX]", "python-pyright-ignore"),
        ("# mypy: ignore-errors", "python-mypy-ignore"),
        ("# pylint: disable=all", "python-pylint-disable"),
        ("# pylint: skip-file", "python-pylint-disable"),
        ("# ruff: " + NQ, "python-ruff-ignore"),
        ("# flake8: " + NQ, "python-flake8-ignore"),
        ("run(cmd)  # nosec B603", "python-nosec"),
        ("if x:  # " + PNC, "coverage-pragma"),
        ("else:  # pragma: no branch", "coverage-pragma"),
        # coverage.py's default regexes accept these too (colon optional, any case).
        ("if x:  # pragma no cover", "coverage-pragma"),
        ("if x:  #pragma nocover", "coverage-pragma"),
        ("if x:  # PRAGMA NO BRANCH", "coverage-pragma"),
        ('exclude_lines = ["raise"]', "coverage-exclude"),
        ("exclude_also = [", "coverage-exclude"),
        ('omit = ["src/fork_linux/ui.py"]', "coverage-exclude"),
        ("v = 1  # " + NS, "sonar-nosonar"),
        ("// " + NL + "NEXTLINE(readability-magic-numbers)", "clang-tidy-nolint"),
        ("int x; // " + NL, "clang-tidy-nolint"),
        ("/* " + NL + "BEGIN */", "clang-tidy-nolint"),
        ('#pragma GCC diagnostic ignored "-Wformat"', "c-pragma-diagnostic"),
        ('#  pragma clang diagnostic ignored "-Wall"', "c-pragma-diagnostic"),
        ("#pragma warning(disable: 4996)", "c-pragma-warning"),
        ("__attribute__((no_sanitize(\"address\")))", "c-no-sanitize"),
        ("cflags += -Wno-error=format", "compiler-wno-error"),
        ("# " + SC + " disable=SC2086", "shellcheck-disable"),
        ("#" + SC + " disable=SC1090", "shellcheck-disable"),
        (SC + " -x -e SC2086 install.sh", "shellcheck-exclude"),
        (SC + " --exclude=SC2086 x.sh", "shellcheck-exclude"),
        (SC + " -eSC2086 x.sh", "shellcheck-exclude"),
        ("export SHELLCHECK_OPTS='-e SC2086'", "shellcheck-opts"),
        ("# yamllint disable rule:line-length", "yamllint-disable"),
        ("@pytest.mark.skip(reason='no')", "pytest-mark-skip"),
        ("@pytest.mark.skipif(True, reason='no')", "pytest-mark-skip"),
        ("pytestmark = pytest.mark.skip", "pytest-mark-skip"),
        ("@mark.skip", "pytest-mark-skip"),
        ("@mark.skipif(sys.platform == 'x', reason='no')", "pytest-mark-skip"),
        ("@pytest.mark.xfail", "pytest-mark-xfail"),
        ("@mark.xfail(strict=True)", "pytest-mark-xfail"),
        ("    pytest.xfail('later')", "pytest-mark-xfail"),
        ("@unittest.skipIf(True, 'x')", "unittest-skip"),
        ('addFilter("no-documentation")', "rpmlint-filter"),
        ("[tool.ruff.lint.per-file-ignores]", "python-tool-config-ignore"),
        ("per-file-ignores = tests/*:S101", "python-tool-config-ignore"),
        ('extend-per-file-ignores = {"x.py" = ["E1"]}', "python-tool-config-ignore"),
        ("extend-ignore = E501", "python-tool-config-ignore"),
        ("actionlint -ignore 'SC2086' .github/workflows/check.yml", "actionlint-ignore"),
    ],
)
def test_rule_flags_suppression(tmp_path: Path, line: str, rule: str) -> None:
    path = _write(tmp_path / "sample.txt", f"ok = 1\n{line}\n")
    errors = nsl.lint_file(path)
    assert rule in _rules(errors), errors
    assert all(err.startswith(f"{path}:2: [") for err in errors)


@pytest.mark.parametrize(
    "line",
    [
        'pytest.skip("tool missing")',
        "pytest.importorskip('x')",
        SC + " -x scripts/*.sh",
        SC + " --severity=style --enable=all x.sh",
        "grep -e foo file",
        "for root, dirs, files in os.walk(top):",
        "-fno-sanitize-recover=all",
        "-Werror -Wall -Wextra",
        "# This is a normal comment about type hints",
        "exclusions = 3",
        "handler.addFilter(RedactingFilter())",
        "logger.addFilter (f)",
        "omitted = 1",
        "bookmark.skip()",
        "disable = true",
        "  ignore: generic YAML key outside actionlint config",
        "actionlint -color .github/workflows/check.yml",
    ],
)
def test_rule_allows_legitimate_code(tmp_path: Path, line: str) -> None:
    path = _write(tmp_path / "ok.txt", line + "\n")
    assert nsl.lint_file(path) == []


def test_lint_file_display_name(tmp_path: Path) -> None:
    path = _write(tmp_path / "a.py", "x = 1  # " + NQ + "\n")
    assert nsl.lint_file(path, "src/a.py") == [
        "src/a.py:1: [python-noqa] # noqa is forbidden — fix the Python finding"
    ]


def test_lint_file_skips_binary_and_missing(tmp_path: Path) -> None:
    binary = tmp_path / "blob.py"
    binary.write_bytes(b"\xff\xfe\x00" + ("# " + NQ).encode())
    assert nsl.lint_file(binary) == []
    assert nsl.lint_file(tmp_path / "gone.py") == []


def test_lint_file_flags_suppressions_in_non_utf8_text(tmp_path: Path) -> None:
    """A stray latin-1 byte must not hide a suppression on another line."""
    path = tmp_path / "x.sh"
    path.write_bytes(b"echo caf\xe9\n# " + SC.encode() + b" disable=SC2086\n")
    assert _rules(nsl.lint_file(path)) == {"shellcheck-disable"}


@pytest.mark.parametrize(
    ("name", "body", "rule"),
    [
        (".shellcheckrc", "external-sources=true\ndisable=SC2086\n", "shellcheckrc-disable"),
        (
            "actionlint.yaml",
            "paths:\n  .github/workflows/check.yml:\n    ignore:\n      - 'x'\n",
            "actionlint-config-ignore",
        ),
        ("actionlint.yml", "paths:\n  '**':\n    - ignore: x\n", "actionlint-config-ignore"),
    ],
)
def test_name_rules_flag_tool_config(tmp_path: Path, name: str, body: str, rule: str) -> None:
    path = _write(tmp_path / name, body)
    assert _rules(nsl.lint_file(path)) == {rule}


@pytest.mark.parametrize(
    ("name", "body"),
    [
        (".shellcheckrc", "external-sources=true\nsource-path=SCRIPTDIR\n"),
        ("actionlint.yaml", "self-hosted-runner:\n  labels:\n    - ubuntu-26.04\n"),
        ("other.yaml", "disable=1\nignore: x\n"),
    ],
)
def test_name_rules_allow_clean_config(tmp_path: Path, name: str, body: str) -> None:
    assert nsl.lint_file(_write(tmp_path / name, body)) == []


def test_lint_file_reports_unreadable(tmp_path: Path) -> None:
    directory = tmp_path / "dir.py"
    directory.mkdir()
    errors = nsl.lint_file(directory, "dir.py")
    assert len(errors) == 1
    assert errors[0].startswith("dir.py:0: [unreadable] cannot read:")


@pytest.mark.parametrize(
    ("name", "rule"),
    [
        ("fork-linux.lintian-overrides", "lintian-overrides"),
        ("lintian-overrides", "lintian-overrides"),
        ("fork-linux.rpmlintrc", "rpmlint-config"),
        ("rpmlintrc", "rpmlint-config"),
    ],
)
def test_forbidden_file_names(name: str, rule: str) -> None:
    errors = nsl.forbidden_file_errors(Path("debian") / name, f"debian/{name}")
    assert errors == [f"debian/{name}:1: [{rule}] " + dict((r, h) for r, _, h in nsl.FILE_RULES)[rule]]


def test_forbidden_file_names_ignore_normal_files() -> None:
    assert nsl.forbidden_file_errors(Path("debian/control"), "debian/control") == []
    assert nsl.forbidden_file_errors(Path("docs/lintian-overrides.md"), "x") == []


# --- path selection --------------------------------------------------------------


@pytest.mark.parametrize(
    "rel",
    [
        "build-ci-lint/foo.py",
        "build/foo.py",
        "builddir-x/foo.py",
        "bridge/build-mingw/x.c",
        "obj-x86_64-linux-gnu/x.c",
        ".cache/x.py",
        "tests/__pycache__/x.py",
        "artifacts/x.sh",
        "logs/ci/x.sh",
        ".e2e-home/x.sh",
        "tests/.wine/drive_c/x.py",
        "tests/wineprefix-a/x.py",
        "packaging/snap/parts/fork-linux/x.py",
        "packaging/snap/stage/bin/x.sh",
        "packaging/arch/src/x.sh",
        "packaging/arch/pkg/x.sh",
        "debian/tmp/usr/bin/x.sh",
        "debian/fork-linux/usr/x.py",
        "debian/.debhelper/x.sh",
        "scripts/no-suppressions-lint.py",
        "tests/test_no_suppressions_lint.py",
    ],
)
def test_skip_path_true(rel: str) -> None:
    assert nsl.skip_path(Path(rel))


@pytest.mark.parametrize(
    "rel",
    [
        "src/fork_linux/cli.py",
        "scripts/build-bridge.sh",
        "src/fork_linux/steps/stage.py",
        "packaging/arch/PKGBUILD.in",
        "packaging/src/x.sh",
        "tests/parts/x.py",
        "debian/rules",
        # A nested build/, logs/ or artifacts/ is source (.gitignore only hides the root ones).
        "src/fork_linux/build/x.py",
        "src/fork_linux/builders/x.py",
        "tests/logs/x.py",
        "src/artifacts/x.py",
        # Only the linter's own two files are exempt, not copies elsewhere.
        "tests/sub/test_no_suppressions_lint.py",
        "src/fork_linux/no-suppressions-lint.py",
        "",
    ],
)
def test_skip_path_false(rel: str) -> None:
    assert not nsl.skip_path(Path(rel))


@pytest.mark.parametrize(
    "rel",
    [
        "install.sh",
        "uninstall.sh",
        "meson.build",
        "pyproject.toml",
        "src/fork_linux/cli.py",
        "src/fork_linux/data/defaults.ini",
        "src/fork_linux/data/templates/fork.desktop.in",
        "bin/fork-linux.in",
        "libexec/fork-linux-host.in",
        "bridge/common/translate.c",
        "bridge/common/translate.h",
        "bridge/win/res/fl-shim.rc",
        "data/completions/fork-linux",
        "data/completions/_fork-linux",
        "data/completions/fork-linux.fish",
        "packaging/arch/PKGBUILD",
        "packaging/appimage/AppRun",
        "packaging/rpm/fedora/fork-linux.spec",
        "packaging/snap/snapcraft.yaml",
        "packaging/flatpak/io.github.ventura8.ForkLinux.yml",
        "docker/Dockerfile.ci.jammy",
        "docker/Dockerfile.ci",
        "debian/rules",
        "debian/fork-linux.postinst",
        "scripts/read-version.py",
        "tests/test_x.py",
        ".github/workflows/check.yml",
        ".github/actionlint.yaml",
        ".shellcheckrc",
        "tests/.shellcheckrc",
        "src/fork_linux/build/x.py",
    ],
)
def test_is_scanned_file_true(repo: Path, rel: str) -> None:
    path = _write(repo / rel, "x\n")
    assert nsl.is_scanned_file(path, repo)


@pytest.mark.parametrize(
    "rel",
    [
        "README.md",
        "AGENTS.md",
        "docs/x.py",
        ".github/pull_request_template.md",
        ".github/ISSUE_TEMPLATE/bug_report.yml",
        "src/fork_linux/data/runtime-manifest.json",
        "data/io.github.ventura8.ForkLinux.metainfo.xml",
        "data/man/fork-linux.1",
        "data/icons/hicolor/scalable/apps/x.svg",
        "debian/control",
        "debian/changelog",
        "debian/source/options",
        "debian/source/x.sh",
        "build-ci/x.py",
        "scripts/no-suppressions-lint.py",
        "tests/fixtures/sample.reg",
        "tests/fixtures/data-file",
    ],
)
def test_is_scanned_file_false(repo: Path, rel: str) -> None:
    path = _write(repo / rel, "x\n")
    assert not nsl.is_scanned_file(path, repo)


def test_is_scanned_file_extensionless_exec_and_shebang(repo: Path) -> None:
    exe = _write(repo / "scripts" / "tool", "echo hi\n", 0o755)
    shebang = _write(repo / "tests" / "fakes" / "bin" / "wine", "#!/bin/sh\necho\n", 0o644)
    plain = _write(repo / "tests" / "fakes" / "bin" / "notes", "text\n", 0o644)
    assert nsl.is_scanned_file(exe, repo)
    assert nsl.is_scanned_file(shebang, repo)
    assert not nsl.is_scanned_file(plain, repo)


def test_is_scanned_file_outside_repo_and_dangling(repo: Path, tmp_path: Path) -> None:
    outside = _write(tmp_path / "elsewhere" / "x.py", "x\n")
    assert not nsl.is_scanned_file(outside, repo)
    dangling = repo / "bin" / "fork"
    dangling.parent.mkdir(parents=True, exist_ok=True)
    dangling.symlink_to(repo / "bin" / "missing-target")
    assert not nsl.is_scanned_file(dangling, repo)


def test_is_scanned_file_judges_symlinks_by_their_location(repo: Path) -> None:
    """Linking an unscanned file into src/ must not hide it; links out of the repo are not read."""
    _write(repo / "docs" / "evil.py", "x = 1  # " + NQ + "\n")
    inside = repo / "src" / "fork_linux" / "evil.py"
    inside.symlink_to(repo / "docs" / "evil.py")
    assert nsl.is_scanned_file(inside, repo)
    assert "src/fork_linux/evil.py:1: [python-noqa]" in "\n".join(nsl.lint_tree(repo))
    outside_target = _write(repo.parent / "outside.py", "x\n")
    outside = repo / "src" / "fork_linux" / "outside.py"
    outside.symlink_to(outside_target)
    assert not nsl.is_scanned_file(outside, repo)


def test_is_scanned_file_symlink_loop(repo: Path) -> None:
    loop = repo / "bin" / "loop"
    loop.parent.mkdir(parents=True)
    loop.symlink_to(loop)
    assert not nsl.is_scanned_file(loop, repo)


def test_is_scanned_file_through_symlinked_repo_path(repo: Path, tmp_path: Path) -> None:
    alias = tmp_path / "alias"
    alias.symlink_to(repo, target_is_directory=True)
    assert nsl.is_scanned_file(repo / "src" / "fork_linux" / "__init__.py", alias)
    assert nsl.is_scanned_file(alias / "src" / "fork_linux" / "__init__.py", repo)


def test_is_scanned_file_skips_fifo_without_blocking(repo: Path) -> None:
    fifo = repo / "tests" / "fakes" / "bin" / "pipe"
    fifo.parent.mkdir(parents=True)
    os.mkfifo(fifo)
    assert not nsl.is_scanned_file(fifo, repo)
    assert nsl.lint_tree(repo) == []


def test_lint_tree_reports_symlinked_dirs(repo: Path) -> None:
    _write(repo / "docs" / "pkg" / "bad.py", "# " + NQ + "\n")
    (repo / "src" / "fork_linux" / "pkg").symlink_to(repo / "docs" / "pkg", target_is_directory=True)
    (repo / "bin").symlink_to(repo / "docs" / "pkg", target_is_directory=True)
    errors = nsl.lint_tree(repo)
    assert [err.split(":", 1)[0] for err in errors] == ["bin", "src/fork_linux/pkg"]
    assert all("[symlinked-dir]" in err for err in errors)


def test_lint_tree_ignores_dangling_symlinked_scan_root(repo: Path) -> None:
    (repo / "libexec").symlink_to(repo / "missing", target_is_directory=True)
    assert nsl.lint_tree(repo) == []


def test_has_shebang_unreadable(tmp_path: Path) -> None:
    assert not nsl._has_shebang(tmp_path / "missing")


# --- tree walking + CLI -------------------------------------------------------------


def test_lint_tree_clean(repo: Path) -> None:
    _write(repo / "scripts" / "ok.sh", "#!/bin/sh\necho ok\n", 0o755)
    _write(repo / "install.sh", "#!/bin/sh\nmain() { :; }\nmain\n", 0o755)
    assert nsl.lint_tree(repo) == []


def test_lint_tree_finds_content_and_file_violations(repo: Path) -> None:
    _write(repo / "src" / "fork_linux" / "bad.py", "x = 1  # " + TI + "\n")
    _write(repo / "uninstall.sh", "# " + SC + " disable=SC2086\n")
    _write(repo / "debian" / "fork-linux.lintian-overrides", "fork-linux: some-tag\n")
    _write(repo / "debian" / "source" / "lintian-overrides", "x\n")
    _write(repo / "packaging" / "rpm" / "fedora" / "fork-linux.rpmlintrc", "addFilter('x')\n")
    # Skipped trees and docs are never reported.
    _write(repo / "build-ci" / "x.py", "# " + NQ + "\n")
    _write(repo / "debian" / "tmp" / "x.lintian-overrides", "x\n")
    _write(repo / "packaging" / "snap" / "parts" / "x.py", "# " + NQ + "\n")
    _write(repo / "docs" / "x.py", "# " + NQ + "\n")
    _write(repo / "src" / "README.md", "# " + NQ + "\n")
    errors = nsl.lint_tree(repo)
    assert errors == [
        "debian/fork-linux.lintian-overrides:1: [lintian-overrides] "
        "lintian override files are forbidden — fix the packaging",
        "debian/source/lintian-overrides:1: [lintian-overrides] "
        "lintian override files are forbidden — fix the packaging",
        "packaging/rpm/fedora/fork-linux.rpmlintrc:1: [rpmlint-config] "
        "rpmlintrc filter files are forbidden — fix the spec",
        "src/fork_linux/bad.py:1: [python-type-ignore] # type: ignore is forbidden — fix types or imports",
        "uninstall.sh:1: [shellcheck-disable] # shellcheck disable is forbidden — fix the script",
    ]


def test_iter_scan_files(repo: Path) -> None:
    _write(repo / "tests" / "test_a.py", "x\n")
    _write(repo / "tests" / "README.md", "x\n")
    _write(repo / "meson.build", "project('x')\n")
    rels = [p.relative_to(repo).as_posix() for p in nsl.iter_scan_files(repo)]
    assert rels == ["meson.build", "src/fork_linux/__init__.py", "tests/test_a.py"]


def test_walk_errors_are_reported(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    locked = repo / "tests" / "locked"
    _write(locked / "x.py", "x\n")
    locked.chmod(0)
    try:
        if os.access(locked, os.R_OK):
            pytest.skip("running as root: permissions are not enforced")
        assert nsl.lint_tree(repo) == []
    finally:
        locked.chmod(0o755)
    assert "cannot walk path" in capsys.readouterr().err


def test_main_refuses_without_agents_md(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert nsl.main(["lint", str(tmp_path)]) == 2
    assert "no AGENTS.md" in capsys.readouterr().err


def test_main_clean(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert nsl.main(["lint", str(repo)]) == 0
    assert capsys.readouterr().out.startswith("no-suppressions-lint: OK")


def test_main_default_repo_is_cwd(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(repo)
    assert nsl.main(["lint"]) == 0


def test_main_findings(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write(repo / "tests" / "test_bad.py", "import x\n@pytest.mark.xfail\ndef test_x():\n    pass\n")
    assert nsl.main(["lint", str(repo)]) == 1
    err = capsys.readouterr().err.splitlines()
    assert err[0] == "no-suppressions-lint: FORBIDDEN suppressions found:"
    assert err[1] == "tests/test_bad.py:2: [pytest-mark-xfail] pytest xfail is forbidden — fix the failure"
    assert err[2].startswith("no-suppressions-lint: 1 finding(s).")


def test_script_runs_as_program(repo: Path) -> None:
    _write(repo / "bridge" / "common" / "x.c", "int x; /* " + NL + " */\n")
    out = subprocess.run(
        [sys.executable, str(SCRIPT), str(repo)], capture_output=True, text=True, check=False
    )
    assert out.returncode == 1
    assert "bridge/common/x.c:1: [clang-tidy-nolint]" in out.stderr


def test_repo_has_no_suppressions() -> None:
    """Production, tests and packaging must stay clean (same gate as the CI lint stage)."""
    errors = nsl.lint_tree(ROOT)
    assert errors == [], "\n".join(errors)
