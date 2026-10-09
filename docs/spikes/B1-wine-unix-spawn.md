# Spike B1: Windows process starting a native Linux program under Wine

Date: 2026-10-09. Host: Ubuntu 26.04.1, kernel 7.0.0-38, i9-11900H, glibc 2.43, git 2.53.0.
Wine builds:

- **W10**: system `wine-10.0 (Ubuntu 10.0~repack-12ubuntu1)`.
- **W11s**: Kron4ek `wine-11.0-staging-amd64-wow64`. This is the managed default pinned by spike 01.

Each build ran in its own scratch `WINEPREFIX` and fake `HOME`. `~/.wine` was never used as a prefix.

The sources are in `bridge/spike/`: `probe.c`, `target.c`, `target.sh` and `drv.c`. They are built by
`bridge/spike/build.sh` in `fork-linux-ci-bridge:26.04` (MinGW-w64 GCC 13, musl 1.2.5, GCC 15.2) and run
by `bridge/spike/run.sh b1`.

## Question

Can a PE process launch a Linux program through `CreateProcessW`? If so, what does the program receive:
argv, cwd, environment, std handles, session? And what does the parent get back: process handle and exit code?

## Verdict: PASS (feasible). Three of the plan's four constraints were confirmed, and W11s adds two more.

AF_UNIX, the fourth constraint, was not tested.

A PE can start a script, a dynamic PIE or a static ET_EXEC program. On W11s a static ET_EXEC must be
linked high, and about 1 start in 1,000 of any dynamic program dies (see "Interaction with ASLR"). The
parent never gets a usable process handle or exit code back. It never gets stderr. Std handles reach the
child only in one narrow case. Data and exit status must therefore travel over a side channel, which is
spike B2. All numbers below were reproduced by an independent re-run (see "Verification").

| Plan claim (from the Wine 10 source) | W10 | W11s |
|---|---|---|
| Non-PE images are started with `fork_and_exec`. No usable process handle, no exit code. | confirmed | confirmed |
| `hStdError` is ignored. Pipes become `/dev/null`. `CREATE_NO_WINDOW` forces `/dev/null`. | confirmed, and more cases force `/dev/null` (see the matrix) | identical to W10 |
| A static-pie ELF is misclassified, so the helper must be `-static -no-pie` (ET_EXEC). | confirmed (GLE 193) | static-pie: confirmed. **The ET_EXEC helper is killed (SIGSYS) by a seccomp filter.** |
| AF_UNIX is staging-only. | not tested (TCP used) | not tested |
| **New:** the `lpEnvironment` block reaches the child. | yes, with renames | **no: the child gets the Wine process's own Unix environment** |

## Method

`probe.exe` runs every combination of the following and appends one TSV row per case:

- **Targets (5):** `#!/bin/sh` script, glibc dynamic PIE, musl `-static -no-pie` at 0x400000,
  musl `-static-pie`, and musl ET_EXEC linked at 0x7e0000000000 (added after the W11s finding).
- **Creation flags (4):** `0`, `CREATE_NO_WINDOW`, `DETACHED_PROCESS`, `CREATE_NEW_PROCESS_GROUP`.
- **Std handles (5):**
  - `none`: no `STARTF_USESTDHANDLES`
  - `invalid`: all three set to `INVALID_HANDLE_VALUE`
  - `CreatePipe`
  - a file
  - a connected Winsock TCP socket (non-overlapped)
- **lpCurrentDirectory:** explicit (a directory with a space in its name), plus one `NULL` case per target.

That gives 21 cases per target and parent context. There are four parent contexts, so each Wine build ran
**420 cases**:

| ctx | Parent |
|---|---|
| con-shell | console-subsystem probe started by `wine` from bash |
| gui-shell | GUI-subsystem probe started by `wine` from bash |
| con-nested | console probe started by `drv.exe` the way .NET `Process.Start` does: `CREATE_NO_WINDOW`, pipes, inherit (the real Fork-to-shim situation) |
| gui-nested | the same, with the GUI-subsystem probe |

Each target writes the following to a log file:

- argv; it checks argv[3..] against the expected list `arg with space`, `q"uote`, `back\slash`, `C:\win\path`, `tail\`, the empty string, and `` $HOME;`id` ``
- pid, ppid, pgid and sid
- whether it has a controlling tty
- cwd
- `PATH`, `HOME`, `FL_TOKEN` (from the explicit env block), `WINEPATH` and `WINEHOME`
- every open fd (the `ls -l /proc/self/fd` view)
- `Seccomp`, `Seccomp_filters` and `NoNewPrivs` from `/proc/self/status`
- up to 63 bytes read from stdin within 300 ms

It then writes `OUT-<case>` to fd 1 and `ERR-<case>` to fd 2, and exits with code 7. The probe then:

- waits for the log
- calls `WaitForSingleObject(pi.hProcess, 1000)` and `GetExitCodeProcess`
- looks for the markers on its own pipe, file or socket ends, and in Wine's captured stdout and stderr

## Results

### Per target (420 cases per build; flags and std handles make no difference)

| Target | W10 `CreateProcessW` | W10 runs | W11s `CreateProcessW` | W11s runs |
|---|---|---|---|---|
| `#!/bin/sh` script | TRUE | 84/84 | TRUE | 84/84 |
| glibc dynamic PIE | TRUE | 84/84 | TRUE | 84/84 |
| musl static ET_EXEC @0x400000 | TRUE | 84/84 | TRUE | **0/84 (killed by SIGSYS)** |
| musl static-pie | FALSE, GLE 193 `ERROR_BAD_EXE_FORMAT` | 0/84 | FALSE, GLE 193 | 0/84 |
| musl static ET_EXEC @0x7e0000000000 | TRUE | 84/84 | TRUE | 84/84 |

Return values for every launched case on both builds:

- `pi.hProcess` and `pi.hThread` are NULL.
- `WaitForSingleObject(pi.hProcess)` returns `WAIT_FAILED` with GLE 6 (`ERROR_INVALID_HANDLE`).
- `GetExitCodeProcess` fails, so the target's exit code 7 is never visible.
- `pi.dwProcessId` is not a pid. On W10 it is uninitialised stack data from inside Wine (e.g. 4264689664,
  sometimes 0; the probe zeroes `pi` before the call). On W11s it is always 0. Never use it.
- `CreateProcessW` is synchronous: median 1.55 ms (W10) and 1.50 ms (W11s); the re-run measured 1.54 and
  1.51 ms. Wine double-forks and waits for the intermediate child.

On W11s a launch whose child dies at once still returns TRUE: the SIGSYS death happens after `exec`, so the
parent learns nothing.

### Std-handle matrix (identical on W10 and W11s; identical across the four runnable targets)

| Parent and flags | stdio=none | invalid | pipe | file | socket | setsid |
|---|---|---|---|---|---|---|
| console parent, flags `0` | fd0/fd1 = the parent's own std handles if they map to Unix fds; for con-nested they are pipes, so `/dev/null` | `/dev/null` | **`/dev/null`** | **real file fds** (stdin data read, OUT reaches the file) | **real socket fds** (stdin data read, OUT reaches the socket) | no |
| console parent, `CREATE_NO_WINDOW` / `DETACHED_PROCESS` / `CREATE_NEW_PROCESS_GROUP` | `/dev/null` | `/dev/null` | `/dev/null` | `/dev/null` | `/dev/null` | **yes** |
| GUI parent, any flags | `/dev/null` | `/dev/null` | `/dev/null` | `/dev/null` | `/dev/null` | **yes** |

- **fd 2 is always Wine's own Unix stderr** (`hStdError` is ignored in every case). `ERR-<case>` always
  landed in the captured Wine stderr. Under Fork that means `fork-linux`'s log, not Fork's pipe.
- No case gives the child a controlling tty.
- The child's ppid is the session subreaper (here `systemd --user`, pid 6817), not Wine.
- Without setsid the child keeps the Wine process's pgid and sid.

### argv, cwd and environment

| Item | W10 | W11s |
|---|---|---|
| argv[1..] | exact round trip of all 7 tricky arguments (Windows command-line rules) | same |
| argv[0] (ELF) | the **DOS path** (`Z:\home\…\target-dyn-pie`) | same |
| argv[0] / `$0` (script) | Unix path through the prefix symlink: `$WINEPREFIX/dosdevices/z:/home/…/target-script.sh` | same |
| explicit `lpCurrentDirectory` | honoured (path with spaces) | honoured |
| `lpCurrentDirectory=NULL` | the parent's current directory | same |
| `FL_TOKEN` from the explicit env block | present | **absent** |
| `PATH`, `HOME` | the Unix values. The block's `HOME=C:\fakehome` arrives renamed as `WINEHOME`, and `PATH` as `WINEPATH`. | the Unix values. No Windows variables at all: no `ComSpec`, `windir` or `WINEHOME`. |

