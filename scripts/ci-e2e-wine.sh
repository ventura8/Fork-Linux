#!/usr/bin/env bash
# ci-e2e-wine.sh - the real end-to-end tier of Fork for Linux (unofficial): FL_E2E_FORK=1
# tests/e2e (setup with the managed Wine, .NET and the official Fork installer, Fork's window
# under Xvfb, fork <repo>, doctor, snapshot / rollback, purge) in docker/Dockerfile.e2e.wine,
# as the invoking unprivileged user with a scratch HOME (never ~/.wine, never root).
#
# Downloads happen on this machine at run time (~2.5 GB); nothing downloaded is cached in
# CI or uploaded, and no seed is used. Only logs are kept: logs/e2e-wine/ (screenshots show
# Fork's logo and stay in the scratch root, which is deleted afterwards).
#
# Each scratch root is a fresh directory under ${TMPDIR:-/tmp}, refused when it is or lies
# inside a home directory (AGENTS.md hard rule 18); a container sees only the read-only
# source tree and its root. It shares scripts/lib-e2e-docker.sh (scratch checks, host-wide
# slots, image build, logs-only copy) with scripts/e2e-docker.sh, the local runner.
#
# Every test module of tests/e2e is one scenario whose steps build on each other, so each runs
# in a pytest session and a fresh scratch root of its own (one container per module, the root
# deleted afterwards), and one module's failure never touches another's. Logs go to
# logs/e2e-wine/<module>/ (pytest.log + the root's text logs, paths relative to the root):
#   test_real_fork.py        always: setup, Fork's window, doctor, snapshot / rollback, purge
#   test_fork_update.py      FL_E2E_FORK_UPDATES=1: Fork 2.23.1 updating itself and through
#                            fork-linux; its bridge scenarios need the bridge, built first
#                            (scripts/build-bridge.sh, in its own CI image) when it is missing
#   test_wine_app_upgrade.py FL_E2E_APP_UPGRADE=1: upgrades of fork-linux itself. Its A6 step
#                            replaces the bridge binaries of one version by another's while the
#                            daemon runs, so two bridge builds of different versions are made
#                            first (variant trees of this one, 0.9.1 and 0.9.2, by
#                            scripts/build-version-variant.sh --tree-only) into a directory of
#                            their own, mounted read-only (FL_E2E_BRIDGE_BUILDS=/bridge-builds)
# Every module runs even when an earlier one failed; the exit status is the first failure's.
# A skipped test fails its module: every tier this script runs was asked for, and its tools and
# builds are made first, so a skip means a scenario silently did not run (pytest -rs lists why).
#
# Usage: ./scripts/ci-e2e-wine.sh [pytest args...]   (added to every module's run)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "${ROOT}"
FL_E2E_PROG="ci-e2e-wine.sh"
# shellcheck source=scripts/lib-e2e-docker.sh
. "${ROOT}/scripts/lib-e2e-docker.sh"

IMAGE="fork-linux-ci-e2e-wine:26.04"
LOG_DIR="${ROOT}/logs/e2e-wine"
CONTAINER="fl-e2e-ci-wine-$$"
BUILDS_IN="/bridge-builds"

command -v docker >/dev/null 2>&1 || fl_e2e_die "docker not found"
TMP_BASE="$(fl_e2e_check_dir "${TMPDIR:-/tmp}" "TMPDIR")" || exit $?
SCRATCH=""
BUILDS=""
# On every exit: remove the container if it still runs, then the scratch roots (marker-checked).
cleanup() {
	docker rm -f "${CONTAINER}" >/dev/null 2>&1 9>&- || true
	if [[ -n "${SCRATCH}" ]]; then
		fl_e2e_wipe "${SCRATCH}"
	fi
	if [[ -n "${BUILDS}" ]]; then
		fl_e2e_wipe "${BUILDS}"
	fi
}
trap cleanup EXIT

# new_scratch: a fresh, marked scratch root under TMPDIR (refused inside a home directory).
new_scratch() {
	local dir
	dir="$(mktemp -d "${TMP_BASE}/fl-e2e-wine.XXXXXX")"
	dir="$(fl_e2e_check_dir "${dir}" "scratch root")" || exit $?
	fl_e2e_prepare_scratch "${dir}" 0 >&2
	printf '%s\n' "${dir}"
}

MODULES=(test_real_fork.py)
if [[ "${FL_E2E_FORK_UPDATES:-0}" == "1" ]]; then
	MODULES+=(test_fork_update.py)
