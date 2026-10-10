#!/usr/bin/env bash
# fl-qa-gitprobe.sh — reproduce the bundled-git findings of docs/qa/FUNCTIONALITY-REPORT.md
# without driving the GUI. Runs Fork's own bundled git.exe under the managed Wine
# of the isolated QA install created by scripts/qa/fl-qa.sh (never the real prefix).
#
# Containers only (AGENTS.md hard rule 18): run it inside the same runner as fl-qa.sh, e.g.
#   scripts/e2e-docker.sh --name qa --keep shell -- scripts/qa/fl-qa-gitprobe.sh
#
# Usage: scripts/qa/fl-qa-gitprobe.sh
# Each probe prints PROBE <name>: <observed> and a short verdict. Exit code is 0;
# this is a diagnostic, not a gate.
# Diagnostic: keep going after failing probes (no -e, no pipefail).
set -u

if [ ! -e /.dockerenv ] && [ ! -e /run/.containerenv ]; then
    printf 'fl-qa-gitprobe: refusing to run Wine on the host (AGENTS.md hard rule 18); use\n' >&2
    printf '  scripts/e2e-docker.sh --name qa --keep shell -- scripts/qa/fl-qa-gitprobe.sh\n' >&2
    exit 2
fi
R="${FL_QA_ROOT:-/e2e/qa}"
case "$R" in
    /?*) ;;
    *) printf 'fl-qa-gitprobe: FL_QA_ROOT must be an absolute path below /\n' >&2; exit 1 ;;
esac
PREFIX="$R/home/.local/share/fork-linux/prefix"
WORK="$R/probe"
WIN_USER="$(id -un)"
GIT_WIN="C:\\users\\$WIN_USER\\AppData\\Local\\Fork\\gitInstance\\2.50.1\\cmd\\git.exe"
SH_WIN="C:\\users\\$WIN_USER\\AppData\\Local\\Fork\\gitInstance\\2.50.1\\bin\\sh.exe"

die() {
    printf 'fl-qa-gitprobe: %s\n' "$*" >&2
    exit 1
}

[ -f "$PREFIX/.fork-linux/created-by" ] || die "no isolated QA prefix at $PREFIX (run scripts/qa/fl-qa.sh init + setup first)"

