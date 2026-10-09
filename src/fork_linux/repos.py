"""The user's repositories as Fork knows them: what breaks under Wine, and opt-in repairs.

Fork lists its repositories in ``ForkData\\repositories.toml`` and its
workspaces in ``settings.json``. :func:`known` maps them back to Linux paths;
:func:`scan` asks the host's git (never Fork's bundled one) about each:

* executable hooks: Fork's bundled git runs hooks with msys ``sh``, which dies
  after its first child under Wine, so a hook that runs programs is silently
  skipped (QA 7.4);
* tracked symbolic links: Wine shows them to Windows programs as regular
  files, so Fork always lists them as modified (QA 3.2);
* ``.gitmodules``: ``git submodule update`` is an msys shell script (QA 7.1);
* remotes given as Linux paths that the git overlay does not rewrite (QA 5.2);
* ``core.filemode = true``: Fork computes the status itself and then shows
  every executable file as modified (QA 3.1).

:func:`fix` and :func:`undo` change a repository's own configuration only when
the user asked for it (``fork-linux repo fix`` or a confirmed
``doctor --fix``): ``core.filemode = false`` and ``skip-worktree`` for the
tracked links. What was changed is recorded in the repository's config
(``forklinux.*``) so :func:`undo` reverts exactly that.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import fork_data, fork_settings, gitconfig, sandbox
from .errors import ForkLinuxError, UsageError
from .fork_layout import ForkLayout
from .pathmap import PathMap
from .procrun import Runner

GIT_TIMEOUT = 30.0
SYMLINK_MODE = "120000"
MARK_FILEMODE = "forklinux.filemodefixed"
MARK_SKIP = "forklinux.skipworktree"
WORKSPACES = "Workspaces"


@dataclass
class RepoReport:
    """What :func:`scan` found in one repository."""

    path: Path
    error: str = ""
    hooks: list[str] = field(default_factory=list)
    symlinks: list[str] = field(default_factory=list)
    skipped_links: list[str] = field(default_factory=list)
    submodules: bool = False
    linux_remotes: list[str] = field(default_factory=list)
    filemode: bool = False

    @property
    def name(self) -> str:
        """The directory name, for short messages."""
        return self.path.name or str(self.path)


def _workspace_paths(settings: Mapping[str, Any]) -> list[str]:
    """Repository paths of every workspace in ``settings.json``."""
    found: list[str] = []
    workspaces = fork_settings.get(settings, f"{WORKSPACES}.All", [])
    for workspace in workspaces if isinstance(workspaces, list) else []:
        repos = workspace.get("Repositories") if isinstance(workspace, dict) else None
        found.extend(item for item in repos or [] if isinstance(item, str))
    return found


def known(layout: ForkLayout, pathmap: PathMap) -> list[Path]:
    """Existing repositories from ``repositories.toml`` and ``settings.json`` workspaces, as Linux paths."""
    text = fork_data.read_text(fork_data.path(layout.forkdata_dir)) or ""
    windows = fork_data.repositories(text) + _workspace_paths(fork_settings.load(layout.settings_file))
    found: list[Path] = []
    for win in windows:
        try:
            path = pathmap.win_to_unix(win)
        except UsageError:
            continue
        if path not in found and path.is_dir():
            found.append(path)
    return found


class Git:
    """The host's git, run with a clean environment."""

    def __init__(self, runner: Runner, env: Mapping[str, str]) -> None:
        self.runner = runner
        self.env = sandbox.clean_env(env)
        self.exe = runner.which("git", self.env.get("PATH"))

    @property
    def available(self) -> bool:
        """True when the host has git."""
        return self.exe is not None

    def run(self, repo: Path, *args: str) -> tuple[int, str]:
        """``(exit status, stdout)`` of ``git -C repo args``."""
        if self.exe is None:
            raise ForkLinuxError("git is not installed on this computer")
        completed = self.runner.run([self.exe, "-C", str(repo), *args], env=self.env, timeout=GIT_TIMEOUT)
        return completed.returncode, completed.stdout

    def config_list(self, repo: Path, key: str) -> list[str]:
        """Every value of ``key`` in the repository's own config."""
        status, out = self.run(repo, "config", "--local", "--get-all", key)
        return out.splitlines() if status == 0 else []


def _hooks(hooks_dir: Path) -> list[str]:
    """Executable hook files (not ``*.sample``) in ``hooks_dir``."""
    try:
        entries = sorted(os.scandir(hooks_dir), key=lambda entry: entry.name)
    except OSError:
        return []
    return [
        entry.name
        for entry in entries
        if not entry.name.endswith(".sample")
        and entry.is_file()
        and stat.S_IMODE(entry.stat().st_mode) & 0o111
    ]


