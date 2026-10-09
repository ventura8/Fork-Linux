"""fork-linux(1) and fork(1): generated from the parser, covering every command, credits included."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from fixtures import gen_data

from fork_linux import credits
from fork_linux.errors import ExitCode

ROOT = Path(__file__).resolve().parents[1]
PAGES = {"fork-linux": ROOT / "data" / "man" / "fork-linux.1", "fork": ROOT / "data" / "man" / "fork.1"}


gen = gen_data.load()
TREE = gen.load_tree()


def _plain(text: str) -> str:
    return text.replace("\\-", "-").replace("\\e", "\\").replace("\\fB", "").replace("\\fI", "").replace("\\fR", "")


@pytest.mark.parametrize("page", sorted(PAGES))
def test_committed_page_is_current(page: str) -> None:
    assert PAGES[page].read_text(encoding="utf-8") == gen.render_man(page, TREE), "run scripts/gen-data.py write"


def test_fork_linux_page_covers_every_command_and_option() -> None:
    text = _plain(PAGES["fork-linux"].read_text(encoding="utf-8"))
    for node in gen.all_nodes(TREE)[1:]:
        assert f".SS {node.key}" in text or f"alias: {node.path[-1]}" in text or node.path[-1] in text
        for option in node.options:
            for flag in option.flags:
                assert flag in text, f"{node.key} {flag} missing"
    for code in ExitCode:
        assert f"\n{int(code)}\n" in text
        assert code.name in text


@pytest.mark.parametrize("page", sorted(PAGES))
def test_credits_and_disclaimer(page: str) -> None:
    text = _plain(PAGES[page].read_text(encoding="utf-8"))
    assert credits.developers_text() in text
    assert "https://git-fork.com/buy" in text
    assert "NOT affiliated" in text
    assert ".SH SEE ALSO" in text


def test_fork_page_lists_the_shortcut_options() -> None:
    text = _plain(PAGES["fork"].read_text(encoding="utf-8"))
    for flag in ("--prefix", "--offline", "--allow-root", "--help", "--version"):
        assert flag in text
    assert "--json" not in text


def test_unknown_page_is_refused() -> None:
    with pytest.raises(SystemExit):
        gen.render_man("git", TREE)


def test_roff_escaping() -> None:
    assert gen.roff(".x -y \\z") == "\\&.x \\-y \\ez"


@pytest.mark.parametrize("page", sorted(PAGES))
def test_groff_renders_without_warnings(page: str) -> None:
    if shutil.which("groff") is None:
        pytest.skip("groff is not installed")
    result = subprocess.run(
        ["groff", "-man", "-Tutf8", "-ww", "-z", str(PAGES[page])], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == "", result.stderr
