# E2E results: official Fork 2.23.2 on the managed Wine runtime

Date: 2026-10-09. Host: Ubuntu 26.04 x86_64, Python 3.14 (unit tiers also pass on 3.10 in
`fork-linux-ci-jammy:22.04`). Wine: managed `kron4ek-11.0-staging-wow64`. Display: Xvfb `:98`,
1600x1000x24. CLI run from the source tree (`PYTHONPATH=src python3 -m fork_linux`) and once through
a rendered `bin/fork-linux` launcher (`@PYTHON@` replaced, `fork` symlink).

**Isolation.** The E2E root had a fake `HOME`, all XDG directories, and `XDG_RUNTIME_DIR` set to mode
0700. `~/.wine/SENTINEL` was created inside the fake home. It was still there after
`uninstall --purge`. The real home and the real `~/.wine` were never used. The planned root under
`/tmp` (a tmpfs with `usrquota`, shared with other jobs) failed with "Disk quota exceeded" while
dotnet40 was unpacking. The root was then moved to `/var/tmp/fork-linux-e2e-*`, and the old `/tmp`
path was kept as a symlink to it, so a symlinked `HOME` was tested as well. Screenshots stayed in
`<root>/shots`. They show Fork's logo and must never be copied into the repository.

**Caches.** The downloads cache was seeded with `wine-11.0-staging-amd64-wow64.tar.xz`,
`Fork-2.23.2.exe` and `winetricks-20260125`, under the same file names as the URLs. Every file
was re-verified by size and sha256. The winetricks cache was also seeded with the installers for
dotnet48 and corefonts.

## Automated tier

The run below used this command on the host:

```
FL_E2E_FORK=1 FL_E2E_ROOT=/var/tmp/fork-linux-e2e-pytest FL_E2E_SEED=<downloads> \
  FL_E2E_WINETRICKS_CACHE=<winetricks cache> python3 -m pytest -v tests/e2e
```

Since 2026-10-10 the tier runs **only inside a container** (AGENTS.md hard rule 18; on the host
pytest stops at once). Today's equivalent, with the seed in `/var/tmp/fork-linux-e2e/seed`:

```
scripts/e2e-docker.sh --name e2e pytest tests/e2e
```

Result: **7 passed in 314.75 s**. It ran from a fresh prefix, with only the caches reused.

| Test | Time |
|---|---|
| `test_01_setup_completes_and_resumes`: full setup, `--list-steps` all done, no-op re-run in under 10 s | 259.6 s |
| `test_02_doctor_has_no_failures`: `doctor --deep --json`, no `fail` and no `warn` | 0.8 s |
| `test_03_first_launch_opens_the_repository`: first-run dialogs, main window, screenshot std-dev above 0.02 | 19.9 s |
| `test_04_second_tab_and_fork_alias`: `run second` fast path plus `main([...], prog='fork')`, still one Fork process | 4.6 s |
| `test_05_relaunch_applies_dark_theme`: Alt+F4, relaunch with `FORK_LINUX_DISPLAY_THEME=dark`, settings checked, mean brightness below 0.35 | 16.7 s |
| `test_07_snapshot_and_rollback`: rollback refused while running (exit 15), snapshot create, `rollback --to-version 2.23.2 --no-pin`, Fork launches again | 11.7 s |
| `test_10_logs_bundle_and_purge_second_prefix`: bundle has no `accounts.json` and secrets are redacted; uninstall, then `--purge --yes` with `FORK_LINUX_PREFIX=prefix2`; `~/.wine` untouched | 1.0 s |

## Fork-update tier (`tests/e2e/test_fork_update.py`)

Date: 2026-10-11, in `fork-linux-ci-e2e-wine:26.04` (Python 3.14, Wine `kron4ek-11.0-staging-wow64`,
Xvfb 1600x1000), seeded with Wine, winetricks, Selawik and the .NET / core-font cache; Fork 2.23.1
came from `https://cdn.fork.dev/win/Fork-2.23.1.exe`, the updates from Fork's own feed. Bridge built
with `scripts/build-bridge.sh`.

