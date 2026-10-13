"""Tests for fork_linux.config_edit: comment-preserving INI set/unset (ported from Ubuntu-Hello)."""

from __future__ import annotations

import configparser
import os
import stat
from pathlib import Path

import pytest

from fork_linux import config_edit

SAMPLE = """# top comment
[core]
# keep
disabled = false

[notifications]
# card on/off
enabled = true
details = false

[rubberstamps]
enabled = false
"""


@pytest.fixture
def cfg(tmp_path: Path) -> Path:
    path = tmp_path / "config.ini"
    path.write_text(SAMPLE, encoding="utf-8")
    return path


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _temp_files(directory: Path) -> list[str]:
    return [name for name in os.listdir(directory) if name.endswith(".tmp")]


# --- set_option ---------------------------------------------------------------


def test_set_replaces_in_the_right_section_only(cfg: Path) -> None:
    config_edit.set_option(cfg, "notifications", "enabled", "false")
    text = _read(cfg)
    assert "[notifications]\n# card on/off\nenabled = false\n" in text
    assert "[rubberstamps]\nenabled = false\n" in text
    assert "# top comment" in text
    assert "# keep" in text


def test_set_matches_keys_case_insensitively_and_colon_delimiters(tmp_path: Path) -> None:
    path = tmp_path / "c.ini"
    path.write_text("[wine]\nProvider: system\n", encoding="utf-8")
    config_edit.set_option(path, "wine", "provider", "managed")
    assert _read(path) == "[wine]\nprovider = managed\n"


def test_set_appends_missing_key_to_section(cfg: Path) -> None:
    config_edit.set_option(cfg, "notifications", "sound", "false")
    assert "details = false\nsound = false\n\n[rubberstamps]" in _read(cfg)


def test_set_appends_to_last_section(cfg: Path) -> None:
    config_edit.set_option(cfg, "rubberstamps", "extra", "1")
    assert _read(cfg).endswith("[rubberstamps]\nenabled = false\nextra = 1\n")


def test_set_into_empty_section_goes_right_after_header(tmp_path: Path) -> None:
    path = tmp_path / "c.ini"
    path.write_text("[a]\n\n[b]\nx = 1\n", encoding="utf-8")
    config_edit.set_option(path, "a", "k", "v")
    assert _read(path) == "[a]\nk = v\n\n[b]\nx = 1\n"


def test_set_places_value_after_commented_default(tmp_path: Path) -> None:
    path = tmp_path / "c.ini"
    path.write_text(
        "[wine]\n# Which Wine.\n# provider = managed\n\n# Debug.\n; debug: -all\n\n[fork]\n",
        encoding="utf-8",
    )
    config_edit.set_option(path, "wine", "provider", "system")
    config_edit.set_option(path, "wine", "debug", "+seh")
    assert _read(path) == (
        "[wine]\n# Which Wine.\n# provider = managed\nprovider = system\n\n"
        "# Debug.\n; debug: -all\ndebug = +seh\n\n[fork]\n"
    )


def test_commented_default_in_another_section_is_ignored(tmp_path: Path) -> None:
    path = tmp_path / "c.ini"
    path.write_text("[a]\n# k = 1\n\n[b]\nx = 1\n", encoding="utf-8")
    config_edit.set_option(path, "b", "k", "2")
    assert _read(path) == "[a]\n# k = 1\n\n[b]\nx = 1\nk = 2\n"


def test_set_appends_missing_section(cfg: Path) -> None:
    config_edit.set_option(cfg, "brand_new", "key", "1")
    assert _read(cfg).endswith("[rubberstamps]\nenabled = false\n\n[brand_new]\nkey = 1\n")


def test_set_missing_section_after_trailing_blank_line_adds_no_second_blank(tmp_path: Path) -> None:
    path = tmp_path / "c.ini"
    path.write_text("[core]\nx = 1\n\n", encoding="utf-8")
    config_edit.set_option(path, "new", "k", "v")
    assert _read(path) == "[core]\nx = 1\n\n[new]\nk = v\n"


def test_set_appends_section_when_file_lacks_trailing_newline(tmp_path: Path) -> None:
    path = tmp_path / "c.ini"
    path.write_text("[core]\nx = 1", encoding="utf-8")
    config_edit.set_option(path, "notifications", "enabled", "true")
    assert _read(path) == "[core]\nx = 1\n\n[notifications]\nenabled = true\n"


def test_set_appends_key_when_file_lacks_trailing_newline(tmp_path: Path) -> None:
    path = tmp_path / "c.ini"
    path.write_text("[core]\nx = 1", encoding="utf-8")
    config_edit.set_option(path, "core", "y", "2")
    assert _read(path) == "[core]\nx = 1\ny = 2\n"


