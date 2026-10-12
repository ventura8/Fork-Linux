#!/usr/bin/env python3
"""A simulated, completed setup for the packaging E2E: what the next launch after an upgrade re-runs.

``scripts/packaging-e2e-install.sh`` installs the previous release (N-1), lets this script
lay out what a finished setup leaves in the test user's home, without Wine and without
downloading anything, and then asks every installed release (N, N-1 again after a
downgrade, ...) ``fork-linux setup --list-steps --json``: which steps would its next launch
run again (scenario A2 of docs/qa/UPDATE-SCENARIOS.md)?

* ``prepare STATUS_JSON MANIFEST``: our prefix marker, a placeholder Fork install of the
  manifest's default version (``current/Fork.exe`` + ``sq.version``; nothing of Fork's) and the
  managed Wine runtime's ``.complete`` marker with stand-in ``wine`` / ``wineserver`` scripts
  that answer ``--version`` and record any other call in ``E2E_WINE_RAN`` (a failure: no Wine
  may run). ``STATUS_JSON`` is ``fork-linux status --json`` of the installed release, so the
  paths are the program's own (``~/.var/app/<id>`` in the Flatpak). The placeholder
  ``Fork.exe`` is a minimal PE32+ x86-64 image without resources (:func:`placeholder_exe`):
  the per-user installs (``install.sh``, ``AppImage --install-desktop``) run ``fork-linux
  desktop install``, which finds no icon in it and keeps the neutral icon without a warning.
* ``record STATUS_JSON STEPS_JSON``: ``state.json`` with a marker for every step, taken from
  the installed release's own ``--list-steps --json`` (revision and inputs hash), as a
  completed setup records them.
* ``check STEPS_JSON CHANGED``: the steps that are no longer recorded are exactly ``CHANGED``
  (comma-separated, may be empty), none of them downloads anything, ``needs_setup`` agrees
  and the placeholder Fork still counts as installed.
* ``digest STATUS_JSON``: one sha256 over every file this script wrote; the E2E compares it
  after each package operation (packages never touch a home directory) and it fails when a
  stand-in Wine was run.
* ``markers STATUS_JSON``: the steps ``state.json`` holds a marker for (comma-separated);
  ``missing STATUS_JSON MARKERS``: those of ``MARKERS`` it no longer holds. Removing a
  per-user install (``uninstall.sh``, ``AppImage --remove-desktop``) runs ``fork-linux
  uninstall`` / ``desktop remove``, which clear the ``icon`` and ``desktop_entry`` markers so
  the next launch puts Fork's icon and the menu entry back (scenario A11).

Runs as the unprivileged test user with the system ``python3`` (3.10+, standard library).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import struct
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

WINE_RAN = "E2E_WINE_RAN"
STATE_SCHEMA = 1
NUSPEC = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    '<package xmlns="http://schemas.microsoft.com/packaging/2010/07/nuspec.xsd">\n'
    "  <metadata>\n"
    "    <id>Fork</id>\n"
    "    <version>{version}</version>\n"
    "    <authors>packaging E2E placeholder, not Fork</authors>\n"
    "  </metadata>\n"
    "</package>\n"
)
STUB = """#!/bin/sh
# packaging E2E stand-in for the managed Wine: it answers --version; any other call is a failure.
if [ "$1" = "--version" ]; then
	echo "wine-{version}{flavor}"
	exit 0