```
scripts/build-bridge.sh
FL_E2E_FORK_UPDATES=1 scripts/e2e-docker.sh --name upd-tier pytest --durations=0 tests/e2e/test_fork_update.py
```

Result: **12 passed in 675 s**, twice in a row from a fresh prefix (`<root>/fork-update`). What Fork
did is read from its `velopack.log`, `/proc`, the bridge daemon's log and native git; dialogs and
notifications went to stand-in `zenity` / `notify-send` scripts that record them.

| Test | Time |
|---|---|
| `test_00_setup_installs_fork_n_minus_1`: `setup --fork-version 2.23.1 --allow-untested` (trust on first use); the installed full nupkg equals the feed's SHA256 | 287.8 s |
| `test_f9_stable_channel_reads_the_stable_feed`: channel Stable, `update --check` names `--latest`; File > Check for Updates... reads `/win/stable/releases.win.json` (2.21.1), "No Updates Available"; the first launch kept a 2.23.1 snapshot | 44.4 s |
| `test_f5_known_bad_override_warns_and_doctor_fails`: override marks 2.23.1 known-bad: doctor exit 17 with the rollback hint, `update --check` warns, `--fork-version 2.23.1` refused (exit 2), `fork` shows the warning and starts Fork | 15.1 s |
| `test_f6_staged_update_reported_and_dropped_when_pinned`: Check for Updates stages 2.23.2 (Close, no restart); doctor and `update --check` report it; pinned, the next launch deletes it before Fork starts (no "Auto apply"), puts 2.23.1's package back (the feed's SHA256) and turns Fork's update check off | 42.4 s |
| `test_f1_in_app_update_keeps_env_and_notifies`: Restart and Update; the new Fork keeps our environment (Wine adds only `WINESERVERSOCKET`, `WINEPRELOADRESERVE`, `GNUTLS_SYSTEM_PRIORITY_FILE`); the next launch sends "Fork updated 2.23.1 -> 2.23.2" through `notify-send` without waiting for it, re-runs the icon step, snapshots 2.23.2; doctor ok / ok / info | 45.6 s |
| `test_f10_fork_path_during_restart_and_update`: `fork <path>` during the apply waited for Velopack and reached the restarted Fork (one Fork, the tab open); `update --fork` and `rollback` refused with exit 15 ("Fork is running" / "Fork is updating itself"); afterwards the fast path opens a tab | 7.2 s |
| `test_f4_rollback_pins_and_blocks_the_in_app_update`: `rollback --to-version 2.23.1` pins it, `ApplicationUpdateType = 2` (0 saved); Fork's own check reads the Stable feed and offers nothing; a relaunch stays on 2.23.1; unpinning brings 0 back | 47.0 s |
| `test_f7_bridge_survives_the_self_restart`: bridge on, Restart and Update: same daemon pid and port, the restarted Fork stages and commits through it (native git), the daemon exits `prefix-idle` after Fork | 63.4 s |
| `test_f2_cli_update_preserves_user_data`: refused while Fork runs (15); `update --fork` 2.23.1 -> 2.23.2 keeps `settings.json`, `accounts.json`, `custom-commands.json`, Fork's logs and ForkData byte for byte; copy snapshot without `accounts.json`; the three steps re-ran | 17.7 s |
| `test_f19_update_fork_keeps_the_bridge_working`: the next `fork` starts one daemon, no first-run dialog; stage + commit through native git | 38.6 s |
| `test_f3_update_fork_latest_verifies_against_the_feed`: `update --fork --latest` installs the feed's newest version, its package equals the feed's SHA256, `fork.request = latest`, user files kept | 8.3 s |
| `test_f23_staged_update_applied_at_the_next_start`: a staged 2.23.2 is applied by Velopack as Fork starts ("Auto apply"): the path opens in the new Fork, which is the only one, uses the launch's bridge daemon and port; the next launch notices the update | 57.1 s |

