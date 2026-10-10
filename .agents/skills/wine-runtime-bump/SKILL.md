---
name: wine-runtime-bump
description: Bump the pinned Kron4ek Wine build / winetricks in the runtime manifest.
---

# Wine Runtime Bump

Bump the pinned Kron4ek Wine build / winetricks in the runtime manifest.

Canonical rules: [AGENTS.md](../../../AGENTS.md).

1. Update `src/fork_linux/data/runtime-manifest.json` (URL on the allowlist, size, sha256) — never mirror binaries.
2. `python3 -m pytest -q tests/test_manifest.py tests/test_wine_provider.py`.
3. Bridge tier + real E2E, containers only (hard rule 18): `FL_CI_STAGE=bridge ./scripts/ci-docker.sh`,
   `scripts/e2e-docker.sh --name wine-bump pytest tests/e2e` (put the new tarball in
   `/var/tmp/fork-linux-e2e/seed` or let the container download it) or `./scripts/ci-e2e-wine.sh`.
4. Record results in `tests/e2e/RESULTS.md` and `docs/COMPATIBILITY.md` (evidence only).