fi
echo "$0 $*" >>{ran}
exit 97
"""


class E2EFailure(Exception):
    """A check of the simulated setup failed."""


def _load_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise E2EFailure(f"{path}: not a JSON object")
    return data


def _write(path: Path, data: str | bytes, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        data = data.encode("utf-8")
    path.write_bytes(data)
    os.chmod(path, mode)


class Layout:
    """Where the simulated setup lives, from ``fork-linux status --json`` and the manifest."""

    def __init__(self, status: dict[str, Any], user: str) -> None:
        self.prefix = Path(status["prefix"])
        self.state_file = Path(status["state_file"])
        self.writer = str(status["fork_linux"])
        self.data_dir = self.prefix.parent
        self.fork_dir = self.prefix / "drive_c" / "users" / user / "AppData" / "Local" / "Fork" / "current"
        self.created_by = self.prefix / ".fork-linux" / "created-by"
        self.wine_ran = self.data_dir / WINE_RAN

    def runtime(self, build_id: str) -> Path:
        return self.data_dir / "runtimes" / "wine" / build_id

    def seeded(self) -> list[Path]:
        """Every file :func:`prepare` and :func:`record` write, sorted."""
        found = [self.created_by, self.state_file, *self.fork_dir.glob("*")]
        found += list((self.data_dir / "runtimes").rglob("*"))
        return sorted(path for path in found if path.is_file())


def placeholder_exe() -> bytes:
    """A minimal, valid PE32+ x86-64 GUI image: headers only, no sections, no resources (no icon).

    MZ header with ``e_lfanew`` = 0x40, the PE signature, a COFF header (machine 0x8664, no
    sections, 240-byte optional header) and a PE32+ optional header (magic 0x20B, subsystem 2,
    16 empty data directories). Nothing of Fork's.
    """
    dos = b"MZ" + bytes(0x3A) + struct.pack("<I", 0x40)
    coff = struct.pack("<HHIIIHH", 0x8664, 0, 0, 0, 0, 240, 0x22)
    optional = bytearray(240)
    struct.pack_into("<H", optional, 0, 0x20B)
    struct.pack_into("<H", optional, 68, 2)
    struct.pack_into("<I", optional, 108, 16)
    return dos + b"PE\0\0" + coff + bytes(optional)


def _layout(status_json: Path) -> Layout:
    user = os.environ.get("USER", "")
    if not user:
        raise E2EFailure("USER is not set")
    return Layout(_load_json(status_json), user)


def prepare(status_json: Path, manifest_json: Path) -> Layout:
    """Lay out our prefix, a placeholder Fork and the managed runtime's markers."""
    layout = _layout(status_json)
    manifest = _load_json(manifest_json)
    layout.prefix.mkdir(parents=True, exist_ok=True)
    os.chmod(layout.prefix, 0o700)
    _write(layout.created_by, f"fork-linux {layout.writer}\n")
    fork_version = manifest["fork"]["default"]
    _write(layout.fork_dir / "Fork.exe", placeholder_exe())
    _write(layout.fork_dir / "sq.version", NUSPEC.format(version=fork_version))
    build_id = manifest["wine"]["default"]
    build = manifest["wine"]["builds"][build_id]
    root = layout.runtime(build_id)
    flavor = " (Staging)" if build.get("flavor") == "staging" else ""
    stub = STUB.format(version=build["version"], flavor=flavor, ran=layout.wine_ran)
    for name in ("wine", "wineserver"):
        _write(root / "bin" / name, stub, mode=0o755)
    _write(root / ".complete", build["sha256"] + "\n")
    return layout


