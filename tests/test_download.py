"""Tests for fork_linux.download against a real loopback HTTP server with fault injection."""

from __future__ import annotations

import hashlib
import io
import socket
import ssl
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from fixtures.http_server import FakeHTTPServer
from fork_linux import download, version
from fork_linux.errors import DownloadFailed, ExitCode, IntegrityFailed

LOOP = download.Policy(allow_loopback_http=True)
BODY = bytes(range(256)) * 20  # 5120 bytes
BODY_SHA = hashlib.sha256(BODY).hexdigest()


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def get(server: FakeHTTPServer, path: str, cache: Path, **kwargs: Any) -> Path:
    """Fetch ``path`` from the fake server with the loopback policy and a no-op sleep."""
    kwargs.setdefault("policy", LOOP)
    kwargs.setdefault("sleep", lambda _seconds: None)
    return download.fetch(server.url(path), Path(path).name, cache_dir=cache, **kwargs)


def closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class FakeOpener:
    """Stands in for an OpenerDirector; raises ``error`` from open()."""

    def __init__(self, error: BaseException) -> None:
        self.error = error
        self.calls = 0

    def open(self, request: Any, timeout: float = 0) -> Any:
        self.calls += 1
        raise self.error


# -- happy path and cache ------------------------------------------------------------


def test_fetch_downloads_verifies_and_moves_into_place(http_server: FakeHTTPServer, tmp_path: Path) -> None:
    http_server.add("/wine.tar.xz", BODY)
    seen: list[tuple[int, int | None]] = []
    cache = tmp_path / "cache" / "nested"
    path = get(
        http_server,
        "/wine.tar.xz",
        cache,
        sha256=BODY_SHA.upper(),
        size=len(BODY),
        progress=lambda d, t: seen.append((d, t)),
    )
    assert path == cache / "wine.tar.xz"
    assert path.read_bytes() == BODY
    assert not (cache / "wine.tar.xz.part").exists()
    assert seen[0] == (0, len(BODY))
    assert seen[-1] == (len(BODY), len(BODY))
    request = http_server.requests_for("/wine.tar.xz")[0]
    assert request.headers["User-Agent"].startswith("fork-linux/")
    assert request.headers["User-Agent"].endswith("(+https://github.com/ventura8/Fork-Linux)")
    assert request.headers["Accept-Encoding"] == "identity"
    assert "Range" not in request.headers


def test_cache_hit_by_sha_needs_no_network(http_server: FakeHTTPServer, tmp_path: Path) -> None:
    http_server.add("/f.bin", BODY)
    (tmp_path / "f.bin").write_bytes(BODY)
    assert get(http_server, "/f.bin", tmp_path, sha256=BODY_SHA) == tmp_path / "f.bin"
    assert http_server.requests == []


def test_cache_hit_by_size_when_no_sha(http_server: FakeHTTPServer, tmp_path: Path) -> None:
    http_server.add("/f.bin", BODY)
    (tmp_path / "f.bin").write_bytes(b"x" * len(BODY))
    assert get(http_server, "/f.bin", tmp_path, size=len(BODY)).read_bytes() == b"x" * len(BODY)
    assert http_server.requests == []


def test_stale_cached_file_is_replaced(http_server: FakeHTTPServer, tmp_path: Path) -> None:
    http_server.add("/f.bin", BODY)
    (tmp_path / "f.bin").write_bytes(b"old")
    assert get(http_server, "/f.bin", tmp_path, sha256=BODY_SHA).read_bytes() == BODY
    assert len(http_server.requests) == 1


def test_without_sha_or_size_always_downloads(http_server: FakeHTTPServer, tmp_path: Path) -> None:
    http_server.add("/f.bin", BODY)
    (tmp_path / "f.bin").write_bytes(b"old")
    assert get(http_server, "/f.bin", tmp_path).read_bytes() == BODY


