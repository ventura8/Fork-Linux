#!/usr/bin/env python3
"""Write the static README badges of Fork for Linux (unofficial) to docs/badges/.

* ``version.svg``  from VERSION (the single source of truth);
* ``coverage.svg`` from the coverage stage's Cobertura report
  (``artifacts/coverage/coverage.xml``; run ``FL_CI_STAGE=coverage ./scripts/ci-docker.sh``
  first), skipped with a note when the report is missing;
* ``python.svg``   the minimum Python (3.10) and ``arch.svg`` (x86_64 only).

Usage: ./scripts/generate-badges.py [--coverage-xml FILE] [--out DIR]
"""

from __future__ import annotations

import argparse
import os
import xml.etree.ElementTree as ET
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parent.parent
CHAR_WIDTH = 7
PADDING = 10


def badge(label: str, value: str, color: str) -> str:
    """A flat shields-style SVG badge (our own drawing, no external service)."""
    left = len(label) * CHAR_WIDTH + PADDING
    right = len(value) * CHAR_WIDTH + PADDING
    width = left + right
    label_x = left / 2
    value_x = left + right / 2
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="20" role="img" '
        f'aria-label="{escape(label)}: {escape(value)}">\n'
        f"  <title>{escape(label)}: {escape(value)}</title>\n"
        f'  <rect width="{left}" height="20" fill="#555"/>\n'
        f'  <rect x="{left}" width="{right}" height="20" fill="{color}"/>\n'
        '  <g fill="#fff" text-anchor="middle" font-family="Verdana,DejaVu Sans,sans-serif" font-size="11">\n'
        f'    <text x="{label_x}" y="14">{escape(label)}</text>\n'
        f'    <text x="{value_x}" y="14">{escape(value)}</text>\n'
        "  </g>\n"
        "</svg>\n"
    )


def coverage_color(percent: float) -> str:
    """Green at the 90% floor and above, yellow below it, red under 75%."""
    if percent >= 90:
        return "#4c1"
    if percent >= 75:
        return "#dfb317"
    return "#e05d44"


def coverage_percent(report: Path) -> float | None:
    """The line-rate of a Cobertura report as a percentage, or None when there is none."""
    if not report.is_file():
        return None
    rate = ET.parse(report).getroot().get("line-rate")
    return None if rate is None else round(float(rate) * 100, 1)


def confined(raw: str | Path) -> Path:
    """``raw`` resolved; refused unless it lies in the repository or the working directory."""
    resolved = os.path.realpath(raw)
    for base in (ROOT, Path.cwd()):
        real_base = os.path.realpath(base)
        if resolved == real_base or resolved.startswith(real_base + os.sep):
            return Path(resolved)
    raise SystemExit(f"generate-badges.py: refusing {raw}: outside the repository and the working directory")


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--coverage-xml", default=str(ROOT / "artifacts" / "coverage" / "coverage.xml"))
    parser.add_argument("--out", default=str(ROOT / "docs" / "badges"))
    args = parser.parse_args(argv)
    out = confined(args.out)
    out.mkdir(parents=True, exist_ok=True)
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    badges = {
        "version.svg": badge("version", version, "#007ec6"),
        "python.svg": badge("python", ">= 3.10", "#3776ab"),
        "arch.svg": badge("arch", "x86_64", "#555"),
    }
    percent = coverage_percent(confined(args.coverage_xml))
    if percent is None:
        print(f"note: {args.coverage_xml} not found; coverage badge not updated")
    else:
        badges["coverage.svg"] = badge("coverage", f"{percent}%", coverage_color(percent))
    for name, svg in badges.items():
        (out / name).write_text(svg, encoding="utf-8")
        print(f"wrote {out / name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
