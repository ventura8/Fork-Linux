#!/usr/bin/env bash
# ci-matrix.sh - run every compat cell of Fork for Linux (unofficial) in parallel.
#
# Cells (AGENTS.md §4.8): jammy noble resolute debian13 fedora44 leap16 arch, each
# scripts/ci-docker.sh FL_CI_STAGE=compat in its own image, never sequentially.
# Logs: logs/ci-matrix/<cell>.log. Usage: ./scripts/ci-matrix.sh [cell...]
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

CELLS=(jammy noble resolute debian13 fedora44 leap16 arch)
if (($# > 0)); then
	CELLS=("$@")
fi
LOG_DIR="${ROOT}/logs/ci-matrix"
mkdir -p "${LOG_DIR}"

declare -a PIDS=()
declare -a CELL_FOR_PID=()

echo "==> ci-matrix: launching ${#CELLS[@]} compat cells in parallel"
for cell in "${CELLS[@]}"; do
	log="${LOG_DIR}/${cell}.log"
	(
		export FL_CI_STAGE=compat
		export FL_CI_CELL="${cell}"
		export FL_CI_PARALLEL_BUILD=1
		echo "[${cell}] compat cell (log: ${log})"
		./scripts/ci-docker.sh
	) >"${log}" 2>&1 &
	PIDS+=("$!")
	CELL_FOR_PID+=("${cell}")
	echo "==> started compat cell=${cell} pid=$! -> ${log}"
done

fail=0
failed=()
for i in "${!PIDS[@]}"; do
	if wait "${PIDS[$i]}"; then
		echo "==> OK   compat cell=${CELL_FOR_PID[$i]}"
	else
		echo "==> FAIL compat cell=${CELL_FOR_PID[$i]} (see logs/ci-matrix/${CELL_FOR_PID[$i]}.log)" >&2
		fail=1
		failed+=("${CELL_FOR_PID[$i]}")
	fi
done

if ((fail)); then
	echo "error: ci-matrix failed for: ${failed[*]}" >&2
	exit 1
fi
echo "==> ci-matrix: all ${#CELLS[@]} compat cells passed"
