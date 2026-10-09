# bridge/ — the native-git bridge (experimental, opt-in)

Fork for Linux (unofficial) runs the official, unmodified Fork for Windows under Wine. By
default Fork uses its bundled Git for Windows, which runs under Wine as well. The **git
bridge** is an experimental, opt-in mode in which Fork's `git.exe` / `bash.exe` / `sh.exe`
calls are carried out by the host's native `/usr/bin/git` and `/bin/sh`, outside Wine.

This file is the **hand-off specification** for the Python integration (launcher, setup
step, `git-bridge` CLI, doctor). Component details live next to the code:

| Document | Covers |
|---|---|
| [`unix/README.md`](unix/README.md) | the daemon `fl-bridge-helper` and its personas, in depth |
| [`win/README.md`](win/README.md) | the Windows shims `fl-shim.exe` / `fl-launch.exe`, in depth |
| [`common/fl_proto.h`](common/fl_proto.h) | the frozen wire protocol (the normative definition) |
| [`common/fl_translate.h`](common/fl_translate.h) | argv / environment / output translation |
| [`../docs/spikes/B1-wine-unix-spawn.md`](../docs/spikes/B1-wine-unix-spawn.md), [`B2-rendezvous.md`](../docs/spikes/B2-rendezvous.md) | why the design looks like this (verified facts, the four B2 fixes) |
| [`../docs/spikes/B3-B4-real-fork-bridge.md`](../docs/spikes/B3-B4-real-fork-bridge.md) | the bridge with the real Fork 2.23.2: what works, what cannot |
| [`spike/`](spike/) | the historical Phase 0 prototype (`rv.c`, `rv-helper.c`, `drv.c`); not built by meson, kept for reference; its `out/` is gitignored |

## Architecture

```text
                    Linux (native)                             Wine prefix (Windows)
  ┌──────────────────────────────────────┐        ┌───────────────────────────────────────┐
  │ fork-linux launcher (Python)         │        │ Fork.exe (official, unmodified)       │
  │  1. token -> 0600 file               │ execve │   FORKGITINSTANCE=C:\fork-linux\      │
  │  2. start fl-bridge-helper --daemon ─┼──┐  ──►│                     gitInstance       │
  │  3. read "FL_BRIDGE_PORT=<n>"        │  │     │   │ CreateProcess                     │
  │  4. export FL_* + FORKGITINSTANCE,   │  │     │   ▼                                   │
  │     execve wine Fork.exe             │  │     │ fl-shim.exe as git.exe/bash.exe/sh.exe│
  └──────────────────────────────────────┘  │     │ fl-launch.exe terminal|open|diff|...  │
                                            │     └──────────────┬────────────────────────┘
  ┌──────────────────────────────────────┐  │                    │ TCP 127.0.0.1:<port>
  │ fl-bridge-helper --daemon            │◄─┘                    │ mutual HMAC-SHA256 auth,
  │  (outside Wine: no seccomp, no       │◄──────────────────────┘ framed full duplex
  │   NoNewPrivs inherited from staging) │
  │  fork() per connection ─► session ───┼──► /usr/bin/git, /bin/sh, fork-linux-host
  └──────────────────────────────────────┘          │
                                                    │ git calls a Windows program back
                                                    ▼ (editor, askpass, credential helper)
                                       fl-winexec / fl-askpass / fl-ssh-askpass
                                       (personas = symlinks to fl-bridge-helper)
                                                    │
                                                    ▼ execv $FL_WINE <WinExe> <translated args>
                                       e.g. Fork.RI.exe (interactive rebase), Fork.AskPass.exe
```

Three pieces, all built from this directory:

1. **`fl-bridge-helper --daemon`** (`unix/`, C11, glibc PIE for distro packages, musl
   `-static -no-pie` for the AppImage / tarball). Started by the launcher **before** Wine,
   **outside** Wine. Spike B2 showed that every Linux process spawned *by* a wine-staging
   process inherits staging's seccomp filter and `NoNewPrivs` (Go tools and static binaries
   die with SIGSYS, ~1 in 1,000 dynamic starts fails too), so nothing Linux-side is ever
   started through Wine's `CreateProcessW`. The daemon listens on loopback, forks one
   session process per connection and runs the requested program natively.
