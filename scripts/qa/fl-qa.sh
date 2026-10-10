#!/usr/bin/env bash
# fl-qa.sh — isolated manual-QA driver for Fork for Linux (unofficial).
#
# Containers only (AGENTS.md hard rule 18): it starts Wine and the official Fork, so it
# refuses to run on the host. Run it through the E2E runner, e.g.
#   scripts/e2e-docker.sh --name qa --keep shell -- bash -c \
#       'scripts/qa/fl-qa.sh init && scripts/qa/fl-qa.sh setup && scripts/qa/fl-qa.sh fixtures'
#   scripts/e2e-docker.sh --name qa --keep shell -- bash      # interactive (a TTY)
# and look at the screenshots on the host under /var/tmp/fork-linux-e2e/qa/qa/shots/.
#
# Everything lives under one scratch root (FL_QA_ROOT, default /e2e/qa inside the
# container's scratch mount): a fake HOME + XDG dirs, a private Xvfb display, fixture repos
# and screenshots. Screenshots show Fork's UI (and logo): keep them in the scratch root,
# never copy them into the repository. Downloads are seeded from the runner's read-only
# seed (FL_E2E_SEED, FL_E2E_WINETRICKS_CACHE); our code re-verifies every file.
#
# Usage: scripts/qa/fl-qa.sh <command> [args]
#   env                 print the export lines (eval "$(fl-qa.sh env)")
#   init                create the scratch tree and seed download caches
#   xvfb                start Xvfb on $FL_QA_DISPLAY (default: the runner's $DISPLAY, else :96)
#   setup               fork-linux setup --accept-fork-eula --no-gui
#   cli <args...>       run fork-linux from the source tree in the isolated env
#   run <path>          launch Fork on <path> (background, log in $R/logs)
#   wine <args...>      run the managed wine with the isolated prefix
#   shot <name>         screenshot the whole display to $R/shots/<name>.png
#   win                 list visible fork.exe windows (id, geometry, title)
#   key <keys...>       xdotool key on the active Fork window
#   type <text>         xdotool type into the active window
#   click <x> <y>       click at absolute screen coordinates
#   fixtures            create the fixture repositories under $R/repos
#   bigrepo             create $R/repos/big (5000 commits, 3000 files)
#   logs                tail Fork's log and wine-last.log
#   kill                wineserver -k for the isolated prefix
#   stop                kill + stop the private Xvfb
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# A fixed root (never derived from $HOME, which qa_env replaces).
R="${FL_QA_ROOT:-/e2e/qa}"
DISP="${FL_QA_DISPLAY:-${DISPLAY:-:96}}"

die() {
    printf 'fl-qa: %s\n' "$*" >&2
    exit 1
}

if [ ! -e /.dockerenv ] && [ ! -e /run/.containerenv ]; then
    printf 'fl-qa: refusing to run Wine / Fork on the host (AGENTS.md hard rule 18); use\n' >&2
    printf '  scripts/e2e-docker.sh --name qa --keep shell -- scripts/qa/fl-qa.sh %s\n' "${1:-<command>}" >&2
    exit 2
fi
case "$R" in
    /?*) ;;
    *) die "FL_QA_ROOT must be an absolute path below /: '$R'" ;;
esac
case "/$R/" in
    */../* | */./*) die "FL_QA_ROOT must not contain '.' or '..': '$R'" ;;
esac

qa_env() {
    export HOME="$R/home"
    export XDG_CONFIG_HOME="$R/home/.config"
    export XDG_DATA_HOME="$R/home/.local/share"
    export XDG_CACHE_HOME="$R/home/.cache"
    export XDG_STATE_HOME="$R/home/.local/state"
    export XDG_RUNTIME_DIR="$R/run"
    export DISPLAY="$DISP"
    export PYTHONPATH="$REPO_ROOT/src"
    export PATH="$R/home/.local/bin:$PATH"
    # Drop the host session (Wayland/GDK/Xauthority/agent env) so tools target the private Xvfb.
    unset WAYLAND_DISPLAY DBUS_SESSION_BUS_ADDRESS GDK_BACKEND XAUTHORITY QT_QPA_PLATFORM GIT_EDITOR
}

prefix() { printf '%s\n' "$R/home/.local/share/fork-linux/prefix"; }

wine_bin_dir() {
    local d
    for d in "$R"/home/.local/share/fork-linux/runtimes/wine/*/; do
        if [ -x "$d/bin/wine" ]; then
            printf '%s\n' "${d%/}/bin"
            return 0
        fi
        local inner
        for inner in "$d"*/bin; do
            if [ -x "$inner/wine" ]; then
                printf '%s\n' "$inner"
                return 0
            fi
        done
    done
    return 1
}

