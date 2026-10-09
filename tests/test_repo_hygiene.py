"""Repository hygiene contract tests (AGENTS.md §1 HARD RULES, §4.7, §4.7.1, §5).

The file list comes from git (tracked + untracked-but-not-ignored), i.e. what a
commit could contain; build trees and other ignored output are never inspected.
"""

from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC_PKG = ROOT / "src" / "fork_linux"

FORBIDDEN_BINARY_SUFFIXES = frozenset({".exe", ".dll", ".msi", ".nupkg", ".ico"})
IMAGE_SUFFIXES = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".svg", ".svgz", ".webp",
     ".tif", ".tiff", ".xpm", ".icns", ".avif"}
)
IMAGE_ALLOWED_FILES = frozenset({".github/banner.png"})
IMAGE_ALLOWED_DIRS = ("docs/assets/", "docs/badges/", "data/icons/", "src/fork_linux/data/icons/")
IMAGE_MAGIC = (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a")

ALLOWED_ROOT_FILES = frozenset(
    {
        "VERSION", "meson.build", "meson.options", "meson_options.txt", "install.sh",
        "uninstall.sh", "pyproject.toml", ".clang-tidy", "sonar-project.properties", "AGENTS.md", "CLAUDE.md",
        "GEMINI.md", "agent.md", "skills.md", "README.md", "LICENSE", ".gitignore",
        ".dockerignore",
    }
)
ALLOWED_ROOT_DIRS = frozenset(
    {
        "src", "bin", "libexec", "bridge", "data", "packaging", "debian", "docker",
        "scripts", "tests", "docs", "logs", ".agents", ".github", ".cursor",
    }
)

# Python 3.11+ APIs (AGENTS.md §1 hard rule 15). Modules (dotted) and module attributes
# (dotted "module.attr"); the 3.10 CI run additionally catches new stdlib modules through
# sys.stdlib_module_names and new syntax through ast.parse.
PY311_MODULES = frozenset(
    {
        "tomllib",
        "wsgiref.types",
        "annotationlib",
        "compression",
        "string.templatelib",
        "concurrent.interpreters",
    }
)
PY311_ATTRS = frozenset(
    {
        "hashlib.file_digest",
        "enum.StrEnum",
        "enum.ReprEnum",
        "enum.EnumCheck",
        "enum.FlagBoundary",
        "enum.verify",
        "enum.member",
        "enum.nonmember",
        "enum.global_enum",
        "datetime.UTC",
        "typing.Self",
        "typing.LiteralString",
        "typing.Never",
        "typing.assert_never",
        "typing.assert_type",
        "typing.reveal_type",
        "typing.override",
        "typing.Required",
        "typing.NotRequired",
        "typing.ReadOnly",
        "typing.TypeIs",
        "typing.TypeVarTuple",
        "typing.Unpack",
        "typing.TypeAliasType",
        "typing.dataclass_transform",
        "typing.get_overloads",
        "typing.clear_overloads",
        "contextlib.chdir",
        "itertools.batched",
        "asyncio.TaskGroup",
        "asyncio.Runner",
        "asyncio.Barrier",
        "asyncio.timeout",
        "asyncio.timeout_at",
        "math.cbrt",
        "math.exp2",
        "math.fma",
        "operator.call",
        "re.NOFLAG",
        "sys.exception",
        "logging.getLevelNamesMapping",
        "logging.getHandlerByName",
        "logging.getHandlerNames",
        "os.process_cpu_count",
        "os.path.splitroot",
    }
)
PY311_NAMES = frozenset({"ExceptionGroup", "BaseExceptionGroup"})
PY312_KEYWORDS = frozenset({"onexc"})  # shutil.rmtree(onexc=) is 3.12+
# Path.walk() is 3.12+; flagged when the receiver is evidently a pathlib path.
PATH_CLASSES = frozenset(
    {"pathlib.Path", "pathlib.PurePath", "pathlib.PosixPath", "pathlib.PurePosixPath"}
)
PATH_METHODS = frozenset(
    {"resolve", "absolute", "expanduser", "joinpath", "with_name", "with_suffix", "relative_to",
     "home", "cwd"}
)
# tarfile / shutil extraction filter= must be guarded by hasattr(tarfile, "data_filter").
EXTRACT_FUNCS = frozenset({"extractall", "extract", "unpack_archive"})

# Any standalone ".wine" path component: "~/.wine", "$HOME/.wine", '".wine"', "{home}/.wine".
WINE_HOME_RE = re.compile(r"(?<![\w.-])\.wine(?![\w-])")
WINE_REFUSAL_WORDS = ("refus", "never", "forbidden")
# Shipped runtime code (src/, the bin/ launcher, the libexec/ host helper).
RUNTIME_CODE_DIRS = ("src", "bin", "libexec")

ADAPTERS = (
    "CLAUDE.md",
    "GEMINI.md",
    "agent.md",
    "skills.md",
    ".github/copilot-instructions.md",
    ".cursor/rules/fork-linux-agents.mdc",
)


def _repo_files() -> list[str]:
    """Return repo-relative paths git would commit (tracked + untracked, not ignored)."""
    try:
        out = subprocess.run(
            [
                "git", "-c", f"safe.directory={ROOT}", "-C", str(ROOT),
                "ls-files", "-z", "--cached", "--others", "--exclude-standard",
            ],
            capture_output=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        pytest.skip(f"git file listing unavailable: {exc}")
    return sorted({p for p in out.stdout.decode("utf-8", "surrogateescape").split("\0") if p})


def _existing(rel: str) -> Path | None:
    path = ROOT / rel
    return path if path.is_file() else None


def _head(path: Path, size: int = 8) -> bytes:
    with path.open("rb") as handle:
        return handle.read(size)


def _py_files(*roots: Path) -> list[Path]:
    files: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            files.extend(Path(dirpath) / n for n in filenames if n.endswith(".py"))
    return sorted(files)


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


# --- Fork binaries and artwork (hard rules 1 and 4) -------------------------


def test_no_windows_binaries_or_packages_in_repo() -> None:
    bad = [rel for rel in _repo_files() if Path(rel).suffix.lower() in FORBIDDEN_BINARY_SUFFIXES]
    assert bad == [], f"Windows binaries / nupkgs / icons must never be committed: {bad}"


def test_no_pe_images_hidden_under_other_names() -> None:
    bad = []
    for rel in _repo_files():
        path = _existing(rel)
        if path is not None and _head(path, 2) == b"MZ":
            bad.append(rel)
    assert bad == [], f"PE executables must never be committed: {bad}"


def _image_allowed(rel: str) -> bool:
    return rel in IMAGE_ALLOWED_FILES or rel.startswith(IMAGE_ALLOWED_DIRS)


def test_images_only_in_allowlisted_locations() -> None:
    bad = []
    for rel in _repo_files():
        if _image_allowed(rel):
            continue
        path = _existing(rel)
        is_image = Path(rel).suffix.lower() in IMAGE_SUFFIXES
        if not is_image and path is not None:
            head = _head(path, 12)
            is_image = head.startswith(IMAGE_MAGIC) or (head[:4] == b"RIFF" and head[8:12] == b"WEBP")
        if is_image:
            bad.append(rel)
    assert bad == [], (
        "images are only allowed at .github/banner.png and under "
        f"{', '.join(IMAGE_ALLOWED_DIRS)} (never Fork artwork): {bad}"
    )


# --- Root stays clean (AGENTS.md §4.7.1) -------------------------------------


def test_repo_root_only_holds_allowed_entries() -> None:
    bad = set()
    for rel in _repo_files():
        top, sep, _ = rel.partition("/")
        if sep:
            if top not in ALLOWED_ROOT_DIRS:
                bad.add(top + "/")
        elif top not in ALLOWED_ROOT_FILES:
            bad.add(top)
    assert sorted(bad) == [], f"unexpected repo-root entries (AGENTS.md §4.7.1): {sorted(bad)}"


# --- .gitignore / .dockerignore contract (AGENTS.md §4.7.1) --------------------

# Real sources the Python .gitignore template would hide if its rules were not anchored.
GIT_KEPT = (
    "src/fork_linux/download.py",
    "src/fork_linux/downloads/x.py",
    "src/fork_linux/lib/x.py",
    "src/fork_linux/build/x.py",
    "src/fork_linux/steps/prefix_init.py",
    "src/fork_linux/data/runtime-manifest.json",
    "src/fork_linux/data/templates/fork.desktop.in",
    "src/fork_linux/data/icons/placeholder.svg",
    "bridge/common/lib/translate.c",
    "bridge/win/app.manifest",
    "bridge/win/res/app.manifest",
    "bridge/win/res/fl-shim.rc",
    "packaging/rpm/fedora/fork-linux.spec",
    "packaging/rpm/opensuse/fork-linux.spec",
    "packaging/arch/PKGBUILD.in",
    "packaging/snap/snapcraft.yaml",
    "packaging/flatpak/io.github.ventura8.ForkLinux.yml",
    "debian/control",
    "debian/rules",
    "debian/source/format",
    "data/icons/hicolor/scalable/apps/io.github.ventura8.ForkLinux.svg",
    "docs/assets/banner.svg",
    "docs/releases/v1.0.0.md",
    ".github/banner.png",
    ".cursor/rules/fork-linux-agents.mdc",
    ".agents/skills/release/SKILL.md",
    "logs/README.md",
    "tests/fixtures/crash/fork.log",
    "tests/fakes/bin/wine",
    "scripts/build-bridge.sh",
    "bin/fork-linux.in",
    "libexec/fork-linux-host.in",
)
# Build output, downloads, Wine state and secrets that must never be committed.
GIT_IGNORED = (
    "build/meson-logs/x.txt",
    "build-ci-lint/x",
    "builddir/x",
    "obj-x86_64-linux-gnu/x",
    "artifacts/fork-linux_0.1.0_amd64.deb",
    "fork-linux_0.1.0_amd64.deb",
    "fork-linux-0.1.0-1.fc44.x86_64.rpm",
    "fork-linux-0.1.0-1-x86_64.pkg.tar.zst",
    "fork-linux_0.1.0_amd64.snap",
    "fork-linux-0.1.0-x86_64.AppImage",
    "fork-linux-0.1.0-x86_64.AppImage.zsync",
    "io.github.ventura8.ForkLinux.flatpak",
    "Fork-2.23.2.exe",
    "bridge/win/fl-shim.exe",
    "x.dll",
    "x.msi",
    "Fork-2.23.2-full.nupkg",
    "data/fork.ico",
    "src/fork_linux/_build.py",
    ".wine/system.reg",
    "tests/.wine-scratch/user.reg",
    "wineprefix-e2e/user.reg",
    ".e2e-home/.local/share/x",
    "logs/agent-progress.log",
    "logs/ci-matrix/jammy.log",
    "debian/.debhelper/x",
    "debian/tmp/usr/bin/fork-linux",
    "debian/fork-linux/usr/bin/fork-linux",
    "debian/files",
    "debian/fork-linux.substvars",
    "packaging/snap/parts/x",
    "packaging/snap/stage/x",
    "packaging/snap/prime/x",
    "packaging/snap/overlay/x",
    "packaging/snap/.craft/x",
    "packaging/arch/src/x",
    "packaging/arch/pkg/x",
    "packaging/arch/PKGBUILD",
    ".flatpak-builder/cache/x",
    "squashfs-root/AppRun",
    ".sonar-token",
    ".scannerwork/x",
    ".claude/worktrees/x/AGENTS.md",
    ".claude/settings.local.json",
    "src/fork_linux/__pycache__/x.cpython-310.pyc",
    ".coverage",
)


def _git_ignored(paths: tuple[str, ...]) -> set[str]:
    """Return the subset of ``paths`` the .gitignore rules ignore (pattern check only)."""
    try:
        out = subprocess.run(
            ["git", "-c", f"safe.directory={ROOT}", "-C", str(ROOT), "check-ignore", "--no-index",
             "--stdin", "-z"],
            input="\0".join(paths).encode("utf-8"),
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        pytest.skip(f"git unavailable: {exc}")
    if out.returncode not in (0, 1):
        pytest.skip(f"git check-ignore unavailable: {out.stderr.decode(errors='replace')}")
    return {p for p in out.stdout.decode("utf-8").split("\0") if p}


def test_gitignore_keeps_real_sources() -> None:
    assert sorted(_git_ignored(GIT_KEPT)) == [], "these sources must stay committable"


def test_gitignore_hides_outputs_binaries_and_secrets() -> None:
    assert sorted(set(GIT_IGNORED) - _git_ignored(GIT_IGNORED)) == [], "these must be gitignored"


def test_dockerignore_excludes_secrets_binaries_and_output() -> None:
    entries = {
        line.strip()
        for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }
    required = {
        ".git", ".sonar-token", ".env", ".claude", "artifacts", "logs", "build", "build-*",
        "*.exe", "**/*.exe", "*.dll", "**/*.dll", "*.nupkg", "**/*.nupkg", "*.ico", "**/*.ico",
        ".wine*", "**/.wine*", "*.deb", "*.rpm", "*.AppImage", "*.flatpak", "*.snap",
    }
    assert sorted(required - entries) == []


# --- Python: stdlib only, no 3.11+ APIs (hard rule 15) -----------------------


def _top_level_imports(tree: ast.Module) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((node.lineno, alias.name.split(".")[0]) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.append((node.lineno, node.module.split(".")[0]))
    return found


def test_package_imports_only_stdlib_or_fork_linux() -> None:
    files = _py_files(SRC_PKG)
    assert files, "src/fork_linux has no Python files"
    allowed = set(sys.stdlib_module_names) | {"fork_linux"}
    bad = [
        f"{path.relative_to(ROOT)}:{lineno}: import {name}"
        for path in files
        for lineno, name in _top_level_imports(_parse(path))
        if name not in allowed
    ]
    assert bad == [], "src/fork_linux must use the standard library only:\n" + "\n".join(bad)


def test_tests_and_scripts_import_only_stdlib_pytest_or_local() -> None:
    """pytest is the only test dependency (AGENTS.md §1); local helpers live in tests/."""
    local = {
        entry.stem if entry.suffix == ".py" else entry.name
        for entry in (ROOT / "tests").iterdir()
        if entry.suffix == ".py" or (entry / "__init__.py").is_file()
    }
    allowed = set(sys.stdlib_module_names) | {"fork_linux", "pytest"} | local
    bad = [
        f"{path.relative_to(ROOT)}:{lineno}: import {name}"
        for path in _py_files(ROOT / "tests", ROOT / "scripts")
        for lineno, name in _top_level_imports(_parse(path))
        if name not in allowed
    ]
    assert bad == [], "tests/ and scripts/ may import only stdlib, pytest and fork_linux:\n" + "\n".join(bad)


def _aliases(tree: ast.Module) -> tuple[dict[str, str], dict[str, str]]:
    """Map local names to what they refer to: (``import x [as y]`` modules, from-imports)."""
    modules: dict[str, str] = {}
    names: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    modules[alias.asname] = alias.name
                else:
                    head = alias.name.split(".")[0]
                    modules[head] = head
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            for alias in node.names:
                names[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return modules, names


def _dotted(node: ast.AST, aliases: dict[str, str]) -> str | None:
    """Return the fully qualified dotted name of ``a.b.c`` (head resolved via ``aliases``)."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name) or node.id not in aliases:
        return None
    return ".".join([aliases[node.id], *reversed(parts)])


def _prefixes(dotted: str) -> list[str]:
    """``a.b.c`` -> ``["a", "a.b", "a.b.c"]``."""
    parts = dotted.split(".")
    return [".".join(parts[: i + 1]) for i in range(len(parts))]


def _looks_like_path(node: ast.AST, aliases: dict[str, str]) -> bool:
    """True if ``node`` is evidently a pathlib path (or class): ``Path(x)``, ``p / "x"``, ..."""
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        return True
    if isinstance(node, ast.Attribute) and node.attr == "parent":
        return True
    if isinstance(node, ast.Call):
        func = node.func
        if _dotted(func, aliases) in PATH_CLASSES:
            return True
        return isinstance(func, ast.Attribute) and func.attr in PATH_METHODS
    return _dotted(node, aliases) in PATH_CLASSES


def _has_filter_guard(tree: ast.Module) -> bool:
    """True if the module probes ``hasattr/getattr(<x>, "<...>filter")`` (a 3.10 guard)."""
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in {"hasattr", "getattr"}
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and str(node.args[1].value).endswith("filter")
        ):
            return True
    return False


def py311_violations(tree: ast.Module) -> list[tuple[int, str]]:
    """Return (line, description) for every Python 3.11+ API used in ``tree``."""
    modules, names = _aliases(tree)
    every = {**names, **modules}
    guarded = _has_filter_guard(tree)
    found: list[tuple[int, str]] = []
    try_star = getattr(ast, "TryStar", None)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend(
                (node.lineno, f"import {a.name}")
                for a in node.names
                if any(p in PY311_MODULES for p in _prefixes(a.name))
            )
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            if any(p in PY311_MODULES for p in _prefixes(node.module)):
                found.append((node.lineno, f"from {node.module} import ..."))
            found.extend(
                (node.lineno, f"from {node.module} import {a.name}")
                for a in node.names
                if f"{node.module}.{a.name}" in PY311_ATTRS | PY311_MODULES
            )
        elif isinstance(node, ast.Attribute):
            dotted = _dotted(node, every)
            if dotted in PY311_ATTRS:
                found.append((node.lineno, dotted))
        elif isinstance(node, ast.Name) and node.id in PY311_NAMES:
            found.append((node.lineno, node.id))
        elif isinstance(node, ast.keyword) and node.arg in PY312_KEYWORDS:
            found.append((getattr(node, "lineno", 0), f"{node.arg}= keyword"))
        elif try_star is not None and isinstance(node, try_star):
            found.append((node.lineno, "except*"))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            func = node.func
            if func.attr == "walk" and _looks_like_path(func.value, every):
                found.append((node.lineno, "Path.walk()"))
            elif (
                func.attr in EXTRACT_FUNCS
                and any(k.arg == "filter" for k in node.keywords)
                and not guarded
            ):
                found.append((node.lineno, f"{func.attr}(filter=) without a hasattr guard"))
    return found


def test_py311_detector_catches_known_apis() -> None:
    sample = ast.parse(
        "import tomllib\n"
        "import hashlib as h\n"
        "import shutil\n"
        "from enum import StrEnum\n"
        "from typing import Self\n"
        "import datetime\n"
        "x = datetime.UTC\n"
        "y = h.file_digest\n"
        "raise ExceptionGroup('m', [])\n"
        "shutil.rmtree('p', onexc=None)\n"
    )
    found = {desc for _, desc in py311_violations(sample)}
    assert found == {
        "import tomllib",
        "from enum import StrEnum",
        "from typing import Self",
        "datetime.UTC",
        "hashlib.file_digest",
        "ExceptionGroup",
        "onexc= keyword",
    }
    clean = ast.parse("import datetime\nfrom datetime import timezone\nz = datetime.timezone.utc\n")
    assert py311_violations(clean) == []


def test_py311_detector_dotted_modules_and_attrs() -> None:
    sample = ast.parse(
        "import os.path\n"
        "import os.path as osp\n"
        "from os import path\n"
        "import string.templatelib\n"
        "from wsgiref import types\n"
        "from compression import zstd\n"
        "import sys\n"
        "a = os.path.splitroot('x')\n"
        "b = osp.splitroot('x')\n"
        "c = path.splitroot('x')\n"
        "d = sys.exception()\n"
    )
    found = sorted(py311_violations(sample))
    assert found == [
        (4, "import string.templatelib"),
        (5, "from wsgiref import types"),
        (6, "from compression import ..."),
        (8, "os.path.splitroot"),
        (9, "os.path.splitroot"),
        (10, "os.path.splitroot"),
        (11, "sys.exception"),
    ]


def test_py311_detector_path_walk() -> None:
    sample = ast.parse(
        "import os, ast, pathlib\n"
        "from pathlib import Path\n"
        "for a in Path('x').walk(): pass\n"
        "for b in (Path.home() / 'x').walk(): pass\n"
        "for c in Path.walk(Path('x')): pass\n"
        "for d in pathlib.Path('x').resolve().walk(): pass\n"
        "for e in Path('x').parent.walk(): pass\n"
        "for f in os.walk('x'): pass\n"
        "for g in ast.walk(tree): pass\n"
        "for h in Walker(data).walk(): pass\n"
        "for i in msg.walk(): pass\n"
    )
    assert [line for line, desc in py311_violations(sample) if desc == "Path.walk()"] == [3, 4, 5, 6, 7]


def test_py311_detector_tarfile_filter_needs_guard() -> None:
    unguarded = ast.parse("import tarfile\nt.extractall(dest, filter='data')\n")
    assert py311_violations(unguarded) == [(2, "extractall(filter=) without a hasattr guard")]
    shutil_call = ast.parse("import shutil\nshutil.unpack_archive(a, d, filter='data')\n")
    assert py311_violations(shutil_call) == [(2, "unpack_archive(filter=) without a hasattr guard")]
    guarded = ast.parse(
        "import tarfile\n"
        "if hasattr(tarfile, 'data_filter'):\n"
        "    t.extractall(dest, filter='data')\n"
    )
    assert py311_violations(guarded) == []
    no_filter = ast.parse("t.extractall(dest)\nt.extract(m, path=dest)\n")
    assert py311_violations(no_filter) == []


def test_no_python_311_apis() -> None:
    files = _py_files(ROOT / "src", ROOT / "tests", ROOT / "scripts")
    bad = [
        f"{path.relative_to(ROOT)}:{lineno}: {desc}"
        for path in files
        for lineno, desc in py311_violations(_parse(path))
    ]
    assert bad == [], "Python 3.11+ APIs are forbidden (3.10 is supported):\n" + "\n".join(bad)


def test_python_modules_use_future_annotations() -> None:
    missing = []
    for path in _py_files(SRC_PKG, ROOT / "tests", ROOT / "scripts"):
        tree = _parse(path)
        if not tree.body:
            continue
        has_future = any(
            isinstance(node, ast.ImportFrom)
            and node.module == "__future__"
            and any(a.name == "annotations" for a in node.names)
            for node in tree.body
        )
        has_code = any(
            not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant))
            and not isinstance(node, ast.Assign)
            for node in tree.body
        )
        if has_code and not has_future:
            missing.append(str(path.relative_to(ROOT)))
    assert missing == [], f"add 'from __future__ import annotations' to: {missing}"


# --- ~/.wine is never targeted (hard rule 5) ----------------------------------


def _refusal_scopes(text: str) -> list[tuple[int, int]]:
    """Line ranges of Python functions whose name or docstring says refus/never/forbidden."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    scopes: list[tuple[int, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            words = f"{node.name} {ast.get_docstring(node) or ''}".lower()
            if any(word in words for word in WINE_REFUSAL_WORDS):
                scopes.append((node.lineno, getattr(node, "end_lineno", node.lineno)))
    return scopes


def wine_home_violations(text: str) -> list[tuple[int, str]]:
    """Return (line, text) for ``.wine`` home-prefix mentions that are not refusals.

    A mention counts as a refusal when its line or the line just above it (a comment
    over code) contains "refus", "never" or "forbidden", or when it sits inside a Python
    function whose name or docstring does (``def _always_refused(...)``).
    """
    lines = text.splitlines()
    scopes = _refusal_scopes(text) if WINE_HOME_RE.search(text) else []
    found: list[tuple[int, str]] = []
    for lineno, line in enumerate(lines, start=1):
        if not WINE_HOME_RE.search(line):
            continue
        window = " ".join(lines[max(lineno - 2, 0): lineno]).lower()
        if any(word in window for word in WINE_REFUSAL_WORDS):
            continue
        if any(first <= lineno <= last for first, last in scopes):
            continue
        found.append((lineno, line.strip()))
    return found


def test_wine_home_detector() -> None:
    assert wine_home_violations('prefix = "~/.wine"\n') == [(1, 'prefix = "~/.wine"')]
    assert wine_home_violations('x = 1\nos.environ["WINEPREFIX"] = "$HOME/.wine"\n') == [
        (2, 'os.environ["WINEPREFIX"] = "$HOME/.wine"')
    ]
    assert wine_home_violations('p = Path.home() / ".wine"\n') == [(1, 'p = Path.home() / ".wine"')]
    assert wine_home_violations('p = os.path.join(home, ".wine")\n') != []
    assert wine_home_violations('p = f"{home}/.wine/drive_c"\n') != []
    assert wine_home_violations('WINEPREFIX="${HOME}/.wine" wine x\n') != []
    assert wine_home_violations('raise UsageError("we never touch ~/.wine")\n') == []
    assert wine_home_violations('def _always_refused():\n    """``~/.wine`` and below."""\n') == []
    assert wine_home_violations("# Forbidden target:\nBAD = '~/.wine'\n") == []
    refusing = (
        "def _always_refused(path, home):\n"
        '    """Our refusal list."""\n'
        "    real = home.resolve()\n"
        '    wine = (home / ".wine").resolve()\n'
        "    return path == wine\n"
    )
    assert wine_home_violations(refusing) == []
    # Unrelated names are not ".wine" path components.
    for ok in ("WINEPREFIX=$prefix", "x.winecfg", "fork.wine_root", "~/.wine-fork-linux", "a.wine.b"):
        assert wine_home_violations(ok + "\n") == [], ok
    # A refusing function elsewhere does not excuse an unrelated function.
    mixed = refusing + 'def setup(home):\n    return home / ".wine"\n'
    assert wine_home_violations(mixed) == [(7, 'return home / ".wine"')]
    assert wine_home_violations("not python (\nprefix=~/.wine\n") == [(2, "prefix=~/.wine")]


def test_wine_home_only_mentioned_to_refuse_it() -> None:
    bad = []
    for top in RUNTIME_CODE_DIRS:
        for dirpath, dirnames, filenames in os.walk(ROOT / top):
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            for name in filenames:
                path = Path(dirpath) / name
                try:
                    text = path.read_text(encoding="utf-8")
                except (UnicodeDecodeError, OSError):
                    continue
                bad.extend(
                    f"{path.relative_to(ROOT)}:{lineno}: {line}"
                    for lineno, line in wine_home_violations(text)
                )
    assert bad == [], "shipped code must never target ~/.wine (only refuse it):\n" + "\n".join(bad)


# --- Trademark-safe naming (hard rule 10) -------------------------------------


def _naming_files() -> list[Path]:
    candidates: list[Path] = []
    for base in (ROOT / "data", SRC_PKG / "data" / "templates"):
        if base.is_dir():
            candidates.extend(sorted(base.glob("*.desktop*")))
            candidates.extend(sorted(base.glob("*.metainfo.xml*")))
            candidates.extend(sorted(base.glob("*.appdata.xml*")))
    return [p for p in candidates if p.is_file()]


def _desktop_entry_group(text: str) -> str:
    """Return the ``[Desktop Entry]`` group (Desktop Action names are not app names)."""
    if "[Desktop Entry]" not in text:
        return text
    group = text.split("[Desktop Entry]", 1)[1]
    return re.split(r"(?m)^\[", group, maxsplit=1)[0]


def naming_problems(text: str) -> list[str]:
    """Return product-name occurrences in a desktop/metainfo file lacking "(unofficial)"."""
    flat = re.sub(r"\s+", " ", text)
    problems = [
        flat[m.start(): m.end() + 14]
        for m in re.finditer(r"Fork for Linux", flat)
        if not flat[m.end():].startswith(" (unofficial)")
    ]
    for match in re.finditer(r"(?m)^Name(?:\[[^\]]+\])?=(.*)$", _desktop_entry_group(text)):
        value = match.group(1)
        if "@" not in value and "(unofficial)" not in value:
            problems.append(match.group(0))
    component = re.sub(r"<developer\b.*?</developer>", "", text, flags=re.S)
    for match in re.finditer(r"<name(?:\s[^>]*)?>(.*?)</name>", component, re.S):
        value = match.group(1)
        if "@" not in value and "(unofficial)" not in value:
            problems.append(match.group(0))
    return problems


def test_naming_detector() -> None:
    assert naming_problems("Name=Fork for Linux (unofficial)\nComment=Fork for Linux (unofficial) runs Fork") == []
    assert naming_problems("Name=@APP_NAME@\n") == []
    assert naming_problems("<name>Fork for Linux\n  (unofficial)</name>") == []
    assert naming_problems("Name=Fork for Linux\n") != []
    assert naming_problems("Name=Fork\n") != []
    assert naming_problems("<name>Fork</name>") != []
    desktop = "[Desktop Entry]\nName=Fork for Linux (unofficial)\n[Desktop Action open]\nName=Open Repository\n"
    assert naming_problems(desktop) == []
    metainfo = "<name>Fork for Linux (unofficial)</name><developer id='x'><name>Someone</name></developer>"
    assert naming_problems(metainfo) == []


def test_user_visible_names_say_unofficial() -> None:
    files = _naming_files()
    if not files:
        pytest.skip("no desktop/metainfo files yet (data/ is planned)")
    bad = {
        str(path.relative_to(ROOT)): naming_problems(path.read_text(encoding="utf-8"))
        for path in files
    }
    bad = {k: v for k, v in bad.items() if v}
    assert bad == {}, f"user-visible names must include '(unofficial)': {bad}"


# --- Credits and disclaimer stay intact (hard rule 11) -------------------------


@pytest.mark.parametrize("rel", ["README.md", "docs/CREDITS.md"])
def test_credits_and_disclaimer_present(rel: str) -> None:
    text = (ROOT / rel).read_text(encoding="utf-8")
    for needle in ("Dan Pristupov", "Tanya Pristupova", "https://git-fork.com/buy", "https://git-fork.com"):
        assert needle in text, f"{rel} lost {needle!r}"
    assert re.search(r"(?i)not affiliated", text), f"{rel} lost the not-affiliated disclaimer"


def test_readme_credits_markers_wrap_the_block() -> None:
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    begin, end = "<!-- credits:begin -->", "<!-- credits:end -->"
    assert text.count(begin) == 1
    assert text.count(end) == 1
    block = text[text.index(begin): text.index(end)]
    assert "https://git-fork.com/buy" in block
    assert "Dan Pristupov" in block
    assert "Tanya Pristupova" in block


# --- Agent docs stay in sync (AGENTS.md §4.7, §5) ------------------------------


def test_agents_exit_code_table_matches_errors() -> None:
    from fork_linux.errors import ExitCode

    text = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    section = text.split("## 5. Standard Exit Code Mapping", 1)[1].split("\n## ", 1)[0]
    rows = dict(
        (int(code), name)
        for code, name in re.findall(r"(?m)^\|\s*`(\d+)`\s*\|\s*`([A-Z_]+)`\s*\|", section)
    )
    assert rows == {member.value: member.name for member in ExitCode}


@pytest.mark.parametrize("rel", ADAPTERS)
def test_thin_adapters_point_at_agents_md(rel: str) -> None:
    text = (ROOT / rel).read_text(encoding="utf-8")
    assert "AGENTS.md" in text
    assert len(text.splitlines()) < 60, f"{rel} must stay a thin pointer, not a second rulebook"


# Agent-facing docs whose relative links must resolve unless the line says "(planned)".
# README.md is excluded on purpose: it is the user-facing skeleton (Phase 12 fills its docs).
LINKED_DOCS = (
    "AGENTS.md",
    "CLAUDE.md",
    "GEMINI.md",
    "agent.md",
    "skills.md",
    ".github/copilot-instructions.md",
    ".github/pull_request_template.md",
    "logs/README.md",
    "docs/CREDITS.md",
)
MD_LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")


def link_problems(rel: str, text: str, exists: Callable[[Path], bool]) -> list[str]:
    """Return relative links whose target is missing on a line not marked "(planned)".

    ``exists`` answers whether a repo-relative target path exists.
    """
    problems: list[str] = []
    base = Path(rel).parent
    for lineno, line in enumerate(text.splitlines(), start=1):
        targets = [
            t.split("#", 1)[0]
            for t in MD_LINK.findall(line)
            if not re.match(r"^[a-z][a-z0-9+.-]*:", t) and not t.startswith("#")
        ]
        if not targets:
            continue
        present = all(exists(Path(os.path.normpath(base / t))) for t in targets)
        if not present and "(planned)" not in line:
            problems.append(f"{rel}:{lineno}: dead link (mark the line '(planned)' or fix it)")
    return problems


def test_link_problems_detector() -> None:
    def present(target: Path) -> bool:
        return target.as_posix() in {"AGENTS.md", "docs/CREDITS.md"}

    assert link_problems("CLAUDE.md", "[a](AGENTS.md) and [w](https://x.y/z) [s](#top)\n", present) == []
    assert link_problems("CLAUDE.md", "- [d](docs/SECURITY.md)\n", present) == [
        "CLAUDE.md:1: dead link (mark the line '(planned)' or fix it)"
    ]
    assert link_problems("CLAUDE.md", "- [d](docs/SECURITY.md) (planned)\n", present) == []
    assert link_problems("CLAUDE.md", "- [c](docs/CREDITS.md#top) and [m](missing.md)\n", present) == [
        "CLAUDE.md:1: dead link (mark the line '(planned)' or fix it)"
    ]
    assert link_problems(".github/x.md", "[a](../AGENTS.md)\n", present) == []
    assert link_problems("x.md", "no links here (planned)\n", present) == []


def test_agent_docs_links_resolve_or_are_planned() -> None:
    problems = [
        problem
        for rel in LINKED_DOCS
        for problem in link_problems(
            rel, (ROOT / rel).read_text(encoding="utf-8"), lambda target: (ROOT / target).exists()
        )
    ]
    assert problems == [], "\n".join(problems)


def test_cursor_rule_always_applies() -> None:
    text = (ROOT / ".cursor/rules/fork-linux-agents.mdc").read_text(encoding="utf-8")
    front = text.split("---", 2)[1]
    assert "alwaysApply: true" in front
    assert "description:" in front


def test_skills_index_matches_skill_dirs() -> None:
    index = (ROOT / "skills.md").read_text(encoding="utf-8")
    listed = dict(re.findall(r"(?m)^\| \[([a-z0-9-]+)\]\([^)]*\)( \(planned\))? \|", index))
    assert listed, "skills.md lists no skills"
    skills_dir = ROOT / ".agents" / "skills"
    present = {p.parent.name for p in skills_dir.glob("*/SKILL.md")} if skills_dir.is_dir() else set()
    unlisted = sorted(present - set(listed))
    assert unlisted == [], f"skills missing from skills.md: {unlisted}"
    wrong = sorted(
        name for name, planned in listed.items() if bool(planned) == (name in present)
    )
    assert wrong == [], f"fix the (planned) marker in skills.md for: {wrong}"
