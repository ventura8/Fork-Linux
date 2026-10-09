# Spike S9 — Fork's in-app self-update (Velopack) under Wine

Date: 2026-10-09 · Host: Ubuntu 26.04 x86_64 · Wine: managed Kron4ek `wine-11.0-staging-amd64-wow64`
· fork-linux from source (`PYTHONPATH=src python3 -m fork_linux`, branch `feature/v0.1.0`).

Question: *can a user update Fork when Fork says a new update is available?*
**Answer: yes.** Fork's own updater works under Wine end to end; two wrapper bugs it exposed
(the git-bridge daemon dying on Fork's restart, and a false doctor failure after a delta
update) are fixed, and `[fork] update_policy = pinned` now really keeps Fork on its version.

## Setup

Isolated root `R=~/.cache/fork-linux-update-test` (fake `HOME` + XDG dirs, `XDG_RUNTIME_DIR=$R/run`
with `bus` / `systemd` symlinked to the real user session while testing), private `Xvfb :95`
without a window manager, caches seeded from the real `~/.cache/fork-linux/downloads` and
`~/.cache/winetricks`. Screenshots stayed in `$R/shots` (they show Fork's UI and logo — never
committed). The real `~/.local/share/fork-linux` and `~/.wine` were not touched.

```bash
fork-linux setup --fork-version 2.23.1 --allow-untested --accept-fork-eula --no-gui   # TOFU path
```

Fork 2.23.1 came from `https://cdn.fork.dev/win/Fork-2.23.1.exe` (76,210,856 B, sha256
`a2e7fc44…b4aa`, trust on first use); Fork's update feed offers 2.23.2.

## How Velopack updates Fork (observed in `AppData\Local\Fork\velopack.log`)

