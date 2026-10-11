---
name: release-packaging
description: Local multi-format builds: deb, rpm, arch, snap, AppImage, Flatpak, tarball (+ PPA dry run, AUR render).
---

# Release Packaging

Local multi-format builds: deb, rpm, arch, snap, AppImage, Flatpak, tarball (+ PPA dry run, AUR render).

Canonical rules: [AGENTS.md](../../../AGENTS.md).

- One format: `./scripts/ci-packaging-cell.sh <deb|deb-jammy|rpm-fedora|rpm-opensuse|arch|snap|appimage|flatpak|tarball>`
  (build → `packaging-smoke-verify.sh` → N-1 build (`build-version-variant.sh`, `FL_PACKAGING_PREVIOUS`, default 0.9.9) →
  `packaging-e2e-install.sh`: N-1 → N upgrade, downgrade and back, upgrade in place, remove, reinstall). After each
  operation it also checks what the next launch would re-run against a simulated setup N-1 left in the test user's
  home (`scripts/packaging-e2e-state.py`; the `ok  next launch re-runs ...` lines: nothing under N-1, `host_shims`
  under N, plus `desktop_entry` / `icon` after a per-user remove; never a downloading step). All:
  `./scripts/ci-packaging-matrix.sh`; read every `logs/ci-packaging/<format>.log`. CI runs the same cell for every
  format in `check.yml` (push to main, same-repo PRs) and `release.yml`, N-1 -> N upgrade included
  (`tests/test_workflows.py::test_check_packaging_runs_the_real_upgrade_per_format`).
- PPA: `docker run --rm -v "$PWD:/src" -w /src fork-linux-ci-ppa:26.04 ./scripts/ppa-docker.sh --dry-run`
  → `artifacts/ppa/<series>/fork-linux_X+ppa1~ubuntuNN.NN.1*`.
- AUR: `./scripts/aur-render.sh [--tarball FILE]` → `artifacts/aur/{PKGBUILD,.SRCINFO}`.
- Rules (AGENTS.md §4.9): no maintainer scripts, amd64 only, never the distro `wine`, lintian `--fail-on error` without
  overrides, rpmlint without filters (any E/W fails), Flatpak finish-args each with `# why:`.
- Another version (upgrade tests, N-1 = 0.9.9): `./scripts/build-version-variant.sh [--date D] 0.9.9 OUTDIR FORMAT...`
  inside the format's image (worktree read-only is fine; output to OUTDIR), or `--tree-only` for just the rewritten tree.
  The upgrade / downgrade scenarios it serves: `docs/qa/UPDATE-SCENARIOS.md` (A1, A2, A8, A10, A11).
- Dependency names come from `fork_linux.hostdeps`: `python3 scripts/check-dep-names.py [--available FAMILY]`.
- Snap needs `--privileged` + host cgroups (systemd in Docker); Flatpak needs `--privileged` and Flathub runtimes.
