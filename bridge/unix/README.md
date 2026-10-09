# fl-bridge-helper (native side of the git bridge)

`fl-bridge-helper` is the Linux half of the **experimental** native-git bridge of
Fork for Linux (unofficial). Fork's `git.exe`, `bash.exe` and `fl-launch.exe` calls go to
the Windows shims (`bridge/win/`). The shims connect over loopback TCP to this daemon,
which runs the real program (`/usr/bin/git`, `fork-linux-host`, ...) natively and relays
stdin, stdout, stderr, signals and the exit status.

The launcher starts the daemon **outside Wine, before Wine starts**. Spike B2 explains why
(`docs/spikes/B2-rendezvous.md`). On the pinned wine-staging runtime, every Linux process
spawned by a Wine process inherits staging's seccomp filter and `NoNewPrivs`. Go tools and
static binaries then die with SIGSYS, and about 1 start in 1,000 of any dynamic program
fails the same way. Processes run by this daemon inherit nothing from Wine.

The same binary also provides three **personas**, selected by `basename(argv[0])`.
Git uses them to call back into Windows programs that Fork configured:

| Persona | Runs |
|---|---|
| `fl-winexec <WinExe> [args...]` | `wine <WinExe> <args>`, with absolute and existing relative Unix paths translated to Windows form (for example `GIT_SEQUENCE_EDITOR` → `Fork.RI.exe`) |
| `fl-askpass <prompt>` | `wine $FL_ASKPASS_TARGET <prompt>` |
| `fl-ssh-askpass <prompt>` | `wine $FL_SSH_ASKPASS_TARGET <prompt>` |

Sources:

- `fl_bridge_helper.c`: the daemon, sessions and personas.
- `fl_helper_util.{c,h}`: pure helpers, unit-tested natively.

The wire format is the frozen contract in `bridge/common/fl_proto.h`. Hashing comes from
`bridge/common/fl_sha256.h`.

## Command line

```text
fl-bridge-helper --daemon [--token-file FILE] [--port N] [--host-helper PATH]
                 [--parent-pid PID] [--watch-prefix DIR [--watch-exe-dir WINDIR]]
                 [--log FILE]
fl-bridge-helper --version | --help
```

Every option also accepts the `--opt=value` form.

