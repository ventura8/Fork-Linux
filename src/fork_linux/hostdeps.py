"""Host prerequisites for Wine and setup: distribution family, tools, shared libraries, install hints.

Checks never need root: tools are looked up on ``PATH``, libraries are probed
by loading their soname with :class:`ctypes.CDLL` (what Wine's loader will do)
and a Wine runtime's Unix libraries are scanned with ``ldd``. The hint names
the packages for the user's package manager; we never run it ourselves.
"""

from __future__ import annotations

import ctypes
import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .errors import ForkLinuxError
from .procrun import Runner

log = logging.getLogger(__name__)

FAMILIES = ("debian", "fedora", "suse", "arch", "unknown")
OS_RELEASE = Path("/etc/os-release")
LDD_TIMEOUT = 60.0

REQUIRED_TOOLS = ("cabextract", "unzip")
# winetricks downloads .NET and the core fonts itself and needs one of these.
DOWNLOADERS = ("curl", "wget", "aria2c")
OPTIONAL_TOOLS = ("7z", "zenity", "kdialog", "xdg-open", "git", "xdotool", "fc-list")

REQUIRED_LIBS = (
    "libfreetype.so.6",
    "libfontconfig.so.1",
    "libgnutls.so.30",
    "libX11.so.6",
    "libXext.so.6",
    "libXrender.so.1",
    "libXi.so.6",
    "libXrandr.so.2",
    "libXcursor.so.1",
    "libXcomposite.so.1",
    "libXinerama.so.1",
    "libXfixes.so.3",
    "libGL.so.1",
)
OPTIONAL_LIBS = (
    "libvulkan.so.1",
    "libdbus-1.so.3",
    "libasound.so.2",
    "libpulse.so.0",
    "libxkbcommon.so.0",
    "libwayland-client.so.0",
    "libEGL.so.1",
)

# ``ldd`` "not found" entries that are expected: optional media codecs (winedmo),
# libpcap (wpcap) and Wine-internal modules its own loader resolves.
IGNORED_LDD = ("libavcodec", "libavformat", "libavutil", "libpcap", "ntdll.so", "win32u.so")
# Where a Wine root keeps its 64-bit Unix libraries (upstream/Kron4ek, Fedora/SUSE, Debian).
UNIX_LIB_DIRS = ("lib/wine/x86_64-unix", "lib64/wine/x86_64-unix", "lib/x86_64-linux-gnu/wine/x86_64-unix")

# Package names per family. ``a|b`` lists alternatives, newest name first
# (Debian's 64-bit time_t transition renamed some libraries).
# Library or tool -> its package on debian, fedora, suse and arch.
_PACKAGE_TABLE: tuple[tuple[str, str, str, str, str], ...] = (
    ("libfreetype.so.6", "libfreetype6", "freetype", "libfreetype6", "freetype2"),
    ("libfontconfig.so.1", "libfontconfig1", "fontconfig", "fontconfig", "fontconfig"),
    ("libgnutls.so.30", "libgnutls30t64|libgnutls30", "gnutls", "libgnutls30", "gnutls"),
    ("libX11.so.6", "libx11-6", "libX11", "libX11-6", "libx11"),
    ("libXext.so.6", "libxext6", "libXext", "libXext6", "libxext"),
    ("libXrender.so.1", "libxrender1", "libXrender", "libXrender1", "libxrender"),
    ("libXi.so.6", "libxi6", "libXi", "libXi6", "libxi"),
    ("libXrandr.so.2", "libxrandr2", "libXrandr", "libXrandr2", "libxrandr"),
    ("libXcursor.so.1", "libxcursor1", "libXcursor", "libXcursor1", "libxcursor"),
    ("libXcomposite.so.1", "libxcomposite1", "libXcomposite", "libXcomposite1", "libxcomposite"),
    ("libXinerama.so.1", "libxinerama1", "libXinerama", "libXinerama1", "libxinerama"),
    ("libXfixes.so.3", "libxfixes3", "libXfixes", "libXfixes3", "libxfixes"),
    ("libGL.so.1", "libgl1", "libglvnd-glx", "libGL1", "libglvnd"),
    ("libvulkan.so.1", "libvulkan1", "vulkan-loader", "libvulkan1", "vulkan-icd-loader"),
    ("libdbus-1.so.3", "libdbus-1-3", "dbus-libs", "libdbus-1-3", "dbus"),
    ("libasound.so.2", "libasound2t64|libasound2", "alsa-lib", "libasound2", "alsa-lib"),
    ("libpulse.so.0", "libpulse0", "pulseaudio-libs", "libpulse0", "libpulse"),
    ("libxkbcommon.so.0", "libxkbcommon0", "libxkbcommon", "libxkbcommon0", "libxkbcommon"),
    ("libwayland-client.so.0", "libwayland-client0", "libwayland-client", "libwayland-client0", "wayland"),
    ("libEGL.so.1", "libegl1", "libglvnd-egl", "libEGL1", "libglvnd"),
    ("cabextract", "cabextract", "cabextract", "cabextract", "cabextract"),
    ("unzip", "unzip", "unzip", "unzip", "unzip"),
    ("curl", "curl", "curl", "curl", "curl"),
    ("7z", "7zip|p7zip-full", "7zip|p7zip-plugins", "7zip|p7zip-full", "7zip|p7zip"),
    ("zenity", "zenity", "zenity", "zenity", "zenity"),
    ("kdialog", "kdialog", "kdialog", "kdialog", "kdialog"),
    ("xdg-open", "xdg-utils", "xdg-utils", "xdg-utils", "xdg-utils"),
    ("git", "git", "git", "git", "git"),
    ("xdotool", "xdotool", "xdotool", "xdotool", "xdotool"),
    ("fc-list", "fontconfig", "fontconfig", "fontconfig", "fontconfig"),
)
_TABLE_FAMILIES = ("debian", "fedora", "suse", "arch")
PACKAGES: dict[str, dict[str, str]] = {
    family: {row[0]: row[column] for row in _PACKAGE_TABLE} for column, family in enumerate(_TABLE_FAMILIES, 1)
}

