"""Tests for scripts/ci-e2e-de.sh (the desktop E2E runner) and its use of scripts/lib-e2e-docker.sh.

The script runs for real, from a copy inside a throwaway git repository with a linked
worktree, with fake ``docker`` and ``flock`` first on PATH that only record what they were
asked to do (the fake ``docker run`` of the release image drops a tarball into the scratch
root). Checked: the same scratch-root refusals and mounts as scripts/e2e-docker.sh, that the
integration tier holds no E2E slot and sees no seed while ``--fork`` does both, that the
release tarball is built from the read-only tree into the scratch root, the image tags read
from the Dockerfiles, the logs-only copy and that lock descriptors never reach a container.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from test_e2e_docker import FAKE_FLOCK, _git, _opts

REPO = Path(__file__).resolve().parents[1]
COPIED = (
    "scripts/ci-e2e-de.sh",
    "scripts/lib-e2e-docker.sh",
    "docker/Dockerfile.e2e.de.xfce",
    "docker/Dockerfile.release",
)
IMAGE = "fork-linux-e2e-de-xfce:26.04"
RELEASE_IMAGE = "fork-linux-ci-release:26.04"
MARKER = ".fl-e2e-scratch"

FAKE_DOCKER = """#!{python}
import json, os, sys
fds = {{fd: os.path.exists(f"/proc/self/fd/{{fd}}") for fd in (8, 9)}}
args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps({{"tool": "docker", "args": args, "fds": fds}}) + "\\n")
if args[:2] == ["image", "inspect"]:
    print(os.environ.get("FAKE_IMAGE_DIGEST", ""))
    sys.exit(0)
if args[:1] == ["run"] and args[-2:] == ["cat", "/etc/passwd"]:
    print("root:x:0:0:root:/root:/bin/bash")
    if os.environ.get("FAKE_UID_KNOWN") == "1":
        print(f"ubuntu:x:{{os.getuid()}}:{{os.getgid()}}:Ubuntu:/home/ubuntu:/bin/bash")
    sys.exit(0)
if args[:1] == ["run"]:
    e2e = next(args[i + 1].split(":")[0] for i, arg in enumerate(args) if arg == "--volume" and ":/e2e:" in args[i + 1])
    if "./scripts/release-portable.sh" in args:
        if os.environ.get("FAKE_TARBALL_RC", "0") != "0":
            print("meson: error")
            sys.exit(int(os.environ["FAKE_TARBALL_RC"]))
        with open(os.path.join(e2e, "dist", "fork-linux-1.0.0-x86_64.tar.gz"), "wb") as handle:
            handle.write(b"tarball")
        sys.exit(0)
    for rel, text in (("root/logs/xvfb.log", "x"), ("root/shots/03-main.png", "PNG"),
                      ("root/home/.local/share/fork-linux/prefix/drive_c/users/u/fork.log", "secret")):
        path = os.path.join(e2e, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
    print("fake desktop tier output")
    sys.exit(int(os.environ.get("FAKE_RUN_RC", "0")))
sys.exit(0)
"""


class DeBox:
    """A throwaway repository + linked worktree holding the runner, fakes and a fake home."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.main = tmp / "repo"
        self.home = tmp / "home"
        self.bin = tmp / "bin"
        self.log = tmp / "calls.jsonl"
        self.scratch = tmp / "var" / "scratch"
        self.locks = tmp / "var" / "locks"
        self.seed = tmp / "var" / "seed"
        for directory in (self.main, self.home, self.bin, self.seed / "winetricks"):
            directory.mkdir(parents=True, exist_ok=True)
        for rel in COPIED:
            target = self.main / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(REPO / rel, target)
        _git(self.main, "init", "-q")
        _git(self.main, "add", "-A")
        _git(self.main, "commit", "-qm", "scripts")
        self.wt = tmp / "wt"
        _git(self.main, "worktree", "add", "-q", str(self.wt), "-b", "topic")
        self.git_common = Path(_git(self.wt, "rev-parse", "--path-format=absolute", "--git-common-dir"))
        (self.bin / "docker").write_text(FAKE_DOCKER.format(python=sys.executable), encoding="utf-8")
        (self.bin / "flock").write_text(FAKE_FLOCK, encoding="utf-8")
        for tool in ("docker", "flock"):
            (self.bin / tool).chmod(0o755)

    def run(self, *args: str, **extra: str) -> subprocess.CompletedProcess[str]:
        env = {
            key: value for key, value in os.environ.items()
            if not key.startswith(("FL_E2E_", "FL_REAL_", "FAKE_", "BUILDX_")) and key != "TMPDIR"
        }
        env.update(
            {
                "PATH": f"{self.bin}:{os.environ.get('PATH', '/usr/bin:/bin')}",
                "HOME": str(self.home),
                "FAKE_LOG": str(self.log),
                "FL_E2E_SCRATCH": str(self.scratch),
                "FL_E2E_LOCK_DIR": str(self.locks),
                "FL_E2E_SEED_DIR": str(self.seed),
                **extra,
            }
        )
        return subprocess.run(
            [str(self.wt / "scripts" / "ci-e2e-de.sh"), *args],
            cwd=str(self.tmp), env=env, capture_output=True, text=True, timeout=60, check=False,
        )

    def calls(self, tool: str) -> list[dict[str, Any]]:
        if not self.log.exists():
            return []
        rows = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]
        return [row for row in rows if row["tool"] == tool]

    def docker(self, verb: str) -> list[list[str]]:
        return [row["args"] for row in self.calls("docker") if row["args"][:1] == [verb]]

    def tier_run(self) -> list[str]:
        """The ``docker run`` of the desktop image that runs pytest."""
        runs = [argv for argv in self.docker("run") if IMAGE in argv and "pytest" in argv]
        assert len(runs) == 1, self.docker("run")
        return runs[0]


