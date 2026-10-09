"""Wine tier of the native-git bridge: the real shims, under Wine, against the real daemon.

For every wine binary (``FL_TEST_WINE``, ``:``-separated; default ``/usr/bin/wine``;
add the pinned staging build there) a scratch prefix is booted, the shims are installed at the
production layout (``C:\\fork-linux\\gitInstance\\cmd\\git.exe`` ...), the native
``fl-bridge-helper --daemon`` is started with a token file, and every program is spawned
by ``fl-testdriver.exe`` exactly like Fork's .NET ``Process.Start`` does. Results per
Wine build are recorded in docs/spikes/B5-bridge-wine-tier.md.

Gated: module-level skip unless ``FL_REAL_WINE=1``. Build first: ``scripts/build-bridge.sh``.
Run: ``FL_REAL_WINE=1 python3 -m pytest tests/bridge/wine --basetemp=<scratch dir>``.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import subprocess
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

if os.environ.get("FL_REAL_WINE") != "1":
    pytest.skip("requires FL_REAL_WINE=1", allow_module_level=True)

from .fl_winetier import (
    DriverResult,
    WineBridge,
    fwd_path,
    locate_build,
    native_git,
    wait_until,
    win_path,
    wine_candidates,
    wine_label,
)

WINES = wine_candidates()


@pytest.fixture(
    scope="module",
    params=WINES or ["<none>"],
    ids=[wine_label(w) for w in WINES] or ["no-wine"],
)
def bridge(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[WineBridge]:
    wine = str(request.param)
    if wine == "<none>":
        pytest.skip("no wine binary found (set FL_TEST_WINE)")
    build, why = locate_build()
    if build is None:
        pytest.skip(why)
    b = WineBridge(wine, build, tmp_path_factory.mktemp(f"bridge-{wine_label(wine)}"))
    try:
        b.start()
        yield b
    finally:
        b.stop()


@pytest.fixture(scope="module")
def repo(bridge: WineBridge) -> Path:
    """A repository whose path has a space and a non-ASCII letter, with two commits."""
    path = bridge.root / "repo ü space"
    path.mkdir()
    for args in (
        ["init", "-q", "-b", "main"],
        ["commit", "-q", "--allow-empty", "-m", "first"],
    ):
        assert native_git(args, path, bridge.home).returncode == 0
    (path / "file ü.txt").write_text("hello\n", encoding="utf-8")
    for args in (["add", "-A"], ["commit", "-q", "-m", "second ü"]):
        assert native_git(args, path, bridge.home).returncode == 0
    return path


def git(bridge: WineBridge, *args: str, **kw: Any) -> DriverResult:
    return bridge.drive(bridge.git, list(args), **kw)


def native_out(repo: Path, bridge: WineBridge, *args: str) -> bytes:
    proc = native_git(list(args), repo, bridge.home)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


# --------------------------------------------------------------------------
# basics
# --------------------------------------------------------------------------


def test_git_version_matches_host(bridge: WineBridge) -> None:
    r = git(bridge, "--version")
    host = subprocess.run(["git", "--version"], capture_output=True, check=True).stdout
    assert r.rc == 0, r.err
    assert r.stdout == host


def test_log_with_space_and_umlaut_path_matches_native(
    bridge: WineBridge, repo: Path
) -> None:
    fmt = "--format=%H%n%s%n%an <%ae>"
    r = git(bridge, "-C", win_path(repo), "log", "-1", fmt)
    assert r.rc == 0, r.err
    assert r.stdout == native_out(repo, bridge, "log", "-1", fmt)


def test_rev_parse_show_toplevel_is_z_forward_slash_form(
    bridge: WineBridge, repo: Path
) -> None:
    r = git(bridge, "-C", win_path(repo), "rev-parse", "--show-toplevel")
    assert r.rc == 0, r.err
    assert r.out == fwd_path(repo) + "\n"
    # Same through the working directory instead of -C (how Fork usually calls git).
    r = git(
        bridge, "rev-parse", "--show-toplevel", "--absolute-git-dir", cwd=win_path(repo)
    )
    assert r.rc == 0, r.err
    assert r.out == f"{fwd_path(repo)}\n{fwd_path(repo)}/.git\n"


@pytest.mark.parametrize(
    ("args", "code"),
    [
        (["rev-parse", "HEAD"], 0),
        (["config", "--get", "no.such.key"], 1),
        (["rev-parse", "--verify", "--quiet", "no-such-ref^{commit}"], 1),
        (["rev-parse", "--verify", "no-such-ref"], 128),
        (["-c", "alias.x=!exit 42", "x"], 42),
    ],
    ids=["0", "1-config", "1-verify", "128", "42"],
)
def test_exit_codes(bridge: WineBridge, repo: Path, args: list[str], code: int) -> None:
    r = git(bridge, *args, cwd=win_path(repo))
    assert r.rc == code, r.err


def test_stdout_and_stderr_stay_separate(bridge: WineBridge, repo: Path) -> None:
    r = git(
        bridge,
        "-c",
        "alias.x=!f() { echo out-1; echo err-1 >&2; echo out-2; exit 3; }; f",
        "x",
        cwd=win_path(repo),
    )
    assert r.rc == 3
    assert r.out == "out-1\nout-2\n"
    assert r.err == "err-1\n"
    r = git(bridge, "rev-parse", "--verify", "no-such-ref", cwd=win_path(repo))
    assert r.rc == 128
    assert r.stdout == b""
    assert "fatal" in r.err


# --------------------------------------------------------------------------
# data volume
# --------------------------------------------------------------------------


def test_stdin_hash_object_10mb_matches_native(bridge: WineBridge, repo: Path) -> None:
    data = secrets.token_bytes(10 * 1024 * 1024)
    src = bridge.root / "stdin-10mb.bin"
    src.write_bytes(data)
    r = git(bridge, "hash-object", "--stdin", cwd=win_path(repo), stdin_file=src)
    assert r.rc == 0, r.err
    src.unlink()
    native = native_git(["hash-object", "--stdin"], repo, bridge.home, stdin=data)
    assert r.stdout == native.stdout
    assert len(r.out.strip()) == 40


def test_cat_file_100mb_blob_sha256_matches(bridge: WineBridge, repo: Path) -> None:
    data = os.urandom(1024 * 1024) * 100
    src = bridge.root / "blob-100mb.bin"
    src.write_bytes(data)
    oid = native_out(repo, bridge, "hash-object", "-w", str(src)).decode().strip()
    src.unlink()  # scratch space is tight (tmpfs); the blob lives on in the repo
    r = git(bridge, "cat-file", "blob", oid, cwd=win_path(repo), max_capture=0)
    assert r.rc == 0, r.err
    assert r.stdout_len == len(data)
    assert r.stdout_sha256 == hashlib.sha256(data).hexdigest()


def test_twenty_parallel_calls_keep_their_output_and_code(
    bridge: WineBridge, repo: Path
) -> None:
    alias = 'alias.x=!f() { echo "job-$1"; exit "$2"; }; f'

    def one(i: int) -> tuple[int, DriverResult]:
        return i, git(bridge, "-c", alias, "x", str(i), str(i % 7), cwd=win_path(repo))

    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(one, range(20)))
    bad = [
        (i, r.rc, r.out, r.err)
        for i, r in results
        if r.rc != i % 7 or r.out != f"job-{i}\n"
    ]
    assert bad == []


# --------------------------------------------------------------------------
# lifecycle and authentication
# --------------------------------------------------------------------------


def _pids_of(marker: str) -> list[int]:
    pids = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes()
            state = (entry / "stat").read_bytes().rsplit(b")", 1)[1].split()[0]
        except OSError:
            continue
        if (
            marker.encode() in cmdline
            and b"\0" in cmdline
            and state != b"Z"
            and b"sleep" in cmdline
        ):
            pids.append(int(entry.name))
    return pids


def test_terminate_process_kills_the_native_child(
    bridge: WineBridge, repo: Path
) -> None:
    marker = f"3{secrets.randbelow(10**6)}.5"  # a unique `sleep` argument
    box: dict[str, DriverResult] = {}

    def run() -> None:
        box["r"] = git(
            bridge,
            "-c",
            f"alias.x=!sleep {marker}",
            "x",
            cwd=win_path(repo),
            kill_after_ms=4000,
        )

    t = threading.Thread(target=run)
    t.start()
    assert wait_until(lambda: bool(_pids_of(marker)), 30), (
        "the native sleep never started"
    )
    t.join(120)
    assert not t.is_alive()
    killed_at = time.monotonic()
    assert box["r"].killed
    gone = wait_until(lambda: not _pids_of(marker), 3.0)
    assert gone, (
        f"native child still alive {time.monotonic() - killed_at:.2f}s after TerminateProcess"
    )


def test_wrong_token_fails_closed_with_125(bridge: WineBridge, repo: Path) -> None:
    r = git(
        bridge,
        "rev-parse",
        "HEAD",
        cwd=win_path(repo),
        env={"FL_BRIDGE_TOKEN": secrets.token_hex(32)},
    )
    assert r.rc == 125
    assert r.stdout == b""
    # The rejected client does not disturb the daemon: the right token still works.
    r = git(bridge, "rev-parse", "HEAD", cwd=win_path(repo))
    assert r.rc == 0, r.err
    assert r.stdout == native_out(repo, bridge, "rev-parse", "HEAD")


# --------------------------------------------------------------------------
# translation
# --------------------------------------------------------------------------


def test_worktree_list_is_translated(bridge: WineBridge, repo: Path) -> None:
    wt = bridge.root / "wt ü"
    if not wt.exists():
        assert (
            native_git(
                ["worktree", "add", "-q", "-b", "side", str(wt)], repo, bridge.home
            ).returncode
            == 0
        )
    r = git(bridge, "-C", win_path(repo), "worktree", "list", "--porcelain")
    assert r.rc == 0, r.err
    trees = [
        line.split(" ", 1)[1]
        for line in r.out.splitlines()
        if line.startswith("worktree ")
    ]
    assert trees == [fwd_path(repo), fwd_path(wt)]
    r = git(bridge, "-C", win_path(repo), "worktree", "list", "--porcelain", "-z")
    assert r.rc == 0, r.err
    recs = [
        rec[len("worktree ") :]
        for rec in r.out.split("\0")
        if rec.startswith("worktree ")
    ]
    assert recs == [fwd_path(repo), fwd_path(wt)]
    # Plain `worktree list` with ASCII-only paths (no C quoting).
    plain = bridge.root / "wt-plain"
    if not plain.exists():
        assert (
            native_git(
                ["worktree", "add", "-q", "-b", "plain", str(plain)], repo, bridge.home
            ).returncode
            == 0
        )
    r = git(bridge, "-C", win_path(plain), "worktree", "list")
    assert r.rc == 0, r.err
    firsts = [line.split(" ", 1)[0] for line in r.out.splitlines()]
    assert fwd_path(plain) in firsts


def test_worktree_list_c_quoted_paths_are_translated(
    bridge: WineBridge, repo: Path
) -> None:
    """Non-porcelain `worktree list` C-quotes non-ASCII paths (core.quotePath); Git for
    Windows prints them as "Z:/..." inside the same quotes. The whole output must be the
    native output with every (quoted or bare) absolute path given its Z: form."""
    r = git(bridge, "-C", win_path(repo), "worktree", "list")
    assert r.rc == 0, r.err
    native = native_out(repo, bridge, "worktree", "list").decode("utf-8")
    want = [
        ('"Z:' + line[1:]) if line.startswith('"/') else ("Z:" + line)
        for line in native.splitlines()
    ]
    assert native.splitlines()[0].startswith('"/'), "the repo path should be C-quoted"
    assert r.out.splitlines() == want
    assert r.out.endswith("\n")


def test_git_dir_given_as_windows_path(bridge: WineBridge, repo: Path) -> None:
    gitdir = win_path(repo / ".git")
    r = git(
        bridge,
        "log",
        "-1",
        "--format=%H",
        cwd=win_path(bridge.home),
        env={"GIT_DIR": gitdir},
    )
    assert r.rc == 0, r.err
    assert r.stdout == native_out(repo, bridge, "log", "-1", "--format=%H")
    r = git(
        bridge,
        "rev-parse",
        "--absolute-git-dir",
        cwd=win_path(bridge.home),
        env={"GIT_DIR": gitdir},
    )
    assert r.rc == 0, r.err
    assert r.out == fwd_path(repo / ".git") + "\n"


@pytest.mark.parametrize(
    ("value", "exe"),
    [
        (
            '"C:\\Program Files\\Fork\\Fork.RI.exe"',
            "C:\\Program Files\\Fork\\Fork.RI.exe",
        ),
        (
            "C:/Users/me/AppData/Local/Fork/Fork.RI.exe",
            "C:/Users/me/AppData/Local/Fork/Fork.RI.exe",
        ),
    ],
    ids=["quoted-backslashes", "bare-forward-slashes"],
)
def test_core_editor_becomes_a_winexec_invocation(
    bridge: WineBridge, repo: Path, value: str, exe: str
) -> None:
    """A Windows editor (git runs core.editor through sh, so a path with spaces is quoted,
    as with Git for Windows) is wrapped as '<fl-winexec>' '<exe>'."""
    want = f"'{bridge.winexec}' '{exe}'\n"
    r = git(
        bridge, "-c", f"core.editor={value}", "var", "GIT_EDITOR", cwd=win_path(repo)
    )
    assert r.rc == 0, r.err
    assert r.out == want
    # The rewritten value is what git's own children see (what `git rebase -i` would run).
    r = git(
        bridge,
        "-c",
        f"core.editor={value}",
        "-c",
        "alias.x=!git config core.editor",
        "x",
        cwd=win_path(repo),
    )
    assert r.rc == 0, r.err
    assert r.out == want
    r = git(bridge, "var", "GIT_EDITOR", cwd=win_path(repo), env={"GIT_EDITOR": value})
    assert r.rc == 0, r.err
    assert r.out == want


# --------------------------------------------------------------------------
# fl-launch.exe and the shell personas
# --------------------------------------------------------------------------


def test_fl_launch_run_waits_and_returns_the_status(bridge: WineBridge) -> None:
    r = bridge.drive(bridge.launch, ["run", "--", "/bin/echo", "hi"])
    assert r.rc == 0, r.err
    assert r.out == "hi\n"
    r = bridge.drive(bridge.launch, ["run", "--", "/bin/sh", "-c", "sleep 1; exit 5"])
    assert r.rc == 5, r.err
    assert r.ms >= 1000


@pytest.mark.parametrize(
    "rel", ["bin/bash.exe", "bin/sh.exe", "usr/bin/bash.exe", "usr/bin/sh.exe"]
)
def test_shell_persona_spawns_children(bridge: WineBridge, rel: str) -> None:
    """The hook-spawn fix: bundled MSYS bash cannot fork under Wine, the bridged one can."""
    r = bridge.drive(
        bridge.shim(rel), ["-c", "echo ok; ls / >/dev/null && echo child-ok"]
    )
    assert r.rc == 0, r.err
    assert r.out == "ok\nchild-ok\n"
