#!/usr/bin/env bash
# ci-docker.sh - build and run one CI stage of Fork for Linux (unofficial) inside Docker.
#
# Stages (FL_CI_STAGE):
#   lint      docker/Dockerfile.ci.lint      meson --werror build (native + MinGW) + data tests
#                                            (desktop-file-validate, appstreamcli validate),
#                                            clang-tidy + cppcheck on bridge/, Python syntax, ruff,
#                                            no-suppressions-lint, gen-data check, shellcheck on
#                                            every discovered shell file, actionlint, yamllint,
#                                            hadolint, xmllint, bash -n / zsh -n / fish -n
#   coverage  docker/Dockerfile.ci.coverage  pytest with the coverage gates (90% overall, 100%
#                                            branch on the critical modules) ->
#                                            artifacts/coverage/coverage.xml; the native C unit
#                                            tests under ASan + UBSan; then the C coverage report
#                                            (scripts/ci-c-coverage.sh, when present)
#   bridge    docker/Dockerfile.ci.bridge    MinGW + musl builds with the reproducibility check,
#                                            native unit tests, FL_REAL_WINE=1 Wine tier
#   compat    one Dockerfile per cell        meson build + meson test, py_compile, pytest (no
#             (FL_CI_CELL)                   coverage floors)
#
# Compat cells (FL_CI_CELL): jammy noble resolute debian13 fedora44 leap16 arch
#
# Everything runs as the invoking (unprivileged) user with HOME=/tmp/fl-home; the source is
# bind-mounted at /src (the coverage stage mounts it at its host path so coverage.xml carries
# the paths SonarQube sees). Dockerfiles live under docker/ (AGENTS.md §4.7.1, §4.8).
#
# Caching (speed only; never changes a gate):
#   FL_CI_DOCKER_CACHE=gha|local|none (default local)
#     gha    buildx + GitHub Actions cache (check.yml)
#     local  buildx + .cache/docker-ci/<scope>; skip the build when the image's
#            Dockerfile-digest label matches
#     none   plain docker build (BuildKit layer cache only)
#   FL_CI_FORCE_BUILD=1     always rebuild
#   FL_CI_PARALLEL_BUILD=1  plain docker build (parallel matrix: buildx cache export deadlocks)
#   FL_CI_SKIP_BUILD=1      use the existing image
#
# Usage: FL_CI_STAGE=lint ./scripts/ci-docker.sh [--build-only]
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FL_CI_STAGE="${FL_CI_STAGE:-compat}"
FL_CI_CELL="${FL_CI_CELL:-resolute}"
FL_CI_DOCKER_CACHE="${FL_CI_DOCKER_CACHE:-local}"
FL_CI_FORCE_BUILD="${FL_CI_FORCE_BUILD:-0}"
COMPAT_CELLS=(jammy noble resolute debian13 fedora44 leap16 arch)
DIGEST_LABEL="fork-linux.ci.dockerfile-digest"

die() {
	echo "ci-docker.sh: $*" >&2
	exit 1
}

