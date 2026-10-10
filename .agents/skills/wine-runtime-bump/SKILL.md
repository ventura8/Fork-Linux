---
name: wine-runtime-bump
description: Bump the pinned Kron4ek Wine build / winetricks in the runtime manifest.
---

# Wine Runtime Bump

Bump the pinned Kron4ek Wine build / winetricks in the runtime manifest.

Canonical rules: [AGENTS.md](../../../AGENTS.md).

1. Update `src/fork_linux/data/runtime-manifest.json` (URL on the allowlist, size, sha256) — never mirror binaries.
   The same rules apply to the `ui_font` pin (Selawik release zip on `github.com`; `faces` lists the `.ttf` members
   copied into the prefix); bump `revision` with any pin change.
2. `python3 -m pytest -q tests/test_manifest.py tests/test_wine_provider.py`.
3. Bridge tier + real E2E: `FL_CI_STAGE=bridge ./scripts/ci-docker.sh`, `./scripts/ci-e2e-wine.sh`.
4. Record results in `tests/e2e/RESULTS.md` and `docs/COMPATIBILITY.md` (evidence only).
