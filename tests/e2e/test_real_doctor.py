"""``fork-linux doctor`` on the real install: each ``--fix`` path, repository fixes with consent
and undo, missing host libraries, the network checks, the status and log commands and ``ssh sync``.

Every test breaks one thing the way it happens on a real machine, checks that doctor names it,
lets ``doctor --fix`` repair it and checks the result. Fork stays closed (fixes that touch Fork's
files need it closed). Gated by ``FL_E2E_FORK=1``; containers only.
"""

from __future__ import annotations

import json
import os
import re
import select
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from fork_driver import git, make_repo, windows_path

from fixtures import real_tier

REPO_ROOT = Path(__file__).resolve().parents[2]

if os.environ.get("FL_E2E_FORK") != "1":
    pytest.skip("requires FL_E2E_FORK=1 (the real install in a container)", allow_module_level=True)
real_tier.enforce()

CHECKS_FAILED = 17


def _check(e2e: Any, check_id: str, *extra: str) -> dict[str, Any]:
    """One doctor check's result (``doctor --check ID --json``)."""
    result = e2e.cli("doctor", "--json", "--check", check_id, *extra, check=False)
    report = json.loads(result.stdout)
    return next(item for item in report["checks"] if item["id"] == check_id)


def test_fix_resets_a_terminal_tool_without_a_daemon(closed: Any) -> None:
    """A Shell tool pointing at fl-launch.exe with no bridge daemon is flagged and reset by --fix.

    Scenarios: D-FIX-SHELL-TOOL
    """
    e2e = closed
    dead = json.dumps({"Type": "Custom", "ApplicationPath": "C:\\fork-linux\\bin\\fl-launch.exe",
                       "Arguments": "terminal"})
    e2e.cli("settings", "set", "ShellTool", dead)
    assert _check(e2e, "fork.tools")["status"] == "warn"
    e2e.cli("doctor", "--fix", "--check", "fork.tools")
    assert _check(e2e, "fork.tools")["status"] == "ok"
    assert "fl-launch.exe" not in json.dumps(e2e.fork.settings()["ShellTool"])


def test_git_bash_terminal_is_flagged_and_replaced(closed: Any) -> None:
    """Git Bash as Fork's terminal (it exits at once under Wine) is flagged; unset, the Linux one comes back.

    Scenarios: I-GIT-BASH
    """
    e2e = closed
    e2e.cli("settings", "set", "ShellTool", json.dumps({"Type": "GitBash", "ApplicationPath": "", "Arguments": ""}))
    result = _check(e2e, "fork.tools")
    assert result["status"] == "warn"
    assert "Git Bash" in result["detail"]
    e2e.cli("settings", "unset", "ShellTool")
    e2e.fork.start(make_repo(e2e.repos / "git-bash", {".gitignore": "*.tmp\n"}))
    e2e.fork.close()
    assert "fork-linux-terminal" in json.dumps(e2e.fork.settings()["ShellTool"])
    assert _check(e2e, "fork.tools")["status"] == "ok"


def test_fix_points_the_source_folder_at_the_home(closed: Any) -> None:
    """Fork's clone folder inside the prefix is flagged; --fix points it at the Linux home.

    Scenarios: D-FIX-SOURCE-DIRS
    """
    e2e = closed
    toml = e2e.fork.local_dir / "ForkData" / "repositories.toml"
    text = toml.read_text(encoding="utf-8")
    lines = [f"source_dirs = ['C:\\users\\{e2e.fork.user}\\']" if line.startswith("source_dirs") else line
             for line in text.splitlines()]
    toml.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert _check(e2e, "fork.source_dirs")["status"] == "warn"
    e2e.cli("doctor", "--fix", "--check", "fork.source_dirs")
    assert _check(e2e, "fork.source_dirs")["status"] == "ok"
    fixed = toml.read_text(encoding="utf-8")
    assert windows_path(e2e.home) in fixed
    backups = e2e.home / ".local" / "share" / "fork-linux" / "settings-backups"
    assert any(name.startswith("repositories.toml.") for name in os.listdir(backups)), "no backup was made"


