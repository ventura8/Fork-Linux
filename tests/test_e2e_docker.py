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
COPIED = ("scripts/e2e-docker.sh", "scripts/lib-e2e-docker.sh", "scripts/ci-e2e-wine.sh", "scripts/e2e-plan.py",
          "tests/e2e/timings.json", "docker/Dockerfile.e2e.wine")
NAME = "unit"
# The real-tier modules of the throwaway worktree (ci-e2e-wine.sh runs what tests/e2e holds).
MODULES = ("test_fork_update.py", "test_real_bridge.py", "test_real_fork.py", "test_wine_app_upgrade.py")
MARKER = ".fl-e2e-scratch"

FAKE_DOCKER = """#!{python}
import json, os, sys, time
fds = {{fd: os.path.exists(f"/proc/self/fd/{{fd}}") for fd in (7, 8, 9)}}
args = sys.argv[1:]
# seen: the files already in the container's root when it starts (what the runner seeded it with).
seen = []
for i, arg in enumerate(args[:-1]):
    if args[:1] == ["run"] and arg in ("--volume", "-v") and args[i + 1].split(":")[1:2] == ["/e2e"]:
        top = os.path.join(args[i + 1].split(":")[0], "root")
        seen = sorted(os.path.relpath(os.path.join(d, f), top) for d, _, fs in os.walk(top) for f in fs)
with open(os.environ["FAKE_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps({{"tool": "docker", "args": args, "fds": fds, "seen": seen, "t": time.time()}}) + "\\n")
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
                ("root/home/.local/share/fork-linux/prefix/system.reg", "prefix"),
                ("root/.fl-e2e-root", "fork-linux E2E root"),
            ):
                if rel == "root/.fl-e2e-root" and os.environ.get("FAKE_NO_MARKER") == "1":
                    continue
                path = os.path.join(e2e, rel)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write(text)
            link = os.path.join(e2e, "root", "logs", "link.log")
            if not os.path.lexists(link):
                os.symlink("/etc/hostname", link)
    print("fake container output")
    # FAKE_RUN_OUTPUTS / FAKE_RUN_RCS: one value per docker run, in order ("|||" / "," separated).
    with open(os.environ["FAKE_LOG"], encoding="utf-8") as log:
        nth = sum(1 for line in log if json.loads(line).get("args", [None])[:1] == ["run"]) - 1
    outputs = os.environ.get("FAKE_RUN_OUTPUTS")
    rcs = os.environ.get("FAKE_RUN_RCS")
    time.sleep(float(os.environ.get("FAKE_RUN_SLEEP", "0")))
    print(outputs.split("|||")[nth] if outputs else os.environ.get("FAKE_RUN_OUTPUT", ""))
    with open(os.environ["FAKE_LOG"], "a", encoding="utf-8") as log:
        log.write(json.dumps({{"tool": "docker-end", "name": args[args.index("--name") + 1], "t": time.time()}}) + "\\n")
    sys.exit(int(rcs.split(",")[nth] if rcs else os.environ.get("FAKE_RUN_RC", "0")))
sys.exit(0)
"""

