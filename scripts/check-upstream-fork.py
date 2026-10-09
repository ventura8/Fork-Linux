#!/usr/bin/env python3
"""Detect a new Fork for Windows release for Fork for Linux (unofficial) (upstream-watch.yml).

Reads Fork's update feed (the manifest's ``feed_url``; HTTPS, host on the manifest's
allowlist), takes the newest full package and compares its version with the versions the
runtime manifest already knows (known-good or known-bad). Versions are strictly validated
(``fork_linux.versions.is_valid``); the installer URL is built locally from the manifest's
``installer_url_template`` and never taken from the feed (AGENTS.md hard rule 2).

Usage:
  check-upstream-fork.py [--feed-file FILE] [--github-output FILE]
      print {"latest", "known", "new"} as JSON; with --github-output also write
      new=true|false and version=X for GitHub Actions
  check-upstream-fork.py --add VERSION --installer FILE [--feed-sha256 HEX]
      add VERSION to src/fork_linux/data/runtime-manifest.json as known-good with the
      installer's size and sha256 (FILE was downloaded from cdn.fork.dev by the workflow;
      it is never committed, cached or uploaded)
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import re
import sys
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "src" / "fork_linux" / "data" / "runtime-manifest.json"
FEED_LIMIT = 5 * 1024 * 1024
# The feeds this script may read: the manifest must name one of them (hard rule 7).
VERSION_RE = re.compile(r"\d{1,6}(?:\.\d{1,6}){1,3}")
SHA256_RE = re.compile(r"[0-9A-Fa-f]{64}")
KNOWN_FEEDS = ("https://git-fork.com/update/win/releases.win.json",)


def confined(raw: str | Path) -> Path:
    """``raw`` resolved; refused unless it is in the repository, the working directory, the
    system temp directory or the Actions runner's temp directory (``$RUNNER_TEMP``)."""
    resolved = os.path.realpath(raw)
    bases = [ROOT, Path.cwd(), Path(tempfile.gettempdir())]
    if os.environ.get("RUNNER_TEMP"):
        bases.append(Path(os.environ["RUNNER_TEMP"]))
    for base in bases:
        real_base = os.path.realpath(base)
        if resolved == real_base or resolved.startswith(real_base + os.sep):
            return Path(resolved)
    raise SystemExit(f"check-upstream-fork.py: refusing {raw}: outside the allowed directories")


def _package() -> tuple[ModuleType, ModuleType]:
    """``fork_linux.feeds`` and ``fork_linux.versions`` from this source tree."""
    if str(ROOT / "src") not in sys.path:
        sys.path.insert(0, str(ROOT / "src"))
    return importlib.import_module("fork_linux.feeds"), importlib.import_module("fork_linux.versions")


def load_manifest(path: Path = MANIFEST) -> dict:
    """The runtime manifest."""
    return json.loads(path.read_text(encoding="utf-8"))


def known_versions(manifest: dict) -> set[str]:
    """Every Fork version the manifest already pins or rejects."""
    fork = manifest["fork"]
    return set(fork.get("versions", {})) | set(fork.get("known_bad", {}))


def fetch_feed(manifest: dict) -> str:
    """Fork's update feed: only a known feed URL, over HTTPS, never redirected off the allowlist."""
    wanted = manifest["fork"]["feed_url"]
    url = next((known for known in KNOWN_FEEDS if known == wanted), None)
    if url is None:
        raise SystemExit(f"check-upstream-fork.py: refusing feed URL {wanted}")
    request = urllib.request.Request(url, headers={"User-Agent": "fork-linux-upstream-watch"})
    with urllib.request.urlopen(request, timeout=60) as response:
        final = urllib.parse.urlsplit(response.geturl())
        if final.scheme != "https" or final.hostname not in manifest["fork"]["allowed_hosts"]:
            raise SystemExit(f"check-upstream-fork.py: the feed redirected to {response.geturl()}")
        body = response.read(FEED_LIMIT + 1)
    if len(body) > FEED_LIMIT:
        raise SystemExit("check-upstream-fork.py: the feed is too large")
    return body.decode("utf-8")


def latest_version(feed_text: str) -> str:
    """The newest full package's version in the feed, strictly validated."""
    feeds, versions = _package()
    asset = feeds.latest_full(feeds.parse_releases_json(feed_text))
    if asset is None:
        raise SystemExit("check-upstream-fork.py: the feed lists no full package")
    if not versions.is_valid(asset.version):
        raise SystemExit(f"check-upstream-fork.py: invalid version in the feed: {asset.version!r}")
    return asset.version


def status(feed_text: str, manifest: dict) -> dict:
    """``{"latest", "known", "new"}`` for the feed against the manifest."""
    _feeds, versions = _package()
    latest = latest_version(feed_text)
    known = sorted(known_versions(manifest))
    newest_known = manifest["fork"]["default"]
    is_new = latest not in known and versions.compare(latest, newest_known) > 0
    return {"latest": latest, "known": known, "new": is_new}


def add_version(version: str, installer: Path, feed_sha256: str | None) -> dict:
    """Record ``version`` as known-good in the repository's runtime manifest (``MANIFEST``)."""
    _feeds, versions = _package()
    version_match = VERSION_RE.fullmatch(version)
    if version_match is None or not versions.is_valid(version):
        raise SystemExit(f"check-upstream-fork.py: invalid version {version!r}")
    clean_version = version_match.group(0)
    sha_match = SHA256_RE.fullmatch(feed_sha256) if feed_sha256 else None
    if feed_sha256 and sha_match is None:
        raise SystemExit("check-upstream-fork.py: --feed-sha256 must be 64 hex digits")
    manifest = load_manifest(MANIFEST)
    data = confined(installer).read_bytes()
    if data[:2] != b"MZ":
        raise SystemExit(f"check-upstream-fork.py: {installer} is not a Windows executable")
    entry = {
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "status": "known-good",
        "tested_with": list(manifest["fork"]["versions"][manifest["fork"]["default"]].get("tested_with", [])),
    }
    if sha_match is not None:
        entry["full_nupkg_sha256"] = sha_match.group(0).upper()
    manifest["fork"]["versions"][clean_version] = entry
    with MANIFEST.open("w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2)
        handle.write("\n")
    return entry


def main(argv: list[str] | None = None) -> None:
    """Command-line entry point (errors exit through SystemExit)."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--feed-file", help="read the feed from a file instead of the network")
    parser.add_argument("--github-output", help="append new= / version= for GitHub Actions")
    parser.add_argument("--add", metavar="VERSION", help="add VERSION to the manifest as known-good")
    parser.add_argument("--installer", help="with --add: the downloaded official installer")
    parser.add_argument("--feed-sha256", help="with --add: the feed's sha256 of the full nupkg")
    args = parser.parse_args(argv)

    if args.add:
        if args.installer is None:
            parser.error("--add needs --installer")
        entry = add_version(args.add, Path(args.installer), args.feed_sha256)
        print(json.dumps({"added": args.add, **entry}, indent=2))
        return
    manifest = load_manifest()
    if args.feed_file:
        feed_text = confined(args.feed_file).read_text(encoding="utf-8")
    else:
        feed_text = fetch_feed(manifest)
    result = status(feed_text, manifest)
    print(json.dumps(result, indent=2))
    if args.github_output:
        with confined(args.github_output).open("a", encoding="utf-8") as handle:
            handle.write(f"new={'true' if result['new'] else 'false'}\nversion={result['latest']}\n")


if __name__ == "__main__":
    main()
