export const meta = {
  name: 'fork-linux-update-tests',
  description: 'Automated tests for every update scenario: Fork updates (in-app Velopack, CLI, rollback, pin, known-bad, staged, bridge across restart) and Fork-for-Linux app upgrades (all 9 package formats N-1 -> N, install.sh, manifest/runtime migrations, upgrade while running), CI wiring, review',
  phases: [
    { title: 'Plan', detail: 'scenario matrix + N-1 build helper' },
    { title: 'Implement', detail: 'fork-update tier and app-upgrade tier in parallel' },
    { title: 'Integrate', detail: 'CI wiring, full run' },
    { title: 'Review', detail: 'adversarial completeness + re-run' },
  ],
}

const REPO = '/home/sergiu-alexandrescu/Projects/Fork-Linux'
const WT = '/home/sergiu-alexandrescu/Projects/Fork-Linux-wt/update-tests'
const CTX = `
Project "Fork for Linux (unofficial)": unofficial Wine wrapper running the official Fork git client for Windows on Linux. Read AGENTS.md
(binding: Python 3.10 stdlib + pytest, no suppressions (no skip/xfail decorators — runtime pytest.skip only for absent tools/gates),
100% line+branch coverage, pinned Docker images, never commit Fork screenshots/binaries, never touch ~/.wine or the user's live
~/.local/share/fork-linux / ~/.local/opt/fork-linux), docs/spikes/S9-fork-self-update.md (real Velopack update findings: File > Check for
Updates... > Restart and Update; Fork Stable channel lists 2.21.1, Develop offers 2.23.2; pinned -> ApplicationUpdateType=2; daemon
--watch-prefix survives Fork self-restart; rollback restores the nupkg), src/fork_linux/{updates,snapshots,fork_install,launcher,bridge,
bootstrap,manifest,state}.py, steps/*, tests/e2e/, scripts/{packaging-e2e-install,ci-packaging-cell,release-*}.sh, install.sh.
WORKSPACE: git -C ${REPO} worktree add ${WT} -b fix/update-tests 0f9a7fc (reuse if present). Work + commit only there (Conventional Commits
ending with "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"); never push, never merge, never touch ${REPO} or other worktrees.
HOST LOAD: run any Wine/real-Fork tier under 'flock $HOME/.cache/fl-de-e2e.lock <cmd>' (shared with other agents); TMPDIR=$HOME/.cache/fl-upd-tmp.
Real-Fork runs: isolated roots under $HOME/.cache/fl-upd-<name> (HOME + all XDG_* inside, XDG_RUNTIME_DIR=$R/run), own Xvfb displays :70-:79
(never :0 :1 :50 :81-:98), seeded caches from /home/sergiu-alexandrescu/.cache/fork-linux/downloads and /home/sergiu-alexandrescu/.cache/winetricks
(downloading the OFFICIAL Fork-2.23.1.exe from https://cdn.fork.dev/win/ for N-1 Fork scenarios is allowed; TOFU path with --allow-untested).
Screenshots stay inside the scratch roots; kill everything you start.
`

