# bridge/meson.build - the experimental native-git bridge of Fork for Linux (unofficial).
#
# Included from the top-level meson.build with subdir('bridge'). Builds:
#
#   native (host compiler)
#     fl-bridge-helper          the launcher-started daemon (outside Wine; spike B2),
#                               installed to <prefix>/lib/fork-linux, with the persona
#                               symlinks fl-winexec / fl-askpass / fl-ssh-askpass
#     fl-bridge-helper-static   musl-gcc -static -no-pie (ET_EXEC) for the AppImage /
#                               tarball; built when musl-gcc exists, never installed
#   Windows (MinGW-w64, x86_64; option 'bridge')
#     fl-shim.exe, fl-launch.exe   GUI-subsystem PEs installed (mode 0644, Wine does not
#                               need +x) to <prefix>/lib/fork-linux/win64; the Python
#                               runtime copies them into the prefix (C:\fork-linux\)
#   tests (option 'tests'; bridge/tests/meson.build)
#     native unit tests registered with test() (suites bridge-unit), and the test-only
#     Wine drivers fl-testdriver.exe / fl-testdrv.exe (never installed)
#
# The PEs are built with custom_target()s that call the cross compiler directly, so the
# native project needs no cross file. Reproducible: no timestamps, no build ids, source
# paths remapped; scripts/build-bridge.sh --repro checks it. Design notes:
# docs/spikes/B1-wine-unix-spawn.md, docs/spikes/B2-rendezvous.md, bridge/win/README.md,
# bridge/unix/README.md, and bridge/README.md (the hand-off spec for the launcher).

fl_cc = meson.get_compiler('c')
fl_version = meson.project_version()
fl_version_parts = fl_version.split('.')
if fl_version_parts.length() != 3
  error('VERSION must be N.N.N, got "@0@"'.format(fl_version))
endif

fl_bridge_libdir = get_option('prefix') / 'lib' / 'fork-linux'
fl_bridge_windir = fl_bridge_libdir / 'win64'
fl_src_root = meson.project_source_root()
fl_common_dir = meson.current_source_dir() / 'common'
fl_unix_dir = meson.current_source_dir() / 'unix'
fl_win_dir = meson.current_source_dir() / 'win'

fl_common_inc = include_directories('common')
fl_unix_inc = include_directories('common', 'unix')

fl_proto_src = files('common/fl_proto.c')
fl_sha256_src = files('common/fl_sha256.c')
fl_shquote_src = files('common/fl_shquote.c')
fl_translate_src = files('common/fl_translate.c')
fl_common_hdrs = files(
  'common/fl_proto.h',
  'common/fl_sha256.h',
  'common/fl_shquote.h',
  'common/fl_translate.h',
)
fl_helper_util_src = files('unix/fl_helper_util.c')

# Extra warnings on top of the project's warning_level=3 (-Wall -Wextra -Wpedantic) and
# werror=true; the same gate as bridge/unix/build-dev.sh and bridge/win/build-dev.sh.
fl_warn = [
  '-Wconversion',
  '-Wsign-conversion',
  '-Wshadow',
  '-Wstrict-prototypes',
  '-Wmissing-prototypes',
  '-Wformat=2',
  '-Wvla',
  '-Wundef',
  '-Wpointer-arith',
]
fl_prefix_map = '-ffile-prefix-map=@0@=.'.format(fl_src_root)

# --------------------------------------------------------------------------
# native: fl-bridge-helper (+ personas)
# --------------------------------------------------------------------------

fl_helper_cargs = fl_warn + [fl_prefix_map, '-DFL_VERSION="@0@"'.format(fl_version)]
fl_helper_sources = files('unix/fl_bridge_helper.c') + fl_helper_util_src + fl_proto_src + fl_sha256_src

fl_bridge_helper = executable(
  'fl-bridge-helper',
  fl_helper_sources,
  include_directories: fl_unix_inc,
  c_args: fl_helper_cargs,
  link_args: fl_cc.get_supported_link_arguments('-Wl,-z,relro', '-Wl,-z,now'),
  pie: true,
  install: true,
  install_dir: fl_bridge_libdir,
)

foreach fl_persona : ['fl-winexec', 'fl-askpass', 'fl-ssh-askpass']
  install_symlink(
    fl_persona,
    pointing_to: 'fl-bridge-helper',
    install_dir: fl_bridge_libdir,
  )
endforeach

