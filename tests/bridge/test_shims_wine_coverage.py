"""Wine tier, part 2: the edge cases of the Windows bridge (bridge/win) under real Wine.

Complements tests/bridge/test_shims_wine.py (whose fixtures it reuses) with:

* ``fl-win-unit.exe`` (bridge/win/tests/fl_win_unit.c): unit tests of fl_win.c run
  under Wine, including injected allocation failures;
* a scripted fake daemon (Python) that speaks the frame protocol of
  bridge/common/fl_proto.h, to drive the shims through the daemon misbehaviours a real
  daemon never shows (no challenge, rejected AUTH, SPAWN_ERR, malformed frames, a
  connection lost mid-call, a SPAWN_OK larger than one chunk);
* more shim scenarios against the real daemon: git global options and anchors, record
  mode of the bash / sh personas, an empty or over-long command line, fl-launch's log
  line and configuration errors, a stdin pipe that is never closed.

Gated like the rest of the Wine tier: ``FL_REAL_WINE=1`` (AGENTS.md hard rule 16).
"""

from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from types import ModuleType

import pytest

from fixtures import real_tier

if os.environ.get("FL_REAL_WINE") != "1":
    pytest.skip("requires FL_REAL_WINE=1", allow_module_level=True)
# Containers only (AGENTS.md hard rule 18): FL_CI_STAGE=bridge ./scripts/ci-docker.sh.
real_tier.enforce()


def _load_base() -> ModuleType:
    """tests/bridge/test_shims_wine.py (the module pytest collected, or a fresh load)."""
    name = "test_shims_wine"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(f"{name}.py"))
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


base = _load_base()

Env = base.Env
err_text = base.err_text

# The fixtures of test_shims_wine, re-bound so pytest finds them in this module.
tools = base.tools
helper_bin = base.helper_bin
env = base.env

# Frame types (bridge/common/fl_proto.h).
F_HELLO, F_CHALLENGE, F_AUTH, F_AUTH_OK, F_REQ = 1, 2, 3, 4, 5
F_STDIN, F_STDIN_EOF, F_SIGNAL, F_SPAWN_OK, F_SPAWN_ERR = 6, 7, 8, 9, 10
F_STDOUT, F_STDERR, F_EXIT = 11, 12, 13
OUT_OF_MEMORY = "out of memory"


# --------------------------------------------------------------------------
# fl-win-unit.exe
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def unit_exe(env: Env) -> Path:
    exe = base._default_win_dir() / "tests" / "fl-win-unit.exe"
    if not exe.is_file():
        pytest.skip(f"{exe} is missing: run scripts/build-bridge.sh")
    dst = env.bin_dir / "fl-win-unit.exe"
    shutil.copyfile(exe, dst)
    return dst


def test_win_unit_pure(env: Env, unit_exe: Path) -> None:
    proc = env.run([unit_exe, "pure", env.root])
    out = proc.stdout.decode("utf-8", "replace")
    assert proc.returncode == 0, out + err_text(proc)
    assert ", 0 failed" in out
    assert (env.root / "fl-unit.log").read_text() == "two\n"


def test_win_unit_from_a_long_module_path(env: Env, unit_exe: Path) -> None:
    """GetModuleFileNameW needs a second, larger buffer past 512 characters."""
    deep = env.root
    for i in range(3):
        deep = deep / (f"d{i}" + "x" * 200)
    deep.mkdir(parents=True)
    exe = deep / "fl-win-unit.exe"
    shutil.copyfile(unit_exe, exe)
    proc = env.run([exe, "pure"])
    out = proc.stdout.decode("utf-8", "replace")
    assert proc.returncode == 0, out + err_text(proc)


