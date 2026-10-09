# bridge/tests/meson.build - tests of the native-git bridge (option 'tests').
#
#   meson test --suite bridge-unit    native unit tests (TAP): bridge/common (protocol,
#                                     SHA-256/HMAC, sh quoting, translation vectors) and
#                                     the daemon's pure helpers
#   fl-testdriver.exe, fl-testdrv.exe test-only MinGW drivers for the Wine tier
#                                     (tests/bridge/wine/, tests/bridge/test_shims_wine.py);
#                                     console subsystem, never installed
#   fl-win-unit.exe                   test-only unit tests of bridge/win/fl_win.c under Wine
#                                     (tests/bridge/test_shims_wine_coverage.py)
#
# Sanitizers: configure with -Db_sanitize=address,undefined (bridge/tests/unit/run.sh
# does the same build by hand).

fl_unit_dir = meson.current_source_dir() / 'unit'
fl_vectors_dir = meson.current_source_dir() / 'vectors'
fl_unit_inc = include_directories('unit')

# Allocation-failure injection (unit/test_alloc.c): the code under test allocates through
# __wrap_malloc & co., so the out-of-memory paths run too.
fl_alloc_wrap = ['-Wl,--wrap=malloc', '-Wl,--wrap=calloc', '-Wl,--wrap=realloc']

fl_unit_tests = executable(
  'fl-unit-tests',
  files(
    'unit/test_alloc.c',
    'unit/test_main.c',
    'unit/test_proto.c',
    'unit/test_sha256.c',
    'unit/test_shquote.c',
    'unit/test_translate.c',
    'unit/test_translate_cov.c',
  ) + fl_proto_src + fl_sha256_src + fl_shquote_src + fl_translate_src,
  include_directories: [fl_common_inc, fl_unit_inc],
  c_args: fl_warn,
  link_args: fl_alloc_wrap,
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
      files('unit/' + fl_t + '.c', 'unit/test_alloc.c') + fl_helper_util_src + fl_proto_src + fl_sha256_src,
      include_directories: [fl_unix_inc, fl_unit_inc],
      c_args: fl_warn,
      link_args: fl_alloc_wrap,
      build_by_default: true,
    ),
    suite: 'bridge-unit',
    timeout: 120,
  )
endforeach

# The daemon's own code (unit/test_helper_daemon.c includes bridge/unix/fl_bridge_helper.c)
# with system-call and allocation failures injected through ld --wrap (unit/test_sys.c,
# unit/test_alloc.c). _FORTIFY_SOURCE would route read/recv to the __*_chk variants and
# around the wraps. Coverage builds also wrap _exit for the forked children (as the daemon).
fl_sys_wrap = []
foreach fl_fn : ['pipe2', 'fork', 'poll', 'recv', 'send', 'read', 'write', 'getrandom', 'waitpid', 'dup2',
                 'setsid', 'socket', 'accept4', 'opendir', 'chdir', 'getcwd']
  fl_sys_wrap += '-Wl,--wrap=' + fl_fn
endforeach
test(
  'test-helper-daemon',
  executable(
    'test_helper_daemon',
    files('unit/test_helper_daemon.c', 'unit/test_sys.c', 'unit/test_alloc.c') + fl_helper_util_src
      + fl_proto_src + fl_sha256_src + fl_helper_cov_sources,
    include_directories: [fl_unix_inc, fl_unit_inc],
    c_args: fl_warn + ['-U_FORTIFY_SOURCE'],
    link_args: fl_alloc_wrap + fl_sys_wrap + fl_helper_cov_ldflags,
    build_by_default: true,
  ),
  suite: 'bridge-unit',
  timeout: 120,
)

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
  # Unit tests of bridge/win/fl_win.c (bridge/win/tests/fl_win_unit.c), run under Wine
  # by tests/bridge/test_shims_wine_coverage.py. Allocation failures are injected by
  # wrapping malloc / calloc / realloc. Coverage builds compile it like the shims.
  fl_win_unit_exe = custom_target(
    'fl-win-unit.exe',
    input: files('../win/tests/fl_win_unit.c', '../win/fl_win.c') + fl_pe_common_sources
      + fl_pe_cov_sources,
    output: 'fl-win-unit.exe',
    depend_files: fl_common_hdrs + files('../win/fl_win.h'),
    command: [
      fl_mingw, fl_testdrv_cflags, fl_pe_cov_cflags, '-I' + fl_common_dir, '-I' + fl_win_dir,
      '-o', '@OUTPUT@', '@INPUT@', fl_pe_ldflags, fl_pe_libs,
      '-Wl,--wrap=malloc,--wrap=calloc,--wrap=realloc',
    ],
    build_by_default: true,
  )
endif
