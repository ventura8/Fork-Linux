"""Wine tier: the launcher-started bridge daemon and a shim under real Wine (``FL_REAL_WINE=1``).

Builds the bridge with ``scripts/build-bridge.sh``, boots a scratch prefix in a fake HOME
(our default prefix location under that HOME, never ``~/.wine``), installs the shims with
the real ``host_shims`` step, enables ``[git] bridge`` and runs ``launcher_child.py``: the
launcher's own bridge path (``bridge.start_daemon`` -> ``launcher.build_spec`` ->
``launcher._exec``), with Wine running the bridged ``sh.exe`` instead of Fork. Checks that
the daemon survives the exec (its parent is the Wine process that replaced the launcher),
that the shim runs native git and ``/bin/sh`` through it, and that the daemon stops once
that Wine process exits.

Inputs: ``FL_WINE`` (default ``/usr/bin/wine``); ``FL_BRIDGE_SKIP_BUILD=1`` uses an
existing ``build-bridge/`` instead of running the build script.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

if os.environ.get("FL_REAL_WINE") != "1":
    pytest.skip("requires FL_REAL_WINE=1", allow_module_level=True)

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
CHILD = Path(__file__).resolve().parent / "launcher_child.py"
USER = "flqa"
BOOT_TIMEOUT = 300.0
RUN_TIMEOUT = 120.0
SCRIPT = "sleep 3; uname -s; git --version; pwd"


def _wineserver(wine: Path) -> Path:
    for candidate in (
        wine.with_name("wineserver"),
        Path("/usr/lib/wine/wineserver"),
        Path("/usr/lib/x86_64-linux-gnu/wine/wineserver"),
    ):
        if candidate.is_file():
            return candidate
    found = shutil.which("wineserver")
    if found is None:
        pytest.skip(f"no wineserver for {wine}")
    return Path(found)


def _build() -> None:
    if os.environ.get("FL_BRIDGE_SKIP_BUILD") == "1":
        return
    done = subprocess.run(
        [str(ROOT / "scripts" / "build-bridge.sh")], capture_output=True, text=True, timeout=3600, check=False
    )
    assert done.returncode == 0, done.stdout[-2000:] + done.stderr[-2000:]


def _env(home: Path) -> dict[str, str]:
    env = {
        "HOME": str(home),
        "USER": USER,
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_DATA_HOME": str(home / ".local/share"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "XDG_STATE_HOME": str(home / ".local/state"),
        "XDG_RUNTIME_DIR": str(home / "run"),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    for name in ("DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY"):
        if os.environ.get(name):
            env[name] = os.environ[name]
    return env


def _alive(pid: int) -> bool:
    try:
        return b"fl-bridge-helper" in Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return False


def _ppid(pid: int) -> int:
    for line in Path(f"/proc/{pid}/status").read_text(encoding="utf-8").splitlines():
        if line.startswith("PPid:"):
            return int(line.split()[1])
    return -1


def test_launcher_started_daemon_serves_the_shim_and_follows_wine(tmp_path: Path) -> None:
    wine = Path(os.environ.get("FL_WINE", "/usr/bin/wine"))
    if not wine.is_file():
        pytest.skip(f"no wine at {wine}")
    _build()
    home = tmp_path / "home"
    for sub in (".config", ".local/share", ".cache", ".local/state", "run"):
        (home / sub).mkdir(parents=True)
    (home / "run").chmod(0o700)
    env = _env(home)
    prefix = home / ".local/share/fork-linux/prefix"
    prefix.mkdir(parents=True, mode=0o700)
    server = _wineserver(wine)
    boot_env = {
        **env,
        "WINEPREFIX": str(prefix),
        "WINEDEBUG": "-all",
        "WINEDLLOVERRIDES": "mscoree,mshtml=;winemenubuilder.exe=d",
    }
    try:
        subprocess.run([str(wine), "wineboot", "-i"], env=boot_env, timeout=BOOT_TIMEOUT, check=True,
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run([str(server), "-w"], env=boot_env, timeout=BOOT_TIMEOUT, check=False)
        # The real host_shims step and the real setting, in the fake HOME.
        setup = (
            "import os, sys; sys.path.insert(0, sys.argv[1])\n"
            "from fork_linux.config import Config\nfrom fork_linux.paths import Paths\n"
            "from fork_linux import bridge\nfrom fork_linux.steps import integration\n"
            "from fixtures.setup_ctx import make_ctx\n"
            "ctx = make_ctx(env=dict(os.environ))\nintegration.run_shims(ctx)\n"
            "assert integration.verify_shims(ctx)\nctx.config.set('git', 'bridge', 'on')\n"
        )
        subprocess.run([sys.executable, "-c", setup, str(SRC)], env={**env, "PYTHONPATH": str(ROOT / "tests")},
                       check=True, timeout=60)
        repo = tmp_path / "repo"
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        config = tmp_path / "child.json"
        config.write_text(json.dumps({
            "src": str(SRC), "user": USER, "wine": str(wine), "wineserver": str(server),
            "script": SCRIPT, "cwd": str(repo),
        }), encoding="utf-8")
        child = subprocess.Popen([sys.executable, str(CHILD), str(config)], env=env,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        session_file = home / "run/fork-linux/session.json"
        deadline = time.monotonic() + 30
        while not session_file.exists() and time.monotonic() < deadline and child.poll() is None:
            time.sleep(0.05)
        session = json.loads(session_file.read_text(encoding="utf-8"))
        daemon = session["bridge"]["pid"]
        assert session["pid"] == child.pid
        # During the script's sleep: the daemon runs, child of the process that is now Wine.
        time.sleep(1.5)
        assert _alive(daemon)
        assert _ppid(daemon) == child.pid
        assert b"python" not in Path(f"/proc/{child.pid}/exe").resolve().name.encode()
        out, err = child.communicate(timeout=RUN_TIMEOUT)
        assert child.returncode == 0, err.decode(errors="replace")
        lines = out.decode().split()
        native = subprocess.run(["git", "--version"], capture_output=True, text=True, check=True).stdout.strip()
        text = out.decode()
        assert "Linux" in lines
        assert native in text
        assert str(repo) in text
        deadline = time.monotonic() + 5
        while _alive(daemon) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _alive(daemon), "the daemon must stop with the Wine process"
        assert not list((home / "run/fork-linux").glob("bridge-token-*"))
        log = next((home / ".local/state/fork-linux/logs").glob("bridge-*.log")).read_text(encoding="utf-8")
        assert "argv0=sh" in log
        assert "daemon exit" in log
    finally:
        subprocess.run([str(server), "-k"], env=boot_env, timeout=60, check=False)