def test_fix_deletes_exactly_our_menubuilder_leftovers(closed: Any) -> None:
    """Wine menu entries of our prefix are deleted by --fix; another prefix's entry is kept.

    Scenarios: D-FIX-MENUBUILDER
    """
    e2e = closed
    wine_apps = e2e.home / ".local" / "share" / "applications" / "wine" / "Programs"
    wine_apps.mkdir(parents=True, exist_ok=True)
    ours = wine_apps / "Fork.desktop"
    ours.write_text(f'[Desktop Entry]\nName=Fork\nExec=env WINEPREFIX="{e2e.prefix}" wine C:\\\\Fork.exe\n'
                    "Type=Application\n", encoding="utf-8")
    theirs = wine_apps / "Fork (other).desktop"
    theirs.write_text(f'[Desktop Entry]\nName=Fork\nExec=env WINEPREFIX="{e2e.home / ".wine"}" wine Fork.exe\n'
                      "Type=Application\n", encoding="utf-8")
    result = _check(e2e, "prefix.menubuilder")
    assert result["status"] == "warn"
    assert str(ours) in result['detail']
    assert str(theirs) not in result['detail']
    e2e.cli("doctor", "--fix", "--check", "prefix.menubuilder")
    assert not ours.exists()
    assert theirs.is_file()
    assert _check(e2e, "prefix.menubuilder")["status"] == "ok"
    theirs.unlink()


def test_fix_rewrites_a_missing_git_overlay(closed: Any) -> None:
    """A lost git config overlay is flagged; --fix re-runs the overlay step.

    Scenarios: D-FIX-OVERLAY
    """
    e2e = closed
    gitconfig = e2e.prefix / "drive_c" / "users" / e2e.fork.user / ".gitconfig"
    gitconfig.write_text("[user]\n\tname = Someone\n", encoding="utf-8")
    assert _check(e2e, "git.overlay")["status"] == "warn"
    e2e.cli("doctor", "--fix", "--check", "git.overlay")
    assert _check(e2e, "git.overlay")["status"] == "ok"
    restored = gitconfig.read_text(encoding="utf-8")
    assert "[include]" in restored
    assert "name = Someone" in restored, "the user's own lines must be kept"


def test_fix_reruns_the_setup_step_of_a_broken_check(closed: Any) -> None:
    """A registry value changed behind our back (Wine's DPI) is repaired by re-running its step.

    Scenarios: D-FIX-STEP
    """
    e2e = closed
    user_reg = e2e.prefix / "user.reg"
    text = user_reg.read_text(encoding="utf-8", errors="replace")
    assert '"LogPixels"=dword:' in text
    broken = []
    for line in text.splitlines(keepends=True):
        broken.append('"LogPixels"=dword:000000c8\n' if line.startswith('"LogPixels"=dword:') else line)
    user_reg.write_text("".join(broken), encoding="utf-8")
    assert _check(e2e, "prefix.dpi")["status"] == "warn"
    e2e.cli("doctor", "--fix", "--check", "prefix.dpi", timeout=900)
    assert _check(e2e, "prefix.dpi")["status"] == "ok"


def _problem_repo(e2e: Any) -> Path:
    """A repository Fork knows with core.filemode = true, an executable file and a tracked symlink."""
    repo = make_repo(e2e.repos / "doctor-repo", {".gitignore": "*.tmp\n", "README.md": "# doctor\n"})
    script = repo / "run.sh"
    script.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    script.chmod(0o755)
    (repo / "link.md").symlink_to("README.md")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "script and link")
    git(repo, "config", "core.filemode", "true")
    e2e.fork.start(repo)
    e2e.fork.close()
    return repo