# Image tags carry an explicit version suffix equal to the Dockerfile's FROM tag
# (tests/test_docker.py); never the implicit ":latest".
resolve_stage() {
	case "${FL_CI_STAGE}" in
		lint)
			DOCKERFILE="docker/Dockerfile.ci.lint"
			IMAGE="fork-linux-ci-lint:26.04"
			BUILD_DIR="build-ci-lint"
			;;
		coverage)
			DOCKERFILE="docker/Dockerfile.ci.coverage"
			IMAGE="fork-linux-ci-coverage:26.04"
			BUILD_DIR="build-ci-coverage"
			;;
		bridge)
			DOCKERFILE="docker/Dockerfile.ci.bridge"
			IMAGE="fork-linux-ci-bridge:26.04"
			BUILD_DIR="build-bridge"
			;;
		compat)
			case "${FL_CI_CELL}" in
				jammy) DOCKERFILE="docker/Dockerfile.ci.jammy" IMAGE="fork-linux-ci-jammy:22.04" ;;
				noble) DOCKERFILE="docker/Dockerfile.ci.noble" IMAGE="fork-linux-ci-noble:24.04" ;;
				resolute) DOCKERFILE="docker/Dockerfile.ci" IMAGE="fork-linux-ci-resolute:26.04" ;;
				debian13) DOCKERFILE="docker/Dockerfile.ci.debian13" IMAGE="fork-linux-ci-debian13:13" ;;
				fedora44) DOCKERFILE="docker/Dockerfile.ci.fedora44" IMAGE="fork-linux-ci-fedora44:44" ;;
				leap16) DOCKERFILE="docker/Dockerfile.ci.leap16" IMAGE="fork-linux-ci-leap16:16.0" ;;
				arch) DOCKERFILE="docker/Dockerfile.ci.arch" IMAGE="fork-linux-ci-arch:base-20260906.0.587075" ;;
				*) die "unknown FL_CI_CELL='${FL_CI_CELL}' (expected: ${COMPAT_CELLS[*]})" ;;
			esac
			BUILD_DIR="build-ci-${FL_CI_CELL}"
			;;
		*)
			die "unknown FL_CI_STAGE='${FL_CI_STAGE}' (expected: lint coverage bridge compat)"
			;;
	esac
	BUILD_DIR="${FL_CI_BUILD_DIR:-${BUILD_DIR}}"
}

scope_name() {
	if [[ "${FL_CI_STAGE}" == "compat" ]]; then
		echo "fork-linux-ci-compat-${FL_CI_CELL}"
	else
		echo "fork-linux-ci-${FL_CI_STAGE}"
	fi
}

dockerfile_digest() {
	sha256sum "${DOCKERFILE}" | awk '{print $1}'
}

image_digest() {
	docker image inspect "${IMAGE}" --format "{{index .Config.Labels \"${DIGEST_LABEL}\"}}" 2>/dev/null || true
}

build_image() {
	export DOCKER_BUILDKIT=1
	local digest scope cache_dir rc
	digest="$(dockerfile_digest)"
	scope="$(scope_name)"
	if [[ "${FL_CI_FORCE_BUILD}" != "1" && "$(image_digest)" == "${digest}" ]]; then
		echo "==> reusing ${IMAGE} (Dockerfile digest ${digest:0:12} unchanged; FL_CI_FORCE_BUILD=1 rebuilds)"
		return 0
	fi
	local -a common=(--file "${DOCKERFILE}" --tag "${IMAGE}" --label "${DIGEST_LABEL}=${digest}")
	if [[ "${FL_CI_PARALLEL_BUILD:-0}" == "1" ]]; then
		echo "==> docker build ${IMAGE} (parallel matrix, no buildx cache export)"
		docker build "${common[@]}" docker
		return 0
	fi
	echo "==> docker build ${IMAGE} from ${DOCKERFILE} (cache=${FL_CI_DOCKER_CACHE})"
	case "${FL_CI_DOCKER_CACHE}" in
		gha)
			docker buildx build "${common[@]}" \
				--cache-from "type=gha,scope=${scope}" \
				--cache-to "type=gha,mode=max,scope=${scope}" \
				--load docker
			;;
		local)
			cache_dir="${ROOT}/.cache/docker-ci/${scope}"
			if [[ ! -f "${cache_dir}/index.json" ]]; then
				rm -rf "${cache_dir}"
			else
				rm -rf "${cache_dir}/ingest"
			fi
			mkdir -p "${cache_dir}"
			rc=0
			docker buildx build "${common[@]}" \
				--cache-from "type=local,src=${cache_dir}" \
				--cache-to "type=local,dest=${cache_dir},mode=max" \
				--load docker || rc=$?
			if ((rc != 0)); then
				# A failed cache export after a good --load is fine only when the label matches.
				if [[ "$(image_digest)" == "${digest}" ]]; then
					echo "warning: local cache export failed; ${IMAGE} digest matches, continuing" >&2
				else
					echo "==> retrying ${IMAGE} without the local cache"
					docker buildx build "${common[@]}" --load docker
				fi
			fi
			;;
		none)
			docker build "${common[@]}" docker
			;;
		*)
			die "unknown FL_CI_DOCKER_CACHE='${FL_CI_DOCKER_CACHE}' (expected: gha local none)"
			;;
	esac
}

