#!/usr/bin/env bash
# build-dev.sh - developer build of the native side of the Fork for Linux (unofficial)
# git bridge into build-bridge/unix/ (gitignored):
#   fl-bridge-helper          glibc dynamic PIE (the distro build)
#   fl-bridge-helper-static   musl-gcc -static -no-pie, ET_EXEC (AppImage / tarball)
#   fl-winexec, fl-askpass, fl-ssh-askpass -> fl-bridge-helper (persona symlinks)
#
# usage: bridge/unix/build-dev.sh [--test]
#   --test  also build and run the native unit tests (bridge/tests/unit/test_helper_*.c)
#           under ASan/UBSan and smoke-test both binaries
#
# On the host it re-runs itself inside fork-linux-ci-bridge:26.04
# (docker/Dockerfile.ci.bridge, built on demand) as the calling user. The version
# comes from the repository's VERSION file (never a hard-coded literal). The protocol
# suite runs on the host: FL_BRIDGE_HELPER_BIN=build-bridge/unix/fl-bridge-helper-static \
#   python3 -m pytest -q tests/bridge/test_daemon_protocol.py
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${HERE}/../.." && pwd)"
IMAGE="${FL_BRIDGE_IMAGE:-fork-linux-ci-bridge:26.04}"

if [[ "${1:-}" != "--inside" ]]; then
  if ! docker image inspect "${IMAGE}" > /dev/null 2>&1; then
    DOCKER_BUILDKIT=1 docker build -f "${ROOT}/docker/Dockerfile.ci.bridge" -t "${IMAGE}" "${ROOT}/docker"
  fi
  exec docker run --rm -u "$(id -u):$(id -g)" -v "${ROOT}:/src" -w /src "${IMAGE}" \
    bridge/unix/build-dev.sh --inside "$@"
fi
shift

RUN_TESTS=0
for arg in "$@"; do
  case "${arg}" in
    --test) RUN_TESTS=1 ;;
    *)
      echo "build-dev.sh: unknown argument: ${arg}" >&2
      exit 2
      ;;
  esac
done

die() {
  echo "build-dev.sh: $*" >&2
  exit 1
}

OUT="${ROOT}/build-bridge/unix"
COMMON="${ROOT}/bridge/common"
export SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-1767225600}"

command -v gcc > /dev/null 2>&1 || die "gcc not found"
command -v musl-gcc > /dev/null 2>&1 || die "musl-gcc not found (musl-tools)"
[[ -f "${COMMON}/fl_proto.c" && -f "${COMMON}/fl_sha256.c" ]] || die "bridge/common is missing"

VERSION_TEXT="$(tr -d '[:space:]' < "${ROOT}/VERSION")"
[[ "${VERSION_TEXT}" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "VERSION is not N.N.N: '${VERSION_TEXT}'"

CFLAGS=(-std=c11 -O2 -Wall -Wextra -Werror -Wconversion -Wsign-conversion -Wshadow
  -Wstrict-prototypes -Wmissing-prototypes -Wformat=2 -Wvla -Wundef -Wpointer-arith
  "-DFL_VERSION=\"${VERSION_TEXT}\"" "-ffile-prefix-map=${ROOT}=."
  "-I${HERE}" "-I${COMMON}")
SRC=("${HERE}/fl_bridge_helper.c" "${HERE}/fl_helper_util.c" "${COMMON}/fl_proto.c" "${COMMON}/fl_sha256.c")

# elf_type <file>: prints ET_EXEC / ET_DYN from the ELF header (e_type at offset 16).
elf_type() {
  local file="$1"
  python3 -I -c '
import struct, sys
with open(sys.argv[1], "rb") as fh:
    head = fh.read(18)
assert head[:4] == b"\x7fELF", "not an ELF file"
print({2: "ET_EXEC", 3: "ET_DYN"}.get(struct.unpack("<H", head[16:18])[0], "other"))
' "${file}"
}

mkdir -p "${OUT}"
echo "==> fl-bridge-helper (gcc $(gcc -dumpversion), dynamic PIE)"
gcc "${CFLAGS[@]}" -fPIE -pie -s -Wl,-z,relro,-z,now,--build-id=none -o "${OUT}/fl-bridge-helper" "${SRC[@]}"
echo "==> fl-bridge-helper-static (musl-gcc, -static -no-pie)"
musl-gcc "${CFLAGS[@]}" -static -no-pie -s -Wl,--build-id=none -o "${OUT}/fl-bridge-helper-static" "${SRC[@]}"
for persona in fl-winexec fl-askpass fl-ssh-askpass; do
  ln -sfn fl-bridge-helper "${OUT}/${persona}"
done

[[ "$(elf_type "${OUT}/fl-bridge-helper")" == "ET_DYN" ]] || die "fl-bridge-helper is not a PIE"
[[ "$(elf_type "${OUT}/fl-bridge-helper-static")" == "ET_EXEC" ]] || die "fl-bridge-helper-static is not ET_EXEC"

if ((RUN_TESTS)); then
  echo "==> unit tests (ASan/UBSan)"
  for name in test_helper_util test_helper_paths; do
    gcc -std=c11 -O1 -g -Wall -Wextra -Werror -Wconversion -Wsign-conversion -Wshadow -Wformat=2 \
      -fsanitize=address,undefined \
      -fno-sanitize-recover=all "-I${HERE}" "-I${COMMON}" -o "${OUT}/${name}" \
      "${ROOT}/bridge/tests/unit/${name}.c" "${HERE}/fl_helper_util.c" "${COMMON}/fl_proto.c" \
      "${COMMON}/fl_sha256.c"
    "${OUT}/${name}"
  done
  echo "==> smoke"
  for bin in fl-bridge-helper fl-bridge-helper-static; do
    "${OUT}/${bin}" --version
  done
fi

echo "==> outputs"
(
  cd "${OUT}"
  for f in fl-bridge-helper fl-bridge-helper-static; do
    printf '%-26s %s\n' "${f}" "$(file -b "${f}" | cut -c1-100)"
  done
  sha256sum fl-bridge-helper fl-bridge-helper-static
)
echo "build-dev.sh: OK -> ${OUT}"