def test_repo_fixes_need_consent_and_undo_reverts_them(closed: Any) -> None:
    """``repo check`` names a repository's problems; ``doctor --fix`` asks first; ``repo undo`` reverts.

    Scenarios: D-REPO-CHECK, D-REPO-FIX-UNDO
    """
    e2e = closed
    repo = _problem_repo(e2e)
    listed = e2e.cli("repo", "check", "--json").stdout
    assert str(repo) in listed
    assert 'filemode' in listed
    assert 'symlink' in listed
    # Nobody to ask (no terminal): doctor --fix changes no repository.
    declined = e2e.cli("--no-gui", "doctor", "--fix", "--check", "repo.filemode", "repo.symlinks", check=False)
    assert "not confirmed" in declined.stdout + declined.stderr
    assert git(repo, "config", "core.filemode") == "true"
    e2e.cli("repo", "fix", str(repo), "--yes")
    assert git(repo, "config", "core.filemode") == "false"
    assert git(repo, "ls-files", "-v", "link.md").startswith("S ")
    e2e.cli("repo", "undo", str(repo))
    assert git(repo, "config", "core.filemode") == "true"
    assert git(repo, "ls-files", "-v", "link.md").startswith("H ")


def test_missing_host_library_is_named_with_its_package(closed: Any) -> None:
    """A library the Wine runtime needs but the host lacks is named, with the package to install.

    Scenarios: S-MISSING-LIBS
    """
    e2e = closed
    stubs = e2e.root / "ldd-stub"
    stubs.mkdir(exist_ok=True)
    real_ldd = subprocess.run(["sh", "-c", "command -v ldd"], capture_output=True, text=True, check=True).stdout.strip()
    ldd = stubs / "ldd"
    ldd.write_text(f'#!/bin/sh\n"{real_ldd}" "$@"\necho "\tlibgnutls.so.30 => not found"\n', encoding="utf-8")
    ldd.chmod(0o755)
    result = e2e.cli("doctor", "--json", "--check", "host.libs", extra={"PATH": f"{stubs}:{e2e.env['PATH']}"},
                     check=False)
    report = next(item for item in json.loads(result.stdout)["checks"] if item["id"] == "host.libs")
    assert report["status"] == "fail"
    assert "libgnutls.so.30" in report["detail"]
    assert "libgnutls30" in (report.get("hint") or "")
    assert result.returncode == CHECKS_FAILED


def test_network_checks_reach_the_download_hosts(closed: Any) -> None:
    """``doctor --network`` reaches the download hosts and Fork's update feed.

    Scenarios: D-NETWORK
    """
    result = _check(closed, "network.reach", "--network")
    assert result["status"] == "ok", result


def test_status_version_and_about(closed: Any) -> None:
    """``status``, ``version`` / ``--version`` and ``about`` / ``credits`` describe the install and credit
    Fork's developers.

    Scenarios: D-STATUS
    """
    e2e = closed
    status = json.loads(e2e.cli("status", "--json").stdout)
    assert status["setup_complete"] is True
    assert status["prefix_exists"] is True
    expected = (REPO_ROOT / "VERSION").read_text(encoding="utf-8").strip()
    # The tier runs the source checkout, which reports VERSION with a "-dev" suffix.
    version = json.loads(e2e.cli("version", "--json").stdout)["fork_linux"]
    assert version in (expected, f"{expected}-dev"), version
    assert e2e.cli("version").stdout.startswith(f"fork-linux {version} ")
    assert e2e.cli("--version").stdout.startswith(f"fork-linux {version}")
    about = e2e.cli("about").stdout
    assert 'Dan Pristupov' in about
    assert 'https://git-fork.com/buy' in about
    assert "not affiliated" in about.lower()
    assert e2e.cli("credits").stdout == about


