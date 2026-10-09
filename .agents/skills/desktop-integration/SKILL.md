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
