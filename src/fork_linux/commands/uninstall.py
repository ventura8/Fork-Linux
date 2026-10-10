"""``fork-linux uninstall [--purge]``: remove the desktop integration, or everything fork-linux created.

Without ``--purge`` only the menu entry, icons, file-manager actions and CLI
aliases go (:func:`desktop_integration.remove`); Fork, its prefix and your
settings stay. ``--purge`` also deletes our Wine prefix (only when it carries
our created-by marker), the managed Wine runtimes, snapshots, downloads, logs
and ``config.ini``. We never touch ``~/.wine`` or any prefix fork-linux did
not create. The package itself is removed with your package manager.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

from .. import APP_NAME, desktop_integration, fsutil, procs, wine_provider
from ..cli import AppContext
from ..errors import Declined
from ..locking import FileLock
from ..paths import Paths

CONFIRM_WORD = "purge"
KILL_TIMEOUT = 60.0
LICENSE_WARNING = (
    "Purging deletes Fork together with its Wine prefix. Each prefix counts as one of your Fork license's "
    "machine activations: if you plan to keep using Fork elsewhere, first deactivate it in Fork "
    "(Help > Activation > Deactivate), then run this again."
)


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Add ``uninstall``."""
    parser = subparsers.add_parser(
        "uninstall",
        help="remove the desktop integration (with --purge: everything fork-linux created)",
        description="Remove the menu entry, icons, file-manager actions and command aliases. With --purge also "
        "delete fork-linux's Wine prefix (Fork included), Wine runtimes, snapshots, downloads, logs and "
        "settings. ~/.wine is never touched. Remove the fork-linux package with your package manager.",
    )
    parser.add_argument("--purge", action="store_true", help="also delete the prefix, runtimes, caches and config")
    parser.add_argument(
        "--keep-downloads", action="store_true", help="with --purge: keep the download caches (ours and winetricks')"
    )
    parser.add_argument("--yes", action="store_true", help="with --purge: do not ask for confirmation")
    parser.set_defaults(func=run)


def _ask(prompt: str) -> str | None:
    """A line typed by the user, or None when nobody can answer (tests replace this)."""
    if not sys.stdin.isatty():
        return None
    try:
        return input(prompt)
    except EOFError:
        return None


def _confirm(args: argparse.Namespace) -> None:
    """Show the license warning and require ``--yes`` or the word ``purge``."""
    print(f"warning: {LICENSE_WARNING}", file=sys.stderr)
    if args.yes:
        return
    answer = _ask(f"Type '{CONFIRM_WORD}' to delete everything {APP_NAME} created: ")
    if answer is None:
        raise Declined("nothing was deleted: confirmation needed", hint="run it in a terminal, or pass --yes")
    if answer.strip() != CONFIRM_WORD:
        raise Declined("nothing was deleted")


def _wineservers(paths: Paths) -> list[Path]:
    """``wineserver`` binaries that may serve our prefix: the session's, then the managed builds'."""
    from .. import launcher

    found: list[Path] = []
    session = launcher.read_session(paths)
    if session is not None and isinstance(session.get("wineserver"), str):
        found.append(Path(session["wineserver"]))
    for build_id in wine_provider.installed_builds(paths):
        found.append(wine_provider.managed_root(paths, build_id) / "bin" / "wineserver")
    return [path for path in found if path.is_file()]


def _stop_wine(ctx: AppContext) -> bool:
    """``wineserver -k`` for our prefix when something still runs in it; True if a server was asked."""
    paths = ctx.paths
    if not procs.prefix_pids(paths.prefix):
        return False
    env = {key: value for key, value in ctx.env.items() if not key.startswith("WINE")}
    env["WINEPREFIX"] = str(paths.prefix)
    for server in _wineservers(paths):
        ctx.runner.run([str(server), "-k"], env=env, timeout=KILL_TIMEOUT)
        return True
    return False


