"""Native protocol tests for ``fl-bridge-helper --daemon`` (bridge/unix), without Wine.

The daemon is built with the host C compiler into a temp directory (ASan/UBSan when
available) and driven over loopback TCP by a reference client written here with
the standard library only (socket + hmac + struct). The frame and REQ layouts are
the frozen contract of ``bridge/common/fl_proto.h``.

``FL_BRIDGE_HELPER_BIN=/path/to/fl-bridge-helper`` tests a prebuilt binary instead
(for example the musl ``-static`` build) and skips the compile step.
"""

from __future__ import annotations

import errno
import hashlib
import hmac
import json
import os
import secrets
import shutil
import signal
import socket
import stat
import struct
import subprocess
import sys
import threading
import time
from collections.abc import Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
UNIX_DIR = ROOT / "bridge" / "unix"
COMMON_DIR = ROOT / "bridge" / "common"

# Frame types (fl_proto.h enum fl_frame_type).
HELLO, CHALLENGE, AUTH, AUTH_OK, REQ = 1, 2, 3, 4, 5
STDIN, STDIN_EOF, SIGNAL, SPAWN_OK, SPAWN_ERR = 6, 7, 8, 9, 10
STDOUT, STDERR, EXIT = 11, 12, 13
# REQ TLV tags and flags.
T_CWD, T_ARG, T_ENV_SET, T_ENV_UNSET, T_FLAGS, T_ANCHOR = 1, 2, 3, 4, 5, 6
REQ_DETACH, REQ_HOST_HELPER = 0x1, 0x2
MAGIC = b"FLB1"
VERSION = 1
CHUNK = 64 * 1024
SOCK_TIMEOUT = 30.0
# The bridge's C warning gate (as in bridge/unix/build-dev.sh and bridge/tests/unit/run.sh).
WARN_FLAGS = [
    "-Wall",
    "-Wextra",
    "-Werror",
    "-Wconversion",
    "-Wsign-conversion",
    "-Wshadow",
    "-Wstrict-prototypes",
    "-Wmissing-prototypes",
    "-Wformat=2",
    "-Wvla",
    "-Wundef",
    "-Wpointer-arith",
]


class BridgeError(Exception):
    """The peer broke the protocol or failed authentication."""


# --------------------------------------------------------------------------
# reference client
# --------------------------------------------------------------------------


def recv_exact(sock: socket.socket, n: int) -> bytes:
    """Read exactly n bytes; EOFError when the peer closes first."""
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise EOFError("peer closed the connection")
        buf += chunk
    return bytes(buf)


def send_frame(sock: socket.socket, ftype: int, payload: bytes = b"") -> None:
    """Send one frame: u8 type, u32 BE length, payload."""
    sock.sendall(struct.pack(">BI", ftype, len(payload)) + payload)


def recv_frame(sock: socket.socket) -> tuple[int, bytes]:
    """Receive one frame as (type, payload)."""
    ftype, length = struct.unpack(">BI", recv_exact(sock, 5))
    return ftype, recv_exact(sock, length) if length else b""


def auth_mac(
    key: bytes, side: bytes, client_nonce: bytes, server_nonce: bytes
) -> bytes:
    """HMAC-SHA256(key, "fl-bridge-v1|<side>|" || client_nonce || server_nonce)."""
    msg = b"fl-bridge-v1|" + side + b"|" + client_nonce + server_nonce
    return hmac.new(key, msg, hashlib.sha256).digest()


def hello_payload(
    client_nonce: bytes, magic: bytes = MAGIC, version: int = VERSION
) -> bytes:
    """HELLO payload: magic(4) version(u16 BE) client_nonce(32)."""
    return magic + struct.pack(">H", version) + client_nonce


def connect(port: int, token_hex: str, *, verify_daemon: bool = True) -> socket.socket:
    """Open an authenticated connection (mutual HMAC challenge/response)."""
    key = bytes.fromhex(token_hex)
    sock = socket.create_connection(("127.0.0.1", port), timeout=SOCK_TIMEOUT)
    try:
        client_nonce = secrets.token_bytes(32)
        send_frame(sock, HELLO, hello_payload(client_nonce))
        ftype, payload = recv_frame(sock)
        if ftype != CHALLENGE or len(payload) != 64:
            raise BridgeError(
                f"expected CHALLENGE, got type {ftype} ({len(payload)} bytes)"
            )
        server_nonce, daemon_mac = payload[:32], payload[32:]
        if verify_daemon and not hmac.compare_digest(
            daemon_mac, auth_mac(key, b"daemon", client_nonce, server_nonce)
        ):
            raise BridgeError("the daemon could not prove the token")
        send_frame(sock, AUTH, auth_mac(key, b"client", client_nonce, server_nonce))
        ftype, payload = recv_frame(sock)
        if ftype != AUTH_OK or payload:
            raise BridgeError(f"expected AUTH_OK, got type {ftype}")
    except BaseException:
        sock.close()
        raise
    return sock


def tlv(tag: int, data: bytes) -> bytes:
    """One REQ record: u8 tag, u32 BE length, bytes."""
    return struct.pack(">BI", tag, len(data)) + data


def encode_req(
    cwd: str | os.PathLike[str],
    argv: Sequence[str],
    *,
    env_set: Sequence[str] = (),
    env_unset: Sequence[str] = (),
    flags: int = 0,
    anchors: Sequence[str] = (),
) -> bytes:
    """Encode a REQ payload (TLV records)."""
    out = [tlv(T_CWD, os.fsencode(cwd))]
    out += [tlv(T_ARG, a.encode()) for a in argv]
    out += [tlv(T_ENV_SET, kv.encode()) for kv in env_set]
    out += [tlv(T_ENV_UNSET, k.encode()) for k in env_unset]
    out.append(tlv(T_FLAGS, struct.pack(">I", flags)))
    out += [tlv(T_ANCHOR, a.encode()) for a in anchors]
    return b"".join(out)


@dataclass
class Result:
    """Outcome of one bridged command."""

    pid: int | None = None
    anchors: list[str] = field(default_factory=list)
    spawn_errno: int | None = None
    spawn_msg: str = ""
    stdout: bytes = b""
    stderr: bytes = b""
    stdout_len: int = 0
    stdout_sha256: str = ""
    exit_kind: int | None = None
    exit_code: int | None = None
    frames: list[int] = field(default_factory=list)

    @property
    def status(self) -> int:
        """The shim's exit status: code, 128 + signal, or 127 for a spawn failure."""
        if self.spawn_errno is not None:
            return 127
        assert self.exit_kind is not None
        assert self.exit_code is not None
        return 128 + self.exit_code if self.exit_kind == 1 else self.exit_code


