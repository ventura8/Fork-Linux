"""Fork's own ``settings.json``: read, merge a few keys, back up, write atomically.

Rules (AGENTS.md hard rule 3): the file is only edited while Fork is closed
(the caller guarantees it), it is backed up before every write, unknown keys
are preserved exactly, and it is never created unless the caller asks for it:
Fork writes ``settings.json`` itself after its first completed launch (spike S8).
:func:`seed` pre-creates it before that first launch (E2E, Fork 2.23.2): Fork
keeps the seeded keys, adds its own, and replaces its "User information"
welcome dialog with a short "update the Fork git instance" dialog.
Keys are addressed with dotted paths (``RepositoryManager.SourceDirectories``).
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import stat
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import fork_tools, fsutil
from .config import Config
from .errors import ForkLinuxError, IntegrityFailed
from .fork_layout import ForkLayout
from .paths import Paths

log = logging.getLogger(__name__)

# Keys verified in spike S8 and against a real Fork 2.23.2 settings.json (tests/test_settings_contract.py).
GUID = "Guid"
THEME = "Theme"  # 0 light, 1 dark
FOLLOW_SYSTEM_THEME = "FollowSystemTheme"
LAYOUT_SCALING = "LayoutScaling"  # percent
UPDATE_SUBMODULES_ON_CHECKOUT = "UpdateSubmodulesOnCheckout"
DISABLE_HARDWARE_ACCELERATION = "DisableHardwareAcceleration"
SHELL_TOOL = fork_tools.SHELL_TOOL
EXTERNAL_DIFF_TOOL = fork_tools.EXTERNAL_DIFF_TOOL
EXTERNAL_MERGE_TOOL = fork_tools.EXTERNAL_MERGE_TOOL  # Fork 2.23 stores "MergeTool"
EXTERNAL_DIFF_TOOLS = fork_tools.EXTERNAL_DIFF_TOOLS
EXTERNAL_MERGE_TOOLS = fork_tools.EXTERNAL_MERGE_TOOLS
GIT_INSTANCE_PATH = "GitInstancePath"
APPLICATION_UPDATE_TYPE = "ApplicationUpdateType"  # 0 Develop (Fork's default), 1 Stable, 2 Off
REPOSITORY_MANAGER = "RepositoryManager"
SOURCE_DIRECTORIES = "RepositoryManager.SourceDirectories"
WORKSPACES = "Workspaces"
MAIN_WINDOW_LOCATION_STATE = "MainWindowLocationState"

THEME_LIGHT = 0
THEME_DARK = 1
SHELL_TOOL_COMMAND_PROMPT = "CommandPrompt"

# Wine-safe values kept before every launch for the keys named in [fork] enforce_settings.
ENFORCED_DEFAULTS: dict[str, object] = {
    UPDATE_SUBMODULES_ON_CHECKOUT: False,
    DISABLE_HARDWARE_ACCELERATION: True,
}

LAYOUT_SCALING_MIN = 100
LAYOUT_SCALING_MAX = 300
BASE_DPI = 96

SETTINGS_NAME = "settings.json"
BACKUP_DIR_NAME = "settings-backups"
DEFAULT_KEEP = 3
# settings.json is small; anything bigger is not Fork's and is never read whole.
MAX_BYTES = 16 * 1024 * 1024
FILE_MODE = 0o644
BACKUP_MODE = 0o600

_RESTORE_HINT = "close Fork and run 'fork-linux settings restore' to go back to a backup"
_BACKUP = re.compile(r"settings\.json\.(\d{8}T\d{6}\.\d{6}Z)(?:-(\d{1,6}))?", re.ASCII)
_DPI = re.compile(r"\d{1,4}", re.ASCII)


def default_backup_dir(paths: Paths) -> Path:
    """Where backups of ``settings.json`` go: ``$XDG_DATA_HOME/fork-linux/settings-backups``."""
    return paths.data_dir / BACKUP_DIR_NAME


def _utcnow() -> datetime:
    """The current UTC time (tests replace this)."""
    return datetime.now(timezone.utc)


def _split(dotted: str) -> list[str]:
    parts = dotted.split(".")
    if not all(parts):
        raise ValueError(f"invalid settings key: {dotted!r}")
    return parts


def _corrupt(path: Path, why: str) -> IntegrityFailed:
    return IntegrityFailed(f"Fork's settings file {path} is unusable: {why}", hint=_RESTORE_HINT)


def _check_writable_target(path: Path) -> bool:
    """True if ``path`` exists as a regular file; raise if it is a symlink or something else."""
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(mode):
        raise IntegrityFailed(
            f"refusing to write {path}: it is a symbolic link",
            hint="fork-linux only edits Fork's own settings file; replace the link with a regular file",
        )
    if not stat.S_ISREG(mode):
        raise _corrupt(path, "it is not a regular file")
    return True


def load(path: Path) -> dict[str, Any]:
    """Parse ``settings.json``; a missing file gives ``{}``, a corrupt one raises :class:`IntegrityFailed`."""
    path = Path(path)
    try:
        # O_NONBLOCK: a FIFO planted here must not hang the launcher.
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise ForkLinuxError(f"cannot read {path}: {exc.strerror or exc}") from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise _corrupt(path, "it is not a regular file")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            raw = handle.read(MAX_BYTES + 1)
    finally:
        os.close(fd)
    if len(raw) > MAX_BYTES:
        raise _corrupt(path, f"it is larger than {MAX_BYTES} bytes")
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (ValueError, RecursionError) as exc:
        raise _corrupt(path, f"not valid JSON ({exc})") from None
    if not isinstance(data, dict):
        raise _corrupt(path, "the top level is not a JSON object")
    return data


def get(data: Mapping[str, Any], dotted: str, default: Any = None) -> Any:
    """The value at ``dotted`` (``"RepositoryManager.SourceDirectories"``), or ``default``."""
    node: Any = data
    for part in _split(dotted):
        if not isinstance(node, Mapping) or part not in node:
            return default
        node = node[part]
    return node


def _same(a: Any, b: Any) -> bool:
    """JSON-level equality: ``True`` differs from ``1`` and ``100`` from ``100.0``."""
    return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def merge_set(data: dict[str, Any], dotted: str, value: Any) -> bool:
    """Set ``dotted`` to ``value``, creating missing (or null) intermediate objects; True if it changed."""
    *parents, leaf = _split(dotted)
    node = data
    for part in parents:
        child = node.get(part)
        if child is None:
            child = node[part] = {}
        elif not isinstance(child, dict):
            raise IntegrityFailed(
                f"Fork setting {part!r} is not an object, so {dotted!r} cannot be set",
                hint=_RESTORE_HINT,
            )
        node = child
    if leaf in node and _same(node[leaf], value):
        return False
    node[leaf] = copy.deepcopy(value)
    return True


def _backup_key(path: Path) -> tuple[str, int] | None:
    match = _BACKUP.fullmatch(path.name)
    if match is None:
        return None
    return match.group(1), int(match.group(2) or 0)


def list_backups(backup_dir: Path) -> list[Path]:
    """Backups of ``settings.json`` in ``backup_dir``, newest first (other files are ignored)."""
    try:
        entries = list(os.scandir(backup_dir))
    except OSError:
        return []
    keyed = []
    for entry in entries:
        key = _backup_key(Path(entry.path))
        if key is not None and entry.is_file(follow_symlinks=False):
            keyed.append((key, Path(entry.path)))
    keyed.sort(reverse=True)
    return [path for _key, path in keyed]


def backup(path: Path, *, backup_dir: Path, keep: int = DEFAULT_KEEP) -> Path | None:
    """Copy ``path`` to ``backup_dir/settings.json.<UTC timestamp>``; keep the newest ``keep`` backups.

    Returns the new backup, or None when ``path`` does not exist. A symlinked
    ``path`` raises :class:`IntegrityFailed`.
    """
    if keep < 1:
        raise ValueError("keep must be at least 1")
    path = Path(path)
    if not _check_writable_target(path):
        return None
    with open(path, "rb") as handle:
        raw = handle.read()
    fsutil.ensure_dir(backup_dir, 0o700)
    stamp = _utcnow().strftime("%Y%m%dT%H%M%S.%fZ")
    target = Path(backup_dir) / f"{SETTINGS_NAME}.{stamp}"
    counter = 0
    while os.path.lexists(target):
        counter += 1
        target = Path(backup_dir) / f"{SETTINGS_NAME}.{stamp}-{counter}"
    fsutil.atomic_write(target, raw, mode=BACKUP_MODE)
    for old in list_backups(backup_dir)[keep:]:
        old.unlink()
    return target


def save(path: Path, data: Mapping[str, Any], *, backup_dir: Path, keep: int = DEFAULT_KEEP) -> Path | None:
    """Back up the existing file, then write ``data`` atomically (indent 2, UTF-8, mode 0644).

    Returns the backup path (None if there was no file to back up). A symlinked
    ``path`` is refused with :class:`IntegrityFailed`.
    """
    path = Path(path)
    saved = backup(path, backup_dir=backup_dir, keep=keep)
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    fsutil.atomic_write(path, text, mode=FILE_MODE)
    return saved


def _clamp_scaling(percent: int) -> int:
    return max(LAYOUT_SCALING_MIN, min(LAYOUT_SCALING_MAX, percent))


def _theme_values(mode: str, theme: str | None) -> dict[str, object]:
    """``Theme`` / ``FollowSystemTheme`` for ``[display] theme`` and the desktop's theme."""
    if mode == "follow":
        chosen = theme
    elif mode in ("dark", "light"):
        chosen = mode
    else:
        return {}
    if chosen == "dark":
        return {THEME: THEME_DARK, FOLLOW_SYSTEM_THEME: False}
    if chosen == "light":
        return {THEME: THEME_LIGHT, FOLLOW_SYSTEM_THEME: False}
    return {}