const PLAN = `
TASK (plan + shared helpers): write docs/qa/UPDATE-SCENARIOS.md — an exhaustive scenario matrix with an ID per scenario, the tier that covers it
(unit/integration with fakes, packaging container E2E, real-Fork E2E), and expected results. MUST include at least:
FORK UPDATES (F-*): F1 in-app Velopack update 2.23.1 -> 2.23.2 via Fork's UI (xdotool: File > Check for Updates > Restart and Update) with
our env preserved after restart, snapshot of 2.23.1 exists, next 'fork' launch notifies (non-blocking) and re-applies icon/settings;
F2 'fork-linux update --fork' CLI (2.23.1 -> 2.23.2) incl. snapshot, settings/accounts/custom-commands preserved, repo list preserved;
F3 'update --fork --latest' TOFU path with feed SHA256 verification (and a tampered-feed / mismatched-nupkg negative case);
F4 rollback --to-version after an update, version pinned, Fork's updater disabled (ApplicationUpdateType=2) and no update applied on relaunch,
unpin restores the user's previous ApplicationUpdateType; F5 known-bad version in the manifest -> launcher warns and doctor fails with rollback
hint; F6 staged-but-unapplied packages (packages/Fork-<v>-full.nupkg newer) -> doctor pending_update, pinned policy deletes them;
F7 bridge enabled during Fork self-restart -> git works after restart, single daemon; F8 update while offline / feed unreachable / malformed
feed -> clear errors, nothing changed; F9 Stable vs Develop channel handling in update --check; F10 Fork update while a second fork
<path> forwarding happens; F11 disk full / download interrupted mid-update -> resume, no partial install.
APP UPGRADES (A-*): A1 install.sh upgrade N-1 -> N (per-user tarball) preserving config.ini, state.json, prefix, integrations, CLI links;
A2 for EACH package format (deb, deb-jammy, rpm-fedora, rpm-opensuse, arch, snap, appimage, flatpak, tarball) a REAL version upgrade N-1 -> N
(not a same-version reinstall) in the packaging containers, asserting --version changes, user markers survive, desktop entry/actions point at the
new install, and the next launch applies newly-bumped step revisions WITHOUT re-downloading Wine/.NET; A3 manifest revision bump (new
bootstrap_revision / step rev) -> only the changed steps re-run on next launch; A4 Wine runtime pin change in the manifest (build A -> build B)
-> new runtime downloaded, old wineserver killed with the OLD binary, wineboot -u with the new one, wine-sensitive steps re-run, old build kept,
'config set wine.build <old>' reverts; A5 upgrade while Fork is running -> nothing breaks, fast path works, pending steps applied after Fork
closes; A6 bridge binaries replaced while the daemon runs -> Fork keeps working, new shims used next start; A7 state.json schema handling (older
schema migrated, newer schema refused gracefully with hint); A8 downgrade N -> N-1 (warn, keep working); A9 AppImage update info (zsync
'gh-releases-zsync' embedded and correct) + moving the AppImage file re-points the desktop entry; A10 Flatpak bundle update + snap refresh;
A11 uninstall then reinstall keeps the prefix (no license re-activation); A12 upgrade of our app + Fork self-update in the same session.
Also add the shared helper: scripts/build-version-variant.sh <version> <outdir> (copies the tree to a temp dir, sets VERSION + matching
debian/changelog top entry + metainfo <release> + docs/releases stub as the contract tests require, then builds the requested format(s) via the
existing release scripts) so N-1 = e.g. 0.9.9 artifacts can be produced for upgrade tests without editing the real tree. shellcheck clean; unit
tests for any Python helpers. Commit.`

const IMPL_FORK = `
TASK (Fork-update tier): implement every F-* scenario from docs/qa/UPDATE-SCENARIOS.md (read it in the worktree). Unit/integration tests with
fakes for logic paths (100% coverage), and a real-Fork tier tests/e2e/test_fork_updates.py gated by FL_E2E_FORK=1 FL_E2E_FORK_UPDATES=1 that
automates F1, F2, F4, F6, F7 (and F3 positive) against real Fork 2.23.1 -> 2.23.2 under Xvfb (xdotool + screenshots kept in scratch; LOOK at
them while developing). Make them robust (wait helpers, timeouts, retries only on Wine transport glitches). Run the real tier (under the lock)
until green; fix product bugs you find in src/ with unit tests. Update docs/qa/UPDATE-SCENARIOS.md status column. Commit.`

const IMPL_APP = `
TASK (app-upgrade tier): implement every A-* scenario from docs/qa/UPDATE-SCENARIOS.md. Extend scripts/packaging-e2e-install.sh (and
ci-packaging-cell.sh) with a REAL N-1 -> N upgrade per format using scripts/build-version-variant.sh (build N-1 = 0.9.9 inside the same
container image), keeping the existing install/remove/reinstall checks; assert --version changes, markers survive, integration files updated,
no re-download on next launch (simulate a set-up user state in the container with a fake prefix + state.json + seeded runtime marker, and run
'fork-linux setup --list-steps' / launcher pre-launch in a no-Wine mode to show which steps would re-run). Unit/integration tests with fakes
for A3, A4, A5, A6, A7, A8, A11, A12 logic (100% coverage) plus one real-Wine run of A4 (runtime switch between two pinned builds — use the
current pin and the previous/next Kron4ek build pinned by URL+sha256 in a test-only manifest override) under the lock. Run all 9 packaging cells
(./scripts/ci-packaging-matrix.sh) until green; read every logs/ci-packaging/*.log. Fix product bugs you find with tests. Commit.`