def _inside(path: Path, root: Path) -> bool:
    """True if ``path`` is ``root`` or below it (lexically, after resolving both)."""
    resolved = path.resolve()
    base = root.resolve()
    return resolved == base or resolved.is_relative_to(base)


def _never_deleted(prefix: Path, home: Path) -> bool:
    """True for a prefix we refuse to delete whatever its marker says: ``~/.wine`` or below it."""
    return _inside(prefix, home / ".wine")


def _remove(path: Path, removed: list[str], *, marker: Path | None = None) -> None:
    """``safe_rmtree`` one of our directories; note it when it existed."""
    if not os.path.lexists(path):
        return
    fsutil.safe_rmtree(path, marker=marker)
    removed.append(str(path))


def _purge_prefix(paths: Paths, home: Path, removed: list[str], kept: list[str]) -> None:
    """Delete the Wine prefix when it is ours; note it as kept when it is not."""
    if not os.path.lexists(paths.prefix):
        return
    if _never_deleted(paths.prefix, home):
        kept.append(str(paths.prefix))
    elif os.path.lexists(paths.created_by_marker):
        _remove(paths.prefix, removed, marker=paths.created_by_marker)
    elif not _inside(paths.prefix, paths.data_dir):
        kept.append(str(paths.prefix))


def _purge_cache_keeping_downloads(paths: Paths, removed: list[str], kept: list[str]) -> None:
    """Empty the cache directory except for the downloads: ours and winetricks' (.NET, core fonts)."""
    downloads = (paths.downloads_dir, paths.winetricks_cache_dir)
    try:
        names = sorted(os.listdir(paths.cache_dir))
    except OSError:
        names = []
    for name in names:
        if paths.cache_dir / name not in downloads:
            _remove(paths.cache_dir / name, removed)
    kept.extend(str(directory) for directory in downloads if os.path.lexists(directory))


def _purge(ctx: AppContext, args: argparse.Namespace) -> dict[str, Any]:
    """Delete everything we created (Fork must be closed); return what was removed and kept."""
    paths = ctx.paths
    home = Path(ctx.env.get("HOME") or Path.home())
    procs.require_closed(paths.prefix)
    removed: list[str] = []
    kept: list[str] = []
    with FileLock(paths.lock_file, "uninstall"):
        stopped = _stop_wine(ctx)
        _purge_prefix(paths, home, removed, kept)
        if args.keep_downloads:
            _purge_cache_keeping_downloads(paths, removed, kept)
        else:
            _remove(paths.cache_dir, removed)
        for directory in (paths.data_dir, paths.state_dir, paths.config_dir):
            _remove(directory, removed)
        if os.path.lexists(paths.session_file):
            paths.session_file.unlink()
    return {"removed": removed, "kept": kept, "wineserver_stopped": stopped}


def run(args: argparse.Namespace, ctx: AppContext) -> int:
    """Remove the integration, then (``--purge``) everything else."""
    if args.purge:
        _confirm(args)
        # Checked before anything is removed, so a running Fork leaves the installation untouched.
        procs.require_closed(ctx.paths.prefix)
    integration = desktop_integration.remove(ctx.paths, ctx.env, runner=ctx.runner)
    result: dict[str, Any] = {"integration_removed": [str(path) for path in integration], "purged": args.purge}
    if args.purge:
        result.update(_purge(ctx, args))
    if ctx.json:
        ctx.print_json(result)
    else:
        _print_result(result, len(integration))
    return 0


def _print_result(result: dict[str, Any], integration_count: int) -> None:
    """Describe what the uninstall removed and kept."""
    print(f"removed {integration_count} desktop integration file(s)")
    if not result["purged"]:
        print("Fork, its Wine prefix and your settings were kept; 'fork-linux uninstall --purge' deletes them")
        return
    for path in result["removed"]:
        print(f"deleted {path}")
    for path in result["kept"]:
        print(f"kept {path}")
    print("~/.wine was never touched; remove the fork-linux package with your package manager")
