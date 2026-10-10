"""Tests for scripts/e2e-docker.sh and scripts/ci-e2e-wine.sh (+ scripts/lib-e2e-docker.sh).

The scripts run for real, from a copy inside a throwaway git repository with a linked
worktree, with fake ``docker`` and ``flock`` first on PATH that only record what they were
asked to do. Checked: the scratch-root refusals ($HOME, '/', symlinks, unmarked directories),
that the container sees no host path except the read-only worktree / git common dir, the
scratch root and the read-only seed, the environment whitelist, the host-wide slots, the
logs-only copy, and that the lock descriptors never reach the container.
"""

from __future__ import annotations

import json
import re
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[1]
COPIED = ("scripts/e2e-docker.sh", "scripts/lib-e2e-docker.sh", "scripts/ci-e2e-wine.sh", "docker/Dockerfile.e2e.wine")
NAME = "unit"
MARKER = ".fl-e2e-scratch"

FAKE_DOCKER = """#!{python}
import json, os, sys
fds = {{fd: os.path.exists(f"/proc/self/fd/{{fd}}") for fd in (8, 9)}}
args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps({{"tool": "docker", "args": args, "fds": fds}}) + "\\n")
if args[:2] == ["image", "inspect"]:
    if os.environ.get("FAKE_IMAGE_MISSING") == "1":
        sys.exit(1)
    print(os.environ.get("FAKE_IMAGE_DIGEST", ""))
    sys.exit(0)
if args[:1] == ["run"]:
    for i, arg in enumerate(args):
        if arg in ("--volume", "-v") and args[i + 1].split(":")[1:2] == ["/e2e"]:
            e2e = args[i + 1].split(":")[0]
            for rel, text in (
                ("root/logs/launch.log", "launched"),
                ("root/logs/setup.json", "{{}}"),
                ("root/shots/03-main.png", "PNG"),
                ("root/home/.local/share/fork-linux/prefix/drive_c/users/u/fork.log", "secret"),
                ("root/home/.cache/fork-linux/downloads/notes.txt", "cache"),
            ):
                path = os.path.join(e2e, rel)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write(text)
            link = os.path.join(e2e, "root", "logs", "link.log")
            if not os.path.lexists(link):
                os.symlink("/etc/hostname", link)
    print("fake container output")
    sys.exit(int(os.environ.get("FAKE_RUN_RC", "0")))
sys.exit(0)
"""

FAKE_FLOCK = """#!/usr/bin/env bash
nb=0
if [[ "$1" == "-n" ]]; then nb=1; shift; fi
target="$(basename "$(readlink "/proc/$$/fd/$1")")"
printf '{"tool": "flock", "nonblocking": %s, "lock": "%s"}\\n' "$nb" "$target" >> "$FAKE_LOG"
for busy in ${FAKE_FLOCK_BUSY:-}; do
    if [[ "$busy" == "$target" && "$nb" == 1 ]]; then exit 1; fi
done
exit 0
"""


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=T", "-c", "user.email=t@example.invalid", "-c", "init.defaultBranch=main", *args],
        cwd=str(cwd), capture_output=True, text=True, check=True,
        env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "HOME": str(cwd)},
    ).stdout.strip()


