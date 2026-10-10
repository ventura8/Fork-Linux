#!/usr/bin/env bash
# ci-e2e-de.sh - the desktop E2E tier of Fork for Linux (unofficial): fork-linux, installed from
# the release tarball with install.sh, inside a real desktop session (its settings daemon, window
# manager, file manager and terminal) in docker/Dockerfile.e2e.de.<DESKTOP>; tests/e2e_de.
#
# Usage: scripts/ci-e2e-de.sh [--name NAME] [--keep] DESKTOP [--fork] [-- PYTEST_ARGS...]
#
#   DESKTOP        a desktop with a docker/Dockerfile.e2e.de.<DESKTOP> (xfce, ...)
#   (default)      the integration tier, no Wine: tests/e2e_de/test_de_integration.py
#   --fork         the real-Fork tier: setup with the managed Wine, .NET and the official Fork
#                  installer, then Fork under the desktop (tests/e2e_de/test_de_fork.py); holds one
#                  of the host-wide E2E slots shared with scripts/e2e-docker.sh
#   --name NAME    run name (default de-<DESKTOP>, de-<DESKTOP>-fork with --fork): scratch root
#                  /var/tmp/fork-linux-e2e/<NAME> (or FL_E2E_SCRATCH), container fl-e2e-<NAME>
#   --keep         reuse the scratch root of the previous run with this NAME instead of wiping it
#
# Like scripts/e2e-docker.sh (AGENTS.md hard rule 18) the container sees only the worktree
# (read-only, at /src), the git common dir (read-only), the scratch root (/e2e) and, for --fork,
# the read-only seed FL_E2E_SEED_DIR or /var/tmp/fork-linux-e2e/seed (absent in CI: everything is
# downloaded fresh). It runs as your uid:gid with --init --rm, never --privileged, no host X
# socket; a uid the image does not know gets a read-only /etc/passwd (the image's plus one line)
# from the scratch root, because dbus-daemon needs one. The release tarball is built from the read-only tree in docker/Dockerfile.release into
# the scratch root (FL_E2E_DE_TARBALL=FILE installs that tarball instead).
#
# Logs: logs/e2e-de/<NAME>.log (the pytest tee) and logs/e2e-de/<NAME>/ (text logs copied from the
# scratch root: *.log *.json *.txt, never Wine's drive_c, never a download cache, never images).
# Screenshots stay in <scratch root>/root/shots: they may show Fork's logo.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
FL_E2E_PROG="ci-e2e-de.sh"
# shellcheck source=scripts/lib-e2e-docker.sh
. "${ROOT}/scripts/lib-e2e-docker.sh"

RELEASE_DOCKERFILE="docker/Dockerfile.release"
RESERVED_NAMES=(seed locks)

usage() {
	sed -n '6p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

usage_error() {
	echo "${FL_E2E_PROG}: $*" >&2
	usage >&2
	exit 2
}

NAME=""
KEEP=0
FORK=0
DESKTOP=""
while (($# > 0)); do
	case "$1" in
		--name)
			(($# >= 2)) || usage_error "--name needs a value"
			NAME="$2"
			shift 2
			;;
		--keep)
			KEEP=1
			shift
			;;
		--fork)
			FORK=1
			shift
			;;
		-h | --help)
			usage
			exit 0
			;;
		--)
			shift
			break
			;;
		-*)
			usage_error "unknown option: $1"
			;;
		*)
			[[ -z "${DESKTOP}" ]] || usage_error "one desktop at a time (got '${DESKTOP}' and '$1')"
			DESKTOP="$1"
			shift
			;;
	esac
done
[[ -n "${DESKTOP}" ]] || usage_error "missing DESKTOP"
[[ "${DESKTOP}" =~ ^[a-z][a-z0-9]{0,31}$ ]] || usage_error "invalid desktop '${DESKTOP}'"
DOCKERFILE="docker/Dockerfile.e2e.de.${DESKTOP}"
[[ -f "${ROOT}/${DOCKERFILE}" ]] || usage_error "no ${DOCKERFILE} for desktop '${DESKTOP}'"
if [[ -z "${NAME}" ]]; then
	NAME="de-${DESKTOP}"
	((FORK == 0)) || NAME="${NAME}-fork"
fi
[[ "${NAME}" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$ ]] || usage_error "invalid --name '${NAME}' (letters, digits, . _ -)"
for reserved in "${RESERVED_NAMES[@]}"; do
	[[ "${NAME}" != "${reserved}" ]] || usage_error "--name ${NAME} is reserved"
done

command -v docker >/dev/null 2>&1 || fl_e2e_die "docker not found"
command -v flock >/dev/null 2>&1 || fl_e2e_die "flock not found (util-linux)"

