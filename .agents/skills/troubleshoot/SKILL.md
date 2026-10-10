---
name: troubleshoot
description: Setup / launch failures, logs, Wine debug knobs.
---

# Troubleshoot

Setup / launch failures, logs, Wine debug knobs.

Canonical rules: [AGENTS.md](../../../AGENTS.md).

- `fork-linux doctor --deep` (and `--json`), `fork-linux logs` / `logs --bundle`, `fork-linux run --debug --wine-debug +loaddll`.
- Setup resumes at the failed step: `fork-linux setup --list-steps`, `--from-step STEP`, `--only STEP`.
- Wrong or narrow interface text: `fork-linux doctor --check prefix.ui_font --check prefix.font_replacements --check prefix.font_smoothing`,
  then `fork-linux setup --only ui_font --only font_replacements` (design: `docs/spikes/S10-text-rendering.md`).
- Cyrillic / Greek in a narrow font, Korean or CJK punctuation missing (boxes or nothing):
  `fork-linux doctor --check prefix.font_fallback`. It names the patched folders of WPF's composite font
  (`WPF\` on .NET 4.8, `WPF\Fonts\` on the 4.7.2 fallback) and warns when no Linux font covers Greek,
  Cyrillic, Hangul or Han (install the Noto fonts, e.g. `fonts-noto-core fonts-noto-cjk`), when a copy is not
  patched (`fork-linux setup --only font_fallback`) or is missing / damaged (setup only warned and wrote
  nothing; `fork-linux setup --reset` rebuilds the prefix). Design: S10 Finding 7.
- Downloads: `~/.cache/fork-linux/downloads` (ours), `~/.cache/fork-linux/winetricks` (winetricks' `W_CACHE`);
  `~/.cache/winetricks` is only read.
- Locks: `$XDG_RUNTIME_DIR/fork-linux/setup.lock`. Logs: `~/.local/state/fork-linux/logs/`.
- Package problems: run the format's cell and read `logs/ci-packaging/<format>.log`.
- Never touch `~/.wine` or a prefix without `.fork-linux/created-by`.
- Agents reproduce setup / launch problems only in a container (hard rule 18):
  `scripts/e2e-docker.sh --name repro --keep shell -- bash -c 'python3 -m fork_linux setup --accept-fork-eula --no-gui; python3 -m fork_linux doctor --deep'`
  (add `--ptrace` for strace). Never run `fork-linux setup` / `run`, Wine or winetricks on the host.