@pytest.mark.parametrize(
    ("case", "code", "needle"),
    [
        ("exit-hook", 7, ""),
        ("fail", 125, "unit: plain failure\n"),
        ("fail-s", 125, "unit: pre<mid>post\n"),
        ("fail-n", 125, "unit: n=-12.\n"),
        ("fail-buf", 125, "unit: built\n"),
        ("xmalloc", 125, OUT_OF_MEMORY),
        ("xcalloc", 125, OUT_OF_MEMORY),
        ("xstrdup", 125, OUT_OF_MEMORY),
        ("args-ok", 0, ""),
        ("args-bad", 125, "argument 1 is not valid UTF-16"),
        ("environ-oom", 125, "cannot translate the environment"),
        ("no-such-case", 3, ""),
    ],
)
def test_win_unit_exit_cases(
    env: Env, unit_exe: Path, case: str, code: int, needle: str
) -> None:
    proc = env.run([unit_exe, case])
    assert proc.returncode == code, err_text(proc)
    assert needle in err_text(proc)
    if case == "exit-hook":
        assert proc.stdout.strip() == b"hook ran 7"


def test_win_unit_usage(env: Env, unit_exe: Path) -> None:
    assert env.run([unit_exe]).returncode == 2


# --------------------------------------------------------------------------
# a scripted fake daemon
# --------------------------------------------------------------------------


class Conn:
    """One accepted connection, with frame helpers."""

    def __init__(self, sock: socket.socket, key: bytes) -> None:
        self.sock = sock
        self.key = key
        self.cn = b""
        self.sn = os.urandom(32)

    def recv_exact(self, n: int) -> bytes:
        data = b""
        while len(data) < n:
            chunk = self.sock.recv(n - len(data))
            if not chunk:
                raise ConnectionError("peer closed")
            data += chunk
        return data

    def recv(self) -> tuple[int, bytes]:
        typ, n = struct.unpack(">BI", self.recv_exact(5))
        return typ, self.recv_exact(n)

    def send(self, typ: int, payload: bytes = b"") -> None:
        self.sock.sendall(struct.pack(">BI", typ, len(payload)) + payload)

    def mac(self, who: bytes) -> bytes:
        msg = b"fl-bridge-v1|" + who + b"|" + self.cn + self.sn
        return hmac.new(self.key, msg, hashlib.sha256).digest()

    def hello(self) -> None:
        typ, payload = self.recv()
        assert typ == F_HELLO
        self.cn = payload[6:38]

    def challenge(self) -> None:
        self.send(F_CHALLENGE, self.sn + self.mac(b"daemon"))
        typ, payload = self.recv()
        assert typ == F_AUTH
        assert payload == self.mac(b"client")

    def handshake_and_req(self) -> bytes:
        self.hello()
        self.challenge()
        self.send(F_AUTH_OK)
        typ, req = self.recv()
        assert typ == F_REQ
        return req

    def drain(self) -> None:
        """Read what the client still sends (stdin frames) until it closes."""
        try:
            while self.recv():
                pass
        except (ConnectionError, OSError):
            pass


Script = Callable[[Conn], None]


class FakeDaemon:
    """Accept connections on 127.0.0.1 and run `script` on each, in a thread."""

    def __init__(self, token: str, script: Script) -> None:
        self.key = bytes.fromhex(token)
        self.script = script
        self.errors: list[BaseException] = []
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(4)
        self.port = self.srv.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        while True:
            try:
                sock, _ = self.srv.accept()
            except OSError:
                return
            with sock:
                try:
                    self.script(Conn(sock, self.key))
                except (ConnectionError, OSError):
                    pass
                except AssertionError as exc:
                    self.errors.append(exc)

    def close(self) -> None:
        self.srv.close()
        self.thread.join(5)


@pytest.fixture
def fake(env: Env) -> Iterator[Callable[[Script], FakeDaemon]]:
    made: list[FakeDaemon] = []

    def make(script: Script) -> FakeDaemon:
        d = FakeDaemon(env.token, script)
        made.append(d)
        return d

    yield make
    for d in made:
        d.close()
        assert not d.errors, d.errors


def run_against(
    env: Env, daemon: FakeDaemon, args: list[str | Path], **extra: str
) -> subprocess.CompletedProcess[bytes]:
    return env.run(args, env=env.wine_env(FL_BRIDGE_PORT=str(daemon.port), **extra))


def _no_challenge(c: Conn) -> None:
    c.hello()