2. **The shims** (`win/`, MinGW-w64 C11, PE32+ x86-64, GUI subsystem `-mwindows
   -municode`, `_dowildcard = 0` so pathspecs are never globbed):
   - `fl-shim.exe`, installed as `git.exe`, `bash.exe` and `sh.exe`; the persona is the
     module's basename. It translates argv, cwd and environment (Windows → Unix), connects
     to the daemon, relays stdin / stdout / stderr, translates path output back
     (`Z:/home/u/repo`, like Git for Windows), and exits with the child's status.
     `FL_BRIDGE_MODE=record` instead forwards unchanged to Fork's bundled git (corpus
     capture; spike B3).
   - `fl-launch.exe <verb>` for host tools (`terminal | open | reveal` detached,
     `diff | merge | edit | run` waited), executed by the daemon as
     `fork-linux-host <verb> <args>`.
3. **The personas** `fl-winexec`, `fl-askpass`, `fl-ssh-askpass`: the same ELF selected by
   `basename(argv[0])`. Native git calls them when Fork configured a *Windows* program as
   editor / askpass / credential helper; they `execv` `$FL_WINE` with that program in the
   same prefix and translate Unix path arguments to Windows paths.

`bridge/common/` holds the pure, OS-independent C shared by both sides (protocol codec,
SHA-256/HMAC, POSIX sh quoting, translation tables). It is unit-tested natively under
ASan/UBSan with the vectors in `tests/vectors/*.tsv`.

## Protocol (`FLB1`, version 1)

Frame = 5-byte header (`u8 type`, `u32 BE payload length`) + payload. Limits are checked on
both ends (`STDIN`/`STDOUT`/`STDERR` ≤ 64 KiB, `REQ` ≤ 1 MiB; fixed-size frames must have
their exact size).

```text
client (shim)                                     daemon (session process)
HELLO      "FLB1" | u16 version=1 | cn[32]  ──►
                                            ◄──   CHALLENGE  sn[32] | HMAC(K, "fl-bridge-v1|daemon|" cn sn)
  (shim verifies the daemon MAC, constant time, BEFORE proving anything)
AUTH       HMAC(K, "fl-bridge-v1|client|" cn sn) ──►
                                            ◄──   AUTH_OK                    (whole handshake ≤ 5 s)
REQ        TLV: CWD, FLAGS, ARG..., ENV_SET..., ENV_UNSET..., ANCHOR...  ──►   (≤ 10 s)
                                            ◄──   SPAWN_OK u32 pid | realpath(anchor)\0 ...
                                                  or SPAWN_ERR i32 errno | message  (shim exits 127)
STDIN*  STDIN_EOF  SIGNAL(signo)            ──►
                                            ◄──   STDOUT*  STDERR*
                                            ◄──   EXIT u8 kind (0 exited / 1 signaled) | i32 code
```

- `K` = the 32 raw bytes decoded from the 64-hex-character `FL_BRIDGE_TOKEN` (256 bits).
  `cn` / `sn` are fresh 32-byte nonces (`BCryptGenRandom` / `getrandom`).
- **Mutual authentication.** The daemon proves it knows the token first; the shim sends its
  own proof only after checking it. A process that squats on the port after a daemon crash
  can therefore neither feed Fork fake git output nor learn anything usable. Distinct
  domain-separation strings make the two MACs non-reflectable.
- `FLAGS`: `FL_REQ_DETACH` (0x1, report `EXIT 0` right after exec, the program keeps
  running in its own session) and `FL_REQ_HOST_HELPER` (0x2, the daemon prepends its
  configured `--host-helper`). Unknown bits and unknown TLV tags are rejected (the protocol
  version changes instead).
- `SIGNAL(13)` means "Fork closed its reader": the daemon closes the child's stdout so it
  gets a genuine `EPIPE` / `SIGPIPE`. `SIGNAL` 1/2/9/15 are `killpg`'d; others ignored.
- If the shim's socket goes away (Fork killed `git.exe`), the daemon `killpg`s the child's
  process group with SIGTERM, then SIGKILL after 3 s.
