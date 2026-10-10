export const meta = {
  name: 'fork-linux-coverage-and-e2e-scenarios',
  description: 'Enforce >=90% coverage per file (Python + C) in CI, and build an enforced E2E scenario registry with real-Fork tests for every user-facing scenario',
  phases: [
    { title: 'Foundations', detail: 'per-file coverage gate + scenario catalog/registry + gate test' },
    { title: 'E2E', detail: 'real-Fork tests for uncovered scenarios, split by area' },
    { title: 'Integrate', detail: 'registry mapping, CI wiring, full run' },
    { title: 'Review', detail: 'completeness critic + re-run' },
  ],
}

const REPO = '/home/sergiu-alexandrescu/Projects/Fork-Linux'
const WT = '/home/sergiu-alexandrescu/Projects/Fork-Linux-wt/e2e-coverage'
const CTX = `
Project "Fork for Linux (unofficial)": unofficial Wine wrapper running the official Fork git client for Windows on Linux. Read AGENTS.md
(binding rules: Python 3.10 stdlib + pytest, no suppressions (no skip/xfail decorators; runtime pytest.skip only for gates/absent tools),
100% line+branch Python coverage, pinned Docker, never commit Fork screenshots/binaries, never touch ~/.wine or the user's live
~/.local/share/fork-linux / ~/.local/opt/fork-linux), README.md (features + CLI), docs/qa/FUNCTIONALITY-REPORT.md (every Fork feature area the
QA audit exercised by hand), scripts/qa/fl-qa.sh (xdotool driving helpers), tests/e2e/ (existing real tier: conftest gating FL_E2E_FORK=1,
helpers), scripts/ci-sonar.sh + scripts/ci-c-coverage.sh + scripts/gcov-sonar.py (coverage reports), sonar-project.properties.
Other branches being built concurrently will ALSO contribute E2E tests and must be referenced by the registry after merge: fix/update-tests
(docs/qa/UPDATE-SCENARIOS.md: scenario IDs F1-F11 Fork updates, A1-A12 app upgrades; tests in tests/e2e/test_fork_updates.py and the packaging
upgrade cells), fix/de-e2e (tests/e2e_de/: per-desktop integration + real-Fork tiers for gnome kde xfce cinnamon mate budgie lxqt), and the
fix/* pre-release branches (wine-dark-and-launchers, gitbash-and-dialog-paths, modes-and-symlinks, hygiene-and-flatpak,
credentials-and-pinning, bridge-git-floor, spellcheck), font-fallback, deps-*. Do not edit those worktrees; reference their scenarios.
WORKSPACE: git -C ${REPO} worktree add ${WT} -b fix/e2e-coverage 0f9a7fc (reuse if present). Work + commit only there (Conventional Commits
ending with "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"); never push/merge; never touch ${REPO} or other worktrees.
HOST LOAD: all real-Fork/Wine runs under 'flock $HOME/.cache/fl-de-e2e.lock <cmd>'; TMPDIR=$HOME/.cache/fl-e2ecov-tmp; isolated roots
$HOME/.cache/fl-e2ecov-<name> (HOME + XDG_* inside, XDG_RUNTIME_DIR=$R/run); own Xvfb displays :60-:69 (never :0 :1 :50 :70-:98); seeded
caches from /home/sergiu-alexandrescu/.cache/fork-linux/downloads and /home/sergiu-alexandrescu/.cache/winetricks; screenshots stay in
scratch roots (LOOK at them while developing); kill everything you start.
`

const COVGATE = `
TASK (per-file coverage gate): add scripts/check-coverage.py (stdlib) that reads artifacts/coverage/coverage.xml (Cobertura; line-rate AND
branch-rate per <class filename>) and artifacts/coverage/c-coverage.xml (SonarQube generic format; lines covered + branches covered per <file>)
and fails (exit 1, table of offenders) if ANY source file is below --min 90 for combined coverage computed the way SonarQube does
((covered lines + covered conditions) / (lines + conditions)), plus a separate --min-python-line/branch 100 flag reproducing today's 100% Python
gate; excludes exactly what sonar.coverage.exclusions excludes (parse sonar-project.properties so the two can never drift). Wire it into the
coverage stage of scripts/ci-docker.sh (after scripts/ci-c-coverage.sh), scripts/ci-sonar.sh (FL_SONAR_COVERAGE=1 path), and
.github/workflows/check.yml coverage job (before the Sonar scan). Tests for the script (load via importlib; fixtures with passing/failing
reports, parsing the real sonar-project.properties), contract tests asserting the wiring, AGENTS.md §4.5 + §4.8 text ("every file >= 90%,
enforced by scripts/check-coverage.py"), skills test-runner/pipeline-runner. Run it against freshly generated reports (FL_SONAR_COVERAGE=1
reports or the coverage stage) — must pass today. Commit.`

