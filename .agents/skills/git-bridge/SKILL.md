---
name: git-bridge
description: Experimental native-git bridge: shims, helper, record mode.
---

# Git Bridge

Experimental native-git bridge: shims, helper, record mode.

Canonical rules: [AGENTS.md](../../../AGENTS.md).

- Build + unit tests: `./scripts/build-bridge.sh [--repro]`; Wine tier (containers only, hard rule 18):
  `FL_CI_STAGE=bridge ./scripts/ci-docker.sh`. By hand under Wine: `scripts/e2e-docker.sh --name shim
  --image fork-linux-ci-bridge:26.04 shell -- bash` (never `FL_REAL_WINE=1` or `wine` on the host).
- Install layout and protocol: `bridge/README.md`. Packages ship the glibc PIE helper; tarball / AppImage / snap ship the
  static musl helper (ET_EXEC), checked by the packaging E2E.
- C rules: C11, `-Werror`, clang-tidy (`.clang-tidy`, WarningsAsErrors), cppcheck, no `system()` / shell strings.
