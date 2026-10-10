"""The single source of every credit, disclaimer and "please buy Fork" text.

Used by ``fork-linux about``, the consent dialog, the ``.desktop`` comment, the
AppStream description, the README credits block and ``docs/CREDITS.md``.
``tests/test_credits.py`` fails when those drift apart.
"""

from __future__ import annotations

from . import APP_NAME

LINKS: dict[str, str] = {
    "website": "https://git-fork.com",
    "buy": "https://git-fork.com/buy",
    "release_notes": "https://git-fork.com/releasenoteswin",
    "license": "https://git-fork.com/license",
    "twitter": "https://twitter.com/git_fork",
    "tracker_win": "https://github.com/fork-dev/TrackerWin",
    "tracker_mac": "https://github.com/fork-dev/Tracker",
    "support": "support@fork.dev",
    "project": "https://github.com/ventura8/Fork-Linux",
    "project_issues": "https://github.com/ventura8/Fork-Linux/issues",
}

DEVELOPERS: tuple[str, ...] = ("Dan Pristupov", "Tanya Pristupova")

PRICE = "$59.99"
LICENSE_SCOPE = "one user on up to 3 machines"
INSTALLER_HOST = "cdn.fork.dev"

# Community work on running Fork (or another Windows git GUI) under Wine.
COMMUNITY_PRIOR_ART: tuple[tuple[str, str, str], ...] = (
    (
        "pixiekat Wine guide",
        "https://github.com/pixiekat/gists/blob/main/install-wine-and-delinea.md",
        "a Wine recipe for Fork that works without patching it",
    ),
    (
        "jasonnicholson/fork-wine-setup",
        "https://github.com/jasonnicholson/fork-wine-setup",
        "setup automation and a launcher (it patches Fork.exe; we deliberately do not)",
    ),
    (
        "NitroHxC gist",
        "https://gist.github.com/NitroHxC/ff579d57b15f7ba5dcd1429eac13468f",
        "setup and run scripts using Fork's release feed",
    ),
    (
        'Fork Tracker#2033 "Run Fork with Wine"',
        "https://github.com/fork-dev/Tracker/issues/2033",
        "the developers' own thread on running Fork under Wine",
    ),
    (
        "Fork Tracker#153",
        "https://github.com/fork-dev/Tracker/issues/153",
        "the long-standing request for a Linux version",
    ),
    (
        "WineHQ AppDB",
        "https://appdb.winehq.org/objectManager.php?sClass=application&iId=19743",
        "community test reports for Fork under Wine",
    ),
    (
        "dakusan/tortoisewine",
        "https://github.com/dakusan/tortoisewine",
        "bridging a Windows git GUI under Wine to native git",
    ),
    (
        "coskunergan/ubuntu_tortoise",
        "https://github.com/coskunergan/ubuntu_tortoise",
        "bridging a Windows git GUI under Wine to native git",
    ),
    (
        "andy-5/wslgit",
        "https://github.com/andy-5/wslgit",
        "a Windows-to-WSL git forwarder that handles Fork's rebase helper",
    ),
)

# Upstream projects fork-linux builds on at runtime.
UPSTREAM_PROJECTS: tuple[tuple[str, str, str], ...] = (
    ("Wine", "https://www.winehq.org", "runs Fork for Windows on Linux"),
    ("Kron4ek Wine-Builds", "https://github.com/Kron4ek/Wine-Builds", "the pinned managed Wine runtime"),
    ("Winetricks", "https://github.com/Winetricks/winetricks", "installs .NET Framework and the core fonts"),
    ("Selawik", "https://github.com/microsoft/Selawik", "the OFL-1.1 Segoe UI stand-in for Fork's interface"),
)

PRIOR_ART: list[tuple[str, str, str]] = [*COMMUNITY_PRIOR_ART, *UPSTREAM_PROJECTS]


def developers_text(joiner: str = " and ") -> str:
    """``Dan Pristupov and Tanya Pristupova``."""
    return joiner.join(DEVELOPERS)


def disclaimer() -> str:
    """One sentence: not affiliated, report problems here."""
    return (
        f"{APP_NAME} is NOT affiliated with, endorsed by or supported by the Fork developers; "
        f"report Linux and Wine problems to {LINKS['project_issues']}, not to Fork support."
    )


def short_footer() -> str:
    """``Fork © Dan Pristupov & Tanya Pristupova — https://git-fork.com/buy``."""
    return f"Fork © {developers_text(' & ')} — {LINKS['buy']}"


