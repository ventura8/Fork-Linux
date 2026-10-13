#!/usr/bin/env bash
# run.sh - run the B1/B2 bridge spikes under Wine in an isolated scratch prefix.
#
# usage: bridge/spike/run.sh [b1|b2|bench|all]      (default: all; run build.sh first)
#
#   b1     probe-{con,gui}.exe CreateProcessW matrix against the four native targets,
#          from four parent contexts (console/GUI probe, started from the shell or
#          nested under a .NET-like CREATE_NO_WINDOW + pipes parent) -> b1/summary.md
#   b2     rendezvous correctness: git --version, exit codes, 10 MB stdin echo,
#          100 MB stdout, stderr separation, argv/cwd/token, kill propagation
#          -> b2/report.txt
#   bench  latency: 100 sequential spawns, bash loop vs one wine process -> bench/report.txt
#
# Environment:
#   FL_SPIKE_ROOT    scratch root (default: ${TMPDIR:-/tmp}/fork-linux-bridge-spike-<uid>)
#   FL_SPIKE_HOME    fake HOME for Wine          (default: $FL_SPIKE_ROOT/home)
#   FL_SPIKE_PREFIX  WINEPREFIX                  (default: $FL_SPIKE_ROOT/prefix)
#   FL_SPIKE_WINE    wine binary                 (default: wine from PATH)
#   FL_SPIKE_TAG     results sub-directory       (default: from `wine --version`)
#   FL_SPIKE_N       benchmark iterations        (default: 100)
# An ambient WINEPREFIX is ignored, and ~/.wine is refused outright.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="${HERE}/out"
WHAT="${1:-all}"
N="${FL_SPIKE_N:-100}"

ROOT="${FL_SPIKE_ROOT:-${TMPDIR:-/tmp}/fork-linux-bridge-spike-$(id -u)}"
mkdir -p "${ROOT}"
ROOT="$(cd "${ROOT}" && pwd -P)"
SPIKE_HOME="${FL_SPIKE_HOME:-${ROOT}/home}"
SPIKE_PREFIX="${FL_SPIKE_PREFIX:-${ROOT}/prefix}"
WINE="${FL_SPIKE_WINE:-$(command -v wine || true)}"

die() {
  echo "run.sh: $*" >&2
  exit 1
}

[[ -n "${WINE}" && -x "${WINE}" ]] || die "wine not found (set FL_SPIKE_WINE)"
for f in probe-con.exe probe-gui.exe rv-con.exe rv-gui.exe noop-con.exe noop-gui.exe drv.exe \
  rv-helper rv-helper-high rv-helper-dyn target-script.sh target-dyn-pie target-static-nopie \
  target-static-pie target-static-high; do
  [[ -e "${OUT}/${f}" ]] || die "missing ${OUT}/${f}; run bridge/spike/build.sh first"
done

REAL_HOME="$(getent passwd "$(id -u)" | cut -d: -f6)"
mkdir -p "${SPIKE_HOME}"
SPIKE_PREFIX="$(mkdir -p "$(dirname "${SPIKE_PREFIX}")" && cd "$(dirname "${SPIKE_PREFIX}")" && pwd -P)/$(basename "${SPIKE_PREFIX}")"
case "${SPIKE_PREFIX}/" in
  "${REAL_HOME}/.wine/"*) die "refusing to use ${SPIKE_PREFIX}: never touch ~/.wine" ;;
  *) ;;
esac

export HOME="${SPIKE_HOME}"
export WINEPREFIX="${SPIKE_PREFIX}"
export WINEDEBUG=-all
export WINEDLLOVERRIDES="mscoree,mshtml=;winemenubuilder.exe=d"
unset WINEARCH WINELOADER WINESERVER DISPLAY WAYLAND_DISPLAY

WINE_DIR="$(dirname "${WINE}")"
for c in "${WINE_DIR}/wineserver" /usr/lib/wine/wineserver /usr/lib/x86_64-linux-gnu/wine/wineserver; do
  if [[ -x "${c}" ]]; then
    export WINESERVER="${c}"
    break
  fi
done
[[ -n "${WINESERVER:-}" ]] || die "wineserver not found next to ${WINE}"

WINE_VERSION="$("${WINE}" --version 2> /dev/null || echo unknown)"
TAG="${FL_SPIKE_TAG:-$(printf '%s' "${WINE_VERSION}" | tr -c 'A-Za-z0-9._-' '_')}"
RES="${ROOT}/results/${TAG}"
mkdir -p "${RES}"