1. **Check**: `GET https://fork.dev/update/win/releases.win.json?…&localVersion=2.23.1`
   (channel *Develop*, `ApplicationUpdateType = 0`, Fork's default). Channel *Stable* (1)
   reads `…/win/stable/releases.win.json`, which on 2026-10-09 still listed **2.21.1**, so a
   2.23.x user on Stable is told "No Updates Available". With *Off* (2) a manual
   **File > Check for Updates...** also reads the Stable feed (same answer); there is no
   automatic check.
2. **Download**: with `packages\Fork-2.23.1-full.nupkg` present, the delta
   `Fork-2.23.2-delta.nupkg` (checksum verified by Velopack) is applied by
   `Update.exe patch` to rebuild `Fork-2.23.2-full.nupkg` (~1.4 s); without a base package
   (after a rollback) the full package is downloaded and its checksum verified. Velopack
   then deletes the older full package and the delta, and extracts the new `Update.exe`.
3. **Dialog**: "Fork update is ready to be installed — Please restart application to update",
   buttons **Restart and Update** / **Close**.
4. **Apply**: `Update.exe apply --waitPid <Fork> --restart`: waits for Fork to exit, extracts
   the package to `packages\VelopackTemp\…`, runs the `--veloapp-obsolete` hook, swaps
   `current\` (old one moved to a temp backup), runs `--veloapp-updated`, updates the Start
   Menu `Fork.lnk`, and starts `C:\users\<u>\AppData\Local\Fork\current\Fork.exe` with no
   arguments. Total: ~4 s from the click to the new process.

## Results — bundled git (bridge off)

| # | Step | Result | Observation |
|---|---|---|---|
| 1 | Install 2.23.1 (`--fork-version --allow-untested`) | **PASS** | TOFU download from the CDN, setup rc 0 (corefonts needed its built-in retry). |
| 2 | First launch: pre-launch snapshot of 2.23.1 | **PASS** | `snapshot list` → `2.23.1-…  hardlink`. |
| 3 | Update offered on channel Stable (1) | **FAIL (Fork's feed)** | Stable feed is at 2.21.1 → "No Updates Available". Not a wrapper problem; `update --check` now prints Fork's channel. |
| 4 | Update offered on channel Develop (0) | **PASS** | File > Check for Updates... → delta download → "ready to be installed". |
| 5 | Restart and Update: Velopack apply under Wine | **PASS** | `current\sq.version` = 2.23.2; "Package version 2.23.2 applied successfully". |
| 6 | Restarted Fork comes up | **PASS** | Main window, repository tabs restored. New pid; its parent is the user's systemd (reparented), not our launcher. |
| 7 | Restarted Fork keeps our environment | **PASS** | `/proc/<pid>/environ` identical for `WINEPREFIX`, `WINESERVER`, `WINELOADER`, `WINEDEBUG`, `WINEDLLOVERRIDES`, `WINEHOME`, `FL_WINE`, `GIT_CONFIG_COUNT/KEY_*/VALUE_*` (only Wine's `WINESERVERSOCKET` added). With the bridge also `FORKGITINSTANCE`, `FL_BRIDGE_PORT`, `FL_BRIDGE_TOKEN`, … |
| 8 | `fork <path>` while the restarted Fork runs (fast path) | **PASS** | rc 0, path forwarded through Fork's single-instance pipe. |
| 9 | Next `fork` launch notices the change | **PASS** | `Fork updated 2.23.1 -> 2.23.2; 'fork-linux rollback' restores 2.23.1` (zenity info dialog; it is modal, Fork starts after OK). The `icon` step re-ran (new `Fork.exe`), settings re-applied, then a snapshot of 2.23.2 was taken. |
| 10 | `doctor`: `fork.version` / `fork.pending_update` | **PASS** | "Fork 2.23.2 is known-good"; "no staged Fork update". |
| 11 | `doctor`: `fork.integrity` after a **delta** update | **FAIL → fixed** | The delta-rebuilt `Fork-2.23.2-full.nupkg` (sha256 `0423f874…b927`) is not byte-identical to the published one the manifest pins, so doctor reported *fail*. Now *info* ("rebuilt by Fork's own updater …") when Fork updated itself (installed ≠ `state fork.version`); a full download still matches (*ok*). |
| 12 | `rollback --to-version 2.23.1` | **PASS** | 0.25 s; pinned; newer staged package removed. |
| 13 | Pinned Fork's own updater | **FAIL → fixed** | Fork kept `ApplicationUpdateType = 0` while pinned, so it would offer (and could apply) 2.23.2 again. Now rollback and the new `Fork update check` pre-launch hook set it to 2 (Off) and remember the old value; verified: Check for Updates → "No Updates Available". `config set fork.update_policy auto` + next launch restored 0 and the update was offered again. |

## Results — native-git bridge on (`git-bridge enable`)

| # | Step | Result | Observation |
|---|---|---|---|
| 14 | Daemon across Restart and Update (before the fix) | **FAIL** | Daemon log `daemon exit reason=signal` the moment the old Fork exited (PDEATHSIG; `--parent-pid` = the old Fork). The restarted Fork kept `FL_BRIDGE_PORT=52923`, so every git call failed: Fork's "Git Error" dialog *"fl-shim(git): cannot connect to the bridge daemon on 127.0.0.1:52923 (WSA error 10061)"* (staging a file). |
| 15 | Daemon across Restart and Update (after the fix) | **PASS** | Same daemon pid and port before and after; the restarted 2.23.2 Fork staged and committed through native git (`c1b094c after velopack restart`). |
| 16 | Fast path while the restarted Fork runs | **PASS** | `git-bridge status` → `daemon: pid …, port …`; `fork <path>` reuses it from `session.json`. |
| 17 | Daemon ends with Fork | **PASS** | `reason=prefix-idle` ~4 s after Fork exited. |
| 18 | Quick close + relaunch (Wine's services still up) | **PASS** | First fix version (environment only) leaked: `services.exe`, `winedevice.exe`, `mscorsvw.exe`, `rpcss.exe`… inherit Fork's environment and outlive it. With `--watch-exe-dir` only programs from Fork's directory count: the old daemon exited, one daemon left. |

## The fix (bridge daemon lifetime)

`fl-bridge-helper --daemon` got `--watch-prefix DIR` and `--watch-exe-dir WINDIR`
(`bridge/unix/fl_bridge_helper.c`; spec in `bridge/README.md`, `bridge/unix/README.md`).
With them the daemon sets no PDEATHSIG and every 2 s scans `/proc` for a process of this
user whose `argv[0]` starts with `WINDIR` (Wine shows `C:\users\<u>\AppData\Local\Fork\…`:
`Fork.exe`, `Update.exe`) and whose environment holds both `WINEPREFIX=DIR` and
`FL_BRIDGE_PORT=<its port>`. Until it has seen one, `--parent-pid` still applies (a launch
abandoned before the exec); afterwards it exits 6 s after the last one is gone. The same
port and token stay valid, so the Fork that `Update.exe` restarts — which inherits them —
keeps working, and the launcher's fast path keeps reusing the daemon from `session.json`.
`bridge.start_daemon` passes both options (`ForkLayout.win_local_dir`).

Alternatives considered: starting the daemon from the restarted process is impossible
(Velopack starts `Fork.exe` directly, not through our launcher, and Linux processes spawned
from Wine inherit staging's seccomp, spike B2); a fixed per-prefix port would not help
without a process that outlives Fork.

## Other changes

- `updates.sync_update_type` + launcher hook `Fork update check` + `rollback`: pinned →
  `ApplicationUpdateType = 2`, the previous value kept in `state.json`
  (`fork.update_type_before_pin`) and restored when the policy is `auto` again (unless the
  user changed the setting meanwhile).
- `update --check` prints Fork's channel (`develop` / `stable` / `off`) and, on develop, the
  in-app route (File > Check for Updates..., Restart and Update).
- `doctor fork.integrity`: *info* instead of *fail* for a package Velopack rebuilt from a delta.
- README "Updating" section.

## Open

- The version-change notice is a modal zenity / kdialog info box that delays Fork until it is
  dismissed; a desktop notification would be friendlier.
- After a rollback `packages\` has no full package of the restored version (Velopack deleted
  it during the update; rollback removes the newer one), so doctor `fork.integrity` warns
  "missing; cannot verify" and Velopack's next update downloads the full package instead of
  a delta. Snapshotting the version's full nupkg would fix both.
- Launches in this isolated environment spent ~30 s in the desktop probes before setup's
  `preflight` after the update (likely a D-Bus timeout of the fake session: Xvfb display with
  the real session bus); not seen on a normal desktop session, not investigated further.
