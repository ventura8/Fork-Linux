---
name: wine-runtime-bump
description: Bump the pinned Kron4ek Wine build / winetricks in the runtime manifest.
---

# Wine Runtime Bump

Bump the pinned Kron4ek Wine build / winetricks in the runtime manifest.

Canonical rules: [AGENTS.md](../../../AGENTS.md).

1. Update `src/fork_linux/data/runtime-manifest.json` (URL on the allowlist, size, sha256) — never mirror binaries.
   Take size + sha256 from the downloaded file itself and compare them with the release's `sha256sums.txt` and
   the GitHub asset digest. Keep the previous default listed (status `known-good`) so
   `fork-linux config set wine.build <old id>` goes back without a download, and record why a newer build was
   not taken in `wine._comment`. The same rules apply to the `ui_font` pin (Selawik release zip on `github.com`;
   `faces` lists the `.ttf` members copied into the prefix); bump `revision` with any pin change.
2. `python3 -m pytest -q tests/test_manifest.py tests/test_wine_provider.py tests/test_steps_preflight.py`
   (the consent text names the build and its download size).
3. Bridge tier + real E2E, containers only (hard rule 18): `FL_CI_STAGE=bridge ./scripts/ci-docker.sh`,
   `scripts/e2e-docker.sh --name wine-bump pytest tests/e2e` (put the new tarball in
   `/var/tmp/fork-linux-e2e/seed` or let the container download it) or `./scripts/ci-e2e-wine.sh`.
   A fresh prefix is not enough: also take a prefix the **previous** pin created, with Fork used in it,
   to the new pin (the A4 path: new runtime, `wineboot -u`, the Wine-dependent steps) and check that Fork
   starts there and commits, and that `doctor` has no new warnings. Wine's `wine.inf` re-registers some of
   its own file types on that update (`.js` -> `JSFile`); `host_integration` has the Wine build in its
   inputs for that reason. `tests/e2e/test_wine_pin_upgrade.py` (scenario A24) is exactly that path: set its
   `OLD_ID` to the pin being replaced (it must stay listed in the manifest) and run it through
   `scripts/e2e-docker.sh`. The app-upgrade tier covers the switch too
   (`FL_E2E_APP_UPGRADE=1 ... pytest tests/e2e/test_wine_app_upgrade.py`: A4 switches a set-up prefix to the
   previous pin and back).
4. A new Kron4ek release is a development build: Fork may not start on it at all (2026-10-11: Kron4ek
   11.19 staging, `System.IO.FileLoadException` 0x80131047 while WPF builds Fork's main window, fresh and
   upgraded prefixes alike). Bisect the releases between the current pin and the newest one with one
   prefix and a pin per probe (a manifest override with a new `revision` and `wine.default`, then
   `setup` and a Fork start), then run step 3 on the newest good build.
5. Record results in `tests/e2e/RESULTS.md` and `docs/COMPATIBILITY.md` (evidence only).
