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
  `prefix.font_fallback` (WPF's composite font -> Linux fonts for non-Latin scripts; fix step `font_fallback`),
  `prefix.font_smoothing` (fix step `registry`). `host.tools` treats zenity / kdialog as alternatives
  (`hostdeps.DIALOG_TOOLS`): one is suggested only when both are missing (kdialog on KDE).
- `desktop.handoff`: how Fork's terminals and apps leave Wine's process tree (`bridge.handoff_route`; without
  systemd: the running daemon in `session.json`, else the one the next launch starts) and a warn for a
  `handoff.log` line newer than the last launch. Only reads: no daemon, no Wine.
- `ssh.agent`: whether Fork's ssh can use the session's ssh-agent (the launcher's relay for bundled ssh,
  `[ssh] agent_relay`): info when the relay is off or `$SSH_AUTH_SOCK` is no socket, ok with the native-git
  bridge, warn without `fl-bridge-helper`, when the newest `bridge-*.log` has `ssh-agent relay off: <why>`
  (`bridge.relay_failure`; usually no extended attributes on the prefix's file system) or when Fork runs
  without the relay (`session.json` `bridge.agent`). `ssh.passphrase` names keys with a passphrase.
- Host libraries and their package names: `fork_linux.hostdeps` (also used by install.sh and `scripts/check-dep-names.py`).
