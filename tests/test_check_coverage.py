"""Tests for scripts/check-coverage.py, the per-file coverage gate (loaded via importlib).

Fixture repositories are written under tmp_path: a sonar-project.properties, a few Python and
C sources, a Cobertura report and a SonarQube generic report. The real properties file and the
CI wiring (ci-docker.sh, ci-sonar.sh, check.yml) are checked at the end.
"""

from __future__ import annotations

import importlib.util
import re
import runpy
import shutil
import subprocess
import sys
from fractions import Fraction
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check-coverage.py"
GATE = "python3 scripts/check-coverage.py --min 90 --min-python-line 100 --min-python-branch 100"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_coverage", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look their module up while the class is built
    spec.loader.exec_module(module)
    return module


cc = _load()

PROPERTIES = """\
sonar.sources=src/pkg,bridge
sonar.python.coverage.reportPaths=out/coverage.xml
sonar.coverageReportPaths=out/c-coverage.xml
sonar.exclusions=\\
  bridge/tests/**,\\
  **/build-*/**
sonar.coverage.exclusions=src/pkg/gen/**
"""


def _cobertura(source: Path, classes: dict[str, list[tuple[int, int, str | None]]]) -> str:
    """A coverage.py-like report: filename -> [(line, hits, condition-coverage or None)]."""
    parts = [f'<?xml version="1.0" ?>\n<coverage version="7"><sources><source>{source}</source></sources>']
    parts.append('<packages><package name="."><classes>')
    for filename, lines in classes.items():
        parts.append(f'<class name="{filename}" filename="{filename}"><methods/><lines>')
        for number, hits, cond in lines:
            branch = f' branch="true" condition-coverage="{cond}"' if cond else ""
            parts.append(f'<line number="{number}" hits="{hits}"{branch}/>')
        parts.append("</lines></class>")
    parts.append("</classes></package></packages></coverage>")
    return "".join(parts)


def _generic(files: dict[str, list[tuple[int, bool, int, int]]]) -> str:
    """A SonarQube generic report: path -> [(line, covered, branches, covered branches)]."""
    parts = ['<?xml version="1.0" encoding="utf-8"?>\n<coverage version="1">']
    for path, lines in files.items():
        parts.append(f'<file path="{path}">')
        for number, covered, branches, taken in lines:
            extra = f' branchesToCover="{branches}" coveredBranches="{taken}"' if branches else ""
            parts.append(f'<lineToCover lineNumber="{number}" covered="{str(covered).lower()}"{extra}/>')
        parts.append("</file>")
    parts.append("</coverage>")
    return "".join(parts)


FULL_PY = {
    "a.py": [(1, 1, None), (2, 1, "100% (2/2)")],
    "sub/b.py": [(1, 1, None)],
}
# 18 of 20 lines + 9 of 10 branches: (18 + 9) / 30 = 90% exactly.
C_AT_BAR = [(n, n <= 18, 0, 0) for n in range(1, 21)]
C_AT_BAR[0] = (1, True, 10, 9)


class Repo:
    """A fixture repository with sources, the properties file and both reports."""

    def __init__(self, root: Path) -> None:
        self.root = root
        for rel in ("src/pkg/a.py", "src/pkg/sub/b.py", "src/pkg/gen/g.py", "bridge/x.c", "bridge/x.h"):
            self.write(rel, "\n")
        self.write("bridge/tests/t.c", "\n")
        self.write("sonar-project.properties", PROPERTIES)
        self.python(FULL_PY)
        self.c({"bridge/x.c": C_AT_BAR})

    def write(self, rel: str, text: str) -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def python(self, classes: dict[str, list[tuple[int, int, str | None]]]) -> None:
        self.write("out/coverage.xml", _cobertura(self.root / "src" / "pkg", classes))

    def c(self, files: dict[str, list[tuple[int, bool, int, int]]]) -> None:
        self.write("out/c-coverage.xml", _generic(files))

    def main(self, *extra: str) -> int:
        return cc.main(["--root", str(self.root), *extra])


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    return Repo(tmp_path / "repo")


# ---------------------------------------------------------------------------
# the gate
# ---------------------------------------------------------------------------


