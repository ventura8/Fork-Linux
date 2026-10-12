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
# What runs:
#   With test paths (tests/e2e/test_*.py: one CI shard of e2e-wine.yml), each module named runs
#   in a pytest session, a container and a fresh scratch root of its own (the root deleted
#   afterwards), so one module never depends on what another left behind; logs go to
#   logs/e2e-wine/<module>/. Naming an update module switches its tier on.
#   Without test paths, the whole tier runs in one session and one root (the modules share the
#   main prefix in tests/e2e/conftest.py's MODULE_ORDER; upstream-watch.yml), logs in
#   logs/e2e-wine/tier/: every module except the two update tiers, which join only when asked:
#     test_fork_update.py      FL_E2E_FORK_UPDATES=1: Fork 2.23.1 updating itself and through
#                              fork-linux, in a prefix of its own
#     test_wine_app_upgrade.py FL_E2E_APP_UPGRADE=1: upgrades of fork-linux itself. Its A6 step
#                              replaces the bridge binaries of one version by another's while the
#                              daemon runs, so two bridge builds of different versions are made
#                              first (variant trees of this one, 0.9.1 and 0.9.2, by
#                              scripts/build-version-variant.sh --tree-only) into a directory of
#                              their own, mounted read-only (FL_E2E_BRIDGE_BUILDS=/bridge-builds)
#   Other arguments are extra pytest options for every session (-k, -x, ...).
# The bridge scenarios need the shims: scripts/build-bridge.sh (its own CI image) runs first
# when build-bridge/ has none.
# Every session runs even when an earlier one failed; the exit status is the first failure's.
# Velopack (Fork's own updater) now and then cannot start the updated Fork under Wine: "Failed to
# start application (OS Error 731)" (maintainer decision 2026-10-12, docs/qa/RELEASE-DECISIONS.md).
# A session that failed with it re-runs its failed modules once, each in a fresh root of its own
# (logs in logs/e2e-wine/<module>-rerun/), and says so on stdout and in logs/e2e-wine/reruns.log;
# the re-run's status counts. Any other failure is never re-run.
# A skipped test fails its session: every tier this script runs was asked for, and its tools and
# builds are made first, so a skip means a scenario silently did not run (pytest -rs lists why).
#
# Usage: ./scripts/ci-e2e-wine.sh [tests/e2e/test_*.py...] [pytest args...]
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
FORK_UPDATE="test_fork_update.py"
VELOPACK_731="Failed to start application (OS Error 731)"
APP_UPGRADE="test_wine_app_upgrade.py"

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
	return 0
}
trap cleanup EXIT

# new_scratch: a fresh, marked scratch root under TMPDIR (refused inside a home directory).
new_scratch() {
	local dir
	dir="$(mktemp -d "${TMP_BASE}/fl-e2e-wine.XXXXXX")"
	dir="$(fl_e2e_check_dir "${dir}" "scratch root")" || exit $?
	fl_e2e_prepare_scratch "${dir}" 0 >&2
	printf '%s\n' "${dir}"
	return 0
}

