"""Every script that starts Wine, winetricks or Fork refuses to do so on the host (AGENTS.md
hard rule 18): the QA driver, the git probe, the spikes, the container half of ci-docker.sh and
the Wine tier of ci-c-coverage.sh. Each carries the container check before it does anything;
on the host it exits at once and creates nothing."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from fixtures import real_tier

REPO = Path(__file__).resolve().parents[1]
CHECK = "/.dockerenv"
# script -> (args, exit status on the host, the line the guard must come before)
GUARDED = {
    "scripts/qa/fl-qa.sh": (["init"], 2, 'qa_env() {'),
    "scripts/qa/fl-qa-gitprobe.sh": ([], 2, 'PREFIX="$R/home'),
    "scripts/spike/fl-spike.sh": (["s1"], 2, 'ROOT="$(mkdir -p'),
    "scripts/ci-docker.sh": (["--inside"], 1, "\trun_inside\n\texit 0"),
    "scripts/ci-c-coverage.sh": (["--no-docker"], 1, "  inside\n  exit 0"),
}


@pytest.mark.parametrize("rel", sorted(GUARDED))
def test_the_container_check_comes_first(rel: str) -> None:
    text = (REPO / rel).read_text(encoding="utf-8")
    first_action = GUARDED[rel][2]
    assert CHECK in text and "/run/.containerenv" in text
    assert text.index(CHECK) < text.index(first_action)
    assert "hard rule 18" in text


@pytest.mark.parametrize("rel", sorted(GUARDED))
def test_on_the_host_the_script_refuses_and_creates_nothing(rel: str, tmp_path: Path) -> None:
    args, status, _ = GUARDED[rel]
    env = {
        **{k: v for k, v in os.environ.items() if not k.startswith(("FL_", "WINE"))},
        "HOME": str(tmp_path / "home"),
        "FL_QA_ROOT": str(tmp_path / "qa"),
        "FL_SPIKE_ROOT": str(tmp_path / "spike"),
        "FL_CI_STAGE": "bridge",
        "TMPDIR": str(tmp_path / "tmp"),
    }
    if real_tier.in_container():
        # Inside a container the scripts are allowed to run; only the static check above applies,
        # and nothing is started here.
        assert real_tier.refusal({"FL_E2E_FORK": "1"}) is None
        return
    result = subprocess.run([str(REPO / rel), *args], cwd=str(tmp_path), env=env, capture_output=True,
                            text=True, timeout=60, check=False)
    assert result.returncode == status, result.stdout + result.stderr
    assert "container" in result.stderr or "e2e-docker.sh" in result.stderr
    assert list(tmp_path.iterdir()) == []


def test_the_qa_driver_never_derives_its_root_from_home() -> None:
    text = (REPO / "scripts" / "qa" / "fl-qa.sh").read_text(encoding="utf-8")
    assert 'R="${FL_QA_ROOT:-/e2e/qa}"' in text
    assert "REAL_HOME" not in text
    assert "$HOME/.cache" not in text
    probe = (REPO / "scripts" / "qa" / "fl-qa-gitprobe.sh").read_text(encoding="utf-8")
    assert 'R="${FL_QA_ROOT:-/e2e/qa}"' in probe