def test_set_creates_a_missing_file_private(tmp_path: Path) -> None:
    path = tmp_path / "sub" / "config.ini"
    config_edit.set_option(path, "wine", "provider", "system")
    assert _read(path) == "[wine]\nprovider = system\n"
    assert stat.S_IMODE(path.stat().st_mode) == config_edit.DEFAULT_MODE


def test_set_section_header_with_trailing_comment(tmp_path: Path) -> None:
    path = tmp_path / "c.ini"
    path.write_text("[wine] # the Wine section\nprovider = system\n", encoding="utf-8")
    config_edit.set_option(path, "wine", "provider", "managed")
    assert _read(path) == "[wine] # the Wine section\nprovider = managed\n"


def test_set_keeps_indented_comment_after_replaced_key(tmp_path: Path) -> None:
    """An indented "#"/";" line is a comment, not a value continuation."""
    path = tmp_path / "config.ini"
    path.write_text(
        "[rubberstamps]\n"
        "stamp_rules = hotkey 5s failsafe\n"
        "\t# indented note about the rule\n"
        "\t; semicolon note too\n"
        "other = 1\n",
        encoding="utf-8",
    )
    config_edit.set_option(path, "rubberstamps", "stamp_rules", "nod 10s failsafe")
    out = _read(path)
    assert "stamp_rules = nod 10s failsafe" in out
    assert "# indented note about the rule" in out
    assert "; semicolon note too" in out
    assert "other = 1" in out


def test_set_drops_continuations_that_follow_an_indented_comment(tmp_path: Path) -> None:
    """Preserving the comment must not end the skip of the old value."""
    path = tmp_path / "config.ini"
    path.write_text(
        "[rubberstamps]\n"
        "stamp_rules = hotkey 5s failsafe\n"
        "\tnod 10s failsafe\n"
        "\t# why the second rule exists\n"
        "\tblink 3s faildeadly\n"
        "other = 1\n",
        encoding="utf-8",
    )
    config_edit.set_option(path, "rubberstamps", "stamp_rules", "nod 4s failsafe")
    out = _read(path)
    assert "stamp_rules = nod 4s failsafe" in out
    assert "# why the second rule exists" in out
    assert "hotkey 5s failsafe" not in out
    assert "nod 10s failsafe" not in out
    assert "blink 3s faildeadly" not in out
    assert "other = 1" in out


def test_set_replaces_a_multi_line_value_without_leaving_leftovers(tmp_path: Path) -> None:
    path = tmp_path / "config.ini"
    path.write_text(
        "[rubberstamps]\nenabled = false\n"
        "stamp_rules =\n\tnod\t5s\tfailsafe     min_distance=12\n"
        "\n[video]\ncertainty = 4.2\n",
        encoding="utf-8",
    )
    config_edit.set_option(path, "rubberstamps", "stamp_rules", "nod\t5s\tfailsafe\tmin_distance=12")
    text = _read(path)
    assert text.count("nod") == 1
    assert "certainty = 4.2" in text
    parser = configparser.ConfigParser()
    parser.read(path)
    assert parser.get("rubberstamps", "stamp_rules").split() == ["nod", "5s", "failsafe", "min_distance=12"]


def test_set_only_edits_the_first_of_duplicate_sections(tmp_path: Path) -> None:
    path = tmp_path / "c.ini"
    path.write_text("[a]\nk = 1\n[a]\nk = 2\n", encoding="utf-8")
    config_edit.set_option(path, "a", "k", "3")
    assert _read(path) == "[a]\nk = 3\n[a]\nk = 2\n"


def test_section_names_are_case_sensitive(tmp_path: Path) -> None:
    path = tmp_path / "c.ini"
    path.write_text("[Wine]\nprovider = system\n", encoding="utf-8")
    config_edit.set_option(path, "wine", "provider", "managed")
    assert _read(path) == "[Wine]\nprovider = system\n\n[wine]\nprovider = managed\n"


def test_indented_key_lines_are_continuations_not_keys(tmp_path: Path) -> None:
    path = tmp_path / "c.ini"
    path.write_text("[a]\nlist = x\n  k = inside value\n", encoding="utf-8")
    config_edit.set_option(path, "a", "k", "v")
    assert _read(path) == "[a]\nlist = x\n  k = inside value\nk = v\n"


def test_set_preserves_mode(cfg: Path) -> None:
    os.chmod(cfg, 0o640)
    config_edit.set_option(cfg, "core", "disabled", "true")
    assert stat.S_IMODE(cfg.stat().st_mode) == 0o640
    assert _temp_files(cfg.parent) == []


