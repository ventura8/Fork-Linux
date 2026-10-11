---
name: diagnostics
description: fork-linux doctor checks, --fix, --json.
---

# Diagnostics

fork-linux doctor checks, --fix, --json.

Canonical rules: [AGENTS.md](../../../AGENTS.md).

- `fork-linux doctor [--offline] [--deep] [--network] [--check ID] [--fix] [--json]`; exit 17 when a check fails.
- The packaging E2E runs `doctor --offline --json` without Wine and expects JSON with `checks` (exit 0 or 17).
- Text rendering checks: `host.fonts`, `prefix.ui_font` (Selawik; fix step `ui_font`), `prefix.font_replacements`,
  `prefix.font_smoothing` (fix step `registry`). `host.tools` treats zenity / kdialog as alternatives
  (`hostdeps.DIALOG_TOOLS`): one is suggested only when both are missing (kdialog on KDE).
- `desktop.handoff`: how Fork's terminals and apps leave Wine's process tree (`bridge.handoff_route`; without
  systemd: the running daemon in `session.json`, else the one the next launch starts) and a warn for a
  `handoff.log` line newer than the last launch. Only reads: no daemon, no Wine.
- Host libraries and their package names: `fork_linux.hostdeps` (also used by install.sh and `scripts/check-dep-names.py`).