const V = { type: 'object', properties: {
  scenarios: { type: 'array', items: { type: 'object', properties: { id: { type: 'string' }, status: { type: 'string' }, test: { type: 'string' } }, required: ['id', 'status', 'test'] } },
  bugs_fixed: { type: 'array', items: { type: 'string' } }, commits: { type: 'array', items: { type: 'string' } },
  gate: { type: 'string' }, remaining_problems: { type: 'array', items: { type: 'string' } } },
  required: ['scenarios', 'bugs_fixed', 'commits', 'gate', 'remaining_problems'] }

phase('Plan')
const plan = await agent(`${CTX}\n${PLAN}`, { label: 'plan', phase: 'Plan', schema: V })

phase('Implement')
const impl = await parallel([
  () => agent(`${CTX}\nPlan report: ${JSON.stringify(plan).slice(0, 4000)}\n${IMPL_FORK}`, { label: 'fork-updates', phase: 'Implement', schema: V }),
  () => agent(`${CTX}\nPlan report: ${JSON.stringify(plan).slice(0, 4000)}\n${IMPL_APP}`, { label: 'app-upgrades', phase: 'Implement', schema: V }),
])

phase('Integrate')
const integ = await agent(`${CTX}
ROLE: INTEGRATOR of fix/update-tests. Reports: ${JSON.stringify(impl.filter(Boolean)).slice(0, 10000)}
1. Resolve overlaps between the two implementers; full suite host + py3.10 (docker run --rm -u $(id -u):$(id -g) -e HOME=/tmp -e
   PYTHONDONTWRITEBYTECODE=1 -v ${WT}:/src -w /src fork-linux-ci-jammy:22.04 python3 -m pytest -q -p no:cacheprovider) at 100% coverage; ruff,
   shellcheck, no-suppressions.
2. CI: packaging matrix in check.yml now exercises the real N-1 -> N upgrade per format; e2e-wine.yml (weekly + dispatch + release branches)
   runs the real Fork-update tier (FL_E2E_FORK_UPDATES=1); contract tests assert both. AGENTS.md (§4.8/§4.9 packaging E2E text), skills
   (release-packaging, test-runner), docs/INSTRUCTIONS.md updated. actionlint/yamllint clean.
3. Run: all 9 packaging cells, the real Fork-update tier once (under the lock). Every scenario in docs/qa/UPDATE-SCENARIOS.md must be COVERED and
   PASSING (or explicitly marked impossible with proof). Commit.`, { label: 'integrate', phase: 'Integrate', schema: V })

phase('Review')
const rev = await agent(`${CTX}
ROLE: ADVERSARIAL REVIEWER-FIXER of fix/update-tests. Integrator report: ${JSON.stringify(integ).slice(0, 6000)}
Hunt for: scenarios missing from the matrix (think like a user: auto-update on Develop vs Stable, Fork restarted by updater while our launcher is
not the parent, update during setup, power loss mid-apply, two users, Flatpak sandbox paths, snap revision rollback, AppImage renamed, package
manager upgrade while bridge daemon runs, manifest override present across upgrades), tests that pass without testing the upgrade (same version
both sides, mocks that make assertions vacuous), silent skips, real-tier tests that never ran. Add missing scenarios + tests, re-run the
real Fork-update tier and 3 packaging upgrade cells (deb, flatpak, appimage) yourself. Fix on the branch (commit with the trailer). Final verdict
per scenario.`, { label: 'review', phase: 'Review', schema: V })

return { plan, impl, integ, rev }