| Option | Meaning |
|---|---|
| `--daemon` | Required. Run the daemon. |
| `--token-file FILE` | Read the token, then unlink the file. The file must be a regular file owned by the current user with no group or other access (`0600`). It must hold exactly 64 hex characters, optionally followed by `\n` or `\r\n`. Without this option the token comes from `FL_BRIDGE_TOKEN`. |
| `--port N` | Listen on `127.0.0.1:N`. The default is `0`, an ephemeral port. Only loopback is ever bound. |
| `--host-helper PATH` | Absolute path of `fork-linux-host`. Requests with `FL_REQ_HOST_HELPER` run `[PATH, verb, args...]`. Without this option such requests fail with `SPAWN_ERR(ENOENT)`. |
| `--parent-pid PID` | Exit once `PID` no longer exists. It is checked with `kill(pid, 0)` every 2 s. With `--watch-prefix`, only until the first watched process was seen. |
| `--watch-prefix DIR` | Outlive the parent (no PDEATHSIG) while a process of this user carries both `WINEPREFIX=DIR` and `FL_BRIDGE_PORT=<this daemon's port>` in its environment (`/proc/<pid>/environ`, every 2 s); exit 6 s after the last one is gone (`reason=prefix-idle`). Fork restarted by its own updater inherits both (spike S9). |
| `--watch-exe-dir WINDIR` | With `--watch-prefix`: only processes whose `argv[0]` starts with `WINDIR` (ASCII case-insensitive; Wine shows the Windows path, e.g. `C:\users\<u>\AppData\Local\Fork\`) count, so Wine's own services that inherited the environment do not keep the daemon alive. |
| `--log FILE` | Append one line per session (mode `0600`, `O_APPEND`, `O_NOFOLLOW`). The log never contains the token, arguments, environment values or stream data. |

`FL_BRIDGE_TOKEN` is always scrubbed: the value is overwritten in place, which also blanks
`/proc/<pid>/environ`, and then unset. This happens even when `--token-file` is used. The
HMAC key is the 32 raw bytes decoded from the hex token.

Daemon exit codes:

| Code | When |
|---|---|
| `0` | Stopped by SIGTERM, SIGINT or SIGHUP, by the parent's death, because `--parent-pid` vanished, or because the `--watch-prefix` users are gone |
| `1` | Start-up failure: token, host helper, log, bind, or the parent already gone |
| `2` | Usage error |

## Lifecycle

### Start-up, in this order

1. Make sure fds 0–2 exist and close every inherited fd ≥ 3.
2. Read the token from the token file (then unlink it) or from the environment, and scrub
   `FL_BRIDGE_TOKEN`.
3. Resolve the host helper (`realpath` + `X_OK`) and open the log.
4. `chdir("/")`.
5. Bind `127.0.0.1:port` and `listen(128)`.
6. Install the signal handlers through a self-pipe and call `setsid()`, so a terminal
   Ctrl+C does not reach the daemon.
7. Call `prctl(PR_SET_PDEATHSIG, SIGTERM)` (not with `--watch-prefix`). If the parent
   already changed, exit 1.
8. Print **`FL_BRIDGE_PORT=<n>\n`** on stdout, then point stdin and stdout at `/dev/null`.
   The launcher reads one line and then gets EOF. stderr stays the launcher's and only
   carries start-up errors, but detached children (`FL_REQ_DETACH`) inherit it too: the
   launcher must give the daemon a stderr that is always drained (a log file, its own
   terminal or `/dev/null`), never a pipe it stops reading, or a chatty detached program
   blocks on a full pipe.

### Shutdown

SIGTERM, SIGINT and SIGHUP stop the daemon. So does the death of the process that started
it (PDEATHSIG). PDEATHSIG fires when the *thread* that forked the daemon exits, so spawn it
from the launcher's main thread. An `execve` of the launcher into Wine keeps the same thread
and pid, so it is fine. `--parent-pid` disappearing also stops the daemon. With
`--watch-prefix` there is no PDEATHSIG: the daemon stops 6 s after the last watched process
(see the option) is gone, and `--parent-pid` only counts until one was seen. The listening
socket closes. Running sessions are **not** killed: each one lives until its shim's socket
closes, as a Windows `git.exe` would.

### Per connection

The daemon forks a session process (at most 256 at a time; more are accepted and closed).
The session calls `setsid()` and sets `TCP_NODELAY`, then runs these steps:

1. **Mutual authentication**, all within 5 s of the accept. Any failure closes the
   connection and logs `auth-failed:<why>`.

   ```text
   client -> HELLO      "FLB1" | u16 BE version 1 | client_nonce[32]
   daemon -> CHALLENGE  server_nonce[32] | HMAC(key, "fl-bridge-v1|daemon|" | cn | sn)
   client -> AUTH       HMAC(key, "fl-bridge-v1|client|" | cn | sn)   (constant-time compare)
   daemon -> AUTH_OK
   ```

   The daemon proves that it knows the token before the client sends anything secret or
   runs anything. A process that grabs the port after a daemon crash cannot feed Fork fake
   output.

2. **REQ**, within 10 s, decoded with `fl_req_decode`. A malformed request closes the
   connection (the shim reports 125).

3. **Spawn.**
   - **Environment:** the daemon's own environment, with the token already removed. The
     REQ `ENV_UNSET` operations are applied first, then the `ENV_SET` operations; the last
     set wins. `HOME`, `PATH` and `TMPDIR` always keep the daemon's values. `LANG` is never
     removed, though a set may replace it. `FL_BRIDGE_TOKEN` is never passed on. Ignored
     operations are counted in the session's log line (`env-ignored=N`; a Windows `PATH` unset is normal).
   - **Program lookup:** `argv[0]` containing `/` is exec'd directly. Otherwise the program
     is searched for in the daemon's `PATH`, skipping empty and relative entries, so a
     repository cannot plant a `git` in its working tree. A script without a shebang gives
     `ENOEXEC`, because nothing ever runs through a shell. `FL_REQ_HOST_HELPER` prepends
     the configured host helper.
   - **Attached (default):** `fork()`. The child calls `setpgid(0,0)` and
     `PR_SET_PDEATHSIG(SIGKILL)`, gets three `O_CLOEXEC` pipes as fds 0–2, has its signal
     dispositions and mask reset, then `chdir(cwd)` and `execve`. A fourth `O_CLOEXEC`
     pipe reports a chdir or exec errno.
   - **`FL_REQ_DETACH`:** a double fork. The intermediate process calls `setsid()` and
     exits. The grandchild gets `/dev/null` on stdin and stdout, keeps the daemon's stderr
     (the launcher's log), and has no PDEATHSIG. The session sends `SPAWN_OK` and then
     `EXIT(0, 0)` right after a successful exec. The program keeps running.

4. **Spawn reply.** On failure the daemon sends `SPAWN_ERR`: i32 BE errno plus a UTF-8
   message such as `cannot run 'git': No such file or directory`; the shim exits 127. On
   success it sends `SPAWN_OK`: u32 BE pid, then one NUL-terminated record per REQ anchor,
   in order. Each record is the anchor's `realpath()` (relative anchors resolve against
   the cwd), or an empty string when it cannot be resolved.

5. **Relay**, a single `poll()` loop:
   - **stdin:** STDIN frames go into a 1 MiB buffer. The buffer is written to the child
     through a non-blocking pipe with backpressure: frames that do not fit stay in the
     socket. `STDIN_EOF` closes the child's stdin once the buffer is drained. If the child
     closes its stdin (`EPIPE`), the rest is dropped. As with any pipe, the client must
     keep reading frames while it sends `STDIN` (the shim pumps stdin from its own thread).
   - **Output:** child stdout and stderr go out as `STDOUT` / `STDERR` frames of at most
     64 KiB each.
   - **Signals:** `SIGNAL(signo)` runs `killpg` for SIGINT, SIGTERM, SIGKILL and SIGHUP.
     `SIGNAL(13)` (SIGPIPE: Fork closed its reader) closes the daemon's end of the stdout
     pipe instead, so the child gets a genuine EPIPE or SIGPIPE on its next write. Every
     other signal is ignored.
   - **Child exit:** a SIGCHLD self-pipe wakes `poll()` (spike B2 fix 3). The child is
     reaped, its pipes are drained for at most 2 s (a grandchild may hold them open), and
     then the daemon sends `EXIT`: u8 kind (0 exited, 1 signaled) and i32 BE code (exit
     code or signal number). The shim returns `code`, or `128 + code` for a signal.
   - **Shim gone:** socket EOF, `POLLRDHUP` or an error. The daemon sends `killpg(SIGTERM)`,
     waits until the process group is empty, and sends `killpg(SIGKILL)` after 3 s. The
     shim must therefore never half-close its socket: it signals end of input with
     `STDIN_EOF`.
   - A protocol violation (an unknown or out-of-place frame) ends the session the same way.

6. **Lingering close** (spike B2 fix 2). After the last frame the daemon calls
   `shutdown(SHUT_WR)` and drains the socket until EOF, for at most 2 s. Closing with
   unread data would send a TCP RST, and Wine would then fail the shim's pending `recv`
   before it reads `EXIT`.

### Log lines

```text
2026-10-09T11:00:00.123Z fl-bridge-helper[4242] daemon start port=40123 pid=4242 parent=4200 host-helper=yes watch-prefix=yes
2026-10-09T11:00:01.456Z fl-bridge-helper[4250] session peer=127.0.0.1:51234 argv0=git argc=4 flags=0x0 env-ignored=1 result=exit:0 ms=14
```

The possible `result=` values are:

- `exit:<code>`
- `signal:<signo>`
- `detached:<pid>`
- `spawn-error:<errno>`
- `auth-failed:<why>`
- `bad-request`
- `peer-closed`
- `protocol-error`

The `argv0=` field is the basename of the requested program, cut at 63 bytes, with
every control character, space and DEL replaced by `?` so a client cannot forge lines
or fields.

The log grows by one line per git call. The launcher owns its location and rotation, for
example one file per launch under `~/.local/state/fork-linux/logs/`.

## Launcher contract

These are notes for the Python launcher.

1. Generate 32 random bytes (`secrets.token_hex(32)`). Write the hex to a new `0600` file
   in a `0700` directory (for example `$XDG_RUNTIME_DIR/fork-linux/`), opened with
   `O_CREAT|O_EXCL`.
2. Start the daemon:

   ```text
   fl-bridge-helper --daemon --token-file <file> --host-helper <libexec>/fork-linux-host \
     --parent-pid <launcher pid> --watch-prefix <WINEPREFIX> \
     --watch-exe-dir 'C:\users\<u>\AppData\Local\Fork\' --log <logfile>
   ```

   Pass `stdin=DEVNULL` and `stdout=PIPE`, and start it from the main thread. Read one
   line, `FL_BRIDGE_PORT=<n>`. A non-zero exit before that line means the bridge is
   unavailable: fall back to bundled git.
3. Export the following into the environment that Wine and Fork inherit. Both W10 and
   W11s keep `FL_*` names unchanged in the Windows environment.

   | Variable | Value |
   |---|---|
   | `FL_BRIDGE_PORT` | the port |
   | `FL_BRIDGE_TOKEN` | the hex token |
   | `FL_BRIDGE_WINEXEC` | the Unix path of the `fl-winexec` symlink |
   | `FL_BRIDGE_ASKPASS` | the Unix path of the `fl-askpass` symlink |
   | `FL_BRIDGE_SSH_ASKPASS` | the Unix path of the `fl-ssh-askpass` symlink |
   | `FL_WINE` | the Wine loader that the personas exec; it must be the same Wine build that runs Fork (the wineserver protocol must match), so also keep the pinned `WINESERVER` / `WINELOADER` in the daemon's environment |
   | `WINEPREFIX` | the prefix |
   | `FORKGITINSTANCE` | `C:\fork-linux\gitInstance` |

   Also set `WINEDLLOVERRIDES` (with `winemenubuilder.exe=d`) and `WINEDEBUG`. The
   personas add `winemenubuilder.exe=d` and default to `WINEDEBUG=-all` if either is
   missing.
4. Keep the daemon's own environment Unix-clean (`HOME`, `PATH`, `TMPDIR`, `LANG`,
   `SSH_AUTH_SOCK`, `DISPLAY`, ...). Children get exactly that environment plus the REQ
   operations. The personas need `FL_WINE` and `WINEPREFIX`, which arrive either from the
   daemon's environment or as REQ sets.

## Personas

`fl-winexec <WinExe> [args...]` runs
`execv($FL_WINE, ["wine", WinExe, args...])`, or `execvp("wine", ...)` when `FL_WINE` is
unset. It keeps `WINEPREFIX`, makes sure `WINEDLLOVERRIDES` disables `winemenubuilder`, and
sets `WINEDEBUG=-all` when unset. Arguments are translated as follows:

- An absolute Unix path (except a single component that does not exist, such as the
  Windows switches `/w` or `/n:3`), or a relative one naming an existing file (git hands editors paths
  like `.git/COMMIT_EDITMSG`), is translated: `realpath()` first, then the longest match
  among:
  - the entries of `FL_BRIDGE_ANCHORS="unix=win;unix=win;..."`. An entry splits at the
    first `=` that starts a Windows path such as `X:` or `\\`.
  - `$WINEPREFIX/drive_c`, which counts as an anchor for `C:`. On a tie the explicit
    anchor wins.

  Paths that match neither become `Z:` + the path. The result uses backslashes, for
  example `C:\Repo\.git\COMMIT_EDITMSG`.
- Options (`-x`), Windows paths and plain text pass through unchanged.

`fl-askpass` and `fl-ssh-askpass` run `wine <target> <prompt>`. The target comes from
`FL_ASKPASS_TARGET` or `FL_SSH_ASKPASS_TARGET`, and the prompt is never translated.

All three personas exit 127 with a message on stderr when `WINEPREFIX`, the askpass
target or the Wine loader is missing. With `wine` exec'd in place, Wine's exit status is
the persona's exit status.

## Build

| How | Output |
|---|---|
| `bridge/meson.build` (top-level Meson build; `scripts/build-bridge.sh`) | `fl-bridge-helper`, a dynamic PIE installed into `<prefix>/lib/fork-linux` with the persona symlinks; `fl-bridge-helper-static` (musl `-static -no-pie`, ET_EXEC, not installed) when `musl-gcc` exists; unit tests in suite `bridge-unit`. (`bridge/unix/meson.build` is an older standalone variant that the project does not use.) |
| `bridge/unix/build-dev.sh [--test]` (Docker `fork-linux-ci-bridge:26.04`) | `build-bridge/unix/{fl-bridge-helper,fl-bridge-helper-static,fl-winexec,fl-askpass,fl-ssh-askpass}`; checks the ELF types; `--test` runs the unit tests under ASan/UBSan |

The flags are `-std=c11 -Wall -Wextra -Werror -Wconversion -Wsign-conversion -Wshadow
-Wstrict-prototypes -Wmissing-prototypes -Wformat=2 -Wvla -Wundef -Wpointer-arith`, plus
`-DFL_VERSION` from `VERSION`. The helper needs only
`bridge/common/fl_proto.c` and `fl_sha256.c`. The daemon is never spawned by Wine, so the
spike's `-static -no-pie` requirement no longer applies. The distro build can be a normal
PIE, and the musl build exists only for portability (AppImage, tarball).

## Tests

- `bridge/tests/unit/test_helper_util.c` and `test_helper_paths.c` cover the pure helpers.
  They run under `meson test --suite bridge-unit`, `build-dev.sh --test`, and pytest.
- `tests/bridge/test_daemon_protocol.py` (pytest, stdlib-only client) builds the daemon
  with the host `gcc` under ASan/UBSan, or uses `FL_BRIDGE_HELPER_BIN`. It covers:
  - mutual authentication in both directions
  - `git --version`, exit 42, 128 + signal, 127 for a missing program or cwd
  - 10 MB stdin, 100 MB stdout, stderr separation, backpressure
  - kill on disconnect (and SIGKILL after 3 s for a SIGTERM-ignoring child)
  - SIGNAL frames, detached mode, the host-helper flag
  - environment rules, anchors
  - 20 parallel sessions
  - `--parent-pid` and PDEATHSIG shutdown
  - token scrubbing, the log format
  - the personas, with a fake `wine`

## Security notes

- The token is the only gate. Anything that can read Fork's environment can run commands
  as the user, and that includes every Windows process in the prefix (Wine is not a
  sandbox). The daemon binds loopback only and authenticates in both directions. It never
  logs the token, arguments or data, and it never passes the token to children.
- Nothing is exec'd through a shell or built from strings. PATH lookup ignores relative
  entries, and children only inherit fds 0–2.
