# Compatibility

Filled **only from recorded evidence** (AGENTS.md §1). "Package E2E" means the packaging
cell's live cycle (install → assert → upgrade → remove → reinstall, run as an unprivileged
user) passed in a container of that distribution; it does not start Fork. The real-Fork tier
(`scripts/ci-e2e-wine.sh`, `tests/e2e/RESULTS.md`) is recorded separately.

## Packages (amd64 / x86_64)

| Channel | Distribution (container) | Build | Package E2E | Evidence |
|---|---|---|---|---|
| `.deb` | Ubuntu 26.04 | ✅ lintian clean | ✅ | local `ci-packaging-cell.sh deb`, 2026-10-09 |
| `.deb` | Ubuntu 22.04 | ❌ gcc 11 `-Werror=maybe-uninitialized` in `bridge/unix/fl_helper_util.c` | — | local `ci-packaging-cell.sh deb-jammy`, 2026-10-09 |
| `.rpm` | Fedora 44 | ✅ rpmlint clean | ✅ | local cell `rpm-fedora`, 2026-10-09 |
| `.rpm` | openSUSE Leap 16.0 | ✅ rpmlint clean (MinGW available, no prebuilt shims needed) | ✅ | local cell `rpm-opensuse`, 2026-10-09 |
| `.pkg.tar.zst` | Arch Linux (2026-09-06 base, updated) | ✅ | ✅ | local cell `arch`, 2026-10-09 |
| tarball + `install.sh` | Ubuntu 26.04 | ✅ | ✅ (per user) | local cell `tarball`, 2026-10-09 |
| AppImage | Ubuntu 26.04 | ✅ | ✅ (`--install-desktop`) | local cell `appimage`, 2026-10-09 |
| Flatpak bundle | Ubuntu 26.04 host, org.winehq.Wine wow64-25.08 | ✅ | ✅ | local cell `flatpak`, 2026-10-09 |
| snap (classic, core24) | Ubuntu 24.04 (snapd in a systemd container) | ✅ | ✅ | local cell `snap`, 2026-10-09 |

## Source / test suite (compat cells)

| Cell | meson + tests | Evidence |
|---|---|---|
| jammy (22.04, Python 3.10), resolute (26.04), noble (24.04), Debian 13, Fedora 44, openSUSE Leap 16.0, Arch | ✅ | `./scripts/ci-matrix.sh`, 2026-10-09 |

## Real Fork under Wine

Not yet recorded by CI for v1.0.0; see `tests/e2e/RESULTS.md` for the manual runs.

Managed Wine builds (Kron4ek staging WoW64) with Fork 2.23.2, `scripts/e2e-docker.sh` on an Ubuntu 26.04
host, 2026-10-11 (details: `tests/e2e/RESULTS.md`, "Wine pin 11.0 -> 11.18"):

| Build | Fresh prefix | Prefix of the previous pin (11.0) moved to it | Evidence |
|---|---|---|---|
| `kron4ek-11.0-staging-wow64` (previous pin) | ✅ | — | real tier, 2026-10-09 |
| `kron4ek-11.18-staging-wow64` (**default**) | ✅ | ✅ (Fork starts, commits) | `test_real_fork.py` 7/7, `test_fork_update.py` 13/13, `test_wine_app_upgrade.py` 6/6 (A6 skipped), pin-upgrade scenario 3/3 |
| `kron4ek-11.19-staging-wow64` | ❌ | ❌ | Fork exits at start: `System.IO.FileLoadException` 0x80131047 (C.UTF-8 and en_US.UTF-8) |
| 11.9, 11.14, 11.16, 11.17 | — | ✅ (Fork's main window) | bisection probes in one prefix |
