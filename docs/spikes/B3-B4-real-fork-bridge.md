# Spikes B3 + B4: the native-git bridge with the real Fork 2.23.2

Date: 2026-10-09. Host: Ubuntu 26.04, native git 2.53.0. Runtime: the managed
Kron4ek `wine-11.0-staging-amd64-wow64` (W11s). Fork 2.23.2 had already been installed and
launched in the spike-01 scratch prefix (fake `HOME`, Xvfb `:97`). Screenshots stayed in
the scratch directory, because they show Fork's logo (hard rule 4).

Components under test (built from this tree):

- `bridge/win/build-dev.sh` built `fl-shim.exe` and `fl-launch.exe`.
- `bridge/unix/build-dev.sh` built `fl-bridge-helper` (glibc PIE) and the `fl-winexec`,
  `fl-askpass` and `fl-ssh-askpass` persona symlinks.

The shims were copied into the prefix as `C:\fork-linux\gitInstance\{cmd,bin,mingw64\bin}\git.exe`,
`bin\{bash,sh}.exe` and `usr\bin\{bash,sh}.exe`, plus `C:\fork-linux\bin\fl-launch.exe`. A
shell script started the daemon natively:

```
fl-bridge-helper --daemon --token-file <0600 file> --log <file> --parent-pid <script pid>
```

The script stays in `wait`, so PDEATHSIG and `--parent-pid` both hold. Fork was then
launched with the following environment:

```
FORKGITINSTANCE='C:\fork-linux\gitInstance'
FL_BRIDGE_PORT  FL_BRIDGE_TOKEN  FL_BRIDGE_WINEXEC  FL_BRIDGE_ASKPASS  FL_BRIDGE_SSH_ASKPASS
FL_WINE  WINEPREFIX  FL_BRIDGE_LOG=<jsonl>
[record mode only] FL_BRIDGE_MODE=record
                   FL_BRIDGE_BUNDLED_GIT='C:\users\<user>\AppData\Local\Fork\gitInstance\2.50.1\cmd\git.exe'
```

In bridge mode the daemon's `PATH` started with a spike-only `git` wrapper. The wrapper
logged the argv and the `GIT_*`, `FORK_*`, `SSH_ASKPASS*` and `FL_*` variables of every
native git, then ran `exec /usr/bin/git`. This exposed values that the shim log redacts
(`SSH_ASKPASS*`).

Test repositories (all scratch):

| Repository | Contents |
|---|---|
| `execbit` | `run.sh` (mode 100755), `link.sh -> run.sh` (symlink) and a pre-commit hook |
| `hooktest` | A pre-commit hook that runs `uname -s > .git/hook-ran.txt` and `ls` |
| `rebasetest` | Four commits, with a local bare `origin` added during the run |

The sanitized corpus is in [`bridge/tests/corpus/fork-2.23.2.jsonl`](../../bridge/tests/corpus/fork-2.23.2.jsonl):

- 11 record-mode calls and 31 bridge-mode calls.
- Placeholders replace scratch paths and the user name: `<repos>`, `<prefix>`, `<libexec>`,
  `<user>`, `<locale>` and `<fork-pid>`.
- `t_ms` is the time relative to the start of each session.

[`tests/bridge/test_corpus.py`](../../tests/bridge/test_corpus.py) keeps the corpus well formed
and sanitized. It also pins two facts: no Windows path reaches git, and the rebase editor
is wrapped with `fl-winexec`.

> **Prefix gotcha (not a bridge issue):** the scratch-prefix override
> `WINEDLLOVERRIDES="mscoree,mshtml="` stops Fork from starting at all. Fork.exe is an
> IL-only binary, and without `mscoree` the result is `status c0000135` with no output and
> exit code 0. Fork prefixes must keep `mscoree`. Only `winemenubuilder.exe=d` belongs in
> the override (hard rule 12).

## Results

