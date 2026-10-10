#!/usr/bin/env bash
# e2e-docker.sh - the ONLY way to run real-Fork / Wine work locally (AGENTS.md hard rule 18):
# the FL_E2E_FORK=1 tier, xdotool exploration, strace, a winetricks experiment... always
# inside a throwaway container of fork-linux-ci-e2e-wine:26.04 (docker/Dockerfile.e2e.wine).
#
# Usage: scripts/e2e-docker.sh [--name NAME] [--image IMG] [--ptrace] [--keep] pytest [ARGS...]
#        scripts/e2e-docker.sh [--name NAME] [--image IMG] [--ptrace] [--keep] shell -- CMD...
#
#   pytest [ARGS]  python3 -m pytest -p no:cacheprovider -o addopts= -v ARGS (default tests/e2e)
#                  with FL_E2E_FORK=1 FL_E2E_ROOT=/e2e/root; tee'd to
#                  logs/e2e-docker/<NAME>/pytest.log
#   shell -- CMD   CMD with the same environment and an Xvfb on DISPLAY=:99 (for xdotool /
#                  import exploration); screenshots stay in the scratch root
#   --name NAME    run name: scratch root, log directory, container fl-e2e-<NAME>
#                  (default: the worktree's directory name); one run per NAME at a time
#   --image IMG    another image (must exist); default fork-linux-ci-e2e-wine:26.04, built
#                  on demand from docker/Dockerfile.e2e.wine
#   --ptrace       add CAP_SYS_PTRACE (strace / gdb inside the container)
#   --keep         reuse the scratch root of the previous run with this NAME (prefix, Fork
#                  install, download cache) instead of wiping it first
#
# What the container sees (nothing else from the host):
#   /src           this worktree, read-only
#   <git common>   the repository's git common dir, read-only at its own path (linked worktrees)
#   /e2e           the scratch root FL_E2E_SCRATCH or /var/tmp/fork-linux-e2e/<NAME> (rw, 0700);
#                  HOME=/e2e/home, FL_E2E_ROOT=/e2e/root, screenshots in /e2e/root/shots
#   /seed          FL_E2E_SEED_DIR or /var/tmp/fork-linux-e2e/seed (read-only, when present):
#                  wine-*.tar.xz, Fork-*.exe, winetricks-*, selawik-*.zip, winetricks/ cache;
#                  FL_E2E_SEED=/seed FL_E2E_WINETRICKS_CACHE=/seed/winetricks (our code
#                  re-verifies every file by size and sha256)
# Runs as your uid:gid with --init --rm, a label fl-e2e=<NAME>, network on, no host X socket,
# never --privileged. The scratch root is refused (exit 2) when it is '/', $HOME or the passwd
# home, lies inside or contains one of them, or has a symlink component; a non-empty scratch
# root without our marker is never wiped.
#
# Slots: at most FL_E2E_SLOTS (default 3) of these containers run host-wide (flock on
# /var/tmp/fork-linux-e2e/locks/slot-<n>; every free slot is tried first, then the run waits).
# Every FL_E2E_* variable you set is passed through (except the runner's own: SCRATCH,
# SEED_DIR, SLOTS, LOCK_DIR, and ROOT / SEED / WINETRICKS_CACHE, which point inside).
# Afterwards text logs (*.log *.json *.txt < 20 MB, not under drive_c) are copied from the
# scratch root to logs/e2e-docker/<NAME>/; the scratch path is printed for the screenshots.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
FL_E2E_PROG="e2e-docker.sh"
# shellcheck source=scripts/lib-e2e-docker.sh
. "${ROOT}/scripts/lib-e2e-docker.sh"

# FL_E2E_* variables that steer this script and are not forwarded into the container as-is.
RUNNER_VARS=(FL_E2E_SCRATCH FL_E2E_SEED_DIR FL_E2E_SLOTS FL_E2E_LOCK_DIR FL_E2E_ROOT FL_E2E_SEED
	FL_E2E_WINETRICKS_CACHE FL_E2E_FORK)
RESERVED_NAMES=(seed locks)
SHELL_DISPLAY=":99"

