#!/usr/bin/env bash
# ci-e2e-wine.sh - the real end-to-end tier of Fork for Linux (unofficial): FL_E2E_FORK=1
# tests/e2e (setup with the managed Wine, .NET and the official Fork installer, Fork's window
# under Xvfb, fork <repo>, doctor, snapshot / rollback, purge) in docker/Dockerfile.e2e.wine,
# as the invoking unprivileged user with a scratch HOME (never ~/.wine, never root).
#
# Downloads happen on this machine at run time; nothing downloaded is cached in CI or uploaded,
# and no seed is used. Only logs are kept: logs/e2e-wine/ (screenshots show Fork's logo and stay
# in the scratch roots, which are deleted afterwards).
#
# Every scratch root lies in one fresh run directory under ${TMPDIR:-/tmp}, refused when it is or
# lies inside a home directory (AGENTS.md hard rule 18); a container sees only the read-only
# source tree and its own root. It shares scripts/lib-e2e-docker.sh (scratch checks, host-wide
# slots, image build, logs-only copy) with scripts/e2e-docker.sh, the local runner.
#
# What runs:
#   With test paths (tests/e2e/test_*.py: one CI shard of e2e-wine.yml) or --all (every module,
#   both update tiers included), each module runs in a pytest session, a container and a scratch
#   root of its own (deleted afterwards), so one module never depends on what another left
#   behind; logs go to logs/e2e-wine/<module>/. Naming an update module switches its tier on.
#   The modules run in parallel, --jobs N at a time (default: the host-wide slots, below),
#   longest first (scripts/e2e-plan.py, durations in tests/e2e/timings.json):
#     template  a module that only needs the main prefix set up starts from a copy of a template
#               root: one container sets it up once per run (tests/e2e/test_real_fork.py's
#               first test, logs in logs/e2e-wine/template/) and every such module reuses that
#               prefix (FL_E2E_REUSE=1) instead of setting up its own. FL_E2E_TEMPLATE=0 turns it
#               off; when the template fails, these modules set up their own prefix.
#     fresh     a module that sets up prefixes of its own (test_real_fork.py, the setup errors,
#               the update tiers) starts from the template's download caches only, or from
#               nothing when it starts before the template is ready.
#   A template lives in this run's directory and is deleted with it (CI never keeps one).
#   Locally FL_E2E_TEMPLATE_CACHE=DIR (outside any home) keeps it across runs in DIR/<key>, the
#   key being a hash of the image, the Fork version and the setup code (e2e-plan.py key); a
#   template of another key there is deleted.
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
#   --remote HOST [--remote-jobs M] (with --all or test paths) shares the modules with a second
#   machine reached by ssh, so that both end about together (e2e-plan.py split): this commit is
#   shipped as a git bundle into its clone (FL_E2E_REMOTE_DIR, default ~/fork-linux-ci; the
#   worktree must be clean), which runs its share with this script, M at a time (default --jobs),
#   under the same container-only rules and its own template; its logs land in
#   logs/e2e-wine/remote-HOST/ and its status counts like a module's.
#   Other arguments are extra pytest options for every module's session (-k, -x, ...).
# Slots: every container holds one of the host-wide slots shared with scripts/e2e-docker.sh
# (FL_E2E_SLOTS; default one per two cores and per 2 GiB of available memory).
# The bridge scenarios need the shims: scripts/build-bridge.sh (its own CI image) runs first
# when build-bridge/ lacks one of them or is older than a bridge source (a clone that was
# checked out again, e.g. the --remote one, must not test yesterday's fl-launch.exe).
# Every session runs even when an earlier one failed; the exit status is the first failure's
# (in the order the modules finish). Each finished module prints its status and time; a failed
# one the end of its log. logs/e2e-wine/results.tsv lists module, status, seconds, root and the
# seconds it waited for a slot (not counted in its time).
# Velopack (Fork's own updater) now and then cannot start the updated Fork under Wine: "Failed to
# start application (OS Error 731)" (maintainer decision 2026-10-12, docs/qa/RELEASE-DECISIONS.md).
# A session that failed with it re-runs its failed modules once, each in a fresh root of its own
# (logs in logs/e2e-wine/<module>-rerun/), and says so on stdout and in logs/e2e-wine/reruns.log;
# the re-run's status counts. Any other failure is never re-run.
# A skipped test fails its session: every tier this script runs was asked for, and its tools and
# builds are made first, so a skip means a scenario silently did not run (pytest -rs lists why).
#
# Usage: ./scripts/ci-e2e-wine.sh [--jobs N] [--remote HOST [--remote-jobs M]]
#                                [--all | tests/e2e/test_*.py...] [pytest args...]
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
# The template: the first test of test_real_fork.py, a fresh setup and its no-op re-run.
TEMPLATE_TEST=(tests/e2e/test_real_fork.py -k test_01_setup_completes_and_resumes)
TAIL_LINES=60