fork_wid() {
    xdotool search --onlyvisible --class fork.exe 2>/dev/null | tail -1
}

cmd_env() {
    qa_env
    local v
    for v in HOME XDG_CONFIG_HOME XDG_DATA_HOME XDG_CACHE_HOME XDG_STATE_HOME XDG_RUNTIME_DIR DISPLAY PYTHONPATH PATH; do
        printf 'export %s=%q\n' "$v" "${!v}"
    done
    printf 'unset WAYLAND_DISPLAY DBUS_SESSION_BUS_ADDRESS GDK_BACKEND XAUTHORITY QT_QPA_PLATFORM GIT_EDITOR\n'
}

cmd_init() {
    mkdir -p "$R"/{home,run,shots,repos,logs} "$R/home/.cache/fork-linux/downloads" "$R/home/.local/bin"
    chmod 700 "$R/run"
    local seed="${FL_E2E_SEED:-}" cache="${FL_E2E_WINETRICKS_CACHE:-}" f
    if [ -n "$seed" ] && [ -d "$seed" ]; then
        for f in "$seed"/wine-*.tar.xz "$seed"/Fork-*.exe "$seed"/winetricks-*; do
            [ -f "$f" ] && cp -n "$f" "$R/home/.cache/fork-linux/downloads/"
        done
    fi
    if [ -n "$cache" ] && [ -d "$cache" ] && [ ! -d "$R/home/.cache/winetricks" ]; then
        cp -r "$cache" "$R/home/.cache/winetricks"
    fi
    printf 'scratch root ready: %s\n' "$R"
}

cmd_xvfb() {
    if xdpyinfo -display "$DISP" >/dev/null 2>&1; then
        printf 'display %s already up\n' "$DISP"
        return 0
    fi
    # -noreset: a server reset recompiles the XKB keymap into /tmp, which fails (and
    # kills Xvfb) when /tmp is full.
    nohup setsid Xvfb "$DISP" -screen 0 1920x1200x24 -nolisten tcp -noreset >"$R/logs/xvfb.log" 2>&1 &
    printf '%s\n' "$!" >"$R/run/xvfb.pid"
    sleep 1
    xdpyinfo -display "$DISP" >/dev/null 2>&1 || die "Xvfb did not start on $DISP"
    printf 'Xvfb %s pid %s\n' "$DISP" "$(cat "$R/run/xvfb.pid")"
}

cmd_cli() {
    qa_env
    python3 -m fork_linux "$@"
}

cmd_run() {
    qa_env
    local ts
    ts="$(date +%H%M%S)"
    nohup python3 -m fork_linux run "$@" >"$R/logs/run-$ts.log" 2>&1 &
    printf 'launched (pid %s, log %s)\n' "$!" "$R/logs/run-$ts.log"
}

cmd_wine() {
    qa_env
    local bin
    bin="$(wine_bin_dir)" || die "managed wine not installed yet"
    WINEPREFIX="$(prefix)" WINEDEBUG=-all WINEDLLOVERRIDES="winemenubuilder.exe=" "$bin/wine" "$@"
}

cmd_kill() {
    qa_env
    local bin
    bin="$(wine_bin_dir)" || return 0
    WINEPREFIX="$(prefix)" "$bin/wineserver" -k || true
}

cmd_stop() {
    cmd_kill
    if [ -f "$R/run/xvfb.pid" ]; then
        kill "$(cat "$R/run/xvfb.pid")" 2>/dev/null || true
        rm -f "$R/run/xvfb.pid"
    fi
}

cmd_shot() {
    qa_env
    local name="${1:?shot name}"
    import -display "$DISP" -window root "$R/shots/$name.png"
    printf '%s\n' "$R/shots/$name.png"
}

cmd_win() {
    qa_env
    local w
    for w in $(xdotool search --onlyvisible --class fork.exe 2>/dev/null); do
        printf '%s\t%s\t%s\n' "$w" "$(xdotool getwindowgeometry "$w" | tr '\n' ' ')" "$(xdotool getwindowname "$w")"
    done
}

cmd_key() {
    qa_env
    local w
    w="$(fork_wid)"
    [ -n "$w" ] && xdotool windowactivate --sync "$w" 2>/dev/null || true
    xdotool key --delay 80 "$@"
}

cmd_type() {
    qa_env
    xdotool type --delay 40 -- "$*"
}

cmd_click() {
    qa_env
    xdotool mousemove "$1" "$2" sleep 0.2 click 1
}

