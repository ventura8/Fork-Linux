# bridge/tests/meson.build - tests of the native-git bridge (option 'tests').
#
#   meson test --suite bridge-unit    native unit tests (TAP): bridge/common (protocol,
#                                     SHA-256/HMAC, sh quoting, translation vectors) and
#                                     the daemon's pure helpers
#   fl-testdriver.exe, fl-testdrv.exe test-only MinGW drivers for the Wine tier
#                                     (tests/bridge/wine/, tests/bridge/test_shims_wine.py);
#                                     console subsystem, never installed
#
# Sanitizers: configure with -Db_sanitize=address,undefined (bridge/tests/unit/run.sh
# does the same build by hand).

fl_unit_dir = meson.current_source_dir() / 'unit'
fl_vectors_dir = meson.current_source_dir() / 'vectors'
fl_unit_inc = include_directories('unit')

fl_unit_tests = executable(
  'fl-unit-tests',
  files(
    'unit/test_main.c',
    'unit/test_proto.c',
    'unit/test_sha256.c',
    'unit/test_shquote.c',
    'unit/test_translate.c',
  ) + fl_proto_src + fl_sha256_src + fl_shquote_src + fl_translate_src,
  include_directories: [fl_common_inc, fl_unit_inc],
  c_args: fl_warn,
  build_by_default: true,
)
test(
  'bridge-common',
  fl_unit_tests,
  args: [fl_vectors_dir],
  protocol: 'tap',
  suite: 'bridge-unit',
  timeout: 300,
)

foreach fl_t : ['test_helper_util', 'test_helper_paths']
  test(
    fl_t.replace('_', '-'),
    executable(
      fl_t,
      files('unit/' + fl_t + '.c') + fl_helper_util_src + fl_proto_src + fl_sha256_src,
      include_directories: fl_unix_inc,
      c_args: fl_warn,
      build_by_default: true,
    ),
    suite: 'bridge-unit',
    timeout: 120,
  )
endforeach

if fl_have_mingw
  # Console subsystem on purpose: run from a shell it prints its JSON on stdout.
  fl_testdrv_cflags = [
    '-std=c11', '-O2', '-municode', '-DUNICODE', '-D_UNICODE', '-D_WIN32_WINNT=0x0601',
    '-Wall', '-Wextra', '-Wpedantic', '-Werror', '-Wconversion', '-Wsign-conversion',
    '-Wshadow', '-Wstrict-prototypes', '-Wmissing-prototypes', '-fno-ident', fl_prefix_map,
  ]
  fl_testdriver_exe = custom_target(
    'fl-testdriver.exe',
    input: files('testdriver/fl_testdriver.c') + fl_sha256_src,
    output: 'fl-testdriver.exe',
    depend_files: files('../common/fl_sha256.h'),
    command: [
      fl_mingw, fl_testdrv_cflags, '-I' + fl_common_dir,
      '-o', '@OUTPUT@', '@INPUT@', fl_pe_ldflags,
    ],
    build_by_default: true,
  )
  # The shims task's driver (bridge/win/tests/), for tests/bridge/test_shims_wine.py
  # with FL_BRIDGE_WIN_DIR=<builddir>/bridge.
  fl_testdrv_exe = custom_target(
    'fl-testdrv.exe',
    input: files('../win/tests/fl_testdrv.c'),
    output: 'fl-testdrv.exe',
    command: [fl_mingw, fl_testdrv_cflags, '-o', '@OUTPUT@', '@INPUT@', fl_pe_ldflags],
    build_by_default: true,
  )
endif
