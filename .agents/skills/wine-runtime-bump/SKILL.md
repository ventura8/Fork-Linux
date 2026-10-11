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
3. Bridge tier + real E2E, containers only (hard rule 18): `FL_CI_STAGE=bridge ./scripts/ci-docker.sh`,
   `scripts/e2e-docker.sh --name wine-bump pytest tests/e2e` (put the new tarball in
   `/var/tmp/fork-linux-e2e/seed` or let the container download it) or `./scripts/ci-e2e-wine.sh`.
   An existing user's prefix moves to the new build on its next launch: also run the app-upgrade tier
   (`FL_E2E_APP_UPGRADE=1 ... pytest tests/e2e/test_wine_app_upgrade.py`, A4 switches a set-up prefix to
   another build and back) with the new build as `WINE_B`, and check that Fork itself starts on it: Fork
   2.23.1 does not start on Kron4ek 11.19 staging (UPD-26 in `docs/qa/UPDATE-SCENARIOS.md`).
4. Record results in `tests/e2e/RESULTS.md` and `docs/COMPATIBILITY.md` (evidence only).
