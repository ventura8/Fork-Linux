# meson.build - top-level build of Fork for Linux (unofficial).
#
# Builds and installs (layout: AGENTS.md §3, bridge/README.md "Install layout"):
#   <bindir>/fork-linux, <bindir>/fork -> fork-linux     relocatable launcher (#!@PYTHON@ -I)
#   <datadir>/fork-linux/fork_linux/                     the Python package (+ generated _build.py)
#   <prefix>/lib/fork-linux/                             host helpers (fork-linux-host, -open-url,
#                                                        -explorer, -terminal, -handoff) and the
#                                                        bridge (fl-bridge-helper + personas, win64/)
#   <datadir>/applications, metainfo, icons/hicolor      menu entry, AppStream, our neutral icons
#   <mandir>/man1, bash / zsh / fish completions
#   optional system-wide file-manager actions (-Dfile_manager_actions=true)
# The version comes from the repository's VERSION file (single source of truth); the app id
# and the menu entry / metainfo / man / completion contents come from the Python package
# through scripts/gen-data.py, so nothing here retypes them.
project(
  'fork-linux',
  'c',
  version: run_command('python3', 'scripts/read-version.py', check: true).stdout().strip(),
  license: 'MIT',
  meson_version: '>= 0.61.0',
  default_options: ['c_std=c11', 'warning_level=3', 'werror=true'],
)

fl_python = find_program('python3')
fl_gen = files('scripts/gen-data.py')
fl_shebang = get_option('python')
if fl_shebang == ''
  fl_shebang = fl_python.full_path()
endif
fl_app_id = run_command(fl_python, fl_gen, 'name', 'APP_ID', check: true).stdout().strip()

fl_prefix = get_option('prefix')
fl_bindir = get_option('bindir')
fl_datadir = get_option('datadir')
fl_pkgdatadir = fl_datadir / 'fork-linux'
fl_libexecdir = fl_prefix / 'lib' / 'fork-linux'

# ---------------------------------------------------------------------------
# launchers
# ---------------------------------------------------------------------------

fl_launcher_conf = configuration_data()
fl_launcher_conf.set('PYTHON', fl_shebang)

configure_file(
  input: 'bin/fork-linux.in',
  output: 'fork-linux',
  configuration: fl_launcher_conf,
  install: true,
  install_dir: fl_bindir,
  install_mode: 'rwxr-xr-x',
)
if get_option('fork_alias')
  install_symlink('fork', pointing_to: 'fork-linux', install_dir: fl_bindir)
endif

# ---------------------------------------------------------------------------
# the Python package
# ---------------------------------------------------------------------------

install_subdir(
  'src/fork_linux',
  install_dir: fl_pkgdatadir,
  exclude_directories: [
    '__pycache__',
    '.ruff_cache',
    '.mypy_cache',
    'commands/__pycache__',
    'steps/__pycache__',
    'data/__pycache__',
    'data/templates',
  ],
  exclude_files: ['_build.py'],
)
# The desktop / file-manager templates; the Nautilus script template keeps its shebang, so it
# is installed executable like the script `fork-linux desktop install` renders from it.
fl_templates = 'src/fork_linux/data/templates'
install_data(
  fl_templates / 'io.github.ventura8.ForkLinux.desktop.in',
  fl_templates / 'dolphin-servicemenu.desktop.in',
  fl_templates / 'fma-action.desktop.in',
  fl_templates / 'fork_linux_nautilus.py.in',
  fl_templates / 'nemo_action.in',
  fl_templates / 'thunar-uca-action.xml.in',
  install_dir: fl_pkgdatadir / 'fork_linux' / 'data' / 'templates',
  install_mode: 'rw-r--r--',
)
install_data(
  fl_templates / 'nautilus-script.in',
  install_dir: fl_pkgdatadir / 'fork_linux' / 'data' / 'templates',
  install_mode: 'rwxr-xr-x',
)

custom_target(
  'fl-build-info',
  output: '_build.py',
  command: [fl_python, fl_gen, 'build-info', meson.project_version(), get_option('flavor'), '@OUTPUT@'],
  install: true,
  install_dir: fl_pkgdatadir / 'fork_linux',
)

# ---------------------------------------------------------------------------
# host helpers (Fork's ShellTool, App Paths explorer, winebrowser target, hand-off)
# ---------------------------------------------------------------------------

