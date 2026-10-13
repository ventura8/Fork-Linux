"""Pick the Wine that runs Fork: managed (pinned tarball), system, flatpak or a custom path.

* ``managed`` (the default outside Flatpak): a manifest-pinned Wine build,
  downloaded with a sha256 check and extracted safely into
  ``runtimes/wine/<build id>/``. A ``.complete`` marker holding the verified
  sha256 is written last, so an interrupted install is never mistaken for a
  finished one.
* ``system``: ``wine`` from ``PATH``, at least the manifest's minimum version.
* ``flatpak``: the ``org.winehq.Wine`` BaseApp mounted at ``/app`` inside our
  Flatpak (preferred there unless the user picked a provider).
* an absolute path: a Wine root containing ``bin/wine``, or that binary.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Callable, Mapping
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from . import download, fsutil, sandbox, versions
from .config import SOURCE_DEFAULT, Config
from .errors import IntegrityFailed, UsageError, WineUnavailable
from .manifest import Manifest, WineBuild
from .paths import Paths
from .procrun import Runner
from .winecmd import WineInfo, query_version

log = logging.getLogger(__name__)

COMPLETE_MARKER = ".complete"
WINE_STORE = "wine"
STAGING_INFIX = ".tmp-"
FLATPAK_ROOT = Path("/app")
# Hosts the managed Wine tarball may come from (AGENTS.md hard rule 7); GitHub
# then redirects to its release-asset CDN, which fetch() allows.
WINE_DOWNLOAD_HOSTS = ("github.com",)
# Where distributions hide wineserver when it is not on PATH (Debian/Ubuntu).
SYSTEM_WINESERVER_FALLBACKS = (Path("/usr/lib/wine/wineserver64"), Path("/usr/lib/wine/wineserver"))
# Wine library directories relative to a Wine root: upstream/Kron4ek, Fedora/SUSE, Debian.
WINE_LIB_DIRS = ("lib/wine", "lib64/wine", "lib32/wine", "lib/x86_64-linux-gnu/wine", "lib/i386-linux-gnu/wine")

_BUILD_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}")

Progress = Callable[[int, int | None], None]


def managed_root(paths: Paths, build_id: str) -> Path:
    """``<data>/runtimes/wine/<build_id>``; the id must be a single safe path component."""
    if _BUILD_ID.fullmatch(build_id) is None:
        raise UsageError(f"not a Wine build id: {build_id!r}", hint="build ids come from the runtime manifest")
    return paths.runtimes_dir / WINE_STORE / build_id


def _marker_sha(root: Path) -> str | None:
    """The sha256 recorded in ``root``'s complete marker, or None if there is no usable marker."""
    marker = root / COMPLETE_MARKER
    if marker.is_symlink() or not marker.is_file():
        return None
    try:
        return marker.read_text(encoding="utf-8").strip().lower()
    except (OSError, UnicodeDecodeError):
        return None


def is_installed(paths: Paths, build: WineBuild) -> bool:
    """True if ``build`` is fully installed: marker with its sha256 and ``bin/wine`` present."""
    root = managed_root(paths, build.id)
    return _marker_sha(root) == build.sha256 and (root / "bin" / "wine").is_file()


def managed_info(build: WineBuild, root: Path) -> WineInfo:
    """The :class:`WineInfo` of an installed managed build (from the manifest, no Wine started)."""
    return WineInfo(
        provider="managed",
        build_id=build.id,
        root=root,
        wine=root / "bin" / "wine",
        wineserver=root / "bin" / "wineserver",
        version=build.version,
        staging=build.flavor == "staging",
        wow64=build.wow64,
    )


def _archive_name(build: WineBuild) -> str:
    """The cache file name for ``build``: the URL's basename (or a name derived from the id)."""
    name = PurePosixPath(urlsplit(build.url).path).name
    return name or f"wine-{build.id}.tar"


