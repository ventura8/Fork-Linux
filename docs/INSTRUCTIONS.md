# Fork for Linux (unofficial) — setup, build, extend, debug

Canonical rules: [AGENTS.md](../AGENTS.md). Component details: [architecture/README.md](architecture/README.md).

## 1. Run from a checkout

```sh
python3 bin/fork-linux.in --version        # the launcher finds src/ in a checkout
python3 -m pytest -q                       # unit + contract tests (no network, no Wine, no real home)
```

Python 3.10+, standard library only; pytest (+ pytest-cov) is the only test dependency.

## 2. Build and install with meson

```sh
meson setup build --prefix="$HOME/.local" -Dbridge=auto
meson compile -C build
meson test -C build                        # data checks (+ bridge unit tests with -Dtests=true)
DESTDIR=/tmp/stage meson install -C build  # inspect the layout
```

Options (`meson.options`): `bridge` (feature; MinGW-w64 shims), `bridge_prebuilt_dir`
(install shims built elsewhere when MinGW is missing), `tests`, `python` (launcher shebang;
portable builds use `/usr/bin/env -S python3`), `flavor` (baked into `fork_linux/_build.py`),
`fork_alias` (the `fork` symlink), `desktop`, `file_manager_actions` (system-wide "Open in
Fork"; off by default because `fork-linux desktop install` adds per-user ones).

Installed layout: `bin/fork-linux` + `bin/fork`, `share/fork-linux/fork_linux/`,
`lib/fork-linux/` (host helpers, `fl-bridge-helper` + personas, `win64/*.exe`),
`share/applications`, `share/metainfo`, `share/icons/hicolor`, `share/man/man1`,
bash / zsh / fish completions.

### Generated data (single sources)

`scripts/gen-data.py` renders, from the Python package:

| Output | Source |
|---|---|
| system `.desktop` | `src/fork_linux/data/templates/io.github.ventura8.ForkLinux.desktop.in` via `desktop_integration` (identical to the per-user entry for `fork-linux` on `PATH`) |
| metainfo | `data/io.github.ventura8.ForkLinux.metainfo.xml.in` + `credits.render_metainfo_paragraphs()` |
| file-manager actions | the per-user templates |
| `data/completions/*`, `data/man/*.1` | the argparse parser (committed; `scripts/gen-data.py write` regenerates, `check` fails on drift) |

After changing a command or option: `python3 scripts/gen-data.py write`.

## 3. The CI gate (Docker)

```sh
FL_CI_STAGE=lint ./scripts/ci-docker.sh        # every linter (see AGENTS.md §4.8)
FL_CI_STAGE=coverage ./scripts/ci-docker.sh    # pytest gates + ASan/UBSan unit tests + C coverage
FL_CI_STAGE=bridge ./scripts/ci-docker.sh      # MinGW/musl, reproducibility, FL_REAL_WINE=1 tier
FL_CI_STAGE=compat FL_CI_CELL=jammy ./scripts/ci-docker.sh
./scripts/ci-matrix.sh                         # 7 compat cells in parallel
./scripts/ci-packaging-cell.sh deb             # one packaging cell: build + smoke + live E2E
./scripts/ci-packaging-matrix.sh               # 9 cells in parallel
./scripts/ci-pipeline.sh                       # everything, fail-fast, tee'd under logs/
./scripts/ci-e2e-wine.sh                       # the real Fork under Wine (Xvfb, ~2.5 GB download)
```

Read every `logs/ci-packaging/<format>.log` after a packaging run (exit 0 is not enough).

## 4. Packaging

| Format | Build | Image |
|---|---|---|
| deb / deb-jammy | `scripts/release-deb.sh` (dpkg-buildpackage + lintian `--fail-on error`) | `Dockerfile.ppa{,.jammy}` |
| PPA | `scripts/ppa-docker.sh --dry-run` (unsigned `0.1.0+ppa1~ubuntuNN.NN.1` source packages) | `Dockerfile.ppa` |
| rpm | `scripts/release-rpm.sh fedora|opensuse` (rpmlint, any E/W fails) | `Dockerfile.rpm.*` |
| Arch | `scripts/release-arch.sh` (makepkg; `.SRCINFO` checked against makepkg); AUR: `scripts/aur-render.sh` | `Dockerfile.arch` |
| tarball / AppImage / Flatpak | `scripts/release-portable.sh tarball|appimage|flatpak` | `Dockerfile.release` |
| snap | `scripts/ci-snap-build.sh` (systemd container) | `Dockerfile.snap` |

`install.sh` / `uninstall.sh` install the tarball per user (`~/.local/opt/fork-linux`, links
in `~/.local/bin`) or `--system` (`/usr/local`), verify `SHA256SUMS`, record a manifest and
remove exactly what they recorded.

## 5. Releasing

Bump `VERSION`, add a `debian/changelog` entry and a metainfo `<release>`, write
`docs/releases/vX.Y.Z.md` + `vX.Y.Z_github_description.md`, then
`./scripts/release-verify-tag-version.sh vX.Y.Z`. Pushing the tag (maintainer only) runs
`.github/workflows/release.yml`; `workflow_dispatch` with `dry_run` builds everything without
publishing.

## 6. Debugging

- `fork-linux doctor [--json] [--deep]`, `fork-linux logs [--bundle]`, `fork-linux run --debug`.
- Packaging E2E failures: the cell log names the failed assert (`E2E FAIL (<format>): ...`).
- Docker images are cached by Dockerfile digest; `FL_CI_FORCE_BUILD=1` rebuilds.
