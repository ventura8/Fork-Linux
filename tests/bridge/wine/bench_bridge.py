#!/usr/bin/env python3
"""Benchmark of the native-git bridge under Wine (not collected by pytest).

For each wine binary it boots a scratch prefix exactly like the Wine tier
(fl_winetier.WineBridge), creates a repository with 2000 files, and measures with
``fl-testdriver.exe bench`` (CreateProcessW + pipes + CREATE_NO_WINDOW, as Fork does):

* ``noop``: the cheapest GUI PE (Wine process start-up floor),
* ``bridge``: the shim at C:\\fork-linux\\gitInstance\\cmd\\git.exe -> daemon -> /usr/bin/git,
* ``bundled``: Fork's own Git for Windows (``--bundled-git``, a Unix path reached as Z:\\),

for ``rev-parse HEAD`` and ``status -z -uall``, plus the host git started natively.
Prints a Markdown table (docs/spikes/B5-bridge-wine-tier.md).

usage (inside a container only, e.g. scripts/e2e-docker.sh --image fork-linux-ci-bridge:26.04 shell --):
       (cd tests/bridge && python3 -m wine.bench_bridge --scratch DIR [--iterations 200]
           [--wine PATH ...] [--bundled-git PATH] [--noop PATH])
"""

from __future__ import annotations

import argparse
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path

# Run as a module (python3 -m wine.bench_bridge from tests/bridge) for the relative import.
from .fl_winetier import (
    ROOT,
    WineBridge,
    locate_build,
    native_git,
    win_path,
    wine_candidates,
    wine_label,
)

COMMANDS = {
    "rev-parse HEAD": ["rev-parse", "HEAD"],
    "status -z -uall": ["status", "-z", "-uall"],
}


def make_repo(bridge: WineBridge, nfiles: int) -> Path:
    """A repository with `nfiles` committed files, 20 of them modified, 20 untracked."""
    repo = bridge.root / "bench repo"
    repo.mkdir()
    assert native_git(["init", "-q", "-b", "main"], repo, bridge.home).returncode == 0
    for i in range(nfiles):
        d = repo / f"dir{i % 40:02d}"
        d.mkdir(exist_ok=True)
        (d / f"file{i:04d}.txt").write_text(f"line {i}\n", encoding="utf-8")
    for args in (["add", "-A"], ["commit", "-q", "-m", "bench"]):
        assert native_git(args, repo, bridge.home).returncode == 0
    for i in range(20):
        (repo / f"dir{i:02d}" / f"file{i:04d}.txt").write_text(
            "changed\n", encoding="utf-8"
        )
        (repo / f"untracked{i:02d}.txt").write_text("new\n", encoding="utf-8")
    return repo


def native_bench(repo: Path, home: Path, args: list[str], n: int) -> dict[str, float]:
    env = {
        "HOME": str(home),
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    ms = []
    for _ in range(n):
        t0 = time.perf_counter()
        subprocess.run(
            ["git", *args], cwd=repo, env=env, capture_output=True, check=True
        )
        ms.append((time.perf_counter() - t0) * 1000.0)
    ms.sort()
    return {
        "p50_ms": statistics.median(ms),
        "p95_ms": ms[int(0.95 * (n - 1))],
        "mean_ms": statistics.fmean(ms),
    }


def row(label: str, what: str, res: dict[str, object]) -> str:
    def f(key: str) -> str:
        val = res.get(key)
        return f"{float(val):.2f}" if isinstance(val, (int, float)) else "-"

    fails = res.get("failures", 0)
    return f"| {label} | {what} | {f('p50_ms')} | {f('p95_ms')} | {f('mean_ms')} | {fails} |"


# Wine runs only inside a container (AGENTS.md hard rule 18): /.dockerenv or /run/.containerenv.
CONTAINER_MARKERS = ("/.dockerenv", "/run/.containerenv")


def main() -> int:
    if not any(os.path.lexists(marker) for marker in CONTAINER_MARKERS):
        print(
            "bench_bridge: refusing to run Wine on the host; run it inside the bridge image, e.g. "
            "scripts/e2e-docker.sh --image fork-linux-ci-bridge:26.04 shell -- sh -c "
            "'cd tests/bridge && python3 -m wine.bench_bridge --scratch /e2e/bench'",
            file=sys.stderr,
        )
        return 2
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--scratch",
        required=True,
        type=Path,
        help="empty scratch directory (prefixes, fake HOME)",
    )
    ap.add_argument("--iterations", type=int, default=200)
    ap.add_argument("--files", type=int, default=2000)
    ap.add_argument("--wine", action="append", default=[])
    ap.add_argument(
        "--bundled-git",
        default="",
        help="Fork's bundled git.exe as a Unix path, for example <prefix>/drive_c/users/"
        "<user>/AppData/Local/Fork/gitInstance/2.50.1/cmd/git.exe (omitted: no bundled rows)",
    )
    ap.add_argument(
        "--noop", default=str(ROOT / "bridge" / "spike" / "out" / "noop-gui.exe")
    )
    opts = ap.parse_args()

    build, why = locate_build()
    if build is None:
        print(why, file=sys.stderr)
        return 2
    wines = opts.wine or wine_candidates()
    opts.scratch.mkdir(parents=True, exist_ok=True)
    n = opts.iterations
    print("| Wine | variant / command | p50 ms | p95 ms | mean ms | failures |")
    print("|---|---|---|---|---|---|")
    for wine in wines:
        label = wine_label(wine)
        b = WineBridge(wine, build, opts.scratch / f"bench-{label}")
        b.start()
        try:
            repo = make_repo(b, opts.files)
            cwd = win_path(repo)
            # Warm-up: page cache, wineserver, daemon.
            b.bench(10, b.git, ["rev-parse", "HEAD"], cwd=cwd)
            if Path(opts.noop).is_file():
                print(
                    row(label, "noop-gui.exe", b.bench(n, opts.noop, [], cwd=cwd)),
                    flush=True,
                )
            for what, args in COMMANDS.items():
                print(
                    row(label, f"bridge `{what}`", b.bench(n, b.git, args, cwd=cwd)),
                    flush=True,
                )
                if opts.bundled_git and Path(opts.bundled_git).is_file():
                    res = b.bench(n, win_path(opts.bundled_git), args, cwd=cwd)
                    print(
                        row(label, f"bundled Git for Windows `{what}`", res), flush=True
                    )
                print(
                    row(
                        label,
                        f"host git, native `{what}`",
                        native_bench(repo, b.home, args, n),
                    ),
                    flush=True,
                )
        finally:
            b.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