@pytest.fixture
def box(tmp_path: Path) -> DeBox:
    return DeBox(tmp_path)


def _after(argv: list[str], image: str) -> list[str]:
    return argv[argv.index(image) + 1:]


def test_integration_tier_builds_the_tarball_and_runs_without_wine(box: DeBox) -> None:
    result = box.run("xfce", "--", "-k", "theme", FAKE_RUN_RC="1")
    assert result.returncode == 1, result.stderr
    # Both images are built from their Dockerfiles, tagged as the Dockerfiles declare, with the default builder.
    built = {argv[argv.index("-t") + 1]: argv[argv.index("-f") + 1] for argv in box.docker("build")}
    assert built == {
        IMAGE: str(box.wt.resolve() / "docker" / "Dockerfile.e2e.de.xfce"),
        RELEASE_IMAGE: str(box.wt.resolve() / "docker" / "Dockerfile.release"),
    }
    tarball_run, passwd_run, tier = box.docker("run")
    assert _after(passwd_run, IMAGE) == ["cat", "/etc/passwd"]
    assert _after(tarball_run, RELEASE_IMAGE) == ["./scripts/release-portable.sh", "tarball"]
    assert "FL_ARTIFACTS_DIR=/e2e/dist" in _opts(tarball_run, "--env")
    assert f"{box.wt.resolve()}:/src:ro" in _opts(tarball_run, "--volume")
    assert _after(tier, IMAGE) == [
        "python3", "-m", "pytest", "-p", "no:cacheprovider", "-o", "addopts=", "-v", "--basetemp=/e2e/pytest",
        "tests/e2e_de/test_de_integration.py", "-k", "theme",
    ]
    opts = tier[: tier.index(IMAGE)]
    assert sorted(_opts(opts, "--volume")) == sorted(
        [
            f"{box.wt.resolve()}:/src:ro",
            f"{box.git_common}:{box.git_common}:ro",
            f"{box.scratch}:/e2e:rw",
            f"{box.scratch}/passwd:/etc/passwd:ro",
        ]
    )
    # The image does not know this uid: its passwd plus one line for it (dbus-daemon needs one).
    passwd = (box.scratch / "passwd").read_text(encoding="utf-8").splitlines()
    assert passwd[0] == "root:x:0:0:root:/root:/bin/bash"
    assert passwd[1].split(":")[2:4] == [str(os.getuid()), str(os.getgid())]
    assert passwd[1].endswith(":/e2e/home:/bin/bash")
    envs = _opts(opts, "--env")
    assert {"FL_E2E_DE=xfce", "FL_E2E_ROOT=/e2e/root", "FL_E2E_DE_TARBALL=/e2e/dist/fork-linux-1.0.0-x86_64.tar.gz",
            "HOME=/e2e/home"} <= set(envs)
    assert not any(value.startswith(("FL_E2E_DE_FORK", "FL_E2E_SEED", "DISPLAY")) for value in envs)
    assert _opts(opts, "--label") == ["fl-e2e=de-xfce"]
    assert _opts(opts, "--user") == [f"{os.getuid()}:{os.getgid()}"]
    assert "--privileged" not in tier and "--rm" in opts and "--init" in opts
    # No E2E slot for the Wine-free tier, only the name lock.
    assert [row["lock"] for row in box.calls("flock")] == ["name-de-xfce"]
    for row in box.calls("docker"):
        assert row["fds"] == {"8": False, "9": False}
    assert stat.S_IMODE(box.scratch.stat().st_mode) == 0o700
    assert (box.scratch / MARKER).is_file()
    logs = box.wt / "logs" / "e2e-de"
    assert "fake desktop tier output" in (logs / "de-xfce.log").read_text(encoding="utf-8")
    copied = sorted(str(path.relative_to(logs / "de-xfce")) for path in (logs / "de-xfce").rglob("*") if path.is_file())
    assert copied == ["root/logs/xvfb.log", "tarball.log"]


