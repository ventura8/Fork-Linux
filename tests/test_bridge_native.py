"""Native unit tests of the C bridge core (bridge/common) under ASan + UBSan.

Builds bridge/tests/unit/run.sh into a temporary directory with the host gcc and checks the
TAP stream: a plan, every check "ok", and a zero exit status. Skipped only when gcc (with
the address/undefined sanitizers) or bash is not installed.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RUN_SH = ROOT / "bridge" / "tests" / "unit" / "run.sh"
VECTORS = ROOT / "bridge" / "tests" / "vectors"


def _require_toolchain(tmp_path: Path) -> None:
    """Skip when gcc, bash or the sanitizer runtimes are missing (probe with a trivial program)."""
    gcc = shutil.which("gcc")
    if gcc is None or shutil.which("bash") is None:
        pytest.skip("gcc and bash are needed to build the native bridge tests")
    probe = tmp_path / "probe.c"
    probe.write_text("int main(void) { return 0; }\n", encoding="utf-8")
    built = subprocess.run(
        [
            gcc,
            "-std=c11",
            "-fsanitize=address,undefined",
            "-o",
            str(tmp_path / "probe"),
            str(probe),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if built.returncode != 0:
        pytest.skip(f"gcc cannot link ASan/UBSan here: {built.stderr.strip()[:200]}")


def _run_unit_tests(tmp_path: Path) -> subprocess.CompletedProcess[str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("ASAN_OPTIONS", "UBSAN_OPTIONS")
    }
    env["CC"] = "gcc"
    return subprocess.run(
        ["bash", str(RUN_SH), str(tmp_path / "build")],
        capture_output=True,
        text=True,
        env=env,
        timeout=900,
        check=False,
    )


def test_vectors_and_runner_exist() -> None:
    assert RUN_SH.is_file()
    for name in ("argv.tsv", "env.tsv", "out.tsv"):
        assert (VECTORS / name).is_file(), name


def test_vector_files_are_well_formed() -> None:
    """Every data line has the column count test_translate.c expects (TAB-separated)."""
    columns = {"argv.tsv": 6, "env.tsv": 4, "out.tsv": 6}
    for name, want in columns.items():
        rows = [
            line
            for line in (VECTORS / name).read_text(encoding="utf-8").splitlines()
            if line and not line.startswith("#")
        ]
        assert rows, name
        bad = [line for line in rows if len(line.split("\t")) != want]
        assert bad == [], f"{name}: rows without {want} columns: {bad[:3]}"
        names = [line.split("\t", 1)[0] for line in rows]
        assert len(names) == len(set(names)), f"{name}: duplicate vector names"


def test_bridge_common_unit_tests_pass(tmp_path: Path) -> None:
    _require_toolchain(tmp_path)
    proc = _run_unit_tests(tmp_path)
    out = proc.stdout
    assert proc.returncode == 0, (
        f"run.sh exited {proc.returncode}\n{out[-4000:]}\n{proc.stderr[-4000:]}"
    )
    plans = re.findall(r"^1\.\.(\d+)$", out, flags=re.MULTILINE)
    assert len(plans) == 1, "exactly one TAP plan line"
    planned = int(plans[0])
    oks = re.findall(r"^ok \d+ - ", out, flags=re.MULTILINE)
    not_oks = re.findall(r"^not ok \d+ - .*$", out, flags=re.MULTILINE)
    assert not_oks == []
    assert planned == len(oks) > 1000
    for marker in (
        "RFC 4231 case 7",
        "req mutations",
        "oversized records",
        "argv.tsv loaded",
        "env.tsv loaded",
        "out.tsv loaded",
    ):
        assert marker in out, marker