def _reject_auth(c: Conn) -> None:
    c.hello()
    c.challenge()
    c.send(F_STDOUT, b"")


def _close_before_spawn(c: Conn) -> None:
    c.handshake_and_req()


def _spawn_err(c: Conn) -> None:
    c.handshake_and_req()
    c.send(F_SPAWN_ERR, struct.pack(">i", 2) + b"no\x1bsuch\x7ffile")


def _spawn_err_empty(c: Conn) -> None:
    c.handshake_and_req()
    c.send(F_SPAWN_ERR, struct.pack(">i", 5))


def _wrong_first_frame(c: Conn) -> None:
    c.handshake_and_req()
    c.send(F_STDOUT, b"x")


def _spawn_ok(c: Conn, extra: bytes = b"") -> None:
    c.handshake_and_req()
    c.send(F_SPAWN_OK, struct.pack(">I", 4242) + extra)


def _big_spawn_ok(c: Conn) -> None:
    _spawn_ok(c, b"/r" * 40000 + b"\0\0/last")
    c.send(F_STDOUT, b"out")
    c.send(F_STDERR, b"err")
    c.send(F_EXIT, b"\0" + struct.pack(">i", 3))
    c.drain()


def _signaled(c: Conn) -> None:
    _spawn_ok(c)
    c.send(F_EXIT, b"\1" + struct.pack(">i", 9))
    c.drain()


def _unexpected_in_relay(c: Conn) -> None:
    _spawn_ok(c)
    c.send(F_CHALLENGE, b"\0" * 64)
    c.drain()


def _lost_in_relay(c: Conn) -> None:
    _spawn_ok(c)


def _malformed_exit(c: Conn) -> None:
    _spawn_ok(c)
    c.send(F_EXIT, b"\2" + struct.pack(">i", 0))
    c.drain()


def _bad_header(c: Conn) -> None:
    _spawn_ok(c)
    c.sock.sendall(struct.pack(">BI", 99, 0))
    c.drain()


@pytest.mark.parametrize(
    ("script", "code", "needle"),
    [
        (_no_challenge, 125, "did not answer HELLO with a challenge"),
        (_reject_auth, 125, "rejected our token"),
        (_close_before_spawn, 125, "closed the connection before starting '/bin/sh'"),
        (_spawn_err, 127, "cannot run '/bin/sh': no such file (errno 2)"),
        (_spawn_err_empty, 127, "cannot run '/bin/sh': spawn failed (errno 5)"),
        (_wrong_first_frame, 125, "expected SPAWN_OK from the bridge daemon, got frame type 11"),
        (_signaled, 137, ""),
        (_unexpected_in_relay, 125, "unexpected frame type 2 from the bridge daemon"),
        (_lost_in_relay, 125, "lost before the exit status"),
        (_malformed_exit, 125, "malformed EXIT frame"),
        (_bad_header, 125, "lost before the exit status"),
    ],
)
def test_fake_daemon_misbehaviour(
    env: Env,
    fake: Callable[[Script], FakeDaemon],
    script: Script,
    code: int,
    needle: str,
) -> None:
    d = fake(script)
    proc = run_against(env, d, [env.sh, "-c", "true"])
    assert proc.returncode == code, err_text(proc)
    assert needle in err_text(proc)


def test_fake_daemon_spawn_ok_larger_than_a_chunk(
    env: Env, fake: Callable[[Script], FakeDaemon]
) -> None:
    d = fake(_big_spawn_ok)
    proc = run_against(env, d, [env.git, "-C", "C:\\r\\repo", "rev-parse", "--show-toplevel"])
    assert proc.returncode == 3, err_text(proc)
    assert proc.stdout == b"out"
    assert err_text(proc).endswith("err")


def test_fake_daemon_launch_detached(
    env: Env, fake: Callable[[Script], FakeDaemon]
) -> None:
    def script(c: Conn) -> None:
        req = c.handshake_and_req()
        assert b"open" in req
        c.send(F_SPAWN_OK, struct.pack(">I", 1))
        typ, _ = c.recv()
        assert typ == F_STDIN_EOF  # detached verbs never pump stdin
        c.send(F_EXIT, b"\0" + struct.pack(">i", 0))

    d = fake(script)
    proc = run_against(env, d, [env.launch, "open", "x"])
    assert proc.returncode == 0, err_text(proc)


