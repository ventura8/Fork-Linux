"""The native-git bridge with the official Fork: enable / status / record / disable, the daemon's
life (reuse, a restarted Fork, exit after the last Fork), commits that keep exec bits and
symlinks, hooks, submodules, ssh through the agent or a passphrase prompt, the Linux diff and
merge tools, and the speed of a git call.

Needs the bridge built in the checkout (``scripts/build-bridge.sh``; ``ci-e2e-wine.sh`` builds it).
Gated by ``FL_E2E_FORK=1``; containers only.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fork_driver import fork_pids, git, processes, remotes_dir, wait_until, windows_path
from fork_ui import ASKPASS_FIELD, STAGE_BUTTON, ForkUI, new_repo
from git_servers import HttpGitServer, SshServer

from fixtures import real_tier

if os.environ.get("FL_E2E_FORK") != "1":
    pytest.skip("requires FL_E2E_FORK=1 (the official Fork under Wine)", allow_module_level=True)
real_tier.enforce()

REPO = Path(__file__).resolve().parents[2]
SHIM_GIT = "C:\\fork-linux\\gitInstance\\bin\\git.exe"
DAEMON_EXIT_SECONDS = 20.0
REMOTES = remotes_dir()
# The Linux tools the bridge's host actions start: a stand-in "meld" records what it was given.
MELD_STUB = """#!/bin/sh
log="{log}"
printf 'meld %s\\n' "$*" >>"$log"
for arg in "$@"; do
    case $arg in
        --output=*) out=${{arg#--output=}} ;;
        *) [ -f "$arg" ] && {{ printf -- '--- %s\\n' "$arg"; cat "$arg"; }} >>"$log" ;;
    esac
done
sleep 3
[ -n "${{out:-}}" ] && printf 'line 1\\nline 2 resolved in Linux\\nline 3\\n' >"$out"
printf 'meld done\\n' >>"$log"
exit 0
"""
# Inside the Local Changes view with a merge in progress (a banner pushes the list down).
CONFLICTED_FILE = (420, 186)
MERGE_IN_LINUX = (1028, 400)
SUBMODULES_HEADER = (90, 380)
FIRST_SUBMODULE = (120, 402)
# The submodule's menu: Open, Open Git Bash, Open In Shell, Show in File Explorer, History, Update...
SUBMODULE_MENU_UPDATE = 169


def _shims_built() -> bool:
    return (REPO / "build-bridge" / "bridge" / "fl-shim.exe").is_file()


@pytest.fixture(scope="module")
def bridged(installed: Any) -> Iterator[tuple[ForkUI, dict[str, str], Path]]:
    """The bridge enabled, Fork started with a stand-in meld first on PATH; disabled afterwards."""
    assert _shims_built(), "the bridge is not built: run scripts/build-bridge.sh before the E2E tier"
    stubs = installed.root / "bridge-stubs"
    stubs.mkdir(exist_ok=True)
    log = installed.root / "meld-calls.log"
    meld = stubs / "meld"
    meld.write_text(MELD_STUB.format(log=log), encoding="utf-8")
    meld.chmod(0o755)
    extra = {"PATH": f"{stubs}:{installed.env['PATH']}"}
    installed.fork.ensure_closed()
    installed.fork.forget_tabs()
    installed.cli("git-bridge", "enable")
    yield ForkUI(installed.fork), extra, log
    installed.fork.ensure_closed()
    installed.cli("git-bridge", "record", "off")
    installed.cli("git-bridge", "disable")


def _daemons() -> list[tuple[int, str]]:
    return processes(lambda cmd: "fl-bridge-helper" in cmd and "--daemon" in cmd)


def _shim_calls(e2e: Any) -> list[dict[str, Any]]:
    """Every git call the shims recorded (``run --debug`` writes ``bridge-calls-<time>.jsonl``)."""
    logs = e2e.home / ".local" / "state" / "fork-linux" / "logs"
    calls = []
    for path in sorted(logs.glob("bridge-calls-*.jsonl")):
        calls += [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return calls


def _repo(e2e: Any, name: str, files: dict[str, str] | None = None) -> Path:
    path = new_repo(e2e.repos / name, files)
    git(path, "config", "core.filemode", "true")
    return path


def _start(ui: ForkUI, repo: Path, extra: dict[str, str]) -> Any:
    """Open ``repo`` in the running Fork, or start Fork with ``--debug`` (the shims log every call)."""
    if fork_pids(ui.fork.prefix):
        return ui.open(repo)
    window = ui.fork.start(repo, args=("--debug",), extra=extra)
    ui.fork.focus(window)
    return window


def test_enable_starts_the_daemon_and_fork_uses_linux_git(bridged: Any, installed: Any) -> None:
    """With the bridge on, Fork's git is the shim, the daemon runs, and a commit is made by Linux git.

    Scenarios: B-ENABLE, B-STATUS
    """
    ui, extra, _log = bridged
    status = json.loads(installed.cli("git-bridge", "status", "--json").stdout)
    assert status["enabled"] is True and status["ready"] is True, status
    repo = _repo(installed, "bridge-commit", {"a.txt": "a\n"})
    ui.fork.ensure_closed()
    _start(ui, repo, extra)
    assert SHIM_GIT in ui.fork.fork_log()
    assert len(_daemons()) == 1
    running = json.loads(installed.cli("git-bridge", "status", "--json").stdout)
    assert running["daemon"], running
    (repo / "a.txt").write_text("a changed\n", encoding="utf-8")
    ui.refresh()
    ui.local_changes()
    ui.click(ui.unstaged_row(0))
    ui.click(STAGE_BUTTON)
    ui.commit("committed through the bridge")
    wait_until(lambda: git(repo, "log", "-1", "--format=%s") == "committed through the bridge" or None, 60,
               "the commit")
    commits = [call for call in _shim_calls(installed) if "commit" in call.get("argv", [])]
    assert commits and all(call["mode"] == "bridge" for call in commits), commits[-3:]


def test_commits_through_fork_keep_exec_bits_and_symlinks(bridged: Any, installed: Any) -> None:
    """Linux git sees real modes: a new executable and a new symlink are committed as such.

    Scenarios: B-EXEC-SYMLINK
    """
    ui, extra, _log = bridged
    repo = _repo(installed, "bridge-modes")
    script = repo / "run.sh"
    script.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    script.chmod(0o755)
    (repo / "link.md").symlink_to("README.md")
    _start(ui, repo, extra)
    ui.refresh()
    ui.local_changes()
    # Rows: link.md, run.sh. Fork's own status keeps listing a symlink as changed after it is
    # staged, so the lower row is staged first.
    ui.click(ui.unstaged_row(1), repeat=2)
    time.sleep(1.0)
    ui.click(ui.unstaged_row(0), repeat=2)
    wait_until(lambda: sorted(git(repo, "diff", "--cached", "--name-only").split()) == ["link.md", "run.sh"]
               or None, 60, "both files to be staged")
    ui.commit("script and link")
    wait_until(lambda: git(repo, "log", "-1", "--format=%s") == "script and link" or None, 60, "the commit")
    modes = {line.split()[-1]: line.split()[0] for line in git(repo, "ls-files", "-s").splitlines()}
    assert modes["run.sh"] == "100755"
    assert modes["link.md"] == "120000"
    assert git(repo, "status", "--porcelain") == ""


def test_failing_pre_commit_hook_blocks_the_commit(bridged: Any, installed: Any) -> None:
    """A pre-commit hook that runs programs and fails stops Fork's commit (Linux sh runs it).

    Scenarios: G-HOOK-BRIDGE
    """
    ui, extra, _log = bridged
    repo = _repo(installed, "bridge-hook", {"a.txt": "a\n"})
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text('#!/bin/sh\ndate >"$(git rev-parse --git-dir)/hook-ran"\necho "e2e hook says no" >&2\nexit 1\n',
                    encoding="utf-8")
    hook.chmod(0o755)
    head = git(repo, "rev-parse", "HEAD")
    (repo / "a.txt").write_text("a changed\n", encoding="utf-8")
    _start(ui, repo, extra)
    ui.refresh()
    ui.local_changes()
    ui.click(ui.unstaged_row(0))
    ui.click(STAGE_BUTTON)
    ui.commit("must be blocked")
    wait_until(lambda: (repo / ".git" / "hook-ran").is_file(), 60, "the hook to run")
    error = wait_until(lambda: ui.dialogs(), 60, "Fork's error dialog")
    ui.screen.screenshot("hook-blocked")
    assert error
    assert git(repo, "rev-parse", "HEAD") == head


def test_submodule_update_works(bridged: Any, installed: Any) -> None:
    """Update Submodule (sidebar) initialises and checks out a submodule with Linux git.

    Scenarios: G-SUBMODULE
    """
    ui, extra, _log = bridged
    sub = _repo(installed, "bridge-subproj", {"s.txt": "s\n"})
    subprocess.run(["git", "clone", "-q", "--bare", str(sub), str(REMOTES / "bridge-subproj.git")], check=True)
    # The submodule comes from a (public) HTTP server: git refuses file:// submodules since 2.38.1.
    with HttpGitServer(REMOTES, auth=False) as server:
        sup = _repo(installed, "bridge-super")
        git(sup, "submodule", "add", "-q", server.url("bridge-subproj.git"), "subrepo")
        git(sup, "commit", "-qm", "add submodule")
        subprocess.run(["git", "clone", "-q", "--bare", str(sup), str(REMOTES / "bridge-super.git")], check=True)
        clone = installed.repos / "bridge-super-clone"
        subprocess.run(["git", "clone", "-q", f"file://{REMOTES}/bridge-super.git", str(clone)], check=True)
        assert not (clone / "subrepo" / "s.txt").exists()
        window = _start(ui, clone, extra)
        ui.all_commits()
        ui.screen.click_in(window, *SUBMODULES_HEADER)
        ui.screen.key("Right", settle=1.0)
        ui.screen.screenshot("submodule-sidebar")
        before = ui.screen.windows()
        ui.screen.click_in(window, *FIRST_SUBMODULE, button=3)
        menu = ui.screen.new_window(before, timeout=10)
        ui.screen.screenshot("submodule-menu")
        ui.screen.click(menu.x + 60, menu.y + SUBMODULE_MENU_UPDATE, settle=2.0)
        if ui.dialogs():
            ui.screen.key("Return", settle=2.0)
        wait_until(lambda: (clone / "subrepo" / "s.txt").is_file(), 120, "the submodule checkout")


def test_git_flow_start_feature(bridged: Any, installed: Any) -> None:
    """Git Flow: Start Feature (git-flow's scripts, run by Linux git with git-flow installed).

    Scenarios: G-GITFLOW
    """
    ui, extra, _log = bridged
    repo = _repo(installed, "bridge-gitflow")
    _start(ui, repo, extra)
    ui.quick_launch("Git Flow")
    ui.wait_dialog("Initialize Git Flow")
    ui.screen.key("Return", settle=3.0)
    wait_until(lambda: git(repo, "branch", "--list", "develop"), 60, "the develop branch")
    ui.quick_launch("Git Flow: Start Feature")
    ui.wait_dialog("Start Git Flow feature")
    ui.screen.type("login")
    ui.screen.key("Return", settle=3.0)
    wait_until(lambda: git(repo, "branch", "--show-current") == "feature/login", 60, "the feature branch")
    assert git(repo, "rev-parse", "feature/login") == git(repo, "rev-parse", "develop")


def test_lfs_push_uploads_the_object(bridged: Any, installed: Any) -> None:
    """Push of an LFS repository: Linux git runs LFS's pre-push hook, so the object reaches the remote's LFS
    store with the commit. (Fork's bundled git runs that hook with msys ``sh``, which can hang under Wine.)

    Scenarios: G-LFS-PUSH
    """
    ui, extra, _log = bridged
    seed = _repo(installed, "bridge-lfs-seed", {"a.txt": "a\n"})
    bare = REMOTES / "bridge-lfs.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(seed), str(bare)], check=True, timeout=120)
    work = installed.repos / "bridge-lfs-work"
    subprocess.run(["git", "clone", "-q", f"file://{bare}", str(work)], check=True, timeout=120)
    git(work, "config", "user.name", "E2E Test")
    git(work, "config", "user.email", "e2e@example.invalid")
    git(work, "lfs", "install", "--local")
    (work / ".gitattributes").write_text("*.bin filter=lfs diff=lfs merge=lfs -text\n", encoding="utf-8")
    (work / "data.bin").write_bytes(os.urandom(4096))
    git(work, "add", ".gitattributes", "data.bin")
    git(work, "commit", "-qm", "add an lfs file")
    pointer = git(work, "show", "HEAD:data.bin")
    oid = next(line.split(":", 1)[1] for line in pointer.splitlines() if line.startswith("oid sha256:"))
    _start(ui, work, extra)
    ui.keys("ctrl+shift+p", settle=1.0)
    ui.wait_dialog("Push")
    ui.screen.key("Return", settle=2.0)
    head = git(work, "rev-parse", "HEAD")
    wait_until(lambda: git(bare, "rev-parse", "main") == head or None, 120, "the pushed commit on the remote")
    assert (bare / "lfs" / "objects" / oid[:2] / oid[2:4] / oid).is_file(), "the push left the LFS object behind"


def test_linux_diff_tool_runs_and_fork_waits(bridged: Any, installed: Any) -> None:
    """Diff in Linux (fork-linux), Ctrl+D on a changed file, opens the Linux diff tool on both versions.

    Scenarios: I-EXTERNAL-DIFF
    """
    ui, extra, log = bridged
    repo = _repo(installed, "bridge-diff", {"a.txt": "old line\n"})
    (repo / "a.txt").write_text("new line\n", encoding="utf-8")
    _start(ui, repo, extra)
    ui.refresh()
    ui.local_changes()
    ui.click(ui.unstaged_row(0))
    ui.keys("ctrl+d", settle=2.0)
    wait_until(lambda: log.is_file() and "meld done" in log.read_text(encoding="utf-8"), 60, "the diff tool")
    recorded = log.read_text(encoding="utf-8")
    assert "old line" in recorded and "new line" in recorded


def test_linux_merge_tool_resolves_a_conflict(bridged: Any, installed: Any) -> None:
    """Merge in Linux (fork-linux) runs the Linux merge tool; its result resolves the conflict.

    Scenarios: I-EXTERNAL-MERGE
    """
    ui, extra, log = bridged
    repo = _repo(installed, "bridge-merge", {"shared.txt": "line 1\nline 2\nline 3\n"})
    git(repo, "checkout", "-q", "-b", "other")
    (repo / "shared.txt").write_text("line 1\nline 2 other\nline 3\n", encoding="utf-8")
    git(repo, "commit", "-qam", "other")
    git(repo, "checkout", "-q", "main")
    (repo / "shared.txt").write_text("line 1\nline 2 main\nline 3\n", encoding="utf-8")
    git(repo, "commit", "-qam", "main")
    git(repo, "merge", "other", check=False)
    if log.exists():
        log.unlink()
    _start(ui, repo, extra)
    ui.refresh()
    ui.local_changes()
    ui.click(CONFLICTED_FILE)
    ui.click(MERGE_IN_LINUX)
    wait_until(lambda: log.is_file() and "meld done" in log.read_text(encoding="utf-8"), 60, "the merge tool")
    assert "--output=" in log.read_text(encoding="utf-8")
    wait_until(lambda: "resolved in Linux" in (repo / "shared.txt").read_text(encoding="utf-8"), 60,
               "the merged result")


def _ssh_setup(e2e: Any, server: SshServer, *, passphrase: str) -> tuple[Path, Path]:
    """A key (with ``passphrase``) and an ssh config the test passes to git (core.sshCommand)."""
    base = e2e.root / "bridge-ssh"
    base.mkdir(exist_ok=True)
    key = base / f"id_{'pass' if passphrase else 'plain'}"
    if not key.exists():
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", passphrase, "-f", str(key)], check=True,
                       timeout=60)
    host_key = (server.base / "host_ed25519.pub").read_text(encoding="utf-8").split()
    known = base / "known_hosts"
    known.write_text(f"[127.0.0.1]:{server.port} {host_key[0]} {host_key[1]}\n", encoding="utf-8")
    config = base / "config"
    config.write_text(f"Host e2e-bridge\n    HostName 127.0.0.1\n    Port {server.port}\n    User {server.user}\n"
                      f"    IdentityFile {key}\n    IdentitiesOnly yes\n    UserKnownHostsFile {known}\n",
                      encoding="utf-8")
    return key, config


def _ssh_remote(e2e: Any, name: str, config: Path) -> tuple[Path, Path]:
    seed = _repo(e2e, f"{name}-seed", {"a.txt": "a\n"})
    bare = REMOTES / f"{name}.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(seed), str(bare)], check=True)
    work = e2e.repos / f"{name}-work"
    subprocess.run(["git", "clone", "-q", str(bare), str(work)], check=True)
    git(work, "remote", "set-url", "origin", f"e2e-bridge:{bare}")
    git(work, "config", "core.sshCommand", f"ssh -F {config}")
    git(seed, "remote", "add", "origin", str(bare))
    (seed / "theirs.txt").write_text("theirs\n", encoding="utf-8")
    git(seed, "add", "theirs.txt")
    git(seed, "commit", "-qm", "their work")
    git(seed, "push", "-q", "origin", "main")
    return work, seed


def _fetch(ui: ForkUI) -> None:
    ui.keys("ctrl+shift+f", settle=1.0)
    ui.wait_dialog("Fetch")
    ui.screen.key("Return", settle=1.0)


def test_fetch_over_ssh_with_the_agent(bridged: Any, installed: Any) -> None:
    """Linux ssh with the user's ssh-agent: a passphrase key in the agent is used without a prompt.

    Scenarios: G-SSH-AGENT
    """
    ui, extra, _log = bridged
    server = SshServer(installed.root / "bridge-sshd-agent")
    authorized = installed.root / "bridge-agent-keys"
    agent = installed.root / "agent.sock"
    agent_process = subprocess.Popen(["ssh-agent", "-D", "-a", str(agent)], stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        wait_until(lambda: agent.exists(), 15, "the ssh agent")
        server.start(authorized)
        key, config = _ssh_setup(installed, server, passphrase="e2e agent passphrase")
        authorized.write_text(Path(f"{key}.pub").read_text(encoding="utf-8"), encoding="utf-8")
        askpass = installed.root / "askpass-agent.sh"
        askpass.write_text("#!/bin/sh\necho 'e2e agent passphrase'\n", encoding="utf-8")
        askpass.chmod(0o755)
        subprocess.run(["ssh-add", str(key)], env={**os.environ, "SSH_AUTH_SOCK": str(agent),
                                                   "SSH_ASKPASS": str(askpass), "SSH_ASKPASS_REQUIRE": "force",
                                                   "DISPLAY": installed.display}, check=True, timeout=30,
                       stdin=subprocess.DEVNULL)
        work, seed = _ssh_remote(installed, "bridge-agent", config)
        ui.fork.ensure_closed()
        ui.fork.start(work, extra={**extra, "SSH_AUTH_SOCK": str(agent)})
        _fetch(ui)
        wait_until(lambda: git(work, "rev-parse", "origin/main") == git(seed, "rev-parse", "HEAD"), 90,
                   "the fetch over ssh through the agent")
        assert not ui.dialogs(), "no passphrase prompt when the agent has the key"
    finally:
        ui.fork.ensure_closed()
        server.stop()
        agent_process.send_signal(signal.SIGTERM)
        agent_process.wait(timeout=30)


def test_fetch_over_ssh_with_a_passphrase_prompt(bridged: Any, installed: Any) -> None:
    """Without an agent, Fork asks for the key's passphrase (the bridge's ssh askpass) and fetches.

    Scenarios: G-SSH-PASSPHRASE
    """
    ui, extra, _log = bridged
    server = SshServer(installed.root / "bridge-sshd-pass")
    authorized = installed.root / "bridge-pass-keys"
    try:
        server.start(authorized)
        key, config = _ssh_setup(installed, server, passphrase="e2e prompt passphrase")
        authorized.write_text(Path(f"{key}.pub").read_text(encoding="utf-8"), encoding="utf-8")
        work, seed = _ssh_remote(installed, "bridge-pass", config)
        ui.fork.ensure_closed()
        ui.fork.start(work, extra=extra)
        _fetch(ui)
        prompt = wait_until(lambda: [w for w in ui.screen.any_windows() if not w.is_main], 90,
                            "the passphrase prompt")[0]
        ui.screen.screenshot("ssh-passphrase-prompt", prompt)
        # Fork's AskPass window does not take the keyboard by itself (no window manager): click its field.
        ui.screen.click_in(prompt, *ASKPASS_FIELD)
        ui.screen.type("e2e prompt passphrase")
        ui.screen.key("Return", settle=2.0)
        wait_until(lambda: git(work, "rev-parse", "origin/main") == git(seed, "rev-parse", "HEAD"), 90,
                   "the fetch after the passphrase")
    finally:
        ui.fork.ensure_closed()
        server.stop()


def test_daemon_is_reused_and_stops_after_fork(bridged: Any, installed: Any) -> None:
    """A second ``fork PATH`` reuses the running daemon; it exits a few seconds after Fork closes.

    Scenarios: B-DAEMON-LIFECYCLE
    """
    ui, extra, _log = bridged
    ui.fork.ensure_closed()
    _start(ui, _repo(installed, "bridge-life-1"), extra)
    first = _daemons()
    assert len(first) == 1
    forwarded = ui.fork.launch(_repo(installed, "bridge-life-2"), extra=extra)
    assert forwarded.wait(timeout=60) == 0
    assert _daemons() == first
    ui.fork.close()
    wait_until(lambda: not _daemons(), DAEMON_EXIT_SECONDS, "the daemon to exit after Fork")


def test_daemon_keeps_serving_a_restarted_fork(bridged: Any, installed: Any) -> None:
    """Fork restarted outside fork-linux (as its own updater does, same environment) keeps the daemon.

    Scenarios: B-DAEMON-RESTART
    """
    ui, extra, _log = bridged
    ui.fork.ensure_closed()
    repo = _repo(installed, "bridge-restart")
    _start(ui, repo, extra)
    daemon = _daemons()
    pid = fork_pids(ui.fork.prefix)[0]
    environ = dict(item.split("=", 1) for item in
                   Path("/proc", str(pid), "environ").read_bytes().decode(errors="replace").split("\0") if "=" in item)
    exe = Path("/proc", str(pid), "cmdline").read_bytes().split(b"\0")[0].decode(errors="replace")
    ui.fork.close()
    wine = json.loads((Path(installed.env["XDG_RUNTIME_DIR"]) / "fork-linux" / "session.json").read_text())["wine"]
    subprocess.Popen([wine, exe], env=environ, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)
    ui.fork.wait_main(timeout=120)
    assert _daemons() == daemon, "the same daemon serves the restarted Fork"
    (repo / "README.md").write_text("# restarted\n", encoding="utf-8")
    ui.refresh()
    ui.local_changes()
    ui.click(ui.unstaged_row(0))
    ui.click(STAGE_BUTTON)
    wait_until(lambda: git(repo, "diff", "--cached", "--name-only") == "README.md" or None, 60,
               "a git call through the daemon")


def test_git_calls_meet_the_latency_budget(bridged: Any, installed: Any) -> None:
    """A git call through the bridge is faster than Fork's bundled git under Wine.

    Scenarios: B-PERFORMANCE
    """
    ui, extra, _log = bridged
    repo = _repo(installed, "bridge-bench")
    _start(ui, repo, extra)
    session = json.loads((Path(installed.env["XDG_RUNTIME_DIR"]) / "fork-linux" / "session.json").read_text())
    env = {**installed.env, **session["bridge"]["env"], "WINEPREFIX": str(installed.prefix), "WINEDEBUG": "-all",
           "WINEDLLOVERRIDES": "winemenubuilder.exe="}
    driver = REPO / "build-bridge" / "bridge" / "tests" / "fl-testdriver.exe"
    bundled = next(iter(sorted((installed.fork.local_dir / "Fork" / "gitInstance").glob("*/bin/git.exe"))))
    cwd = windows_path(repo)

    def bench(exe: str) -> dict[str, Any]:
        result = subprocess.run([session["wine"], str(driver), "bench", "30", "--cwd", cwd, "--", exe, "rev-parse",
                                 "HEAD"], env=env, capture_output=True, text=True, timeout=600, check=False)
        line = next(item for item in reversed(result.stdout.splitlines()) if item.startswith("{"))
        return json.loads(line)

    through_bridge = bench(SHIM_GIT)
    bundled_git = bench(windows_path(bundled))
    assert through_bridge["ok"] == through_bridge["n"] and bundled_git["ok"] == bundled_git["n"]
    assert through_bridge["p50_ms"] < bundled_git["p50_ms"], (through_bridge, bundled_git)


def test_record_mode_forwards_to_bundled_git(bridged: Any, installed: Any) -> None:
    """``git-bridge record on``: the shims forward to Fork's bundled git (debugging), and git still works.

    Scenarios: B-RECORD
    """
    ui, extra, _log = bridged
    ui.fork.ensure_closed()
    installed.cli("git-bridge", "record", "on")
    assert json.loads(installed.cli("git-bridge", "status", "--json").stdout)["mode"] == "record"
    repo = _repo(installed, "bridge-record", {"a.txt": "a\n"})
    (repo / "a.txt").write_text("a changed\n", encoding="utf-8")
    _start(ui, repo, extra)
    ui.local_changes()
    ui.click(ui.unstaged_row(0))
    ui.click(STAGE_BUTTON)
    wait_until(lambda: git(repo, "diff", "--cached", "--name-only") == "a.txt" or None, 60, "the staged file")
    assert any(call.get("mode") == "record" for call in _shim_calls(installed))
    ui.fork.close()
    installed.cli("git-bridge", "record", "off")


def test_enable_refuses_an_old_host_git(bridged: Any, installed: Any) -> None:
    """``git-bridge enable`` needs host git 2.40 or newer: an older one is refused with the reason.

    Scenarios: B-GIT-FLOOR
    """
    stubs = installed.root / "old-git"
    stubs.mkdir(exist_ok=True)
    old = stubs / "git"
    old.write_text("#!/bin/sh\necho 'git version 2.34.1'\n", encoding="utf-8")
    old.chmod(0o755)
    result = installed.cli("git-bridge", "enable", extra={"PATH": f"{stubs}:{installed.env['PATH']}"}, check=False)
    assert result.returncode != 0
    assert "2.40" in result.stderr


def test_disable_goes_back_to_bundled_git(bridged: Any, installed: Any) -> None:
    """``git-bridge disable``: the next Fork uses its bundled git again and no daemon starts.

    Scenarios: B-DISABLE
    """
    ui, extra, _log = bridged
    ui.fork.ensure_closed()
    installed.cli("git-bridge", "disable")
    assert json.loads(installed.cli("git-bridge", "status", "--json").stdout)["enabled"] is False
    ui.fork.start(_repo(installed, "bridge-off"), extra=extra)
    assert SHIM_GIT not in ui.fork.fork_log()
    assert not _daemons()
    ui.fork.close()
