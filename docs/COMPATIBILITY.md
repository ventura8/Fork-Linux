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

## Desktops (desktop E2E tier)

fork-linux installed from the release tarball inside a real desktop session in a container
(`scripts/ci-e2e-de.sh`, [docs/qa/DE-E2E.md](qa/DE-E2E.md)): menu entry, file-manager action,
theme and scale detection, terminal, reveal, default applications; the real-Fork column sets up
the official Fork 2.23.2 and drives it under the desktop's window manager.

| Desktop (container) | Integration | Real Fork | Evidence |
|---|---|---|---|
| Xubuntu 26.04, XFCE 4.20 (xfwm4, Thunar, xfce4-terminal) | ✅ 13/13 | ✅ 9/9 | local `scripts/ci-e2e-de.sh xfce [--fork]`, 2026-10-11 |
| Kubuntu 26.04, KDE Plasma 6.6 on X11 (kwin_x11, Dolphin, Konsole) | ✅ 14/14 | ✅ 9/9 | local `scripts/ci-e2e-de.sh kde [--fork]`, 2026-10-11 |

Known desktop behaviour (not a fork-linux bug): on Xubuntu, XFCE's own Ctrl+Alt+T shortcut
shadows Fork's "Open In Shell" key; Fork's Repository menu and Shell button work.

## Real Fork under Wine

Not yet recorded by CI for v1.0.0; see `tests/e2e/RESULTS.md` for the manual runs.