def test_passing_reports_exit_zero(repo: Repo, capsys: pytest.CaptureFixture[str]) -> None:
    assert repo.main() == 0
    out = capsys.readouterr().out
    assert "check-coverage: OK, 3 files measured (2 Python, 1 C)" in out
    assert "lowest 90.00% bridge/x.c" in out
    assert "below the bar" not in out


def test_a_c_file_below_the_bar_fails_with_a_table(repo: Repo, capsys: pytest.CaptureFixture[str]) -> None:
    lines = list(C_AT_BAR)
    lines[1] = (2, False, 0, 0)  # 17/20 lines + 9/10 branches = 26/30
    repo.c({"bridge/x.c": lines})
    assert repo.main() == 1
    out = capsys.readouterr().out
    assert "1 file(s) below the bar:" in out
    assert "  coverage  lines        conditions   file" in out
    assert re.search(r"86\.66%\s+17/20\s+9/10\s+bridge/x\.c  \(coverage 86\.66% < 90\.00%\)", out)
    assert "check-coverage: FAIL" in out


def test_sonarqube_counts_conditions_with_lines() -> None:
    cov = cc.FileCoverage()
    for number in range(1, 9):
        cov.add(number, number <= 7)  # 7 of 8 lines
    cov.add(9, True, 4, 1)  # 1 of 4 conditions
    assert cov.line_counts == (8, 9)
    assert cov.condition_counts == (1, 4)
    assert cov.combined == Fraction(100 * 9, 13)
    assert cc.FileCoverage().combined == 100


def test_python_files_need_full_line_and_branch_coverage(repo: Repo, capsys: pytest.CaptureFixture[str]) -> None:
    lines = [(n, 1, None) for n in range(1, 40)] + [(40, 0, None), (41, 1, "50% (1/2)")]
    repo.python({"a.py": lines, "sub/b.py": [(1, 1, None)]})
    assert repo.main() == 1
    out = capsys.readouterr().out
    assert "(line 97.56% < 100.00%; branch 50.00% < 100.00%)" in out
    assert "coverage 9" not in out  # 41/43 = 95.34% passes the combined bar
    assert repo.main("--min-python-line", "97", "--min-python-branch", "50") == 0


def test_sources_missing_from_the_reports_fail(repo: Repo, capsys: pytest.CaptureFixture[str]) -> None:
    repo.write("src/pkg/new.py", "\n")
    repo.write("bridge/y.c", "\n")
    repo.write("bridge/y.h", "\n")  # headers are scored only when a report lists them
    assert repo.main() == 1
    out = capsys.readouterr().out
    assert "2 measured file(s) missing from the reports (SonarQube counts them uncovered):" in out
    assert "  bridge/y.c\n  src/pkg/new.py\n" in out
    assert "bridge/y.h" not in out


