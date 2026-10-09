"""Shared setup of the bridge Wine tier (tests/bridge/wine): prefix, shims, daemon, driver.

Used by ``test_wine_bridge.py`` (pytest) and ``bench_bridge.py`` (the benchmark script).
Everything lives in a caller-given scratch directory: a fresh Wine prefix booted with
``wineboot -i``, a fake HOME, the shims installed at the production layout
(``C:\\fork-linux\\gitInstance\\...``), and a real ``fl-bridge-helper --daemon`` started
natively (outside Wine) with a token file, the way the launcher starts it. Never touches
``~/.wine`` or the real HOME.

Inputs (environment):

* ``FL_TEST_WINE``: ``os.pathsep``-separated wine binaries (default: ``/usr/bin/wine`` when
  present); add the pinned staging build here, for example
  ``FL_TEST_WINE=/usr/bin/wine:<runtimes>/wine-11.0-staging-amd64-wow64/bin/wine``;
* ``FL_BRIDGE_BUILD``: the meson output directory holding ``fl-shim.exe``,
  ``fl-launch.exe``, ``fl-bridge-helper`` and ``tests/fl-testdriver.exe`` (default:
  ``build-bridge/bridge``, built by ``scripts/build-bridge.sh``);
* ``FL_BRIDGE_HELPER_BIN``: a different daemon binary (for example the musl build).
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import shutil
import signal
import subprocess
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
WINE_ISOLATION = {
    "WINEDEBUG": "-all",
    "WINEDLLOVERRIDES": "mscoree,mshtml=;winemenubuilder.exe=d",
}
# Where the Python runtime installs the shims (bridge/win/README.md "Install layout").
GIT_PERSONAS = ("cmd/git.exe", "bin/git.exe", "mingw64/bin/git.exe")
SHELL_PERSONAS = ("bin/bash.exe", "bin/sh.exe", "usr/bin/bash.exe", "usr/bin/sh.exe")
RUN_TIMEOUT = 300.0

FAKE_HOST_HELPER = """#!/bin/sh
# Test stand-in for fork-linux-host: "run [--] PROG ARGS..." runs PROG and returns its status.
verb="$1"; shift
case "$verb" in
  run) [ "$1" = "--" ] && shift; exec "$@" ;;
  *) printf 'fake host helper: %s\\n' "$verb" >&2; exit 3 ;;
