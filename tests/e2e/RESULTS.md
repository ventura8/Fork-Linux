# E2E results: official Fork 2.23.2 on the managed Wine runtime

## 2026-10-12 (later): completeness review: Fork's menus, every CLI option, new scenarios (`fix/e2e-coverage`)

**Method.** Fork's menu bar, toolbar drop-downs and context menus were walked and screenshotted in a
real session (`scripts/e2e-docker.sh`, scratch root only; the screenshots show Fork and were deleted
with the root). Every `fork-linux` command and option, README.md, docs/qa/FUNCTIONALITY-REPORT.md
and bridge/README.md were compared with `tests/e2e/scenarios.json`. Each module below ran from a
fresh scratch root, as one e2e-wine.yml job does.

| Module | Result | Time |
|---|---|---|
| test_real_setup_errors | 8/8 | 0:03 |
| test_real_setup_variants | 12/12 | 22:16 |
| test_real_doctor | 14/15 (a test bug, fixed), then 15/15 | 5:57, rerun 6:01 |
| test_real_integration | 14/15, then 15/15 | 13:59, rerun 12:54 |
| test_git_ops_local | 6/6 | 10:28 |
| test_real_git_changes | 7/7 | 7:47 |
| test_real_git_branches | 10/10 | 12:06 |

New scenarios: S-INTEGRITY, S-LIST-STEPS, S-FROM-STEP, S-PREFIX-OPTION, S-ROOT-REFUSED (deb,
deb-jammy, rpm-fedora, rpm-opensuse and arch cells), G-APPLY-PATCH, G-INIT-REPO, G-REMOTE-ADD,
I-CLIPBOARD-OUT, I-OPEN-REPO-DIALOG, I-SETTINGS-FILE, I-DESKTOP-PARTIAL, I-SSH-SYNC-OPTIONS,
D-REPO-FIX-PARTIAL and D-LOGS-SOURCES. D-STATUS now checks `version`, `--version` and `credits` too,
and S-CONFIG-CLI that `set` and `unset` keep the file's comments.

Bugs found and fixed:

1. **Fork's Apply Patch failed on every repository Linux git had indexed** ("error: a.txt: does not
   match index"). Under Wine, Git for Windows sees no inode, device or owner, so every such file
   looked changed to `git apply --index`. `git.env_overrides` now also passes
   `core.checkStat=minimal` to bundled git (never to native git through the bridge).
