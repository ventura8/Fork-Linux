"""Contract: README.md's CLI table names every command and option of the parser, and doctor's check count."""

from __future__ import annotations

import re
from pathlib import Path

from fixtures import gen_data

from fork_linux import doctor

ROOT = Path(__file__).resolve().parents[1]
gen = gen_data.load()
TREE = gen.load_tree()


def _cli_section() -> str:
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    start = text.index("\n## CLI\n")
    return text[start : text.index("\n## ", start + 1)]


def test_every_command_and_option_is_in_the_readme() -> None:
    section = _cli_section()
    global_set = set(gen.global_flags(TREE)) | {"-h", "--help", "-V", "--version"}
    missing = [
        f"{node.key} {flag}"
        for node in gen.walk(TREE)[1:]
        for option in node.options
        for flag in option.flags
        if flag.startswith("--") and flag not in global_set and flag not in section
    ]
    missing += [
        name for name, _help, _node in TREE.subcommands if f"`{name}" not in section and f" {name}" not in section
    ]
    assert missing == [], f"README.md's CLI table lacks: {missing}"
    for option in TREE.options:
        if not set(option.flags) & {"--help", "--version"}:
            assert any(flag in section for flag in option.flags), option.flags


def test_readme_counts_every_doctor_check() -> None:
    [count] = re.findall(r"\| (\d+) health checks", _cli_section())
    assert int(count) == len(doctor.CHECKS)