def test_offline_hit_and_miss(http_server: FakeHTTPServer, tmp_path: Path) -> None:
    http_server.add("/f.bin", BODY)
    with pytest.raises(DownloadFailed) as info:
        get(http_server, "/f.bin", tmp_path, sha256=BODY_SHA, offline=True)
    assert info.value.exit_code == ExitCode.DOWNLOAD_FAILED
    assert "--offline" in info.value.hint
    (tmp_path / "f.bin").write_bytes(BODY)
    assert get(http_server, "/f.bin", tmp_path, sha256=BODY_SHA, offline=True).read_bytes() == BODY
    assert http_server.requests == []


# -- resume ---------------------------------------------------------------------------


def test_resume_from_part_file(http_server: FakeHTTPServer, tmp_path: Path) -> None:
    http_server.add("/f.bin", BODY)
    (tmp_path / "f.bin.part").write_bytes(BODY[:1000])
    seen: list[tuple[int, int | None]] = []
    path = get(
        http_server, "/f.bin", tmp_path, sha256=BODY_SHA, size=len(BODY), progress=lambda d, t: seen.append((d, t))
    )
    assert path.read_bytes() == BODY
    assert http_server.requests_for("/f.bin")[0].headers["Range"] == "bytes=1000-"
    assert seen[0] == (1000, len(BODY))


def test_server_ignoring_range_restarts_from_zero(http_server: FakeHTTPServer, tmp_path: Path) -> None:
    http_server.add("/f.bin", BODY, ranges=False)
    (tmp_path / "f.bin.part").write_bytes(b"Z" * 1000)
    assert get(http_server, "/f.bin", tmp_path, sha256=BODY_SHA).read_bytes() == BODY
    assert len(http_server.requests) == 1


def test_range_not_satisfiable_discards_part_and_retries(http_server: FakeHTTPServer, tmp_path: Path) -> None:
    http_server.add("/f.bin", BODY)
    (tmp_path / "f.bin.part").write_bytes(BODY + b"extra")
    sleeps: list[float] = []
    assert get(http_server, "/f.bin", tmp_path, sha256=BODY_SHA, sleep=sleeps.append).read_bytes() == BODY
    assert sleeps == [1.0]
    assert [r.headers.get("Range") for r in http_server.requests] == [f"bytes={len(BODY) + 5}-", None]


def test_part_larger_than_size_is_discarded(http_server: FakeHTTPServer, tmp_path: Path) -> None:
    http_server.add("/f.bin", BODY)
    (tmp_path / "f.bin.part").write_bytes(b"Z" * (len(BODY) + 1))
    assert get(http_server, "/f.bin", tmp_path, size=len(BODY)).read_bytes() == BODY
    assert "Range" not in http_server.requests[0].headers


def test_complete_part_is_verified_without_network(http_server: FakeHTTPServer, tmp_path: Path) -> None:
    http_server.add("/f.bin", BODY)
    (tmp_path / "f.bin.part").write_bytes(BODY)
    assert get(http_server, "/f.bin", tmp_path, sha256=BODY_SHA, size=len(BODY)).read_bytes() == BODY
    assert http_server.requests == []


def test_complete_but_corrupt_part_is_redownloaded(http_server: FakeHTTPServer, tmp_path: Path) -> None:
    http_server.add("/f.bin", BODY)
    (tmp_path / "f.bin.part").write_bytes(b"Z" * len(BODY))
    assert get(http_server, "/f.bin", tmp_path, sha256=BODY_SHA, size=len(BODY)).read_bytes() == BODY
    assert len(http_server.requests) == 1
    assert "Range" not in http_server.requests[0].headers


def test_corrupt_resumed_prefix_gets_one_fresh_retry(http_server: FakeHTTPServer, tmp_path: Path) -> None:
    http_server.add("/f.bin", BODY)
    (tmp_path / "f.bin.part").write_bytes(b"Z" * 1000)
    sleeps: list[float] = []
    assert get(http_server, "/f.bin", tmp_path, sha256=BODY_SHA, sleep=sleeps.append).read_bytes() == BODY
    assert [r.headers.get("Range") for r in http_server.requests] == ["bytes=1000-", None]
    assert sleeps == []


