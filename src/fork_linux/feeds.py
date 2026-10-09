"""Fork's update feeds: Velopack ``releases.win.json`` and the legacy Squirrel ``RELEASES``.

Both feeds are untrusted input. Parsing is strict (types, versions, file names,
hash formats) and raises :class:`~fork_linux.errors.IntegrityFailed` on anything
malformed; unknown JSON keys are ignored. Asset order is never trusted:
:func:`latest_full` compares versions. Hashes are exposed lowercase.

:func:`fetch_feed` keeps one cached copy per ``cache_dir`` (``feed.json`` plus
``feed.meta.json``) and revalidates it with ``If-None-Match`` /
``If-Modified-Since`` once it is older than ``max_age``. Use a separate
``cache_dir`` per feed URL; a cache written for another URL counts as a miss.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import re
import time
import urllib.error
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import download, fsutil, versions
from .errors import DownloadFailed, IntegrityFailed

FEED_MAX_BYTES = 5 * 1024 * 1024
META_MAX_BYTES = 64 * 1024
FEED_FILE = "feed.json"
META_FILE = "feed.meta.json"
TYPES = ("Full", "Delta")

_FILENAME = re.compile(r"Fork-([0-9]{1,6}(?:\.[0-9]{1,6}){0,3})-(full|delta)\.nupkg")
_SHA1 = re.compile(r"[0-9a-fA-F]{40}")
_SHA256 = re.compile(r"[0-9a-fA-F]{64}")
_SIZE = re.compile(r"[0-9]{1,15}")
_HINT = "Fork's update feed looks malformed; try again later or report it to ventura8/Fork-Linux"
_BOM = "\ufeff"


@dataclass(frozen=True)
class FeedAsset:
    """One package listed in a feed. ``sha256`` is None for the legacy feed."""

    version: str
    type: str
    filename: str
    sha256: str | None
    sha1: str | None
    size: int


def _bad(problem: str) -> IntegrityFailed:
    return IntegrityFailed(f"update feed: {problem}", hint=_HINT)


def _dedupe(assets: list[FeedAsset]) -> list[FeedAsset]:
    """Drop exact repeats (the real RELEASES has some); conflicting repeats are an error."""
    seen: dict[str, FeedAsset] = {}
    for asset in assets:
        prior = seen.setdefault(asset.filename, asset)
        if prior != asset:
            raise _bad(f"conflicting entries for {asset.filename}")
    return list(seen.values())


def _reject_constant(name: str) -> Any:
    raise ValueError(f"{name} is not allowed")


def _json_asset(index: int, raw: Any) -> FeedAsset:
    where = f"Assets[{index}]"
    if not isinstance(raw, dict):
        raise _bad(f"{where} is not an object")
    version = raw.get("Version")
    kind = raw.get("Type")
    filename = raw.get("FileName")
    sha256 = raw.get("SHA256")
    sha1 = raw.get("SHA1")
    size = raw.get("Size")
    if raw.get("PackageId", "Fork") != "Fork":
        raise _bad(f"{where}: unexpected PackageId {raw.get('PackageId')!r}")
    if not isinstance(version, str) or not versions.is_valid(version):
        raise _bad(f"{where}: invalid Version {version!r}")
    if not isinstance(kind, str) or kind not in TYPES:
        raise _bad(f"{where}: invalid Type {kind!r}")
    match = _FILENAME.fullmatch(filename) if isinstance(filename, str) else None
    if match is None or match.group(1) != version or match.group(2) != kind.lower():
        raise _bad(f"{where}: FileName {filename!r} does not match Version {version} / Type {kind}")
    if not isinstance(sha256, str) or _SHA256.fullmatch(sha256) is None:
        raise _bad(f"{where}: invalid SHA256")
    if sha1 is not None and (not isinstance(sha1, str) or _SHA1.fullmatch(sha1) is None):
        raise _bad(f"{where}: invalid SHA1")
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        raise _bad(f"{where}: invalid Size {size!r}")
    return FeedAsset(
        version=version,
        type=kind,
        filename=filename,
        sha256=sha256.lower(),
        sha1=None if sha1 is None else sha1.lower(),
        size=size,
    )


def parse_releases_json(text: str) -> list[FeedAsset]:
    """Parse a Velopack ``releases.win.json`` document."""
    try:
        data = json.loads(text.lstrip(_BOM), parse_constant=_reject_constant)
    except (ValueError, RecursionError) as exc:
        raise _bad(f"not valid JSON ({exc})") from None
    if not isinstance(data, dict) or not isinstance(data.get("Assets"), list):
        raise _bad('expected an object with an "Assets" list')
    return _dedupe([_json_asset(index, raw) for index, raw in enumerate(data["Assets"])])


def parse_releases_legacy(text: str) -> list[FeedAsset]:
    """Parse a legacy Squirrel ``RELEASES`` file (``SHA1 filename size`` per line)."""
    assets = []
    for number, line in enumerate(text.lstrip(_BOM).splitlines(), 1):
        fields = line.split()
        if not fields:
            continue
        match = _FILENAME.fullmatch(fields[1]) if len(fields) == 3 else None
        if (
            match is None
            or _SHA1.fullmatch(fields[0]) is None
            or _SIZE.fullmatch(fields[2]) is None
            or int(fields[2]) <= 0
        ):
            raise _bad(f"RELEASES line {number} is malformed: {line.strip()[:120]!r}")
        assets.append(
            FeedAsset(
                version=match.group(1),
                type=match.group(2).capitalize(),
                filename=fields[1],
                sha256=None,
                sha1=fields[0].lower(),
                size=int(fields[2]),
            )
        )
    return _dedupe(assets)


def latest_full(assets: Iterable[FeedAsset]) -> FeedAsset | None:
    """The full package with the highest version, whatever order the feed lists them in."""
    best: FeedAsset | None = None
    for asset in assets:
        if asset.type == "Full" and (best is None or versions.Version(asset.version) > versions.Version(best.version)):
            best = asset
    return best


def _read_limited(path: Path, limit: int) -> bytes:
    """Read ``path``; raise ``ValueError`` if it holds more than ``limit`` bytes."""
    with open(path, "rb") as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"{path} is larger than {limit} bytes")
    return data


def _read_cache(cache_dir: Path, url: str) -> tuple[str | None, dict[str, Any]]:
    """Return (cached text, meta) when a sound cache for ``url`` exists, else (None, {})."""
    try:
        meta = json.loads(_read_limited(cache_dir / META_FILE, META_MAX_BYTES))
        body = _read_limited(cache_dir / FEED_FILE, FEED_MAX_BYTES)
    except (OSError, ValueError, RecursionError):
        return None, {}
    if (
        not isinstance(meta, dict)
        or meta.get("url") != url
        or meta.get("sha256") != hashlib.sha256(body).hexdigest()
        or not isinstance(meta.get("fetched_at"), (int, float))
        or isinstance(meta.get("fetched_at"), bool)
    ):
        return None, {}
    return body.decode("utf-8", errors="replace"), meta


def _store(cache_dir: Path, meta: dict[str, Any], body: bytes | None = None) -> None:
    """Write the cache; it is only an optimisation, so a failure to write it is ignored."""
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        if body is not None:
            fsutil.atomic_write(cache_dir / FEED_FILE, body)
        fsutil.atomic_write(cache_dir / META_FILE, json.dumps(meta, indent=2, sort_keys=True).encode("utf-8"))
    except OSError:
        return


def _read_capped(response: Any, url: str) -> bytes:
    """Read the whole body: at most ``FEED_MAX_BYTES``, and all the bytes Content-Length announced."""
    announced = download.parse_length(response.headers.get("Content-Length"))
    if announced is not None and announced > FEED_MAX_BYTES:
        raise _bad(f"the feed is larger than {FEED_MAX_BYTES} bytes ({announced} announced)")
    body = response.read(FEED_MAX_BYTES + 1)
    if len(body) > FEED_MAX_BYTES:
        raise _bad(f"the feed is larger than {FEED_MAX_BYTES} bytes")
    if announced is not None and len(body) < announced:
        raise DownloadFailed(
            f"fetching {url} failed: the connection closed after {len(body)} of {announced} bytes",
            hint="check your network connection and proxy settings",
        )
    return body


def fetch_feed(
    url: str,
    cache_dir: Path,
    *,
    offline: bool = False,
    max_age: float = 86400,
    timeout: float = 15,
    opener: Any = None,
    policy: download.Policy | None = None,
    validate: Callable[[str], object] | None = None,
) -> str:
    """Return the feed text, from the cache when it is fresh enough or offline.

    ``opener`` (anything with ``open(request, timeout=...)``) replaces the default
    urllib opener. ``validate(text)`` runs before a new body is cached, so a feed
    that fails to parse is never stored. Bodies over 5 MB raise IntegrityFailed.
    """
    chosen = download.Policy() if policy is None else policy
    host = download.check_url(url, chosen)
    cache = Path(cache_dir)
    cached, meta = _read_cache(cache, url)
    if offline:
        if cached is None:
            raise DownloadFailed(
                f"no cached copy of {url} and offline mode is on",
                hint="run the command again without --offline to fetch it",
            )
        return cached
    if cached is not None and 0 <= time.time() - meta["fetched_at"] < max_age:
        return cached

    headers: dict[str, str] = {}
    if cached is not None and isinstance(meta.get("etag"), str):
        headers["If-None-Match"] = meta["etag"]
    if cached is not None and isinstance(meta.get("last_modified"), str):
        headers["If-Modified-Since"] = meta["last_modified"]
    request = download.make_request(url, headers=headers)
    active = download.build_opener(chosen, host) if opener is None else opener
    try:
        with active.open(request, timeout=timeout) as response:
            body = _read_capped(response, url)
            etag = response.headers.get("ETag")
            last_modified = response.headers.get("Last-Modified")
    except urllib.error.HTTPError as exc:
        exc.close()
        if exc.code == 304 and cached is not None:
            meta["fetched_at"] = time.time()
            _store(cache, meta)
            return cached
        raise DownloadFailed(
            f"fetching {url} failed: HTTP {exc.code} {exc.reason}", hint="try again later"
        ) from None
    except (OSError, http.client.HTTPException) as exc:
        raise DownloadFailed(
            f"fetching {url} failed: {exc}", hint="check your network connection and proxy settings"
        ) from None
    try:
        text = body.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise _bad("the feed is not UTF-8 text") from None
    if validate is not None:
        validate(text)
    encoded = text.encode("utf-8")
    _store(
        cache,
        {
            "url": url,
            "etag": etag,
            "last_modified": last_modified,
            "fetched_at": time.time(),
            "sha256": hashlib.sha256(encoded).hexdigest(),
        },
        encoded,
    )
    return text