- The four spike B2 fixes are kept in production: (1) the shim's stdin pump thread is
  always cancelled and joined before `ExitProcess` (else Wine may report exit 0); (2) the
  daemon does a lingering close (`shutdown(SHUT_WR)` + drain ≤ 2 s) so Wine never sees an
  RST before `EXIT`; (3) a SIGCHLD self-pipe wakes the daemon's `poll()`; (4) the shim never
  closes the socket while the pump may be inside `send()`.

## Exit codes

What `git.exe` / `bash.exe` / `sh.exe` / `fl-launch.exe` return to Fork:

| Code | Meaning |
|---|---|
| child's code | normal exit (0–255), e.g. `diff --no-index` → 1 |
| 128 + n | the child was killed by signal n |
| 125 | **bridge failure**: `FL_BRIDGE_*` missing/invalid, daemon unreachable, authentication failed (either direction), protocol error, connection lost, invalid UTF-16 argument/cwd, unknown persona |
| 127 | the program could not be started (`SPAWN_ERR`: not found, bad cwd, `ENOEXEC`, ...; record mode: `CreateProcessW` failed) |
| 141 | Fork closed the shim's stdout (SIGPIPE equivalent) |
| 2 | `fl-launch.exe` usage error (no / unknown verb) |

There is **no automatic fallback** to bundled git on 125: Fork shows the git error. The
launcher must therefore only export `FORKGITINSTANCE` once the daemon is confirmed running
(see below); `fork-linux git-bridge disable` returns to bundled git.

`fl-bridge-helper --daemon` itself exits `0` (SIGTERM / SIGINT / SIGHUP, parent death,
`--parent-pid` gone), `1` (start-up failure: token, host helper, log, bind, parent already
gone) or `2` (usage error). The personas exit 127 when `WINEPREFIX`, the askpass target or
the Wine loader is missing, otherwise Wine's exit status.

## Environment contract

### Set by the launcher in the environment Wine / Fork inherits

Wine copies Unix environment variables into the Windows environment; `FL_*` names arrive
unchanged on Wine 10 and wine-11 staging (spike B1). Fork passes its environment on to
`git.exe`, so the shims see them.

| Variable | Required | Consumer | Value |
|---|---|---|---|
| `FL_BRIDGE_PORT` | yes | shim, launch | the port from the daemon's `FL_BRIDGE_PORT=<n>` line (1–65535). Missing/invalid → 125 |
| `FL_BRIDGE_TOKEN` | yes | shim, launch | the same 64-hex-character token given to the daemon. Missing/invalid → 125. The shim never forwards it; the daemon never passes it to children |
| `FL_BRIDGE_WINEXEC` | yes (shim) | shim → native git | absolute Unix path of the `fl-winexec` persona symlink. The shim refuses to run (125) without it, because Fork's editors (Fork.RI.exe) must be wrapped |
| `FL_BRIDGE_ASKPASS` | recommended | shim | Unix path of the `fl-askpass` persona; a Windows `GIT_ASKPASS` becomes this persona + `FL_ASKPASS_TARGET` |
| `FL_BRIDGE_SSH_ASKPASS` | recommended | shim | Unix path of the `fl-ssh-askpass` persona. Fork always sets `SSH_ASKPASS=…\Fork.AskPass.exe` + `SSH_ASKPASS_REQUIRE=force`; **without this variable the translator unsets `SSH_ASKPASS` and ssh/HTTPS prompts are lost** |
| `FL_BRIDGE_LOG` | no | shim, launch | JSON-line call log (Windows path, or Unix path starting with `/`); secrets redacted, never the token. For `--debug` / doctor runs only: one line per git call |
| `FL_BRIDGE_MODE` | no | shim | `bridge` (default) or `record` (`git-bridge record`); anything else → 125 |
| `FL_BRIDGE_BUNDLED_GIT` | record mode | shim | Windows path of Fork's bundled `…\Fork\gitInstance\<ver>\cmd\git.exe`; the shim refuses a target that resolves to itself |
| `FL_WINE` | yes | personas (via native git) | the Wine loader the personas `execv`; **must be the same Wine build that runs Fork** (wineserver protocol) |
| `WINEPREFIX` | yes | personas | the fork-linux prefix |
| `FORKGITINSTANCE` | yes | Fork | `C:\fork-linux\gitInstance` (only when the bridge is enabled and the daemon is up) |