class Session:
    """One authenticated connection carrying one REQ."""

    def __init__(self, port: int, token_hex: str) -> None:
        """Connect and authenticate."""
        self.sock = connect(port, token_hex)
        self.result = Result()
        self.keep_stdout = True
        self._out_hash = hashlib.sha256()
        self._writer: threading.Thread | None = None
        self.writer_error: BaseException | None = None

    def request(
        self,
        cwd: str | os.PathLike[str],
        argv: Sequence[str],
        *,
        env_set: Sequence[str] = (),
        env_unset: Sequence[str] = (),
        flags: int = 0,
        anchors: Sequence[str] = (),
    ) -> Result:
        """Send REQ and read SPAWN_OK / SPAWN_ERR."""
        payload = encode_req(
            cwd,
            argv,
            env_set=env_set,
            env_unset=env_unset,
            flags=flags,
            anchors=anchors,
        )
        send_frame(self.sock, REQ, payload)
        ftype, payload = recv_frame(self.sock)
        self.result.frames.append(ftype)
        if ftype == SPAWN_ERR:
            self.result.spawn_errno = struct.unpack(">i", payload[:4])[0]
            self.result.spawn_msg = payload[4:].decode("utf-8", "replace")
        elif ftype == SPAWN_OK:
            self.result.pid = struct.unpack(">I", payload[:4])[0]
            records = payload[4:].split(b"\0")
            self.result.anchors = (
                [r.decode() for r in records[:-1]] if payload[4:] else []
            )
        else:
            raise BridgeError(f"expected SPAWN_OK/SPAWN_ERR, got {ftype}")
        return self.result

    def feed(self, data: bytes, eof: bool = True) -> None:
        """Pump stdin from a thread (the daemon may push back while we read)."""

        def pump() -> None:
            try:
                for off in range(0, len(data), CHUNK):
                    send_frame(self.sock, STDIN, data[off : off + CHUNK])
                if eof:
                    send_frame(self.sock, STDIN_EOF)
            except OSError as exc:
                self.writer_error = exc

        self._writer = threading.Thread(target=pump, daemon=True)
        self._writer.start()

    def signal(self, signo: int) -> None:
        """Send a SIGNAL frame."""
        send_frame(self.sock, SIGNAL, bytes([signo]))

    def read_until(self, want: bytes, limit: float = 10.0) -> None:
        """Read STDOUT frames until `want` appears in the collected stdout."""
        deadline = time.monotonic() + limit
        while want not in self.result.stdout:
            assert time.monotonic() < deadline, f"timed out waiting for {want!r}"
            self._one()

    def _one(self) -> int:
        """Receive and record one relay frame (STDOUT / STDERR / EXIT); returns its type."""
        ftype, payload = recv_frame(self.sock)
        self.result.frames.append(ftype)
        if ftype == STDOUT:
            self.result.stdout_len += len(payload)
            self._out_hash.update(payload)
            if self.keep_stdout:
                self.result.stdout += payload
        elif ftype == STDERR:
            self.result.stderr += payload
        elif ftype == EXIT:
            assert len(payload) == 5
            kind, code = struct.unpack(">Bi", payload)
            self.result.exit_kind, self.result.exit_code = kind, code
        else:
            raise BridgeError(f"unexpected frame {ftype}")
        return ftype

    def collect(self) -> Result:
        """Read frames until EXIT, then expect the daemon's orderly close."""
        if self.result.spawn_errno is None:
            while self._one() != EXIT:
                pass
        if self._writer is not None:
            self._writer.join(10)
        self.sock.settimeout(5)
        try:
            assert self.sock.recv(1) == b"", "data after the final frame"
        finally:
            self.sock.close()
        self.result.stdout_sha256 = self._out_hash.hexdigest()
        return self.result

    def close(self) -> None:
        """Drop the connection (the shim died)."""
        self.sock.close()


# --------------------------------------------------------------------------
# build + daemon fixtures
# --------------------------------------------------------------------------


def _compile(cc: str, out: Path, sanitize: bool) -> subprocess.CompletedProcess[str]:
    """Compile the daemon with the bridge warning flags (optionally under ASan/UBSan)."""
    sources = [UNIX_DIR / "fl_bridge_helper.c", UNIX_DIR / "fl_helper_util.c"]
    sources += [COMMON_DIR / "fl_proto.c", COMMON_DIR / "fl_sha256.c"]
    cmd = [cc, "-std=c11", "-O1", "-g", *WARN_FLAGS]
    if sanitize:
        cmd += [
            "-fsanitize=address,undefined",
            "-fno-sanitize-recover=all",
            "-fno-omit-frame-pointer",
        ]
    cmd += [f"-I{COMMON_DIR}", f"-I{UNIX_DIR}", '-DFL_VERSION="test"', "-o", str(out)]
    cmd += [str(s) for s in sources]
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


@pytest.fixture(scope="session")
def helper_bin(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The fl-bridge-helper under test (built natively unless FL_BRIDGE_HELPER_BIN is set)."""
    prebuilt = os.environ.get("FL_BRIDGE_HELPER_BIN")
    if prebuilt:
        return Path(prebuilt).resolve()
    cc = shutil.which("gcc") or shutil.which("cc")
    if cc is None:
        pytest.skip("no C compiler (gcc/cc) on PATH")
    out = tmp_path_factory.mktemp("fl-helper") / "fl-bridge-helper"
    proc = _compile(cc, out, sanitize=True)
    if proc.returncode != 0 and "sanitize" in proc.stderr:
        proc = _compile(cc, out, sanitize=False)
    assert proc.returncode == 0, f"building fl-bridge-helper failed:\n{proc.stderr}"
    return out


@dataclass
class Daemon:
    """A running daemon: process, port, token and paths."""

    proc: subprocess.Popen[bytes]
    port: int
    token: str
    log: Path
    root: Path

    def session(self) -> Session:
        """Open an authenticated session."""
        return Session(self.port, self.token)

    def run(
        self,
        argv: Sequence[str],
        *,
        stdin: bytes = b"",
        cwd: Path | None = None,
        env_set: Sequence[str] = (),
        env_unset: Sequence[str] = (),
        flags: int = 0,
    ) -> Result:
        """Run one command to completion."""
        s = self.session()
        res = s.request(
            cwd or self.root, argv, env_set=env_set, env_unset=env_unset, flags=flags
        )
        if res.spawn_errno is None:
            s.feed(stdin)
        return s.collect()

    def stop(self) -> str:
        """SIGTERM the daemon, wait, and return its stderr."""
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
        try:
            _, err = self.proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            _, err = self.proc.communicate()
        return err.decode("utf-8", "replace")


def base_env(root: Path) -> dict[str, str]:
    """A small, isolated environment for the daemon (never the real HOME)."""
    home = root / "home"
    home.mkdir(exist_ok=True)
    return {
        "HOME": str(home),
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "TMPDIR": str(root),
        "FL_TEST_MARKER": "from-daemon",
    }


def start_daemon(
    helper: Path,
    root: Path,
    *,
    extra_args: Sequence[str] = (),
    env: dict[str, str] | None = None,
    token_via_env: bool = False,
) -> Daemon:
    """Start ``fl-bridge-helper --daemon`` and read its port line."""
    token = secrets.token_hex(32)
    log = root / "daemon.log"
    args = [str(helper), "--daemon", "--port", "0", "--log", str(log), *extra_args]
    denv = dict(env or base_env(root))
    if token_via_env:
        denv["FL_BRIDGE_TOKEN"] = token
    else:
        tok_file = root / "token"
        fd = os.open(tok_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(token + "\n")
        args += ["--token-file", str(tok_file)]
    proc = subprocess.Popen(
        args,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=denv,
    )
    assert proc.stdout is not None
    line = proc.stdout.readline().decode()
    if not line.startswith("FL_BRIDGE_PORT="):
        proc.kill()
        _, err = proc.communicate()
        raise AssertionError(f"daemon did not start: {line!r} {err.decode()!r}")
    assert proc.stdout.read() == b"", "stdout must be closed after the port line"
    return Daemon(proc, int(line.split("=", 1)[1]), token, log, root)


def assert_clean_stderr(err: str) -> None:
    """No sanitizer report and no crash output from the daemon."""
    assert "Sanitizer" not in err, err
    assert "runtime error" not in err, err


@pytest.fixture(scope="module")
def daemon(
    helper_bin: Path, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[Daemon]:
    """A shared daemon for the tests that need no special options."""
    root = tmp_path_factory.mktemp("daemon")
    d = start_daemon(helper_bin, root)
    yield d
    err = d.stop()
    assert d.proc.returncode == 0, err
    assert_clean_stderr(err)


def pid_alive(pid: int) -> bool:
    """True while pid exists and is not a zombie."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            state = fh.read().rsplit(b")", 1)[1].split()[0]
    except OSError:
        return False
    return state != b"Z"


