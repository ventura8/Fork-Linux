---
name: test-runner
description: pytest unit + contract tests on the host; the FL_REAL_WINE=1 bridge tier and the FL_E2E_FORK=1 tier only in containers.
---

# Test Runner

pytest unit + contract tests on the host; the FL_REAL_WINE=1 bridge tier and the FL_E2E_FORK=1 tier only in containers.

Canonical rules: [AGENTS.md](../../../AGENTS.md).

- Targeted: `python3 -m pytest -q tests/test_<module>.py --cov=fork_linux.<module> --cov-branch --cov-report=term-missing`.
- Contract tests: `tests/test_{packaging_release,workflows,docker,install_sh,meson_install,desktop_files,metainfo,completions,manpages,version,credits,repo_hygiene,e2e_docker,host_guards,real_tier_guard,check_update_scenarios,version_variant,e2e_scenarios}.py`.
- Update scenarios (Fork updates F*, app upgrades A*): `docs/qa/UPDATE-SCENARIOS.md` names each scenario's tier and tests;
  `python3 scripts/check-update-scenarios.py` keeps it honest (`--strict`: every scenario covered). Add a new test to
  its row (Planned -> Covered by) in the same change.
- Test module file names are unique across `tests/` (no `__init__.py`: pytest 6.2 on jammy stops the whole
  collection on two modules of one name; `tests/test_repo_hygiene.py::test_test_module_basenames_are_unique`).
- E2E scenario registry: `tests/test_e2e_scenarios.py` checks `tests/e2e/scenarios.json` against the test tree
  (every scenario has an existing test, every real-tier test has its `Scenarios:` line) and the generated
  `docs/qa/E2E-SCENARIOS.md`; after changing either run `python3 scripts/gen-scenarios-doc.py write`. Every
  user-facing change needs a scenario id and a real-tier test (AGENTS.md §4.5); check.yml runs `validate` + `check`
  on every pull request, even after a failed lint stage. A scenario of an unmerged branch carries `pending_merge`
  (`gen-scenarios-doc.py pending` checks it there; after the merge `declare BRANCH`, `clear-pending BRANCH`, `write`).
- Python 3.10: `FL_CI_STAGE=compat FL_CI_CELL=jammy ./scripts/ci-docker.sh`.
- Coverage gates: `FL_CI_STAGE=coverage ./scripts/ci-docker.sh` (90% overall, 100% branch on the critical modules,
  ASan/UBSan native tests, then the C coverage report in its container and the per-file gate: every file >= 90%,
  Python files 100% line + branch, enforced by `scripts/check-coverage.py`). Re-check existing reports with
  `python3 scripts/check-coverage.py -v` (exit 1: offenders table; exit 2: missing or malformed report).
- **Real tiers run only in containers (hard rule 18; 2026-10-10 home-directory deletion).** Never set
  `FL_E2E_FORK=1` / `FL_REAL_WINE=1` (or any `FL_E2E_*` / `FL_REAL_*` switch) for a host pytest: it stops
  at once with status 2 (`tests/fixtures/real_tier.py`), and there is no override.
  - Wine tier: `FL_CI_STAGE=bridge ./scripts/ci-docker.sh`.
  - Real Fork, locally: `scripts/e2e-docker.sh --name <run> pytest tests/e2e` (seeded, logs in
    `logs/e2e-docker/<run>/`, screenshots in `/var/tmp/fork-linux-e2e/<run>/root/shots/`; see the
    [e2e-docker](../e2e-docker/SKILL.md) skill). The update tiers, one `--name` each: the
    Fork-update tier `FL_E2E_FORK_UPDATES=1 ... pytest tests/e2e/test_fork_update.py` (after
    `scripts/build-bridge.sh`), the app-upgrade tier `FL_E2E_APP_UPGRADE=1 ... pytest tests/e2e/test_wine_app_upgrade.py`
    (each sets up its own prefix in a sub-root, `E2E.child`). CI's tier: `./scripts/ci-e2e-wine.sh` (fresh
    downloads, no seed): `--all` or named modules (`tests/e2e/<module>.py`; naming an update module switches its
    tier on) each run in a container and scratch root of their own, `--jobs N` at a time (default: the host-wide
    slots), longest first (`scripts/e2e-plan.py`, durations in `tests/e2e/timings.json`: update it from
    `logs/e2e-wine/results.tsv` when a module's time changes a lot). One container sets the main prefix up once
    (a template root); modules that use the main prefix start from a copy of it (`FL_E2E_REUSE=1`), modules that
    set up their own (`e2e-plan.py` `FRESH`) from its download caches; logs in `logs/e2e-wine/<module>/`. So a
    module must not depend on another having run (`installed` sets the prefix up when it is missing); without
    paths the whole tier runs in one root. The full tier (`--all` with both update tiers) takes about 40 minutes
    on the 16-core laptop; e2e-wine.yml runs it as five shards of two containers each, under an hour.
    `--remote HOST [--remote-jobs M]` ships the last commit (git bundle) to HOST's clone
    (`FL_E2E_REMOTE_DIR`, default `~/fork-linux-ci`) and runs its share of the modules there (`e2e-plan.py
    split`), containers only on that machine too, logs in `logs/e2e-wine/remote-HOST/`.
- Tests never touch the real home, network or Wine unless gated (hard rule 16); use `xdg`, `fake_bin`, `http_server`.
