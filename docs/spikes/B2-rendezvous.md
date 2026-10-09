# Spike B2: loopback-TCP rendezvous between a PE shim and a native helper

Date: 2026-10-09. Host and Wine builds are the same as [B1](B1-wine-unix-spawn.md):

- **W10**: system Wine 10.0.
- **W11s**: Kron4ek `wine-11.0-staging-amd64-wow64`, the managed default.

The prototype is in `bridge/spike/`:

- `rvproto.h`: the frame format
- `rv.c`: Windows side, built as `rv-con.exe` and `rv-gui.exe`
- `rv-helper.c`: Linux side
- `drv.c`: a driver that spawns PEs the way .NET does
- `noop.c`: baseline

Build with `bridge/spike/build.sh` (Docker `fork-linux-ci-bridge:26.04`). Run with
`bridge/spike/run.sh b2` and `bridge/spike/run.sh bench`.

## Question and criteria

Can a PE shim run a host program with streaming stdin, stdout and stderr, and return its exit code? The criteria:

- `git --version` works
- exit code 42 propagates
- a 10 MB stdin echo keeps the same sha256
- stderr stays separate
- killing the shim kills the child within 3 s
- latency is at most noop + 10 ms at p50 (plan)

## Verdict

| Transport | W10 | W11s (managed default) |
|---|---|---|
| **per-call** as in the plan: `rv.exe` runs `CreateProcessW` on a musl `-static -no-pie` helper; token in the env block | **PASS**, 66/66 | **FAIL**: the helper dies with SIGSYS at its first syscall, and the env-block token is dropped |
| per-call, helper ET_EXEC @0x7e0000000000 or glibc dynamic PIE; token also via a `$XDG_RUNTIME_DIR` file | **PASS**, 66/66 each | **FAIL**: the bridge itself works (62/66 each in the re-run, 61/66 in the original run). The 4 deterministic failures are the static and Go grandchild checks; the original run's 5th was a rare crash. Every bridged process inherits staging's seccomp filter and `NoNewPrivs=1`. Go binaries (`go`, `gh` and `docker` tested; `git-lfs` is also Go but is not installed here) and static tools die with SIGSYS (exit 159). About 1 dynamic process in 1,000 dies the same way (B1, ASLR). The 0x7e… high base only clears the filter for the helper itself. |
| **daemon**: `rv-helper --daemon` started outside Wine by the launcher; `rv.exe` connects to it with the token from its environment | **PASS**, 64/64 | **PASS** for the bridge: 64/64 in the re-run, 63/64 in the original run. The original run's one failure was a rare W11s crash of an rv.exe Wine process (see "W11s instability"). Bridged processes have no seccomp, so Go binaries work. |

**Decision:** the bridge transport becomes the **launcher-started daemon**, using the same frames and the
same per-connection lifecycle. The plan called the daemon "a v2 speed-up". On the pinned staging runtime it
is a correctness requirement. It is also faster: about +4 ms over noop, against +6 ms per call. The
per-call path is not kept; it would only work on non-staging Wine.

The daemon does **not** fix one W11s problem. About 1 PE start in 1,000 by a W11s process fails (GLE 731),
and that hits the shim exactly as it hits bundled `git.exe` (see "W11s instability"). This is a runtime
problem, not a reason to doubt the transport, but it bears on keeping the staging pin.

## Prototype

- **Frames:** a 5-byte header (type, then a big-endian u32 length), followed by the payload. Sizes:
  STDIN/STDOUT/STDERR chunks are at most 64 KiB and REQ is at most 1 MiB.
  - rv → helper: `REQ(cwd\0argv0\0argv1…)`, `STDIN`, `STDIN_EOF`.
  - helper → rv: `HELLO(64-hex token)` (per-call), `SPAWN_OK`, `SPAWN_ERR(errno)`, `STDOUT`, `STDERR`,
    `EXIT(code; 128+signal)`.
  - In daemon mode `HELLO` goes from rv to the daemon.
- **rv.exe, per-call:**
  1. Listen on `127.0.0.1:0`.
  2. Get 256 bits from `BCryptGenRandom` and hex-encode them.
  3. Run `CreateProcessW(helper --port N [--token-file F])` with `DETACHED_PROCESS` and an explicit
     env block containing `FL_BRIDGE_TOKEN`.
  4. Accept connections until one sends the right token (constant-time compare). Intruders are logged and
     dropped. Give up after 10 s with exit 125.
  5. Send REQ, with the cwd mapped by `wine_get_unix_file_name`.
  6. Pump stdin from a thread; the main thread writes STDOUT and STDERR frames to its handles.
  7. Exit codes: the program's own, 127 if it could not be spawned, 141 if rv's stdout reader went away,
     125 for a bridge failure.
