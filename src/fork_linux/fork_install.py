"""Choose, download, run and verify the official Fork for Windows installer.

The installer URL is always built from the manifest template and a validated
version (AGENTS.md hard rule 2), never taken from a feed. A pinned version is
verified by size + sha256; an unpinned one (``--latest`` or an explicit
``--allow-untested``) is trust-on-first-use: HTTPS from the allowed hosts, an
``MZ`` header and a plausible size now, and after installation the full
``.nupkg`` must match the feed's sha256 (:func:`verify_nupkg`).
"""

from __future__ import annotations

import contextlib
import os
import re
import stat
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from . import download, feeds, fsutil, versions
from .errors import ForkLinuxError, IntegrityFailed, SetupFailed, UsageError
from .fork_layout import ForkLayout
from .manifest import Manifest
from .pathmap import PathMap
from .paths import Paths
from .procrun import Runner, tail

STEP = "fork_install"
MiB = 1024 * 1024
MAX_INSTALLER_SIZE = 300 * MiB
MIN_TOFU_SIZE = 20 * MiB
INSTALL_TIMEOUT = 900
SOURCES = ("default", "requested", "latest")
_PLAIN_VERSION = re.compile(r"\d{1,6}(?:\.\d{1,6}){0,3}", re.ASCII)


@dataclass(frozen=True)
class InstallPlan:
    """Which Fork installer to fetch and how to verify it."""

    version: str
    url: str
    sha256: str | None
    size: int | None
    tofu: bool
    source: str

    @property
    def file_name(self) -> str:
        """Name of the installer in the download cache."""
        return f"Fork-{self.version}.exe"


def _refuse_known_bad(manifest: Manifest, version: str) -> None:
    reason = manifest.known_bad_reason(version)
    if reason is not None:
        raise UsageError(
            f"Fork {version} is known not to work with fork-linux: {reason}",
            hint=f"install the tested default ({manifest.fork_default}) instead",
        )


def _make_plan(manifest: Manifest, version: str, source: str, *, allow_tofu: bool) -> InstallPlan:
    """A plan for ``version``; a version without a pinned sha256 needs ``allow_tofu``."""
    known = manifest.fork_version(version)
    canonical = version if known is None else known.version
    entry = manifest.installer_entry(canonical)
    if entry.requires_tofu and not allow_tofu:
        raise UsageError(
            f"Fork {canonical} is not pinned in fork-linux's runtime manifest",
            hint="pass --allow-untested to install it with trust-on-first-use checks, "
            f"or install the tested default ({manifest.fork_default})",
        )
    return InstallPlan(
        version=canonical,
        url=entry.url,
        sha256=entry.sha256,
        size=entry.size,
        tofu=entry.requires_tofu,
        source=source,
    )


def plan(
    manifest: Manifest,
    *,
    requested: str | None = None,
    latest: bool = False,
    feed_assets: Iterable[feeds.FeedAsset] | None = None,
    allow_untested: bool = False,
) -> InstallPlan:
    """Decide which installer to use: the manifest default, a requested version or the feed's latest."""
    if requested is not None and latest:
        raise UsageError("choose either a Fork version or --latest, not both")
    if latest:
        newest = feeds.latest_full(feed_assets or ())
        if newest is None:
            raise IntegrityFailed(
                "Fork's update feed lists no full package",
                hint=f"try again later, or install the tested default ({manifest.fork_default}) without --latest",
            )
        _refuse_known_bad(manifest, newest.version)
        return _make_plan(manifest, newest.version, "latest", allow_tofu=True)
    if requested is None:
        return _make_plan(manifest, manifest.fork_default, "default", allow_tofu=allow_untested)
    if not isinstance(requested, str) or not versions.is_valid(requested):
        raise UsageError(f"not a Fork version: {requested!r}", hint="use a dotted number such as 2.23.2")
    _refuse_known_bad(manifest, requested)
    return _make_plan(manifest, requested.strip(), "requested", allow_tofu=allow_untested)


def _reject(path: Path, message: str) -> IntegrityFailed:
    """Delete a downloaded installer that failed a check and build the error."""
    with contextlib.suppress(FileNotFoundError):
        path.unlink()
    return IntegrityFailed(
        message, hint="the download was deleted and nothing was installed; try again or report it"
    )