def legacy_scaling(dpi: str, scale: int | None) -> int | None:
    """The ``LayoutScaling`` fork-linux before 0.1.0 wrote for ``[display] dpi`` (None: none).

    Scaling is now Wine's ``LogPixels`` alone (the ``display_dpi`` step):
    Fork's ``LayoutScaling`` on top of it scaled the UI twice (QA 1.4). The
    ``fork_settings`` step resets a ``LayoutScaling`` that still holds this
    value back to 100.
    """
    if dpi == "auto":
        if isinstance(scale, int) and not isinstance(scale, bool) and scale > 0:
            return _clamp_scaling(scale)
        return None
    if _DPI.fullmatch(dpi) and int(dpi) > 0:
        return _clamp_scaling(round(int(dpi) / BASE_DPI * 100))
    return None


def _tool_replaceable(key: str, tool: Any) -> bool:
    """True for Fork's default (null, CommandPrompt, an empty Custom tool) or a tool we set ourselves."""
    if tool is None or fork_tools.is_ours(tool):
        return True
    if not isinstance(tool, Mapping):
        return False
    if key == SHELL_TOOL:
        return tool.get("Type") == SHELL_TOOL_COMMAND_PROMPT
    return _same(tool, fork_tools.DEFAULT_EXTERNAL_TOOL)


