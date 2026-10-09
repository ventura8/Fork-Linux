"""``fork-linux repo check|fix|undo``: repository settings that make Fork under Wine misreport changes.

``check`` lists what breaks in each repository (hooks, symlinks, submodules,
Linux-path remotes, ``core.filemode``); ``fix`` sets ``core.filemode = false``
and marks tracked symbolic links ``skip-worktree`` after asking (``--yes``
skips the question); ``undo`` reverts exactly what ``fix`` recorded.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Any

from .. import repos, winecmd
from .. import ui as ui_mod
from ..cli import AppContext
from ..errors import UsageError
from ..fork_layout import ForkLayout
from ..pathmap import PathMap


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Add ``repo`` and its actions."""
    parser = subparsers.add_parser(
        "repo",
        help="check or fix repository settings for Fork under Wine",
        description="Fork under Wine shows executable files and symbolic links as modified, skips hooks "
        "that run programs and cannot update submodules. 'check' lists the affected repositories, 'fix' "
        "changes the repository settings that can be changed (after asking), 'undo' reverts them.",
    )
    actions = parser.add_subparsers(dest="repo_action", metavar="ACTION", title="actions", required=True)
    check = actions.add_parser("check", help="list problems (default: every repository Fork knows)")
    check.add_argument("paths", metavar="PATH", nargs="*", help="repository to check")
    check.set_defaults(func=run_check)
    fix = actions.add_parser("fix", help="set core.filemode=false and skip-worktree for tracked symlinks")
    fix.add_argument("paths", metavar="PATH", nargs="+", help="repository to change")
    fix.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    fix.add_argument("--no-filemode", action="store_true", help="leave core.filemode alone")
    fix.add_argument("--no-symlinks", action="store_true", help="leave symbolic links alone")
    fix.set_defaults(func=run_fix)
    undo = actions.add_parser("undo", help="revert what 'fix' changed")
    undo.add_argument("paths", metavar="PATH", nargs="+", help="repository to restore")
    undo.set_defaults(func=run_undo)


def _git(ctx: AppContext) -> repos.Git:
    git = repos.Git(ctx.runner, ctx.env)
    if not git.available:
        raise UsageError("git is not installed on this computer", hint="install git, then try again")
    return git


def _paths(raw: list[str]) -> list[Path]:
    found = []
    for item in raw:
        path = Path(os.path.abspath(item))
        if not path.is_dir():
            raise UsageError(f"no such directory: {item}", hint="pass a repository directory")
        found.append(path)
    return found


def _known(ctx: AppContext) -> list[Path]:
    layout = ForkLayout(ctx.paths, winecmd.windows_user(ctx.env))
    return repos.known(layout, PathMap.from_prefix(ctx.paths.prefix))


def _as_dict(report: repos.RepoReport) -> dict[str, Any]:
    return {
        "path": str(report.path),
        "error": report.error,
        "hooks": report.hooks,
        "symlinks": report.symlinks,
        "skip_worktree": report.skipped_links,
        "submodules": report.submodules,
        "linux_remotes": report.linux_remotes,
        "filemode": report.filemode,
    }


def _lines(report: repos.RepoReport) -> list[str]:
    if report.error:
        return [f"  {report.error}"]
    lines = []
    if report.hooks:
        lines.append(f"  hooks Fork's git skips under Wine: {', '.join(report.hooks)}")
    if report.symlinks:
        lines.append(f"  symbolic links Fork shows as modified: {', '.join(report.symlinks)}")
    if report.skipped_links:
        lines.append(f"  symbolic links marked skip-worktree by fork-linux: {', '.join(report.skipped_links)}")
    if report.submodules:
        lines.append("  submodules: update them from a Linux terminal")
    if report.linux_remotes:
        lines.append(f"  remotes Fork's git cannot open: {', '.join(report.linux_remotes)}")
    if report.filemode:
        lines.append("  core.filemode = true: Fork shows executable files as modified")
    return lines or ["  ok"]


def run_check(args: argparse.Namespace, ctx: AppContext) -> int:
    """Print the problems of the named (or every known) repository."""
    git = _git(ctx)
    targets = _paths(args.paths) if args.paths else _known(ctx)
    reports = repos.scan(git, targets, repos.overlay_text(ctx.paths, winecmd.windows_user(ctx.env)))
    if ctx.json:
        ctx.print_json({"repositories": [_as_dict(report) for report in reports]})
    else:
        _print_reports(reports)
    return 0


def _print_reports(reports: list[repos.RepoReport]) -> None:
    """Each repository followed by its problems."""
    if not reports:
        print("no repositories (open some in Fork, or pass a path)")
    for report in reports:
        print(report.path)
        print("\n".join(_lines(report)))


def _plan(todo: list[repos.RepoReport], *, filemode: bool, symlinks: bool) -> list[str]:
    """One line per change ``fix`` would make."""
    plan = []
    for report in todo:
        if filemode and report.filemode:
            plan.append(f"{report.path}: set core.filemode = false")
        if symlinks and report.symlinks:
            plan.append(f"{report.path}: skip-worktree for {', '.join(report.symlinks)}")
    return plan


def run_fix(args: argparse.Namespace, ctx: AppContext) -> int:
    """Change the repositories' settings after confirmation."""
    git = _git(ctx)
    filemode = not args.no_filemode
    symlinks = not args.no_symlinks
    reports = repos.scan(git, _paths(args.paths), "")
    todo = [r for r in reports if not r.error and ((filemode and r.filemode) or (symlinks and r.symlinks))]
    if not todo:
        print("nothing to change")
        return 0
    plan = _plan(todo, filemode=filemode, symlinks=symlinks)
    ui = ui_mod.choose(ctx.env, gui=ctx.gui, mode=ctx.config.get("ui", "progress"), runner=ctx.runner)
    text = "\n".join(plan) + "\n\nUndo with 'fork-linux repo undo PATH'."
    if not args.yes and not ui.confirm("Change these repositories?", text, default=False):
        print("nothing changed")
        return 1
    for report in todo:
        for line in repos.fix(git, report, filemode=filemode, symlinks=symlinks):
            print(line)
    return 0


def run_undo(args: argparse.Namespace, ctx: AppContext) -> int:
    """Revert what ``fix`` recorded."""
    git = _git(ctx)
    for repo in _paths(args.paths):
        done = repos.undo(git, repo)
        print("\n".join(done) if done else f"{repo}: nothing to undo")
    return 0
