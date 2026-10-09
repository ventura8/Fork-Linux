# Spike 01 — managed Wine runtime, .NET, Fork install, rendering

Date: 2026-10-09 · Host: Ubuntu 26.04 x86_64 · Script: `scripts/spike/fl-spike.sh`
(isolated scratch root: fake `HOME`, XDG dirs and `WINEPREFIX`; `~/.wine` untouched).
Screenshots stayed in the scratch dir (they show Fork's logo — never committed).

## Results

| # | Spike | Verdict | Observation | Decision |
|---|---|---|---|---|
| S1 | Kron4ek `wine-11.0-staging-amd64-wow64` | **PASS** | sha256 `e6538a41…055f2f` matches; `wine --version` = `wine-11.0 (Staging)`; `wineboot --init` 10 s; `#arch=win64`; no i386 libs needed. ldd "not found": only optional `libavcodec/libavformat/libavutil` (winedmo media), `libpcap` (wpcap) and Wine-internal `ntdll.so`/`win32u.so` (loader-resolved, false positive). | Pin this build as the managed default. `host.libs` doctor check must ignore the optional/internal sonames. |
| S2 | winetricks 20260125 `dotnet48` + `corefonts` | **PASS** | winetricks sha256 `431f82fc…12a7b` (830,687 B). `dotnet48` rc 0 in **161 s**; `NDP\v4\Full\Release` = `0x80eb1` (528049 = 4.8). corefonts rc 0 in 48 s. Registry batch (Avalon `DisableHWAcceleration=1`, `AppDefaults\Fork.exe Version=win7`, font replacements Segoe UI → Noto Sans) applied via `regedit /S` of a UTF-16LE `.reg`. | Verb order `[dotnet48, dotnet472]` confirmed; dotnet472 fallback stays untested until needed. |
| S3 | `Fork-2.23.2.exe --silent` | **PASS** | Installer sha256 **`fee9b2bf84aca6297d7b7e10b29a09c624ac16a82486f2c04136f5ebaf8f079e`**, 76,278,256 B, `MZ`. Install rc 0 in **2 s**; Fork is **not** auto-launched by `--silent`. Layout: `Fork/{Update.exe,current/,packages/Fork-2.23.2-full.nupkg,logs/,velopack.log}`; `current/` holds `Fork.exe`, `Fork.RI.exe`, `Fork.AskPass.exe`, `7za.exe`, `PortableGit-2.50.1-64-bit-7z`, `sq.version` (nuspec XML with `<version>`). **Full nupkg sha256 equals the feed's `SHA256`.** Shortcuts written: `drive_c/users/<u>/Desktop/Fork.lnk` and `AppData/Roaming/Microsoft/Windows/Start Menu/Programs/Fork.lnk`. | Record the installer sha in the manifest (known-good). The post-install nupkg-vs-feed check is a valid TOFU verifier for `--latest`. Keep the "Desktop must be a real dir" step so `Fork.lnk` never reaches the Linux desktop (with a real HOME Wine symlinks Desktop → `~/Desktop`). |
| S4 | Rendering under Xvfb | **PASS** | First launch shows the "Welcome to Fork" dialog (title "User information"), then the main window (title "Fork"). `WM_CLASS` = `fork.exe` for every window. Software rendering + Noto Sans replacement render cleanly; minor kerning artefacts only. Fork's own updater fetched `https://fork.dev/update/win/releases.win.json` fine (TLS works). | `StartupWMClass=fork.exe` confirmed. E2E waits for a visible `--class fork.exe` window wider than 200 px, not a title. |
| S5 | Windows-side `HOME` | **PASS (with WINEHOME)** | Without help, `%HOME%` is unset on the Windows side; `WINEHOME='C:\users\<u>'` makes `%HOME%` = `C:\users\<u>`. | Launcher exports `WINEHOME`; do not override the Unix `HOME`. |
| S6 | Bundled git 2.50.1.windows.1 on a Linux clone | **PARTIAL** | Bundled `etc/gitconfig` sets `core.autocrlf=true`, `core.symlinks=false`, `core.fscache=true`, `credential.helper=manager`. On a natively-created repo: exec-bit file shows ` M` (fixed by `GIT_CONFIG_*` `core.filemode=false`); a symlink shows ` M`, and still ` T` with `core.symlinks=true` (Wine does not present Unix symlinks as symlinks to msys) — **committing through bundled git turned the symlink into a regular file**. `autocrlf=true` warns "LF will be replaced by CRLF". **msys bash cannot spawn children** (`/usr/bin/ls: Bad address`) even on this staging build → hooks that run programs, bash custom commands and `sh.exe`-based features fail (Wine bug 55138). Plain status/log/commit work (0.14 s per `git status`). | Launcher env overrides: `core.filemode=false`, `core.autocrlf=false`, `core.symlinks=true`. Enforce `UpdateSubmodulesOnCheckout=false`. Doctor warns about repos with symlinks or hooks. **The native-git bridge is the real fix** — highest-value experimental feature. |
| S7 | Single-instance forwarding | **PASS** | `Fork.exe <Z:\…\second>` while Fork runs opens a new tab and the second process exits 0 in ~2.2 s. | `fork <path>` can simply exec Wine again. |
| S8 | `settings.json` keys | **PASS** | Fork creates `settings.json` on first completed launch. Relevant keys: `Theme` (0 light / 1 dark), `FollowSystemTheme`, `LayoutScaling` (percent, e.g. 125), `UpdateSubmodulesOnCheckout`, `DisableHardwareAcceleration`, `ShellTool` (null by default), `ExternalDiffTool`/`MergeTool` `{Type:"Custom",ApplicationPath,Arguments}` + `ExternalDiffTools`/`ExternalMergeTools` lists, `GitInstancePath`, `ApplicationUpdateType`, `RepositoryManager.SourceDirectories` (defaults to `C:\users\<u>`), `Workspaces`. Setting `Theme=1, FollowSystemTheme=false, LayoutScaling=125` with Fork closed gave a dark, scaled UI and the values persisted. Repo list lives in `AppData/Local/ForkData/repositories.toml`. The welcome dialog writes `user.name/email` to the Wine user's `.gitconfig`. | Theme sync and HiDPI via `settings.json` (no WinRT needed). Seed `RepositoryManager.SourceDirectories` with the Linux home (`Z:\home\<u>`). Map `ApplicationUpdateType` values later (UI). |

## Disk and time budget (measured)

Prefix 2.0 GB, extracted runtime 0.7 GB, downloads 145 MB, winetricks cache 124 MB.
End-to-end setup ≈ 4 minutes on this host (dominated by dotnet48 161 s + corefonts 48 s).

## Open (later spikes)

Velopack in-app update/downgrade (S9); Wayland driver; `ApplicationUpdateType` mapping;
`LogPixels` vs `LayoutScaling`; license MachineGuid stability across Wine upgrades; system Wine 10.0 provider run.

### Resolved by the E2E run (2026-10-09, `tests/e2e/RESULTS.md`)

- **`settings.json` preseed before the first launch: safe, adopted.** A seeded
  `{"Guid": <uuid4>, "UpdateSubmodulesOnCheckout": false, "Theme": 1, "FollowSystemTheme": false}`
  was kept by Fork, which added its own keys (99 in total). The "User information" welcome dialog
  is skipped; Fork shows its one-time "update the Fork git instance" dialog (Start, then Close)
  instead. user.name/email then come from the host `~/.gitconfig` through the git overlay.
  `fork_settings.seed()` is used by the `fork` setup step and the launcher's pre-launch hook.
- **Hardlink snapshots + rollback (part of S9):** `snapshot create` (hardlink) and
  `rollback --to-version 2.23.2 --no-pin` work (0.19 s); Fork starts afterwards; rollback while
  Fork runs is refused with exit 15. Velopack's own update/downgrade path is still open.
- **Theme and scaling via `settings.json` from the pre-launch hook:** `Theme=1`,
  `FollowSystemTheme=false`, `LayoutScaling=100` applied with Fork closed and persisted after
  Alt+F4 (dark UI confirmed by screenshot brightness).
- **New open item:** Fork's own status engine reads the repository's `.git/config`
  (`filemode = true`), so exec-bit-only changes still show as modified despite the
  `GIT_CONFIG_*` `core.filemode=false` override (needs a per-repo opt-in or the native-git bridge).

## Addendum — bundled git child spawning, staging vs upstream (2026-10-09)

With Fork's bundled `gitInstance\2.50.1` run under **system Wine 10.0 (non-staging)** in a separate scratch
prefix, `bash.exe -c 'ls'` and `git commit` with a pre-commit hook that runs `ls` both **hang** until
killed (60 s timeout) — Wine bug 55138. Under the pinned **11.0 staging** build the same commands fail
fast (`Bad address`) instead of hanging. Decision: keep the staging pin (fail-fast is better than a hung
Fork); treat hooks/custom bash commands as unsupported with bundled git and steer users to the native-git
bridge. Spike B2 also observed rare `SIGSYS` crashes of Wine processes under staging's seccomp filter —
a stress run of Fork + bundled git on the pinned runtime is a follow-up before v1.0.