# The arguments: test paths (one session each) and pytest options (for every session).
SHARD=()
OPTIONS=()
for arg in "$@"; do
	if [[ "${arg}" == tests/* ]]; then
		SHARD+=("${arg}")
	else
		OPTIONS+=("${arg}")
	fi
done
if ((${#SHARD[@]} > 0)); then
	MODULES=("${SHARD[@]##*/}")
else
	MODULES=()
	for path in tests/e2e/test_*.py; do
		module="${path##*/}"
		if [[ "${module}" == "${FORK_UPDATE}" && "${FL_E2E_FORK_UPDATES:-0}" != "1" ]] ||
			[[ "${module}" == "${APP_UPGRADE}" && "${FL_E2E_APP_UPGRADE:-0}" != "1" ]]; then
			continue
		fi
		MODULES+=("${module}")
	done
fi
for module in "${MODULES[@]}"; do
	[[ -f "tests/e2e/${module}" ]] || fl_e2e_die "no such real-tier module: tests/e2e/${module}"
done

fl_e2e_build_image "${ROOT}" "${IMAGE}"
if [[ ! -x "${ROOT}/build-bridge/bridge/fl-bridge-helper" ]]; then
	"${ROOT}/scripts/build-bridge.sh"
fi
if [[ " ${MODULES[*]} " == *" ${APP_UPGRADE} "* ]]; then
	BUILDS="$(new_scratch)"
	for n in 1 2; do
		"${ROOT}/scripts/build-version-variant.sh" --tree-only "0.9.${n}" "${BUILDS}/variants"
		"${BUILDS}/variants/fork-linux-0.9.${n}/scripts/build-bridge.sh" --out "${BUILDS}/${n}"
	done
	rm -rf -- "${BUILDS:?}/variants"
fi
# Host-wide slots shared with scripts/e2e-docker.sh (FL_E2E_SLOTS, default 3); one slot for the
# sessions, which run one after the other.
BASE="$(fl_e2e_base)"
LOCK_DIR="$(fl_e2e_check_dir "${FL_E2E_LOCK_DIR:-${BASE}/locks}" "lock directory")" || exit $?
if [[ "${LOCK_DIR}" == "${BASE}/locks" ]]; then
	fl_e2e_own_dir "${BASE}" "E2E base directory"
fi
fl_e2e_own_dir "${LOCK_DIR}" "lock directory"
fl_e2e_acquire_slot "${LOCK_DIR}" "${FL_E2E_SLOTS:-3}"

USER_NAME="$(fl_e2e_user)"

# run_session NAME MODULE...: one pytest session over MODULE... in a fresh scratch root, then its
# logs (logs/e2e-wine/NAME/) and the root's deletion. Returns pytest's status (1 for a skip).
run_session() {
	local name="$1" logs status=0 updates=0 upgrade=0 builds_env="" module
	shift
	local -a targets=()
	for module in "$@"; do
		targets+=("tests/e2e/${module}")
		[[ "${module}" != "${FORK_UPDATE}" ]] || updates=1
		[[ "${module}" != "${APP_UPGRADE}" ]] || upgrade=1
	done
	logs="${LOG_DIR}/${name}"
	SCRATCH="$(new_scratch)"
	mkdir -p -- "${SCRATCH}/root" "${SCRATCH}/home" "${logs}"
	local -a mounts=(-v "${ROOT}:/src:ro" -v "${SCRATCH}:/e2e:rw")
	if ((upgrade == 1)); then
		mounts+=(-v "${BUILDS}:${BUILDS_IN}:ro")
		builds_env="${BUILDS_IN}"
	fi
	echo "==> e2e-wine: ${targets[*]} (scratch root ${SCRATCH})"
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
		-e FL_E2E_FORK_UPDATES="${updates}" \
		-e FL_E2E_APP_UPGRADE="${upgrade}" \
		-e FL_E2E_BRIDGE_BUILDS="${builds_env}" \
		-e PYTHONDONTWRITEBYTECODE=1 \
		"${mounts[@]}" \
		-w /src \
		"${IMAGE}" \
		python3 -m pytest -v -rs -p no:cacheprovider --basetemp=/e2e/pytest "${targets[@]}" "${OPTIONS[@]}" \
		9>&- 2>&1 | tee "${logs}/pytest.log" 9>&- || status=$?
	if ((status == 0)) && grep -q '^SKIPPED ' "${logs}/pytest.log"; then
		echo "==> e2e-wine: ${name} skipped tests; every scenario of a requested tier must run:" >&2
		grep '^SKIPPED ' "${logs}/pytest.log" >&2
		status=1
	fi
	# Logs only: text files from the scratch root, never images, executables or archives.
	fl_e2e_copy_logs "${SCRATCH}" "${logs}"
	fl_e2e_wipe "${SCRATCH}"
	SCRATCH=""
	return "${status}"
}

# failed_modules LOG: the tests/e2e modules with a FAILED or ERROR line in a pytest log, one per line.
failed_modules() {
	grep -oE '^(FAILED|ERROR) tests/e2e/test_[A-Za-z0-9_]+\.py' "$1" | sed 's|.*/||' | sort -u
	return 0
}

# rerun_after_velopack_731 NAME: when session NAME failed (session_status != 0) and its log has
# Velopack's start failure, re-run its failed modules once, each in a session of its own;
# session_status becomes the first failing re-run's status (0 when they all pass).
rerun_after_velopack_731() {
	local name="$1" log="${LOG_DIR}/$1/pytest.log" module rerun=0 rerun_status
	local -a modules=()
	if ((session_status == 0)) || ! grep -qF "${VELOPACK_731}" "${log}"; then
		return 0
	fi
	mapfile -t modules < <(failed_modules "${log}")
	((${#modules[@]} > 0)) || return 0
	for module in "${modules[@]}"; do
		echo "==> e2e-wine: ${name}: Fork's updater failed with '${VELOPACK_731}'; re-running tests/e2e/${module} once" |
			tee -a "${LOG_DIR}/reruns.log"
		rerun_status=0
		run_session "${module%.py}-rerun" "${module}" || rerun_status=$?
		echo "==> e2e-wine: re-run of tests/e2e/${module} exit ${rerun_status}" | tee -a "${LOG_DIR}/reruns.log"
		if ((rerun == 0)); then
			rerun="${rerun_status}"
		fi
	done
	session_status="${rerun}"
	return 0
}

# record WHAT STATUS: print a session's result; the first failure becomes the script's status.
status=0
record() {
	echo "==> e2e-wine: $1 exit $2"
	if ((status == 0)); then
		status="$2"
	fi
	return 0
}

rm -rf -- "${LOG_DIR:?}"
mkdir -p -- "${LOG_DIR}"
if ((${#SHARD[@]} > 0)); then
	for module in "${MODULES[@]}"; do
		session_status=0
		run_session "${module%.py}" "${module}" || session_status=$?
		rerun_after_velopack_731 "${module%.py}"
		record "tests/e2e/${module}" "${session_status}"
	done
else
	session_status=0
	run_session tier "${MODULES[@]}" || session_status=$?
	rerun_after_velopack_731 tier
	record "tests/e2e (${#MODULES[@]} modules)" "${session_status}"
fi
cleanup
echo "==> e2e-wine logs: ${LOG_DIR}"
exit "${status}"