commit_n() {
    # commit_n <dir> <count> <prefix>: append-and-commit loop with fixed dates.
    local dir="$1" n="$2" pfx="$3" i
    for i in $(seq 1 "$n"); do
        printf '%s line %s\n' "$pfx" "$i" >>"$dir/log-$pfx.txt"
        git -C "$dir" add -A
        GIT_AUTHOR_DATE="2026-01-01T10:$((i % 60)):00" GIT_COMMITTER_DATE="2026-01-01T10:$((i % 60)):00" \
            git -C "$dir" commit -q -m "$pfx: change $i"
    done
}

cmd_fixtures() {
    local base="$R/repos"
    mkdir -p "$base"
    export GIT_AUTHOR_NAME="QA Fixture" GIT_AUTHOR_EMAIL="fixture@example.invalid"
    export GIT_COMMITTER_NAME="QA Fixture" GIT_COMMITTER_EMAIL="fixture@example.invalid"
    local main="$base/main" bare="$base/remote.git"
    rm -rf "${main:?}" "${bare:?}" "${base:?}/withsub" "${base:?}/subproj" "${base:?}/hooks" "${base:?}/conflict"
    git init -q -b main "$main"
    printf '#!/bin/sh\necho hello\n' >"$main/run.sh"
    chmod +x "$main/run.sh"
    printf 'target file\n' >"$main/target.txt"
    ln -s target.txt "$main/link-to-target"
    python3 - "$main/image.png" <<'PY'
import struct, sys, zlib
w = h = 64
raw = b"".join(b"\x00" + b"".join(bytes((x * 4 % 256, y * 4 % 256, 128)) for x in range(w)) for y in range(h))
def chunk(t, d):
    return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")
open(sys.argv[1], "wb").write(png)
PY
    head -c 20000000 /dev/urandom | base64 >"$main/large.txt"
    printf 'crlf line 1\r\ncrlf line 2\r\n' >"$main/crlf.txt"
    printf 'lf line 1\nlf line 2\n' >"$main/lf.txt"
    printf 'umlaut\n' >"$main/grüße-ü.txt"
    printf 'chinese\n' >"$main/中文.txt"
    mkdir -p "$main/dir with spaces"
    printf 'spaced\n' >"$main/dir with spaces/file name.txt"
    printf 'def hello():\n    return "world"\n' >"$main/code.py"
    git -C "$main" add -A
    git -C "$main" commit -q -m "initial fixture files"
    git -C "$main" tag -a v0.1 -m "first tag"
    commit_n "$main" 10 main
    git -C "$main" checkout -q -b feature/alpha
    commit_n "$main" 5 alpha
    git -C "$main" tag v0.2-alpha
    git -C "$main" checkout -q main
    git -C "$main" checkout -q -b bugfix/beta
    commit_n "$main" 4 beta
    git -C "$main" checkout -q main
    commit_n "$main" 3 main2
    git -C "$main" merge -q --no-ff feature/alpha -m "Merge branch 'feature/alpha'"
    git -C "$main" tag v1.0
    git clone -q --bare "$main" "$bare"
    git -C "$main" remote add origin "file://$bare"
    git -C "$main" fetch -q origin
    git -C "$main" branch -q -u origin/main main
    # Uncommitted state for the local-changes view.
    printf 'modified\n' >>"$main/code.py"
    printf 'new untracked\n' >"$main/untracked.txt"

    # Submodule.
    git init -q -b main "$base/subproj"
    printf 'sub\n' >"$base/subproj/sub.txt"
    git -C "$base/subproj" add -A
    git -C "$base/subproj" commit -q -m "sub initial"
    git init -q -b main "$base/withsub"
    printf 'super\n' >"$base/withsub/README"
    git -C "$base/withsub" add -A
    git -C "$base/withsub" commit -q -m "super initial"
    git -C "$base/withsub" -c protocol.file.allow=always submodule add -q "file://$base/subproj" libs/subproj
    git -C "$base/withsub" commit -q -m "add submodule"

    # Hook that runs a program.
    git init -q -b main "$base/hooks"
    printf 'a\n' >"$base/hooks/a.txt"
    git -C "$base/hooks" add -A
    git -C "$base/hooks" commit -q -m "initial"
    cat >"$base/hooks/.git/hooks/pre-commit" <<'HOOK'
#!/bin/sh
echo "pre-commit hook ran: $(date +%s)" >>"$(git rev-parse --git-dir)/hook-ran.log"
ls >/dev/null
exit 0
HOOK
    chmod +x "$base/hooks/.git/hooks/pre-commit"
    printf 'b\n' >"$base/hooks/b.txt"

    # Conflict scenario: main and other both edit line 2.
    git init -q -b main "$base/conflict"
    printf 'line 1\nline 2\nline 3\n' >"$base/conflict/shared.txt"
    git -C "$base/conflict" add -A
    git -C "$base/conflict" commit -q -m "base"
    git -C "$base/conflict" checkout -q -b other
    printf 'line 1\nline 2 from other\nline 3\n' >"$base/conflict/shared.txt"
    git -C "$base/conflict" commit -q -am "other edits line 2"
    git -C "$base/conflict" checkout -q main
    printf 'line 1\nline 2 from main\nline 3\n' >"$base/conflict/shared.txt"
    git -C "$base/conflict" commit -q -am "main edits line 2"

    # Extra small repos for the tab stress test.
    local i
    for i in 1 2 3; do
        rm -rf "${base:?}/tab$i"
        git init -q -b main "$base/tab$i"
        printf 'tab %s\n' "$i" >"$base/tab$i/f.txt"
        git -C "$base/tab$i" add -A
        git -C "$base/tab$i" commit -q -m "tab$i initial"
    done
    printf 'fixtures in %s\n' "$base"
}

