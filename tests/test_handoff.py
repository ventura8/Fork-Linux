"""``fork-linux-host --handoff``: the client side of the daemon's ``FL_REQ_HANDOFF`` request.

A small in-process daemon (a thread on a loopback socket) speaks the FLB1 handshake with the
same token, and can misbehave in each way the client must survive. The real C daemon is
driven by the same client in tests/bridge/test_daemon_protocol.py.
"""

from __future__ import annotations

import os
import secrets
import socket
import stat
import struct
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from fork_linux import handoff
from fork_linux.paths import Paths

TOKEN = "ab" * 32
KEY = bytes.fromhex(TOKEN)
OK_EXIT = struct.pack(">Bi", 0, 0)


def _frame(ftype: int, payload: bytes = b"") -> bytes:
    return struct.pack(">BI", ftype, len(payload)) + payload


def _read_frame(conn: socket.socket) -> tuple[int, bytes]:
    head = handoff._recv_exact(conn, 5)
    ftype, size = struct.unpack(">BI", head)
    return ftype, handoff._recv_exact(conn, size) if size else b""


def _tlvs(payload: bytes) -> list[tuple[int, bytes]]:
    records, pos = [], 0
    while pos < len(payload):
        tag, size = struct.unpack(">BI", payload[pos : pos + 5])
        records.append((tag, payload[pos + 5 : pos + 5 + size]))
        pos += 5 + size
    return records


class FakeDaemon:
    """Answers one connection per ``behaviour`` call; records the REQ it got."""

    def __init__(self, behaviour: Callable[[socket.socket, FakeDaemon], None]) -> None:
        self.server = socket.create_server(("127.0.0.1", 0))
        self.port = self.server.getsockname()[1]
        self.behaviour = behaviour
        self.request: list[tuple[int, bytes]] = []
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        conn, _addr = self.server.accept()
        with conn:
            self.behaviour(conn, self)

    def handshake(self, conn: socket.socket, *, daemon_key: bytes = KEY) -> bool:
        """The daemon's side of the handshake; True when the client proved the token."""
        ftype, hello = _read_frame(conn)
        assert ftype == handoff.HELLO and hello[:6] == b"FLB1\x00\x01" and len(hello) == 38
        client_nonce, server_nonce = hello[6:], secrets.token_bytes(32)
        mac = handoff.auth_mac(daemon_key, b"daemon", client_nonce, server_nonce)
        conn.sendall(_frame(handoff.CHALLENGE, server_nonce + mac))
        try:
            ftype, proof = _read_frame(conn)
        except handoff.HandoffError:
            return False
        return ftype == handoff.AUTH and proof == handoff.auth_mac(KEY, b"client", client_nonce, server_nonce)

    def close(self) -> None:
        self.thread.join(5)
        self.server.close()


def _serving(*replies: bytes) -> Callable[[socket.socket, FakeDaemon], None]:
    """A daemon that authenticates, reads the REQ and sends ``replies``."""

    def behaviour(conn: socket.socket, daemon: FakeDaemon) -> None:
        assert daemon.handshake(conn)
        conn.sendall(_frame(handoff.AUTH_OK))
        ftype, payload = _read_frame(conn)
        assert ftype == handoff.REQ
        daemon.request = _tlvs(payload)
        conn.sendall(b"".join(replies))

    return behaviour


