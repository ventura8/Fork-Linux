"""scripts/version-variant.py and scripts/build-version-variant.sh: N-1 / N+1 trees for upgrade tests.

The Python helper is loaded with importlib (its name is hyphenated) and run on copies of the
real version sources under ``tmp_path``; the shell wrapper runs with ``--tree-only`` (no
package build) and ``TMPDIR`` inside ``tmp_path``. The real tree is never written.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import os
import re
import runpy
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "version-variant.py"
BUILD = ROOT / "scripts" / "build-version-variant.sh"
VERSION = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
DATE = dt.date(2026, 10, 1)
SOURCES = (
    "VERSION",
    "debian/changelog",
    "data/io.github.ventura8.ForkLinux.metainfo.xml.in",
    "packaging/rpm/fedora/fork-linux.spec",
    "packaging/rpm/opensuse/fork-linux.spec",
    "scripts/read-version.py",
    "scripts/release-verify-tag-version.sh",
    f"docs/releases/v{VERSION}.md",
    f"docs/releases/v{VERSION}_github_description.md",
)


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("version_variant", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


vv = _load()


def _next(version: str) -> str:
    major, minor, patch = vv.parse_version(version)
    return f"{major}.{minor}.{patch + 1}"


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A copy of the files the version contract ties together."""
    root = tmp_path / "tree"
    for rel in SOURCES:
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, root / rel)
    return root


# --------------------------------------------------------------------- pieces


@pytest.mark.parametrize(("text", "parsed"), [("0.9.9", (0, 9, 9)), (" 10.0.1\n", (10, 0, 1))])
def test_parse_version(text: str, parsed: tuple[int, int, int]) -> None:
    assert vv.parse_version(text) == parsed


@pytest.mark.parametrize("text", ["1.0", "01.0.0", "1.0.0-rc1", "v1.0.0", ""])
def test_parse_version_rejects(text: str) -> None:
    with pytest.raises(vv.VariantError, match="not an N.N.N version"):
        vv.parse_version(text)


DEB = """fork-linux (1.1.0) resolute; urgency=medium

  * Second.

 -- Some One <one@example.invalid>  Sat, 10 Oct 2026 12:00:00 +0000

fork-linux (1.0.0+ppa1) resolute; urgency=medium

  * First.

 -- Some One <one@example.invalid>  Fri, 09 Oct 2026 12:00:00 +0000
"""


def test_debian_changelog_for_an_older_version_drops_newer_entries() -> None:
    out = vv.rewrite_debian_changelog(DEB, "1.0.1", DATE)
    assert out.splitlines()[0] == "fork-linux (1.0.1) resolute; urgency=medium"
    assert " -- Some One <one@example.invalid>  Thu, 01 Oct 2026 12:00:00 +0000\n" in out
    assert "(1.1.0)" not in out
    assert "fork-linux (1.0.0+ppa1)" in out
    assert out.endswith("+0000\n")
    assert max(len(line) for line in out.splitlines()) <= 90
    assert all(len(line) <= 80 for line in out.splitlines() if not line.startswith(" -- "))


def test_debian_changelog_for_a_newer_version_keeps_every_entry() -> None:
    out = vv.rewrite_debian_changelog(DEB, "2.0.0", DATE)
    assert out.index("(2.0.0)") < out.index("(1.1.0)") < out.index("(1.0.0+ppa1)")
    assert "\n\nfork-linux (1.1.0)" in out


def test_debian_changelog_where_every_entry_is_newer() -> None:
    out = vv.rewrite_debian_changelog(DEB, "0.0.1", DATE)
    assert out.count("fork-linux (") == 1
    assert out.endswith("+0000\n")


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("", "no fork-linux entry"),
        ("fork-linux (1.0.0) resolute; urgency=medium\n\n  * x\n", "no maintainer line"),
        (DEB.replace("(1.0.0+ppa1)", "(next)"), "unexpected version"),
    ],
)
def test_debian_changelog_errors(text: str, message: str) -> None:
    with pytest.raises(vv.VariantError, match=message):
        vv.rewrite_debian_changelog(text, "1.0.1", DATE)


