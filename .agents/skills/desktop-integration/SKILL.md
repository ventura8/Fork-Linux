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