@pytest.fixture(autouse=True)
def _no_flatpak(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(handoff.sandbox, "FLATPAK_INFO", tmp_path / "no-flatpak-info")
    monkeypatch.delenv("FLATPAK_ID", raising=False)


@pytest.fixture
def daemons() -> Iterator[Callable[[Callable[[socket.socket, FakeDaemon], None]], FakeDaemon]]:
    started: list[FakeDaemon] = []

    def start(behaviour: Callable[[socket.socket, FakeDaemon], None]) -> FakeDaemon:
        started.append(FakeDaemon(behaviour))
        return started[-1]

    yield start
    for daemon in started:
        daemon.close()


# -- request ----------------------------------------------------------------------------------


def test_request_sends_only_the_verb_and_its_argument(daemons: Any) -> None:
    daemon = daemons(_serving(_frame(handoff.SPAWN_OK, struct.pack(">I", 99)), _frame(handoff.EXIT, OK_EXIT)))
    handoff.request(daemon.port, TOKEN, "terminal", "Z:\\home\\ada\\repo", "/home/ada")
    daemon.close()
    assert daemon.request == [
        (handoff.T_CWD, b"/home/ada"),
        (handoff.T_FLAGS, struct.pack(">I", handoff.REQ_HANDOFF)),
        (handoff.T_ARG, b"terminal"),
        (handoff.T_ARG, b"Z:\\home\\ada\\repo"),
    ]


def test_encode_request_matches_the_c_layout() -> None:
    payload = handoff.encode_request("/", "open", "/x")
    assert payload == (
        b"\x01\x00\x00\x00\x01/" + b"\x05\x00\x00\x00\x04\x00\x00\x00\x04"
        + b"\x02\x00\x00\x00\x04open" + b"\x02\x00\x00\x00\x02/x"
    )


def _fails(daemons: Any, behaviour: Callable[[socket.socket, FakeDaemon], None], match: str) -> None:
    daemon = daemons(behaviour)
    with pytest.raises(handoff.HandoffError, match=match):
        handoff.request(daemon.port, TOKEN, "open", "/x", "/")


def test_request_spawn_error(daemons: Any) -> None:
    refused = _frame(handoff.SPAWN_ERR, struct.pack(">i", 1) + b"cannot start 'open': Operation not permitted")
    _fails(daemons, _serving(refused), "could not start fork-linux-host: cannot start 'open'")
    _fails(daemons, _serving(_frame(handoff.SPAWN_ERR, struct.pack(">i", 2))), "errno 2")


def test_request_unexpected_replies(daemons: Any) -> None:
    _fails(daemons, _serving(_frame(11, b"out")), "unexpected reply 11")
    _fails(daemons, _serving(_frame(handoff.SPAWN_ERR, b"x")), "unexpected reply 10")
    started = _frame(handoff.SPAWN_OK, struct.pack(">I", 7))
    _fails(daemons, _serving(started, _frame(handoff.EXIT, struct.pack(">Bi", 0, 1))), "did not confirm")
    _fails(daemons, _serving(started, _frame(12, b"err")), r"did not confirm the start \(frame 12\)")
    _fails(daemons, _serving(started), "closed the connection")
    _fails(daemons, _serving(struct.pack(">BI", handoff.SPAWN_OK, handoff.MAX_FRAME + 1)), "oversized frame")


def test_request_daemon_without_the_token(daemons: Any) -> None:
    """The daemon must prove the token before this client sends its proof or anything else."""

    def impostor(conn: socket.socket, daemon: FakeDaemon) -> None:
        assert not daemon.handshake(conn, daemon_key=bytes(32))

    _fails(daemons, impostor, "could not prove the token")


def test_request_handshake_failures(daemons: Any) -> None:
    def no_challenge(conn: socket.socket, daemon: FakeDaemon) -> None:
        _read_frame(conn)
        conn.sendall(_frame(handoff.AUTH_OK))

    def refuses(conn: socket.socket, daemon: FakeDaemon) -> None:
        assert daemon.handshake(conn)
        conn.sendall(_frame(handoff.SPAWN_ERR, bytes(4)))

    def resets(conn: socket.socket, daemon: FakeDaemon) -> None:
        conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))

    _fails(daemons, no_challenge, "did not answer the hello")
    _fails(daemons, refuses, "refused the token")
    _fails(daemons, resets, "connection to the daemon failed|closed the connection")


def test_request_nobody_listening() -> None:
    with socket.create_server(("127.0.0.1", 0)) as probe:
        port = probe.getsockname()[1]
    with pytest.raises(handoff.HandoffError, match=f"cannot connect to the daemon on port {port}"):
        handoff.request(port, TOKEN, "open", "/x", "/")

    def refuse(*_args: Any, **_kwargs: Any) -> socket.socket:
        raise OSError("no route")

    with pytest.raises(handoff.HandoffError, match="no route"):
        handoff.request(port, TOKEN, "open", "/x", "/", connect=refuse)


# -- helpers ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("port", "token", "expected"),
    [
        ("4242", TOKEN, (4242, TOKEN)),
        ("4242", TOKEN.upper(), (4242, TOKEN.upper())),
        ("", TOKEN, None),
        ("0", TOKEN, None),
        ("65536", TOKEN, None),
        ("²", TOKEN, None),
        ("4242", "ab" * 31, None),
        ("4242", "zz" * 32, None),
    ],
)
def test_daemon_address(port: str, token: str, expected: tuple[int, str] | None) -> None:
    assert handoff.daemon_address({handoff.PORT_ENV: port, handoff.TOKEN_ENV: token}) == expected
    assert handoff.daemon_address({}) is None


def test_accepted() -> None:
    assert handoff.accepted("/home/ada")
    assert handoff.accepted("x" * handoff.ARG_MAX)
    assert not handoff.accepted("x" * (handoff.ARG_MAX + 1))
    assert not handoff.accepted("-x")
    assert not handoff.accepted("")


def test_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert handoff._cwd() == str(tmp_path)

    def gone() -> str:
        raise FileNotFoundError

    monkeypatch.setattr(handoff.os, "getcwd", gone)
    assert handoff._cwd() == "/"


# -- main ---------------------------------------------------------------------------------------


class _Here:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]) -> int:
        self.calls.append(args)
        return 5


def _env(xdg: Path, **extra: str) -> dict[str, str]:
    return {**{key: value for key, value in os.environ.items() if not key.startswith("FL_BRIDGE_")}, **extra}


