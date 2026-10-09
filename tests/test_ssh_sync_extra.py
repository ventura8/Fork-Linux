"""``_directive_parts`` splits ssh config lines exactly like the former keyword regex."""

from __future__ import annotations

import re

import pytest

from fork_linux import ssh_sync

# The pattern ``_directive_parts`` replaced (it backtracked super-linearly on long lines).
_FORMER = re.compile(r"^(\s*)([A-Za-z][A-Za-z0-9]*)(?:\s*=\s*|\s+)(.*?)\s*$")

SAMPLES = (
    "Host example",
    "  HostName  example.com  ",
    "\tIdentityFile=~/.ssh/id_ed25519",
    "Port = 22",
    "Port =",
    "Port=",
    "Key value=x",
    "Host",
    "Host  ",
    "Host-name x",
    "  =foo",
    "1abc def",
    "ProxyJump bastion",
    "  Ciphers  aes128-ctr, aes256-ctr\t",
    "Match exec \"true\"",
)


@pytest.mark.parametrize("line", SAMPLES)
def test_same_split_as_the_former_regex(line: str) -> None:
    former = _FORMER.match(line)
    assert ssh_sync._directive_parts(line) == (None if former is None else former.groups())


def test_blank_and_comment_lines_are_not_directives() -> None:
    assert ssh_sync._directive_parts("   ") is None
    assert ssh_sync._directive_parts("  # Host x") is None
