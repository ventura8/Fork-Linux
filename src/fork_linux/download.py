"""HTTPS downloads into the cache: resume, retries, exact size and streamed sha256.

Rules (AGENTS.md hard rule 4):

* Only ``https://`` URLs. Plain ``http://`` is accepted solely for the test suite:
  the :class:`Policy` must allow it, the host must be loopback, and
  ``FORK_LINUX_TEST_LOOPBACK_HTTP=1`` must be set.
* Redirects may never downgrade https to http. ``allowed_hosts`` is enforced on
  the initial URL only, because release hosts such as GitHub redirect to CDNs.
* Partial data lives in ``<dest>.part`` and is resumed with ``Range``; a ``200``
  answer to a ranged request restarts from zero. A ``.part`` that is a symlink or
  not a regular file is deleted, never followed.
* Network failures, timeouts, truncated bodies and HTTP 5xx are retried with a
  1, 2, 4, 8 s backoff; HTTP 4xx fails at once.
* A size or sha256 mismatch deletes the partial file and raises
  :class:`~fork_linux.errors.IntegrityFailed`; only a verified file is moved into
  place, so ``cache_dir/<dest_name>`` is always complete.
"""

from __future__ import annotations

import hashlib
import http.client
import os
import re
import ssl
import stat
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .errors import DownloadFailed, IntegrityFailed
from .version import get_version

PROJECT_URL = "https://github.com/ventura8/Fork-Linux"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
LOOPBACK_ENV = "FORK_LINUX_TEST_LOOPBACK_HTTP"
CHUNK_SIZE = 64 * 1024
MAX_BACKOFF = 8.0

_SHA256 = re.compile(r"[0-9a-fA-F]{64}")
_UA_VERSION = re.compile(r"[0-9A-Za-z.+_-]{1,64}")
_DIGITS = re.compile(r"[0-9]{1,18}")
_CONTENT_RANGE = re.compile(r"bytes ([0-9]{1,18})-[0-9]{1,18}/([0-9]{1,18}|\*)")
_NETWORK_HINT = "check your network connection and proxy settings; the partial download is kept and resumes next time"
_CLIENT_HINTS = {
    401: "the server asked for credentials; downloads never need any, so check proxy settings",
    403: "the server refused access; check proxy or firewall settings",
    404: "the file is no longer published at this URL; a newer fork-linux release may pin another version",
    410: "the file is no longer published at this URL; a newer fork-linux release may pin another version",
}
# Transient failures while talking to the server (HTTPError/URLError are classified first).
_RETRYABLE = (http.client.HTTPException, ConnectionError, TimeoutError, ssl.SSLError)


@dataclass(frozen=True)
class Policy:
    """Transport policy. Production code uses the defaults; only tests relax them."""

    allow_loopback_http: bool = False


@dataclass(frozen=True)
class _Transfer:
    digest: str
    resumed: bool


def user_agent() -> str:
    """``fork-linux/<version> (+project URL)``; a version unsafe in a header becomes ``0``."""
    version = str(get_version())
    if _UA_VERSION.fullmatch(version) is None:
        version = "0"
    return f"fork-linux/{version} (+{PROJECT_URL})"


def _loopback_http_allowed(host: str, policy: Policy) -> bool:
    return policy.allow_loopback_http and host in LOOPBACK_HOSTS and os.environ.get(LOOPBACK_ENV) == "1"