# --------------------------------------------------------------------------
# shims against the real daemon
# --------------------------------------------------------------------------


def test_large_request_is_sent_in_two_writes(env: Env) -> None:
    """A REQ over one chunk (64 KiB) goes out as header, then payload."""
    big = {f"FLU_BIG{i}": str(i) * 30_000 for i in range(3)}
    proc = env.run(
        [env.sh, "-c", 'printf "%s" "$FLU_BIG0$FLU_BIG1$FLU_BIG2" | wc -c'],
        env=env.wine_env(**big),
    )
    assert proc.returncode == 0, err_text(proc)
    assert proc.stdout.strip() == b"90000"


def test_git_global_options_and_anchors(env: Env) -> None:
    log = env.root / "anchors.jsonl"
    proc = env.run(
        [
            env.git,
            "-C",
            "",
            "-C",
            "C:\\r",
            "-C",
            "repo",
            "-C",
            ".",
            "--git-dir=C:\\r\\repo\\.git",
            "--work-tree",
            "C:\\r\\repo",
            "-c",
            "core.quotePath=false",
            "--namespace",
            "ns",
            "--work-tree=C:\\r\\repo",
            "--git-dir",
            "C:\\r\\repo\\.git",
            "--no-pager",
            "rev-parse",
            "--show-toplevel",
        ],
        env=env.wine_env(FL_BRIDGE_LOG=str(log), GIT_DIR="C:\\r\\repo\\.git"),
    )
    assert proc.returncode == 0, err_text(proc)
    assert proc.stdout.decode().splitlines() == ["C:/r/repo"]
    rec = json.loads(log.read_text().splitlines()[-1])
    assert rec["subcmd"] == "rev-parse"
    assert rec["unix_pid"] > 0


def test_many_anchors_and_odd_names(env: Env) -> None:
    """More than 8 anchors, a duplicate, one containing ';' and a drive root."""
    dirs = [f"C:\\r\\a{i}" for i in range(10)]
    args: list[str | Path] = [env.git]
    for d in ["C:\\r\\semi;colon", "C:\\r\\repo", "C:\\r\\repo", "Z:\\", *dirs]:
        args += ["--work-tree", d]
    args += ["-C", "C:\\r\\repo", "rev-parse", "--show-toplevel"]
    proc = env.run(args)
    assert proc.returncode == 0, err_text(proc)


def test_git_double_dash_ends_global_options(env: Env) -> None:
    proc = env.run([env.git, "--", "version"])
    assert proc.returncode != 125, err_text(proc)


def test_git_without_subcommand_shows_usage(env: Env) -> None:
    proc = env.run([env.git, "-c", "x.y=1"])
    assert proc.returncode == 1, err_text(proc)


def test_nul_records_and_double_dash_in_subcommand(env: Env) -> None:
    for extra in (["ls-files", "-z"], ["config", "--null", "--list"], ["ls-files", "--", "-z"]):
        proc = env.run([env.git, "-C", "C:\\r\\repo", *extra])
        assert proc.returncode == 0, err_text(proc)


def test_drive_root_anchor_drops_trailing_slash(env: Env) -> None:
    link = env.prefix / "dosdevices" / "r:"
    link.symlink_to(env.repo)
    try:
        proc = env.run([env.git, "-C", "R:\\", "rev-parse", "--show-toplevel"])
    finally:
        link.unlink()
    assert proc.returncode == 0, err_text(proc)
    assert proc.stdout.decode().strip() == "R:/"


def test_stdin_pipe_never_closed_does_not_hold_the_exit(env: Env) -> None:
    """Our stdin is a Unix pipe Wine cannot cancel a read on: the pump is abandoned."""
    argv = [env.tools.wine, str(env.sh), "-c", "exit 3"]
    with subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        cwd=env.repo,
        env=env.wine_env(),
    ) as proc:
        try:
            rc = proc.wait(timeout=60)
        finally:
            assert proc.stdin is not None
            proc.stdin.close()
    assert rc == 3