esac
"""


def wine_candidates() -> list[str]:
    """The wine binaries to test with (FL_TEST_WINE, else /usr/bin/wine when it exists)."""
    raw = os.environ.get("FL_TEST_WINE", "")
    if raw:
        return [w for w in raw.split(os.pathsep) if w]
    system = Path("/usr/bin/wine")
    return [str(system)] if system.is_file() and os.access(system, os.X_OK) else []


def wine_label(wine: str) -> str:
    """A short id for a wine binary: 'system' for /usr/bin/wine, else its runtime dir."""
    path = Path(wine)
    if path.parent.name == "bin" and path.parent.parent.name.startswith("wine-"):
        return path.parent.parent.name
    return "system" if str(path) == "/usr/bin/wine" else path.name


def find_wineserver(wine: str) -> str | None:
    """The wineserver that belongs to `wine` (sibling, the Debian/Ubuntu libdir, PATH)."""
    resolved = Path(shutil.which(wine) or wine)
    for cand in (
        resolved.with_name("wineserver"),
        Path("/usr/lib/wine/wineserver"),
        Path("/usr/lib/x86_64-linux-gnu/wine/wineserver"),
        Path(shutil.which("wineserver") or "/nonexistent"),
    ):
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand)
    return None


@dataclass
class Build:
    """The bridge binaries under test."""

    shim: Path
    launch: Path
    helper: Path
    driver: Path


def locate_build() -> tuple[Build | None, str]:
    """The built binaries, or (None, reason) when something is missing."""
    out = Path(os.environ.get("FL_BRIDGE_BUILD", str(ROOT / "build-bridge" / "bridge")))
    helper = Path(os.environ.get("FL_BRIDGE_HELPER_BIN", str(out / "fl-bridge-helper")))
    build = Build(
        out / "fl-shim.exe",
        out / "fl-launch.exe",
        helper,
        out / "tests" / "fl-testdriver.exe",
    )
    for path in (build.shim, build.launch, build.helper, build.driver):
        if not path.is_file():
            return None, f"{path} is missing: run scripts/build-bridge.sh"
    return build, ""


def win_path(unix: Path | str) -> str:
    """The Z: form of a Unix path, with backslashes (how Fork spells paths)."""
    return "Z:" + str(unix).replace("/", "\\")


def fwd_path(unix: Path | str) -> str:
    """The Z:/ form Git for Windows prints (and the bridge's output translation emits)."""
    return "Z:" + str(unix)


@dataclass
class DriverResult:
    """One fl-testdriver.exe ``run`` result."""

    rc: int
    ms: float
    stdout: bytes
    stderr: bytes
    stdout_len: int
    stdout_sha256: str
    killed: bool
    raw: dict[str, object] = field(repr=False)

    @property
    def out(self) -> str:
        return self.stdout.decode("utf-8", "replace")

    @property
    def err(self) -> str:
        return self.stderr.decode("utf-8", "replace")


class WineBridge:
    """A prefix with the shims installed and a running daemon, for one wine binary."""

    def __init__(self, wine: str, build: Build, root: Path) -> None:
        self.wine = wine
        self.build = build
        self.root = root
        self.home = root / "home"
        self.prefix = root / "prefix"
        self.libexec = root / "libexec"
        self.token = secrets.token_hex(32)
        self.port = 0
        self.daemon: subprocess.Popen[bytes] | None = None
        self.wineserver = find_wineserver(wine)
        self.instance = self.prefix / "drive_c" / "fork-linux" / "gitInstance"
        self.launch_exe = (
            self.prefix / "drive_c" / "fork-linux" / "bin" / "fl-launch.exe"
        )
        self.driver = root / "fl-testdriver.exe"
        self.boot_attempts = 0

    # ------------------------------------------------------------------ setup

    def base_env(self) -> dict[str, str]:
        """Isolated environment for every process we start (fake HOME, scratch prefix)."""
        return {
            "HOME": str(self.home),
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "LANG": "C.UTF-8",
            # No TMPDIR: Ubuntu 26.04's wine 10.0 aborts in wineboot (free(): invalid
            # pointer) inside the CI container when TMPDIR is set (B5 notes).
            "WINEPREFIX": str(self.prefix),
            **WINE_ISOLATION,
        }

    def _quiet(self, argv: Sequence[str], timeout: float) -> int:
        return subprocess.run(
            list(argv),
            env=self.base_env(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        ).returncode

    def boot_prefix(self, attempts: int = 3, timeout: float = 240.0) -> None:
        """``wineboot -i`` a fresh prefix, retrying a hung boot.

        A fresh wine-11.0-staging prefix sometimes never finishes booting (explorer.exe
        dies early and rundll32 setupapi waits forever; docs/spikes/B5-bridge-wine-tier.md).
        A hung attempt is killed with ``wineserver -k`` and the prefix is recreated.
        """
        for attempt in range(1, attempts + 1):
            if self.wineserver is not None:
                # One persistent wineserver for the whole run: no start-up cost per call.
                self._quiet([self.wineserver, "-p"], 60)
            try:
                rc = self._quiet([self.wine, "wineboot", "-i"], timeout)
            except subprocess.TimeoutExpired:
                rc = None
            if rc == 0:
                self.boot_attempts = attempt
                return
            if self.wineserver is not None:
                self._quiet([self.wineserver, "-k"], 60)
            shutil.rmtree(self.prefix, ignore_errors=True)
        raise RuntimeError(f"wineboot -i failed {attempts} times for {self.wine}")

    def start(self) -> None:
        """Boot the prefix, install the shims, start the daemon."""
        self.home.mkdir(parents=True)
        self.libexec.mkdir()
        self.boot_prefix()
        for rel in GIT_PERSONAS + SHELL_PERSONAS:
            dst = self.instance / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(self.build.shim, dst)
        self.launch_exe.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self.build.launch, self.launch_exe)
        shutil.copyfile(self.build.driver, self.driver)

        helper = self.libexec / "fl-bridge-helper"
        shutil.copyfile(self.build.helper, helper)
        helper.chmod(0o755)
        for persona in ("fl-winexec", "fl-askpass", "fl-ssh-askpass"):
            (self.libexec / persona).symlink_to("fl-bridge-helper")
        host = self.libexec / "fork-linux-host"
        host.write_text(FAKE_HOST_HELPER, encoding="utf-8")
        host.chmod(0o755)
        (self.home / ".gitconfig").write_text(
            "[user]\n\tname = Wine Tier\n\temail = wine-tier@example.invalid\n",
            encoding="utf-8",
        )

        tok_file = self.root / "token"
        fd = os.open(tok_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as fh:
            fh.write(self.token + "\n")
        denv = {**self.base_env(), "FL_WINE": self.wine, "GIT_CONFIG_NOSYSTEM": "1"}
        self.daemon = subprocess.Popen(
            [
                str(helper),
                "--daemon",
                "--port",
                "0",
                "--token-file",
                str(tok_file),
                "--host-helper",
                str(host),
                "--log",
                str(self.root / "daemon.log"),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=denv,
        )
        assert self.daemon.stdout is not None
        line = self.daemon.stdout.readline().decode("ascii", "replace")
        if not line.startswith("FL_BRIDGE_PORT="):
            raise RuntimeError(f"daemon did not report its port: {line!r}")
        self.port = int(line.split("=", 1)[1])

    def stop(self) -> None:
        """Stop the daemon and the wineserver."""
        if self.daemon is not None:
            self.daemon.send_signal(signal.SIGTERM)
            try:
                self.daemon.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.daemon.kill()
                self.daemon.wait()
            if self.daemon.stdout is not None:
                self.daemon.stdout.close()
        if self.wineserver is not None:
            self._quiet([self.wineserver, "-k"], 60)

    # ------------------------------------------------------------------ running

    @property
    def git(self) -> str:
        """Fork's git: the shim at gitInstance\\cmd\\git.exe."""
        return "C:\\fork-linux\\gitInstance\\cmd\\git.exe"

    def shim(self, rel: str) -> str:
        return "C:\\fork-linux\\gitInstance\\" + rel.replace("/", "\\")

    @property
    def launch(self) -> str:
        return "C:\\fork-linux\\bin\\fl-launch.exe"

    @property
    def winexec(self) -> str:
        return str(self.libexec / "fl-winexec")

    def wine_env(self, **extra: str) -> dict[str, str]:
        """What Fork's children inherit: the launcher's FL_* contract over the base env."""
        env = {
            **self.base_env(),
            "FL_BRIDGE_PORT": str(self.port),
            "FL_BRIDGE_TOKEN": self.token,
            "FL_BRIDGE_WINEXEC": self.winexec,
            "FL_WINE": self.wine,
        }
        env.update(extra)
        return env

    def wine_run(
        self,
        argv: Sequence[str],
        env: dict[str, str] | None = None,
        timeout: float = RUN_TIMEOUT,
    ) -> subprocess.CompletedProcess[bytes]:
        """``wine <argv>``; std streams are temp files so a lingering Wine process cannot hold them."""
        cmd = [self.wine, *argv]
        with tempfile.TemporaryFile() as fout, tempfile.TemporaryFile() as ferr:
            proc = subprocess.run(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=fout,
                stderr=ferr,
                cwd=self.root,
                env=env or self.wine_env(),
                timeout=timeout,
                check=False,
            )
            fout.seek(0)
            ferr.seek(0)
            return subprocess.CompletedProcess(
                cmd, proc.returncode, fout.read(), ferr.read()
            )

    def drive(
        self,
        exe: str,
        args: Sequence[str],
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        unset: Sequence[str] = (),
        stdin_file: Path | None = None,
        kill_after_ms: int = 0,
        max_capture: int | None = None,
        wine_env: dict[str, str] | None = None,
    ) -> DriverResult:
        """Run <exe> through fl-testdriver.exe (.NET Process.Start style) and parse its JSON."""
        argv = [str(self.driver), "run"]
        if cwd is not None:
            argv += ["--cwd", cwd]
        for key, value in (env or {}).items():
            argv += ["--env", f"{key}={value}"]
        for key in unset:
            argv += ["--unset", key]
        if stdin_file is not None:
            argv += ["--stdin", str(stdin_file)]
        if kill_after_ms:
            argv += ["--kill-after", str(kill_after_ms)]
        if max_capture is not None:
            argv += ["--max-capture", str(max_capture)]
        argv += ["--", exe, *args]
        proc = self.wine_run(argv, env=wine_env)
        return parse_driver(proc)

    def bench(
        self, n: int, exe: str, args: Sequence[str], *, cwd: str | None = None
    ) -> dict[str, object]:
        """fl-testdriver.exe bench: N sequential runs; returns its JSON summary."""
        argv = [str(self.driver), "bench", str(n)]
        if cwd is not None:
            argv += ["--cwd", cwd]
        argv += ["--", exe, *args]
        proc = self.wine_run(argv, timeout=3600)
        return json.loads(last_json_line(proc))


def last_json_line(proc: subprocess.CompletedProcess[bytes]) -> str:
    for line in reversed(proc.stdout.decode("utf-8", "replace").splitlines()):
        if line.startswith("{"):
            return line
    raise AssertionError(
        f"no JSON from fl-testdriver (rc {proc.returncode}): "
        f"{proc.stdout[-400:]!r} {proc.stderr[-2000:]!r}"
    )


def parse_driver(proc: subprocess.CompletedProcess[bytes]) -> DriverResult:
    data = json.loads(last_json_line(proc))
    if "error" in data:
        raise AssertionError(f"fl-testdriver could not start the program: {data}")
    return DriverResult(
        rc=int(data["rc"]),
        ms=float(data["ms"]),
        stdout=base64.b64decode(str(data["stdout_b64"])),
        stderr=base64.b64decode(str(data["stderr_b64"])),
        stdout_len=int(data["stdout_len"]),
        stdout_sha256=str(data["stdout_sha256"]),
        killed=bool(data["killed"]),
        raw=data,
    )


def native_git(
    args: Sequence[str], cwd: Path, home: Path, stdin: bytes | None = None
) -> subprocess.CompletedProcess[bytes]:
    """Host git with the same isolated config the daemon's children see."""
    env = {
        "HOME": str(home),
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    return subprocess.run(
        ["git", *args], cwd=cwd, env=env, input=stdin, capture_output=True, check=False
    )


def wait_until(pred: Callable[[], bool], timeout: float, step: float = 0.02) -> bool:
    """Poll `pred` until it returns true or `timeout` seconds pass."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(step)
    return pred()
