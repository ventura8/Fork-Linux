"""Step ``font_fallback``: Linux fonts for the scripts Selawik lacks, through WPF's composite font.

When Fork's interface font has no glyph for a character (Cyrillic, Greek,
Chinese, Japanese, Korean, ...), WPF falls back to its composite font
``Global User Interface``: ``GlobalUserInterface.CompositeFont`` in the
64-bit framework's ``WPF`` folder, whose ``FontFamilyMap`` entries name
Windows fonts (Segoe UI, Yu Gothic UI, Malgun Gothic, ...) that a Wine prefix
does not have. Without them, Cyrillic and Greek fall back to Wine's narrow
Tahoma and Korean (and some CJK punctuation) is not drawn at all.

Spike S10 Finding 7 (strace of Fork under Wine) showed which file WPF reads:

* .NET Framework 4.8: ``Framework64\\v4.0.30319\\WPF\\GlobalUserInterface.CompositeFont``
  (4.8 installs a second copy under ``WPF\\Fonts\\``, which it never opens);
* .NET Framework 4.7.2 (setup's fallback when ``dotnet48`` fails): only
  ``Framework64\\v4.0.30319\\WPF\\Fonts\\GlobalUserInterface.CompositeFont``
  exists and is read (its PresentationCore builds the folder as the framework's
  ``InstallPath`` + ``WPF\\Fonts\\``).

Fork is x64 only, so the 32-bit ``Framework`` copies are never touched.

The step inserts one marked block of ``FontFamilyMap`` entries at the top of
every ``<FontFamily.FamilyMaps>`` section (one per ``OS`` section of the
collection; maps are matched in order, so ours win) of every copy that
exists. Each entry names only Linux fonts that ``fc-list`` reports, so a
script without an installed font keeps Microsoft's maps. The block is
rebuilt from scratch on every run (idempotent; removing it gives back
Microsoft's file byte for byte), and with no suitable font installed the file
is restored. Every copy is read, patched and checked as XML before anything is
written; when any copy is damaged nothing is written and setup only warns
(the doctor check ``prefix.font_fallback`` reports it). Files are written
atomically, keep their byte-order mark, line endings and mode, and are never
reached through a symlink.
"""

from __future__ import annotations

import logging
import os
import re
import stat
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import fsutil
from ..bootstrap import Ctx, Step
from ..errors import ForkLinuxError
from . import fonts as fonts_step

log = logging.getLogger(__name__)

COMPOSITE_FONT_NAME = "GlobalUserInterface.CompositeFont"
# drive_c\windows\Microsoft.NET\Framework64\v4.0.30319\WPF (64-bit only: Fork is x64).
WPF_DIR_PARTS = ("windows", "Microsoft.NET", "Framework64", "v4.0.30319", "WPF")
# Sub-folders of WPF\ holding a copy WPF may read: WPF\ itself (4.8) and WPF\Fonts\ (4.7.2).
COMPOSITE_FONT_LOCATIONS: tuple[tuple[str, ...], ...] = ((), ("Fonts",))
FRAMEWORK64_WIN = "C:\\windows\\Microsoft.NET\\Framework64\\v4.0.30319\\"
# Microsoft's file is ~265 KB; anything far larger is not a composite font.
MAX_BYTES = 8 * 1024 * 1024
BOM = b"\xef\xbb\xbf"
NAMESPACE = "{http://schemas.microsoft.com/winfx/2006/xaml/composite-font}"
FAMILY_MAPS = "FontFamily.FamilyMaps"
FAMILY_MAPS_LINE = re.compile(r"[ \t]*<FontFamily\.FamilyMaps\s*>[ \t]*\r?\n")
BEGIN = "<!-- fork-linux font_fallback begin"
END = "<!-- fork-linux font_fallback end -->"
BEGIN_LINE = (
    BEGIN + ": Linux fonts for scripts the Windows fonts below would cover; "
    "'fork-linux setup' rewrites this block -->"
)
INDENT = "    "
REPAIR_HINT = "repair the .NET Framework: 'fork-linux setup --reset' builds a fresh prefix"
FORBIDDEN = ("<!DOCTYPE", "<!ENTITY")