# --------------------------------------------------------------------------
# record mode: bash / sh personas, errors
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def bundled(env: Env) -> str:
    """A fake gitInstance: <ver>\\cmd\\git.exe, usr\\bin\\bash.exe and bin\\sh.exe (cmd.exe copies)."""
    cmd = env.prefix / "drive_c" / "windows" / "system32" / "cmd.exe"
    ver = env.prefix / "drive_c" / "gi" / "ver"
    for rel in ("cmd/git.exe", "usr/bin/bash.exe", "bin/sh.exe"):
        (ver / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(cmd, ver / rel)
    return "C:\\gi\\ver\\cmd\\git.exe"


def record_env(env: Env, git: str, **extra: str) -> dict[str, str]:
    return env.wine_env(FL_BRIDGE_MODE="record", FL_BRIDGE_BUNDLED_GIT=git, **extra)


@pytest.mark.parametrize(("persona", "code"), [("bash", 5), ("sh", 6)])
def test_record_mode_shell_personas(
    env: Env, bundled: str, persona: str, code: int
) -> None:
    exe = env.bash if persona == "bash" else env.sh
    proc = env.run([exe, "/c", f"exit {code}"], env=record_env(env, bundled))
    assert proc.returncode == code, err_text(proc)


def test_record_mode_without_arguments(env: Env, bundled: str) -> None:
    """A command line that is only argv[0] (cmd.exe then reads the empty stdin)."""
    proc = env.run([env.drv, "run", env.sh], env=record_env(env, bundled))
    assert proc.returncode == 0, err_text(proc)


def test_record_mode_quoted_program_name(env: Env, bundled: str) -> None:
    spaced = env.root / "with space"
    spaced.mkdir(exist_ok=True)
    exe = spaced / "sh.exe"
    shutil.copyfile(env.sh, exe)
    proc = env.run([env.drv, "run", exe, "/c", "exit\t4"], env=record_env(env, bundled))
    assert proc.returncode == 4, err_text(proc)


@pytest.mark.parametrize(
    ("exe_name", "git", "code", "needle"),
    [
        ("git", "", 125, "record mode needs FL_BRIDGE_BUNDLED_GIT"),
        ("sh", "git.exe", 125, "cannot derive the bundled sh"),
        ("bash", "C:\\cmd\\git.exe", 127, "cannot start the bundled bash 'C:\\bin\\bash.exe'"),
        ("git", "C:\\no-such\\cmd\\git.exe", 127, "cannot start the bundled git 'C:\\no-such\\cmd\\git.exe'"),
    ],
)
def test_record_mode_errors(
    env: Env, exe_name: str, git: str, code: int, needle: str
) -> None:
    proc = env.run([env.bin_dir / f"{exe_name}.exe", "x"], env=record_env(env, git))
    assert proc.returncode == code, err_text(proc)
    assert needle in err_text(proc)


def test_record_mode_command_line_too_long(env: Env) -> None:
    target = "C:\\" + "t" * 300 + "\\git.exe"
    proc = env.run([env.drv, "long", env.git], env=record_env(env, target))
    assert proc.returncode == 125, err_text(proc)
    assert "command line too long" in err_text(proc)


# --------------------------------------------------------------------------
# fl-launch.exe
# --------------------------------------------------------------------------


def test_launch_logs_its_call(env: Env) -> None:
    log = env.root / "launch.jsonl"
    proc = env.run(
        [env.launch, "diff", "C:\\r\\repo\\a", "rel"],
        env=env.wine_env(FL_BRIDGE_LOG=str(log)),
    )
    assert proc.returncode == 9, err_text(proc)
    rec = json.loads(log.read_text().splitlines()[-1])
    assert rec["tool"] == "fl-launch"
    assert rec["xargv"][0] == "diff"
    assert rec["xargv"][2] == "rel"
    assert rec["exit"] == 9


def test_launch_bad_configuration(env: Env) -> None:
    proc = env.run([env.launch, "diff"], env=env.wine_env(FL_BRIDGE_PORT="<unset>"))
    assert proc.returncode == 125
    assert "FL_BRIDGE_PORT is not set" in err_text(proc)


def test_launch_unknown_verb_logs(env: Env) -> None:
    log = env.root / "launch-usage.jsonl"
    proc = env.run([env.launch, "explode"], env=env.wine_env(FL_BRIDGE_LOG=str(log)))
    assert proc.returncode == 2
    assert "unknown verb 'explode'" in err_text(proc)
    assert json.loads(log.read_text().splitlines()[-1])["exit"] == 2


# --------------------------------------------------------------------------
# odd inputs: global options, empty settings, unusual directories
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "args",
    [
        ["-C", "\\\\no-server\\share", "version"],
        ["--git-dir=\\\\no-server\\x", "version"],
        ["--git-dir=", "version"],
        ["--config-env", "a.b=HOME", "version"],
        ["--work-tree", "C:\\r\\no-such-fl", "-C", "C:\\r\\repo", "rev-parse", "--show-toplevel"],
        ["-C"],
        ["--git-dir"],
    ],
)
def test_odd_global_options_never_fail_the_bridge(env: Env, args: list[str]) -> None:
    proc = env.run([env.git, *args])
    assert proc.returncode not in (125, 127), err_text(proc)


