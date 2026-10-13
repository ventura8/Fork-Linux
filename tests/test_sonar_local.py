"""Tests for scripts/sonar-local.py, the local stand-in for SonarCloud's complexity, shell and CPD rules."""

from __future__ import annotations

import ast
import importlib.util
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "sonar-local.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("fork_linux_sonar_local", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look their module up while the class is built
    spec.loader.exec_module(module)
    return module


sl = _load()


def _score(source: str) -> int:
    [function] = list(sl._functions(ast.parse(textwrap.dedent(source))))
    return sl.function_complexity(function)


# -- python:S3776 ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("def f():\n    return 1\n", 0),
        # if +1, elif +1, else +1; the nested if +2.
        (
            "def f(a, b):\n    if a:\n        if b:\n            pass\n"
            "    elif b:\n        pass\n    else:\n        pass\n",
            5,
        ),
        # else: if (not an elif) nests: else +1, if +2.
        ("def f(a, b):\n    if a:\n        pass\n    else:\n        if b:\n            pass\n", 4),
        # A sequence of like operators counts once; a change of operator once more.
        ("def f(a, b, c):\n    return a and b and c\n", 1),
        ("def f(a, b, c):\n    return a and b or c\n", 2),
        ("def f(a, b, c):\n    return a or (b or c)\n", 1),
        # for +1, its else +1; the nested while +2; except +1 (at nesting 0 inside try).
        ("def f(x):\n    for i in x:\n        while i:\n            i -= 1\n    else:\n        pass\n", 4),
        (
            "def f():\n    try:\n        pass\n    except (OSError, ValueError):\n        pass\n"
            "    finally:\n        pass\n",
            1,
        ),
        ("def f():\n    try:\n        pass\n    except:\n        pass\n", 1),
        # Ternaries: +1 and the nesting; nested ternaries count more.
        ("def f(a, b):\n    return 1 if a else (2 if b else 3)\n", 3),
        # Nested functions and lambdas raise the nesting level and count in the enclosing function.
        ("def f(a):\n    def g():\n        if a:\n            pass\n    return lambda: 1 if a else 2\n", 4),
        ("def f(a):\n    class C:\n        def m(self):\n            if a:\n                pass\n    return C\n", 3),
        ("def f(x):\n    match x:\n        case 1:\n            if x:\n                pass\n", 3),
        ("async def f(x):\n    async for i in x:\n        pass\n", 1),
    ],
)
def test_cognitive_complexity(source: str, expected: int) -> None:
    assert _score(source) == expected


def test_complexity_reference_values_match_sonarcloud() -> None:
    """Values SonarCloud (and a manual count with its rules) gave for functions of this repository."""
    param_id = '''
    def _param_id(value, index, argnames):
        if isinstance(value, ast.Call) and _call_name(value) == "param":
            for keyword in value.keywords:
                if keyword.arg == "id" and isinstance(keyword.value, ast.Constant):
                    return str(keyword.value.value)
            if len(value.args) == 1:
                return _param_id(value.args[0], index, argnames)
            return None
        if isinstance(value, ast.Tuple) and len(argnames) > 1:
            parts = [_scalar_id(item, index, name) for item, name in zip(value.elts, argnames)]
            return None if None in parts else "-".join(str(part) for part in parts)
        return _scalar_id(value, index, argnames[0] if argnames else "")
    '''
    assert _score(param_id) == 15


def test_check_complexity_reports_methods_over_the_limit_and_syntax_errors() -> None:
    source = "class A:\n    def m(self, a):\n        if a:\n            if a:\n                pass\n"
    [finding] = sl.check_complexity("x.py", source, 2)
    assert (finding.path, finding.line, finding.rule) == ("x.py", 2, "python:S3776")
    assert "cognitive complexity of m is 3 (more than 2)" in str(finding)
    assert sl.check_complexity("x.py", source, 3) == []
    [broken] = sl.check_complexity("bad.py", "def f(:\n", 15)
    assert broken.rule == "python:syntax"
    # A NUL byte: SyntaxError since Python 3.12, ValueError on 3.10 and 3.11.
    [nul] = sl.check_complexity("bad.py", "\x00", 15)
    assert (nul.line, nul.rule) == (1, "python:syntax")


def test_check_complexity_reports_a_value_error_as_syntax(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_args: object, **_kwargs: object) -> ast.Module:
        raise ValueError("source code string cannot contain null bytes")

    monkeypatch.setattr(sl.ast, "parse", refuse)
    [finding] = sl.check_complexity("bad.py", "x = 1\n", 15)
    assert (finding.line, finding.rule, finding.message) == (
        1, "python:syntax", "source code string cannot contain null bytes"
    )


# -- shelldre:S7682 / S131 ------------------------------------------------------------------------

SHELL = """\
#!/usr/bin/env bash
ok() {
\techo hi
\treturn 0
}
missing() {
\techo hi
}
quits() {
\texit 3
}
replaced() {
\texec true
}
heredoc() {
\tpython3 - <<'EOF' || return
def f():
    return 1
}
EOF
\treturn 0
}
heredoc_last() {
\tcat <<-END
\t\treturn 0
\tEND
}
one_ok() { echo x; return 0; }
one_missing() { echo x; }
one_open() { echo x
}
empty() {
}
# comment() {
\tnested() {
\t\ttrue
\t}
unclosed() {
\techo never closed
"""


def test_shell_functions_without_an_explicit_end() -> None:
    findings = sl.check_shell("x.sh", SHELL)
    named = {finding.message.split()[1] for finding in findings if finding.rule == "shelldre:S7682"}
    assert named == {"missing", "heredoc_last", "one_missing", "one_open", "nested"}
    assert all(finding.path == "x.sh" for finding in findings)


