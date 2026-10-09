#!/usr/bin/env bash
# ci-packaging-cell.sh - one packaging cell of Fork for Linux (unofficial): build the image,
# build the package, smoke-verify it, then the live install E2E (install -> upgrade ->
# remove -> reinstall; scripts/packaging-e2e-install.sh). Shared by check.yml, release.yml
# and scripts/ci-packaging-matrix.sh, so a cell fails the same way everywhere.
#
# Usage: ./scripts/ci-packaging-cell.sh <format>
# Formats: deb deb-jammy rpm-fedora rpm-opensuse arch snap appimage flatpak tarball
#
# Env: FL_ARTIFACTS_DIR (default artifacts/), FL_PACKAGING_ARTIFACTS_ISOLATE=1 puts each
# cell's output in artifacts/ci-packaging/<format> (parallel matrix).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

FORMAT="${1:?usage: ci-packaging-cell.sh <deb|deb-jammy|rpm-fedora|rpm-opensuse|arch|snap|appimage|flatpak|tarball>}"
export DOCKER_BUILDKIT=1
if [[ "${FL_PACKAGING_ARTIFACTS_ISOLATE:-0}" == "1" ]]; then
	export FL_ARTIFACTS_DIR="${ROOT}/artifacts/ci-packaging/${FORMAT}"
fi
export FL_ARTIFACTS_DIR="${FL_ARTIFACTS_DIR:-${ROOT}/artifacts}"
[[ "${FL_ARTIFACTS_DIR}" == "${ROOT}"/* ]] || {
	echo "error: FL_ARTIFACTS_DIR must be inside ${ROOT} (it is bind-mounted with the tree)" >&2
	exit 1
}
mkdir -p "${FL_ARTIFACTS_DIR}"
ART_IN="/src/${FL_ARTIFACTS_DIR#"${ROOT}"/}"

DOCKER_FLAGS=()
case "${FORMAT}" in
	deb)
		DOCKERFILE=docker/Dockerfile.ppa IMAGE=fork-linux-ci-ppa:26.04
		BUILD=(./scripts/release-deb.sh)
		;;
	deb-jammy)
		DOCKERFILE=docker/Dockerfile.ppa.jammy IMAGE=fork-linux-ci-ppa-jammy:22.04
		BUILD=(./scripts/release-deb.sh)
		;;
	rpm-fedora)
		DOCKERFILE=docker/Dockerfile.rpm.fedora IMAGE=fork-linux-ci-rpm-fedora:44
		BUILD=(./scripts/release-rpm.sh fedora)
		;;
	rpm-opensuse)
		DOCKERFILE=docker/Dockerfile.rpm.opensuse IMAGE=fork-linux-ci-rpm-opensuse:16.0
		BUILD=(./scripts/release-rpm.sh opensuse)
		;;
	arch)
		DOCKERFILE=docker/Dockerfile.arch IMAGE=fork-linux-ci-arch-pkg:base-devel-20260906.0.587075
		BUILD=(./scripts/release-arch.sh)
		;;
	tarball)
		DOCKERFILE=docker/Dockerfile.release IMAGE=fork-linux-ci-release:26.04
		BUILD=(./scripts/release-portable.sh tarball)
		;;
	appimage)
		DOCKERFILE=docker/Dockerfile.release IMAGE=fork-linux-ci-release:26.04
		BUILD=(./scripts/release-portable.sh appimage)
		;;
	flatpak)
		# flatpak-builder and flatpak run use bubblewrap (user namespaces, mounts).
		DOCKERFILE=docker/Dockerfile.release IMAGE=fork-linux-ci-release:26.04
		BUILD=(./scripts/release-portable.sh flatpak)
		DOCKER_FLAGS=(--privileged)
		;;
	snap)
		DOCKERFILE="" IMAGE="" BUILD=()
		;;
	*)
		echo "error: unknown packaging format: ${FORMAT}" >&2
		exit 2
		;;
esac

echo "==> packaging cell ${FORMAT} (artifacts: ${FL_ARTIFACTS_DIR})"
status=0
if [[ "${FORMAT}" == snap ]]; then
	./scripts/ci-snap-build.sh || status=$?
else
	docker build -f "${DOCKERFILE}" -t "${IMAGE}" docker
	run_in_image() {
		docker run --rm "${DOCKER_FLAGS[@]}" -e "FL_ARTIFACTS_DIR=${ART_IN}" \
			-v "${ROOT}:/src:rw" -w /src "${IMAGE}" "$@"
	}
	run_in_image "${BUILD[@]}" || status=$?
	if ((status == 0)); then
		./scripts/packaging-smoke-verify.sh "${FORMAT}" || status=$?
	fi
	if ((status == 0)); then
		run_in_image ./scripts/packaging-e2e-install.sh "${FORMAT}" || status=$?
	fi
fi
# The packaging containers run as root: hand the outputs back to the invoking user.
docker run --rm -v "${FL_ARTIFACTS_DIR}:/a" busybox:1.37 chown -R "$(id -u):$(id -g)" /a >/dev/null 2>&1 || true
if ((status != 0)); then
	echo "==> packaging cell FAILED: ${FORMAT} (exit ${status})" >&2
	exit "${status}"
fi
echo "==> packaging cell OK: ${FORMAT}"
