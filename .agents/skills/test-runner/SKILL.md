---
name: test-runner
description: pytest unit + contract tests, the FL_REAL_WINE=1 bridge tier and the FL_E2E_FORK=1 tier.
---

# Test Runner

pytest unit + contract tests, the FL_REAL_WINE=1 bridge tier and the FL_E2E_FORK=1 tier.

Canonical rules: [AGENTS.md](../../../AGENTS.md).

- Targeted: `python3 -m pytest -q tests/test_<module>.py --cov=fork_linux.<module> --cov-branch --cov-report=term-missing`.
- Contract tests: `tests/test_{packaging_release,workflows,docker,install_sh,meson_install,desktop_files,metainfo,completions,manpages,version,credits,repo_hygiene}.py`.
- Python 3.10: `FL_CI_STAGE=compat FL_CI_CELL=jammy ./scripts/ci-docker.sh`.
- Coverage gates: `FL_CI_STAGE=coverage ./scripts/ci-docker.sh` (90% overall, 100% branch on the critical modules, ASan/UBSan native tests).
- Wine tier: `FL_CI_STAGE=bridge ./scripts/ci-docker.sh`. Real Fork: `./scripts/ci-e2e-wine.sh` (downloads ~2.5 GB).
- Tests never touch the real home, network or Wine unless gated (hard rule 16); use `xdg`, `fake_bin`, `http_server`.
