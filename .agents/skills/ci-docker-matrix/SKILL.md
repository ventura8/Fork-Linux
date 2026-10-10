---
name: ci-docker-matrix
description: Docker CI stages and per-distro compat cells (docker/Dockerfile.ci*), image tags and caching.
---

# Ci Docker Matrix

Docker CI stages and per-distro compat cells (docker/Dockerfile.ci*), image tags and caching.

Canonical rules: [AGENTS.md](../../../AGENTS.md).

- Stages: `FL_CI_STAGE=lint|coverage|bridge|compat ./scripts/ci-docker.sh`; cells `FL_CI_CELL=jammy|noble|resolute|debian13|fedora44|leap16|arch`.
- One Dockerfile per stage / cell under `docker/`; `# syntax=docker/dockerfile:1.27.0`; pinned bases only
  (`ubuntu:26.04|24.04|22.04`, `debian:13`, `fedora:44`, `opensuse/leap:16.0`, dated `archlinux:base(-devel)-YYYYMMDD.0.N`).
- Each Dockerfile declares `# Image tag: fork-linux-ci-<name>:<FROM tag>`; scripts must use exactly that tag (`tests/test_docker.py`).
- Pip pins are exact `==`; tools come from pinned images (`rhysd/actionlint:1.7.12`, `hadolint/hadolint:v2.14.0`).
- Caching: `FL_CI_DOCKER_CACHE=gha|local|none`, `FL_CI_FORCE_BUILD=1`, `FL_CI_PARALLEL_BUILD=1` in the matrix.
- Real-Fork image `fork-linux-ci-e2e-wine:26.04` (`docker/Dockerfile.e2e.wine`): CI via `scripts/ci-e2e-wine.sh`, locally
  only via `scripts/e2e-docker.sh` (hard rule 18; shared code in `scripts/lib-e2e-docker.sh`). `ci-docker.sh --inside`
  refuses outside a container.
- When you change an image, script or cell: update AGENTS.md §4.8, this skill and `tests/test_docker.py` in the same change.