# ---------------------------------------------------------------------------
# inside the container
# ---------------------------------------------------------------------------

meson_build() {
	local -a opts=("$@")
	echo "==> meson setup ${BUILD_DIR} ${opts[*]} [stage=${FL_CI_STAGE} cell=${FL_CI_CELL}]"
	rm -rf "${BUILD_DIR}"
	meson setup "${BUILD_DIR}" "${opts[@]}"
	echo "==> meson compile"
	meson compile -C "${BUILD_DIR}"
}

run_meson_tests() {
	echo "==> meson test"
	meson test -C "${BUILD_DIR}" --print-errorlogs
}

run_py_compile() {
	echo "==> Python syntax (compile() on every tracked Python file)"
	python3 - <<'EOF'
import subprocess

files = subprocess.run(
    ["git", "-c", "safe.directory=*", "ls-files", "-co", "--exclude-standard", "*.py"],
    check=True, capture_output=True, text=True,
).stdout.split()
for path in files:
    with open(path, encoding="utf-8") as handle:
        compile(handle.read(), path, "exec", dont_inherit=True)
print(f"py_compile: {len(files)} files OK")
EOF
}

run_no_suppressions() {
	echo "==> no-suppressions-lint"
	python3 scripts/no-suppressions-lint.py .
}

run_gen_data_check() {
	echo "==> generated completions / manual pages are current"
	python3 scripts/gen-data.py check
}

