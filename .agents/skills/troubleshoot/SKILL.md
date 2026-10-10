---
name: troubleshoot
description: Setup / launch failures, logs, Wine debug knobs.
---

# Troubleshoot

Setup / launch failures, logs, Wine debug knobs.

Canonical rules: [AGENTS.md](../../../AGENTS.md).

- `fork-linux doctor --deep` (and `--json`), `fork-linux logs` / `logs --bundle`, `fork-linux run --debug --wine-debug +loaddll`.
- Setup resumes at the failed step: `fork-linux setup --list-steps`, `--from-step STEP`, `--only STEP`.
- Locks: `$XDG_RUNTIME_DIR/fork-linux/setup.lock`. Logs: `~/.local/state/fork-linux/logs/`.
- Package problems: run the format's cell and read `logs/ci-packaging/<format>.log`.
- Never touch `~/.wine` or a prefix without `.fork-linux/created-by`.
- Agents reproduce setup / launch problems only in a container (hard rule 18):
  `scripts/e2e-docker.sh --name repro --keep shell -- bash -c 'python3 -m fork_linux setup --accept-fork-eula --no-gui; python3 -m fork_linux doctor --deep'`
  (add `--ptrace` for strace). Never run `fork-linux setup` / `run`, Wine or winetricks on the host.