def _log(env: dict[str, str]) -> Path:
    return Paths.from_env(env).logs_dir / handoff.LOG_NAME


def test_main_through_the_daemon(daemons: Any, xdg: Path, capsys: pytest.CaptureFixture[str]) -> None:
    daemon = daemons(_serving(_frame(handoff.SPAWN_OK, struct.pack(">I", 99)), _frame(handoff.EXIT, OK_EXIT)))
    here = _Here()
    env = _env(xdg, FL_BRIDGE_PORT=str(daemon.port), FL_BRIDGE_TOKEN=TOKEN)
    assert handoff.main(["reveal", "/home/ada/x"], run_here=here, env=env) == 0
    assert here.calls == []
    assert capsys.readouterr().err == ""
    assert not _log(env).exists()


def test_main_without_a_daemon_runs_here_and_says_so(xdg: Path, capsys: pytest.CaptureFixture[str]) -> None:
    here = _Here()
    env = _env(xdg)
    assert handoff.main(["terminal", "/home/ada/private"], run_here=here, env=env) == 5
    assert here.calls == [["terminal", "/home/ada/private"]]
    err = capsys.readouterr().err
    assert err.startswith("fork-linux-host: terminal: running inside Wine's process tree")
    assert "no daemon in Fork's environment" in err
    log = _log(env)
    text = log.read_text(encoding="utf-8")
    assert "terminal: running inside Wine's process tree" in text and "/home/ada/private" not in text
    assert stat.S_IMODE(log.stat().st_mode) == 0o600


def test_main_falls_back_when_the_daemon_fails(daemons: Any, xdg: Path, capsys: pytest.CaptureFixture[str]) -> None:
    refused = _frame(handoff.SPAWN_ERR, struct.pack(">i", 1) + b"cannot start 'open': Operation not permitted")
    daemon = daemons(_serving(refused))
    here = _Here()
    env = _env(xdg, FL_BRIDGE_PORT=str(daemon.port), FL_BRIDGE_TOKEN=TOKEN)
    assert handoff.main(["open", "/x"], run_here=here, env=env) == 5
    assert here.calls == [["open", "/x"]]
    assert "Operation not permitted" in capsys.readouterr().err


def test_main_argument_the_daemon_refuses(xdg: Path, capsys: pytest.CaptureFixture[str]) -> None:
    here = _Here()
    env = _env(xdg, FL_BRIDGE_PORT="4242", FL_BRIDGE_TOKEN=TOKEN)
    assert handoff.main(["open", "-rf"], run_here=here, env=env) == 5
    assert "does not take this argument" in capsys.readouterr().err


@pytest.mark.parametrize("args", [[], ["open"], ["diff", "/x"], ["open", ""], ["open", "/x", "/y"]])
def test_main_usage(args: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    here = _Here()
    assert handoff.main(args, run_here=here, env={}) == handoff.EXIT_USAGE
    assert here.calls == []
    assert "usage: fork-linux-host --handoff terminal|reveal|open|open-url ARG" in capsys.readouterr().err


def test_main_inside_flatpak_runs_here_without_a_note(
    xdg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Inside Flatpak the helper starts its tools on the host itself (flatpak-spawn --host)."""
    monkeypatch.setattr(handoff.sandbox, "FLATPAK_INFO", tmp_path / "no-flatpak-info")
    here = _Here()
    env = _env(xdg, FLATPAK_ID="io.github.ventura8.ForkLinux", FL_BRIDGE_PORT="4242", FL_BRIDGE_TOKEN=TOKEN)
    assert handoff.main(["open", "/x"], run_here=here, env=env) == 5
    assert here.calls == [["open", "/x"]]
    assert capsys.readouterr().err == ""
    assert not _log(env).exists()


def test_main_defaults_to_the_process_environment(
    xdg: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("FL_BRIDGE_PORT", raising=False)
    here = _Here()
    assert handoff.main(["open", "/x"], run_here=here) == 5
    assert _log(dict(os.environ)).is_file()


def test_log_rotates_and_never_blocks(xdg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = _env(xdg)
    log = _log(env)
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_bytes(b"x" * (handoff.LOG_MAX + 1))
    handoff.note_fallback(env, "open", "test")
    assert (log.parent / (handoff.LOG_NAME + ".1")).stat().st_size == handoff.LOG_MAX + 1
    assert log.read_text(encoding="utf-8").count("\n") == 1
    # a log that cannot be written (here: a symlink, O_NOFOLLOW) is skipped
    log.unlink()
    log.symlink_to(log.parent / "elsewhere")
    handoff.note_fallback(env, "open", "test")
    assert not (log.parent / "elsewhere").exists()

    def broken(_env: Any) -> Paths:
        raise handoff.ForkLinuxError("no paths")

    monkeypatch.setattr(handoff.Paths, "from_env", broken)
    handoff.note_fallback(env, "open", "test")