| # | Item | Verdict | Evidence |
|---|---|---|---|
| B3.1 | Fork honours `FORKGITINSTANCE` | **PASS** | Every call went to the shim. Fork calls **`<instance>\bin\git.exe`** and never `cmd\git.exe` or `mingw64\bin\git.exe`. |
| B3.2 | Record mode forwards to bundled git unchanged | **PASS** | 11 calls, exit codes propagated (0/1/128), output shown by Fork (diffs, rebase, commit). |
| B3.3 | Corpus collected and sanitized | **PASS** | `bridge/tests/corpus/fork-2.23.2.jsonl`; see "What Fork runs" below. |
| B3.4 | History in bridge mode | **PASS** | Fork reads history itself (no git call), so history renders exactly as before. Commit details, branch list and the remote-tracking ref `origin/master` appear. The ref came from a fetch that succeeded **only through the bridge**: bundled git exited 128 on the Unix-path remote. |
| B3.5 | Local Changes in bridge mode (diffs, stage, unstage, commit) | **PASS** | Stage = `add --pathspec-from-file=- --pathspec-file-nul` with paths on **stdin**. Unstage = `reset -q --no-refresh HEAD --pathspec-from-file=-`. Diffs use `diff … -- <file>` and `diff --no-index -- /dev/null <new>` (exit 1, as with bundled git). Commit = `commit --file <repo>\.git\COMMITMESSAGE`. Push with `--force-with-lease --set-upstream` to the local origin worked. |
| B3.6 | Exec-bit file and symlink shown clean | **FAIL (not fixable by the bridge)** | Native git through the shim reports the repository clean: `status --porcelain` is empty, and the diffs Fork requests for `run.sh` / `link.sh` come back **empty**. Yet Fork still lists both files under *Local Changes (2)*, in record mode and bridge mode alike. **Fork computes working-tree status in-process**: there is no `git status` call in either corpus, and 75 s idle (the 60 s auto-refresh) produced **zero** git calls. Setting `core.filemode=false` *in the repository's* `.git/config` removed `run.sh` from Fork's list. The same setting in the Wine user's global `.gitconfig` did not, because the repo-level `filemode=true` written by a Linux `git init` wins. The symlink stays "modified": Wine presents a symlink as its target file. |
| B3.7 | Pre-commit hook that runs a program, committed through the Fork UI | **PASS** | Commit button in Fork → `git commit --file …` through the bridge → the hook ran natively and wrote `Linux` to `.git/hook-ran.txt`. Its `ls` child worked; this is the case that fails with bundled msys bash (spike 01, Wine bug 55138). |
| B3.8 | `bash.exe` / `sh.exe` personas | **PASS** (manual) | `wine C:\fork-linux\gitInstance\bin\bash.exe -c 'uname -s; ls \| wc -l; git status --porcelain \| wc -l'` printed `Linux`, `3`, `0`. `usr\bin\sh.exe -c 'exit 7'` exited 7. Fork itself started neither persona in this run (no custom commands configured). |
| B3.9 | Translator gap found in the corpus | **FIXED** | Fork exports `GIT_CONFIG_SYSTEM=C:/users/<user>/AppData/Local/Fork/gitInstance/2.50.1/etc/gitconfig`, pointing at its **bundled** instance, not at `FORKGITINSTANCE`. The translator turned it into a Unix path, so native git would read Git for Windows' system config (`core.autocrlf=true`, `core.symlinks=false`, `core.fscache`, `credential.helper=manager`, `http.sslBackend=schannel`). `fl_translate_env` now **unsets** `GIT_CONFIG_SYSTEM` and `GIT_TEMPLATE_DIR` when they are Windows paths with a `gitInstance` component. Two new `env.tsv` vectors cover it, and 5 `argv.tsv` vectors replay Fork's real argv. The native env log confirms the variable is gone in bridge mode. |
| B4.1 | Interactive rebase **started from Fork's UI** through the bridge | **PASS** | *Interactive Rebase → Drop* on `commit 2`. Fork ran `git … -c sequence.editor=C:/…/Fork.RI.exe -c core.editor=C:/…/Fork.RI.exe rebase -i --autosquash --update-refs <base>`. The shim rewrote both editor values to `'<libexec>/fl-winexec' 'C:/users/<user>/AppData/Local/Fork/current/Fork.RI.exe'`. git ran the persona with the todo path, `fl-winexec` ran `wine Fork.RI.exe Z:\…\.git\rebase-merge\git-rebase-todo`, and the rebase finished with commit 2 dropped. It took 19.6 s, against 19.7 s for the same flow on bundled git: Fork.RI.exe is a .NET start under Wine. |
| B4.2 | `git rebase -i HEAD~2` **from a Linux shell** with `GIT_SEQUENCE_EDITOR="'<fl-winexec>' 'C:\users\<user>\AppData\Local\Fork\current\Fork.RI.exe'"`, `FORK_PROCESS_ID=<Fork's Windows pid>`, `WINEPREFIX`, `FL_WINE` | **FAIL (by Fork's design)** | `fl-winexec` started Fork.RI.exe with the translated Windows todo path. Fork's main window noticed the rebase ("Rebasing 'master' → '9b4d40d'", Resolve/Abort bar). **No interactive-rebase window appeared** in 90 s or in 75 s, with or without `FORK_REPOSITORY_PATH`. Fork.RI.exe is an IPC client of the Fork process and only completes a plan that Fork's own dialog prepared. Both attempts were aborted cleanly: Fork.RI.exe was killed, so git reported "problem with the editor" and left no rebase state, and a second attempt ended with `git rebase --abort`. The repository stayed clean. |
| B4.3 | What Fork passes for interactive rebase | recorded | `-c core.commentChar=^ -c rebase.instructionFormat=#!_%H -c rebase.abbreviateCommands=true -c sequence.editor=<Fork.RI.exe, forward slashes> -c core.editor=<same> rebase -i --autosquash --update-refs <sha>`. The environment also has `GIT_EDITOR=true` (Fork disables the commit-message editor), `FORK_PROCESS_ID` and `FORK_REPOSITORY_PATH`. |

## What Fork 2.23.2 runs (corpus summary)

- **Instance sub-path:** only `bin\git.exe` (42/42 calls). `cmd\`, `mingw64\bin\`,
  `bash.exe` and `sh.exe` were never used in these flows. The full install set must stay,
  because custom commands and other Fork versions may use them.
- **Read path:** history, refs, status and the auto-refresh all happen inside Fork, with no
  process. git.exe is used for diffs, writes (add, reset, commit, rebase, push, fetch) and
  config queries (`config --list --show-origin --name-only -z --system` at start-up, and
  `config commit.template` before each commit).
- **Calls per refresh:** 0 while idle. The 60 s automatic refresh and window focus run no
  git. On a file click Fork runs 1 `diff`. On commit Fork runs 2–3 calls.
- **Parallelism:** none observed. At most 1 call was in flight at a time in both sessions.
- **stdin:** `add` and `reset` take NUL-separated pathspecs on a pipe. Most other calls get
  an inherited `char` handle (Fork's own stdin). stdout and stderr are always pipes.
- **Long-lived processes:** only `rebase -i`, which waits ~20 s for Fork.RI.exe. No
  `cat-file --batch` or `fsmonitor` style daemons.
- **Environment Fork sets:**

  | Variable | Value |
  |---|---|
  | `FORK_PROCESS_ID` | Fork's Windows pid |
  | `FORK_REPOSITORY_PATH` | `Z:\…` backslash form, empty for the start-up config call |
  | `GIT_EDITOR` | `true` |
  | `GIT_CONFIG_SYSTEM` | Bundled `etc/gitconfig`, forward slashes, absent for the first call |
  | `SSH_ASKPASS` | `C:\users\<user>\AppData\Local\Fork\current\Fork.AskPass.exe` |
  | `SSH_ASKPASS_REQUIRE` | `force` |

  `SSH_AUTH_SOCK` is inherited from the Unix side. Fork set no `GIT_ASKPASS` in these flows,
  so credential prompts reach git through `SSH_ASKPASS`.
- **`-c` keys:** `core.quotepath=false` (diffs), `push.default=upstream` (push), and the
  rebase set above.
- **Direct non-git executables:** none started by Fork in these flows. Fork.RI.exe is
  started *by git* as the editor; Fork.AskPass.exe would be started by ssh/git.
- **Exit codes Fork tolerates:** `config commit.template` → 1 (unset). `diff --no-index` → 1.
  Native `config --system` → 128, because the host has no `/etc/gitconfig`; bundled git
  returned 0. Fork showed no error.
- **Timing (shim-measured, excluding the shim's own Wine start):**

  | Call | Record mode (bundled git under Wine) | Bridge mode |
  |---|---|---|
  | `diff` | 94–129 ms | 5–7 ms |
  | `commit` | 470 ms | 8–12 ms |
  | `push` | – | 16 ms |
  | `fetch` | – | 9 ms |

  The daemon log agrees (5–19 ms per session).

## Design consequences

1. **The bridge cannot fix Fork's Local Changes list for exec-bit or symlink files.** The
   list comes from Fork's in-process status engine, which reads the files through Wine.
   What the bridge does fix: diffs (empty, correct), staging and commits (native git keeps
   the mode and the symlink, so committing no longer turns a symlink into a file), hooks,
   custom shell commands, fetch/push to Unix-path remotes, and native credential/ssh tooling.
   Docs and the doctor must say so plainly. An **opt-in** per-repository
   `core.filemode=false` hides exec-bit noise in Fork, at the price of native git no longer
   tracking mode changes in that repository. It must stay a user decision; the wrapper
   never writes it silently, because it is the user's repo config. Symlinks need a Wine-side
   fix (reparse-point symlinks); keep the doctor warning for repositories with symlinks.
2. **Do not translate `FORK_*`.** `FORK_REPOSITORY_PATH` and `FORK_PROCESS_ID` are read by
   Windows programs that git starts (Fork.RI.exe, Fork.AskPass.exe) through `fl-winexec`.
   Those programs need the Windows form. Native hooks see a `Z:\…` path in
   `FORK_REPOSITORY_PATH`; document it rather than break Fork's helpers.
3. **`GIT_CONFIG_SYSTEM` / `GIT_TEMPLATE_DIR` into a gitInstance are unset** (fixed in
   `fl_translate.c`). Native git then uses the distribution's system config. The other
   values Fork's bundled config used to supply (`credential.helper=manager`, autocrlf) no
   longer apply under the bridge, which is the intended behaviour on Linux.
4. **Native git version floor: 2.38.** Fork passes `rebase --update-refs` (git ≥ 2.38) even
   with "Update dependent branches" unchecked. It also uses `--pathspec-from-file` /
   `--pathspec-file-nul` (≥ 2.26). Ubuntu 22.04 ships 2.34, so the doctor must refuse or
   warn about enabling the bridge with git < 2.38 there. Interactive rebase would otherwise
   fail.
5. **Interactive rebase works only when Fork starts it.** A shell-started `git rebase -i`
   with Fork.RI.exe as the editor hangs until timeout. Never point users' global
   `sequence.editor` at Fork.RI.exe, and never offer it as a terminal integration. Fork's
   own flows already work through the bridge.
6. **Installing only `bin\git.exe` would suffice for 2.23.2**, but the full persona set
   stays (cheap, and future-proof for custom commands / other versions).
7. **No concurrency pressure.** Fork serialises its git calls. The daemon's 256-session
   cap and per-connection fork model are far above need, and per-call latency is dominated
   by the shim's own Wine start, not by the bridge.
8. **Launcher:** a Fork prefix must not override `mscoree`. Pass `FL_BRIDGE_ASKPASS` /
   `FL_BRIDGE_SSH_ASKPASS`: Fork always sets `SSH_ASKPASS` + `SSH_ASKPASS_REQUIRE=force`,
   and without the persona the translator unsets `SSH_ASKPASS` and ssh prompts are lost.

## Open issues

- HTTPS credential prompts through `fl-ssh-askpass` → Fork.AskPass.exe are untested (no
  HTTPS remote in the scratch setup). Neither are ssh remotes, GPG/SSH signing, LFS or
  submodules.
- `config --list --system` exits 128 natively when `/etc/gitconfig` is missing. Fork
  tolerated it here; a later Fork feature that reads system config might not. A possible
  fix is to give the daemon `GIT_CONFIG_SYSTEM=/dev/null` only when `/etc/gitconfig` does
  not exist. That stays undecided until a Fork feature is seen to depend on it.
- Custom commands (`bash.exe` / `sh.exe` started by Fork) were not configured in this run.
  The personas were verified by hand only.
- Fork.RI.exe as `core.editor` (Reword) was not exercised separately. It is expected to
  behave like the sequence editor: it works when Fork starts it.
