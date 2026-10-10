"""Wine tier of the Windows bridge shims (bridge/win): fl-shim.exe and fl-launch.exe.

Runs the real PEs under Wine against a real ``fl-bridge-helper --daemon`` and checks
the behaviour Fork depends on: exit-status contract (child code, 128+signal, 125 /
127 / 141), stdin relay and the B2 exit fixes (stdin pipe held open, reader closed,
TerminateProcess), mutual authentication, lone-surrogate rejection, record mode
(including that the bridge token never reaches the bundled git) and fl-launch.

Gated: it only runs with ``FL_REAL_WINE=1`` (AGENTS.md hard rule 16). Inputs:

* ``FL_BRIDGE_WIN_DIR``: directory holding fl-shim.exe, fl-launch.exe and
  tests/fl-testdrv.exe (default: the meson outputs in build-bridge/bridge, built by
  scripts/build-bridge.sh, when complete; else build-bridge/dev, built by
  bridge/win/build-dev.sh);
* ``FL_WINE``: the wine binary (default: ``wine`` on PATH);
* ``FL_BRIDGE_HELPER_BIN``: a prebuilt daemon (default: built here with the host gcc).

Every Wine call runs in a throwaway prefix under pytest's temp directory with a fake
HOME, ``WINEDEBUG=-all`` and winemenubuilder disabled.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import signal
import subprocess
import tempfile
import time
from collections.abc import Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import pytest

from fixtures import real_tier

ROOT = Path(__file__).resolve().parents[2]
UNIX_DIR = ROOT / "bridge" / "unix"
COMMON_DIR = ROOT / "bridge" / "common"
RUN_TIMEOUT = 90.0


# --------------------------------------------------------------------------
# environment: gate, tools, prefix, daemon
# --------------------------------------------------------------------------


@dataclass
class Tools:
    """The binaries under test and the Wine to run them with."""

    wine: str
    shim: Path
    launch: Path
    drv: Path


def _default_win_dir() -> Path:
    """FL_BRIDGE_WIN_DIR, else the meson outputs (scripts/build-bridge.sh), else build-dev.sh's."""
    env = os.environ.get("FL_BRIDGE_WIN_DIR")
    if env:
        return Path(env)
    meson_dir = ROOT / "build-bridge" / "bridge"
    if all(
        (meson_dir / rel).is_file()
        for rel in ("fl-shim.exe", "fl-launch.exe", "tests/fl-testdrv.exe")
    ):
        return meson_dir
    return ROOT / "build-bridge" / "dev"


@pytest.fixture(scope="module")
def tools() -> Tools:
    """Locate Wine and the built PEs (gated on FL_REAL_WINE=1)."""
    if os.environ.get("FL_REAL_WINE") != "1":
        pytest.skip("Wine tier: set FL_REAL_WINE=1 to run the shims under Wine")
    # Containers only (AGENTS.md hard rule 18): FL_CI_STAGE=bridge ./scripts/ci-docker.sh.
    real_tier.enforce()
    wine = os.environ.get("FL_WINE") or shutil.which("wine")
    if not wine:
        pytest.skip("wine is not installed")
    win_dir = _default_win_dir()
    shim, launch, drv = (
        win_dir / "fl-shim.exe",
        win_dir / "fl-launch.exe",
        win_dir / "tests" / "fl-testdrv.exe",
    )
    for exe in (shim, launch, drv):
        if not exe.is_file():
            pytest.skip(f"{exe} is missing: run scripts/build-bridge.sh")
    return Tools(wine, shim, launch, drv)


