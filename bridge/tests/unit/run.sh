#!/usr/bin/env bash
# run.sh - build and run the bridge/common native unit tests under ASan + UBSan.
#
# Usage: bridge/tests/unit/run.sh [BUILD_DIR]
#   BUILD_DIR defaults to build-bridge/unit (gitignored). The test binary prints TAP on
#   stdout and exits non-zero when a check fails; this script exits with its status.
# Environment: CC (default gcc; must support -fsanitize=address,undefined), ASAN_OPTIONS,
#   UBSAN_OPTIONS (defaults below are used when unset).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${HERE}/../../.." && pwd)"
OUT="${1:-${ROOT}/build-bridge/unit}"
CC="${CC:-gcc}"

CFLAGS=(
  -std=c11 -O1 -g
  -Wall -Wextra -Werror -Wconversion -Wshadow -Wstrict-prototypes -Wmissing-prototypes
  -Wformat=2 -Wvla -Wundef -Wpointer-arith
  "-fsanitize=address,undefined" -fno-sanitize-recover=all -fno-omit-frame-pointer
)
SOURCES=(
  "${HERE}/test_alloc.c"
  "${ROOT}/bridge/common/fl_proto.c"
  "${ROOT}/bridge/common/fl_sha256.c"
  "${ROOT}/bridge/common/fl_shquote.c"
  "${ROOT}/bridge/common/fl_translate.c"
  "${HERE}/test_main.c"
  "${HERE}/test_proto.c"
  "${HERE}/test_sha256.c"
  "${HERE}/test_shquote.c"
  "${HERE}/test_translate.c"
  "${HERE}/test_translate_cov.c"
)
# Allocation-failure injection (test_alloc.c wraps the allocator of the code under test).
LDFLAGS=("-Wl,--wrap=malloc" "-Wl,--wrap=calloc" "-Wl,--wrap=realloc")

mkdir -p -- "${OUT}"
"${CC}" "${CFLAGS[@]}" -I "${ROOT}/bridge/common" -I "${HERE}" -o "${OUT}/fl-unit-tests" "${SOURCES[@]}" "${LDFLAGS[@]}"

export ASAN_OPTIONS="${ASAN_OPTIONS:-abort_on_error=1:strict_string_checks=1:detect_stack_use_after_return=1}"
export UBSAN_OPTIONS="${UBSAN_OPTIONS:-print_stacktrace=1:halt_on_error=1}"
exec "${OUT}/fl-unit-tests" "${ROOT}/bridge/tests/vectors"
