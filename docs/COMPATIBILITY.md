# Compatibility

Filled **only from recorded evidence** (AGENTS.md §1). "Package E2E" means the packaging
cell's live cycle (install → assert → upgrade → remove → reinstall, run as an unprivileged
user) passed in a container of that distribution; it does not start Fork. The real-Fork tier
(`scripts/ci-e2e-wine.sh`, `tests/e2e/RESULTS.md`) is recorded separately.

## Packages (amd64 / x86_64)

| Channel | Distribution (container) | Build | Package E2E | Evidence |
|---|---|---|---|---|
| `.deb` | Ubuntu 26.04 | ✅ lintian clean | ✅ | local `ci-packaging-cell.sh deb`, 2026-10-11 |
| `.deb` | Ubuntu 22.04 | ✅ lintian clean | ✅ | local `ci-packaging-cell.sh deb-jammy`, 2026-10-11 |
| `.rpm` | Fedora 44 | ✅ rpmlint clean | ✅ | local cell `rpm-fedora`, 2026-10-11 |
| `.rpm` | openSUSE Leap 16.0 | ✅ rpmlint clean (MinGW available, no prebuilt shims needed) | ✅ | local cell `rpm-opensuse`, 2026-10-11 |
| `.pkg.tar.zst` | Arch Linux (2026-10-04 base, updated) | ✅ | ✅ | local cell `arch`, 2026-10-11 |
| tarball + `install.sh` | Ubuntu 26.04 | ✅ | ✅ (per user) | local cell `tarball`, 2026-10-11 |
| AppImage | Ubuntu 26.04 (type2 runtime 20251108) | ✅ | ✅ (`--install-desktop`) | local cell `appimage`, 2026-10-11 |
| Flatpak bundle | Ubuntu 26.04 host, freedesktop 26.08, org.winehq.Wine stable-26.08 (WoW64) | ✅ | ✅ | local cell `flatpak`, 2026-10-11 |
| snap (classic, core26) | Ubuntu 26.04 (snapd in a systemd container, snapcraft 9.1.4) | ✅ | ✅ | local cell `snap`, 2026-10-11 |

## Source / test suite (compat cells)

| Cell | meson + tests | Evidence |
|---|---|---|
| jammy (22.04, Python 3.10), resolute (26.04), noble (24.04), Debian 13.7, Fedora 44, openSUSE Leap 16.0, Arch (base-20261004.0.606936) | ✅ | `./scripts/ci-matrix.sh`, 2026-10-11 |

## Real Fork under Wine

Not yet recorded by CI for v1.0.0; see `tests/e2e/RESULTS.md` for the manual runs.