FAKE_FLOCK = """#!/usr/bin/env bash
nb=0
if [[ "$1" == "-n" ]]; then nb=1; shift; fi
target="$(basename "$(readlink "/proc/$$/fd/$1")")"
printf '{"tool": "flock", "nonblocking": %s, "lock": "%s"}\\n' "$nb" "$target" >> "$FAKE_LOG"
for busy in ${FAKE_FLOCK_BUSY:-}; do
    if [[ "$busy" == "$target" && "$nb" == 1 ]]; then
        # FAKE_FLOCK_BUSY_TRIES=N: a busy slot frees up after N tries (default: never).
        tries="$(grep -cF -- "\\"$target\\"" "$FAKE_LOG")"
        if [[ -z "${FAKE_FLOCK_BUSY_TRIES:-}" || "$tries" -le "$FAKE_FLOCK_BUSY_TRIES" ]]; then exit 1; fi
    fi
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
        for module in MODULES:
            (self.main / "tests" / "e2e").mkdir(parents=True, exist_ok=True)
            (self.main / "tests" / "e2e" / module).write_text('"""A real-tier module."""\n', encoding="utf-8")
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
                # The default follows the host's cores and memory (test_default_slots_follow_cores_and_memory).
                "FL_E2E_SLOTS": "3",
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
    assert "-e" not in opts
    assert "-v" not in opts
    assert all(not value.startswith(("DISPLAY", "XAUTHORITY", "SECRET")) for value in _opts(opts, "--env"))
    # The lock descriptors never reach docker (and so never a container process).
    for row in box.calls("docker"):
        assert row["fds"] == {"7": False, "8": False, "9": False}
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
            assert source != home
            assert not source.startswith(home + "/")
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
    assert "refusing" in result.stderr
    assert message in result.stderr
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


def test_all_slots_busy_takes_the_first_that_frees_up(box: Box) -> None:
    result = box.run("--name", NAME, "pytest", FL_E2E_SLOTS="2", FL_E2E_SLOT_POLL="0",
                     FAKE_FLOCK_BUSY="slot-1 slot-2", FAKE_FLOCK_BUSY_TRIES="2")
    assert result.returncode == 0, result.stderr
    flocks = [(row["lock"], row["nonblocking"]) for row in box.calls("flock")]
    # Never a blocking wait on one slot: every round tries them all, slot-1 frees up on its third try.
    assert flocks == [(f"name-{NAME}", 1), ("slot-1", 1), ("slot-2", 1), ("slot-1", 1), ("slot-2", 1), ("slot-1", 1)]
    assert result.stdout.count("all 2 E2E slots are busy") == 1
    assert "holding E2E slot 1/2" in result.stdout
    assert len(box.docker_runs()) == 1
    assert "FL_E2E_SLOT_POLL" not in " ".join(box.docker_runs()[0])


@pytest.mark.parametrize("poll", ["-1", "x", "1000", "1e3", "0.1234"])
def test_bad_slot_poll(box: Box, poll: str) -> None:
    result = box.run("--name", NAME, "pytest", FL_E2E_SLOT_POLL=poll)
    assert result.returncode == 1
    assert "FL_E2E_SLOT_POLL must be a number of seconds" in result.stderr
    assert box.docker_runs() == []


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
    _bridge_built(box)
    result = box.run("-k", "setup", script="ci-e2e-wine.sh", TMPDIR=str(tmpdir), FL_E2E_FORK_VERSION="2.23.2",
                     FAKE_FLOCK_BUSY="slot-1")
    assert result.returncode == 0, result.stdout + result.stderr
    assert [(row["lock"], row["nonblocking"]) for row in box.calls("flock")] == [("slot-1", 1), ("slot-2", 1)]
    (argv,) = box.docker_runs()
    image = _image_index(argv, "fork-linux-ci-e2e-wine:26.04")
    # No test paths: the whole tier in one session, without the update tiers nobody asked for.
    assert argv[image + 1:][-4:] == ["tests/e2e/test_real_bridge.py", "tests/e2e/test_real_fork.py", "-k", "setup"]
    argv = argv[:image]
    volumes = _opts(argv, "-v")
    assert volumes[0] == f"{box.wt.resolve()}:/src:ro"
    assert volumes[1].startswith(f"{tmpdir}/fl-e2e-wine.")
    assert volumes[1].endswith(":/e2e:rw")
    assert len(volumes) == 2
    assert "FL_E2E_FORK_VERSION" in _opts(argv, "-e")
    assert "FL_E2E_BRIDGE_BUILDS=" in _opts(argv, "-e")
    assert not any(value.endswith(":/seed:ro") or value.startswith("FL_E2E_SEED") for value in argv)
    assert "--init" in argv
    assert "--rm" in argv
    assert list(tmpdir.iterdir()) == [], "the scratch root is deleted afterwards"
    assert "FL_E2E_FORK_UPDATES=0" in _opts(argv, "-e")
    assert "FL_E2E_APP_UPGRADE=0" in _opts(argv, "-e")
    logs = box.wt / "logs" / "e2e-wine" / "tier"
    assert (logs / "root" / "logs" / "launch.log").is_file()
    assert "fake container output" in (logs / "pytest.log").read_text(encoding="utf-8")
    assert not list(logs.rglob("*.png"))
    for row in box.calls("docker"):
        assert row["fds"]["9"] is False


FAKE_VARIANT = """#!/usr/bin/env bash
# fake build-version-variant.sh --tree-only VERSION OUTDIR: a variant tree with a fake build-bridge.sh
set -euo pipefail
printf '{"tool": "variant", "args": "%s"}\\n' "$*" >> "$FAKE_LOG"
tree="$3/fork-linux-$2"
mkdir -p "$tree/scripts"
cp "$(dirname "$0")/build-bridge.sh" "$tree/scripts/build-bridge.sh"
"""
FAKE_BUILD_BRIDGE = """#!/usr/bin/env bash
# fake build-bridge.sh [--out DIR]
set -euo pipefail
printf '{"tool": "build-bridge", "args": "%s"}\\n' "$*" >> "$FAKE_LOG"
if [[ "${1:-}" == "--out" ]]; then
    mkdir -p "$2"
    for name in fl-bridge-helper fl-shim.exe fl-launch.exe; do printf 'build\\n' > "$2/$name"; done