def render_text() -> str:
    """The ``fork-linux about`` screen (plain text)."""
    lines = [
        APP_NAME,
        "",
        "Runs the official, unmodified Fork for Windows git client under Wine.",
        f"Fork is made by {developers_text()}.",
        "",
        f"  Website:        {LINKS['website']}",
        f"  Buy a license:  {LINKS['buy']}",
        f"                  Fork is paid software: {PRICE} for {LICENSE_SCOPE}.",
        "                  The evaluation is free - please buy a license if you keep using it.",
        f"  Release notes:  {LINKS['release_notes']}",
        f"  Issue trackers: {LINKS['tracker_win']} (Windows)",
        f"                  {LINKS['tracker_mac']} (Mac and general)",
        f"  Twitter:        {LINKS['twitter']}",
        f"  Contact:        {LINKS['support']} (licensing and purchases only)",
        "",
        "Disclaimer",
        f"  {APP_NAME} is NOT affiliated with, endorsed by or supported by",
        "  the Fork developers. Report Linux, Wine and wrapper problems to",
        f"  {LINKS['project_issues']} - not to Fork support.",
        "  If a problem also happens on Windows, report it to Fork's TrackerWin.",
        "",
        f"  Fork is proprietary software under its EULA: {LINKS['license']}",
        f"  fork-linux downloads the official installer from {INSTALLER_HOST} on your machine",
        "  and never redistributes, patches or decompiles Fork.",
        f"  Project: {LINKS['project']}",
        "",
        "Thanks to the prior art and upstream projects this builds on:",
    ]
    lines.extend(f"  - {name}: {url}" for name, url, _note in PRIOR_ART)
    return "\n".join(lines) + "\n"


def _md_list(entries: tuple[tuple[str, str, str], ...]) -> str:
    """``[name](url), [name](url)``."""
    return ", ".join(f"[{name}]({url})" for name, url, _note in entries)


def render_markdown() -> str:
    """The README / CREDITS "Credits & support the developers" block (Markdown, no heading)."""
    tracker_win = f"[fork-dev/TrackerWin]({LINKS['tracker_win']})"
    lines = [
        f"**Fork** is made by **{DEVELOPERS[0]}** and **{DEVELOPERS[1]}** — {LINKS['website']}. "
        "Everything that makes Fork great is their work; this wrapper only makes it start on Linux.",
        "",
        f"- **Please buy a license:** {LINKS['buy']} — the evaluation is free; a license "
        f"({PRICE} at the time of writing) covers {LICENSE_SCOPE}.",
        f"- Release notes: {LINKS['release_notes']} · License (EULA): {LINKS['license']} "
        f"· Twitter: [@git_fork]({LINKS['twitter']})",
        f"- Fork's issue trackers: {tracker_win} (Windows) · "
        f"[fork-dev/Tracker]({LINKS['tracker_mac']}) (Mac and general). "
        f"{LINKS['support']} is for licensing questions only.",
        "",
        f"> {APP_NAME} is **NOT affiliated with, endorsed by, or supported by** Fork's developers. "
        f"Report Linux and wrapper issues to [ventura8/Fork-Linux]({LINKS['project_issues']}) "
        f"({LINKS['project']}), not to Fork support. "
        f"If a problem also happens on Windows, report it to {tracker_win}.",
        "",
        f"> Fork is proprietary software under its [EULA]({LINKS['license']}). fork-linux downloads "
        f"the official installer from `{INSTALLER_HOST}` on your machine and never redistributes, "
        "patches or decompiles Fork.",
        "",
        f"**Community prior art:** {_md_list(COMMUNITY_PRIOR_ART)}.",
        "",
        f"**Upstream projects:** {_md_list(UPSTREAM_PROJECTS)}.",
    ]
    return "\n".join(lines) + "\n"


def render_desktop_comment() -> str:
    """The ``.desktop`` ``Comment=`` value: one line of at most 120 characters."""
    return (
        "Unofficial Wine wrapper for the Fork git client, not affiliated with its developers. "
        f"Buy Fork: {LINKS['buy']}"
    )


def render_metainfo_paragraphs() -> list[str]:
    """Plain-text paragraphs for the AppStream ``<description>`` (no markup)."""
    return [
        f"{APP_NAME} runs the official, unmodified Fork for Windows git client under Wine, "
        "with a menu entry, a fork command for opening repositories, update snapshots and a health check.",
        f"Fork is proprietary, paid software made by {developers_text()}. "
        f"The evaluation is free; please buy a license at {LINKS['buy']} if you keep using it.",
        "This project is not affiliated with, endorsed by or supported by the Fork developers. "
        f"It downloads the official Fork installer from {INSTALLER_HOST} on your machine and never "
        "redistributes, patches or decompiles Fork. Report problems with the wrapper at "
        f"{LINKS['project_issues']}.",
    ]