def test_stale_report_entries_fail(repo: Repo, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    outside = tmp_path / "elsewhere.py"
    outside.write_text("\n", encoding="utf-8")
    repo.python({**FULL_PY, "gone.py": [(1, 1, None)], str(outside): [(1, 1, None)]})
    assert repo.main() == 1
    out = capsys.readouterr().out
    assert "2 stale report entr(y/ies): the file does not exist (regenerate the reports):" in out
    assert f"  {outside.as_posix()}\n" in out
    assert "  src/pkg/gone.py\n" in out


def test_reports_that_measure_nothing_fail(repo: Repo, capsys: pytest.CaptureFixture[str]) -> None:
    for rel in ("src/pkg/a.py", "src/pkg/sub/b.py", "bridge/x.c"):
        (repo.root / rel).unlink()
    repo.python({})
    repo.c({})
    assert repo.main() == 1
    out = capsys.readouterr().out
    assert "the reports measure no file SonarQube analyses" in out
    summary = "check-coverage: FAIL, 0 files measured (0 Python, 0 C), 0 report entries out of scope"
    assert out.rstrip().endswith(summary)


def test_files_sonarqube_does_not_measure_are_skipped(repo: Repo, capsys: pytest.CaptureFixture[str]) -> None:
    # gen/ is under sonar.coverage.exclusions, bridge/tests/ under sonar.exclusions: their
    # entries are neither scored nor stale, and nobody expects them in a report.
    repo.python({**FULL_PY, "gen/g.py": [(1, 0, None)]})
    repo.c({"bridge/x.c": C_AT_BAR, "bridge/tests/t.c": [(1, False, 0, 0)]})
    assert repo.main() == 0
    assert "3 files measured (2 Python, 1 C), 2 report entries out of scope" in capsys.readouterr().out


def test_verbose_lists_every_measured_file_lowest_first(repo: Repo, capsys: pytest.CaptureFixture[str]) -> None:
    assert repo.main("-v") == 0
    out = capsys.readouterr().out.splitlines()
    start = out.index("every measured file, lowest first:")
    rows = [line.split()[-1] for line in out[start + 2 : start + 5]]
    assert rows == ["bridge/x.c", "src/pkg/a.py", "src/pkg/sub/b.py"]


def test_floor_comparison_is_exact(repo: Repo, capsys: pytest.CaptureFixture[str]) -> None:
    # 8999 of 10000 lines: 89.99% never rounds up to the 90% floor.
    repo.c({"bridge/x.c": [(n, n <= 8999, 0, 0) for n in range(1, 10001)]})
    assert repo.main() == 1
    assert "(coverage 89.99% < 90.00%)" in capsys.readouterr().out
    assert repo.main("--min", "89.99") == 0
    assert cc.pct_text(Fraction(179993, 2000)) == "89.99%"  # 89.9965 truncates


def test_input_errors_exit_two(repo: Repo, capsys: pytest.CaptureFixture[str]) -> None:
    (repo.root / "out" / "c-coverage.xml").unlink()
    assert repo.main() == 2
    assert "check-coverage: error: cannot read " in capsys.readouterr().err
    assert repo.main("--properties", str(repo.root / "nope.properties")) == 2
    assert "nope.properties" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("which", "text", "message"),
    [
        ("c", "<coverage version='1'>", "is not well-formed XML"),
        ("c", "<report/>", "the root element is <report>, expected <coverage>"),
        ("c", "<coverage version='2'/>", "expected version='1'"),
        ("c", "<coverage version='1'><file/></coverage>", "a <file> has no 'path'"),
        ("c", "<coverage version='1'><file path='bridge/x.c'><lineToCover covered='true'/></file></coverage>",
         "needs a non-negative integer 'lineNumber', got None"),
        ("c", "<coverage version='1'><file path='bridge/x.c'><lineToCover lineNumber='-1' covered='true'/>"
              "</file></coverage>", "got '-1'"),
        ("c", "<coverage version='1'><file path='bridge/x.c'><lineToCover lineNumber='1' covered='yes'/>"
              "</file></coverage>", "needs covered='true' or 'false', got 'yes'"),
        ("c", "<coverage version='1'><file path='bridge/x.c'><lineToCover lineNumber='1' covered='true' "
              "branchesToCover='2'/></file></coverage>", "'coveredBranches', got None"),
        ("c", "<coverage version='1'><file path='bridge/x.c'><lineToCover lineNumber='1' covered='true' "
              "branchesToCover='2' coveredBranches='3'/></file></coverage>", "line 1 covers 3 of 2 branches"),
        ("py", "<coverage><classes><class/></classes></coverage>", "a <class> has no 'filename'"),
        ("py", "<coverage><class filename='a.py'><lines><line number='1' hits='x'/></lines></class></coverage>",
         "'hits', got 'x'"),
        ("py", "<coverage><class filename='a.py'><lines><line number='1' hits='1' branch='true'/></lines>"
               "</class></coverage>", "branch line 1 has no 'condition-coverage'"),
        ("py", "<coverage><class filename='a.py'><lines><line number='1' hits='1' branch='true' "
               "condition-coverage='150% (3/2)'/></lines></class></coverage>", "line 1 covers 3 of 2 conditions"),
    ],
)
def test_malformed_reports_exit_two(
    repo: Repo, capsys: pytest.CaptureFixture[str], which: str, text: str, message: str
) -> None:
    repo.write("out/c-coverage.xml" if which == "c" else "out/coverage.xml", text)
    assert repo.main() == 2
    err = capsys.readouterr().err
    assert err.startswith("check-coverage: error: ")
    assert message in err


@pytest.mark.parametrize(
    ("properties", "message"),
    [
        ("sonar.sources=src\n", "no coverage report: set sonar.python.coverage.reportPaths"),
        ("sonar.exclusions=file:/abs/**\nsonar.coverageReportPaths=out/c-coverage.xml\n",
         "sonar.exclusions: absolute 'file:' patterns are not supported"),
        ("sonar.sources=\\u12\n", "malformed \\uXXXX escape"),
    ],
)
def test_unusable_properties_exit_two(
    repo: Repo, capsys: pytest.CaptureFixture[str], properties: str, message: str
) -> None:
    repo.write("sonar-project.properties", properties)
    assert repo.main() == 2
    assert message in capsys.readouterr().err