command -v docker >/dev/null 2>&1 || fl_e2e_die "docker not found"
((BASH_VERSINFO[0] > 5 || (BASH_VERSINFO[0] == 5 && BASH_VERSINFO[1] >= 1))) ||
	fl_e2e_die "bash 5.1 or newer is needed (wait -n -p)"
TMP_BASE="$(fl_e2e_check_dir "${TMPDIR:-/tmp}" "TMPDIR")" || exit $?
RUN=""
BUILDS=""
# On every exit: stop the module jobs and their containers, then delete the run directory (every
# scratch root and the template inside it) and the bridge builds (marker-checked).
cleanup() {
	local pid
	for pid in $(jobs -p); do
		kill "${pid}" 2>/dev/null || true
	done
	wait 2>/dev/null || true
	docker ps -aq --filter "name=^${CONTAINER}" 2>/dev/null 9>&- | xargs -r docker rm -f >/dev/null 2>&1 9>&- || true
	if [[ -n "${RUN}" ]]; then
		fl_e2e_wipe "${RUN}"
	fi
	if [[ -n "${BUILDS}" ]]; then
		fl_e2e_wipe "${BUILDS}"
	fi
	return 0
}
trap cleanup EXIT

# new_scratch [PARENT]: a fresh, marked scratch root in PARENT (default: TMPDIR), refused inside a home.
new_scratch() {
	local dir
	dir="$(mktemp -d "${1:-${TMP_BASE}}/fl-e2e-wine.XXXXXX")"
	dir="$(fl_e2e_check_dir "${dir}" "scratch root")" || exit $?
	fl_e2e_prepare_scratch "${dir}" 0 >&2
	printf '%s\n' "${dir}"
	return 0
}

