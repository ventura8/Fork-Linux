"""The AppStream metainfo: generated from data/<app-id>.metainfo.xml.in and credits.py."""

from __future__ import annotations

import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from fixtures import gen_data

from fork_linux import APP_ID, APP_NAME, credits

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "data" / f"{APP_ID}.metainfo.xml.in"
VERSION = (ROOT / "VERSION").read_text(encoding="utf-8").strip()


gen = gen_data.load()
TEXT = gen.render_metainfo(TEMPLATE.read_text(encoding="utf-8"))
ROOT_EL = ET.fromstring(TEXT)


def test_identity_and_licenses() -> None:
    assert ROOT_EL.get("type") == "desktop-application"
    assert ROOT_EL.findtext("id") == APP_ID
    assert ROOT_EL.findtext("name") == APP_NAME
    assert ROOT_EL.findtext("metadata_license") == "CC0-1.0"
    assert ROOT_EL.findtext("project_license") == "MIT"
    developer = ROOT_EL.find("developer")
    assert developer is not None
    assert developer.get("id") == "io.github.ventura8"
    launchable = ROOT_EL.find("launchable")
    assert launchable is not None
    assert launchable.text == f"{APP_ID}.desktop"


def test_description_carries_the_credits_and_disclaimer() -> None:
    description = ROOT_EL.find("description")
    assert description is not None
    paragraphs = ["".join(p.itertext()) for p in description.findall("p")]
    assert len(paragraphs) == len(credits.render_metainfo_paragraphs())
    text = " ".join(paragraphs)
    for developer in credits.DEVELOPERS:
        assert developer in text
    assert "git-fork.com/buy" in text
    assert "not affiliated with, endorsed by or supported by the Fork developers" in text
    assert "://" not in text  # AppStream: no plain-text URLs in descriptions


def test_newest_release_is_version_and_no_screenshots() -> None:
    releases = ROOT_EL.find("releases")
    assert releases is not None
    assert releases[0].get("version") == VERSION
    assert ROOT_EL.find("screenshots") is None  # hard rule 4: no Fork screenshots


def test_template_has_no_hard_coded_identity() -> None:
    template = TEMPLATE.read_text(encoding="utf-8")
    assert "<id>@APP_ID@</id>" in template
    assert "@DESCRIPTION@" in template
    assert APP_ID not in template


def test_unknown_placeholder_is_an_error() -> None:
    bad = TEMPLATE.read_text(encoding="utf-8").replace("<name>", "<name>@NOPE@")
    with pytest.raises(SystemExit):
        gen.render_metainfo(bad)


def test_template_without_placeholders_is_an_error() -> None:
    with pytest.raises(SystemExit):
        gen.render_metainfo("<component/>")


def test_appstreamcli_validate(tmp_path: Path) -> None:
    if shutil.which("appstreamcli") is None:
        pytest.skip("appstreamcli is not installed (the lint stage runs it)")
    path = tmp_path / f"{APP_ID}.metainfo.xml"
    path.write_text(TEXT, encoding="utf-8")
    subprocess.run(["appstreamcli", "validate", "--no-net", str(path)], check=True, capture_output=True)
