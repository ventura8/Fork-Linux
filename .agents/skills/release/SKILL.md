---
name: release
description: Release notes from all branch changes, VERSION bump, release commit (maintainer-gated).
---

# Release

Release notes from all branch changes, VERSION bump, release commit (maintainer-gated).

Canonical rules: [AGENTS.md](../../../AGENTS.md).

1. Write `VERSION`; add the `debian/changelog` top entry (series `resolute`) and a metainfo `<release>` in
   `data/io.github.ventura8.ForkLinux.metainfo.xml.in`.
2. Write `docs/releases/vX.Y.Z.md` and `docs/releases/vX.Y.Z_github_description.md` covering **all** branch changes,
   with the not-affiliated disclaimer and the buy link.
3. `./scripts/release-verify-tag-version.sh vX.Y.Z` and `python3 -m pytest -q tests/test_packaging_release.py tests/test_workflows.py`.
4. Commit `release: vX.Y.Z - <Title>` only when asked. Never tag, push or publish without the maintainer (hard rule 17).
5. The tag runs `release.yml`: verify → PPA (gated) → 9 cells → GitHub Release with SHA256SUMS(.asc).
   A `workflow_dispatch` with `dry_run` builds everything and publishes nothing.