fl_musl_gcc = find_program('musl-gcc', required: false)
if fl_musl_gcc.found()
  fl_bridge_helper_static = custom_target(
    'fl-bridge-helper-static',
    input: fl_helper_sources,
    output: 'fl-bridge-helper-static',
    depend_files: fl_common_hdrs + files('unix/fl_helper_util.h'),
    command: [
      fl_musl_gcc,
      '-std=c11', '-O2', '-Wall', '-Wextra', '-Wpedantic', '-Werror', fl_helper_cargs,
      '-I' + fl_common_dir, '-I' + fl_unix_dir,
      '-static', '-no-pie', '-s', '-Wl,--build-id=none',
      '-o', '@OUTPUT@', '@INPUT@',
    ],
    build_by_default: true,
  )
else
  message('musl-gcc not found: fl-bridge-helper-static (portable ET_EXEC build) is skipped')
endif

# --------------------------------------------------------------------------
# Windows: fl-shim.exe, fl-launch.exe (MinGW-w64 cross compiler, no cross file)
# --------------------------------------------------------------------------

fl_mingw = find_program('x86_64-w64-mingw32-gcc', required: get_option('bridge'))
fl_windres = find_program('x86_64-w64-mingw32-windres', required: get_option('bridge'))
fl_have_mingw = fl_mingw.found() and fl_windres.found() and not get_option('bridge').disabled()

# Flags of the shims task (bridge/win/build-dev.sh). GUI subsystem (-mwindows): under
# CREATE_NO_WINDOW a console child costs Wine a conhost, ~5 ms per call (spike B2).
fl_pe_cflags = [
  '-std=c11', '-O2', '-municode', '-DUNICODE', '-D_UNICODE', '-D_WIN32_WINNT=0x0601',
  '-Wall', '-Wextra', '-Wpedantic', '-Werror',
  '-Wconversion', '-Wsign-conversion', '-Wshadow', '-Wstrict-prototypes', '-Wmissing-prototypes',
  '-ffunction-sections', '-fdata-sections', '-fno-ident', fl_prefix_map,
]
fl_pe_ldflags = [
  '-static', '-static-libgcc', '-s',
  '-Wl,--gc-sections,--no-insert-timestamp,--build-id=none',
]
fl_pe_libs = ['-lws2_32', '-lbcrypt']
fl_pe_common_sources = [fl_proto_src, fl_sha256_src, fl_shquote_src, fl_translate_src]

if fl_have_mingw
  fl_version_conf = configuration_data()
  fl_version_conf.set('FL_VERSION_MAJOR', fl_version_parts[0].to_int())
  fl_version_conf.set('FL_VERSION_MINOR', fl_version_parts[1].to_int())
  fl_version_conf.set('FL_VERSION_PATCH', fl_version_parts[2].to_int())
  fl_version_conf.set_quoted('FL_VERSION_STR', fl_version)
  fl_version_h = configure_file(output: 'fl_version.h', configuration: fl_version_conf)

  fl_pe_targets = {}
  foreach fl_pe : ['fl_shim', 'fl_launch']
    fl_res = custom_target(
      fl_pe + '_res',
      input: 'win/res/' + fl_pe + '.rc',
      output: fl_pe + '_res.o',
      depend_files: [fl_version_h, files('win/res/app.manifest')],
      command: [
        fl_windres, '--input-format=rc', '--output-format=coff',
        '-I', meson.current_build_dir(), '-I', fl_win_dir / 'res',
        '-i', '@INPUT@', '-o', '@OUTPUT@',
      ],
    )
    fl_pe_targets += {
      fl_pe: custom_target(
        fl_pe.replace('_', '-') + '.exe',
        input: [files('win/' + fl_pe + '.c', 'win/fl_win.c')] + fl_pe_common_sources + [fl_res],
        output: fl_pe.replace('_', '-') + '.exe',
        depend_files: fl_common_hdrs + files('win/fl_win.h'),
        command: [
          fl_mingw, fl_pe_cflags, '-mwindows',
          '-I' + fl_common_dir, '-I' + fl_win_dir,
          '-o', '@OUTPUT@', '@INPUT@',
          '-municode', '-mwindows', fl_pe_ldflags, fl_pe_libs,
        ],
        build_by_default: true,
        install: true,
        install_dir: fl_bridge_windir,
        install_mode: 'rw-r--r--',
      ),
    }
  endforeach
  fl_shim_exe = fl_pe_targets['fl_shim']
  fl_launch_exe = fl_pe_targets['fl_launch']
elif get_option('bridge').disabled()
  message('bridge=disabled: the Windows shims (fl-shim.exe, fl-launch.exe) are not built')
else
  message('x86_64-w64-mingw32-gcc / -windres not found: skipping the Windows shims '
    + '(fl-shim.exe, fl-launch.exe); build them with scripts/build-bridge.sh')
endif

if get_option('tests')
  subdir('tests')
endif
