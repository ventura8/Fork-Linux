#!/usr/bin/env python3
"""Fail if production code, tests or packaging contain linter/test suppressions.

Scans the Fork for Linux (unofficial) sources for NOLINT, NOSONAR, shellcheck
disable, noqa, type: ignore, "pragma: no cover", "#pragma GCC diagnostic
ignored", pytest skip/xfail markers and similar silence-the-tool patterns, and
for suppression *files* such as debian/*.lintian-overrides. Docs may mention
these tokens as forbidden; this lint only covers executable production, test,
build and packaging files (AGENTS.md §4.5).

Usage:
  python3 scripts/no-suppressions-lint.py
  python3 scripts/no-suppressions-lint.py /path/to/repo

Exit status: 0 clean, 1 findings ("path:line: [rule] hint"), 2 not a repo root.
"""

from __future__ import annotations

import os
import re
import stat
import sys
from pathlib import Path

# (rule_id, compiled pattern, human hint)
# Patterns match intentional suppressions, not prose in markdown/skills.
RULES: list[tuple[str, re.Pattern[str], str]] = [
    (
        "shellcheck-disable",
        re.compile(r"(?i)#\s*shellcheck\s+disable\b"),
        "# shellcheck disable is forbidden — fix the script",
    ),
    (
        "shellcheck-exclude",
        re.compile(r"(?i)\bshellcheck\b[^\n]*\s(?:-e(?=[\s=]|SC)|--exclude\b)"),
        "shellcheck -e/--exclude is forbidden — fix the script",
    ),
    (
        "shellcheck-opts",
        re.compile(r"\bSHELLCHECK_OPTS\b[^\n]*(?:-e(?=[\s=]|SC)|--exclude\b)"),
        "SHELLCHECK_OPTS exclusions are forbidden — fix the script",
    ),
    (
        "clang-tidy-nolint",
        re.compile(r"\bNOLINT\w*"),
        "NOLINT/NOLINTNEXTLINE/NOLINTBEGIN is forbidden — fix the C finding",
    ),
    (
        "c-pragma-diagnostic",
        re.compile(r"#\s*pragma\s+(?:GCC|clang)\s+diagnostic\s+ignored\b"),
        "#pragma GCC/clang diagnostic ignored is forbidden — fix the warning",
    ),
    (
        "c-pragma-warning",
        re.compile(r"#\s*pragma\s+warning\s*\(\s*disable\b"),
        "#pragma warning(disable) is forbidden — fix the warning",
    ),
    (
        "c-no-sanitize",
        re.compile(r"\bno_sanitize\w*"),
        "no_sanitize attributes are forbidden — fix the sanitizer finding",
    ),
    (
        "compiler-wno-error",
        re.compile(r"-Wno-error\b"),
        "-Wno-error is forbidden — fix the warning",
    ),
    (
        "sonar-nosonar",
        re.compile(r"(?i)\bNOSONAR\b"),
        "NOSONAR is forbidden — fix the Sonar finding",
    ),
    (
        "coverage-pragma",
        # Same shape as coverage.py's default exclusion regexes: the colon is optional.
        re.compile(r"(?i)#\s*pragma[:\s]?\s*no\s*(?:cover|branch)\b"),
        "# pragma: no cover/no branch is forbidden — test the code or delete it",
    ),
    (
        "coverage-exclude",
        re.compile(
            r"^\s*(?:exclude_lines|exclude_also|partial_branches|partial_also|omit)\s*="
        ),
        "coverage exclusion settings are forbidden — test the code or delete it",
    ),
    (
        "python-noqa",
        re.compile(r"(?i)#\s*noqa\b"),
        "# noqa is forbidden — fix the Python finding",
    ),
    (
        "python-type-ignore",
        re.compile(r"(?i)#\s*type:\s*ignore\b"),
        "# type: ignore is forbidden — fix types or imports",
    ),
    (
        "python-pyright-ignore",
        re.compile(r"(?i)#\s*pyright:\s*(?:ignore|basic)\b"),
        "# pyright: ignore is forbidden",
    ),
    (
        "python-mypy-ignore",
        re.compile(r"(?i)#\s*mypy:\s*(?:ignore|disable)"),
        "# mypy: ignore is forbidden",
    ),
    (
        "python-pylint-disable",
        re.compile(r"(?i)#\s*pylint:\s*(?:disable|skip-file)\b"),
        "# pylint: disable is forbidden",
    ),
    (
        "python-ruff-ignore",
        re.compile(r"(?i)#\s*ruff:\s*(?:noqa|ignore)\b"),
        "# ruff: noqa/ignore is forbidden",
    ),
    (
        "python-flake8-ignore",
        re.compile(r"(?i)#\s*flake8:\s*noqa\b"),
        "# flake8: noqa is forbidden",
    ),
    (
        "python-nosec",
        re.compile(r"(?i)#\s*nosec\b"),
        "# nosec is forbidden — fix the security finding",
    ),
    (
        "yamllint-disable",
        re.compile(r"(?i)#\s*yamllint\s+disable\b"),
        "# yamllint disable is forbidden — fix the YAML",
    ),
    (
        "pytest-mark-skip",
        # Also `from pytest import mark` + `@mark.skip`.
        re.compile(r"\bmark\.skip(?:if)?\b"),
        "pytest.mark.skip(if) is forbidden — fix the test or use a runtime pytest.skip()",
    ),
    (
        "pytest-mark-xfail",
        re.compile(r"\bmark\.xfail\b|\bpytest\.xfail\s*\("),
        "pytest xfail is forbidden — fix the failure",
    ),
    (
        "unittest-skip",
        re.compile(r"\bunittest\.(?:skip|skipIf|skipUnless|expectedFailure)\b"),
        "unittest skip/expectedFailure is forbidden — tests are pytest only",
    ),
    (
        "python-tool-config-ignore",
        # ruff / flake8 per-file or extended ignore lists in pyproject.toml / setup.cfg / tox.ini.
        re.compile(r"(?i)\b(?:extend-)?per-file-ignores\b|^\s*extend-ignore\s*="),
        "per-file-ignores / extend-ignore lint config is forbidden — fix the findings",
    ),
    (
        "actionlint-ignore",
        re.compile(r"\bactionlint\b[^\n]*\s-ignore\b"),
        "actionlint -ignore is forbidden — fix the workflow",
    ),
    (
        "rpmlint-filter",
        # Bare rpmlintrc call at line start; never logging's handler.addFilter(...).
        re.compile(r"^\s*addFilter\s*\("),
        "rpmlint addFilter() is forbidden — fix the spec",
    ),
]

