# Desktop E2E tier

fork-linux is installed the way a user installs it (the release tarball through `install.sh
--from-tarball`) inside a real desktop session in a container, and every desktop-facing feature
is checked by asking that desktop's **own** programs: its menu library, its file manager, its
settings daemon, its terminal and its default applications. A second tier sets up the official
Fork with the managed Wine and drives it under the desktop's window manager.

Containers only (AGENTS.md hard rule 18): `FL_E2E_DE` / `FL_E2E_DE_FORK` make pytest exit at
once on the host, and the runner mounts nothing of `$HOME` (the worktree read-only, the scratch
root under `/var/tmp/fork-linux-e2e/`, the read-only seed for the real-Fork tier).

## Matrix

| Desktop | Image | Integration tier | Real-Fork tier | Evidence |
|---|---|---|---|---|
| Xubuntu 26.04, XFCE 4.20 (xfwm4, xfsettingsd, Thunar 4.20 + its FileManager1, xfce4-terminal, garcon menu, Greybird / Greybird-dark) | `fork-linux-e2e-de-xfce:26.04` | ✅ 13/13 | ✅ 9/9 (setup 235 s from the seed, tier 292 s) | local `scripts/ci-e2e-de.sh xfce [--fork]`, 2026-10-11 |

GNOME, KDE Plasma, Cinnamon, MATE, Budgie and LXQt follow the same pattern: one
`docker/Dockerfile.e2e.de.<desktop>` plus one entry in `tests/e2e_de/desktops.py` (see "Adding a
desktop").

## How to run

```sh
./scripts/ci-e2e-de.sh xfce                    # integration tier (no Wine), ~30 s after the image build
./scripts/ci-e2e-de.sh xfce --fork             # real-Fork tier: setup + Fork under xfwm4, ~6-10 min
./scripts/ci-e2e-de.sh xfce -- -k theme        # pytest arguments after --
./scripts/ci-e2e-de-matrix.sh                  # every desktop's integration tier, in parallel
./scripts/ci-e2e-de-matrix.sh --fork           # every desktop's real-Fork tier, through the E2E slots
```

- `scripts/ci-e2e-de.sh [--name NAME] [--keep] DESKTOP [--fork] [-- PYTEST_ARGS]` builds
  `docker/Dockerfile.e2e.de.<DESKTOP>` (tag from its `# Image tag:` header, rebuilt only when the
  Dockerfile changes) and `docker/Dockerfile.release`, builds the release tarball from the
  read-only tree into the scratch root (`FL_E2E_DE_TARBALL=FILE` installs a given tarball
  instead), and runs `tests/e2e_de/test_de_integration.py` (or `test_de_fork.py` with `--fork`).
- It shares `scripts/lib-e2e-docker.sh` with `scripts/e2e-docker.sh`: the same scratch-root
  refusals (`/`, `$HOME`, symlinks, unmarked directories), one run per name, and for `--fork` one
  of the host-wide E2E slots (`FL_E2E_SLOTS`, default 3) and the seed
  (`/var/tmp/fork-linux-e2e/seed`, or `FL_E2E_SEED_DIR`; without one everything is downloaded).
- Logs: `logs/e2e-de/<NAME>.log` (pytest) and `logs/e2e-de/<NAME>/` (text logs from the scratch
  root: the session's daemons, fork-linux's logs, the setup log; never `drive_c`, never images).
  Screenshots stay in `/var/tmp/fork-linux-e2e/<NAME>/root/shots/` (they show Fork's logo).

## The session (`tests/e2e_de/dekit.py`)

`Session.start()` runs, in order, and waits for each to be really up: Xvfb (1920x1080, 96 DPI),
`dbus-daemon --session` on `$XDG_RUNTIME_DIR/bus` (so D-Bus activated services such as `xfconfd`,
the portals and the file manager inherit the session's environment), the desktop's settings
daemon (until it has published `Xft.dpi`), its window manager (until `_NET_SUPPORTING_WM_CHECK`
names it) and its file-manager service (until `org.freedesktop.FileManager1` has an owner). The
environment is what the desktop's display manager exports (for Xubuntu: `XDG_CURRENT_DESKTOP=XFCE`,
`DESKTOP_SESSION=xubuntu`, `XDG_MENU_PREFIX=xfce-`, `XDG_CONFIG_DIRS=/etc/xdg/xdg-xubuntu:/etc/xdg`,
...). Every process of the session carries `FL_E2E_DE_SESSION` or its `DISPLAY`; `stop()` ends
exactly those.

## Integration tier (`test_de_integration.py`, no Wine)

| Test | What the desktop itself shows |
|---|---|
| install from the release tarball | `install.sh --from-tarball --no-deps`: `fork-linux --version` is `VERSION`, `desktop status` is ok |
| menu entry | `desktop-file-validate`; the desktop's menu library lists it under Development (XFCE: garcon, the panel's library, through `python3-gi`); `gio launch` of the entry starts fork-linux, which asks its first-run license question (zenity), then declined |
| file manager "Open in Fork" | the action is installed next to the desktop's own (XFCE: Xubuntu's "Open Terminal Here" kept in `uca.xml`); the file manager's real context menu (keys from the recipe) runs `fork-linux open --from-file-manager <folder>` |
| theme light / dark | the desktop's own setting (XFCE: `/Net/ThemeName` Greybird / Greybird-dark) -> `theme.detect` of the installed package |
| scale per case | XFCE: default 100 %, Window Scaling 2x 200 %, custom DPI 192 200 % -> `display.scale_percent` |
| terminal | `fork-linux-host terminal DIR` opens the desktop's terminal (window class) with a shell whose cwd is DIR (`/proc`) |
| reveal | `fork-linux-host reveal FILE` -> FileManager1 -> a file-manager window on its folder |
| open file / open URL | `fork-linux-host open` -> xdg-open -> the default application (a stub registered with `xdg-mime`; XFCE hands web links to its own preferred browser, so the stub is also the `helpers.rc` WebBrowser) |
| doctor | only `wine.present`, `prefix.exists`, `fork.installed` fail before setup; no warnings |