def linux_path_remote(url: str) -> str | None:
    """The top-level directory of a remote URL that is a Linux path (``/home/…``, ``file:///home/…``)."""
    path = url[len("file://") :] if url.startswith("file://") else url
    if not path.startswith("/") or path.startswith("//"):
        return None
    top = path.split("/")[1]
    if len(top) == 2 and top[1] == ":":
        return None  # file:///Z:/… is already a Windows path
    return top


def covered_tops(overlay_text: str) -> set[str]:
    """Top-level directories the git overlay rewrites (``insteadOf = /home/``)."""
    tops = set()
    for line in overlay_text.splitlines():
        key, sep, value = line.strip().partition("=")
        value = value.strip()
        if sep and key.strip().lower() == "insteadof" and value.startswith("/") and value.endswith("/"):
            tops.add(value.strip("/"))
    return tops


def scan_one(git: Git, repo: Path, covered: set[str]) -> RepoReport:
    """Ask the host's git about ``repo``."""
    report = RepoReport(repo)
    status, out = git.run(repo, "rev-parse", "--absolute-git-dir", "--git-path", "hooks")
    lines = out.splitlines()
    if status != 0 or len(lines) < 2:
        report.error = "not a git repository"
        return report
    hooks_dir = Path(lines[1]) if os.path.isabs(lines[1]) else repo / lines[1]
    report.hooks = _hooks(hooks_dir)
    status, out = git.run(repo, "ls-files", "-s", "-z")
    links = [entry.split("\t", 1)[1] for entry in out.split("\0") if entry.startswith(SYMLINK_MODE + " ")]
    skipped = set(git.config_list(repo, MARK_SKIP))
    report.symlinks = [link for link in links if link not in skipped]
    report.skipped_links = [link for link in links if link in skipped]
    report.submodules = (repo / ".gitmodules").is_file()
    status, out = git.run(repo, "config", "--local", "--get-regexp", r"^remote\..*\.(url|pushurl)$")
    for line in out.splitlines():
        _key, _sep, url = line.partition(" ")
        top = linux_path_remote(url)
        if top is not None and top not in covered:
            report.linux_remotes.append(url)
    status, out = git.run(repo, "config", "--local", "--bool", "core.filemode")
    report.filemode = status == 0 and out.strip() == "true"
    return report


def scan(git: Git, repos: Iterable[Path], overlay_text: str) -> list[RepoReport]:
    """:func:`scan_one` for every repository."""
    covered = covered_tops(overlay_text)
    return [scan_one(git, repo, covered) for repo in repos]


def overlay_text(paths: Any, user: str) -> str:
    """The generated git overlay (``""`` when it does not exist)."""
    try:
        return gitconfig.overlay_path(paths, user).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _check(git: Git, repo: Path, *args: str) -> None:
    status, _out = git.run(repo, *args)
    if status != 0:
        raise ForkLinuxError(f"git {' '.join(args)} failed in {repo}")


def fix(git: Git, report: RepoReport, *, filemode: bool = True, symlinks: bool = True) -> list[str]:
    """Set ``core.filemode = false`` and mark tracked links ``skip-worktree``; return what was done."""
    done: list[str] = []
    repo = report.path
    if filemode and report.filemode:
        _check(git, repo, "config", "--local", "core.filemode", "false")
        _check(git, repo, "config", "--local", MARK_FILEMODE, "true")
        done.append(f"{repo}: core.filemode = false")
    if symlinks and report.symlinks:
        _check(git, repo, "update-index", "--skip-worktree", "--", *report.symlinks)
        for link in report.symlinks:
            _check(git, repo, "config", "--local", "--add", MARK_SKIP, link)
        done.append(f"{repo}: skip-worktree for {', '.join(report.symlinks)}")
    return done


def undo(git: Git, repo: Path) -> list[str]:
    """Revert what :func:`fix` recorded in ``repo``; return what was done."""
    done: list[str] = []
    if git.config_list(repo, MARK_FILEMODE):
        _check(git, repo, "config", "--local", "core.filemode", "true")
        _check(git, repo, "config", "--local", "--unset-all", MARK_FILEMODE)
        done.append(f"{repo}: core.filemode = true again")
    links = git.config_list(repo, MARK_SKIP)
    if links:
        _check(git, repo, "update-index", "--no-skip-worktree", "--", *links)
        _check(git, repo, "config", "--local", "--unset-all", MARK_SKIP)
        done.append(f"{repo}: skip-worktree removed from {', '.join(links)}")
    return done


def describe(reports: Sequence[RepoReport], attr: str) -> list[str]:
    """``name (items)`` for every report whose ``attr`` is set."""
    out = []
    for report in reports:
        value = getattr(report, attr)
        if isinstance(value, list) and value:
            out.append(f"{report.name} ({', '.join(value)})")
        elif value is True:
            out.append(report.name)
    return out