# The arguments: --jobs N, --all, test paths (one session each) and pytest options (for every session).
SHARD=()
OPTIONS=()
ALL=0
JOBS=""
REMOTE=""
REMOTE_JOBS=""
while (($# > 0)); do
	case "$1" in
		--jobs | --remote | --remote-jobs)
			(($# > 1)) || fl_e2e_die "$1 needs a value"
			case "$1" in
				--jobs) JOBS="$2" ;;
				--remote) REMOTE="$2" ;;
				*) REMOTE_JOBS="$2" ;;
			esac
			shift
			;;
		--all) ALL=1 ;;
		tests/*) SHARD+=("$1") ;;
		*) OPTIONS+=("$1") ;;
	esac
	shift
done
if ((ALL == 1)); then
	((${#SHARD[@]} == 0)) || fl_e2e_die "--all runs every module; name no test paths with it"
	SHARD=(tests/e2e/test_*.py)
fi
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
SLOTS="$(fl_e2e_default_slots)"
JOBS="${JOBS:-${SLOTS}}"
[[ "${JOBS}" =~ ^[1-9][0-9]?$ ]] || fl_e2e_die "--jobs must be a number from 1 to 99 (got '${JOBS}')"
REMOTE_JOBS="${REMOTE_JOBS:-${JOBS}}"
REMOTE_MODULES=()
if [[ -n "${REMOTE}" ]]; then
	((${#SHARD[@]} > 0)) || fl_e2e_die "--remote needs --all or test paths"
	[[ "${REMOTE}" =~ ^[A-Za-z0-9][A-Za-z0-9_.@-]*$ ]] || fl_e2e_die "--remote must be an ssh host name (got '${REMOTE}')"
	[[ "${REMOTE_JOBS}" =~ ^[1-9][0-9]?$ ]] ||
		fl_e2e_die "--remote-jobs must be a number from 1 to 99 (got '${REMOTE_JOBS}')"
	[[ -z "$(git status --porcelain --untracked-files=no)" ]] ||
		fl_e2e_die "--remote ships the last commit: commit or stash your changes first"
	split="$(python3 "${ROOT}/scripts/e2e-plan.py" split --jobs "${JOBS}" --remote-jobs "${REMOTE_JOBS}" \
		"${MODULES[@]}")" || fl_e2e_die "scripts/e2e-plan.py split failed"
	MODULES=()
	while read -r where module; do
		if [[ "${where}" == "remote" ]]; then
			REMOTE_MODULES+=("${module}")
		else
			MODULES+=("${module}")
		fi
	done <<<"${split}"
fi

# bridge_stale: true when build-bridge/ misses an output or a bridge source is newer than it.
bridge_stale() {
	local out="${ROOT}/build-bridge/bridge" name
	for name in fl-bridge-helper fl-launch.exe fl-shim.exe fl-filedlg.dll; do
		[[ -e "${out}/${name}" ]] || return 0
	done
	local sources=()
	for name in bridge meson.build VERSION; do
		[[ -e "${ROOT}/${name}" ]] && sources+=("${ROOT}/${name}")
	done
	((${#sources[@]} > 0)) || return 1
	[[ -n "$(find "${sources[@]}" -newer "${out}/fl-launch.exe" -print -quit)" ]]
	return $?
}

fl_e2e_build_image "${ROOT}" "${IMAGE}"
if bridge_stale; then
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
# Host-wide slots shared with scripts/e2e-docker.sh (FL_E2E_SLOTS): each container takes one.
BASE="$(fl_e2e_base)"
LOCK_DIR="$(fl_e2e_check_dir "${FL_E2E_LOCK_DIR:-${BASE}/locks}" "lock directory")" || exit $?
if [[ "${LOCK_DIR}" == "${BASE}/locks" ]]; then
	fl_e2e_own_dir "${BASE}" "E2E base directory"
fi
fl_e2e_own_dir "${LOCK_DIR}" "lock directory"

USER_NAME="$(fl_e2e_user)"
# Seconds a job waited for host-wide slots (taken by other runs); not counted in its time.
SLOT_WAIT=0
RUN="$(new_scratch)"
TEMPLATE=""
# GHA uid 1001 has no passwd entry in the image; ssh-keygen and pwd.getpwuid need one.
mapfile -t PASSWD_ARGS < <(fl_e2e_passwd_args "${IMAGE}" "${RUN}/passwd" "${USER_NAME}")

# seed_root SCRATCH SEED: the root a session starts from: a copy of the template (SEED=template),
# the template's download caches only (SEED=fresh) or nothing (SEED=none).
seed_root() {
	local scratch="$1" seed="$2" started="${SECONDS}"
	case "${seed}" in
		template)
			cp -a --reflink=auto -- "${TEMPLATE}/root" "${scratch}/root" || return
			echo "==> e2e-wine: copied the template root in $((SECONDS - started)) s"
			;;
		fresh)
			mkdir -p -- "${scratch}/root/home"
			cp -a -- "${TEMPLATE}/root/.fl-e2e-root" "${scratch}/root/" || return
			if [[ -d "${TEMPLATE}/root/home/.cache" ]]; then
				cp -a --reflink=auto -- "${TEMPLATE}/root/home/.cache" "${scratch}/root/home/" || return
			fi
			;;
		*) mkdir -p -- "${scratch}/root" ;;
	esac
	mkdir -p -- "${scratch}/home"
	return 0
}

# in_container NAME SCRATCH LOG UPDATES UPGRADE REUSE PYTEST-ARGS...: python3 -m pytest PYTEST-ARGS
# in container CONTAINER-NAME with SCRATCH at /e2e, holding a host-wide slot (its wait goes to
# runner.log next to LOG); output in LOG. Returns pytest's status.
in_container() {
	local name="$1" scratch="$2" log="$3" updates="$4" upgrade="$5" reuse="$6" builds_env="" status=0
	shift 6
	local -a mounts=(-v "${ROOT}:/src:ro" -v "${scratch}:/e2e:rw")
	if ((upgrade == 1)); then
		mounts+=(-v "${BUILDS}:${BUILDS_IN}:ro")
		builds_env="${BUILDS_IN}"
	fi
	local asked="${SECONDS}"
	fl_e2e_acquire_slot "${LOCK_DIR}" "${SLOTS}" >>"${log%/*}/runner.log"
	SLOT_WAIT=$((SLOT_WAIT + SECONDS - asked))
	docker run --rm --init \
		--name "${CONTAINER}-${name}" \
		--label fl-e2e=ci-e2e-wine \
		--user "$(id -u):$(id -g)" \
		-e HOME=/e2e/home \
		-e USER="${USER_NAME}" \
		-e LOGNAME="${USER_NAME}" \
		-e FL_E2E_FORK=1 \
		-e FL_E2E_ROOT=/e2e/root \
		-e FL_E2E_REUSE="${reuse}" \
		-e FL_E2E_FORK_VERSION \
		-e FL_E2E_FORK_UPDATES="${updates}" \
		-e FL_E2E_APP_UPGRADE="${upgrade}" \
		-e FL_E2E_BRIDGE_BUILDS="${builds_env}" \
		-e PYTHONDONTWRITEBYTECODE=1 \
		"${mounts[@]}" \
		"${PASSWD_ARGS[@]}" \
		-w /src \
		"${IMAGE}" \
		python3 -m pytest -v -rs --durations=0 --durations-min=5 -p no:cacheprovider --basetemp=/e2e/pytest "$@" \
		>"${log}" 2>&1 7>&- 9>&- || status=$?
	exec 9>&-
	return "${status}"
}

# run_session NAME SEED TARGET...: one pytest session over TARGET... (tests/e2e/<module>[::test], or
# --deselect=...) in a fresh scratch root that starts as seed_root SEED says, then its logs
# (logs/e2e-wine/NAME/) and the root's deletion. Returns pytest's status (1 for a skip).
run_session() {
	local name="$1" seed="$2" logs status=0 updates=0 upgrade=0 reuse=0 target module scratch
	shift 2
	local -a targets=("$@")
	for target in "${targets[@]}"; do
		module="${target%%::*}"
		module="${module##*/}"
		[[ "${module}" != "${FORK_UPDATE}" ]] || updates=1
		[[ "${module}" != "${APP_UPGRADE}" ]] || upgrade=1
	done
	[[ "${seed}" != "template" ]] || reuse=1
	logs="${LOG_DIR}/${name}"
	scratch="$(new_scratch "${RUN}")"
	mkdir -p -- "${logs}"
	echo "==> e2e-wine: ${targets[*]} started (${seed} root, scratch root ${scratch})"
	if seed_root "${scratch}" "${seed}" >>"${logs}/runner.log" 2>&1; then
		in_container "${name}" "${scratch}" "${logs}/pytest.log" "${updates}" "${upgrade}" "${reuse}" \
			"${targets[@]}" "${OPTIONS[@]}" || status=$?
	else
		echo "==> e2e-wine: ${name}: could not prepare its root (${seed}); see ${logs}/runner.log" >"${logs}/pytest.log"
		status=1
	fi
	if ((status == 0)) && grep -q '^SKIPPED ' "${logs}/pytest.log"; then
		echo "==> e2e-wine: ${name} skipped tests; every scenario of a requested tier must run:" >&2
		grep '^SKIPPED ' "${logs}/pytest.log" >&2
		status=1
	fi
	if ((status != 0)); then
		echo "==> e2e-wine: ${name}: the last ${TAIL_LINES} lines of ${logs}/pytest.log:"
		tail -n "${TAIL_LINES}" "${logs}/pytest.log"
	fi
	# Logs only: text files from the scratch root, never images, executables or archives.
	fl_e2e_copy_logs "${scratch}" "${logs}"
	fl_e2e_wipe "${scratch}"
	return "${status}"
}