@pytest.mark.parametrize(
    ("extra", "code"),
    [
        ({"GIT_DIR": ""}, 128),  # git itself refuses an empty GIT_DIR; no anchor for it
        ({"FL_BRIDGE_MODE": "bridge"}, 0),
        ({"FL_BRIDGE_MODE": ""}, 0),
        ({"FL_BRIDGE_LOG": ""}, 0),
    ],
)
def test_empty_or_explicit_settings(env: Env, extra: dict[str, str], code: int) -> None:
    proc = env.run([env.git, "-C", "C:\\r\\repo", "rev-parse", "--show-toplevel"], env=env.wine_env(**extra))
    assert proc.returncode == code, err_text(proc)


def test_launch_with_an_empty_log_setting(env: Env) -> None:
    proc = env.run([env.launch, "diff", "x"], env=env.wine_env(FL_BRIDGE_LOG=""))
    assert proc.returncode == 9, err_text(proc)


def test_cwd_with_a_semicolon_is_not_an_anchor(env: Env) -> None:
    """FL_BRIDGE_ANCHORS cannot hold a path containing ';': none is passed."""
    odd = env.root / "semi;colon"
    odd.mkdir(exist_ok=True)
    proc = env.run([env.sh, "-c", 'printf "%s" "${FL_BRIDGE_ANCHORS-unset}"'], cwd=odd)
    assert proc.returncode == 0, err_text(proc)
    assert proc.stdout == b"unset"


def test_record_mode_forward_slashes_and_a_directory_in_the_way(env: Env) -> None:
    """usr\\bin\\sh.exe is a directory here: the persona falls back to bin\\sh.exe."""
    cmd = env.prefix / "drive_c" / "windows" / "system32" / "cmd.exe"
    ver = env.prefix / "drive_c" / "gi2" / "ver"
    (ver / "usr" / "bin" / "sh.exe").mkdir(parents=True, exist_ok=True)
    (ver / "bin").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(cmd, ver / "bin" / "sh.exe")
    proc = env.run([env.sh, "/c", "exit 8"], env=record_env(env, "C:/gi2/ver/cmd/git.exe"))
    assert proc.returncode == 8, err_text(proc)


def test_record_mode_unc_target_and_no_std_handles(env: Env, bundled: str) -> None:
    proc = env.run([env.git, "x"], env=record_env(env, "\\\\no-server\\share\\git.exe"))
    assert proc.returncode == 127, err_text(proc)
    proc = env.run([env.drv, "nostd", env.git, "/c", "exit 2"], env=record_env(env, bundled))
    assert proc.returncode == 2, err_text(proc)
