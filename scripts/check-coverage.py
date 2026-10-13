#!/usr/bin/env python3
"""Per-file coverage gate: every source file SonarQube measures must reach the bar.

usage: scripts/check-coverage.py [--root DIR] [--properties FILE]
                                 [--python-report FILE ...] [--c-report FILE ...]
                                 [--min PCT] [--min-python-line PCT]
                                 [--min-python-branch PCT] [--verbose]

Reads the Python Cobertura report (artifacts/coverage/coverage.xml, written by the
coverage stage) and the C bridge's SonarQube generic report
(artifacts/coverage/c-coverage.xml, written by scripts/ci-c-coverage.sh) and scores every
file the way SonarQube does: (covered lines + covered conditions) / (lines + conditions).
A file below --min (default 90) fails the gate. Python files must also reach
--min-python-line and --min-python-branch (default 100 each: today's Python gate).

The scope is read from sonar-project.properties itself (Java .properties rules, SonarQube
wildcard matching): sonar.sources, sonar.inclusions, sonar.exclusions and
sonar.coverage.exclusions, plus both report paths, so the gate can never drift from
SonarQube's own view. Like the scanner, files git ignores are not analysed.

The gate also fails when a measured Python or C file is missing from the reports (SonarQube
would count it as uncovered) and on stale report entries (files that no longer exist).

Exit status: 0 every file passes, 1 the gate failed (a table of offenders is printed),
2 bad input (a missing or malformed report or properties file, a bad option).
Python 3.10, standard library only.
"""

from __future__ import annotations

import argparse
import math
import os
import posixpath
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Languages SonarQube measures coverage for here, by suffix. Every measured .py and .c file
# must be in a report; a header is scored only when a report lists it (declarations alone
# have no executable lines).
LANGUAGES = {".py": "python", ".c": "c", ".h": "c"}
REQUIRED_SUFFIXES = (".py", ".c")
# Path-list keys in sonar-project.properties that must never carry absolute ``file:`` patterns.
SONAR_PATH_KEYS = ("sonar.inclusions", "sonar.exclusions", "sonar.coverage.exclusions")

PYTHON_REPORTS = "sonar.python.coverage.reportPaths"
GENERIC_REPORTS = "sonar.coverageReportPaths"

_CONDITIONS = re.compile(r"\((\d+)/(\d+)\)")
_ESCAPES = {"t": "\t", "n": "\n", "r": "\r", "f": "\f"}
# Whitespace of the .properties format (space, tab, form feed); line breaks end a line.
_BLANKS = " \t\f"
# Characters WildcardPattern escapes before building its regular expression.
_REGEX_SPECIAL = "()[]^$.{}+|"


class InputError(Exception):
    """A report, the properties file or an option cannot be used (exit status 2)."""


# ---------------------------------------------------------------------------
# sonar-project.properties: java.util.Properties.load() rules
# ---------------------------------------------------------------------------


def _continues(line: str) -> bool:
    """True when a physical line ends with an odd number of backslashes."""
    return (len(line) - len(line.rstrip("\\"))) % 2 == 1


def _logical_lines(text: str) -> list[str]:
    """Join continuation lines; drop blank and comment lines (Properties.load)."""
    result: list[str] = []
    pending: str | None = None
    for raw in re.split(r"\r\n|\r|\n", text):
        stripped = raw.lstrip(_BLANKS)
        if pending is None:
            if not stripped or stripped[0] in "#!":
                continue
            pending = ""
        if _continues(stripped):
            pending += stripped[:-1]
            continue
        result.append(pending + stripped)
        pending = None
    if pending is not None:
        result.append(pending)
    return result


def _unescape(text: str) -> str:
    """Resolve \\t, \\n, \\r, \\f, \\uXXXX and \\<char> escapes."""
    out: list[str] = []
    index = 0
    while index < len(text):
        char = text[index]
        index += 1
        if char != "\\" or index == len(text):
            out.append(char)
            continue
        char = text[index]
        index += 1
        if char == "u":
            digits = text[index : index + 4]
            if not re.fullmatch(r"[0-9a-fA-F]{4}", digits):
                raise InputError(f"malformed \\uXXXX escape in {text!r}")
            out.append(chr(int(digits, 16)))
            index += 4
        else:
            out.append(_ESCAPES.get(char, char))
    return "".join(out)


