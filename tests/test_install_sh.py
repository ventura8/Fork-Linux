"""install.sh / uninstall.sh behaviour in a throwaway HOME with a local release server.

The release tarball is a small fake (same layout as ``scripts/release-portable.sh tarball``)
whose ``bin/fork-linux`` is a Python script that records how it was called, so the tests see
exactly what the installer runs. Nothing touches the real home: HOME, PATH and XDG
variables are replaced for every run (AGENTS.md hard rule 16).
"""

from __future__ import annotations

import hashlib
import io
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / "install.sh"
UNINSTALL = ROOT / "uninstall.sh"
VERSION = "9.8.7"
APP_ID = "io.github.ventura8.ForkLinux"

FAKE_CLI = """#!/usr/bin/env python3
import os, sys
log = os.path.join(os.environ["HOME"], "cli-calls.log")
with open(log, "a", encoding="utf-8") as handle:
    handle.write(os.path.basename(sys.argv[0]) + " " + " ".join(sys.argv[1:]) + "\\n")
if sys.argv[1:] == ["--version"]:
    print("fork-linux @VERSION@ (tarball)")
"""

TREE_FILES = {
    "share/fork-linux/fork_linux/__init__.py": "APP_ID = 'x'\n",
    "share/man/man1/fork-linux.1": ".TH FORK-LINUX 1\n",
    "share/man/man1/fork.1": ".TH FORK 1\n",
    "share/bash-completion/completions/fork-linux": "complete -F _x fork-linux\n",
    "share/fish/vendor_completions.d/fork-linux.fish": "complete -c fork-linux -f\n",
    "share/zsh/site-functions/_fork-linux": "#compdef fork-linux fork\n",
    f"share/metainfo/{APP_ID}.metainfo.xml": "<component/>\n",
    f"share/icons/hicolor/scalable/apps/{APP_ID}.svg": "<svg/>\n",
    f"share/icons/hicolor/symbolic/apps/{APP_ID}-symbolic.svg": "<svg/>\n",
    f"share/applications/{APP_ID}.desktop": "[Desktop Entry]\n",
    "lib/fork-linux/fork-linux-host": "#!/bin/sh\n",
}


def _tarball(version: str = VERSION, extra: dict[str, tuple[bytes, int]] | None = None) -> bytes:
    """A fake ``fork-linux-VERSION-x86_64.tar.gz`` (``extra``: more members, name -> (data, mode))."""
    buffer = io.BytesIO()
    root = f"fork-linux-{version}"
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:

        def add(name: str, data: bytes, mode: int = 0o644) -> None:
            info = tarfile.TarInfo(f"{root}/{name}")
            info.size = len(data)
            info.mode = mode
            tar.addfile(info, io.BytesIO(data))

        add("bin/fork-linux", FAKE_CLI.replace("@VERSION@", version).encode(), 0o755)
        link = tarfile.TarInfo(f"{root}/bin/fork")
        link.type = tarfile.SYMTYPE
        link.linkname = "fork-linux"
        tar.addfile(link)
        for name, text in TREE_FILES.items():
            add(name, text.encode(), 0o755 if name.startswith("lib/") else 0o644)
        for name, (data, mode) in (extra or {}).items():
            add(name, data, mode)
    return buffer.getvalue()


def _sums(name: str, data: bytes) -> bytes:
    return f"{hashlib.sha256(data).hexdigest()}  {name}\n".encode()


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """A fake HOME with a pre-existing ~/.wine and fork-linux user data that must survive."""
    home = tmp_path / "home"
    (home / ".wine").mkdir(parents=True)
    (home / ".wine" / "E2E_SENTINEL").write_text("keep\n", encoding="utf-8")
    (home / ".local" / "share" / "fork-linux").mkdir(parents=True)
    (home / ".local" / "share" / "fork-linux" / "E2E_MARKER").write_text("keep\n", encoding="utf-8")
    # The host may have fork-linux installed by its package manager (the scripts then refuse to
    # run): every package manager the scripts ask reports "not installed" unless a test says so.
    _fake_tools(_no_package_bin(home), {"dpkg-query": "exit 1", "rpm": "exit 1", "pacman": "exit 1"})
    (home.parent / "system-share").mkdir()
    return home


