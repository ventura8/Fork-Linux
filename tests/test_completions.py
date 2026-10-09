"""bash / zsh / fish completions: generated from the argparse parser and complete for every command."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from fixtures import gen_data

ROOT = Path(__file__).resolve().parents[1]
FILES = {
    "bash": ROOT / "data" / "completions" / "fork-linux.bash",
    "zsh": ROOT / "data" / "completions" / "_fork-linux",
    "fish": ROOT / "data" / "completions" / "fork-linux.fish",
}


gen = gen_data.load()
TREE = gen.load_tree()


@pytest.mark.parametrize("shell", sorted(FILES))
def test_committed_completion_is_current(shell: str) -> None:
    assert FILES[shell].read_text(encoding="utf-8") == gen.render_completions(shell, TREE), (
        "run scripts/gen-data.py write"
    )


@pytest.mark.parametrize("shell", sorted(FILES))
def test_every_command_and_option_is_completed(shell: str) -> None:
    text = FILES[shell].read_text(encoding="utf-8")
    for node in gen.all_nodes(TREE):
        for name, _help, _child in node.subcommands:
            assert name in text, f"{shell}: command {name!r} missing"
        for option in node.options:
            for flag in option.flags:
                needle = flag if shell != "fish" else (f"-l {flag[2:]}" if flag.startswith("--") else f"-s {flag[1:]}")
                assert needle in text, f"{shell}: {node.key or 'fork-linux'} {flag} missing"


def test_unknown_shell_is_refused() -> None:
    with pytest.raises(SystemExit):
        gen.render_completions("tcsh", TREE)


@pytest.mark.parametrize(
    ("shell", "argv"),
    [("bash", ["bash", "-n"]), ("zsh", ["zsh", "-n"]), ("fish", ["fish", "--no-execute"])],
)
def test_syntax(shell: str, argv: list[str]) -> None:
    if shutil.which(argv[0]) is None:
        pytest.skip(f"{argv[0]} is not installed (the lint stage checks it)")
    subprocess.run([*argv, str(FILES[shell])], check=True)


def _bash_complete(*words: str) -> list[str]:
    script = (
        f'source "{FILES["bash"]}"\n'
        f"COMP_WORDS=({' '.join(repr(w) for w in words)})\n"
        f"COMP_CWORD={len(words) - 1}\n"
        "_fork_linux\n"
        'printf "%s\\n" "${COMPREPLY[@]}"\n'
    )
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True).stdout.split()


def test_bash_completes_commands_subcommands_and_choices() -> None:
    assert "snapshot" in _bash_complete("fork-linux", "sn")
    assert sorted(_bash_complete("fork-linux", "snapshot", "")) == ["create", "delete", "list", "prune"]
    assert _bash_complete("fork-linux", "setup", "--dotnet", "") == ["auto", "dotnet48", "dotnet472"]
    assert "--accept-fork-eula" in _bash_complete("fork-linux", "setup", "--acc")
    assert "--from-file-manager" in _bash_complete("fork-linux", "--offline", "run", "--from")
    assert _bash_complete("fork-linux", "ssh", "sync", "--mode", "") == ["link", "copy"]


def test_bash_completion_is_shellcheck_clean() -> None:
    if shutil.which("shellcheck") is None:
        pytest.skip("shellcheck is not installed (the lint stage runs it)")
    subprocess.run(["shellcheck", "--shell=bash", str(FILES["bash"])], check=True)