# --- paths ---------------------------------------------------------------------------------------
SRC_DIR="$(fl_e2e_check_ro_dir "${ROOT}" "worktree")" || exit $?
GIT_COMMON="$(git -C "${ROOT}" rev-parse --path-format=absolute --git-common-dir 2>/dev/null || true)"
if [[ -n "${GIT_COMMON}" ]]; then
	GIT_COMMON="$(fl_e2e_check_ro_dir "${GIT_COMMON}" "git common dir")" || exit $?
	[[ "${GIT_COMMON}" != "${SRC_DIR}/"* ]] || GIT_COMMON=""
fi
BASE="$(fl_e2e_base)"
SCRATCH="$(fl_e2e_check_dir "${FL_E2E_SCRATCH:-${BASE}/${NAME}}" "scratch root")" || exit $?
LOCK_DIR="$(fl_e2e_check_dir "${FL_E2E_LOCK_DIR:-${BASE}/locks}" "lock directory")" || exit $?
SEED=""
if ((FORK)); then
	SEED="${FL_E2E_SEED_DIR:-${BASE}/seed}"
	if [[ -e "${SEED}" ]]; then
		SEED="$(fl_e2e_check_dir "${SEED}" "seed directory")" || exit $?
		[[ -d "${SEED}" ]] || fl_e2e_refuse "seed '${SEED}' is not a directory"
	else
		echo "==> no seed at ${SEED}: the container downloads Wine, winetricks and Fork itself"
		SEED=""
	fi
fi
TARBALL_SOURCE=""
if [[ -n "${FL_E2E_DE_TARBALL:-}" ]]; then
	TARBALL_SOURCE="$(realpath -e -- "${FL_E2E_DE_TARBALL}" 2>/dev/null)" ||
		fl_e2e_die "FL_E2E_DE_TARBALL '${FL_E2E_DE_TARBALL}' does not exist"
	[[ -f "${TARBALL_SOURCE}" && "${TARBALL_SOURCE}" == *.tar.gz ]] ||
		fl_e2e_die "FL_E2E_DE_TARBALL '${TARBALL_SOURCE}' is not a .tar.gz file"
fi
LOG_DIR="${SRC_DIR}/logs/e2e-de"

# --- one run per NAME (fd 8), then the scratch root -----------------------------------------------
if [[ "${LOCK_DIR}" == "${BASE}/locks" || "${SCRATCH}" == "${BASE}/${NAME}" ]]; then
	fl_e2e_own_dir "${BASE}" "E2E base directory"
fi
fl_e2e_own_dir "${LOCK_DIR}" "lock directory"
[[ ! -L "${LOCK_DIR}/name-${NAME}" ]] || fl_e2e_refuse "lock file ${LOCK_DIR}/name-${NAME} is a symlink"
exec 8>>"${LOCK_DIR}/name-${NAME}"
flock -n 8 || fl_e2e_refuse "another E2E run uses --name ${NAME}; pick another name"
CONTAINER="fl-e2e-${NAME}"
BUILDER="fl-e2e-${NAME}-tarball"
cleanup() {
	docker rm -f "${CONTAINER}" "${BUILDER}" >/dev/null 2>&1 8>&- 9>&- || true
}
trap cleanup EXIT
cleanup

fl_e2e_prepare_scratch "${SCRATCH}" "${KEEP}"
mkdir -p -- "${SCRATCH}/home" "${SCRATCH}/root" "${SCRATCH}/dist" "${LOG_DIR}"
if [[ -d "${LOG_DIR}/${NAME}" && "${KEEP}" != "1" ]]; then
	rm -rf -- "${LOG_DIR:?}/${NAME:?}"
fi
mkdir -p -- "${LOG_DIR}/${NAME}"

# --- images and the release tarball ----------------------------------------------------------------
IMAGE="$(fl_e2e_image_tag "${SRC_DIR}" "${DOCKERFILE}")"
fl_e2e_build_image "${SRC_DIR}" "${IMAGE}" "${DOCKERFILE}"
USER_NAME="$(fl_e2e_user)"
mounts=(--volume "${SRC_DIR}:/src:ro" --volume "${SCRATCH}:/e2e:rw")
if [[ -n "${GIT_COMMON}" ]]; then
	mounts+=(--volume "${GIT_COMMON}:${GIT_COMMON}:ro")
fi
find -P "${SCRATCH:?}/dist" -mindepth 1 -maxdepth 1 -name 'fork-linux-*.tar.gz' -type f -delete
if [[ -n "${TARBALL_SOURCE}" ]]; then
	cp -- "${TARBALL_SOURCE}" "${SCRATCH}/dist/"
