export const meta = {
  name: 'fork-linux-de-e2e',
  description: 'Docker E2E matrix for the big desktop environments (GNOME, KDE, XFCE, Cinnamon, MATE, Budgie, LXQt): integration tier + real-Fork tier, CI wiring, fixes, review',
  phases: [
    { title: 'Framework', detail: 'shared harness, cache, gate, first DE (xfce) end to end' },
    { title: 'Desktops', detail: 'one agent per remaining DE' },
    { title: 'Integrate', detail: 'apply src fixes, CI workflows, run full matrix' },
    { title: 'Review', detail: 'adversarial re-run' },
  ],
}

const REPO = '/home/sergiu-alexandrescu/Projects/Fork-Linux'
const WT = '/home/sergiu-alexandrescu/Projects/Fork-Linux-wt/de-e2e'
const CTX = `
Project "Fork for Linux (unofficial)": unofficial Wine wrapper running the official Fork git client for Windows on Linux. Read AGENTS.md
(binding: pinned Docker bases with explicit image-tag suffix == FROM tag, '# syntax=docker/dockerfile:1.27.0', BuildKit cache mounts,
bind-mount /src, no 'latest', one Dockerfile per cell (never ARG-switched), no suppressions, Python 3.10 stdlib + pytest for tests,
runtime pytest.skip only, never commit Fork screenshots/binaries, E2E logs only (no screenshots uploaded), never cache the Fork installer in
CI), the existing E2E tier (tests/e2e/, scripts/ci-e2e-wine.sh, docker/Dockerfile.e2e.wine), docs/qa/*.md, and the reference project
/home/sergiu-alexandrescu/Projects/Ubuntu-Hello (docker/Dockerfile.ci.{gnome,kde,xfce,cinnamon,mate,budgie,lxqt}, scripts/ci-matrix.sh,
its per-DE compat cells — reuse their package choices and session tricks).
WORKSPACE: git -C ${REPO} worktree add ${WT} -b fix/de-e2e 0f9a7fc (reuse if present). Work and commit only there (Conventional Commits
ending with "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"); never push, never merge, never touch ${REPO} or other worktrees.
HOST LOAD: other agents run Wine/Fork tests on this host too. Run real-Fork (Wine) container tiers ONE AT A TIME across all agents using
'flock $HOME/.cache/fl-de-e2e.lock <cmd>'. Integration tiers (no Wine) may run in parallel. Use TMPDIR=$HOME/.cache/fl-de-tmp (not /tmp).
Seeded download cache for local runs (re-verified by sha256 by our code): /home/sergiu-alexandrescu/.cache/fork-linux/downloads and
/home/sergiu-alexandrescu/.cache/winetricks — mount read-only and copy in; CI downloads fresh (no Fork-installer caching).
Never use the host's real displays; containers run their own Xvfb.
`

