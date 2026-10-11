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
| Lubuntu 26.04, LXQt 2.3 (lxqt-session + openbox, pcmanfm-qt 2.3 + its FileManager1, qterminal, panel menu via GMenu, Kvantum Lubuntu / KvArcDark, Fusion palettes, Global Screen Scaling) | `fork-linux-e2e-de-lxqt:26.04` | ❌ 13/17 until [de-fixes/lxqt.md](de-fixes/lxqt.md) is applied (theme `light` and `light:dark-panel-theme`, PCManFM "Open in Fork", qterminal); ✅ 17/17 with it | ❌ stops at 03 (Fork starts dark on the light Lubuntu desktop); ✅ 9/9 with the patch (setup 294 s from the seed, tier 351 s) | local `scripts/ci-e2e-de.sh lxqt [--fork]`, 2026-10-11 |
| Kubuntu 26.04, KDE Plasma 6.6 on X11 (kwin_x11, kcminit + kded6, Dolphin 25.12 + its FileManager1 and our service menu, Konsole, Kickoff's menu through kbuildsycoca6, Kubuntu / Kubuntu Dark / Breeze Dark global themes and colour schemes) | `fork-linux-e2e-de-kde:26.04` | ❌ 17/18: the `dark:custom-colors` theme case fails until the `theme.py` fix in [de-fixes/kde.md](de-fixes/kde.md) lands (18/18 with it) | ✅ 9/9 (setup 308 s from the seed, tier 369 s) | local `scripts/ci-e2e-de.sh kde [--fork]`, 2026-10-11 |

GNOME, Cinnamon, MATE, Budgie and LXQt follow the same pattern: one
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
...; a value may name `{home}`, the session's home, as KDE's `XDG_CONFIG_DIRS` does). Every
process of the session carries `FL_E2E_DE_SESSION` or its `DISPLAY`; `stop()` ends exactly those.

## Integration tier (`test_de_integration.py`, no Wine)

| Test | What the desktop itself shows |
|---|---|
| install from the release tarball | `install.sh --from-tarball --no-deps`: `fork-linux --version` is `VERSION`, `desktop status` is ok |
| menu entry | `desktop-file-validate`; the desktop's menu library lists it under Development (XFCE: garcon, the panel's library, through `python3-gi`; KDE: `kbuildsycoca6 --menutest`, the menu KService builds for Kickoff); the desktop's own launcher (`Desktop.menu_launcher`: `gio launch`; KDE: `kioclient exec`, KIO's application launcher) starts fork-linux, which asks its first-run license question (zenity), then declined |
| file manager "Open in Fork" | the action is installed next to the desktop's own (XFCE: Xubuntu's "Open Terminal Here" kept in `uca.xml`; KDE: an executable service menu in `~/.local/share/kio/servicemenus` for `inode/directory`); the file manager's real context menu (keys from the recipe) runs `fork-linux open --from-file-manager <folder>` |
| theme light / dark | the desktop's own setting (XFCE: `/Net/ThemeName` Greybird / Greybird-dark; KDE: the Global Themes Kubuntu, Kubuntu Dark and Breeze Dark, Colors on their own, a dark scheme named without "dark") -> `theme.detect` of the installed package |
| scale per case | XFCE: default 100 %, Window Scaling 2x 200 %, custom DPI 192 200 %; KDE: default 100 %, global scale 200 % and 150 %, Force font DPI 192 200 % -> `display.scale_percent` |
| terminal | `fork-linux-host terminal DIR` opens the desktop's terminal (window class) with a shell whose cwd is DIR (`/proc`) |
| reveal | `fork-linux-host reveal FILE` -> FileManager1 -> a file-manager window on its folder |
| open file / open URL | `fork-linux-host open` -> xdg-open -> the default application (a stub registered with `xdg-mime`; XFCE hands web links to its own preferred browser, so the stub is also the `helpers.rc` WebBrowser; KDE asks kdeglobals' `BrowserApplication` first, so the stub is also named there, as Default Applications does) |
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
  of `[Colors:Window] BackgroundNormal` below 192). fork-linux reads only the names, so a dark
  scheme named without "dark" (Nordic, Dracula, a user's own; the tier's "Nightfall" is Breeze
  Dark's colours under another name) gives a light Fork: the `dark:custom-colors` case fails
  until the fix proposed in [de-fixes/kde.md](de-fixes/kde.md) lands.
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
- Not covered: Plasma on Wayland (kwin_wayland + Xwayland), Kubuntu 26.04's default session.

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
  `gtk-theme` on Lubuntu is `Arc-Darker` (dark header bars only), which today's fork-linux reads
  as "dark" (see [de-fixes/lxqt.md](de-fixes/lxqt.md)). The cases are `light` (Lubuntu as
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
  builds a context menu, so it needs no restart. In the folder's context menu, "Open in Fork"
  sits just above Folder Properties: Menu, Up, Up, Return. `pcmanfm-qt DIR` hands the folder to
  the running desktop instance, which owns `org.freedesktop.FileManager1`.
- Terminal: Debian's `x-terminal-emulator` alternative points to qterminal. A qterminal started
  through it is named `x-terminal-emul` in `/proc`, which is how the tier sees that fork-linux
  did not pick qterminal by name (see de-fixes/lxqt.md, problem 3). Lubuntu's global shortcuts
  bind Ctrl+Alt+T to qterminal, so as on XFCE the desktop takes Fork's "Open In Shell" key, and
  the tier uses Fork's menu.
- `xdg-open` on LXQt runs `qtxdg-mat open`, which follows `xdg-mime default` (`mimeapps.list`) for
  files and links alike. Lubuntu's `session.conf` sets `BROWSER=firefox`, which `xdg-open` does
  not use on LXQt.
- Lubuntu installs `fonts-dejavu-core` (`lubuntu-desktop`), and the image does too. Without it,
  `doctor` warns `host.fonts`.
- Flaky spot, seen once in three LXQt real-Fork runs: `fork-linux setup` failed in its `fonts` step
  because Wine could not start `C:\windows\syswow64\regedit.exe` ("ShellExecuteEx failed")
  for one font, twice in a row (after 7 and after 3 fonts had registered). fork-linux retries
  `winetricks corefonts` once, and both tries hit it. The next run passed. Nothing in that step
  depends on the desktop. It is recorded here in case it comes back.

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
   the menu folder that lists our entry.
3. `tests/test_docker.py` `EXPECTED`; `tests/test_e2e_de_recipes.py` keeps images, recipes and
   checkers in step on its own.
4. Run both tiers, record the result in the matrix above and in `docs/COMPATIBILITY.md`.