# failed_modules LOG: the tests/e2e modules with a FAILED or ERROR line in a pytest log, one per line.
failed_modules() {
	grep -oE '^(FAILED|ERROR) tests/e2e/test_[A-Za-z0-9_]+\.py' "$1" | sed 's|.*/||' | sort -u
	return 0
}

# rerun_after_velopack_731 NAME SEED: when session NAME failed (session_status != 0) and its log has
# Velopack's start failure, re-run its failed modules once, each in a session of its own;
# session_status becomes the first failing re-run's status (0 when they all pass).
rerun_after_velopack_731() {
	local name="$1" seed="$2" log="${LOG_DIR}/$1/pytest.log" module rerun=0 rerun_status
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
		run_session "${module%.py}-rerun" "${seed}" "tests/e2e/${module}" || rerun_status=$?
		echo "==> e2e-wine: re-run of tests/e2e/${module} exit ${rerun_status}" | tee -a "${LOG_DIR}/reruns.log"
		if ((rerun == 0)); then
			rerun="${rerun_status}"
		fi
	done
	session_status="${rerun}"
	return 0
}

# module_job WHAT NAME SEED TARGET...: a session (+ its Velopack re-run) as a background job: prints
# "WHAT exit STATUS (N s)", appends a row to results.tsv and exits with the session's status.
module_job() {
	local what="$1" name="$2" seed="$3" started="${SECONDS}" session_status=0 took
	shift 3
	run_session "${name}" "${seed}" "$@" || session_status=$?
	rerun_after_velopack_731 "${name}" "${seed}"
	took=$((SECONDS - started - SLOT_WAIT))
	echo "==> e2e-wine: ${what} exit ${session_status} (${took} s, ${SLOT_WAIT} s waiting for a slot)"
	printf '%s\t%s\t%s\t%s\t%s\n' "${what}" "${session_status}" "${took}" "${seed}" "${SLOT_WAIT}" >>"${LOG_DIR}/results.tsv"
	exit "${session_status}"
}

