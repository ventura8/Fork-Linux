---
name: e2e-docker
description: Run real-Fork / Wine work (the FL_E2E_FORK=1 tier, xdotool exploration, strace, QA, spikes) — only in Docker via scripts/e2e-docker.sh.
---

# E2E in Docker

Real-Fork / Wine work runs **only** in containers (AGENTS.md hard rule 18). On 2026-10-10 a
developer's whole home directory was recursively deleted while agents ran real-Fork tests on
the host; the cause was never found. Never run Wine, wineserver, wineboot, winetricks,
`Fork.exe`, `fork-linux setup` / `run` / `fork`, Xvfb + Wine or any `FL_E2E_*` / `FL_REAL_*`
tier on the host: pytest stops at once (status 2) and the scripts refuse.

Canonical rules: [AGENTS.md](../../../AGENTS.md) (§1 rule 18, §4.8 `scripts/e2e-docker.sh`).

## Commands

```sh
scripts/e2e-docker.sh --name my-run pytest tests/e2e                     # the whole real tier
scripts/e2e-docker.sh --name my-run pytest tests/e2e -k doctor           # any pytest args
scripts/e2e-docker.sh --name my-run --keep pytest tests/e2e              # reuse the previous scratch root
FL_E2E_FORK_UPDATES=1 scripts/e2e-docker.sh --name upd-fork pytest tests/e2e/test_fork_update.py
                                                     # Fork 2.23.1 updating, F rows (docs/qa/UPDATE-SCENARIOS.md)
FL_E2E_APP_UPGRADE=1 scripts/e2e-docker.sh --name upd-app pytest tests/e2e/test_wine_app_upgrade.py
                                                     # app upgrades A3-A6, A11, A12 (docs/qa/UPDATE-SCENARIOS.md)
scripts/e2e-docker.sh --name explore --keep shell -- bash                # interactive (TTY), Xvfb on :99
scripts/e2e-docker.sh --name explore --keep shell -- xdotool search --class fork.exe
scripts/e2e-docker.sh --name trace --ptrace shell -- strace -f -o /e2e/trace.txt python3 -m fork_linux doctor
scripts/e2e-docker.sh --name qa --keep shell -- bash -c 'scripts/qa/fl-qa.sh init && scripts/qa/fl-qa.sh setup'
scripts/e2e-docker.sh --name shim --image fork-linux-ci-bridge:26.04 shell -- bash   # another pinned image
```

- `--name NAME` (default: the worktree's directory name; letters, digits, `. _ -`): the scratch
  root `/var/tmp/fork-linux-e2e/<NAME>` (or `FL_E2E_SCRATCH`), the log directory
  `logs/e2e-docker/<NAME>/`, the container `fl-e2e-<NAME>` (label `fl-e2e=<NAME>`). One run per
  name at a time; pick a distinct name per agent.
- Without `--keep` the scratch root of the previous run with that name is wiped first (only when
  it carries `.fl-e2e-scratch`); with `--keep` it is reused (the conftest keeps
  `root/home/.cache`, everything else of the E2E root is rebuilt).
- `--ptrace` adds only `CAP_SYS_PTRACE`. Never `--privileged`.
- The two update modules each build their own prefix in a sub-root of the session root
  (`root/fork-update`, `root/app-upgrade`), seeded from the session's caches, so they never share
  another module's prefix. Their steps build on each other; a failed step fails the rest of that
  module only. CI (`scripts/ci-e2e-wine.sh`) still gives every module a fresh scratch root, and
  fails a module that skipped a test (`pytest -rs`; every tier it runs was asked for).
- Fork checks for updates by itself about a minute after it starts, when `LastUpdateCheck` in its
  `settings.json` is old enough; on Develop it then downloads the update and shows "ready to be
  installed", and Velopack applies it at the next start. The update modules seed `LastUpdateCheck`
  before every start (now, or long ago where scenario F24 wants the check), so it never fires
  uninvited; do the same in exploration scripts that keep Fork open longer.
- `tests/e2e/test_fork_update.py` (gated by `FL_E2E_FORK_UPDATES=1` as well) installs Fork 2.23.1
  from `cdn.fork.dev` in a prefix of its own (`root/fork-update`) and lets it update to the tested
  version through Fork's own updater (Velopack: File > Check for Updates..., Restart and Update; a
  staged package applied at the next start) and through `fork-linux update --fork`. Its bridge
  scenarios (F7, F19, F23) need `build-bridge/` in the worktree (`scripts/build-bridge.sh` first;
  they skip without it). Dialogs and desktop notifications go to stand-in `zenity` / `notify-send`
  scripts that record them in `root/fork-update/logs/desktop-ui.log`; what Fork did is read from
  its `velopack.log`, `/proc`, the bridge daemon's log and native git.