def download_installer(
    plan: InstallPlan,
    manifest: Manifest,
    paths: Paths,
    *,
    offline: bool = False,
    progress: Callable[[int, int | None], None] | None = None,
) -> Path:
    """Fetch (or reuse from the cache) and verify the installer described by ``plan``."""
    if _PLAIN_VERSION.fullmatch(plan.version) is None or plan.url != manifest.installer_url(plan.version):
        raise IntegrityFailed(
            f"refusing installer URL {plan.url!r} for Fork {plan.version!r}",
            hint="the installer URL must come from fork-linux's runtime manifest",
        )
    path = download.fetch(
        plan.url,
        plan.file_name,
        cache_dir=paths.downloads_dir,
        sha256=plan.sha256,
        size=plan.size,
        allowed_hosts=manifest.allowed_hosts,
        max_size=MAX_INSTALLER_SIZE,
        offline=offline,
        progress=progress,
    )
    with open(path, "rb") as handle:
        header = handle.read(2)
    if header != b"MZ":
        raise _reject(path, f"{plan.file_name} is not a Windows program (no MZ header)")
    if plan.tofu:
        size = path.stat().st_size
        if not MIN_TOFU_SIZE <= size <= MAX_INSTALLER_SIZE:
            raise _reject(
                path,
                f"{plan.file_name} has an implausible size ({size} bytes; expected "
                f"{MIN_TOFU_SIZE} to {MAX_INSTALLER_SIZE})",
            )
    return path


def run_installer(
    runner: Runner,
    env: Mapping[str, str],
    wine: Path,
    installer: Path,
    pathmap: PathMap,
    *,
    timeout: float = INSTALL_TIMEOUT,
    log_file: Path | None = None,
) -> None:
    """Run ``wine <installer> --silent``; a failure raises :class:`SetupFailed` for this step."""
    argv = [os.fspath(wine), pathmap.unix_to_win(installer), "--silent"]
    hint = "run 'fork-linux setup' again to retry"
    if log_file is not None:
        hint = f"see {log_file}; {hint}"
    try:
        result = runner.run(argv, env=env, cwd=Path(installer).parent, timeout=timeout, log_file=log_file)
    except ForkLinuxError as exc:
        raise SetupFailed(STEP, f"the Fork installer did not finish: {exc}", hint=hint) from exc
    if result.returncode != 0:
        detail = tail(result.stderr)
        message = f"the Fork installer exited with code {result.returncode}"
        raise SetupFailed(STEP, f"{message}\n{detail}" if detail else message, hint=hint)


def _inside(path: Path, root: Path) -> bool:
    return path == root or path.is_relative_to(root)


def finish_install(layout: ForkLayout, plan: InstallPlan) -> str:
    """Check that ``plan.version`` is installed and drop the installer's Wine desktop shortcut.

    The shortcut is removed only when it is a regular file whose directory
    lies inside the prefix; symlinks are never followed. Returns the installed version.
    """
    installed = layout.installed_version()
    if installed is None or versions.Version(installed) != versions.Version(plan.version):
        raise SetupFailed(
            STEP,
            f"Fork {plan.version} was not installed (found {installed or 'no installed version'})",
            hint="run 'fork-linux setup' again; if it keeps failing, run it with --debug and report the log",
        )
    lnk = layout.desktop_lnk
    try:
        mode = os.lstat(lnk).st_mode
    except OSError:
        return installed
    if stat.S_ISREG(mode) and _inside(lnk.parent.resolve(), layout.paths.prefix.resolve()):
        lnk.unlink()
    return installed


def verify_nupkg(layout: ForkLayout, version: str, expected_sha256: str) -> bool:
    """True if ``packages/Fork-<version>-full.nupkg`` is a regular file with sha256 ``expected_sha256``."""
    if not isinstance(version, str) or _PLAIN_VERSION.fullmatch(version) is None:
        return False
    package = layout.packages_dir / f"Fork-{version}-full.nupkg"
    try:
        if not stat.S_ISREG(os.lstat(package).st_mode):
            return False
    except OSError:
        return False
    return fsutil.sha256_file(package) == expected_sha256.strip().lower()