def test_fork_tier_holds_a_slot_and_mounts_the_seed(box: DeBox) -> None:
    result = box.run("--fork", "xfce", FL_E2E_FORK_VERSION="2.23.2", FL_E2E_SLOTS="2")
    assert result.returncode == 0, result.stderr
    tier = box.tier_run()
    assert _after(tier, IMAGE)[-1] == "tests/e2e_de/test_de_fork.py"
    opts = tier[: tier.index(IMAGE)]
    assert f"{box.seed}:/seed:ro" in _opts(opts, "--volume")
    assert {"FL_E2E_DE_FORK=1", "FL_E2E_SEED=/seed", "FL_E2E_WINETRICKS_CACHE=/seed/winetricks",
            "FL_E2E_FORK_VERSION=2.23.2"} <= set(_opts(opts, "--env"))
    assert _opts(opts, "--label") == ["fl-e2e=de-xfce-fork"]
    assert [row["lock"] for row in box.calls("flock")] == ["name-de-xfce-fork", "slot-1"]
    assert "holding E2E slot 1/2" in result.stdout
    assert (box.wt / "logs" / "e2e-de" / "de-xfce-fork.log").is_file()


def test_fork_tier_without_a_seed_downloads(box: DeBox) -> None:
    shutil.rmtree(box.seed)
    result = box.run("xfce", "--fork")
    assert result.returncode == 0
    assert "no seed at" in result.stdout
    opts = box.tier_run()
    assert not any(volume.endswith(":/seed:ro") for volume in _opts(opts, "--volume"))


def test_a_given_tarball_is_installed_instead_of_building_one(box: DeBox) -> None:
    given = box.tmp / "dist" / "fork-linux-9.9.9-x86_64.tar.gz"
    given.parent.mkdir()
    given.write_bytes(b"tarball")
    assert box.run("xfce", FL_E2E_DE_TARBALL=str(given)).returncode == 0
    assert [argv for argv in box.docker("run") if RELEASE_IMAGE in argv] == []
    assert "FL_E2E_DE_TARBALL=/e2e/dist/fork-linux-9.9.9-x86_64.tar.gz" in _opts(box.tier_run(), "--env")
    assert (box.scratch / "dist" / given.name).read_bytes() == b"tarball"


@pytest.mark.parametrize("value", ["{tmp}/missing.tar.gz", "{tmp}/home"])
def test_a_bad_given_tarball_is_refused(box: DeBox, value: str) -> None:
    result = box.run("xfce", FL_E2E_DE_TARBALL=value.format(tmp=box.tmp))
    assert result.returncode == 1
    assert "FL_E2E_DE_TARBALL" in result.stderr
    assert box.docker("run") == []


def test_a_failed_tarball_build_stops_before_the_tier(box: DeBox) -> None:
    result = box.run("xfce", FAKE_TARBALL_RC="1")
    assert result.returncode == 1
    assert "release tarball did not build" in result.stderr
    assert [argv for argv in box.docker("run") if IMAGE in argv and "pytest" in argv] == []
    assert "meson: error" in (box.wt / "logs" / "e2e-de" / "de-xfce" / "tarball.log").read_text(encoding="utf-8")


def test_images_are_reused_when_their_dockerfile_did_not_change(box: DeBox) -> None:
    digests = {
        rel: hashlib.sha256((box.wt / rel).read_bytes()).hexdigest()
        for rel in ("docker/Dockerfile.e2e.de.xfce", "docker/Dockerfile.release")
    }
    # The fake reports one digest for every image: only the image whose Dockerfile has it is reused.
    assert box.run("xfce", FAKE_IMAGE_DIGEST=digests["docker/Dockerfile.e2e.de.xfce"]).returncode == 0
    assert [argv[argv.index("-t") + 1] for argv in box.docker("build")] == [RELEASE_IMAGE]


@pytest.mark.parametrize(
    "args",
    [[], ["gnome-nope"], ["XFCE"], ["xfce", "kde"], ["--bogus", "xfce"], ["--name"], ["--name", "seed", "xfce"],
     ["--name", "../x", "xfce"]],
)
def test_usage_errors(box: DeBox, args: list[str]) -> None:
    result = box.run(*args)
    assert result.returncode == 2, result.stdout + result.stderr
    assert box.calls("docker") == []


