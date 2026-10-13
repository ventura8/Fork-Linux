# fl-bridge-helper (native side of the git bridge)

`fl-bridge-helper` is the Linux half of the **experimental** native-git bridge of
Fork for Linux (unofficial). Fork's `git.exe`, `bash.exe` and `fl-launch.exe` calls go to
the Windows shims (`bridge/win/`). The shims connect over loopback TCP to this daemon,
which runs the real program (`/usr/bin/git`, `fork-linux-host`, ...) natively and relays
stdin, stdout, stderr, signals and the exit status.

The same daemon also carries the **desktop hand-off**: when Wine-started scripts
(`libexec/fork-linux-handoff`) want a terminal, a file manager or a default application and
there is no systemd user manager to start it, `fork-linux-host --handoff` asks this daemon to
run `fork-linux-host VERB ARG` (`FL_REQ_HANDOFF`, below). With the git bridge off the
launcher starts it as a hand-off starter only (`--handoff-only`).

It also carries the **ssh-agent relay** (`--ssh-agent`, below): Fork's bundled Git for
Windows runs msys OpenSSH, which cannot open the Linux agent's Unix socket; the daemon gives
it an msys socket file to connect to and forwards its agent requests to `$SSH_AUTH_SOCK`.

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
- `fl_agent_relay.{c,h}`: the ssh-agent relay (msys socket file, peer check, request
  filter, one connection), unit-tested natively (`bridge/tests/unit/test_agent_relay.c`).

The wire format is the frozen contract in `bridge/common/fl_proto.h`. Hashing comes from
`bridge/common/fl_sha256.h`.

## Command line

```text
fl-bridge-helper --daemon [--token-file FILE] [--port N] [--host-helper PATH]
                 [--handoff-only] [--parent-pid PID]
                 [--watch-prefix DIR [--watch-exe-dir WINDIR]] [--log FILE]
                 [--ssh-agent SOCK --ssh-agent-file FILE]
fl-bridge-helper --version | --help
```

Every option also accepts the `--opt=value` form.