fi
"""


def _bridge_built(box: Box) -> None:
    """The worktree already holds a bridge build (scripts/build-bridge.sh is not run again)."""
    helper = box.wt / "build-bridge" / "bridge" / "fl-bridge-helper"
    helper.parent.mkdir(parents=True)
    helper.write_text("#!/bin/sh\n", encoding="utf-8")
    helper.chmod(0o755)


TEMPLATE_TEST = ["tests/e2e/test_real_fork.py", "-k", "test_01_setup_completes_and_resumes"]
# tests/e2e/timings.json, longest first: what the four modules of the throwaway worktree run as.
PLANNED = ["test_fork_update.py", "test_wine_app_upgrade.py", "test_real_bridge.py", "test_real_fork.py"]
SEEDED_CACHE = [".fl-e2e-root", "home/.cache/fork-linux/downloads/notes.txt"]
PREFIX_FILE = "home/.local/share/fork-linux/prefix/system.reg"


def _variant_tools(box: Box) -> None:
    for name, text in (("build-version-variant.sh", FAKE_VARIANT), ("build-bridge.sh", FAKE_BUILD_BRIDGE)):
        path = box.wt / "scripts" / name
        path.write_text(text, encoding="utf-8")
        path.chmod(0o755)


def _root(argv: list[str]) -> str:
    return next(v for v in _opts(argv, "-v") if v.endswith(":/e2e:rw"))


def _env(argv: list[str], name: str) -> str:
    return next(value.split("=", 1)[1] for value in _opts(argv, "-e") if value.startswith(f"{name}="))


def test_ci_e2e_wine_runs_each_module_of_a_shard_in_a_scratch_root_of_its_own(box: Box) -> None:
    """Named modules: the template first, then one container and root each, longest first; the update tiers
    switched on for their own module only; the bridge is built first, the app-upgrade module gets two bridge
    builds read-only; every module runs although each fails."""
    tmpdir = box.tmp / "var" / "tmp"
    tmpdir.mkdir()
    _variant_tools(box)
    shard = ["tests/e2e/test_real_fork.py", "tests/e2e/test_fork_update.py", "tests/e2e/test_wine_app_upgrade.py",
             "tests/e2e/test_real_bridge.py"]
    result = box.run("--jobs", "1", *shard, "-x", script="ci-e2e-wine.sh", TMPDIR=str(tmpdir),
                     FAKE_RUN_RCS="0,3,3,3,3")
    assert result.returncode == 3, result.stdout + result.stderr
    builds = [row["args"] for row in box.calls() if row["tool"] in ("variant", "build-bridge")]
    assert builds[0] == ""
    assert [args.split()[:2] for args in builds[1:]] == [
        ["--tree-only", "0.9.1"], ["--out", builds[2].split()[1]], ["--tree-only", "0.9.2"],
        ["--out", builds[4].split()[1]],
    ]
    rows = [row for row in box.calls("docker") if row["args"][:1] == ["run"]]
    full = [row["args"] for row in rows]
    assert full[0][-3:] == TEMPLATE_TEST
    assert [argv[-2:] for argv in full[1:]] == [[f"tests/e2e/{module}", "-x"] for module in PLANNED]
    runs = [argv[:_image_index(argv, "fork-linux-ci-e2e-wine:26.04")] for argv in full]
    assert len({_root(argv) for argv in runs}) == 5
    assert all(_root(argv).startswith(f"{tmpdir}/fl-e2e-wine.") for argv in runs)
    switches = [(_env(argv, "FL_E2E_FORK_UPDATES"), _env(argv, "FL_E2E_APP_UPGRADE"), _env(argv, "FL_E2E_REUSE"))
                for argv in runs]
    assert switches == [("0", "0", "0"), ("1", "0", "0"), ("0", "1", "0"), ("0", "0", "1"), ("0", "0", "0")]
    # The template's root: copied whole for a template module, its download caches for a fresh one.
    seen = [row["seen"] for row in rows]
    assert seen[0] == []
    assert seen[1] == seen[2] == seen[4] == SEEDED_CACHE
    assert PREFIX_FILE in seen[3] and "home/.cache/fork-linux/downloads/notes.txt" in seen[3]
    names = [_opts(argv, "--name")[0] for argv in runs]
    assert names[0].endswith("-template") and names[1].endswith("-test_fork_update")
    for argv in (runs[0], runs[1], runs[3], runs[4]):
        assert len(_opts(argv, "-v")) == 2
        assert _env(argv, "FL_E2E_BRIDGE_BUILDS") == ""
    upgrade = _opts(runs[2], "-v")
    assert upgrade[2].endswith(":/bridge-builds:ro")
    built = Path(upgrade[2].split(":")[0])
    assert _env(runs[2], "FL_E2E_BRIDGE_BUILDS") == "/bridge-builds"
    assert not built.exists(), "the bridge builds are deleted afterwards"
    assert list(tmpdir.iterdir()) == []
    logs = box.wt / "logs" / "e2e-wine"
    assert sorted(path.name for path in logs.iterdir()) == [
        "results.tsv", "template", "test_fork_update", "test_real_bridge", "test_real_fork", "test_wine_app_upgrade"]
    for module in ("test_real_fork", "test_fork_update", "test_wine_app_upgrade", "test_real_bridge"):
        assert f"tests/e2e/{module}.py exit 3" in result.stdout
        assert (logs / module / "pytest.log").is_file()
        assert "fake container output" in (logs / module / "pytest.log").read_text(encoding="utf-8")
    assert "the last 60 lines of" in result.stdout
    assert "==> e2e-wine: template exit 0" in result.stdout
    results = [line.split("\t") for line in (logs / "results.tsv").read_text(encoding="utf-8").splitlines()]
    assert [(row[0], row[1], row[3]) for row in results] == [
        ("template", "0", "-"),
        *[(f"tests/e2e/{module}", "3", "template" if module == "test_real_bridge.py" else "fresh")
          for module in PLANNED],
    ]
    for row in box.calls("docker"):
        assert row["fds"]["7"] is False and row["fds"]["9"] is False


def test_ci_e2e_wine_runs_the_modules_in_parallel(box: Box) -> None:
    """--jobs 3: the template and two fresh modules at once; the template module only after the template,
    from a copy of it; a fresh module that starts before the template is ready starts from nothing."""
    tmpdir = box.tmp / "var" / "tmp"
    tmpdir.mkdir()
    _variant_tools(box)
    _bridge_built(box)
    result = box.run("--all", "--jobs", "3", script="ci-e2e-wine.sh", TMPDIR=str(tmpdir), FAKE_RUN_SLEEP="1")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "4 modules, 3 at a time" in result.stdout
    rows = [row for row in box.calls("docker") if row["args"][:1] == ["run"]]
    ends = {row["name"]: row["t"] for row in box.calls("docker-end")}
    spans = {_opts(row["args"], "--name")[0].rsplit("-", 1)[-1]: (row["t"], ends[_opts(row["args"], "--name")[0]], row)
             for row in rows}
    assert sorted(spans) == ["template", "test_fork_update", "test_real_bridge", "test_real_fork",
                             "test_wine_app_upgrade"]
    first = sorted(spans, key=lambda name: spans[name][0])[:3]
    assert sorted(first) == ["template", "test_fork_update", "test_wine_app_upgrade"]
    assert max(spans[name][0] for name in first) < min(spans[name][1] for name in first), "they overlap"
    bridge = spans["test_real_bridge"]
    assert bridge[0] >= spans["template"][1]
    assert PREFIX_FILE in bridge[2]["seen"]
    for name in ("test_fork_update", "test_wine_app_upgrade"):
        assert spans[name][2]["seen"] == []
        assert _env(spans[name][2]["args"], "FL_E2E_REUSE") == "0"
    # --all switches both update tiers on, each for its own module.
    assert _env(spans["test_fork_update"][2]["args"], "FL_E2E_FORK_UPDATES") == "1"
    assert _env(spans["test_wine_app_upgrade"][2]["args"], "FL_E2E_APP_UPGRADE") == "1"
    assert list(tmpdir.iterdir()) == []


def test_ci_e2e_wine_without_a_template(box: Box) -> None:
    """FL_E2E_TEMPLATE=0, or a template that failed: every module sets up its own prefix; a failed template
    fails the run."""
    tmpdir = box.tmp / "var" / "tmp"
    tmpdir.mkdir()
    _bridge_built(box)
    shard = ["tests/e2e/test_real_bridge.py", "tests/e2e/test_real_fork.py"]
    off = box.run("--jobs", "1", *shard, script="ci-e2e-wine.sh", TMPDIR=str(tmpdir), FL_E2E_TEMPLATE="0")
    assert off.returncode == 0, off.stdout + off.stderr
    rows = [row for row in box.calls("docker") if row["args"][:1] == ["run"]]
    assert [row["args"][-1] for row in rows] == shard
    assert [row["seen"] for row in rows] == [[], []]
    assert [_env(row["args"], "FL_E2E_REUSE") for row in rows] == ["0", "0"]
    box.log.unlink()
    failed = box.run("--jobs", "1", *shard, script="ci-e2e-wine.sh", TMPDIR=str(tmpdir), FAKE_RUN_RCS="4,0,0",
                     FAKE_RUN_OUTPUTS="setup failed|||1 passed|||1 passed")
    assert failed.returncode == 4, failed.stdout + failed.stderr
    assert "==> e2e-wine: template exit 4" in failed.stdout
    assert "setup failed" in failed.stdout
    rows = [row for row in box.calls("docker") if row["args"][:1] == ["run"]]
    assert [row["args"][-1] for row in rows] == ["test_01_setup_completes_and_resumes", *shard]
    assert [row["seen"] for row in rows[1:]] == [[], []]
    assert "tests/e2e/test_real_bridge.py exit 0" in failed.stdout
    assert list(tmpdir.iterdir()) == []


def test_ci_e2e_wine_reports_a_root_it_could_not_prepare(box: Box) -> None:
    """A template without the tier's marker cannot seed a fresh root: that module fails, the others run."""
    tmpdir = box.tmp / "var" / "tmp"
    tmpdir.mkdir()
    _bridge_built(box)
    shard = ["tests/e2e/test_real_bridge.py", "tests/e2e/test_real_fork.py"]
    result = box.run("--jobs", "1", *shard, script="ci-e2e-wine.sh", TMPDIR=str(tmpdir), FAKE_NO_MARKER="1")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "test_real_fork: could not prepare its root (fresh)" in result.stdout
    assert "tests/e2e/test_real_bridge.py exit 0" in result.stdout
    assert len(box.docker_runs()) == 2
    runner = (box.wt / "logs" / "e2e-wine" / "test_real_fork" / "runner.log").read_text(encoding="utf-8")
    assert ".fl-e2e-root" in runner