- **rv-helper:**
  1. Read the token from the environment or the token file (then unlink the file), and remove it from the
     environment (`unsetenv`; the in-memory copy is not wiped).
  2. Reopen fds 0, 1 and 2 to `/dev/null` (fd 2 is kept when `FL_RV_DEBUG` is set), and close inherited
     fds ≥ 3. Call `setsid` (per-call).
  3. Connect, or accept in daemon mode, where each connection gets its own forked session.
  4. Run the child with `fork`/`exec`. The child gets `setpgid`, `PR_SET_PDEATHSIG(SIGKILL)`, a cwd and
     three `O_CLOEXEC` pipes. An exec-status pipe produces `SPAWN_ERR`.
  5. Run a `poll()` loop:
     - socket → child stdin (non-blocking, 1 MiB buffer, backpressure)
     - child stdout and stderr → frames
     - a SIGCHLD self-pipe for the exit
  6. On socket EOF, RDHUP or error, send `killpg(SIGTERM)`, then `SIGKILL` after 3 s.
  7. The final frame is followed by `shutdown(SHUT_WR)` and a drain (see bug 2 below).

## Correctness (per mode: rv-con and rv-gui, each started directly by `wine` and nested under `drv.exe run`, which mimics .NET)

All of the following passed in every working mode on both builds. W10 ran four modes, 262/262 checks, in
both the original run and the re-run. Ranges below cover both runs.

| Check | Result |
|---|---|
| `git --version` | same output as host `/usr/bin/git`, rc 0 |
| `sh -c 'exit 42'` | 42. `kill -TERM $$` gives 143. A missing program gives 127. |
| 10 MB stdin through `cat` | sha256 equal. 29–125 ms including Wine start. |
| 100 MB stdout (`head -c 100M /dev/zero`) | sha256 equal. 93–107 ms direct (≈1 GB/s including Wine start); 146–177 ms through `drv` pipes. |
| stdout and stderr separation | separate; interleaving within each stream preserved |
| argv round trip, including `'a b' 'q"x' 'back\slash' 'tail\' '' '$HOME;`id`' 'ü€'` | exact |
| cwd forwarded (path with a space) | exact |
| token scrubbed from the child env; child has only fds 0–2 | yes |
| kill rv.exe during `sleep 30`: `kill -KILL`, `kill -TERM`, `TerminateProcess` from a Windows parent | sleep gone in **15–21 ms** in every mode and on both builds. This is an upper bound: `run.sh` polls `pgrep` every ≈10 ms. |
| the same, with a child that ignores SIGTERM | killed by the SIGKILL escalation after **3007–3035 ms** |
| `rv.exe yes \| head -1` (reader closes) | rv exits 141 and `yes` is reaped |
| child closes stdin early (`head -c 10` with 1 MiB of input) | 10 bytes, no hang |
| 3×20 parallel jobs | correct output and exit code. W10: 4 consecutive clean runs after bug 4 was fixed, plus the re-run. W11s re-run: clean in all modes. The original W11s run had 1 failure in each of three modes (rare crashes). |
| intruder: wrong token first, then the real helper (per-call) | intruder rejected, command succeeds |
| wrong token sent to the daemon | refused (rv exits 125) |
| missing helper | rv exits 125 promptly |
| static ET_EXEC or Go grandchild (`go version`) | W10: all modes OK. W11s: OK only with the daemon; per-call modes give **159 (SIGSYS)**. |

## Bugs found and fixed during the spike (production code must keep these fixes)

The "before" counts come from the original run and were not re-measured. The re-run confirmed that all
four fixes are in `rv.c` and `rv-helper.c`, and the suites ran clean on W10.

1. **Exit code became 0 (W10, 4 of 200 parallel jobs).** The stdin thread returned while the main thread
   was in `ExitProcess(code)`, and Wine reported exit code 0.
   - Reproduced with a variant that returns from the thread: rc 0 instead of 3, 13, 8 and so on.
   - Fix: the pump thread never exits by itself. The main thread sets an event, calls
     `CancelSynchronousIo`, joins the thread (500 ms cap), and only then calls `ExitProcess`.
2. **`WSAECONNRESET` before EXIT** (W10, 13 of 200 parallel jobs). The helper closed its socket while
   `STDIN_EOF` was still unread. Linux then sends RST, and Wine fails rv's pending `recv`, so the EXIT frame
   is lost.
   - Fix: lingering close. Send the final frame, `shutdown(SHUT_WR)`, then drain until EOF (2 s cap).