cmd_bigrepo() {
    local big="$R/repos/big"
    rm -rf "${big:?}"
    git init -q -b main "$big"
    python3 - "$big" <<'PY'
import os, subprocess, sys
root = sys.argv[1]
for i in range(3000):
    d = os.path.join(root, f"d{i // 100:02d}")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, f"f{i:04d}.txt"), "w") as fh:
        fh.write(f"file {i}\n")
# 5000 commits via fast-import (fast and deterministic).
lines = []
mark = 0
ts = 1767225600
for c in range(5000):
    mark += 1
    path = f"d{c % 30:02d}/f{(c * 7) % 3000:04d}.txt"
    content = f"file {(c * 7) % 3000}\nrev {c}\n".encode()
    msg = f"big: commit {c}\n".encode()
    lines.append(b"commit refs/heads/main\n")
    lines.append(f"mark :{mark}\n".encode())
    lines.append(f"author QA Fixture <fixture@example.invalid> {ts + c * 60} +0000\n".encode())
    lines.append(f"committer QA Fixture <fixture@example.invalid> {ts + c * 60} +0000\n".encode())
    lines.append(f"data {len(msg)}\n".encode() + msg)
    if c > 0:
        lines.append(f"from :{mark - 1}\n".encode())
    lines.append(f"M 100644 inline {path}\n".encode())
    lines.append(f"data {len(content)}\n".encode() + content + b"\n")
subprocess.run(["git", "-C", root, "fast-import", "--quiet"], input=b"".join(lines), check=True)
PY
    git -C "$big" reset -q --hard main 2>/dev/null || true
    git -C "$big" add -A
    GIT_AUTHOR_NAME="QA Fixture" GIT_AUTHOR_EMAIL="fixture@example.invalid" \
        GIT_COMMITTER_NAME="QA Fixture" GIT_COMMITTER_EMAIL="fixture@example.invalid" \
        git -C "$big" commit -q -m "add 3000 files"
    printf 'big repo: %s commits, %s files\n' "$(git -C "$big" rev-list --count HEAD)" "$(git -C "$big" ls-files | wc -l)"
}

cmd_logs() {
    local forklog
    forklog="$(prefix)/drive_c/users/$USER/AppData/Local/Fork/logs/fork.log"
    printf '== %s\n' "$forklog"
    tail -n "${1:-40}" "$forklog" 2>/dev/null || printf '(missing)\n'
    printf '== wine-last.log\n'
    tail -n "${1:-40}" "$R/home/.local/state/fork-linux/logs/wine-last.log" 2>/dev/null || printf '(missing)\n'
}

main() {
    local cmd="${1:-}"
    [ -n "$cmd" ] || die "usage: fl-qa.sh <env|init|xvfb|setup|cli|run|wine|shot|win|key|type|click|fixtures|bigrepo|logs|kill|stop>"
    shift
    case "$cmd" in
        env) cmd_env ;;
        init) cmd_init ;;
        xvfb) cmd_xvfb ;;
        setup) cmd_cli setup --accept-fork-eula --no-gui "$@" ;;
        cli) cmd_cli "$@" ;;
        run) cmd_run "$@" ;;
        wine) cmd_wine "$@" ;;
        shot) cmd_shot "$@" ;;
        win) cmd_win ;;
        key) cmd_key "$@" ;;
        type) cmd_type "$@" ;;
        click) cmd_click "$@" ;;
        fixtures) cmd_fixtures ;;
        bigrepo) cmd_bigrepo ;;
        logs) cmd_logs "$@" ;;
        kill) cmd_kill ;;
        stop) cmd_stop ;;
        *) die "unknown command: $cmd" ;;
    esac
}

main "$@"