def test_ci_e2e_wine_keeps_a_template_cache_by_key(box: Box) -> None:
    """FL_E2E_TEMPLATE_CACHE (local runs): built once into DIR/<key>, reused while the key holds, a template
    of another key deleted; one run at a time; never inside a home."""
    tmpdir = box.tmp / "var" / "tmp"
    tmpdir.mkdir()
    _bridge_built(box)
    (box.wt / "src" / "fork_linux").mkdir(parents=True)
    (box.wt / "src" / "fork_linux" / "setup.py").write_text("v1\n", encoding="utf-8")
    (box.wt / "tests" / "e2e" / "conftest.py").write_text("", encoding="utf-8")
    (box.wt / "tests" / "e2e" / "fork_driver.py").write_text("", encoding="utf-8")
    cache = box.tmp / "var" / "templates"
    stale = cache / "0123456789abcdef"
    stale.mkdir(parents=True)
    (stale / MARKER).write_text("x", encoding="utf-8")
    shard = "tests/e2e/test_real_bridge.py"
    first = box.run("--jobs", "1", shard, script="ci-e2e-wine.sh", TMPDIR=str(tmpdir), FL_E2E_TEMPLATE_CACHE=str(cache))
    assert first.returncode == 0, first.stdout + first.stderr
    (template,) = [path for path in cache.iterdir() if path.is_dir()]
    assert len(template.name) == 16 and template != stale
    assert (template / ".complete").is_file() and (template / "root" / PREFIX_FILE).is_file()
    assert len(box.docker_runs()) == 2
    assert _root(box.docker_runs()[0]) == f"{template}:/e2e:rw"
    box.log.unlink()
    second = box.run("--jobs", "1", shard, script="ci-e2e-wine.sh", TMPDIR=str(tmpdir), FL_E2E_TEMPLATE_CACHE=str(cache))
    assert second.returncode == 0, second.stdout + second.stderr
    assert f"reusing the template {template}" in second.stdout
    (run,) = [row for row in box.calls("docker") if row["args"][:1] == ["run"]]
    assert PREFIX_FILE in run["seen"]
    box.log.unlink()
    (box.wt / "src" / "fork_linux" / "setup.py").write_text("v2\n", encoding="utf-8")
    third = box.run("--jobs", "1", shard, script="ci-e2e-wine.sh", TMPDIR=str(tmpdir), FL_E2E_TEMPLATE_CACHE=str(cache))
    assert third.returncode == 0, third.stdout + third.stderr
    assert not template.exists(), "the template of the old key is deleted"
    assert len(box.docker_runs()) == 2
    busy = box.run("--jobs", "1", shard, script="ci-e2e-wine.sh", TMPDIR=str(tmpdir), FL_E2E_TEMPLATE_CACHE=str(cache),
                   FAKE_FLOCK_BUSY=".lock")
    assert busy.returncode == 1
    assert "another run uses the template cache" in busy.stderr
    home = box.run(shard, script="ci-e2e-wine.sh", TMPDIR=str(tmpdir), FL_E2E_TEMPLATE_CACHE=str(box.home / "t"))
    assert home.returncode == 2
    assert "home directory" in home.stderr
    assert list(tmpdir.iterdir()) == []