fi
if [[ "${FL_E2E_APP_UPGRADE:-0}" == "1" ]]; then
	MODULES+=(test_wine_app_upgrade.py)
fi

fl_e2e_build_image "${ROOT}" "${IMAGE}"
if [[ "${FL_E2E_FORK_UPDATES:-0}" == "1" && ! -x "${ROOT}/build-bridge/bridge/fl-bridge-helper" ]]; then
	"${ROOT}/scripts/build-bridge.sh"
fi
if [[ "${FL_E2E_APP_UPGRADE:-0}" == "1" ]]; then
	BUILDS="$(new_scratch)"
	for n in 1 2; do
		"${ROOT}/scripts/build-version-variant.sh" --tree-only "0.9.${n}" "${BUILDS}/variants"
		"${BUILDS}/variants/fork-linux-0.9.${n}/scripts/build-bridge.sh" --out "${BUILDS}/${n}"
	done
	rm -rf -- "${BUILDS:?}/variants"
fi
# Host-wide slots shared with scripts/e2e-docker.sh (FL_E2E_SLOTS, default 3); one slot for the
# modules, which run one after the other.
BASE="$(fl_e2e_base)"
LOCK_DIR="$(fl_e2e_check_dir "${FL_E2E_LOCK_DIR:-${BASE}/locks}" "lock directory")" || exit $?
if [[ "${LOCK_DIR}" == "${BASE}/locks" ]]; then
	fl_e2e_own_dir "${BASE}" "E2E base directory"
fi
fl_e2e_own_dir "${LOCK_DIR}" "lock directory"
fl_e2e_acquire_slot "${LOCK_DIR}" "${FL_E2E_SLOTS:-3}"

USER_NAME="$(fl_e2e_user)"

# run_module MODULE [pytest args...]: one module in a fresh scratch root, then its logs and the
# root's deletion. Returns pytest's status.
run_module() {
	local module="$1" logs status=0
	shift
	logs="${LOG_DIR}/${module%.py}"
	SCRATCH="$(new_scratch)"
	mkdir -p -- "${SCRATCH}/root" "${SCRATCH}/home" "${logs}"
	local -a mounts=(-v "${ROOT}:/src:ro" -v "${SCRATCH}:/e2e:rw")
	local builds_env=""
	if [[ "${module}" == test_wine_app_upgrade.py ]]; then
		mounts+=(-v "${BUILDS}:${BUILDS_IN}:ro")
		builds_env="${BUILDS_IN}"
	fi
	echo "==> e2e-wine: tests/e2e/${module} (scratch root ${SCRATCH})"
	docker run --rm --init \
		--name "${CONTAINER}" \
		--label fl-e2e=ci-e2e-wine \
		--user "$(id -u):$(id -g)" \
		-e HOME=/e2e/home \
		-e USER="${USER_NAME}" \
		-e LOGNAME="${USER_NAME}" \
		-e FL_E2E_FORK=1 \
		-e FL_E2E_ROOT=/e2e/root \
		-e FL_E2E_FORK_VERSION \
		-e FL_E2E_FORK_UPDATES \
		-e FL_E2E_APP_UPGRADE \
		-e FL_E2E_BRIDGE_BUILDS="${builds_env}" \
		-e PYTHONDONTWRITEBYTECODE=1 \
		"${mounts[@]}" \
		-w /src \
		"${IMAGE}" \
		python3 -m pytest -v -rs -p no:cacheprovider --basetemp=/e2e/pytest "tests/e2e/${module}" "$@" \
		9>&- 2>&1 | tee "${logs}/pytest.log" 9>&- || status=$?
	if ((status == 0)) && grep -q '^SKIPPED ' "${logs}/pytest.log"; then
		echo "==> e2e-wine: tests/e2e/${module} skipped tests; every scenario of a requested tier must run:" >&2
		grep '^SKIPPED ' "${logs}/pytest.log" >&2
		status=1
	fi
	# Logs only: text files from the scratch root, never images, executables or archives.
	fl_e2e_copy_logs "${SCRATCH}" "${logs}"
	fl_e2e_wipe "${SCRATCH}"
	SCRATCH=""
	return "${status}"
}

rm -rf -- "${LOG_DIR:?}"
mkdir -p -- "${LOG_DIR}"
status=0
for module in "${MODULES[@]}"; do
	module_status=0
	run_module "${module}" "$@" || module_status=$?
	echo "==> e2e-wine: tests/e2e/${module} exit ${module_status}"
	if ((status == 0)); then
		status="${module_status}"
	fi
done
cleanup
echo "==> e2e-wine logs: ${LOG_DIR}"
exit "${status}"
