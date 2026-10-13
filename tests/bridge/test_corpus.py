"""Checks for the recorded Fork call corpus (``bridge/tests/corpus/*.jsonl``).

The corpus is what the real Fork 2.23.2 ran through the shims during spike B3
(``docs/spikes/B3-B4-real-fork-bridge.md``): record mode (forwarded to Fork's bundled
Git for Windows) and bridge mode (native git through ``fl-bridge-helper``). It is
committed, so it must stay sanitized (placeholders instead of user names and scratch
paths) and well formed. Bridge-mode rows also pin two translation facts the spike
found: what the daemon ran never carries a Windows path outside a wrapped editor
value, and Fork's interactive rebase editor is wrapped with the fl-winexec persona.
"""

from __future__ import annotations

import json
import re
from itertools import pairwise
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CORPUS_DIR = ROOT / "bridge" / "tests" / "corpus"
CORPORA = sorted(CORPUS_DIR.glob("*.jsonl"))

REQUIRED = {
    "session": str,
    "t_ms": int,
    "mode": str,
    "persona": str,
    "exe": str,
    "argv": list,
    "cwd": str,
    "env": dict,
    "stdio": dict,
    "exit": int,
    "duration_ms": int,
}

# Anything that would identify the machine or the person who recorded the corpus.
LEAKS = (
    re.compile(r"/home/"),
    re.compile(r"/tmp/"),
    re.compile(r"/var/tmp/"),
    re.compile(r"scratchpad", re.IGNORECASE),
    re.compile(r"claude-\d+"),
    re.compile(r"\b[0-9a-f]{64}\b"),
    re.compile(r"[\w.+-]+@(?!example\.invalid)[\w-]+\.[\w.-]+"),
)

WIN_ABS = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\\?\\)")


def _rows(path: Path) -> list[dict[str, object]]:
    rows = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        assert line.strip(), f"{path.name}:{n}: empty line"
        row = json.loads(line)
        assert isinstance(row, dict), f"{path.name}:{n}: not an object"
        rows.append(row)
    return rows


def test_corpus_present() -> None:
    assert CORPORA, f"no corpus under {CORPUS_DIR}"


@pytest.mark.parametrize("path", CORPORA, ids=lambda p: p.name)
def test_rows_well_formed(path: Path) -> None:
    rows = _rows(path)
    assert rows
    for n, row in enumerate(rows, 1):
        for key, typ in REQUIRED.items():
            assert isinstance(row.get(key), typ), (
                f"{path.name}:{n}: {key} missing or not {typ.__name__}"
            )
        assert row["mode"] in ("record", "bridge"), f"{path.name}:{n}: mode"
        assert row["persona"] in ("git", "bash", "sh"), f"{path.name}:{n}: persona"
        assert all(isinstance(a, str) for a in row["argv"]), f"{path.name}:{n}: argv"
        assert set(row["stdio"]) == {"stdin", "stdout", "stderr"}, (
            f"{path.name}:{n}: stdio"
        )
        if row["mode"] == "record":
            assert isinstance(row.get("target"), str), (
                f"{path.name}:{n}: record row without target"
            )
        else:
            assert isinstance(row.get("xargv"), list), (
                f"{path.name}:{n}: bridge row without xargv"
            )
            assert isinstance(row.get("subcmd"), str), (
                f"{path.name}:{n}: bridge row without subcmd"
            )


@pytest.mark.parametrize("path", CORPORA, ids=lambda p: p.name)
def test_sanitized(path: Path) -> None:
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        for leak in LEAKS:
            m = leak.search(line)
            assert m is None, f"{path.name}:{n}: unsanitized text {m.group(0)!r}"
        row = json.loads(line)
        env = row["env"]
        assert "FL_BRIDGE_TOKEN" not in env, f"{path.name}:{n}: token in env"
        for key, value in env.items():
            if key.startswith("LC_"):
                assert value == "<locale>", f"{path.name}:{n}: {key} not replaced"
        if "FORK_PROCESS_ID" in env:
            assert env["FORK_PROCESS_ID"] == "<fork-pid>", (
                f"{path.name}:{n}: FORK_PROCESS_ID"
            )


def _editor_value(arg: str) -> bool:
    key = arg.split("=", 1)[0].lower()
    return key in ("core.editor", "sequence.editor") and "fl-winexec" in arg


@pytest.mark.parametrize("path", CORPORA, ids=lambda p: p.name)
def test_bridge_rows_ran_unix_paths(path: Path) -> None:
    for n, row in enumerate(_rows(path), 1):
        if row["mode"] != "bridge":
            continue
        xargv = row["xargv"]
        assert isinstance(xargv, list)
        assert xargv[0] == "git", f"{path.name}:{n}: daemon ran {xargv[0]!r}"
        for prev, arg in pairwise(xargv):
            if prev == "-c" and _editor_value(arg):
                continue
            assert not WIN_ABS.match(arg), (
                f"{path.name}:{n}: Windows path reached git: {arg!r}"
            )


@pytest.mark.parametrize("path", CORPORA, ids=lambda p: p.name)
def test_interactive_rebase_editor_wrapped(path: Path) -> None:
    rebases = [
        r for r in _rows(path) if r["mode"] == "bridge" and r.get("subcmd") == "rebase"
    ]
    for row in rebases:
        xargv = row["xargv"]
        assert isinstance(xargv, list)
        editors = [
            a
            for p, a in pairwise(xargv)
            if p == "-c" and a.startswith("sequence.editor=")
        ]
        assert editors, "Fork's rebase -i carries -c sequence.editor"
        assert editors[0].startswith("sequence.editor='<libexec>/fl-winexec' '"), (
            editors[0]
        )
        assert editors[0].endswith("Fork.RI.exe'"), editors[0]
