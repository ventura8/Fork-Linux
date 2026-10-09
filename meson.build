# meson.build - top-level build of Fork for Linux (unofficial).
#
# Minimal for now: only the experimental native-git bridge (bridge/) is built here.
# The Python CLI, data files and packaging hooks are added by their own tasks.
# The version comes from the repository's VERSION file (single source of truth).
project(
  'fork-linux',
  'c',
  version: run_command('python3', 'scripts/read-version.py', check: true).stdout().strip(),
  license: 'MIT',
  meson_version: '>= 0.61.0',
  default_options: ['c_std=c11', 'warning_level=3', 'werror=true'],
)

subdir('bridge')