# template_job: set the main prefix up once in a root of its own (the template), keep that root.
# Exits with the session's status; on success TEMPLATE/root is complete and TEMPLATE/.complete exists.
template_job() {
	local logs="${LOG_DIR}/template" status=0 started="${SECONDS}"
	mkdir -p -- "${logs}" "${TEMPLATE}/home"
	echo "==> e2e-wine: building the template root (${TEMPLATE_TEST[*]})"
	in_container template "${TEMPLATE}" "${logs}/pytest.log" 0 0 0 "${TEMPLATE_TEST[@]}" || status=$?
	fl_e2e_copy_logs "${TEMPLATE}" "${logs}"
	if ((status == 0)); then
		: >"${TEMPLATE}/.complete"
	else
		tail -n "${TAIL_LINES}" "${logs}/pytest.log"
	fi
	echo "==> e2e-wine: template exit ${status} ($((SECONDS - started - SLOT_WAIT)) s, ${SLOT_WAIT} s waiting for a slot)"
	printf 'template\t%s\t%s\t-\t%s\n' "${status}" "$((SECONDS - started - SLOT_WAIT))" "${SLOT_WAIT}" >>"${LOG_DIR}/results.tsv"
	exit "${status}"
}

# prepare_template: TEMPLATE in this run's directory, or DIR/<key> of FL_E2E_TEMPLATE_CACHE (local
# runs only, one at a time: fd 7 holds DIR/.lock; other keys there are deleted). TEMPLATE_STATE
# becomes "ready" when a complete template is there already.
prepare_template() {
	local cache key entry
	if [[ -z "${FL_E2E_TEMPLATE_CACHE:-}" ]]; then
		TEMPLATE="$(new_scratch "${RUN}")"
		return 0
	fi
	cache="$(fl_e2e_check_dir "${FL_E2E_TEMPLATE_CACHE}" "template cache")" || exit $?
	fl_e2e_own_dir "${cache}" "template cache"
	[[ ! -L "${cache}/.lock" ]] || fl_e2e_refuse "lock file ${cache}/.lock is a symlink"
	exec 7>>"${cache}/.lock"
	flock -n 7 || fl_e2e_die "another run uses the template cache ${cache}"
	key="$(python3 "${ROOT}/scripts/e2e-plan.py" key --image-id "$(docker image inspect "${IMAGE}" --format '{{.Id}}' 9>&-)" \
		--fork-version "${FL_E2E_FORK_VERSION:-}")"
	for entry in "${cache}"/*; do
		if [[ -d "${entry}" && "${entry}" != "${cache}/${key:0:16}" ]]; then
			fl_e2e_wipe "${entry}"
		fi
	done
	TEMPLATE="${cache}/${key:0:16}"
	if [[ -f "${TEMPLATE}/.complete" ]]; then
		echo "==> e2e-wine: reusing the template ${TEMPLATE}"
		TEMPLATE_STATE="ready"
		return 0
	fi
	fl_e2e_prepare_scratch "${TEMPLATE}" 0 >&2
	return 0
}

# remote_job: ship this commit to REMOTE's clone, run REMOTE_MODULES there with this script, copy its
# logs to logs/e2e-wine/remote-REMOTE/ and its results into results.tsv; exits with its status.
remote_job() {
	local dir="${FL_E2E_REMOTE_DIR:-fork-linux-ci}" logs="${LOG_DIR}/remote-${REMOTE}" status=0 started="${SECONDS}"
	local tag="fl-remote-$$-${RANDOM}" command arg base
	base="$(fl_e2e_base)"
	mkdir -p -- "${logs}"
	command="set -e; cd ${dir@Q}; mkdir -p -m 0700 ${base}/${tag}; cat >${base}/${tag}.bundle;"
	command+=" git fetch -q ${base}/${tag}.bundle HEAD; git checkout -q --detach FETCH_HEAD; rm -f ${base}/${tag}.bundle;"
	command+=" rc=0; TMPDIR=${base}/${tag}"
	if [[ -n "${FL_E2E_FORK_VERSION:-}" ]]; then
		command+=" FL_E2E_FORK_VERSION=${FL_E2E_FORK_VERSION@Q}"
	fi
	command+=" ./scripts/ci-e2e-wine.sh"
	command+=" --jobs ${REMOTE_JOBS}"
	for arg in "${REMOTE_MODULES[@]/#/tests/e2e/}" "${OPTIONS[@]}"; do
		command+=" ${arg@Q}"
	done
	command+=" || rc=\$?; rmdir ${base}/${tag}; exit \$rc"
	echo "==> e2e-wine: ${REMOTE}: ${REMOTE_MODULES[*]} (${REMOTE_JOBS} at a time)"
	git bundle create - HEAD 2>/dev/null | ssh -o BatchMode=yes "${REMOTE}" "${command}" >"${logs}/runner.log" 2>&1 9>&- ||
		status=$?
	scp -o BatchMode=yes -rq "${REMOTE}:${dir}/logs/e2e-wine/." "${logs}/" 9>&- || true
	if [[ -f "${logs}/results.tsv" ]]; then
		sed "s|^|${REMOTE}:|" "${logs}/results.tsv" >>"${LOG_DIR}/results.tsv"
	fi
	if ((status != 0)); then
		tail -n "${TAIL_LINES}" "${logs}/runner.log"
	fi
	echo "==> e2e-wine: remote ${REMOTE} exit ${status} ($((SECONDS - started)) s)"
	exit "${status}"
}

# record STATUS: the first failure becomes the script's status.
status=0
record() {
	if ((status == 0)); then
		status="$1"
	fi
	return 0
}

# The scheduler: PENDING holds e2e-plan.py's "WHAT SEED SECONDS TARGET..." rows longest first; a template module waits until the
# template is ready (or failed: it then sets up its own prefix). JOBS sessions run at a time.
declare -A RUNNING=()
TEMPLATE_PID=""
TEMPLATE_STATE="off"
PENDING=()

# launch_next: start the first pending session that can start now; status 1 when none can. Its logs
# go to logs/e2e-wine/<module> (tests/e2e/<module>.py[<part>]: <module>-<part>).
launch_next() {
	local index what seed name
	local -a row
	for index in "${!PENDING[@]}"; do
		read -r -a row <<<"${PENDING[${index}]}"
		what="${row[0]}"
		seed="${row[1]}"
		if [[ "${seed}" == "template" && "${TEMPLATE_STATE}" == "building" ]]; then
			continue
		fi
		case "${seed}:${TEMPLATE_STATE}" in
			template:ready | fresh:ready) ;;
			*) seed="none" ;;
		esac
		name="${what#tests/e2e/}"
		name="${name/.py[/-}"
		name="${name%]}"
		module_job "${what}" "${name%.py}" "${seed}" "${row[@]:3}" &
		RUNNING[$!]="${what}"
		unset "PENDING[${index}]"
		PENDING=("${PENDING[@]}")
		return 0
	done
	return 1
}

# reap: wait for one job; a finished template build sets TEMPLATE_STATE.
reap() {
	local done_pid="" rc=0
	wait -n -p done_pid "${!RUNNING[@]}" || rc=$?
	if [[ "${done_pid}" == "${TEMPLATE_PID}" ]]; then
		TEMPLATE_STATE="ready"
		if ((rc != 0)); then
			TEMPLATE_STATE="failed"
		fi
	fi
	unset "RUNNING[${done_pid}]"
	record "${rc}"
	return 0
}

# schedule MODULE...: every module in a session of its own, JOBS at a time, longest first.
schedule() {
	local what seed took line plan needs_template=0
	plan="$(python3 "${ROOT}/scripts/e2e-plan.py" plan "$@")" || fl_e2e_die "scripts/e2e-plan.py plan failed"
	while read -r line; do
		read -r what seed took _ <<<"${line}"
		PENDING+=("${line}")
		[[ "${seed}" != "template" ]] || needs_template=1
		echo "==> e2e-wine: plan: ${what} (${seed} root, ~${took} s)"
	done <<<"${plan}"
	if [[ "${FL_E2E_TEMPLATE:-1}" == "1" ]] && ((needs_template == 1)); then
		prepare_template
		if [[ "${TEMPLATE_STATE}" != "ready" ]]; then
			TEMPLATE_STATE="building"
			template_job &
			TEMPLATE_PID=$!
			RUNNING[$!]="template"
		fi
	fi
	while ((${#PENDING[@]} > 0 || ${#RUNNING[@]} > 0)); do
		if ((${#RUNNING[@]} < JOBS)) && launch_next; then
			continue
		fi
		reap
	done
	return 0
}

rm -rf -- "${LOG_DIR:?}"
mkdir -p -- "${LOG_DIR}"
STARTED="${SECONDS}"
if ((${#SHARD[@]} > 0)); then
	if ((${#REMOTE_MODULES[@]} > 0)); then
		remote_job &
		RUNNING[$!]="remote"
	fi
	echo "==> e2e-wine: ${#MODULES[@]} modules, ${JOBS} at a time"
	# The remote run holds no local slot.
	JOBS=$((JOBS + ${#RUNNING[@]}))
	if ((${#MODULES[@]} > 0)); then
		schedule "${MODULES[@]}"
	fi
	while ((${#RUNNING[@]} > 0)); do
		reap
	done
else
	JOBS=1
	module_job "tests/e2e (${#MODULES[@]} modules)" tier none "${MODULES[@]/#/tests/e2e/}" &
	RUNNING[$!]="tier"
	reap
fi
echo "==> e2e-wine: done in $((SECONDS - STARTED)) s (exit ${status}); results: ${LOG_DIR}/results.tsv"
cleanup
RUN=""
BUILDS=""
echo "==> e2e-wine logs: ${LOG_DIR}"
exit "${status}"