if [[ ! -f "${WINEPREFIX}/system.reg" ]]; then
  echo "==> wineboot -i (${WINEPREFIX})"
  "${WINE}" wineboot -i > "${RES}/wineboot.log" 2>&1
fi
"${WINESERVER}" -p > /dev/null 2>&1 || true
cleanup() {
  if declare -F mode_off > /dev/null; then
    mode_off
  fi
  "${WINESERVER}" -k 2> /dev/null || true
}
trap cleanup EXIT

now_ms() { # wall clock in milliseconds (bash 5 EPOCHREALTIME)
  local t="${EPOCHREALTIME/[.,]/}"
  echo "$((t / 1000))"
}

{
  echo "date: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "wine: ${WINE} (${WINE_VERSION})"
  echo "wineserver: ${WINESERVER}"
  echo "kernel: $(uname -r)"
  echo "cpu: $(grep -m1 'model name' /proc/cpuinfo | cut -d: -f2- | sed 's/^ *//')"
  echo "git: $(git --version)"
  echo "WINEPREFIX: ${WINEPREFIX}"
} > "${RES}/env.txt"

# ----------------------------------------------------------------------------- B1
b1() {
  local res="${RES}/b1"
  local tgts=("${OUT}/target-script.sh" "${OUT}/target-dyn-pie" "${OUT}/target-static-nopie"
    "${OUT}/target-static-pie" "${OUT}/target-static-high")
  rm -rf "${res}"
  mkdir -p "${res}/logs" "${res}/cwd dir"
  b1_ctx() { # b1_ctx <ctx> <launcher...>
    local ctx="$1"
    shift
    echo "==> B1 ${ctx}"
    "${WINE}" "$@" "${ctx}" "${res}/results.tsv" "${res}/logs" "${res}/cwd dir" "${tgts[@]}" \
      < /dev/null > "${res}/${ctx}.stdout" 2> "${res}/${ctx}.stderr" || echo "    (exit $?)"
  }
  b1_ctx con-shell "${OUT}/probe-con.exe"
  b1_ctx gui-shell "${OUT}/probe-gui.exe"
  b1_ctx con-nested "${OUT}/drv.exe" run "${OUT}/probe-con.exe"
  b1_ctx gui-nested "${OUT}/drv.exe" run "${OUT}/probe-gui.exe"
  python3 -I - "${res}" > "${res}/summary.md" << 'PY'
import collections, os, re, sys

res = sys.argv[1]
cols = ("ctx target flags stdio cwdmode created gle hproc pid wait wait_gle exitcode spawn_ms ran ran_ms "
        "argv_ok argv0 session_leader ctty ppid cwd path home winehome token fd0 fd1 fd2 stdin outw errw "
        "out_got err_got").split()
rows = []
with open(os.path.join(res, "results.tsv"), encoding="utf-8", errors="replace") as fh:
    for line in fh:
        parts = line.rstrip("\n").split("\t")
        if len(parts) == len(cols):
            rows.append(dict(zip(cols, parts)))
captures = {}
for ctx in ("con-shell", "gui-shell", "con-nested", "gui-nested"):
    for stream in ("stdout", "stderr"):
        p = os.path.join(res, f"{ctx}.{stream}")
        captures[(ctx, stream)] = open(p, encoding="utf-8", errors="replace").read() if os.path.exists(p) else ""

def fdkind(v, ctx):
    if v == "/dev/null":
        return "null"
    if v.startswith("socket:"):
        return "socket"
    if v.startswith("pipe:"):
        return "pipe"
    for stream in ("stdout", "stderr"):
        if v.endswith(f"/{ctx}.{stream}"):
            return f"wine-{stream}"
    if v.endswith((".stdin", ".stdout", ".stderr")):
        return "file"
    if v.startswith("/dev/pts") or v.startswith("/dev/tty"):
        return "tty"
    return "other:" + v[-24:]

def case_id(r):
    return f"{r['ctx']}.{r['target']}.{r['flags']}.{r['stdio']}" + (".nullcwd" if r["cwdmode"] == "null" else "")

def where(r, tag):
    cid = case_id(r)
    marker = f"{tag}-{cid}"
    got = r["out_got"] if tag == "OUT" else r["err_got"]
    places = []
    if marker in got.split():
        places.append("parent-" + r["stdio"])
    for stream in ("stdout", "stderr"):
        if re.search(re.escape(marker) + r"$", captures[(r["ctx"], stream)], re.M):
            places.append("wine-" + stream)
    return "+".join(places) if places else "lost"

print("# B1 raw summary\n")
print(f"{len(rows)} cases.\n")
print("## Per target\n")
print("| target | cases | CreateProcessW ok | GLE when failed | hProcess | WaitForSingleObject | ran |")
print("|---|---|---|---|---|---|---|")
by_t = collections.defaultdict(list)
for r in rows:
    by_t[r["target"]].append(r)
for t, rs in by_t.items():
    ok = sum(r["created"] == "1" for r in rs)
    gles = sorted({r["gle"] for r in rs if r["created"] == "0"})
    hp = sorted({r["hproc"] for r in rs})
    waits = sorted({f"{r['wait']}({r['wait_gle']})" for r in rs})
    ran = sum(r["ran"] == "1" for r in rs)
    print(f"| {t} | {len(rs)} | {ok} | {','.join(gles) or '-'} | {','.join(hp)} | {','.join(waits)} | {ran} |")

print("\n## Std handles by parent context x flags x stdio (targets that ran)\n")
print("| ctx | flags | stdio | setsid | fd0 | fd1 | fd2 | stdin data | OUT marker | ERR marker | consistent |")
print("|---|---|---|---|---|---|---|---|---|---|---|")
groups = collections.OrderedDict()
for r in rows:
    if r["ran"] != "1" or r["cwdmode"] != "explicit":
        continue
    key = (r["ctx"], r["flags"], r["stdio"])
    groups.setdefault(key, []).append(r)
for (ctx, flags, stdio), rs in groups.items():
    descs = set()
    for r in rs:
        stdin_ok = "IN-marker" if r["stdin"].startswith("IN-") else ("eof" if r["stdin"] in ("", "<eof>") else r["stdin"])
        descs.add((r["session_leader"], fdkind(r["fd0"], ctx), fdkind(r["fd1"], ctx), fdkind(r["fd2"], ctx),
                   stdin_ok, where(r, "OUT"), where(r, "ERR")))
    d = sorted(descs)[0]
    print(f"| {ctx} | {flags} | {stdio} | {d[0]} | {d[1]} | {d[2]} | {d[3]} | {d[4]} | {d[5]} | {d[6]} | "
          f"{'yes' if len(descs) == 1 else 'NO: ' + str(sorted(descs))} |")

print("\n## argv / cwd / environment (targets that ran)\n")
ran_rows = [r for r in rows if r["ran"] == "1"]
def tally(fn):
    c = collections.Counter(fn(r) for r in ran_rows)
    return ", ".join(f"{k}: {v}" for k, v in c.most_common())
cwd_expected = os.path.join(res, "cwd dir")
print(f"- argv[3..] round-trip exact: {tally(lambda r: r['argv_ok'])}")
print(f"- argv[0] forms: {tally(lambda r: 'DOS path' if re.match(r'^[A-Za-z]:', r['argv0']) else r['argv0'][:40])}")
print(f"- cwd (explicit lpCurrentDirectory) honoured: "
      f"{tally(lambda r: 'n/a(null)' if r['cwdmode'] == 'null' else str(r['cwd'] == cwd_expected))}")
print(f"- cwd when lpCurrentDirectory=NULL: "
      f"{', '.join(sorted({r['cwd'] for r in ran_rows if r['cwdmode'] == 'null'}))}")
print(f"- FL_TOKEN from env block visible: {tally(lambda r: str(r['token'].startswith('tok-')))}")
print(f"- PATH is the unix PATH: {tally(lambda r: str(r['path'].startswith('/')))}")
print(f"- HOME is the unix HOME (block had HOME=C:\\fakehome): {tally(lambda r: str(r['home'].startswith('/')))}")
print(f"- WINEHOME (renamed Windows HOME): {tally(lambda r: r['winehome'])}")
print(f"- controlling tty: {tally(lambda r: r['ctty'])}")
print(f"- ppid: {tally(lambda r: r['ppid'])}")
print(f"- GetExitCodeProcess(pi.hProcess) value: {tally(lambda r: r['exitcode'])} (target exits 7; 4294967295 = call failed)")
print(f"- CreateProcessW duration ms (ran cases): min {min(float(r['spawn_ms']) for r in ran_rows):.2f}, "
      f"median {sorted(float(r['spawn_ms']) for r in ran_rows)[len(ran_rows)//2]:.2f}")
print(f"- dwProcessId values (sample; target pids are unrelated): {sorted({r['pid'] for r in ran_rows})[:6]}")

print("\n## Inherited seccomp state (from the target's /proc/self/status)\n")
sec = collections.Counter()
for r in ran_rows:
    p = os.path.join(res, "logs", case_id(r) + ".log")
    try:
        text = open(p, encoding="utf-8", errors="replace").read()
    except OSError:
        continue
    vals = dict(re.findall(r"^status\.(Seccomp|Seccomp_filters|NoNewPrivs)=(\S+)$", text, re.M))
    sec[(r["target"], vals.get("Seccomp", "?"), vals.get("Seccomp_filters", "?"), vals.get("NoNewPrivs", "?"))] += 1
for (t, mode, nf, nnp), n in sorted(sec.items()):
    print(f"- {t}: Seccomp={mode} Seccomp_filters={nf} NoNewPrivs={nnp} ({n} cases)")

print("\n## Inherited extra fds (fd >= 3, excluding the target's own log)\n")
logs = os.path.join(res, "logs")
per_ctx = collections.defaultdict(list)
for r in ran_rows:
    p = os.path.join(logs, case_id(r) + ".log")
    try:
        text = open(p, encoding="utf-8", errors="replace").read()
    except OSError:
        continue
    extra = [l for l in text.splitlines() if re.match(r"^fd\.(\d+)=", l) and int(l[3:l.index("=")]) >= 3
             and not l.endswith(".log")]
    per_ctx[r["ctx"]].append((len(extra), extra[:3]))
for ctx, vals in per_ctx.items():
    counts = [v[0] for v in vals]
    print(f"- {ctx}: extra fds per case min {min(counts)} max {max(counts)}; e.g. {vals[-1][1]}")
PY
  echo "    -> ${res}/summary.md"
}

