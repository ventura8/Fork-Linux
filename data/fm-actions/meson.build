# data/fm-actions/meson.build - system-wide "Open in Fork" actions (-Dfile_manager_actions=true).
#
# Rendered by scripts/gen-data.py fm-action from the per-user templates in
# src/fork_linux/data/templates (the single source). The file names match the per-user
# ones, so `fork-linux desktop install` copies in ~/.local/share shadow these.
# FMA's file has the same name as Dolphin's, so it is built in its own subdirectory.
fl_fm_actions = {
  'nautilus': [fl_datadir / 'nautilus-python' / 'extensions', 'fork_linux_nautilus.py', 'rw-r--r--'],
  'nemo': [fl_datadir / 'nemo' / 'actions', 'fork-linux-open.nemo_action', 'rw-r--r--'],
  'dolphin': [fl_datadir / 'kio' / 'servicemenus', 'fork-linux-open.desktop', 'rwxr-xr-x'],
}
foreach fl_kind, fl_spec : fl_fm_actions
  custom_target(
    'fl-fm-' + fl_kind,
    output: fl_spec[1],
    command: [fl_python, fl_gen, 'fm-action', fl_kind, '@OUTPUT@'],
    depend_files: fl_desktop_sources,
    install: true,
    install_dir: fl_spec[0],
    install_mode: fl_spec[2],
  )
endforeach

subdir('fma')