# -- which Linux fonts serve which script ----------------------------------------------------------

SANS = ("Noto Sans", "DejaVu Sans", "Liberation Sans")
CJK_JP = ("Noto Sans CJK JP", "Noto Sans JP", "Source Han Sans JP", "Source Han Sans")
CJK_KR = ("Noto Sans CJK KR", "Noto Sans KR", "Source Han Sans KR", "NanumGothic")
CJK_SC = ("Noto Sans CJK SC", "Noto Sans SC", "Source Han Sans SC", "Source Han Sans CN", "WenQuanYi Micro Hei")
CJK_TC = ("Noto Sans CJK TC", "Noto Sans TC", "Source Han Sans TC", "Source Han Sans TW")
CJK_HK = ("Noto Sans CJK HK", "Noto Sans HK", "Source Han Sans HK", "Source Han Sans HC")
CJK_LAST = ("WenQuanYi Zen Hei", "Droid Sans Fallback")
HAN = "Han"
HANGUL = "Hangul"
# Han ideographs, kana, bopomofo, CJK symbols and punctuation, full-width forms.
HAN_RANGES = (
    "2E80-2FDF, 2FF0-303F, 3040-30FF, 3100-312F, 3190-31FF, 3200-33FF, 3400-4DBF, 4E00-9FFF, "
    "F900-FAFF, FE30-FE4F, FF00-FF9F, FFE0-FFEF, 20000-2FA1F"
)
HANGUL_RANGES = "1100-11FF, 3130-318F, A960-A97F, AC00-D7FF, FFA0-FFDF"


@dataclass(frozen=True)
class Script:
    """One ``FontFamilyMap``: ``unicode`` ranges (``language`` only) -> the first installed ``candidates``."""

    name: str
    unicode: str
    candidates: tuple[str, ...]
    language: str = ""


# Language-specific maps come before the generic one of the same ranges (WPF takes the first
# map whose ranges and language match; zh-HK before its parent zh-Hant). Han text without a
# language uses the Japanese variant first, like Microsoft's own "Other" maps.
SCRIPTS: tuple[Script, ...] = (
    Script("Greek", "0370-03FF, 1F00-1FFF", SANS),
    Script("Cyrillic", "0400-052F, 1C80-1C8F, 2DE0-2DFF, A640-A69F", SANS),
    Script("Armenian", "0530-058F, FB13-FB17", ("Noto Sans Armenian", "DejaVu Sans")),
    Script("Hebrew", "0590-05FF, FB1D-FB4F", ("Noto Sans Hebrew", "DejaVu Sans", "Liberation Sans")),
    Script(
        "Arabic",
        "0600-06FF, 0750-077F, 0870-08FF, FB50-FDFF, FE70-FEFF",
        ("Noto Sans Arabic", "Noto Naskh Arabic", "DejaVu Sans"),
    ),
    Script("Devanagari", "0900-097F, A8E0-A8FF", ("Noto Sans Devanagari",)),
    Script("Bengali", "0980-09FF", ("Noto Sans Bengali",)),
    Script("Gurmukhi", "0A00-0A7F", ("Noto Sans Gurmukhi",)),
    Script("Gujarati", "0A80-0AFF", ("Noto Sans Gujarati",)),
    Script("Oriya", "0B00-0B7F", ("Noto Sans Oriya",)),
    Script("Tamil", "0B80-0BFF", ("Noto Sans Tamil",)),
    Script("Telugu", "0C00-0C7F", ("Noto Sans Telugu",)),
    Script("Kannada", "0C80-0CFF", ("Noto Sans Kannada",)),
    Script("Malayalam", "0D00-0D7F", ("Noto Sans Malayalam",)),
    Script("Sinhala", "0D80-0DFF", ("Noto Sans Sinhala",)),
    Script("Thai", "0E00-0E7F", ("Noto Sans Thai", "Noto Sans Thai Looped")),
    Script("Lao", "0E80-0EFF", ("Noto Sans Lao",)),
    Script("Tibetan", "0F00-0FFF", ("Noto Serif Tibetan", "Noto Sans Tibetan")),
    Script("Myanmar", "1000-109F, A9E0-A9FF, AA60-AA7F", ("Noto Sans Myanmar",)),
    Script("Georgian", "10A0-10FF, 1C90-1CBF, 2D00-2D2F", ("Noto Sans Georgian", "DejaVu Sans")),
    Script("Ethiopic", "1200-139F, 2D80-2DDF, AB00-AB2F", ("Noto Sans Ethiopic",)),
    Script("Khmer", "1780-17FF, 19E0-19FF", ("Noto Sans Khmer",)),
    Script(HANGUL, HANGUL_RANGES, CJK_KR + CJK_JP + CJK_SC + CJK_TC + CJK_HK + CJK_LAST),
    Script(HAN, HAN_RANGES, CJK_SC + CJK_TC + CJK_HK + CJK_JP + CJK_KR + CJK_LAST, "zh-Hans"),
    Script(HAN, HAN_RANGES, CJK_HK + CJK_TC + CJK_SC + CJK_JP + CJK_KR + CJK_LAST, "zh-HK"),
    Script(HAN, HAN_RANGES, CJK_TC + CJK_HK + CJK_SC + CJK_JP + CJK_KR + CJK_LAST, "zh-Hant"),
    Script(HAN, HAN_RANGES, CJK_JP + CJK_SC + CJK_TC + CJK_HK + CJK_KR + CJK_LAST, "ja"),
    Script(HAN, HAN_RANGES, CJK_KR + CJK_JP + CJK_SC + CJK_TC + CJK_HK + CJK_LAST, "ko"),
    Script(HAN, HAN_RANGES, CJK_JP + CJK_SC + CJK_TC + CJK_HK + CJK_KR + CJK_LAST),
)
# Scripts whose missing font the doctor reports (the others are rarer in repository names).
KEY_SCRIPTS = ("Greek", "Cyrillic", HANGUL, HAN)
FONTS_HINT = (
    "install the Noto fonts (Debian / Ubuntu: fonts-noto-core fonts-noto-cjk; Fedora: "
    "google-noto-sans-fonts google-noto-sans-cjk-fonts; openSUSE: noto-sans-fonts noto-sans-cjk-fonts; "
    "Arch: noto-fonts noto-fonts-cjk), then run 'fork-linux doctor --fix'"
)