# ----------------------------------------------------------------------------- B2
B2_PASS=0
B2_FAIL=0
report() { # report PASS|FAIL <name> <detail>
  printf '%-4s %-14s %-46s %s\n' "$1" "[${MODE}]" "$2" "$3" | tee -a "${RES}/b2/report.txt"
  if [[ "$1" == PASS ]]; then B2_PASS=$((B2_PASS + 1)); else B2_FAIL=$((B2_FAIL + 1)); fi
}

info() { # info <name> <detail>: recorded, not counted
  printf '%-4s %-14s %-46s %s\n' INFO "[${MODE}]" "$1" "$2" | tee -a "${RES}/b2/report.txt"
}

rv() { # rv <variant> <direct|dotnet> <program> [args...]
  local variant="$1" path="$2"
  shift 2
  if [[ "${path}" == direct ]]; then
    "${WINE}" "${OUT}/${variant}.exe" "$@"
  else
    "${WINE}" "${OUT}/drv.exe" run "${OUT}/${variant}.exe" "$@"
  fi
}

check() { # check <name> <expected> <actual>
  if [[ "$2" == "$3" ]]; then report PASS "$1" "$3"; else report FAIL "$1" "expected [$2] got [$3]"; fi
}

kill_test() { # kill_test <variant> <how: KILL|TERM|TerminateProcess> [limit_ms] [ignore-term]
  local variant="$1" how="$2" limit="${3:-3000}" mode="${4:-}" tag wpid t0 t1 i
  local marker="${RES}/b2/kill.marker" cmd
  tag="30.$((RANDOM % 9000 + 1000))$((RANDOM % 9000 + 1000))"
  cmd=(sleep "${tag}")
  if [[ "${mode}" == ignore-term ]]; then
    cmd=(sh -c "trap '' TERM; sleep ${tag}; :")
  fi
  rm -f "${marker}"
  if [[ "${how}" == TerminateProcess ]]; then
    "${WINE}" "${OUT}/drv.exe" kill 1500 "${marker}" "${OUT}/${variant}.exe" "${cmd[@]}" \
      < /dev/null > "${RES}/b2/kill.out" 2>&1 &
  else
    "${WINE}" "${OUT}/${variant}.exe" "${cmd[@]}" < /dev/null > /dev/null 2>&1 &
  fi
  wpid=$!
  for ((i = 0; i < 200; i++)); do
    pgrep -f -x "sleep ${tag}" > /dev/null && break
    sleep 0.05
  done
  if ! pgrep -f -x "sleep ${tag}" > /dev/null; then
    report FAIL "kill ${variant} ${how}" "sleep never started"
    kill -KILL "${wpid}" 2> /dev/null || true
    return
  fi
  if [[ "${how}" == TerminateProcess ]]; then
    for ((i = 0; i < 200; i++)); do
      [[ -e "${marker}" ]] && break
      sleep 0.01
    done
  else
    exec 3>&2 2> /dev/null # silence bash's "Killed" job notice for the wine process
    kill "-${how}" "${wpid}"
  fi
  t0="$(now_ms)"
  for ((i = 0; i < 1000; i++)); do
    pgrep -f -x "sleep ${tag}" > /dev/null || break
    sleep 0.01
  done
  t1="$(now_ms)"
  wait "${wpid}" || true
  if [[ "${how}" != TerminateProcess ]]; then
    exec 2>&3 3>&-
  fi
  if pgrep -f -x "sleep ${tag}" > /dev/null; then
    pkill -KILL -f -x "sleep ${tag}" || true
    report FAIL "kill ${variant} ${how} ${mode}" "sleep still alive after $((t1 - t0)) ms"
  elif ((t1 - t0 <= limit)); then
    report PASS "kill ${variant} ${how} ${mode}" "sleep gone after $((t1 - t0)) ms (limit ${limit})"
  else
    report FAIL "kill ${variant} ${how} ${mode}" "sleep gone after $((t1 - t0)) ms (> ${limit})"
  fi
}