def _steps(steps_json: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    listed = _load_json(steps_json)
    rows = listed.get("steps")
    if not isinstance(rows, list) or not rows:
        raise E2EFailure(f"{steps_json}: no steps listed")
    return listed, rows


def record(status_json: Path, steps_json: Path) -> Layout:
    """Write ``state.json`` as a completed setup leaves it, with the installed release's markers."""
    layout = _layout(status_json)
    listed, rows = _steps(steps_json)
    now = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    markers = {
        row["id"]: {"rev": row["rev"], "inputs_hash": row["inputs_hash"], "completed": now}
        for row in rows
        if row.get("inputs_hash") is not None
    }
    revisions = listed["revisions"]
    state = {
        "schema": STATE_SCHEMA,
        "steps": markers,
        "setup": {
            "complete": True,
            "bootstrap_revision": revisions["bootstrap"],
            "manifest_revision": revisions["manifest"],
            "completed_at": now,
            "last_error": None,
        },
        "consent": {"fork_eula": now, "method": "packaging-e2e"},
        "written_by": layout.writer,
    }
    _write(layout.state_file, json.dumps(state, indent=2, sort_keys=True) + "\n", mode=0o600)
    return layout


def check(steps_json: Path, changed: str) -> str:
    """Compare what the next launch would re-run with ``changed``; return a summary line."""
    listed, rows = _steps(steps_json)
    expected = sorted(name for name in changed.split(",") if name)
    stale = sorted(row["id"] for row in rows if row.get("recorded") is False)
    problems = []
    if stale != expected:
        problems.append(f"steps the next launch re-runs: {stale}, expected {expected}")
    downloads = [row["id"] for row in rows if row.get("needs_network") and row.get("recorded") is not True]
    if downloads:
        problems.append(f"the next launch would download again: {downloads}")
    if listed.get("needs_setup") is not bool(stale):
        problems.append(f"needs_setup is {listed.get('needs_setup')!r} with {stale or 'nothing'} to re-run")
    installed = {row["id"]: row.get("status") for row in rows}.get("fork_install")
    if installed != "done":
        problems.append(f"fork_install is {installed!r}: the placeholder Fork no longer counts as installed")
    if problems:
        raise E2EFailure("; ".join(problems))
    recorded = sum(1 for row in rows if row.get("recorded") is True)
    rerun = ", ".join(stale) if stale else "nothing"
    return f"ok  next launch re-runs {rerun}; {recorded} steps stay recorded, nothing is downloaded again"


def digest(status_json: Path) -> str:
    """sha256 over the seeded files (paths and contents); fails when a stand-in Wine ran."""
    layout = _layout(status_json)
    if layout.wine_ran.exists():
        ran = layout.wine_ran.read_text(encoding="utf-8", errors="replace").strip()
        raise E2EFailure(f"Wine was run without a real setup: {ran}")
    files = layout.seeded()
    if not files:
        raise E2EFailure(f"nothing seeded under {layout.data_dir}")
    total = hashlib.sha256()
    for path in files:
        total.update(str(path).encode("utf-8") + b"\0")
        total.update(hashlib.sha256(path.read_bytes()).hexdigest().encode("ascii") + b"\n")
    return f"{total.hexdigest()} {len(files)}"


def markers(status_json: Path) -> list[str]:
    """The step ids ``state.json`` holds a marker for, sorted."""
    layout = _layout(status_json)
    steps = _load_json(layout.state_file).get("steps")
    if not isinstance(steps, dict):
        raise E2EFailure(f"{layout.state_file}: no step markers")
    return sorted(steps)


def missing(status_json: Path, expected: str) -> list[str]:
    """The step ids of ``expected`` (comma-separated) without a marker now, sorted."""
    present = set(markers(status_json))
    return sorted(name for name in expected.split(",") if name and name not in present)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="packaging-e2e-state.py", description=__doc__.split("\n", 1)[0])
    sub = parser.add_subparsers(dest="command", required=True)
    cmd = sub.add_parser("prepare")
    cmd.add_argument("status_json", type=Path)
    cmd.add_argument("manifest", type=Path)
    cmd = sub.add_parser("record")
    cmd.add_argument("status_json", type=Path)
    cmd.add_argument("steps_json", type=Path)
    cmd = sub.add_parser("check")
    cmd.add_argument("steps_json", type=Path)
    cmd.add_argument("changed")
    cmd = sub.add_parser("digest")
    cmd.add_argument("status_json", type=Path)
    cmd = sub.add_parser("markers")
    cmd.add_argument("status_json", type=Path)
    cmd = sub.add_parser("missing")
    cmd.add_argument("status_json", type=Path)
    cmd.add_argument("expected")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "prepare":
            layout = prepare(args.status_json, args.manifest)
            print(f"ok  simulated setup prepared in {layout.data_dir}")
        elif args.command == "record":
            layout = record(args.status_json, args.steps_json)
            print(f"ok  setup markers of fork-linux {layout.writer} recorded in {layout.state_file}")
        elif args.command == "check":
            print(check(args.steps_json, args.changed))
        elif args.command == "markers":
            print(",".join(markers(args.status_json)))
        elif args.command == "missing":
            print(",".join(missing(args.status_json, args.expected)))
        else:
            print(digest(args.status_json))
    except (E2EFailure, KeyError, OSError, ValueError) as exc:
        print(f"packaging-e2e-state.py {args.command}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