@pytest.mark.parametrize(("args", "message"), [
    (["--jobs"], "--jobs needs a value"),
    (["--jobs", "0", "tests/e2e/test_real_fork.py"], "--jobs must be a number from 1 to 99 (got '0')"),
    (["--jobs", "x", "tests/e2e/test_real_fork.py"], "--jobs must be a number from 1 to 99 (got 'x')"),
    (["--all", "tests/e2e/test_real_fork.py"], "--all runs every module; name no test paths with it"),
])
def test_ci_e2e_wine_usage_errors(box: Box, args: list[str], message: str) -> None:
    tmpdir = box.tmp / "var" / "tmp"
    tmpdir.mkdir()
    result = box.run(*args, script="ci-e2e-wine.sh", TMPDIR=str(tmpdir))
    assert result.returncode == 1
    assert message in result.stderr
    assert box.docker_runs() == []


FAKE_SSH = """#!{python}
import json, os, sys
data = sys.stdin.buffer.read()
with open(os.environ["FAKE_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps({{"tool": "ssh", "args": sys.argv[1:], "stdin": data[:20].decode("latin-1")}}) + "\\n")
print("remote output")
sys.exit(int(os.environ.get("FAKE_SSH_RC", "0")))
"""
FAKE_SCP = """#!{python}
import json, os, sys
with open(os.environ["FAKE_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps({{"tool": "scp", "args": sys.argv[1:]}}) + "\\n")
dest = sys.argv[-1]
os.makedirs(os.path.join(dest, "test_real_bridge"), exist_ok=True)
with open(os.path.join(dest, "results.tsv"), "w", encoding="utf-8") as handle:
    handle.write("tests/e2e/test_real_bridge.py\\t0\\t700\\ttemplate\\n")
"""