configure_file(
  input: 'libexec/fork-linux-host.in',
  output: 'fork-linux-host',
  configuration: fl_launcher_conf,
  install: true,
  install_dir: fl_libexecdir,
  install_mode: 'rwxr-xr-x',
)
install_data(
  'libexec/fork-linux-open-url',
  'libexec/fork-linux-explorer',
  'libexec/fork-linux-terminal',
  'libexec/fork-linux-handoff',
  install_dir: fl_libexecdir,
  install_mode: 'rwxr-xr-x',
)

# ---------------------------------------------------------------------------
# the experimental native-git bridge (bridge/meson.build)
# ---------------------------------------------------------------------------

subdir('bridge')

fl_prebuilt = get_option('bridge_prebuilt_dir')
if not fl_have_mingw and fl_prebuilt != '' and not get_option('bridge').disabled()
  # Distros without MinGW-w64 (openSUSE Leap): ship shims built elsewhere from this tree.
  install_data(
    fl_prebuilt / 'fl-shim.exe',
    fl_prebuilt / 'fl-launch.exe',
    install_dir: fl_bridge_windir,
    install_mode: 'rw-r--r--',
  )
endif

# ---------------------------------------------------------------------------
# desktop integration, AppStream, manual pages, completions
# ---------------------------------------------------------------------------

fl_desktop_sources = files(
  'src/fork_linux/data/templates/io.github.ventura8.ForkLinux.desktop.in',
  'src/fork_linux/credits.py',
  'src/fork_linux/desktop_integration.py',
  'src/fork_linux/__init__.py',
)

if get_option('desktop')
  fl_desktop_file = custom_target(
    'fl-desktop',
    output: fl_app_id + '.desktop',
    command: [fl_python, fl_gen, 'desktop', '@OUTPUT@'],
    depend_files: fl_desktop_sources,
    install: true,
    install_dir: fl_datadir / 'applications',
  )
  fl_metainfo_file = custom_target(
    'fl-metainfo',
    input: 'data/' + fl_app_id + '.metainfo.xml.in',
    output: fl_app_id + '.metainfo.xml',
    command: [fl_python, fl_gen, 'metainfo', '@INPUT@', '@OUTPUT@'],
    depend_files: files('src/fork_linux/credits.py', 'src/fork_linux/__init__.py'),
    install: true,
    install_dir: fl_datadir / 'metainfo',
  )
  install_subdir('data/icons/hicolor', install_dir: fl_datadir / 'icons')
endif

install_man('data/man/fork-linux.1', 'data/man/fork.1')

fl_bash_dir = fl_datadir / 'bash-completion' / 'completions'
fl_fish_dir = fl_datadir / 'fish' / 'vendor_completions.d'
install_data('data/completions/fork-linux.bash', rename: 'fork-linux', install_dir: fl_bash_dir)
install_data('data/completions/_fork-linux', install_dir: fl_datadir / 'zsh' / 'site-functions')
install_data('data/completions/fork-linux.fish', install_dir: fl_fish_dir)
if get_option('fork_alias')
  # bash-completion and fish load completions by command name.
  install_symlink('fork', pointing_to: 'fork-linux', install_dir: fl_bash_dir)
  install_symlink('fork.fish', pointing_to: 'fork-linux.fish', install_dir: fl_fish_dir)
endif

if get_option('file_manager_actions')
  # Same file names as the per-user actions, so a user's own copy shadows the system one.
  subdir('data/fm-actions')
endif

# ---------------------------------------------------------------------------
# build-time checks (meson test)
# ---------------------------------------------------------------------------

# The data checks are cheap and always registered; option 'tests' only gates the C unit tests.
test('gen-data-check', fl_python, args: [fl_gen, 'check'], suite: 'data')
if get_option('desktop')
  fl_dfv = find_program('desktop-file-validate', required: false)
  if fl_dfv.found()
    test('desktop-file-validate', fl_dfv, args: [fl_desktop_file], suite: 'data')
  endif
  fl_appstreamcli = find_program('appstreamcli', required: false)
  if fl_appstreamcli.found()
    test('appstreamcli-validate', fl_appstreamcli, args: ['validate', '--no-net', fl_metainfo_file], suite: 'data')
  endif
endif
