# B5: Wine tier and benchmark of the native-git bridge

Date: 2026-10-09. These are the production bridge components, not the B2 prototype. Each
one is built by meson (`scripts/build-bridge.sh`, image `fork-linux-ci-bridge:26.04`):

- `fl-shim.exe` and `fl-launch.exe`: MinGW-w64 GCC 13, GUI subsystem.
- `fl-bridge-helper --daemon`: glibc PIE, started natively the way the launcher will start it.
- `fl-testdriver.exe`: a test-only driver (`bridge/tests/testdriver/`) that spawns programs the
  way Fork's .NET `Process.Start` does:
  - three `CreatePipe` pipes, `STARTF_USESTDHANDLES`
  - `CREATE_NO_WINDOW | CREATE_UNICODE_ENVIRONMENT`
  - an explicit sorted environment block and an explicit working directory
  - asynchronous readers

Host and Wine builds are the same as in [B1](B1-wine-unix-spawn.md) and [B2](B2-rendezvous.md):

- **W10**: system Wine 10.0 (`/usr/bin/wine`, Ubuntu 26.04).
- **W11s**: Kron4ek `wine-11.0-staging-amd64-wow64`, the managed default.

Host git is 2.53.0.

Suite: `tests/bridge/wine/test_wine_bridge.py`, with shared setup in `fl_winetier.py`. It is
gated by a module-level `pytest.skip("requires FL_REAL_WINE=1")`. Without `FL_REAL_WINE=1`
(plain CI), `tests/bridge` together with `tests/test_bridge_native.py` reports 69 passed and 33
skipped.

The suite tests `/usr/bin/wine` by default. Other Wine builds (W11s) are listed in
`FL_TEST_WINE`, separated by `:`:

```sh
FL_CI_STAGE=bridge ./scripts/ci-docker.sh   # build + FL_REAL_WINE=1 tests/bridge in fork-linux-ci-bridge:26.04
```

(The measurements below were taken on the host with
`FL_TEST_WINE=... FL_REAL_WINE=1 python3 -m pytest tests/bridge`. Since 2026-10-10 the Wine tier
refuses to run outside a container, AGENTS.md hard rule 18; other Wine builds go in
`FL_TEST_WINE` inside the bridge image.)

For each Wine build the suite:

1. boots a fresh scratch prefix (`wineboot -i`) with a fake HOME;
2. installs the shims at the production layout:
   - `C:\fork-linux\gitInstance\{cmd,bin,mingw64\bin}\git.exe`
   - `{bin,usr\bin}\{bash,sh}.exe`
   - `C:\fork-linux\bin\fl-launch.exe`
3. starts the daemon with a token file and a stand-in `fork-linux-host`;
4. runs every program through `fl-testdriver.exe`, passing `FL_BRIDGE_PORT`, `FL_BRIDGE_TOKEN`,
   `FL_BRIDGE_WINEXEC` and `FL_WINE` in the Windows environment.

## Results

The suite was run 10 times on the host, covering both Wine builds. Two of those runs were
broken by setup problems: a W11s `wineboot` hang (failure 3 below), and once the scratch tmpfs
quota ran out. It was also run once inside the CI image (W10 only, Wine 10.0 from Ubuntu 26.04),
with the same results as on the host.

Review re-run (same day, after the C-quoted path fix below), on the whole `tests/bridge`
directory (daemon protocol, shims tier and this suite):

- Host, W10 + W11s: 6 runs of 146 tests, all passed (52–58 s each). The run before the fix had
  144 passed and 2 failed, the expected C-quoted failure on both builds.
- CI image, W10 only (`-e HOME=/tmp/h`, `--basetemp=/tmp/h/pt`, source mounted read-only):
  122 passed in 38 s.

