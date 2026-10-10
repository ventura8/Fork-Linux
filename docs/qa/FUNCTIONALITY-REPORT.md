# Fork functionality audit under Wine (v0.1.0 branch)

Date: 2026-10-09 · Host: Ubuntu 26.04 x86_64, host git 2.53.0 · Fork **2.23.2** (bundled git
2.50.1.windows.1, git-lfs 3.7.0) · Wine: managed Kron4ek **11.0 staging WoW64** · wrapper:
`fork-linux 0.1.0-dev (source)` run from `src/` of `feature/v0.1.0`.

## Method and isolation

* Everything ran under the scratch root `~/.cache/fork-linux-audit` (historical: QA now runs only
  in containers, see Reproduce) with a fake `HOME` and XDG
  dirs, a private `Xvfb :96` (1920x1200, **no window manager**: no X11 WM is installed on the
  host), and download caches seeded from the real cache. The user's prefix, `~/.wine`, `:0`,
  `:97` and `:98` were never touched. Screenshots stayed in the scratch root (they show Fork's
  logo) and are **not** in the repository.
* Install: `fork-linux setup --accept-fork-eula --no-gui` (4m17s, no downloads). First run
  completed the "User information" dialog (QA Tester / qa@example.invalid), then a relaunch with
  `FORK_LINUX_DISPLAY_THEME=dark` so the settings hooks applied.
* Fork was driven with xdotool (keyboard shortcuts, menus, context menus) and every screenshot was
  inspected. Every git-level effect was verified with native git; `fork.log` was read after each
  area.
* Fixtures (`scripts/qa/fl-qa.sh fixtures` / `bigrepo`): a history repo (26 commits, 3 branches,
  3 tags, a `--no-ff` merge, exec script, symlink, PNG, 27 MB text file, CRLF + LF files,
  `grüße-ü.txt`, `中文.txt`, `dir with spaces/`), a bare `file://` remote, a submodule repo, a
  pre-commit-hook repo, a two-branch conflict repo, 3 small tab repos and a 5,001-commit /
  5,898-file repo. No sshd was running, so ssh remotes were not tested.
* Deviations, all reverted inside the scratch prefix: to keep testing remotes after finding #2,
  an `insteadOf` rewrite was appended to the Wine-side `.gitconfig` (it simulates the proposed
  fix). An IFEO `Debugger` key for `explorer.exe` was tried and deleted. Logging stubs for
  `xdg-open`, `gdbus`, terminals and file managers were put first on Fork's `PATH` to see which
  host program is spawned.

Reusable helpers: [`scripts/qa/fl-qa.sh`](../../scripts/qa/fl-qa.sh) (isolated env, Xvfb,
setup, run, screenshots, fixtures, cleanup) and
[`scripts/qa/fl-qa-gitprobe.sh`](../../scripts/qa/fl-qa-gitprobe.sh) (reproduces the
bundled-git findings without the GUI). Both pass `shellcheck`.

## Summary table

Legend: **PASS** works as on Windows · **PARTIAL** works with caveats or only with a workaround ·
**FAIL** broken or silently wrong · **N/T** not tested (reason given).

