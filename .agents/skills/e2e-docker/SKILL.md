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
  module only. CI (`scripts/ci-e2e-wine.sh`) still gives every module a fresh scratch root.
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
first that frees up. Do not raise it to get around a busy host.

## Never

- `FL_E2E_FORK=1 python3 -m pytest …` or `FL_REAL_WINE=1 …` on the host (no override exists).
- A scratch root under `$HOME`, `--privileged`, a writable mount of any part of `$HOME`.
- Deleting anything outside your worktree or your own scratch root; `rm` through Wine drive letters.