def wait_gone(pid: int, limit: float) -> float:
    """Seconds until pid is gone; fails after `limit`."""
    start = time.monotonic()
    while pid_alive(pid):
        assert time.monotonic() - start < limit, (
            f"pid {pid} still alive after {limit} s"
        )
        time.sleep(0.01)
    return time.monotonic() - start


# --------------------------------------------------------------------------
# native C unit tests of the pure helpers (bridge/tests/unit/test_helper_*.c)
# --------------------------------------------------------------------------

UNIT_DIR = ROOT / "bridge" / "tests" / "unit"


@pytest.mark.parametrize("name", ["test_helper_util", "test_helper_paths"])
def test_c_unit_tests(name: str, tmp_path: Path) -> None:
    """Build one C unit test with ASan/UBSan (when available) and run it."""
    cc = shutil.which("gcc") or shutil.which("cc")
    if cc is None:
        pytest.skip("no C compiler (gcc/cc) on PATH")
    out = tmp_path / name
    base = [
        cc,
        "-std=c11",
        "-O1",
        "-g",
        *WARN_FLAGS,
        f"-I{COMMON_DIR}",
        f"-I{UNIX_DIR}",
        "-o",
        str(out),
        str(UNIT_DIR / f"{name}.c"),
        str(UNIX_DIR / "fl_helper_util.c"),
        str(COMMON_DIR / "fl_proto.c"),
        str(COMMON_DIR / "fl_sha256.c"),
    ]
    proc = subprocess.run(
        [
            *base[:1],
            "-fsanitize=address,undefined",
            "-fno-sanitize-recover=all",
            *base[1:],
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0 and "sanitize" in proc.stderr:
        proc = subprocess.run(base, capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr
    run = subprocess.run(
        [str(out)], capture_output=True, text=True, timeout=60, check=False
    )
    assert run.returncode == 0, run.stdout + run.stderr
    assert "checks passed" in run.stdout


# --------------------------------------------------------------------------
# authentication
# --------------------------------------------------------------------------


def test_mutual_auth_succeeds(daemon: Daemon) -> None:
    """The daemon proves the token (CHALLENGE MAC) and accepts the client's AUTH."""
    sock = connect(daemon.port, daemon.token)
    sock.close()


def test_client_with_wrong_token_is_rejected(daemon: Daemon) -> None:
    """AUTH under another key gets no AUTH_OK: the daemon closes the connection."""
    wrong_token = secrets.token_hex(32)
    with pytest.raises((EOFError, ConnectionError)):
        connect(daemon.port, wrong_token, verify_daemon=False)
    deadline = time.monotonic() + 5
    while "auth-failed:bad-mac" not in daemon.log.read_text():
        assert time.monotonic() < deadline
        time.sleep(0.02)


@pytest.mark.parametrize(
    "payload",
    [
        hello_payload(b"\0" * 32, magic=b"XLB1"),
        hello_payload(b"\0" * 32, version=2),
        hello_payload(b"\0" * 31),
    ],
    ids=["bad-magic", "bad-version", "short-nonce"],
)
def test_bad_hello_is_rejected(daemon: Daemon, payload: bytes) -> None:
    """A HELLO with the wrong magic, version or size is dropped without a CHALLENGE."""
    with socket.create_connection(
        ("127.0.0.1", daemon.port), timeout=SOCK_TIMEOUT
    ) as sock:
        send_frame(sock, HELLO, payload)
        with pytest.raises((EOFError, ConnectionError)):
            recv_frame(sock)


def test_request_before_auth_is_rejected(daemon: Daemon) -> None:
    """Skipping the handshake (REQ first) closes the connection, nothing runs."""
    with socket.create_connection(
        ("127.0.0.1", daemon.port), timeout=SOCK_TIMEOUT
    ) as sock:
        send_frame(sock, REQ, encode_req("/", ["true"]))
        with pytest.raises((EOFError, ConnectionError)):
            recv_frame(sock)


def test_silent_client_times_out(daemon: Daemon) -> None:
    """A connection that never sends HELLO is closed after the 5 s auth budget."""
    with socket.create_connection(
        ("127.0.0.1", daemon.port), timeout=SOCK_TIMEOUT
    ) as sock:
        start = time.monotonic()
        assert sock.recv(1) == b""
        elapsed = time.monotonic() - start
    assert 4.0 <= elapsed <= 8.0, elapsed


def test_client_rejects_daemon_that_cannot_prove_token(tmp_path: Path) -> None:
    """A fake daemon without the token (e.g. one squatting the port) never gets AUTH or REQ."""
    real_token = secrets.token_hex(32)
    impostor_key = secrets.token_bytes(32)
    got_after_challenge: list[bytes] = []
    lst = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    lst.bind(("127.0.0.1", 0))
    lst.listen(1)
    lst.settimeout(SOCK_TIMEOUT)

    def impostor() -> None:
        conn, _ = lst.accept()
        with conn:
            conn.settimeout(SOCK_TIMEOUT)
            ftype, payload = recv_frame(conn)
            assert ftype == HELLO
            cn = payload[6:]
            sn = secrets.token_bytes(32)
            send_frame(conn, CHALLENGE, sn + auth_mac(impostor_key, b"daemon", cn, sn))
            data = b""
            try:
                while True:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    data += chunk
            except OSError:
                pass
            got_after_challenge.append(data)

    t = threading.Thread(target=impostor, daemon=True)
    t.start()
    impostor_port = lst.getsockname()[1]
    with pytest.raises(BridgeError, match="could not prove"):
        connect(impostor_port, real_token)
    t.join(10)
    lst.close()
    assert got_after_challenge == [b""], (
        "the client must not send AUTH (or anything) to an impostor"
    )


# --------------------------------------------------------------------------
# running commands
# --------------------------------------------------------------------------


def test_git_version(daemon: Daemon) -> None:
    """git --version through the bridge matches the host git."""
    git = shutil.which("git")
    if git is None:
        pytest.skip("git not installed")
    want = subprocess.run([git, "--version"], capture_output=True, check=True).stdout
    res = daemon.run(["git", "--version"])
    assert (res.exit_kind, res.exit_code) == (0, 0)
    assert res.stdout == want
    assert res.frames[0] == SPAWN_OK
    assert res.frames[-1] == EXIT


def test_exit_code_42(daemon: Daemon) -> None:
    """The child's exit code is reported as EXIT(0, code)."""
    res = daemon.run(["sh", "-c", "exit 42"])
    assert (res.exit_kind, res.exit_code, res.status) == (0, 42, 42)


def test_killed_child_reports_signal(daemon: Daemon) -> None:
    """A child killed by SIGTERM gives EXIT(1, 15): status 128 + 15."""
    res = daemon.run(["sh", "-c", "kill -TERM $$"])
    assert (res.exit_kind, res.exit_code, res.status) == (1, signal.SIGTERM, 143)


@pytest.mark.parametrize(
    "argv0", ["/nonexistent/fl-no-such-program", "fl-no-such-program-xyz"]
)
def test_missing_program_is_spawn_error(daemon: Daemon, argv0: str) -> None:
    """A program that cannot be found gives SPAWN_ERR(ENOENT): status 127."""
    res = daemon.run([argv0, "arg"])
    assert res.spawn_errno == errno.ENOENT
    assert "cannot run" in res.spawn_msg
    assert res.status == 127


def test_missing_cwd_is_spawn_error(daemon: Daemon, tmp_path: Path) -> None:
    """A cwd that does not exist gives SPAWN_ERR(ENOENT)."""
    res = daemon.run(["true"], cwd=tmp_path / "missing")
    assert res.spawn_errno == errno.ENOENT
    assert "cannot change to directory" in res.spawn_msg


def test_cwd_and_argv_round_trip(daemon: Daemon, tmp_path: Path) -> None:
    """cwd (with a space) and tricky arguments reach the child exactly; no shell involved."""
    work = tmp_path / "dir with space"
    work.mkdir()
    args = [
        "a b",
        'q"x',
        "back\\slash",
        "tail\\",
        "",
        "$HOME;`id`",
        "\u00fc\u20ac",
        "*",
    ]
    res = daemon.run(
        [
            sys.executable,
            "-I",
            "-c",
            "import os,sys; print(os.getcwd()); print(repr(sys.argv[1:]))",
            *args,
        ],
        cwd=work,
    )
    assert res.exit_code == 0, res.stderr
    lines = res.stdout.decode().splitlines()
    assert lines[0] == str(work)
    assert lines[1] == repr(args)


def test_stdin_10mb_echo(daemon: Daemon) -> None:
    """10 MB of stdin through cat comes back byte-identical."""
    data = secrets.token_bytes(10 * 1024 * 1024)
    s = daemon.session()
    s.request(daemon.root, ["cat"])
    s.keep_stdout = False
    s.feed(data)
    res = s.collect()
    assert res.exit_code == 0
    assert res.stdout_len == len(data)
    assert res.stdout_sha256 == hashlib.sha256(data).hexdigest()


def test_stdout_100mb(daemon: Daemon) -> None:
    """100 MB of stdout arrives complete, in frames of at most 64 KiB."""
    size = 100 * 1024 * 1024
    s = daemon.session()
    s.request(daemon.root, ["head", "-c", str(size), "/dev/zero"])
    s.keep_stdout = False
    s.feed(b"")
    res = s.collect()
    assert res.exit_code == 0
    assert res.stdout_len == size
    assert res.stdout_sha256 == hashlib.sha256(bytes(size)).hexdigest()


def test_stderr_is_separate(daemon: Daemon) -> None:
    """stdout and stderr travel in their own frames, order kept within each stream."""
    res = daemon.run(["sh", "-c", "echo out1; echo err1 >&2; echo out2; echo err2 >&2"])
    assert res.stdout == b"out1\nout2\n"
    assert res.stderr == b"err1\nerr2\n"


def test_child_closing_stdin_early_does_not_hang(daemon: Daemon) -> None:
    """head -c 10 with 1 MiB of input: 10 bytes back, no hang, no error."""
    res = daemon.run(["head", "-c", "10"], stdin=b"x" * (1024 * 1024))
    assert res.exit_code == 0
    assert res.stdout == b"x" * 10


def test_slow_reader_gets_all_stdin(daemon: Daemon) -> None:
    """Backpressure: a child that starts reading late still receives every byte."""
    data = secrets.token_bytes(4 * 1024 * 1024)
    s = daemon.session()
    s.request(daemon.root, ["sh", "-c", "sleep 1; exec sha256sum"])
    s.feed(data)
    res = s.collect()
    assert res.exit_code == 0
    assert res.stdout.split()[0].decode() == hashlib.sha256(data).hexdigest()


def test_child_sees_only_std_fds(daemon: Daemon) -> None:
    """No daemon descriptor (socket, pipes, log) leaks into the child."""
    res = daemon.run(
        [
            sys.executable,
            "-I",
            "-c",
            "import json,os; print(json.dumps(os.listdir('/proc/self/fd')))",
        ]
    )
    fds = sorted(int(x) for x in json.loads(res.stdout))
    assert fds[:3] == [0, 1, 2]
    assert len(fds) <= 4, fds  # 0-2 plus the listdir handle itself


def test_child_runs_in_own_process_group(daemon: Daemon) -> None:
    """The child leads its own process group (killpg target) in the session's session."""
    res = daemon.run(
        [
            sys.executable,
            "-I",
            "-c",
            "import os; print(os.getpid(), os.getpgrp(), os.getsid(0))",
        ]
    )
    pid, pgrp, sid = (int(x) for x in res.stdout.split())
    assert pid == pgrp == res.pid
    assert sid != pid


def test_grandchild_holding_stdout_does_not_block_exit(daemon: Daemon) -> None:
    """After the child exits its pipes are drained for at most ~2 s, then EXIT is sent."""
    start = time.monotonic()
    res = daemon.run(["sh", "-c", "sleep 30 & echo $!; exit 3"])
    elapsed = time.monotonic() - start
    bg = int(res.stdout.split()[0])
    try:
        assert res.exit_code == 3
        assert 1.5 <= elapsed < 6.0, elapsed
    finally:
        try:
            os.kill(bg, signal.SIGKILL)
        except ProcessLookupError:
            pass


# --------------------------------------------------------------------------
# lifecycle: kill on disconnect, SIGNAL frames
# --------------------------------------------------------------------------


def test_socket_close_kills_child(daemon: Daemon) -> None:
    """Dropping the connection SIGTERMs the child's group: gone well within 3 s."""
    s = daemon.session()
    res = s.request(daemon.root, ["sleep", "30"])
    assert res.pid
    assert pid_alive(res.pid)
    s.close()
    assert wait_gone(res.pid, 3.0) < 3.0


def test_sigterm_ignoring_child_is_sigkilled(daemon: Daemon) -> None:
    """A child that ignores SIGTERM is SIGKILLed about 3 s after the disconnect."""
    s = daemon.session()
    res = s.request(
        daemon.root, ["sh", "-c", "trap '' TERM; echo ready; exec sleep 60"]
    )
    s.read_until(b"ready")
    assert res.pid
    s.close()
    time.sleep(1.5)
    assert pid_alive(res.pid), "killed before the SIGTERM grace period ended"
    elapsed = 1.5 + wait_gone(res.pid, 4.0)
    assert 2.8 <= elapsed <= 5.5, elapsed


def test_signal_frame_interrupts_child(daemon: Daemon) -> None:
    """SIGNAL(SIGINT) reaches the child's process group."""
    s = daemon.session()
    s.request(daemon.root, ["sleep", "30"])
    s.signal(signal.SIGINT)
    res = s.collect()
    assert (res.exit_kind, res.exit_code) == (1, signal.SIGINT)


def test_signal_frame_disallowed_signal_is_ignored(daemon: Daemon) -> None:
    """Only INT/TERM/KILL/HUP are delivered; SIGSTOP and SIGUSR1 are ignored."""
    s = daemon.session()
    res = s.request(daemon.root, ["sleep", "30"])
    s.signal(signal.SIGSTOP)
    s.signal(signal.SIGUSR1)
    time.sleep(0.3)
    assert res.pid
    assert pid_alive(res.pid)
    s.signal(signal.SIGTERM)
    res = s.collect()
    assert (res.exit_kind, res.exit_code) == (1, signal.SIGTERM)


def test_sigpipe_frame_closes_child_stdout(daemon: Daemon) -> None:
    """SIGNAL(SIGPIPE) (the Windows-side reader went away) makes `yes` die of SIGPIPE."""
    s = daemon.session()
    s.request(daemon.root, ["yes"])
    s.keep_stdout = False
    s._one()
    s.signal(signal.SIGPIPE)
    res = s.collect()
    assert (res.exit_kind, res.exit_code, res.status) == (1, signal.SIGPIPE, 141)


def test_unknown_frame_after_req_kills_child(daemon: Daemon) -> None:
    """A protocol violation ends the session and kills the child."""
    s = daemon.session()
    res = s.request(daemon.root, ["sleep", "30"])
    send_frame(s.sock, REQ, encode_req("/", ["true"]))
    with pytest.raises((EOFError, ConnectionError)):
        while True:
            recv_frame(s.sock)
    s.close()
    assert res.pid
    wait_gone(res.pid, 3.0)


@pytest.mark.parametrize(
    "payload",
    [
        encode_req("/", ["true"], flags=0x80),
        encode_req("relative/cwd", ["true"]),
        encode_req("/", []),
        tlv(99, b"x") + encode_req("/", ["true"]),
    ],
    ids=["unknown-flag", "relative-cwd", "no-argv", "unknown-tag"],
)
def test_malformed_request_is_refused(daemon: Daemon, payload: bytes) -> None:
    """A REQ the decoder rejects ends the session before anything is spawned."""
    s = daemon.session()
    send_frame(s.sock, REQ, payload)
    with pytest.raises((EOFError, ConnectionError)):
        recv_frame(s.sock)
    s.close()


# --------------------------------------------------------------------------
# environment, anchors
# --------------------------------------------------------------------------


def test_environment_operations(daemon: Daemon) -> None:
    """Base env from the daemon, REQ unset then set; HOME/PATH/TMPDIR/LANG kept; no token."""
    res = daemon.run(
        [
            sys.executable,
            "-I",
            "-c",
            "import json,os; print(json.dumps(dict(os.environ)))",
        ],
        env_set=[
            "FORK_PROCESS_ID=1234",
            "FL_TEST_MARKER=from-req",
            "HOME=C:\\users\\me",
            "PATH=C:\\Windows",
            "FL_BRIDGE_TOKEN=" + "0" * 64,
            "EMPTY=",
        ],
        env_unset=["TMPDIR", "LANG", "PATH", "HOME", "TEMP"],
    )
    env = json.loads(res.stdout)
    root_env = base_env(daemon.root)
    assert env["FORK_PROCESS_ID"] == "1234"
    assert env["FL_TEST_MARKER"] == "from-req"
    assert env["EMPTY"] == ""
    assert env["HOME"] == root_env["HOME"]
    assert env["PATH"] == root_env["PATH"]
    assert env["TMPDIR"] == root_env["TMPDIR"]
    assert env["LANG"] == root_env["LANG"]
    assert "FL_BRIDGE_TOKEN" not in env
    assert "env-ignored=7 " in daemon.log.read_text()


def test_environment_unset_removes_daemon_variable(daemon: Daemon) -> None:
    """ENV_UNSET removes a variable inherited from the daemon."""
    res = daemon.run(
        ["sh", "-c", 'echo "[${FL_TEST_MARKER-unset}]"'], env_unset=["FL_TEST_MARKER"]
    )
    assert res.stdout == b"[unset]\n"


def test_anchors_are_resolved(daemon: Daemon, tmp_path: Path) -> None:
    """SPAWN_OK carries the realpath of each anchor, "" when it cannot be resolved."""
    real = tmp_path / "real-repo"
    real.mkdir()
    link = tmp_path / "link-repo"
    link.symlink_to(real)
    s = daemon.session()
    res = s.request(
        tmp_path, ["true"], anchors=[str(link), "link-repo", str(tmp_path / "nope")]
    )
    s.feed(b"")
    s.collect()
    assert res.anchors == [str(real), str(real), ""]


# --------------------------------------------------------------------------
# detached mode, host helper
# --------------------------------------------------------------------------


def test_detached_reports_exit_0_at_once(daemon: Daemon, tmp_path: Path) -> None:
    """FL_REQ_DETACH: EXIT 0 right after exec; the child keeps running in its own session."""
    marker = tmp_path / "done"
    start = time.monotonic()
    res = daemon.run(
        [
            sys.executable,
            "-I",
            "-c",
            "import os,sys,time; time.sleep(1); open(sys.argv[1],'w').write(str(os.getsid(0)))",
            str(marker),
        ],
        flags=REQ_DETACH,
    )
    assert time.monotonic() - start < 1.0
    assert (res.exit_kind, res.exit_code) == (0, 0)
    assert res.pid
    deadline = time.monotonic() + 10
    while not marker.exists() or not marker.read_text():
        assert time.monotonic() < deadline, "the detached child did not finish"
        time.sleep(0.05)
    assert int(marker.read_text()) != os.getsid(0)


def test_detached_spawn_error(daemon: Daemon) -> None:
    """A detached program that cannot start still gives SPAWN_ERR."""
    res = daemon.run(["fl-no-such-program-xyz"], flags=REQ_DETACH)
    assert res.spawn_errno == errno.ENOENT


def test_host_helper_flag(helper_bin: Path, tmp_path: Path) -> None:
    """FL_REQ_HOST_HELPER prepends the configured host helper to argv."""
    host = tmp_path / "fork-linux-host"
    host.write_text('#!/bin/sh\nprintf "%s|" "$0" "$@"\n')
    host.chmod(0o755)
    d = start_daemon(helper_bin, tmp_path, extra_args=["--host-helper", str(host)])
    try:
        res = d.run(["open", "/tmp/some dir"], flags=REQ_HOST_HELPER)
        assert res.exit_code == 0, res.stderr
        assert res.stdout.decode() == f"{host}|open|/tmp/some dir|"
    finally:
        err = d.stop()
    assert_clean_stderr(err)


def test_host_helper_flag_without_helper(daemon: Daemon) -> None:
    """Without --host-helper a HOST_HELPER request is SPAWN_ERR(ENOENT)."""
    res = daemon.run(["open", "/tmp"], flags=REQ_HOST_HELPER)
    assert res.spawn_errno == errno.ENOENT


def test_relative_path_entries_are_not_searched(
    helper_bin: Path, tmp_path: Path
) -> None:
    """A program planted in the cwd is never found through '.' or '' PATH entries."""
    env = base_env(tmp_path)
    env["PATH"] = ".::/usr/bin:/bin"
    planted = tmp_path / "fl-planted-prog"
    planted.write_text("#!/bin/sh\necho planted\n")
    planted.chmod(0o755)
    d = start_daemon(helper_bin, tmp_path, env=env)
    try:
        res = d.run(["fl-planted-prog"], cwd=tmp_path)
        assert res.spawn_errno == errno.ENOENT
        assert d.run(["./fl-planted-prog"], cwd=tmp_path).stdout == b"planted\n"
    finally:
        err = d.stop()
    assert_clean_stderr(err)


# --------------------------------------------------------------------------
# concurrency
# --------------------------------------------------------------------------


def test_twenty_parallel_sessions(daemon: Daemon) -> None:
    """20 concurrent sessions each get their own output and exit code."""

    def one(i: int) -> Result:
        return daemon.run(
            [
                "sh",
                "-c",
                f"head -c 200000 /dev/zero | tr '\\0' '{i % 10}'; echo; sleep 0.2; exit {i}",
            ]
        )

    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(one, range(20), timeout=60))
    for i, res in enumerate(results):
        assert res.exit_code == i
        assert res.stdout == str(i % 10).encode() * 200000 + b"\n"


# --------------------------------------------------------------------------
# daemon start-up and lifecycle
# --------------------------------------------------------------------------


def test_token_file_is_unlinked_and_env_token_scrubbed(
    helper_bin: Path, tmp_path: Path
) -> None:
    """The token file disappears after start-up; an env token leaves no trace in /proc."""
    d = start_daemon(helper_bin, tmp_path)
    try:
        assert not (tmp_path / "token").exists()
    finally:
        d.stop()
    root2 = tmp_path / "env"
    root2.mkdir()
    d2 = start_daemon(helper_bin, root2, token_via_env=True)
    try:
        environ = Path(f"/proc/{d2.proc.pid}/environ").read_bytes()
        assert d2.token.encode() not in environ
        assert b"FL_BRIDGE_TOKEN=" + d2.token.encode() not in environ
        res = d2.run(["sh", "-c", 'echo "[${FL_BRIDGE_TOKEN-unset}]"'])
        assert res.stdout == b"[unset]\n"
    finally:
        err = d2.stop()
    assert_clean_stderr(err)


def test_log_has_one_line_per_session_and_no_secrets(
    helper_bin: Path, tmp_path: Path
) -> None:
    """--log gets a start line and one line per session; never the token or arguments."""
    d = start_daemon(helper_bin, tmp_path)
    try:
        d.run(["sh", "-c", "exit 7", "secret-argument"])
        d.run(["fl-no-such-program-xyz"])
        wrong_token = secrets.token_hex(32)
        with pytest.raises((EOFError, ConnectionError)):
            connect(d.port, wrong_token, verify_daemon=False)
        deadline = time.monotonic() + 5
        while d.log.read_text().count(" session ") < 3:
            assert time.monotonic() < deadline
            time.sleep(0.02)
    finally:
        d.stop()
    text = d.log.read_text()
    lines = [ln for ln in text.splitlines() if " session " in ln]
    assert len(lines) == 3, text
    assert "result=exit:7" in lines[0]
    assert "argv0=sh" in lines[0]
    assert f"result=spawn-error:{errno.ENOENT}" in lines[1]
    assert "result=auth-failed:bad-mac" in lines[2]
    assert "daemon start" in text
    assert "daemon exit reason=signal" in text
    assert d.token not in text
    assert "secret-argument" not in text
    assert stat.S_IMODE(d.log.stat().st_mode) == 0o600


@pytest.mark.parametrize(
    ("mode", "content"),
    [(0o644, "x" * 64), (0o600, "zz" * 32), (0o600, "ab" * 31)],
    ids=["group-readable", "not-hex", "too-short"],
)
def test_bad_token_file_is_refused(
    helper_bin: Path, tmp_path: Path, mode: int, content: str
) -> None:
    """The daemon refuses a token file readable by others or without a 64-hex token."""
    tok = tmp_path / "token"
    tok.write_text(content)
    tok.chmod(mode)
    proc = subprocess.run(
        [str(helper_bin), "--daemon", "--token-file", str(tok)],
        capture_output=True,
        env=base_env(tmp_path),
        timeout=10,
        check=False,
    )
    assert proc.returncode == 1
    assert proc.stdout == b""
    assert b"token" in proc.stderr


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["--daemon", "--port", "70000"],
        ["--daemon", "--parent-pid", "0"],
        ["--bogus"],
        ["--daemon", "--host-helper", "relative/path"],
        ["--daemon", "--watch-prefix", "relative"],
    ],
    ids=["no-daemon", "bad-port", "bad-pid", "unknown", "relative-helper", "relative-prefix"],
)
def test_usage_errors(helper_bin: Path, tmp_path: Path, args: list[str]) -> None:
    """Bad command lines exit 2 without listening."""
    proc = subprocess.run(
        [str(helper_bin), *args],
        capture_output=True,
        env=base_env(tmp_path),
        timeout=10,
        check=False,
    )
    assert proc.returncode == 2
    assert b"FL_BRIDGE_PORT" not in proc.stdout