# Suppression *files*: their mere existence silences a tool.
# (rule_id, filename predicate, human hint)
FILE_RULES: list[tuple[str, re.Pattern[str], str]] = [
    (
        "lintian-overrides",
        re.compile(r"(?:^|\.)lintian-overrides$"),
        "lintian override files are forbidden — fix the packaging",
    ),
    (
        "rpmlint-config",
        re.compile(r"(?:^|\.)rpmlintrc$"),
        "rpmlintrc filter files are forbidden — fix the spec",
    ),
]

# An optional YAML list dash, then the key (one optional dash keeps the match linear).
_ACTIONLINT_RULES: list[tuple[str, re.Pattern[str], str]] = [
    (
        "actionlint-config-ignore",
        re.compile(r"^\s*(?:-\s*)?ignore\s*:"),
        "actionlint config ignore: lists are forbidden — fix the workflow",
    ),
]

# Extra content rules for tool config files, keyed by file name: their keys are generic
# words (`disable=`, `ignore:`) that must only be flagged inside that tool's config.
NAME_RULES: dict[str, list[tuple[str, re.Pattern[str], str]]] = {
    ".shellcheckrc": [
        (
            "shellcheckrc-disable",
            re.compile(r"^\s*disable\s*="),
            ".shellcheckrc disable= is forbidden — fix the script",
        ),
    ],
    "actionlint.yaml": _ACTIONLINT_RULES,
    "actionlint.yml": _ACTIONLINT_RULES,
}

