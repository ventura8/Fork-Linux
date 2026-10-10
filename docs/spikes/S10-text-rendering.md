# Spike S10 — Fork's interface text under Wine (Selawik UI font, font replacements)

> **Reconstructed 2026-10-10 from the NUC build.** The original spike notes, measurements and
> screenshots were lost with the developer's home directory on 2026-10-10. Apart from
> [Finding 7](#finding-7-wpfs-composite-font-fallback-for-non-latin-scripts) (re-measured
> 2026-10-11 in containers), this page records only what the recovered fork-linux 1.0.0 sources
> (built from `fix/nuc-integration` on 2026-10-10 08:06) implement and say about themselves.

## Problem

Fork's WPF interface draws its text with Windows' message font. Under Wine that font is Wine's
own Tahoma, while Fork's layout is designed around **Segoe UI**, which Wine does not have and
which must not be redistributed. `steps/fonts.py` records that the spike compared the
candidates under Wine at 192 DPI; the result is the design below.

## Design (as implemented)

Four setup steps, in this order (`src/fork_linux/steps/fonts.py`, `src/fork_linux/steps/font_fallback.py`):

| Step | What it does | Verified by |
|---|---|---|
| `fonts` | `winetricks corefonts` (Arial & co.) for documents and fallbacks; one retry | Arial present and `corefonts` in `winetricks.log` |
| `ui_font` (rev 1) | Downloads **Selawik 1.01** — Microsoft's open-source stand-in for Segoe UI with matching metrics, SIL OFL 1.1 — from its GitHub release (pinned URL, size and sha256 in `runtime-manifest.json` → `ui_font`; host `github.com` only), copies exactly the face files the manifest lists into the prefix's `C:\windows\Fonts` (0644) and registers each face under `HKLM\Software\Microsoft\Windows NT\CurrentVersion\Fonts` (`Selawik Bold (TrueType)` = `selawkb.ttf`, ...) | every face file is a regular, non-empty file and every registry value is present |
| `font_replacements` (rev 2) | `HKCU\Software\Wine\Fonts\Replacements` plus the system fonts in `HKCU\Control Panel\Desktop\WindowMetrics` | every replacement and every system font face |
| `font_fallback` (rev 2) | A marked block of `FontFamilyMap`s naming installed Linux fonts at the top of every `FamilyMaps` section of WPF's `GlobalUserInterface.CompositeFont`, in every 64-bit copy (`WPF\`, `WPF\Fonts\`); see Finding 7 | every copy holds exactly the block the installed fonts call for (a missing or damaged copy is only warned about) |

Replacements written by `font_replacements`:

