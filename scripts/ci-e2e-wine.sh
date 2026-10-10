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
# The scratch root is a fresh directory under ${TMPDIR:-/tmp}, refused when it is or lies
# inside a home directory (AGENTS.md hard rule 18); the container sees only the read-only
# source tree and that root. It shares scripts/lib-e2e-docker.sh (scratch checks, host-wide
# slots, image build, logs-only copy) with scripts/e2e-docker.sh, the local runner.
#
# Usage: ./scripts/ci-e2e-wine.sh [pytest args...]
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "${ROOT}"
FL_E2E_PROG="ci-e2e-wine.sh"
# shellcheck source=scripts/lib-e2e-docker.sh
. "${ROOT}/scripts/lib-e2e-docker.sh"

IMAGE="fork-linux-ci-e2e-wine:26.04"
LOG_DIR="${ROOT}/logs/e2e-wine"
CONTAINER="fl-e2e-ci-wine-$$"

command -v docker >/dev/null 2>&1 || fl_e2e_die "docker not found"
TMP_BASE="$(fl_e2e_check_dir "${TMPDIR:-/tmp}" "TMPDIR")" || exit $?
SCRATCH="$(mktemp -d "${TMP_BASE}/fl-e2e-wine.XXXXXX")"
SCRATCH="$(fl_e2e_check_dir "${SCRATCH}" "scratch root")" || exit $?
fl_e2e_prepare_scratch "${SCRATCH}" 0
# On every exit: remove the container if it still runs, then the scratch root (marker-checked).
trap 'docker rm -f "${CONTAINER}" >/dev/null 2>&1 9>&- || true; fl_e2e_wipe "${SCRATCH}"' EXIT
mkdir -p -- "${SCRATCH}/root" "${SCRATCH}/home" "${LOG_DIR}"

fl_e2e_build_image "${ROOT}" "${IMAGE}"
# Host-wide slots shared with scripts/e2e-docker.sh (FL_E2E_SLOTS, default 3).
BASE="$(fl_e2e_base)"
LOCK_DIR="$(fl_e2e_check_dir "${FL_E2E_LOCK_DIR:-${BASE}/locks}" "lock directory")" || exit $?
if [[ "${LOCK_DIR}" == "${BASE}/locks" ]]; then
	fl_e2e_own_dir "${BASE}" "E2E base directory"
fi
fl_e2e_own_dir "${LOCK_DIR}" "lock directory"
fl_e2e_acquire_slot "${LOCK_DIR}" "${FL_E2E_SLOTS:-3}"

status=0
docker run --rm --init \
	--name "${CONTAINER}" \
	--label fl-e2e=ci-e2e-wine \
	--user "$(id -u):$(id -g)" \
	-e HOME=/e2e/home \
	-e USER="$(id -un)" \
	-e LOGNAME="$(id -un)" \
	-e FL_E2E_FORK=1 \
	-e FL_E2E_ROOT=/e2e/root \
	-e FL_E2E_FORK_VERSION \
	-e PYTHONDONTWRITEBYTECODE=1 \
	-v "${ROOT}:/src:ro" \
	-v "${SCRATCH}:/e2e:rw" \
	-w /src \
	"${IMAGE}" \
	python3 -m pytest -v -p no:cacheprovider --basetemp=/e2e/pytest tests/e2e "$@" \
	9>&- 2>&1 | tee "${LOG_DIR}/pytest.log" 9>&- || status=$?

# Logs only: text files from the scratch root, never images, executables or archives.
fl_e2e_copy_logs "${SCRATCH}" "${LOG_DIR}"
echo "==> e2e-wine logs: ${LOG_DIR}"
exit "${status}"