## Real-Fork tier (`test_de_fork.py`, `FL_E2E_DE_FORK=1`)

Steps in order (a failed step fails the rest fast): 01 `fork-linux setup` (seeded downloads,
re-verified) completes; 02 `doctor --deep` has no failure or warning; 03 Fork's main window
(`WM_CLASS` fork.exe, the menu entry's `StartupWMClass`) is managed and framed by the desktop's
window manager, its screenshot is not uniform, settings say light; 04 `fork <repo>` hands a second
tab to the running Fork (one process); 05 Repository > Open In Shell opens the desktop's terminal
in the repository; 06 Repository > Open In File Explorer opens the desktop's file manager on it;
07 the file manager's "Open in Fork" reaches the running Fork; 08 after closing, Fork kept all three
tabs and the terminal it opened still runs, and with the desktop switched to dark it relaunches
with `Theme=1` and a dark window;
09 `doctor --deep` after use has no failure.

## XFCE facts the recipe relies on

- Window Scaling (Settings > Appearance, `/Gdk/WindowScalingFactor`) leaves the published
  `Xft.dpi` at 96. fork-linux read `Xft.dpi` first and answered 100 % on a 2x desktop; it now
  asks XFCE's xsettings first on XFCE (fixed in `display.py`, `DESKTOP_PROBES`).
- A running Thunar reads `uca.xml` once: after its first context menu, a new action stays
  invisible in old and new windows until `thunar -q`. `desktop install` now writes `uca.xml` only
  when it changes and then warns a running Thunar to restart; the tier checks that warning in
  `install.sh`'s output and then restarts Thunar as told.
- Xubuntu's xfsettingsd grabs Ctrl+Alt+T (`xfce4-keyboard-shortcuts.xml`: `exo-open --launch
  TerminalEmulator`), which is also Fork's "Open In Shell" shortcut: on Xubuntu that key opens
  xfce4-terminal in the session's working directory (normally home), not in the repository,
  because the desktop's grab wins over the application (checked in the container). Fork's
  Repository > Open In Shell menu and its Shell toolbar button are not affected; the tier uses the
  menu. A user who wants the key in Fork removes or changes the XFCE shortcut (Settings >
  Keyboard > Application Shortcuts); fork-linux never edits desktop shortcuts.
- `xdg-open https://…` on XFCE goes through `exo-open` to the preferred WebBrowser of
  `helpers.rc` (Xubuntu: firefox); `mimeapps.list` is not asked for web links.
- xfce4-terminal reuses one process for all its windows: the shell is found among that
  process's children.
- Fork's Repository > Open In Shell / Open In File Explorer act on the **active** tab: after
  `fork <second repo>` that is the second repository.
- The tier closes Fork with its own title-bar close button, never a synthetic Alt+F4: an
  `xdotool key alt+F4` under xfwm4 left a pending Alt+F4 that xfwm4 replayed when it re-grabbed
  its keys after the theme switch, closing every window (the terminal, Thunar, the relaunched
  Fork) and leaving a black screen. Real key presses do not do this.
- A screenshot is judged only once it is drawn (not one flat colour, `forkui.drawn_screenshot`);
  a blank one writes `logs/<name>-x11.txt` (windows, `xwininfo -tree`, processes) for the report.

## Adding a desktop

1. `docker/Dockerfile.e2e.de.<key>` (copy the XFCE one: `FROM ubuntu:26.04`, `# Image tag:
   fork-linux-e2e-de-<key>:26.04`, the same Wine host libraries and tier tools, plus the desktop's
   real components).
2. A `Desktop` entry in `tests/e2e_de/desktops.py` (session variables, settings daemon, window
   manager, file manager + service, terminal, theme and scale commands as the desktop's own
   settings dialogs run them, reset commands, context-menu keys), and a checker in
   `test_de_integration.py` for any new integration kind or menu reader.
3. `tests/test_docker.py` `EXPECTED`; `tests/test_e2e_de_recipes.py` keeps images, recipes and
   checkers in step on its own.
4. Run both tiers, record the result in the matrix above and in `docs/COMPATIBILITY.md`.
