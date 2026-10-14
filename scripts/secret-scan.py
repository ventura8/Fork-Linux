#!/usr/bin/env python3
"""Scan the repository, and optionally its whole git history, for committed secrets.

Fork for Linux (unofficial) never needs a credential in the tree: CI secrets live in GitHub,
the SonarCloud token in ``SONAR_TOKEN`` or the gitignored ``.sonar-token``. This scanner looks
for the shapes real credentials have (private key blocks, cloud, GitHub, GitLab, Slack, Google,
SonarCloud, npm and PyPI tokens), standard library only, so the lint stage needs no third-party
scanner. Test data that has to look like a secret is built at run time, never written out.

Usage:
  python3 scripts/secret-scan.py [ROOT]            # the files git tracks
  python3 scripts/secret-scan.py --history [ROOT]  # every blob reachable from any ref

Exit status: 0 clean, 1 findings ("where: [rule]"; the secret itself is never printed),
2 not a git work tree.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections.abc import Iterable, Iterator
from pathlib import Path

# (rule id, pattern). Each pattern is specific to one kind of credential.
RULES: tuple[tuple[str, re.Pattern[bytes]], ...] = (
    # A key block with a body: at least four full base64 lines (a header alone is how code and tests
    # recognise key files; the shortest real key, ed25519 in OpenSSH's format, has five).
    (
        "private-key",
        re.compile(
            rb"-----BEGIN (?:RSA |DSA |EC |OPENSSH |ENCRYPTED |PGP )?PRIVATE KEY(?: BLOCK)?-----\r?\n"
            rb"(?:[A-Za-z][A-Za-z-]*:[^\n]*\r?\n|\r?\n)*(?:[A-Za-z0-9+/=]{60,}\r?\n){4,}"
        ),
    ),
    ("aws-access-key", re.compile(rb"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github-token", re.compile(rb"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("github-fine-grained-token", re.compile(rb"\bgithub_pat_\w{60,}\b")),
    ("gitlab-token", re.compile(rb"\bglpat-[A-Za-z0-9_-]{20,}\b")),
    ("slack-token", re.compile(rb"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    ("google-api-key", re.compile(rb"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("sonar-token", re.compile(rb"\bsq[uap]_[0-9a-f]{40}\b")),
    ("npm-token", re.compile(rb"\bnpm_[A-Za-z0-9]{36}\b")),
    ("pypi-token", re.compile(rb"\bpypi-AgEIcHlwaS5vcmc[A-Za-z0-9_-]{50,}")),
)
# Larger blobs are binaries or archives, never configuration.
MAX_BYTES = 2 * 1024 * 1024


def scan_bytes(data: bytes) -> list[tuple[int, str]]:
    """``(line, rule)`` for every secret-shaped match in ``data`` (empty for binary data)."""
    if len(data) > MAX_BYTES or b"\0" in data[:8192]:
        return []
    found = []
    for rule, pattern in RULES:
        for match in pattern.finditer(data):
            found.append((data.count(b"\n", 0, match.start()) + 1, rule))
    return sorted(found)


def _git(root: Path, *args: str, data: bytes | None = None) -> bytes:
    return subprocess.run(["git", "-C", str(root), *args], input=data, capture_output=True, check=True).stdout


def tracked(root: Path) -> Iterator[tuple[str, bytes]]:
    """``(path, content)`` of every file git tracks in the work tree (missing files skipped)."""
    for name in _git(root, "ls-files", "-z").split(b"\0"):
        if not name:
            continue
        path = root / name.decode("utf-8", "surrogateescape")
        if path.is_file() and not path.is_symlink():
            yield str(path.relative_to(root)), path.read_bytes()


def _batch(root: Path, objects: list[tuple[str, str]]) -> Iterator[tuple[str, bytes]]:
    """``(label, content)`` of the blob ids in ``objects`` through one ``git cat-file --batch``."""
    if not objects:
        return
    output = _git(root, "cat-file", "--batch", data="".join(f"{oid}\n" for oid, _ in objects).encode())
    offset = 0
    for oid, label in objects:
        header_end = output.index(b"\n", offset)
        size = int(output[offset:header_end].split()[2])
        yield f"{label} (blob {oid[:12]})", output[header_end + 1 : header_end + 1 + size]
        offset = header_end + 1 + size + 1


def history(root: Path) -> Iterator[tuple[str, bytes]]:
    """``(label, content)`` of every blob reachable from any ref, each blob once."""
    listing = _git(root, "rev-list", "--objects", "--all").decode("utf-8", "surrogateescape")
    candidates = [(line.split(" ", 1)[0], line.split(" ", 1)[1]) for line in listing.splitlines() if " " in line]
    if not candidates:
        return
    kinds = _git(root, "cat-file", "--batch-check=%(objecttype) %(objectsize)",
                 data="".join(f"{oid}\n" for oid, _ in candidates).encode()).decode().splitlines()
    blobs = [
        pair for pair, kind in zip(candidates, kinds, strict=True)
        if kind.split()[0] == "blob" and int(kind.split()[1]) <= MAX_BYTES
    ]
    yield from _batch(root, blobs)


def findings(items: Iterable[tuple[str, bytes]]) -> list[str]:
    """``where:line: [rule]`` for every match (never the matched text)."""
    return [f"{where}:{line}: [{rule}]" for where, data in items for line, rule in scan_bytes(data)]


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("root", nargs="?", default=".", type=Path)
    parser.add_argument("--history", action="store_true", help="scan every blob reachable from any ref")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    try:
        _git(root, "rev-parse", "--is-inside-work-tree")
    except (subprocess.CalledProcessError, OSError):
        print(f"secret-scan: {root} is not a git work tree", file=sys.stderr)
        return 2
    found = findings(history(root) if args.history else tracked(root))
    for line in found:
        print(line, file=sys.stderr)
    if found:
        print(f"secret-scan: {len(found)} finding(s); remove the secret and rotate it", file=sys.stderr)
        return 1
    print(f"secret-scan: no secrets found ({'history' if args.history else 'tracked files'})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
