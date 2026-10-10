# Spike S10 — Fork's interface text under Wine (Selawik UI font, font replacements)

> **Reconstructed 2026-10-10 from the NUC build.** The original spike notes, measurements and
> screenshots were lost with the developer's home directory on 2026-10-10. This page records
> only what the recovered fork-linux 1.0.0 sources (built from `fix/nuc-integration` on
> 2026-10-10 08:06) implement and say about themselves; nothing here was re-measured.

## Problem

Fork's WPF interface draws its text with Windows' message font. Under Wine that font is Wine's
own Tahoma, while Fork's layout is designed around **Segoe UI**, which Wine does not have and
which must not be redistributed. `steps/fonts.py` records that the spike compared the
candidates under Wine at 192 DPI; the result is the design below.

## Design (as implemented)

Three setup steps, in this order (`src/fork_linux/steps/fonts.py`):

| Step | What it does | Verified by |
|---|---|---|
| `fonts` | `winetricks corefonts` (Arial & co.) for documents and fallbacks; one retry | Arial present and `corefonts` in `winetricks.log` |
| `ui_font` (rev 1) | Downloads **Selawik 1.01** — Microsoft's open-source stand-in for Segoe UI with matching metrics, SIL OFL 1.1 — from its GitHub release (pinned URL, size and sha256 in `runtime-manifest.json` → `ui_font`; host `github.com` only), copies exactly the face files the manifest lists into the prefix's `C:\windows\Fonts` (0644) and registers each face under `HKLM\Software\Microsoft\Windows NT\CurrentVersion\Fonts` (`Selawik Bold (TrueType)` = `selawkb.ttf`, ...) | every face file is a regular, non-empty file and every registry value is present |
| `font_replacements` (rev 2) | `HKCU\Software\Wine\Fonts\Replacements` plus the system fonts in `HKCU\Control Panel\Desktop\WindowMetrics` | every replacement and every system font face |

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
`prefix.font_smoothing` (smoothing on, gamma 1000–2200; fix: step `registry`).

## Not part of this build

Later work on WPF's composite-font fallback for scripts Selawik lacks (CJK, Cyrillic, Greek;
a `font_fallback` step patching `GlobalUserInterface.CompositeFont`) was never committed and
is not in the NUC build; its report is preserved on the `recovery/nuc-snapshot` branch
(`recovery/fork-linux-font-fallback-dotnet472.result.json`).

## Legal

Selawik is downloaded per user at setup time and never redistributed by this repository or its
packages; it is credited in `credits.py` (`UPSTREAM_PROJECTS`), the README credits block and
[docs/CREDITS.md](../CREDITS.md) (SIL Open Font License 1.1). The consent dialog lists the
download with its license.