Found by this tier and fixed (with unit tests): the official installer deletes Fork's whole
`AppData\Local\Fork`, so `update --fork` lost the user's settings, accounts, custom commands, logs and
unpacked git (UPD-23); after the pin dropped a staged update, the installed version's package
(deleted by Velopack when it staged the update) was missing (UPD-24). Observed, not ours: once the
delta package failed to apply in Velopack ("invalid Zip archive"), which then downloaded the full
package by itself.

## App-upgrade tier (`tests/e2e/test_wine_app_upgrade.py`)

Date: 2026-10-11, in `fork-linux-ci-e2e-wine:26.04` (Python 3.14, Wine `kron4ek-11.0-staging-wow64`,
Xvfb 1600x1000), seeded like the tier above. Fork 2.23.1 came from `https://cdn.fork.dev/win/` (trust
on first use against Fork's feed), Kron4ek 11.19 staging from GitHub (pinned by URL, size and sha256 in
a test-only manifest override), and Fork 2.23.2 from Fork's own updater. An "app upgrade" is a copy of
this tree's `fork_linux` package with a higher `VERSION` and one bumped step revision, started through
the same `fork-linux` on `PATH` with the same helper directory, as a package replaces its files in place.
The two bridge builds of A6 (`VERSION` 1.0.1 and 1.0.2) were made with `scripts/build-bridge.sh --out`.

```
FL_E2E_APP_UPGRADE=1 FL_E2E_BRIDGE_BUILDS=/e2e/bridge \
  scripts/e2e-docker.sh --name upd-app-wine --keep pytest --durations=0 tests/e2e/test_wine_app_upgrade.py
```

Result: **7 passed in 884 s**, from a fresh prefix (`--keep` reuses only the download cache and the
bridge builds in the scratch root).

| Test | Time |
|---|---|
| `test_01_setup_with_the_previous_fork`: setup with Fork 2.23.1, `--list-steps` says nothing to re-run, first launch (first-run dialogs answered), a 2.23.1 snapshot | 281.6 s |
| `test_02_a3_upgrade_reruns_only_the_bumped_step`: N bumps `display_dpi` and the manifest's revisions: `--list-steps` names exactly that step, the first launch re-runs it alone, records the new revisions and `written_by`, downloads nothing | 13.2 s |
| `test_03_a5_upgrade_while_fork_runs`: N+1 (`fork_settings` bumped) installed while Fork runs: `run second` hands over to the running Fork, which stays the only one, and no marker changes; the launch after Fork closed re-runs that step alone | 15.7 s |
| `test_04_a4_wine_pin_change_and_revert`: a persistent wineserver of 11.0 left in the prefix, pin changed to 11.19: setup re-runs `wine_runtime` and `prefix_init`, stops the old server with 11.0's own `wineserver -k`, boots the prefix with 11.19 (verified archive and `.complete`, 11.0 kept); `config unset wine.build` goes back without a download, nothing heavy re-runs, and Fork starts on 11.0 | 460.7 s |
| `test_05_a11_uninstall_then_reinstall_keeps_the_prefix`: `uninstall` removes the menu entry and clears the `icon` / `desktop_entry` markers only; Fork's settings stay; the next launch re-runs those two, with no first-run dialog | 13.6 s |
| `test_06_a6_bridge_binaries_replaced_while_the_daemon_runs`: bridge on with build 1; build 2 renamed over it while the daemon serves Fork: same daemon and port, its executable shows `(deleted)`, `git rev-parse HEAD` through the installed `git.exe` shim with Fork's environment still answers, the fast path hands over; after Fork closed the next launch re-runs `host_shims` only and starts the new helper | 38.1 s |
| `test_07_a12_app_upgrade_and_fork_self_update_on_one_launch`: while Fork runs, N+2 (`host_integration` bumped) is installed and Fork updates itself (File > Check for Updates..., Restart and Update); the next launch re-runs `host_integration` and `icon` only, keeps 2.23.2 (never reinstalls the requested 2.23.1), reports "Fork updated 2.23.1 -> 2.23.2" once and snapshots 2.23.2 | 60.4 s |

