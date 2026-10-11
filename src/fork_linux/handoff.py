"""``fork-linux-host --handoff VERB ARG``: start a desktop tool outside Wine's process tree.

Fork asks for a terminal, the file manager or a default application through
``libexec/fork-linux-{terminal,explorer,open-url}``, scripts that Wine starts
inside its own process tree. Every program started from there inherits
wine-staging's seccomp filter and ``NoNewPrivs=1`` (spike B1): setuid
launchers (Snap's snap-confine) fail and GTK 3 programs that load images
through glycin (Tilix, Nemo) abort. ``libexec/fork-linux-handoff`` therefore
hands ``fork-linux-host VERB ARG`` to a process outside that tree:

1. a transient ``systemd-run --user`` unit, when there is a systemd user manager;
2. otherwise this module: the launcher started ``fl-bridge-helper --daemon``
   before Wine (the git bridge's daemon, or a ``--handoff-only`` one when the
   bridge is off) and put its ``FL_BRIDGE_PORT`` / ``FL_BRIDGE_TOKEN`` into
   Fork's environment. This client authenticates like the shims (mutual
   HMAC-SHA256 over the 256-bit token, ``bridge/common/fl_proto.h``) and sends
   one ``FL_REQ_HANDOFF`` request: the daemon runs ``fork-linux-host VERB ARG``
   detached, in its own environment, so the tool inherits nothing from Wine;
3. only when no daemon answers does the verb run here, inside Wine's tree, as
   before; that last resort is written to ``<logs>/handoff.log`` and stderr.

Inside Flatpak the verb always runs here: the host helper starts every tool on
the host through ``flatpak-spawn --host``, outside Wine's tree and the sandbox.

The request carries only the verb and its one argument: never a program, an
environment or an option (``fl_handoff_ok`` in the daemon checks the same).
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import socket
import struct
import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone

from . import sandbox
from .errors import ForkLinuxError
from .paths import Paths

PROG = "fork-linux-host"
EXIT_USAGE = 2
# The hand-off verbs of fork-linux-host (fl_proto.c k_handoff_verbs).
VERBS = ("terminal", "reveal", "open", "open-url")
ARG_MAX = 8192  # FL_HANDOFF_ARG_MAX
PORT_ENV = "FL_BRIDGE_PORT"
TOKEN_ENV = "FL_BRIDGE_TOKEN"
TIMEOUT = 5.0
LOG_NAME = "handoff.log"
LOG_MAX = 64 * 1024
_LOG_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_CLOEXEC | os.O_NOFOLLOW
_TOKEN_RE = re.compile(r"[0-9a-fA-F]{64}")
_PORT_RE = re.compile(r"[0-9]{1,5}")

# fl_proto.h: frame types, REQ tags and the hand-off flag.
MAGIC = b"FLB1"
VERSION = 1
HELLO, CHALLENGE, AUTH, AUTH_OK, REQ = 1, 2, 3, 4, 5
SPAWN_OK, SPAWN_ERR, EXIT = 9, 10, 13
T_CWD, T_ARG, T_FLAGS = 1, 2, 5
REQ_HANDOFF = 0x4
NONCE_LEN = 32
MAX_FRAME = 1024 * 1024  # FL_MAX_REQ, the largest frame the daemon sends

Connect = Callable[..., socket.socket]


class HandoffError(ForkLinuxError):
    """The daemon could not take the hand-off (the verb then runs here)."""


# -- protocol --------------------------------------------------------------------------------


def auth_mac(key: bytes, side: bytes, client_nonce: bytes, server_nonce: bytes) -> bytes:
    """HMAC-SHA256(key, ``fl-bridge-v1|<side>|`` client_nonce server_nonce)."""
    return hmac.new(key, b"fl-bridge-v1|" + side + b"|" + client_nonce + server_nonce, hashlib.sha256).digest()


def _tlv(tag: int, data: bytes) -> bytes:
    return struct.pack(">BI", tag, len(data)) + data


def encode_request(cwd: str, verb: str, arg: str) -> bytes:
    """The REQ payload of a hand-off: CWD, FLAGS = ``FL_REQ_HANDOFF``, ARG verb, ARG arg."""
    return b"".join(
        (
            _tlv(T_CWD, os.fsencode(cwd)),
            _tlv(T_FLAGS, struct.pack(">I", REQ_HANDOFF)),
            _tlv(T_ARG, os.fsencode(verb)),
            _tlv(T_ARG, os.fsencode(arg)),
        )
    )


def _send(sock: socket.socket, ftype: int, payload: bytes = b"") -> None:
    sock.sendall(struct.pack(">BI", ftype, len(payload)) + payload)


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    data = b""
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise HandoffError("the daemon closed the connection")
        data += chunk
    return data


def _recv(sock: socket.socket) -> tuple[int, bytes]:
    ftype, size = struct.unpack(">BI", _recv_exact(sock, 5))
    if size > MAX_FRAME:
        raise HandoffError(f"the daemon sent an oversized frame ({size} bytes)")
    return ftype, _recv_exact(sock, size) if size else b""


def _authenticate(sock: socket.socket, key: bytes) -> None:
    """The mutual handshake: the daemon proves the token first, then this client does."""
    client_nonce = secrets.token_bytes(NONCE_LEN)
    _send(sock, HELLO, MAGIC + struct.pack(">H", VERSION) + client_nonce)
    ftype, payload = _recv(sock)
    if ftype != CHALLENGE or len(payload) != 2 * NONCE_LEN:
        raise HandoffError("the daemon did not answer the hello")
    server_nonce, daemon_mac = payload[:NONCE_LEN], payload[NONCE_LEN:]
    if not hmac.compare_digest(daemon_mac, auth_mac(key, b"daemon", client_nonce, server_nonce)):
        raise HandoffError("the daemon could not prove the token")
    _send(sock, AUTH, auth_mac(key, b"client", client_nonce, server_nonce))
    ftype, payload = _recv(sock)
    if ftype != AUTH_OK or payload:
        raise HandoffError("the daemon refused the token")


def _spawn_error(payload: bytes) -> str:
    return payload[4:].decode("utf-8", "replace") or f"errno {struct.unpack('>i', payload[:4])[0]}"


def request(
    port: int, token: str, verb: str, arg: str, cwd: str, *, connect: Connect = socket.create_connection
) -> None:
    """Have the daemon on ``127.0.0.1:port`` run ``fork-linux-host VERB ARG``; :class:`HandoffError` if it does not."""
    try:
        sock = connect(("127.0.0.1", port), timeout=TIMEOUT)
    except OSError as exc:
        raise HandoffError(f"cannot connect to the daemon on port {port}: {exc.strerror or exc}") from exc
    try:
        _authenticate(sock, bytes.fromhex(token))
        _send(sock, REQ, encode_request(cwd, verb, arg))
        ftype, payload = _recv(sock)
        if ftype == SPAWN_ERR and len(payload) >= 4:
            raise HandoffError(f"the daemon could not start {PROG}: {_spawn_error(payload)}")
        if ftype != SPAWN_OK:
            raise HandoffError(f"unexpected reply {ftype} from the daemon")
        ftype, payload = _recv(sock)
        if ftype != EXIT or payload != bytes(5):
            raise HandoffError(f"the daemon did not confirm the start (frame {ftype})")
    except OSError as exc:
        raise HandoffError(f"the connection to the daemon failed: {exc}") from exc
    finally:
        sock.close()


# -- command line ----------------------------------------------------------------------------


def daemon_address(env: Mapping[str, str]) -> tuple[int, str] | None:
    """``(port, token)`` of the launcher's daemon from Fork's environment, or None."""
    port, token = env.get(PORT_ENV, ""), env.get(TOKEN_ENV, "")
    if not _PORT_RE.fullmatch(port) or not 1 <= int(port) <= 65535 or not _TOKEN_RE.fullmatch(token):
        return None
    return int(port), token


def accepted(arg: str) -> bool:
    """Whether the daemon takes ``arg`` (non-empty, not an option, at most :data:`ARG_MAX` bytes)."""
    return bool(arg) and not arg.startswith("-") and len(os.fsencode(arg)) <= ARG_MAX


def _cwd() -> str:
    """The working directory for the request ("/" when it is gone)."""
    try:
        return os.getcwd()
    except OSError:
        return "/"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def note_fallback(env: Mapping[str, str], verb: str, reason: str) -> None:
    """Say on stderr and in ``<logs>/handoff.log`` (0600) that ``verb`` runs inside Wine's tree.

    Only the verb and the reason are written, never the path or URL. A log
    that cannot be written is skipped: the hand-off itself goes on.
    """
    line = (
        f"{_now()} {PROG}[{os.getpid()}] {verb}: running inside Wine's process tree "
        f"(the program inherits Wine's NoNewPrivs and seccomp filter): {reason}\n"
    )
    sys.stderr.write(f"{PROG}: {line.split(' ', 2)[2]}")
    try:
        logs = Paths.from_env(env).logs_dir
        logs.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = logs / LOG_NAME
        if path.is_file() and path.stat().st_size > LOG_MAX:
            os.replace(path, logs / (LOG_NAME + ".1"))
        fd = os.open(path, _LOG_FLAGS, 0o600)
        try:
            os.write(fd, line.encode("utf-8", "replace"))
        finally:
            os.close(fd)
    except (OSError, ForkLinuxError):
        return


def main(
    args: Sequence[str],
    *,
    run_here: Callable[[list[str]], int],
    env: Mapping[str, str] | None = None,
    connect: Connect = socket.create_connection,
) -> int:
    """``--handoff VERB ARG``: through the daemon, else ``run_here([VERB, ARG])`` with a log line."""
    environ = os.environ if env is None else env
    if len(args) != 2 or args[0] not in VERBS or not args[1]:
        print(f"usage: {PROG} --handoff {'|'.join(VERBS)} ARG", file=sys.stderr)
        return EXIT_USAGE
    verb, arg = args
    if sandbox.detect(environ) == sandbox.FLATPAK:
        return run_here([verb, arg])  # the helper starts every tool on the host (flatpak-spawn --host)
    address = daemon_address(environ)
    if address is None:
        reason = f"no daemon in Fork's environment ({PORT_ENV} / {TOKEN_ENV})"
    elif not accepted(arg):
        reason = "the daemon does not take this argument"
    else:
        try:
            request(*address, verb, arg, _cwd(), connect=connect)
        except HandoffError as exc:
            reason = exc.message
        else:
            return 0
    note_fallback(environ, verb, reason)
    return run_here([verb, arg])