const REGISTRY = `
TASK (scenario catalog + enforced registry): build an EXHAUSTIVE catalog of user-facing scenarios and make E2E coverage of it enforced.
- tests/e2e/scenarios.toml? NO (tomllib is 3.11+) — use tests/e2e/scenarios.json: list of {id, area, title, tiers:[real-fork|de|packaging|
  e2e-wine-docker], provided_by: "this-branch"|"fix/update-tests"|"fix/de-e2e"|..., tests: [pytest node ids or script cell names]}.
  ID namespaces: S-* setup (fresh setup managed Wine, resume after interrupt mid-download and mid-dotnet, offline with full cache, --reset with
  license warning, system-Wine provider, flatpak provider, custom provider, --fork-version, --latest, missing host tools/libs/downloader -> clear
  errors), L-* launch (first launch welcome, relaunch, fork <path> forwarding, fork . from subdir, --from-file-manager on a file, Wayland/X11
  driver choice, --debug logs, launch when setup incomplete -> GUI progress), G-* git operations through Fork's UI (open repo history, graph,
  search/filter, blame, file history, diff views incl image diff, stage/unstage file+hunk+line, discard, commit, amend, commit with UTF-8
  message, create/checkout/rename/delete branch, merge, rebase, interactive rebase (reorder/squash/drop via Fork's dialog), cherry-pick,
  revert, reset, tags create/push, stash save/apply/pop/drop, fetch/pull/push to file://, HTTP with basic auth (local test server), SSH with
  agent and with passphrase key (local sshd on high port), clone (file://, https public), conflict resolution in Fork's merger, submodules
  init/update, worktrees create/open/delete, LFS, git-flow, hooks (failing pre-commit blocks with bridge; documented bundled behaviour)), I-* OS
  integration (links/Help URLs, Show in Explorer, Open file, Console terminal, external diff + merge tool waiting, Preferences persistence,
  dark/light theme follow, HiDPI 96/144/192, Selawik UI font + non-Latin fallback rendering, spell check, Git Bash, typed Unix paths in file
  dialogs, Wine dialogs dark), B-* bridge (enable/disable/status/record, daemon lifecycle incl. Fork restart, exec bits + symlinks via
  native git, performance budget), D-* doctor (all checks pass on healthy install, each --fix path: shell tool, source dirs, menubuilder
  leftovers, overlay, repo fix/undo with consent), U-* uninstall (non-purge keeps prefix, purge removes ours, never ~/.wine, license warning),
  R-* rollback/snapshots, plus F-* / A-* (from fix/update-tests) and DE-<desktop>-* (from fix/de-e2e) as provided_by entries.
- docs/qa/E2E-SCENARIOS.md generated from the JSON (scripts/gen-scenarios-doc.py, drift-tested).
- Enforcement: tests/test_e2e_scenarios.py (normal unit tier, always runs): JSON schema valid; ids unique; every scenario has >=1 test reference;
  every referenced test node id EXISTS (collect via ast parsing of tests/ — function names + parametrize ids) unless provided_by names another
  branch AND a "pending-merge" flag is set (the final merge will clear all pending flags and the test must then fail if any remain);
  every real-tier test function declares its scenario ids (a module-level SCENARIOS mapping or a docstring tag "Scenarios: G-COMMIT, ..."
  parsed by ast — NOT a skip/xfail marker) and every declared id exists in the registry.
Commit. Report the full list of scenario ids with provided_by.`

const E2E_AREAS = [
  { key: 'git-ops-local', ids: 'G-* local operations: history/graph/search/blame/file history/diff views incl image diff, stage/unstage file/hunk/line, discard, commit/amend/UTF-8, branches create/checkout/rename/delete, merge, rebase, interactive rebase via Fork dialog, cherry-pick, revert, reset, tags, stash, conflict resolution in Fork merger, git-flow, worktrees, submodules, LFS (if git-lfs available in the image; else install it in the e2e image), hooks' },
  { key: 'git-ops-remote', ids: 'G-* remote operations: fetch/pull/push file:// and bare /home paths, HTTP with basic auth (local git http-backend server; Fork.AskPass prompt filled via xdotool with TEST credentials), SSH via agent and passphrase key (local sshd on a high port in the scratch root), clone file:// and https://github.com/octocat/Hello-World.git, tags push; in both bundled-git and bridge modes' },
  { key: 'os-integration', ids: 'I-*, L-*, S-*, D-*, U-*, R-*, B-* scenarios not provided by other branches: links, Show in Explorer, Open, Console, external diff/merge waiting, Preferences persistence, theme follow, HiDPI 96/144/192 sanity (screenshot size/layout checks), fonts (Selawik present, glyph sanity via screenshot stddev and window-name text), launch variants, setup variants (interrupt+resume, offline cache, reset, providers where available), doctor --fix paths, uninstall/purge, rollback/snapshots, bridge lifecycle' },
]

