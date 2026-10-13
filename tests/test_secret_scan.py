"""Tests for scripts/secret-scan.py (credential-shaped text in the tracked files and the history).

Every secret-shaped sample is assembled at run time, so this file never matches the scanner itself.
"""

from __future__ import annotations

import base64
import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "secret-scan.py"
DASHES = "-" * 5


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("secret_scan", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


scan = _load()


def _key_block(kind: str = "OPENSSH ", lines: int = 5, headers: str = "") -> str:
    body = base64.b64encode(bytes(range(256)) * 2).decode()
    rows = [body[i : i + 70] for i in range(0, 70 * lines, 70)]
    return (f"{DASHES}BEGIN {kind}PRIVATE KEY{DASHES}\n{headers}" + "\n".join(rows)
            + f"\n{DASHES}END {kind}PRIVATE KEY{DASHES}\n")


SAMPLES = {
    "private-key": _key_block(),
    "aws-access-key": "AK" + "IA" + "Q" * 16,
    "github-token": "gh" + "p_" + "a1" * 18,
    "github-fine-grained-token": "github" + "_pat_" + "A1_" * 21,
    "gitlab-token": "gl" + "pat-" + "x" * 20,
    "slack-token": "xo" + "xb-" + "1234567890-abc",
    "google-api-key": "AI" + "za" + "B" * 35,
    "sonar-token": "sq" + "p_" + "0f" * 20,
    "npm-token": "np" + "m_" + "Z" * 36,
    "pypi-token": "py" + "pi-AgEIcHlwaS5vcmc" + "Q" * 50,
}


@pytest.mark.parametrize("rule", sorted(SAMPLES))
def test_each_kind_of_secret_is_found_on_its_line(rule: str) -> None:
    assert scan.scan_bytes(f"first line\nvalue = {SAMPLES[rule]}\n".encode()) == [(2, rule)]


@pytest.mark.parametrize(
    "text",
    [
        # How code and tests recognise key files: a header (and a short body) is not a key.
        f"{DASHES}BEGIN OPENSSH PRIVATE KEY{DASHES}\n",
        f"{DASHES}BEGIN RSA PRIVATE KEY{DASHES}\nProc-Type: 4,ENCRYPTED\nMIIE\n{DASHES}END RSA PRIVATE KEY{DASHES}\n",
        _key_block(lines=3),
        "token=abcdef password=hunter2",
        "gh" + "p_short",
        "AK" + "IA" + "lowercase0000000",
    ],
)
def test_lookalikes_are_not_secrets(text: str) -> None:
    assert scan.scan_bytes(text.encode()) == []


def test_an_encrypted_pem_key_with_headers_is_found() -> None:
    block = _key_block("RSA ", headers="Proc-Type: 4,ENCRYPTED\nDEK-Info: AES-128-CBC,00FF\n\n")
    assert scan.scan_bytes(block.encode()) == [(1, "private-key")]


def test_binary_and_oversized_data_are_skipped() -> None:
    secret = SAMPLES["github-token"].encode()
    assert scan.scan_bytes(b"\0binary " + secret) == []
    assert scan.scan_bytes(secret + b" " * scan.MAX_BYTES) == []


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=T", "-c", "user.email=t@example.invalid", "-c", "init.defaultBranch=main", *args],
        cwd=str(cwd), capture_output=True, check=True,
        env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "HOME": str(cwd)},
    )


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, check=False)


def test_tracked_files_and_history(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    assert _run(str(repo)).returncode == 0, "an empty repository is clean"
    assert _run("--history", str(repo)).returncode == 0
    (repo / "clean.txt").write_text("nothing here\n", encoding="utf-8")
    (repo / "config.ini").write_text(f"[ci]\ntoken = {SAMPLES['sonar-token']}\n", encoding="utf-8")
    (repo / "image.bin").write_bytes(b"\0\1\2" + SAMPLES["aws-access-key"].encode())
    (repo / "link").symlink_to("config.ini")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "oops")
    (repo / "config.ini").write_text("[ci]\ntoken = from the environment\n", encoding="utf-8")
    _git(repo, "commit", "-qam", "remove it")
    (repo / "untracked.txt").write_text(SAMPLES["github-token"], encoding="utf-8")
    now = _run(str(repo))
    assert now.returncode == 0, now.stderr
    assert "no secrets found (tracked files)" in now.stdout
    (repo / "clean.txt").unlink()  # tracked, deleted in the work tree: skipped
    assert _run(str(repo)).returncode == 0
    past = _run("--history", str(repo))
    assert past.returncode == 1
    assert "config.ini (blob " in past.stderr
    assert ":2: [sonar-token]" in past.stderr
    assert SAMPLES["sonar-token"] not in past.stderr + past.stdout, "the secret itself is never printed"
    assert "1 finding(s)" in past.stderr
    (repo / "config.ini").write_text(f"[ci]\ntoken = {SAMPLES['sonar-token']}\n", encoding="utf-8")
    dirty = _run(str(repo))
    assert dirty.returncode == 1
    assert "config.ini:2: [sonar-token]" in dirty.stderr


def test_not_a_git_work_tree(tmp_path: Path) -> None:
    result = _run(str(tmp_path))
    assert result.returncode == 2
    assert "is not a git work tree" in result.stderr


def test_the_repository_has_no_secrets() -> None:
    """What the lint stage runs (scripts/ci-docker.sh run_secret_scan)."""
    result = _run(str(ROOT))
    assert result.returncode == 0, result.stderr
