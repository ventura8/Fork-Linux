#!/usr/bin/env python3
"""Convert gcov data of the C bridge into a SonarQube generic coverage report.

usage: scripts/gcov-sonar.py --root DIR --out FILE --gcov TOOL=GLOB [--gcov ...]
                             [--include PREFIX ...] [--exclude PREFIX ...]

For every ``TOOL=GLOB`` pair, each ``.gcda`` file matching GLOB (relative to the
current directory, ``**`` allowed) is read with
``TOOL --json-format --stdout`` (``gcov`` for the native objects,
``x86_64-w64-mingw32-gcov`` for the PE shims: the data format follows the compiler that
wrote it). Lines and branches of the same source seen in several objects (the unit
tests, the daemon, both PEs) are merged: a line is covered if any object covered it; a
line's branches are the widest set any object reports, covered branch by branch when
the shapes agree. Paths are written relative to ``--root`` (what
sonar.coverageReportPaths expects); sources outside ``--include`` (default
``bridge/``) or under ``--exclude`` are dropped. Python 3.10, standard library only.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class LineCov:
    """Merged coverage of one source line."""

    hits: int = 0
    branches: list[bool] = field(default_factory=list)

    def merge(self, hits: int, branches: list[bool]) -> None:
        self.hits += hits
        if len(branches) == len(self.branches):
            self.branches = [a or b for a, b in zip(self.branches, branches, strict=True)]
        elif sum(branches) > sum(self.branches) or len(branches) > len(self.branches):
            self.branches = list(branches)


Report = dict[str, dict[int, LineCov]]


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, required=True, help="repository root")
    parser.add_argument("--out", type=Path, required=True, help="report to write")
    parser.add_argument(
        "--gcov", action="append", default=[], metavar="TOOL=GLOB", help="gcov tool and the .gcda files it reads"
    )
    parser.add_argument("--include", action="append", default=[], help="keep sources under this prefix")
    parser.add_argument("--exclude", action="append", default=[], help="drop sources under this prefix")
    return parser.parse_args(argv)


def resolve_source(root: Path, cwd: str, name: str) -> str | None:
    """The repo-relative path of a gcov source name, or None when it is not in the repo."""
    candidates = [Path(name)] if Path(name).is_absolute() else [Path(cwd) / name, root / name]
    for candidate in candidates:
        try:
            rel = candidate.resolve().relative_to(root.resolve())
        except ValueError:
            continue
        if (root / rel).is_file():
            return rel.as_posix()
    return None


def wanted(rel: str, include: list[str], exclude: list[str]) -> bool:
    if not any(rel.startswith(prefix) for prefix in include):
        return False
    return not any(rel.startswith(prefix) for prefix in exclude)


def add_document(report: Report, doc: dict, root: Path, include: list[str], exclude: list[str]) -> None:
    """Merge one gcov JSON document (one .gcda) into the report."""
    cwd = doc.get("current_working_directory", str(root))
    for entry in doc.get("files", []):
        rel = resolve_source(root, cwd, entry["file"])
        if rel is None or not wanted(rel, include, exclude):
            continue
        lines = report.setdefault(rel, {})
        for line in entry.get("lines", []):
            branches = [b["count"] > 0 for b in line.get("branches", []) if not b.get("throw")]
            lines.setdefault(line["line_number"], LineCov()).merge(line["count"], branches)


def run_gcov(tool: str, gcda: Path) -> list[dict]:
    """gcov's JSON documents for one .gcda (one per line of output)."""
    proc = subprocess.run(
        [tool, "--json-format", "--stdout", "--branch-probabilities", gcda.name],
        cwd=gcda.parent,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"{tool} failed on {gcda}: {proc.stderr.strip()}")
    return [json.loads(chunk) for chunk in proc.stdout.splitlines() if chunk.strip()]


def write_report(report: Report, out: Path) -> None:
    top = ET.Element("coverage", version="1")
    for rel in sorted(report):
        node = ET.SubElement(top, "file", path=rel)
        for number in sorted(report[rel]):
            cov = report[rel][number]
            attrs = {"lineNumber": str(number), "covered": "true" if cov.hits > 0 else "false"}
            if cov.branches:
                attrs["branchesToCover"] = str(len(cov.branches))
                attrs["coveredBranches"] = str(sum(cov.branches))
            ET.SubElement(node, "lineToCover", attrs)
    out.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(top).write(out, encoding="utf-8", xml_declaration=True)


def summary(report: Report) -> str:
    """One row per file: Sonar's coverage ((lines + branches covered) / (lines + branches))."""
    rows = []
    for rel in sorted(report):
        lines = report[rel].values()
        covered = sum(1 for cov in lines if cov.hits > 0)
        branches = sum(len(cov.branches) for cov in lines)
        taken = sum(sum(cov.branches) for cov in lines)
        total = len(report[rel]) + branches
        pct = 100.0 * (covered + taken) / total if total else 100.0
        rows.append(f"{pct:6.1f}%  lines {covered:5d}/{len(report[rel]):<5d} branches {taken:5d}/{branches:<5d} {rel}")
    return "\n".join(rows)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    include = args.include or ["bridge/"]
    report: Report = {}
    for spec in args.gcov:
        tool, sep, pattern = spec.partition("=")
        if not sep:
            print(f"gcov-sonar: --gcov wants TOOL=GLOB, got {spec!r}", file=sys.stderr)
            return 2
        for gcda in sorted(Path().glob(pattern)):
            for doc in run_gcov(tool, gcda):
                add_document(report, doc, args.root, include, args.exclude)
    write_report(report, args.out)
    print(summary(report))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