def _split_key_value(line: str) -> tuple[str, str]:
    """Split one logical line at the first unescaped '=', ':' or whitespace."""
    index = 0
    while index < len(line) and line[index] not in "=:" + _BLANKS:
        index += 2 if line[index] == "\\" else 1
    key = line[:index]
    rest = line[index:].lstrip(_BLANKS)
    if rest[:1] in ("=", ":"):
        rest = rest[1:].lstrip(_BLANKS)
    return _unescape(key), _unescape(rest)


def parse_properties(text: str) -> dict[str, str]:
    """Parse a .properties document; a later key replaces an earlier one, as in Java."""
    return dict(_split_key_value(line) for line in _logical_lines(text))


def split_list(value: str) -> list[str]:
    """A comma-separated property value, trimmed, without empty items."""
    return [item.strip() for item in value.split(",") if item.strip()]


# ---------------------------------------------------------------------------
# SonarQube WildcardPattern (Ant-style: *, **, ?)
# ---------------------------------------------------------------------------


def wildcard_regex(pattern: str) -> re.Pattern[str]:
    """Compile a SonarQube path pattern to the regular expression WildcardPattern builds.

    '*' matches within one path segment, '**' any number of segments, '?' one character
    other than '/'. A leading '/' is ignored and a trailing '/' means '/**' (Ant's rule).
    """
    pattern = pattern.strip().replace("\\", "/")
    if pattern.endswith("/"):
        pattern += "**"
    index = 1 if pattern.startswith("/") else 0
    parts = ["^"]
    while index < len(pattern):
        char = pattern[index]
        if pattern.startswith("**/", index):
            parts.append("(?:.*/|)")
            index += 3
        elif pattern.startswith("**", index):
            parts.append(".*")
            index += 2
        else:
            parts.append(_wildcard_char(char))
            index += 1
    parts.append("$")
    return re.compile("".join(parts))


def _wildcard_char(char: str) -> str:
    """The regular expression for one character of a pattern (not part of '**')."""
    if char == "*":
        return "[^/]*?"
    if char == "?":
        return "[^/]"
    if char in _REGEX_SPECIAL:
        return "\\" + char
    return char


def matches_any(path: str, patterns: list[re.Pattern[str]]) -> bool:
    """True when the repository-relative path matches one of the patterns."""
    return any(pattern.match(path) for pattern in patterns)


# ---------------------------------------------------------------------------
# Scope: which files SonarQube measures
# ---------------------------------------------------------------------------


@dataclass
class Scope:
    """The analysis scope from sonar-project.properties."""

    sources: list[str]
    inclusions: list[re.Pattern[str]]
    exclusions: list[re.Pattern[str]]
    coverage_exclusions: list[re.Pattern[str]]
    python_reports: list[str]
    generic_reports: list[str]

    @classmethod
    def from_properties(cls, props: dict[str, str]) -> Scope:
        """Build the scope; unset keys take SonarQube's defaults (sources '.', no patterns)."""

        def patterns(key: str) -> list[re.Pattern[str]]:
            return [wildcard_regex(item) for item in split_list(props.get(key, ""))]

        for key in SONAR_PATH_KEYS:
            if any(item.startswith("file:") for item in split_list(props.get(key, ""))):
                raise InputError(f"{key}: absolute 'file:' patterns are not supported")
        return cls(
            sources=[posixpath.normpath(item) for item in split_list(props.get("sonar.sources", "."))],
            inclusions=patterns("sonar.inclusions"),
            exclusions=patterns("sonar.exclusions"),
            coverage_exclusions=patterns("sonar.coverage.exclusions"),
            python_reports=split_list(props.get(PYTHON_REPORTS, "")),
            generic_reports=split_list(props.get(GENERIC_REPORTS, "")),
        )

    def in_sources(self, path: str) -> bool:
        """True when the path lies in (or is) one of sonar.sources."""
        return any(src == "." or path == src or path.startswith(src + "/") for src in self.sources)

    def reason(self, path: str) -> str | None:
        """Why SonarQube does not measure this path's coverage, or None when it does."""
        if Path(path).suffix not in LANGUAGES:
            return "not Python or C"
        if not self.in_sources(path):
            return "outside sonar.sources"
        if self.inclusions and not matches_any(path, self.inclusions):
            return "not in sonar.inclusions"
        if matches_any(path, self.exclusions):
            return "sonar.exclusions"
        if matches_any(path, self.coverage_exclusions):
            return "sonar.coverage.exclusions"
        return None


