"""Tests for fork_linux.credits: the single source of credits, disclaimer and buy links.

Also a drift check: README.md and docs/CREDITS.md must carry every link.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from fork_linux import APP_NAME, credits

ROOT = Path(__file__).resolve().parents[1]
DOCS = (ROOT / "README.md", ROOT / "docs" / "CREDITS.md")

EXPECTED_LINKS = {
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

RENDERERS = {"text": credits.render_text, "markdown": credits.render_markdown}


def test_links_are_exactly_the_contract() -> None:
    assert credits.LINKS == EXPECTED_LINKS
    for name, url in credits.LINKS.items():
        if name != "support":
            assert url.startswith("https://"), name


def test_developers() -> None:
    assert credits.DEVELOPERS == ("Dan Pristupov", "Tanya Pristupova")


def test_prior_art_entries() -> None:
    names = [name for name, _url, _note in credits.PRIOR_ART]
    assert len(names) == len(set(names)) == 12
    for name, url, note in credits.PRIOR_ART:
        assert name and note
        assert url.startswith("https://"), name
    urls = {url for _name, url, _note in credits.PRIOR_ART}
    assert "https://github.com/jasonnicholson/fork-wine-setup" in urls
    assert "https://github.com/fork-dev/Tracker/issues/2033" in urls
    assert "https://www.winehq.org" in urls
    assert credits.PRIOR_ART == [*credits.COMMUNITY_PRIOR_ART, *credits.UPSTREAM_PROJECTS]


@pytest.mark.parametrize("kind", sorted(RENDERERS))
def test_every_link_and_credit_is_rendered(kind: str) -> None:
    text = RENDERERS[kind]()
    for name, url in credits.LINKS.items():
        assert url in text, f"{kind} is missing LINKS[{name!r}]"
    for developer in credits.DEVELOPERS:
        assert developer in text
    for name, url, _note in credits.PRIOR_ART:
        assert url in text, f"{kind} is missing prior art {name}"
    assert "NOT affiliated" in text
    assert "never redistributes, patches or decompiles Fork" in text
    assert credits.INSTALLER_HOST in text
    assert credits.PRICE in text
    assert "evaluation is free" in text
    assert "licensing" in text
    assert text.endswith("\n")


def test_about_text_content() -> None:
    text = credits.render_text()
    assert text.startswith(APP_NAME + "\n")
    assert "official, unmodified Fork for Windows" in text
    assert "under Wine" in text
    assert "please buy a license" in text
    assert "endorsed by" in text and "supported by" in text
    assert "not to Fork support" in text
    assert "EULA" in text
    assert "Thanks to" in text
    for name, _url, _note in credits.PRIOR_ART:
        assert name in text


def test_markdown_uses_links_for_prior_art_and_trackers() -> None:
    text = credits.render_markdown()
    for name, url, _note in credits.PRIOR_ART:
        assert f"[{name}]({url})" in text
    assert f"]({credits.LINKS['tracker_win']})" in text
    assert f"]({credits.LINKS['tracker_mac']})" in text
    assert f"]({credits.LINKS['twitter']})" in text
    assert not text.startswith("#")  # the caller owns the heading
    assert text.count("[") == text.count("]")


def test_desktop_comment() -> None:
    comment = credits.render_desktop_comment()
    assert "\n" not in comment
    assert len(comment) <= 120
    assert comment.startswith("Unofficial Wine wrapper for the Fork git client")
    assert "not affiliated" in comment
    assert credits.LINKS["buy"] in comment


def test_metainfo_paragraphs_are_plain_text() -> None:
    paragraphs = credits.render_metainfo_paragraphs()
    assert isinstance(paragraphs, list) and len(paragraphs) >= 3
    joined = " ".join(paragraphs)
    for paragraph in paragraphs:
        assert paragraph.strip() == paragraph and paragraph
        assert not re.search(r"[<>&*`\[\]\n]", paragraph), paragraph
    assert APP_NAME in joined
    for developer in credits.DEVELOPERS:
        assert developer in joined
    assert credits.LINKS["buy"] in joined
    assert credits.LINKS["project_issues"] in joined
    assert "not affiliated" in joined
    assert "never redistributes" in joined


def test_short_footer_exact() -> None:
    assert credits.short_footer() == "Fork © Dan Pristupov & Tanya Pristupova — https://git-fork.com/buy"


def test_disclaimer_and_developers_text() -> None:
    assert credits.developers_text() == "Dan Pristupov and Tanya Pristupova"
    assert credits.developers_text(" & ") == "Dan Pristupov & Tanya Pristupova"
    sentence = credits.disclaimer()
    assert APP_NAME in sentence
    assert "NOT affiliated" in sentence
    assert credits.LINKS["project_issues"] in sentence


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: str(p.relative_to(ROOT)))
def test_docs_carry_every_link(doc: Path) -> None:
    if not doc.is_file():
        pytest.skip(f"{doc.relative_to(ROOT)} does not exist yet")
    text = doc.read_text(encoding="utf-8")
    missing = [name for name, url in credits.LINKS.items() if url not in text]
    assert missing == [], f"{doc.relative_to(ROOT)} lacks LINKS {missing}"
    for developer in credits.DEVELOPERS:
        assert developer in text
