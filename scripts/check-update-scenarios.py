#!/usr/bin/env python3
"""Check the update scenario matrix (docs/qa/UPDATE-SCENARIOS.md) against the tests.

Every table row whose first cell is a scenario ID (``F<n>`` or ``A<n>``) has the columns
ID | Scenario | Tiers | Expected result | Covered by | Planned | Status. The checks:

* the required IDs (F1-F11, A1-A12) are present and no ID appears twice;
* tiers are ``U`` (unit / integration with fakes), ``P`` (packaging container E2E) or
  ``R`` (real Fork + Wine E2E in Docker), comma separated;
* the status is ``covered``, ``partial`` or ``planned`` and matches the columns: covered
  has tests and nothing planned, planned has no tests, partial has both;
* every test under "Covered by" exists (``tests/x.py::test_name`` is a ``def test_name``,
  ``scripts/x.sh::name`` a shell function, a bare path a file) and every test under
  "Planned" does not exist yet (move it to "Covered by" when it does).

``--strict`` also fails while any scenario is not covered. Exit status: 0 clean, 1 problems.

Usage: check-update-scenarios.py [--strict] [--root DIR] [MATRIX]
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import NamedTuple

ROOT = Path(__file__).resolve().parent.parent
MATRIX = Path("docs/qa/UPDATE-SCENARIOS.md")
REQUIRED = tuple(f"F{n}" for n in range(1, 12)) + tuple(f"A{n}" for n in range(1, 13))
TIERS = {"U", "P", "R"}
STATUSES = ("covered", "partial", "planned")
COLUMNS = 7
ROW = re.compile(r"^\|\s*([FA]\d+)\s*\|")
CODE = re.compile(r"`([^`]+)`")
REF = re.compile(r"(?P<path>(?:tests|scripts)/[\w./-]+)?(?:::(?P<name>\w+)(?:\[[^\]]*\])?)?")
NONE = "—"


class Scenario(NamedTuple):
    """One row of the matrix."""

    id: str
    tiers: list[str]
    covered: list[str]
    planned: list[str]
    status: str
    line: int


def _cells(line: str) -> list[str]:
    """The cells of a Markdown table row (``\\|`` is a literal pipe)."""
    body = line.strip().strip("|").replace("\\|", "\0")
    return [cell.strip().replace("\0", "|") for cell in body.split("|")]


def _refs(cell: str) -> list[str]:
    """The test references in a cell: backticked items, ``::name`` continuing the previous file."""
    refs: list[str] = []
    current = ""
    for item in CODE.findall(cell):
        if item.startswith("::") and current:
            refs.append(current + item)
        else:
            refs.append(item)
            current = item.split("::", 1)[0]
    return refs


def parse(text: str) -> list[Scenario]:
    """Every scenario row of ``text``; a row with the wrong number of cells raises ValueError."""
    scenarios = []
    for number, line in enumerate(text.splitlines(), start=1):
        match = ROW.match(line)
        if match is None:
            continue
        cells = _cells(line)
        if len(cells) != COLUMNS:
            raise ValueError(f"line {number}: {match.group(1)} has {len(cells)} cells, expected {COLUMNS}")
        scenarios.append(
            Scenario(
                id=cells[0],
                tiers=[tier.strip() for tier in cells[2].split(",")],
                covered=_refs(cells[4]),
                planned=_refs(cells[5]),
                status=cells[6],
                line=number,
            )
        )
    return scenarios


def _defines(path: Path, name: str) -> bool:
    """True when ``path`` defines the Python function or shell function ``name``."""
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".py":
        return re.search(rf"^def {re.escape(name)}\(", text, re.MULTILINE) is not None
    return re.search(rf"^{re.escape(name)}\(\)", text, re.MULTILINE) is not None


def exists(root: Path, ref: str) -> bool:
    """True when the test (or script / function) ``ref`` exists under ``root``; ValueError if malformed."""
    match = REF.fullmatch(ref)
    if match is None or match.group("path") is None:
        raise ValueError(f"not a test reference: {ref!r}")
    path = root / match.group("path")
    if not path.is_file():
        return False
    name = match.group("name")
    return name is None or _defines(path, name)


def _status_problems(scenario: Scenario) -> list[str]:
    """Problems with the status column of one row."""
    if scenario.status not in STATUSES:
        return [f"status {scenario.status!r} is not one of {', '.join(STATUSES)}"]
    expected = {
        "covered": bool(scenario.covered) and not scenario.planned,
        "partial": bool(scenario.covered) and bool(scenario.planned),
        "planned": not scenario.covered and bool(scenario.planned),
    }
    if expected[scenario.status]:
        return []
    return [f"status {scenario.status} does not match its columns ({len(scenario.covered)} covered, "
            f"{len(scenario.planned)} planned)"]


def _ref_problems(root: Path, scenario: Scenario) -> list[str]:
    """Covered tests that are missing, planned tests that already exist, malformed references."""
    problems = []
    for ref, want in [(ref, True) for ref in scenario.covered] + [(ref, False) for ref in scenario.planned]:
        try:
            found = exists(root, ref)
        except ValueError as exc:
            problems.append(str(exc))
            continue
        if want and not found:
            problems.append(f"covered test does not exist: {ref}")
        elif not want and found:
            problems.append(f"planned test already exists (move it to 'Covered by'): {ref}")
    return problems


def check(root: Path, scenarios: list[Scenario], *, strict: bool = False) -> list[str]:
    """Every problem with the matrix, as ``ID (line N): message`` lines."""
    problems = []
    seen: set[str] = set()
    for scenario in scenarios:
        found = []
        if scenario.id in seen:
            found.append("duplicate ID")
        seen.add(scenario.id)
        unknown = [tier for tier in scenario.tiers if tier not in TIERS]
        if unknown:
            found.append(f"unknown tier(s) {', '.join(unknown)} (U, P or R)")
        found += _status_problems(scenario)
        found += _ref_problems(root, scenario)
        if strict and scenario.status != "covered":
            found.append(f"not covered yet ({scenario.status})")
        problems += [f"{scenario.id} (line {scenario.line}): {message}" for message in found]
    problems += [f"{missing}: required scenario is missing" for missing in REQUIRED if missing not in seen]
    return problems


def main(argv: list[str] | None = None) -> int:
    """Command line entry point; returns the exit status."""
    parser = argparse.ArgumentParser(description="Check the update scenario matrix against the tests.")
    parser.add_argument("matrix", nargs="?", type=Path, help=f"the matrix (default: {MATRIX})")
    parser.add_argument("--root", type=Path, default=ROOT, help="the repository the references are relative to")
    parser.add_argument("--strict", action="store_true", help="also fail while a scenario is not covered")
    args = parser.parse_args(argv)
    matrix = args.matrix or args.root / MATRIX
    try:
        scenarios = parse(matrix.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    problems = check(args.root, scenarios, strict=args.strict)
    for problem in problems:
        print(f"error: {problem}", file=sys.stderr)
    counts = {status: sum(s.status == status for s in scenarios) for status in STATUSES}
    summary = ", ".join(f"{counts[status]} {status}" for status in STATUSES)
    print(f"{len(scenarios)} scenarios ({summary}); {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
