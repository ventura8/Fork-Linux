---
name: test-runner
description: pytest unit + contract tests on the host; the FL_REAL_WINE=1 bridge tier and the FL_E2E_FORK=1 tier only in containers.
---

# Test Runner

pytest unit + contract tests on the host; the FL_REAL_WINE=1 bridge tier and the FL_E2E_FORK=1 tier only in containers.

Canonical rules: [AGENTS.md](../../../AGENTS.md).

- Targeted: `python3 -m pytest -q tests/test_<module>.py --cov=fork_linux.<module> --cov-branch --cov-report=term-missing`.
- Contract tests: `tests/test_{packaging_release,workflows,docker,install_sh,meson_install,desktop_files,metainfo,completions,manpages,version,credits,repo_hygiene,e2e_docker,host_guards,real_tier_guard}.py`.
- Python 3.10: `FL_CI_STAGE=compat FL_CI_CELL=jammy ./scripts/ci-docker.sh`.
- Coverage gates: `FL_CI_STAGE=coverage ./scripts/ci-docker.sh` (90% overall, 100% branch on the critical modules, ASan/UBSan native tests).
- **Real tiers run only in containers (hard rule 18; 2026-10-10 home-directory deletion).** Never set
  `FL_E2E_FORK=1` / `FL_REAL_WINE=1` (or any `FL_E2E_*` / `FL_REAL_*` switch) for a host pytest: it stops
  at once with status 2 (`tests/fixtures/real_tier.py`), and there is no override.
  - Wine tier: `FL_CI_STAGE=bridge ./scripts/ci-docker.sh`.
  - Real Fork, locally: `scripts/e2e-docker.sh --name <run> pytest tests/e2e` (seeded, logs in
    `logs/e2e-docker/<run>/`, screenshots in `/var/tmp/fork-linux-e2e/<run>/root/shots/`; see the
    [e2e-docker](../e2e-docker/SKILL.md) skill). CI's tier: `./scripts/ci-e2e-wine.sh` (fresh downloads, ~2.5 GB).
- Tests never touch the real home, network or Wine unless gated (hard rule 16); use `xdg`, `fake_bin`, `http_server`.
