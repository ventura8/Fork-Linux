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

## The real tier (tests/e2e)

- `test_real_*.py` modules share one prefix and run in `conftest.py`'s `MODULE_ORDER` (setup
  errors, the main setup, launch, Fork's git UI, OS integration, display, bridge, doctor,
  snapshots, setup variants, uninstall last). Iterate on one module without a new setup:
  `FL_E2E_REUSE=1 scripts/e2e-docker.sh --name my-run --keep pytest tests/e2e/test_real_launch.py`
  (the first run of that name sets up; `--keep` and `FL_E2E_REUSE=1` keep the prefix after it).
- Build the bridge first for `test_real_bridge.py`: `scripts/build-bridge.sh` (its own image).
- Helpers: `fork_driver.py` (windows, keys, clicks, screenshots, Fork sessions, git),
  `fork_ui.py` (Fork's layout in the seeded 1500x940 window and its keyboard paths: Quick Launch,
  shortcuts, context menus clicked from their bottom edge), `git_servers.py` (HTTP with basic
  auth, sshd on a high port). A failed test leaves `failed-<test>.png` in the shots directory.
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
`/var/tmp/fork-linux-e2e/locks/slot-<n>` without blocking, then waits on one ("all N E2E slots
are busy"). Do not raise it to get around a busy host.

## Never

- `FL_E2E_FORK=1 python3 -m pytest …` or `FL_REAL_WINE=1 …` on the host (no override exists).
- A scratch root under `$HOME`, `--privileged`, a writable mount of any part of `$HOME`.
- Deleting anything outside your worktree or your own scratch root; `rm` through Wine drive letters.
