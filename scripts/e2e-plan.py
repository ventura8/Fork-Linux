#!/usr/bin/env python3
"""Plan a parallel run of the real-Fork tier (scripts/ci-e2e-wine.sh): order, roots, template key.

Each module of ``tests/e2e`` runs in a container and a scratch root of its own. A module that
only needs the main prefix set up starts from a copy of a *template* root, which one container
built once per run (``template`` mode: the copy keeps the prefix, runtimes and settings and the
tier reuses them, ``FL_E2E_REUSE=1``); a module that sets up prefixes of its own, or times the
first setup, starts with the template's download caches only (``fresh`` mode). Modules run
longest first, as many at a time as there are jobs; ``template`` modules wait for the template.

The durations come from ``tests/e2e/timings.json`` (seconds per module from a measured run,
setup excluded for ``template`` modules; ``setup`` is the template build). A module missing
there counts as the longest, so it starts first.

Usage:
  e2e-plan.py plan MODULE...                       one line per session: WHAT MODE SECONDS PYTEST-ARGS...
  e2e-plan.py estimate --jobs N MODULE...          the run's wall-clock estimate in seconds
  e2e-plan.py split --jobs N --remote-jobs M MODULE...
                                                   one line per module: local|remote NAME, so that
                                                   both machines (each its own template) end together
  e2e-plan.py key --image-id ID [--fork-version V] the template key (sha256 of what setup depends on)

Standard library only; it reads files and prints, it never starts a container.
"""

from __future__ import annotations

import argparse
import hashlib
import heapq
import json
import os
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import NamedTuple

REPO = Path(__file__).resolve().parents[1]
TIMINGS = REPO / "tests" / "e2e" / "timings.json"
# Modules that never use the main prefix (they set up prefixes of their own, or time the first
# setup of the main one): they start from the template's download caches only.
FRESH = frozenset(
    {
        "test_real_setup_errors.py",
        "test_real_fork.py",
        "test_fork_update.py",
        "test_wine_app_upgrade.py",
        "test_wine_pin_upgrade.py",
    }
)
# What a template depends on besides the image: the code that sets up, and the tier's own setup call.
KEY_TREES = ("src/fork_linux",)
KEY_FILES = ("tests/e2e/conftest.py", "tests/e2e/fork_driver.py", "tests/e2e/test_real_fork.py")
TEMPLATE = "template"
# Long modules run as several sessions (each from the template) so that none holds up the whole run:
# module -> {part: its tests}; the module's other tests make one more session ("rest").
PARTS: dict[str, dict[str, tuple[str, ...]]] = {
    "test_real_setup_variants.py": {"dotnet472": ("test_dotnet472_prefix_runs_fork",)},
}


class Job(NamedTuple):
    """One pytest session of the plan: a whole module, or a part of a long one."""

    what: str
    mode: str
    seconds: float
    args: tuple[str, ...]


class Timings(NamedTuple):
    """``tests/e2e/timings.json``: the template build, each module, and the parts of a split module."""

    setup: float
    modules: dict[str, float]
    parts: dict[str, dict[str, float]]


def mode(module: str) -> str:
    """``fresh`` or ``template``: how the root of ``module`` starts."""
    return "fresh" if module in FRESH else TEMPLATE


def load_timings(path: Path = TIMINGS) -> Timings:
    data = json.loads(path.read_text(encoding="utf-8"))
    modules = {name: float(row["seconds"]) for name, row in data["modules"].items()}
    parts = {name: {part: float(took) for part, took in row["parts"].items()}
             for name, row in data["modules"].items() if "parts" in row}
    return Timings(float(data["setup"]), modules, parts)


def jobs_of(module: str, timings: Timings) -> list[Job]:
    """The sessions of ``module``: one, or one per entry of ``PARTS`` plus one for the rest of its tests."""
    path = f"tests/e2e/{module}"
    took = timings.modules.get(module, float("inf"))
    if module not in PARTS:
        return [Job(path, mode(module), took, (path,))]
    jobs = []
    rest = took
    for part, tests in PARTS[module].items():
        seconds = timings.parts.get(module, {}).get(part, took / (len(PARTS[module]) + 1))
        rest -= seconds
        jobs.append(Job(f"{path}[{part}]", mode(module), seconds, tuple(f"{path}::{test}" for test in tests)))
    deselect = tuple(f"--deselect={path}::{test}" for tests in PARTS[module].values() for test in tests)
    jobs.append(Job(f"{path}[rest]", mode(module), max(rest, 1.0), (path, *deselect)))
    return jobs