else
	RELEASE_IMAGE="$(fl_e2e_image_tag "${SRC_DIR}" "${RELEASE_DOCKERFILE}")"
	fl_e2e_build_image "${SRC_DIR}" "${RELEASE_IMAGE}" "${RELEASE_DOCKERFILE}"
	echo "==> release tarball from the read-only tree (${RELEASE_IMAGE})"
	docker run --rm --init --name "${BUILDER}" --label "fl-e2e=${NAME}" --user "$(id -u):$(id -g)" \
		"${mounts[@]}" --workdir /src --env HOME=/e2e/home --env FL_ARTIFACTS_DIR=/e2e/dist \
		"${RELEASE_IMAGE}" ./scripts/release-portable.sh tarball 8>&- 9>&- >"${LOG_DIR}/${NAME}/tarball.log" 2>&1 ||
		fl_e2e_die "the release tarball did not build (${LOG_DIR}/${NAME}/tarball.log)"
fi
TARBALLS=("${SCRATCH}"/dist/fork-linux-*.tar.gz)
((${#TARBALLS[@]} == 1)) && [[ -f "${TARBALLS[0]}" ]] || fl_e2e_die "expected one release tarball in ${SCRATCH}/dist"
TARBALL="/e2e/dist/$(basename -- "${TARBALLS[0]}")"

# --- a passwd entry for this uid ---------------------------------------------------------------------
# dbus-daemon refuses a uid without one ("Could not get password database information"); CI
# runners (uid 1001) have none in the image. The image's own /etc/passwd plus one line for this
# uid is mounted read-only from the scratch root.
PASSWD_ARGS=()
uid="$(id -u)"
docker run --rm --name "${BUILDER}" --label "fl-e2e=${NAME}" --user "${uid}:$(id -g)" "${IMAGE}" \
	cat /etc/passwd 8>&- 9>&- >"${SCRATCH}/passwd" || fl_e2e_die "cannot read /etc/passwd of ${IMAGE}"
if ! cut -d: -f3 "${SCRATCH}/passwd" | grep -qx -- "${uid}"; then
	printf '%s:x:%s:%s:fork-linux E2E:/e2e/home:/bin/bash\n' "${USER_NAME}" "${uid}" "$(id -g)" >>"${SCRATCH}/passwd"
	PASSWD_ARGS=(--volume "${SCRATCH}/passwd:/etc/passwd:ro")
fi

# --- the container ---------------------------------------------------------------------------------
MODULE="tests/e2e_de/test_de_integration.py"
args=(run --rm --init --name "${CONTAINER}" --label "fl-e2e=${NAME}" --user "$(id -u):$(id -g)" --workdir /src
	"${mounts[@]}" "${PASSWD_ARGS[@]}"
	--env HOME=/e2e/home --env "USER=${USER_NAME}" --env "LOGNAME=${USER_NAME}" --env PYTHONDONTWRITEBYTECODE=1
	--env "FL_E2E_DE=${DESKTOP}" --env FL_E2E_ROOT=/e2e/root --env "FL_E2E_DE_TARBALL=${TARBALL}")
if ((FORK)); then
	MODULE="tests/e2e_de/test_de_fork.py"
	args+=(--env FL_E2E_DE_FORK=1)
	if [[ -n "${FL_E2E_FORK_VERSION:-}" ]]; then
		args+=(--env "FL_E2E_FORK_VERSION=${FL_E2E_FORK_VERSION}")
	fi
	if [[ -n "${SEED}" ]]; then
		args+=(--volume "${SEED}:/seed:ro" --env FL_E2E_SEED=/seed)
		if [[ -d "${SEED}/winetricks" ]]; then
			args+=(--env FL_E2E_WINETRICKS_CACHE=/seed/winetricks)
		fi
	fi
	fl_e2e_acquire_slot "${LOCK_DIR}" "${FL_E2E_SLOTS:-3}"
fi

TIER="integration"
((FORK == 0)) || TIER="real-Fork"
echo "==> ci-e2e-de: ${DESKTOP} ${TIER} tier in ${IMAGE} [name ${NAME}]"
echo "==> scratch root: ${SCRATCH} (screenshots: ${SCRATCH}/root/shots)"
status=0
docker "${args[@]}" "${IMAGE}" python3 -m pytest -p no:cacheprovider -o addopts= -v --basetemp=/e2e/pytest \
	"${MODULE}" "$@" 8>&- 9>&- 2>&1 | tee "${LOG_DIR}/${NAME}.log" 8>&- 9>&- || status=$?

fl_e2e_copy_logs "${SCRATCH}" "${LOG_DIR}/${NAME}"
echo "==> logs: ${LOG_DIR}/${NAME}.log and ${LOG_DIR}/${NAME}/"
echo "==> scratch root: ${SCRATCH} (screenshots: ${SCRATCH}/root/shots; --keep reuses it)"
exit "${status}"