Also keep `WINEDLLOVERRIDES` (with `winemenubuilder.exe=d`, **not** `mscoree=`: Fork.exe is
IL-only and silently fails to start without mscoree, spike B3) and `WINEDEBUG`; the
personas add `winemenubuilder.exe=d` and default `WINEDEBUG=-all` if missing, and pass the
pinned `WINESERVER` / `WINELOADER` through.

The shim adds one variable on its own: `FL_BRIDGE_ANCHORS=unix=win;…` (cwd, `-C`,
`--git-dir`, `--work-tree`, `GIT_DIR`), which `fl-winexec` uses to map paths back to the
Windows form Fork used. No other `FL_BRIDGE_*` variable reaches the Unix side.

### The daemon's own environment

Children of the daemon get **the daemon's environment** plus the REQ set/unset operations,
with `HOME`, `PATH` and `TMPDIR` always taken from the daemon, `LANG` never removed, and
`FL_BRIDGE_TOKEN` never passed on. So start the daemon with a Unix-clean environment:
`HOME`, `PATH` (must find `git`; relative / empty entries are ignored), `TMPDIR`, `LANG` /
`LC_*`, `SSH_AUTH_SOCK`, `DISPLAY` / `WAYLAND_DISPLAY`, `XDG_*`, `DBUS_SESSION_BUS_ADDRESS`,
plus `FL_WINE`, `WINEPREFIX`, `WINESERVER`, `WINELOADER`, `WINEDEBUG`, `WINEDLLOVERRIDES`
for the personas. Do **not** put Wine's Windows-only variables into it.

> **`GIT_CONFIG_COUNT` overlay.** The launcher's bundled-git overlay (`core.filemode=false`,
> `core.autocrlf=false`, `core.symlinks=true` via `GIT_CONFIG_COUNT/KEY/VALUE`) is
> inherited by Fork and **forwarded by the shim to native git**. Native git tracks modes and
> symlinks correctly, so when the bridge is enabled the launcher should drop that overlay
> (at least `core.filemode=false`), or native git will silently stop recording exec-bit
> changes.

## Install layout

Built by meson (`bridge/meson.build`) and installed (packages) to:

```text
<prefix>/lib/fork-linux/fl-bridge-helper        daemon (0755)
<prefix>/lib/fork-linux/fl-winexec        -> fl-bridge-helper
<prefix>/lib/fork-linux/fl-askpass        -> fl-bridge-helper
<prefix>/lib/fork-linux/fl-ssh-askpass    -> fl-bridge-helper
<prefix>/lib/fork-linux/win64/fl-shim.exe       (0644, Wine needs no +x)
<prefix>/lib/fork-linux/win64/fl-launch.exe
```

`fl-bridge-helper-static` (musl, ET_EXEC) and the test drivers are built but never
installed. In a source checkout the outputs are in `build-bridge/bridge/`; there are no
persona symlinks there, so the launcher (or the tests, see `tests/bridge/wine/fl_winetier.py`)
must create `fl-winexec` / `fl-askpass` / `fl-ssh-askpass` symlinks to the helper in a
private directory.

The Python setup step copies the shims into the prefix (outside Fork's Velopack-managed
folder, so Fork updates never touch them):

```text
C:\fork-linux\gitInstance\cmd\git.exe          fl-shim.exe
C:\fork-linux\gitInstance\bin\git.exe          fl-shim.exe   (the only one Fork 2.23.2 uses)
C:\fork-linux\gitInstance\mingw64\bin\git.exe  fl-shim.exe
C:\fork-linux\gitInstance\bin\bash.exe         fl-shim.exe
C:\fork-linux\gitInstance\bin\sh.exe           fl-shim.exe
C:\fork-linux\gitInstance\usr\bin\bash.exe     fl-shim.exe
C:\fork-linux\gitInstance\usr\bin\sh.exe       fl-shim.exe
C:\fork-linux\bin\fl-launch.exe                fl-launch.exe
```