INSTALL_COMMANDS = {
    "debian": "sudo apt install",
    "fedora": "sudo dnf install",
    "suse": "sudo zypper install",
    "arch": "sudo pacman -S",
}

# os-release IDs that identify a family, checked against ID first, then ID_LIKE.
_FAMILY_IDS: tuple[tuple[str, frozenset[str]], ...] = (
    ("debian", frozenset({"debian", "ubuntu"})),
    ("fedora", frozenset({"fedora", "rhel", "centos"})),
    ("suse", frozenset({"suse", "opensuse", "sles", "sled", "opensuse-leap", "opensuse-tumbleweed"})),
    ("arch", frozenset({"arch", "archlinux"})),
)
_UNESCAPE = {'\\"': '"', "\\\\": "\\", "\\$": "$", "\\`": "`"}


@dataclass(frozen=True)
class DistroInfo:
    """What ``/etc/os-release`` says about the host distribution."""

    id: str
    like: tuple[str, ...]
    family: str
    pretty: str


def _unquote(value: str) -> str:
    """An os-release value without its shell quoting."""
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        inner = value[1:-1]
        if value[0] == "'":
            return inner
        for escaped, plain in _UNESCAPE.items():
            inner = inner.replace(escaped, plain)
        return inner
    return value


def parse_os_release(text: str) -> dict[str, str]:
    """``KEY=value`` pairs of an os-release file (comments and malformed lines skipped)."""
    values: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        key, sep, value = stripped.partition("=")
        if sep and key and not stripped.startswith("#"):
            values[key.strip()] = _unquote(value)
    return values


def family_of(distro_id: str, like: Sequence[str]) -> str:
    """``debian``, ``fedora``, ``suse``, ``arch`` or ``unknown`` for an os-release ID / ID_LIKE."""
    for candidate in (distro_id, *like):
        for family, ids in _FAMILY_IDS:
            if candidate in ids or (family == "suse" and "suse" in candidate):
                return family
    return "unknown"


def distro(os_release: Path = OS_RELEASE) -> DistroInfo:
    """Read ``os_release``; a missing or unreadable file gives the spec's defaults (``linux``)."""
    try:
        values = parse_os_release(Path(os_release).read_text(encoding="utf-8", errors="replace"))
    except OSError:
        values = {}
    distro_id = values.get("ID", "").lower() or "linux"
    like = tuple(item.lower() for item in values.get("ID_LIKE", "").split())
    return DistroInfo(
        id=distro_id,
        like=like,
        family=family_of(distro_id, like),
        pretty=values.get("PRETTY_NAME", "") or values.get("NAME", "") or "Linux",
    )


