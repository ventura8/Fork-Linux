"""``fork-linux doctor`` on the real install: each ``--fix`` path, repository fixes with consent
and undo, missing host libraries, the network checks and the status commands.

Every test breaks one thing the way it happens on a real machine, checks that doctor names it,
lets ``doctor --fix`` repair it and checks the result. Fork stays closed (fixes that touch Fork's
files need it closed). Gated by ``FL_E2E_FORK=1``; containers only.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
from fork_driver import git, make_repo

from fixtures import real_tier

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
    assert "Z:" + str(e2e.home).replace("/", "\\") in fixed
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
    assert str(ours) in result["detail"] and str(theirs) not in result["detail"]
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
    assert "filemode" in listed and "symlink" in listed
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
    """``status``, ``version`` and ``about`` describe the install and credit Fork's developers.

    Scenarios: D-STATUS
    """
    e2e = closed
    status = json.loads(e2e.cli("status", "--json").stdout)
    assert status["setup_complete"] is True
    assert status["prefix_exists"] is True
    version = e2e.cli("version").stdout
    assert version.strip()
    about = e2e.cli("about").stdout
    assert "Dan Pristupov" in about and "https://git-fork.com/buy" in about
    assert "not affiliated" in about.lower()