SPEC = """Name: fork-linux

%changelog
* Sat Oct 10 2026 Some One <one@example.invalid> - 1.1.0-1
- Second

* Fri Oct 09 2026 Some One <one@example.invalid> - 1.0.0-1
- First
"""


def test_rpm_changelog_for_an_older_version() -> None:
    out = vv.rewrite_rpm_changelog(SPEC, "1.0.1", DATE)
    lines = out.splitlines()
    assert lines[:3] == ["Name: fork-linux", "", "%changelog"]
    assert lines[3] == "* Thu Oct 01 2026 Some One <one@example.invalid> - 1.0.1-1"
    assert lines[4].startswith("- Version variant 1.0.1 ")
    assert "1.1.0-1" not in out
    assert out.endswith("- First\n")


def test_rpm_changelog_for_a_newer_version_keeps_every_entry() -> None:
    out = vv.rewrite_rpm_changelog(SPEC, "2.0.0", DATE)
    assert out.index("2.0.0-1") < out.index("1.1.0-1") < out.index("1.0.0-1")


def test_rpm_changelog_drops_blank_lines_before_the_first_entry() -> None:
    out = vv.rewrite_rpm_changelog(SPEC.replace("%changelog\n", "%changelog\n\n"), "1.0.1", DATE)
    assert "%changelog\n* Thu Oct 01 2026" in out


def test_rpm_changelog_where_every_entry_is_newer() -> None:
    out = vv.rewrite_rpm_changelog(SPEC, "0.0.1", DATE)
    assert out.endswith("never released.\n")


@pytest.mark.parametrize(
    ("text", "message"),
    [("Name: x\n", "no %changelog"), ("Name: x\n%changelog\n", "has no entry")],
)
def test_rpm_changelog_errors(text: str, message: str) -> None:
    with pytest.raises(vv.VariantError, match=message):
        vv.rewrite_rpm_changelog(text, "1.0.1", DATE)


META = """<component>
  <releases>
    <release version="1.1.0" date="2026-10-10">
      <description>
        <p>Second.</p>
      </description>
    </release>
    <release version="1.0.0" date="2026-10-09"/>
  </releases>
</component>
"""


def _releases(text: str) -> list[tuple[str | None, str | None]]:
    releases = ET.fromstring(text).find("releases")
    assert releases is not None
    return [(release.get("version"), release.get("date")) for release in releases]


def test_metainfo_for_an_older_version() -> None:
    out = vv.rewrite_metainfo(META, "1.0.1", DATE)
    assert _releases(out) == [("1.0.1", "2026-10-01"), ("1.0.0", "2026-10-09")]
    assert '\n    <release version="1.0.1" date="2026-10-01">\n      <description>\n' in out
    assert out.endswith("  </releases>\n</component>\n")


def test_metainfo_for_a_newer_version_keeps_every_release() -> None:
    out = vv.rewrite_metainfo(META, "2.0.0", DATE)
    assert [version for version, _date in _releases(out)] == ["2.0.0", "1.1.0", "1.0.0"]


def test_metainfo_with_an_empty_release_list() -> None:
    out = vv.rewrite_metainfo("<component>\n  <releases>\n  </releases>\n</component>\n", "1.0.1", DATE)
    assert _releases(out) == [("1.0.1", "2026-10-01")]


@pytest.mark.parametrize("text", ["<component/>\n", "<component></releases><releases>\n</component>"])
def test_metainfo_without_a_release_list(text: str) -> None:
    with pytest.raises(vv.VariantError, match="no <releases> list"):
        vv.rewrite_metainfo(text, "1.0.1", DATE)


def test_release_notes_carry_the_disclaimer_and_buy_link() -> None:
    notes = vv.release_notes("0.9.9", "1.0.0")
    assert sorted(notes) == ["v0.9.9.md", "v0.9.9_github_description.md"]
    for body in notes.values():
        assert "not affiliated" in body
        assert "https://git-fork.com/buy" in body
        assert "Dan Pristupov and Tanya Pristupova" in body
        assert "from the 1.0.0 tree" in body


