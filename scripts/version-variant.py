#!/usr/bin/env python3
"""Turn a copy of the Fork for Linux (unofficial) tree into a build of another version.

Used by ``scripts/build-version-variant.sh`` to build "N-1" (or "N+1") packages for the
upgrade tests (docs/qa/UPDATE-SCENARIOS.md) without editing the real tree. In the copy
``TREE`` it rewrites everything the version contract (AGENTS.md §4.7.2) ties together:

* ``VERSION``;
* the ``debian/changelog`` top entry (entries of this or a newer version are dropped, so
  the changelog stays in order and lintian accepts it);
* the ``%changelog`` top entry of both RPM specs (rpmlint wants it to match the version);
* the metainfo ``<release>`` list (newest first, as appstreamcli requires);
* ``docs/releases/vVERSION.md`` and ``vVERSION_github_description.md`` stubs, carrying the
  not-affiliated disclaimer and the buy link like every release note.

Usage: version-variant.py TREE VERSION [--date YYYY-MM-DD]

It refuses to rewrite the checkout it belongs to: only a copy is ever changed.
"""

from __future__ import annotations

import argparse
import datetime as dt
import email.utils
import re
import sys
import textwrap
from pathlib import Path

SEMVER = re.compile(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)")
DEB_HEADER = re.compile(r"^fork-linux \(([^)]+)\) ([a-z]+); urgency=medium$")
DEB_TRAILER = re.compile(r"^ -- (.+?)  \S.*$")
RPM_HEADER = re.compile(r"^\* \w{3} \w{3} \d{2} \d{4} (.+?) - ([^-\s]+)-\d+$")
RELEASE = re.compile(r'([ \t]*)<release version="([^"]+)"[^>]*?(?:/>|>.*?</release>)[ \t]*\n', re.DOTALL)
APP_ID = "io.github.ventura8.ForkLinux"
SPECS = ("packaging/rpm/fedora/fork-linux.spec", "packaging/rpm/opensuse/fork-linux.spec")
METAINFO = f"data/{APP_ID}.metainfo.xml.in"
NOTE = "Version variant {version} of Fork for Linux (unofficial), built for upgrade tests; never released."
DISCLAIMER = (
    "Fork is made by **Dan Pristupov and Tanya Pristupova**. This project is **not affiliated with, "
    "endorsed by or supported by** them. Fork is paid software: please "
    "[buy a license](https://git-fork.com/buy) if you keep using it."
)


class VariantError(Exception):
    """The tree cannot be turned into the requested version."""


def parse_version(text: str) -> tuple[int, int, int]:
    """``N.N.N`` as a tuple; anything else raises :class:`VariantError`."""
    match = SEMVER.fullmatch(text.strip())
    if match is None:
        raise VariantError(f"not an N.N.N version: {text!r}")
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def _upstream(version: str) -> tuple[int, int, int]:
    """The ``N.N.N`` part of a package version such as ``1.0.0`` or ``1.0.0+ppa1``."""
    match = SEMVER.match(version)
    if match is None:
        raise VariantError(f"unexpected version in a changelog: {version!r}")
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def _blocks(lines: list[str], is_header: re.Pattern[str]) -> tuple[list[str], list[list[str]]]:
    """Split ``lines`` into what precedes the first header and one block per header."""
    head: list[str] = []
    blocks: list[list[str]] = []
    for line in lines:
        if is_header.match(line):
            blocks.append([line])
        elif blocks:
            blocks[-1].append(line)
        else:
            head.append(line)
    return head, blocks


def rewrite_debian_changelog(text: str, version: str, date: dt.date) -> str:
    """``debian/changelog`` with ``version`` on top and no entry of this or a newer version."""
    wanted = parse_version(version)
    _head, blocks = _blocks(text.splitlines(), DEB_HEADER)
    if not blocks:
        raise VariantError("debian/changelog has no fork-linux entry")
    top = blocks[0]
    header = DEB_HEADER.match(top[0])
    trailer = next((DEB_TRAILER.match(line) for line in top if DEB_TRAILER.match(line)), None)
    if header is None or trailer is None:
        raise VariantError("the top debian/changelog entry has no maintainer line")
    stamp = email.utils.format_datetime(dt.datetime(date.year, date.month, date.day, 12, tzinfo=dt.timezone.utc))
    note = textwrap.wrap(NOTE.format(version=version), width=76, initial_indent="  * ", subsequent_indent="    ")
    entry = [
        f"fork-linux ({version}) {header.group(2)}; urgency=medium",
        "",
        *note,
        "",
        f" -- {trailer.group(1)}  {stamp}",
        "",
    ]
    kept = [block for block in blocks if _upstream(DEB_HEADER.match(block[0]).group(1)) < wanted]
    out = entry + [line for block in kept for line in block]
    while out and out[-1] == "":
        out.pop()
    return "\n".join(out) + "\n"


