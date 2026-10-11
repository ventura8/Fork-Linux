#!/usr/bin/env bash
# ci-e2e-de-matrix.sh - the desktop E2E tier for every desktop (or the ones named), in parallel:
# one scripts/ci-e2e-de.sh per desktop. The integration tier (no Wine) runs all desktops at once;
# with --fork the real-Fork tiers start together too, but each one waits for one of the
# host-wide E2E slots (FL_E2E_SLOTS, shared with scripts/e2e-docker.sh), so at most that many
# run Wine at a time.
#
# Usage: scripts/ci-e2e-de-matrix.sh [--fork] [DESKTOP...]
#   DESKTOP   default: every docker/Dockerfile.e2e.de.<DESKTOP>
#
# Per-desktop output: logs/e2e-de/matrix-<NAME>.out (the runner's own output) next to the
# runner's logs/e2e-de/<NAME>.log and logs/e2e-de/<NAME>/. Exit status: 0 when every desktop
# passed, 1 otherwise (a summary lists each desktop's status).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
LOG_DIR="${ROOT}/logs/e2e-de"

usage() {
	sed -n '8p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

FORK_ARGS=()
DESKTOPS=()
while (($# > 0)); do
	case "$1" in
		--fork)
			FORK_ARGS=(--fork)
			shift
			;;
		-h | --help)
			usage
			exit 0
			;;
		-*)
			echo "ci-e2e-de-matrix.sh: unknown option: $1" >&2
			usage >&2
			exit 2
			;;
		*)
			DESKTOPS+=("$1")
			shift
			;;
	esac
done
if ((${#DESKTOPS[@]} == 0)); then
	for dockerfile in "${ROOT}"/docker/Dockerfile.e2e.de.*; do
		[[ -f "${dockerfile}" ]] || continue
		DESKTOPS+=("${dockerfile##*.}")
	done
fi
((${#DESKTOPS[@]} > 0)) || {
	echo "ci-e2e-de-matrix.sh: no docker/Dockerfile.e2e.de.* found" >&2
	exit 2
}
mkdir -p -- "${LOG_DIR}"

declare -A PIDS=()
for desktop in "${DESKTOPS[@]}"; do
	name="de-${desktop}"
	((${#FORK_ARGS[@]} == 0)) || name="${name}-fork"
	echo "==> ${desktop}: scripts/ci-e2e-de.sh ${desktop} ${FORK_ARGS[*]} (output: logs/e2e-de/matrix-${name}.out)"
	"${ROOT}/scripts/ci-e2e-de.sh" "${desktop}" "${FORK_ARGS[@]}" >"${LOG_DIR}/matrix-${name}.out" 2>&1 &
	PIDS["${desktop}"]=$!
done

failed=0
for desktop in "${DESKTOPS[@]}"; do
	if wait "${PIDS[${desktop}]}"; then
		printf '%-10s PASS\n' "${desktop}"
	else
		printf '%-10s FAIL (exit %s)\n' "${desktop}" "$?"
		failed=1
	fi
done
exit "${failed}"
