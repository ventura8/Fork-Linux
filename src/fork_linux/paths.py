"""XDG locations of every file fork-linux owns, plus the prefix safety policy.

Nothing here touches the filesystem except :meth:`Paths.ensure` and the
marker/existence probes of :func:`is_forbidden_prefix`. ``~/.wine`` is never
an acceptable prefix, and neither is any directory we did not create.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .errors import UsageError

APP_DIR = "fork-linux"
META_DIR = ".fork-linux"
CREATED_BY = "created-by"
PREFIX_ENV = "FORK_LINUX_PREFIX"
ADOPT_ENV = "FORK_LINUX_ADOPT_PREFIX"
# Subdirectories of data_dir that fork-linux manages (and deletes from) itself.
RESERVED_STORES = ("runtimes", "snapshots")

_PRIVATE = 0o700
_SHARED = 0o755


@dataclass(frozen=True)
class Paths:
    """Resolved locations for one user (and one Wine prefix)."""

    config_dir: Path
    data_dir: Path
    cache_dir: Path
    state_dir: Path
    runtime_dir: Path
    prefix: Path

    @property
    def config_file(self) -> Path:
        """Our INI configuration."""
        return self.config_dir / "config.ini"

    @property
    def manifest_override(self) -> Path:
        """Optional local runtime manifest that overrides the packaged one."""
        return self.config_dir / "manifest.local.json"

    @property
    def runtimes_dir(self) -> Path:
        """Managed Wine/winetricks builds."""
        return self.data_dir / "runtimes"

    @property
    def snapshots_dir(self) -> Path:
        """Pre-launch snapshots of Fork's install directory."""
        return self.data_dir / "snapshots"

    @property
    def integrations_file(self) -> Path:
        """Registry of desktop/file-manager files we installed."""
        return self.data_dir / "integrations.json"

    @property
    def downloads_dir(self) -> Path:
        """Verified download cache."""
        return self.cache_dir / "downloads"

    @property
    def feeds_dir(self) -> Path:
        """Cached Fork release feeds."""
        return self.cache_dir / "feeds"

    @property
    def logs_dir(self) -> Path:
        """Rotating log files."""
        return self.state_dir / "logs"

    @property
    def lock_file(self) -> Path:
        """flock() target serialising setup/update/rollback/uninstall."""
        return self.runtime_dir / "setup.lock"

    @property
    def session_file(self) -> Path:
        """Details of the running Fork session."""
        return self.runtime_dir / "session.json"

    @property
    def prefix_meta_dir(self) -> Path:
        """Our metadata directory inside the Wine prefix."""
        return self.prefix / META_DIR

    @property
    def state_file(self) -> Path:
        """Bootstrap state (step markers, versions)."""
        return self.prefix_meta_dir / "state.json"

    @property
    def created_by_marker(self) -> Path:
        """Marker proving this prefix was created by fork-linux."""
        return self.prefix_meta_dir / CREATED_BY

    def wine_user_dir(self, user: str) -> Path:
        """``C:\\users\\<user>`` inside the prefix."""
        return self.prefix / "drive_c" / "users" / user

    def fork_local_dir(self, user: str) -> Path:
        """``%LOCALAPPDATA%\\Fork`` (Fork's Velopack root)."""
        return self.wine_user_dir(user) / "AppData" / "Local" / "Fork"

    def fork_current_dir(self, user: str) -> Path:
        """``%LOCALAPPDATA%\\Fork\\current`` (the installed Fork.exe)."""
        return self.fork_local_dir(user) / "current"

    @property
    def fork_linux_win_dir(self) -> Path:
        """``C:\\fork-linux`` (our shims, outside Fork's install directory)."""
        return self.prefix / "drive_c" / "fork-linux"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, prefix: Path | None = None) -> Paths:
        """Build paths from ``env`` (default ``os.environ``); refuse a forbidden prefix."""
        environ: Mapping[str, str] = os.environ if env is None else env
        home = _home(environ)
        config_dir = _xdg(environ, "XDG_CONFIG_HOME", home / ".config") / APP_DIR
        data_dir = _xdg(environ, "XDG_DATA_HOME", home / ".local" / "share") / APP_DIR
        cache_dir = _xdg(environ, "XDG_CACHE_HOME", home / ".cache") / APP_DIR
        state_dir = _xdg(environ, "XDG_STATE_HOME", home / ".local" / "state") / APP_DIR
        runtime_base = environ.get("XDG_RUNTIME_DIR", "")
        if runtime_base and os.path.isabs(runtime_base):
            runtime_dir = Path(runtime_base) / APP_DIR
        else:
            runtime_dir = state_dir / "run"

        if prefix is not None:
            chosen = _absolute(Path(prefix), home)
        elif environ.get(PREFIX_ENV):
            chosen = _absolute(Path(environ[PREFIX_ENV]), home)
        else:
            chosen = data_dir / "prefix"
        _check_prefix(chosen, home, data_dir, adopt=environ.get(ADOPT_ENV) == "1")
        return cls(
            config_dir=config_dir,
            data_dir=data_dir,
            cache_dir=cache_dir,
            state_dir=state_dir,
            runtime_dir=runtime_dir,
            prefix=chosen,
        )

    def ensure(self) -> None:
        """Create our directories; data and runtime directories are private (0700)."""
        for directory, mode in (
            (self.config_dir, _SHARED),
            (self.data_dir, _PRIVATE),
            (self.cache_dir, _SHARED),
            (self.state_dir, _SHARED),
            (self.runtime_dir, _PRIVATE),
            (self.logs_dir, _PRIVATE),
        ):
            directory.mkdir(parents=True, exist_ok=True, mode=mode)
            if mode == _PRIVATE:
                os.chmod(directory, mode)


