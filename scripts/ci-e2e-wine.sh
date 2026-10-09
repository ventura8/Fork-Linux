#!/usr/bin/env bash
# ci-e2e-wine.sh - the real end-to-end tier of Fork for Linux (unofficial): FL_E2E_FORK=1
# tests/e2e (setup with the managed Wine, .NET and the official Fork installer, Fork's window
# under Xvfb, fork <repo>, doctor, snapshot / rollback, purge) in docker/Dockerfile.e2e.wine,
# as the invoking unprivileged user with a scratch HOME (never ~/.wine, never root).
#
# Downloads happen on this machine at run time (~2.5 GB); nothing downloaded is cached in
# CI or uploaded. Only logs are kept: logs/e2e-wine/ (screenshots show Fork's logo and stay
# in the scratch root, which is deleted afterwards).
#
# Usage: ./scripts/ci-e2e-wine.sh [pytest args...]
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

IMAGE="fork-linux-ci-e2e-wine:26.04"
LOG_DIR="${ROOT}/logs/e2e-wine"
SCRATCH="$(mktemp -d "${TMPDIR:-/tmp}/fl-e2e-wine.XXXXXX")"
trap 'rm -rf "${SCRATCH}"' EXIT
mkdir -p "${LOG_DIR}"

DOCKER_BUILDKIT=1 docker build -f docker/Dockerfile.e2e.wine -t "${IMAGE}" docker

status=0
docker run --rm \
	--user "$(id -u):$(id -g)" \
	-e HOME=/tmp/fl-home \
	-e FL_E2E_FORK=1 \
	-e FL_E2E_ROOT=/e2e \
	-e FL_E2E_FORK_VERSION \
	-e PYTHONDONTWRITEBYTECODE=1 \
	-v "${ROOT}:/src:ro" \
	-v "${SCRATCH}:/e2e:rw" \
	-w /src \
	"${IMAGE}" \
	python3 -m pytest -v -p no:cacheprovider --basetemp=/tmp/fl-home/pytest tests/e2e "$@" \
	2>&1 | tee "${LOG_DIR}/pytest.log" || status=$?

# Logs only: text files from the scratch root, never images, executables or archives.
find "${SCRATCH}" -type f \( -name '*.log' -o -name '*.json' -o -name '*.txt' \) -size -20M \
	-not -path '*/drive_c/*' -exec cp --parents -t "${LOG_DIR}" {} + 2>/dev/null || true
echo "==> e2e-wine logs: ${LOG_DIR}"
exit "${status}"
