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
- The daemon also runs the **ssh-agent relay** for Fork's bundled (msys) ssh (`--ssh-agent SOCK --ssh-agent-file
  FILE`, `bridge/unix/fl_agent_relay.{c,h}`; launcher side `bridge.agent_socket` / `_launch`). Tests:
  `bridge/tests/unit/test_agent_relay.c` (meson suite bridge-unit, includes the .c so its wraps survive
  `_FORTIFY_SOURCE`), `tests/bridge/test_daemon_protocol.py -k ssh_agent` (an msys-style client in Python; the
  bridge image carries `openssh-client` for the real-agent case), `tests/e2e/test_git_ops_remote_ssh.py`.
