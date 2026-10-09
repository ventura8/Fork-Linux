---
name: diagnostics
description: fork-linux doctor checks, --fix, --json.
---

# Diagnostics

fork-linux doctor checks, --fix, --json.

Canonical rules: [AGENTS.md](../../../AGENTS.md).

- `fork-linux doctor [--offline] [--deep] [--network] [--check ID] [--fix] [--json]`; exit 17 when a check fails.
- The packaging E2E runs `doctor --offline --json` without Wine and expects JSON with `checks` (exit 0 or 17).
- Host libraries and their package names: `fork_linux.hostdeps` (also used by install.sh and `scripts/check-dep-names.py`).