sigpipe_test() { # sigpipe_test <variant>: reader closes early -> rv exits 141, child reaped
  local variant="$1" tag rc first i r
  tag="fl-spike-$((RANDOM % 9000 + 1000))$((RANDOM % 9000 + 1000))"
  {
    r=0
    "${WINE}" "${OUT}/${variant}.exe" yes "${tag}" < /dev/null 2> /dev/null || r=$?
    echo "${r}" > "${RES}/b2/sigpipe.rc"
  } | head -n 1 > "${RES}/b2/sigpipe.out"
  first="$(cat "${RES}/b2/sigpipe.out")"
  rc="$(cat "${RES}/b2/sigpipe.rc")"
  for ((i = 0; i < 300; i++)); do
    pgrep -f -x "yes ${tag}" > /dev/null || break
    sleep 0.01
  done
  if pgrep -f -x "yes ${tag}" > /dev/null; then
    pkill -KILL -f -x "yes ${tag}" || true
    report FAIL "${variant}/direct SIGPIPE (yes | head -1)" "yes still running; rv rc=${rc}"
  else
    check "${variant}/direct SIGPIPE (yes | head -1)" "${tag} rc=141" "${first} rc=${rc}"
  fi
}

parallel_test() { # parallel_test <variant>: 3 rounds of 20 concurrent bridged commands
  local variant="$1" i round fails=0
  for ((round = 0; round < 3; round++)); do
    parallel_round "${variant}"
  done
  check "${variant}/direct 3x20 parallel (output + exit code)" "0 failures" "${fails} failures"
}