def repository_files(root: Path) -> list[str]:
    """Files the scanner indexes: git's tracked + untracked-not-ignored, else every file."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            capture_output=True,
            check=False,
        )
    except OSError:
        proc = None
    if proc is not None and proc.returncode == 0:
        names = {name for name in proc.stdout.decode("utf-8", "surrogateescape").split("\0") if name}
        return sorted(name for name in names if (root / name).is_file())
    return _walk(root)


def _walk(root: Path) -> list[str]:
    """Every regular file below root, without .git (the scanner's view without git)."""
    found: list[str] = []
    for current, dirs, files in os.walk(root):
        dirs[:] = sorted(name for name in dirs if name != ".git")
        base = Path(current).relative_to(root)
        found.extend((base / name).as_posix() for name in files)
    return sorted(found)


# ---------------------------------------------------------------------------
# Coverage reports
# ---------------------------------------------------------------------------


@dataclass
class FileCoverage:
    """Merged coverage of one file: line -> [covered, conditions, covered conditions]."""

    lines: dict[int, list[int]] = field(default_factory=dict)

    def add(self, number: int, covered: bool, conditions: int = 0, taken: int = 0) -> None:
        """Merge one line: covered by any report, the widest condition count wins."""
        entry = self.lines.setdefault(number, [0, 0, 0])
        entry[0] = max(entry[0], int(covered))
        entry[1] = max(entry[1], conditions)
        entry[2] = max(entry[2], taken)

    def merge(self, other: FileCoverage) -> None:
        """Merge another report's coverage of the same file."""
        for number, (covered, conditions, taken) in other.lines.items():
            self.add(number, bool(covered), conditions, taken)

    @property
    def line_counts(self) -> tuple[int, int]:
        """(covered lines, lines)."""
        return sum(entry[0] for entry in self.lines.values()), len(self.lines)

    @property
    def condition_counts(self) -> tuple[int, int]:
        """(covered conditions, conditions)."""
        return sum(entry[2] for entry in self.lines.values()), sum(entry[1] for entry in self.lines.values())

    @property
    def combined(self) -> Fraction:
        """SonarQube's coverage: (covered lines + covered conditions) / (lines + conditions)."""
        (lines_hit, lines), (conds_hit, conds) = self.line_counts, self.condition_counts
        return percent(lines_hit + conds_hit, lines + conds)


Coverage = dict[str, FileCoverage]


def percent(part: int, whole: int) -> Fraction:
    """An exact percentage; nothing to cover counts as fully covered."""
    return Fraction(100 * part, whole) if whole else Fraction(100)


def _int_attr(node: ET.Element, name: str, where: str) -> int:
    value = node.get(name)
    if value is None or not value.isdigit():
        raise InputError(f"{where}: <{node.tag}> needs a non-negative integer '{name}', got {value!r}")
    return int(value)


def _load_xml(path: Path) -> ET.Element:
    try:
        top = ET.parse(path).getroot()
    except OSError as exc:
        raise InputError(f"cannot read {path}: {exc.strerror or exc}") from exc
    except ET.ParseError as exc:
        raise InputError(f"{path} is not well-formed XML: {exc}") from exc
    if top.tag != "coverage":
        raise InputError(f"{path}: the root element is <{top.tag}>, expected <coverage>")
    return top


def _report_key(candidate: Path, root: Path) -> str:
    """Repository-relative posix path, or the absolute path when it lies outside root."""
    resolved = Path(os.path.normpath(candidate))
    try:
        return resolved.relative_to(Path(os.path.normpath(root))).as_posix()
    except ValueError:
        return resolved.as_posix()


def _cobertura_file(filename: str, sources: list[Path], root: Path) -> str:
    """Resolve a Cobertura class filename against <source> entries, then the root."""
    candidates = [Path(filename)] if Path(filename).is_absolute() else [src / filename for src in sources]
    candidates.append(root / filename)
    existing = [candidate for candidate in candidates if candidate.is_file()]
    return _report_key((existing or candidates)[0], root)


def _cobertura_line(cov: FileCoverage, line: ET.Element, where: str) -> None:
    number, hits = _int_attr(line, "number", where), _int_attr(line, "hits", where)
    if line.get("branch") != "true":
        cov.add(number, hits > 0)
        return
    match = _CONDITIONS.search(line.get("condition-coverage", ""))
    if match is None:
        raise InputError(f"{where}: branch line {number} has no 'condition-coverage' like '50% (1/2)'")
    taken, conditions = int(match.group(1)), int(match.group(2))
    if taken > conditions:
        raise InputError(f"{where}: line {number} covers {taken} of {conditions} conditions")
    cov.add(number, hits > 0, conditions, taken)


def read_cobertura(path: Path, root: Path) -> Coverage:
    """Read a Cobertura XML report (coverage.py's --cov-report=xml)."""
    top = _load_xml(path)
    sources = [Path(node.text.strip()) for node in top.iter("source") if node.text and node.text.strip()]
    if not sources:
        sources = [root]
    sources = [src if src.is_absolute() else root / src for src in sources]
    report: Coverage = {}
    for cls in top.iter("class"):
        filename = cls.get("filename")
        if not filename:
            raise InputError(f"{path}: a <class> has no 'filename'")
        cov = report.setdefault(_cobertura_file(filename, sources, root), FileCoverage())
        for line in cls.findall("./lines/line"):
            _cobertura_line(cov, line, f"{path} ({filename})")
    return report


def _generic_line(cov: FileCoverage, node: ET.Element, where: str) -> None:
    number = _int_attr(node, "lineNumber", where)
    covered = node.get("covered")
    if covered not in ("true", "false"):
        raise InputError(f"{where}: line {number} needs covered='true' or 'false', got {covered!r}")
    if node.get("branchesToCover") is None:
        cov.add(number, covered == "true")
        return
    conditions = _int_attr(node, "branchesToCover", where)
    taken = _int_attr(node, "coveredBranches", where)
    if taken > conditions:
        raise InputError(f"{where}: line {number} covers {taken} of {conditions} branches")
    cov.add(number, covered == "true", conditions, taken)


def read_generic(path: Path, root: Path) -> Coverage:
    """Read a SonarQube generic coverage report (scripts/gcov-sonar.py)."""
    top = _load_xml(path)
    if top.get("version") != "1":
        raise InputError(f"{path}: <coverage version={top.get('version')!r}>, expected version='1'")
    report: Coverage = {}
    for node in top.findall("file"):
        name = node.get("path")
        if not name:
            raise InputError(f"{path}: a <file> has no 'path'")
        cov = report.setdefault(_report_key(root / name, root), FileCoverage())
        for line in node.findall("lineToCover"):
            _generic_line(cov, line, f"{path} ({name})")
    return report


def merge_reports(reports: list[Coverage]) -> Coverage:
    """One coverage per file across every report."""
    merged: Coverage = {}
    for report in reports:
        for name, cov in report.items():
            merged.setdefault(name, FileCoverage()).merge(cov)
    return merged


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


@dataclass
class Floors:
    """The bars: combined for every file, line and branch on top for Python."""

    combined: Fraction
    python_line: Fraction
    python_branch: Fraction


@dataclass
class Result:
    """What the gate found."""

    scored: dict[str, FileCoverage] = field(default_factory=dict)
    offenders: dict[str, list[str]] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)
    stale: list[str] = field(default_factory=list)
    skipped: int = 0

    @property
    def failed(self) -> bool:
        """True when any file fails, any is missing or stale, or nothing was measured."""
        return bool(self.offenders or self.missing or self.stale or not self.scored)