CODE_SUFFIXES = frozenset(
    {
        ".py",
        ".sh",
        ".bash",
        ".zsh",
        ".fish",
        ".c",
        ".h",
        ".cc",
        ".cpp",
        ".cxx",
        ".hh",
        ".hpp",
        ".rc",
        ".in",
        ".spec",
        ".yml",
        ".yaml",
        ".toml",
        ".cfg",
        ".ini",
        ".build",
        ".options",
        ".mk",
    }
)

# Extension-less build/packaging entrypoints scanned by name.
NAMED_FILES = frozenset({"PKGBUILD", "AppRun", "Makefile", "configure", "rules"})

# Top-level trees that hold production, test, build and packaging files.
SCAN_ROOTS = (
    "src",
    "tests",
    "scripts",
    "bridge",
    "packaging",
    "docker",
    "libexec",
    "bin",
    "data",
    "debian",
    ".github/workflows",
)

# Repo-root entrypoints and tool configs (repo-relative paths).
ROOT_FILES = (
    "install.sh",
    "uninstall.sh",
    "meson.build",
    "meson.options",
    "pyproject.toml",
    "setup.cfg",
    "tox.ini",
    "pytest.ini",
    ".coveragerc",
    ".shellcheckrc",
    ".github/actionlint.yaml",
    ".github/actionlint.yml",
)

# Directory names skipped anywhere (caches, VCS, tool output). Mirrors the unanchored
# .gitignore rules: a directory git would commit is never skipped.
SKIP_DIR_NAMES = frozenset(
    {
        ".git",
        ".cache",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".tox",
        ".venv",
        "venv",
        "__pycache__",
        "node_modules",
        ".flatpak-builder",
        ".scannerwork",
        "squashfs-root",
    }
)

# Directory-name prefixes skipped anywhere (build-*/, builddir*/, obj-*/, Wine prefixes).
# Deliberately not a bare "build": a nested build/ directory is source (.gitignore only
# hides the root /build/).
SKIP_DIR_PREFIXES = ("build-", "builddir", "obj-", ".wine", "wineprefix")

# Tool output trees skipped only under one parent ("" = the repo root); the names are
# legitimate elsewhere (e.g. src/ is a scan root, tests/logs/ would be source).
SCOPED_SKIP_DIRS: dict[str, frozenset[str]] = {
    "": frozenset({"build", "artifacts", "logs", ".e2e-home"}),
    "packaging/snap": frozenset({"parts", "stage", "prime", "overlay", ".craft"}),
    "packaging/arch": frozenset({"src", "pkg"}),
    "debian": frozenset({"tmp", ".debhelper", "fork-linux", "files"}),
}

# debian/ top-level files whose content is scanned by suffix (extension-less files such as
# rules are scanned by name, exec bit or shebang; control / changelog are data).
DEBIAN_SUFFIXES = frozenset({".sh", ".preinst", ".postinst", ".prerm", ".postrm"})

# The two files that document the forbidden tokens on purpose (exact repo paths only, so
# a copy elsewhere is still linted).
SELF_FILES = frozenset({"scripts/no-suppressions-lint.py", "tests/test_no_suppressions_lint.py"})

# Bytes inspected to tell binary files (NUL bytes) from text.
_BINARY_PROBE = 8192


def _skip_dir(parent: str, name: str) -> bool:
    """True if directory ``name`` inside repo-relative ``parent`` must not be walked."""
    if name in SKIP_DIR_NAMES or name.startswith(SKIP_DIR_PREFIXES):
        return True
    return name in SCOPED_SKIP_DIRS.get(parent, frozenset())