def rewrite_rpm_changelog(text: str, version: str, date: dt.date) -> str:
    """An RPM spec whose ``%changelog`` starts with ``version`` and lists no newer entry."""
    wanted = parse_version(version)
    lines = text.splitlines()
    if "%changelog" not in lines:
        raise VariantError("the spec has no %changelog")
    cut = lines.index("%changelog") + 1
    _head, blocks = _blocks(lines[cut:], RPM_HEADER)
    if not blocks:
        raise VariantError("the spec's %changelog has no entry")
    packager = RPM_HEADER.match(blocks[0][0]).group(1)
    entry = [f"* {date.strftime('%a %b %d %Y')} {packager} - {version}-1", f"- {NOTE.format(version=version)}", ""]
    kept = [block for block in blocks if _upstream(RPM_HEADER.match(block[0]).group(2)) < wanted]
    out = lines[:cut] + entry + [line for block in kept for line in block]
    while out and out[-1] == "":
        out.pop()
    return "\n".join(out) + "\n"


def rewrite_metainfo(text: str, version: str, date: dt.date) -> str:
    """The metainfo template with a ``<release>`` for ``version`` first and none newer."""
    wanted = parse_version(version)
    start = text.find("<releases>\n")
    end = text.find("</releases>")
    if start < 0 or end < start:
        raise VariantError("the metainfo template has no <releases> list")
    body_start = start + len("<releases>\n")
    body = text[body_start:end]
    found = list(RELEASE.finditer(body))
    indent = found[0].group(1) if found else "    "
    kept = "".join(m.group(0) for m in found if _upstream(m.group(2)) < wanted)
    inner = indent + "  "
    entry = (
        f'{indent}<release version="{version}" date="{date.isoformat()}">\n'
        f"{inner}<description>\n"
        f"{inner}  <p>{NOTE.format(version=version)}</p>\n"
        f"{inner}</description>\n"
        f"{indent}</release>\n"
    )
    closing = body[found[-1].end():] if found else body
    return text[:body_start] + entry + kept + closing + text[end:]


def release_notes(version: str, base: str) -> dict[str, str]:
    """The two release-note stubs for ``version`` (built from the ``base`` tree)."""
    note = f"Built by scripts/build-version-variant.sh from the {base} tree for upgrade tests; never released."
    title = f"# Fork for Linux (unofficial) v{version} — upgrade-test variant"
    return {
        f"v{version}.md": f"{title}\n\n{note}\n\n{DISCLAIMER}\n",
        f"v{version}_github_description.md": f"{note}\n\n> [!IMPORTANT]\n> {DISCLAIMER}\n",
    }


def _own_checkout() -> Path:
    """The tree this script belongs to (never rewritten)."""
    return Path(__file__).resolve().parent.parent


def apply(tree: Path, version: str, date: dt.date) -> list[str]:
    """Rewrite ``tree`` for ``version``; return the files written (relative to ``tree``)."""
    parse_version(version)
    tree = tree.resolve()
    if tree == _own_checkout():
        raise VariantError(f"refusing to rewrite {tree}: it is this script's own checkout; pass a copy")
    version_file = tree / "VERSION"
    if not version_file.is_file():
        raise VariantError(f"{tree} is not a Fork for Linux tree (no VERSION file)")
    base = version_file.read_text(encoding="utf-8").strip()
    if parse_version(base) == parse_version(version):
        raise VariantError(f"the tree already is version {version}")
    rewrites = {
        "debian/changelog": rewrite_debian_changelog,
        METAINFO: rewrite_metainfo,
        **{spec: rewrite_rpm_changelog for spec in SPECS},
    }
    written = []
    for rel, rewrite in rewrites.items():
        path = tree / rel
        path.write_text(rewrite(path.read_text(encoding="utf-8"), version, date), encoding="utf-8")
        written.append(rel)
    for name, body in release_notes(version, base).items():
        (tree / "docs" / "releases" / name).write_text(body, encoding="utf-8")
        written.append(f"docs/releases/{name}")
    version_file.write_text(f"{version}\n", encoding="utf-8")
    written.append("VERSION")
    return written


def _date(text: str) -> dt.date:
    try:
        return dt.date.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a YYYY-MM-DD date: {text!r}") from None


def main(argv: list[str] | None = None) -> int:
    """Command line entry point; returns the exit status."""
    parser = argparse.ArgumentParser(description="Rewrite a copy of the tree for another version.")
    parser.add_argument("tree", type=Path, help="the copy of the tree to rewrite")
    parser.add_argument("version", help="the version it should claim (N.N.N)")
    parser.add_argument("--date", type=_date, help="release date (YYYY-MM-DD, default: today, UTC)")
    args = parser.parse_args(argv)
    date = args.date or dt.datetime.now(dt.timezone.utc).date()
    try:
        written = apply(args.tree, args.version, date)
    except (VariantError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    for rel in written:
        print(f"rewrote {rel}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