On W11s the child's environment equals the Unix environment that the Wine process tree started with,
including `PWD`. On the Windows side, W11s also renames host-specific variables to `WINE_HOST_<NAME>`:
`WINE_HOST_HOME`, `WINE_HOST_PATH`, `WINE_HOST_PWD`, `WINE_HOST_XDG_RUNTIME_DIR` and the other `XDG_*`
variables. Other variables such as `FL_*`, `DISPLAY` and `SSH_AUTH_SOCK` keep their names. W10 imports
`XDG_RUNTIME_DIR` under its own name.

### W11s: inherited seccomp filter (new, blocking for the plan's helper)

- Linux children of a W11s process have `Seccomp: 2`, `Seccomp_filters: 1` and **`NoNewPrivs: 1`**:
  251 of the 252 runnable B1 cases. The 252nd lost those lines because the script's own `sed` was killed by
  the filter (see ASLR below). The exception is a parent in which Wine skipped installing the filter
  (6 of 3,000 per-call runs in B2).
  The filter comes from wine-staging's syscall emulation. Its `ntdll.so` strings include "Installing seccomp
  filters" and "Native libs are being loaded in low addresses … not installing seccomp".
- Seccomp filters cannot be removed. They survive `fork` and `execve`, so the whole descendant tree keeps it.
  This includes **PE children**: a PE started by a W11s process inherits the parent's filter. It shows that
  single filter, not a second one of its own (see B2, "W11s instability").
- **Threshold, bisected** with static ET_EXEC builds at different bases (exit 7 = ran, 159 = SIGSYS).
  Rows marked ✓ were re-run during verification, 3 times each:

  | Base | Result |
  |---|---|
  | 0x400000 ✓ | SIGSYS |
  | 0x100000000 ✓ | SIGSYS |
  | 0x10000000000 | SIGSYS |
  | 0x600000000000 ✓ | SIGSYS |
  | 0x700000000000 ✓ | SIGSYS |
  | 0x700080000000 ✓ | SIGSYS |
  | 0x7000fff00000 ✓ | SIGSYS |
  | **0x700100000000** ✓ | runs |
  | 0x7008… ✓, 0x7100…, 0x7400…, 0x7800…, 0x7c00…, 0x7e00… ✓, 0x7f00…, 0x7ff0… ✓ | run |

  A syscall instruction below **0x7001_0000_0000** traps. This fits a filter that compares the high 32
  bits of the instruction pointer against 0x7000. All of these binaries exit 7 when run outside Wine.
- Dynamic programs usually survive because their syscalls come from `ld.so` and `libc.so`, which are
  usually mapped high (the ASLR item below gives the exception). Programs that issue syscalls from their own
  text die: static ET_EXEC, and **every Go binary, PIE or not**. B2 checked this with the host's `go`
  (static), `gh` (dynamic) and `docker` (dynamic PIE), which all exit 159 when run as a descendant of a W11s
  process.
- No environment variable disables the filter: none of the `WINE*` variables named in `ntdll.so` relates to
  it. A pre-installed allow-all filter does not prevent it either. Re-tested: a child then shows
  `Seccomp_filters: 2`, and `go version` still exits 159.
- **Interaction with ASLR (confirmed for child processes).** This host loads `ld.so` anywhere from 0x6ffc…
  to 0x7ffb…. That is 32-bit mmap randomisation, the Ubuntu default. In 20,000 process starts, 20 (0.10%)
  put `ld.so` below the threshold. A dynamic program started under the filter then dies with SIGSYS at
  its first syscall, which is inside `ld.so`.
  - Evidence: during these spikes apport wrote crash reports for `sed`, `dash`, GNU `true` and uutils
    `pwd`, `readlink` and `timeout`. All were SIGSYS deaths, and each had `ld-linux` mapped at 0x6ffc…
    to 0x7000…. Where the report includes registers, `rip` is inside that mapping.
  - So "dynamic programs survive" holds for about 999 starts in 1,000.
  - PE children of a W11s process fail the same way (B2).
  - A Wine process whose own libraries load low skips installing the filter, so its children have none.
  - Side effect: each such crash leaves an apport report in `/var/crash`, which whoopsie may upload.

### File-descriptor leak (both builds)