def skip_path(path: Path) -> bool:
    """True if repo-relative ``path`` is in a skipped tree or is this linter / its test."""
    parts = path.parts
    for index, part in enumerate(parts[:-1]):
        if _skip_dir("/".join(parts[:index]), part):
            return True
    return path.as_posix() in SELF_FILES


def _rel(path: Path, repo: Path) -> Path | None:
    """Return where ``path`` lives relative to ``repo`` (its own symlink is not followed).

    Only the parent directory is resolved, so a repo reached through a symlinked path
    still matches, while a symlinked file is judged by its own location.
    """
    absolute = os.path.abspath(path)
    location = Path(os.path.realpath(os.path.dirname(absolute))) / os.path.basename(absolute)
    try:
        return location.relative_to(os.path.realpath(repo))
    except ValueError:
        return None


def _inside(path: Path, repo: Path) -> bool:
    """True if ``path`` (symlinks followed) stays inside ``repo``."""
    try:
        path.resolve().relative_to(repo.resolve())
    except (ValueError, OSError, RuntimeError):
        # RuntimeError: symlink loop on Python 3.10-3.12.
        return False
    return True


def _in_scan_root(rel: Path) -> bool:
    posix = rel.as_posix()
    return any(posix.startswith(root + "/") for root in SCAN_ROOTS)


def _has_shebang(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return handle.read(2) == b"#!"
    except OSError:
        return False


def is_scanned_file(path: Path, repo: Path) -> bool:
    """True if ``path`` holds code/config whose content must be lint-checked.

    Selection uses the path inside the repo (a symlink is judged by where it lives, so
    linking a file into src/ from an unscanned tree does not hide it); the target must
    be a regular file inside the repo.
    """
    rel = _rel(path, repo)
    if rel is None or not rel.parts or skip_path(rel) or not _inside(path, repo):
        return False
    try:
        mode = path.stat().st_mode
    except OSError:
        # Dangling symlink, or removed since the walk.
        return False
    if not stat.S_ISREG(mode):
        # Directories, FIFOs (opening one would block), devices, sockets.
        return False
    if rel.as_posix() in ROOT_FILES:
        return True
    if not _in_scan_root(rel):
        return False
    debian = _debian_choice(path, rel)
    if debian is not None:
        return debian
    return _scanned_kind(path, rel, mode)


def _debian_choice(path: Path, rel: Path) -> bool | None:
    """Whether a debian/ entry is scanned; None when that depends on its kind (or it is elsewhere).

    Only debian/ top-level scripts (rules, *.sh, maintainer scripts); never dh build trees
    or data files such as control / changelog.
    """
    if rel.parts[0] != "debian":
        return None
    if len(rel.parts) != 2 or (path.suffix and path.suffix not in DEBIAN_SUFFIXES):
        return False
    if path.suffix:
        return True
    return None


def _scanned_kind(path: Path, rel: Path, mode: int) -> bool:
    """True for code by suffix or name, Dockerfiles, completions, and executable or shebang files."""
    name = path.name
    if path.suffix.lower() in CODE_SUFFIXES or name in NAMED_FILES or name in NAME_RULES:
        return True
    if name.startswith("Dockerfile") or rel.as_posix().startswith("data/completions/"):
        return True
    if path.suffix:
        return False
    return bool(mode & 0o111) or _has_shebang(path)


def forbidden_file_errors(path: Path, display: str) -> list[str]:
    """Return findings for suppression files (e.g. *.lintian-overrides) by name."""
    return [
        f"{display}:1: [{rule_id}] {hint}"
        for rule_id, pattern, hint in FILE_RULES
        if pattern.search(path.name)
    ]


def _walk_onerror(err: OSError) -> None:
    print(f"no-suppressions-lint: warning: cannot walk path: {err}", file=sys.stderr)


def _walk_root(repo: Path, root: Path) -> list[Path]:
    """Every non-skipped file under the real directory ``root``, plus its symlinked subdirectories."""
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root, onerror=_walk_onerror):
        parent = Path(dirpath).relative_to(repo).as_posix()
        dirnames[:] = sorted(d for d in dirnames if not _skip_dir(parent, d))
        names = list(filenames)
        names.extend(d for d in dirnames if (Path(dirpath) / d).is_symlink())
        for name in names:
            path = Path(dirpath) / name
            if not skip_path(path.relative_to(repo)):
                files.append(path)
    return files


