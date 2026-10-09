"""Tests for fork_linux.versions: dotted version parsing and ordering."""

from __future__ import annotations

import pytest

from fork_linux import versions
from fork_linux.versions import Version


@pytest.mark.parametrize(
    ("raw", "parts"),
    [
        ("2.23.2", (2, 23, 2)),
        ("11.0", (11,)),
        ("11", (11,)),
        ("0", (0,)),
        ("0.0.0", (0,)),
        ("v1.2", (1, 2)),
        ("  1.2.3-rc1  ", (1, 2, 3)),
        ("2.23.2-beta", (2, 23, 2)),
        ("1.10.0.0", (1, 10)),
    ],
)
def test_parts_ignore_suffixes_and_trailing_zeros(raw: str, parts: tuple[int, ...]) -> None:
    assert Version(raw).parts == parts


@pytest.mark.parametrize("raw", ["", "  ", "beta", "x1.2", "-1", ".5", "V1.0"])
def test_rejects_strings_without_a_leading_number(raw: str) -> None:
    with pytest.raises(ValueError, match="not a version"):
        Version(raw)
    assert versions.is_valid(raw) is False


def test_raw_is_kept_for_display() -> None:
    version = versions.parse(" 2.23.2-beta ")
    assert isinstance(version, Version)
    assert str(version) == "2.23.2-beta"
    assert version.raw == "2.23.2-beta"
    assert repr(version) == "Version('2.23.2-beta')"


def test_equality_and_hash_follow_numeric_parts() -> None:
    assert Version("11") == Version("11.0.0")
    assert Version("2.23.2-beta") == Version("2.23.2")
    assert hash(Version("11")) == hash(Version("11.0"))
    assert len({Version("1.0"), Version("1"), Version("1.0.0-rc")}) == 1
    assert Version("1.2") != Version("1.2.1")


def test_never_equal_to_other_types() -> None:
    assert (Version("1.0") == "1.0") is False
    assert Version("1.0") != 1
    assert Version("1.0").__eq__("1.0") is NotImplemented


def test_ordering_is_numeric_not_lexical() -> None:
    ordered = [Version(v) for v in ("1.2", "1.10", "1.9.9", "2", "1.2.0.1", "10.0")]
    assert [str(v) for v in sorted(ordered)] == ["1.2", "1.2.0.1", "1.9.9", "1.10", "2", "10.0"]
    assert Version("2.23.10") > Version("2.23.9")
    assert Version("2.23.2") >= Version("2.23.2-rc1")
    assert Version("9.0") <= Version("11.0")


@pytest.mark.parametrize(
    ("a", "b", "result"),
    [("2.23.1", "2.23.2", -1), ("2.23.2", "2.23.2.0", 0), ("11.0", "9.22", 1), ("v3", "3.0.0-final", 0)],
)
def test_compare(a: str, b: str, result: int) -> None:
    assert versions.compare(a, b) == result
    assert versions.compare(b, a) == -result


def test_compare_rejects_invalid_input() -> None:
    with pytest.raises(ValueError, match="not a version"):
        versions.compare("1.0", "latest")


@pytest.mark.parametrize("raw", ["1", "1.2.3", "v2.0", " 3.4-rc"])
def test_is_valid(raw: str) -> None:
    assert versions.is_valid(raw) is True


def test_ordering_against_non_version_raises_type_error() -> None:
    with pytest.raises(TypeError):
        _ = Version("1.0") < "0.9"  # type-mismatch must not silently compare