def test_unexpected_content_range_restarts(http_server: FakeHTTPServer, tmp_path: Path) -> None:
    http_server.add("/f.bin", BODY, content_range=f"bytes 0-{len(BODY) - 1}/{len(BODY)}")
    (tmp_path / "f.bin.part").write_bytes(BODY[:1000])
    sleeps: list[float] = []
    assert get(http_server, "/f.bin", tmp_path, sha256=BODY_SHA, sleep=sleeps.append).read_bytes() == BODY
    assert sleeps == [1.0]
    assert [r.headers.get("Range") for r in http_server.requests] == ["bytes=1000-", None]


def test_content_range_with_unknown_total(http_server: FakeHTTPServer, tmp_path: Path) -> None:
    http_server.add("/f.bin", BODY, content_range=f"bytes 1000-{len(BODY) - 1}/*")
    (tmp_path / "f.bin.part").write_bytes(BODY[:1000])
    seen: list[tuple[int, int | None]] = []
    path = get(http_server, "/f.bin", tmp_path, size=len(BODY), progress=lambda d, t: seen.append((d, t)))
    assert path.read_bytes() == BODY
    assert seen[-1] == (len(BODY), len(BODY))


# -- retries --------------------------------------------------------------------------


def test_truncated_body_is_resumed(http_server: FakeHTTPServer, tmp_path: Path) -> None:
    http_server.add("/f.bin", BODY, truncate_after=1000)
    sleeps: list[float] = []
    assert (
        get(http_server, "/f.bin", tmp_path, sha256=BODY_SHA, size=len(BODY), sleep=sleeps.append).read_bytes() == BODY
    )
    assert sleeps == [1.0]
    assert [r.headers.get("Range") for r in http_server.requests] == [None, "bytes=1000-"]


def test_truncated_chunked_body_is_retried(http_server: FakeHTTPServer, tmp_path: Path) -> None:
    http_server.add("/f.bin", BODY, chunked=True, truncate_after=1500)
    sleeps: list[float] = []
    assert get(http_server, "/f.bin", tmp_path, sha256=BODY_SHA, sleep=sleeps.append).read_bytes() == BODY
    assert sleeps == [1.0]


def test_server_errors_are_retried_with_backoff(http_server: FakeHTTPServer, tmp_path: Path) -> None:
    http_server.add("/f.bin", BODY, fail_first=2)
    sleeps: list[float] = []
    assert get(http_server, "/f.bin", tmp_path, sha256=BODY_SHA, sleep=sleeps.append).read_bytes() == BODY
    assert sleeps == [1.0, 2.0]


def test_retries_exhausted(http_server: FakeHTTPServer, tmp_path: Path) -> None:
    http_server.add("/f.bin", BODY, fail_first=100, fail_status=503)
    sleeps: list[float] = []
    with pytest.raises(DownloadFailed, match=r"after 6 attempt\(s\): HTTP 503") as info:
        get(http_server, "/f.bin", tmp_path, sha256=BODY_SHA, retries=5, sleep=sleeps.append)
    assert sleeps == [1.0, 2.0, 4.0, 8.0, 8.0]
    assert "network" in info.value.hint
    assert not (tmp_path / "f.bin").exists()


@pytest.mark.parametrize(
    ("status", "hint"), [(404, "no longer published"), (403, "refused access"), (418, "try again later")]
)
def test_client_errors_are_not_retried(http_server: FakeHTTPServer, tmp_path: Path, status: int, hint: str) -> None:
    http_server.add("/f.bin", status=status)
    sleeps: list[float] = []
    with pytest.raises(DownloadFailed, match=f"HTTP {status}") as info:
        get(http_server, "/f.bin", tmp_path, sleep=sleeps.append)
    assert hint in info.value.hint
    assert sleeps == []
    assert len(http_server.requests) == 1


