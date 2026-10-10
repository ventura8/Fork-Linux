"""Steps ``fonts``, ``ui_font`` and ``font_replacements``: the fonts Fork's interface renders with.

Fork's WPF interface draws its text with Windows' message font, which Wine
sets to its own Tahoma; Fork's layout is designed around Segoe UI. Spike S10
compared the candidates under Wine at 192 DPI:

* ``fonts``: ``winetricks corefonts`` (Arial & co.) for documents and fallbacks;
* ``ui_font``: Selawik, Microsoft's open-source (SIL OFL 1.1) stand-in for
  Segoe UI with matching metrics, downloaded (pinned + sha256) into the
  prefix's ``windows\\Fonts`` and registered like an installed font;
* ``font_replacements``: ``Segoe UI`` (and its Semibold / Light / Semilight
  faces) -> Selawik, ``Segoe UI Symbol`` / ``Consolas`` -> the best installed
  Linux font (``fc-list``), and Windows 10's system fonts (Segoe UI 9 pt) in
  ``Control Panel\\Desktop\\WindowMetrics``, so WPF and Wine's own dialogs ask
  for Segoe UI instead of Tahoma.
"""

from __future__ import annotations

import logging
import os
import struct
import zipfile
from pathlib import Path
from typing import Any

from .. import download, fsutil, sandbox, winetricks
from ..bootstrap import Ctx, Step
from ..errors import ForkLinuxError, IntegrityFailed, SetupFailed
from ..manifest import UiFont
from ..registry import RegBatch
from . import prefix as prefix_steps

REPLACEMENTS_KEY = r"HKCU\Software\Wine\Fonts\Replacements"
SYSTEM_FONTS_KEY = r"HKLM\Software\Microsoft\Windows NT\CurrentVersion\Fonts"
METRICS_KEY = r"HKCU\Control Panel\Desktop\WindowMetrics"
FC_LIST_TIMEOUT = 30.0
SEGOE_UI = "Segoe UI"
# Segoe UI faces that GDI programs ask for by name; each maps to the matching
# face of the interface font, then to its family.
SEGOE_UI_WEIGHTS = ("Semibold", "Semilight", "Light")
SYMBOL_FAMILIES = ("Segoe UI Symbol",)
MONO_FAMILIES = ("Consolas",)
# DejaVu Sans first: the widest symbol coverage (Noto Sans has no U+2713 check mark).
SYMBOL_CHOICES = ("DejaVu Sans", "Noto Sans", "Liberation Sans")
MONO_CHOICES = ("Noto Sans Mono", "DejaVu Sans Mono", "Liberation Mono")
# Always available: Wine ships its own Tahoma, corefonts brings Courier New.
SYMBOL_FALLBACK = "Tahoma"
MONO_FALLBACK = "Courier New"
_FC_CACHE = "fc_list_families"
COREFONTS = "corefonts"
# corefonts is resumable (finished fonts are skipped); one retry absorbs the
# occasional "Application could not be started" of regedit under Wine staging.
COREFONTS_ATTEMPTS = 2

# The interface font archive comes from GitHub release assets, like the Wine runtime.
UI_FONT_HOSTS = ("github.com",)
MAX_FACE_BYTES = 16 * 1024 * 1024

# Windows 10's system fonts: Segoe UI 9 pt (-12 px at 96 DPI; Wine scales to LogPixels).
METRICS_FONTS = ("CaptionFont", "IconFont", "MenuFont", "MessageFont", "SmCaptionFont", "StatusFont")
SYSTEM_FONT_HEIGHT = -12
FW_NORMAL = 400
DEFAULT_CHARSET = 1
_LOGFONT_HEAD = struct.Struct("<5i8B")
LF_FACESIZE = 32
LOGFONT_SIZE = _LOGFONT_HEAD.size + 2 * LF_FACESIZE

log = logging.getLogger(__name__)


# -- fonts ---------------------------------------------------------------------------------