def check_url(url: str, policy: Policy = Policy(), allowed_hosts: Iterable[str] | None = None) -> str:
    """Return the lowercase host of ``url``; raise :class:`IntegrityFailed` if the policy forbids it."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise IntegrityFailed(f"refusing malformed URL {url!r}") from None
    host = parts.hostname or ""
    if (
        not host
        or "@" in parts.netloc
        or port == 0
        or not url.isascii()
        or any(ch.isspace() or ord(ch) < 0x20 or ch == "\x7f" for ch in url)
    ):
        raise IntegrityFailed(f"refusing malformed URL {url!r}")
    if not (parts.scheme == "https" or (parts.scheme == "http" and _loopback_http_allowed(host, policy))):
        raise IntegrityFailed(
            f"refusing to download over {parts.scheme or 'an unknown scheme'}: {url}",
            hint="fork-linux only downloads over https://",
        )
    if allowed_hosts is not None and host not in {h.lower() for h in allowed_hosts}:
        raise IntegrityFailed(
            f"refusing to download from {host}: it is not an allowed host for this file",
            hint="only the official download hosts listed in the runtime manifest are used",
        )
    return host


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follow redirects, but never from https to http and never to a URL the policy forbids."""

    def __init__(self, policy: Policy) -> None:
        super().__init__()
        self.policy = policy

    def redirect_request(
        self, req: urllib.request.Request, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> urllib.request.Request | None:
        """Validate ``newurl`` before delegating to the standard handler."""
        try:
            check_url(newurl, self.policy)
            if urlsplit(req.full_url).scheme == "https" and urlsplit(newurl).scheme != "https":
                raise IntegrityFailed(
                    f"refusing redirect from https to {newurl}", hint="fork-linux never downgrades to plain http"
                )
        except IntegrityFailed:
            fp.close()
            raise
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def build_opener(policy: Policy, host: str) -> urllib.request.OpenerDirector:
    """An opener with the safe redirect handler; loopback hosts bypass any proxy."""
    handlers: list[urllib.request.BaseHandler] = [SafeRedirectHandler(policy)]
    if host in LOOPBACK_HOSTS:
        handlers.append(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener(*handlers)


def make_request(url: str, *, method: str = "GET", headers: dict[str, str] | None = None) -> urllib.request.Request:
    """A request carrying our User-Agent and asking for an unencoded body."""
    request = urllib.request.Request(url, method=method)
    request.add_header("User-Agent", user_agent())
    request.add_header("Accept-Encoding", "identity")
    for name, value in (headers or {}).items():
        request.add_header(name, value)
    return request


def parse_length(value: str | None) -> int | None:
    """A ``Content-Length`` header value as an int, or None if it is absent or malformed."""
    if value is None or _DIGITS.fullmatch(value.strip()) is None:
        return None
    return int(value.strip())


def _describe(exc: BaseException) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP {exc.code} {exc.reason}"
    if isinstance(exc, urllib.error.URLError):
        return str(exc.reason)
    return f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__


def _discard(path: Path) -> None:
    path.unlink(missing_ok=True)


def _part_offset(part: Path) -> int:
    """Size of the resumable ``part`` file; anything but a regular file is deleted first."""
    try:
        info = part.lstat()
    except FileNotFoundError:
        return 0
    if not stat.S_ISREG(info.st_mode):
        _discard(part)
        return 0
    return info.st_size


def _open_part(part: Path, append: bool) -> Any:
    """Open ``part`` for writing without following a symlink planted in its place."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW
    flags |= os.O_APPEND if append else os.O_TRUNC
    return os.fdopen(os.open(part, flags, 0o644), "ab" if append else "wb")


def _check_name(dest_name: str) -> None:
    if not dest_name or dest_name in (".", "..") or "/" in dest_name or "\0" in dest_name:
        raise ValueError(f"dest_name must be a plain file name, got {dest_name!r}")


def _normalize_sha(sha256: str | None) -> str | None:
    if sha256 is None:
        return None
    if _SHA256.fullmatch(sha256) is None:
        raise ValueError(f"sha256 must be 64 hex digits, got {sha256!r}")
    return sha256.lower()


def _hash_file(path: Path) -> Any:
    hasher = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(CHUNK_SIZE), b""):
            hasher.update(block)
    return hasher


def _cache_hit(dest: Path, sha256: str | None, size: int | None) -> bool:
    if not dest.is_file():
        return False
    if sha256 is not None:
        return _hash_file(dest).hexdigest() == sha256
    return size is not None and dest.stat().st_size == size


def _transfer(
    opener: urllib.request.OpenerDirector,
    url: str,
    part: Path,
    *,
    size: int | None,
    max_size: int | None,
    timeout: float,
    progress: Callable[[int, int | None], None] | None,
) -> _Transfer:
    """One attempt: resume or restart ``part`` and stream the body into it."""
    offset = _part_offset(part)
    if size is not None and offset > size:
        _discard(part)
        offset = 0
    if size is not None and offset == size and offset:
        return _Transfer(_hash_file(part).hexdigest(), True)

    headers = {"Range": f"bytes={offset}-"} if offset else {}
    try:
        response = opener.open(make_request(url, headers=headers), timeout=timeout)
    except urllib.error.HTTPError as exc:
        if exc.code == 416 and offset:
            exc.close()
            _discard(part)
            raise http.client.HTTPException("the server rejected the resume range; restarting") from None
        raise

    with response:
        if offset and response.status == 206:
            match = _CONTENT_RANGE.fullmatch(response.headers.get("Content-Range", "").strip())
            if match is None or int(match.group(1)) != offset:
                _discard(part)
                raise http.client.HTTPException("the server answered with an unexpected Content-Range; restarting")
            total = None if match.group(2) == "*" else int(match.group(2))
            hasher = _hash_file(part)
        else:
            offset = 0
            total = parse_length(response.headers.get("Content-Length"))
            hasher = hashlib.sha256()
        if total is not None and ((size is not None and total != size) or (max_size is not None and total > max_size)):
            _discard(part)
            raise IntegrityFailed(
                f"{url} has the wrong size: the server announced {total} bytes, expected "
                f"{size if size is not None else f'at most {max_size}'}",
                hint="the upstream file changed or the download was tampered with; nothing was installed",
            )
        expected = total if total is not None else size
        written = offset
        overflow = False
        with _open_part(part, append=offset > 0) as out:
            if progress is not None:
                progress(written, expected)
            for chunk in iter(lambda: response.read(CHUNK_SIZE), b""):
                written += len(chunk)
                if (size is not None and written > size) or (max_size is not None and written > max_size):
                    overflow = True
                    break
                hasher.update(chunk)
                out.write(chunk)
                if progress is not None:
                    progress(written, expected)
            out.flush()
            os.fsync(out.fileno())
    if overflow:
        _discard(part)
        raise IntegrityFailed(
            f"{url} is larger than expected (more than {size if size is not None else max_size} bytes)",
            hint="the upstream file changed or the download was tampered with; nothing was installed",
        )
    if expected is not None and written < expected:
        raise http.client.IncompleteRead(b"", expected - written)
    return _Transfer(hasher.hexdigest(), offset > 0)


def _retryable(exc: OSError | http.client.HTTPException, url: str, cache: Path) -> BaseException:
    """Return ``exc`` when another attempt may succeed; raise :class:`DownloadFailed` when it cannot."""
    if isinstance(exc, urllib.error.HTTPError):
        exc.close()
        if exc.code < 500:
            raise DownloadFailed(
                f"downloading {url} failed: {_describe(exc)}",
                hint=_CLIENT_HINTS.get(exc.code, "the server refused the request; try again later"),
            ) from None
        return exc
    if isinstance(exc, urllib.error.URLError):
        if isinstance(exc.reason, ssl.SSLCertVerificationError):
            raise DownloadFailed(
                f"downloading {url} failed: TLS certificate verification failed ({exc.reason})",
                hint="check the system clock and CA certificates, or a proxy intercepting TLS",
            ) from None
        return exc
    if isinstance(exc, _RETRYABLE):
        return exc
    raise DownloadFailed(
        f"downloading {url} failed: {exc}", hint=f"check free disk space and permissions of {cache}"
    ) from None


def fetch(
    url: str,
    dest_name: str,
    *,
    cache_dir: Path,
    sha256: str | None = None,
    size: int | None = None,
    policy: Policy = Policy(),
    offline: bool = False,
    retries: int = 4,
    timeout: float = 30,
    progress: Callable[[int, int | None], None] | None = None,
    allowed_hosts: Iterable[str] | None = None,
    max_size: int | None = None,
    sleep: Callable[[float], None] | None = None,
) -> Path:
    """Download ``url`` to ``cache_dir/dest_name`` (or reuse a verified cached copy) and return the path.

    ``progress(done, total)`` is called as bytes arrive (``total`` may be None).
    ``sleep`` replaces :func:`time.sleep` between retries (tests pass a recorder).
    """
    _check_name(dest_name)
    expected_sha = _normalize_sha(sha256)
    host = check_url(url, policy, allowed_hosts)
    cache = Path(cache_dir)
    dest = cache / dest_name
    if _cache_hit(dest, expected_sha, size):
        return dest
    if offline:
        raise DownloadFailed(
            f"{dest_name} is not in the download cache and offline mode is on",
            hint="run the command again without --offline to download it",
        )
    part = cache / f"{dest_name}.part"
    opener = build_opener(policy, host)
    pause = time.sleep if sleep is None else sleep
    attempt = 0
    restarted = False
    while True:
        try:
            cache.mkdir(parents=True, exist_ok=True)
            result = _transfer(opener, url, part, size=size, max_size=max_size, timeout=timeout, progress=progress)
        except (OSError, http.client.HTTPException) as exc:
            error = _retryable(exc, url, cache)
            if attempt >= retries:
                raise DownloadFailed(
                    f"downloading {url} failed after {attempt + 1} attempt(s): {_describe(error)}", hint=_NETWORK_HINT
                ) from None
            pause(min(2.0**attempt, MAX_BACKOFF))
            attempt += 1
            continue
        if expected_sha is None or result.digest == expected_sha:
            break
        _discard(part)
        if result.resumed and not restarted:
            restarted = True
            continue
        raise IntegrityFailed(
            f"sha256 mismatch for {dest_name}: expected {expected_sha}, got {result.digest}",
            hint="the download was corrupted or tampered with and has been deleted; nothing was installed",
        )
    try:
        os.replace(part, dest)
    except OSError as exc:
        raise DownloadFailed(
            f"cannot move the verified download into {dest}: {exc}", hint=f"check permissions of {cache}"
        ) from None
    return dest


def head_ok(url: str, timeout: float = 10, *, policy: Policy = Policy()) -> bool:
    """True if a HEAD request to ``url`` succeeds (used by ``doctor --network``)."""
    try:
        host = check_url(url, policy)
        with build_opener(policy, host).open(make_request(url, method="HEAD"), timeout=timeout):
            return True
    except urllib.error.HTTPError as exc:
        exc.close()
        return False
    except (OSError, http.client.HTTPException, IntegrityFailed):
        return False