Copies, not links (the persona is the module basename; any other name exits 125). Record a
sha256 of each installed file so `git-bridge status` / doctor can detect drift, and
reinstall when the packaged shims change (`git-bridge repair`).

## Launcher: starting and stopping the daemon

Only when `[git] bridge=on`. Every failure below means "start Fork with bundled git": do not
export `FORKGITINSTANCE`, log the reason, and show it in `doctor`.

1. **Preconditions.** Native `git --version` ≥ 2.38 (Fork passes `rebase --update-refs`;
   `--pathspec-from-file` needs 2.26; spike B3); shims installed and their sha256 matching.
2. **Token.** `token = secrets.token_hex(32)` (64 hex chars). Create
   `$XDG_RUNTIME_DIR/fork-linux/` with mode `0700` (verify owner and mode if it exists), and
   write the token to a fresh file there with `os.open(path, O_WRONLY|O_CREAT|O_EXCL|O_NOFOLLOW, 0o600)`.
   The daemon refuses a file that is not a regular file owned by the user with mode `0600`,
   reads it, and **unlinks it**. Never pass the token in argv. If `XDG_RUNTIME_DIR` is unset,
   use a `0700` directory from `tempfile.mkdtemp()`.
3. **Start** from the launcher's **main thread** (the daemon's `PR_SET_PDEATHSIG` fires when
   the *thread* that forked it exits):

   ```python
   argv = [helper, "--daemon",
           "--token-file", token_file,
           "--host-helper", host_helper_path,   # fork-linux-host (absolute)
           "--parent-pid", str(os.getpid()),
           "--log", daemon_log]                 # e.g. ~/.local/state/fork-linux/logs/bridge-<ts>.log
   proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                           stderr=log_fh,       # a file: detached children inherit it
                           env=daemon_env,      # Unix-clean, WITHOUT FL_BRIDGE_TOKEN
                           close_fds=True)
   ```

   `--port N` pins a port (default `0`, ephemeral; always `127.0.0.1`). stderr must be
   something that is always drained (a log file or `/dev/null`), never a pipe the launcher
   stops reading.
4. **Read the port.** Read exactly one line from `proc.stdout` with a timeout (5 s,
   `selectors`): it must match `^FL_BRIDGE_PORT=([0-9]{1,5})\n$`. The daemon then points its
   stdout at `/dev/null`, so the next read is EOF; close the pipe. EOF / timeout / an early
   exit (`proc.poll() is not None`) → bridge unavailable (exit 1 = start-up failure, 2 =
   usage), terminate the process if still alive.
5. **Export** the variables of the table above into Fork's environment
   (`FL_BRIDGE_PORT`, `FL_BRIDGE_TOKEN`, `FL_BRIDGE_WINEXEC`, `FL_BRIDGE_ASKPASS`,
   `FL_BRIDGE_SSH_ASKPASS`, `FL_WINE`, `WINEPREFIX`, `FORKGITINSTANCE`, optionally
   `FL_BRIDGE_LOG`), then `os.execve` Wine as usual. The exec keeps the pid and the main
   thread, so `--parent-pid` and PDEATHSIG now track the Wine process that runs Fork.
6. **Stop.** Nothing to do in the normal case: when the Wine process exits, PDEATHSIG
   (SIGTERM) or `--parent-pid` (polled every 2 s) stops the daemon. If the launcher gives up
   before the exec, send `SIGTERM` and `wait()`. A stopped daemon closes its listening
   socket but **does not kill running sessions**; each lives until its shim disconnects,
   like a real `git.exe` would.
7. **Second launch while Fork runs.** Fork's single-instance pipe forwards the paths to the
   running Fork, which keeps using the *first* launch's daemon. The second launcher may skip
   starting a daemon; if it starts one, it simply dies with that short-lived Wine process.

The daemon logs one line per session to `--log` (`0600`, `O_APPEND|O_NOFOLLOW`; never the
token, arguments, environment values or data). The launcher owns rotation.

## Building and testing

