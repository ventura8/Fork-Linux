---
name: pipeline-runner
description: Full local gate: lint, coverage, bridge, compat x7, desktop integration x8, packaging x9; fix until green.
---

# Pipeline Runner

Full local gate: lint, coverage, bridge, compat x7, desktop integration x8, packaging x9; fix until green.

Canonical rules: [AGENTS.md](../../../AGENTS.md).

1. `./scripts/ci-pipeline.sh` (fail-fast; tees `logs/ci-{lint,coverage,bridge,matrix,e2e-de,packaging}.log`).
2. The coverage stage ends with the per-file gate (every file >= 90%, Python 100% line + branch, enforced by
   `scripts/check-coverage.py`): add tests for the files in its table, never exclusions.
   On a failure, re-run only that stage: `FL_CI_STAGE=<stage> ./scripts/ci-docker.sh`,
   `./scripts/ci-matrix.sh <cell>`, `./scripts/ci-e2e-de.sh <desktop>`, `./scripts/ci-packaging-cell.sh <format>`.
3. Read **every** `logs/ci-packaging/<format>.log`, `logs/ci-matrix/<cell>.log` and `logs/e2e-de/de-<desktop>.log`,
   not only exit codes.
4. Fix the cause. Never add suppressions, lower floors, skip steps or ignore warnings (AGENTS.md §4.8, hard rule 14).
   SonarCloud's own scan uploads (publishing) and serves only `main`'s results here: before a pull request,
   `python3 scripts/sonar-local.py` (also in the lint stage) checks the rules a scan has flagged in this
   repository (cognitive complexity, shell `return` / `case` defaults, duplicated blocks).
5. Re-run until every stage and cell is green; report each result honestly.
6. Real-Fork / Wine stages run only in containers (hard rule 18): the bridge stage through
   `ci-docker.sh`, the real tier through `./scripts/e2e-docker.sh --name <run> pytest tests/e2e`
   (or `./scripts/ci-e2e-wine.sh --all` / `tests/e2e/<module>.py...`, one module per scratch root, in
   parallel from a template root, as e2e-wine.yml's shards do), the real-Fork tier per desktop through `./scripts/ci-e2e-de-matrix.sh --fork` (not part of the
   pipeline: it holds the host-wide E2E slots). check.yml's lint job also runs the scenario registry gate
   (`gen-scenarios-doc.py validate` and `check`, even when the lint stage failed) and the coverage job the
   per-file gate (`scripts/check-coverage.py`). Never `ci-docker.sh --inside`, `ci-c-coverage.sh --no-docker`
   or `FL_E2E_FORK=1` / `FL_REAL_WINE=1` pytest on the host — they refuse.