Found by this tier and fixed (with unit tests): updating a prefix that holds .NET for another Wine
build (11.0 -> 11.1, 11.19 -> 11.0) leaves Wine's 32-bit `rundll32 ... Wow64Install` in a "This
application could not be started" dialog; `wineboot` waits 5 minutes for it and `prefix_init` /
`update --wine` then failed on `wineserver -w` (UPD-25: the leftovers are stopped after 2 minutes, the
prefix is ready and Fork starts on it; A4 above still spends ~7 minutes on the way back). The shims are
installed before Fork's settings, whose tools depend on them, so a bridge upgrade no longer re-runs
`fork_settings` twice (UPD-28). Observed, not ours: Fork 2.23.1 does not start on Kron4ek 11.19 staging in
a prefix 11.0 created (`System.IO.FileLoadException` in WPF's BAML reader), which is why A4 checks Fork
after going back rather than on 11.19 (UPD-26); once, the dotnet40 installer of the first setup sat idle
for 20 minutes without a window (the run was stopped and repeated).

## Integration run of the three modules (2026-10-11)

On the integrated `fix/update-tests` tree, each module ran once in a fresh scratch root of its own,
in `fork-linux-ci-e2e-wine:26.04` (seeded), the bridge built with `scripts/build-bridge.sh` and the two
A6 bridge builds made from variant trees 0.9.1 / 0.9.2 (`build-version-variant.sh --tree-only`,
`build-bridge.sh --out`), the way `scripts/ci-e2e-wine.sh` makes them:

```
scripts/e2e-docker.sh --name updint-real pytest --durations=0 tests/e2e/test_real_fork.py
FL_E2E_FORK_UPDATES=1 scripts/e2e-docker.sh --name updint-fork pytest --durations=0 tests/e2e/test_fork_update.py
FL_E2E_APP_UPGRADE=1 FL_E2E_BRIDGE_BUILDS=/e2e/bridge \
  scripts/e2e-docker.sh --name updint-app --keep pytest --durations=0 tests/e2e/test_wine_app_upgrade.py
```

Result: `test_real_fork.py` **7 passed in 310 s**, `test_fork_update.py` **12 passed in 596 s**,
`test_wine_app_upgrade.py` **7 passed in 854 s** (now in its own prefix, `<root>/app-upgrade`). The same
three had passed once before on the tree without that change (7 in 411 s, 12 in 1115 s, 7 in 955 s).

## Review run of the update tiers (2026-10-11)

An adversarial review of `fix/update-tests` added scenario F24 (Fork's own automatic update check),
a real-tier F20 (an interrupted Velopack apply) and, in F1 / F7, Fork restarted by Velopack while our
launcher is not its parent (with the bridge: `fork <path>` takes the fast path and reuses the
session's daemon). Exploration first (Fork 2.23.1 left alone on each channel, nothing clicked): on
Develop Fork reads the feed about a minute after it starts (65 s), downloads 2.23.2 (delta) and shows
"Fork update is ready to be installed" by itself; it does so at most once per interval it keeps in
`settings.json` (`LastUpdateCheck`). No earlier run had seen it: every scenario closed Fork within a
minute. The update modules now seed `LastUpdateCheck` before every start (long ago only where F24
wants the check), so it can no longer stage 2.23.2 uninvited (Velopack would apply it at the next
start, under the app-upgrade tier's feet).

```
FL_E2E_FORK_UPDATES=1 scripts/e2e-docker.sh --name updrev-fork pytest --durations=0 tests/e2e/test_fork_update.py
FL_E2E_APP_UPGRADE=1 FL_E2E_BRIDGE_BUILDS=/e2e/bridge \
  scripts/e2e-docker.sh --name updrev-app --keep pytest --durations=0 tests/e2e/test_wine_app_upgrade.py
```

Result: `test_fork_update.py` **13 passed in 885 s**, `test_wine_app_upgrade.py` **7 passed in 884 s**
(fresh prefixes; `--keep` kept only the two A6 bridge builds). New or changed steps:

| Test | Time |
|---|---|
| `test_f9_stable_channel_reads_the_stable_feed`: + F24 on Stable: the automatic check, nothing clicked, reads `/win/stable/releases.win.json` and stages nothing | 105.1 s |
| `test_f4_rollback_pins_and_blocks_the_in_app_update`: + F24 pinned: no automatic check in 120 s although one is due, no package downloaded | 162.3 s |
| `test_f7_bridge_survives_the_self_restart`: + the restarted Fork's parent is not the test's launcher; after the commit, `fork <path>` hands over to it and reuses the same daemon (one daemon, same port) | 65.1 s |
| `test_f23_staged_update_applied_at_the_next_start`: 2.23.2 now staged by Fork's automatic check (F24, the ready dialog came up by itself), then applied by Velopack as Fork starts | 108.1 s |
| `test_f20_interrupted_apply_is_reinstalled_keeping_the_users_files`: `current\` moved into `packages\VelopackTemp`; the next `fork` reinstalls 2.23.2 from the official installer (feed SHA256 checked), says "Fork 2.23.2 was missing or incomplete; Fork 2.23.2 was installed again", no "Fork updated" notice, no first-run dialog, `accounts.json` / `custom-commands.json` unchanged, doctor `fork.installed` and `fork.integrity` ok | 25.5 s |

Flaky spot, not ours: once (of 7 Restart and Update clicks in these runs, with four packaging cells
building on the same host) Velopack applied 2.23.2 but could not start the new `Fork.exe`
("Apply error: Failed to start application (OS Error 731)", `ERROR_WAIT_1`: the new process died while
Wine started it); Fork stayed closed until started again, which works. `restart_and_update` now names
that error instead of only "no main window". The packaging cells deb, appimage (now also moved and
renamed, `appimage_moved`), flatpak and snap (now also `snap revert` back and forward,
`snap_revert`) passed in the same session, every log read.

## Manual scenario (timings and outcomes)

| # | Step | Outcome | Time |
|---|---|---|---|
| 1 | `setup --accept-fork-eula --no-gui` | First attempt failed in dotnet (tmpfs quota, an environment problem). The resumed run passed dotnet; corefonts then failed once (regedit "Application could not be started"). The next resume wrongly counted fonts as done (**bug 1**). After the fix, the remaining corefonts fonts were installed. A clean full setup on a new prefix later passed in one go. | 253 s (prefix2), 264 s (pytest root) |
| 1 | `setup --list-steps` / re-run | All steps `done`. The re-run is a no-op. | 0.48 s |
| 2 | `doctor --deep --json` | 0 fail, 0 warn. Info only: kdialog missing, bridge off, `fork` alias not on PATH, license reminder. | 0.8 s |
| 3 | `run repos/demo` (first run) | The first attempt aborted with "Setting up ...: cancelled": zenity could not open the display (**bug 2**). After the fix, the "User information" dialog (640x370) appeared, the fields were filled with xdotool, and the main window "Fork" (1000x600) showed the demo repo. | 8.1 s to the dialog, 7.7 s after Finish |
| 4 | `run repos/second` while Fork runs | A new tab opened, exit 0 (fast path). The `fork` alias path also worked. | 2.26 s / 2.31 s |
| 5 | Close, then relaunch with `FORK_LINUX_DISPLAY_THEME=dark` | The pre-launch hook set `Theme=1`, `FollowSystemTheme=false`, `UpdateSubmodulesOnCheckout=false`, `DisableHardwareAcceleration=true`, `LayoutScaling=100`, ShellTool `fl-launch.exe terminal` and `SourceDirectories=[Z:\...home]`, after a backup. The UI was dark. Fork kept the values after Alt+F4. | 7.1 s |
| 6 | Settings preseed experiment (prefix2) | See below. Result: **preseeding is safe; the step and the launcher now seed `settings.json`.** | |
| 7 | `snapshot list` / `create` / `rollback --to-version 2.23.2 --no-pin` | A snapshot of 2.23.2 existed (hardlink). Rollback took 0.19 s. Fork launched afterwards in 8.7 s. Rollback while Fork runs is refused with exit 15. | |
| 8 | `desktop install --file-managers all` + `status` | Installed 12 files. `desktop-file-validate` passes for the menu entry. Icons extracted from the user's own Fork.exe: 16, 32, 64, 128 and 256 px RGBA PNGs under `hicolor/*/apps/`. Exec pointed at a `bin/fork-linux` that does not exist in a checkout (**bug 3**). | |
| 9 | `ssh sync --dry-run` / `ssh sync` | Keys are symlinked, the config is translated and the folder is 0700. The git overlay (`[include]` of `gitconfig-host`, with `core.editor` dropped) is honoured by the bundled git. | |
| 10 | `logs --bundle` | 13 files, no `accounts.json`. Passwords in URLs, `ghp_` tokens, `password=` and `Bearer` were all redacted. | |
| 10 | `uninstall`, then `uninstall --purge --yes` (prefix2) | 12 integration files removed. The purge deleted prefix2 (it has our marker, outside the data dir), plus the cache, data, state and config directories. `~/.wine/SENTINEL` and `~/.cache/winetricks` are intact. | 0.57 s |

Launching through the rendered `bin/fork` launcher, with `FORK_LINUX_LIBDIR=src` and no
`PYTHONPATH`, opened the main window in 7.6 s. Closing Fork with Alt+F4 ends `Fork.exe` within 1 s.

### Settings preseed experiment (open question from the spikes)

Before the first launch of a new prefix, this `settings.json` was written:
`{"Guid": <uuid4>, "UpdateSubmodulesOnCheckout": false, "Theme": 1, "FollowSystemTheme": false}`.

- The "User information" welcome dialog did **not** appear. Instead Fork showed "We need to
  update the Fork git instance to 2.50.1" with a **Start** button (680x374). Clicking Start
  unpacked PortableGit in about 5.5 s, then showed "Done. Enjoy using Fork!" with **Close**
  (680x336). After that the main window opened on the repository.
  With the Selawik interface font (2026-10-10) the same dialog is 680x382 to start, 680x446
  while it updates and 680x340 when done; its one button stays centred 41 px above the bottom
  edge, which is how test_03 finds it now.
- Fork **kept** the seeded Guid and values and added its own keys, 99 in total.
- The welcome dialog is the only place where Fork asks for user.name and user.email. With
  seeding, the identity comes from the host `~/.gitconfig` through our git overlay instead.

**Decision:** seed. `fork_settings.seed()` creates a missing file, adding a fresh uuid4 `Guid`.
The `fork_settings` step and the launcher's pre-launch hook use it, so the first session already
runs with the Wine-safe values, the theme and the scaling. The automated tier (test_03) follows
the new first-run flow.

## Bugs found and fixed (with regression tests)

1. **The fonts step counted an interrupted corefonts as done.** It only checked `arial.ttf`.
   corefonts installs its fonts one by one, so a failed run left Arial without Times New Roman,
   Verdana and the others, and the step was marked done. The step now requires `corefonts` in
   `winetricks.log` and retries corefonts once after a transient failure. Doctor
   `prefix.corefonts` warns when the fonts are only partly installed. Tests:
   `test_fonts_partial_corefonts_is_not_done`, `test_fonts_retries_a_transient_failure`,
   `test_fonts_failure` (tests/test_steps_fonts.py), `test_prefix_corefonts`
   (tests/test_doctor.py). The `FakeWine` fixture now writes `winetricks.log` and supports
   `fail_once`.
2. **A zenity progress dialog that could not open the display was taken as "user cancelled".**
   This happens with a stale `DISPLAY`, or `GDK_BACKEND=wayland` without Wayland. It aborted
   `fork-linux run` with "cancelled". zenity's stderr now goes to a temporary file, and a
   display error means "carry on without a dialog". Tests:
   `test_zenity_progress_without_a_display_goes_on`, `test_zenity_progress_without_a_display_at_finish`,
   `test_zenity_progress_cancel_is_still_declined` (tests/test_ui.py).
3. **In a source checkout, the menu entry and file-manager actions pointed at `<repo>/bin/fork-linux`,
   which is never rendered there.** `desktop status` still said ok. `launcher_command` now falls
   back to `python3 -I <repo>/bin/fork-linux.in`; the template finds `src/` by itself. Tests:
   `test_launcher_command_in_a_checkout_runs_the_template`,
   `test_launcher_command_falls_back_to_the_installation` (tests/test_desktop_integration.py).
   `test_status` and `test_desktop_entry_is_installed` were adjusted.
4. **Misleading error for a new `FORK_LINUX_PREFIX` outside the data dir.** It said "it was not
   created by fork-linux" for a path that does not exist. It now says that new prefixes are only
   created under the data dir, or that you can create the directory and adopt it. The rules
   themselves are unchanged. Test: `test_missing_prefix_outside_data_dir_says_so`
   (tests/test_paths.py).
5. **Settings seeding**, the change decided by the experiment above. Tests:
   `test_seed_creates_the_file_with_a_guid` (tests/test_fork_settings.py),
   `test_settings_are_seeded_before_the_first_start` (tests/test_steps_fork.py) and
   `test_settings_hook_seeds_a_missing_settings_file` (tests/test_launch_flow.py). The step's
   `settings_present` input was dropped, because the step creates the file.
6. **Doctor `fork.gitinstance` warned before the first start once seeding was in place.** It used
   the existence of `settings.json` as the sign that Fork had started. It now looks for
   "Start IPC server" in `fork.log`; the installer's Velopack hook writes a `fork.log` without
   that line. Test: `test_fork_gitinstance` (tests/test_doctor.py).
7. **E2E gate placement (a test-infrastructure finding).** A module-level `pytest.skip` in
   `tests/e2e/conftest.py` ends the **whole session** under pytest 6.2.5, the version in the
   jammy CI image ("1 skipped", nothing else runs). Under pytest 8 it crashes `pytest tests/e2e`.
   The gate is therefore the module-level skip in `test_real_fork.py`; the conftest only holds
   the fixtures.

## Remaining problems (not fixed here)

- **Fork's UI shows exec-bit-only changes on Linux-created repositories** (`run.sh` "M", empty
  diff), even though the launcher exports `GIT_CONFIG_*` `core.filemode=false`. The bundled
  `git.exe status` honours the override (only `link.md` is listed). Fork's own status engine
  reads the repository's `.git/config` (`filemode = true` from Linux `git init`). Symlinks show as
  modified either way, as known from spike S6. Fixing this needs a per-repository opt-in
  (`core.filemode=false` in the repo) or the native-git bridge. A doctor hint would help.
- **`uninstall --purge` is not per-prefix.** With `FORK_LINUX_PREFIX=prefix2` it also deletes the
  whole data dir: the default prefix, the runtimes and the snapshots. It only checks that Fork is
  not running in the *selected* prefix, so a Fork running in the default prefix would not stop it.
- **A Fork that has lost its window keeps the fast path busy.** `xdotool windowclose`
  (XDestroyWindow) left `Fork.exe` running without a window for minutes. The next `fork-linux run`
  forwarded to it: exit 0, nothing shown, and "Cannot read cliRequest from pipe" in fork.log. A
  normal close (Alt+F4 or the title-bar X) exits cleanly within 1 s, but a hung or crashed UI would
  look the same to users. A "no window appeared" fallback, or a `run --restart`, would be worth
  adding.
- **Setup's disk-space preflight uses free space only and ignores per-user quotas.** A
  quota-limited tmpfs failed dotnet with the hint "check your network connection", which is
  misleading for EDQUOT and ENOSPC.
- **`ssh sync --dry-run` reports "would link" for links that are already current.** It is
  cosmetic.
- **`desktop remove` / `uninstall` leave empty directories behind:** `applications/` (with an
  empty `mimeinfo.cache`), `icons/hicolor/<N>/apps/`, `nautilus*/`, `nemo/actions/`,
  `kio/servicemenus/`, `file-manager/actions/` and `Thunar/`.
- **The KIO service menu and the FMA action file do not pass `desktop-file-validate`.** This is
  expected: they are KDE and FMA formats, not freedesktop Application entries. Only the menu entry
  is validated.