# --------------------------------------------------------------------- the whole tree


def test_apply_rewrites_every_version_source(tree: Path) -> None:
    written = vv.apply(tree, "0.9.9", DATE)
    assert set(written) == {
        "VERSION",
        "debian/changelog",
        "data/io.github.ventura8.ForkLinux.metainfo.xml.in",
        "packaging/rpm/fedora/fork-linux.spec",
        "packaging/rpm/opensuse/fork-linux.spec",
        "docs/releases/v0.9.9.md",
        "docs/releases/v0.9.9_github_description.md",
    }
    assert (tree / "VERSION").read_text(encoding="utf-8") == "0.9.9\n"
    assert (tree / "debian/changelog").read_text(encoding="utf-8").startswith(
        "fork-linux (0.9.9) resolute; urgency=medium\n"
    )
    meta = (tree / "data/io.github.ventura8.ForkLinux.metainfo.xml.in").read_text(encoding="utf-8")
    assert re.findall(r'<release version="([^"]+)"', meta) == ["0.9.9"]
    for spec in ("fedora", "opensuse"):
        text = (tree / f"packaging/rpm/{spec}/fork-linux.spec").read_text(encoding="utf-8")
        top = text.splitlines()[text.splitlines().index("%changelog") + 1]
        assert top.endswith(" - 0.9.9-1")
        assert f"- {VERSION}-1" not in text
    # The real tree is untouched.
    assert (ROOT / "VERSION").read_text(encoding="utf-8").strip() == VERSION


def test_variant_tree_passes_the_release_gate(tree: Path) -> None:
    """scripts/release-verify-tag-version.sh (release.yml's first job) accepts the variant."""
    newer = _next(VERSION)
    for version in ("0.9.9", newer):
        copy = tree.parent / f"tree-{version}"
        shutil.copytree(tree, copy)
        vv.apply(copy, version, DATE)
        result = subprocess.run(
            ["bash", str(copy / "scripts/release-verify-tag-version.sh"), f"v{version}"],
            capture_output=True, text=True, check=False, env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        )
        assert result.returncode == 0, result.stderr
        meta = (copy / "data/io.github.ventura8.ForkLinux.metainfo.xml.in").read_text(encoding="utf-8")
        assert re.findall(r'<release version="([^"]+)"', meta)[0] == version


def test_apply_refuses_its_own_checkout() -> None:
    with pytest.raises(vv.VariantError, match="own checkout"):
        vv.apply(ROOT, "0.9.9", DATE)


def test_apply_refuses_a_tree_without_version(tmp_path: Path) -> None:
    with pytest.raises(vv.VariantError, match="no VERSION file"):
        vv.apply(tmp_path, "0.9.9", DATE)


def test_apply_refuses_the_same_version(tree: Path) -> None:
    with pytest.raises(vv.VariantError, match="already is version"):
        vv.apply(tree, VERSION, DATE)


def test_apply_refuses_a_bad_version_before_reading_anything(tmp_path: Path) -> None:
    with pytest.raises(vv.VariantError, match="not an N.N.N version"):
        vv.apply(tmp_path / "missing", "1.0", DATE)


# --------------------------------------------------------------------- command line