def _no_package_bin(home: Path) -> Path:
    return home.parent / "no-package-bin"


def _fake_tools(directory: Path, fakes: dict[str, str]) -> None:
    directory.mkdir(exist_ok=True)
    for name, body in fakes.items():
        (directory / name).write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
        (directory / name).chmod(0o755)


def _env(home: Path, **extra: str) -> dict[str, str]:
    python_dir = str(Path(sys.executable).resolve().parent)
    return {
        "HOME": str(home),
        "PATH": f"{home / '.local' / 'bin'}:{_no_package_bin(home)}:{python_dir}:/usr/bin:/bin",
        "XDG_DATA_DIRS": str(home.parent / "system-share"),
        "FORK_LINUX_PYTHON": sys.executable,
        "LANG": "C.UTF-8",
        "TMPDIR": str(home.parent),
        **extra,
    }


def _run(script: Path, home: Path, *args: str, **extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(script), *args],
        env=_env(home, **extra),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


@pytest.fixture
def release(http_server):
    """The local release server: /download/vX/... and /latest/download/..."""
    name = f"fork-linux-{VERSION}-x86_64.tar.gz"
    data = _tarball()
    for base in (f"/download/v{VERSION}", "/latest/download"):
        http_server.add(f"{base}/{name}", data)
        http_server.add(f"{base}/SHA256SUMS", _sums(name, data))
    return http_server


def _snapshot(path: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(path)): p.read_bytes()
        for p in sorted(path.rglob("*"))
        if p.is_file() and not p.is_symlink()
    }


def _skip_if_root() -> None:
    if os.geteuid() == 0:
        pytest.skip("install.sh refuses per-user installs as root; run the tests as a normal user")


def _need_downloader() -> None:
    _skip_if_root()
    if shutil.which("curl") is None and shutil.which("wget") is None:
        pytest.skip("install.sh downloads with curl or wget; neither is installed")


def test_scripts_are_valid_bash() -> None:
    for script in (INSTALL, UNINSTALL):
        subprocess.run(["bash", "-n", str(script)], check=True)


def _top_level_lines(script: Path) -> list[str]:
    """Unindented, non-comment lines outside heredocs."""
    out: list[str] = []
    heredoc_end = None
    for line in script.read_text(encoding="utf-8").splitlines():
        if heredoc_end is not None:
            if line.strip() == heredoc_end:
                heredoc_end = None
            continue
        if "<<'" in line or "<<EOF" in line:
            heredoc_end = line.split("<<", 1)[1].strip().strip("'\"").split()[0]
        if line and not line[0].isspace() and not line.startswith("#"):
            out.append(line)
    return out


@pytest.mark.parametrize("script", [INSTALL, UNINSTALL], ids=["install", "uninstall"])
def test_whole_body_is_inside_main(script: Path) -> None:
    """curl | bash safety: the top level only defines functions; the last line calls main."""
    lines = _top_level_lines(script)
    assert lines[-1] == 'main "$@"'
    assert lines[0] == "main() {"
    for line in lines[:-1]:
        assert line.endswith("() {") or line == "}", f"{script.name}: top-level statement: {line!r}"