def test_case_without_a_default() -> None:
    text = """\
case "$a" in
x) : ;;
*) : ;;
esac
case "$b" in
y)
\tcase "$c" in
\t(*) : ;;
\tesac
\t;;
esac
esac
"""
    [finding] = sl.check_shell("x.sh", text)
    assert (finding.line, finding.rule) == (5, "shelldre:S131")


def test_here_strings_are_not_heredocs() -> None:
    text = 'f() {\n\tread -r x <<<"$y"\n\treturn 0\n}\n'
    assert sl.check_shell("x.sh", text) == []


# -- duplicated blocks ------------------------------------------------------------------------------

BLOCK = "\n".join(f"value_{n} = compute(alpha, beta, gamma, delta, {n})  # note" for n in range(10)) + "\n"


def test_duplicated_blocks_across_files_and_within_one() -> None:
    files = {
        "a.py": (BLOCK, "python"),
        "b.py": ("x = 1\n\n# only a comment\n" + BLOCK, "python"),
        "c.sh": (BLOCK, "shell"),  # another language never matches
        "d.py": (BLOCK + "y = 2\n" + BLOCK, "python"),
    }
    findings = sl.check_duplication(files, 10, 100)
    assert [(f.path, f.line) for f in findings] == [("b.py", 4), ("d.py", 1), ("d.py", 12)]
    assert "10 lines (" in findings[0].message and "duplicated from a.py:1" in findings[0].message


def test_short_or_overlapping_repeats_are_not_blocks() -> None:
    small = "\n".join(f"x{n} = 1" for n in range(10)) + "\n"
    assert sl.check_duplication({"a.py": (small, "python"), "b.py": (small, "python")}, 10, 100) == []
    # A run of one line: 15 lines hold no two separate 10-line windows, 20 lines do.
    assert sl.check_duplication({"a.py": ("x = 1\n" * 15, "python")}, 10, 1) == []
    assert sl.check_duplication({"a.py": ("x = 1\n" * 20, "python")}, 10, 1) == [
        sl.Finding("a.py", 11, "common:duplicated-block", "10 lines (30 tokens) duplicated from a.py:1")
    ]


def test_a_longer_duplicate_is_one_finding() -> None:
    long = "\n".join(f"value_{n} = compute(alpha, beta, gamma, delta, {n})" for n in range(25)) + "\n"
    c_comments = "/* header */\n// note\n * star\n" + long
    findings = sl.check_duplication({"a.c": (long, "c"), "b.c": (c_comments, "c")}, 10, 100)
    assert len(findings) == 1
    assert findings[0].message.startswith("25 lines (")


def test_stretches_split_at_a_gap_or_another_source() -> None:
    matches = {0: ("a", 0), 1: ("a", 1), 5: ("a", 9), 6: ("b", 10)}
    assert sl._stretches(matches, 3) == [(0, 4, "a", 0), (5, 8, "a", 9), (6, 9, "b", 10)]


# -- files and the command line ---------------------------------------------------------------------


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "skipped").mkdir()
    (root / "sonar-project.properties").write_text(
        "sonar.sources=src\nsonar.exclusions=src/skipped/**\n", encoding="utf-8"
    )
    (root / "src" / "ok.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (root / "src" / "run").write_text("#!/bin/sh\nf() {\n\ttrue\n}\n", encoding="utf-8")
    (root / "src" / "notes").write_text("plain text\n", encoding="utf-8")
    (root / "src" / "data.json").write_text("{}\n", encoding="utf-8")
    (root / "src" / "skipped" / "bad.sh").write_text("g() {\n\ttrue\n}\n", encoding="utf-8")
    return root


def test_language(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    assert sl.language(root, "src/ok.py") == "python"
    assert sl.language(root, "src/run") == "shell"
    assert sl.language(root, "src/notes") is None
    assert sl.language(root, "src/data.json") is None
    assert sl.language(root, "x.h") == "c"
    assert sl.language(root, "x.bash") == "shell"
    assert sl.language(root, "missing") is None


def test_main_on_a_fixture_repository(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _repo(tmp_path)
    assert sl.main(["--root", str(root)]) == 1
    out = capsys.readouterr().out
    assert "src/run:2: shelldre:S7682: function f does not end with an explicit return or exit" in out
    assert "bad.sh" not in out
    assert out.rstrip().endswith("1 finding(s)")
    assert sl.main(["--root", str(root), "complexity", "duplication"]) == 0
    with_inclusions = root / "inclusions.properties"
    with_inclusions.write_text("sonar.sources=src\nsonar.inclusions=**/*.py\n", encoding="utf-8")
    assert sl.main(["--root", str(root), "--properties", str(with_inclusions)]) == 0
    assert "3 files" not in capsys.readouterr().out


def test_main_bad_input(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert sl.main(["--root", str(tmp_path)]) == 2
    assert "sonar-project.properties" in capsys.readouterr().err
    bad = tmp_path / "bad.properties"
    bad.write_text("sonar.exclusions=file:/abs\n", encoding="utf-8")
    assert sl.main(["--root", str(tmp_path), "--properties", str(bad)]) == 2
    with pytest.raises(SystemExit) as caught:
        sl.main(["nonsense"])
    assert caught.value.code == 2
    assert "unknown check(s): nonsense" in capsys.readouterr().err


def test_the_repository_has_no_findings() -> None:
    """The gate itself: what the lint stage runs (scripts/ci-docker.sh run_sonar_local)."""
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    result = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True, check=False, cwd=ROOT)
    assert result.returncode == 0, result.stdout + result.stderr


def test_lint_stage_runs_it() -> None:
    text = (ROOT / "scripts" / "ci-docker.sh").read_text(encoding="utf-8")
    assert "python3 scripts/sonar-local.py" in text
    assert "\t\t\trun_sonar_local\n" in text
