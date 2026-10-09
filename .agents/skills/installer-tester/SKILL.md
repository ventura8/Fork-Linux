---
name: installer-tester
description: install.sh / uninstall.sh behaviour and tests.
---

# Installer Tester

install.sh / uninstall.sh behaviour and tests.

Canonical rules: [AGENTS.md](../../../AGENTS.md).

- Unit tests: `python3 -m pytest -q tests/test_install_sh.py` (fake tarball, local HTTP server, temp HOME).
- Live: `./scripts/ci-packaging-cell.sh tarball` (installs as the unprivileged `tester`, upgrade, uninstall, reinstall).
- Invariants: whole body in `main()`; HTTPS only (loopback http for tests); SHA256SUMS verified before unpacking;
  per-user tree `~/.local/opt/fork-linux`, links in `~/.local/bin`; manifest-based removal of unchanged files only;
  `--purge` delegates to `fork-linux uninstall --purge`; refuses when a distro package owns fork-linux; never `~/.wine`.
- `shellcheck -x install.sh uninstall.sh`.
