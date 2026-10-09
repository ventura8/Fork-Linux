"""A loopback HTTP server for download and feed tests: Range support plus fault injection.

Usage in a test module::

    from fixtures.http_server import http_server  # the pytest fixture

    def test_something(http_server):
        res = http_server.add("/file.bin", b"payload", fail_first=2)
        url = http_server.url("/file.bin")

The fixture also sets ``FORK_LINUX_TEST_LOOPBACK_HTTP=1`` (the download module's
test-only escape hatch for plain http on loopback) and clears proxy variables.
Faults are configured per path through :class:`Resource` fields.
"""

from __future__ import annotations

import functools
import re
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

_RANGE = re.compile(r"bytes=([0-9]+)-")
_PROXY_VARS = (
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
)


@dataclass
class Resource:
    """What the server answers for one path, and which faults to inject."""

    body: bytes = b""
    status: int = 200  # any non-200 status is sent with an empty body
    fail_first: int = 0  # the first K requests answer ``fail_status``
    fail_status: int = 500
    truncate_after: int | None = None  # drop the connection after N body bytes ...
    truncate_times: int = 1  # ... on this many served requests, then serve normally
    content_length: int | str | None = None  # lie about Content-Length (int or raw text)
    content_range: str | None = None  # override the Content-Range of 206 answers
    redirect_to: str | None = None  # answer ``redirect_status`` with this Location
    redirect_status: int = 302
    delay: float = 0.0  # seconds to stall after the headers (slow server)
    ranges: bool = True  # honour ``Range: bytes=N-``; False answers 200 with the full body
    chunked: bool = False  # use Transfer-Encoding: chunked instead of Content-Length
    etag: str | None = None  # enables If-None-Match -> 304
    last_modified: str | None = None  # enables If-Modified-Since -> 304
    headers: dict[str, str] = field(default_factory=dict)
    hits: int = 0  # requests received for this path
    served: int = 0  # requests that reached the body stage


@dataclass(frozen=True)
class RequestRecord:
    """One request as the server saw it."""

    method: str
    path: str
    headers: dict[str, str]


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    fake: FakeHTTPServer

    def handle_error(self, request: Any, client_address: Any) -> None:
        """Record handler failures (clients hanging up mid-body is expected) instead of printing."""
        self.fake.errors.append(str(client_address))


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: _Server

    def log_message(self, format: str, *args: Any) -> None:
        """Keep test output quiet."""

    def do_GET(self) -> None:
        """Serve a GET."""
        self._serve(head=False)

    def do_HEAD(self) -> None:
        """Serve a HEAD."""
        self._serve(head=True)

    def _bare(self, status: int, headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

    def _serve(self, *, head: bool) -> None:
        fake = self.server.fake
        resource, hit = fake.record(self.command, self.path, dict(self.headers.items()))
        if resource is None:
            self._bare(404)
            return
        if hit <= resource.fail_first:
            self._bare(resource.fail_status)
            return
        if resource.redirect_to is not None:
            self._bare(resource.redirect_status, {"Location": resource.redirect_to})
            return
        if resource.status != 200:
            self._bare(resource.status)
            return
        validators: dict[str, str] = {}
        if resource.etag is not None:
            validators["ETag"] = resource.etag
        if resource.last_modified is not None:
            validators["Last-Modified"] = resource.last_modified
        if (resource.etag is not None and self.headers.get("If-None-Match") == resource.etag) or (
            resource.last_modified is not None and self.headers.get("If-Modified-Since") == resource.last_modified
        ):
            self._bare(304, validators)
            return
        self._send_body(resource, validators, head=head)

    def _send_body(self, resource: Resource, validators: dict[str, str], *, head: bool) -> None:
        body = resource.body
        start, status = 0, 200
        match = _RANGE.fullmatch(self.headers.get("Range", ""))
        if match is not None and resource.ranges:
            start = int(match.group(1))
            if start >= len(body):
                self._bare(416, {"Content-Range": f"bytes */{len(body)}"})
                return
            status = 206
        payload = body[start:]
        with self.server.fake.lock:
            resource.served += 1
            truncate = resource.truncate_after is not None and resource.served <= resource.truncate_times
        self.send_response(status)
        if status == 206:
            default_range = f"bytes {start}-{len(body) - 1}/{len(body)}"
            self.send_header("Content-Range", resource.content_range or default_range)
        if resource.chunked:
            self.send_header("Transfer-Encoding", "chunked")
        else:
            length = resource.content_length if resource.content_length is not None else len(payload)
            self.send_header("Content-Length", str(length))
        for name, value in {**validators, **resource.headers}.items():
            self.send_header(name, value)
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        if head:
            return
        if resource.delay and self.server.fake.stopping.wait(resource.delay):
            return
        if truncate:
            payload = payload[: resource.truncate_after]
        if resource.chunked:
            for offset in range(0, len(payload), 1000):
                piece = payload[offset : offset + 1000]
                self.wfile.write(b"%x\r\n%s\r\n" % (len(piece), piece))
            if not truncate:
                self.wfile.write(b"0\r\n\r\n")
        else:
            self.wfile.write(payload)
        self.wfile.flush()


class FakeHTTPServer:
    """A ThreadingHTTPServer on 127.0.0.1 serving :class:`Resource` objects by path."""

    def __init__(self) -> None:
        self.resources: dict[str, Resource] = {}
        self.requests: list[RequestRecord] = []
        self.errors: list[str] = []
        self.lock = threading.Lock()
        self.stopping = threading.Event()
        self._httpd = _Server(("127.0.0.1", 0), _Handler)
        self._httpd.fake = self
        self._thread = threading.Thread(
            target=functools.partial(self._httpd.serve_forever, poll_interval=0.02), name="fake-http", daemon=True
        )

    @property
    def port(self) -> int:
        """The ephemeral port the server listens on."""
        return int(self._httpd.server_address[1])

    def url(self, path: str) -> str:
        """Absolute http:// URL for ``path`` on this server."""
        return f"http://127.0.0.1:{self.port}{path}"

    def add(self, path: str, body: bytes = b"", **options: Any) -> Resource:
        """Serve ``body`` at ``path``; ``options`` are :class:`Resource` fields."""
        resource = Resource(body=body, **options)
        with self.lock:
            self.resources[path] = resource
        return resource

    def record(self, method: str, path: str, headers: dict[str, str]) -> tuple[Resource | None, int]:
        """Log a request and return its resource with the 1-based hit count."""
        with self.lock:
            self.requests.append(RequestRecord(method, path, headers))
            resource = self.resources.get(path)
            if resource is None:
                return None, 0
            resource.hits += 1
            return resource, resource.hits

    def requests_for(self, path: str) -> list[RequestRecord]:
        """Requests received for ``path``, oldest first."""
        with self.lock:
            return [r for r in self.requests if r.path == path]

    def start(self) -> None:
        """Start serving in a daemon thread."""
        self._thread.start()

    def stop(self) -> None:
        """Wake stalled handlers, stop serving and close the socket."""
        self.stopping.set()
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)


@contextmanager
def serve() -> Iterator[FakeHTTPServer]:
    """Run a :class:`FakeHTTPServer` for the duration of the ``with`` block."""
    server = FakeHTTPServer()
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def http_server(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeHTTPServer]:
    """A running loopback server, with the loopback-http escape hatch on and proxies cleared."""
    monkeypatch.setenv("FORK_LINUX_TEST_LOOPBACK_HTTP", "1")
    for var in _PROXY_VARS:
        monkeypatch.delenv(var, raising=False)
    with serve() as server:
        yield server