usage() {
	sed -n '6,7p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

usage_error() {
	echo "${FL_E2E_PROG}: $*" >&2
	usage >&2
	exit 2
}

NAME="$(basename -- "${ROOT}")"
DEFAULT_IMAGE="$(fl_e2e_default_image)"
IMAGE="${DEFAULT_IMAGE}"
PTRACE=0
KEEP=0
MODE=""
while (($# > 0)); do
	case "$1" in
		--name)
			(($# >= 2)) || usage_error "--name needs a value"
			NAME="$2"
			shift 2
			;;
		--image)
			(($# >= 2)) || usage_error "--image needs a value"
			IMAGE="$2"
			shift 2
			;;
		--ptrace)
			PTRACE=1
			shift
			;;
		--keep)
			KEEP=1
			shift
			;;
		-h | --help)
			usage
			exit 0
			;;
		pytest)
			MODE=pytest
			shift
			break
			;;
		shell)
			MODE=shell
			shift
			[[ "${1:-}" == "--" ]] || usage_error "shell needs '-- CMD...'"
			shift
			(($# > 0)) || usage_error "shell needs a command after '--'"
			break
			;;
		*)
			usage_error "unknown argument: $1"
			;;
	esac
done
[[ -n "${MODE}" ]] || usage_error "missing mode: pytest or shell"
[[ "${NAME}" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$ ]] || usage_error "invalid --name '${NAME}' (letters, digits, . _ -)"
for reserved in "${RESERVED_NAMES[@]}"; do
	[[ "${NAME}" != "${reserved}" ]] || usage_error "--name ${NAME} is reserved"
done
[[ "${IMAGE}" =~ ^[A-Za-z0-9][A-Za-z0-9_./:-]*$ && "${IMAGE}" == *:* && "${IMAGE}" != *:latest ]] ||
	usage_error "--image needs an explicit, pinned tag (never latest): '${IMAGE}'"

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
LOCK_DIR="${FL_E2E_LOCK_DIR:-${BASE}/locks}"
if [[ -n "${FL_E2E_SCRATCH:-}" ]]; then
	SCRATCH="${FL_E2E_SCRATCH}"
else
	SCRATCH="${BASE}/${NAME}"
fi
SCRATCH="$(fl_e2e_check_dir "${SCRATCH}" "scratch root")" || exit $?
LOCK_DIR="$(fl_e2e_check_dir "${LOCK_DIR}" "lock directory")" || exit $?
SEED="${FL_E2E_SEED_DIR:-${BASE}/seed}"
if [[ -e "${SEED}" ]]; then
	SEED="$(fl_e2e_check_dir "${SEED}" "seed directory")" || exit $?
	[[ -d "${SEED}" ]] || fl_e2e_refuse "seed '${SEED}' is not a directory"
else
	echo "==> no seed at ${SEED}: the container downloads Wine, winetricks and Fork itself"
	SEED=""
fi
LOG_DIR="${SRC_DIR}/logs/e2e-docker/${NAME}"

# --- locks: one run per NAME (fd 8), FL_E2E_SLOTS runs host-wide (fd 9) -------------------------
if [[ "${LOCK_DIR}" == "${BASE}/locks" || "${SCRATCH}" == "${BASE}/${NAME}" ]]; then
	fl_e2e_own_dir "${BASE}" "E2E base directory"
fi
fl_e2e_own_dir "${LOCK_DIR}" "lock directory"
[[ ! -L "${LOCK_DIR}/name-${NAME}" ]] || fl_e2e_refuse "lock file ${LOCK_DIR}/name-${NAME} is a symlink"
exec 8>>"${LOCK_DIR}/name-${NAME}"
flock -n 8 || fl_e2e_refuse "another e2e-docker.sh run uses --name ${NAME}; pick another name"
CONTAINER="fl-e2e-${NAME}"
cleanup() {
	docker rm -f "${CONTAINER}" >/dev/null 2>&1 8>&- 9>&- || true
}
trap cleanup EXIT
# A container with this name can only be left over from a killed run (we hold the name lock):
# remove it before its scratch root is touched.
cleanup

fl_e2e_prepare_scratch "${SCRATCH}" "${KEEP}"
mkdir -p -- "${SCRATCH}/home" "${SCRATCH}/root"
if [[ -d "${LOG_DIR}" && "${KEEP}" != "1" ]]; then
	rm -rf -- "${LOG_DIR:?}"
fi
mkdir -p -- "${LOG_DIR}"

if [[ "${IMAGE}" == "${DEFAULT_IMAGE}" ]]; then
	fl_e2e_build_image "${SRC_DIR}" "${IMAGE}"
elif ! docker image inspect "${IMAGE}" >/dev/null 2>&1 8>&- 9>&-; then
	fl_e2e_die "image ${IMAGE} not found (build it first, e.g. FL_CI_STAGE=bridge ./scripts/ci-docker.sh --build-only)"
fi

fl_e2e_acquire_slot "${LOCK_DIR}" "${FL_E2E_SLOTS:-3}"

# --- the container ---------------------------------------------------------------------------------
args=(run --rm --init --name "${CONTAINER}" --label "fl-e2e=${NAME}"
	--user "$(id -u):$(id -g)" --workdir /src
	--volume "${SRC_DIR}:/src:ro" --volume "${SCRATCH}:/e2e:rw")
if [[ -n "${GIT_COMMON}" ]]; then
	args+=(--volume "${GIT_COMMON}:${GIT_COMMON}:ro")
fi
if [[ -n "${SEED}" ]]; then
	args+=(--volume "${SEED}:/seed:ro")
fi
if ((PTRACE)); then
	args+=(--cap-add SYS_PTRACE)
fi
args+=(--env HOME=/e2e/home --env "USER=$(id -un)" --env "LOGNAME=$(id -un)"
	--env PYTHONDONTWRITEBYTECODE=1 --env FL_E2E_FORK=1 --env FL_E2E_ROOT=/e2e/root)
if [[ -n "${SEED}" ]]; then
	args+=(--env FL_E2E_SEED=/seed)
	if [[ -d "${SEED}/winetricks" ]]; then
		args+=(--env FL_E2E_WINETRICKS_CACHE=/seed/winetricks)
	fi
fi
while IFS= read -r var; do
	[[ "${var}" =~ ^FL_E2E_[A-Z0-9_]+$ ]] || continue
	skip=0
	for runner_var in "${RUNNER_VARS[@]}"; do
		[[ "${var}" != "${runner_var}" ]] || skip=1
	done
	((skip)) || args+=(--env "${var}=${!var}")
done < <(compgen -e | LC_ALL=C sort)

echo "==> e2e-docker: ${MODE} in ${IMAGE} [name ${NAME}]"
echo "==> scratch root: ${SCRATCH} (screenshots: ${SCRATCH}/root/shots)"
status=0
if [[ "${MODE}" == "pytest" ]]; then
	(($# > 0)) || set -- tests/e2e
	docker "${args[@]}" "${IMAGE}" python3 -m pytest -p no:cacheprovider -o addopts= -v "$@" \
		8>&- 9>&- 2>&1 | tee "${LOG_DIR}/pytest.log" 8>&- 9>&- || status=$?
else
	tty_args=()
	if [[ -t 0 ]]; then
		tty_args+=(--interactive)
		if [[ -t 1 ]]; then
			tty_args+=(--tty --env "TERM=${TERM:-xterm}")
		fi
	fi
	# Xvfb inside (never the host's X socket): DISPLAY=:99 for xdotool / import.
	docker "${args[@]}" "${tty_args[@]}" --env "DISPLAY=${SHELL_DISPLAY}" "${IMAGE}" bash -c '
if command -v Xvfb >/dev/null 2>&1; then
	Xvfb "$DISPLAY" -screen 0 1600x1000x24 -nolisten tcp >/e2e/xvfb.log 2>&1 &
	for _ in $(seq 1 50); do
		xdpyinfo -display "$DISPLAY" >/dev/null 2>&1 && break
		sleep 0.1
	done
fi
exec "$@"' fl-e2e-shell "$@" 8>&- 9>&- || status=$?
fi

fl_e2e_copy_logs "${SCRATCH}" "${LOG_DIR}"
echo "==> logs: ${LOG_DIR}"
echo "==> scratch root: ${SCRATCH} (screenshots: ${SCRATCH}/root/shots; --keep reuses it)"
exit "${status}"