def _find_ci(root: Path, parts: tuple[str, ...]) -> Path | None:
    """``root/<parts>`` matching each component case-insensitively, or None."""
    current = root
    for part in parts:
        try:
            names = os.listdir(current)
        except OSError:
            return None
        match = next((name for name in sorted(names) if name.lower() == part.lower()), None)
        if match is None:
            return None
        current = current / match
    return current


def arial(ctx: Ctx) -> Path | None:
    """``C:\\windows\\Fonts\\arial.ttf`` (any case), if installed."""
    found = _find_ci(ctx.paths.prefix / "drive_c", ("windows", "Fonts", "arial.ttf"))
    return found if found is not None and found.is_file() else None


def run_fonts(ctx: Ctx) -> None:
    """``winetricks -q corefonts`` unless it already finished; one retry after a failure.

    Arial alone is not proof: corefonts installs its fonts one by one, so an
    interrupted run leaves Arial without Times New Roman or Verdana.
    """
    if verify_fonts(ctx):
        return
    for _attempt in range(COREFONTS_ATTEMPTS - 1):
        try:
            prefix_steps.run_verbs(ctx, "fonts", [COREFONTS])
            return
        except SetupFailed:
            log.warning("winetricks %s failed; trying once more", COREFONTS)
    prefix_steps.run_verbs(ctx, "fonts", [COREFONTS])


def verify_fonts(ctx: Ctx) -> bool:
    """Arial is installed and winetricks recorded the whole ``corefonts`` verb as done."""
    return arial(ctx) is not None and COREFONTS in winetricks.installed_verbs(ctx.paths.prefix)


# -- ui_font -------------------------------------------------------------------------------


def fonts_dir(ctx: Ctx) -> Path:
    """The prefix's ``C:\\windows\\Fonts`` (existing spelling kept; created by Wine or by us)."""
    found = _find_ci(ctx.paths.prefix / "drive_c", ("windows", "Fonts"))
    return found if found is not None else ctx.paths.prefix / "drive_c" / "windows" / "Fonts"


def _registry_name(face: str) -> str:
    """How Windows lists an installed TrueType face: ``Selawik Bold (TrueType)``."""
    return f"{face} (TrueType)"


def ui_font_registry(font: UiFont) -> list[tuple[str, str, object]]:
    """``(key, name, value)`` registering every face of ``font`` as an installed font."""
    return [(SYSTEM_FONTS_KEY, _registry_name(face), file_name) for file_name, face in font.faces]


def missing_faces(ctx: Ctx) -> list[str]:
    """Face files of the interface font that are not installed (regular, non-empty files)."""
    folder = fonts_dir(ctx)
    missing = []
    for file_name, _face in ctx.manifest.ui_font.faces:
        path = folder / file_name
        if path.is_symlink() or not path.is_file() or path.stat().st_size == 0:
            missing.append(file_name)
    return missing


def _corrupt(archive: Path, problem: str) -> IntegrityFailed:
    return IntegrityFailed(
        f"{archive.name}: {problem}",
        hint="delete the download cache and run setup again",
    )


def _member(bundle: zipfile.ZipFile, archive: Path, file_name: str) -> bytes:
    """One listed member's bytes (size-capped; :mod:`zipfile` checks the CRC)."""
    try:
        info = bundle.getinfo(file_name)
    except KeyError:
        raise _corrupt(archive, f"{file_name} is missing from the archive") from None
    if info.file_size > MAX_FACE_BYTES:
        raise _corrupt(archive, f"{file_name} is implausibly large ({info.file_size} bytes)")
    return bundle.read(info)