def _tool_values(tools: Mapping[str, Any], current: Mapping[str, Any]) -> dict[str, object]:
    """Fork's terminal / diff / merge settings: ours where Fork's default or ours is set.

    A key we have no program for goes back to Fork's default when it still
    names one of our programs (``fl-launch.exe`` without the bridge daemon
    would make the button do nothing); a tool the user picked is never touched.
    """
    values: dict[str, object] = {}
    for key in fork_tools.TOOL_KEYS:
        present = get(current, key)
        wanted = tools.get(key)
        if wanted is not None and _tool_replaceable(key, present):
            values[key] = wanted
        elif wanted is None and fork_tools.is_ours(present):
            values[key] = copy.deepcopy(fork_tools.DEFAULTS[key])
    for key in fork_tools.TOOL_LIST_KEYS:
        merged = fork_tools.merged_list(get(current, key), tools.get(key))
        if merged is not None:
            values[key] = merged
    return values


def _default_source_dirs(value: Any, user: str) -> bool:
    """True if ``value`` is unset or Fork's default ``["C:\\users\\<user>"]``.

    Unset matters on a fresh ``settings.json``: Fork rebuilds a
    ``repositories.toml`` it cannot read from this key, and falls back to the
    prefix folder when the key is missing.
    """
    if value is None:
        return True
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], str):
        return False
    return value[0].rstrip("\\").lower() == f"c:\\users\\{user}".lower()