| Check | W10 | W11s |
|---|---|---|
| `git --version` is byte-identical to host `git --version` | PASS | PASS |
| `-C "Z:\…\repo ü space" log -1` is identical to native | PASS | PASS |
| `rev-parse --show-toplevel` returns `Z:/…/repo ü space`, both via `-C` and via the cwd; `--absolute-git-dir` too | PASS | PASS |
| exit codes 0 / 1 (`config --get`, `rev-parse --verify --quiet`) / 128 / 42 (alias `!exit 42`) | PASS | PASS |
| stdout and stderr stay separate (interleaved writes, `fatal:` only on stderr) | PASS | PASS |
| 10 MB stdin to `hash-object --stdin` gives the same OID as native | PASS | PASS |
| 100 MB `cat-file blob`: length and sha256 equal | PASS | PASS |
| 20 parallel calls: each keeps its own stdout and exit code | PASS | PASS |
| `TerminateProcess` on the shim during `sleep` kills the native child within 3 s | PASS | PASS |
| wrong token gives 125, with no output | PASS | PASS |
| `worktree list --porcelain` (with and without `-z`) and plain `worktree list` with ASCII paths are translated to `Z:/…` | PASS | PASS |
| plain `worktree list` with a non-ASCII (C-quoted) path is the native output with `"Z:/…"` | PASS (after fix 1) | PASS (after fix 1) |
| `GIT_DIR=Z:\…\.git` (cwd elsewhere): `log` matches native; `--absolute-git-dir` is `Z:/…/.git` | PASS | PASS |
| `-c core.editor="C:\Program Files\Fork\Fork.RI.exe"` and `C:/…/Fork.RI.exe` become `'<fl-winexec>' '<exe>'`, checked with `git var GIT_EDITOR`, with the alias `!git config core.editor`, and with `GIT_EDITOR` | PASS | PASS (1 run: GLE 731, see below) |
| `fl-launch.exe run -- /bin/echo hi` prints `hi` and returns 0; `run -- sh -c 'sleep 1; exit 5'` waits and returns 5 | PASS | PASS |
| `bash.exe` / `sh.exe` (`bin\` and `usr\bin\`) `-c 'echo ok; ls / >/dev/null && echo child-ok'` print `child-ok` | PASS | PASS |

The last row is the hook-spawn fix: the bundled MSYS bash cannot run child processes under Wine,
but the bridged `/bin/bash` can.

`tests/bridge/test_shims_wine.py` (the shims task's own Wine tier) also passes, 32/32 on W10.
It now uses the meson outputs (`build-bridge/bridge`) by default when they are complete. Before
the review it defaulted to `build-bridge/dev` (`bridge/win/build-dev.sh`), which could be an
older build. `FL_BRIDGE_WIN_DIR` and `FL_BRIDGE_HELPER_BIN` still override the defaults.

### Failures and flakes

1. **Bridge bug, fixed in review: C-quoted paths were not translated (both builds,
   deterministic).** When a path
   contains a non-ASCII byte, a quote or a control character, plain `git worktree list` C-quotes it
   (`core.quotePath`). The line then starts with `"` (for example `"/tmp/…/repo \303\274 space" 0271d2b [main]`).
   The `FL_OUT_WORKTREE` translator (`bridge/common/fl_translate.c`) only recognises unquoted
   absolute paths, so the Unix path reaches Fork unchanged. Git for Windows would print
   `"Z:/…/repo \303\274 space"`.
   - The porcelain form is not affected, and Fork is expected to use it.
   - Fix: `tr_worktree` now unquotes a leading `"/…"` path, translates it and quotes it again
     the same way. Bytes ≥ 0x80 stay octal-escaped, or stay raw when the input had them raw
     (`core.quotePath=false`). `git config --show-origin` uses the same helper
     (`tr_quoted_path`). There are four new `out.tsv` vectors (`worktree-default-c-quoted*`).
   - `test_worktree_list_c_quoted_paths_are_translated` now compares the whole output with the
     native output, Z:-translated, instead of only the first line's prefix.
   - Still open: C-quoted paths inside stderr lines (`tr_paths_lines` only handles
     `'…'`-quoted ones).
2. **W11s: `CreateProcessW` GLE 731 (1 call in about 480 W11s driver calls).** This is the runtime
   instability already measured in B2 ("W11s instability"). One `fl-testdriver.exe` spawn of the shim
   failed before the shim ran. The same thing happens to bundled `git.exe`: the bundled `status` series
   in the benchmark below lost 1 of 200 runs. It is not caused by the bridge.
3. **W11s: a fresh prefix sometimes never finishes `wineboot -i` (2 of 11 fresh prefixes).** In these
   cases `explorer.exe` is already defunct, and `rundll32 setupapi,InstallHinfSection` (`PreInstall` or
   `DefaultInstall`) waits forever. `WineBridge.boot_prefix` now kills a hung boot after 240 s with
   `wineserver -k`, recreates the prefix and retries (3 attempts). After that change, no suite run
   errored. This affects setup only (the product's first-run `setup` has the same exposure). It is
   worth tracking against the staging pin.
4. **CI image: wine 10.0 `wineboot` aborts with `free(): invalid pointer` (rc 134) when `TMPDIR` is
   set.** This happens inside `fork-linux-ci-bridge:26.04` and is reproducible with `TMPDIR=/tmp/h`
   alone; it does not happen on the host. The Wine tier therefore does not set `TMPDIR` for Wine
   processes. A CI stage running the tier needs a writable `HOME` owned by the user (for example
   `-e HOME=/tmp/h` plus `--basetemp=/tmp/h/pt`), because Wine refuses to create a prefix under a
   directory the user does not own.

## Benchmark

`(cd tests/bridge && python3 -m wine.bench_bridge --scratch <dir> --iterations 200 --wine <w10> --wine <w11s> --bundled-git <prefix>/drive_c/users/<user>/AppData/Local/Fork/gitInstance/2.50.1/cmd/git.exe)`
measures (the bundled rows need `--bundled-git`; without `--wine` only `/usr/bin/wine` is measured)
`fl-testdriver.exe bench`:

- 200 sequential spawns through .NET-style pipes in one Wine process, with a warm wineserver and
  page cache.
- The repository has 2,000 committed files in 40 directories, 20 of them modified, plus 20 untracked
  files.
- **bundled** is Fork's own Git for Windows 2.50.1 (`…/Fork/gitInstance/2.50.1/cmd/git.exe` from the
  spike prefix, reached as a `Z:\` path).
- **host git, native** is `/usr/bin/git` started from Python without Wine; it is the floor.

| Wine | variant / command | p50 ms | p95 ms | mean ms | failures |
|---|---|---|---|---|---|
| W10 | noop-gui.exe | 9.15 | 10.20 | 9.30 | 0 |
| W10 | bridge `rev-parse HEAD` | **13.56** | 15.97 | 13.82 | 0 |
| W10 | bundled Git for Windows `rev-parse HEAD` | 92.00 | 98.31 | 92.65 | 0 |
| W10 | host git, native `rev-parse HEAD` | 0.72 | 0.83 | 0.73 | 0 |
| W10 | bridge `status -z -uall` | **15.64** | 17.52 | 15.92 | 0 |
| W10 | bundled Git for Windows `status -z -uall` | 106.66 | 114.53 | 109.41 | 0 |
| W10 | host git, native `status -z -uall` | 2.07 | 2.46 | 2.14 | 0 |
| W11s | noop-gui.exe | 7.37 | 8.33 | 7.47 | 0 |
| W11s | bridge `rev-parse HEAD` | **12.41** | 13.90 | 12.58 | 0 |
| W11s | bundled Git for Windows `rev-parse HEAD` | 89.80 | 96.97 | 90.94 | 0 |
| W11s | host git, native `rev-parse HEAD` | 0.70 | 0.78 | 0.71 | 0 |
| W11s | bridge `status -z -uall` | **14.31** | 16.08 | 14.57 | 0 |
| W11s | bundled Git for Windows `status -z -uall` | 108.13 | 121.02 | 111.98 | 1 (GLE 731) |
| W11s | host git, native `status -z -uall` | 2.05 | 2.21 | 2.09 | 0 |

Review re-run, 200 iterations, same machine, with more load: W10 noop 9.60, bridge `rev-parse`
15.55, bundled `rev-parse` 101.43, bridge `status` 17.08, bundled `status` 115.80. W11s noop 7.69,
bridge `rev-parse` 12.82, bundled `rev-parse` 93.26 (1 failure, GLE 731), bridge `status` 16.72,
bundled `status` 109.91 (all p50 ms). Host git: 0.74–0.75 and 2.07–2.23 ms.

- **The bridge is 6.5–7.3× (`rev-parse`) and 6.6–7.6× (`status`) faster than bundled git per
  call**, across both runs. At p50, `rev-parse HEAD` costs noop + 4.4 ms (W10) and noop + 5.0 ms
  (W11s) in the table, and noop + 6.0 / + 5.1 ms in the re-run. That is the B2 daemon
  figure (+3.9 / +4.8 ms) plus the production work: translation, the mutual HMAC handshake and
  anchors.
- **Bundled git costs 80–100 ms per call.** Its MSYS2 runtime start-up under Wine dominates. Fork runs
  dozens of git calls per refresh, so the gain is visible.
- **The remaining bridge cost is almost all Wine process creation of the shim** (the noop row). The git
  work itself is native speed (0.7 / 2.1 ms).

## Build and reproducibility

- `scripts/build-bridge.sh --repro` builds twice in fresh build directories with the same
  `SOURCE_DATE_EPOCH` (the last commit time). All outputs are byte-identical:
  - `fl-shim.exe`, `fl-launch.exe`
  - `fl-bridge-helper` (glibc PIE) and `fl-bridge-helper-static` (musl `-static -no-pie`)
  - `fl-testdriver.exe`, `fl-testdrv.exe`
- `meson test --suite bridge-unit`: 3/3 pass. That covers the bridge/common TAP suite (1,391 checks
  after the review's four new vectors; 1,367 before),
  `test-helper-util` and `test-helper-paths`.
- Without MinGW (`-Dbridge=auto`, for example on a plain host), the PE targets are skipped with a
  message; `-Dbridge=enabled` fails instead. Re-checked on the host (gcc 15.2, no MinGW): `auto`
  configures and passes the 3 unit tests; `enabled` stops at `x86_64-w64-mingw32-gcc` not found.
- Re-checked in review: `scripts/build-bridge.sh --repro` (with `--out`) gives all six outputs
  byte-identical. `shellcheck` reports nothing for `scripts/build-bridge.sh`,
  `bridge/{unix,win}/build-dev.sh` and `bridge/tests/unit/run.sh`. clang-tidy is not installed on
  this host, so it was not run.
