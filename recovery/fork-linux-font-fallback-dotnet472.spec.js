export const meta = {
  name: 'fork-linux-font-fallback-dotnet472',
  description: 'Fix font_fallback step/doctor for the .NET 4.7.2 fallback layout (CompositeFont under WPF\\Fonts\\); verify which file WPF reads; adversarially reviewed',
  phases: [{ title: 'Fix' }, { title: 'Review' }],
}
const REPO = '/home/sergiu-alexandrescu/Projects/Fork-Linux'
const WT = '/home/sergiu-alexandrescu/Projects/Fork-Linux-wt/font-fallback-472'
const CTX = `Project "Fork for Linux (unofficial)". Follow AGENTS.md (Python 3.10 stdlib, no suppressions, 100% line+branch coverage, never patch Fork,
never touch ~/.wine or the user's real install). WORKSPACE: git -C ${REPO} worktree add ${WT} -b fix/font-fallback-472 fix/integration (reuse if present);
commit only there (Conventional Commits, trailer "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"); never push/merge; don't edit other worktrees.
Real Wine runs: isolated root $HOME/.cache/fl-ff472 (HOME/XDG inside; seed downloads from /home/sergiu-alexandrescu/.cache/fork-linux/downloads and
~/.cache/winetricks), own Xvfb :88 started OUTSIDE the lock, each Wine command under 'flock $HOME/.cache/fl-de-e2e.lock <cmd>' releasing it at once
(never hold loops; daemons must not inherit the lock fd — use 'flock -o'). TMPDIR=$HOME/.cache/fl-ff472/tmp.
ISSUE: when setup falls back from dotnet48 to dotnet472 (winetricks), the font_fallback step (src/fork_linux/steps/ — see docs/spikes/S10-text-rendering.md
Finding 7, which strace-confirmed WPF 4.8 reads Framework64\\v4.0.30319\\WPF\\GlobalUserInterface.CompositeFont) only looks in WPF\\, but 4.7.2 keeps it
under WPF\\Fonts\\ only. doctor prefix.font_fallback then warns and the non-Latin fallback is not patched. Determine with a real dotnet472 prefix (winetricks
dotnet472 into a fresh prefix, strace -f -e trace=openat Fork or a tiny WPF probe if feasible, otherwise Fork itself with a CJK repo name) which file(s)
WPF 4.7.2 actually reads; patch the right one(s) (both WPF\\ and WPF\\Fonts\\ where present, per framework dir incl. Framework (32-bit) if WPF reads it),
idempotent, rev bump, doctor check updated, tests. Verify a CJK/Cyrillic label renders with Noto fallback in Fork on the 4.7.2 prefix (screenshot, view it).
Gate: host pytest 100%, ruff, no-suppressions; py3.10 via FL_CI_STAGE=compat FL_CI_CELL=jammy ./scripts/ci-docker.sh.`
const S = { type: 'object', properties: { summary: { type: 'string' }, commits: { type: 'array', items: { type: 'string' } }, verification: { type: 'array', items: { type: 'string' } }, remaining_problems: { type: 'array', items: { type: 'string' } } }, required: ['summary', 'commits', 'verification', 'remaining_problems'] }
phase('Fix')
const b = await agent(`${CTX}\nROLE: investigate and fix.`, { label: 'fix', phase: 'Fix', schema: S })
phase('Review')
const r = await agent(`${CTX}\nROLE: adversarial reviewer-fixer. Builder report: ${JSON.stringify(b).slice(0, 6000)}\nCheck the 4.8 path is unchanged (no regression), upgrade from an already-patched 4.8 prefix, a prefix that has both files, permissions/encoding of the XML (BOM), and that doctor matches reality. Fix on the branch; verdict.`, { label: 'review', phase: 'Review', schema: S })
return { b, r }
