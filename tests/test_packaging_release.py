"""Contract tests for the packaging recipes and release scripts (AGENTS.md §4.7.2, §4.9).

Version sync (VERSION, debian/changelog, metainfo, release notes), no maintainer scripts,
amd64 only, never the distribution's wine, the app id used everywhere, the Flatpak
finish-arg allowlist, and the release scripts' own behaviour on fake artifacts.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from fork_linux import APP_ID, PACKAGE, hostdeps

ROOT = Path(__file__).resolve().parents[1]
VERSION = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
FORMATS = ["deb", "deb-jammy", "rpm-fedora", "rpm-opensuse", "arch", "snap", "appimage", "flatpak", "tarball"]
SPECS = [ROOT / "packaging" / "rpm" / d / "fork-linux.spec" for d in ("fedora", "opensuse")]
PKGBUILD = ROOT / "packaging" / "arch" / "PKGBUILD.in"
SNAPCRAFT = ROOT / "packaging" / "snap" / "snapcraft.yaml"
FLATPAK = ROOT / "packaging" / "flatpak" / f"{APP_ID}.yml"
METAINFO = ROOT / "data" / f"{APP_ID}.metainfo.xml.in"
SCRIPTS = [
    "release-common.sh", "release-deb.sh", "release-rpm.sh", "release-arch.sh", "release-portable.sh",
    "aur-render.sh", "ppa-docker.sh", "release-verify-tag-version.sh", "packaging-smoke-verify.sh",
    "packaging-e2e-install.sh", "ci-docker.sh", "ci-matrix.sh", "ci-pipeline.sh", "ci-packaging-cell.sh",
    "ci-packaging-matrix.sh", "ci-snap-build.sh", "ci-e2e-wine.sh", "check-upstream-fork.py",
    "check-dep-names.py", "generate-badges.py", "gen-data.py",
]
FINISH_ARGS_ALLOWED = {
    "--share=network",
    "--socket=x11",
    "--socket=pulseaudio",
    "--device=dri",
    "--filesystem=home",
    "--filesystem=/media",
    "--filesystem=/mnt",
    "--filesystem=/run/media",
    "--socket=ssh-auth",
    "--talk-name=org.freedesktop.Flatpak",
}


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# --------------------------------------------------------------------- version single source


def test_debian_changelog_top_entry_is_version() -> None:
    first = _text(ROOT / "debian" / "changelog").splitlines()[0]
    assert re.fullmatch(rf"fork-linux \({re.escape(VERSION)}\) resolute; urgency=medium", first)


def test_metainfo_newest_release_is_version() -> None:
    releases = re.findall(r'<release version="([^"]+)" date="(\d{4}-\d{2}-\d{2})"', _text(METAINFO))
    assert releases
    assert releases[0][0] == VERSION


def test_release_notes_exist_for_version() -> None:
    for name in (f"v{VERSION}.md", f"v{VERSION}_github_description.md"):
        text = _text(ROOT / "docs" / "releases" / name)
        assert "not affiliated" in text
        assert "https://git-fork.com/buy" in text


def test_no_recipe_hardcodes_the_version() -> None:
    literal = re.compile(rf"(?<![\d.]){re.escape(VERSION)}(?![\d.])")
    for path in [*SPECS, PKGBUILD, SNAPCRAFT, FLATPAK, ROOT / "meson.build", ROOT / "debian" / "control"]:
        body = "\n".join(line for line in _text(path).splitlines() if not line.startswith(("#", "*", "-")))
        assert not literal.search(body), f"{path.relative_to(ROOT)} hard-codes {VERSION}"
    for spec in SPECS:
        assert "Version:        %{fl_version}" in _text(spec)
    assert "pkgver=@VERSION@" in _text(PKGBUILD)
    assert "adopt-info: fork-linux" in _text(SNAPCRAFT)


# --------------------------------------------------------------------- packaging rules


def test_debian_package_rules() -> None:
    control = _text(ROOT / "debian" / "control")
    assert "Architecture: amd64" in control
    assert "Architecture: any" not in control
    assert "debhelper-compat (= 13)" in control
    assert "gcc-mingw-w64-x86-64" in control
    assert _text(ROOT / "debian" / "source" / "format").strip() == "3.0 (native)"
    assert "dh $@ --buildsystem=meson" in _text(ROOT / "debian" / "rules")
    names = {path.name for path in (ROOT / "debian").iterdir()}
    for forbidden in ("postinst", "preinst", "postrm", "prerm", "config", "triggers"):
        assert not any(name == forbidden or name.endswith("." + forbidden) for name in names), forbidden
    assert not list((ROOT / "debian").rglob("*lintian-overrides*"))
    assert "lintian --fail-on error" in _text(ROOT / "scripts" / "release-deb.sh")


@pytest.mark.parametrize("spec", SPECS, ids=lambda p: p.parent.name)
def test_rpm_spec_rules(spec: Path) -> None:
    text = _text(spec)
    assert "ExclusiveArch:  x86_64" in text
    for scriptlet in ("%pre", "%post", "%preun", "%postun", "%pretrans", "%posttrans", "%triggerin"):
        assert not re.search(rf"^{scriptlet}\b", text, re.MULTILINE), scriptlet
    assert "-D \"fl_version ${VERSION}\"" in _text(ROOT / "scripts" / "release-rpm.sh")
    for lib in hostdeps.REQUIRED_LIBS:
        assert f"Requires:       {lib}()(64bit)" in text


def test_no_rpmlint_filters_or_lintian_overrides_in_the_tree() -> None:
    assert not [p for p in ROOT.glob("packaging/**/*") if p.name.endswith(("rpmlintrc", ".lintian-overrides"))]


def test_arch_pkgbuild_rules() -> None:
    text = _text(PKGBUILD)
    assert "arch=('x86_64')" in text
    assert not re.search(r"^install=", text, re.MULTILINE)
    assert "pkgname=fork-linux" in text


def test_snap_rules() -> None:
    text = _text(SNAPCRAFT)
    assert "base: core24" in text
    assert "confinement: classic" in text
    assert re.search(r"build-for: \[amd64\]", text)
    assert "hooks" not in text
    assert f"common-id: {APP_ID}" in text
    assert "fl-bridge-helper-static" in text


def test_flatpak_rules() -> None:
    text = _text(FLATPAK)
    assert f"id: {APP_ID}" in text
    assert "base: org.winehq.Wine" in text
    assert "base-version: wow64-25.08" in text
    assert "runtime-version: '25.08'" in text
    assert "command: fork-linux" in text
    assert "-Dflavor=flatpak" in text


def test_flatpak_finish_args_are_explained_and_allowlisted() -> None:
    lines = _text(FLATPAK).splitlines()
    start = lines.index("finish-args:")
    args = []
    for index in range(start + 1, len(lines)):
        line = lines[index]
        if not line.startswith("  "):
            break
        if line.strip().startswith("- "):
            arg = line.strip()[2:]
            assert lines[index - 1].strip().startswith("# why: "), f"{arg} has no '# why:' comment"
            args.append(arg)
    assert args
    assert set(args) <= FINISH_ARGS_ALLOWED
    assert len(args) == len(set(args))


def test_no_recipe_depends_on_distribution_wine() -> None:
    subprocess.run([sys.executable, str(ROOT / "scripts" / "check-dep-names.py")], check=True)


def test_app_id_is_used_consistently() -> None:
    assert (ROOT / "src" / "fork_linux" / "data" / "templates" / f"{APP_ID}.desktop.in").is_file()
    assert METAINFO.is_file()
    assert FLATPAK.is_file()
    assert (ROOT / "data" / "icons" / "hicolor" / "scalable" / "apps" / f"{APP_ID}.svg").is_file()
    assert (ROOT / "data" / "icons" / "hicolor" / "symbolic" / "apps" / f"{APP_ID}-symbolic.svg").is_file()
    for spec in SPECS:
        assert f"%global app_id {APP_ID}" in _text(spec)
    assert f"usr/share/applications/{APP_ID}.desktop" in _text(SNAPCRAFT)
    assert "@APP_ID@" in _text(METAINFO)
    meson = _text(ROOT / "meson.build")
    assert APP_ID not in meson.replace(f"{APP_ID}.desktop.in", "")  # read from the package, never retyped
    assert f"pkgname={PACKAGE}" in _text(PKGBUILD)
    assert f"Package: {PACKAGE}" in _text(ROOT / "debian" / "control")


def test_ppa_versions() -> None:
    text = _text(ROOT / "scripts" / "ppa-docker.sh")
    assert 'ppa_version="${VERSION}+ppa1~ubuntu${release}.1"' in text
    assert "jammy) echo 22.04" in text
    assert "noble) echo 24.04" in text
    assert "resolute) echo 26.04" in text
    assert "--dry-run" in text
    assert 'UPLOAD_PPA:-0}" == "1"' in text


def test_cell_formats_everywhere() -> None:
    cell = _text(ROOT / "scripts" / "ci-packaging-cell.sh")
    for fmt in FORMATS:
        assert re.search(rf"^\t{re.escape(fmt)}\)$", cell, re.MULTILINE), fmt
    smoke = _text(ROOT / "scripts" / "packaging-smoke-verify.sh")
    e2e = _text(ROOT / "scripts" / "packaging-e2e-install.sh")
    for fmt in FORMATS:
        assert fmt in smoke
        assert fmt in e2e


def test_e2e_asserts_what_agents_md_requires() -> None:
    e2e = _text(ROOT / "scripts" / "packaging-e2e-install.sh")
    for needle in (
        "--version",
        "doctor --offline --json",
        "PE32+ x86-64",
        "ET_EXEC",
        "desktop-file-validate",
        "metainfo",
        "man1",
        "bash-completion",
        "E2E_MARKER",
        ".wine/E2E_SENTINEL",
        'echo "==> E2E upgrade in place',
        'echo "==> E2E remove',
        'echo "==> E2E reinstall',
        'runuser -u "${TESTER}"',
    ):
        assert needle in e2e, needle


@pytest.mark.parametrize("name", SCRIPTS)
def test_scripts_are_executable(name: str) -> None:
    mode = (ROOT / "scripts" / name).stat().st_mode
    if name != "release-common.sh":
        assert mode & stat.S_IXUSR, f"scripts/{name} is not executable"


def test_shellcheck_packaging_scripts() -> None:
    if shutil.which("shellcheck") is None:
        pytest.skip("shellcheck is not installed (the lint stage runs it)")
    files = [str(ROOT / "scripts" / n) for n in SCRIPTS if n.endswith(".sh")]
    files += [
        str(ROOT / p)
        for p in (
            "packaging/common/find-python.sh",
            "packaging/appimage/AppRun",
            "packaging/appimage/build-appimage.sh",
            "packaging/snap/fork-linux-wrapper.sh",
            "docker/snap-entrypoint.sh",
        )
    ]
    subprocess.run(["shellcheck", "-x", *files], cwd=ROOT, check=True)


# --------------------------------------------------------------------- scripts on fake artifacts


def _bash(script: str, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    full_env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", "/tmp")}
    full_env.update(env or {})
    return subprocess.run(
        ["bash", str(ROOT / "scripts" / script), *args],
        capture_output=True,
        text=True,
        check=False,
        env=full_env,
        cwd=ROOT,
    )


FAKES = {
    "deb": [f"fork-linux_{VERSION}_amd64.deb"],
    "rpm-fedora": [f"fork-linux-{VERSION}-1.fc44.x86_64.rpm"],
    "rpm-opensuse": [f"fork-linux-{VERSION}-1.lp160.x86_64.rpm"],
    "arch": [f"fork-linux-{VERSION}-1-x86_64.pkg.tar.zst"],
    "snap": [f"fork-linux_{VERSION}_amd64.snap"],
    "appimage": [f"fork-linux-{VERSION}-x86_64.AppImage", f"fork-linux-{VERSION}-x86_64.AppImage.zsync"],
    "flatpak": [f"{APP_ID}-{VERSION}-x86_64.flatpak"],
}


@pytest.mark.parametrize("fmt", sorted(FAKES))
def test_smoke_verify_accepts_the_artifact(tmp_path: Path, fmt: str) -> None:
    for name in FAKES[fmt]:
        (tmp_path / name).write_bytes(b"x")
    result = _bash("packaging-smoke-verify.sh", fmt, env={"FL_ARTIFACTS_DIR": str(tmp_path)})
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("fmt", [*sorted(FAKES), "tarball"])
def test_smoke_verify_rejects_missing_or_empty(tmp_path: Path, fmt: str) -> None:
    for name in FAKES.get(fmt, []):
        (tmp_path / name).write_bytes(b"")
    assert _bash("packaging-smoke-verify.sh", fmt, env={"FL_ARTIFACTS_DIR": str(tmp_path)}).returncode != 0


def test_smoke_verify_rejects_unknown_format(tmp_path: Path) -> None:
    assert _bash("packaging-smoke-verify.sh", "msi", env={"FL_ARTIFACTS_DIR": str(tmp_path)}).returncode != 0


def test_aur_render_writes_pkgbuild_and_srcinfo(tmp_path: Path) -> None:
    tarball = tmp_path / "tag.tar.gz"
    tarball.write_bytes(b"archive")
    result = _bash("aur-render.sh", "--tarball", str(tarball), "--out", str(tmp_path / "aur"))
    assert result.returncode == 0, result.stderr
    pkgbuild = _text(tmp_path / "aur" / "PKGBUILD")
    srcinfo = _text(tmp_path / "aur" / ".SRCINFO")
    assert "@" not in pkgbuild.replace("alexandrescu.sergiu@gmail.com", "")
    assert f"pkgver={VERSION}" in pkgbuild
    assert f"Fork-Linux-{VERSION}" in pkgbuild
    assert f"\tpkgver = {VERSION}\n" in srcinfo
    assert "\tarch = x86_64\n" in srcinfo
    assert "\tdepends = gnutls\n" in srcinfo
    url = f"https://github.com/ventura8/Fork-Linux/archive/v{VERSION}.tar.gz"
    assert f"source = fork-linux-{VERSION}.tar.gz::{url}" in srcinfo
    assert srcinfo.endswith("\npkgname = fork-linux\n")


def test_source_tarball_contains_only_what_git_would_commit(tmp_path: Path) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    out = tmp_path / "src.tar.gz"
    script = f'. "{ROOT}/scripts/release-common.sh"; fl_create_source_tarball 0.0.0 "{out}"'
    subprocess.run(["bash", "-c", script], check=True, cwd=ROOT)
    listing = subprocess.run(["tar", "-tzf", str(out)], capture_output=True, text=True, check=True).stdout.split()
    assert "fork-linux-0.0.0/VERSION" in listing
    assert "fork-linux-0.0.0/meson.build" in listing
    for entry in listing:
        rel = entry.split("/", 1)[1] if "/" in entry else ""
        assert not rel.startswith(("artifacts/", ".git/", "build-")), entry
        assert not rel.startswith("logs/") or rel in {"logs/", "logs/README.md"}, entry
        assert "__pycache__" not in entry
        assert not entry.endswith((".exe", ".pyc"))
        assert ".sonar-token" not in entry
