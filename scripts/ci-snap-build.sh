#!/usr/bin/env bash
# ci-snap-build.sh - build the fork-linux snap in docker/Dockerfile.snap (Ubuntu 26.04, systemd
# as PID 1 so snapd works) and run the snap packaging E2E in the same container.
#
# The container needs --privileged and the host cgroup tree (snapd mounts squashfs images
# and manages them with systemd units). FL_SNAP_E2E=0 skips the install E2E (never the smoke verify).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "${ROOT}"

IMAGE="fork-linux-ci-snap:26.04"
CONTAINER="fork-linux-snap-build-$$"
export FL_ARTIFACTS_DIR="${FL_ARTIFACTS_DIR:-${ROOT}/artifacts}"
mkdir -p "${FL_ARTIFACTS_DIR}"
ART_IN="/src/${FL_ARTIFACTS_DIR#"${ROOT}"/}"
[[ "${FL_ARTIFACTS_DIR}" == "${ROOT}"/* ]] || {
	echo "error: FL_ARTIFACTS_DIR must be inside ${ROOT}" >&2
	exit 1
}

# shellcheck source=scripts/release-common.sh
. scripts/release-common.sh
mapfile -t GIT_MOUNT < <(fl_git_mount_args)

cleanup() {
	docker rm -f "${CONTAINER}" >/dev/null 2>&1 || true
	return 0
}
trap cleanup EXIT

DOCKER_BUILDKIT=1 docker build -f docker/Dockerfile.snap -t "${IMAGE}" .

docker run -d --name "${CONTAINER}" --privileged --cgroupns=host \
	-v /sys/fs/cgroup:/sys/fs/cgroup:rw \
	-v /sys/kernel/security:/sys/kernel/security:rw \
	-v "${ROOT}:/src:rw" "${GIT_MOUNT[@]}" -w /src "${IMAGE}" >/dev/null

echo "==> waiting for systemd in ${CONTAINER}"
ready=0
for _ in $(seq 1 90); do
	state="$(docker exec "${CONTAINER}" systemctl is-system-running 2>/dev/null || true)"
	if [[ "${state}" == running || "${state}" == degraded ]]; then
		ready=1
		break
	fi
	sleep 1
done
if ((ready == 0)); then
	echo "error: systemd did not start in ${CONTAINER}" >&2
	docker logs --tail 50 "${CONTAINER}" >&2 || true
	exit 1
fi

docker exec -w /src -e "FL_ARTIFACTS_DIR=${ART_IN}" "${CONTAINER}" \
	/usr/local/bin/snap-entrypoint.sh ./scripts/release-portable.sh snap

# The smoke verify always runs; FL_SNAP_E2E=0 only skips the live install E2E (and the
# previous release it upgrades from, FL_PACKAGING_PREVIOUS, built by ci-packaging-cell.sh's rules).
./scripts/packaging-smoke-verify.sh snap
if [[ "${FL_SNAP_E2E:-1}" == "1" ]]; then
	previous="${FL_PACKAGING_PREVIOUS:-0.9.9}"
	rm -rf "${FL_ARTIFACTS_DIR:?}/previous"
	docker exec -w /src -e "FL_ARTIFACTS_DIR=${ART_IN}" "${CONTAINER}" \
		/usr/local/bin/snap-entrypoint.sh ./scripts/build-version-variant.sh "${previous}" "${ART_IN}/previous" snap
	docker exec -w /src -e "FL_ARTIFACTS_DIR=${ART_IN}" -e "FL_PREVIOUS_ARTIFACTS_DIR=${ART_IN}/previous" \
		-e "FL_PREVIOUS_VERSION=${previous}" "${CONTAINER}" \
		/usr/local/bin/snap-entrypoint.sh ./scripts/packaging-e2e-install.sh snap
fi