class Unpatchable(ForkLinuxError):
    """A composite font copy is missing or damaged: nothing is written."""


@dataclass(frozen=True)
class FamilyMap:
    """A ``FontFamilyMap`` of the block: ``script``'s ranges -> the installed ``targets``."""

    script: str
    unicode: str
    targets: tuple[str, ...]
    language: str = ""

    def render(self) -> str:
        """The element on one line."""
        language = f' Language="{self.language}"' if self.language else ""
        return f'<FontFamilyMap Unicode="{self.unicode}"{language} Target="{", ".join(self.targets)}" />'


@dataclass(frozen=True)
class Patch:
    """One copy: its ``path`` (in ``folder``), its current bytes and the bytes it must hold (``mode`` is kept)."""

    path: Path
    folder: str
    current: bytes
    updated: bytes
    mode: int

    @property
    def stale(self) -> bool:
        """True when the copy must be rewritten."""
        return self.current != self.updated


# -- the maps --------------------------------------------------------------------------------------


def family_maps(families: set[str]) -> list[FamilyMap]:
    """The block's maps: every script with at least one installed candidate, in :data:`SCRIPTS` order."""
    maps = []
    for script in SCRIPTS:
        targets = tuple(name for name in script.candidates if name in families)
        if targets:
            maps.append(FamilyMap(script.name, script.unicode, targets, script.language))
    return maps


def wanted_maps(ctx: Ctx) -> list[FamilyMap]:
    """The maps for the fonts ``fc-list`` reports (cached per run by :mod:`.fonts`)."""
    return family_maps(fonts_step.installed_families(ctx))


def mapped_scripts(maps: list[FamilyMap]) -> list[str]:
    """The distinct script names in ``maps``, in order."""
    return list(dict.fromkeys(item.script for item in maps))


def unmapped_key_scripts(maps: list[FamilyMap]) -> list[str]:
    """The :data:`KEY_SCRIPTS` without an installed Linux font."""
    mapped = set(mapped_scripts(maps))
    return [name for name in KEY_SCRIPTS if name not in mapped]


