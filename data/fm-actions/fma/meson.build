# data/fm-actions/fma/meson.build - the FileManager-Actions entry (Caja, Nautilus with FMA).
custom_target(
  'fl-fm-fma',
  output: 'fork-linux-open.desktop',
  command: [fl_python, fl_gen, 'fm-action', 'fma', '@OUTPUT@'],
  depend_files: fl_desktop_sources,
  install: true,
  install_dir: fl_datadir / 'file-manager' / 'actions',
  install_mode: 'rw-r--r--',
)
