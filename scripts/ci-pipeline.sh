#!/usr/bin/env bash
# ci-pipeline.sh - the full local gate of Fork for Linux (unofficial), fail-fast between stages:
#   1) lint       scripts/ci-docker.sh FL_CI_STAGE=lint
#   2) coverage   scripts/ci-docker.sh FL_CI_STAGE=coverage (Python gates + C coverage report)
#   3) bridge     scripts/ci-docker.sh FL_CI_STAGE=bridge (MinGW/musl, repro, Wine tier)
#   4) compat     scripts/ci-matrix.sh (7 cells in parallel)
#   5) packaging  scripts/ci-packaging-matrix.sh (9 formats in parallel)
# The same scripts run in .github/workflows/check.yml. Output is tee'd under logs/
# (AGENTS.md §4.6); read logs/ci-packaging/<format>.log afterwards (§4.8).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"
mkdir -p logs

stage() {
	local name="$1"
	shift
	echo "==> pipeline: stage ${name} ($(date -u +%H:%M:%SZ))"
	"$@" 2>&1 | tee "logs/ci-${name}.log"
}

stage lint env FL_CI_STAGE=lint ./scripts/ci-docker.sh
stage coverage env FL_CI_STAGE=coverage ./scripts/ci-docker.sh
stage bridge env FL_CI_STAGE=bridge ./scripts/ci-docker.sh
stage matrix ./scripts/ci-matrix.sh
stage packaging ./scripts/ci-packaging-matrix.sh

echo "==> pipeline: all stages passed (lint, coverage, bridge, compat x7, packaging x9)"