parallel_round() { # one round of 20 concurrent jobs; increments the caller's ${fails}
  local variant="$1" i
  for ((i = 0; i < 20; i++)); do
    "${WINE}" "${OUT}/${variant}.exe" sh -c "${PAR_JOB}" sh "${i}" \
      < /dev/null > "${RES}/b2/par.${i}.out" 2>&1 &
    echo "$!" > "${RES}/b2/par.${i}.pid"
  done
  for ((i = 0; i < 20; i++)); do
    local rc=0
    wait "$(cat "${RES}/b2/par.${i}.pid")" || rc=$?
    if [[ "${rc}" != "$((i % 7))" || "$(cat "${RES}/b2/par.${i}.out")" != "job-${i}" ]]; then
      fails=$((fails + 1))
      info "${variant}/direct parallel job ${i} failed" \
        "rc=${rc} (want $((i % 7))) out=[$(head -c 200 "${RES}/b2/par.${i}.out" | tr '\n' ' ')]"
    fi
  done
}

# Arguments that must survive Windows command-line quoting unchanged.
ARGV_CASES=('a b' 'q"x' 'back\slash' "tail\\" '' "\$HOME;\`id\`" 'ü€')
# Parallel job: print job-<n>, exit n % 7.
PAR_JOB="printf 'job-%s\\n' \"\$1\"; exit \$((\$1 % 7))"
# Child-side check: the token must be scrubbed and only fds 0-2 (+ ls's dir fd) open.
ENV_FD_PROBE="echo \"\${FL_BRIDGE_TOKEN-unset} \$(ls /proc/self/fd | wc -l)\""
# Child-side facts: inherited seccomp / no_new_privs state.
SECCOMP_PROBE="sed -n 's/^\\(Seccomp\\|NoNewPrivs\\):[[:space:]]*/\\1=/p' /proc/self/status | tr '\\n' ' '"