def pct_text(value: Fraction) -> str:
    """A percentage truncated (never rounded up) to two decimals."""
    return f"{math.floor(value * 100) / 100:.2f}%"


def file_problems(name: str, cov: FileCoverage, floors: Floors) -> list[str]:
    """Every bar this file misses (empty when it passes)."""
    problems = []
    if cov.combined < floors.combined:
        problems.append(f"coverage {pct_text(cov.combined)} < {pct_text(floors.combined)}")
    if Path(name).suffix == ".py":
        line = percent(*cov.line_counts)
        branch = percent(*cov.condition_counts)
        if line < floors.python_line:
            problems.append(f"line {pct_text(line)} < {pct_text(floors.python_line)}")
        if branch < floors.python_branch:
            problems.append(f"branch {pct_text(branch)} < {pct_text(floors.python_branch)}")
    return problems


def evaluate(root: Path, measured: set[str], coverage: Coverage, floors: Floors) -> Result:
    """Score every measured file; collect offenders, missing sources and stale entries."""
    result = Result()
    for name in sorted(coverage):
        if name in measured:
            result.scored[name] = coverage[name]
            problems = file_problems(name, coverage[name], floors)
            if problems:
                result.offenders[name] = problems
        elif Path(name).is_absolute() or not (root / name).is_file():
            result.stale.append(name)
        else:
            result.skipped += 1
    result.missing = sorted(
        name for name in measured if name not in coverage and Path(name).suffix in REQUIRED_SUFFIXES
    )
    return result


