---
name: pipeline-runner
description: Full local gate: lint, coverage, bridge, compat x7, packaging x9; fix until green.
---

# Pipeline Runner

Full local gate: lint, coverage, bridge, compat x7, packaging x9; fix until green.

Canonical rules: [AGENTS.md](../../../AGENTS.md).

1. `./scripts/ci-pipeline.sh` (fail-fast; tees `logs/ci-{lint,coverage,bridge,matrix,packaging}.log`).
2. On a failure, re-run only that stage: `FL_CI_STAGE=<stage> ./scripts/ci-docker.sh`,
   `./scripts/ci-matrix.sh <cell>`, `./scripts/ci-packaging-cell.sh <format>`.
3. Read **every** `logs/ci-packaging/<format>.log` and `logs/ci-matrix/<cell>.log`, not only exit codes.
4. Fix the cause. Never add suppressions, lower floors, skip steps or ignore warnings (AGENTS.md §4.8, hard rule 14).
5. Re-run until every stage and cell is green; report each result honestly.
