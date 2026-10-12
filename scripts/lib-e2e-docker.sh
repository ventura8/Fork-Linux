# shellcheck shell=bash
# lib-e2e-docker.sh - shared by scripts/e2e-docker.sh (local real-Fork / Wine runs),
# scripts/ci-e2e-wine.sh (the CI tier) and scripts/ci-e2e-de.sh (the desktop tier): scratch-root
# safety checks, host-wide slots, the E2E image builds and the logs-only copy. Sourced, never
# executed.
#
# Why this exists: on 2026-10-10 a developer's whole home directory was recursively deleted
# while agents ran real-Fork tests on the host (cause never found). Real-Fork / Wine work
# therefore runs only in containers that see the read-only source tree and one scratch root
# outside $HOME (AGENTS.md hard rule 18).

FL_E2E_DOCKERFILE="docker/Dockerfile.e2e.wine"
FL_E2E_SCRATCH_MARKER=".fl-e2e-scratch"
FL_E2E_DIGEST_LABEL="fork-linux.ci.dockerfile-digest"
FL_E2E_LOG_MAX="20M"

# fl_e2e_default_image: the real-Fork image (docker/Dockerfile.e2e.wine declares this tag).
fl_e2e_default_image() {
	printf '%s\n' "fork-linux-ci-e2e-wine:26.04"
	return 0
}

# fl_e2e_base: the host-wide E2E directory (scratch roots, the seed, the slot locks), 0700.
fl_e2e_base() {
	printf '%s\n' "/var/tmp/fork-linux-e2e"
	return 0
}

# fl_e2e_user: this user's login name (a uid without a passwd entry falls back to $USER / uid<N>).
fl_e2e_user() {
	local name
	if name="$(id -un 2>/dev/null)"; then
		printf '%s\n' "${name}"
	else
		printf '%s\n' "${USER:-uid$(id -u)}"
	fi
	return 0
}

# fl_e2e_refuse MESSAGE: a safety refusal (exit status 2).
fl_e2e_refuse() {
	echo "${FL_E2E_PROG:-e2e-docker}: refusing: $*" >&2
	exit 2
}

fl_e2e_die() {
	echo "${FL_E2E_PROG:-e2e-docker}: $*" >&2
	exit 1
}

# fl_e2e_homes: $HOME and this uid's passwd home, resolved, one per line (missing ones skipped).
fl_e2e_homes() {
	local pw_home=""
	if [[ -n "${HOME:-}" ]]; then
		realpath -m -- "${HOME}"
	fi
	pw_home="$(getent passwd "$(id -u)" 2>/dev/null | cut -d: -f6)" || pw_home=""
	if [[ -n "${pw_home}" ]]; then
		realpath -m -- "${pw_home}"
	fi
	return 0
}