@pytest.mark.parametrize("value", ["abc", "1/0", "-1", "100.5"])
def test_floors_must_be_percentages(value: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        cc.parse_args(["--min", value])
    assert exc.value.code == 2
    assert "argument --min" in capsys.readouterr().err


def test_defaults_are_the_ci_gate() -> None:
    args = cc.parse_args([])
    assert (args.min, args.min_python_line, args.min_python_branch) == (90, 100, 100)
    assert args.root == ROOT
    assert args.properties is None
    assert args.python_report is None
    assert args.c_report is None
    assert cc.parse_args(["--min", "92.5"]).min == Fraction(185, 2)


def test_report_paths_default_to_the_properties_and_flags_override(
    repo: Repo, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    moved = tmp_path / "moved.xml"
    shutil.move(repo.root / "out" / "c-coverage.xml", moved)
    assert repo.main() == 2  # the configured path is gone
    capsys.readouterr()
    assert repo.main("--c-report", str(moved)) == 0
    relative = repo.write("other/c.xml", moved.read_text(encoding="utf-8"))
    assert repo.main("--c-report", "other/c.xml") == 0
    relative.unlink()
    # An explicit Python report replaces the configured one; an empty one leaves a.py missing.
    repo.write("other/py.xml", _cobertura(repo.root / "src" / "pkg", {"sub/b.py": [(1, 1, None)]}))
    assert repo.main("--c-report", str(moved), "--python-report", "other/py.xml") == 1
    assert "  src/pkg/a.py\n" in capsys.readouterr().out


def test_runs_as_a_program(repo: Repo, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--root", str(repo.root)])
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(str(SCRIPT), run_name="__main__")
    assert exc.value.code == 0
    assert "check-coverage: OK" in capsys.readouterr().out
    ok = subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(repo.root)], capture_output=True, text=True, check=False
    )
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert "check-coverage: OK" in ok.stdout
    usage = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, check=False)
    assert usage.returncode == 0
    assert "--min-python-branch" in usage.stdout
    assert SCRIPT.stat().st_mode & 0o111
    assert SCRIPT.read_text(encoding="utf-8").startswith("#!/usr/bin/env python3\n")


# ---------------------------------------------------------------------------
# readers
# ---------------------------------------------------------------------------


def test_cobertura_reader_counts_lines_and_conditions(tmp_path: Path) -> None:
    root = tmp_path / "r"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "m.py").write_text("\n", encoding="utf-8")
    (root / "top.py").write_text("\n", encoding="utf-8")
    report = tmp_path / "c.xml"
    report.write_text(
        "<coverage><sources><source>  </source><source>pkg</source></sources>"
        "<class filename='m.py'><methods><method><lines><line number='9' hits='1'/></lines></method></methods>"
        "<lines><line number='1' hits='0'/><line number='2' hits='3' branch='true' condition-coverage='50% (1/2)'/>"
        "</lines></class>"
        "<class filename='m.py'><lines><line number='1' hits='2'/></lines></class>"
        "<class filename='top.py'><lines><line number='1' hits='1'/></lines></class>"
        f"<class filename='{root / 'pkg' / 'm.py'}'><lines><line number='3' hits='0'/></lines></class>"
        "</coverage>",
        encoding="utf-8",
    )
    cov = cc.read_cobertura(report, root)
    assert sorted(cov) == ["pkg/m.py", "top.py"]  # relative source, root fallback, absolute filename
    assert cov["pkg/m.py"].lines == {1: [1, 0, 0], 2: [1, 2, 1], 3: [0, 0, 0]}  # <methods> lines ignored
    # Without <source> entries filenames resolve against the root.
    report.write_text("<coverage><class filename='top.py'><lines/></class></coverage>", encoding="utf-8")
    assert list(cc.read_cobertura(report, root)) == ["top.py"]
    # An unresolvable name keeps the first candidate (a stale entry).
    report.write_text(
        f"<coverage><sources><source>{root / 'pkg'}</source></sources><class filename='x.py'/></coverage>",
        encoding="utf-8",
    )
    assert list(cc.read_cobertura(report, root)) == ["pkg/x.py"]