# Shell files: *.sh plus anything whose first line is a sh/bash shebang, from git's view
# (tracked + untracked-but-not-ignored), and the bash completion.
discover_shell_files() {
	local f first
	while IFS= read -r f; do
		[[ -f "${f}" ]] || continue
		case "${f}" in
			*.sh)
				printf '%s\n' "${f}"
				;;
			*)
				IFS= read -r first <"${f}" || first=""
				if [[ "${first}" =~ ^\#!.*(/|env\ )(ba|da)?sh([[:space:]]|$) ]]; then
					printf '%s\n' "${f}"
				fi
				;;
		esac
	done < <(git -c safe.directory='*' ls-files -co --exclude-standard)
}

run_shellcheck() {
	local -a files
	mapfile -t files < <(discover_shell_files)
	echo "==> shellcheck (${#files[@]} discovered files + the bash completion)"
	shellcheck -x "${files[@]}"
	shellcheck --shell=bash data/completions/fork-linux.bash
}

run_actionlint() {
	echo "==> actionlint"
	actionlint
}

run_ruff() {
	echo "==> ruff $(ruff --version | awk '{print $2}') (pyproject.toml [tool.ruff]: py310, E F W B UP)"
	ruff check --no-cache .
}

run_yamllint() {
	echo "==> yamllint (workflows, packaging manifests)"
	yamllint --strict -c .github/yamllint.yaml .github/workflows packaging/snap/snapcraft.yaml \
		packaging/flatpak/*.yml .github/actionlint.yaml
}

run_hadolint() {
	# Reported in full; only errors fail: the DL3008 / DL3041 / DL3037 "pin package versions"
	# warnings contradict AGENTS.md §4.8 (packages are locked by the pinned base images).
	echo "==> hadolint (docker/Dockerfile.*)"
	hadolint --failure-threshold error docker/Dockerfile.*
}

run_xmllint() {
	echo "==> xmllint (metainfo template + generated, Thunar template, our SVG icons)"
	xmllint --noout data/*.metainfo.xml.in src/fork_linux/data/templates/thunar-uca-action.xml.in \
		"${BUILD_DIR}"/*.metainfo.xml data/icons/hicolor/*/apps/*.svg
}

run_cppcheck() {
	echo "==> cppcheck (bridge/common, bridge/unix)"
	cppcheck --error-exitcode=1 --quiet --std=c11 --enable=warning,portability,performance \
		-Ibridge/common -Ibridge/unix bridge/common bridge/unix
}

run_completion_syntax() {
	echo "==> bash -n / zsh -n / fish -n on data/completions"
	bash -n data/completions/fork-linux.bash
	zsh -n data/completions/_fork-linux
	fish --no-execute data/completions/fork-linux.fish
}

run_clang_tidy() {
	echo "==> clang-tidy (bridge/common, bridge/unix against ${BUILD_DIR}/compile_commands.json)"
	local -a sources
	mapfile -t sources < <(find bridge/common bridge/unix -name '*.c' | sort)
	clang-tidy --quiet -p "${BUILD_DIR}" "${sources[@]}"
	echo "==> clang-tidy (bridge/win, MinGW-w64 target)"
	local sysroot
	sysroot="$(x86_64-w64-mingw32-gcc -print-sysroot)"
	mapfile -t sources < <(find bridge/win -maxdepth 1 -name '*.c' | sort)
	clang-tidy --quiet "${sources[@]}" -- \
		--target=x86_64-w64-mingw32 -std=c11 -municode -DUNICODE -D_UNICODE -D_WIN32_WINNT=0x0601 \
		-Ibridge/common -Ibridge/win -I"${BUILD_DIR}/bridge" \
		-isystem "${sysroot:-/usr}/x86_64-w64-mingw32/include"
}

run_pytest_coverage() {
	local cov="${BUILD_DIR}/.coverage" module
	local -a critical=(download manifest feeds fsutil locking state config_edit registry pathmap
		fork_settings snapshots bootstrap pe_resources imaging ssh_sync gitconfig errors)
	export COVERAGE_FILE="${cov}"
	rm -rf "${BUILD_DIR}"
	mkdir -p "${BUILD_DIR}" artifacts/coverage
	echo "==> pytest with coverage [COVERAGE_FILE=${cov}]"
	python3 -m pytest -q -p no:cacheprovider --basetemp="${HOME}/pytest" \
		--cov=fork_linux --cov-branch --cov-report=term-missing:skip-covered tests
	# Cobertura XML for SonarQube Cloud, written before the gates so a failing gate still
	# leaves a report behind.
	python3 -m coverage xml --data-file="${cov}" -o artifacts/coverage/coverage.xml
	echo "==> wrote artifacts/coverage/coverage.xml"
	echo "==> gate: 90% overall"
	python3 -m coverage report --data-file="${cov}" --precision=2 --fail-under=90 | tail -n 1
	echo "==> gate: 100% branch coverage on the critical modules"
	for module in "${critical[@]}"; do
		python3 -m coverage report --data-file="${cov}" --include="*/fork_linux/${module}.py" \
			--precision=2 --fail-under=100 | tail -n 1
	done
}

run_sanitized_unit_tests() {
	echo "==> bridge native unit tests under ASan + UBSan (meson -Db_sanitize=address,undefined)"
	rm -rf "${BUILD_DIR}-asan"
	meson setup "${BUILD_DIR}-asan" -Dbridge=disabled -Db_sanitize=address,undefined -Db_lundef=false
	meson compile -C "${BUILD_DIR}-asan"
	meson test -C "${BUILD_DIR}-asan" --suite bridge-unit --print-errorlogs
}

run_pytest_compat() {
	echo "==> pytest (compat, no coverage floors) [cell=${FL_CI_CELL}]"
	python3 -m pytest -q -p no:cacheprovider --basetemp="${HOME}/pytest" tests
}

run_bridge() {
	echo "==> bridge: MinGW + musl build, reproducibility, native unit tests"
	./scripts/build-bridge.sh --no-docker --repro
	echo "==> bridge: Wine tier (FL_REAL_WINE=1, unprivileged, throwaway prefixes)"
	# The shims were just built above: the Wine tests must not rebuild them (in Docker).
	FL_REAL_WINE=1 FL_BRIDGE_SKIP_BUILD=1 xvfb-run -a python3 -m pytest -q -p no:cacheprovider --basetemp="${HOME}/pytest" tests/bridge
}

run_inside() {
	cd "${FL_CI_WORKDIR:-/src}"
	mkdir -p "${HOME}"
	export PYTHONDONTWRITEBYTECODE=1
	case "${FL_CI_STAGE}" in
		lint)
			meson_build --werror -Dbridge=enabled -Dfile_manager_actions=true
			meson test -C "${BUILD_DIR}" --suite data --print-errorlogs
			run_clang_tidy
			run_cppcheck
			run_py_compile
			run_ruff
			run_no_suppressions
			run_gen_data_check
			run_shellcheck
			run_actionlint
			run_yamllint
			run_hadolint
			run_xmllint
			run_completion_syntax
			;;
		coverage)
			run_pytest_coverage
			run_sanitized_unit_tests
			;;
		bridge)
			run_bridge
			;;
		compat)
			meson_build -Dfile_manager_actions=true
			run_meson_tests
			run_py_compile
			run_pytest_compat
			;;
		*)
			die "unknown FL_CI_STAGE='${FL_CI_STAGE}'"
			;;
	esac
	echo "==> CI checks passed [stage=${FL_CI_STAGE} cell=${FL_CI_CELL}]"
}