| Windows family | Replacement |
|---|---|
| `Segoe UI` | `Selawik` |
| `Segoe UI Semibold` / `Semilight` / `Light` | `REG_MULTI_SZ` [`Selawik <weight>`, `Selawik`] (Wine's GDI and DirectWrite take the first existing family) |
| `Segoe UI Symbol` | the first installed (`fc-list`) of DejaVu Sans, Noto Sans, Liberation Sans; else Tahoma. DejaVu Sans comes first for its symbol coverage (Noto Sans has no U+2713 check mark) |
| `Consolas` | the first installed of Noto Sans Mono, DejaVu Sans Mono, Liberation Mono; else Courier New |

System fonts: `CaptionFont`, `IconFont`, `MenuFont`, `MessageFont`, `SmCaptionFont` and
`StatusFont` become `LOGFONTW` records (92 bytes, `REG_BINARY`) for **Segoe UI 9 pt** (height
−12 at 96 DPI, weight 400, `DEFAULT_CHARSET`) — Windows 10's defaults — so WPF and Wine's own
dialogs ask for Segoe UI (that is, Selawik) instead of Tahoma. Wine scales the height to the
prefix's `LogPixels` and rewrites the rest of each record; the step verifies only the face name.

The `registry` step (rev 3) also sets `FontSmoothingGamma` = 1400, Windows' default ClearType
gamma (Wine's own value 0 means "unset"), next to `FontSmoothing` = 2, `FontSmoothingType` = 2
and `FontSmoothingOrientation` = 1.

## Checks

`fork-linux doctor`: `host.fonts` (a Linux font for Segoe UI Symbol and Consolas),
`prefix.ui_font` (Selawik installed and registered; fix: step `ui_font`),
`prefix.font_replacements` (replacements and system fonts; fix: step `font_replacements`),
`prefix.font_fallback` (the composite font copies are patched for the installed fonts and name the
folders, e.g. `24 scripts mapped in WPF's composite font (WPF\, WPF\Fonts\; CJK: Noto Sans CJK JP
first)`; warns when a copy is stale, missing or damaged, or when Greek, Cyrillic, Hangul or Han has no
Linux font; fix: step `font_fallback`),
`prefix.font_smoothing` (smoothing on, gamma 1000–2200; fix: step `registry`).

## Finding 7: WPF's composite font (fallback for non-Latin scripts)

Selawik covers Latin only. For a character the interface font lacks, WPF falls back to its
composite font **Global User Interface** (`GlobalUserInterface.CompositeFont` in the framework's
`WPF` folder). Microsoft's file is a `FontFamilyCollection` with one `FontFamily` per `OS`
(.NET 4.8: Windows10RS5, Windows10RS1, Windows10, WindowsXPSP2; 4.7.2: the last three), each
with a `FontFamily.FamilyMaps` list matched in order (first map whose `Unicode` ranges and
`Language` match wins; `Target` is a list of families). Every target is a Windows font Wine does
not have (Segoe UI, Yu Gothic UI, Malgun Gothic, Microsoft YaHei, ...), so Cyrillic and Greek fall
back to Wine's narrow Tahoma and Korean (and the Chinese / Japanese full-width colon) is not drawn.

Measured 2026-10-11 with Fork 2.23.2, Wine 11.0-staging (Kron4ek, the managed runtime) and the
`fonts-noto-core` / `fonts-noto-cjk` fonts, each prefix built by `fork-linux setup` inside
`scripts/e2e-docker.sh` (`--dotnet dotnet472` for the second), opening a repository named
`字体-шрифт` whose commit subjects and branch names are Russian, Greek, Chinese, Japanese and
Korean:

| | .NET 4.8 (`dotnet48`, Release 528049) | .NET 4.7.2 (`dotnet472`, Release 461814) |
|---|---|---|
| Copies installed (64-bit) | `Framework64\v4.0.30319\WPF\` and `WPF\Fonts\` (identical, sha256 `c0f281bf…95bfc757`, CRLF, UTF-8 BOM, 4 sections) | `WPF\Fonts\` only (sha256 `3ab0675d…6ef6e3`, 3 sections) |
| Opened by Fork (`strace -f`, wineserver included) | only `WPF\GlobalUserInterface.CompositeFont` | only `WPF\Fonts\GlobalUserInterface.CompositeFont` |
| Microsoft's file | Korean branch / ref / subject empty, full-width colon missing, Cyrillic and Greek in Tahoma | same |
| Patched (`font_fallback`) | Cyrillic, Greek in Noto Sans; Chinese, Japanese, Korean in Noto Sans CJK (Fork maps `NotoSans-*.ttf`, `NotoSansCJK-*.ttc`) | same |
| Only the other copy patched | `WPF\Fonts\` patched, `WPF\` original: rendered as Microsoft's file (4.8 never reads `WPF\Fonts\`) | — |

The 32-bit `Framework\` copies are never read (Fork is x64 only) and are left alone. The prior,
lost attempt (2026-10-10) also found that 4.7.2's PresentationCore builds the folder from the
framework's `InstallPath` + `WPF\Fonts\` and 4.8's from `InstallPath` + `WPF\`.

The `font_fallback` step therefore patches **every 64-bit copy that exists**. In each
`FamilyMaps` section it inserts, right after the opening tag, one block between
`<!-- fork-linux font_fallback begin… -->` and `<!-- fork-linux font_fallback end -->` lines: a
`FontFamilyMap` per script naming only the candidate Linux fonts `fc-list` reports (Noto first,
then DejaVu / Liberation / Source Han / WenQuanYi / Droid Sans Fallback where they cover the
script). Han ranges get language maps (zh-Hans → SC, zh-HK → HK, zh-Hant → TC, ja → JP, ko → KR;
zh-HK before its parent zh-Hant) and a generic map with the JP variant first, as Microsoft's own
"Other" maps prefer Japanese fonts. Scripts without an installed font keep Microsoft's maps.

* Idempotent: the block is removed and rebuilt on every run; with no suitable font installed the
  file goes back to Microsoft's. Removing the block restored Microsoft's bytes exactly in both
  prefixes (sha256 above).
* Safe: every copy is read (`O_NOFOLLOW`, ≤ 8 MiB, regular file, no symlink anywhere below
  `drive_c`), checked (no DTD, well-formed before and after, one `FamilyMaps` tag line per
  section) and patched before any is written; one damaged or missing copy means nothing is
  written and setup only warns. Writes are atomic and keep the BOM, CRLF and mode.
* Inputs: the maps (they follow the installed fonts), so installing Noto later re-runs the step.
* rev 2: the lost pre-release rev 1 patched only `WPF\`, which left a 4.7.2 prefix unpatched.

## Legal

Selawik is downloaded per user at setup time and never redistributed by this repository or its
packages; it is credited in `credits.py` (`UPSTREAM_PROJECTS`), the README credits block and
[docs/CREDITS.md](../CREDITS.md) (SIL Open Font License 1.1). The consent dialog lists the
download with its license.