def _pid_alive(pid: int) -> bool:
    """True if process ``pid`` exists (possibly owned by someone else)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _remove_stale_staging(store: Path) -> None:
    """Delete ``<id>.tmp-<pid>`` directories left behind by installs that were killed."""
    for entry in store.iterdir():
        _head, sep, pid = entry.name.rpartition(STAGING_INFIX)
        if sep and pid.isdigit() and int(pid) != os.getpid() and not _pid_alive(int(pid)):
            log.info("removing an interrupted Wine runtime install: %s", entry)
            fsutil.safe_rmtree(entry)


def _verify_extracted(runner: Runner, staging: Path, build: WineBuild) -> None:
    """The extracted tree must have ``bin/wine`` + ``bin/wineserver`` and report the pinned version."""
    wine = staging / "bin" / "wine"
    if not (wine.is_file() and (staging / "bin" / "wineserver").is_file()):
        raise IntegrityFailed(
            f"Wine build {build.id} has no bin/wine and bin/wineserver after extraction",
            hint="the runtime manifest's strip_components may be wrong for this archive",
        )
    # Inherited WINE* variables belong to some other Wine setup (another prefix); drop them.
    probe_env = {key: value for key, value in sandbox.clean_env(os.environ).items() if not key.startswith("WINE")}
    version, _staging = query_version(runner, wine, probe_env)
    if versions.Version(version) != versions.Version(build.version):
        raise IntegrityFailed(
            f"Wine build {build.id} reports version {version}, but the manifest pins {build.version}",
            hint="the runtime manifest entry does not match its archive; nothing was installed",
        )


def install_managed(
    build: WineBuild, paths: Paths, runner: Runner, *, offline: bool = False, progress: Progress | None = None
) -> Path:
    """Download, verify and extract ``build``; return its root. Idempotent.

    A root whose marker already records ``build.sha256`` is returned at once.
    Otherwise the verified archive is extracted into ``<root>.tmp-<pid>``,
    ``bin/wine --version`` must run and match the pinned version, then the tree
    replaces any incomplete earlier attempt and the marker is written last.
    """
    root = managed_root(paths, build.id)
    if is_installed(paths, build):
        return root
    archive = download.fetch(
        build.url,
        _archive_name(build),
        cache_dir=paths.downloads_dir,
        sha256=build.sha256,
        size=build.size,
        offline=offline,
        progress=progress,
        allowed_hosts=WINE_DOWNLOAD_HOSTS,
    )
    store = fsutil.ensure_dir(root.parent)
    _remove_stale_staging(store)
    staging = store / f"{build.id}{STAGING_INFIX}{os.getpid()}"
    fsutil.safe_rmtree(staging)
    try:
        fsutil.safe_extract(archive, staging, strip_components=build.strip_components)
        _verify_extracted(runner, staging, build)
        fsutil.safe_rmtree(root)
        os.replace(staging, root)
    except BaseException:
        fsutil.safe_rmtree(staging)
        raise
    fsutil.atomic_write(root / COMPLETE_MARKER, build.sha256 + "\n", mode=0o644)
    log.info("installed Wine runtime %s in %s", build.id, root)
    return root


def is_wow64_layout(root: Path) -> bool:
    """Heuristic: is the Wine under ``root`` a new-style WoW64 build?

    New-style WoW64 builds (Wine >= 9 built with ``--enable-archs``, e.g.
    Kron4ek's ``-wow64`` tarballs) ship 32-bit PE modules (``i386-windows``)
    but no 32-bit Unix libraries (``i386-unix``), so 32-bit Windows programs
    run without i386 host libraries. Classic multilib builds have
    ``i386-unix``; a 64-bit-only build has no ``i386-windows`` at all. Both are
    reported as ``False``. The directories are looked for in every
    :data:`WINE_LIB_DIRS` location (upstream, Fedora/SUSE and Debian layouts).
    """
    lib_dirs = [root / relative for relative in WINE_LIB_DIRS]
    has_pe32 = any((lib / "i386-windows").is_dir() for lib in lib_dirs)
    has_unix32 = any((lib / "i386-unix").is_dir() for lib in lib_dirs)
    return has_pe32 and not has_unix32


def _system_wineserver(runner: Runner, env: Mapping[str, str], root: Path) -> Path | None:
    """wineserver from PATH, else ``<root>/bin``, else the Debian/Ubuntu private locations."""
    found = runner.which("wineserver", path=env.get("PATH"))
    if found is not None:
        return Path(found)
    for candidate in (root / "bin" / "wineserver", *SYSTEM_WINESERVER_FALLBACKS):
        if candidate.is_file():
            return candidate
    return None


def system_wine(runner: Runner, env: Mapping[str, str], min_version: str) -> WineInfo | None:
    """The distribution's Wine if it is on ``PATH``, runs, and is at least ``min_version``; else None.

    ``root`` is the parent of the directory holding ``wine`` (``/usr`` for
    ``/usr/bin/wine``); ``wow64`` comes from :func:`is_wow64_layout`, applied
    to ``root`` and, when ``wine`` is a symlink (WineHQ's ``/usr/bin/wine`` ->
    ``/opt/wine-staging/bin/wine``), to the root of its target as well.
    """
    found = runner.which("wine", path=env.get("PATH"))
    if found is None:
        log.info("system Wine: no 'wine' on PATH")
        return None
    wine = Path(found)
    root = wine.parent.parent
    server = _system_wineserver(runner, env, root)
    if server is None:
        log.info("system Wine: %s has no wineserver", wine)
        return None
    try:
        version, staging = query_version(runner, wine, env)
    except WineUnavailable as exc:
        log.info("system Wine: %s", exc)
        return None
    if versions.Version(version) < versions.Version(min_version):
        log.info("system Wine: %s is version %s, older than %s", wine, version, min_version)
        return None
    return WineInfo(
        provider="system",
        build_id=None,
        root=root,
        wine=wine,
        wineserver=server,
        version=version,
        staging=staging,
        wow64=is_wow64_layout(root) or is_wow64_layout(wine.resolve().parent.parent),
    )


def flatpak_wine(env: Mapping[str, str], app_root: Path = FLATPAK_ROOT) -> WineInfo | None:
    """The BaseApp Wine at ``app_root`` when running inside our Flatpak, else None.

    No Wine is started here, so ``version`` is ``""`` and ``staging`` False;
    :func:`resolve` fills both in with one ``wine --version``.
    """
    if sandbox.detect(env) != sandbox.FLATPAK:
        return None
    wine = app_root / "bin" / "wine"
    if not wine.is_file():
        return None
    return WineInfo(
        provider="flatpak",
        build_id=None,
        root=app_root,
        wine=wine,
        wineserver=app_root / "bin" / "wineserver",
        version="",
        staging=False,
        wow64=is_wow64_layout(app_root),
    )


def custom_wine(runner: Runner, env: Mapping[str, str], location: Path) -> WineInfo:
    """A user-chosen Wine: a root directory containing ``bin/wine``, or the ``wine`` binary itself."""
    if location.is_dir():
        root, wine = location, location / "bin" / "wine"
    else:
        root, wine = location.parent.parent, location
    server = wine.parent / "wineserver"
    if not (wine.is_file() and os.access(wine, os.X_OK) and server.is_file()):
        raise WineUnavailable(
            f"no usable Wine at {location}: expected executable bin/wine and bin/wineserver",
            hint="set wine.provider to a Wine root directory or its bin/wine, or back to managed",
        )
    version, staging = query_version(runner, wine, env)
    return WineInfo(
        provider="custom",
        build_id=None,
        root=root,
        wine=wine,
        wineserver=server,
        version=version,
        staging=staging,
        wow64=is_wow64_layout(root),
    )


def _with_version(info: WineInfo, runner: Runner, env: Mapping[str, str]) -> WineInfo:
    """``info`` with ``version``/``staging`` from ``wine --version`` (flatpak provider)."""
    version, staging = query_version(runner, info.wine, env)
    return WineInfo(
        provider=info.provider,
        build_id=info.build_id,
        root=info.root,
        wine=info.wine,
        wineserver=info.wineserver,
        version=version,
        staging=staging,
        wow64=info.wow64,
    )


def _resolve_managed(
    config: Config,
    manifest: Manifest,
    paths: Paths,
    runner: Runner,
    *,
    install: bool,
    offline: bool,
    progress: Progress | None,
) -> WineInfo:
    """The managed build (``wine.build`` or the manifest default), installing it if allowed."""
    build = manifest.wine_build(config.get("wine", "build") or manifest.wine_default.id)
    if build.status == "known-bad":
        raise WineUnavailable(
            f"the Wine build {build.id} is marked known-bad in the runtime manifest",
            hint="run 'fork-linux config unset wine.build' to use the tested default",
        )
    root = managed_root(paths, build.id)
    if not is_installed(paths, build):
        if not install:
            raise WineUnavailable(
                f"the managed Wine runtime {build.id} is not installed", hint="run 'fork-linux setup'"
            )
        root = install_managed(build, paths, runner, offline=offline, progress=progress)
    return managed_info(build, root)


def resolve(
    config: Config,
    manifest: Manifest,
    paths: Paths,
    runner: Runner,
    env: Mapping[str, str],
    *,
    choice: str | None = None,
    install: bool = False,
    offline: bool = False,
    progress: Progress | None = None,
) -> WineInfo:
    """The Wine to use: ``choice`` (e.g. ``setup --wine``) over ``[wine] provider``.

    Inside our Flatpak, with the provider left at its default, the BaseApp Wine
    is preferred. ``install=True`` lets the managed provider download its
    build; otherwise a missing build raises :class:`WineUnavailable`.
    """
    provider = choice or config.get("wine", "provider")
    if not choice and config.source("wine", "provider") == SOURCE_DEFAULT:
        bundled = flatpak_wine(env, FLATPAK_ROOT)
        if bundled is not None:
            return _with_version(bundled, runner, env)
    if provider == "managed":
        return _resolve_managed(
            config, manifest, paths, runner, install=install, offline=offline, progress=progress
        )
    if provider == "system":
        found = system_wine(runner, env, manifest.min_system_wine)
        if found is None:
            raise WineUnavailable(
                f"no usable system Wine: need 'wine' {manifest.min_system_wine} or newer with wineserver on PATH",
                hint="install your distribution's wine package, or use the managed Wine (wine.provider=managed)",
            )
        if not found.staging:
            log.warning(
                "system Wine %s is not a staging build: hooks and bash custom commands may hang (Wine bug 55138)",
                found.version,
            )
        return found
    if provider == "flatpak":
        bundled = flatpak_wine(env, FLATPAK_ROOT)
        if bundled is None:
            raise WineUnavailable(
                "the flatpak Wine provider only works inside the Fork for Linux (unofficial) Flatpak",
                hint="use wine.provider=managed or system",
            )
        return _with_version(bundled, runner, env)
    if os.path.isabs(provider):
        return custom_wine(runner, env, Path(provider))
    raise UsageError(
        f"unknown Wine provider {provider!r}", hint="use managed, system, flatpak or an absolute path to Wine"
    )


def installed_builds(paths: Paths) -> list[str]:
    """Ids of the managed Wine builds with a complete marker, sorted."""
    store = paths.runtimes_dir / WINE_STORE
    try:
        entries = sorted(store.iterdir())
    except OSError:
        return []
    return [
        entry.name
        for entry in entries
        if not entry.is_symlink() and entry.is_dir() and _marker_sha(entry) is not None
    ]


def prune(paths: Paths, keep: set[str]) -> list[str]:
    """Delete complete managed builds not in ``keep``; return their ids.

    Only directories carrying the complete marker are removed (through
    :func:`fsutil.safe_rmtree` with that marker); anything else in the store is
    left alone.
    """
    removed = []
    for build_id in installed_builds(paths):
        if build_id in keep:
            continue
        root = paths.runtimes_dir / WINE_STORE / build_id
        fsutil.safe_rmtree(root, marker=root / COMPLETE_MARKER)
        log.info("removed Wine runtime %s", build_id)
        removed.append(build_id)
    return removed