3. **+50 ms whenever the child wrote output** (`echo hi`: p50 68 ms against 16 ms). The helper polled the
   child's exit with a 50 ms timeout.
   - Fix: a SIGCHLD self-pipe in the `poll()` set. Now `git --version` is 15.98 ms.
4. **Stray `wine client error:…: sendmsg: Socket operation on non-socket` on stderr** (about 1 per 240 jobs
   inside the full suite). `closesocket()` in the main thread raced with the pump thread's `send()`.
   - Fix: do not close the socket; join the pump thread (fix 1), and let process teardown close it.
   - Before: 3 failures in 3 suite runs. After: 0 in 4 runs.

## Latency (N=100 sequential runs, p50/p95 in ms, warm `wineserver -p`)

**One Wine process** (`drv.exe` spawns the PE N times with `CreateProcessW`, pipes and `CREATE_NO_WINDOW`,
which is what Fork does):

| | W10 p50 | W10 p95 | W11s p50 | W11s p95 |
|---|---|---|---|---|
| noop-gui.exe | 9.71 | 12.25 | 7.48 | 8.32 |
| rv-gui.exe true, per-call (plan helper) | 15.71 (+6.0) | 16.96 | unusable | |
| rv-gui.exe true, per-call-high | 15.54 (+5.8) | 16.63 | 14.15 (+6.7) | 15.53 |
| rv-gui.exe true, **daemon** | **13.64 (+3.9)** | 15.29 | **12.25 (+4.8)** | 14.86 |
| rv-gui.exe git --version, per-call / daemon | 15.98 / 13.92 | 19.64 / 14.60 | 14.39 (high) / 12.62 | 15.42 / 13.95 |
| noop-con.exe | 15.10 | 16.95 | 12.58 | 15.86 |
| rv-con.exe true, per-call / daemon | 21.14 / 19.40 | 23.36 / 21.82 | 19.17 (high) / 17.57¹ | 21.04 / 19.64¹ |

¹ Taken from the previous full W11s run. In the final run, one `CreateProcessW` failed with GLE 731 after a
SIGSYS crash of the new rv.exe, and `drv bench` aborted (see below). The re-run measured 19.39 / 17.57
(p95 22.01 / 19.58) directly.

The re-run reproduced every p50 in both tables to within 0.5 ms. W10: noop-gui 9.76, per-call 15.49,
daemon 13.74, noop-con 15.02, rv-con 21.08 / 19.39. W11s: per-call-high 14.28, daemon 12.20,
noop-con 12.45; noop-gui 7.49–7.95 in later 500-spawn rounds. On W11s three `drv bench` series aborted with
GLE 731, `noop-gui.exe` among them (see "W11s instability").

**Bash loop, one `wine <exe>` per iteration:**

| | W10 p50 | W11s p50 |
|---|---|---|
| native `/bin/true` | 0.49 | 0.51 |
| native `git --version` | 0.74 | 0.77 |
| wine noop-gui.exe | 10.73 | 7.90 |
| rv-gui.exe true, per-call | 16.89 | 14.70 (high) |
| rv-gui.exe true, daemon | 14.91 | 12.72 |

- **Criterion met.** Every working mode is within noop + 7 ms at p50. The daemon is +3.9 to +4.8 ms. The
  per-call cost is about 1.5 ms for Wine's synchronous double-fork in `CreateProcessW` (B1), plus the helper
  exec and connect-back.
- **GUI subsystem beats console subsystem by 5 ms** under `CREATE_NO_WINDOW`: 9.71 against 15.10 ms (W10),
  7.48 against 12.58 ms (W11s). Wine starts a conhost for a console child.
  - Re-run check: during 300 `drv` spawns, 234 distinct `conhost.exe` processes were seen for
    `noop-con.exe` and none besides drv's own for `noop-gui.exe`.
  - Started straight from bash, the two subsystems cost the same: 10.50 against 10.25 ms on W10.
  - `rv-gui.exe` was fully verified with redirected .NET-style pipes and when started directly.
  - Decision: build the shims with `-mwindows`. That settles the plan's open "GUI vs console" question.

## W11s instability (outside the bridge protocol)

The original run saw these failures:

- During the full suites on W11s, Wine processes running rv.exe occasionally died with
  **"Bad system call (core dumped)"** (exit 159):
  - 3 of about 360 parallel jobs in the final suite, including one in daemon mode
  - 2 `drv` spawns failed with GLE 731
  - an earlier stress run had 1 in 300