def test_generic_reader_counts_lines_and_branches(tmp_path: Path) -> None:
    report = tmp_path / "g.xml"
    report.write_text(
        _generic({"bridge/a.c": [(1, True, 0, 0), (2, False, 4, 0)], "./bridge/../bridge/a.c": [(2, True, 4, 3)]}),
        encoding="utf-8",
    )
    cov = cc.read_generic(report, tmp_path)
    assert list(cov) == ["bridge/a.c"]
    assert cov["bridge/a.c"].lines == {1: [1, 0, 0], 2: [1, 4, 3]}
    assert cov["bridge/a.c"].combined == Fraction(100 * 5, 6)


def test_file_coverage_merges_lines_and_counts_like_sonarqube() -> None:
    first, second = cc.FileCoverage(), cc.FileCoverage()
    first.add(1, False, 2, 1)
    second.add(1, True, 4, 0)
    second.add(2, False)
    merged = cc.merge_reports([{"f.c": first}, {"f.c": second, "g.c": cc.FileCoverage()}])
    assert merged["f.c"].lines == {1: [1, 4, 1], 2: [0, 0, 0]}
    assert merged["g.c"].lines == {}
    assert cc.percent(0, 0) == 100


def test_floors_apply_per_language() -> None:
    floors = cc.Floors(Fraction(90), Fraction(100), Fraction(100))
    partial = cc.FileCoverage()
    partial.add(1, True, 2, 1)
    partial.add(2, True)  # lines 2/2, conditions 1/2: (2 + 1) / (2 + 2) = 75%
    assert cc.file_problems("x.c", partial, floors) == ["coverage 75.00% < 90.00%"]
    assert cc.file_problems("x.c", partial, cc.Floors(Fraction(75), Fraction(100), Fraction(100))) == []
    assert cc.file_problems("x.py", partial, cc.Floors(Fraction(0), Fraction(100), Fraction(50))) == []
    assert cc.file_problems("x.py", partial, floors) == ["coverage 75.00% < 90.00%", "branch 50.00% < 100.00%"]


# ---------------------------------------------------------------------------
# sonar-project.properties: Java rules and SonarQube wildcards
# ---------------------------------------------------------------------------


def test_properties_parser_follows_java_rules() -> None:
    text = (
        "# comment\r\n"
        "  ! another comment \\\n"
        "plain=value\n"
        "spaced   =   padded value  \n"
        "colon:x\n"
        "blank key value\n"
        "\tcont = a,\\\n"
        "     b,\\\r"
        "\t c\n"
        "escaped\\=key\\ name = \\u0041\\t\\x\\\\\n"
        "even = ends\\\\\n"
        "next = line\n"
        "form\ffeed=ff\n"
        "only-key\n"
        "plain = again\n"
        "dangling = \\\n"
    )
    props = cc.parse_properties(text)
    assert props == {
        "plain": "again",
        "spaced": "padded value  ",
        "colon": "x",
        "blank": "key value",
        "cont": "a,b,c",
        "escaped=key name": "A\tx\\",
        "even": "ends\\",
        "next": "line",
        "form": "feed=ff",
        "only-key": "",
        "dangling": "",
    }
    assert "another comment" not in str(props)
    assert cc.parse_properties("k=v\\") == {"k": "v"}  # a continuation at the end of the file
    assert cc._unescape("a\\") == "a\\"  # a lone trailing backslash stays


def test_split_list_trims_and_drops_empty_items() -> None:
    assert cc.split_list(" a , ,b,") == ["a", "b"]
    assert cc.split_list("") == []