# A real Go program (Go issues raw syscalls from its own text), if the host has one.
GO_BIN=""
for c in git-lfs go gh docker; do
  if GO_BIN="$(command -v "${c}")"; then
    break
  fi
  GO_BIN=""
done

# Helper modes (FL_SPIKE_MODES):
#   percall       rv.exe CreateProcessW's out/rv-helper (musl static ET_EXEC @0x400000; the plan)
#   percall-high  rv.exe CreateProcessW's out/rv-helper-high (static ET_EXEC @0x7e0000000000)
#   percall-dyn   rv.exe CreateProcessW's out/rv-helper-dyn (glibc dynamic PIE)
#   daemon        out/rv-helper --daemon started by this script outside Wine; rv.exe connects
MODES="${FL_SPIKE_MODES:-percall percall-high percall-dyn daemon}"
MODE=""
DAEMON_PID=""

dos_path() { # unix path -> Z:\... (Wine's default Z: = /)
  local p="$1"
  printf 'Z:%s' "${p//\//\\}"
}

mode_off() {
  unset FL_RV_HELPER FL_RV_DAEMON_PORT FL_BRIDGE_TOKEN
  if [[ -n "${DAEMON_PID:-}" ]]; then
    {
      kill "${DAEMON_PID}" || true
      wait "${DAEMON_PID}" || true
    } 2> /dev/null
    DAEMON_PID=""
  fi
}

mode_on() { # mode_on <mode>: export what rv.exe reads for that mode
  local pf tok i
  mode_off
  MODE="$1"
  case "${MODE}" in
    percall) ;;
    percall-high | percall-dyn)
      export FL_RV_HELPER
      FL_RV_HELPER="$(dos_path "${OUT}/rv-helper-${MODE#percall-}")"
      ;;
    daemon)
      pf="${RES}/daemon.port"
      rm -f "${pf}"
      tok="$(od -An -N32 -tx1 /dev/urandom | tr -d ' \n')"
      FL_BRIDGE_TOKEN="${tok}" "${OUT}/rv-helper" --daemon "${pf}" < /dev/null > /dev/null 2>&1 &
      DAEMON_PID=$!
      for ((i = 0; i < 100; i++)); do
        [[ -s "${pf}" ]] && break
        sleep 0.02
      done
      [[ -s "${pf}" ]] || die "rv-helper --daemon did not publish its port"
      export FL_RV_DAEMON_PORT FL_BRIDGE_TOKEN
      FL_RV_DAEMON_PORT="$(cat "${pf}")"
      FL_BRIDGE_TOKEN="${tok}"
      ;;
    *) die "unknown mode ${MODE}" ;;
  esac
}

mode_works() { # quick preflight: does 'rv-gui.exe true' work in the current mode?
  FL_RV_ACCEPT_TIMEOUT_MS=3000 "${WINE}" "${OUT}/rv-gui.exe" true < /dev/null > /dev/null 2>&1
}

