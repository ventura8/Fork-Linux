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

Release round 2 (2026-10-12, `fix/integration` with the ssh-agent relay and the hand-off daemon
merged): `scripts/ci-e2e-de.sh <desktop> --fork` passed 10/10 on all eight desktops, one after
the other on a host shared with the full real tier and the packaging matrix. The tenth step,
`test_10_install_sh_never_breaks_the_installed_fork_linux`, runs install.sh over the working
install while a package manager owns fork-linux (A13: refused, nothing written) and an upgrade
interrupted after its trees were swapped (A14: `fork-linux` stays on PATH and starts Fork; the
next install finishes the job).

| Desktop | Image | Integration tier | Real-Fork tier | Evidence |
|---|---|---|---|---|
| Xubuntu 26.04, XFCE 4.20 (xfwm4, xfsettingsd, Thunar 4.20 + its FileManager1, xfce4-terminal, garcon menu, Greybird / Greybird-dark) | `fork-linux-e2e-de-xfce:26.04` | ✅ 15/15 | ✅ 10/10 (tier 666 s, release round 2, 2026-10-12) | local `scripts/ci-e2e-de.sh <desktop> [--fork]` with the hand-off daemon and doctor's `desktop.handoff`, 2026-10-11 |
| Ubuntu 26.04, GNOME 50 (GNOME Shell 50 in Ubuntu's mode on mutter's headless Wayland backend + rootless Xwayland, gsd-xsettings, Nautilus 50 + python3-nautilus and its FileManager1, Ptyxis via xdg-terminal-exec, the shell's own app grid, Yaru / Yaru-dark + color-scheme) | `fork-linux-e2e-de-gnome:26.04` | ✅ 16/16 | ✅ 10/10 (tier 343 s, release round 2, 2026-10-12) | local `scripts/ci-e2e-de.sh <desktop> [--fork]` on the reviewed tree, 2026-10-11 |
| Kubuntu 26.04, KDE Plasma 6.6 on X11 (kwin_x11, kcminit + kded6, Dolphin 25.12 + its FileManager1 and our service menu, Konsole, Kickoff's menu through kbuildsycoca6, Kubuntu / Kubuntu Dark / Breeze Dark global themes and colour schemes) | `fork-linux-e2e-de-kde:26.04` | ✅ 20/20 | ✅ 10/10 (tier 386 s, release round 2, 2026-10-12) | local `scripts/ci-e2e-de.sh <desktop> [--fork]` on the reviewed tree, 2026-10-11 |
| Kubuntu 26.04, KDE Plasma 6.6 on Wayland, its default session (kwin_wayland + rootless Xwayland, kcminit + kded6, Dolphin + its FileManager1 and our service menu, Konsole, kbuildsycoca6's menu, the same themes, Display Configuration's scale through kscreen-doctor) | `fork-linux-e2e-de-kdewayland:26.04` | ✅ 19/19 | ✅ 10/10 (tier 370 s, release round 2, 2026-10-12) | local `scripts/ci-e2e-de.sh <desktop> [--fork]` on the reviewed tree, 2026-10-11 |
| Ubuntu Cinnamon 26.04, Cinnamon 6.4 (rootless Xorg + dummy driver, cinnamon itself: panel, menu applet, window manager on libmuffin; csd-xsettings + csd-settings-remap, Nemo 6.4 + its FileManager1 and our Nemo actions, Tilix from Preferred Applications next to gnome-terminal + xdg-terminal-exec, Yaru-cinnamon / Yaru-cinnamon-dark + Dark mode, User interface scale x text scaling) | `fork-linux-e2e-de-cinnamon:26.04` | ✅ 17/17 | ✅ 10/10 (tier 376 s, release round 2, 2026-10-12) | local `scripts/ci-e2e-de.sh <desktop> [--fork]` with the hand-off daemon, 2026-10-11 |
| Ubuntu MATE 26.04, MATE 1.26 (marco, mate-settings-daemon, Caja 1.26 + its FileManager1 and caja-actions, mate-terminal, mate-panel's menu, Yaru-MATE light / dark and the stock Menta, BlackMATE, HighContrastInverse) | `fork-linux-e2e-de-mate:26.04` | ✅ 19/19 | ✅ 10/10 (tier 354 s, release round 2, 2026-10-12) | local `scripts/ci-e2e-de.sh <desktop> [--fork]` on the reviewed tree, 2026-10-11 |
| Ubuntu Budgie 26.04, Budgie 10.10 (labwc 0.9 headless Wayland + rootless Xwayland, budgie-daemon, Nemo 6.4 + its FileManager1 service, xfce4-terminal, the panel menus' libbudgie-appindexer, Pocillo / Pocillo-dark + the Dark theme switch, display scale and text scaling) | `fork-linux-e2e-de-budgie:26.04` | ✅ 16/16 | ✅ 10/10 (tier 505 s, release round 2, 2026-10-12) | local `scripts/ci-e2e-de.sh <desktop> [--fork]` on the reviewed tree, 2026-10-11 |
| Lubuntu 26.04, LXQt 2.3 (lxqt-session + openbox and lxqt-panel, pcmanfm-qt 2.3 + its FileManager1, qterminal, panel menu via GMenu, Kvantum Lubuntu / KvArcDark, Fusion palettes, Global Screen Scaling) | `fork-linux-e2e-de-lxqt:26.04` | ✅ 19/19 | ✅ 10/10 (tier 750 s, release round 2, 2026-10-12) | local `scripts/ci-e2e-de.sh <desktop> [--fork]` on the reviewed tree, 2026-10-11 |

The review of 2026-10-11 re-ran every tier on the final tree (the integrator's earlier 7-desktop
results stand in git history): it replaced the GNOME and Cinnamon stand-ins (mutter's and
muffin's own binaries) with GNOME Shell and cinnamon, added Plasma on Wayland, the components /
portal / kept-running checks and the Cinnamon / MATE menu notice, and fixed two harness flakes
(lxqt-panel's crash loop, Xvfb resetting under KDE's kcminit). Seen once in 9 real-Fork runs of
the final tree: setup's `prefix_init` failed with "wine: could not load kernel32.dll, status
c0000135" 8 s into the Cinnamon run; another workflow's plain Xvfb runs on the same host showed
the same error that day, nothing in it depends on the desktop, and the re-run passed.

The hand-off fix (2026-10-11, maintainer decision for 1.0.0) re-ran both tiers on XFCE and
Cinnamon (now with Tilix); the other desktops' rows are from the review before it. Step 05's
`logs/05-confinement.txt`, XFCE, the tree before the fix (13002fe with the new step 05):
`xfce4-terminal` had `NoNewPrivs: 1`, `Seccomp_filters: 2`, like Fork's Wine process, against
the container's `NoNewPrivs: 0`, `Seccomp_filters: 1`; on Cinnamon no Tilix window ever appeared
(step 05 timed out after 60 s). With the fix the hand-off daemon and the terminal (xfce4-terminal,
Tilix) have the container's own values and Tilix stays open. One Cinnamon run of the fixed
tree hung in setup (winetricks' .NET 4.0 installer idle for 20 min under a load average of 23,
before Fork ever started; nothing in it depends on the hand-off) and was stopped; the re-run
passed. The `desktop.handoff` doctor check (route and fallbacks) re-ran both XFCE tiers: while
Fork ran, `launcher daemon (no systemd user session): the hand-off daemon runs (pid 15383, port
47777)`, the pid `session.json` recorded and whose `/proc` status matched the container's
baseline; after Fork closed, `a hand-off-only daemon starts with Fork`, ok, with `handoff.log`
empty.

Each desktop is one `docker/Dockerfile.e2e.de.<desktop>` plus one entry in
`tests/e2e_de/desktops.py` (see "Adding a desktop").

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
  of the host-wide E2E slots (`FL_E2E_SLOTS`, default from the host's cores and memory) and the seed
  (`/var/tmp/fork-linux-e2e/seed`, or `FL_E2E_SEED_DIR`; without one everything is downloaded).
- Logs: `logs/e2e-de/<NAME>.log` (pytest) and `logs/e2e-de/<NAME>/` (text logs from the scratch
  root: the session's daemons, fork-linux's logs, the setup log; never `drive_c`, never images).
  Screenshots stay in `/var/tmp/fork-linux-e2e/<NAME>/root/shots/` (they show Fork's logo).
- CI: `check.yml` job `e2e-de` runs every desktop's integration tier on every pull request (a
  8-cell matrix, `fail-fast: false`, images built with the Actions cache, `FL_CI_DOCKER_CACHE=gha`);
  `e2e-wine.yml` job `e2e-de` runs every desktop's real-Fork tier weekly, on demand and on
  release-branch PRs, with fresh downloads (no seed, nothing cached). Both upload `logs/e2e-de/`
  only. Locally `scripts/ci-pipeline.sh` runs the integration matrix as its `e2e-de` stage.

## The session (`tests/e2e_de/dekit.py`)

`Session.start()` runs, in order, and waits for each to be really up: Xvfb (1920x1080, 96 DPI,
`-noreset`: an X server whose last client leaves resets and deletes `Xft.dpi`, which once left
KDE's session waiting for kcminit's `Xft.dpi` after kcminit had exited and before kded6 connected),
`dbus-daemon --session` on `$XDG_RUNTIME_DIR/bus` (so D-Bus activated services such as `xfconfd`,
the portals and the file manager inherit the session's environment), the desktop's settings
daemon (until it has published `Xft.dpi`), its window manager (until `_NET_SUPPORTING_WM_CHECK`
names it) and its file-manager service (until `org.freedesktop.FileManager1` has an owner). The
environment is what the desktop's display manager exports (for Xubuntu: `XDG_CURRENT_DESKTOP=XFCE`,
`DESKTOP_SESSION=xubuntu`, `XDG_MENU_PREFIX=xfce-`, `XDG_CONFIG_DIRS=/etc/xdg/xdg-xubuntu:/etc/xdg`,
...; a value may name `{home}`, the session's home, as KDE's `XDG_CONFIG_DIRS` does). Every
process of the session carries `FL_E2E_DE_SESSION` or its `DISPLAY`; `stop()` ends exactly those.
A recipe may run its own X server instead of Xvfb (`Desktop.x_server`, the session's display is
appended): Cinnamon's muffin rejects Xvfb's only RandR mode (no refresh rate) and could not apply
a user interface scale, so the Cinnamon image runs Xorg with the dummy driver, rootless.
A Wayland desktop (`Desktop.wayland_compositor`: GNOME Shell, Budgie's labwc) starts its
compositor right after the bus instead of an X server; the session moves to the compositor's
rootless Xwayland, whose `DISPLAY` / `XAUTHORITY` / `WAYLAND_DISPLAY` labwc's startup command
writes to `$XDG_RUNTIME_DIR/session.env`, and which the session reads from the Xwayland process
itself for gnome-shell (`hands_over_xwayland=False`, no startup command), then puts into the D-Bus
activation environment as gnome-session does. gnome-shell aborts without a system bus
(`Desktop.system_bus`): it gets a private one with no services (`dekit.SYSTEM_BUS_CONFIG`).

Once up, the session checks that the desktop's **own** programs run (`Desktop.components`, by
executable or `argv[0]` name: the shell or window manager, the settings daemon, the file-manager
service that stays up; for example GNOME: `gnome-shell`, `Xwayland`, `gsd-xsettings`; Cinnamon:
`cinnamon`, `Xorg`, `csd-xsettings`, `csd-settings-remap`) and lists every process of the session
(pid, parent, executable, command line) in `logs/session-processes.txt`, again when the portal
started and before it stops: the logs show which window manager, file manager, terminal and
portal processes really ran. At the end of each tier the same components must still be the
processes the last start found (`Session.components_gone`; `Desktop.restarted` names the ones the
desktop or the tier starts again on purpose: Thunar after `thunar -q`, MATE's marco and settings
daemon), so a component that crashed and was started again fails the tier.

Nothing skips silently: with `FL_E2E_DE` set, a run in which any test skipped fails
(`tests/e2e_de/conftest.py`), and a program the tier needs that is missing from the image fails
the session instead of skipping it.

## Integration tier (`test_de_integration.py`, no Wine)

| Test | What the desktop itself shows |
|---|---|
| the desktop's own programs | its window manager's EWMH name (`Desktop.wm_name`), its components (above) still running, and `org.freedesktop.portal.Settings` answered by the backend its `<desktop>-portals.conf` picks (`Desktop.portal`: gnome, kde, xapp, gtk; Lubuntu installs none that answers it) |
| install from the release tarball | `install.sh --from-tarball --no-deps`: `fork-linux --version` is `VERSION`, `desktop status` is ok |
| menu entry | `desktop-file-validate`; the desktop's menu lists it under Development (XFCE: garcon, the panel's library, through `python3-gi`; KDE: `kbuildsycoca6 --menutest`, the menu KService builds for Kickoff; GNOME: the running GNOME Shell's own app grid, through its Eval); the desktop's own launcher (`Desktop.menu_launcher`: `gio launch`; KDE: `kioclient exec`, KIO's application launcher; GNOME Shell and Cinnamon: their own `App.open_new_window`, as a click in their menus) starts fork-linux, which asks its first-run license question (zenity), then declined. On Cinnamon and MATE (`menu_after_login`) install.sh had to create `~/.local/share/applications`, which their menus do not watch: fork-linux says so (`MENU_RELOGIN`) and the session restarts first, as the user logs out and in |
| file manager "Open in Fork" | the action is installed next to the desktop's own (XFCE: Xubuntu's "Open Terminal Here" kept in `uca.xml`; KDE: an executable service menu in `~/.local/share/kio/servicemenus` for `inode/directory`); the file manager's real context menu (keys from the recipe) runs `fork-linux open --from-file-manager <folder>` |
| theme light / dark | the desktop's own setting (XFCE: `/Net/ThemeName` Greybird / Greybird-dark; KDE: the Global Themes Kubuntu, Kubuntu Dark and Breeze Dark, Colors on their own, a dark scheme named without "dark") -> `theme.detect` of the installed package; and when the desktop's portal states a preference (`color-scheme` 1 dark, 2 light; 0 is none), it is the same (every answer in `logs/portal-color-scheme.txt`: KDE's, Budgie's and GNOME's / Cinnamon's dark mode state one, XFCE's and MATE's gtk backend never does) |
| scale per case | XFCE: default 100 %, Window Scaling 2x 200 %, custom DPI 192 200 %; KDE: default 100 %, global scale 200 % and 150 %, Force font DPI 192 200 % -> `display.scale_percent` |
| terminal | `fork-linux-host terminal DIR` opens the desktop's terminal (window class, and the window's process is that terminal's program) with a shell whose cwd is DIR (`/proc`) |
| reveal | `fork-linux-host reveal FILE` -> FileManager1 -> a file-manager window on its folder |
| open file / open URL | `fork-linux-host open` -> xdg-open -> the default application (a stub registered with `xdg-mime`; XFCE hands web links to its own preferred browser, so the stub is also the `helpers.rc` WebBrowser; KDE asks kdeglobals' `BrowserApplication` first, so the stub is also named there, as Default Applications does) |
| doctor | only `wine.present`, `prefix.exists`, `fork.installed` fail before setup; no warnings |
| kept running | none of the desktop's own programs crashed or was started again during the tier (above) |

## Real-Fork tier (`test_de_fork.py`, `FL_E2E_DE_FORK=1`)

Steps in order (a failed step fails the rest fast): 01 `fork-linux setup` (seeded downloads,
re-verified) completes; 02 `doctor --deep` has no failure or warning; 03 Fork's main window
(`WM_CLASS` fork.exe, the menu entry's `StartupWMClass`) is managed by the desktop's window manager
(framed, or in its `_NET_CLIENT_LIST` when it does not frame Fork's undecorated window) on the
desktop's light theme, its screenshot is not uniform, settings say light; 04 `fork <repo>` hands a second
tab to the running Fork (one process); 05 Repository > Open In Shell opens the desktop's terminal
in the repository, outside Wine's process tree: the container has no systemd user manager, so the
hand-off goes through the launcher's hand-off daemon (`session.json` records it with
`"handoff": true`); the daemon and the terminal have exactly the `NoNewPrivs`, `Seccomp` and
`Seccomp_filters` of the test process itself (the container's baseline: `NoNewPrivs: 0` and
Docker's default seccomp profile, one filter), while Fork's Wine process has `NoNewPrivs: 1` and
one filter more (wine-staging's); the terminal still runs 5 s later, and `handoff.log` stays
empty (no hand-off ran inside Wine's tree; `logs/05-confinement.txt` lists the four); `doctor
--check desktop.handoff` is ok with `launcher daemon (no systemd user session): the hand-off
daemon runs (pid <session.json's>, port <session.json's>)` (`logs/05-doctor-handoff.json`); 06 Repository > Open In File Explorer opens the desktop's file manager on it;
07 the file manager's "Open in Fork" reaches the running Fork (the processes seen opening the
repository go to `logs/07-opening.txt`); 08 after closing, Fork kept all three
tabs and the terminal it opened still runs, and with the desktop switched to dark it relaunches
with `Theme=1` and a dark window;
09 `doctor --deep` after use has no failure, `desktop.handoff` is ok with the route the next launch
takes (`launcher daemon (no systemd user session): a hand-off-only daemon starts with Fork`, so no
hand-off fell back during the run), and none of the desktop's own programs crashed during
Fork's run.

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

## KDE Plasma facts the recipe relies on

- Kubuntu 26.04 logs in to Plasma on Wayland by default; the X11 session is the separate
  `plasma-session-x11` package (`startplasma-x11`, `plasmax11.desktop`), which the image installs.
  The recipe's environment is what sddm and `startplasma-x11` export (`XDG_CURRENT_DESKTOP=KDE`,
  `DESKTOP_SESSION=plasmax11`, `KDE_FULL_SESSION=true`, `KDE_SESSION_VERSION=6`,
  `XDG_MENU_PREFIX=plasma-`), with `XDG_CONFIG_DIRS` starting with `~/.config/kdedefaults` (startplasma)
  and ending with Kubuntu's `/usr/share/kubuntu-default-settings/kf5-settings`
  (`/etc/xdg/plasma-workspace/env/env.sh`), where `LookAndFeelPackage=org.kubuntu.desktop` and
  `BrowserApplication=firefox_firefox.desktop` come from. Without `XDG_MENU_PREFIX=plasma-`,
  `kbuildsycoca6` looks for an `applications.menu` that Plasma does not ship (only
  `plasma-applications.menu`) and builds an empty menu.
- `plasma_session` runs kcminit's startup modules before kded6; `kcminit kcm_fonts_init` publishes
  `Xft.dpi` (96, or kcmfonts' `forceFontDPI`). kded6 alone publishes no `Xft.dpi`.
- A Global Theme (`plasma-apply-lookandfeel`, System Settings > Colors & Themes) writes its colour
  scheme to `~/.config/kdedefaults/kdeglobals`, not to `~/.config/kdeglobals`: `kreadconfig6` sees
  `ColorScheme=BreezeDark` only through `XDG_CONFIG_DIRS`. fork-linux's KDE probe also reads
  `LookAndFeelPackage` (`org.kubuntudark.desktop`), so a session without kdedefaults is dark too.
  Colors on their own (`plasma-apply-colorscheme`) write `ColorScheme` and the colour groups to
  `~/.config/kdeglobals` and win over the global theme's (Kubuntu Dark + Breeze Light is light);
  applying a global theme takes them back out.
- Plasma calls a colour scheme dark by its colours, not its name (xdg-desktop-portal-kde: the grey
  of `[Colors:Window] BackgroundNormal` below 192; measured in the image: `191,191,191` is dark,
  `192,192,192` light, Breeze Dark `32,35,38` dark, Breeze Light `239,240,241` light). fork-linux
  read only the names, so a dark scheme named without "dark" (Nordic, Dracula, a user's own; the
  tier's "Nightfall" is Breeze Dark's colours under another name) gave a light Fork. `theme._kde`
  now asks that colour first (`kreadconfig6` / `kreadconfig5`, so `~/.config/kdedefaults` counts),
  then the `ColorScheme` / `LookAndFeelPackage` names, then the kdeglobals file; a session where
  no scheme was ever applied has no `[Colors:Window]` and keeps the name-based answer. The
  `dark:custom-colors` case checks it.
- Scale on X11: Display Configuration's global scale writes kdeglobals `[KScreen] ScaleFactor` /
  `ScreenScaleFactors` and kcmfonts `forceFontDPI` (96 x scale) and publishes `Xft.dpi`; Text &
  Fonts' "Force font DPI" writes `forceFontDPI` and publishes `Xft.dpi` the same way (libkrdb, as
  `kcminit kcm_fonts_init` at login). Plasma 6.6's `startplasma-x11` exports only
  `QT_SCREEN_SCALE_FACTORS`, no `GDK_SCALE` / `GDK_DPI_SCALE`, so `Xft.dpi` (then `forceFontDPI`)
  is what fork-linux reads: 200 % -> 192, 150 % -> 144.
- Dolphin reads the service menus each time it opens a context menu: one added while Dolphin runs
  shows up at once (no restart, unlike Thunar and Nautilus). KF6 keeps service menus out of
  KService's sycoca (no `kbuildsycoca6` needed), and KIO ignores a user service menu that is not
  executable. "Open in Fork" (`X-KDE-Priority=TopLevel`) is the 7th item of the folder's context
  menu (after Create New, Open Folder With..., Add to Places, Sort By, Change View Mode, Open
  Terminal Here).
- `fork-linux-host terminal` opens Konsole (`DESKTOP_TERMINALS["KDE"]`, `--workdir`); Kubuntu's
  Konsole profile is `kubuntu.profile`. `reveal` reaches Dolphin through FileManager1
  (`dolphin --daemon`, D-Bus activated), which selects the file.
- Web links: KIO hands http(s) URLs to kdeglobals' `BrowserApplication` first and falls back to
  `mimeapps.list` when that entry is not installed (in the image, Kubuntu's Firefox snap is not).
- On a Kubuntu without zenity, fork-linux asks through kdialog (`ui.KDialogUI`); with both
  installed `auto` picks zenity (kdialog has no progress window). Checked by hand in this image
  with kdialog added and `[ui] progress = kdialog`: the license question is a kdialog window
  titled "Fork license agreement", Escape declines it and fork-linux exits with "Fork's license
  agreement was declined; nothing was downloaded".
- xdg-desktop-portal-kde answers `org.freedesktop.appearance color-scheme` for every theme case
  (2 light, 1 dark, also for the scheme named without "dark"), and fork-linux agrees with it in
  each (`logs/portal-color-scheme.txt`).
- Plasma on Wayland, Kubuntu 26.04's default session, is its own desktop of the tier (`kdewayland`, below).

## KDE Plasma on Wayland facts the recipe relies on (`kdewayland`)

- Kubuntu 26.04 logs in to Plasma on Wayland by default (sddm's "Plasma", `startplasma-wayland`).
  The recipe is the X11 one (`kde`) with Wayland's own window manager and scale: `kwin_wayland`
  on its virtual backend (`--virtual`, one 1920x1080 output `Virtual-0`, software rendering) with
  its own rootless Xwayland, where Wine and Fork run; its `--exit-with-session` command hands the
  session `DISPLAY` and `WAYLAND_DISPLAY` (`dekit.SESSION_ENV`) and the D-Bus activation
  environment, as startplasma does; `XDG_SESSION_TYPE=wayland`, `DESKTOP_SESSION=plasma`.
- `kwin_wayland` carries the file capability `cap_sys_nice`, and a container without
  `CAP_SYS_NICE` cannot execute such a file (EPERM): the image installs a plain copy (kwin then
  logs that it has no real-time priority). kwin also wants `/tmp/.X11-unix` to exist.
- `QT_QPA_PLATFORM=xcb` and `GDK_BACKEND=x11` are the tier's own: Dolphin, Konsole and zenity then
  open on Xwayland, where xdotool sees them. kwin runs on its own platform, and kscreen-doctor on
  Qt's Wayland platform (libkscreen picks its KWin backend there).
- kwin asks for the portal while it starts, before `DISPLAY` is in the activation environment,
  and xdg-desktop-portal-kde (on xcb) aborts without it; xdg-desktop-portal then offers no
  Settings for the rest of the session. The hand-over ends that early portal; the next request
  starts it again with the session's environment (in a Plasma session it starts after startplasma
  exported it). xdg-desktop-portal-kde then answers every theme case, and fork-linux agrees.
- Scale: System Settings > Display Configuration > Scale sets the output's scale through
  libkscreen's KWin backend (the tier runs `kscreen-doctor output.Virtual-0.scale.<s>`, libkscreen's
  own tool). With "Legacy applications (X11): Apply scaling themselves" (Plasma's default) KWin
  publishes `Xft.dpi` = 96 x the scale to its Xwayland (200 % 192, 150 % 144), which fork-linux
  reads (`display._from_xrdb`); kcmfonts' `forceFontDPI` is an X11-session setting. "Scaled by the
  system" did not change Xwayland's scale or `Xft.dpi` in this headless session (kwinrc
  `[Xwayland] XwaylandClientsScale=false`, also from kwin's start), so it is not a tier case.
- Everything else (menu, Dolphin's service menu, Konsole, themes, the browser) is Plasma's own and
  behaves as on X11.


## LXQt (Lubuntu) facts the recipe relies on

- The session starts as Lubuntu's does: `startlxqt`, which sets `QT_QPA_PLATFORMTHEME=lxqt` and
  `XDG_MENU_PREFIX=lxqt-` and then runs lxqt-session. lxqt-session itself starts openbox (the
  `window_manager` of Lubuntu's `session.conf`), `pcmanfm-qt --desktop`, the panel, the global
  shortcuts and the notification daemon. So the recipe's window manager and file-manager
  service are empty, and the session waits for openbox and for pcmanfm-qt's FileManager1.
  `XDG_CONFIG_DIRS=/etc/xdg/xdg-Lubuntu:/etc/xdg:/etc:/usr/share` brings in Lubuntu's own
  defaults (`lubuntu-default-settings`).
- LXQt reads its INI files (`lxqt.conf`, `session.conf`, `panel.conf`) from the user's
  directory first, then from each of `XDG_CONFIG_DIRS`, key by key. The user's files hold only
  what the dialogs changed (plus `__userfile__=true`). The recipe edits them as QSettings does
  and resets a case by removing the keys it set.
- Theme: Lubuntu's Qt style is **kvantum**, so the Kvantum theme colours every Qt application
  (Kvantum Manager: `~/.config/Kvantum/kvantum.kvconfig`; Lubuntu's default is the light
  `Lubuntu`, `KvArcDark` is dark). Under Kvantum, LXQt's widget palette has no effect. Under
  another Qt style (Fusion), the palette's `window_color` colours the applications.
  `palette_override` changes neither. LXQt's own theme (`theme=dark`) styles only the panel and
  the desktop. All of this was checked with screenshots of a pcmanfm-qt window. GNOME's
  `gtk-theme` on Lubuntu is `Arc-Darker` (dark header bars only), which fork-linux used to fall
  back to, so Fork started dark on the light Lubuntu desktop. `theme._lxqt` now reads what colours
  the applications: under the `kvantum` style the Kvantum theme's `[GeneralColors] window.color`
  (the user's copy in `~/.config/Kvantum`, then `$XDG_DATA_DIRS/Kvantum`, then the theme's name;
  Kvantum's built-in `Default` is light), under any other style the palette's `window_color`,
  else LXQt's `theme=`; `lxqt.conf` is merged from `$XDG_CONFIG_HOME` over each of
  `$XDG_CONFIG_DIRS` key by key, as LXQt does, and keys before the first section belong to
  `[General]` (QSettings). The cases are `light` (Lubuntu as
  installed), `light:chosen-in-the-dialogs`, `dark` (Kvantum `KvArcDark` + LXQt theme `dark`),
  `dark:kvantum-dark-theme`, `dark:fusion-dark-palette` and `light:dark-panel-theme`.
- Scale: LXQt's setting is Session Settings > Global Screen Scaling. It writes `QT_SCALE_FACTOR`
  and `GDK_SCALE` (both 1.5 for 1.5) to `session.conf` `[Environment]` (checked by driving the
  real dialog), which lxqt-session exports to the applications it starts, **at login only**. So
  each scale case restarts the session (`scale_restarts_session`), and the session takes the
  exported variables from a child of lxqt-session (`session_exports`). Appearance > Font >
  Resolution (DPI) writes only a fontconfig `dpi` (no `Xft.dpi`, no xsettings without
  xsettingsd), so it is not a scale setting.
- The panel menu (Lubuntu: `fancymenu` on `lxqt-applications-compact.menu`, from `panel.conf`)
  files `Categories=Development` under "Programming". It is read with GMenu
  (`gir1.2-gmenu-3.0`), the XDG menu specification that the panel's libqtxdg implements: libqtxdg
  has no Python binding.
- PCManFM-Qt reads FileManager-Actions entries (`~/.local/share/file-manager/actions`) whenever it
  builds a context menu, so it needs no restart. fork-linux did not know PCManFM and installed no
  "Open in Fork" on Lubuntu; `pcmanfm` (programs `pcmanfm-qt` / `pcmanfm`) is now a file manager of
  `desktop install --file-managers` that gets the same FileManager-Actions entry as Caja. In the folder's context menu, "Open in Fork"
  sits just above Folder Properties: Menu, Up, Up, Return. `pcmanfm-qt DIR` hands the folder to
  the running desktop instance, which owns `org.freedesktop.FileManager1`.
- Terminal: Debian's `x-terminal-emulator` alternative points to qterminal. A qterminal started
  through it is named `x-terminal-emul` in `/proc`, which is how the tier saw that fork-linux did
  not pick qterminal by name (another terminal earlier in its list would have won, and
  distributions without the alternative found none). qterminal is now a known terminal
  (`--workdir`) and LXQt's own (`DESKTOP_TERMINALS["LXQT"]`). Lubuntu's global shortcuts
  bind Ctrl+Alt+T to qterminal, so as on XFCE the desktop takes Fork's "Open In Shell" key, and
  the tier uses Fork's menu.
- Portals: `lxqt-portals.conf` picks lxqt, then kde, then gtk, and Lubuntu installs only
  xdg-desktop-portal-lxqt, a file chooser: nothing answers `org.freedesktop.portal.Settings`, so
  no portal tells applications Lubuntu's colour scheme (`Desktop.portal=""`; the tier checks that
  it stays unanswered).
- `xdg-open` on LXQt runs `qtxdg-mat open`, which follows `xdg-mime default` (`mimeapps.list`) for
  files and links alike. Lubuntu's `session.conf` sets `BROWSER=firefox`, which `xdg-open` does
  not use on LXQt.
- Lubuntu installs `fonts-dejavu-core` (`lubuntu-desktop`), and the image does too. Without it,
  `doctor` warns `host.fonts`.
- Lubuntu's panel menu (fancymenu) names featherpad and lubuntu-update-notifier as favorites, and
  lxqt-panel 2.3 crashes (SIGSEGV) about 15 s after it starts when a favorite's desktop file is
  missing; lxqt-session then starts it again, every 15 s. The image installs both, as
  lubuntu-desktop does, and the tier's "kept running" check would catch such a crash loop.
- Flaky spot, seen once in three LXQt real-Fork runs: `fork-linux setup` failed in its `fonts` step
  because Wine could not start `C:\windows\syswow64\regedit.exe` ("ShellExecuteEx failed")
  for one font, twice in a row (after 7 and after 3 fonts had registered). fork-linux retries
  `winetricks corefonts` once, and both tries hit it. The next run passed. Nothing in that step
  depends on the desktop. It is recorded here in case it comes back.

## GNOME facts the recipe relies on

- The window manager is **GNOME Shell 50** itself, in Ubuntu's session mode (`--mode=ubuntu`:
  the Yaru shell theme and Ubuntu's extensions, the dock and the desktop icons among them; from
  `ubuntu-session`), on mutter's headless backend (`--wayland --headless --virtual-monitor
  1920x1080`; GNOME 50 has no X11 session) with its own rootless Xwayland (`:0`, with an
  `XAUTHORITY` file), and no Xvfb (`Desktop.wayland_compositor`). Its check window names it
  "GNOME Shell". gnome-session cannot run in the container (no systemd user manager), so the tier
  starts gnome-shell as gnome-session's `org.gnome.Shell@wayland` unit would. Until 2026-10-11
  the tier ran mutter's own `mutter` binary instead, which is not what an Ubuntu session runs.
- gnome-shell aborts at start without a system bus (its time-limits manager connects to it): the
  session gives it a private one with no services (`Desktop.system_bus`); it then logs that GDM,
  logind, polkit and accountsservice are missing and carries on. It has no startup command, so
  the session reads `DISPLAY` and `XAUTHORITY` from its Xwayland's command line and
  `WAYLAND_DISPLAY` from its socket, and updates the D-Bus activation environment, as
  gnome-session does with what gnome-shell hands it.
- gnome-shell runs with `--unsafe-mode`, which opens its `org.gnome.Shell.Eval`: the tier reads
  the menu from the running shell's own app grid (`AppDisplay`, laid out again as the shell does
  when apps change), launches our entry with the shell's `App.open_new_window` (a click on its
  icon), and presses "Keep Changes" in the shell's "Keep these display settings?" dialog after a
  scale change (`desktops._display_config_scale`; without it the shell reverts the scale after
  20 s).
- `GDK_BACKEND=x11` is the tier's own: Nautilus, Ptyxis and zenity then open on Xwayland, where
  xdotool sees them. Wine (Fork) is an X client anyway.
- The root window of a rootless Xwayland cannot be captured: a screenshot is a window capture, or
  for the whole screen every visible window pasted at its position on black.
- GNOME Shell (mutter) frames only decorated X windows: Fork draws its own title bar and stays
  unframed (`frames_undecorated=False`); it is still managed (in `_NET_CLIENT_LIST`).
- Ubuntu's desktop-icons extension (DING) draws the desktop as an X window ("Desktop Icons 1",
  `WM_CLASS` gjs) under `GDK_BACKEND=x11`: it is part of the screen in whole-screen screenshots.
- Flaky spot, seen once in four GNOME real-Fork runs of the final tree: step 08 clicked Fork's
  close button right after step 07 had handed the third repository over, and Fork was still
  running 60 s later (the screenshot after 07 shows Fork with two tabs, Nautilus over it; no
  dialog was recorded). `forkui.close_fork` now clicks the button again every 20 s while the main
  window is still shown, as a user would, notes each retry in `logs/close-fork.txt` and keeps a
  `close-timeout` screenshot and X report if Fork still does not exit.
- Flaky spot, seen once in six GNOME integration runs of the final tree (before the retry): the "Open in Fork"
  keys (Menu, Up, Up, Return) reached a Nautilus window that had just been mapped and showed its
  folder, and nothing happened (no menu, no new window, fork-linux never started).
  `Session.context_action` (both tiers) now presses Escape and the keys again, up to three tries,
  as long as no new window appeared (so no other menu item ran), and notes each retry in
  `logs/context-menu.txt`.
- `XDG_CURRENT_DESKTOP=ubuntu:GNOME`: ubuntu-settings' gschema overrides (Yaru) are keyed on
  `ubuntu`. xdg-utils recognises GNOME only by `GNOME*`, `GNOME_DESKTOP_SESSION_ID` (gnome-session
  sets it to `this-is-deprecated`) or gnome-session's `org.gnome.SessionManager`; the recipe
  sets the variable like gnome-session. Without it xdg-open falls back to its generic mode, where
  `file --mime-type` ignores shared-mime-info globs and a file does not reach its default app.
- Scale: Settings > Displays > Scale is mutter's monitor configuration
  (`org.gnome.Mutter.DisplayConfig.ApplyMonitorsConfig`, `~/.config/monitors.xml`, then the
  shell's "Keep Changes"), not `scaling-factor` (0); gsd-xsettings publishes `Xft.dpi` = 96 x
  display scale x text scaling (100 % 96, 200 % 192, Large Text 120, both 240; each case waits for
  its value). fork-linux asked GNOME's gsettings first and
  read 200 % + Large Text as 125 %; GNOME's own probe is now `Xft.dpi` (`DESKTOP_PROBES["gnome"]`),
  with the gsettings as the fallback when there is no `Xft.dpi`.
- Nautilus 50 loads nautilus-python extensions against API 4.1 only (`Nautilus-4.1.typelib`) and
  has loaded it before any extension; fork-linux's extension asked for 4.0, then 3.0, both raised
  `ValueError` and it never loaded. It now requires 4.1, 4.0 or 3.0, the first that works, else
  imports the namespace Nautilus loaded. Nautilus loads extensions only when it starts: writing a
  changed extension or removing it while `nautilus` runs now prints `NAUTILUS_RESTART` ("run
  'nautilus -q'"); the tier reinstalls with a Files window open, expects the notice, then runs
  `nautilus -q`.
- Nautilus's D-Bus service (`nautilus --gapplication-service`) owns FileManager1 and quits when
  idle, so FileManager1 is often only activatable. Its folder context menu opens with the Menu
  key (Shift+F10 does nothing); "Open in Fork" is the second item from the bottom.
- Ptyxis is 26.04's terminal, picked by `xdg-terminal-exec` (`ptyxis --new-window`); its windows
  have `WM_CLASS` `ptyxis` under X11 and its shells are children of `ptyxis-agent`
  (`dekit.children_cwds` walks all descendants). Without a systemd user manager its agent
  starts every shell through `systemd-run --user --scope` and fails, so the image diverts
  `systemd-run` (on a real desktop the user manager is there).
- The menu entry is checked in GNOME Shell's own app grid: ours is an icon on the top level, in
  no folder (`Desktop.menu_folder=()`). GNOME Shell (GIO's app info monitor) notices a new
  `~/.local/share/applications` while it runs.
- xdg-desktop-portal-gnome answers `org.freedesktop.appearance color-scheme`: 0 (no preference)
  for Ubuntu's light style (`color-scheme` `default`), 1 for dark, as fork-linux reads it.

## Budgie facts the recipe relies on

- Budgie 10.10 (Ubuntu Budgie 26.04) is Wayland only: budgie-wm and the X11 session are gone,
  labwc is the window manager (`startbudgielabwc`: Budgie's own copy of labwc's `rc.xml` and
  `menu.xml` in `~/.config/budgie-desktop/labwc`). The tier runs labwc on its headless backend
  (`WLR_BACKENDS=headless`, `WLR_RENDERER=pixman`, one output set to 1920x1080 with
  `wlr-randr`) with the same copied configuration; X11 programs, Wine and Fork included, run on
  labwc's rootless Xwayland, whose X window manager names itself `wlroots wm`. labwc runs its
  startup command without a shell (it splits the words itself), so the hand-over of `DISPLAY`
  and `WAYLAND_DISPLAY` (`dekit.SESSION_ENV`) is a small script. Xwayland drops the first XTEST
  event it gets: the hand-over presses Shift once before the session uses xdotool.
- labwc draws server-side decorations and never reparents X windows: Fork's main window (its
  own title bar) is in `_NET_CLIENT_LIST` but stays a child of the root window
  (`frames_undecorated=False`; step 03 then checks the client list).
- budgie-session cannot run in the container (it aborts without a system bus), so the tier
  starts the components fork-linux talks to: budgie-daemon (its `BudgieDaemon` component, on
  GDK's Wayland backend), Nemo's FileManager1 service and the portals. budgie-daemon turns
  Budgie Desktop Settings > Style > "Dark theme" (`com.solus-project.budgie-panel dark-theme`)
  into `org.gnome.desktop.interface color-scheme` (`prefer-dark` / `prefer-light`); the Widgets
  theme alone (`gtk-theme`) does not change the color scheme. Budgie 10.10 runs no XSETTINGS
  daemon (no `gsd-xsettings` in its session): Xwayland programs get no `Xft.dpi`.
- Ubuntu Budgie starts **dark** (`budgie-desktop-environment`'s `:Budgie` overrides:
  Pocillo-dark, `prefer-dark`, `dark-theme=true`); the tier's light theme is "Dark theme" off
  with the Pocillo widgets, and step 03 sets the desktop's light theme before it launches Fork.
  fork-linux reads `color-scheme` first, then the `gtk-theme` name (`theme._gnome_family`).
- Scaling: Budgie Control Center > Displays (as `wdisplays` and `wlr-randr`) sets labwc's
  output scale; labwc then draws Xwayland's programs scaled up (Xwayland's screen becomes
  960x540 at scale 2), so fork-linux must not scale Fork again and answers 100 %. Budgie Desktop
  Settings > Fonts > Text scaling and Budgie Control Center > Accessibility > Large Text set
  `text-scaling-factor`, which fork-linux reads (125 % and 200 %).
- The panel's menus (Budgie Menu; Ubuntu Budgie's AppMenu groups the same way) list what
  `libbudgie-appindexer` indexes: Gio's desktop entries, grouped by their main category with the
  names of `desktop-directories` (Development is "Programming"). The library has no
  introspection data: the tier calls it with ctypes (its lists are Gee lists).
- GDK_BACKEND is `x11` in the tier (Budgie sets `wayland,x11`) so xdotool sees Nemo,
  xfce4-terminal and zenity on Xwayland; fork-linux never reads GDK_BACKEND.
- `xdg-open` takes Budgie for GNOME (`GNOME_DESKTOP_SESSION_ID`) and runs `gio open`; folders go
  to Nemo through Budgie's `defaults.list` (`/usr/share/budgie-desktop/applications`).
- Nemo's D-Bus service (`nemo --no-default-window`) quits about 10 s after its last window, so
  `org.freedesktop.FileManager1` is usually only activatable. A Nemo that `xdg-open` starts from
  a program Wine started inherits Wine's seccomp filter, its glycin image loaders are killed with
  SIGSYS and it aborts (seen before the hand-off daemon, when the helper ran inside Wine's tree
  for lack of a systemd user manager; see Cinnamon). `fork-linux-host open`
  of a folder therefore asks `FileManager1.ShowFolders` first (gdbus, else dbus-send, shared with
  `reveal`): the session bus starts the file manager outside Wine's process tree; without a
  FileManager1 service it falls back to `xdg-open`. Nemo shows an action with `Selection=s` only on
  a selected item, not in a folder's own menu: fork-linux installs a second Nemo action,
  `fork-linux-open-folder.nemo_action` (kind `nemo-folder`, `Selection=none`, `%P`, also
  system-wide), next to the item action.
- Nemo orders the actions of its context menu by the order it loaded their folders (system ones
  first, the user's last, but a folder that changes while Nemo runs moves to the end), so "Open
  in Fork" is the third or the fifth item of a Budgie folder's menu. The recipe's keys select it
  by label (`menu-item:Open in Fork`): `dekit` asks the open menu over AT-SPI (Budgie's session
  runs with accessibility, `NO_AT_BRIDGE=0`) how many Down presses reach it.
- Once, Repository > Open In File Explorer did nothing on Budgie (the menu closed, the bus log
  shows no FileManager1 activation; not seen again, not root-caused): `forkui.click` now moves
  the pointer, waits 0.3 s and then clicks, as a hand does, instead of pressing in the same
  instant as the move.
- Ubuntu Budgie's terminal is xfce4-terminal (`org.gnome.desktop.default-applications.terminal`
  `exec` in its `:Budgie` gschema override, also labwc's Ctrl+Alt+T). fork-linux reads that key
  on Budgie before xdg-terminal-exec (`DESKTOP_TERMINAL_SCHEMAS`), so a Budgie user who also has
  GNOME's terminal still gets Budgie's.

## MATE facts the recipe relies on

- The session is Ubuntu MATE's as lightdm and Xsession set it (`XDG_CURRENT_DESKTOP=MATE`,
  `DESKTOP_SESSION=mate`, `XDG_MENU_PREFIX=mate-`, `XDG_DATA_DIRS=/usr/share/mate:...`, where
  ubuntu-mate-default-settings keeps its `mate-mimeapps.list` and `defaults.list`). Ubuntu MATE
  26.04 no longer overrides `org.mate.interface gtk-theme`: its default is MATE's Menta; the
  Yaru-MATE themes come from ubuntu-mate-themes (Appearance > Theme lists them).
- marco names itself "Metacity (Marco)" (`_NET_WM_NAME` of its check window), and like metacity
  it does not frame a window that asks for no decorations (`_MOTIF_WM_HINTS`): Fork's main
  window is managed (in `_NET_CLIENT_LIST`, `WM_STATE` Normal) but stays a child of the root
  window (`frames_undecorated=False`); decorated windows such as mate-terminal are framed
  (checked in the container with an undecorated X window and with Fork).
- `xdotool getwindowgeometry` adds a frame's offset twice for a reparented window (a framed
  mate-terminal reported 11 px right and 39 px down of where it is): `dekit` takes window
  positions from `xwininfo`'s absolute upper-left corner, so window screenshots and clicks
  land on the window under every window manager.
- mate-settings-daemon publishes `Xft.dpi` = font DPI (Appearance > Fonts > Details >
  Resolution, `org.mate.font-rendering dpi`, 0 = the X server's 96) x window scaling
  (`org.mate.interface window-scaling-factor`, 0 = auto): window scaling 2 gives 192, so
  fork-linux's `xrdb` probe reads every MATE scale; no MATE probe is needed.
- When the window scaling changes, mate-settings-daemon restarts the window manager (`marco
  --replace`), and the replaced marco never exits: marco 1.26.2 logs `g_main_loop_is_running:
  assertion 'loop != NULL' failed` from its quit path and stays, one more stale marco per
  change. The recipe's window-scaling command (`_mate_window_scaling`) waits for the new marco
  and ends the replaced ones, as their own exit would have.
- Now and then the X server answered nobody for a minute or more: 1 of 16 integration runs (the
  terminal and reveal tests timed out after 30 s in `xdotool search`) and 4 stress runs (5 to 80
  loops of theme and scale switches, then a new mate-terminal) in the same hour, while the host
  was busy with other real-Fork runs; none in about 7000 stress steps later. During one stall
  Xvfb sat idle in `epoll_wait`, marco's main thread waited in `poll` and its two glycin image
  loaders (`glycin-svg`, `glycin-image-rs`, the loaders gdk-pixbuf 2.44 uses) waited on a futex:
  it looked like marco holding a server grab while it waited for an image (a faked low
  `MemAvailable`, a CPU limit and a memory limit did not bring it back). X queries in `dekit`
  (xdotool, xwininfo, xprop, screenshots) may take up to `X_TIMEOUT` (300 s) instead of failing
  after 30 s. fork-linux is not involved.
  The integrator's stress runs (6 to 8 MATE sessions at once, `-k scale`, next to real-Fork runs
  of other desktops: 5 stalls in about 230 runs) found the client that holds the server: right
  after a font DPI or window-scaling change, with `xrdb -query` waiting for the server for more
  than 300 s, ending the new marco changed nothing and ending **mate-settings-daemon** let the
  server answer at once. The daemon holds a server grab while it updates `Xft.dpi` and does not
  let go (its main thread idle in `poll`). The recipe therefore waits for each scale case's
  `Xft.dpi` with `_mate_xft_dpi`, which ends a daemon that keeps the server from answering `xrdb`
  (the session starts it again, see below); `_mate_window_scaling` does the same when no window
  manager answers 10 s after the change, and starts marco again if that is not enough. Each such
  recovery is noted in `logs/settings-daemon-stalls.txt`.
- mate-settings-daemon segfaulted twice in those stress runs (kernel log: a NULL read at 0x78 in
  `libmate-desktop-2.so.17`), each time right after a window-scaling change, and `Xft.dpi` then
  stayed at its old value. mate-session restarts it (`X-MATE-AutoRestart=true` in its autostart
  entry), and so does the recipe's settings daemon command (a restart loop that says so in
  `logs/settings-daemon.log`).
- mate-settings-daemon republishes `Xft.dpi` asynchronously: under load a scale case once asked
  fork-linux while the previous case's 144 was still published (answer 150 for `font-dpi-192`).
  Every scale case (and the reset) now waits until `xrdb` shows the case's `Xft.dpi` before
  fork-linux is asked.
- MATE's own dark theme BlackMATE and HighContrastInverse do not say "dark" in their names, and
  `org.mate.interface` has no color-scheme key: fork-linux read them as light. `theme.DARK_THEMES`
  now names them (case-insensitive, for every probe that classifies a name; the other mate-themes
  were checked against their `index.theme` comments: Blue-Submarine and Green-Submarine have dark
  panels but light windows, `ContrastHigh` is the light `HighContrast`), and the
  `dark:blackmate` / `dark:highcontrastinverse` cases check it. The real-Fork tier's dark step
  uses Yaru-MATE-dark.
- Caja (`caja --no-default-window`, as mate-session and its FileManager1 D-Bus service start
  it) draws the desktop and owns `org.freedesktop.FileManager1`. caja-actions puts every
  FileManager-Actions entry in a "Caja-Actions actions" submenu of the folder's context menu
  (Menu, Down x3 to it, Right opens it on "Open in Fork"), and it watches the actions
  directories: a Caja that was running when `install.sh` wrote the action shows it without a
  restart, so fork-linux rightly prints no restart notice for Caja.
- mate-panel's Applications menu (libmate-menu on `mate-applications.menu`) lists
  `Categories=Development` entries in its "Development" menu, shown as "Programming". The
  MateMenu binding has no `TreeEntry.get_app_info`; the reader takes the command from the
  entry's desktop file. Like Cinnamon's CMenu, libmate-menu watches only the application folders
  that existed when the session started (its tree's `changed` never fires for a
  `~/.local/share/applications` created later): install.sh warns "log out and back in"
  (`MENU_RELOGIN`) and the tier restarts the session before it reads the menu
  (`menu_after_login`). mate-panel itself does not run in the tier; the reader is a new libmate-menu
  tree, which is what the panel loads at the next login.
- MATE's portal backend is xdg-desktop-portal-gtk (`mate-portals.conf`), which reads GNOME's
  `color-scheme`; MATE never writes it, so the portal states no preference for any MATE theme.
- `xdg-open` on MATE runs `gio open`, so web links follow `mimeapps.list` (the user's entry
  wins over `mate-mimeapps.list`, which names firefox). Nothing in the image binds Ctrl+Alt+T
  (marco's `run-command-terminal` is disabled, mate-settings-daemon's media keys have no
  terminal key), unlike Xubuntu; the tier still uses Fork's Repository menu, as on every desktop.

## Cinnamon facts the recipe relies on

- The image is Ubuntu 26.04 with Ubuntu Cinnamon's packages (`ubuntucinnamon-environment`: its
  gschema overrides make `Yaru-cinnamon-dark` the default theme). Linux Mint's Mint-Y themes are
  not packaged for Ubuntu 26.04; fork-linux reads the theme by its name, so Mint-Y / Mint-Y-Dark
  behave like Yaru-cinnamon / Yaru-cinnamon-dark (unit tests in `tests/test_theme.py`).
- The session is what lightdm and cinnamon-session export (`XDG_CURRENT_DESKTOP=X-Cinnamon`,
  `DESKTOP_SESSION=cinnamon`; `cinnamon.session` has `DesktopName=X-Cinnamon`, and nothing sets
  `XDG_MENU_PREFIX`): the menu applet loads `cinnamon-applications.menu` by name with CMenu
  (Cinnamon's GMenu), where `Categories=Development` is the "Programming" folder.
- The settings daemon is what cinnamon-session's autostart starts for settings: csd-xsettings
  (XSETTINGS, `Xft.dpi`) and csd-settings-remap, which copies Cinnamon's interface keys (theme,
  text scaling factor, ...) into `org.gnome.desktop.interface` for GNOME applications.
- The window manager is **cinnamon** itself (`cinnamon --replace`, what cinnamon-session's
  cinnamon-launcher runs): the panel, the menu applet and the window manager on libmuffin, drawn
  with Mesa's software GL on the dummy Xorg. Until 2026-10-11 the tier ran muffin's own `muffin`
  binary instead, which is not what a Cinnamon session runs.
- Cinnamon's menu (CMenu, like MATE's libmate-menu) watches only the application folders that
  existed when the session started: an entry written into a `~/.local/share/applications` created
  later never shows up (checked with Cinnamon's own `AppSystem` and with CMenu's `changed` signal;
  GIO, which GNOME Shell and Budgie use, and XFCE's garcon notice the new folder). The tier's home
  is new, so install.sh creates the folder: fork-linux now warns "log out and back in"
  (`MENU_RELOGIN`) when it had to create it on Cinnamon or MATE, and the tier checks the warning
  and restarts the session before it reads the menu (`menu_after_login`). Our entry is then
  launched with the menu applet's own `App.open_new_window`, through Cinnamon's `org.Cinnamon.Eval`.
- Xvfb cannot host a Cinnamon UI scale: its only RandR mode has no refresh rate, muffin refuses it
  ("Invalid mode 1920x1080 (-nan)") and keeps its monitor configuration unscaled. The recipe runs
  Xorg with the dummy driver instead (`Desktop.x_server`, rootless, one 1920x1080@60 monitor from
  `/etc/X11/fl-e2e-dummy.conf`). System Settings > Display > User interface scale is a monitor
  configuration of muffin's `org.cinnamon.Muffin.DisplayConfig` (`ApplyMonitorsConfig`, as
  Cinnamon's display panel does, then "Keep changes" in cinnamon's "Keep these display settings?"
  dialog, which otherwise reverts the scale after its countdown); the legacy `scaling-factor` key
  is not applied live.
  csd-xsettings then publishes `Xft.dpi` = 96 x scale x text scaling factor (96, 192, 144 for text
  1.5, 240 for scale 2 with text 1.25); each scale case waits for that value.
- Dark mode: Themes > Settings > Dark mode (and the Light / Dark buttons of the simplified Themes
  page) is `org.x.apps.portal color-scheme`, what xdg-desktop-portal-xapp hands applications;
  `org.cinnamon.desktop.interface` has no `color-scheme`. fork-linux asked only the theme name, so
  a light theme with "Prefer dark mode" was light; `theme._cinnamon` now asks `org.x.apps.portal
  color-scheme` first (dconf, then gsettings; `default` falls through to the theme name).
- Scale: csd-settings-remap copies the text scaling factor, but not the UI scale, into
  `org.gnome.desktop.interface`, so asking GNOME's keys first read scale 2 with text 1.25 as 125 %
  while every X client got 240 DPI; Cinnamon's own probe is now `Xft.dpi`
  (`DESKTOP_PROBES["cinnamon"]`).
- cinnamon's window manager names itself "Mutter (Muffin)" and, like mutter, frames only
  decorated windows: Fork's undecorated main window stays a child of the root window
  (`frames_undecorated=False`).
  Started without a user action, a relaunched Fork may open behind the window the user used last
  (focus-stealing prevention): step 08 activates it before its screenshot, as a click would.
- Nemo: `nemo --no-default-window` (its FileManager1 D-Bus service) owns
  `org.freedesktop.FileManager1` at start and exits when it has no window; in a Cinnamon session
  nemo-desktop draws the desktop and every Nemo window is a new or D-Bus activated process. Nemo
  watches its action folders, so actions installed while it runs show at once (no restart). An
  action with `Selection=s` is listed only for a selected item; the folder a window shows needs a
  `Selection=none` action (`nemo-folder`, see Budgie). The Menu key opens the context menu at the pointer; the folder
  menu is Create New Folder, Create New Document, the folder actions (ours), Open in Terminal, ...
- The terminal is System Settings > Preferred Applications > Terminal
  (`org.cinnamon.desktop.default-applications.terminal exec`, the chosen terminal's desktop-entry
  `Exec`), which Nemo's "Open in Terminal" starts. The tier's user ran Ubuntu Cinnamon's
  gnome-terminal once (gnome-terminal then writes itself into `~/.config/x-cinnamon-xdg-terminals.list`
  and `xdg-terminals.list`, which xdg-terminal-exec reads) and picked Tilix there (and turned off
  Tilix's first-start VTE configuration notice, `warn-vte-config-issue`). Before
  gnome-terminal has run, xdg-terminal-exec 0.14 picks `org.gnome.Terminal.Preferences.desktop`
  (`gnome-terminal --preferences`, no terminal) or whichever terminal entry it finds first, and
  afterwards gnome-terminal, never the user's choice. `fork-linux-host terminal` now reads the
  Preferred Applications key on `X-Cinnamon` before xdg-terminal-exec (on the host inside
  Flatpak), drops desktop-entry field codes, and uses it when it is installed: a known terminal
  gets its own directory arguments, another one runs as stored, in the directory. Without an
  answer `X-Cinnamon` prefers gnome-terminal in the generic order.
- The container has no systemd user manager. Until 2026-10-11 `fork-linux-handoff` then ran the
  host helper directly inside Wine's process tree (`NoNewPrivs: 1`, `Seccomp: 2`), and a GTK 3
  program started from there aborted when gdk-pixbuf's glycin loader was used for a raster icon
  (Tilix: status 134, `ensure_surface_for_gicon: assertion failed ... Loader process exited
  early`; programs whose window comes from a process the session bus starts, such as
  gnome-terminal-server or a FileManager1 service, and xfce4-terminal were not affected), so the
  recipe's user had picked Xfce Terminal. Now (maintainer decision for 1.0.0) the launcher starts
  a hand-off-only daemon outside Wine's tree when there is no systemd user manager, and
  `fork-linux-host --handoff` asks it to start the helper (bridge/README.md, "Desktop
  hand-off"): the recipe's user picked Tilix, and step 05 checks that the terminal Fork opens
  runs without Wine's `NoNewPrivs` and seccomp filter and stays open. Folders still open through
  FileManager1 first (see Budgie).

## Adding a desktop

1. `docker/Dockerfile.e2e.de.<key>` (copy the XFCE one: `FROM ubuntu:26.04`, `# Image tag:
   fork-linux-e2e-de-<key>:26.04`, the same Wine host libraries and tier tools, plus the desktop's
   real components).
2. A `Desktop` entry in `tests/e2e_de/desktops.py` (session variables, where `{home}` is the
   session's home, settings daemon, window
   manager, file manager + service, terminal, theme and scale commands as the desktop's own
   settings dialogs run them, reset commands, context-menu keys, the menu's launcher), and a
   checker in `test_de_integration.py` for any new integration kind, menu reader or browser kind.
   An empty window manager or file-manager service means the session manager (the recipe's
   settings daemon) starts it; `session_exports` names variables the session manager hands
   its applications; `scale_restarts_session` restarts the session for each scale case;
   theme cases beyond `light` / `dark` are named `light:<how>` / `dark:<how>`; `menu_folder` is
   the menu folder that lists our entry; `components` are the desktop's own programs the session
   checks (and `restarted` the ones that may be started again), `portal` the xdg-desktop-portal
   backend that answers its Settings.
3. `tests/test_docker.py` `EXPECTED`; the desktop in the `desktop` matrix of the `e2e-de` jobs
   of `.github/workflows/check.yml` and `e2e-wine.yml` (and in `DESKTOPS` of
   `tests/test_workflows.py`); `tests/test_e2e_de_recipes.py` keeps images, recipes, checkers and
   both CI matrices in step on its own.
4. Run both tiers, record the result in the matrix above and in `docs/COMPATIBILITY.md`.

## Final integration run (2026-10-12)

On the merged v1.0.0 tree (Wine 11.18, the images with the Noto fonts): the integration tier
passed on all 8 desktops (xfce 15, gnome 16, kde 20, kdewayland 19, cinnamon 17, mate 19, budgie 16,
lxqt 19: 141 tests), and the real-Fork tier 9 of 9 on xfce, gnome, kde and cinnamon (mate,
budgie, lxqt and kdewayland were not run in this pass; they passed 9 of 9 each later that day, on
the same tree with Wine 11.18). Two changes came out of it: the images
install fonts-noto-core and fonts-noto-cjk (as the flavours do; without them doctor's
`prefix.font_fallback` warned), and the hand-off check no longer expects Wine 11.18 to confine
Fork (it gives Fork no NoNewPrivs and no extra seccomp filter).

## Release round 1 (2026-10-12)

On the release tree after round 1's fixes, every desktop ran both tiers under run names of their
own (`ci-e2e-de.sh --name rf1-de-<desktop> <desktop> [--fork]`): the integration tier passed on all 8
(141 tests) and the real-Fork tier 9 of 9 on all 8 (xfce, gnome, kde, kdewayland, cinnamon, mate,
budgie, lxqt), Wine 11.18 downloaded in each root.