- Per-call with the glibc dynamic helper had 1–2 runs in 100–300 where the helper never connected.
- One check gave the opposite: a per-call child reported *no* seccomp at all.
- Isolated reruns were mostly clean:
  - 0 crashes in 1,500 sequential rv runs with ASLR and 1,500 without
  - 1 crash in 500 parallel daemon runs; 0 in 1,500 more
  - 0 crashes in 600 parallel `noop.exe` starts from the shell

**The re-run measured the rates and confirmed the cause for child processes.** All counts are sequential:

| Test (W11s unless noted) | ASLR on | `setarch -R` |
|---|---|---|
| `ld.so` base below 0x7001_0000_0000, native process starts (no Wine) | 20 / 20,000 (0.10%) | n/a |
| `wine drv.exe run noop-gui.exe`: PE child of a Wine process fails to start (drv exits 255: its `CreateProcessW` failed) | **4 / 5,000** | **0 / 5,000** |
| the same: the top-level `drv.exe` itself dies with SIGSYS (159) | 1 / 5,000 | 0 / 5,000 |
| `drv.exe bench 500 noop-gui.exe`: series aborted by GLE 731 | 4 of 8 series | 0 of 8 series |
| `wine drv.exe run noop-gui.exe` on **W10** | 0 / 2,000 | n/a |
| `wine noop-gui.exe` started from bash | 0 / 3,000 | n/a |
| `wine rv-gui.exe sh -c …`, daemon, from bash | 0 / 5,000 | n/a |
| the same, per-call with the dynamic helper: rc 159 / rc 125 (helper never connected) / child without a filter | 6 / 2 / 6 of 3,000 | n/a |

- **The mechanism for child processes is confirmed.** Staging's filter traps syscalls whose instruction
  pointer is below 0x7001_0000_0000 (B1). The filter is inherited by every child, Linux or PE: a PE child
  of `drv.exe` shows the parent's single filter. Ubuntu's 32-bit mmap randomisation loads `ld.so` anywhere
  from 0x6ffc… to 0x7ffb…, so about 1 start in 1,000 puts it below the threshold. Such a child dies at its
  first syscall, before Wine's SIGSYS handler exists. The evidence:
  - For a PE child, the parent's `CreateProcessW` fails with GLE 731 (`ERROR_WAIT_1`). This most likely
    means Wine saw the new process end before it finished starting.
  - For a Linux child, the process exits 159. Its apport report shows `ld-linux` mapped at 0x6ffc… to
    0x7000….
  - Disabling ASLR removes the failures, as the table shows.
  - Per-call rc 159 and rc 125 match two dynamic processes per run (`sh`, `sed`) and one helper start,
    each at about 0.1%.
