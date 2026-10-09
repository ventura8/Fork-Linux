#!/usr/bin/env bash
# build-bridge.sh - build and unit-test the native-git bridge of Fork for Linux (unofficial)
# with meson inside the CI image fork-linux-ci-bridge:26.04 (docker/Dockerfile.ci.bridge,
# built on demand), into build-bridge/ (gitignored via build-*/).
#
# usage: scripts/build-bridge.sh [--out DIR] [--repro] [--no-docker]
#   --out DIR    copy fl-shim.exe, fl-launch.exe and fl-bridge-helper into DIR afterwards
#   --repro      reproducibility check: two clean builds in fresh directories with the
#                same SOURCE_DATE_EPOCH must give byte-identical outputs (sha256)
#   --no-docker  run the toolchain directly (already inside the CI image or a host with
#                MinGW-w64, meson and ninja)
#
# Outputs (meson build dir build-bridge/): bridge/fl-shim.exe, bridge/fl-launch.exe,
# bridge/fl-bridge-helper, bridge/fl-bridge-helper-static (musl), and the test-only
# bridge/tests/fl-testdriver.exe + fl-testdrv.exe for the Wine tier
# (FL_REAL_WINE=1 python3 -m pytest tests/bridge/wine). The version comes from VERSION
# through the top-level meson.build. SOURCE_DATE_EPOCH defaults to the last commit time.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${HERE}/.." && pwd)"
IMAGE="${FL_BRIDGE_IMAGE:-fork-linux-ci-bridge:26.04}"
BUILD="build-bridge"
OUTPUTS=(bridge/fl-shim.exe bridge/fl-launch.exe bridge/fl-bridge-helper)
REPRO_OUTPUTS=("${OUTPUTS[@]}" bridge/fl-bridge-helper-static bridge/tests/fl-testdriver.exe
  bridge/tests/fl-testdrv.exe)

die() {
  echo "build-bridge.sh: $*" >&2
  exit 1
}

usage() {
  sed -n '6,11p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

OUT_DIR=""
REPRO=0
NO_DOCKER=0
INSIDE=0
while (($# > 0)); do
  case "$1" in
    --out)
      (($# >= 2)) || die "--out needs a directory"
      OUT_DIR="$2"
      shift 2
      ;;
    --repro)
      REPRO=1
      shift
      ;;
    --no-docker)
      NO_DOCKER=1
      shift
      ;;
    --inside)
      INSIDE=1
      shift
      ;;
    -h | --help)
      usage
      exit 0
      ;;
    *)
      usage >&2
      die "unknown argument: $1"
      ;;
  esac
done

if [[ -z "${SOURCE_DATE_EPOCH:-}" ]]; then
  SOURCE_DATE_EPOCH="$(git -C "${ROOT}" log -1 --format=%ct 2> /dev/null || true)"
  SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-1767225600}"
fi
[[ "${SOURCE_DATE_EPOCH}" =~ ^[0-9]+$ ]] || die "SOURCE_DATE_EPOCH is not a number: ${SOURCE_DATE_EPOCH}"
export SOURCE_DATE_EPOCH

# build_in <dir>: configure (or reconfigure) a release build, compile and run the unit tests.
build_in() {
  local dir="$1"
  if [[ -f "${dir}/build.ninja" ]]; then
    meson setup --reconfigure "${dir}" > /dev/null
  else
    meson setup "${dir}" --buildtype=release -Dbridge=enabled -Dtests=true > /dev/null
  fi
  meson compile -C "${dir}"
  meson test -C "${dir}" --suite bridge-unit --print-errorlogs
}

# The toolchain part: runs inside the container (or directly with --no-docker).
inside() {
  cd "${ROOT}"
  local tool
  for tool in meson ninja x86_64-w64-mingw32-gcc x86_64-w64-mingw32-windres file sha256sum; do
    command -v "${tool}" > /dev/null 2>&1 || die "${tool} not found"
  done
  echo "==> meson build in ${BUILD}/ (SOURCE_DATE_EPOCH=${SOURCE_DATE_EPOCH})"
  build_in "${BUILD}"
  echo "==> outputs"
  local f
  for f in "${REPRO_OUTPUTS[@]}"; do
    if [[ -e "${BUILD}/${f}" ]]; then
      printf '%-36s %s\n' "${f}" "$(file -b "${BUILD}/${f}" | cut -c1-100)"
    fi
  done
  (cd "${BUILD}" && sha256sum "${OUTPUTS[@]}")

  if ((REPRO)); then
    echo "==> reproducibility: two clean builds"
    local a="${BUILD}/repro-a" b="${BUILD}/repro-b" bad=0
    rm -rf "${a}" "${b}"
    build_in "${a}" > /dev/null
    build_in "${b}" > /dev/null
    for f in "${REPRO_OUTPUTS[@]}"; do
      [[ -e "${a}/${f}" ]] || continue
      local ha hb
      ha="$(sha256sum < "${a}/${f}" | cut -d' ' -f1)"
      hb="$(sha256sum < "${b}/${f}" | cut -d' ' -f1)"
      if [[ "${ha}" == "${hb}" ]]; then
        printf 'same     %s  %s\n' "${ha}" "${f}"
      else
        printf 'DIFFERS  %s / %s  %s\n' "${ha}" "${hb}" "${f}"
        bad=1
      fi
    done
    rm -rf "${a}" "${b}"
    ((bad == 0)) || die "the build is not reproducible"
    echo "reproducible: OK"
  fi
}

if ((INSIDE)); then
  # The container half: the outer invocation copies --out files and reports the result.
  inside
  exit 0
elif ((NO_DOCKER)); then
  inside
else
  command -v docker > /dev/null 2>&1 || die "docker not found (or use --no-docker)"
  if ! docker image inspect "${IMAGE}" > /dev/null 2>&1; then
    DOCKER_BUILDKIT=1 docker build -f "${ROOT}/docker/Dockerfile.ci.bridge" -t "${IMAGE}" "${ROOT}/docker"
  fi
  inner=(scripts/build-bridge.sh --inside)
  if ((REPRO)); then
    inner+=(--repro)
  fi
  docker run --rm -u "$(id -u):$(id -g)" -e "SOURCE_DATE_EPOCH=${SOURCE_DATE_EPOCH}" -e HOME=/tmp \
    -v "${ROOT}:/src" -w /src "${IMAGE}" "${inner[@]}"
fi

if [[ -n "${OUT_DIR}" ]]; then
  mkdir -p -- "${OUT_DIR}"
  for f in "${OUTPUTS[@]}"; do
    [[ -f "${ROOT}/${BUILD}/${f}" ]] || die "missing output ${BUILD}/${f}"
    cp -f -- "${ROOT}/${BUILD}/${f}" "${OUT_DIR}/"
  done
  echo "build-bridge.sh: copied ${OUTPUTS[*]##*/} -> ${OUT_DIR}"
fi
echo "build-bridge.sh: OK -> ${ROOT}/${BUILD}"
