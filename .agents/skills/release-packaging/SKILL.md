---
name: release-packaging
description: Local multi-format builds: deb, rpm, arch, snap, AppImage, Flatpak, tarball (+ PPA dry run, AUR render).
---

# Release Packaging

Local multi-format builds: deb, rpm, arch, snap, AppImage, Flatpak, tarball (+ PPA dry run, AUR render).

Canonical rules: [AGENTS.md](../../../AGENTS.md).

- One format: `./scripts/ci-packaging-cell.sh <deb|deb-jammy|rpm-fedora|rpm-opensuse|arch|snap|appimage|flatpak|tarball>`
  (build → `packaging-smoke-verify.sh` → `packaging-e2e-install.sh`). All: `./scripts/ci-packaging-matrix.sh`.
- PPA: `docker run --rm -v "$PWD:/src" -w /src fork-linux-ci-ppa:26.04 ./scripts/ppa-docker.sh --dry-run`
  → `artifacts/ppa/<series>/fork-linux_X+ppa1~ubuntuNN.NN.1*`.
- AUR: `./scripts/aur-render.sh [--tarball FILE]` → `artifacts/aur/{PKGBUILD,.SRCINFO}`.
- Rules (AGENTS.md §4.9): no maintainer scripts, amd64 only, never the distro `wine`, lintian `--fail-on error` without
  overrides, rpmlint without filters (any E/W fails), Flatpak finish-args each with `# why:`.
- Dependency names come from `fork_linux.hostdeps`: `python3 scripts/check-dep-names.py [--available FAMILY]`.
- Snap needs `--privileged` + host cgroups (systemd in Docker); Flatpak needs `--privileged` and Flathub runtimes.