| # | Area / feature | Status | Evidence (short) | Root cause | Recommended fix |
|---|---|---|---|---|---|
| 1.1 | Startup, first run | PASS | Welcome dialog, then main window. First start 48 s (`AppInitialization: 46804ms`: Fork extracts gitInstance); later cold starts 5.1–5.4 s by Fork's own timer, 7.6–10.9 s to a visible window | first-run 7z extraction of the bundled git | (a) mention "first start takes ~1 min" in the setup's last message |
| 1.2 | Rendering, fonts | PASS | crisp software rendering; `中文.txt` and `grüße-ü.txt` render in Fork. Minor: `✓` (U+2713) falls back to a `√`-like glyph; Wine's own explorer/file dialogs show CJK as boxes | Noto Sans replacement lacks some symbols; Wine shell UI font has no CJK fallback | (a) add a CJK/symbol fallback (`Noto Sans CJK`, `Noto Sans Symbols2`) to the font-replacement / `FontLink` registry batch |
| 1.3 | Dark theme | PASS | `Theme=1, FollowSystemTheme=false` written; whole UI dark. Wine file dialogs and About stay light (expected) | — | — |
| 1.4 | HiDPI | **FAIL** | `FORK_LINUX_DISPLAY_DPI=144` gave `LogPixels=0x90` **and** `LayoutScaling=150`; UI scaled ~2.25x (tab bar y 84 → 184 px) | `steps/display.py` sets Wine DPI and `fork_settings._scaling_value()` also sets Fork's own scaling: applied twice | (a) use one mechanism: keep `LogPixels=96` and drive `LayoutScaling`, or drop `LayoutScaling` when `LogPixels` ≠ 96 |
| 1.5 | Resize / maximize | PARTIAL | resizing the X window works and the layout follows. Maximize without a WM: Fork lays out for 1920x1200 but the X window stays 1000x600 (clipped); restore state comes back 1000x600 at -4,-4 | Xvfb has no WM, so `_NET_WM_STATE` is ignored | retest on GNOME/KDE; not a wrapper bug |
| 1.6 | Multiple tabs | PASS | 9 tabs; switching, Ctrl+W, restored after restart | — | — |
| 1.7 | Repository Manager | PASS | lists recent/known repos with summary | scans only `C:\users\<u>` (see 1.11) | see 1.11 |
| 1.8 | Quick Launch (Ctrl+P) | PASS | lists repos and commands; switching by name works | — | — |
| 1.9 | File > Open repository | PARTIAL | Wine file dialog shows the `/` tree (Linux FS reachable); typing `Z:\home\…` works. Typing `/home/…` gives Git Error `Win32Exception: Directory name invalid` | the dialog returns the Unix path verbatim; .NET treats it as drive-relative | (c) document "type `Z:\…` or browse via `/`"; (a) optional: add a `H:` drive mapping to `$HOME` |
| 1.10 | `fork <path>` into a running Fork | PASS | new tab each time, 2.29–2.40 s; Fork logs a harmless `Cannot read cliRequest from pipe` | — | — |
| 1.11 | Default source folder | **FAIL** | Preferences > General, the Welcome dialog and Clone's "Parent Folder" all show `C:\users\<u>\` (inside the prefix) although the wrapper wrote `RepositoryManager.SourceDirectories=["Z:\home\<u>"]` | Fork 2.23 reads `source_dirs` from `AppData/Local/ForkData/repositories.toml`; the `settings.json` key is legacy | (a) seed `repositories.toml` `source_dirs` (closed Fork, backup, keep unknown keys). Hard rule 3 must list this file. Today clones land **inside the prefix** and `uninstall --purge` would delete them |
| 2.1 | Commit list, graph, details | PASS | graph, refs and tags render; the external commit made with native git appeared automatically (fs-monitor) | — | — |
| 2.2 | Search / filter | PASS | commit-message search ("topic", 2 results); sidebar filter ("alpha") | — | — |
| 2.3 | Blame, file history | PASS | Blame and History windows with timeline | — | — |
| 2.4 | Diff viewer | PASS | inline + side-by-side; syntax highlighting (Python); hunk headers; "No newline" markers. Word-wrap toggle present (not exercised) | — | — |
| 2.5 | Image diff | PASS | side-by-side, swipe and onion-skin on a changed PNG | — | — |
| 3.1 | Status: exec bit | **FAIL** | `run.sh` shows **M** with an empty diff; disappears only after `git config core.filemode false` in the repo | Fork computes status **in-process** (6–20 ms, no git.exe spawned) and ignores `GIT_CONFIG_*`; it honours the repo's `core.filemode=true` that Linux `git init` writes | (a) doctor check + one-click (consented) `core.filemode=false` per repo in `repositories.toml`; document. `GIT_CONFIG_*` still helps spawned git commands |
| 3.2 | Status: symlink | **FAIL** | `link-to-target` always **M** (diff shows the target's content); bundled `git status` says ` M`, ` T` with `core.symlinks=true`. Blocks rebase / interactive rebase: "cannot rebase: You have unstaged changes" | Wine shows Unix symlinks to Win32 as regular files (spike S6) | (b) native-git bridge. Interim (a): doctor detects symlinks and offers `git update-index --skip-worktree <link>` (verified: clears both Fork's and bundled git's status) |
| 3.3 | Status: CRLF/LF | PASS | `crlf.txt` / `lf.txt` clean; clones by Fork are clean natively (`core.autocrlf=false` override works for spawned git) | — | — |
| 3.4 | Stage / unstage (file, hunk, line) | PASS | hunk Stage button; line selection stages only `+row 28 CHANGED`; Unstage; verified with `git diff --cached` | — | — |
| 3.5 | Discard | PASS | confirmation dialog; file restored | — | — |
| 3.6 | Commit / amend | PASS | UTF-8 subject `ümlaut ✓` byte-exact; author from the welcome dialog; amend adds body (reflog `commit (amend)`) | — | — |
| 3.7 | Commit template | PASS | repo-relative `commit.template` pre-fills subject + body | — | — |
| 3.8 | Spell check | **FAIL** | "English" (2) and "System (en-US)" (1) both persist but no misspelling is underlined | WPF spell checking needs Windows' spell-check COM / ISpellChecker, not implemented in Wine | (c) document; keep default "Disable" |
| 3.9 | Open file in default app | **FAIL** | "Open" greyed out; `fork.log`: `Failed to get associated editor … Could not determine associated string` | no file associations in the prefix (`AssocQueryString` fails); winemenubuilder is off by design | (a) register a `ForkLinux.File` ProgID (`shell\open\command = C:\fork-linux\bin\fl-launch.exe open "%1"`) for common text/source extensions in the registry step, once fl-launch works (row 8.2) |
| 4.1 | Branch create / checkout / rename / delete | PASS | all four via dialogs; verified with `git branch` | — | — |
| 4.2 | Merge | PASS | `--no-ff` merge commit with two parents | — | — |
| 4.3 | Rebase / interactive rebase | PARTIAL | Interactive Rebase window (fixup of the middle commit) rebased 3 commits onto main correctly, **after** working around 3.2; with the fixture symlink present it fails | symlink shows as modified (3.2) | as 3.2 |
| 4.4 | Cherry-pick, revert, reset | PASS | all three verified with `git log` | — | — |
| 4.5 | Tags (create + push) | PASS* | annotated tag created; push failed until the `file://` fix (5.2) | 5.2 | 5.2 |
| 4.6 | Stash save / apply / pop / drop | PASS | including "stage new files" and "delete after applying" | — | — |
| 4.7 | git-flow init | PASS | `develop` created, `gitflow.*` written. Writes `gitflow.path.hooks = Z:/…/.git/hooks` into the repo config | Windows path persisted in a shared repo config | (c) document (native git-flow would read a bogus path) |
| 5.1 | Add remote, Test Connection (HTTPS) | PASS | `gh` → github.com/octocat/Hello-World: "Connection succeeded" (TLS via bundled git) | — | — |
| 5.2 | Fetch / pull / push to a `file:///home/…` remote | **FAIL** → PASS with fix | `fatal: 'C:/users/<u>/AppData/Local/Fork/gitInstance/2.50.1/home/…/remote.git' does not appear to be a git repository`; background auto-fetch fails silently every 10 min. With `url."file:///Z:/home/".insteadOf=file:///home/`: fetch (new remote branch), pull (remote commit), push (+tracking, +tags) all verified | msys maps `/home/…` under its own root; Linux git writes Linux paths into `remote.*.url` | (a) add `insteadOf` rewrites to the managed git overlay for `file:///<top>/` and bare `/<top>/` (home, mnt, media, srv, opt, tmp, var, run) |
| 5.3 | Clone dialog | PASS* | HTTPS clone of Hello-World and a `file://` clone (with 5.2's fix) succeed. Default parent folder is inside the prefix (1.11). Fork-created repos get `core.symlinks=false`, `core.ignorecase=true` | Git for Windows probes the Wine FS at init/clone | (a) doctor flags `core.ignorecase=true` / `core.symlinks=false` in Linux repos and offers to unset them; (b) the bridge removes it |
| 5.4 | Credentials | PARTIAL | `Fork.AskPass.exe` asks for username/password (cancelled, no real credentials). Git Credential Manager fails: `Failed to enumerate credentials. [0x3ec]` / `Invalid flags`; nothing can be stored | Wine's `CredEnumerateW` rejects GCM's flags (ERROR_INVALID_FLAGS) | (a) overlay `credential.credentialStore=dpapi`: verified to get past enumeration. Later (b): host libsecret helper through the bridge |
| 5.5 | Error-dialog noise | PARTIAL | every network git error starts with a 6-line `Cygwin WARNING: Couldn't compute FAST_CWD pointer` | msys2 runtime under Wine | (c) document; it is cosmetic |
| 6 | Merge-conflict resolver | PASS | conflict banner, Merge window (pick side), Resolve marks it resolved, merge commit with 2 parents; Abort clears `MERGE_HEAD` | — | — |
| 7.1 | Submodules | PARTIAL | sidebar, open, context menu work. **Update / init silently does nothing** (rc 0, no dialog): `git submodule update --init` via bundled git leaves the submodule uninitialised | `git-submodule` is still an msys `sh` script; msys `sh` dies after its first child (7.4) | (b) bridge; (a) doctor warning for repos with `.gitmodules` |
| 7.2 | Worktrees | PARTIAL | create / open (Fork opens it as a tab) / delete work in Fork. Native git then sees `…/.git/worktrees/qa-wt/Z:/home/…  prunable`; inside the worktree native git says `fatal: not a git repository` | Git for Windows writes absolute `Z:/…` gitdir links | (a) export `worktree.useRelativePaths=true` **only when host git ≥ 2.48**: it sets `extensions.relativeWorktrees`, which older git (jammy 2.34, noble 2.43) refuses. Verified it fixes interop on 2.53 |
| 7.3 | Git LFS | PASS | bundled git-lfs 3.7.0: track, clean filter (pointer committed), `pre-push` uploaded the object to a `file://` remote. Host `git-lfs` is absent (irrelevant for bundled git) | the LFS hooks call one program each, so 7.4 does not bite | — |
| 7.4 | Hooks (pre-commit running a program) | **FAIL (critical)** | Fork's commit **succeeded** and the hook's log file never appeared. Reproduced with bundled git: a hook doing `ls; …; exit 1` lets the commit through (exit 0). A hook with only builtins and `exit 1` does block ("blocked by hook"). `sh -c 'date; echo B1'` prints the date and dies | msys `sh` dies, with status 0, after its first child exits under Wine 11 staging (bug 55138 family; spike S6 saw `Bad address`). Policy hooks are **silently bypassed**, which is worse than failing | (b) bridge (run hooks with Linux sh). Interim (a): doctor + a first-open warning for repos with executable hooks; README warning |
| 8.1 | Help menu links | PASS | Website, Issue Tracker, Keyboard Shortcuts and Release Notes each went through `fork-linux-open-url` → `gdbus … org.freedesktop.portal.OpenURI` with the right URL | — | — |
| 8.2 | Open in Terminal (Shell button) | **FAIL** | the click does nothing, with no error. `fl-launch.exe terminal` prints `FL_BRIDGE_PORT is not set: start Fork through fork-linux with the git bridge enabled` (rc 125) | `ShellTool` is set to `fl-launch.exe terminal` unconditionally, but the daemon it needs is not started (`bridge.launch_env()` returns `{}` until Phase 7, and only for `[git] bridge=on`) | (a) set `ShellTool` only when the daemon will run, and start the daemon for host actions even with the git bridge off |
| 8.3 | Open Git Bash | FAIL | `mintty` shows "Font not found, using Lucida Console", then exits | msys child spawning (7.4) | (b)/(c) hide or document; the Shell button replaces it |
| 8.4 | Show in Explorer / Open in File Explorer | PARTIAL | first call opened **Wine's** `explorer.exe Z:\…` (not the Linux file manager); later calls did nothing | Fork runs `explorer.exe <path>` directly, so the `Folder\shell\open` redirect never applies | (a) needs a Wine-side redirect of `explorer.exe <dir>` to `fl-launch reveal` (IFEO `Debugger` was inconclusive here) |
| 8.5 | External diff / merge tools | FAIL | Preferences lists only Windows tools. The wrapper writes `ExternalMergeTool`, but Fork 2.23 stores **`MergeTool`** (`ExternalDiffTool` is right); `ExternalDiffTools`/`ExternalMergeTools` are empty | wrong key name; no Linux tool seeded | (a) fix `fork_settings.EXTERNAL_MERGE_TOOL = "MergeTool"`; seed a Custom `fl-launch.exe diff/merge` entry once 8.2 works |
| 8.6 | Preferences (all tabs, persistence) | PASS | General, Commit, Git, Integration, Custom Commands and Updates all open; spell mode and update channel survive a restart. Update channel mapping: **0 = Develop (Fork's default), 1 = Stable (delayed 1 week), 2 = Off** | — | (a) use this mapping for `ApplicationUpdateType`; `fork-linux update --check` still prints "Updates: automatic" while Fork is set to Off |
| 8.7 | About, Check for updates | PASS | About shows 2.23.2.0 and the authors; the stable feed check reports "No Updates Available" | — | — |
| 8.8 | Notifications | N/T | Fork for Windows shows in-app activity only; it has no OS toasts to check | — | — |
| 9.1 | Close / reopen | PASS | File > Exit is clean (no Wine processes left); tabs and settings restored | — | — |
| 9.2 | Large repo (5,001 commits, 5,898 files) | PASS | opened in ~2.4 s including forwarding; refs refresh 28 ms; status 0.3 s; 100 modified files shown within 5 s | — | — |
| 9.3 | Memory / idle CPU | PASS | Fork.exe RSS 320–430 MB; whole prefix ~2% CPU idle; two `mscorsvw.exe` stay resident (0% CPU, ~45 MB) | .NET optimisation service | (c)/(a) optional: disable `clr_optimization_v4*` services after setup |
| 9.4 | Crashes / exceptions | PASS | no crash or WineDbg dialog. `fork.log` errors were only the ones above | — | — |
| 9.5 | Log retention | FAIL | `fork.log` is overwritten at every Fork start, so the evidence of the previous session is gone | Fork behaviour | (a) the launcher copies the previous `fork.log` to `~/.local/state/fork-linux/logs/fork-<ts>.log` (rotate, keep N) before exec; `logs --fork` / `doctor` read the history |
| 10.1 | `status` / `version` / `about` | PASS | correct; credits and disclaimer present | `about` hard-codes "$59.99 … 3 machines" | (a) drop the price (it goes stale) |
| 10.2 | `settings show`, `snapshot list`, `ssh status`, `update --check --offline`, `logs` | PASS | snapshot `2.23.2-…` (hardlink); `logs` prints nothing because `wine-last.log` is empty with `WINEDEBUG=-all` | — | (a) `logs` could say "empty (WINEDEBUG=-all)" |
| 10.3 | `desktop status` | PARTIAL | entries ok, but `Exec=`/`TryExec=` point to the `fork-linux` found on `PATH` (the real `~/.local/bin/fork-linux`), not the install that ran setup | launcher path resolved from `PATH` | (a) resolve from the running install (`resources.install_root()`) |
| 10.4 | `git-bridge status` | PARTIAL | "not built … missing: fl-bridge-helper", although `build-bridge/bridge/fl-bridge-helper` exists | `resources.libexec_dir()` looks in `libexec/` in a source tree | (a) also look in `build*/bridge` (as `shims_dir()` does) |
| 10.5 | `doctor` | PARTIAL | 29 ok, 0 warnings, while 3.1/3.2/5.2/7.4/8.2/1.11 were all present. `env.display` reported "wayland session, XWayland display :96" (it was Xvfb) | missing checks; session type read from the inherited `XDG_SESSION_TYPE` | (a) add `repo.*` checks over `repositories.toml` (symlinks, hooks, `.gitmodules`, `file://` or absolute-path remotes, `core.filemode=true`, `core.ignorecase=true`), `fork.shelltool_reachable`, `fork.source_dirs`; trust `DISPLAY` over `XDG_SESSION_TYPE` |
| 10.6 | Relaunch after a config change | FAIL at test time; fixed in tree since | a relaunch that re-ran `fork_settings` aborted with "Setting up Fork for Linux (unofficial): cancelled": zenity could not open the display (inherited `GDK_BACKEND=wayland` with no Wayland socket) and that was treated as a user cancel | `ui.py` `_send()` | the current `ui.py` now treats display errors as "continue without dialog" (`_DISPLAY_ERRORS`); re-verify. Also unset `GDK_BACKEND` when it does not match the available display |

## Prioritised fix list

### (a) Fixable in the wrapper (registry / settings / env / launcher / host helper)

1. **Remote paths** (5.2, 5.3): in `src/fork_linux/gitconfig.py`, add a generated block to the
   managed overlay (`gitconfig-host`) with `[url "file:///Z:/<top>/"] insteadOf = file:///<top>/`
   and `[url "Z:/<top>/"] insteadOf = /<top>/` for the host's top-level directories. Verified
   end to end for fetch, pull, push, tag push, `file://` clone and LFS upload.
2. **Shell button** (8.2): in `src/fork_linux/fork_settings.py` `desired()`, set `ShellTool` to
   `fl-launch.exe terminal` only when `bridge.launch_env()` provides `FL_BRIDGE_PORT`, or make
   `launcher.py` always start the helper daemon for host actions. Until then keep Fork's default
   (`null`). Also seed `ExternalDiffTool`/`MergeTool` with `fl-launch.exe diff|merge` on the same
   condition.
3. **Wrong settings key** (8.5): `fork_settings.EXTERNAL_MERGE_TOOL` must be `"MergeTool"` (Fork
   2.23.2 writes `MergeTool`; `ExternalMergeTool` does not exist). Add a contract test from a
   real `settings.json` key list.
4. **HiDPI double scaling** (1.4): `src/fork_linux/steps/display.py` + `fork_settings._scaling_value()`:
   apply either `LogPixels` or `LayoutScaling`, never both. `LayoutScaling` alone keeps Wine
   dialogs at 96 DPI; `LogPixels` alone scales Wine dialogs too. Pick one and test both at 144.
5. **Default source folder** (1.11): write `source_dirs` in
   `AppData/Local/ForkData/repositories.toml` (new small TOML-subset writer, Fork closed, backup
   first, unknown keys kept). Add the file to hard rule 3 and AGENTS.md §1 / §4.3. This keeps
   clones out of the prefix (purge safety).
6. **Credentials** (5.4): add `credential.credentialStore = dpapi` to the overlay (verified: it
   gets past `0x3ec`). Document that GCM's browser and OAuth flows are untested under Wine.
7. **Doctor coverage** (10.5) in `src/fork_linux/doctor.py`: per-repository checks over
   `repositories.toml` (executable hooks → **error-level warning** because of 7.4; symlinks;
   `.gitmodules`; Linux-path remotes; `core.filemode=true`; `core.ignorecase=true` /
   `core.symlinks=false` written by Git for Windows; absolute `Z:/` worktree links), plus
   `fork.shelltool_reachable` and `fork.source_dirs`. Offer `--fix` actions that change user
   repos only with explicit consent (`core.filemode=false`, `skip-worktree` for symlinks).
8. **Worktree interop** (7.2): in `gitconfig.env_overrides()`, add
   `worktree.useRelativePaths=true` only when the host git is ≥ 2.48 (older git rejects
   `extensions.relativeWorktrees`).
9. **Log retention** (9.5): in `src/fork_linux/launcher.py`, copy the previous
   `AppData/Local/Fork/logs/fork.log` to `$XDG_STATE_HOME/fork-linux/logs/fork-<ts>.log`
   (rotate, read-only on Fork's file) before exec. Make `logs --fork`, `logs --bundle` and
   `doctor fork.log_signatures` read the history.
10. **Open / reveal** (3.9, 8.4): once fl-launch works, add a `ForkLinux.File` ProgID with
    `shell\open\command` → `fl-launch.exe open "%1"` for common extensions (registry batch in
    `steps/prefix.py`), and find a working redirect for `explorer.exe <dir>`.
11. Smaller items: map `ApplicationUpdateType` 0/1/2 = Develop/Stable/Off in `update` and
    `settings` (8.6); resolve the `.desktop` `Exec` from the running install (10.3); look for
    `fl-bridge-helper` in `build*/bridge` in a source tree (10.4); add CJK/symbol font fallbacks
    (1.2); drop the hard-coded price from `about` (10.1); tell the user at the end of setup that
    the first Fork start takes ~1 min (1.1); unset a mismatched `GDK_BACKEND` before zenity
    (10.6).

### (b) Needs the native-git bridge

* **Hooks silently bypassed** (7.4): the most serious finding. Any hook that runs more than one
  program does not run to completion and reports success. Only Linux `git` running Linux `sh`
  fixes it.
* **Symlinks** (3.2, and so rebase in 4.3): bundled git cannot see Unix symlinks; status is
  wrong and commits through bundled git turn links into files (spike S6).
* **Submodule update / init** (7.1) and every other msys-`sh` code path (`git-bash`, bash custom
  commands, scripted git subcommands).
* **Repo config pollution by Git for Windows** (`core.ignorecase=true`, `core.symlinks=false`,
  absolute `Z:/` worktree links, `gitflow.path.hooks = Z:/…`) disappears with Linux git.
* Credential storage through the host keyring (libsecret) instead of DPAPI-in-prefix.
* Note: Fork's **in-process status engine** does not go through git at all. Even with the
  bridge, the exec bit (3.1) and symlink (3.2) display in the Local Changes list depend on Fork's
  own reader. Verify this when the bridge lands (`FORKGITINSTANCE` may or may not change it).

### (c) Upstream Wine / Fork limitations: document with workarounds

* Spell checking does nothing (3.8): keep "Disable".
* Typed Unix paths in Wine file dialogs fail (1.9): type `Z:\home\…` or browse via `/`.
* `Cygwin WARNING: Couldn't compute FAST_CWD pointer` prefix in git error dialogs (5.5):
  cosmetic.
* Maximize needs a real window manager (1.5): headless/Xvfb E2E should resize with xdotool
  instead.
* Wine's own dialogs (file picker, About) ignore Fork's dark theme (1.3).
* `git-flow` stores `gitflow.path.hooks` as a `Z:/` path (4.7).
* Exec-bit noise (3.1) until doctor's consented fix: `git config core.filemode false` per repo.

## Fixes applied (2026-10-09)

Wrapper fixes for the "(a)" list plus the follow-up scope (A–G), verified against the same isolated
QA install (`~/.cache/fork-linux-audit`, private Xvfb `:96`, Fork 2.23.2, Wine 11.0 staging), with
the CLI run from `src/`. Screenshots stay in the scratch root (`shots/fix*.png`, `inv-*.png`).
Host-action chains were checked with logging stubs for `gdbus` / `xdg-open` and a stub terminal
(`FORK_LINUX_TERMINAL`), so nothing opened on the real desktop; each stub recorded its arguments,
cwd, cgroup and `/proc/self/status`.

| # | Now | Change | Evidence |
|---|---|---|---|
| 5.2 / 4.5 / 5.3 | **PASS** | `gitconfig` overlay ends with a managed block: `[url "file:///Z:/<top>/"] insteadOf = file:///<top>/` and `[url "Z:/<top>/"] insteadOf = /<top>/` for each existing `home, mnt, media, srv, opt, tmp, var, run`; overlay revision 2 (step `git_overlay` rev 2, launcher signature) regenerates it on existing installs | Fork's own **Fetch** on `main` (remote `file:///home/…/remote.git`) brought the new `origin/qa/fixcheck`; no "does not appear to be a git repository" in `fork.log`. Bundled git with the launcher env: `push` to a `file:///home/…` remote and `ls-remote` / `fetch` from a bare `/home/…` remote all succeed. `fl-qa-gitprobe.sh` (now sets `WINEHOME` like the launcher) lists the refs without an explicit rewrite |
| 5.4 | **PASS (storage reachable)** | overlay adds `[credential] credentialStore = dpapi` (last, so it wins over a translated host value) | `git credential-manager get` no longer fails with `Failed to enumerate credentials [0x3ec]`; it reaches the network / prompt stage ("Cannot prompt because user interactivity has been disabled"). OAuth/browser flows still untested |
| 8.2 | **PASS** | `ShellTool` = `Z:\…\libexec\fork-linux-terminal` (a `#!/bin/sh` script Wine starts directly; Fork passes the repo as cwd) → `fork-linux-handoff` → `systemd-run --user --collect --no-block -p KillMode=process` → `fork-linux-host terminal DIR`. `fl-launch.exe` only when `bridge.host_actions_active()` (`FL_BRIDGE_PORT` from `launch_env`, never today); an existing dead `fl-launch.exe` setting is replaced (or reset to `null` when no script exists) | the audit install's `ShellTool` (`fl-launch.exe terminal`) became `fork-linux-terminal` on the next launch. **Open In Shell** (Ctrl+Alt+T): the stub terminal ran with cwd `…/repos/main`, `NoNewPrivs: 0`, `Seccomp: 0`, cgroup `app.slice/run-p…service` (direct fallback inside Wine's tree showed `NoNewPrivs: 1`) |
| 8.5 | **PASS (key) / remains (b)** | `EXTERNAL_MERGE_TOOL = "MergeTool"`; `ExternalDiffTool` / `MergeTool` get `fl-launch.exe diff|merge` only with the bridge daemon; our dead values go back to Fork's empty Custom tool; user tools untouched. Contract test `tests/test_settings_contract.py` pins the Fork 2.23.2 key list | settings keys checked against the real file; diff/merge through Linux tools work with the bridge (BR.10; Fork 2.23 also needs the `ExternalDiffTools` / `ExternalMergeTools` list entry) |
| 1.4 | **PASS** | tested both mechanisms at 144 DPI: `LogPixels=96 + LayoutScaling=150` and `LogPixels=144 + LayoutScaling=100` render Fork identically (1.5x, crisp), but only `LogPixels` also scales Wine's own dialogs (Open Repository file picker) and Fork's stored window size. Chosen: **`LogPixels` only**; fork-linux never writes `LayoutScaling`; step `fork_settings` rev 2 resets a `LayoutScaling` still equal to the value old builds wrote (here 150 → 100) | `shots/fix-main144.png`: tab bar at y≈125 px (84 at 96 DPI, 184 with the old double scaling); `fixA-*` / `fixB-*` are the A/B comparison, `fixB-open.png` the scaled Wine dialog |
| 1.11 / 1.7 | **PASS** | new `fork_data.py`: line-based editor of `ForkData\repositories.toml` that replaces only a default `source_dirs = ['C:\users\<u>\']` (backup `settings-backups/repositories.toml.<ts>`, Fork closed, no symlinks, other bytes kept); creates the file with just `source_dirs` when missing. Applied by step `fork_settings` and the pre-launch hook. AGENTS.md hard rule 3 now names `settings.json` and `repositories.toml` | Preferences > General "Source Code Folder" and Clone's "Parent Folder" show `Z:\home\…\fork-linux-audit\home` (`shots/fix-prefs144.png`, `fix-clone.png`); Fork kept the edited file (repositories intact). A file seeded with only `source_dirs` before start was accepted: Fork added `scan_depth`, `ignore`, `repository` and showed the Linux home in Preferences (`shots/fix-prefs-seeded.png`) |
| 7.2 | **PASS** (host git ≥ 2.48) | `env_overrides` adds `worktree.useRelativePaths=true` when the cached host `git --version` ≥ 2.48 (cache `~/.cache/fork-linux/host-git-version.json`; no git → skipped; a user value wins) | Fork's environment had `GIT_CONFIG_KEY_2=worktree.useRelativePaths`; a worktree made by bundled git has `gitdir: ../sym/.git/worktrees/wt` and native git uses it |
| 9.5 | **PASS** | the launcher copies the previous `fork.log` to `~/.local/state/fork-linux/logs/fork-<UTC mtime>.log` before Fork starts (newest 10 kept, never twice) | `fork-20261009T110645Z.log` and `fork-20261009T112956Z.log` appeared on two consecutive launches |
| 10.5 | **PASS** | `env.display` trusts `DISPLAY` / `WAYLAND_DISPLAY` over `XDG_SESSION_TYPE`; new checks `fork.tools` (dead `fl-launch.exe`, Git Bash; `--fix` restores defaults), `fork.source_dirs` (`--fix`), `prefix.integration`, `repo.hooks`, `repo.symlinks`, `repo.submodules`, `repo.remotes`, `repo.filemode` over `repositories.toml` + `settings.json` Workspaces; `git.overlay` flags a pre-fix overlay. Repo changes only with `--fix` **and** a confirmation (`repo.filemode`, `repo.symlinks`), or `fork-linux repo fix PATH` / `repo undo PATH` | on the QA fixtures with `XDG_SESSION_TYPE=wayland`: `env.display  X11 display :96`; warnings for `hooks (pre-commit)`, symlinks in `main` and `cloned-file`, submodules in `withsub`, `core.filemode = true` in 7 repos; `repo.remotes` ok (overlay rewrites `/home`) |
| A (8.4) | **PASS** | Fork calls `ShellExecute("explorer.exe", "/select, \"<file>\"")` for "Show in File Explorer" and `ShellExecute(<folder>)` (Wine falls back to `explorer`) for "Open In File Explorer" (`WINEDEBUG=+exec`). Step `host_integration` points `HKLM\…\App Paths\explorer.exe` and `\explorer` at `libexec/fork-linux-explorer` (Wine's shell32 consults App Paths first; Wine's `explorer /desktop` uses CreateProcess and is unaffected), which parses `/select,` `/e,` `/root,` and hands off `reveal|open` | "Show in File Explorer" on `code.py` → `gdbus … org.freedesktop.FileManager1.ShowItems ['file:///…/repos/main/code.py']` from a systemd unit (`NoNewPrivs: 0`); "Open In File Explorer" → `xdg-open …/repos/main` (`NoNewPrivs: 0`). No Wine explorer window |
| B (3.9) | **PASS** | Wine's `AssocQueryString` ignores `HKCR\*`, so `host_integration` registers `HKLM\Software\Classes\ForkLinux.File\shell\open\command = winebrowser.exe "%1"` and maps ~100 common extensions to it; `fork-linux-open-url` sends `file://` URLs through `systemd-run … xdg-open` (the portal's OpenURI refuses `file://`) and Windows paths to the host helper | Fork's **Open** item is enabled for `code.py` and runs `xdg-open file:///…/code.py` in a systemd unit (`NoNewPrivs: 0`). Files whose extension is not in the list stay greyed |
| C | **PASS** | see 8.2 | — |
| D (1.9) | **PARTIAL (workaround)** | `host_integration` adds `dosdevices/h:` → `$HOME` when `h:` is free; `PathMap` never picks the alias `H:` for Linux→Windows conversion (repositories keep their `Z:` identity). README: type `H:\…` or `Z:\home\…`. Typed `/home/…` still fails (the Wine dialog returns it verbatim). Wine links `Documents`, `Music`, … to the XDG folders itself when they exist; `Desktop` stays a real folder in the prefix | `dosdevices/h: -> …/fork-linux-audit/home`; `doctor prefix.integration` reports it |
| E (3.1 / 3.2) | **PARTIAL (opt-in)** | no Wine 11 staging switch presents Unix symlinks as reparse points (it stores reparse data in a `user.WINEREPARSE` xattr, which Linux symlinks cannot carry). Opt-in `fork-linux repo fix PATH` (asks; `--yes`) sets `core.filemode=false` and `skip-worktree` on tracked links, recorded as `forklinux.*` in the repo config; `fork-linux repo undo PATH` reverts exactly that; doctor `--fix` offers the same after confirmation | unit tests on real git repos; on the QA install `doctor` lists the affected repos (not changed without consent) |
| F (8.3) | **remains (b)** | a `%APPDATA%\mintty\config` with `Font=Courier New` removes the "Font not found" dialog, but mintty still exits at once without a window (msys child spawning under Wine). Not shipped. Git Bash is a ShellTool *type*; fork-linux's ShellTool replaces it, and `doctor fork.tools` warns when a user picks Git Bash | `inv-gitbash*.png`, `inv-mintty.png` |
| G | **PASS with the bridge (opt-in)** | hooks (7.4), submodule update (7.1), mode/symlink-preserving commits (3.2) and `file://` remotes work with `fork-linux git-bridge enable`; see "Native-git bridge in the launcher" below. Bundled git keeps the old limits | BR.4–BR.8 |

Still open from the "(a)" list: 1.1 (first-start message), 1.2 (CJK/symbol fallbacks), 4.7, 8.6
(`ApplicationUpdateType` 0/1/2 mapping in `update`/`settings`), 10.1–10.4, 10.6 re-check, 9.3, and
the `core.ignorecase` / `core.symlinks` / absolute `Z:` worktree doctor checks. Upstream / (c) items
are unchanged: spell checking (3.8), FAST_CWD noise (5.5), maximize without a WM (1.5), Wine
dialogs ignore the dark theme (1.3).

## Native-git bridge in the launcher (2026-10-09)

The experimental bridge (`bridge/`) is now wired into the Python runtime: setup step
`host_shims` (rev 2) installs `fl-shim.exe` as `C:\fork-linux\gitInstance\{bin,cmd,mingw64\bin}\git.exe`
and `{bin,usr\bin}\{bash,sh}.exe` plus `C:\fork-linux\bin\fl-launch.exe`; with `[git] bridge = on`
the launcher starts `fl-bridge-helper --daemon` before the pre-launch hooks (token in a fresh
`0600` file, `--parent-pid` = the launcher), exports `FORKGITINSTANCE` and the `FL_*` variables,
drops the bundled-git-only `GIT_CONFIG_*` overrides and execs Wine. Same isolated install as
above (`~/.cache/fork-linux-audit`, Xvfb `:96`, no WM, Fork 2.23.2, Wine 11.0 staging WoW64,
host git 2.53.0), bridge built with `scripts/build-bridge.sh`, CLI from `src/`. Terminal and
diff tools were stand-ins (a logging terminal stub that shows an `xmessage`, a `meld` stub that
logs and sleeps 6 s); screenshots `shots/br-*.png` stay in the scratch root.

| # | Check | Result | Evidence |
|---|---|---|---|
| BR.1 | `git-bridge enable` → next launch | **PASS** | `host_shims` re-ran (rev 2), daemon `pid 1509379 on 127.0.0.1:50629`; its `--parent-pid` and parent are the launcher pid, which is now `Fork.exe` (exec keeps the pid); `NoNewPrivs: 0`, `Seccomp: 0`; token file gone; `session.json` 0600 in the 0700 runtime dir |
| BR.2 | Second `fork PATH` while Fork runs | **PASS** | fast path reused the session's daemon (still one `fl-bridge-helper`), Fork opened the repository in a new tab |
| BR.3 | Fork's environment | **PASS** | `FORKGITINSTANCE=C:\fork-linux\gitInstance`, `FL_BRIDGE_PORT`, `FL_BRIDGE_TOKEN`, `FL_BRIDGE_{WINEXEC,ASKPASS,SSH_ASKPASS}` (links in `~/.local/share/fork-linux/bridge/bin`), `FL_WINE`; `GIT_CONFIG_*` holds only `worktree.useRelativePaths` (no `core.filemode=false` / `core.autocrlf=false`) |
| BR.4 | Pre-commit hook that runs `ls` / `uname` and exits 1, commit from Fork's UI | **PASS** (was 7.4 FAIL) | the commit is **blocked**; Fork's "Git Error" dialog shows the hook's stderr `QA-HOOK: lint failed on Linux (2 files checked) - commit blocked` and offers "Skip pre-commit hooks and commit"; native `git log` unchanged (`br-07-hookfail.png`) |
| BR.5 | Passing hook, stage + commit in Fork | **PASS** | hook log `pre-commit ran on Linux: 4 files`; commit created; `run.sh` stays `100755` in the index |
| BR.6 | Push to a `file:///home/…` remote from Fork's Push dialog | **PASS** | bare `origin.git` has the new commit; `main...origin/main` in sync |
| BR.7 | Interactive rebase from Fork's dialog (Drop) | **PASS** | Fork.RI.exe dialog through `fl-winexec`; the rebase session (66 s while the dialog was open) exited 0, the dropped commit is gone, no rebase state left (`br-16-ri.png`, `br-21-rebased.png`) |
| BR.8 | Submodule → Update 'libs/sub' (7.1) | **PASS** | `git submodule status` went from `-bf25b6d` to ` bf25b6d libs/sub (heads/main)`. A `file://` submodule URL needs `protocol.file.allow=always` in the user's git config, as with any git ≥ 2.38.1 |
| BR.9 | Shell / Console button | **PASS after fix** | `ShellTool` = `fl-launch.exe terminal`. Fork passes no argument (only the repo as cwd) and `fork-linux-host terminal` required one: fixed (`terminal [DIR]`, default the working directory). Now the configured terminal runs natively in the repository (`NoNewPrivs: 0`, `Seccomp: 0`, `git log` works; `br-24-console.png`) |
| BR.10 | External diff | **PASS after fix** | Fork 2.23 offers "Diff in <name>" (Ctrl+D) only for entries of its `ExternalDiffTools` list (`{"Type":"Custom","Name","Path","Arguments"}`, format read from the file Fork itself wrote); `ExternalDiffTool` alone shows nothing. fork-linux now adds one `Linux (fork-linux)` entry to `ExternalDiffTools` / `ExternalMergeTools` while the bridge runs and removes only that entry otherwise. `fl-launch.exe diff` with Fork's temp files → `meld` stub with Unix paths; Fork waited 6.07 s and got exit 0 |
| BR.11 | Fork exits | **PASS** | daemon gone within ~1 s (`daemon exit reason=signal`, PDEATHSIG) |
| BR.12 | `git-bridge disable` + relaunch | **PASS** | no daemon, no `FORKGITINSTANCE`, the `core.filemode=false` overlay is back; `ShellTool` → `fork-linux-terminal`, diff/merge tools → Fork's empty defaults, both tool lists empty again. Committing in the blocking-hook repo now **succeeds** with the hook silently skipped (bundled git, 7.4) |
| BR.13 | `doctor` with the bridge on | **PASS** | `bridge.status ok … Linux git 2.53.0; daemon running (pid …, port …)`; `repo.hooks` / `repo.submodules` turn `ok` (the bridge runs them) |
| BR.14 | Still as before | known | Fork's own Local Changes list still shows `run.sh` (exec bit) as modified with an empty diff (spike B3.6: status is computed in-process through Wine); `repo.filemode` / `repo.symlinks` keep warning |

Automated: `tests/bridge/test_launcher_wine.py` (`FL_REAL_WINE=1`) builds with
`scripts/build-bridge.sh`, installs the shims with the real step and runs the launcher's bridge
path (`start_daemon` → `build_spec` → `_exec`) with Wine running the bridged `sh.exe`: passes on
Wine 10.0 (14.5 s) and the pinned 11.0 staging (19.7 s); it checks that the daemon's parent is
the exec'd Wine process, that native `git --version` comes back, and that the daemon stops with
Wine.

## Things confirmed working well

History, graph, search, blame, file history, inline / side-by-side / image diffs, hunk and line
staging, discard, commit / amend / templates, branches, merge, interactive rebase (Fork.RI
window), cherry-pick, revert, reset, tags, stash, git-flow, the merge-conflict resolver and
abort, HTTPS remotes and clone, LFS, worktree management inside Fork, tabs, Quick Launch,
Repository Manager, single-instance forwarding, the dark theme, Help links through the XDG
portal, About / update check, settings persistence, a 5k-commit repo, and an idle CPU near zero.

## Reproduce

The audit above ran on the host. Since 2026-10-10 the QA scripts run **only inside a
container** (AGENTS.md hard rule 18): they refuse to start on the host. Run them through the
E2E runner; the scratch root is `/e2e/qa` inside, `/var/tmp/fork-linux-e2e/qa/qa` on the host
(screenshots in its `shots/`), and the downloads come from the runner's read-only seed.

```sh
QA="scripts/e2e-docker.sh --name qa --keep shell --"
$QA bash -c 'scripts/qa/fl-qa.sh init && scripts/qa/fl-qa.sh setup'   # isolated install, seeded caches
$QA bash -c 'scripts/qa/fl-qa.sh fixtures && scripts/qa/fl-qa.sh bigrepo'
$QA bash                                       # interactive: fl-qa.sh run /e2e/qa/repos/main, shot NAME, key, click, ...
$QA scripts/qa/fl-qa-gitprobe.sh               # GUI-free repro of 3.2, 5.2, 7.2, 7.4, 5.3, 5.4
```

Each `shell` container has its own Xvfb on `:99` and ends (with Wine) when its command exits.