def test_main_rewrites_and_reports(tree: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert vv.main([str(tree), "0.9.9", "--date", "2026-10-01"]) == 0
    out = capsys.readouterr().out
    assert "rewrote VERSION\n" in out
    assert "Thu, 01 Oct 2026" in (tree / "debian/changelog").read_text(encoding="utf-8")


def test_main_defaults_to_today(tree: Path) -> None:
    assert vv.main([str(tree), "0.9.9"]) == 0
    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    meta = (tree / "data/io.github.ventura8.ForkLinux.metainfo.xml.in").read_text(encoding="utf-8")
    assert f'date="{today}"' in meta


def test_main_reports_errors(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert vv.main([str(tmp_path), "0.9.9"]) == 1
    assert capsys.readouterr().err.startswith("error: ")


def test_main_reports_unwritable_files(tree: Path, capsys: pytest.CaptureFixture[str]) -> None:
    releases = tree / "docs" / "releases"
    shutil.rmtree(releases)
    releases.write_text("not a directory\n", encoding="utf-8")
    assert vv.main([str(tree), "0.9.9"]) == 1
    assert "Not a directory" in capsys.readouterr().err


def test_main_rejects_a_bad_date(tree: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        vv.main([str(tree), "0.9.9", "--date", "01/10/2026"])
    assert exc.value.code == 2
    assert "not a YYYY-MM-DD date" in capsys.readouterr().err


def test_main_module_entry_point(tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), str(tree), "0.9.9"])
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(str(SCRIPT), run_name="__main__")
    assert exc.value.code == 0


def test_script_runs_standalone(tree: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-I", str(SCRIPT), str(tree), "0.9.9", "--date", "2026-10-01"],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert (tree / "VERSION").read_text(encoding="utf-8") == "0.9.9\n"


# --------------------------------------------------------------------- build-version-variant.sh


def _build(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tmp_path), "TMPDIR": str(tmp_path)}
    return subprocess.run(
        ["bash", str(BUILD), *args], capture_output=True, text=True, check=False, env=env, cwd=tmp_path
    )


def test_build_tree_only_writes_a_variant_tree_and_cleans_up(tmp_path: Path) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    out = tmp_path / "out"
    result = _build(tmp_path, "--tree-only", "--date", "2026-10-01", "0.9.9", str(out))
    assert result.returncode == 0, result.stderr
    variant = out / "fork-linux-0.9.9"
    assert (variant / "VERSION").read_text(encoding="utf-8") == "0.9.9\n"
    assert (variant / "docs/releases/v0.9.9.md").is_file()
    assert (variant / "meson.build").is_file()
    assert not (variant / ".git").exists()
    assert not list(variant.rglob("__pycache__"))
    assert not (variant / "artifacts").exists()
    assert not list(tmp_path.glob("fl-variant-*")), "the private copy was not removed"
    assert (ROOT / "VERSION").read_text(encoding="utf-8").strip() == VERSION
    again = _build(tmp_path, "--tree-only", "0.9.9", str(out))
    assert again.returncode == 1
    assert "already exists" in again.stderr
    assert not list(tmp_path.glob("fl-variant-*"))


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ((), "usage:"),
        (("0.9.9",), "usage:"),
        (("--date",), "usage:"),
        (("--help",), "usage:"),
        (("--bogus", "0.9.9", "out"), "unknown option --bogus"),
        (("1.0", "out", "deb"), "VERSION must be N.N.N"),
        (("0.9.9", "out"), "name at least one format"),
        (("--tree-only", "0.9.9", "out", "deb"), "--tree-only builds nothing"),
        (("0.9.9", "out", "msi"), "unknown format msi"),
    ],
)
def test_build_rejects_bad_arguments(tmp_path: Path, args: tuple[str, ...], message: str) -> None:
    result = _build(tmp_path, *args)
    assert result.returncode == 2
    assert message in result.stderr
    assert not (tmp_path / "out").exists()


def test_build_refuses_the_current_version(tmp_path: Path) -> None:
    result = _build(tmp_path, "--tree-only", VERSION, str(tmp_path / "out"))
    assert result.returncode == 1
    assert "already is version" in result.stderr
    assert not list(tmp_path.glob("fl-variant-*"))


def test_build_knows_every_packaging_format() -> None:
    text = BUILD.read_text(encoding="utf-8")
    for fmt in ("deb", "deb-jammy", "rpm-fedora", "rpm-opensuse", "arch", "snap", "appimage", "flatpak", "tarball"):
        assert re.search(rf"(^|[ |]){re.escape(fmt)}( |\)|$)", text, re.MULTILINE), fmt
