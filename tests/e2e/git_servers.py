"""Local git servers for the remote scenarios: smart HTTP with basic auth, and sshd on a high port.

Both listen on 127.0.0.1 inside the E2E container only, serve repositories from a directory
the test owns, and use test credentials generated for the run (never a real account).
"""

from __future__ import annotations

import base64
import os
import pwd
import secrets
import shutil
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def free_port() -> int:
    """An unused TCP port on 127.0.0.1."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class _GitHttpServer(ThreadingHTTPServer):
    """The HTTP server with what its handlers need: the repositories, the user and the log."""

    def __init__(self, port: int, root: Path, user: str, authorization: str | None) -> None:
        super().__init__(("127.0.0.1", port), _GitHandler)
        self.root = root
        self.user = user
        self.authorization = authorization
        self.requests: list[str] = []


class _GitHandler(BaseHTTPRequestHandler):
    """Runs ``git http-backend`` (CGI) for each request after checking basic auth."""

    server_version = "fl-e2e-git/1"
    server: _GitHttpServer

    def log_message(self, fmt: str, *args: object) -> None:
        self.server.requests.append(fmt % args)

    def _authorized(self) -> bool:
        expected = self.server.authorization
        return expected is None or self.headers.get("Authorization", "") == expected

    def _body(self) -> bytes:
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            chunks = []
            while True:
                size = int(self.rfile.readline().strip() or b"0", 16)
                if size == 0:
                    self.rfile.readline()
                    return b"".join(chunks)
                chunks.append(self.rfile.read(size))
                self.rfile.readline()
        return self.rfile.read(int(self.headers.get("Content-Length") or 0))

    def _handle(self) -> None:
        if not self._authorized():
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="fl-e2e"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        path, _, query = self.path.partition("?")
        body = self._body() if self.command == "POST" else b""
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "GIT_PROJECT_ROOT": str(self.server.root),
            "GIT_HTTP_EXPORT_ALL": "1",
            "REQUEST_METHOD": self.command,
            "PATH_INFO": path,
            "QUERY_STRING": query,
            "CONTENT_TYPE": self.headers.get("Content-Type", ""),
            "CONTENT_LENGTH": str(len(body)),
            "REMOTE_USER": self.server.user,
            "REMOTE_ADDR": "127.0.0.1",
        }
        if self.headers.get("Content-Encoding"):
            env["HTTP_CONTENT_ENCODING"] = self.headers["Content-Encoding"]
        if self.headers.get("Git-Protocol"):
            env["GIT_PROTOCOL"] = self.headers["Git-Protocol"]
        result = subprocess.run(["git", "http-backend"], input=body, env=env, capture_output=True, timeout=120,
                                check=False)
        head, _, payload = result.stdout.partition(b"\r\n\r\n")
        status = 200
        headers = []
        for line in head.decode("latin-1").split("\r\n"):
            name, _, value = line.partition(":")
            if name.lower() == "status":
                status = int(value.split()[0])
            elif name:
                headers.append((name, value.strip()))
        self.send_response(status)
        for name, value in headers:
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        """A smart-HTTP GET (info/refs)."""
        self._handle()

    def do_POST(self) -> None:
        """A smart-HTTP POST (upload-pack / receive-pack)."""
        self._handle()


class HttpGitServer:
    """Smart HTTP for the bare repositories in ``root``, behind basic auth with a test password
    (``auth=False``: open to anyone, like a public server)."""

    def __init__(self, root: Path, *, auth: bool = True) -> None:
        self.root = root
        self.user = "e2e-user"
        self.password = "e2e-" + secrets.token_hex(8)
        self.port = free_port()
        token = base64.b64encode(f"{self.user}:{self.password}".encode()).decode()
        self._server = _GitHttpServer(self.port, root, self.user, f"Basic {token}" if auth else None)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def url(self, name: str) -> str:
        """The URL of a repository (no credentials in it: git has to ask)."""
        return f"http://127.0.0.1:{self.port}/{name}"

    @property
    def requests(self) -> list[str]:
        """The request log lines so far."""
        return list(self._server.requests)

    def __enter__(self) -> HttpGitServer:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=10)


class SshServer:
    """``sshd`` as the test user on a high port, for keys and config the test writes."""

    def __init__(self, base: Path) -> None:
        self.base = base
        self.port = free_port()
        self.user = pwd.getpwuid(os.getuid()).pw_name
        self.process: subprocess.Popen[bytes] | None = None

    def start(self, authorized_keys: Path) -> None:
        """Start sshd with a fresh host key; only ``authorized_keys`` may log in."""
        self.base.mkdir(parents=True, exist_ok=True)
        host_key = self.base / "host_ed25519"
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(host_key)], check=True, timeout=60)
        config = self.base / "sshd_config"
        config.write_text(
            "\n".join([
                f"Port {self.port}",
                "ListenAddress 127.0.0.1",
                f"HostKey {host_key}",
                f"PidFile {self.base / 'sshd.pid'}",
                f"AuthorizedKeysFile {authorized_keys}",
                "PasswordAuthentication no",
                "KbdInteractiveAuthentication no",
                "PubkeyAuthentication yes",
                "UsePAM no",
                "StrictModes no",
                f"AllowUsers {self.user}",
                "",
            ]),
            encoding="utf-8",
        )
        sshd = shutil.which("sshd") or "/usr/sbin/sshd"
        with open(self.base / "sshd.log", "wb") as log:
            self.process = subprocess.Popen([sshd, "-D", "-e", "-f", str(config)], stdout=log,
                                            stderr=subprocess.STDOUT, start_new_session=True)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            with socket.socket() as probe:
                if probe.connect_ex(("127.0.0.1", self.port)) == 0:
                    return
            if self.process.poll() is not None:
                break
            time.sleep(0.2)
        raise RuntimeError(f"sshd did not start: {(self.base / 'sshd.log').read_text(errors='replace')}")

    def stop(self) -> None:
        """Stop sshd."""
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            self.process.wait(timeout=30)