def test_set_edits_the_target_of_a_symlink(tmp_path: Path) -> None:
    real = tmp_path / "dotfiles" / "config.ini"
    real.parent.mkdir()
    real.write_text("[core]\nx = 1\n", encoding="utf-8")
    link = tmp_path / "config.ini"
    link.symlink_to(real)
    config_edit.set_option(link, "core", "x", "2")
    assert link.is_symlink()
    assert _read(real) == "[core]\nx = 2\n"


def test_failed_write_leaves_original_and_no_temp_file(cfg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(src: str, dst: str) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("fork_linux.fsutil.os.replace", boom)
    with pytest.raises(OSError):
        config_edit.set_option(cfg, "core", "disabled", "true")
    assert _temp_files(cfg.parent) == []
    assert "disabled = false" in _read(cfg)


def test_set_coerces_value_to_str(cfg: Path) -> None:
    config_edit.set_option(cfg, "core", "count", 3)
    assert "count = 3\n" in _read(cfg)


@pytest.mark.parametrize(
    ("section", "key", "value"),
    [
        ("", "k", "v"),
        (" a", "k", "v"),
        ("a]", "k", "v"),
        ("a\nb", "k", "v"),
        ("a", "", "v"),
        ("a", "bad key", "v"),
        ("a", "k=1", "v"),
        ("a", "k", "line1\nline2"),
        ("a", "k", "x\r[evil]"),
    ],
)
def test_set_rejects_names_and_values_that_would_corrupt_the_file(
    cfg: Path, section: str, key: str, value: str
) -> None:
    before = _read(cfg)
    with pytest.raises(ValueError):
        config_edit.set_option(cfg, section, key, value)
    assert _read(cfg) == before


# --- unset_option ---------------------------------------------------------------


def test_unset_removes_key_and_its_continuations_but_keeps_comments(tmp_path: Path) -> None:
    path = tmp_path / "c.ini"
    path.write_text(
        "[a]\n# about k\nk = 1\n  more\n  # inner note\n  tail\nother = 2\n[b]\nk = 3\n",
        encoding="utf-8",
    )
    assert config_edit.unset_option(path, "a", "k") is True
    assert _read(path) == "[a]\n# about k\n  # inner note\nother = 2\n[b]\nk = 3\n"


def test_unset_missing_key_leaves_file_untouched(cfg: Path) -> None:
    before = cfg.stat().st_mtime_ns
    assert config_edit.unset_option(cfg, "core", "nope") is False
    assert config_edit.unset_option(cfg, "nosuchsection", "disabled") is False
    assert cfg.stat().st_mtime_ns == before
    assert _read(cfg) == SAMPLE


def test_unset_missing_file_is_a_no_op(tmp_path: Path) -> None:
    path = tmp_path / "absent.ini"
    assert config_edit.unset_option(path, "a", "k") is False
    assert not path.exists()


def test_unset_validates_names(cfg: Path) -> None:
    with pytest.raises(ValueError):
        config_edit.unset_option(cfg, "core", "bad key")


def test_set_then_unset_round_trips_to_the_original(cfg: Path) -> None:
    config_edit.set_option(cfg, "core", "fresh", "1")
    assert config_edit.unset_option(cfg, "core", "fresh") is True
    assert _read(cfg) == SAMPLE


# --- pure helpers ---------------------------------------------------------------


def test_split_lines_only_splits_on_newline() -> None:
    assert config_edit.split_lines("a\nb c\r\nd") == ["a\n", "b c\r\n", "d"]
    assert config_edit.split_lines("") == []
    assert config_edit.split_lines("x\n") == ["x\n"]


def test_rewrite_set_on_empty_input() -> None:
    assert config_edit.rewrite_set([], "s", "k", "v") == ["[s]\n", "k = v\n"]


def test_rewrite_unset_does_not_mutate_input() -> None:
    lines = ["[s]\n", "k = v\n"]
    out, removed = config_edit.rewrite_unset(lines, "s", "k")
    assert removed is True
    assert out == ["[s]\n"]
    assert lines == ["[s]\n", "k = v\n"]


# --- configparser-faithful line classification ------------------------------------


def _parsed(text: str) -> dict[str, dict[str, str]]:
    parser = configparser.ConfigParser(interpolation=None)
    parser.read_string(text)
    return {section: dict(parser.items(section, raw=True)) for section in parser.sections()}


def _set_text(text: str, section: str, key: str, value: str) -> str:
    return "".join(config_edit.rewrite_set(config_edit.split_lines(text), section, key, value))


def _unset_text(text: str, section: str, key: str) -> tuple[str, bool]:
    lines, removed = config_edit.rewrite_unset(config_edit.split_lines(text), section, key)
    return "".join(lines), removed


TRICKY = [
    "[a]\n  k = 1\n",
    "[a]\n  k = 1\n  z = 2\n",
    "[a]\nk = 1,\n    b\n\n    c\nz = 1\n",
    "[a]\n# k = 1\n  z = 3\n",
    "[a]\nz = 1\n  [b]\n",
    "[a]\nlist = x\n  k = inside value\n",
    "[a]\nk = 1\n# col-0 note\n    more\nz = 2\n",
    "[a]\n\tk: 1\n\t# note\n[b]\nk = 2\n",
    "[a] ; trailing [x]\nk = 1\n",
    "[a]\nz = 1\n\n\n[b]\n  y = 2\n",
    "[a]\n    z = 1\n  [b]\nx = 1\n",
    "[a]\n  [b]\nx = 1\n",
    "[a]\n\t\tz = 1\n\t[b]\n",
]


@pytest.mark.parametrize("text", TRICKY)
def test_set_never_changes_other_values_or_duplicates_options(text: str) -> None:
    before = _parsed(text)
    after = _parsed(_set_text(text, "a", "k", "new"))
    expected = {section: dict(options) for section, options in before.items()}
    expected.setdefault("a", {})["k"] = "new"
    assert after == expected


@pytest.mark.parametrize("text", TRICKY)
def test_unset_never_changes_other_values(text: str) -> None:
    before = _parsed(text)
    out, removed = _unset_text(text, "a", "k")
    after = _parsed(out)
    assert removed is ("k" in before.get("a", {}))
    expected = {section: dict(options) for section, options in before.items()}
    expected.get("a", {}).pop("k", None)
    assert after == expected


def test_indented_option_is_replaced_in_place_keeping_its_indentation() -> None:
    assert _set_text("[a]\n  k = 1\n  z = 2\n", "a", "k", "9") == "[a]\n  k = 9\n  z = 2\n"


def test_blank_lines_inside_a_value_do_not_end_it() -> None:
    text = "[a]\nk = 1,\n    b\n\n    c\nz = 1\n"
    assert _set_text(text, "a", "k", "2") == "[a]\nk = 2\nz = 1\n"
    assert _unset_text(text, "a", "k") == ("[a]\nz = 1\n", True)


def test_unindented_comment_inside_a_value_is_kept_and_the_value_fully_replaced() -> None:
    text = "[a]\nk = 1\n# col-0 note\n    more\nz = 2\n"
    assert _set_text(text, "a", "k", "2") == "[a]\nk = 2\n# col-0 note\nz = 2\n"


def test_new_option_copies_the_indentation_of_the_next_line() -> None:
    """An unindented line there would swallow the indented option below it as a continuation."""
    assert _set_text("[a]\n# k = 1\n  z = 3\n", "a", "k", "2") == "[a]\n# k = 1\n  k = 2\n  z = 3\n"


def test_commented_default_inside_a_value_is_not_used() -> None:
    text = "[a]\nz = 1\n# k = 0\n    more\n# k = 1\n\n[b]\n"
    assert _set_text(text, "a", "k", "2") == "[a]\nz = 1\n# k = 0\n    more\n# k = 1\nk = 2\n\n[b]\n"


def test_new_option_before_an_indented_header_is_indented_like_it() -> None:
    text = "[a]\n    z = 1\n  [b]\nx = 1\n"
    assert _parsed(text) == {"a": {"z": "1"}, "b": {"x": "1"}}
    out = _set_text(text, "a", "k", "2")
    assert out == "[a]\n    z = 1\n  k = 2\n  [b]\nx = 1\n"
    assert _parsed(out) == {"a": {"z": "1", "k": "2"}, "b": {"x": "1"}}
    empty = "[a]\n  [b]\nx = 1\n"
    assert _set_text(empty, "a", "k", "2") == "[a]\n  k = 2\n  [b]\nx = 1\n"


def test_indented_header_is_a_continuation_when_configparser_says_so() -> None:
    text = "[a]\nz = 1\n  [b]\n"
    assert _set_text(text, "a", "k", "2") == "[a]\nz = 1\n  [b]\nk = 2\n"


def test_lines_before_the_first_section_and_bogus_lines_are_left_alone() -> None:
    text = "stray = 1\n[a]\nnot an option\n= no name\nk = 1\n"
    assert _set_text(text, "a", "k", "2") == "stray = 1\n[a]\nnot an option\n= no name\nk = 2\n"