def is_forbidden_prefix(path: Path, home: Path, *, data_dir: Path | None = None) -> bool:
    """True if ``path`` must never be used (or modified) as our Wine prefix.

    Forbidden: ``~/.wine`` and anything inside it, ``home`` itself and its
    ancestors (including ``/``), our own runtime and snapshot stores, and any
    path outside ``data_dir`` (default: ``$XDG_DATA_HOME/fork-linux``) that
    does not carry our created-by marker.
    """
    if data_dir is None:
        data_dir = _xdg(os.environ, "XDG_DATA_HOME", home / ".local" / "share") / APP_DIR
    if _always_refused(path, home, data_dir):
        return True
    target = path.resolve()
    owned_root = data_dir.resolve()
    if target != owned_root and target.is_relative_to(owned_root):
        return False
    marker = target / META_DIR / CREATED_BY
    return not marker.is_file()


def _always_refused(path: Path, home: Path, data_dir: Path) -> bool:
    """``~/.wine`` (and below), ``home`` or an ancestor such as ``/``, or one of our stores."""
    target = path.resolve()
    real_home = home.resolve()
    wine = (home / ".wine").resolve()
    if target == wine or target.is_relative_to(wine):
        return True
    if any(target.is_relative_to((data_dir / store).resolve()) for store in RESERVED_STORES):
        return True
    return target == Path(target.anchor) or real_home.is_relative_to(target)


def _check_prefix(prefix: Path, home: Path, data_dir: Path, *, adopt: bool) -> None:
    """Raise :class:`UsageError` unless ``prefix`` is ours (or explicitly adopted)."""
    if not is_forbidden_prefix(prefix, home, data_dir=data_dir):
        return
    if _always_refused(prefix, home, data_dir):
        raise UsageError(
            f"refusing to use {prefix} as the Wine prefix",
            hint="fork-linux never touches ~/.wine, your home directory or its parents, "
            f"or its own {' and '.join(RESERVED_STORES)} directories; "
            f"unset {PREFIX_ENV} to use the default prefix",
        )
    if adopt and prefix.is_dir():
        return
    raise UsageError(
        f"refusing to use {prefix} as the Wine prefix: it was not created by fork-linux",
        hint=f"use a path under {data_dir}, or set {ADOPT_ENV}=1 to adopt an existing prefix",
    )


def _home(env: Mapping[str, str]) -> Path:
    """``$HOME`` from ``env``, falling back to the password database."""
    value = env.get("HOME", "")
    return Path(value) if value else Path.home()


def _xdg(env: Mapping[str, str], name: str, default: Path) -> Path:
    """An XDG base directory; relative values are ignored, as the spec requires."""
    value = env.get(name, "")
    return Path(value) if value and os.path.isabs(value) else default


def _absolute(path: Path, home: Path) -> Path:
    """Expand a leading ``~`` against ``home`` and make ``path`` absolute."""
    text = str(path)
    if text == "~" or text.startswith("~/"):
        path = home / text[2:]
    return path.absolute()
