# Agent progress logs

This directory holds **agent progress** for active workstreams and **local CI tee output**.

## Convention

- Agents append timestamped status lines to files under `logs/` as they complete significant steps.
- **Preferred:** also echo the same lines to the terminal (`tee -a` or equivalent) so progress is visible in the session and on disk.
- General log: `agent-progress.log`. Workstreams may add sibling `*.log` files here (e.g. `bridge-progress.log`).
- Never write secrets, license keys, or content from a real user's Wine prefix into these logs.

## CI pipeline logs

When running the Docker CI gate (`scripts/ci-pipeline.sh` / `ci-docker.sh` / `ci-matrix.sh` / `ci-packaging-matrix.sh` / `ci-e2e-wine.sh`), tee output here:

| Log | Source |
|---|---|
| `ci-pipeline.log` | Full `./scripts/ci-pipeline.sh` tee (optional outer wrap) |
| `ci-lint.log` | Lint stage (`FL_CI_STAGE=lint`) |
| `ci-coverage.log` | Coverage stage (`FL_CI_STAGE=coverage`) |
| `ci-bridge.log` | Bridge stage (`FL_CI_STAGE=bridge`) |
| `ci-matrix-summary.log` | Compat matrix launcher summary |
| `ci-matrix/<cell>.log` | Per-distro compat cell (`jammy`, `noble`, `resolute`, `debian13`, `fedora44`, `leap16`, `arch`) |
| `ci-packaging-summary.log` | Packaging matrix launcher summary |
| `ci-packaging/<format>.log` | Per-format packaging cell (`deb`, `deb-jammy`, `rpm-fedora`, `rpm-opensuse`, `arch`, `snap`, `appimage`, `flatpak`, `tarball`) |
| `ci-e2e-wine.log` | Real Wine + Fork end-to-end run (`./scripts/ci-e2e-wine.sh`) |
| `e2e-wine/` | `./scripts/ci-e2e-wine.sh`: `pytest.log` + text logs copied from its scratch root |
| `e2e-docker/<NAME>/` | `./scripts/e2e-docker.sh --name NAME …`: `pytest.log` (pytest mode) + text logs (`*.log *.json *.txt` < 20 MB, never `drive_c`) copied from the scratch root `/var/tmp/fork-linux-e2e/<NAME>`; screenshots stay there (`root/shots/`) |

Real-Fork / Wine runs happen **only in containers** (AGENTS.md hard rule 18): never point a
scratch root or a log at `$HOME`, never run Wine or the `FL_E2E_*` / `FL_REAL_*` tiers on the host.

After packaging, agents must **scan every** `ci-packaging/<format>.log` for meaningful ERROR/WARNING product issues (not only cell exit codes) — see [AGENTS.md](../AGENTS.md) §4.8 packaging log scan and `.agents/skills/pipeline-runner/SKILL.md`.

See [AGENTS.md](../AGENTS.md) §4.6 (progress visibility), §4.7.1 (root clean / `docker/` Dockerfiles), §4.8, and `.agents/skills/pipeline-runner/SKILL.md`.

## Git

`*.log` files and subdirectories under `logs/` are gitignored. This README is committed so the folder purpose stays documented.