Each successful `CreateProcessW` of a Linux binary leaks one fd for the child's cwd in the parent Wine
process. It is not `FD_CLOEXEC`, so every later child inherits all of them: up to 83 extra `cwd dir` fds by
the end of a probe run. The `NULL`-cwd cases leak the parent's cwd the same way. No other Wine
descriptors were seen in the C targets.

## Design consequences

1. **Fork must always start a real PE (the shim). Data, stderr and exit status go over a side channel.**
   The rendezvous of spike B2 is required. Socket and file std handles do reach a child, but only from a
   console parent with flags `0`, never stderr, and never the exit code.
2. **Never rely on the creation result.** `CreateProcessW` returns TRUE even when the child dies at once.
   The shim must time out on the connect-back (B2 uses 10 s) and never touch `hProcess` or `dwProcessId`.
3. **Helper ELF type:**
   - static-pie is unusable (misclassified).
   - Static ET_EXEC at the default base dies on W11s.
   - ET_EXEC linked at 0x7e0000000000 (`-Wl,-Ttext-segment`, plus `--defsym=_DYNAMIC` for musl's `crt1.o`)
     and a glibc dynamic PIE both start on W11s. The dynamic PIE still fails about 1 start in 1,000 (ASLR).
     0x7e… is a safety margin, not the threshold, which is 0x7001_0000_0000. The comment in `build.sh`
     ("below ~0x7e00'0000'0000") overstates it. B2 shows both helpers still pass the seccomp filter on to
     git and its children.
4. **Do not pass secrets or settings through `lpEnvironment`.** W11s drops the block. Use a 0700 runtime-dir
   file (path in argv) or, better, no Wine-spawned helper at all. That is the daemon chosen in B2.
5. The helper must:
   - close inherited fds ≥ 3
   - reopen fds 0 and 1 to `/dev/null`
   - treat fd 2 as Wine's log
   - call `setsid`
   - derive any persona from `basename(argv[0])` after both `\` and `/`, because argv[0] is a DOS path
6. **Shims should be GUI-subsystem.** Std handles never reach a Linux child from them anyway, and B2 shows
   that console shims cost about 5 ms extra under `CREATE_NO_WINDOW` (conhost).

## Verification (independent re-run, same day)

The spike was re-run from scratch, with fresh scratch prefixes and `HOME` for both builds; `~/.wine` was
not used.

- **Build:** `build.sh` rebuilt all 15 artefacts byte-identically. `out/SHA256SUMS` was unchanged.
- **Per-target table, return values and std-handle matrix:** reproduced exactly on both builds.
  - The W10 and W11s matrices are identical to each other.
  - dwProcessId: W10 gave 4264689664 and other large values, plus 0; W11s gave only 0.
- **argv, cwd, environment, controlling tty, ppid and the fd leak:** reproduced (at most 83 extra fds;
  only cwd fds leak).
- **W11s environment renames:** checked with `wine cmd /c set`.
  - Renamed on W11s: `WINE_HOST_HOME`, `_PATH`, `_PWD` and `_XDG_*`.
  - Unchanged on W11s: `FL_*`, `DISPLAY` and `SSH_AUTH_SOCK`.
  - W10 keeps all of these names.
- **Seccomp:** the threshold was re-bisected and narrowed to 0x7000fff00000 (traps) versus 0x700100000000
  (runs). `go`, `gh` and `docker` exit 159. Filter stacking was re-tested. The plan's `rv-helper` exits 159
  under the filter, and `rv-helper-high` runs.
- **Corrections made from the re-run:**
  - The verdict claimed all four plan constraints were confirmed, but AF_UNIX was not tested.
  - The mmap range was given as "0x7006… to 0x7f70…". The measured range is 0x6ffc… to 0x7ffb…, with
    0.10% of starts below the threshold.
  - The ASLR interaction is now confirmed for child processes from crash reports.
  - "Every child has the filter" became "almost every".
  - `dwProcessId` on W10 can also be 0.

## Reproduce

```sh
bridge/spike/build.sh                                  # docker: fork-linux-ci-bridge:26.04
FL_SPIKE_ROOT=/scratch/dir bridge/spike/run.sh b1      # system wine
FL_SPIKE_ROOT=/scratch/dir FL_SPIKE_PREFIX=/scratch/pfx-k11 \
  FL_SPIKE_WINE=/path/to/wine-11.0-staging-amd64-wow64/bin/wine bridge/spike/run.sh b1
# -> $FL_SPIKE_ROOT/results/<wine-version>/b1/{summary.md,results.tsv,logs/}
```