b2_suite() { # full correctness suite for the current MODE
  local res="${RES}/b2" v p rc out t0 t1 sha_in sha_100m
  sha_in="$(sha256sum < "${res}/in10m.bin" | cut -d' ' -f1)"
  sha_100m="$(head -c 104857600 /dev/zero | sha256sum | cut -d' ' -f1)"
  for v in rv-con rv-gui; do
    for p in direct dotnet; do
      echo "==> B2 [${MODE}] ${v} ${p}"
      out="$(rv "${v}" "${p}" git --version < /dev/null 2> "${res}/err")" && rc=0 || rc=$?
      [[ -s "${res}/err" ]] && out="${out} stderr=[$(head -c 300 "${res}/err" | tr '\n' ' ')]"
      check "${v}/${p} git --version" "$(git --version) rc=0" "${out} rc=${rc}"

      rv "${v}" "${p}" sh -c 'exit 42' < /dev/null > /dev/null 2>&1 && rc=0 || rc=$?
      check "${v}/${p} exit 42" "42" "${rc}"

      rv "${v}" "${p}" sh -c 'kill -TERM $$' < /dev/null > /dev/null 2>&1 && rc=0 || rc=$?
      check "${v}/${p} child killed by SIGTERM" "143" "${rc}"

      rv "${v}" "${p}" /nonexistent/program < /dev/null > /dev/null 2>&1 && rc=0 || rc=$?
      check "${v}/${p} missing program" "127" "${rc}"

      t0="$(now_ms)"
      rv "${v}" "${p}" cat < "${res}/in10m.bin" > "${res}/out10m.bin" 2> "${res}/err" && rc=0 || rc=$?
      t1="$(now_ms)"
      check "${v}/${p} 10 MB stdin echo sha256" "${sha_in} rc=0" \
        "$(sha256sum < "${res}/out10m.bin" | cut -d' ' -f1) rc=${rc}"
      echo "     10 MB echo took $((t1 - t0)) ms" | tee -a "${res}/report.txt"

      t0="$(now_ms)"
      out="$({ rv "${v}" "${p}" head -c 104857600 /dev/zero < /dev/null 2> "${res}/err" || true; } \
        | sha256sum | cut -d' ' -f1)"
      t1="$(now_ms)"
      check "${v}/${p} 100 MB stdout sha256" "${sha_100m}" "${out}"
      echo "     100 MB stdout took $((t1 - t0)) ms" | tee -a "${res}/report.txt"

      rv "${v}" "${p}" sh -c 'echo to-out; echo to-err >&2; echo more-out' < /dev/null \
        > "${res}/o.txt" 2> "${res}/e.txt" && rc=0 || rc=$?
      check "${v}/${p} stdout separate" "to-out more-out" "$(tr '\n' ' ' < "${res}/o.txt" | sed 's/ $//')"
      check "${v}/${p} stderr separate" "to-err" "$(cat "${res}/e.txt")"

      out="$(rv "${v}" "${p}" printf '[%s]' "${ARGV_CASES[@]}" < /dev/null 2>&1)" || true
      check "${v}/${p} argv round-trip" "$(printf '[%s]' "${ARGV_CASES[@]}")" "${out}"

      out="$(cd "${res}/cwd dir" && rv "${v}" "${p}" pwd < /dev/null 2>&1)" || true
      check "${v}/${p} cwd forwarded" "${res}/cwd dir" "${out}"

      out="$(rv "${v}" "${p}" sh -c "${ENV_FD_PROBE}" < /dev/null 2>&1)" || true
      check "${v}/${p} token scrubbed, fds 0-2 only (+ls dir fd)" "unset 4" "${out}"
    done
    kill_test "${v}" KILL
    kill_test "${v}" TERM
    kill_test "${v}" TerminateProcess
    kill_test "${v}" KILL 3600 ignore-term
    sigpipe_test "${v}"
    parallel_test "${v}"

    out="$(head -c 1048576 /dev/zero | { rv "${v}" direct head -c 10 2> /dev/null || true; } | wc -c)"
    check "${v}/direct child closes stdin early" "10" "${out}"

    rv "${v}" direct "${OUT}/target-static-nopie" "${res}/descendant.log" descendant \
      < /dev/null > /dev/null 2>&1 && rc=0 || rc=$?
    check "${v}/direct static ET_EXEC grandchild (Go-like)" "7" "${rc}"

    if [[ -n "${GO_BIN}" ]]; then
      rv "${v}" direct "${GO_BIN}" version < /dev/null > /dev/null 2>&1 && rc=0 || rc=$?
      check "${v}/direct Go binary grandchild ($(basename "${GO_BIN}") version)" "0" "${rc}"
    fi

    out="$(rv "${v}" direct sh -c "${SECCOMP_PROBE}" < /dev/null 2>&1)" || true
    info "${v}/direct bridged child seccomp state" "${out}"

    if [[ "${MODE}" != daemon ]]; then
      out="$(FL_RV_TEST_BADTOKEN=1 rv "${v}" direct echo authenticated < /dev/null 2> "${res}/err")" \
        && rc=0 || rc=$?
      check "${v}/direct intruder with wrong token rejected" \
        "authenticated rc=0 rejected=1" "${out} rc=${rc} rejected=$(grep -c 'rejected a connection' "${res}/err")"

      FL_RV_HELPER='Z:\nonexistent\rv-helper' rv "${v}" direct true < /dev/null > /dev/null 2>&1 \
        && rc=0 || rc=$?
      check "${v}/direct helper missing -> bridge failure" "125" "${rc}"
    else
      out="$(FL_BRIDGE_TOKEN="$(printf '0%.0s' {1..64})" rv "${v}" direct echo authenticated \
        < /dev/null 2>&1)" && rc=0 || rc=$?
      check "${v}/direct wrong token refused by daemon" "125" "${rc}"
    fi
  done
}