def extract_faces(archive: Path, font: UiFont, dest: Path) -> list[Path]:
    """Copy the listed ``.ttf`` members of the verified ``archive`` into ``dest`` (0644 each).

    Only the exact member names from the manifest are read (never paths taken
    from the archive), so nothing else in it can land anywhere.
    """
    try:
        with zipfile.ZipFile(archive) as bundle:
            faces = [(file_name, _member(bundle, archive, file_name)) for file_name, _face in font.faces]
    except (zipfile.BadZipFile, OSError) as exc:
        raise _corrupt(archive, f"cannot unpack: {exc}") from None
    written = []
    for file_name, data in faces:
        target = dest / file_name
        try:
            fsutil.atomic_write(target, data, mode=0o644)
        except OSError as exc:
            raise SetupFailed("ui_font", f"cannot write {target}: {exc}") from None
        written.append(target)
    return written


def ui_font_inputs(ctx: Ctx) -> dict[str, Any]:
    """The pinned interface font (family, version and archive sha256)."""
    font = ctx.manifest.ui_font
    return {"family": font.family, "version": font.version, "sha256": font.sha256}


def run_ui_font(ctx: Ctx) -> None:
    """Download the pinned archive (size + sha256 checked), install its faces and register them."""
    font = ctx.manifest.ui_font
    archive = download.fetch(
        font.url,
        font.archive_name,
        cache_dir=ctx.paths.downloads_dir,
        sha256=font.sha256,
        size=font.size,
        offline=ctx.offline,
        allowed_hosts=UI_FONT_HOSTS,
        max_size=font.size,
    )
    extract_faces(archive, font, fonts_dir(ctx))
    batch = RegBatch()
    for key, name, value in ui_font_registry(font):
        batch.set_sz(key, name, str(value))
    prefix_steps.import_batch(ctx, batch, "ui-font")


def verify_ui_font(ctx: Ctx) -> bool:
    """Every face file is installed and registered."""
    return not missing_faces(ctx) and prefix_steps.reg_matches(ctx, ui_font_registry(ctx.manifest.ui_font))


# -- font_replacements -------------------------------------------------------------------


def parse_families(output: str) -> set[str]:
    """Family names in ``fc-list : family`` output (one font per line, aliases comma separated)."""
    families = set()
    for line in output.splitlines():
        for name in line.split(","):
            name = name.strip().replace("\\-", "-")
            if name:
                families.add(name)
    return families


def installed_families(ctx: Ctx) -> set[str]:
    """Font families fontconfig knows (empty when ``fc-list`` is missing or fails)."""
    cached = ctx.cache.get(_FC_CACHE)
    if isinstance(cached, set):
        return cached
    try:
        result = ctx.runner.run(["fc-list", ":", "family"], env=sandbox.clean_env(ctx.env), timeout=FC_LIST_TIMEOUT)
    except ForkLinuxError:
        families: set[str] = set()
    else:
        families = parse_families(result.stdout) if result.ok else set()
    ctx.cache[_FC_CACHE] = families
    return families


def pick(families: set[str], choices: tuple[str, ...], fallback: str) -> str:
    """The first of ``choices`` in ``families``, else ``fallback``."""
    return next((choice for choice in choices if choice in families), fallback)


def segoe_ui_replacements(font: UiFont) -> dict[str, str | list[str]]:
    """``Segoe UI`` -> the interface font; ``Segoe UI <weight>`` -> its matching face, then the family.

    A list becomes a ``REG_MULTI_SZ``: Wine (GDI and DirectWrite) uses the
    first entry that names an existing family.
    """
    faces = {face for _file, face in font.faces}
    chosen: dict[str, str | list[str]] = {SEGOE_UI: font.family}
    for weight in SEGOE_UI_WEIGHTS:
        face = f"{font.family} {weight}"
        chosen[f"{SEGOE_UI} {weight}"] = [face, font.family] if face in faces else font.family
    return chosen