const FRAMEWORK = `
TASK (framework + XFCE first): build the DE E2E harness.
- docker/Dockerfile.e2e.de.<de> per DE, FROM ubuntu:26.04 (pinned), tag fork-linux-e2e-de-<de>:26.04, with: Xvfb, dbus-x11/dbus-daemon,
  xdotool, x11-utils, imagemagick, python3, python3-pytest, git, curl, cabextract, unzip, 7zip, the Wine host libs (from
  fork_linux.hostdeps, like Dockerfile.e2e.wine), plus the DE's real components: for xfce: xfwm4, xfsettingsd, xfconf, thunar (+ its D-Bus
  FileManager1 service), xfce4-terminal, xdg-desktop-portal-gtk, exo-utils, xdg-utils, a GTK theme pair (Greybird + Greybird-dark) — a
  non-root user 'tester' uid 1000.
- tests/e2e_de/: conftest.py gated by FL_E2E_DE=<de> (module-level pytest.skip otherwise) that starts, inside the container,
  'dbus-run-session' + Xvfb :50 -screen 0 1920x1080x24 -dpi <96 or 192 per case> + the DE's settings daemon + WM + file-manager service
  (per-DE recipe in tests/e2e_de/desktops.py: a DESKTOPS dict with packages-independent launch commands, env XDG_CURRENT_DESKTOP /
  DESKTOP_SESSION / XDG_SESSION_DESKTOP, how to set dark theme + scaling (gsettings / kwriteconfig6 / xfconf-query / dconf), expected
  terminal binary, file-manager binary + whether it provides org.freedesktop.FileManager1, file-manager integration kind (nautilus python
  ext / dolphin servicemenu / thunar uca / nemo action / caja fma / pcmanfm-qt fma).
  Integration tier tests (no Wine): install fork-linux from the release tarball (build with ./scripts/release-portable.sh tarball or reuse
  artifacts/) via install.sh --from-tarball --no-deps as tester; assert: desktop file valid + visible to the DE's menu lookup
  (xdg-desktop-menu / desktop-file-validate / gio launch dry-run or kbuildsycoca6 for KDE), the DE's file manager loads our action (parse
  with the FM's own config reader where possible, else structural check + FM log), theme detection (fork_linux.theme.detect) reads the DE
  setting for light and dark, scaling detection (display.scale_percent) for 100/200, 'fork-linux-host terminal <dir>' launches the DE's
  terminal with cwd=<dir> (find its window with xdotool and its process cwd via /proc), 'fork-linux-host reveal <file>' makes the DE's
  file manager show the item (FileManager1 call succeeds + FM window appears), 'fork-linux-host open <file>' / open-url reach xdg-open's
  handler (use a stub default app registered via xdg-mime for a test MIME type), doctor runs (no fail except Wine-not-set-up).
  Real-Fork tier (FL_E2E_DE_FORK=1): full 'fork-linux setup' (seeded cache locally), launch Fork under the DE's WM, welcome dialog via
  xdotool, main window present with WM_CLASS fork.exe and a WM frame, non-uniform screenshot (kept inside the container's scratch root,
  never copied out), dark theme applied after relaunch when the DE is dark, Console → DE terminal, Show in Explorer → DE file manager,
  fork <path> second tab, doctor --deep no fail.
- scripts/ci-e2e-de.sh <de> [--fork] : builds the image, runs the tiers with the repo bind-mounted read-only and a scratch dir, tees
  logs/e2e-de/<de>.log, copies only text logs out. scripts/ci-e2e-de-matrix.sh runs all DEs (integration in parallel, fork tier serialized
  via the lock).
Get XFCE fully green on BOTH tiers (run them). Write docs/qa/DE-E2E.md (matrix, what each tier checks, how to run). For bugs in src/ found
via a DE, FIX them in src with unit tests at 100% coverage (you own src fixes in this phase). Gate: pytest host + py3.10 (docker run --rm
-u $(id -u):$(id -g) -e HOME=/tmp -e PYTHONDONTWRITEBYTECODE=1 -v ${WT}:/src -w /src fork-linux-ci-jammy:22.04 python3 -m pytest -q -p
no:cacheprovider), ruff, shellcheck, hadolint (pinned docker image), no-suppressions, tests/test_docker.py contract tests extended for the new
Dockerfiles. Commit.`

const DES = [
  { key: 'gnome', spec: 'GNOME: gnome-shell can not run nested easily — use mutter --x11 --replace (or --nested) as WM on Xvfb, gsd (gnome-settings-daemon pieces as needed), nautilus (FileManager1 provider) + nautilus-python (python3-nautilus) to load our extension, gnome-terminal (or ptyxis, the 26.04 default — test the default), xdg-desktop-portal-gnome/gtk, gsettings color-scheme prefer-dark + text-scaling/scaling-factor, Yaru themes.' },
  { key: 'kde', spec: 'KDE Plasma 6: kwin_x11 as WM, kded6, dolphin (FileManager1 provider) with our servicemenu (verify via kbuildsycoca6 + dolphin --select / servicemenu listing), konsole, xdg-desktop-portal-kde, Breeze/Breeze Dark via kwriteconfig6 kdeglobals + plasma-apply-colorscheme if available, scaling via kwriteconfig6 kcmfonts forceFontDPI / Xft.dpi.' },
  { key: 'cinnamon', spec: 'Cinnamon: muffin as WM (or cinnamon --replace if feasible headless), nemo (FileManager1 provider) with our nemo_action, gnome-terminal (Mint default) or the configured org.cinnamon.desktop.default-applications terminal, gsettings org.cinnamon.desktop.interface gtk-theme Mint-Y(-Dark) / color-scheme, scaling.' },
  { key: 'mate', spec: 'MATE: marco as WM, mate-settings-daemon, caja (FileManager1 provider) with caja-actions/FileManager-Actions (our fma .desktop), mate-terminal, gsettings org.mate.interface gtk-theme (Menta / BlackMATE or Ambiant-MATE-Dark), scaling.' },
  { key: 'budgie', spec: 'Budgie: budgie-wm (or mutter fallback headless) + budgie-daemon pieces, nautilus or nemo per Ubuntu Budgie 26.04 default file manager (check: Ubuntu Budgie uses nemo), tilix or gnome-terminal per default, gsettings themes/color-scheme, scaling.' },
  { key: 'lxqt', spec: 'LXQt: openbox or xfwm4 as WM (as Lubuntu uses), lxqt-config/lxqt-session pieces, pcmanfm-qt (FileManager1 provider? check) with our FileManager-Actions .desktop, qterminal, theme via lxqt.conf / Qt palette (detection best-effort per our theme.py), scaling via Xft.dpi/QT_SCALE_FACTOR.' },
]