def test_help(box: DeBox) -> None:
    result = box.run("--help")
    assert result.returncode == 0
    assert "DESKTOP [--fork]" in result.stdout


@pytest.mark.parametrize(
    ("scratch", "message"),
    [("{home}", "home directory"), ("{home}/.cache/de", "home directory"), ("/", "must not be '/'")],
)
def test_scratch_refusals(box: DeBox, scratch: str, message: str) -> None:
    result = box.run("xfce", FL_E2E_SCRATCH=scratch.format(home=box.home))
    assert result.returncode == 2
    assert "refusing" in result.stderr and message in result.stderr
    assert box.calls("docker") == []


def test_an_unmarked_scratch_root_is_never_wiped(box: DeBox) -> None:
    box.scratch.mkdir(parents=True)
    (box.scratch / "precious.txt").write_text("keep", encoding="utf-8")
    result = box.run("xfce")
    assert result.returncode == 2
    assert (box.scratch / "precious.txt").read_text(encoding="utf-8") == "keep"


def test_keep_reuses_the_scratch_root(box: DeBox) -> None:
    assert box.run("xfce").returncode == 0
    (box.scratch / "root" / "kept.txt").write_text("kept", encoding="utf-8")
    assert box.run("--keep", "xfce").returncode == 0
    assert (box.scratch / "root" / "kept.txt").is_file()
    assert box.run("xfce").returncode == 0
    assert not (box.scratch / "root" / "kept.txt").exists()


def test_one_run_per_name(box: DeBox) -> None:
    result = box.run("xfce", FAKE_FLOCK_BUSY="name-de-xfce")
    assert result.returncode == 2
    assert "another E2E run uses --name de-xfce" in result.stderr


# -- the matrix -------------------------------------------------------------------------------------

FAKE_RUNNER = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "${FAKE_LOG}.runner"
[[ " ${FAKE_FAILING:-} " != *" $1 "* ]]
"""


def _matrix(tmp_path: Path, *args: str, desktops: tuple[str, ...] = ("xfce", "kde"), **extra: str):
    root = tmp_path / "tree"
    (root / "scripts").mkdir(parents=True)
    (root / "docker").mkdir()
    shutil.copy2(REPO / "scripts" / "ci-e2e-de-matrix.sh", root / "scripts")
    runner = root / "scripts" / "ci-e2e-de.sh"
    runner.write_text(FAKE_RUNNER, encoding="utf-8")
    runner.chmod(0o755)
    for desktop in desktops:
        (root / "docker" / f"Dockerfile.e2e.de.{desktop}").write_text("# fake\n", encoding="utf-8")
    log = tmp_path / "calls"
    result = subprocess.run(
        [str(root / "scripts" / "ci-e2e-de-matrix.sh"), *args],
        env={**os.environ, "FAKE_LOG": str(log), **extra}, capture_output=True, text=True, timeout=60, check=False,
    )
    recorded = Path(f"{log}.runner")
    runs = sorted(recorded.read_text(encoding="utf-8").splitlines()) if recorded.exists() else []
    return result, runs, root / "logs" / "e2e-de"


def test_matrix_runs_every_desktop(tmp_path: Path) -> None:
    result, runs, logs = _matrix(tmp_path)
    assert result.returncode == 0, result.stderr
    assert runs == ["kde", "xfce"]
    assert "kde        PASS" in result.stdout and "xfce       PASS" in result.stdout
    assert (logs / "matrix-de-xfce.out").is_file()


def test_matrix_fork_tier_and_a_failure(tmp_path: Path) -> None:
    result, runs, logs = _matrix(tmp_path, "--fork", "xfce", FAKE_FAILING="xfce")
    assert result.returncode == 1
    assert runs == ["xfce --fork"]
    assert "xfce       FAIL (exit 1)" in result.stdout
    assert (logs / "matrix-de-xfce-fork.out").is_file()


@pytest.mark.parametrize(("args", "desktops"), [(("--bogus",), ("xfce",)), ((), ())])
def test_matrix_usage_errors(tmp_path: Path, args: tuple[str, ...], desktops: tuple[str, ...]) -> None:
    result, runs, _logs = _matrix(tmp_path, *args, desktops=desktops)
    assert result.returncode == 2
    assert runs == []


def test_matrix_help(tmp_path: Path) -> None:
    result, _runs, _logs = _matrix(tmp_path, "--help")
    assert result.returncode == 0
    assert "[--fork] [DESKTOP...]" in result.stdout


def test_a_uid_the_image_knows_keeps_its_passwd(box: DeBox) -> None:
    assert box.run("xfce", FAKE_UID_KNOWN="1").returncode == 0
    assert not any(volume.endswith(":/etc/passwd:ro") for volume in _opts(box.tier_run(), "--volume"))