def desired(
    config: Config,
    *,
    user: str,
    theme: str | None,
    tools: Mapping[str, Any],
    home_win: str | None,
    current: Mapping[str, Any],
    reset_scaling: int | None = None,
) -> dict[str, object]:
    """The settings fork-linux wants (dotted key -> value), given the current ``settings.json``.

    * keys of ``[fork] enforce_settings`` that have a Wine-safe default;
    * ``[display] theme``: ``follow`` uses the desktop ``theme`` (``dark``/``light``),
      ``dark``/``light`` force it; Fork's own system-theme following is turned off;
    * ``tools`` (:func:`fork_tools.wanted`) for ``ShellTool`` / ``ExternalDiffTool``
      / ``MergeTool``: they replace Fork's defaults or tools we set before, never
      a tool the user picked; our tools without a replacement go back to Fork's default;
      in ``ExternalDiffTools`` / ``ExternalMergeTools`` only our own entry is added
      or removed, the user's entries stay;
    * ``home_win`` replaces the default repository source directory ``C:\\users\\<user>``
      (or sets it when ``settings.json`` has none yet, so even the first session is right);
    * ``reset_scaling``: a ``LayoutScaling`` holding this old value goes back to 100
      (scaling is Wine's ``LogPixels`` alone now).
    """
    wanted: dict[str, object] = {}
    for key in config.getlist("fork", "enforce_settings"):
        if key in ENFORCED_DEFAULTS:
            wanted[key] = ENFORCED_DEFAULTS[key]
        else:
            log.warning("[fork] enforce_settings: no Wine-safe default is known for %s; ignored", key)
    wanted.update(_theme_values(config.raw("display", "theme").strip().lower(), theme))
    if reset_scaling is not None and reset_scaling != LAYOUT_SCALING_MIN and _same(
        get(current, LAYOUT_SCALING), reset_scaling
    ):
        wanted[LAYOUT_SCALING] = LAYOUT_SCALING_MIN
    wanted.update(_tool_values(tools, current))
    if home_win is not None and _default_source_dirs(get(current, SOURCE_DIRECTORIES), user):
        wanted[SOURCE_DIRECTORIES] = [home_win]
    return wanted


def apply(layout: ForkLayout, wanted: Mapping[str, object], *, backup_dir: Path, create: bool = False) -> list[str]:
    """Merge ``wanted`` into Fork's ``settings.json``; return the keys that changed.

    A missing file is left alone (Fork creates it on its first completed
    launch) unless ``create`` is true. The file is written, after a backup,
    only when something changed. The caller makes sure Fork is closed.
    """
    path = layout.settings_file
    if not os.path.lexists(path) and not create:
        return []
    data = load(path)
    changed = [key for key, value in wanted.items() if merge_set(data, key, value)]
    if changed:
        save(path, data, backup_dir=backup_dir)
    return changed


def seed(layout: ForkLayout, wanted: Mapping[str, object], *, backup_dir: Path) -> list[str]:
    """Like :func:`apply`, but a missing ``settings.json`` is created with a fresh ``Guid``.

    Fork gives every installation a random ``Guid`` the first time it writes
    the file; a seeded file gets one the same way. The caller makes sure Fork
    is closed.
    """
    if os.path.lexists(layout.settings_file):
        return apply(layout, wanted, backup_dir=backup_dir)
    return apply(layout, {GUID: str(uuid.uuid4()), **wanted}, backup_dir=backup_dir, create=True)
