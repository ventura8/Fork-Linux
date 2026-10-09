"""The pinned runtime manifest: Wine builds, winetricks, .NET verbs and Fork versions.

The manifest ships with the package (``data/runtime-manifest.json``); there is no
remote manifest. An optional user override file is applied on top of it as an
RFC 7386 JSON merge patch (objects merge recursively, ``null`` deletes a key,
anything else replaces), and the merged document is validated strictly. Every
problem raises :class:`~fork_linux.errors.IntegrityFailed`.

Keys starting with ``_`` are comments and are ignored at every level. The only
placeholder allowed is the literal ``"PENDING"`` for a Fork installer sha256,
which means "not pinned yet": installing that version needs the explicit TOFU
flag (``--latest``), exactly like a version that is not in the manifest at all.
All sha256 values are exposed lowercase.

An override may pin other versions or builds, but it may not add hosts to
``fork.allowed_hosts``: the Fork installer only ever comes from the official
hosts listed in the packaged manifest (AGENTS.md hard rule 1).
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import resources, versions
from .errors import IntegrityFailed, NotFound, UsageError

SCHEMA = 1
PENDING = "PENDING"
STATUSES = ("known-good", "testing", "untested", "known-bad")
ARCHES = ("x86_64",)
DEFAULT_PATH = resources.manifest_path()

_HINT = "reinstall fork-linux, or fix or remove your manifest override file"
_MISSING = object()
_SHA256 = re.compile(r"[0-9a-f]{64}")
_SHA256_ANY_CASE = re.compile(r"[0-9a-fA-F]{64}")
_REVISION = re.compile(r"[0-9]{4}\.[0-9]{1,2}\.[0-9]{1,4}")
_PLAIN_VERSION = re.compile(r"[0-9]{1,6}(?:\.[0-9]{1,6}){0,3}")
_BUILD_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_TOKEN = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
_VERB = re.compile(r"dotnet[0-9]{2,3}")
_WINETRICKS_VERSION = re.compile(r"[0-9]{8}")
_HOSTNAME = re.compile(r"(?=.{1,253}\Z)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*")


@dataclass(frozen=True)
class WineBuild:
    """One pinned Wine runtime tarball."""

    id: str
    version: str
    flavor: str
    arch: str
    wow64: bool
    url: str
    sha256: str
    size: int
    strip_components: int
    min_glibc: str
    status: str


@dataclass(frozen=True)
class Winetricks:
    """The pinned winetricks script."""

    version: str
    url: str
    sha256: str
    size: int


@dataclass(frozen=True)
class ForkVersion:
    """A Fork for Windows release the manifest knows about."""

    version: str
    size: int
    sha256: str | None
    full_nupkg_sha256: str
    status: str
    tested_with: tuple[str, ...]


@dataclass(frozen=True)
class InstallerEntry:
    """Where to download a Fork installer and how to verify it."""

    version: str
    url: str
    size: int | None
    sha256: str | None

    @property
    def requires_tofu(self) -> bool:
        """True when no sha256 is pinned, so only an explicit ``--latest`` may install it."""
        return self.sha256 is None


def _fail(where: str, problem: str) -> IntegrityFailed:
    return IntegrityFailed(f"runtime manifest: {where}: {problem}", hint=_HINT)


def _dict(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _fail(where, "must be an object")
    return value


def _entries(value: Any, where: str) -> dict[str, Any]:
    """An object used as a map (builds, versions, known_bad), minus ``_`` comment keys."""
    return {key: item for key, item in _dict(value, where).items() if not key.startswith("_")}


def _obj(value: Any, where: str, required: tuple[str, ...], optional: tuple[str, ...] = ()) -> dict[str, Any]:
    obj = _dict(value, where)
    missing = [key for key in required if key not in obj]
    if missing:
        raise _fail(where, f"missing key(s): {', '.join(missing)}")
    unknown = sorted(k for k in obj if k not in required and k not in optional and not k.startswith("_"))
    if unknown:
        raise _fail(where, f"unknown key(s): {', '.join(unknown)}")
    return obj


def _str(value: Any, where: str, pattern: re.Pattern[str] | None = None) -> str:
    if not isinstance(value, str) or not value:
        raise _fail(where, "must be a non-empty string")
    if pattern is not None and pattern.fullmatch(value) is None:
        raise _fail(where, f"invalid value {value!r}")
    return value


def _int(value: Any, where: str, minimum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise _fail(where, f"must be an integer >= {minimum}")
    return value


def _bool(value: Any, where: str) -> bool:
    if not isinstance(value, bool):
        raise _fail(where, "must be true or false")
    return value


def _choice(value: Any, where: str, choices: tuple[str, ...]) -> str:
    text = _str(value, where)
    if text not in choices:
        raise _fail(where, f"must be one of {', '.join(choices)}")
    return text


def _version(value: Any, where: str) -> str:
    return _str(value, where, _PLAIN_VERSION)


def _str_list(value: Any, where: str, pattern: re.Pattern[str], *, allow_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list) or (not value and not allow_empty):
        raise _fail(where, "must be a non-empty list" if not allow_empty else "must be a list")
    items = tuple(_str(item, f"{where}[{index}]", pattern) for index, item in enumerate(value))
    if len(set(items)) != len(items):
        raise _fail(where, "contains duplicate entries")
    return items


def _https(value: Any, where: str, hosts: tuple[str, ...] | None = None) -> str:
    url = _str(value, where)
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise _fail(where, f"not a valid URL: {url!r}") from None
    if (
        parts.scheme != "https"
        or _HOSTNAME.fullmatch(parts.hostname or "") is None
        or "@" in parts.netloc
        or port == 0
        or not url.isascii()
        or any(ch.isspace() or ord(ch) < 0x20 or ch == "\x7f" for ch in url)
    ):
        raise _fail(where, f"must be a plain https:// URL, got {url!r}")
    if hosts is not None and parts.hostname not in hosts:
        raise _fail(where, f"host {parts.hostname!r} is not in fork.allowed_hosts")
    return url


def _parse_build(build_id: str, value: Any) -> WineBuild:
    where = f"wine.builds.{build_id}"
    _str(build_id, where, _BUILD_ID)
    build = _obj(
        value,
        where,
        ("version", "flavor", "arch", "wow64", "url", "sha256", "size", "strip_components", "min_glibc", "status"),
    )
    return WineBuild(
        id=build_id,
        version=_version(build["version"], f"{where}.version"),
        flavor=_str(build["flavor"], f"{where}.flavor", _TOKEN),
        arch=_choice(build["arch"], f"{where}.arch", ARCHES),
        wow64=_bool(build["wow64"], f"{where}.wow64"),
        url=_https(build["url"], f"{where}.url"),
        sha256=_str(build["sha256"], f"{where}.sha256", _SHA256),
        size=_int(build["size"], f"{where}.size", 1),
        strip_components=_int(build["strip_components"], f"{where}.strip_components", 0),
        min_glibc=_version(build["min_glibc"], f"{where}.min_glibc"),
        status=_choice(build["status"], f"{where}.status", STATUSES),
    )


def _parse_fork_version(key: str, value: Any) -> ForkVersion:
    where = f"fork.versions.{key}"
    entry = _obj(value, where, ("size", "sha256", "full_nupkg_sha256", "status", "tested_with"))
    sha = entry["sha256"]
    return ForkVersion(
        version=key,
        size=_int(entry["size"], f"{where}.size", 1),
        sha256=None if sha == PENDING else _str(sha, f"{where}.sha256", _SHA256),
        full_nupkg_sha256=_str(entry["full_nupkg_sha256"], f"{where}.full_nupkg_sha256", _SHA256_ANY_CASE).lower(),
        status=_choice(entry["status"], f"{where}.status", STATUSES),
        tested_with=_str_list(entry["tested_with"], f"{where}.tested_with", _BUILD_ID, allow_empty=True),
    )


def _unique_versions(raw: dict[str, Any], where: str) -> dict[versions.Version, str]:
    """Map each key's :class:`Version` to the key, rejecting equal spellings like ``2.1``/``2.1.0``."""
    out: dict[versions.Version, str] = {}
    for key in raw:
        parsed = versions.Version(_version(key, f"{where}.{key}"))
        if parsed in out:
            raise _fail(where, f"{key!r} duplicates {out[parsed]!r}")
        out[parsed] = key
    return out


