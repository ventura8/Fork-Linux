---
name: fork-version-bump
description: Mark a new Fork version known-good (installer sha256 + E2E evidence).
---

# Fork Version Bump

Mark a new Fork version known-good (installer sha256 + E2E evidence).

Canonical rules: [AGENTS.md](../../../AGENTS.md).

1. `python3 scripts/check-upstream-fork.py` (reads the official feed; strict version validation).
2. Download the installer from `https://cdn.fork.dev/win/Fork-X.Y.Z.exe` to a scratch dir, then
   `python3 scripts/check-upstream-fork.py --add X.Y.Z --installer FILE [--feed-sha256 HEX]`. Never commit or upload the installer.
3. Real E2E in a container only (hard rule 18): `FL_E2E_FORK_VERSION=X.Y.Z scripts/e2e-docker.sh --name fork-X.Y.Z pytest tests/e2e`
   (or `./scripts/ci-e2e-wine.sh`); on success open the PR, on failure an issue "Fork X fails under Wine".
   `upstream-watch.yml` automates this weekly.