def iter_tree_files(repo: Path) -> list[Path]:
    """Return every non-skipped entry under the scan roots plus the root entrypoints.

    Symlinked directories are returned as entries (os.walk does not descend into them)
    so :func:`lint_tree` can report them instead of silently skipping their contents.
    """
    files: list[Path] = []
    for root_name in SCAN_ROOTS:
        root = repo / root_name
        if root.is_symlink():
            files.append(root)
        elif root.is_dir():
            files.extend(_walk_root(repo, root))
    for name in ROOT_FILES:
        path = repo / name
        if path.is_file():
            files.append(path)
    return sorted(files)


def iter_scan_files(repo: Path) -> list[Path]:
    """Return the files whose content is lint-checked."""
    return [path for path in iter_tree_files(repo) if is_scanned_file(path, repo)]


def _read_text(path: Path) -> str | None:
    """Return the file's text (undecodable bytes replaced), or None for binary files."""
    data = path.read_bytes()
    if b"\0" in data[:_BINARY_PROBE]:
        return None
    # errors="replace": a stray non-UTF-8 byte must not hide a suppression on another line.
    return data.decode("utf-8", errors="replace")


def lint_file(path: Path, display: str | None = None) -> list[str]:
    """Return "path:line: [rule] hint" findings for one file's content."""
    shown = display if display is not None else str(path)
    try:
        text = _read_text(path)
    except FileNotFoundError:
        # Removed since the walk.
        return []
    except OSError as err:
        return [f"{shown}:0: [unreadable] cannot read: {err}"]
    if text is None:
        return []
    rules = RULES + NAME_RULES.get(path.name, [])
    errors: list[str] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for rule_id, pattern, hint in rules:
            if pattern.search(line):
                errors.append(f"{shown}:{lineno}: [{rule_id}] {hint}")
    return errors


def lint_tree(repo: Path) -> list[str]:
    """Lint every scanned file and suppression file under ``repo``."""
    errors: list[str] = []
    for path in iter_tree_files(repo):
        display = path.relative_to(repo).as_posix()
        if path.is_symlink() and path.is_dir():
            errors.append(
                f"{display}:0: [symlinked-dir] symlinked directories are not linted — "
                "use a real directory"
            )
            continue
        errors.extend(forbidden_file_errors(path, display))
        if is_scanned_file(path, repo):
            errors.extend(lint_file(path, display))
    return errors


def main(argv: list[str]) -> int:
    """CLI entry point; ``argv[1]`` is the optional repo root (default: cwd)."""
    repo = Path(argv[1] if len(argv) > 1 else ".").resolve()
    if not (repo / "AGENTS.md").is_file():
        print(
            f"error: not a Fork for Linux repo root (no AGENTS.md): {repo}",
            file=sys.stderr,
        )
        return 2
    errors = lint_tree(repo)
    if errors:
        print("no-suppressions-lint: FORBIDDEN suppressions found:", file=sys.stderr)
        for err in errors:
            print(err, file=sys.stderr)
        print(
            f"no-suppressions-lint: {len(errors)} finding(s). "
            "Fix the code; do not silence linters/tests.",
            file=sys.stderr,
        )
        return 1
    print(
        "no-suppressions-lint: OK (no NOLINT / NOSONAR / shellcheck disable / noqa / "
        "type: ignore / pragma: no cover / skip / xfail / lintian overrides)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