def test_truncated_download_runs_nothing(tmp_path: Path, home: Path) -> None:
    _skip_if_root()
    text = INSTALL.read_text(encoding="utf-8")
    half = tmp_path / "half.sh"
    half.write_text(text[: len(text) // 2], encoding="utf-8")
    before = _snapshot(home)
    subprocess.run(["bash", str(half)], env=_env(home), capture_output=True, text=True, check=False)
    assert _snapshot(home) == before


def test_install_from_release_server(home: Path, release) -> None:
    _need_downloader()
    result = _run(INSTALL, home, "--no-deps", FORK_LINUX_RELEASES_URL=release.url(""))
    assert result.returncode == 0, result.stderr
    tree = home / ".local" / "opt" / "fork-linux"
    assert (tree / "bin" / "fork-linux").is_file()
    assert (tree / ".fork-linux-install").read_text(encoding="utf-8").strip() == VERSION
    for name in ("fork-linux", "fork"):
        link = home / ".local" / "bin" / name
        assert link.is_symlink()
        assert os.readlink(link) == str(tree / "bin" / name)
    assert (home / ".local" / "share" / "man" / "man1" / "fork-linux.1").is_file()
    assert (home / ".local" / "share" / "bash-completion" / "completions" / "fork").is_symlink()
    assert (home / ".local" / "share" / "metainfo" / f"{APP_ID}.metainfo.xml").is_file()
    # The per-user menu entry is fork-linux's own job; install.sh never writes it directly.
    assert not (home / ".local" / "share" / "applications" / f"{APP_ID}.desktop").exists()
    calls = (home / "cli-calls.log").read_text(encoding="utf-8").splitlines()
    # The file-manager actions follow fork-linux's default (--file-managers auto: the installed ones).
    assert "fork-linux desktop install" in calls
    assert "sha256 OK" in result.stdout
    manifest = (tree / "install-manifest.txt").read_text(encoding="utf-8")
    assert f"version\t{VERSION}" in manifest
    assert "desktop\tfork-linux" in manifest
    assert (home / ".wine" / "E2E_SENTINEL").read_text(encoding="utf-8") == "keep\n"


def test_install_pinned_version_and_upgrade(home: Path, release) -> None:
    _need_downloader()
    first = _run(INSTALL, home, "--no-deps", "--version", VERSION, FORK_LINUX_RELEASES_URL=release.url(""))
    assert first.returncode == 0, first.stderr
    again = _run(INSTALL, home, "--no-deps", FORK_LINUX_RELEASES_URL=release.url(""))
    assert again.returncode == 0, again.stderr
    assert "upgrading the existing install" in again.stdout
    assert (home / ".local" / "bin" / "fork-linux").is_symlink()


def test_checksum_mismatch_installs_nothing(home: Path, http_server) -> None:
    _need_downloader()
    name = f"fork-linux-{VERSION}-x86_64.tar.gz"
    http_server.add(f"/latest/download/{name}", _tarball())
    http_server.add("/latest/download/SHA256SUMS", _sums(name, b"something else"))
    result = _run(INSTALL, home, "--no-deps", FORK_LINUX_RELEASES_URL=http_server.url(""))
    assert result.returncode != 0
    assert "sha256 mismatch" in result.stderr
    assert not (home / ".local" / "opt").exists()
    assert not (home / ".local" / "bin").exists()


def test_refuses_plain_http_to_other_hosts(home: Path) -> None:
    _skip_if_root()
    result = _run(INSTALL, home, "--no-deps", FORK_LINUX_RELEASES_URL="http://example.invalid/releases")
    assert result.returncode != 0
    assert "non-HTTPS" in result.stderr


def test_dry_run_changes_nothing(home: Path, release) -> None:
    _need_downloader()
    before = _snapshot(home)
    result = _run(INSTALL, home, "--no-deps", "--dry-run", FORK_LINUX_RELEASES_URL=release.url(""))
    assert result.returncode == 0, result.stderr
    assert "dry-run:" in result.stdout
    assert _snapshot(home) == before
    assert not (home / ".local" / "opt").exists()


def test_from_tarball_verifies_against_local_sums(tmp_path: Path, home: Path) -> None:
    _skip_if_root()
    name = f"fork-linux-{VERSION}-x86_64.tar.gz"
    # One build: gzip stamps the current second into the header, so two calls can differ.
    tarball = _tarball()
    (tmp_path / name).write_bytes(tarball)
    (tmp_path / "SHA256SUMS").write_bytes(_sums(name, b"tampered"))
    bad = _run(INSTALL, home, "--no-deps", "--from-tarball", str(tmp_path / name))
    assert bad.returncode != 0
    assert "sha256 mismatch" in bad.stderr
    # The user's own file is never deleted, only a download is.
    assert f"{tmp_path / name} was left in place" in bad.stderr
    assert (tmp_path / name).read_bytes() == tarball
    assert not (home / ".local" / "opt").exists()
    (tmp_path / "SHA256SUMS").write_bytes(_sums(name, tarball))
    good = _run(INSTALL, home, "--no-deps", "--no-desktop", "--from-tarball", str(tmp_path / name))
    assert good.returncode == 0, good.stderr
    assert not (home / "cli-calls.log").exists() or "desktop install" not in (home / "cli-calls.log").read_text(
        encoding="utf-8"
    )


def test_rejects_bad_options(home: Path) -> None:
    _skip_if_root()
    assert _run(INSTALL, home, "--bogus").returncode != 0
    assert _run(INSTALL, home, "--version", "1.2").returncode != 0
    result = _run(INSTALL, home, "--system")
    assert result.returncode != 0
    assert "needs root" in result.stderr
    assert _run(INSTALL, home, "--help").returncode == 0


def test_uninstall_removes_exactly_what_was_installed(home: Path, release) -> None:
    _need_downloader()
    before = _snapshot(home)
    assert _run(INSTALL, home, "--no-deps", FORK_LINUX_RELEASES_URL=release.url("")).returncode == 0
    # A file the user changed after the install is kept.
    changed = home / ".local" / "share" / "man" / "man1" / "fork.1"
    changed.write_text("my notes\n", encoding="utf-8")
    result = _run(UNINSTALL, home)
    assert result.returncode == 0, result.stderr
    assert not (home / ".local" / "opt" / "fork-linux").exists()
    assert not (home / ".local" / "bin" / "fork-linux").exists()
    assert not (home / ".local" / "share" / "man" / "man1" / "fork-linux.1").exists()
    assert changed.read_text(encoding="utf-8") == "my notes\n"
    assert "fork-linux uninstall" in (home / "cli-calls.log").read_text(encoding="utf-8")
    after = _snapshot(home)
    after.pop("cli-calls.log", None)
    after.pop(".local/share/man/man1/fork.1", None)
    assert after == before  # ~/.wine and the user's fork-linux data are untouched


def test_uninstall_purge_delegates_to_fork_linux(home: Path, release) -> None:
    _need_downloader()
    assert _run(INSTALL, home, "--no-deps", FORK_LINUX_RELEASES_URL=release.url("")).returncode == 0
    result = _run(UNINSTALL, home, "--purge", "--yes")
    assert result.returncode == 0, result.stderr
    assert "fork-linux uninstall --purge --yes" in (home / "cli-calls.log").read_text(encoding="utf-8")
    assert (home / ".wine" / "E2E_SENTINEL").is_file()


def test_uninstall_without_install_is_a_no_op(home: Path) -> None:
    _skip_if_root()
    before = _snapshot(home)
    result = _run(UNINSTALL, home)
    assert result.returncode == 0
    assert "nothing to do" in result.stdout
    assert _snapshot(home) == before


def test_uninstall_points_a_dpkg_install_at_apt(tmp_path: Path, home: Path) -> None:
    _skip_if_root()
    fake_bin = tmp_path / "fake-bin"
    _fake_tools(fake_bin, {"dpkg-query": "printf 'install ok installed'"})
    before = _snapshot(home)
    result = _run(UNINSTALL, home, PATH=f"{fake_bin}:{_env(home)['PATH']}")
    assert result.returncode != 0
    assert "fork-linux was installed with apt/dpkg: sudo apt remove fork-linux" in result.stderr
    assert _snapshot(home) == before


def test_uninstall_dry_run_keeps_everything(home: Path, release) -> None:
    _need_downloader()
    assert _run(INSTALL, home, "--no-deps", FORK_LINUX_RELEASES_URL=release.url("")).returncode == 0
    before = _snapshot(home)
    result = _run(UNINSTALL, home, "--dry-run")
    assert result.returncode == 0, result.stderr
    assert _snapshot(home) == before


def test_shellcheck_clean() -> None:
    if shutil.which("shellcheck") is None:
        pytest.skip("shellcheck is not installed (the lint stage runs it)")
    subprocess.run(["shellcheck", "-x", str(INSTALL), str(UNINSTALL)], check=True, cwd=ROOT)


# -- upgrade scenarios (docs/qa/UPDATE-SCENARIOS.md) ------------------------------------------


def _local_tarball(directory: Path, version: str, extra: dict[str, tuple[bytes, int]] | None = None) -> Path:
    """``fork-linux-VERSION-x86_64.tar.gz`` with its SHA256SUMS in ``directory``."""
    directory.mkdir(parents=True, exist_ok=True)
    name = f"fork-linux-{version}-x86_64.tar.gz"
    data = _tarball(version, extra)
    (directory / name).write_bytes(data)
    (directory / "SHA256SUMS").write_bytes(_sums(name, data))
    return directory / name


def test_a1_upgrade_from_previous_version_keeps_user_state(tmp_path: Path, home: Path) -> None:
    """N-1 -> N with install.sh: the tree, links and manifest move to N; the user's state stays."""
    _skip_if_root()
    old = _run(INSTALL, home, "--no-deps", "--from-tarball", str(_local_tarball(tmp_path / "n-1", "9.8.6")))
    assert old.returncode == 0, old.stderr
    user_files = {
        home / ".config" / "fork-linux" / "config.ini": b"[fork]\nupdate_policy = pinned\n",
        home / ".local" / "share" / "fork-linux" / "prefix" / ".fork-linux" / "state.json": b'{"schema": 1}\n',
        home / ".local" / "share" / "fork-linux" / "integrations.json": b'{"files": []}\n',
    }
    for path, data in user_files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    edited = home / ".local" / "share" / "man" / "man1" / "fork.1"
    edited.write_text("my notes\n", encoding="utf-8")
    before = _snapshot(home)
    new = _run(INSTALL, home, "--no-deps", "--from-tarball", str(_local_tarball(tmp_path / "n", VERSION)))
    assert new.returncode == 0, new.stderr
    assert "upgrading the existing install" in new.stdout
    assert f"keeping {edited}" in new.stderr
    dest = home / ".local" / "opt" / "fork-linux"
    assert (dest / ".fork-linux-install").read_text(encoding="utf-8") == f"{VERSION}\n"
    assert f"version\t{VERSION}\n" in (dest / "install-manifest.txt").read_text(encoding="utf-8")
    assert os.readlink(home / ".local" / "bin" / "fork-linux") == str(dest / "bin" / "fork-linux")
    version = subprocess.run(
        [sys.executable, str(home / ".local" / "bin" / "fork-linux"), "--version"],
        capture_output=True, text=True, check=True, env=_env(home),
    )
    assert version.stdout.strip() == f"fork-linux {VERSION} (tarball)"
    after = _snapshot(home)
    for path, data in user_files.items():
        assert path.read_bytes() == data
    assert edited.read_text(encoding="utf-8") == "my notes\n"
    for key in (".wine/E2E_SENTINEL", ".local/share/fork-linux/E2E_MARKER"):
        assert after[key] == before[key]
    calls = (home / "cli-calls.log").read_text(encoding="utf-8").splitlines()
    assert calls.count("fork-linux desktop install") == 2, "the menu entry is refreshed by the new release"
    assert not list(dest.parent.glob("fork-linux.*")), "no staging or retired copy is left"


def test_a14_failed_upgrade_keeps_the_previous_install(tmp_path: Path, home: Path) -> None:
    """UPD-7: an upgrade that fails while copying leaves the previous install untouched."""
    _skip_if_root()
    quiet = ("--no-deps", "--no-desktop", "--from-tarball")
    first = _run(INSTALL, home, *quiet, str(_local_tarball(tmp_path / "a", VERSION)))
    assert first.returncode == 0, first.stderr
    dest = home / ".local" / "opt" / "fork-linux"
    installed = _snapshot(dest)
    broken = _local_tarball(tmp_path / "b", "9.9.0", {"share/fork-linux/unreadable.dat": (b"x", 0o000)})
    failed = _run(INSTALL, home, *quiet, str(broken))
    assert failed.returncode != 0
    assert _snapshot(dest) == installed
    assert (home / ".local" / "bin" / "fork-linux").is_symlink()
    assert (dest.parent / "fork-linux.new" / ".fork-linux-install").is_file(), "the partial copy is marked as ours"
    fixed = _run(INSTALL, home, *quiet, str(_local_tarball(tmp_path / "c", "9.9.0")))
    assert fixed.returncode == 0, fixed.stderr
    assert (dest / ".fork-linux-install").read_text(encoding="utf-8") == "9.9.0\n"
    assert not list(dest.parent.glob("fork-linux.*"))


@pytest.mark.parametrize("leftover", ["fork-linux.new", "fork-linux.old"])
def test_a14_refuses_a_staging_path_that_is_not_ours(tmp_path: Path, home: Path, leftover: str) -> None:
    _skip_if_root()
    foreign = home / ".local" / "opt" / leftover
    foreign.mkdir(parents=True)
    (foreign / "mine.txt").write_text("keep\n", encoding="utf-8")
    result = _run(INSTALL, home, "--no-deps", "--from-tarball", str(_local_tarball(tmp_path / "a", VERSION)))
    assert result.returncode != 0
    assert f"{foreign} exists but was not created by install.sh" in result.stderr
    assert (foreign / "mine.txt").read_text(encoding="utf-8") == "keep\n"
    assert not (home / ".local" / "opt" / "fork-linux").exists()


def test_a14_uninstall_removes_leftovers_of_an_interrupted_upgrade(tmp_path: Path, home: Path) -> None:
    _skip_if_root()
    tarball = _local_tarball(tmp_path / "a", VERSION)
    assert _run(INSTALL, home, "--no-deps", "--no-desktop", "--from-tarball", str(tarball)).returncode == 0
    opt = home / ".local" / "opt"
    for name in ("fork-linux.new", "fork-linux.old"):
        (opt / name).mkdir()
        (opt / name / ".fork-linux-install").write_text("9.9.0\n", encoding="utf-8")
    (opt / "fork-linux.other").mkdir()
    result = _run(UNINSTALL, home)
    assert result.returncode == 0, result.stderr
    assert sorted(p.name for p in opt.iterdir()) == ["fork-linux.other"]


@pytest.mark.parametrize(
    ("owner", "fakes"),
    [
        ("dpkg", {"dpkg-query": "printf 'install ok installed'"}),
        ("rpm", {"dpkg-query": "exit 1", "rpm": "exit 0"}),
        ("pacman", {"dpkg-query": "exit 1", "rpm": "exit 1", "pacman": "exit 0"}),
    ],
)
def test_a13_refuses_to_shadow_a_package_install(
    tmp_path: Path, home: Path, owner: str, fakes: dict[str, str]
) -> None:
    _skip_if_root()
    fake_bin = tmp_path / "fake-bin"
    _fake_tools(fake_bin, fakes)
    path = f"{fake_bin}:{_env(home)['PATH']}"
    result = _run(INSTALL, home, "--no-deps", "--from-tarball", str(_local_tarball(tmp_path / "a", VERSION)), PATH=path)
    assert result.returncode != 0
    assert f"fork-linux is installed by your package manager ({owner})" in result.stderr
    assert not (home / ".local" / "opt").exists()
    assert not (home / ".local" / "bin").exists()