def han_first(maps: list[FamilyMap]) -> str | None:
    """The font Han text without a language gets first, or None when no CJK font is installed."""
    return next((item.targets[0] for item in maps if item.script == HAN and not item.language), None)


# -- finding the copies ----------------------------------------------------------------------------


def _child(directory: Path, name: str) -> Path | None:
    """``directory/<name>`` (any case) when it exists and is not a symlink, else None."""
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return None
    match = next((entry for entry in names if entry.lower() == name.lower()), None)
    if match is None:
        return None
    path = directory / match
    return None if path.is_symlink() else path


def _walk(root: Path, parts: tuple[str, ...]) -> Path | None:
    """``root/<parts>`` matched case-insensitively, never through a symlink."""
    current: Path | None = root
    for part in parts:
        current = _child(current, part) if current is not None else None
    return current


def wpf_dir(ctx: Ctx) -> Path | None:
    """The 64-bit framework's ``WPF`` folder of the prefix (a real directory), or None."""
    drive_c = ctx.paths.prefix / "drive_c"
    if drive_c.is_symlink():
        return None
    found = _walk(drive_c, WPF_DIR_PARTS)
    return found if found is not None and found.is_dir() else None


def composite_font_wins() -> list[str]:
    """The Windows paths of the copies WPF may read."""
    return [FRAMEWORK64_WIN + folder_name(sub) + COMPOSITE_FONT_NAME for sub in COMPOSITE_FONT_LOCATIONS]


def folder_name(sub: tuple[str, ...]) -> str:
    """How the doctor shows a location: ``WPF\\``, ``WPF\\Fonts\\``."""
    return "WPF\\" + "".join(part + "\\" for part in sub)


def composite_fonts(ctx: Ctx) -> list[tuple[str, Path]]:
    """``(folder, path)`` of every existing copy WPF may read (``WPF\\`` then ``WPF\\Fonts\\``).

    Only regular files count, found case-insensitively and never through a symlink.
    """
    wpf = wpf_dir(ctx)
    if wpf is None:
        return []
    found = []
    for sub in COMPOSITE_FONT_LOCATIONS:
        path = _walk(wpf, (*sub, COMPOSITE_FONT_NAME))
        if path is not None and stat.S_ISREG(path.lstat().st_mode):
            found.append((folder_name(sub), path))
    return found


# -- patching the text -----------------------------------------------------------------------------


def _damaged(path: Path, problem: str) -> Unpatchable:
    return Unpatchable(
        f"WPF's composite font {path} is damaged ({problem})",
        hint=REPAIR_HINT,
    )


def strip_blocks(text: str, path: Path) -> str:
    """``text`` without our marked blocks (whole lines, so Microsoft's bytes come back exactly)."""
    kept: list[str] = []
    inside = False
    for line in text.splitlines(keepends=True):
        mark = line.strip()
        if mark.startswith(BEGIN):
            if inside:
                raise _damaged(path, "a fork-linux block is not closed")
            inside = True
        elif mark == END:
            if not inside:
                raise _damaged(path, "a fork-linux block end without its beginning")
            inside = False
        elif not inside:
            kept.append(line)
    if inside:
        raise _damaged(path, "a fork-linux block is not closed")
    return "".join(kept)


def render_block(maps: list[FamilyMap], indent: str, newline: str) -> str:
    """The marked block (empty without maps)."""
    if not maps:
        return ""
    lines = [BEGIN_LINE, *(item.render() for item in maps), END]
    return "".join(indent + line + newline for line in lines)


def _family_maps_count(text: str, path: Path) -> int:
    """How many ``<FontFamily.FamilyMaps>`` elements ``text`` has (it must be well-formed XML)."""
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise _damaged(path, f"not well-formed XML: {exc}") from None
    return sum(1 for element in root.iter() if element.tag == NAMESPACE + FAMILY_MAPS)