def _remote_tools(box: Box) -> None:
    for name, text in (("ssh", FAKE_SSH), ("scp", FAKE_SCP)):
        tool = box.bin / name
        tool.write_text(text.format(python=sys.executable), encoding="utf-8")
        tool.chmod(0o755)


def test_ci_e2e_wine_shares_the_modules_with_a_remote_machine(box: Box) -> None:
    """--remote: the plan splits the modules; this commit goes to the remote clone as a bundle, its share runs
    there with this script, its logs and results come back; its failure counts."""
    tmpdir = box.tmp / "var" / "tmp"
    tmpdir.mkdir()
    _bridge_built(box)
    _remote_tools(box)
    shard = ["tests/e2e/test_real_bridge.py", "tests/e2e/test_real_fork.py"]
    result = box.run("--jobs", "1", "--remote", "nuc", "--remote-jobs", "1", *shard, "-x", script="ci-e2e-wine.sh",
                     TMPDIR=str(tmpdir), FL_E2E_FORK_VERSION="2.23.2")
    assert result.returncode == 0, result.stdout + result.stderr
    (ssh,) = box.calls("ssh")
    assert ssh["args"][:3] == ["-o", "BatchMode=yes", "nuc"]
    assert ssh["stdin"].startswith("# v2 git bundle")
    command = ssh["args"][3]
    assert command.startswith("set -e; cd 'fork-linux-ci'; ")
    assert "git fetch -q /var/tmp/fork-linux-e2e/fl-remote-" in command
    assert "FL_E2E_FORK_VERSION='2.23.2' ./scripts/ci-e2e-wine.sh --jobs 1 'tests/e2e/test_real_fork.py' '-x'" in command
    # The longer module stays here (after its template), the other one ends sooner on the remote machine.
    runs = box.docker_runs()
    assert [argv[-1] for argv in runs] == ["test_01_setup_completes_and_resumes", "-x"]
    assert runs[1][-2:] == ["tests/e2e/test_real_bridge.py", "-x"]
    logs = box.wt / "logs" / "e2e-wine"
    assert "remote output" in (logs / "remote-nuc" / "runner.log").read_text(encoding="utf-8")
    assert (logs / "remote-nuc" / "test_real_bridge").is_dir()
    rows = (logs / "results.tsv").read_text(encoding="utf-8").splitlines()
    assert "nuc:tests/e2e/test_real_bridge.py\t0\t700\ttemplate" in rows
    assert "==> e2e-wine: remote nuc exit 0" in result.stdout
    box.log.unlink()
    failed = box.run("--remote", "nuc", *shard, script="ci-e2e-wine.sh", TMPDIR=str(tmpdir), FAKE_SSH_RC="5")
    assert failed.returncode == 5, failed.stdout + failed.stderr
    assert "remote output" in failed.stdout
    (ssh,) = box.calls("ssh")
    assert "FL_E2E_FORK_VERSION" not in ssh["args"][3]
    assert list(tmpdir.iterdir()) == []


@pytest.mark.parametrize(("args", "message"), [
    (["--remote", "nuc"], "--remote needs --all or test paths"),
    (["--remote", "-x", "tests/e2e/test_real_fork.py"], "--remote must be an ssh host name (got '-x')"),
    (["--remote", "nuc", "--remote-jobs", "0", "tests/e2e/test_real_fork.py"],
     "--remote-jobs must be a number from 1 to 99 (got '0')"),
])
def test_ci_e2e_wine_remote_usage_errors(box: Box, args: list[str], message: str) -> None:
    tmpdir = box.tmp / "var" / "tmp"
    tmpdir.mkdir()
    result = box.run(*args, script="ci-e2e-wine.sh", TMPDIR=str(tmpdir))
    assert result.returncode == 1
    assert message in result.stderr


