# bridge/win — Windows side of the native-git bridge (experimental)

Fork for Linux (unofficial) runs the official Fork for Windows under Wine. The **git bridge**
is an opt-in, experimental way to let Fork use the host's native `/usr/bin/git` instead of
its bundled Git for Windows. This directory holds the two small Windows programs (PE32+
x86-64, GUI subsystem, MinGW-w64) that live inside the Wine prefix:

| File | Purpose |
|---|---|
| `fl_win.h`, `fl_win.c` | Shared Win32 helpers: UTF-16 ↔ UTF-8, `wine_get_unix_file_name` / `wine_get_dos_file_name` (via `GetProcAddress(kernel32)`), Winsock connect and framed I/O, `BCryptGenRandom` nonces, the mutual HMAC handshake, the stdin pump thread, the relay loop, exit handling, the JSON-line call log. |
| `fl_shim.c` | `fl-shim.exe`, installed under the names `git.exe`, `bash.exe` and `sh.exe`. |
| `fl_launch.c` | `fl-launch.exe <verb> [args]`: opens host tools (terminal, file manager, diff/merge tools, editor) through the daemon. |
| `res/app.manifest` | asInvoker, UTF-8 active code page, long-path aware, Windows 7–11 `supportedOS`. |
| `res/fl_shim.rc`, `res/fl_launch.rc` | Manifest + `VERSIONINFO` (ProductName "Fork for Linux (unofficial) bridge", CompanyName "Fork-Linux project (unofficial)", LegalCopyright "MIT"). The version comes from a generated `fl_version.h` (see Build). |
| `build-dev.sh` | Developer build into `build-bridge/dev/` inside `fork-linux-ci-bridge:26.04`. |
| `tests/fl_testdrv.c` | **Test only, never installed**: `fl-testdrv.exe` spawns a program the way Fork's .NET `Process.Start` does, for the Wine tier (`tests/bridge/test_shims_wine.py`). |

Both programs code against the frozen C contract in [`bridge/common/`](../common/)
(`fl_proto.h`, `fl_sha256.h`, `fl_translate.h`, `fl_shquote.h`). The Linux side is the
launcher-started daemon in [`bridge/unix/`](../unix/). Why the design looks like this is
recorded in [spike B1](../../docs/spikes/B1-wine-unix-spawn.md) and
[spike B2](../../docs/spikes/B2-rendezvous.md).

## Install layout (done by the Python runtime, not by this directory)

```
C:\fork-linux\gitInstance\cmd\git.exe          fl-shim.exe (persona git)
C:\fork-linux\gitInstance\bin\git.exe          fl-shim.exe (persona git)
C:\fork-linux\gitInstance\mingw64\bin\git.exe  fl-shim.exe (persona git)
C:\fork-linux\gitInstance\bin\bash.exe         fl-shim.exe (persona bash)
C:\fork-linux\gitInstance\bin\sh.exe           fl-shim.exe (persona sh)
C:\fork-linux\gitInstance\usr\bin\bash.exe     fl-shim.exe (persona bash)
C:\fork-linux\gitInstance\usr\bin\sh.exe       fl-shim.exe (persona sh)
C:\fork-linux\bin\fl-launch.exe
```

When the bridge is enabled the launcher exports `FORKGITINSTANCE=C:\fork-linux\gitInstance`.
The persona is the basename of the running module (`GetModuleFileNameW`, case-insensitive);
any other name exits 125.

## Environment (inputs)

The launcher starts `fl-bridge-helper --daemon` outside Wine and passes these through the
environment Fork inherits (Wine imports `FL_*` variables unchanged into the Windows
environment; spike B1):