def test_version(helper_bin: Path, tmp_path: Path) -> None:
    """--version prints the helper version and protocol."""
    proc = subprocess.run(
        [str(helper_bin), "--version"],
        capture_output=True,
        env=base_env(tmp_path),
        timeout=10,
        check=True,
    )
    assert proc.stdout.startswith(b"fl-bridge-helper ")
    assert b"FLB1" in proc.stdout


def test_daemon_exits_when_parent_pid_disappears(
    helper_bin: Path, tmp_path: Path
) -> None:
    """--parent-pid is polled every 2 s; the daemon exits once that process is gone."""
    sleeper = subprocess.Popen(["sleep", "60"])
    d = start_daemon(
        helper_bin, tmp_path, extra_args=["--parent-pid", str(sleeper.pid)]
    )
    try:
        time.sleep(0.3)
        assert d.proc.poll() is None
        sleeper.kill()
        sleeper.wait()
        start = time.monotonic()
        d.proc.wait(timeout=6)
        assert time.monotonic() - start <= 4.5
        assert d.proc.returncode == 0
    finally:
        err = d.stop()
    assert "reason=parent-gone" in d.log.read_text()
    assert_clean_stderr(err)


def test_daemon_exits_with_its_parent(helper_bin: Path, tmp_path: Path) -> None:
    """PR_SET_PDEATHSIG: when the process that started the daemon dies, the daemon exits."""
    token_file = tmp_path / "token"
    fd = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(secrets.token_hex(32))
    script = (
        "import subprocess,sys\n"
        "p = subprocess.Popen(sys.argv[1:], stdout=subprocess.PIPE)\n"
        "print(p.pid, p.stdout.readline().decode().strip(), flush=True)\n"
        "sys.stdin.readline()\n"
    )
    launcher = subprocess.Popen(
        [
            sys.executable,
            "-I",
            "-c",
            script,
            str(helper_bin),
            "--daemon",
            "--token-file",
            str(token_file),
            "--log",
            str(tmp_path / "daemon.log"),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        env=base_env(tmp_path),
    )
    assert launcher.stdout is not None
    assert launcher.stdin is not None
    pid_s, port_line = launcher.stdout.readline().decode().split()
    assert port_line.startswith("FL_BRIDGE_PORT=")
    pid = int(pid_s)
    assert pid_alive(pid)
    launcher.stdin.close()
    launcher.wait(timeout=10)
    wait_gone(pid, 3.0)
    assert "daemon exit reason=signal" in (tmp_path / "daemon.log").read_text()


def test_watch_prefix_outlives_its_parent(helper_bin: Path, tmp_path: Path) -> None:
    """--watch-prefix (spike S9): the daemon survives its parent while a program from
    --watch-exe-dir carries WINEPREFIX=<dir> and FL_BRIDGE_PORT=<its port> (Fork restarted by
    Velopack), then exits once the last one is gone; other programs with that environment
    (Wine's services) do not keep it alive."""
    prefix = tmp_path / "prefix"
    prefix.mkdir()
    token_file = tmp_path / "token"
    fd = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(secrets.token_hex(32))
    script = (
        "import os,subprocess,sys\n"
        "p = subprocess.Popen(sys.argv[1:] + ['--parent-pid', str(os.getpid())], stdout=subprocess.PIPE)\n"
        "print(p.pid, p.stdout.readline().decode().strip(), flush=True)\n"
        "sys.stdin.readline()\n"
    )
    argv = [str(helper_bin), "--daemon", "--token-file", str(token_file), "--watch-prefix", str(prefix)]
    argv += ["--watch-exe-dir", "C:\\Fork\\"]
    argv += ["--log", str(tmp_path / "daemon.log")]
    launcher = subprocess.Popen(
        [sys.executable, "-I", "-c", script, *argv],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        env=base_env(tmp_path),
    )
    assert launcher.stdout is not None
    assert launcher.stdin is not None
    pid_s, port_line = launcher.stdout.readline().decode().split()
    pid = int(pid_s)
    user_env = {**base_env(tmp_path), "WINEPREFIX": str(prefix), "FL_BRIDGE_PORT": port_line.split("=", 1)[1]}
    sleep = shutil.which("sleep")
    assert sleep is not None
    user = subprocess.Popen(["c:\\fork\\current\\Fork.exe", "60"], executable=sleep, env=user_env)
    service = subprocess.Popen(["C:\\windows\\system32\\services.exe", "60"], executable=sleep, env=user_env)
    try:
        time.sleep(2.5)  # one --watch-prefix check sees the user
        launcher.stdin.close()
        launcher.wait(timeout=10)
        time.sleep(3.0)  # no PDEATHSIG, and --parent-pid no longer counts
        assert pid_alive(pid)
        user.kill()
        user.wait()
        wait_gone(pid, 12.0)
    finally:
        for proc in (user, service):
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        if pid_alive(pid):
            os.kill(pid, signal.SIGKILL)
    log = (tmp_path / "daemon.log").read_text()
    assert "watch-prefix=yes" in log
    assert "daemon exit reason=prefix-idle" in log


def test_watch_prefix_follows_the_parent_until_used(helper_bin: Path, tmp_path: Path) -> None:
    """Until a process uses the prefix, --parent-pid still stops a watching daemon."""
    sleeper = subprocess.Popen(["sleep", "60"])
    d = start_daemon(
        helper_bin,
        tmp_path,
        extra_args=["--parent-pid", str(sleeper.pid), "--watch-prefix", str(tmp_path / "unused")],
    )
    try:
        sleeper.kill()
        sleeper.wait()
        d.proc.wait(timeout=6)
        assert d.proc.returncode == 0
    finally:
        err = d.stop()
    assert "reason=parent-gone" in d.log.read_text()
    assert_clean_stderr(err)


# --------------------------------------------------------------------------
# personas
# --------------------------------------------------------------------------


@pytest.fixture
def persona_env(helper_bin: Path, tmp_path: Path) -> tuple[Path, dict[str, str], Path]:
    """Persona symlinks, a fake wine that records its argv and env, and a fake prefix."""
    for name in ("fl-winexec", "fl-askpass", "fl-ssh-askpass"):
        (tmp_path / name).symlink_to(helper_bin)
    out = tmp_path / "wine-calls"
    fake = tmp_path / "fake-wine"
    fake.write_text(
        "#!/bin/sh\n"
        'printf "%s\\0" "$0" "$@" "WINEDLLOVERRIDES=$WINEDLLOVERRIDES" "WINEDEBUG=$WINEDEBUG" '
        '"WINEPREFIX=$WINEPREFIX" > "$FL_TEST_OUT"\n'
        "exit 5\n"
    )
    fake.chmod(0o755)
    prefix = tmp_path / "prefix"
    (prefix / "drive_c" / "users").mkdir(parents=True)
    env = base_env(tmp_path)
    env.update(
        {"FL_WINE": str(fake), "WINEPREFIX": str(prefix), "FL_TEST_OUT": str(out)}
    )
    return tmp_path, env, out


def _wine_call(out: Path) -> list[str]:
    """The argv and WINE* values recorded by the fake wine (NUL-separated)."""
    return out.read_bytes().decode().split("\0")[:-1]


def test_winexec_translates_paths(
    persona_env: tuple[Path, dict[str, str], Path],
) -> None:
    """fl-winexec: anchors (longest first), WINEPREFIX/drive_c, Z: fallback, relative files."""
    root, env, out = persona_env
    repo = root / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "COMMIT_EDITMSG").write_text("msg")
    real_root = root.resolve()
    env["FL_BRIDGE_ANCHORS"] = f"{real_root}=Z:\\outer;{real_root / 'repo'}=C:\\Repo"
    args = [
        str(real_root / "repo" / ".git" / "COMMIT_EDITMSG"),
        str(real_root / "elsewhere.txt"),
        str(root / "prefix" / "drive_c" / "users" / "x.txt"),
        "/opt/fl-no-anchor/file",
        ".git/COMMIT_EDITMSG",
        "--wait",
        "plain text",
    ]
    proc = subprocess.run(
        [str(root / "fl-winexec"), "C:\\Program Files\\Fork\\Fork.RI.exe", *args],
        env=env,
        cwd=repo,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert proc.returncode == 5, proc.stderr
    call = _wine_call(out)
    assert call[1:9] == [
        "C:\\Program Files\\Fork\\Fork.RI.exe",
        "C:\\Repo\\.git\\COMMIT_EDITMSG",
        "Z:\\outer\\elsewhere.txt",
        "C:\\users\\x.txt",
        "Z:\\opt\\fl-no-anchor\\file",
        "C:\\Repo\\.git\\COMMIT_EDITMSG",
        "--wait",
        "plain text",
    ]
    assert "WINEDLLOVERRIDES=winemenubuilder.exe=d" in call
    assert "WINEDEBUG=-all" in call
    assert f"WINEPREFIX={root / 'prefix'}" in call


def test_winexec_keeps_existing_dll_overrides(
    persona_env: tuple[Path, dict[str, str], Path],
) -> None:
    """An existing WINEDLLOVERRIDES is extended with winemenubuilder, WINEDEBUG kept."""
    root, env, out = persona_env
    env["WINEDLLOVERRIDES"] = "mscoree,mshtml="
    env["WINEDEBUG"] = "err+all"
    subprocess.run(
        [str(root / "fl-winexec"), "C:\\x.exe"], env=env, timeout=10, check=False
    )
    call = _wine_call(out)
    assert "WINEDLLOVERRIDES=mscoree,mshtml=;winemenubuilder.exe=d" in call
    assert "WINEDEBUG=err+all" in call


@pytest.mark.parametrize("missing", ["WINEPREFIX", "target"])
def test_personas_exit_127_without_configuration(
    persona_env: tuple[Path, dict[str, str], Path], missing: str
) -> None:
    """No WINEPREFIX, or no askpass target: exit 127 with a message, wine never runs."""
    root, env, out = persona_env
    if missing == "WINEPREFIX":
        del env["WINEPREFIX"]
        env["FL_ASKPASS_TARGET"] = "C:\\askpass.exe"
    proc = subprocess.run(
        [str(root / "fl-askpass"), "Password:"],
        env=env,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert proc.returncode == 127
    assert (
        b"WINEPREFIX" if missing == "WINEPREFIX" else b"FL_ASKPASS_TARGET"
    ) in proc.stderr
    assert not out.exists()


def test_winexec_exit_127_when_wine_cannot_run(
    persona_env: tuple[Path, dict[str, str], Path],
) -> None:
    """A FL_WINE that does not exist gives exit 127."""
    root, env, _ = persona_env
    env["FL_WINE"] = str(root / "no-such-wine")
    proc = subprocess.run(
        [str(root / "fl-winexec"), "C:\\x.exe"],
        env=env,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert proc.returncode == 127
    assert b"cannot run" in proc.stderr


@pytest.mark.parametrize(
    ("persona", "var"),
    [("fl-askpass", "FL_ASKPASS_TARGET"), ("fl-ssh-askpass", "FL_SSH_ASKPASS_TARGET")],
)
def test_askpass_personas_pass_prompt_unchanged(
    persona_env: tuple[Path, dict[str, str], Path], persona: str, var: str
) -> None:
    """fl-askpass / fl-ssh-askpass run wine <target> <prompt>, prompt untranslated."""
    root, env, out = persona_env
    env[var] = "C:\\Program Files\\Fork\\Fork.Askpass.exe"
    prompt = "Password for 'https://user@example.com/repo': "
    proc = subprocess.run(
        [str(root / persona), prompt], env=env, timeout=10, check=False
    )
    assert proc.returncode == 5
    assert _wine_call(out)[1:3] == ["C:\\Program Files\\Fork\\Fork.Askpass.exe", prompt]


def test_log_line_cannot_be_forged_by_argv0(helper_bin: Path, tmp_path: Path) -> None:
    """A newline or spaces in argv[0] cannot add log lines or fake key=value fields."""
    d = start_daemon(helper_bin, tmp_path)
    try:
        d.run(["fl-no-such\nfake session result=exit:0 x"])
        deadline = time.monotonic() + 5
        while " session " not in d.log.read_text():
            assert time.monotonic() < deadline
            time.sleep(0.02)
    finally:
        d.stop()
    lines = [ln for ln in d.log.read_text().splitlines() if " session " in ln]
    assert len(lines) == 1, lines
    assert "argv0=fl-no-such?fake?session?result=exit:0?x " in lines[0]
    assert [f for f in lines[0].split(" ") if f.startswith("result=")] == [
        f"result=spawn-error:{errno.ENOENT}"
    ]


def test_winexec_keeps_windows_switches(
    persona_env: tuple[Path, dict[str, str], Path],
) -> None:
    """A single-component '/switch' that does not exist is a Windows option, not a path."""
    root, env, out = persona_env
    proc = subprocess.run(
        [
            str(root / "fl-winexec"),
            "C:\\x.exe",
            "/w",
            "/n:3",
            "/tmp",
            "/fl-no-such-dir/file",
        ],
        env=env,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert proc.returncode == 5, proc.stderr
    assert _wine_call(out)[1:6] == [
        "C:\\x.exe",
        "/w",
        "/n:3",
        "Z:\\tmp",
        "Z:\\fl-no-such-dir\\file",
    ]