| Option | Meaning |
|---|---|
| `--daemon` | Required. Run the daemon. |
| `--token-file FILE` | Read the token, then unlink the file. The file must be a regular file owned by the current user with no group or other access (`0600`). It must hold exactly 64 hex characters, optionally followed by `\n` or `\r\n`. Without this option the token comes from `FL_BRIDGE_TOKEN`. |
| `--port N` | Listen on `127.0.0.1:N`. The default is `0`, an ephemeral port. Only loopback is ever bound. |
| `--host-helper PATH` | Absolute path of `fork-linux-host`. Requests with `FL_REQ_HOST_HELPER` run `[PATH, verb, args...]`, `FL_REQ_HANDOFF` requests `[PATH, VERB, ARG]`. Without this option such requests fail with `SPAWN_ERR(ENOENT)`. |
| `--handoff-only` | Accept only `FL_REQ_HANDOFF` requests; every other request (a program, `FL_REQ_HOST_HELPER`) gets `SPAWN_ERR(EPERM)` and runs nothing. Needs `--host-helper` (usage error otherwise). The launcher's hand-off starter when the git bridge is off. |
| `--parent-pid PID` | Exit once `PID` no longer exists. It is checked with `kill(pid, 0)` every 2 s. With `--watch-prefix`, only until the first watched process was seen. |
| `--watch-prefix DIR` | Outlive the parent (no PDEATHSIG) while a process of this user carries both `WINEPREFIX=DIR` and `FL_BRIDGE_PORT=<this daemon's port>` in its environment (`/proc/<pid>/environ`, every 2 s); exit 6 s after the last one is gone (`reason=prefix-idle`). Fork restarted by its own updater inherits both (spike S9). |
| `--watch-exe-dir WINDIR` | With `--watch-prefix`: only processes whose `argv[0]` starts with `WINDIR` (ASCII case-insensitive; Wine shows the Windows path, e.g. `C:\users\<u>\AppData\Local\Fork\`) count, so Wine's own services that inherited the environment do not keep the daemon alive. |
| `--log FILE` | Append one line per session (mode `0600`, `O_APPEND`, `O_NOFOLLOW`). The log never contains the token, arguments, environment values or stream data. |
| `--ssh-agent SOCK` | With `--ssh-agent-file`: run the ssh-agent relay for the agent socket `SOCK` (absolute; the session's `$SSH_AUTH_SOCK`). Only one of the two is a usage error. |
| `--ssh-agent-file FILE` | The msys socket file the relay writes (absolute; the launcher passes `<prefix>/drive_c/fork-linux/ssh-agent/agent.sock`, which Fork's `SSH_AUTH_SOCK` names as `C:/fork-linux/ssh-agent/agent.sock`). Its directory must be a directory of this user without group or other access. When the relay cannot start (no socket at `SOCK`, the directory, no extended attributes, ...) the daemon runs without it, writes no file and logs `ssh-agent relay off: <why>`. |

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

### The ssh-agent relay (`--ssh-agent`)

msys (Git for Windows' Cygwin fork) emulates AF_UNIX sockets: a socket is a file with the
Windows SYSTEM attribute whose content is `!<socket >PORT s W0-W1-W2-W3\0` (four 8-digit
hex words), and `connect()` opens TCP `127.0.0.1:PORT`, then runs a handshake: the client
sends the 16 secret bytes (the words as little-endian u32s), the server sends them back,
then each sends 12 bytes of credentials (pid, uid, gid, i32 LE). After that the stream is
the agent protocol. Without the SYSTEM attribute msys does not see a socket ("Bad file
descriptor"); Wine keeps that attribute in the `user.DOSATTRIB` extended attribute (`0x4`).

At start-up, right after binding its own port (step 5), the daemon:

1. checks `SOCK` (absolute, fits `sun_path`, `stat` says socket) and that `FILE`'s directory
   is a real directory owned by this user with no group or other access;
2. listens on a second ephemeral `127.0.0.1` port (non-blocking, in the same `poll()` set);
3. draws a 128-bit secret (`getrandom`) and writes `FILE.tmp` (`O_CREAT|O_EXCL|O_NOFOLLOW`,
   `0600`, then `fsetxattr user.DOSATTRIB=0x4`), renamed onto `FILE`.

Any failure only leaves the relay off (logged; no file). Per connection the daemon forks a
relay process (counted with the sessions, at most 256), which:

1. looks up the peer's end of the connection in `/proc/net/tcp` and refuses a socket that
   does not belong to this user (`auth-failed:peer-uid`);
2. reads the 16-byte secret within 5 s and compares it in constant time
   (`auth-failed:no-secret`, `auth-failed:secret`), sends it back, reads the client's
   credentials and sends its own (`auth-failed:handshake`);
3. connects to `SOCK` (`agent-unreachable`) and relays one request at a time (u32 BE length,
   1 byte to 256 KiB, else `bad-message`): `SSH_AGENTC_REQUEST_IDENTITIES` (11),
   `SSH_AGENTC_SIGN_REQUEST` (13) and `SSH_AGENTC_EXTENSION` (27) named
   `session-bind@openssh.com` go to the agent and its answer comes back; every other request
   is answered `SSH_AGENT_FAILURE` (5) by the relay and never reaches the agent;
4. ends when the client closes (`done`), or the agent (`agent-gone`) or the client
   (`client-gone`) fails, and logs one line.

On exit the daemon closes the relay's port and removes `FILE` while it still holds its
secret (a file someone else put there is left alone). Running relay processes end with their
connection, like sessions.

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
   - **`FL_REQ_HANDOFF`** (a desktop hand-off): `fl_req_decode` accepts it only alone (no
     other flag), with argv exactly `{ VERB, ARG }`, VERB one of `terminal`, `reveal`,
     `open`, `open-url`, ARG non-empty, at most 8 KiB and not starting with `-`, and no
     `ENV_SET`, `ENV_UNSET` or `ANCHOR` (`fl_handoff_ok`; the session checks it again).
     The daemon runs `[--host-helper, VERB, ARG]` detached, as `FL_REQ_HOST_HELPER |
     FL_REQ_DETACH`, in its own environment and the REQ's cwd. Nothing of the client's
     process is passed on: the program inherits neither Wine's seccomp filter nor its
     `NoNewPrivs`. A daemon with `--handoff-only` refuses everything else with
     `SPAWN_ERR(EPERM)` before spawning.

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
2026-10-09T11:00:00.123Z fl-bridge-helper[4242] daemon start port=40123 pid=4242 parent=4200 host-helper=yes handoff-only=no watch-prefix=yes
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

The ssh-agent relay adds `ssh-agent relay port=<n>` or `ssh-agent relay off: <why>` at
start-up, `ssh-agent=on|off` on the start line, and one line per relay connection (no key
material, no request contents):

```text
2026-10-12T11:00:02.789Z fl-bridge-helper[4260] agent peer=127.0.0.1:51240 result=done requests=2 refused=0 ms=310
```

with `result=` one of `done`, `auth-failed:peer-uid|no-secret|secret|handshake`,
`agent-unreachable`, `out-of-memory`, `bad-message`, `agent-gone`, `client-gone`.

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
`-DFL_VERSION` from `VERSION`. Besides its own sources (`fl_bridge_helper.c`,
`fl_helper_util.c`, `fl_agent_relay.c`) the helper needs only `bridge/common/fl_proto.c`
and `fl_sha256.c`. The daemon is never spawned by Wine, so the
spike's `-static -no-pie` requirement no longer applies. The distro build can be a normal
PIE, and the musl build exists only for portability (AppImage, tarball).

## Tests

- `bridge/tests/unit/test_helper_util.c` and `test_helper_paths.c` cover the pure helpers.
  They run under `meson test --suite bridge-unit`, `build-dev.sh --test`, and pytest.
- `bridge/tests/unit/test_agent_relay.c` covers the ssh-agent relay (socket file, peer
  check, filter, whole connections against a fake agent, injected system-call and
  allocation failures); `meson test --suite bridge-unit`.
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
  - the ssh-agent relay: an msys-style client lists keys through it (a fake agent, and
    OpenSSH's own `ssh-agent` when installed), key management is refused, a wrong secret
    is closed, no agent leaves it off, the file is removed on exit

## Security notes

- The token is the only gate. Anything that can read Fork's environment can run commands
  as the user, and that includes every Windows process in the prefix (Wine is not a
  sandbox). The daemon binds loopback only and authenticates in both directions. It never
  logs the token, arguments or data, and it never passes the token to children.
- The ssh-agent relay's gate is the secret in its socket file (0600 in a 0700 directory
  inside the 0700 prefix) plus the peer's owner in `/proc/net/tcp`. Every Windows process in
  the prefix can read that file, so while Fork runs any of them can list the agent's keys
  and ask it to sign; nothing else (no adding, removing or locking keys). The agent's own
  confirmation and constraints still apply. `docs/SECURITY.md` has the threat model.
- Nothing is exec'd through a shell or built from strings. PATH lookup ignores relative
  entries, and children only inherit fds 0–2.