@pytest.mark.parametrize(
    ("pattern", "path", "expected"),
    [
        ("**/build-*/**", "build-cov/bridge/x.c", True),
        ("**/build-*/**", "bridge/build-x/y/z.c", True),
        ("**/build-*/**", "bridge/rebuild-x/y.c", False),
        ("**/build-*/**", "build-cov", False),
        ("bridge/win/tests/**", "bridge/win/tests/fl_testdrv.c", True),
        ("bridge/win/tests/**", "bridge/win/tests", False),
        ("bridge/win/tests/**", "bridge/win/fl_win.c", False),
        ("scripts/*.py", "scripts/check-coverage.py", True),
        ("scripts/*.py", "scripts/qa/x.py", False),
        ("src/?.py", "src/a.py", True),
        ("src/?.py", "src/ab.py", False),
        ("src/?.py", "src//.py", False),
        ("/docs/**", "docs/a.md", True),
        ("docs/", "docs/sub/a.md", True),
        ("a.b+(c)", "a.b+(c)", True),
        ("a.b", "axb", False),
        ("**", "anything/at/all.c", True),
        ("**.c", "deep/dir/x.c", True),
        ("bridge\\unix\\*.c", "bridge/unix/fl.c", True),
        ("tests/**", "src/tests/x.py", False),
    ],
)
def test_wildcards_match_like_sonarqube(pattern: str, path: str, expected: bool) -> None:
    assert bool(cc.wildcard_regex(pattern).match(path)) is expected


def test_scope_reasons_and_defaults() -> None:
    scope = cc.Scope.from_properties({})
    assert scope.sources == ["."]
    assert (scope.python_reports, scope.generic_reports) == ([], [])
    assert scope.reason("anything/at/all.py") is None
    assert scope.reason("README.md") == "not Python or C"
    scope = cc.Scope.from_properties(
        {
            "sonar.sources": "./src/, lib, one.c",
            "sonar.inclusions": "**/*.c,**/*.h,src/**",
            "sonar.exclusions": "src/skip/**",
            "sonar.coverage.exclusions": "lib/gen.c",
        }
    )
    assert scope.sources == ["src", "lib", "one.c"]
    assert scope.reason("src/a.py") is None
    assert scope.reason("lib/a.c") is None
    assert scope.reason("one.c") is None  # a source given as a file
    assert scope.reason("lib/a.py") == "not in sonar.inclusions"
    assert scope.reason("libx/a.c") == "outside sonar.sources"
    assert scope.reason("src/skip/a.py") == "sonar.exclusions"
    assert scope.reason("lib/gen.c") == "sonar.coverage.exclusions"


def test_repository_files_follow_git(tmp_path: Path) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    root = tmp_path / "g"
    root.mkdir()
    for rel in ("kept.py", "ignored.py", "gone.py", "new.py", ".gitignore"):
        (root / rel).write_text("ignored.py\n" if rel == ".gitignore" else "\n", encoding="utf-8")
    env = ["-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false"]
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "kept.py", "gone.py", ".gitignore"], check=True)
    subprocess.run(["git", "-C", str(root), *env, "commit", "-q", "-m", "x"], check=True)
    (root / "gone.py").unlink()
    assert cc.repository_files(root) == [".gitignore", "kept.py", "new.py"]