```sh
scripts/build-bridge.sh            # meson in docker fork-linux-ci-bridge:26.04 -> build-bridge/
scripts/build-bridge.sh --repro    # + two clean builds must be byte-identical (SOURCE_DATE_EPOCH)
scripts/build-bridge.sh --out DIR  # + copy fl-shim.exe, fl-launch.exe, fl-bridge-helper to DIR
scripts/build-bridge.sh --no-docker  # already inside the image / a host with MinGW-w64

# host, no Wine
python3 -m pytest tests/test_bridge_native.py          # bridge/common unit tests, ASan + UBSan (gcc)
python3 -m pytest tests/bridge                          # daemon protocol + corpus (Wine tier skipped)
meson setup build-bridge-asan -Dbridge=disabled -Db_sanitize=address,undefined \
  && meson test -C build-bridge-asan --suite bridge-unit  # all unit tests incl. the daemon helpers

# Wine tier (gated; isolated scratch prefix + fake HOME; never ~/.wine)
FL_REAL_WINE=1 FL_WINE=/usr/bin/wine FL_TEST_WINE=/usr/bin/wine \
  python3 -m pytest tests/bridge --basetemp=<scratch dir>
```

`tests/bridge/test_shims_wine.py` reads `FL_WINE` / `FL_BRIDGE_WIN_DIR`;
`tests/bridge/wine/` reads `FL_TEST_WINE` (`:`-separated list) / `FL_BRIDGE_BUILD`; both
accept `FL_BRIDGE_HELPER_BIN` (e.g. the musl build). PE flags: `-std=c11 -O2 -municode
-mwindows -Wall -Wextra -Wpedantic -Werror -Wconversion -Wsign-conversion …`, static,
stripped, `--no-insert-timestamp --build-id=none`, `-ffile-prefix-map`.

## Known limitations

- **Experimental and opt-in.** Bundled git stays the default; nothing in `settings.json`
  changes.
- **Fork's Local Changes list is computed in-process** through Wine (spike B3.6): exec-bit
  files and symlinks still show as modified even though native git reports them clean.
  The bridge fixes diffs, staging, commits (modes and symlinks are preserved), hooks, and
  Unix-path remotes, not that list.
- **Interactive rebase works only when Fork starts it** (B4): Fork.RI.exe is an IPC client
  of the running Fork; never point a global `sequence.editor` at it.
- **Daemon lifetime is tied to the launch.** If Fork restarts itself (Velopack update
  "restart now") or survives the Wine process the launcher exec'd, the restarted Fork keeps
  `FORKGITINSTANCE` but the daemon is gone: every git call fails with 125 until Fork is
  started again through `fork-linux`.
- **The token is the only gate.** Every Windows process in the prefix can read Fork's
  environment, and so can any process of the same user via `/proc/<pid>/environ` of the
  Wine process; Wine is not a sandbox. The daemon binds loopback only, authenticates both
  ways, and never logs or forwards the token.
- **wine-staging start-up flake**: about 1 PE start in 1,000 fails on the pinned staging
  runtime (spike B2); it hits the shim exactly as it hits bundled `git.exe`.
- **Drive letters other than `C:` / `Z:`**: on Wine 10 `wine_get_unix_file_name` returns
  `dosdevices/<x>:` paths; output translation uses the daemon's realpaths, but
  `fl-winexec` maps such paths back through its fallbacks (`drive_c` → `C:`, else `Z:`).
- `bash.exe` / `sh.exe`: paths inside `-c` scripts are not translated (only whole
  Windows-absolute arguments). Native hooks see `FORK_REPOSITORY_PATH` in its Windows form
  (deliberately not translated; Fork's helpers need it).
- `git config --list --system` exits 128 natively when `/etc/gitconfig` is missing (Fork
  2.23.2 tolerates it).
- Untested so far: HTTPS credential prompts through `fl-ssh-askpass` → Fork.AskPass.exe,
  ssh remotes, signing, LFS, submodules, Fork custom commands.
- `bridge/unix/meson.build` is a stale standalone variant from the daemon task (installs to
  `libexecdir`); the project builds `bridge/meson.build` only.