class Manifest:
    """A validated, read-only view of the runtime manifest."""

    def __init__(self, data: Any, *, overridden: bool = False) -> None:
        top = _obj(data, "manifest", ("schema", "revision", "bootstrap_revision", "wine", "winetricks", "dotnet", "fork"))
        schema = _int(top["schema"], "schema", 1)
        if schema != SCHEMA:
            raise _fail("schema", f"unsupported schema {schema} (this fork-linux understands {SCHEMA})")
        self._data = copy.deepcopy(top)
        self.overridden = overridden
        self.revision = _str(top["revision"], "revision", _REVISION)
        self.bootstrap_revision = _int(top["bootstrap_revision"], "bootstrap_revision", 1)
        self._load_wine(top["wine"])
        self._load_winetricks(top["winetricks"])
        self._load_dotnet(top["dotnet"])
        self._load_fork(top["fork"])

    def _load_wine(self, value: Any) -> None:
        wine = _obj(value, "wine", ("default", "min_system_version", "builds"))
        self.min_system_wine = _version(wine["min_system_version"], "wine.min_system_version")
        builds = _entries(wine["builds"], "wine.builds")
        if not builds:
            raise _fail("wine.builds", "must list at least one build")
        self._wine_builds = {build_id: _parse_build(build_id, raw) for build_id, raw in builds.items()}
        default = _str(wine["default"], "wine.default", _BUILD_ID)
        if default not in self._wine_builds:
            raise _fail("wine.default", f"{default!r} is not in wine.builds")
        if self._wine_builds[default].status == "known-bad":
            raise _fail("wine.default", f"{default!r} is marked known-bad")
        self._wine_default = default

    def _load_winetricks(self, value: Any) -> None:
        tricks = _obj(value, "winetricks", ("version", "url", "sha256", "size"))
        self.winetricks = Winetricks(
            version=_str(tricks["version"], "winetricks.version", _WINETRICKS_VERSION),
            url=_https(tricks["url"], "winetricks.url"),
            sha256=_str(tricks["sha256"], "winetricks.sha256", _SHA256),
            size=_int(tricks["size"], "winetricks.size", 1),
        )

    def _load_dotnet(self, value: Any) -> None:
        dotnet = _obj(value, "dotnet", ("verbs", "min_release"))
        self.dotnet_verbs = _str_list(dotnet["verbs"], "dotnet.verbs", _VERB)
        self.dotnet_min_release = _int(dotnet["min_release"], "dotnet.min_release", 1)

    def _load_fork(self, value: Any) -> None:
        fork = _obj(
            value,
            "fork",
            ("default", "installer_url_template", "feed_url", "legacy_feed_url", "allowed_hosts", "requires", "versions"),
            ("known_bad",),
        )
        self.allowed_hosts = _str_list(fork["allowed_hosts"], "fork.allowed_hosts", _HOSTNAME)
        template = _str(fork["installer_url_template"], "fork.installer_url_template")
        rest = template.replace("{version}", "")
        if template.count("{version}") != 1 or "{" in rest or "}" in rest:
            raise _fail("fork.installer_url_template", "must contain {version} exactly once and no other braces")
        _https(template.replace("{version}", "0"), "fork.installer_url_template", self.allowed_hosts)
        self._installer_template = template
        self.feed_url = _https(fork["feed_url"], "fork.feed_url", self.allowed_hosts)
        self.legacy_feed_url = _https(fork["legacy_feed_url"], "fork.legacy_feed_url", self.allowed_hosts)
        requires = _obj(fork["requires"], "fork.requires", ("dotnet_framework",))
        self.dotnet_framework = _version(requires["dotnet_framework"], "fork.requires.dotnet_framework")

        raw_versions = _entries(fork["versions"], "fork.versions")
        if not raw_versions:
            raise _fail("fork.versions", "must list at least one version")
        keys = _unique_versions(raw_versions, "fork.versions")
        self._fork_versions = {parsed: _parse_fork_version(key, raw_versions[key]) for parsed, key in keys.items()}

        raw_bad = _entries(fork.get("known_bad", {}), "fork.known_bad")
        bad_keys = _unique_versions(raw_bad, "fork.known_bad")
        self._known_bad = {parsed: _str(raw_bad[key], f"fork.known_bad.{key}") for parsed, key in bad_keys.items()}

        default = _version(fork["default"], "fork.default")
        if versions.Version(default) not in self._fork_versions:
            raise _fail("fork.default", f"{default!r} has no entry in fork.versions")
        if self.is_known_bad(default):
            raise _fail("fork.default", f"{default!r} is marked known-bad")
        self.fork_default = default

    # -- Wine ---------------------------------------------------------------

    @property
    def wine_default(self) -> WineBuild:
        """The Wine build used by the managed provider unless the user picks another."""
        return self._wine_builds[self._wine_default]

    @property
    def wine_builds(self) -> tuple[WineBuild, ...]:
        """Every pinned Wine build, in manifest order."""
        return tuple(self._wine_builds.values())

    def wine_build(self, build_id: str) -> WineBuild:
        """Return the build called ``build_id``; raises :class:`NotFound` if there is none."""
        try:
            return self._wine_builds[build_id]
        except KeyError:
            known = ", ".join(self._wine_builds)
            raise NotFound(f"unknown Wine build {build_id!r}", hint=f"known builds: {known}") from None

    # -- Fork ---------------------------------------------------------------

    def fork_version(self, version: str) -> ForkVersion | None:
        """The manifest entry for ``version`` (``2.23.2`` == ``2.23.2.0``), or None."""
        if not isinstance(version, str) or not versions.is_valid(version):
            return None
        return self._fork_versions.get(versions.Version(version))

    def known_bad_reason(self, version: str) -> str | None:
        """Why ``version`` is known-bad, or None if it is not."""
        if not isinstance(version, str) or not versions.is_valid(version):
            return None
        parsed = versions.Version(version)
        if parsed in self._known_bad:
            return self._known_bad[parsed]
        entry = self._fork_versions.get(parsed)
        if entry is not None and entry.status == "known-bad":
            return f"Fork {entry.version} is marked known-bad in the runtime manifest"
        return None

    def is_known_bad(self, version: str) -> bool:
        """True if ``version`` is listed in ``known_bad`` or has status ``known-bad``."""
        return self.known_bad_reason(version) is not None

    def known_good_versions(self) -> list[str]:
        """Versions with status ``known-good`` that are not known-bad, newest first."""
        good = [
            entry.version
            for entry in self._fork_versions.values()
            if entry.status == "known-good" and not self.is_known_bad(entry.version)
        ]
        return sorted(good, key=versions.Version, reverse=True)

    def installer_url(self, version: str) -> str:
        """The official installer URL for ``version``; the host is checked against allowed_hosts."""
        if not isinstance(version, str) or _PLAIN_VERSION.fullmatch(version) is None:
            raise UsageError(f"not a Fork version: {version!r}", hint="use a dotted number such as 2.23.2")
        url = self._installer_template.replace("{version}", version)
        return _https(url, "fork.installer_url_template", self.allowed_hosts)

    def installer_entry(self, version: str | None = None) -> InstallerEntry:
        """Download details for ``version`` (default: ``fork.default``).

        A version missing from the manifest, or whose sha256 is still ``PENDING``,
        comes back with ``sha256=None`` (``requires_tofu``). Known-bad versions are
        returned as well; callers check :meth:`is_known_bad` first.
        """
        chosen = self.fork_default if version is None else version
        url = self.installer_url(chosen)
        entry = self.fork_version(chosen)
        if entry is None:
            return InstallerEntry(version=chosen, url=url, size=None, sha256=None)
        return InstallerEntry(version=chosen, url=url, size=entry.size, sha256=entry.sha256)

    def as_dict(self) -> dict[str, Any]:
        """A deep copy of the merged, validated manifest document."""
        return copy.deepcopy(self._data)