# fl_e2e_check_dir PATH WHAT: print PATH normalised, or refuse (exit 2) when it is relative,
# has a '.' / '..' or symlink component, is '/', or is, contains or lies inside a home directory.
fl_e2e_check_dir() {
	local path="$1" what="$2" prefix="" part resolved home
	[[ "${path}" == /* ]] || fl_e2e_refuse "${what} '${path}' is not an absolute path"
	case "/${path}/" in
		*/../* | */./*) fl_e2e_refuse "${what} '${path}' has a '.' or '..' component" ;;
		*) ;; # no '.' / '..' component: keep checking
	esac
	local -a parts
	IFS=/ read -r -a parts <<<"${path#/}"
	for part in "${parts[@]}"; do
		[[ -n "${part}" ]] || continue
		prefix="${prefix}/${part}"
		if [[ -L "${prefix}" ]]; then
			fl_e2e_refuse "${what} '${path}' has a symlink component (${prefix})"
		fi
	done
	resolved="$(realpath -m -- "${path}")"
	[[ "${resolved}" != "/" ]] || fl_e2e_refuse "${what} must not be '/'"
	while IFS= read -r home; do
		[[ -n "${home}" ]] || continue
		[[ "${home}" != "/" ]] || fl_e2e_refuse "the home directory is '/'"
		if [[ "${resolved}" == "${home}" || "${resolved}" == "${home}/"* || "${home}" == "${resolved}/"* ]]; then
			fl_e2e_refuse "${what} '${resolved}' is, contains or lies inside the home directory ${home}"
		fi
	done < <(fl_e2e_homes)
	printf '%s\n' "${resolved}"
	return 0
}

# fl_e2e_check_ro_dir PATH WHAT: print the resolved PATH of an existing directory that is
# mounted read-only (the worktree, the git common dir), or refuse (exit 2) when it is '/' or
# is or contains a home directory. Lying inside a home is fine for these read-only mounts.
fl_e2e_check_ro_dir() {
	local path="$1" what="$2" resolved home
	resolved="$(realpath -e -- "${path}" 2>/dev/null)" || fl_e2e_refuse "${what} '${path}' does not exist"
	[[ -d "${resolved}" ]] || fl_e2e_refuse "${what} '${resolved}' is not a directory"
	[[ "${resolved}" != "/" ]] || fl_e2e_refuse "${what} must not be '/'"
	while IFS= read -r home; do
		[[ -n "${home}" ]] || continue
		[[ "${home}" != "/" ]] || fl_e2e_refuse "the home directory is '/'"
		if [[ "${resolved}" == "${home}" || "${home}" == "${resolved}/"* ]]; then
			fl_e2e_refuse "${what} '${resolved}' is or contains the home directory ${home}"
		fi
	done < <(fl_e2e_homes)
	printf '%s\n' "${resolved}"
	return 0
}

# fl_e2e_name_problem NAME RESERVED...: print why NAME cannot name a run (nothing when it can).
fl_e2e_name_problem() {
	local name="$1" reserved
	shift
	if [[ ! "${name}" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$ ]]; then
		printf '%s\n' "invalid --name '${name}' (letters, digits, . _ -)"
		return 0
	fi
	for reserved in "$@"; do
		if [[ "${name}" == "${reserved}" ]]; then
			printf '%s\n' "--name ${name} is reserved"
			return 0
		fi
	done
	return 0
}

# fl_e2e_require_tools: docker and flock (util-linux) must be on PATH.
fl_e2e_require_tools() {
	command -v docker >/dev/null 2>&1 || fl_e2e_die "docker not found"
	command -v flock >/dev/null 2>&1 || fl_e2e_die "flock not found (util-linux)"
	return 0
}

# fl_e2e_git_common ROOT SRC_DIR: print the resolved git common dir of the worktree ROOT when it
# lies outside SRC_DIR (a linked worktree; mounted read-only too), nothing otherwise.
fl_e2e_git_common() {
	local root="$1" src="$2" common
	common="$(git -C "${root}" rev-parse --path-format=absolute --git-common-dir 2>/dev/null || true)"
	[[ -n "${common}" ]] || return 0
	common="$(fl_e2e_check_ro_dir "${common}" "git common dir")" || exit $?
	[[ "${common}" == "${src}/"* ]] || printf '%s\n' "${common}"
	return 0
}

# fl_e2e_seed_dir BASE: print the checked seed directory (FL_E2E_SEED_DIR or BASE/seed), nothing
# when there is none; a seed that is not a directory is refused.
fl_e2e_seed_dir() {
	local seed="${FL_E2E_SEED_DIR:-$1/seed}"
	[[ -e "${seed}" ]] || return 0
	seed="$(fl_e2e_check_dir "${seed}" "seed directory")" || exit $?
	[[ -d "${seed}" ]] || fl_e2e_refuse "seed '${seed}' is not a directory"
	printf '%s\n' "${seed}"
	return 0
}

# fl_e2e_own_dir DIR WHAT: create DIR (0700) when missing; refuse a symlink, a non-directory or
# a directory owned by someone else.
fl_e2e_own_dir() {
	local dir="$1" what="$2"
	if [[ ! -e "${dir}" && ! -L "${dir}" ]]; then
		mkdir -m 0700 -- "${dir}"
	fi
	[[ ! -L "${dir}" && -d "${dir}" ]] || fl_e2e_refuse "${what} '${dir}' is not a plain directory"
	[[ "$(stat -c %u -- "${dir}")" == "$(id -u)" ]] || fl_e2e_refuse "${what} '${dir}' is not owned by $(fl_e2e_user)"
	return 0
}

# fl_e2e_wipe DIR: delete a scratch root this library created (it must carry the marker). The
# path was checked by fl_e2e_check_dir; rm never follows symlinks and stays on one file system.
fl_e2e_wipe() {
	local dir="${1:?fl_e2e_wipe needs a directory}"
	[[ -d "${dir}" && ! -L "${dir}" ]] || return 0
	[[ -f "${dir}/${FL_E2E_SCRATCH_MARKER}" && ! -L "${dir}/${FL_E2E_SCRATCH_MARKER}" ]] ||
		fl_e2e_refuse "'${dir}' has no ${FL_E2E_SCRATCH_MARKER} marker; not deleting it"
	# Wine leaves some read-only directories behind; make them deletable (find -P never follows links).
	find -P "${dir:?}" -xdev -type d ! -perm -u+w -exec chmod u+w {} + 2>/dev/null || true
	rm -rf --one-file-system -- "${dir:?}" || return
	return 0
}

# fl_e2e_prepare_scratch DIR KEEP: make DIR a private scratch root carrying the marker. A
# non-empty DIR without the marker is refused. KEEP=0 wipes a previous run first; KEEP=1 reuses it.
fl_e2e_prepare_scratch() {
	local dir="${1:?}" keep="$2"
	if [[ -e "${dir}" || -L "${dir}" ]]; then
		fl_e2e_own_dir "${dir}" "scratch root"
		if [[ -n "$(find -P "${dir}" -mindepth 1 -maxdepth 1 -print -quit)" ]]; then
			if [[ ! -f "${dir}/${FL_E2E_SCRATCH_MARKER}" || -L "${dir}/${FL_E2E_SCRATCH_MARKER}" ]]; then
				fl_e2e_refuse "scratch root '${dir}' is not empty and has no ${FL_E2E_SCRATCH_MARKER} marker (not ours)"
			fi
			if [[ "${keep}" != "1" ]]; then
				echo "==> wiping the previous scratch root ${dir} (--keep reuses it)"
				fl_e2e_wipe "${dir}"
			fi
		fi
	fi
	if [[ ! -e "${dir}" ]]; then
		mkdir -p -- "$(dirname -- "${dir}")"
		mkdir -m 0700 -- "${dir}"
	fi
	fl_e2e_own_dir "${dir}" "scratch root"
	chmod 0700 -- "${dir}"
	if [[ ! -e "${dir}/${FL_E2E_SCRATCH_MARKER}" ]]; then
		printf 'fork-linux e2e-docker scratch root\n' >"${dir}/${FL_E2E_SCRATCH_MARKER}"
	fi
	return 0
}

# fl_e2e_acquire_slot LOCKDIR SLOTS: hold one of SLOTS host-wide slots on fd 9 (flock). Every
# slot is tried without blocking; while all are busy, all are tried again every
# FL_E2E_SLOT_POLL seconds (default 5), so a run takes the first slot that frees up (waiting on
# one fixed slot let later runs overtake it for as long as that one stayed busy). Sets
# FL_E2E_SLOT. Callers close fd 9 for every child (docker run ... 9>&-), so no container
# process ever holds the lock.
fl_e2e_acquire_slot() {
	local dir="$1" slots="$2" poll="${FL_E2E_SLOT_POLL:-5}"
	[[ "${slots}" =~ ^[1-9][0-9]?$ ]] || fl_e2e_die "FL_E2E_SLOTS must be a number from 1 to 99 (got '${slots}')"
	[[ "${poll}" =~ ^[0-9]{1,3}(\.[0-9]{1,3})?$ ]] || fl_e2e_die "FL_E2E_SLOT_POLL must be a number of seconds (got '${poll}')"
	fl_e2e_try_slots "${dir}" "${slots}" && return 0
	echo "==> all ${slots} E2E slots are busy (FL_E2E_SLOTS); waiting for the first one to free up"
	until fl_e2e_try_slots "${dir}" "${slots}"; do
		sleep "${poll}"
	done
	return 0
}

# fl_e2e_try_slots LOCKDIR SLOTS: take the first free slot on fd 9 without blocking (sets
# FL_E2E_SLOT); status 1 when every slot is busy.
fl_e2e_try_slots() {
	local dir="$1" slots="$2" n
	for ((n = 1; n <= slots; n++)); do
		[[ ! -L "${dir}/slot-${n}" ]] || fl_e2e_refuse "lock file ${dir}/slot-${n} is a symlink"
		exec 9>>"${dir}/slot-${n}"
		if flock -n 9; then
			FL_E2E_SLOT="${n}"
			echo "==> holding E2E slot ${FL_E2E_SLOT}/${slots}"
			return 0
		fi
		exec 9>&-
	done
	return 1
}

# fl_e2e_build_image ROOT IMAGE [DOCKERFILE]: build IMAGE from DOCKERFILE (relative to ROOT;
# default docker/Dockerfile.e2e.wine) unless the image already carries that Dockerfile's digest
# (FL_CI_FORCE_BUILD=1 always rebuilds). The build uses the docker driver's 'default' builder
# unless BUILDX_BUILDER names another: a docker-container builder would leave the image in its
# own cache instead of the local image store. In GitHub Actions (FL_CI_DOCKER_CACHE=gha, as in
# scripts/ci-docker.sh) the job's buildx builder loads the image and keeps its layers in the
# Actions cache, one scope per image: package layers only, never a Fork download.
fl_e2e_build_image() {
	local root="$1" image="$2" dockerfile="${3:-${FL_E2E_DOCKERFILE}}" digest current scope
	local -a common
	digest="$(sha256sum "${root}/${dockerfile}" | cut -d' ' -f1)"
	current="$(docker image inspect "${image}" --format "{{index .Config.Labels \"${FL_E2E_DIGEST_LABEL}\"}}" 2>/dev/null 8>&- 9>&- || true)"
	if [[ "${FL_CI_FORCE_BUILD:-0}" != "1" && "${current}" == "${digest}" ]]; then
		echo "==> reusing ${image} (Dockerfile digest ${digest:0:12} unchanged)"
		return 0
	fi
	common=(-f "${root}/${dockerfile}" -t "${image}" --label "${FL_E2E_DIGEST_LABEL}=${digest}")
	if [[ "${FL_CI_DOCKER_CACHE:-}" == "gha" ]]; then
		scope="${image%%:*}"
		echo "==> docker buildx build ${image} from ${dockerfile} (cache=gha, scope ${scope})"
		docker buildx build "${common[@]}" --cache-from "type=gha,scope=${scope}" \
			--cache-to "type=gha,mode=max,scope=${scope}" --load "${root}/docker" 9>&- 8>&- || return
		return 0
	fi
	echo "==> docker build ${image} from ${dockerfile}"
	BUILDX_BUILDER="${BUILDX_BUILDER:-default}" DOCKER_BUILDKIT=1 docker build "${common[@]}" "${root}/docker" 9>&- 8>&- || return
	return 0
}

# fl_e2e_image_tag ROOT DOCKERFILE: the tag DOCKERFILE declares in its '# Image tag:' header.
fl_e2e_image_tag() {
	local tag
	tag="$(sed -n 's/^# Image tag: \([^ ]*\)$/\1/p' "$1/$2" | head -n 1)"
	[[ -n "${tag}" ]] || fl_e2e_die "$2 declares no '# Image tag:'"
	printf '%s\n' "${tag}"
	return 0
}

# fl_e2e_copy_logs SCRATCH DEST: text logs only (*.log *.json *.txt under 20 MB), never from a
# Wine drive_c or a download cache, never through a symlink; paths relative to SCRATCH.
fl_e2e_copy_logs() {
	local src="$1" dest="$2"
	[[ -d "${src}" && ! -L "${src}" ]] || return 0
	mkdir -p -- "${dest}"
	(cd -- "${src}" && find -P . -xdev -type f \( -name '*.log' -o -name '*.json' -o -name '*.txt' \) \
		-size "-${FL_E2E_LOG_MAX}" -not -path '*/drive_c/*' -not -path '*/.cache/*' \
		-exec cp --parents -t "${dest}" {} +) 2>/dev/null || true
	return 0
}