def _row(name: str, cov: FileCoverage) -> str:
    lines_hit, lines = cov.line_counts
    conds_hit, conds = cov.condition_counts
    return f"  {pct_text(cov.combined):>8}  {lines_hit:>5}/{lines:<5}  {conds_hit:>5}/{conds:<5}  {name}"


def render(result: Result, verbose: bool) -> str:
    """The report printed after the gate ran."""
    header = "  coverage  lines        conditions   file"
    out: list[str] = []
    if verbose and result.scored:
        out += ["every measured file, lowest first:", header]
        out += [_row(name, cov) for name, cov in sorted(result.scored.items(), key=lambda item: item[1].combined)]
    if result.offenders:
        out += [f"{len(result.offenders)} file(s) below the bar:", header]
        out += [f"{_row(name, result.scored[name])}  ({'; '.join(probs)})" for name, probs in result.offenders.items()]
    if result.missing:
        count = len(result.missing)
        out.append(f"{count} measured file(s) missing from the reports (SonarQube counts them uncovered):")
        out += [f"  {name}" for name in result.missing]
    if result.stale:
        out.append(f"{len(result.stale)} stale report entr(y/ies): the file does not exist (regenerate the reports):")
        out += [f"  {name}" for name in result.stale]
    if not result.scored:
        out.append("the reports measure no file SonarQube analyses")
    out.append(_summary(result))
    return "\n".join(out)


def _summary(result: Result) -> str:
    verdict = "FAIL" if result.failed else "OK"
    python = sum(1 for name in result.scored if name.endswith(".py"))
    text = (
        f"check-coverage: {verdict}, {len(result.scored)} files measured "
        f"({python} Python, {len(result.scored) - python} C), {result.skipped} report entries out of scope"
    )
    if result.scored:
        name, cov = min(result.scored.items(), key=lambda item: (item[1].combined, item[0]))
        text += f"; lowest {pct_text(cov.combined)} {name}"
    return text


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------


def floor_arg(text: str) -> Fraction:
    """An argparse type: a percentage from 0 to 100."""
    try:
        value = Fraction(text)
    except (ValueError, ZeroDivisionError) as exc:
        raise argparse.ArgumentTypeError(f"not a number: {text!r}") from exc
    if not 0 <= value <= 100:
        raise argparse.ArgumentTypeError(f"must be a percentage from 0 to 100, got {text}")
    return value


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="check-coverage.py", description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=ROOT, help="repository root (default: this checkout)")
    parser.add_argument("--properties", type=Path, help="default: <root>/sonar-project.properties")
    parser.add_argument(
        "--python-report", action="append", type=Path, help=f"Cobertura XML (default: {PYTHON_REPORTS})"
    )
    parser.add_argument(
        "--c-report", action="append", type=Path, help=f"SonarQube generic XML (default: {GENERIC_REPORTS})"
    )
    parser.add_argument("--min", type=floor_arg, default=Fraction(90), help="per-file coverage floor (90)")
    parser.add_argument("--min-python-line", type=floor_arg, default=Fraction(100), help="Python line floor (100)")
    parser.add_argument(
        "--min-python-branch", type=floor_arg, default=Fraction(100), help="Python branch floor (100)"
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="list every measured file, lowest first")
    return parser.parse_args(argv)


def _report_paths(given: list[Path] | None, configured: list[str], root: Path) -> list[Path]:
    paths = given if given is not None else [Path(item) for item in configured]
    return [path if path.is_absolute() else root / path for path in paths]


def run(args: argparse.Namespace) -> Result:
    """Load the scope and the reports, then run the gate."""
    root = args.root.resolve()
    properties = args.properties or root / "sonar-project.properties"
    try:
        text = properties.read_text(encoding="utf-8")
    except OSError as exc:
        raise InputError(f"cannot read {properties}: {exc.strerror or exc}") from exc
    scope = Scope.from_properties(parse_properties(text))
    reports = [read_cobertura(path, root) for path in _report_paths(args.python_report, scope.python_reports, root)]
    reports += [read_generic(path, root) for path in _report_paths(args.c_report, scope.generic_reports, root)]
    if not reports:
        raise InputError(f"no coverage report: set {PYTHON_REPORTS} / {GENERIC_REPORTS} or pass one")
    measured = {name for name in repository_files(root) if scope.reason(name) is None}
    floors = Floors(args.min, args.min_python_line, args.min_python_branch)
    return evaluate(root, measured, merge_reports(reports), floors)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    try:
        result = run(args)
    except InputError as exc:
        print(f"check-coverage: error: {exc}", file=sys.stderr)
        return 2
    print(render(result, args.verbose))
    return 1 if result.failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