2. **`sudo fork-linux ...` wrote bytecode caches into `/usr/share/fork-linux`.** The run was refused
   (exit 19), but removing the package then left directories behind (dpkg: "not empty so not
   removed"). The launcher writes no bytecode as root.

The first integration run's failure: Help > Issue Tracker reached `xdg-open` directly instead of
the portal (`gdbus`), so the test, which only reads portal calls, timed out. Wine's winebrowser falls
back to its own browser list when it cannot start `fork-linux-open-url`. It happened once, with five
real tiers on the host; the rerun passed.

Recorded behaviour: Copy Full Path puts the Windows path (`Z:\...`) on the Linux clipboard, and a
repository opened as `H:\...` stays under `H:` in Fork's tab list (README "Known Wine limitations").

## 2026-10-12: every real-tier module, one job each, as e2e-wine.yml runs them (`fix/e2e-coverage`)

**Scope.** All 18 modules of `tests/e2e`, each in its own `scripts/e2e-docker.sh --name
e2ecovint1-<module>` run from a fresh scratch root (setup included, seeded), three at a time, the
way e2e-wine.yml's jobs run them (`<area>-<module>`, one runner and prefix each). Code: 75ba847.

| Module | Result | Time |
|---|---|---|
| test_real_setup_errors | 7/7 | 0:03 |
| test_real_fork | 7/7 | 6:45 |
| test_real_setup_variants | 11/11 | 28:22 |
| test_real_uninstall | 5/5 | 4:45 |
| test_real_launch | 12/12 | 9:37 |
| test_real_display | 5/6, then 6/6 | 10:28, rerun 6:49 |
| test_real_doctor | 12/12 | 5:29 |
| test_real_snapshots | 3/3 | 6:07 |
| test_real_git_history | 11/11 | 12:00 |
| test_real_git_changes | 7/7 | 8:28 |
| test_real_git_branches | 10/10 | 14:08 |
| test_real_git_advanced | 4/4 | 8:46 |
| test_git_ops_local | 3/3 | 6:41 |
| test_real_git_remotes | 8/8 | 17:00 |
| test_git_ops_remote_ssh | 2/2 | 6:44 |
| test_real_bridge | 17/17 | 19:06 |
| test_git_ops_remote_bridge | 6/6 | 13:05 |
| test_real_integration | 11/11 | 10:53 |

The longest job, setup variants, takes under half of e2e-wine.yml's 90-minute limit with the seed.

The one failure: in `test_real_display`, the first Fork start after setup (`theme_follows_the_desktop
[light]`) drew no window within 180 s. Fork's own log stops after it started its IPC servers, before
*Refresh Theme*, and Wine wrote nothing. The next start in the same prefix (the dark case) and a
rerun of the whole module from a fresh root passed. It was seen once, on a host running three real
tiers and a coverage stage at once, so it is recorded here as a Wine start-up stall under load.

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

## Wine pin 11.0 -> 11.18 (2026-10-11)

Runtime pin audit for v1.0.0, all runs through `scripts/e2e-docker.sh` (Ubuntu 26.04 host, image
`fork-linux-ci-e2e-wine:26.04`, `LANG=C.UTF-8`), seeded caches re-verified by size and sha256.
Newest upstream releases that day: Kron4ek Wine-Builds **11.19**, winetricks **20260125**, Selawik
**1.01**, Fork **2.23.2** (Fork's `releases.win.json`; `cdn.fork.dev` has no 2.23.3 / 2.24.0).
winetricks, Selawik and Fork were already pinned to them (sizes and sha256 re-checked against
fresh downloads; `dotnet48`, `dotnet472`, `dotnet40` and `corefonts` are unchanged verbs).

**Kron4ek 11.19 staging fails.** Fork 2.23.2 exits at start, before its main window, with
`System.IO.FileLoadException: The given assembly name or codebase was invalid. (Exception from
HRESULT: 0x80131047)` (in `Fork.UI.CustomWindow..ctor` → `SetResourceReference`; `fork.log`
first says "Cannot initialize TextEditorContextMenu style"), both in a fresh prefix
(`test_real_fork.py`: setup and doctor pass, `test_03` finds no window) and in a prefix 11.0
created and then moved to 11.19. The same with `LANG=en_US.UTF-8` (an image with that locale), so
the user's locale is not the trigger. Wine 11.19 updated its Unicode tables to 18.0.0 and its
locale data (`ANNOUNCE.md`); the cause is upstream, nothing of ours. Fork 2.23.1 fails the same
way (docs/qa/UPDATE-SCENARIOS.md UPD-26).

**Bisection** (one prefix set up on 11.0 with Fork 2.23.2, each probe pins the next build with a
manifest override and a new `revision`, runs `setup` - the A4 path - and starts Fork): 11.9 ✅,
11.14 ✅, 11.16 ✅, 11.17 ✅, 11.18 ✅ (and once more after the last switch); first bad: 11.19.
Each switch took 15-35 s (`wine_runtime`, `prefix_init` with `wineboot -u`), not minutes.

**Kron4ek 11.18 staging passes** (`wine-11.18-staging-amd64-wow64.tar.xz`, 101,346,404 bytes,
sha256 `15f312ef…5875d99a`, equal to the release's `sha256sums.txt` and the GitHub asset digest;
its Unix libraries need the same host libraries as 11.0 and nothing newer than glibc 2.25):

| Run | Result |
|---|---|
| `test_real_fork.py`, fresh prefix | 7 passed (630 s) |
| `test_fork_update.py` (`FL_E2E_FORK_UPDATES=1`): Fork 2.23.1 updating itself (Velopack) and through `update --fork`, bridge on, commits through Fork | 13 passed (1219 s) |
| `test_wine_app_upgrade.py` (`FL_E2E_APP_UPGRADE=1`), A4 with B = 11.0: switch to the old pin and back | 6 passed, A6 skipped (no `FL_E2E_BRIDGE_BUILDS`) (853 s) |
| Existing prefix: setup on 11.0 with Fork 2.23.2, first run, a commit through Fork; then the pin moves to 11.18 (new `revision` and `wine.default`); `setup` re-ran `wine_runtime`, `prefix_init` (15 s); Fork starts, two commits through Fork in two repositories | 3 passed (405 s) |
| After the fix below, with this commit merged with the update tiers' branch: the existing-prefix scenario as an app upgrade (setup under the previous release's manifest - revision 2026.10.2, default 11.0 - then this release's packaged manifest); `setup` re-ran `wine_runtime`, `prefix_init`, `host_integration` (19 s), `doctor --deep` has no failures and no `prefix.integration` warning; Fork starts and commits twice | 3 passed (426 s) |
| Same tree: `test_real_fork.py` | 7 passed (358 s) |
| Same tree: `test_wine_app_upgrade.py`, A4 with B = the listed 11.0 build (no override), Fork also opened on B | 6 passed, A6 skipped (947 s) |

**Found by the existing-prefix run (fixed):** Wine's `wine.inf` registers `.js` for its own
`JSFile` again when it updates the prefix, replacing our `ForkLinux.File` association, and setup
checked `host_integration` before `prefix_init` ran: `doctor` warned "1 of 107 file-manager / open
redirects are missing" after every Wine change, until a later setup happened to notice. The step's
inputs now include the Wine build, so a Wine change re-runs it after `prefix_init`
(`tests/test_steps_integration.py::test_integration_inputs_follow_the_wine_build`).

## Final integration of the v1.0.0 branches (2026-10-12)

fix/integration with fix/font-fallback-472, fix/update-tests, fix/de-e2e, fix/e2e-coverage,
fix/deps-runtime (Wine 11.18) and fix/deps-ci merged, every real-tier module run through
`scripts/e2e-docker.sh` (seeded; the seed has Wine 11.0, so 11.18 was downloaded in each root):

| Shard (one scratch root each) | Result |
|---|---|
| setup errors, real Fork, launch, integration, display, bridge, bridge remotes, doctor, snapshots (`fi-t1`) | 89 passed, fresh root, final tree (2820 s) |
| git history, changes, branches, remotes, ssh remotes, advanced, local ops (`fi-t2`) | 46 passed, 2 failed in a fresh root; both passed when rerun on that prefix (`test_real_git_changes.py`, `test_real_git_remotes.py`: 15 passed) after the fix below |
| setup variants, uninstall (`fi-t3`) | 17 passed |
| `test_fork_update.py` (`FL_E2E_FORK_UPDATES=1`) | 13 passed (1134 s) |
| `test_wine_app_upgrade.py` (`FL_E2E_APP_UPGRADE=1`, two bridge builds for A6, A4's build B = 11.0) | 7 passed (895 s) |
| `test_wine_pin_upgrade.py` (A24, new) | 3 passed (460 s) |

What the merged tree changed, found by these runs and fixed:

- **Fork's bundled ssh asks for a key's passphrase under Wine 11.18** (it could not under 11.0):
  Fork's own prompt appears and the fetch goes through; ssh-agent still cannot be reached.
  `ssh sync`, `ssh status`, `doctor ssh.passphrase` (now info) and README say so.
- **Fork runs without NoNewPrivs or an extra seccomp filter under 11.18**; the desktop tier's
  hand-off check no longer assumes Fork is confined (the hand-off daemon and the terminal still
  carry nothing more than the test process).
- A launch without a terminal shows its error in a dialog (F21) and waits for it; after the user
  declined the license question in a dialog, that second dialog is no longer shown.
- Test-side: the error dialog is checked and closed by its button (L-ERROR-DIALOG); Open In File
  Explorer goes through FileManager1 first; Consolas maps to Noto Sans Mono where Noto is
  installed; B-DISABLE accepts the hand-off-only daemon; "Push all tags" is read the same way in
  the dark theme; a retried commit replaces its description instead of appending it.
