#!/usr/bin/env bash
# build.sh - build the B1/B2 bridge spike binaries into bridge/spike/out/.
#
# On the host it re-runs itself inside fork-linux-ci-bridge:26.04
# (docker/Dockerfile.ci.bridge, built on demand) as the calling user; inside the
# container (--inside) it compiles:
#   Windows (MinGW-w64): probe-{con,gui}.exe  rv-{con,gui}.exe  noop-{con,gui}.exe  drv.exe
#   Linux targets (B1):  target-script.sh  target-dyn-pie  target-static-nopie  target-static-pie
#                        target-static-high (static ET_EXEC linked at HIGH_BASE)
#   Linux helper (B2):   rv-helper       musl-gcc -static -no-pie (the plan's helper)
#                        rv-helper-high  same, linked at HIGH_BASE (Wine-staging seccomp workaround)
#                        rv-helper-dyn   glibc dynamic PIE
# All C is built with -std=c11 -O2 -Wall -Wextra -Werror.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${HERE}/../.." && pwd)"
IMAGE="${FL_BRIDGE_IMAGE:-fork-linux-ci-bridge:26.04}"

if [[ "${1:-}" != "--inside" ]]; then
  if ! docker image inspect "${IMAGE}" > /dev/null 2>&1; then
    DOCKER_BUILDKIT=1 docker build -f "${ROOT}/docker/Dockerfile.ci.bridge" -t "${IMAGE}" "${ROOT}/docker"
  fi
  exec docker run --rm -u "$(id -u):$(id -g)" -v "${ROOT}:/src" -w /src "${IMAGE}" \
    bridge/spike/build.sh --inside
fi

OUT="${HERE}/out"
mkdir -p "${OUT}"
export SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-1767225600}"

CSTD=(-std=c11 -O2 -Wall -Wextra -Werror)
PE_CC="x86_64-w64-mingw32-gcc"
PE_FLAGS=("${CSTD[@]}" -municode -static -static-libgcc -s "-Wl,--no-insert-timestamp,--build-id=none")
PE_LIBS=(-lws2_32 -lbcrypt)
MUSL_CC="musl-gcc"
if ! command -v "${MUSL_CC}" > /dev/null 2>&1; then
  echo "build.sh: musl-gcc not found" >&2
  exit 1
fi
MUSL_LIB=""
for d in /usr/lib/x86_64-linux-musl /usr/lib/musl/lib /usr/local/musl/lib; do
  if [[ -f "${d}/crt1.o" ]]; then
    MUSL_LIB="${d}"
    break
  fi
done
[[ -n "${MUSL_LIB}" ]] || {
  echo "build.sh: musl crt1.o not found" >&2
  exit 1
}
# Wine-staging's seccomp filter traps syscalls issued below ~0x7e00'0000'0000 (see
# docs/spikes/B1-wine-unix-spawn.md); an ET_EXEC linked here issues them from above.
HIGH_BASE="0x7e0000000000"

high_static() { # high_static <out> <src> [cflags...]: musl static ET_EXEC at HIGH_BASE
  local out="$1" src="$2"
  shift 2
  # musl's crt1.o does "lea _DYNAMIC(%rip)"; the weak undefined symbol (0) is out of
  # PC32 range from HIGH_BASE, so pin it to the text base (crt1 ignores the value).
  "${MUSL_CC}" "${CSTD[@]}" -static -no-pie -nostartfiles "$@" \
    "${MUSL_LIB}/crt1.o" "${MUSL_LIB}/crti.o" \
    "-Wl,-Ttext-segment=${HIGH_BASE}" "-Wl,--defsym=_DYNAMIC=${HIGH_BASE}" \
    -o "${OUT}/${out}" "${HERE}/${src}" "${MUSL_LIB}/crtn.o"
}

pe() { # pe <out> <subsystem: console|windows> <src>
  "${PE_CC}" "${PE_FLAGS[@]}" "-m$2" -o "${OUT}/$1" "${HERE}/$3" "${PE_LIBS[@]}"
}

echo "==> Windows side (MinGW-w64 $("${PE_CC}" -dumpversion))"
pe probe-con.exe console probe.c
pe probe-gui.exe windows probe.c
pe rv-con.exe console rv.c
pe rv-gui.exe windows rv.c
pe noop-con.exe console noop.c
pe noop-gui.exe windows noop.c
pe drv.exe console drv.c

echo "==> Linux targets (B1)"
install -m 0755 "${HERE}/target.sh" "${OUT}/target-script.sh"
gcc "${CSTD[@]}" -fPIE -pie -DTARGET_VARIANT='"dyn-pie"' -o "${OUT}/target-dyn-pie" "${HERE}/target.c"
"${MUSL_CC}" "${CSTD[@]}" -static -no-pie -DTARGET_VARIANT='"static-nopie"' \
  -o "${OUT}/target-static-nopie" "${HERE}/target.c"
"${MUSL_CC}" "${CSTD[@]}" -static-pie -fPIE -DTARGET_VARIANT='"static-pie"' \
  -o "${OUT}/target-static-pie" "${HERE}/target.c"
high_static target-static-high target.c -DTARGET_VARIANT='"static-high"'

echo "==> Linux helper (B2)"
"${MUSL_CC}" "${CSTD[@]}" -static -no-pie -s -o "${OUT}/rv-helper" "${HERE}/rv-helper.c"
high_static rv-helper-high rv-helper.c -s
gcc "${CSTD[@]}" -fPIE -pie -s -o "${OUT}/rv-helper-dyn" "${HERE}/rv-helper.c"

echo "==> Outputs"
(
  cd "${OUT}"
  for f in *.exe target-* rv-helper*; do
    printf '%-22s %s\n' "${f}" "$(file -b "${f}" | cut -c1-110)"
  done
  sha256sum -- *.exe target-* rv-helper* > SHA256SUMS
)
echo "build.sh: OK -> ${OUT}"
