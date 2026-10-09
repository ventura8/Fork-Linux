#!/usr/bin/env bash
# ci-packaging-matrix.sh - run every packaging cell of Fork for Linux (unofficial) in parallel
# (never sequentially), each into artifacts/ci-packaging/<format>, logging to
# logs/ci-packaging/<format>.log. Mirrors the packaging matrix of check.yml. Read every log
# afterwards: exit 0 is not enough (AGENTS.md §4.8).
#
# Usage: ./scripts/ci-packaging-matrix.sh [format...]
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

FORMATS=(deb deb-jammy rpm-fedora rpm-opensuse arch snap appimage flatpak tarball)
if (($# > 0)); then
	FORMATS=("$@")
fi
LOG_DIR="${ROOT}/logs/ci-packaging"
mkdir -p "${LOG_DIR}"

# The images are shared by several cells (tarball / appimage / flatpak): build them once
# up front so parallel cells never race on the same tag.
DOCKER_BUILDKIT=1 docker build -q -f docker/Dockerfile.release -t fork-linux-ci-release:26.04 docker >/dev/null

declare -a PIDS=()
declare -a FMT_FOR_PID=()
echo "==> ci-packaging-matrix: launching ${#FORMATS[@]} cells in parallel"
for fmt in "${FORMATS[@]}"; do
	log="${LOG_DIR}/${fmt}.log"
	(
		export FL_PACKAGING_ARTIFACTS_ISOLATE=1
		echo "[${fmt}] packaging cell (log: ${log})"
		./scripts/ci-packaging-cell.sh "${fmt}"
	) >"${log}" 2>&1 &
	PIDS+=("$!")
	FMT_FOR_PID+=("${fmt}")
	echo "==> started packaging format=${fmt} pid=$! -> ${log}"
done

fail=0
failed=()
for i in "${!PIDS[@]}"; do
	if wait "${PIDS[$i]}"; then
		echo "==> OK   packaging format=${FMT_FOR_PID[$i]}"
	else
		echo "==> FAIL packaging format=${FMT_FOR_PID[$i]} (see logs/ci-packaging/${FMT_FOR_PID[$i]}.log)" >&2
		fail=1
		failed+=("${FMT_FOR_PID[$i]}")
	fi
done
if ((fail)); then
	echo "error: ci-packaging-matrix failed for: ${failed[*]}" >&2
	exit 1
fi
echo "==> ci-packaging-matrix: all ${#FORMATS[@]} cells passed"