def test_repository_files_walk_without_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "w"
    (root / ".git").mkdir(parents=True)
    (root / ".git" / "HEAD").write_text("x\n", encoding="utf-8")
    (root / "sub").mkdir()
    (root / "sub" / "b.c").write_text("\n", encoding="utf-8")
    (root / "a.py").write_text("\n", encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    monkeypatch.setenv("PATH", str(bin_dir))  # no git at all
    assert cc.repository_files(root) == ["a.py", "sub/b.c"]
    git = bin_dir / "git"
    git.write_text("#!/bin/sh\nexit 128\n", encoding="utf-8")
    git.chmod(0o755)  # a git that is not happy with the directory
    assert cc.repository_files(root) == ["a.py", "sub/b.c"]


# ---------------------------------------------------------------------------
# the real repository
# ---------------------------------------------------------------------------


def _real_scope() -> cc.Scope:
    return cc.Scope.from_properties(cc.parse_properties((ROOT / "sonar-project.properties").read_text("utf-8")))


def test_the_real_properties_drive_the_gate() -> None:
    scope = _real_scope()
    assert scope.python_reports == ["artifacts/coverage/coverage.xml"]
    assert scope.generic_reports == ["artifacts/coverage/c-coverage.xml"]
    assert scope.reason("src/fork_linux/cli.py") is None
    assert scope.reason("src/fork_linux/steps/fork.py") is None
    for c_file in ("bridge/common/fl_proto.c", "bridge/unix/fl_bridge_helper.c", "bridge/win/fl_win.c"):
        assert scope.reason(c_file) is None
    assert scope.reason("scripts/check-coverage.py") == "sonar.coverage.exclusions"
    assert scope.reason("libexec/x.py") == "sonar.coverage.exclusions"
    assert scope.reason("scripts/qa/x.py") == "sonar.exclusions"
    assert scope.reason("bridge/win/tests/fl_testdrv.c") == "sonar.exclusions"
    assert scope.reason("scripts/spike/probe.py") == "sonar.exclusions"
    assert scope.reason("bridge/common/build-x/a.c") == "sonar.exclusions"
    assert scope.reason("bridge/spike/probe.c") == "outside sonar.sources"
    assert scope.reason("tests/test_cli.py") == "outside sonar.sources"
    assert scope.reason("bridge/tests/unit/test_proto.c") == "outside sonar.sources"
    assert scope.reason("build-cov/bridge/x.c") == "outside sonar.sources"


def test_the_real_checkout_expects_every_package_module_and_bridge_source() -> None:
    scope = _real_scope()
    measured = {name for name in cc.repository_files(ROOT) if scope.reason(name) is None}
    expected = {p.relative_to(ROOT).as_posix() for p in (ROOT / "src" / "fork_linux").rglob("*.py")}
    expected.discard("src/fork_linux/_build.py")  # generated by Meson, gitignored
    for part in ("common", "unix", "win"):
        expected |= {p.relative_to(ROOT).as_posix() for p in (ROOT / "bridge" / part).glob("*.[ch]")}
    assert measured == expected
    assert any(name.endswith(".c") for name in measured)


# ---------------------------------------------------------------------------
# wiring and docs
# ---------------------------------------------------------------------------


def test_coverage_stage_runs_the_gate_after_the_c_report() -> None:
    text = (ROOT / "scripts" / "ci-docker.sh").read_text(encoding="utf-8")
    tail = text[text.rindex('if [[ "${FL_CI_STAGE}" == "coverage" ]]; then') :]
    c_cov = tail.index("\t\t./scripts/ci-c-coverage.sh\n")
    gate = tail.index(f"\t\t{GATE}\n")
    skipped = tail.index("else")
    assert c_cov < gate < skipped
    assert "-x scripts/ci-c-coverage.sh" not in text  # a missing script fails, it is not skipped


def test_ci_sonar_gates_refreshed_reports_before_scanning() -> None:
    text = (ROOT / "scripts" / "ci-sonar.sh").read_text(encoding="utf-8")
    body = text[text.index("ensure_coverage() {") :]
    refresh = body[: body.index("\n\tfi\n")]
    assert refresh.index('"${ROOT}/scripts/ci-c-coverage.sh"') < refresh.index(
        'python3 "${ROOT}/scripts/check-coverage.py" --min 90 --min-python-line 100 --min-python-branch 100'
    )
    assert 'if [[ "${FL_SONAR_COVERAGE:-0}" == "1" ]]; then' in refresh
    calls = text[text.index("\nload_token\n") :]
    assert calls.index("ensure_coverage") < calls.index("run_scanner")


def test_check_workflow_gates_before_the_sonar_scan() -> None:
    text = (ROOT / ".github" / "workflows" / "check.yml").read_text(encoding="utf-8")
    job = text[text.index("\n  coverage:\n") : text.index("\n  bridge:\n")]
    step = job[job.index("- name: Per-file coverage gate") :]
    step = step[: step.index("\n\n")]
    assert f"run: {GATE}" in step
    assert "if:" not in step  # fork PRs are gated too
    assert job.index("run: ./scripts/ci-c-coverage.sh") < job.index("- name: Per-file coverage gate")
    assert job.index("- name: Per-file coverage gate") < job.index("uses: SonarSource/sonarqube-scan-action@")


def test_agents_md_states_the_per_file_bar() -> None:
    text = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    phrase = "every file >= 90%, enforced by scripts/check-coverage.py"
    section_45 = text[text.index("### 4.5 ") : text.index("### 4.6 ")]
    section_48 = text[text.index("### 4.8 ") : text.index("### 4.9 ")]
    assert phrase in section_45
    assert phrase in section_48


@pytest.mark.parametrize("skill", ["test-runner", "pipeline-runner"])
def test_runbooks_name_the_gate(skill: str) -> None:
    text = (ROOT / ".agents" / "skills" / skill / "SKILL.md").read_text(encoding="utf-8")
    assert "scripts/check-coverage.py" in text
    assert "90%" in text