def patch_text(text: str, maps: list[FamilyMap], path: Path) -> str:
    """``text`` (no BOM) with a fresh block at the top of every ``FamilyMaps`` section.

    Raises :class:`Unpatchable` when the file is not a composite font this
    step understands: not XML, a DTD, a ``FamilyMaps`` tag that is not alone on
    its line, an unbalanced block, or a result that is not XML.
    """
    if any(token in text for token in FORBIDDEN):
        raise _damaged(path, "it declares a DTD")
    clean = strip_blocks(text, path)
    sections = _family_maps_count(clean, path)
    found = list(FAMILY_MAPS_LINE.finditer(clean))
    if not sections or len(found) != sections:
        raise _damaged(path, f"{sections} FamilyMaps sections, {len(found)} found on lines of their own")
    pieces = []
    start = 0
    for match in found:
        line = match.group(0)
        indent = line[: len(line) - len(line.lstrip(" \t"))] + INDENT
        newline = "\r\n" if line.endswith("\r\n") else "\n"
        pieces += [clean[start : match.end()], render_block(maps, indent, newline)]
        start = match.end()
    patched = "".join(pieces) + clean[start:]
    _family_maps_count(patched, path)
    return patched


def _read(path: Path) -> tuple[bytes, int]:
    """The bytes and permission bits of ``path`` (never through a symlink, size-capped)."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as exc:
        raise _damaged(path, f"cannot open it: {exc.strerror}") from None
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
        os.close(fd)
        raise _damaged(path, f"not a regular file of at most {MAX_BYTES} bytes")
    with os.fdopen(fd, "rb") as handle:
        return handle.read(MAX_BYTES), stat.S_IMODE(info.st_mode)


def patch_file(path: Path, maps: list[FamilyMap], folder: str = "") -> Patch:
    """Read ``path`` and work out its patched bytes (nothing is written)."""
    data, mode = _read(path)
    bom = data.startswith(BOM)
    try:
        text = data[len(BOM) if bom else 0 :].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _damaged(path, f"not UTF-8: {exc.reason}") from None
    patched = patch_text(text, maps, path).encode("utf-8")
    return Patch(path, folder, data, (BOM if bom else b"") + patched, mode)


def patched_composite_fonts(ctx: Ctx, maps: list[FamilyMap] | None = None) -> list[Patch]:
    """Every copy with its patched bytes; :class:`Unpatchable` when none exists or any is damaged."""
    copies = composite_fonts(ctx)
    if not copies:
        wanted = " and ".join(composite_font_wins())
        raise Unpatchable(
            f"WPF's composite font is missing ({wanted} do not exist)",
            hint=REPAIR_HINT,
        )
    chosen = wanted_maps(ctx) if maps is None else maps
    return [patch_file(path, chosen, folder) for folder, path in copies]


def write_patch(patch: Patch) -> None:
    """Write one stale copy atomically, keeping its mode (the path is never a symlink here)."""
    fsutil.atomic_write(patch.path, patch.updated, mode=patch.mode)


# -- the step --------------------------------------------------------------------------------------


def inputs(ctx: Ctx) -> dict[str, Any]:
    """The maps (they follow the installed Linux fonts)."""
    return {"maps": [[item.script, item.language, item.unicode, list(item.targets)] for item in wanted_maps(ctx)]}


def run(ctx: Ctx) -> None:
    """Patch every stale copy, or warn and write nothing when a copy is missing or damaged."""
    try:
        patches = patched_composite_fonts(ctx)
    except Unpatchable as exc:
        log.warning("font_fallback: %s", exc.message)
        ctx.ui.warn(f"non-Latin text may not show in Fork: {exc.message}")
        return
    for patch in patches:
        if patch.stale:
            log.info("font_fallback: patching %s", patch.path)
            write_patch(patch)


def verify(ctx: Ctx) -> bool:
    """Every copy is current; a missing or damaged composite font was only warned about."""
    try:
        patches = patched_composite_fonts(ctx)
    except Unpatchable:
        return True
    return not any(patch.stale for patch in patches)


FONT_FALLBACK = Step(
    id="font_fallback",
    title="Mapping non-Latin scripts to Linux fonts",
    # rev 2: every copy (WPF\ and WPF\Fonts\, for .NET 4.7.2), not only WPF\.
    rev=2,
    weight=1,
    run=run,
    verify=verify,
    inputs=inputs,
)

STEPS = (FONT_FALLBACK,)