def test_slow_server_times_out_and_is_retried(http_server: FakeHTTPServer, tmp_path: Path) -> None:
    http_server.add("/f.bin", BODY, delay=5)
    sleeps: list[float] = []
    with pytest.raises(DownloadFailed, match="TimeoutError|timed out"):
        get(http_server, "/f.bin", tmp_path, timeout=0.2, retries=1, sleep=sleeps.append)
    assert sleeps == [1.0]
    assert len(http_server.requests) == 2


def test_connection_refused_is_retried(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(download.LOOPBACK_ENV, "1")
    sleeps: list[float] = []
    url = f"http://127.0.0.1:{closed_port()}/f.bin"
    with pytest.raises(DownloadFailed, match=r"after 2 attempt\(s\)"):
        download.fetch(url, "f.bin", cache_dir=tmp_path, policy=LOOP, retries=1, sleep=sleeps.append)
    assert sleeps == [1.0]


def test_lying_content_length_exhausts_retries(http_server: FakeHTTPServer, tmp_path: Path) -> None:
    http_server.add("/f.bin", BODY, content_length=len(BODY) + 100)
    with pytest.raises(DownloadFailed):
        get(http_server, "/f.bin", tmp_path, retries=1)
    assert not (tmp_path / "f.bin").exists()


def test_certificate_errors_are_not_retried(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    opener = FakeOpener(urllib.error.URLError(ssl.SSLCertVerificationError(1, "certificate verify failed")))
    monkeypatch.setattr(download, "build_opener", lambda policy, host: opener)
    with pytest.raises(DownloadFailed, match="TLS certificate") as info:
        download.fetch("https://example.invalid/f.bin", "f.bin", cache_dir=tmp_path, sleep=lambda _s: None)
    assert "CA certificates" in info.value.hint
    assert opener.calls == 1


def test_disk_errors_are_reported(http_server: FakeHTTPServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    http_server.add("/f.bin", BODY)

    def no_space(_fd: int) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(download.os, "fsync", no_space)
    with pytest.raises(DownloadFailed, match="No space left") as info:
        get(http_server, "/f.bin", tmp_path)
    assert "disk space" in info.value.hint


def test_unmovable_destination_is_reported(http_server: FakeHTTPServer, tmp_path: Path) -> None:
    http_server.add("/f.bin", BODY)
    (tmp_path / "f.bin").mkdir()
    with pytest.raises(DownloadFailed, match="cannot move the verified download"):
        get(http_server, "/f.bin", tmp_path, sha256=BODY_SHA)


# -- integrity --------------------------------------------------------------------------


def test_sha_mismatch_deletes_part(http_server: FakeHTTPServer, tmp_path: Path) -> None:
    http_server.add("/f.bin", BODY)
    with pytest.raises(IntegrityFailed, match="sha256 mismatch") as info:
        get(http_server, "/f.bin", tmp_path, sha256="0" * 64)
    assert info.value.exit_code == ExitCode.INTEGRITY_FAILED
    assert not (tmp_path / "f.bin.part").exists()
    assert not (tmp_path / "f.bin").exists()
    assert len(http_server.requests) == 1


def test_sha_mismatch_after_resume_and_fresh_retry(http_server: FakeHTTPServer, tmp_path: Path) -> None:
    http_server.add("/f.bin", BODY)
    (tmp_path / "f.bin.part").write_bytes(BODY[:1000])
    with pytest.raises(IntegrityFailed):
        get(http_server, "/f.bin", tmp_path, sha256="0" * 64)
    assert [r.headers.get("Range") for r in http_server.requests] == ["bytes=1000-", None]
    assert not (tmp_path / "f.bin.part").exists()


def test_short_content_length_is_caught_by_sha(http_server: FakeHTTPServer, tmp_path: Path) -> None:
    http_server.add("/f.bin", BODY, content_length=len(BODY) - 100)
    with pytest.raises(IntegrityFailed, match="sha256 mismatch"):
        get(http_server, "/f.bin", tmp_path, sha256=BODY_SHA)


def test_announced_size_mismatch(http_server: FakeHTTPServer, tmp_path: Path) -> None:
    http_server.add("/f.bin", BODY)
    sleeps: list[float] = []
    with pytest.raises(IntegrityFailed, match=f"announced {len(BODY)} bytes, expected {len(BODY) - 1}"):
        get(http_server, "/f.bin", tmp_path, size=len(BODY) - 1, sleep=sleeps.append)
    assert sleeps == []
    assert not (tmp_path / "f.bin.part").exists()


def test_announced_size_over_max_size(http_server: FakeHTTPServer, tmp_path: Path) -> None:
    http_server.add("/f.bin", BODY)
    with pytest.raises(IntegrityFailed, match="expected at most 1000"):
        get(http_server, "/f.bin", tmp_path, max_size=1000)


def test_streamed_bytes_over_max_size(http_server: FakeHTTPServer, tmp_path: Path) -> None:
    http_server.add("/f.bin", BODY, chunked=True)
    with pytest.raises(IntegrityFailed, match="more than 1000 bytes"):
        get(http_server, "/f.bin", tmp_path, max_size=1000)
    assert not (tmp_path / "f.bin.part").exists()


def test_streamed_bytes_over_expected_size(http_server: FakeHTTPServer, tmp_path: Path) -> None:
    http_server.add("/f.bin", BODY, chunked=True)
    with pytest.raises(IntegrityFailed, match="more than 1000 bytes"):
        get(http_server, "/f.bin", tmp_path, size=1000)


def test_chunked_download_without_known_size(http_server: FakeHTTPServer, tmp_path: Path) -> None:
    http_server.add("/f.bin", BODY, chunked=True)
    seen: list[tuple[int, int | None]] = []
    path = get(http_server, "/f.bin", tmp_path, max_size=len(BODY), progress=lambda d, t: seen.append((d, t)))
    assert path.read_bytes() == BODY
    assert seen[-1] == (len(BODY), None)


def test_invalid_content_length_header_is_ignored(http_server: FakeHTTPServer, tmp_path: Path) -> None:
    http_server.add("/f.bin", BODY, content_length="abc")
    assert get(http_server, "/f.bin", tmp_path, sha256=BODY_SHA).read_bytes() == BODY


# -- URL policy and redirects ----------------------------------------------------------------


def test_redirects_are_followed(http_server: FakeHTTPServer, tmp_path: Path) -> None:
    http_server.add("/old.bin", redirect_to=http_server.url("/f.bin"))
    http_server.add("/f.bin", BODY)
    path = download.fetch(http_server.url("/old.bin"), "f.bin", cache_dir=tmp_path, policy=LOOP, sha256=BODY_SHA)
    assert path.read_bytes() == BODY


def test_redirect_to_forbidden_url_is_refused(http_server: FakeHTTPServer, tmp_path: Path) -> None:
    http_server.add("/old.bin", redirect_to="http://example.com/f.bin")
    with pytest.raises(IntegrityFailed, match="refusing to download over http"):
        get(http_server, "/old.bin", tmp_path)


def test_redirect_handler_refuses_https_to_http(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(download.LOOPBACK_ENV, "1")
    handler = download.SafeRedirectHandler(LOOP)
    fp = io.BytesIO(b"")
    request = urllib.request.Request("https://example.com/a")
    with pytest.raises(IntegrityFailed, match="from https"):
        handler.redirect_request(request, fp, 302, "Found", {}, "http://127.0.0.1/a")
    assert fp.closed


def test_redirect_handler_allows_https_to_https() -> None:
    handler = download.SafeRedirectHandler(download.Policy())
    request = urllib.request.Request("https://github.com/a", headers={"Range": "bytes=10-"})
    new = handler.redirect_request(request, io.BytesIO(b""), 302, "Found", {}, "https://objects.example.com/b")
    assert new is not None
    assert new.full_url == "https://objects.example.com/b"
    assert new.get_header("Range") == "bytes=10-"


def test_allowed_hosts_apply_to_the_initial_url(http_server: FakeHTTPServer, tmp_path: Path) -> None:
    http_server.add("/f.bin", BODY)
    assert get(http_server, "/f.bin", tmp_path, allowed_hosts=["127.0.0.1"]).read_bytes() == BODY
    with pytest.raises(IntegrityFailed, match="not an allowed host"):
        get(http_server, "/f.bin", tmp_path / "other", allowed_hosts=["cdn.fork.dev"])


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/f.bin",
        "ftp://example.com/f.bin",
        "file:///etc/passwd",
        "https://[::1/f.bin",
        "https://user@example.com/f.bin",
        "https://exa mple.com/f.bin",
        "https:///f.bin",
        "",
        "https://example.com:notaport/f.bin",
        "https://example.com:99999/f.bin",
        "https://example.com:0/f.bin",
        "https://example.com/caf\u00e9.bin",
        "https://ex\u00e4mple.com/f.bin",
        "https://example.com/f\x7f.bin",
        "https://example.com/f\r\nX-Injected: 1",
    ],
)
def test_check_url_rejects(url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(download.LOOPBACK_ENV, "1")
    with pytest.raises(IntegrityFailed, match="refusing"):
        download.check_url(url, LOOP)


def test_loopback_http_needs_policy_and_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(download.LOOPBACK_ENV, raising=False)
    with pytest.raises(IntegrityFailed):
        download.check_url("http://127.0.0.1:8000/f", LOOP)
    monkeypatch.setenv(download.LOOPBACK_ENV, "1")
    with pytest.raises(IntegrityFailed, match="refusing to download over http") as info:
        download.check_url("http://127.0.0.1:8000/f")
    assert "https" in info.value.hint
    assert download.check_url("http://127.0.0.1:8000/f", LOOP) == "127.0.0.1"
    assert download.check_url("http://localhost/f", LOOP) == "localhost"
    assert download.check_url("http://[::1]:80/f", LOOP) == "::1"


def test_check_url_lowercases_and_matches_hosts() -> None:
    assert (
        download.check_url("https://CDN.Fork.dev/win/Fork-2.23.2.exe", allowed_hosts=["cdn.FORK.dev"]) == "cdn.fork.dev"
    )
    with pytest.raises(IntegrityFailed):
        download.check_url("https://cdn.fork.dev.evil.example/x", allowed_hosts=["cdn.fork.dev"])


def test_bad_port_fails_at_once_without_retries(tmp_path: Path) -> None:
    sleeps: list[float] = []
    with pytest.raises(IntegrityFailed, match="malformed URL"):
        download.fetch("https://example.com:notaport/x", "x", cache_dir=tmp_path, sleep=sleeps.append)
    assert sleeps == []


def test_part_symlink_is_replaced_not_followed(http_server: FakeHTTPServer, tmp_path: Path) -> None:
    http_server.add("/f.bin", BODY)
    victim = tmp_path / "victim.txt"
    victim.write_bytes(b"precious")
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "f.bin.part").symlink_to(victim)
    assert get(http_server, "/f.bin", cache, sha256=BODY_SHA).read_bytes() == BODY
    assert victim.read_bytes() == b"precious"
    assert "Range" not in http_server.requests[0].headers
    assert not (cache / "f.bin").is_symlink()


def test_dangling_part_symlink_is_removed(http_server: FakeHTTPServer, tmp_path: Path) -> None:
    http_server.add("/f.bin", BODY)
    (tmp_path / "f.bin.part").symlink_to(tmp_path / "elsewhere" / "target")
    assert get(http_server, "/f.bin", tmp_path, sha256=BODY_SHA).read_bytes() == BODY
    assert not (tmp_path / "elsewhere").exists()


def test_part_that_is_a_directory_is_reported(http_server: FakeHTTPServer, tmp_path: Path) -> None:
    http_server.add("/f.bin", BODY)
    (tmp_path / "f.bin.part").mkdir()
    with pytest.raises(DownloadFailed, match="downloading"):
        get(http_server, "/f.bin", tmp_path, sha256=BODY_SHA)
    assert http_server.requests == []


@pytest.mark.parametrize("name", ["", ".", "..", "a/b", "../x", "a\0b"])
def test_dest_name_must_be_a_plain_file_name(name: str, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="plain file name"):
        download.fetch("https://example.com/x", name, cache_dir=tmp_path)


def test_sha256_argument_is_validated(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="64 hex digits"):
        download.fetch("https://example.com/x", "x", cache_dir=tmp_path, sha256="abc")


def test_build_opener_bypasses_proxies_only_for_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("https_proxy", "http://proxy.example:3128")

    def uses_proxy(opener: urllib.request.OpenerDirector) -> bool:
        return any(isinstance(h, urllib.request.ProxyHandler) for h in opener.handlers)

    assert uses_proxy(download.build_opener(download.Policy(), "127.0.0.1")) is False
    assert uses_proxy(download.build_opener(download.Policy(), "cdn.fork.dev")) is True


# -- User-Agent and head_ok -----------------------------------------------------------------


def test_user_agent_uses_the_package_version(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(download, "get_version", lambda: "9.8.7")
    assert download.user_agent() == "fork-linux/9.8.7 (+https://github.com/ventura8/Fork-Linux)"


def test_user_agent_matches_the_real_version() -> None:
    assert download.user_agent() == f"fork-linux/{version.get_version()} (+https://github.com/ventura8/Fork-Linux)"


def test_user_agent_accepts_the_unknown_version(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(download, "get_version", lambda: version.UNKNOWN)
    assert download.user_agent() == "fork-linux/0+unknown (+https://github.com/ventura8/Fork-Linux)"


@pytest.mark.parametrize("bad", ["1.0\r\nX-Injected: yes", "", "1.0 beta", "x" * 65])
def test_user_agent_rejects_unsafe_versions(monkeypatch: pytest.MonkeyPatch, bad: str) -> None:
    monkeypatch.setattr(download, "get_version", lambda: bad)
    assert download.user_agent() == "fork-linux/0 (+https://github.com/ventura8/Fork-Linux)"


def test_user_agent_keeps_dev_versions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(download, "get_version", lambda: "0.1.0-dev+g1a2b3c")
    assert download.user_agent().startswith("fork-linux/0.1.0-dev+g1a2b3c ")


@pytest.mark.parametrize(
    ("raw", "value"), [(None, None), ("42", 42), (" 7 ", 7), ("-1", None), ("1e3", None), ("", None)]
)
def test_parse_length(raw: str | None, value: int | None) -> None:
    assert download.parse_length(raw) == value


def test_head_ok(http_server: FakeHTTPServer) -> None:
    http_server.add("/f.bin", BODY)
    assert download.head_ok(http_server.url("/f.bin"), policy=LOOP) is True
    assert http_server.requests[0].method == "HEAD"
    assert download.head_ok(http_server.url("/missing"), policy=LOOP) is False
    assert download.head_ok(http_server.url("/f.bin")) is False
    assert download.head_ok(f"http://127.0.0.1:{closed_port()}/x", timeout=2, policy=LOOP) is False


def test_policy_defaults() -> None:
    assert download.Policy().allow_loopback_http is False
    with pytest.raises(AttributeError):
        download.Policy().allow_loopback_http = True