const E2E_TASK = (a) => `
TASK (real E2E area "${a.key}"): read tests/e2e/scenarios.json (registry, may still be in progress by another agent — coordinate by only ADDING
test files and reporting mappings; the registry agent/integrator owns scenarios.json). Implement real-Fork E2E tests for: ${a.ids}.
Put them in tests/e2e/test_${a.key.replace(/-/g, '_')}*.py, gated like the existing tier (FL_E2E_FORK=1), each test function declaring its scenario
ids (module SCENARIOS dict or "Scenarios:" docstring tag as the registry agent defines; if not defined yet, use a docstring line
"Scenarios: <ID>, <ID>"). Drive Fork with xdotool using keyboard shortcuts/menus (robust waits, no fixed sleeps where avoidable), verify
effects with native git and Fork's state (window titles, xprop, screenshots you LOOK at). Share fixtures via tests/e2e/conftest.py helpers —
add new helpers in a new module tests/e2e/_fork_ui.py (create it if missing; if another agent created it, extend carefully). Run your tests
(under the lock) until green and stable (run twice). Fix product bugs you find in src/ with unit tests (keep 100%). Commit. Report the
scenario id -> test node id mapping.`

const V = { type: 'object', properties: {
  mapping: { type: 'array', items: { type: 'object', properties: { id: { type: 'string' }, tests: { type: 'array', items: { type: 'string' } }, status: { type: 'string' } }, required: ['id', 'tests', 'status'] } },
  commits: { type: 'array', items: { type: 'string' } }, gate: { type: 'string' }, remaining_problems: { type: 'array', items: { type: 'string' } } },
  required: ['mapping', 'commits', 'gate', 'remaining_problems'] }

phase('Foundations')
const [cov, reg] = await parallel([
  () => agent(`${CTX}\n${COVGATE}`, { label: 'coverage-gate', phase: 'Foundations', schema: V }),
  () => agent(`${CTX}\n${REGISTRY}`, { label: 'scenario-registry', phase: 'Foundations', schema: V }),
])

phase('E2E')
const areas = await parallel(E2E_AREAS.map(a => () => agent(`${CTX}\nRegistry report: ${JSON.stringify(reg).slice(0, 6000)}\n${E2E_TASK(a)}`, { label: `e2e:${a.key}`, phase: 'E2E', schema: V })))

phase('Integrate')
const integ = await agent(`${CTX}
ROLE: INTEGRATOR of fix/e2e-coverage. Reports: coverage gate ${JSON.stringify(cov).slice(0, 2500)} ; registry ${JSON.stringify(reg).slice(0, 4000)} ;
areas ${JSON.stringify(areas.filter(Boolean)).slice(0, 12000)}.
1. Update tests/e2e/scenarios.json with every mapping; every scenario either has a test on this branch or provided_by another branch with
   pending-merge; regenerate docs/qa/E2E-SCENARIOS.md; tests/test_e2e_scenarios.py green.
2. CI: e2e-wine.yml (weekly + dispatch + release branches) runs ALL real-tier E2E files (split into parallel matrix shards by area to keep
   each job < 90 min); check.yml always runs the registry gate + per-file coverage gate. AGENTS.md (§4.5: every user-facing change needs a
   scenario id + E2E test; §4.8 CI), skills (test-runner, pipeline-runner), docs/INSTRUCTIONS.md. actionlint/yamllint clean.
3. Full gates: pytest host + py3.10 (docker run --rm -u $(id -u):$(id -g) -e HOME=/tmp -e PYTHONDONTWRITEBYTECODE=1 -v ${WT}:/src -w /src
   fork-linux-ci-jammy:22.04 python3 -m pytest -q -p no:cacheprovider) 100%; scripts/check-coverage.py on fresh reports; run every real-tier
   E2E file once more end-to-end (under the lock). Commit.`, { label: 'integrate', phase: 'Integrate', schema: V })

phase('Review')
const rev = await agent(`${CTX}
ROLE: COMPLETENESS CRITIC + adversarial reviewer of fix/e2e-coverage. Integrator report: ${JSON.stringify(integ).slice(0, 6000)}
1. Walk README.md, every CLI command/flag (fork-linux --help, every subcommand --help), docs/qa/FUNCTIONALITY-REPORT.md, bridge/README.md, and
   Fork's menus (File/View/Repository/Window/Help — screenshot them in a real session) and list every user-facing scenario NOT in
   scenarios.json; add them with tests (or provided_by). 2. Check tests really assert the scenario (no vacuous assertions, no "passes because
   nothing happened"), real-tier tests actually ran (logs), declared scenario ids match behaviour. 3. Verify check-coverage.py truly fails
   on a <90% file (mutate a report copy) and matches Sonar's numbers for 3 files (compare with the Sonar API if a token file exists at
   ${REPO}/.sonar-token — pass it to curl via a 0600 -K config file, delete after; never print it). Fix on the branch (commit with the trailer).
Final verdict + list of scenarios added.`, { label: 'review', phase: 'Review', schema: V })

return { cov, reg, areas, integ, rev }
