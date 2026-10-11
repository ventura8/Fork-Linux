# E2E results: official Fork 2.23.2 on the managed Wine runtime

## 2026-10-11 (evening): the OS-integration area again, on `fix/integration`'s latest code

**Scope.** The setup, launch, integration, display, bridge, doctor, rollback and uninstall modules
(91 tests: S-, L-, I-, B-, D-, R- and U- scenarios, plus the G- tests that live in the bridge
module), after merging `fix/integration` (d2f0241). Each run: `scripts/e2e-docker.sh --name
osint-rN pytest -p no:randomly` over those ten modules, from a fresh scratch root.

| Run | Result | Time | Failure |
|---|---|---|---|
| 1 | 91/91 | 54:20 | |
| 2 | 90/91 | 52:28 | B-PERFORMANCE: 1 of 30 bundled `git.exe` calls failed (Wine's process-start transient) |
| 3 | 90/91 | 52:00 | G-GITFLOW: Fork's *Git Error* `OS Error 731` on the last step of *Initialize Git Flow* |
| 4 | 91/91 | 50:11 | |
| 5 | 91/91 | 51:53 | |

Runs 4 and 5 ran the final code.

New checks: Diff and Merge in Linux now prove that Fork waits (the stand-in tool reads Fork's files
only after a pause, and `fl-launch.exe` runs as long as the tool and ends with it); a conflict where
both branches added the file goes through Merge in Linux; the Selawik test also opens a diff and
checks that Consolas is drawn with the host's monospace font (DejaVu Sans Mono in the image).

Fixed:

1. **Merge in Linux did nothing when both branches added the file.** Fork 2.23.2 passes an empty
   `$BASE` then, and `fork-linux-host merge` refused it (exit 2, no message). The tool now gets an
   empty base file next to Fork's local copy, deleted after the tool exits (1107c5a).
2. **`setup --reset` raced what an interrupted setup left running** (the same "Directory not empty"
   as the .NET rebuild): it now ends those processes before deleting the prefix, and does nothing
   to a prefix without our marker (dfdb2f7).
3. B-PERFORMANCE runs a series again (logged) when Wine fails one of its calls (182486f).
4. ForkUI recognises Wine's failed git start in both of Fork's reports (`StartWithCreateProcess` and
   `os error 731`); G-GITFLOW closes it as a user would, and the README names the Git Flow case
   (49a2e74).

## 2026-10-11: every user-facing scenario, end to end (`fix/e2e-coverage`)

**Catalog.** `tests/e2e/scenarios.json` lists 290 user-facing scenarios, and
`docs/qa/E2E-SCENARIOS.md` is generated from it (`scripts/gen-scenarios-doc.py`).
`tests/test_e2e_scenarios.py` enforces both. 155 scenarios are this branch's: git 57, setup 23,
integration 22, launch 16, doctor 13, bridge 9, uninstall 8, rollback 7. The other 135 are
pending the merge of the branches that provide them: `fix/de-e2e` (112, 16 aspects × 7 desktops),
`fix/update-tests` (22: F1-F7, F9, F10, F19, F23, A1-A6, A8-A12) and `fix/font-fallback-472`
(FF-DOCTOR). `gen-scenarios-doc.py pending` finds every pending test on its branch. After a merge,
run `declare BRANCH`, then `clear-pending BRANCH`.

**Setup.** Runs happen only in containers: `scripts/e2e-docker.sh --name cov-full pytest -p
no:randomly -v tests/e2e`, image `docker/Dockerfile.e2e.wine` (Ubuntu 26.04). The display is Xvfb
1600x1000 with no window manager, and Fork is driven by xdotool. The runtime is managed
`kron4ek-11.0-staging-wow64` with the official Fork 2.23.2. Each run starts from a fresh scratch
root; only the seeded download cache is reused. Fifteen modules run in the order of
`tests/e2e/conftest.py` `MODULE_ORDER` (uninstall last).

| Module | Run 1 | Run 2 | Run 3 | Run 4 | Run 5 |
|---|---|---|---|---|---|
| setup_errors | 7/7 | 7/7 | 7/7 | 7/7 | 7/7 |
| fork | 7/7 | 7/7 | 7/7 | 7/7 | 7/7 |
| launch | 12/12 | 12/12 | 12/12 | 12/12 | 12/12 |
| git_history | 11/11 | 11/11 | 11/11 | 11/11 | 10/11 |
| git_changes | 6/7 | 7/7 | 7/7 | 7/7 | 7/7 |
| git_branches | 10/10 | 10/10 | 10/10 | 9/10 | 10/10 |
| git_remotes | 8/8 | 8/8 | 8/8 | 8/8 | 8/8 |
| git_advanced | 3/4 | 3/4 | 4/4 | 4/4 | 4/4 |
| integration | 11/11 | 11/11 | 11/11 | 11/11 | 11/11 |
| display | 6/6 | 6/6 | 6/6 | 6/6 | 6/6 |
| bridge | 15/16 | 15/16 | 15/16 | 16/16 | 16/16 |
| doctor | 11/12 | 12/12 | 12/12 | 12/12 | 12/12 |
| snapshots | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 |
| setup_variants | 10/10 | 5/11 | 11/11 | 11/11 | 11/11 |
| uninstall | 5/5 | 0/5 | 5/5 | 5/5 | 5/5 |
| **total** | 125/129 (1:28) | 117/130 (1:43) | 129/130 (1:45) | 129/130 (1:30) | 129/130 (1:28) |

Run 1 failures:

- The UTF-8 commit subject lost its last CJK character to `xdotool type`.
- Git Flow's *Initialize* took longer than a minute with about fifty tabs open.
- The passphrase test answered the Fetch dialog, which was still closing, instead of Fork's prompt.
- `logs --all` shows only the end of each log.

Run 2 failures:

- A staging double-click only selected the row.
- The passphrase test failed again.
- The **interrupted-setup bug** below broke the main prefix, and every later setup and uninstall
  test failed after it. A flaky .NET 4.6.2 installer also stopped the new dotnet472 test.

Run 3 failure: the agent and passphrase ssh tests shared one key file, so in a full run the prompt
test found the other test's passphrase on it (it passed on its own). Each test now makes its own key,
and the whole bridge module passed 16/16 in sequence.

Run 4 failure: halfway through the interactive rebase the branch already had three commits, so the
test read the file before the squash. Both rebase tests now also wait for `.git/rebase-merge` to go,
and the branches module passed 10/10.

Run 5 failure: Fork showed `System.ComponentModel.Win32Exception (0x80004005): Unknown error (0x2db)
at Process.StartWithCreateProcess` in the diff pane instead of the diff. Under Wine, Fork's start
of a git process failed. The same "Git request failed" appears now and then in Fork's own log, mostly
for background refreshes nobody sees. A user would select the commit again, and so do both diff tests
now (once, after F5); the history module then passed 11/11.

Every failure was fixed (the commits are listed below), and the failed tests passed again before the
next run.

Other gates on the final code:

- **Unit tier:** 3574 passed, with 100% line and branch coverage on every changed module.
- **CI stages:** the coverage stage passed `check-coverage` with every file at 90% or more. The lint
  stage (ruff, shellcheck, no-suppressions) passed, and so did the jammy (Python 3.10) compat cell.
- **Bridge Wine tier** (FL_REAL_WINE=1): 124 passed. A "wine client error: recvmsg" transport glitch
  failed one shim test once; that call is now retried, as `fl_winetier.py` already did.
- **Packaging cells:** deb, appimage and tarball passed install, upgrade, remove and reinstall.

### Bugs found and fixed (product, with unit tests at 100% line + branch)

1. **`uninstall --purge` deleted prefixes while Fork ran in them.** It checked only the selected
   prefix. Now it refuses (exit 15) while Fork runs in any prefix the purge deletes: the selected
   one, the default one, and every prefix fork-linux created in the data directory. It also stops
   each prefix's wineserver (bd51bce, 8911cfa).
2. **A malformed or unpinned `--fork-version` was refused only after earlier steps had run.** It is
   now refused before any step (e546cc3).
3. **The consent step was skipped when `--only` selected a step that downloads.** Fork's license
   question now always comes first (0da6e17).
4. **HTTPS credentials were never kept.** Fork's bundled git used its `helper-selector`, which falls
   back to Fork's AskPass under Wine and stores nothing. The git overlay now makes Git Credential
   Manager the helper, with the DPAPI store (46fa4c9).
5. **A refused `--wine flatpak` was saved to `config.ini` anyway,** which broke the next setup. The
   choice is now checked before it is saved (11710bb).
6. **A full disk or user quota was blamed on the network.** winetricks failures whose output says
   "No space left on device" or "Disk quota exceeded", or that leave almost no space, now raise
   `DiskFull` with a "free some space" hint. dotnet does not rebuild the prefix for it, and fonts do
   not retry (8c25a82).
7. **`ssh sync --dry-run` reported "would link" for keys that were already linked.** Current files
   are now reported as `unchanged` and are not rewritten (562a31c).
8. **`desktop remove` and `uninstall` left empty folders behind.** These were `applications/`,
   `icons/hicolor/<N>/apps/`, `nemo/actions/` and others, plus `~/.local/bin`. Install now records
   the folders it creates, and remove deletes them once they are empty, along with a leftover
   `mimeinfo.cache` (1bdf4db).
9. **A Fork that had lost its window kept the fast path busy.** `fork-linux run --restart` now ends
   it with the session's `wineserver -k` and starts Fork again (814ffcb).
10. **An interrupted setup broke the prefix for good.** The real tier stopped setup during .NET. The
    winetricks script kept starting installers, the resumed setup's attempt failed, and deleting the
    prefix for the next .NET verb failed with "Directory not empty". Every later setup then failed
    (wineboot exit 53). Before winetricks runs, and before a prefix rebuild, anything still running
    in the prefix is now ended: `wineserver -k`, then SIGTERM and SIGKILL (`procs.stop_prefix`).
    A failed .NET winetricks run is also retried once in place; the 4.6.2 installer of the dotnet472
    chain returned status 67 once (0267d62).
11. The dead `ssh.agent_bridge` setting is gone (7c6b7dd). A resumed download now logs the byte it
    continues from (236e647).

### Third-party behaviour under Wine (documented; not ours to fix)

- **Fork sometimes fails to start a git process** (`Win32Exception ... Unknown error (0x2db)` from
  `Process.StartWithCreateProcess`). It is a transient of Wine's CreateProcess: it was seen once in
  the diff pane in five full runs, and selecting the commit again shows the diff.
- **Git Credential Manager sometimes ends without its window** on the first prompt after Fork
  starts: 2 of 12 fresh starts, and never within a running session. Its trace stops at "Prompting
  for basic credentials". git then asks through Fork's own prompt, and GCM still keeps the account.
  The README says so, `test_forks_own_prompt_when_gcm_cannot_ask` covers that path, and the
  basic-auth test accepts either prompt.
- **An LFS push hung in 1 of 4 runs with Fork's bundled git.** LFS's pre-push hook runs under msys
  `sh`, the same Wine limitation that affects other hooks. The README tells LFS users to enable the
  git bridge. G-LFS (initialize, commit as a pointer) stays on bundled git, and G-LFS-PUSH runs
  through the bridge.

### The 2026-10-09 remaining problems

| Problem | Now |
|---|---|
| Exec-bit changes shown on Linux-created repositories | `fork-linux repo check` / `repo fix` (`core.filemode=false`, recorded and reversible) |
| `uninstall --purge` is not per-prefix | Fixed (1 above): it refuses while Fork runs in any prefix it deletes. Deleting every prefix in the data directory is what `--purge` means, and the README says so |
| A windowless Fork keeps the fast path busy | Fixed (9 above): `run --restart`, covered by L-RESTART |
| The disk preflight ignores quotas and the hint blamed the network | Fixed (6 above). The preflight still measures free space only: the stdlib cannot read quotas, so the failure is diagnosed instead |
| `ssh sync --dry-run` "would link" for current links | Fixed (7 above) |
| `desktop remove` leaves empty folders | Fixed (8 above) |
| KIO / FMA files fail `desktop-file-validate` | Expected: they are not freedesktop Application entries |

---

The sections below are the 2026-10-09 run, kept as history.

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

### Remote operations in both git modes (`test_git_ops_remote_*.py`)

`remote_ops.py` holds the remote steps (bare remotes, Fetch / Pull / Push, tags, credential
prompts, Clone) that `test_real_git_remotes.py` (bundled git) and `test_git_ops_remote_bridge.py`
(native-git bridge) share. `test_git_ops_remote_ssh.py` pins what bundled ssh cannot do. Runs went
through `scripts/e2e-docker.sh` only, with the three modules each time: `gor-dev` (prefix kept)
and `gor-run4` (fresh scratch root, setup included).

| Run | bundled (8) | bundled ssh (2) | bridge (6) |
|---|---|---|---|
| 1 | 8/8 | (not written yet) | 5/6 |
| 2 | 8/8 | 2/2 | 5/6 |
| 3 | 8/8 | 2/2 | 6/6 |
| 4 (fresh root) | 8/8 | 2/2 | 6/6 |

- Fork remembers Push's "Push all tags" box from the last Push it ran, and a cancelled dialog
  keeps nothing. The old test unticked the box and pressed Escape, so the box stayed ticked and the
  next tag test unticked it instead (run 1). The box is now read from the screen (a ticked box is
  filled blue), clicked until it shows the wanted state (a click that lands while the dialog
  settles is lost, run 2), and set back by a second Push that has nothing new to send.
- The old tag check compared `git rev-parse refs/tags/<tag>` on the remote with `""`. For a
  missing ref rev-parse prints the name it was given, so the check passed before the tag arrived.
  It now compares the tag's commit on both sides.
- With the bridge, an HTTP password is asked in Fork's own prompt (the askpass persona runs
  Fork.AskPass), and a Linux credential helper (`store`, in the test) keeps it. Each bridge test
  also reads the shims' call log (`run --debug`) to check that its fetch / pull / push / tag /
  clone ran as bridge calls.
- **Bug fixed (product):** with bundled git, a key with a passphrase failed with "Permission
  denied (publickey)" and no prompt. msys ssh logs `read_passphrase: requested to askpass`, then
  `no passphrase given`, because under Wine it cannot start the askpass program. An agent in
  `SSH_AUTH_SOCK` does not help either. `ssh sync`, `ssh status` and the new doctor check
  `ssh.passphrase` now name such keys and point at the git bridge, and README lists the
  limitation.

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