WINE=""
for candidate in "$R"/home/.local/share/fork-linux/runtimes/wine/*/bin/wine; do
    [ -x "$candidate" ] && WINE="$candidate"
done
[ -n "$WINE" ] || die "managed wine not found under $R"

# Same overrides the launcher exports for Fork (spike S6).
# WINEHOME as the launcher sets it: bundled git then reads the managed overlay (~/.gitconfig include).
export HOME="$R/home" WINEPREFIX="$PREFIX" WINEDEBUG=-all WINEDLLOVERRIDES="winemenubuilder.exe="
export WINEHOME="C:\\users\\$WIN_USER"
export GIT_CONFIG_COUNT=2 GIT_CONFIG_KEY_0=core.filemode GIT_CONFIG_VALUE_0=false
export GIT_CONFIG_KEY_1=core.autocrlf GIT_CONFIG_VALUE_1=false
# The launcher adds this one when the host git is >= 2.48.
if git version | awk '{split($3, v, "."); exit !(v[1] > 2 || (v[1] == 2 && v[2] >= 48))}'; then
    export GIT_CONFIG_COUNT=3 GIT_CONFIG_KEY_2=worktree.useRelativePaths GIT_CONFIG_VALUE_2=true
fi
export GIT_AUTHOR_NAME="QA Probe" GIT_AUTHOR_EMAIL="probe@example.invalid"
export GIT_COMMITTER_NAME="QA Probe" GIT_COMMITTER_EMAIL="probe@example.invalid"

# Drop msys' "Cygwin WARNING: Couldn't compute FAST_CWD pointer" banner (6 lines).
filter() {
    grep -v -E 'Cygwin WARNING|FAST_CWD|older Cygwin|available Cygwin|problem persists|persists,|problems\.html|^[[:space:]]*$' || true
}

wgit() {
    # Diagnostic only: never abort the run on a git failure, print it instead.
    local rc=0
    timeout 90 "$WINE" "$GIT_WIN" "$@" 2>&1 | filter || rc=$?
    [ "$rc" -eq 0 ] || printf '(exit %s)\n' "$rc"
}

probe() {
    printf '\nPROBE %s\n' "$1"
}

rm -rf "${WORK:?}"
mkdir -p "$WORK"

probe "symlink status (bundled git vs native)"
git init -q -b main "$WORK/sym"
printf 'target\n' >"$WORK/sym/target.txt"
ln -s target.txt "$WORK/sym/link"
git -C "$WORK/sym" add -A
git -C "$WORK/sym" commit -q -m init
printf 'native : [%s]\n' "$(git -C "$WORK/sym" status --short | tr '\n' ' ')"
printf 'bundled: [%s]\n' "$(cd "$WORK/sym" && wgit status --short | tr '\n' ' ')"
printf 'bundled + core.symlinks=true: [%s]\n' "$(cd "$WORK/sym" && wgit -c core.symlinks=true status --short | tr '\n' ' ')"
printf 'expected: native clean; bundled shows " M link" (blocks rebase: "You have unstaged changes")\n'

probe "file:// remote with a Linux path"
git init -q --bare "$WORK/remote.git"
git -C "$WORK/sym" remote add origin "file://$WORK/remote.git"
git -C "$WORK/sym" push -q origin main
(cd "$WORK/sym" && wgit ls-remote origin | head -2)
printf 'expected with the fork-linux overlay: the refs above (url.file:///Z:/<top>/.insteadOf rewrite)\n'
printf -- '-- with an explicit url."file:///Z:/".insteadOf rewrite:\n'
(cd "$WORK/sym" && wgit -c "url.file:///Z:$(dirname "$HOME" | cut -d/ -f1-2)/.insteadOf=file://$(dirname "$HOME" | cut -d/ -f1-2)/" ls-remote origin | head -2)

probe "pre-commit hook that runs a program and exits 1"
git init -q -b main "$WORK/hook"
printf 'a\n' >"$WORK/hook/a.txt"
git -C "$WORK/hook" add -A
git -C "$WORK/hook" commit -q -m init
printf '#!/bin/sh\nls >/dev/null\necho "hook reached the end" >&2\nexit 1\n' >"$WORK/hook/.git/hooks/pre-commit"
chmod +x "$WORK/hook/.git/hooks/pre-commit"
printf 'b\n' >"$WORK/hook/b.txt"
(cd "$WORK/hook" && wgit add b.txt && wgit commit -m "must be blocked")
printf 'HEAD is now: %s\n' "$(git -C "$WORK/hook" log -1 --format=%s)"
printf 'expected (correct): "init"; observed under Wine 11 staging: "must be blocked" (hook silently bypassed)\n'

probe "msys sh: second command after a child process"
(cd "$WORK/hook" && timeout 30 "$WINE" "$SH_WIN" -c 'date >/dev/null; echo SECOND-COMMAND-RAN' 2>&1 | filter) || true
printf 'expected: SECOND-COMMAND-RAN (missing = sh dies after its first child; Wine bug 55138 family)\n'

probe "worktree created by bundled git, seen by native git"
(cd "$WORK/sym" && wgit worktree add -b wt "Z:${WORK//\//\\}\\wt" >/dev/null) || true
printf 'wt/.git: %s\n' "$(cat "$WORK/wt/.git")"
printf 'native : %s\n' "$(git -C "$WORK/wt" status --short 2>&1 | head -1 || true)"
printf 'expected: a relative gitdir that native git uses (host git >= 2.48); otherwise "not a git repository: .../Z:/..."\n'

probe "repository created by bundled git (core.* written by Git for Windows)"
(cd "$WORK" && wgit init -q fresh >/dev/null)
git -C "$WORK/fresh" config --list --local | grep -E 'core\.(symlinks|ignorecase|filemode)' || true
printf 'expected: symlinks=false ignorecase=true (bad defaults for later native-git use)\n'

probe "Git Credential Manager store"
(cd "$WORK/sym" && printf 'protocol=https\nhost=github.com\n\n' | GCM_INTERACTIVE=never timeout 60 "$WINE" "$GIT_WIN" credential-manager get 2>&1 | filter | head -3) || true
printf 'expected under Wine: "Failed to enumerate credentials. [0x3ec]" (wincredman unusable)\n'

printf '\nprobe workspace: %s (safe to delete)\n' "$WORK"
