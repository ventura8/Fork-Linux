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

Run the tier with this command:

```
FL_E2E_FORK=1 FL_E2E_ROOT=/var/tmp/fork-linux-e2e-pytest FL_E2E_SEED=<downloads> \
  FL_E2E_WINETRICKS_CACHE=<winetricks cache> python3 -m pytest -v tests/e2e
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