b2() {
  local res="${RES}/b2" m
  rm -rf "${res}"
  mkdir -p "${res}/cwd dir"
  : > "${res}/report.txt"
  head -c 10485760 /dev/urandom > "${res}/in10m.bin"
  for m in ${MODES}; do
    mode_on "${m}"
    if mode_works; then
      b2_suite
    else
      report FAIL "preflight: rv-gui.exe true" "helper never connected (mode unusable on this Wine)"
    fi
    mode_off
  done
  MODE=""
  echo "B2: ${B2_PASS} passed, ${B2_FAIL} failed" | tee -a "${res}/report.txt"
}

# ----------------------------------------------------------------------------- bench
stats() { # stats <label> < one value (ms) per line
  sort -n | awk -v label="$1" '{ v[NR] = $1; s += $1 }
    END { n = NR; p50 = v[int(n / 2) + 1]; p95 = v[int(n * 0.95) + 1 > n ? n : int(n * 0.95) + 1];
          printf "%-44s n=%d min=%.2f p50=%.2f p95=%.2f max=%.2f mean=%.2f\n", label, n, v[1], p50, p95, v[n], s / n }'
}

bash_loop() { # bash_loop <label> <cmd...>; prints stats of per-iteration wall time
  local label="$1" i t0 t1
  shift
  for ((i = 0; i < 5; i++)); do "$@" < /dev/null > /dev/null 2>&1 || true; done
  for ((i = 0; i < N; i++)); do
    t0="${EPOCHREALTIME/[.,]/}"
    "$@" < /dev/null > /dev/null 2>&1 || true
    t1="${EPOCHREALTIME/[.,]/}"
    awk -v d="$((t1 - t0))" 'BEGIN { printf "%.3f\n", d / 1000 }'
  done | stats "${label}"
}

drv_bench() { # drv_bench <label> <exe> [args...]: N spawns inside one wine process (after 5 warm-up)
  local label="$1" exe="$2"
  shift 2
  "${WINE}" "${OUT}/drv.exe" bench 5 "${OUT}/${exe}" "$@" < /dev/null > /dev/null 2>&1 || true
  printf '%-44s ' "${label}"
  "${WINE}" "${OUT}/drv.exe" bench "${N}" "${OUT}/${exe}" "$@" < /dev/null 2>&1 || true
}

bench() {
  local res="${RES}/bench" m
  rm -rf "${res}"
  mkdir -p "${res}"
  echo "==> bench (N=${N})"
  {
    echo "# bash loop: one 'wine <exe>' (or native) process per iteration"
    bash_loop "native /bin/true" /bin/true
    bash_loop "native git --version" git --version
    bash_loop "wine noop-con.exe" "${WINE}" "${OUT}/noop-con.exe"
    bash_loop "wine noop-gui.exe" "${WINE}" "${OUT}/noop-gui.exe"
    for m in ${MODES}; do
      mode_on "${m}"
      if mode_works; then
        bash_loop "wine rv-con.exe true [${m}]" "${WINE}" "${OUT}/rv-con.exe" true
        bash_loop "wine rv-gui.exe true [${m}]" "${WINE}" "${OUT}/rv-gui.exe" true
      else
        echo "wine rv-*.exe true [${m}]: mode unusable on this Wine"
      fi
      mode_off
    done
    echo
    echo "# one wine process: drv.exe spawns the PE N times (CreateProcessW + pipes + CREATE_NO_WINDOW)"
    drv_bench "drv: noop-con.exe" noop-con.exe
    drv_bench "drv: noop-gui.exe" noop-gui.exe
    for m in ${MODES}; do
      mode_on "${m}"
      if mode_works; then
        drv_bench "drv: rv-con.exe true [${m}]" rv-con.exe true
        drv_bench "drv: rv-gui.exe true [${m}]" rv-gui.exe true
        drv_bench "drv: rv-gui.exe git --version [${m}]" rv-gui.exe git --version
      else
        echo "drv: rv-*.exe [${m}]: mode unusable on this Wine"
      fi
      mode_off
    done
  } | tee "${res}/report.txt"
}

case "${WHAT}" in
  b1) b1 ;;
  b2) b2 ;;
  bench) bench ;;
  all)
    b1
    b2
    bench
    ;;
  *) die "usage: run.sh [b1|b2|bench|all]" ;;
esac
echo "run.sh: results in ${RES}"