def test_ci_e2e_wine_remote_ships_only_committed_work(box: Box) -> None:
    tmpdir = box.tmp / "var" / "tmp"
    tmpdir.mkdir()
    (box.wt / "scripts" / "e2e-plan.py").write_text("# changed\n", encoding="utf-8")
    result = box.run("--remote", "nuc", "tests/e2e/test_real_fork.py", script="ci-e2e-wine.sh", TMPDIR=str(tmpdir))
    assert result.returncode == 1
    assert "--remote ships the last commit: commit or stash your changes first" in result.stderr


def _slots(*call: str, **env: str) -> str:
    script = f'. "{REPO}/scripts/lib-e2e-docker.sh"; {" ".join(call)}'
    environ = {key: value for key, value in os.environ.items() if key != "FL_E2E_SLOTS"} | env
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True, env=environ).stdout.strip()


@pytest.mark.parametrize(("cores", "mem_kb", "slots"), [
    (16, 30 * 1048576, 8), (16, 12 * 1048576, 4), (4, 15 * 1048576, 2), (1, 1048576, 1), (400, 1024 * 1048576, 99)])
def test_default_slots_follow_cores_and_memory(cores: int, mem_kb: int, slots: int) -> None:
    assert _slots("fl_e2e_slots_for", str(cores), str(mem_kb)) == str(slots)


def test_default_slots_of_this_host_and_the_override() -> None:
    assert _slots("fl_e2e_default_slots", FL_E2E_SLOTS="5") == "5"
    assert 1 <= int(_slots("fl_e2e_default_slots")) <= 99


def test_ci_e2e_wine_whole_tier_takes_the_update_tiers_only_when_asked(box: Box) -> None:
    tmpdir = box.tmp / "var" / "tmp"
    tmpdir.mkdir()
    _bridge_built(box)
    result = box.run(script="ci-e2e-wine.sh", TMPDIR=str(tmpdir), FL_E2E_FORK_UPDATES="1")
    assert result.returncode == 0, result.stdout + result.stderr
    (argv,) = box.docker_runs()
    assert argv[-3:] == ["tests/e2e/test_fork_update.py", "tests/e2e/test_real_bridge.py",
                         "tests/e2e/test_real_fork.py"]
    assert "FL_E2E_FORK_UPDATES=1" in _opts(argv, "-e")
    assert "tests/e2e (3 modules) exit 0" in result.stdout


def test_ci_e2e_wine_refuses_a_module_that_does_not_exist(box: Box) -> None:
    tmpdir = box.tmp / "var" / "tmp"
    tmpdir.mkdir()
    result = box.run("tests/e2e/test_nothing.py", script="ci-e2e-wine.sh", TMPDIR=str(tmpdir))
    assert result.returncode == 1
    assert "no such real-tier module: tests/e2e/test_nothing.py" in result.stderr
    assert box.docker_runs() == []


def test_ci_e2e_wine_fails_a_module_that_skipped_a_test(box: Box) -> None:
    """A skip in CI is a scenario that silently did not run: the module fails although pytest passed."""
    tmpdir = box.tmp / "var" / "tmp"
    tmpdir.mkdir()
    _bridge_built(box)
    shard = "tests/e2e/test_real_fork.py"
    skipped = "SKIPPED [1] tests/e2e/test_real_fork.py:26: requires FL_E2E_FORK=1"
    result = box.run(shard, script="ci-e2e-wine.sh", TMPDIR=str(tmpdir), FAKE_RUN_OUTPUT=f"1 passed\n{skipped}")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "test_real_fork skipped tests; every scenario of a requested tier must run" in result.stderr
    assert skipped in result.stderr
    assert "tests/e2e/test_real_fork.py exit 1" in result.stdout
    (argv,) = box.docker_runs()
    assert "-rs" in argv
    passed = box.run(shard, script="ci-e2e-wine.sh", TMPDIR=str(tmpdir), FAKE_RUN_OUTPUT="1 passed")
    assert passed.returncode == 0, passed.stdout + passed.stderr
    failed = box.run(shard, script="ci-e2e-wine.sh", TMPDIR=str(tmpdir), FAKE_RUN_OUTPUT=skipped, FAKE_RUN_RC="4")
    assert failed.returncode == 4, "pytest's own failure status wins"


VELOPACK_731 = ("Failed: Velopack could not start the updated Fork: Apply error: "
                "Failed to start application (OS Error 731)")