def test_logs_show_and_follow(closed: Any) -> None:
    """``logs --all`` shows fork-linux's and Fork's logs; ``logs --fork --follow`` prints new lines.

    Scenarios: D-LOGS-VIEW
    """
    e2e = closed
    fork_log = e2e.fork.local_dir / "Fork" / "logs" / "fork.log"
    shown = e2e.cli("logs", "--all").stdout
    # --all shows the end of each log: fork.log's header and its last line.
    last = fork_log.read_text(encoding="utf-8", errors="replace").strip().splitlines()[-1]
    assert f"==> fork: {fork_log} <==" in shown, "Fork's own fork.log is part of --all"
    assert last in shown
    follow = subprocess.Popen([sys.executable, "-m", "fork_linux", "logs", "--fork", "--follow"], env=e2e.env,
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, start_new_session=True)
    try:
        time.sleep(2)
        marker = f"E2E followed line {time.time_ns()}"
        with open(fork_log, "a", encoding="utf-8") as handle:
            handle.write(marker + "\n")
        assert _read_until(follow, marker, 30)
    finally:
        follow.terminate()
        follow.wait(timeout=30)


def _read_until(process: subprocess.Popen[str], text: str, timeout: float) -> bool:
    """True once ``process`` printed a line containing ``text`` (within ``timeout`` seconds)."""
    assert process.stdout is not None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ready, _, _ = select.select([process.stdout], [], [], 0.5)
        if ready and text in process.stdout.readline():
            return True
    return False


def test_config_get_set_unset_list_path_edit(closed: Any) -> None:
    """``config`` reads, changes (keeping comments) and resets fork-linux's settings; ``edit`` opens it.

    Scenarios: S-CONFIG-CLI
    """
    e2e = closed
    path = Path(e2e.cli("config", "path").stdout.strip())
    e2e.cli("config", "set", "snapshots.keep", "3")
    assert e2e.cli("config", "get", "snapshots.keep").stdout.strip() == "3"
    listed = e2e.cli("config", "list").stdout
    assert re.search(r"^snapshots\.keep\s+= 3\s+\(", listed, re.MULTILINE), listed
    refused = e2e.cli("config", "set", "snapshots.keep", "many", check=False)
    assert refused.returncode == 2
    editor = e2e.root / "e2e-editor.sh"
    editor.write_text('#!/bin/sh\nprintf "\\n# edited by the E2E editor\\n" >>"$1"\n', encoding="utf-8")
    editor.chmod(0o755)
    e2e.cli("config", "edit", extra={"EDITOR": str(editor), "VISUAL": str(editor)})
    assert "# edited by the E2E editor" in path.read_text(encoding="utf-8")
    e2e.cli("config", "unset", "snapshots.keep")
    assert e2e.cli("config", "get", "snapshots.keep").stdout.strip() == "2"
    assert "# edited by the E2E editor" in path.read_text(encoding="utf-8"), "set and unset must keep comments"


def test_repo_fix_can_leave_filemode_or_symlinks_alone(closed: Any) -> None:
    """``repo fix --no-filemode`` only hides the symlinks, ``--no-symlinks`` only sets ``core.filemode``;
    ``repo undo`` reverts each.

    Scenarios: D-REPO-FIX-PARTIAL
    """
    e2e = closed
    repo = make_repo(e2e.repos / "repo-fix-partial", {".gitignore": "*.tmp\n", "README.md": "# partial\n"})
    (repo / "link.md").symlink_to("README.md")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "link")
    git(repo, "config", "core.filemode", "true")
    e2e.cli("repo", "fix", str(repo), "--yes", "--no-filemode")
    assert git(repo, "config", "core.filemode") == "true"
    assert git(repo, "ls-files", "-v", "link.md").startswith("S ")
    e2e.cli("repo", "undo", str(repo))
    assert git(repo, "ls-files", "-v", "link.md").startswith("H ")
    e2e.cli("repo", "fix", str(repo), "--yes", "--no-symlinks")
    assert git(repo, "config", "core.filemode") == "false"
    assert git(repo, "ls-files", "-v", "link.md").startswith("H ")
    e2e.cli("repo", "undo", str(repo))
    assert git(repo, "config", "core.filemode") == "true"