def missing_tools(runner: Runner, names: Iterable[str]) -> list[str]:
    """The tools in ``names`` that are not on ``PATH``, in order."""
    return [name for name in names if runner.which(name) is None]


def missing_required_tools(runner: Runner) -> list[str]:
    """Missing :data:`REQUIRED_TOOLS`, plus ``curl`` when none of :data:`DOWNLOADERS` is present."""
    missing = missing_tools(runner, REQUIRED_TOOLS)
    if all(runner.which(name) is None for name in DOWNLOADERS):
        missing.append(DOWNLOADERS[0])
    return missing


def missing_libs(sonames: Iterable[str], loader: Callable[[str], object] | None = None) -> list[str]:
    """The sonames in ``sonames`` the dynamic loader cannot load (``ctypes.CDLL`` by default)."""
    load = ctypes.CDLL if loader is None else loader
    missing = []
    for soname in sonames:
        try:
            load(soname)
        except OSError:
            missing.append(soname)
    return missing


def _unix_lib_dir(wine_root: Path) -> Path | None:
    """The first existing directory with the Wine root's 64-bit Unix libraries."""
    for relative in UNIX_LIB_DIRS:
        candidate = wine_root / relative
        if candidate.is_dir():
            return candidate
    return None


def parse_ldd(output: str) -> list[str]:
    """Sonames reported as ``=> not found`` in ``ldd`` output, minus :data:`IGNORED_LDD`, sorted."""
    found = set()
    for line in output.splitlines():
        name, sep, rest = line.partition("=>")
        soname = name.strip()
        if sep and rest.strip() == "not found" and soname and not soname.startswith(IGNORED_LDD):
            found.add(soname)
    return sorted(found)


def ldd_missing(runner: Runner, wine_root: Path) -> list[str]:
    """Libraries the Wine runtime's Unix modules need but the host lacks (``ldd`` scan).

    Runs one ``ldd`` over ``<root>/lib/wine/x86_64-unix/*.so`` (or the
    distribution layout's equivalent). Only use it on a Wine we trust: ``ldd``
    may execute the binaries it inspects.
    """
    lib_dir = _unix_lib_dir(wine_root)
    if lib_dir is None:
        return []
    modules = sorted(str(path) for path in lib_dir.glob("*.so"))
    if not modules:
        return []
    try:
        completed = runner.run(["ldd", *modules], timeout=LDD_TIMEOUT)
    except ForkLinuxError as exc:
        log.warning("could not scan %s with ldd: %s", lib_dir, exc)
        return []
    if completed.returncode in (126, 127):
        log.info("ldd is not available: %s", completed.stderr.strip())
    return parse_ldd(completed.stdout)


def _split_packages(
    family: str, names: Iterable[str]
) -> tuple[list[str], list[tuple[str, list[str]]], list[str]]:
    """``(packages, alternatives, unmapped)`` for ``names`` in ``family``'s package table."""
    table: Mapping[str, str] = PACKAGES[family]
    packages: list[str] = []
    alternatives: list[tuple[str, list[str]]] = []
    unmapped: list[str] = []
    for name in names:
        entry = table.get(name)
        if entry is None:
            unmapped.append(name)
            continue
        first, *others = entry.split("|")
        if first not in packages:
            packages.append(first)
            if others:
                alternatives.append((first, others))
    return packages, alternatives, unmapped


def install_hint(family: str, tools: Iterable[str], libs: Iterable[str]) -> str:
    """How to install the missing ``tools`` and ``libs`` on ``family`` ("" when nothing is missing)."""
    names = [*tools, *libs]
    if not names:
        return ""
    if family not in INSTALL_COMMANDS:
        return "install these with your distribution's package manager: " + ", ".join(names)
    packages, alternatives, unmapped = _split_packages(family, names)
    lines = []
    if packages:
        lines.append(f"{INSTALL_COMMANDS[family]} {' '.join(packages)}")
    for package, others in alternatives:
        lines.append(f"(on older releases install {' or '.join(others)} instead of {package})")
    if unmapped:
        lines.append("also install the packages that provide: " + ", ".join(unmapped))
    return "\n".join(lines)