- `tests/e2e/test_wine_app_upgrade.py` (gated by `FL_E2E_APP_UPGRADE=1` as well) downloads Fork 2.23.1
  and the Kron4ek 11.19 staging build in the container. Its A6 step needs two bridge builds of
  different versions, which the image cannot make (no MinGW): build them with
  `scripts/build-bridge.sh --out` from two copies of the tree with different `VERSION`s into
  `<scratch>/bridge/1/` and `<scratch>/bridge/2/`, then run with `--keep` and
  `FL_E2E_BRIDGE_BUILDS=/e2e/bridge` (without it A6 is skipped).

## The real tier (tests/e2e)

- `test_real_*.py` and `test_git_ops_*.py` modules share one prefix and run in `conftest.py`'s `MODULE_ORDER` (setup
  errors, the main setup, launch, Fork's git UI, OS integration, display, bridge, doctor,
  snapshots, setup variants, uninstall last). Iterate on one module without a new setup:
  `FL_E2E_REUSE=1 scripts/e2e-docker.sh --name my-run --keep pytest tests/e2e/test_real_launch.py`
  (the first run of that name sets up; `--keep` and `FL_E2E_REUSE=1` keep the prefix after it).
- Build the bridge first for `test_real_bridge.py`: `scripts/build-bridge.sh` (its own image).
- Helpers: `fork_driver.py` (windows, keys, clicks, screenshots, Fork sessions, git),
  `fork_ui.py` (Fork's layout in the seeded 1500x940 window and its keyboard paths: Quick Launch,
  shortcuts, context menus clicked from their bottom edge), `git_servers.py` (HTTP with basic
  auth, sshd on a high port), `remote_ops.py` (bare remotes, Fetch / Pull / Push, tags, credential
  prompts and Clone, shared by the bundled-git and bridge modules). A failed test leaves
  `failed-<test>.png` in the shots directory.
- Drive a Fork operation through `ForkUI.operate(start, done, what)`: it waits until native git
  shows the effect and, when Fork reports Wine's failed start of its git (`Win32Exception ...
  0x2db ... StartWithCreateProcess`, README "Known Wine limitations"), closes the report and runs
  the step once more; any other Git Error fails at once with Fork's text. `Screen.clipboard()`
  reads what Fork copied (Copy Commit SHA): it first wakes Wine's clipboard thread, whose claim of
  the X selection a bare Xvfb never flushes.
- Every test names its scenarios on a `Scenarios: ID, ...` docstring line; the registry is
  `tests/e2e/scenarios.json` (`python3 scripts/gen-scenarios-doc.py validate` / `write`).

## What the container sees

`/src` (this worktree, read-only), the git common dir (read-only, same path), `/e2e` (the scratch
root: `HOME=/e2e/home`, `FL_E2E_ROOT=/e2e/root`), `/seed` (read-only: `FL_E2E_SEED_DIR` or
`/var/tmp/fork-linux-e2e/seed` with `wine-*.tar.xz`, `Fork-*.exe`, `winetricks-*`,
`selawik-*.zip`, `winetricks/` cache; our code re-verifies every file). Nothing else from the
host: no `$HOME`, no host X socket. `FL_E2E_*` variables you set are passed through.

## Results

- `logs/e2e-docker/<NAME>/pytest.log` (pytest tee) + text logs copied from the scratch root.
- Screenshots: `/var/tmp/fork-linux-e2e/<NAME>/root/shots/*.png` — **look** at 2–3 of them; they
  show Fork's logo, so never copy them into the repository or a CI upload.

## Slots

At most `FL_E2E_SLOTS` (default 3) of these containers run host-wide: each run tries every
`/var/tmp/fork-linux-e2e/locks/slot-<n>` without blocking; while all are busy ("all N E2E slots
are busy") it tries them all again every `FL_E2E_SLOT_POLL` seconds (default 5) and takes the
first that frees up. Do not raise `FL_E2E_SLOTS` to get around a busy host.

## Desktop tier

`scripts/ci-e2e-de.sh <desktop> [--fork]` runs `tests/e2e_de` (fork-linux from the release tarball in a
real desktop session) in `docker/Dockerfile.e2e.de.<desktop>` with the same library, mounts, refusals and
(for `--fork`) slots and seed as this runner; logs in `logs/e2e-de/`. See `docs/qa/DE-E2E.md`.

## Never

- `FL_E2E_FORK=1 python3 -m pytest …` or `FL_REAL_WINE=1 …` on the host (no override exists).
- A scratch root under `$HOME`, `--privileged`, a writable mount of any part of `$HOME`.
- Deleting anything outside your worktree or your own scratch root; `rm` through Wine drive letters.