def replacements(ctx: Ctx) -> dict[str, str | list[str]]:
    """Windows family -> the family Wine uses instead (a list: the first one installed)."""
    families = installed_families(ctx)
    chosen = segoe_ui_replacements(ctx.manifest.ui_font)
    chosen.update(dict.fromkeys(SYMBOL_FAMILIES, pick(families, SYMBOL_CHOICES, SYMBOL_FALLBACK)))
    chosen.update(dict.fromkeys(MONO_FAMILIES, pick(families, MONO_CHOICES, MONO_FALLBACK)))
    return chosen


def logfont(face: str, *, height: int = SYSTEM_FONT_HEIGHT, weight: int = FW_NORMAL) -> bytes:
    """A ``LOGFONTW`` as Windows stores it in ``WindowMetrics`` (92 bytes)."""
    head = _LOGFONT_HEAD.pack(height, 0, 0, 0, weight, 0, 0, 0, DEFAULT_CHARSET, 0, 0, 0, 0)
    return head + face.encode("utf-16-le")[: 2 * (LF_FACESIZE - 1)].ljust(2 * LF_FACESIZE, b"\0")


def logfont_face(value: object) -> str | None:
    """The face name of a stored ``LOGFONTW``, or None when ``value`` is not one."""
    if not isinstance(value, bytes) or len(value) != LOGFONT_SIZE:
        return None
    return value[_LOGFONT_HEAD.size:].decode("utf-16-le", "replace").split("\0", 1)[0]


def system_fonts() -> dict[str, bytes]:
    """``WindowMetrics`` value -> ``LOGFONTW`` for Segoe UI 9 pt (Windows 10's defaults)."""
    return dict.fromkeys(METRICS_FONTS, logfont(SEGOE_UI))


def replacement_inputs(ctx: Ctx) -> dict[str, Any]:
    """The chosen replacements (derived from the manifest and the ``fc-list`` result)."""
    return {"replacements": replacements(ctx), "system_font": SEGOE_UI, "height": SYSTEM_FONT_HEIGHT}


def run_replacements(ctx: Ctx) -> None:
    """Write ``HKCU\\Software\\Wine\\Fonts\\Replacements`` and the ``WindowMetrics`` fonts."""
    batch = RegBatch()
    for name, target in replacements(ctx).items():
        if isinstance(target, list):
            batch.set_multi_sz(REPLACEMENTS_KEY, name, target)
        else:
            batch.set_sz(REPLACEMENTS_KEY, name, target)
    for name, value in system_fonts().items():
        batch.set_binary(METRICS_KEY, name, value)
    prefix_steps.import_batch(ctx, batch, "font-replacements")


def wrong_system_fonts(ctx: Ctx) -> list[str]:
    """``WindowMetrics`` fonts whose face is not Segoe UI (Wine rewrites the rest of the record)."""
    found = prefix_steps.reg_values(ctx, [(METRICS_KEY, name) for name in METRICS_FONTS])
    return [name for name, value in zip(METRICS_FONTS, found, strict=True) if logfont_face(value) != SEGOE_UI]


def verify_replacements(ctx: Ctx) -> bool:
    """Every replacement is in the registry and the system fonts ask for Segoe UI."""
    expected = [(REPLACEMENTS_KEY, name, target) for name, target in replacements(ctx).items()]
    return prefix_steps.reg_matches(ctx, expected) and not wrong_system_fonts(ctx)


FONTS = Step(
    id="fonts",
    title="Installing Microsoft core fonts",
    rev=1,
    weight=48,
    run=run_fonts,
    verify=verify_fonts,
    needs_network=True,
)

UI_FONT = Step(
    id="ui_font",
    title="Installing the interface font (Selawik)",
    rev=1,
    weight=2,
    run=run_ui_font,
    verify=verify_ui_font,
    inputs=ui_font_inputs,
    needs_network=True,
)

FONT_REPLACEMENTS = Step(
    id="font_replacements",
    title="Choosing interface fonts",
    rev=2,
    weight=3,
    run=run_replacements,
    verify=verify_replacements,
    inputs=replacement_inputs,
)

STEPS = (FONTS, UI_FONT, FONT_REPLACEMENTS)
