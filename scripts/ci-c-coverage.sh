#!/usr/bin/env bash
# ci-c-coverage.sh - C coverage of the native-git bridge for SonarQube Cloud.
#
# usage: scripts/ci-c-coverage.sh [--no-docker] [--no-wine]
#   --no-docker  run the toolchain directly (already inside fork-linux-ci-bridge:26.04, or
#                a host with gcc/gcov, MinGW-w64 (+ its gcov), meson, ninja, Wine, pytest)
#   --no-wine    skip the Wine tier (bridge/win then has no coverage: the report says so)
#
# Writes artifacts/coverage/c-coverage.xml (SonarQube generic coverage format, paths
# relative to the repository root) from a -Db_coverage=true meson build in build-cov/:
#
#   bridge/common, bridge/unix  native unit tests (meson test --suite bridge-unit) and the
#                               daemon protocol tests (tests/bridge/test_daemon_protocol.py,
#                               test_corpus.py) against the coverage build of
#                               fl-bridge-helper (FL_BRIDGE_HELPER_BIN)
#   bridge/win (+ common)       the Wine tier (FL_REAL_WINE=1 tests/bridge/test_shims_wine.py,
#                               test_shims_wine_coverage.py and tests/bridge/wine) against
#                               MinGW --coverage builds of fl-shim.exe / fl-launch.exe /
#                               tests/fl-win-unit.exe; libgcov writes the .gcda files
#                               through Wine's Z: drive, x86_64-w64-mingw32-gcov reads them
#
# The coverage builds link two test-only files from bridge/tests/coverage/ that write
# the counters before _exit() / ExitProcess() (see their headers and bridge/meson.build);
# release builds never contain them. The report is merged by scripts/gcov-sonar.py.
# Called by scripts/ci-sonar.sh when FL_SONAR_COVERAGE=1.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${HERE}/.." && pwd)"
IMAGE="${FL_BRIDGE_IMAGE:-fork-linux-ci-bridge:26.04}"
BUILD="build-cov"
OUT="artifacts/coverage/c-coverage.xml"

die() {
  echo "ci-c-coverage.sh: $*" >&2
  exit 1
}

NO_DOCKER=0
WINE=1
for arg in "$@"; do
  case "${arg}" in
    --no-docker) NO_DOCKER=1 ;;
    --no-wine) WINE=0 ;;
    -h | --help)
      sed -n '4,7p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *) die "unknown argument: ${arg}" ;;
  esac
done

inside() {
  cd "${ROOT}"
  local tool
  for tool in meson ninja gcov python3; do
    command -v "${tool}" > /dev/null 2>&1 || die "${tool} not found"
  done
  if [[ ! -f "${BUILD}/build.ninja" ]]; then
    meson setup "${BUILD}" --buildtype=debug -Db_coverage=true -Dbridge=enabled -Dtests=true > /dev/null
  fi
  find "${BUILD}" -name '*.gcda' -delete
  echo "==> coverage build in ${BUILD}/"
  meson compile -C "${BUILD}"
  echo "==> native unit tests"
  meson test -C "${BUILD}" --suite bridge-unit --print-errorlogs --no-rebuild
  export FL_BRIDGE_HELPER_BIN="${ROOT}/${BUILD}/bridge/fl-bridge-helper"
  echo "==> daemon protocol tests"
  python3 -m pytest -q -p no:cacheprovider tests/bridge/test_daemon_protocol.py tests/bridge/test_corpus.py

  local -a gcov=(
    --gcov "gcov=${BUILD}/bridge/fl-bridge-helper.p/*.gcda"
    --gcov "gcov=${BUILD}/bridge/tests/*.p/*.gcda"
  )
  if ((WINE)); then
    command -v x86_64-w64-mingw32-gcov > /dev/null 2>&1 || die "x86_64-w64-mingw32-gcov not found"
    echo "==> Wine tier (FL_REAL_WINE=1) against the coverage PEs"
    FL_REAL_WINE=1 FL_BRIDGE_WIN_DIR="${ROOT}/${BUILD}/bridge" FL_BRIDGE_BUILD="${ROOT}/${BUILD}/bridge" \
      python3 -m pytest -q -p no:cacheprovider tests/bridge/test_shims_wine.py \
      tests/bridge/test_shims_wine_coverage.py tests/bridge/wine
    gcov+=(--gcov "x86_64-w64-mingw32-gcov=${BUILD}/bridge/*.gcda")
    gcov+=(--gcov "x86_64-w64-mingw32-gcov=${BUILD}/bridge/tests/*.gcda")
  else
    echo "==> note: --no-wine: bridge/win gets no coverage data"
  fi
  echo "==> ${OUT}"
  python3 scripts/gcov-sonar.py --root "${ROOT}" --out "${OUT}" --exclude bridge/tests/ --exclude bridge/win/tests/ "${gcov[@]}"
}

if ((NO_DOCKER)); then
  inside
  exit 0
fi

command -v docker > /dev/null 2>&1 || die "docker not found (or use --no-docker)"
if ! docker image inspect "${IMAGE}" > /dev/null 2>&1; then
  echo "==> building ${IMAGE}"
  DOCKER_BUILDKIT=1 docker build -f "${ROOT}/docker/Dockerfile.ci.bridge" -t "${IMAGE}" "${ROOT}/docker"
fi
args=(--no-docker)
((WINE)) || args+=(--no-wine)
# Same absolute path inside as outside: the .gcda paths are compiled in, and Wine
# reaches them through its Z: drive. Unprivileged (never run Wine as root), own HOME.
docker run --rm \
  --user "$(id -u):$(id -g)" \
  --env HOME=/tmp/h \
  --env PYTHONDONTWRITEBYTECODE=1 \
  --volume "${ROOT}:${ROOT}" \
  --workdir "${ROOT}" \
  "${IMAGE}" "${ROOT}/scripts/ci-c-coverage.sh" "${args[@]}"