| Variable | Used by | Meaning |
|---|---|---|
| `FL_BRIDGE_PORT` | shim, launch | Daemon port on 127.0.0.1 (1–65535). Missing/invalid → 125. |
| `FL_BRIDGE_TOKEN` | shim, launch | 64 hex characters = the 256-bit HMAC key. Missing/invalid → 125. Never forwarded. |
| `FL_BRIDGE_WINEXEC` | shim (required), launch (optional) | Absolute Unix path of the `fl-winexec` persona; Windows editors / credential helpers are wrapped with it. |
| `FL_BRIDGE_ASKPASS`, `FL_BRIDGE_SSH_ASKPASS` | shim, launch | Unix paths of the `fl-askpass` / `fl-ssh-askpass` personas (optional). |
| `FL_BRIDGE_MODE` | shim | `bridge` (default) or `record`; anything else → 125. |
| `FL_BRIDGE_BUNDLED_GIT` | shim (record) | Windows path of Fork's bundled `gitInstance\<ver>\cmd\git.exe`. |
| `FL_BRIDGE_LOG` | shim, launch | Optional JSON-line call log (Windows path, or a Unix path starting with `/`). |

No `FL_BRIDGE_*` variable is forwarded to the Unix side (the daemon also drops
`FL_BRIDGE_TOKEN`). The shim adds one: `FL_BRIDGE_ANCHORS=unix=win;unix=win;...`, the
anchor table (below), which `fl-winexec` uses to hand paths back to Windows programs
(for example the rebase-todo file to Fork's editor).

## fl-shim.exe — bridge mode (default)

1. Read the configuration; `wine_get_unix_file_name` must be available (we run under Wine).
2. **cwd**: `GetCurrentDirectoryW` → Unix path. **argv** (`git`): `fl_translate_argv`
   (whole-argument Windows paths, allowlisted `--opt=` / next-argument options, the `-c`
   sanitizer, subcommand + output plan). **argv** (`bash`, `sh`): every whole argument that
   is Windows-absolute is translated, nothing else (scripts given with `-c` are untouched);
   the program is `/bin/bash` or `/bin/sh`. **Environment**: `GetEnvironmentStringsW` →
   `fl_translate_env` (set / unset operations).
3. **Anchors** (logical Windows path ↔ Unix path): the cwd, every `-C` (cumulative, relative
   ones resolved like git does), `--git-dir`, `--work-tree` and `GIT_DIR`. They are sent as
   `FL_T_ANCHOR` records when the output needs translating, and the daemon returns their
   realpaths in `SPAWN_OK`, so both the physical and the logical Unix form map back to the
   Windows form Fork used.
4. Connect to `127.0.0.1:$FL_BRIDGE_PORT` (`TCP_NODELAY`, not inheritable) and authenticate
   both ways: `HELLO(magic, version, client_nonce)` → `CHALLENGE(server_nonce, daemon_mac)`,
   checked in constant time **before** we prove anything → `AUTH(client_mac)` → `AUTH_OK`.
   A process squatting on the port after a daemon crash cannot feed Fork fake output.
   Handshake and spawn have a 10 s receive timeout; the relay has none.
5. `REQ` → `SPAWN_OK` (or `SPAWN_ERR` → message on stderr, exit 127).
6. Relay: a stdin pump thread sends `STDIN` frames then `STDIN_EOF`; the main thread writes
   `STDOUT` / `STDERR` payloads raw with `WriteFile`, through `fl_out` translators: stdout
   with the plan from `fl_cmdinfo` (`-z` / `--null` after the subcommand selects NUL
   records), stderr with `FL_OUT_PATHS_LINES` when `stderr_paths` is set. Output form is
   `Z:/home/u/repo` (upper-case drive, forward slashes, like Git for Windows).
7. `EXIT` → flush the translators → exit with the child's code, or 128 + signal.
   If Fork closes our stdout, the shim sends `SIGNAL(13)` (the daemon closes the child's
   stdout so it gets `EPIPE`/`SIGPIPE`) and exits 141. A missing stdout handle (NULL) is
   not "closed": the data is discarded.

Spike B2 fixes kept on this side: **fix 1** — the pump thread never exits by itself;
`flw_exit()` sets the quit event, calls `CancelSynchronousIo` repeatedly and joins it before
`ExitProcess`, so Wine cannot report exit code 0. The join is capped at 500 ms while the pump
is inside `send()`, and at 20 ms while it is blocked in a `ReadFile` Wine cannot cancel (a
Unix pipe inherited from a shell; pipes created by a Windows parent such as Fork cancel at
once). A pump that was not joined in time is "abandoned": it can then never return or send,
so it cannot race `ExitProcess`. **Fix 4** — the socket is never closed while the pump may be
inside `send()`; process teardown closes it after the join. Frame sends from both threads are serialised by a critical section; the `SIGNAL` send
never blocks behind a pump stuck in `send()` (200 ms try-lock, 1 s send timeout).

## fl-shim.exe — record mode

`FL_BRIDGE_MODE=record` forwards the call to Fork's bundled git, unchanged, to build the
corpus of what Fork really runs: `git` → `FL_BRIDGE_BUNDLED_GIT`; `bash` / `sh` →
`<bundled root>\usr\bin\<name>.exe` (or `bin\<name>.exe`). The original command-line tail is
reused verbatim (no re-quoting), the std handles are inherited, the child runs with
`CREATE_NO_WINDOW` inside a kill-on-close job object, and its exit code is propagated.
`FL_BRIDGE_TOKEN` is removed from the environment the bundled program inherits (it, its
hooks and its helpers never need it).
A target that resolves to the shim itself is refused (125) to avoid recursion.

## fl-launch.exe

```
fl-launch.exe terminal|open|reveal [args...]   detached: FL_REQ_HOST_HELPER | FL_REQ_DETACH, returns 0 at once
fl-launch.exe diff|merge|edit|run [args...]    FL_REQ_HOST_HELPER, waits, returns the tool's status
```

Arguments that are Windows-absolute (`C:\x`, `Z:/x`, `\\?\...`, `file://C:/...`) are
translated to Unix paths; everything else (options, text) is passed unchanged. The daemon
prepends its configured host-helper path (`fork-linux-host`). Same transport and
authentication as the shim; nothing Linux-side is ever started through Wine's
`CreateProcessW` (spike B2: it would inherit wine-staging's seccomp filter). Unknown verb or
no verb → usage message, exit 2.

## Exit status

| Code | Meaning |
|---|---|
| child's code | normal exit |
| 128 + n | the child was killed by signal n |
| 2 | `fl-launch.exe` usage error |
| 125 | bridge failure: missing/invalid environment, daemon unreachable, authentication failed, protocol error, connection lost |
| 127 | the program could not be started (`SPAWN_ERR`, or record mode `CreateProcessW` failed) |
| 141 | Fork closed our stdout (SIGPIPE equivalent) |

## JSON-line log (`FL_BRIDGE_LOG`)

One line per call, appended with `FILE_APPEND_DATA` under a byte-range lock (parallel calls
do not interleave). Fields: `ts` (UTC, ms), `tool` (`fl-shim` / `fl-launch`), `mode`
(`bridge` / `record` / `launch`), `persona`, `pid`, `exe`, `argv`, `cwd`, `env` (only
`FORK_PROCESS_ID`, `GIT_*`, `SSH_*`, `LANG`, `LC_*`), `stdio` (`GetFileType` of the three
std handles: `disk` / `char` / `pipe` / `remote` / `unknown` / `none`), `duration_ms`, `exit`;
record mode adds `target`; bridge mode adds `unix_cwd`, `xargv` (what the daemon ran),
`subcmd`, `out_plan`, `unix_pid`.

Redaction: an env value is replaced by `<redacted>` when its name **or** value contains
`TOKEN`, `PASS`, `SECRET`, `AUTH`, `COOKIE` or `EXTRAHEADER` (case-insensitive); the value of `-c key=value`
is redacted when the key or value looks secret; the userinfo of every `scheme://user:pw@host`
is replaced in arguments and values. The log never contains `FL_BRIDGE_TOKEN`.

## Build

Release flags (the meson bridge targets use exactly these):

```
x86_64-w64-mingw32-gcc -std=c11 -O2 -municode -mwindows -DUNICODE -D_UNICODE -D_WIN32_WINNT=0x0601
  -Wall -Wextra -Werror -ffunction-sections -fdata-sections -fno-ident
  -static -static-libgcc -s -Wl,--gc-sections,--no-insert-timestamp,--build-id=none
  -lws2_32 -lbcrypt
x86_64-w64-mingw32-windres --input-format=rc --output-format=coff -I <dir of fl_version.h> -I bridge/win/res
```

`bridge/win/*.c` are additionally clean under `-Wconversion -Wsign-conversion -Wshadow
-Wstrict-prototypes -Wmissing-prototypes` (used by `build-dev.sh`).

Each executable links `fl_<name>.c` + `fl_win.c` + `bridge/common/*.c` + its resource object.
The `.rc` files `#include "fl_version.h"`, which the build generates from the repository's
`VERSION` file (single source of truth) — it must define `FL_VERSION_MAJOR`,
`FL_VERSION_MINOR`, `FL_VERSION_PATCH` (numbers) and `FL_VERSION_STR` (`"N.N.N"`); a missing
definition is a hard `#error`. Builds are reproducible (no timestamps, no build id, no
`__DATE__`).

```sh
bridge/win/build-dev.sh     # docker fork-linux-ci-bridge:26.04 -> build-bridge/dev/{fl-shim.exe,fl-launch.exe,SHA256SUMS,tests/fl-testdrv.exe}
```

## Text encoding

UTF-8 inside, UTF-16 only at the Win32 boundary. Conversions are strict
(`WC_ERR_INVALID_CHARS` / `MB_ERR_INVALID_CHARS`): an argument or working directory holding a
lone UTF-16 surrogate exits 125 ("not valid UTF-16") instead of silently becoming U+FFFD and
naming a different file on the Unix side; an environment variable that cannot be converted is
left out of the request.

## Tests (Wine tier)

`tests/bridge/test_shims_wine.py` runs the built PEs under Wine against a real daemon (gated:
`FL_REAL_WINE=1`; `FL_WINE` picks the wine binary, `FL_BRIDGE_WIN_DIR` the build directory,
`FL_BRIDGE_HELPER_BIN` a prebuilt daemon). It covers the exit-status contract, argv and binary
stdin round trips, logical-path output through a symlinked `C:` path, Fork-like pipe spawns
with the stdin pipe held open (B2 fix 1), a reader that closes stdout (141), `TerminateProcess`
of the shim (the Unix child must die), lone surrogates, no std handles, parallel exit-code
exactness, configuration and authentication failures (125), log redaction, record mode and
`fl-launch.exe`.

```sh
bridge/win/build-dev.sh
FL_REAL_WINE=1 python3 -m pytest -q tests/bridge/test_shims_wine.py
FL_REAL_WINE=1 FL_WINE=/path/to/wine-11.0-staging-amd64-wow64/bin/wine python3 -m pytest -q tests/bridge/test_shims_wine.py
```

## Trying it by hand under Wine

Use a scratch prefix and a fake `HOME` — never `~/.wine`:

```sh
export HOME=/scratch/home WINEPREFIX=/scratch/prefix WINEDEBUG=-all \
       WINEDLLOVERRIDES="mscoree,mshtml=;winemenubuilder.exe=d"
export FL_BRIDGE_TOKEN=$(python3 -c 'import secrets; print(secrets.token_hex(32))')
fl-bridge-helper --daemon --port 0 --host-helper /usr/libexec/fork-linux/fork-linux-host &  # prints FL_BRIDGE_PORT=<n>
export FL_BRIDGE_PORT=<n> FL_BRIDGE_WINEXEC=/path/to/fl-winexec
mkdir -p gi/cmd && cp build-bridge/dev/fl-shim.exe gi/cmd/git.exe
cd some/repo && wine /path/to/gi/cmd/git.exe rev-parse --show-toplevel    # -> Z:/path/to/some/repo
```

## Known limitations

- On wine-staging (the managed runtime) about 1 PE start in 1,000 by a Wine process fails at
  start-up (spike B2, "W11s instability"); this hits the shim exactly as it hits bundled
  `git.exe`.
- `wine_get_unix_file_name` returns paths through `dosdevices/<x>:` for drives other than
  `Z:` on Wine 10 (Wine 11 resolves the symlinks and returns the physical path). Git accepts them, and output translation uses the daemon's realpaths, but
  `FL_BRIDGE_ANCHORS` carries the `dosdevices` form, so `fl-winexec` maps paths under
  custom drive letters back through its own fallbacks (`drive_c` → `C:`, else `Z:`).
- `bash.exe` / `sh.exe`: paths inside `-c` scripts are not translated.