@pytest.fixture(scope="module")
def helper_bin(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The daemon (prebuilt via FL_BRIDGE_HELPER_BIN, else built with the host gcc)."""
    prebuilt = os.environ.get("FL_BRIDGE_HELPER_BIN")
    if prebuilt:
        return Path(prebuilt).resolve()
    cc = shutil.which("gcc") or shutil.which("cc")
    if cc is None:
        pytest.skip("no C compiler (gcc/cc) on PATH")
    out = tmp_path_factory.mktemp("fl-helper-wine") / "fl-bridge-helper"
    sources = [UNIX_DIR / "fl_bridge_helper.c", UNIX_DIR / "fl_helper_util.c"]
    sources += [COMMON_DIR / "fl_proto.c", COMMON_DIR / "fl_sha256.c"]
    cmd = [
        cc,
        "-std=c11",
        "-O1",
        "-Wall",
        "-Wextra",
        "-Werror",
        f"-I{COMMON_DIR}",
        f"-I{UNIX_DIR}",
    ]
    cmd += ['-DFL_VERSION="test"', "-o", str(out), *[str(s) for s in sources]]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    assert proc.returncode == 0, f"building fl-bridge-helper failed:\n{proc.stderr}"
    return out


@dataclass
class Env:
    """A Wine prefix with the shims installed and a running daemon."""

    tools: Tools
    root: Path
    home: Path
    prefix: Path
    bin_dir: Path
    repo: Path
    port: int
    token: str
    helper_log: Path
    daemon: subprocess.Popen[bytes]

    @property
    def git(self) -> Path:
        return self.bin_dir / "git.exe"

    @property
    def sh(self) -> Path:
        return self.bin_dir / "sh.exe"

    @property
    def bash(self) -> Path:
        return self.bin_dir / "bash.exe"

    @property
    def launch(self) -> Path:
        return self.bin_dir / "fl-launch.exe"

    @property
    def drv(self) -> Path:
        return self.bin_dir / "fl-testdrv.exe"

    def wine_env(self, **extra: str) -> dict[str, str]:
        """The environment Fork's children inherit: isolated, with the FL_BRIDGE_* contract."""
        env = {
            "HOME": str(self.home),
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "WINEPREFIX": str(self.prefix),
            "WINEDEBUG": "-all",
            "WINEDLLOVERRIDES": "mscoree,mshtml=;winemenubuilder.exe=d",
            "FL_BRIDGE_PORT": str(self.port),
            "FL_BRIDGE_TOKEN": self.token,
            "FL_BRIDGE_WINEXEC": "/usr/bin/true",
        }
        env.update(extra)
        return {k: v for k, v in env.items() if v != "<unset>"}

    def run(
        self,
        args: Sequence[str | Path],
        *,
        stdin: bytes = b"",
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        """Run one PE under Wine and wait for it.

        The std streams are temp files, not pipes: a wineserver or Wine service started
        by this call may inherit them and outlive it, and must not hold the call open.
        """
        argv = [self.tools.wine, *[str(a) for a in args]]
        with (
            tempfile.TemporaryFile() as fin,
            tempfile.TemporaryFile() as fout,
            tempfile.TemporaryFile() as ferr,
        ):
            fin.write(stdin)
            fin.seek(0)
            proc = subprocess.run(
                argv,
                stdin=fin,
                stdout=fout,
                stderr=ferr,
                cwd=cwd or self.repo,
                env=env or self.wine_env(),
                timeout=RUN_TIMEOUT,
                check=False,
            )
            fout.seek(0)
            ferr.seek(0)
            return subprocess.CompletedProcess(
                argv, proc.returncode, fout.read(), ferr.read()
            )


def find_wineserver(wine: str) -> str | None:
    """The wineserver that belongs to `wine` (sibling, PATH, or the Debian/Ubuntu libdir)."""
    resolved = shutil.which(wine) or wine
    for cand in (
        os.environ.get("FL_WINESERVER", ""),
        str(Path(resolved).with_name("wineserver")),
        shutil.which("wineserver") or "",
        "/usr/lib/x86_64-linux-gnu/wine/wineserver",
        "/usr/lib/wine/wineserver",
    ):
        if cand and Path(cand).is_file() and os.access(cand, os.X_OK):
            return cand
    return None


HOST_HELPER = """#!/bin/sh
# fake fork-linux-host: record argv, exit 9 for "diff"
printf '%s\\n' "$@" > "$FL_TEST_HELPER_LOG.$1"
[ "$1" = diff ] && exit 9
exit 0
"""


@pytest.fixture(scope="module")
def env(
    tools: Tools, helper_bin: Path, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[Env]:
    """Prefix + shims + a daemon with a fake host helper."""
    root = tmp_path_factory.mktemp("shims-wine")
    home, prefix, bin_dir, repo = (
        root / "home",
        root / "prefix",
        root / "shims",
        root / "repo",
    )
    for d in (home, bin_dir, repo):
        d.mkdir()
    for name in ("git.exe", "sh.exe", "bash.exe"):
        shutil.copyfile(tools.shim, bin_dir / name)
    shutil.copyfile(tools.launch, bin_dir / "fl-launch.exe")
    shutil.copyfile(tools.drv, bin_dir / "fl-testdrv.exe")
    shutil.copyfile(tools.shim, bin_dir / "notgit.exe")

    base = {
        "HOME": str(home),
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "WINEPREFIX": str(prefix),
        "WINEDEBUG": "-all",
        "WINEDLLOVERRIDES": "mscoree,mshtml=;winemenubuilder.exe=d",
    }
    wineserver = find_wineserver(tools.wine)
    if wineserver is not None:
        # One persistent wineserver for the module: no start-up per call, no lingering one.
        subprocess.run(
            [wineserver, "-p"],
            env=base,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=60,
            check=False,
        )
    boot = subprocess.run(
        [tools.wine, "wineboot", "-i"],
        env=base,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=300,
        check=False,
    )
    assert boot.returncode == 0, "wineboot -i failed"

    git_env = {**base, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"}
    for cmd in (
        ["git", "init", "-q", str(repo)],
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@e",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "init",
        ],
    ):
        subprocess.run(cmd, check=True, env=git_env, capture_output=True)
    # A logical C: path whose physical location differs (realpath anchors).
    (prefix / "drive_c" / "r").mkdir()
    (prefix / "drive_c" / "r" / "repo").symlink_to(repo)

    helper = root / "fork-linux-host"
    helper.write_text(HOST_HELPER)
    helper.chmod(0o755)
    helper_log = root / "helper"
    token = secrets.token_hex(32)
    tok_file = root / "token"
    fd = os.open(tok_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(token + "\n")
    denv = {
        "HOME": str(home),
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "TMPDIR": str(root),
        "GIT_CONFIG_NOSYSTEM": "1",
        "FL_TEST_HELPER_LOG": str(helper_log),
    }
    daemon = subprocess.Popen(
        [
            str(helper_bin),
            "--daemon",
            "--port",
            "0",
            "--token-file",
            str(tok_file),
            "--log",
            str(root / "daemon.log"),
            "--host-helper",
            str(helper),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=denv,
    )
    assert daemon.stdout is not None
    line = daemon.stdout.readline().decode()
    assert line.startswith("FL_BRIDGE_PORT="), line
    e = Env(
        tools,
        root,
        home,
        prefix,
        bin_dir,
        repo,
        int(line.split("=", 1)[1]),
        token,
        helper_log,
        daemon,
    )
    yield e
    daemon.send_signal(signal.SIGTERM)
    try:
        daemon.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        daemon.kill()
        daemon.communicate()
    if wineserver is not None:
        subprocess.run(
            [wineserver, "-k"],
            env=base,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=60,
            check=False,
        )


def err_text(proc: subprocess.CompletedProcess[bytes]) -> str:
    return proc.stderr.decode("utf-8", "replace")


def drv_ms(proc: subprocess.CompletedProcess[bytes]) -> int:
    """The elapsed time fl-testdrv reports for its child."""
    for line in err_text(proc).splitlines():
        if line.startswith("fl-testdrv: rc="):
            return int(line.rsplit("ms=", 1)[1])
    raise AssertionError(f"no fl-testdrv status line in {err_text(proc)!r}")


def pid_alive(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            state = fh.read().rsplit(b")", 1)[1].split()[0]
    except OSError:
        return False
    return state != b"Z"


# --------------------------------------------------------------------------
# git persona, bridge mode
# --------------------------------------------------------------------------


def test_git_version(env: Env) -> None:
    proc = env.run([env.git, "--version"])
    assert proc.returncode == 0, err_text(proc)
    assert proc.stdout.startswith(b"git version ")


def test_rev_parse_paths_come_back_logical(env: Env) -> None:
    """The realpath of C:\\r\\repo is the physical repo; Fork must see C:/r/repo."""
    proc = env.run(
        [
            env.git,
            "-C",
            "C:\\r\\repo",
            "rev-parse",
            "--show-toplevel",
            "--absolute-git-dir",
        ]
    )
    assert proc.returncode == 0, err_text(proc)
    assert proc.stdout.decode().splitlines() == ["C:/r/repo", "C:/r/repo/.git"]


def test_git_error_exit_code_and_stderr(env: Env) -> None:
    proc = env.run([env.git, "-C", "C:\\r\\missing", "status"])
    assert proc.returncode == 128
    assert "C:/r/missing" in err_text(proc)


def test_unknown_persona_is_refused(env: Env) -> None:
    proc = env.run([env.bin_dir / "notgit.exe", "--version"])
    assert proc.returncode == 125
    assert "unknown persona" in err_text(proc)


# --------------------------------------------------------------------------
# sh / bash personas: exit status contract, argv, stdin
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("script", "code"),
    [("exit 0", 0), ("exit 7", 7), ("exit 255", 255), ("kill -TERM $$", 143)],
)
def test_exit_status_contract(env: Env, script: str, code: int) -> None:
    proc = env.run([env.sh, "-c", script])
    assert proc.returncode == code, err_text(proc)


def test_argv_round_trip(env: Env) -> None:
    args = [
        "*",
        "",
        "a b",
        'q"uote',
        "back\\slash\\",
        "\u00fcn\u00ef\u20ac\U0001f600",
        "-m",
        "https://u:p@h/x",
    ]
    proc = env.run(
        [env.bash, "-c", 'for a; do printf "<%s>\\n" "$a"; done', "x", *args]
    )
    assert proc.returncode == 0, err_text(proc)
    assert proc.stdout.decode().splitlines() == [f"<{a}>" for a in args]


def test_whole_argument_windows_paths_are_translated(env: Env) -> None:
    proc = env.run(
        [
            env.sh,
            "-c",
            'printf "%s\\n" "$1" "$2"',
            "x",
            "C:\\r\\repo",
            "--opt=C:\\r\\repo",
        ]
    )
    assert proc.returncode == 0, err_text(proc)
    first, second = proc.stdout.decode().splitlines()
    # Wine 10 keeps the dosdevices form, Wine 11 resolves the symlink: same directory.
    assert first.startswith("/")
    assert os.path.realpath(first) == os.path.realpath(env.repo)
    assert second == "--opt=C:\\r\\repo"


def test_binary_stdin_round_trip(env: Env) -> None:
    data = os.urandom(3 * 1024 * 1024 + 17)
    proc = env.run([env.sh, "-c", "cat"], stdin=data)
    assert proc.returncode == 0, err_text(proc)
    assert proc.stdout == data


# --------------------------------------------------------------------------
# Fork-like spawns (fl-testdrv): B2 fixes and Win32 edge cases
# --------------------------------------------------------------------------


def test_fork_like_pipes(env: Env) -> None:
    proc = env.run(
        [env.drv, "run", env.sh, "-c", "cat; echo err >&2; exit 5"], stdin=b"hello\n"
    )
    assert proc.returncode == 5
    assert proc.stdout == b"hello\n"
    assert "err" in err_text(proc)


def test_stdin_pipe_held_open_does_not_hang_or_lose_the_code(env: Env) -> None:
    """Fork may never close the child's stdin: exit must still be prompt and exact (B2 fix 1)."""
    proc = env.run([env.drv, "hold", env.sh, "-c", "exit 4"])
    assert proc.returncode == 4, err_text(proc)
    assert drv_ms(proc) < 3000


def test_closed_reader_gives_141(env: Env) -> None:
    proc = env.run([env.drv, "closeout", env.sh, "-c", "yes | head -c 100000000"])
    assert proc.returncode == 141, err_text(proc)
    assert drv_ms(proc) < 15000


def test_terminated_shim_takes_the_unix_child_down(env: Env) -> None:
    pidfile = env.root / "killed.pid"
    proc = env.run(
        [env.drv, "kill", "1500", env.sh, "-c", f"echo $$ > '{pidfile}'; exec sleep 60"]
    )
    assert proc.returncode == 99, err_text(proc)
    pid = int(pidfile.read_text().strip())
    deadline = time.monotonic() + 10
    while pid_alive(pid):
        assert time.monotonic() < deadline, (
            f"unix child {pid} survived TerminateProcess of the shim"
        )
        time.sleep(0.05)


def test_lone_surrogate_argument_is_refused(env: Env) -> None:
    """A lone UTF-16 surrogate must not silently become U+FFFD (a different file name)."""
    proc = env.run([env.drv, "surrogate", env.sh, "-c", 'echo "$@"', "x"])
    assert proc.returncode == 125
    assert "not valid UTF-16" in err_text(proc)


def test_no_std_handles(env: Env) -> None:
    proc = env.run([env.drv, "nostd", env.sh, "-c", "exit 6"])
    assert proc.returncode == 6, err_text(proc)


def test_parallel_exit_codes_are_exact(env: Env) -> None:
    def one(i: int) -> tuple[int, int, bytes]:
        mode = "hold" if i % 2 else "run"
        p = env.run(
            [env.drv, mode, env.sh, "-c", f"echo o{i}; exit {i + 2}"], stdin=b"x"
        )
        return i, p.returncode, p.stdout

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(one, range(24)))
    bad = [
        (i, rc, out)
        for i, rc, out in results
        if rc != i + 2 or out != f"o{i}\n".encode()
    ]
    assert not bad


# --------------------------------------------------------------------------
# configuration and authentication failures -> 125
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("extra", "needle"),
    [
        ({"FL_BRIDGE_PORT": "<unset>"}, "FL_BRIDGE_PORT is not set"),
        ({"FL_BRIDGE_PORT": "99999"}, "not a TCP port"),
        ({"FL_BRIDGE_TOKEN": "<unset>"}, "FL_BRIDGE_TOKEN is not set"),
        ({"FL_BRIDGE_TOKEN": "abc"}, "hex characters"),
        ({"FL_BRIDGE_WINEXEC": "<unset>"}, "FL_BRIDGE_WINEXEC"),
        ({"FL_BRIDGE_MODE": "bogus"}, "FL_BRIDGE_MODE"),
    ],
)
def test_bad_configuration(env: Env, extra: dict[str, str], needle: str) -> None:
    proc = env.run([env.git, "--version"], env=env.wine_env(**extra))
    assert proc.returncode == 125
    assert needle in err_text(proc)


def test_wrong_token_fails_closed(env: Env) -> None:
    proc = env.run(
        [env.git, "--version"], env=env.wine_env(FL_BRIDGE_TOKEN=secrets.token_hex(32))
    )
    assert proc.returncode == 125
    assert "authentication failed" in err_text(proc)
    assert b"git version" not in proc.stdout


def test_nothing_listening_fails_closed(env: Env) -> None:
    proc = env.run([env.git, "--version"], env=env.wine_env(FL_BRIDGE_PORT="1"))
    assert proc.returncode == 125


def test_bridge_log_line_redacts_and_never_holds_the_token(env: Env) -> None:
    log = env.root / "bridge-calls.jsonl"
    proc = env.run(
        [
            env.git,
            "-c",
            "http.extraHeader=X-Api-Key: s3cr3t",
            "ls-remote",
            "https://user:hunter2@127.0.0.1:1/x.git",
        ],
        env=env.wine_env(FL_BRIDGE_LOG=str(log), GIT_TERMINAL_PROMPT="0"),
    )
    assert proc.returncode != 125, err_text(proc)
    text = log.read_text()
    assert env.token not in text
    assert "hunter2" not in text
    assert "s3cr3t" not in text
    rec = json.loads(text.splitlines()[-1])
    assert rec["mode"] == "bridge"
    assert rec["persona"] == "git"
    assert rec["exit"] == proc.returncode
    assert rec["xargv"][0] == "git"


# --------------------------------------------------------------------------
# record mode
# --------------------------------------------------------------------------


def test_record_mode_forwards_and_logs(env: Env) -> None:
    log = env.root / "record.jsonl"
    renv = env.wine_env(
        FL_BRIDGE_MODE="record",
        FL_BRIDGE_BUNDLED_GIT="C:\\windows\\system32\\cmd.exe",
        FL_BRIDGE_LOG=str(log),
    )
    proc = env.run([env.drv, "run", env.git, "/c", "exit 3"], env=renv)
    assert proc.returncode == 3, err_text(proc)
    # The bundled program (and so its hooks and helpers) never sees the token.
    proc = env.run([env.git, "/c", "set", "FL_BRIDGE_TOKEN"], env=renv)
    assert env.token.encode() not in proc.stdout
    assert proc.returncode == 1
    lines = [json.loads(line) for line in log.read_text().splitlines()]
    assert [r["exit"] for r in lines] == [3, 1]
    assert lines[0]["mode"] == "record"
    assert lines[0]["argv"][1:] == ["/c", "exit 3"]
    assert lines[0]["stdio"] == {"stdin": "pipe", "stdout": "pipe", "stderr": "pipe"}
    assert env.token not in log.read_text()


def test_record_mode_refuses_to_recurse_into_itself(env: Env) -> None:
    renv = env.wine_env(
        FL_BRIDGE_MODE="record",
        FL_BRIDGE_BUNDLED_GIT="Z:" + str(env.git).replace("/", "\\"),
    )
    proc = env.run([env.git, "--version"], env=renv)
    assert proc.returncode == 125
    assert "refusing to recurse" in err_text(proc)


# --------------------------------------------------------------------------
# fl-launch.exe
# --------------------------------------------------------------------------


def test_launch_usage(env: Env) -> None:
    assert env.run([env.launch]).returncode == 2
    assert env.run([env.launch, "explode"]).returncode == 2


def test_launch_detached_verb_translates_paths(env: Env) -> None:
    out = Path(f"{env.helper_log}.open")
    proc = env.run([env.launch, "open", "C:\\r\\repo\\file.txt", "--flag"])
    assert proc.returncode == 0, err_text(proc)
    deadline = time.monotonic() + 10
    while not out.exists():
        assert time.monotonic() < deadline, "the host helper never ran"
        time.sleep(0.05)
    time.sleep(0.2)
    verb, path, flag = out.read_text().splitlines()
    assert verb == "open"
    assert flag == "--flag"
    assert path.startswith("/")
    assert path.endswith("/file.txt")
    assert os.path.realpath(path) == os.path.realpath(env.repo / "file.txt")


def test_launch_waiting_verb_propagates_exit_code(env: Env) -> None:
    proc = env.run([env.launch, "diff", "C:\\r\\repo\\a", "C:\\r\\repo\\b"])
    assert proc.returncode == 9, err_text(proc)
