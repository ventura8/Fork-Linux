# Architecture

The high-level diagram and lifecycle rules are in [AGENTS.md §2](../../AGENTS.md). This page
maps the pieces to files.

## Runtime

| Piece | Files | Notes |
|---|---|---|
| Launchers | `bin/fork-linux.in` → `bin/fork-linux`, `bin/fork` (symlink) | `#!@PYTHON@ -I`; relocatable: finds `<root>/share/fork-linux` (or `src/` in a checkout); `fork` dispatches on argv[0] |
| CLI | `src/fork_linux/cli.py`, `commands/*` | argparse; also the single source of the completions and man pages (`scripts/gen-data.py`) |
| Setup | `bootstrap.py`, `steps/*` | resumable steps with markers; one flock |
| Launch | `launcher.py`, `pathmap.py`, `gitconfig.py` | `os.execve` into Wine |
| Desktop integration | `desktop_integration.py`, `data/templates/*` | per-user menu entry and file-manager actions; packages ship the same entry system-wide |
| Host helpers | `libexec/*` → `<prefix>/lib/fork-linux/` | terminal / explorer / open-url hand-off outside Wine's process tree |
| Bridge | `bridge/` → `fl-bridge-helper` + personas, `win64/fl-shim.exe`, `fl-launch.exe` | see [bridge/README.md](../../bridge/README.md) |

## Build and packaging

| Piece | Files |
|---|---|
| Build | `meson.build`, `meson.options`, `bridge/meson.build`, `data/fm-actions/` |
| Generated data | `scripts/gen-data.py` (desktop, metainfo, file-manager actions, `_build.py`, completions, man pages) |
| Debian / PPA | `debian/`, `scripts/release-deb.sh`, `scripts/ppa-docker.sh` |
| RPM | `packaging/rpm/{fedora,opensuse}/fork-linux.spec`, `scripts/release-rpm.sh` |
| Arch / AUR | `packaging/arch/PKGBUILD.in`, `scripts/release-arch.sh`, `scripts/aur-render.sh` |
| Portable | `scripts/release-portable.sh`, `packaging/appimage/`, `packaging/flatpak/`, `packaging/snap/`, `packaging/common/find-python.sh`, `install.sh`, `uninstall.sh` |
| Shared helpers | `scripts/release-common.sh` |

Distribution packages ship the glibc PIE `fl-bridge-helper`; the portable channels (tarball,
AppImage, snap) ship the static musl build (ET_EXEC) so they run on any host libc. The
Flatpak runs on its runtime's glibc.

## CI

`scripts/ci-docker.sh` (stages lint / coverage / bridge / compat), `scripts/ci-matrix.sh`,
`scripts/ci-packaging-{cell,matrix}.sh`, `scripts/ci-pipeline.sh`, `scripts/ci-e2e-wine.sh`;
workflows `check.yml`, `release.yml`, `e2e-wine.yml`, `upstream-watch.yml`; one Dockerfile per
stage / cell under `docker/`.