def test_ci_e2e_wine_reruns_a_module_once_after_velopack_731(box: Box) -> None:
    """Fork's updater failing to start the new Fork under Wine re-runs that module once, logged; the
    re-run's status counts. Another failure is never re-run."""
    tmpdir = box.tmp / "var" / "tmp"
    tmpdir.mkdir()
    _bridge_built(box)
    shard = "tests/e2e/test_fork_update.py"
    first = f"FAILED tests/e2e/test_fork_update.py::test_f1_in_app_update - {VELOPACK_731}\n1 failed"
    result = box.run(shard, script="ci-e2e-wine.sh", TMPDIR=str(tmpdir),
                     FAKE_RUN_OUTPUTS=f"{first}|||1 passed", FAKE_RUN_RCS="1,0")
    assert result.returncode == 0, result.stdout + result.stderr
    notice = ("test_fork_update: Fork's updater failed with 'Failed to start application (OS Error 731)'; "
              "re-running tests/e2e/test_fork_update.py once")
    assert notice in result.stdout
    assert "re-run of tests/e2e/test_fork_update.py exit 0" in result.stdout
    assert "tests/e2e/test_fork_update.py exit 0" in result.stdout
    runs = box.docker_runs()
    assert len(runs) == 2
    roots = {next(v for v in _opts(argv, "-v") if v.endswith(":/e2e:rw")) for argv in runs}
    assert len(roots) == 2, "the re-run gets a fresh scratch root"
    assert "FL_E2E_FORK_UPDATES=1" in _opts(runs[1], "-e")
    logs = box.wt / "logs" / "e2e-wine"
    assert (logs / "test_fork_update" / "pytest.log").read_text(encoding="utf-8").count("OS Error 731") == 1
    assert (logs / "test_fork_update-rerun" / "pytest.log").is_file()
    assert notice in (logs / "reruns.log").read_text(encoding="utf-8")
    assert list(tmpdir.iterdir()) == []


def test_ci_e2e_wine_reruns_only_once_and_never_for_other_failures(box: Box) -> None:
    tmpdir = box.tmp / "var" / "tmp"
    tmpdir.mkdir()
    _bridge_built(box)
    shard = "tests/e2e/test_fork_update.py"
    again = f"FAILED tests/e2e/test_fork_update.py::test_f1_in_app_update - {VELOPACK_731}"
    twice = box.run(shard, script="ci-e2e-wine.sh", TMPDIR=str(tmpdir),
                    FAKE_RUN_OUTPUTS=f"{again}|||{again}|||1 passed", FAKE_RUN_RCS="1,5,0")
    assert twice.returncode == 5, "the re-run's failure counts, and there is no second re-run"
    assert len(box.docker_runs()) == 2
    box.log.unlink()
    other = box.run(shard, script="ci-e2e-wine.sh", TMPDIR=str(tmpdir),
                    FAKE_RUN_OUTPUT="FAILED tests/e2e/test_fork_update.py::test_f2 - AssertionError", FAKE_RUN_RC="1")
    assert other.returncode == 1
    assert len(box.docker_runs()) == 1
    assert "re-running" not in other.stdout
    box.log.unlink()
    no_module = box.run(shard, script="ci-e2e-wine.sh", TMPDIR=str(tmpdir),
                        FAKE_RUN_OUTPUT=f"{VELOPACK_731} (in a log line, no FAILED line)", FAKE_RUN_RC="2")
    assert no_module.returncode == 2
    assert len(box.docker_runs()) == 1


def test_ci_e2e_wine_whole_tier_reruns_its_velopack_731_modules(box: Box) -> None:
    tmpdir = box.tmp / "var" / "tmp"
    tmpdir.mkdir()
    _bridge_built(box)
    first = (f"FAILED tests/e2e/test_fork_update.py::test_f1 - {VELOPACK_731}\n"
             "ERROR tests/e2e/test_fork_update.py::test_f2 - chained\n"
             "FAILED tests/e2e/test_real_fork.py::test_03 - AssertionError")
    result = box.run(script="ci-e2e-wine.sh", TMPDIR=str(tmpdir), FL_E2E_FORK_UPDATES="1",
                     FAKE_RUN_OUTPUTS=f"{first}|||1 passed|||2 failed", FAKE_RUN_RCS="1,0,3")
    assert result.returncode == 3, result.stdout + result.stderr
    runs = box.docker_runs()
    assert [argv[-1] for argv in runs[1:]] == ["tests/e2e/test_fork_update.py", "tests/e2e/test_real_fork.py"]
    assert "re-run of tests/e2e/test_fork_update.py exit 0" in result.stdout
    assert "re-run of tests/e2e/test_real_fork.py exit 3" in result.stdout
    assert "tests/e2e (3 modules) exit 3" in result.stdout


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
    for rel in ("scripts/e2e-docker.sh", "scripts/ci-e2e-wine.sh", "scripts/ci-e2e-de.sh"):
        text = (REPO / rel).read_text(encoding="utf-8")
        assert '. "${ROOT}/scripts/lib-e2e-docker.sh"' in text, rel
        for function in ("fl_e2e_check_dir", "fl_e2e_acquire_slot", "fl_e2e_copy_logs"):
            assert function in text, (rel, function)
        assert "--privileged" not in text.replace("never --privileged", "")
        assert "/tmp/.X11-unix" not in text