class Box:
    """A throwaway repository + linked worktree holding the scripts, fakes and fake homes."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.main = tmp / "repo"
        self.wt = tmp / "wt"
        self.home = tmp / "home"
        self.bin = tmp / "bin"
        self.log = tmp / "calls.jsonl"
        self.scratch = tmp / "var" / "scratch"
        self.locks = tmp / "var" / "locks"
        self.seed = tmp / "var" / "seed"
        for directory in (self.main, self.home, self.bin, self.seed / "winetricks" / "dotnet48", self.tmp / "var"):
            directory.mkdir(parents=True, exist_ok=True)
        (self.seed / "Fork-2.23.2.exe").write_bytes(b"MZ")
        for rel in COPIED:
            target = self.main / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(REPO / rel, target)
        _git(self.main, "init", "-q")
        _git(self.main, "add", "-A")
        _git(self.main, "commit", "-qm", "scripts")
        _git(self.main, "worktree", "add", "-q", str(self.wt), "-b", "topic")
        self.git_common = Path(_git(self.wt, "rev-parse", "--path-format=absolute", "--git-common-dir"))
        docker = self.bin / "docker"
        docker.write_text(FAKE_DOCKER.format(python=sys.executable), encoding="utf-8")
        flock = self.bin / "flock"
        flock.write_text(FAKE_FLOCK, encoding="utf-8")
        for tool in (docker, flock):
            tool.chmod(0o755)

    def env(self, **extra: str) -> dict[str, str]:
        env = {
            key: value for key, value in os.environ.items()
            if not key.startswith(("FL_E2E_", "FL_REAL_", "FAKE_")) and key not in ("TMPDIR",)
        }
        env.update(
            {
                "PATH": f"{self.bin}:{os.environ.get('PATH', '/usr/bin:/bin')}",
                "HOME": str(self.home),
                "FAKE_LOG": str(self.log),
                "FL_E2E_SCRATCH": str(self.scratch),
                "FL_E2E_LOCK_DIR": str(self.locks),
                "FL_E2E_SEED_DIR": str(self.seed),
                "SECRET_TOKEN": "do-not-pass",
            }
        )
        env.update(extra)
        return {key: value for key, value in env.items() if value != "<unset>"}

    def run(self, *args: str, script: str = "e2e-docker.sh", **extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(self.wt / "scripts" / script), *args],
            cwd=str(self.tmp), env=self.env(**extra), capture_output=True, text=True, timeout=60, check=False,
        )

    def calls(self, tool: str | None = None) -> list[dict[str, Any]]:
        if not self.log.exists():
            return []
        rows = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]
        return [row for row in rows if tool is None or row["tool"] == tool]

    def docker_runs(self) -> list[list[str]]:
        return [row["args"] for row in self.calls("docker") if row["args"][:1] == ["run"]]


@pytest.fixture
def box(tmp_path: Path) -> Box:
    return Box(tmp_path)


def _opts(argv: list[str], flag: str) -> list[str]:
    return [argv[i + 1] for i, arg in enumerate(argv[:-1]) if arg == flag]


def _image_index(argv: list[str], image: str) -> int:
    return argv.index(image)


# -- pytest mode ----------------------------------------------------------------------------------


def test_pytest_mode_mounts_env_and_logs(box: Box) -> None:
    result = box.run("--name", NAME, "pytest", FL_E2E_FOO="bar", FL_E2E_ROOT="/host/root", FL_E2E_SEED="/host/seed",
                     FL_E2E_FORK="0", FL_E2E_SLOTS="2", FAKE_RUN_RC="3")
    assert result.returncode == 3, result.stderr
    runs = box.docker_runs()
    assert len(runs) == 1
    argv = runs[0]
    image = _image_index(argv, "fork-linux-ci-e2e-wine:26.04")
    assert argv[image + 1:] == ["python3", "-m", "pytest", "-p", "no:cacheprovider", "-o", "addopts=", "-v",
                                "tests/e2e"]
    opts = argv[:image]
    for flag in ("--rm", "--init"):
        assert flag in opts
    assert "--privileged" not in opts
    assert "--cap-add" not in opts
    assert "--network" not in opts
    assert _opts(opts, "--label") == [f"fl-e2e={NAME}"]
    assert _opts(opts, "--name") == [f"fl-e2e-{NAME}"]
    assert _opts(opts, "--user") == [f"{os.getuid()}:{os.getgid()}"]
    assert _opts(opts, "--workdir") == ["/src"]
    assert sorted(_opts(opts, "--volume")) == sorted(
        [
            f"{box.wt.resolve()}:/src:ro",
            f"{box.git_common}:{box.git_common}:ro",
            f"{box.scratch}:/e2e:rw",
            f"{box.seed}:/seed:ro",
        ]
    )
    named = subprocess.run(["id", "-un"], capture_output=True, text=True, check=False)
    user = named.stdout.strip() if named.returncode == 0 else os.environ.get("USER") or f"uid{os.getuid()}"
    assert sorted(_opts(opts, "--env")) == sorted(
        [
            "HOME=/e2e/home",
            f"USER={user}",
            f"LOGNAME={user}",
            "PYTHONDONTWRITEBYTECODE=1",
            "FL_E2E_FORK=1",
            "FL_E2E_ROOT=/e2e/root",
            "FL_E2E_SEED=/seed",
            "FL_E2E_WINETRICKS_CACHE=/seed/winetricks",
            "FL_E2E_FOO=bar",
        ]
    )
    assert "-e" not in opts and "-v" not in opts
    assert all(not value.startswith(("DISPLAY", "XAUTHORITY", "SECRET")) for value in _opts(opts, "--env"))
    # The lock descriptors never reach docker (and so never a container process).
    for row in box.calls("docker"):
        assert row["fds"] == {"8": False, "9": False}
    # Scratch root: private, marked, with HOME and the E2E root inside.
    assert stat.S_IMODE(box.scratch.stat().st_mode) == 0o700
    assert (box.scratch / MARKER).is_file()
    assert (box.scratch / "home").is_dir()
    # Logs: pytest output tee'd, text logs copied, never images, drive_c, caches or symlinks.
    logs = box.wt / "logs" / "e2e-docker" / NAME
    assert "fake container output" in (logs / "pytest.log").read_text(encoding="utf-8")
    copied = sorted(str(path.relative_to(logs)) for path in logs.rglob("*") if path.is_file())
    assert copied == ["pytest.log", "root/logs/launch.log", "root/logs/setup.json"]
    assert f"scratch root: {box.scratch}" in result.stdout
    assert f"{box.scratch}/root/shots" in result.stdout


def test_no_mount_source_lies_in_a_home_except_the_read_only_tree(box: Box) -> None:
    assert box.run("--name", NAME, "pytest", "tests/e2e/test_real_fork.py", "-k", "setup").returncode == 0
    argv = box.docker_runs()[0]
    assert argv[-3:] == ["tests/e2e/test_real_fork.py", "-k", "setup"]
    entry = subprocess.run(["getent", "passwd", str(os.getuid())], capture_output=True, text=True,
                           check=False).stdout.split(":")
    homes = {str(box.home)} | ({entry[5]} if len(entry) > 5 else set())
    for volume in _opts(argv, "--volume"):
        source, target, mode = volume.split(":")
        for home in homes:
            assert source != home and not source.startswith(home + "/")
        if mode == "rw":
            assert target == "/e2e"
            assert source == str(box.scratch)
    assert not any("/tmp/.X11-unix" in value for value in argv)


def test_ptrace_adds_only_sys_ptrace(box: Box) -> None:
    assert box.run("--name", NAME, "--ptrace", "pytest").returncode == 0
    argv = box.docker_runs()[0]
    assert _opts(argv, "--cap-add") == ["SYS_PTRACE"]
    assert "--privileged" not in argv


def test_other_fl_e2e_settings_are_not_forwarded(box: Box) -> None:
    assert box.run("--name", NAME, "pytest", FL_E2E_DISPLAY=":97", FL_E2E_FORK_VERSION="2.23.2",
                   FL_REAL_WINE="1", WINEPREFIX="/home/x/.wine").returncode == 0
    envs = _opts(box.docker_runs()[0], "--env")
    assert "FL_E2E_DISPLAY=:97" in envs
    assert "FL_E2E_FORK_VERSION=2.23.2" in envs
    assert not any(value.startswith(("FL_REAL_", "WINEPREFIX", "FL_E2E_SCRATCH", "FL_E2E_LOCK_DIR",
                                      "FL_E2E_SEED_DIR", "FL_E2E_SLOTS")) for value in envs)


def test_without_a_seed_nothing_is_mounted_for_it(box: Box) -> None:
    shutil.rmtree(box.seed)
    result = box.run("--name", NAME, "pytest")
    assert result.returncode == 0
    assert "no seed at" in result.stdout
    argv = box.docker_runs()[0]
    assert not any(volume.endswith(":/seed:ro") for volume in _opts(argv, "--volume"))
    assert not any(value.startswith(("FL_E2E_SEED", "FL_E2E_WINETRICKS")) for value in _opts(argv, "--env"))


def test_a_seed_without_winetricks_cache(box: Box) -> None:
    shutil.rmtree(box.seed / "winetricks")
    assert box.run("--name", NAME, "pytest").returncode == 0
    envs = _opts(box.docker_runs()[0], "--env")
    assert "FL_E2E_SEED=/seed" in envs
    assert not any(value.startswith("FL_E2E_WINETRICKS_CACHE") for value in envs)


# -- shell mode -----------------------------------------------------------------------------------


def test_shell_mode_runs_the_command_with_a_private_display(box: Box) -> None:
    result = box.run("--name", NAME, "shell", "--", "xdotool", "search", "--class", "fork.exe")
    assert result.returncode == 0, result.stderr
    argv = box.docker_runs()[0]
    assert "DISPLAY=:99" in _opts(argv, "--env")
    assert "FL_E2E_FORK=1" in _opts(argv, "--env")
    image = _image_index(argv, "fork-linux-ci-e2e-wine:26.04")
    assert argv[image + 1:image + 3] == ["bash", "-c"]
    assert "Xvfb" in argv[image + 3]
    assert argv[image + 4:] == ["fl-e2e-shell", "xdotool", "search", "--class", "fork.exe"]
    assert "--tty" not in argv
    assert not (box.wt / "logs" / "e2e-docker" / NAME / "pytest.log").exists()


@pytest.mark.parametrize("args", [["shell"], ["shell", "xdotool"], ["shell", "--"], [], ["bogus"], ["--name"]])
def test_usage_errors(box: Box, args: list[str]) -> None:
    result = box.run(*args)
    assert result.returncode == 2
    assert box.docker_runs() == []


def test_help(box: Box) -> None:
    result = box.run("--help")
    assert result.returncode == 0
    assert "pytest [ARGS...]" in result.stdout


# -- refusals -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("scratch", "message"),
    [
        ("{home}", "home directory"),
        ("{home}/.cache/fl-e2e", "home directory"),
        ("{tmp}", "home directory"),
        ("/", "must not be '/'"),
        ("relative/scratch", "not an absolute path"),
        ("{tmp}/var/../var/scratch", "'..'"),
    ],
)
def test_scratch_refusals(box: Box, scratch: str, message: str) -> None:
    precious = box.home / "precious.txt"
    precious.write_text("keep", encoding="utf-8")
    result = box.run("--name", NAME, "pytest", FL_E2E_SCRATCH=scratch.format(home=box.home, tmp=box.tmp))
    assert result.returncode == 2, result.stdout + result.stderr
    assert "refusing" in result.stderr and message in result.stderr
    assert box.docker_runs() == []
    assert precious.read_text(encoding="utf-8") == "keep"


def test_scratch_with_a_symlink_component_is_refused(box: Box) -> None:
    (box.tmp / "var" / "link").symlink_to(box.tmp / "var")
    result = box.run("--name", NAME, "pytest", FL_E2E_SCRATCH=str(box.tmp / "var" / "link" / "scratch"))
    assert result.returncode == 2
    assert "symlink component" in result.stderr
    box.scratch.symlink_to(box.home)
    result = box.run("--name", NAME, "pytest")
    assert result.returncode == 2
    assert "symlink component" in result.stderr
    assert box.docker_runs() == []


def test_the_passwd_home_is_refused_too(box: Box) -> None:
    passwd_home = subprocess.run(["getent", "passwd", str(os.getuid())], capture_output=True, text=True,
                                 check=False).stdout.split(":")
    target = passwd_home[5] if len(passwd_home) > 5 else "/"
    result = box.run("--name", NAME, "pytest", FL_E2E_SCRATCH=target)
    assert result.returncode == 2
    assert "refusing" in result.stderr


def test_an_unmarked_non_empty_scratch_is_never_wiped(box: Box) -> None:
    box.scratch.mkdir()
    (box.scratch / "someone-elses.txt").write_text("keep", encoding="utf-8")
    for extra in ([], ["--keep"]):
        result = box.run("--name", NAME, *extra, "pytest")
        assert result.returncode == 2
        assert "no .fl-e2e-scratch marker" in result.stderr
    assert (box.scratch / "someone-elses.txt").read_text(encoding="utf-8") == "keep"
    assert box.docker_runs() == []


def test_a_scratch_owned_by_a_file_is_refused(box: Box) -> None:
    box.scratch.write_text("not a directory", encoding="utf-8")
    result = box.run("--name", NAME, "pytest")
    assert result.returncode == 2
    assert "not a plain directory" in result.stderr


@pytest.mark.parametrize("name", ["../x", "a/b", "seed", "locks", ".hidden", "x" * 70])
def test_bad_names_are_refused(box: Box, name: str) -> None:
    result = box.run("--name", name, "pytest")
    assert result.returncode == 2
    assert box.docker_runs() == []


@pytest.mark.parametrize("image", ["fork-linux-ci-e2e-wine", "ubuntu:latest", "bad image:1"])
def test_unpinned_images_are_refused(box: Box, image: str) -> None:
    result = box.run("--name", NAME, "--image", image, "pytest")
    assert result.returncode == 2
    assert "pinned tag" in result.stderr


# -- wipe / keep ----------------------------------------------------------------------------------


def _previous_run(box: Box) -> Path:
    assert box.run("--name", NAME, "pytest").returncode == 0
    stale = box.scratch / "root" / "stale.txt"
    stale.write_text("old", encoding="utf-8")
    victim = box.home / "Projects"
    victim.mkdir()
    (victim / "work.txt").write_text("keep", encoding="utf-8")
    (box.scratch / "root" / "to-home").symlink_to(box.home)
    readonly = box.scratch / "root" / "drive_c-ro"
    readonly.mkdir()
    (readonly / "f").write_text("x", encoding="utf-8")
    readonly.chmod(0o555)
    return stale


def test_a_marked_scratch_is_wiped_without_following_symlinks(box: Box) -> None:
    stale = _previous_run(box)
    result = box.run("--name", NAME, "pytest")
    assert result.returncode == 0, result.stderr
    assert "wiping the previous scratch root" in result.stdout
    assert not stale.exists()
    assert not (box.scratch / "root" / "drive_c-ro").exists()
    assert (box.home / "Projects" / "work.txt").read_text(encoding="utf-8") == "keep"
    assert (box.scratch / MARKER).is_file()


def test_keep_reuses_the_scratch_root(box: Box) -> None:
    stale = _previous_run(box)
    (box.scratch / "root" / "drive_c-ro").chmod(0o755)
    assert box.run("--name", NAME, "--keep", "pytest").returncode == 0
    assert stale.read_text(encoding="utf-8") == "old"


# -- locks and slots ------------------------------------------------------------------------------


def test_slots_are_tried_in_order_then_taken(box: Box) -> None:
    result = box.run("--name", NAME, "pytest", FAKE_FLOCK_BUSY="slot-1 slot-2")
    assert result.returncode == 0, result.stderr
    flocks = [(row["lock"], row["nonblocking"]) for row in box.calls("flock")]
    assert flocks == [(f"name-{NAME}", 1), ("slot-1", 1), ("slot-2", 1), ("slot-3", 1)]
    assert "holding E2E slot 3/3" in result.stdout


def test_all_slots_busy_waits_on_one(box: Box) -> None:
    result = box.run("--name", NAME, "pytest", FL_E2E_SLOTS="2", FAKE_FLOCK_BUSY="slot-1 slot-2")
    assert result.returncode == 0, result.stderr
    flocks = [(row["lock"], row["nonblocking"]) for row in box.calls("flock")]
    assert flocks[:3] == [(f"name-{NAME}", 1), ("slot-1", 1), ("slot-2", 1)]
    assert flocks[3][1] == 0 and flocks[3][0] in ("slot-1", "slot-2")
    assert "all 2 E2E slots are busy" in result.stdout
    assert len(box.docker_runs()) == 1


@pytest.mark.parametrize("slots", ["0", "abc", "100", ""])
def test_bad_slot_counts(box: Box, slots: str) -> None:
    result = box.run("--name", NAME, "pytest", FL_E2E_SLOTS=slots)
    if slots == "":
        assert result.returncode == 0
        return
    assert result.returncode == 1
    assert "FL_E2E_SLOTS" in result.stderr
    assert box.docker_runs() == []


def test_one_run_per_name(box: Box) -> None:
    result = box.run("--name", NAME, "pytest", FAKE_FLOCK_BUSY=f"name-{NAME}")
    assert result.returncode == 2
    assert f"another e2e-docker.sh run uses --name {NAME}" in result.stderr
    assert box.docker_runs() == []
    assert not box.scratch.exists()


def test_leftover_container_is_removed_before_and_after(box: Box) -> None:
    assert box.run("--name", NAME, "pytest").returncode == 0
    rms = [row["args"] for row in box.calls("docker") if row["args"][:1] == ["rm"]]
    assert rms == [["rm", "-f", f"fl-e2e-{NAME}"]] * 2
    kinds = [row["args"][0] for row in box.calls("docker")]
    assert kinds.index("rm") < kinds.index("run") < len(kinds) - 1 - kinds[::-1].index("rm")


# -- image ----------------------------------------------------------------------------------------


def _digest(box: Box) -> str:
    return subprocess.run(["sha256sum", str(box.wt / "docker" / "Dockerfile.e2e.wine")], capture_output=True,
                          text=True, check=True).stdout.split()[0]


def test_default_image_is_built_when_the_dockerfile_changed(box: Box) -> None:
    assert box.run("--name", NAME, "pytest", FAKE_IMAGE_DIGEST="old").returncode == 0
    builds = [row["args"] for row in box.calls("docker") if row["args"][:1] == ["build"]]
    assert len(builds) == 1
    assert f"fork-linux.ci.dockerfile-digest={_digest(box)}" in builds[0]
    assert builds[0][-1] == str(box.wt.resolve() / "docker")


def test_default_image_is_reused_when_current(box: Box) -> None:
    result = box.run("--name", NAME, "pytest", FAKE_IMAGE_DIGEST=_digest(box))
    assert result.returncode == 0
    assert "reusing fork-linux-ci-e2e-wine:26.04" in result.stdout
    assert not [row for row in box.calls("docker") if row["args"][:1] == ["build"]]


def test_another_image_must_exist(box: Box) -> None:
    result = box.run("--name", NAME, "--image", "fork-linux-ci-bridge:26.04", "pytest", FAKE_IMAGE_MISSING="1")
    assert result.returncode == 1
    assert "not found" in result.stderr
    assert box.run("--name", NAME, "--image", "fork-linux-ci-bridge:26.04", "pytest").returncode == 0
    assert "fork-linux-ci-bridge:26.04" in box.docker_runs()[0]
    assert not [row for row in box.calls("docker") if row["args"][:1] == ["build"]]


def test_missing_tools(box: Box, tmp_path: Path) -> None:
    only = tmp_path / "only"
    only.mkdir()
    for tool in ("bash", "dirname", "basename", "env"):
        (only / tool).symlink_to(shutil.which(tool) or f"/usr/bin/{tool}")
    result = box.run("--name", NAME, "pytest", PATH=str(only))
    assert result.returncode == 1
    assert "docker not found" in result.stderr
    (only / "docker").symlink_to(box.bin / "docker")
    result = box.run("--name", NAME, "pytest", PATH=str(only))
    assert result.returncode == 1
    assert "flock not found" in result.stderr


# -- ci-e2e-wine.sh (CI behaviour: fresh downloads, no seed, logs only) ----------------------------


def test_ci_e2e_wine_uses_a_fresh_wiped_scratch_and_no_seed(box: Box) -> None:
    tmpdir = box.tmp / "var" / "tmp"
    tmpdir.mkdir()
    result = box.run("-k", "setup", script="ci-e2e-wine.sh", TMPDIR=str(tmpdir), FL_E2E_FORK_VERSION="2.23.2",
                     FAKE_FLOCK_BUSY="slot-1")
    assert result.returncode == 0, result.stdout + result.stderr
    assert [(row["lock"], row["nonblocking"]) for row in box.calls("flock")] == [("slot-1", 1), ("slot-2", 1)]
    argv = box.docker_runs()[0]
    image = _image_index(argv, "fork-linux-ci-e2e-wine:26.04")
    assert argv[image + 1:][-2:] == ["-k", "setup"]
    argv = argv[:image]
    volumes = _opts(argv, "-v")
    assert volumes[0] == f"{box.wt.resolve()}:/src:ro"
    assert volumes[1].startswith(f"{tmpdir}/fl-e2e-wine.") and volumes[1].endswith(":/e2e:rw")
    assert len(volumes) == 2
    assert "FL_E2E_FORK_VERSION" in _opts(argv, "-e")
    assert not any(value.endswith(":/seed:ro") or value.startswith("FL_E2E_SEED") for value in argv)
    assert "--init" in argv and "--rm" in argv
    assert list(tmpdir.iterdir()) == [], "the scratch root is deleted afterwards"
    logs = box.wt / "logs" / "e2e-wine"
    assert (logs / "root" / "logs" / "launch.log").is_file()
    assert not list(logs.rglob("*.png"))
    for row in box.calls("docker"):
        assert row["fds"]["9"] is False


def test_ci_e2e_wine_refuses_a_tmpdir_in_home(box: Box) -> None:
    result = box.run(script="ci-e2e-wine.sh", TMPDIR=str(box.home))
    assert result.returncode == 2
    assert "home directory" in result.stderr
    assert list(box.home.iterdir()) == []


# -- contracts: docs, skills, image ----------------------------------------------------------------

HOST_RUN = re.compile(r"FL_(?:E2E_FORK|REAL_WINE)=1 (?:python3|pytest|xvfb-run)")


def test_agents_md_hard_rule_18() -> None:
    text = (REPO / "AGENTS.md").read_text(encoding="utf-8")
    rule = next(line for line in text.splitlines() if line.startswith("> 18. "))
    for needle in ("only in containers", "scripts/e2e-docker.sh", "scripts/ci-*.sh", "never on the host",
                   "scratch roots outside `$HOME`", "never mount `$HOME` writable", "2026-10-10"):
        assert needle in rule, needle
    assert "tests/fixtures/real_tier.py" in rule


def test_docs_and_skills_route_real_tiers_through_containers() -> None:
    docs = [REPO / rel for rel in ("AGENTS.md", "skills.md", "docs/INSTRUCTIONS.md", "bridge/README.md",
                                   "bridge/win/README.md", "logs/README.md")]
    skills = sorted((REPO / ".agents" / "skills").glob("*/SKILL.md"))
    for path in docs + skills:
        for line in path.read_text(encoding="utf-8").splitlines():
            if HOST_RUN.search(line):
                assert "on the host" in line, f"{path.relative_to(REPO)} tells to run a real tier: {line}"
    for rel in ("test-runner", "pipeline-runner", "e2e-docker", "wine-runtime-bump", "fork-version-bump",
                "git-bridge", "troubleshoot"):
        text = (REPO / ".agents" / "skills" / rel / "SKILL.md").read_text(encoding="utf-8")
        assert "hard rule 18" in text, rel
    assert "e2e-docker" in (REPO / "skills.md").read_text(encoding="utf-8")
    assert "scripts/e2e-docker.sh" in (REPO / "docs" / "INSTRUCTIONS.md").read_text(encoding="utf-8")
    assert "e2e-docker/<NAME>/" in (REPO / "logs" / "README.md").read_text(encoding="utf-8")


def test_e2e_image_carries_the_exploration_tools() -> None:
    text = (REPO / "docker" / "Dockerfile.e2e.wine").read_text(encoding="utf-8")
    packages = set(re.findall(r"[a-z0-9][a-z0-9.+-]+", text.split("apt-get install", 1)[1].split("\n\n", 1)[0]))
    for package in ("xvfb", "xdotool", "x11-utils", "x11-xserver-utils", "imagemagick", "strace", "git-lfs",
                    "openssh-client", "openssh-server", "python3-pytest", "git"):
        assert package in packages, package
    assert "latest" not in text


def test_both_runners_share_the_library() -> None:
    for rel in ("scripts/e2e-docker.sh", "scripts/ci-e2e-wine.sh"):
        text = (REPO / rel).read_text(encoding="utf-8")
        assert '. "${ROOT}/scripts/lib-e2e-docker.sh"' in text, rel
        assert "fl_e2e_check_dir" in text and "fl_e2e_acquire_slot" in text and "fl_e2e_copy_logs" in text
        assert "--privileged" not in text.replace("never --privileged", "")
        assert "/tmp/.X11-unix" not in text