def plan(modules: Iterable[str], timings: Timings) -> list[Job]:
    """Every session of ``modules`` longest first (a module without a timing first of all)."""
    jobs = [job for module in dict.fromkeys(modules) for job in jobs_of(module, timings)]
    return sorted(jobs, key=lambda job: (-job.seconds, job.what))


def estimate(modules: Sequence[str], jobs: int, timings: Timings) -> float:
    """The wall-clock of the run ci-e2e-wine.sh makes: ``jobs`` at a time, longest first, the template
    built first when a ``template`` session needs it."""
    pending = plan(modules, timings)
    free = [0.0] * jobs
    ready = 0.0
    if any(job.mode == TEMPLATE for job in pending):
        ready = timings.setup
        free[0] = ready
    heapq.heapify(free)
    end = ready
    while pending:
        start = heapq.heappop(free)
        job = next((job for job in pending if job.mode != TEMPLATE or start >= ready), None)
        if job is None:
            heapq.heappush(free, ready)
            continue
        pending.remove(job)
        finish = max(start, ready if job.mode == TEMPLATE else 0.0) + job.seconds
        end = max(end, finish)
        heapq.heappush(free, finish)
    return end


def split(modules: Sequence[str], jobs: int, remote_jobs: int, timings: Timings) -> tuple[list[str], list[str]]:
    """Share the modules between this machine (``jobs`` at a time) and a second one (``remote_jobs``), each
    building its own template: longest first, each module where the estimated end is earlier."""
    local: list[str] = []
    remote: list[str] = []
    for module in sorted(dict.fromkeys(modules), key=lambda name: (-timings.modules.get(name, float("inf")), name)):
        here = estimate([*local, module], jobs, timings)
        there = estimate([*remote, module], remote_jobs, timings)
        (local if here <= there else remote).append(module)
    return local, remote


def _files(root: Path) -> list[Path]:
    found = [root / rel for rel in KEY_FILES]
    for tree in KEY_TREES:
        found.extend(path for path in (root / tree).rglob("*") if path.is_file() and "__pycache__" not in path.parts)
    return sorted(found)


def template_key(root: Path, image_id: str, fork_version: str) -> str:
    """sha256 over the image, the Fork version, the uid and every file setup depends on."""
    digest = hashlib.sha256()
    for part in (image_id, fork_version, str(os.getuid())):
        digest.update(part.encode("utf-8") + b"\0")
    for path in _files(root):
        digest.update(path.relative_to(root).as_posix().encode("utf-8") + b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).hexdigest().encode("ascii") + b"\0")
    return digest.hexdigest()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="e2e-plan.py", description=__doc__.split("\n", 1)[0])
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("plan", "estimate", "split"):
        command = commands.add_parser(name)
        command.add_argument("modules", nargs="+")
        command.add_argument("--timings", type=Path, default=TIMINGS)
        if name != "plan":
            command.add_argument("--jobs", type=int, required=True)
        if name == "split":
            command.add_argument("--remote-jobs", type=int, required=True)
    key = commands.add_parser("key")
    key.add_argument("--image-id", required=True)
    key.add_argument("--fork-version", default="")
    key.add_argument("--root", type=Path, default=REPO)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "key":
        print(template_key(args.root, args.image_id, args.fork_version))
        return 0
    timings = load_timings(args.timings)
    modules = [Path(module).name for module in args.modules]
    if args.command == "plan":
        for job in plan(modules, timings):
            took = f"{job.seconds:.0f}" if job.seconds != float("inf") else "unknown"
            print(" ".join((job.what, job.mode, took, *job.args)))
        return 0
    if min(args.jobs, getattr(args, "remote_jobs", 1)) < 1:
        print("e2e-plan.py: --jobs and --remote-jobs must be at least 1", file=sys.stderr)
        return 2
    if args.command == "estimate":
        print(f"{estimate(modules, args.jobs, timings):.0f}")
        return 0
    local, remote = split(modules, args.jobs, args.remote_jobs, timings)
    for where, names in (("local", local), ("remote", remote)):
        for module in names:
            print(f"{where} {module}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