def merge_patch(target: Any, patch: Any) -> Any:
    """Apply an RFC 7386 JSON merge patch to ``target`` and return the result (inputs untouched)."""
    if not isinstance(patch, dict):
        return copy.deepcopy(patch)
    result = copy.deepcopy(target) if isinstance(target, dict) else {}
    for key, value in patch.items():
        if value is None:
            result.pop(key, None)
        else:
            result[key] = merge_patch(result.get(key), value)
    return result


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"duplicate key {key!r}")
        out[key] = value
    return out


def _reject_constant(name: str) -> Any:
    raise ValueError(f"{name} is not allowed")


def _read_json(path: Path, *, missing_ok: bool = False) -> Any:
    """Parse ``path`` strictly; with ``missing_ok`` a file that does not exist gives ``_MISSING``."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        if missing_ok:
            return _MISSING
        raise IntegrityFailed(f"cannot read runtime manifest {path}: {exc}", hint=_HINT) from None
    except (OSError, UnicodeDecodeError) as exc:
        raise IntegrityFailed(f"cannot read runtime manifest {path}: {exc}", hint=_HINT) from None
    try:
        return json.loads(text, object_pairs_hook=_no_duplicates, parse_constant=_reject_constant)
    except (ValueError, RecursionError) as exc:
        raise IntegrityFailed(f"runtime manifest {path} is not valid JSON: {exc}", hint=_HINT) from None


def load(path: Path | None = None, override: Path | None = None) -> Manifest:
    """Load the packaged manifest (or ``path``), apply ``override`` if that file exists, and validate.

    The base manifest must be valid on its own; the merged result is validated
    again and may not list download hosts the base does not.
    """
    data = _read_json(DEFAULT_PATH if path is None else Path(path))
    base = Manifest(data)
    patch = _MISSING if override is None else _read_json(Path(override), missing_ok=True)
    if patch is _MISSING:
        return base
    if not isinstance(patch, dict):
        raise IntegrityFailed(f"manifest override {override} must be a JSON object", hint=_HINT)
    try:
        merged = Manifest(merge_patch(data, patch), overridden=True)
    except RecursionError:
        raise IntegrityFailed(f"manifest override {override} is nested too deeply", hint=_HINT) from None
    added = sorted(set(merged.allowed_hosts) - set(base.allowed_hosts))
    if added:
        raise IntegrityFailed(
            f"manifest override {override} may not add Fork download hosts: {', '.join(added)}",
            hint="Fork installers only come from the official hosts; remove fork.allowed_hosts from the override",
        )
    return merged