const DE_TASK = (d) => `
TASK (desktop ${d.key}): the framework, XFCE and docs/qa/DE-E2E.md now exist in ${WT} (read them first: tests/e2e_de/, docker/Dockerfile.e2e.de.xfce,
scripts/ci-e2e-de.sh). Add ${d.key}: docker/Dockerfile.e2e.de.${d.key} + its entry in tests/e2e_de/desktops.py (edit ONLY your DE's entry; if the
framework needs a generic change, make it minimal and backwards compatible, and say so). ${d.spec}
Run BOTH tiers for ${d.key} (fork tier under the lock) and get them green. If a failure is a bug in src/fork_linux (theme/DPI detection,
terminal detection, file-manager integration template, reveal), DO NOT edit src/ (other DE agents work in parallel) — instead write a precise
patch proposal (unified diff + unit test) into docs/qa/de-fixes/${d.key}.md and mark the affected test as an expected-known failure ONLY by
recording it in that file (no xfail/skip decorators — leave the test failing). Commit your DE files.`

const V = { type: 'object', properties: {
  de: { type: 'string' }, integration: { type: 'string' }, fork_tier: { type: 'string' },
  src_fixes_needed: { type: 'array', items: { type: 'string' } }, commits: { type: 'array', items: { type: 'string' } },
  remaining_problems: { type: 'array', items: { type: 'string' } } },
  required: ['de', 'integration', 'fork_tier', 'src_fixes_needed', 'commits', 'remaining_problems'] }

phase('Framework')
const fw = await agent(`${CTX}\n${FRAMEWORK}`, { label: 'framework+xfce', phase: 'Framework', schema: V })

phase('Desktops')
const des = await parallel(DES.map(d => () => agent(`${CTX}\n${DE_TASK(d)}`, { label: `de:${d.key}`, phase: 'Desktops', schema: V })))

phase('Integrate')
const integ = await agent(`${CTX}
ROLE: INTEGRATOR of branch fix/de-e2e. Inputs: framework report ${JSON.stringify(fw).slice(0, 3000)} ; DE reports
${JSON.stringify(des.filter(Boolean)).slice(0, 12000)} ; proposals in docs/qa/de-fixes/*.md.
1. Apply every valid src/ fix proposal (deduplicate; keep 100% coverage; host + py3.10 green) and delete the proposal files once applied (fold the
   notes into docs/qa/DE-E2E.md).
2. Wire CI: .github/workflows/check.yml gets an 'e2e-de' matrix job (de: [gnome, kde, xfce, cinnamon, mate, budgie, lxqt]) running the integration
   tier on every PR (no needs:, fail-fast false, pinned actions, ubuntu-26.04); .github/workflows/e2e-wine.yml (weekly + dispatch + release-branch PRs)
   gets the real-Fork tier per DE as a matrix (downloads fresh in CI, logs only). Update scripts/ci-pipeline.sh (local gate) to include the DE
   integration matrix, AGENTS.md §4.8 (stages/cells), .agents/skills/ci-docker-matrix + pipeline-runner, tests/test_workflows.py + test_docker.py
   contract tests (every DE has a Dockerfile, a desktops.py entry, a CI matrix entry). actionlint + yamllint clean.
3. Run the FULL matrix locally: scripts/ci-e2e-de-matrix.sh integration (all 7) and the fork tier for all 7 (serialized via the lock). Fix until green.
Commit. Report per-DE results.`, { label: 'integrate', phase: 'Integrate', schema: V })

phase('Review')
const rev = await agent(`${CTX}
ROLE: ADVERSARIAL REVIEWER-FIXER of branch fix/de-e2e. Integrator report: ${JSON.stringify(integ).slice(0, 6000)}
Check each DE really runs its OWN components (not a shared fallback silently): verify inside each container which WM / file manager /
terminal / portal processes ran during the tests (logs), that assertions are meaningful (not tautological), that no test skips silently,
that pins/tags follow AGENTS.md, that CI wiring matches, and re-run 3 DE integration tiers + 2 fork tiers yourself (gnome, kde + one more).
Fix problems on the branch (commit with the trailer). Report the final verdict.`, { label: 'review', phase: 'Review', schema: V })

return { fw, des, integ, rev }
