"""Tests for fork_linux.hostdeps: os-release, tools, libraries, ldd scan and install hints."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from fork_linux import hostdeps
from fork_linux.errors import ForkLinuxError
from fork_linux.procrun import Completed, RecordingRunner, Runner

UBUNTU = """PRETTY_NAME="Ubuntu 26.04 LTS"
NAME="Ubuntu"
VERSION_ID="26.04"
ID=ubuntu
ID_LIKE=debian
"""


def _os_release(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "os-release"
    path.write_text(text, encoding="utf-8")
    return path


# --------------------------------------------------------------------------- distro


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (UBUNTU, hostdeps.DistroInfo("ubuntu", ("debian",), "debian", "Ubuntu 26.04 LTS")),
        (
            'ID=debian\nPRETTY_NAME="Debian GNU/Linux 13 (trixie)"\n',
            hostdeps.DistroInfo("debian", (), "debian", "Debian GNU/Linux 13 (trixie)"),
        ),
        (
            'ID=linuxmint\nID_LIKE="ubuntu debian"\nNAME="Linux Mint"\n',
            hostdeps.DistroInfo("linuxmint", ("ubuntu", "debian"), "debian", "Linux Mint"),
        ),
        ("ID=fedora\nPRETTY_NAME='Fedora Linux 44'\n", hostdeps.DistroInfo("fedora", (), "fedora", "Fedora Linux 44")),
        (
            'ID="rocky"\nID_LIKE="rhel centos fedora"\n',
            hostdeps.DistroInfo("rocky", ("rhel", "centos", "fedora"), "fedora", "Linux"),
        ),
        (
            'ID="opensuse-tumbleweed"\nID_LIKE="opensuse suse"\n',
            hostdeps.DistroInfo("opensuse-tumbleweed", ("opensuse", "suse"), "suse", "Linux"),
        ),
        ("ID=opensuse-slowroll\n", hostdeps.DistroInfo("opensuse-slowroll", (), "suse", "Linux")),
        ("ID=arch\n", hostdeps.DistroInfo("arch", (), "arch", "Linux")),
        ("ID=manjaro\nID_LIKE=arch\n", hostdeps.DistroInfo("manjaro", ("arch",), "arch", "Linux")),
        ("ID=nixos\n", hostdeps.DistroInfo("nixos", (), "unknown", "Linux")),
        ("ID=Ubuntu\n", hostdeps.DistroInfo("ubuntu", (), "debian", "Linux")),
        ("", hostdeps.DistroInfo("linux", (), "unknown", "Linux")),
    ],
)
def test_distro(tmp_path: Path, text: str, expected: hostdeps.DistroInfo) -> None:
    assert hostdeps.distro(_os_release(tmp_path, text)) == expected


def test_distro_missing_file(tmp_path: Path) -> None:
    assert hostdeps.distro(tmp_path / "nope") == hostdeps.DistroInfo("linux", (), "unknown", "Linux")


def test_distro_default_path_is_readable() -> None:
    assert hostdeps.distro().family in hostdeps.FAMILIES


def test_parse_os_release_quoting_and_noise() -> None:
    text = '# comment\n\nnot a pair\n=novalue\nA="x \\"y\\" \\$z \\`w\\` \\\\"\nB=\'single "q"\'\nC= spaced \nD="\n'
    assert hostdeps.parse_os_release(text) == {"A": 'x "y" $z `w` \\', "B": 'single "q"', "C": "spaced", "D": '"'}


def test_family_of() -> None:
    assert hostdeps.family_of("pop", ["ubuntu", "debian"]) == "debian"
    assert hostdeps.family_of("sles", []) == "suse"
    assert hostdeps.family_of("gentoo", []) == "unknown"


# --------------------------------------------------------------------------- tools and libraries


def test_missing_tools() -> None:
    runner = RecordingRunner(which_map={"cabextract": "/usr/bin/cabextract", "unzip": None, "7z": None})
    assert hostdeps.missing_tools(runner, ["cabextract", "unzip", "7z"]) == ["unzip", "7z"]
    assert hostdeps.missing_tools(runner, []) == []


def test_missing_libs_with_loader() -> None:
    loaded = []

    def loader(name: str) -> object:
        loaded.append(name)
        if name.startswith("libX"):
            raise OSError(f"{name}: cannot open shared object file")
        return object()

    assert hostdeps.missing_libs(["libfreetype.so.6", "libX11.so.6", "libXi.so.6"], loader) == [
        "libX11.so.6",
        "libXi.so.6",
    ]
    assert loaded == ["libfreetype.so.6", "libX11.so.6", "libXi.so.6"]


def test_missing_libs_with_ctypes() -> None:
    assert hostdeps.missing_libs(["libc.so.6", "libfork-linux-does-not-exist.so.99"]) == [
        "libfork-linux-does-not-exist.so.99"
    ]


def test_every_family_maps_every_known_name() -> None:
    names = {*hostdeps.REQUIRED_TOOLS, *hostdeps.OPTIONAL_TOOLS, *hostdeps.REQUIRED_LIBS, *hostdeps.OPTIONAL_LIBS}
    assert set(hostdeps.PACKAGES) == set(hostdeps.INSTALL_COMMANDS) == set(hostdeps.FAMILIES) - {"unknown"}
    for family, table in hostdeps.PACKAGES.items():
        assert set(table) == names, family
        assert all(table.values()), family


# --------------------------------------------------------------------------- ldd


LDD_OUTPUT = """/r/lib/wine/x86_64-unix/winegstreamer.so:
\tlinux-vdso.so.1 (0x00007ffd)
\tlibgstreamer-1.0.so.0 => not found
\tlibc.so.6 => /lib/x86_64-linux-gnu/libc.so.6 (0x00007f)
/r/lib/wine/x86_64-unix/winedmo.so:
\tlibavcodec.so.61 => not found
\tlibavformat.so.61 => not found
\tlibavutil.so.59 => not found
\tntdll.so => not found
/r/lib/wine/x86_64-unix/wpcap.so:
\tlibpcap.so.1 => not found
\twin32u.so => not found
\tlibgnutls.so.30 => not found
/r/lib/wine/x86_64-unix/other.so:
\tlibgstreamer-1.0.so.0 => not found
\tstatically linked
\t => not found
"""


def test_parse_ldd() -> None:
    assert hostdeps.parse_ldd(LDD_OUTPUT) == ["libgnutls.so.30", "libgstreamer-1.0.so.0"]
    assert hostdeps.parse_ldd("") == []


def test_ldd_missing_without_unix_libs(tmp_path: Path) -> None:
    runner = RecordingRunner()
    assert hostdeps.ldd_missing(runner, tmp_path) == []
    (tmp_path / "lib" / "wine" / "x86_64-unix").mkdir(parents=True)
    assert hostdeps.ldd_missing(runner, tmp_path) == []
    assert runner.calls == []


@pytest.mark.parametrize("layout", hostdeps.UNIX_LIB_DIRS)
def test_ldd_missing_scans_every_module(tmp_path: Path, layout: str) -> None:
    lib_dir = tmp_path / layout
    lib_dir.mkdir(parents=True)
    for name in ("winex11.so", "ntdll.so", "notes.txt"):
        (lib_dir / name).write_text("", encoding="utf-8")
    runner = RecordingRunner({"ldd": LDD_OUTPUT})
    assert hostdeps.ldd_missing(runner, tmp_path) == ["libgnutls.so.30", "libgstreamer-1.0.so.0"]
    assert runner.argvs == [["ldd", str(lib_dir / "ntdll.so"), str(lib_dir / "winex11.so")]]
    assert runner.calls[0]["timeout"] == hostdeps.LDD_TIMEOUT


def test_ldd_missing_when_ldd_is_absent(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    lib_dir = tmp_path / "lib" / "wine" / "x86_64-unix"
    lib_dir.mkdir(parents=True)
    (lib_dir / "ntdll.so").write_text("", encoding="utf-8")
    runner = RecordingRunner({"ldd": Completed([], 127, "", "cannot execute ldd\n")})
    with caplog.at_level(logging.INFO, logger="fork_linux.hostdeps"):
        assert hostdeps.ldd_missing(runner, tmp_path) == []
    assert "ldd is not available" in caplog.text


def test_ldd_missing_when_ldd_times_out(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    lib_dir = tmp_path / "lib" / "wine" / "x86_64-unix"
    lib_dir.mkdir(parents=True)
    (lib_dir / "ntdll.so").write_text("", encoding="utf-8")

    def hang(argv: list[str]) -> Completed:
        raise ForkLinuxError("ldd timed out after 60 s")

    with caplog.at_level(logging.WARNING, logger="fork_linux.hostdeps"):
        assert hostdeps.ldd_missing(RecordingRunner({"ldd": hang}), tmp_path) == []
    assert "could not scan" in caplog.text and "timed out" in caplog.text


def test_ldd_missing_with_fake_ldd(fake_bin: Path, tmp_path: Path) -> None:
    lib_dir = tmp_path / "lib" / "wine" / "x86_64-unix"
    lib_dir.mkdir(parents=True)
    (lib_dir / "winex11.so").write_text("libX11.so.6\n!libXcursor.so.1\nntdll.so\n", encoding="utf-8")
    (lib_dir / "winedmo.so").write_text("!libavcodec.so.61\n!ntdll.so\n!libXcursor.so.1\n", encoding="utf-8")
    assert hostdeps.ldd_missing(Runner(), tmp_path) == ["libXcursor.so.1"]
    logged = [json.loads(line) for line in fake_bin.read_text(encoding="utf-8").splitlines()]
    assert logged[0]["argv"][0] == "ldd"
    assert len(logged[0]["argv"]) == 3


# --------------------------------------------------------------------------- install hints


def test_install_hint_debian() -> None:
    hint = hostdeps.install_hint("debian", ["cabextract", "7z"], ["libgnutls.so.30", "libX11.so.6"])
    assert hint.splitlines() == [
        "sudo apt install cabextract 7zip libgnutls30t64 libx11-6",
        "(on older releases install p7zip-full instead of 7zip)",
        "(on older releases install libgnutls30 instead of libgnutls30t64)",
    ]


@pytest.mark.parametrize(
    ("family", "command"),
    [
        ("fedora", "sudo dnf install cabextract unzip gnutls libglvnd-glx libglvnd-egl"),
        ("suse", "sudo zypper install cabextract unzip libgnutls30 libGL1 libEGL1"),
        ("arch", "sudo pacman -S cabextract unzip gnutls libglvnd"),
    ],
)
def test_install_hint_other_families(family: str, command: str) -> None:
    hint = hostdeps.install_hint(family, ["cabextract", "unzip"], ["libgnutls.so.30", "libGL.so.1", "libEGL.so.1"])
    assert hint.splitlines()[0] == command


def test_install_hint_deduplicates_packages() -> None:
    assert hostdeps.install_hint("arch", [], ["libGL.so.1", "libEGL.so.1"]) == "sudo pacman -S libglvnd"
    assert hostdeps.install_hint("debian", ["7z", "7z"], []).count("p7zip-full") == 1


def test_install_hint_unmapped_names() -> None:
    assert hostdeps.install_hint("debian", ["unzip"], ["libweird.so.1"]).splitlines() == [
        "sudo apt install unzip",
        "also install the packages that provide: libweird.so.1",
    ]
    assert hostdeps.install_hint("fedora", ["frobnicate"], []) == "also install the packages that provide: frobnicate"


def test_install_hint_unknown_family() -> None:
    assert hostdeps.install_hint("unknown", ["cabextract"], ["libGL.so.1"]) == (
        "install these with your distribution's package manager: cabextract, libGL.so.1"
    )


def test_install_hint_nothing_missing() -> None:
    assert hostdeps.install_hint("debian", [], []) == ""
    assert hostdeps.install_hint("unknown", iter(()), iter(())) == ""