- **"No filter" children** (0.2%) fit the case where the parent Wine process's own unix libraries load low.
  `ntdll.so` then skips the filter ("Native libs are being loaded in low addresses … not installing
  seccomp"). The earlier text had this backwards ("one that lands high never installs the filter").
- **Top-level Wine processes started from a shell** crash much more rarely: 1 in 5,000 here, and a few
  parallel jobs in the original run. Their mechanism is still unproven; it is probably a library loaded
  below the threshold after the filter is installed.
- **Consequence:** this is a staging runtime issue, not a bridge issue, and the daemon does not avoid it.
  - Fork on W11s starts every `git.exe`, and every bridge shim, as a PE child of its own filtered process.
  - So on the pinned runtime about 1 git invocation in 1,000 can fail at start-up, whether git is the
    bundled one or the bridge.
  - Workarounds measured here:
    - run the Wine tree under `setarch -R`, at the cost of ASLR for Wine processes: 0 failures in 5,000
    - use a non-staging Wine: W10 had 0 failures in 2,000
  - Follow-up before keeping the staging pin: a stress run of Fork's own `git.exe` spawns and of Fork's
    start-up, with and without `setarch -R`.

## Design consequences (bridge layer of the plan)

1. **Transport:** the launcher starts `fl-bridge-helper --daemon`, outside Wine and before Wine starts.
   - It publishes `FL_BRIDGE_PORT` and a 256-bit `FL_BRIDGE_TOKEN` in the environment Fork inherits. Both
     W10 and W11s import `FL_*` variables unchanged into the Windows environment.
   - `fl-shim.exe` connects to it and sends HELLO(token). The daemon forks one session per connection.
   - Result: no Linux process is ever spawned by Wine. That removes the seccomp inheritance of bridged
     processes, the fd leak (B1), the env-block dependency and the ELF-type problem in one step.
   - It does not remove the W11s PE start-up failures: the shim itself is a PE child of Fork (see
     "W11s instability").
   - The daemon dies with the launcher (`PR_SET_PDEATHSIG`; this is in the code but was not exercised by
     the spike). Each session still kills its process group when the shim's socket closes. That was
     verified at 15–21 ms, with SIGKILL after 3 s.
2. **Add mutual authentication.** In the spike only the shim proves the token. Production should
   challenge in both directions, for example HMAC over nonces, so that a process that grabs the port
   after a daemon crash cannot feed Fork fake git output.
3. **Keep the four fixes above:**
   - the pump thread never exits on its own; join it before `ExitProcess`
   - lingering close on the helper side
   - SIGCHLD self-pipe instead of timers
   - no `closesocket()` racing a `send()`
4. **Build the shims as GUI subsystem** (`-mwindows -municode`, `_dowildcard = 0`). The helper's ELF type no
   longer matters with the daemon; a native distro build (dynamic PIE) is fine. The plan's
   `-static -no-pie` requirement only applies to Wine-spawned helpers.
5. **Fork launching terminals and tools (B5).** Anything Fork-side code starts through Wine on W11s carries
   the seccomp filter and `NoNewPrivs=1`. In such a terminal, Go tools break (verified) and so does sudo
   (inferred from `NoNewPrivs=1`, which makes the kernel ignore setuid bits; not run). About 1 program in
   1,000 also dies at start (ASLR). `fl-launch` must go through the daemon too (or D-Bus activation), never
   `CreateProcessW` on a Linux binary.
6. **Exit-status contract** (verified): the child's code, 128+signal, 127 for spawn failure, 141 when Fork
   closes the reader, 125 for a bridge failure.

## Open issues

- **The W11s start-up failures affect the managed runtime choice.** The cause is now confirmed for child
  processes, and it hits any PE that Fork starts, at about 1 in 1,000 (see above). Two things remain open:
  - The rarer crash of top-level Wine processes is unexplained.
  - A vanilla (non-staging) Kron4ek 11.0 build was not tested because downloading needs approval. Upstream
    11.0 may share the `WINE_HOST_` renames and the dropped env block, but not the seccomp filter.
  - Also undecided: whether to run Wine under `setarch -R`, whether to change the pin, or whether to accept
    the rate.
- Resolved: one W11s run had a single `drv run rv-con.exe git --version` returning 255 with no output.
  255 is drv's "CreateProcessW failed" code, so this is the PE start-up failure above. The re-run
  reproduced it at 4 in 5,000.
- The spike's environment variable names (`FL_RV_*`) are prototype-only. Production names belong in the
  bridge design.

## Verification (independent re-run, same day)

Everything was rebuilt (byte-identical) and run again from fresh scratch prefixes on both builds.

| Run | Result |
|---|---|
| W10 `run.sh b2` | 262/262 |
| W11s `run.sh b2` | 188 passed, 9 failed |
| W10 and W11s `run.sh bench` | latencies as above |

The 9 W11s failures:

- the per-call preflight (the plan's helper exits 159 under the filter)
- the 8 static and Go grandchild checks of percall-high and percall-dyn, all 159

The daemon ran 64/64 on W11s.

Corrections from the re-run:

- The deterministic W11s per-call score is 62/66; the original 61/66 included a rare crash.
- Range updates:
  - kill: 15–21 ms
  - SIGKILL escalation: 3007–3035 ms
  - 100 MB: 93–107 and 146–177 ms
- Two helper descriptions were imprecise:
  - The token is unset, not wiped.
  - fd 2 also goes to `/dev/null`.
- The pump-thread join has a 500 ms cap.
- The W11s instability is now measured, and its cause confirmed for child processes. It also hits trivial
  PEs such as `noop-gui.exe` when they are started by a Wine process, so it is not specific to rv.exe or
  Winsock.
- The "no filter" explanation was reversed.
- The daemon does not avoid the PE start-up failures.
- The 255 open issue is explained.

The stress scripts used for the table are not part of `run.sh`. They are simple loops over the commands
named in the table.

## Reproduce

```sh
bridge/spike/build.sh
FL_SPIKE_ROOT=/scratch/dir bridge/spike/run.sh b2       # also: bench, b1, all
FL_SPIKE_MODES="daemon" ...                             # subset of: percall percall-high percall-dyn daemon
# -> $FL_SPIKE_ROOT/results/<wine-version>/{b2/report.txt,bench/report.txt}
```