def test_logs_of_each_source(closed: Any) -> None:
    """``logs --path`` names fork-linux's, Wine's, Fork's and Fork's updater's logs; ``logs --setup`` and
    ``logs --velopack`` show the end of theirs.

    Scenarios: D-LOGS-SOURCES
    """
    e2e = closed
    e2e.fork.start(make_repo(e2e.repos / "logs-sources", {".gitignore": "*.tmp\n"}))
    e2e.fork.close()
    located = json.loads(e2e.cli("logs", "--all", "--path", "--json").stdout)
    logs_dir = e2e.home / ".local" / "state" / "fork-linux" / "logs"
    fork_dir = e2e.fork.local_dir / "Fork"
    assert located["fork"] == [str(fork_dir / "logs" / "fork.log")], located
    assert located["velopack"] == [str(fork_dir / "velopack.log")], located
    assert [Path(path).parent for path in located["setup"] + located["wine"]] == [logs_dir, logs_dir], located
    assert Path(located["wine"][0]).name.startswith("wine-"), located
    own = Path(located["setup"][0])
    assert own.name.startswith(("fork-linux.log", "setup-")), located
    last = own.read_text(encoding="utf-8", errors="replace").strip().splitlines()[-1]
    assert last in e2e.cli("logs", "--setup").stdout
    velopack = (fork_dir / "velopack.log").read_text(encoding="utf-8", errors="replace").strip().splitlines()[-1]
    assert velopack in e2e.cli("logs", "--velopack").stdout


def test_ssh_sync_modes_dry_run_and_config(closed: Any) -> None:
    """``ssh sync --mode copy --no-config`` shares a private copy of a key and leaves the config out; ``--dry-run``
    only reports; a plain sync links the key and translates the config; ``ssh status`` shows both; a key
    deleted from ``~/.ssh`` leaves the prefix too.

    Scenarios: I-SSH-SYNC-OPTIONS
    """
    e2e = closed
    host_ssh = e2e.home / ".ssh"
    host_ssh.mkdir(mode=0o700, exist_ok=True)
    key = host_ssh / "id_e2e_sync"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "e2e-sync", "-f", str(key)],
                   capture_output=True, timeout=60, check=True)
    config = host_ssh / "config"
    config.write_text(f"Host e2e-sync\n  HostName 127.0.0.1\n  IdentityFile {key}\n  ControlMaster auto\n",
                      encoding="utf-8")
    wine_ssh = e2e.prefix / "drive_c" / "users" / e2e.fork.user / ".ssh"
    shared, wine_config = wine_ssh / key.name, wine_ssh / "config"
    try:
        copied = json.loads(e2e.cli("ssh", "sync", "--mode", "copy", "--no-config", "--json").stdout)
        assert str(shared) in copied['copied'], copied
        assert copied['config_path'] is None, copied
        assert not shared.is_symlink()
        assert shared.read_bytes() == key.read_bytes()
        assert stat.S_IMODE(shared.stat().st_mode) == 0o600
        assert not wine_config.exists()
        dry = json.loads(e2e.cli("ssh", "sync", "--dry-run", "--json").stdout)
        assert dry['dry_run'] is True, dry
        assert str(shared) in dry['linked'], dry
        assert not shared.is_symlink(), 'a dry run must change nothing'
        assert not wine_config.exists(), 'a dry run must change nothing'
        synced = json.loads(e2e.cli("ssh", "sync", "--json").stdout)
        assert str(shared) in synced["linked"], synced
        assert shared.is_symlink()
        assert os.readlink(shared) == str(key)
        text = wine_config.read_text(encoding="utf-8")
        assert text.startswith('# GENERATED by fork-linux'), text
        assert '# fork-linux: dropped' in text, text
        assert "~/.ssh/id_e2e_sync" in text, text
        status = json.loads(e2e.cli("ssh", "status", "--json").stdout)
        assert status['perms_ok'] is True, status
        assert status['config'] == 'current', status
        assert {key.name, "config"} <= set(status["managed"]), status
    finally:
        for path in (key, key.with_name(key.name + ".pub"), config):
            path.unlink(missing_ok=True)
    removed = json.loads(e2e.cli("ssh", "sync", "--json").stdout)
    assert str(shared) in removed['removed'], removed
    assert not os.path.lexists(shared), removed
    assert not os.path.lexists(wine_config)