resolve_stage

if [[ "${1:-}" == "--inside" ]]; then
	# The container half only: the bridge stage runs the FL_REAL_WINE=1 tier, which never runs
	# on the host (AGENTS.md hard rule 18). Run ./scripts/ci-docker.sh without --inside.
	[[ -e /.dockerenv || -e /run/.containerenv ]] ||
		die "--inside runs only inside the CI container; run FL_CI_STAGE=${FL_CI_STAGE} ./scripts/ci-docker.sh"
	run_inside
	exit 0
fi

cd "${ROOT}"
[[ -f "${DOCKERFILE}" ]] || die "missing ${DOCKERFILE} (stage=${FL_CI_STAGE} cell=${FL_CI_CELL})"
if [[ "${FL_CI_SKIP_BUILD:-0}" != "1" ]]; then
	build_image
fi
if [[ "${1:-}" == "--build-only" ]]; then
	echo "==> image ready: ${IMAGE}"
	exit 0
fi

WORKDIR="/src"
if [[ "${FL_CI_STAGE}" == "coverage" ]]; then
	WORKDIR="${ROOT}"
fi
echo "==> docker run ${IMAGE} (stage=${FL_CI_STAGE}, build dir ${BUILD_DIR}, workdir ${WORKDIR})"
docker run --rm \
	--user "$(id -u):$(id -g)" \
	-e HOME=/tmp/fl-home \
	-e "FL_CI_STAGE=${FL_CI_STAGE}" \
	-e "FL_CI_CELL=${FL_CI_CELL}" \
	-e "FL_CI_BUILD_DIR=${BUILD_DIR}" \
	-e "FL_CI_WORKDIR=${WORKDIR}" \
	-v "${ROOT}:${WORKDIR}:rw" \
	-w "${WORKDIR}" \
	"${IMAGE}" \
	./scripts/ci-docker.sh --inside

if [[ "${FL_CI_STAGE}" == "coverage" && "${FL_CI_C_COVERAGE:-1}" == "1" && -x scripts/ci-c-coverage.sh ]]; then
	echo "==> C coverage (scripts/ci-c-coverage.sh) -> artifacts/coverage/c-coverage.xml"
	./scripts/ci-c-coverage.sh
fi
echo "==> stage ${FL_CI_STAGE} OK"
