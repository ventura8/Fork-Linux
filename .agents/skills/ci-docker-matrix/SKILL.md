---
name: ci-docker-matrix
description: Docker CI stages and per-distro compat cells (docker/Dockerfile.ci*), image tags and caching.
---

# Ci Docker Matrix

Docker CI stages and per-distro compat cells (docker/Dockerfile.ci*), image tags and caching.

Canonical rules: [AGENTS.md](../../../AGENTS.md).

- Stages: `FL_CI_STAGE=lint|coverage|bridge|compat ./scripts/ci-docker.sh`; cells `FL_CI_CELL=jammy|noble|resolute|debian13|fedora44|leap16|arch`.
- One Dockerfile per stage / cell under `docker/`; `# syntax=docker/dockerfile:1.28.0`; pinned bases only
  (`ubuntu:26.04|24.04|22.04`, `debian:13.7` (latest point release), `fedora:44`, `opensuse/leap:16.0`, dated `archlinux:base(-devel)-YYYYMMDD.0.N`).
- Each Dockerfile declares `# Image tag: fork-linux-ci-<name>:<FROM tag>`; scripts must use exactly that tag (`tests/test_docker.py`).
- Pip pins are exact `==`; tools come from pinned images (`rhysd/actionlint:1.7.12`, `hadolint/hadolint:v2.15.1`).
- The coverage stage runs `scripts/ci-c-coverage.sh` and then `scripts/check-coverage.py` (every file >= 90%) on the
  host side; `FL_CI_C_COVERAGE=0` skips both (check.yml runs them as steps of the coverage job).
- Stage / cell containers run as the invoking uid; `ci-docker.sh` and `ci-c-coverage.sh` mount the image's `/etc/passwd`
  plus a line for that uid (`fl-ci`, home `/nonexistent`) read-only, since ssh-keygen refuses a uid without one
  (Fedora compat cells and GHA runners at uid 1001).
- `e2e-wine.yml` frees runner toolcaches (`/usr/share/dotnet`, Android SDK, …) before the bridge build and the real
  tier so a ~2.8 GiB template plus `matrix.jobs: 2` live copies fit on the runner.
- Caching: `FL_CI_DOCKER_CACHE=gha|local|none`, `FL_CI_FORCE_BUILD=1`, `FL_CI_PARALLEL_BUILD=1` in the matrix.
- Real-Fork image `fork-linux-ci-e2e-wine:26.04` (`docker/Dockerfile.e2e.wine`): CI via `scripts/ci-e2e-wine.sh` (e2e-wine.yml: five shards, each
  one template root and its modules two at a time, every module in a root of its own; a module that failed with Velopack's "Failed to start application (OS Error 731)" is re-run once
  in a fresh root, logged in `logs/e2e-wine/reruns.log`), locally
  only via `scripts/e2e-docker.sh` (hard rule 18; shared code in `scripts/lib-e2e-docker.sh`). `ci-docker.sh --inside`
  refuses outside a container. It carries `fonts-noto-core` / `fonts-noto-cjk`: without them `prefix.font_fallback`
  warns and the E2E doctor step (no warnings allowed) fails.
- Packaging cells (`scripts/ci-packaging-cell.sh`) mount a linked worktree's git common dir read-only
  (`fl_git_mount_args`) and build the previous release (`FL_PACKAGING_PREVIOUS`, default 0.9.9) into
  `<artifacts>/previous` for the real upgrade / downgrade E2E; check.yml does not upload that directory.
- Desktop E2E images `fork-linux-e2e-de-<desktop>:26.04` (`docker/Dockerfile.e2e.de.<desktop>`, present: `xfce`, `gnome`, `kde`,
  `kdewayland`, `cinnamon`, `mate`, `budgie`, `lxqt`): `scripts/ci-e2e-de.sh <desktop> [--fork]` / `scripts/ci-e2e-de-matrix.sh [--fork]`,
  same library and rules as `e2e-docker.sh`; one recipe per image in `tests/e2e_de/desktops.py`. CI: `check.yml` job
  `e2e-de` (integration, every PR, `FL_CI_DOCKER_CACHE=gha`) and `e2e-wine.yml` job `e2e-de` (real Fork, fresh downloads);
  `tests/test_e2e_de_recipes.py` keeps images, recipes, checkers and both matrices in step. Runbook: `docs/qa/DE-E2E.md`.
- When you change an image, script or cell: update AGENTS.md §4.8, this skill and `tests/test_docker.py` in the same change.
