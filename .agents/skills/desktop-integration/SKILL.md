---
name: desktop-integration
description: .desktop, metainfo, icon extraction, file-manager Open in Fork.
---

# Desktop Integration

.desktop, metainfo, icon extraction, file-manager Open in Fork.

Canonical rules: [AGENTS.md](../../../AGENTS.md).

- Single sources: `src/fork_linux/data/templates/*.in` (menu entry and file-manager actions) and `credits.py`.
  `scripts/gen-data.py` renders the system desktop entry (identical to the per-user one for `fork-linux` on PATH),
  the metainfo (`data/io.github.ventura8.ForkLinux.metainfo.xml.in`) and the optional system-wide actions.
- Validate: `meson test -C build --suite data` (desktop-file-validate, appstreamcli validate --no-net).
- Our neutral SVG icons only (`data/icons/hicolor/`); Fork's icon is extracted at runtime, never committed (hard rule 4).
  `icon_extract` writes every hicolor size below Fork's largest image (shrunk by `imaging.downscale_rgba`) and a per-user
  scalable SVG; until Fork is installed the placeholder is copied to the user's scalable icon (not when a package ships it).
- `desktop install --file-managers` defaults to `auto` (`installed_file_managers`: program on `PATH` or menu entry);
  with `auto`, recorded actions of a file manager that is gone are removed. Tests pin `XDG_CONFIG_DIRS` and `PATH`.
- Thunar: a missing `~/.config/Thunar/uca.xml` is seeded from the first `Thunar/uca.xml` in `$XDG_CONFIG_DIRS`
  (registry fields `seed`, `seed_sha256`); removal deletes it only when it is back to that seed.
  It is written only when its content changes; Thunar reads it only when it starts, so a change while a
  Thunar process runs logs `THUNAR_RESTART` ("run 'thunar -q'"); `procs.running` finds the process.
- AppImage: the menu entry, actions and links start the AppImage file (`FORK_LINUX_APPIMAGE`); the `desktop_entry` step
  re-runs when that path changes (the file was moved) and `install(refresh_recorded=True)` re-points every recorded
  action and link. Helper paths Wine and Fork keep go through `helpers.path` (stable launchers, AppRun `--libexec`).
- `uninstall` / `desktop remove` clear the `icon` and `desktop_entry` markers (`bootstrap.forget_markers`), so the next
  launch after a reinstall puts Fork's icon and the menu entry back.
- Nautilus: the extension requires the newest Nautilus API it can (4.1 in Nautilus 50, 4.0, 3.0), else the one
  Nautilus already loaded; Nautilus loads extensions only when it starts, so writing a changed extension or removing it
  while a `nautilus` process runs logs `NAUTILUS_RESTART` ("run 'nautilus -q'"). Nemo needs two actions: `nemo`
  (`Selection=s`, the selected item) and `nemo-folder` (`Selection=none`, `%P`, the folder a window shows). Caja and
  PCManFM(-Qt) share the FileManager-Actions entry (`fma`); Caja-actions, libfm and Nemo watch their folders (no restart).
- Menu: Cinnamon's CMenu and MATE's libmate-menu watch only the application folders that existed at login (GNOME Shell,
  Budgie, garcon and KSycoca notice a new one); a menu entry that had to create `$XDG_DATA_HOME/applications` on
  `X-Cinnamon` / `MATE` logs `MENU_RELOGIN` ("log out and back in", `MENU_AT_LOGIN_DESKTOPS`).
- Host helper: `open` of a folder asks `FileManager1.ShowFolders` before `xdg-open` (the session bus starts the file
  manager outside Wine's process tree); `terminal` asks the desktop's own setting first (`DESKTOP_TERMINAL_SCHEMAS`:
  Cinnamon's Preferred Applications, Budgie's `org.gnome.desktop.default-applications.terminal`), then
  xdg-terminal-exec, `$TERMINAL` and the known terminals with the desktop's own first (`DESKTOP_TERMINALS`, LXQt:
  qterminal).
- Theme (`theme.py`): Cinnamon's `org.x.apps.portal color-scheme`, KDE's window background colour (grey < 192 is dark,
  as Plasma's portal), known dark names without "dark" (`DARK_THEMES`: BlackMATE, HighContrastInverse), LXQt's
  Kvantum theme / widget palette over `lxqt.conf` merged from `$XDG_CONFIG_DIRS`. Scale (`display.py`): GNOME and
  Cinnamon read `Xft.dpi` before GNOME's keys. Every desktop is checked end to end in `docs/qa/DE-E2E.md`.
