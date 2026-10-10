"""Tests for fork_linux.fork_data: Fork's ForkData\\repositories.toml (source folder, repository list)."""

from __future__ import annotations

import os
import stat
from datetime import datetime, timezone
from pathlib import Path

import pytest

from fork_linux import fork_data
from fork_linux.errors import IntegrityFailed

# The layout Fork 2.23.2 writes (QA install, 2026-10-09).
REAL = (
    "source_dirs = ['C:\\users\\tester\\']\n"
    "scan_depth = 5\n"
    "ignore = []\n"
    "\n"
    "[[repository]]\n"
    "path = 'Z:\\home\\tester\\src\\app'\n"
    "opened = 1791549485\n"
    "\n"
    "[[repository]]\n"
    "path = \"Z:\\\\home\\\\tester\\\\it's\"\n"
    "opened = 1791550967\n"
    "\n"
    "[other]\n"
    "path = 'ignored'\n"
)
HOME = "Z:\\home\\tester"


def test_parse_strings() -> None:
    assert fork_data.parse_strings("['a', \"b\\\\c\", 'd\"']") == ["a", "b\\c", 'd"']
    assert fork_data.parse_strings("[]") == []
    assert fork_data.parse_strings("[ 'a' , ]") == ["a"]
    assert fork_data.parse_strings('["\\t\\n"]') == ["\t\n"]
    for bad in ("'a'", "[a]", "['a' 'b']", '["\\u0041"]', "['a'"):
        assert fork_data.parse_strings(bad) is None


@pytest.mark.parametrize(
    ("text", "quoted"),
    [
        ("Z:\\home\\u", "'Z:\\home\\u'"),
        ("it's", '"it\'s"'),
        ('a"b\\c', "'a\"b\\c'"),
        ("x'\\y\"", '"x\'\\\\y\\""'),
        ("tab\there", '"tab\\u0009here"'),
    ],
)
def test_quote_round_trips(text: str, quoted: str) -> None:
    assert fork_data.quote(text) == quoted
    if "\\u" not in quoted:
        assert fork_data.parse_strings(f"[{quoted}]") == [text]


def test_source_dirs_and_repositories() -> None:
    assert fork_data.source_dirs(REAL) == ["C:\\users\\tester\\"]
    assert fork_data.repositories(REAL) == ["Z:\\home\\tester\\src\\app", "Z:\\home\\tester\\it's"]
    assert fork_data.source_dirs("scan_depth = 5\n[x]\nsource_dirs = ['a']\n") is None
    assert fork_data.source_dirs("source_dirs = [\n  'a',\n]\n") is None
    assert fork_data.repositories("[[repository]]\npath = [1]\nnot a key\n") == []


@pytest.mark.parametrize(
    ("dirs", "default"),
    [
        (["C:\\users\\tester\\"], True),
        (["c:\\Users\\Tester"], True),
        (["C:\\users\\other"], False),
        (["C:\\users\\tester", "D:\\x"], False),
        (None, False),
    ],
)
def test_is_default(dirs: list[str] | None, default: bool) -> None:
    assert fork_data.is_default(dirs, "tester") is default


def test_with_source_dirs_keeps_everything_else() -> None:
    crlf = REAL.replace("\n", "\r\n")
    new = fork_data.with_source_dirs(crlf, [HOME])
    assert new == crlf.replace("['C:\\users\\tester\\']", f"['{HOME}']", 1)
    indented = "  source_dirs=['a']\nscan_depth = 5"
    assert fork_data.with_source_dirs(indented, ["b"]) == "  source_dirs = ['b']\nscan_depth = 5"
    with pytest.raises(ValueError, match="source_dirs"):
        fork_data.with_source_dirs("scan_depth = 5\n", ["b"])
    with pytest.raises(ValueError, match="source_dirs"):
        fork_data.with_source_dirs("source_dirs = [1]\n", ["b"])


def test_read_text_refuses_links_and_huge_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    file = tmp_path / "repositories.toml"
    assert fork_data.read_text(file) is None
    target = tmp_path / "elsewhere.toml"
    target.write_text(REAL, encoding="utf-8")
    file.symlink_to(target)
    with pytest.raises(IntegrityFailed, match="not a regular file"):
        fork_data.read_text(file)
    file.unlink()
    file.write_bytes(b"source_dirs = ['\xff']\n")
    assert fork_data.read_text(file) == "source_dirs = ['\udcff']\n"
    monkeypatch.setattr(fork_data, "MAX_BYTES", 4)
    with pytest.raises(IntegrityFailed, match="larger than"):
        fork_data.read_text(file)


def test_ensure_source_dirs_creates_a_missing_file(tmp_path: Path) -> None:
    forkdata = tmp_path / "ForkData"
    backups = tmp_path / "backups"
    assert fork_data.ensure_source_dirs(forkdata, user="tester", home_win=HOME, backup_dir=backups)
    file = fork_data.path(forkdata)
    # Exactly what Fork 2.23.2 writes: it rejects a file without any of the four keys.
    expected = f"source_dirs = ['{HOME}']\nscan_depth = 5\nignore = []\nrepository = []\n"
    assert file.read_text(encoding="utf-8") == expected
    assert stat.S_IMODE(file.stat().st_mode) == fork_data.FILE_MODE
    assert not backups.exists()
    # Already ours: nothing to do.
    assert not fork_data.ensure_source_dirs(forkdata, user="tester", home_win=HOME, backup_dir=backups)


def test_ensure_source_dirs_replaces_only_the_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    forkdata = tmp_path / "ForkData"
    forkdata.mkdir()
    file = fork_data.path(forkdata)
    file.write_bytes(REAL.encode("utf-8") + b"# \xff\n")
    backups = tmp_path / "backups"
    stamps = iter(range(10))
    monkeypatch.setattr(
        fork_data, "_utcnow", lambda: datetime(2026, 10, 9, 12, 0, next(stamps), tzinfo=timezone.utc)
    )
    assert fork_data.ensure_source_dirs(forkdata, user="tester", home_win=HOME, backup_dir=backups)
    assert file.read_bytes() == REAL.replace("'C:\\users\\tester\\'", f"'{HOME}'").encode() + b"# \xff\n"
    saved = fork_data.list_backups(backups)
    assert [path.name for path in saved] == ["repositories.toml.20261009T120000.000000Z"]
    assert saved[0].read_bytes().startswith(b"source_dirs = ['C:\\users\\tester\\']")
    assert stat.S_IMODE(saved[0].stat().st_mode) == fork_data.BACKUP_MODE
    # A folder the user chose is kept.
    chosen = fork_data.new_file("D:\\src")
    file.write_text(chosen, encoding="utf-8")
    assert not fork_data.ensure_source_dirs(forkdata, user="tester", home_win=HOME, backup_dir=backups)
    assert file.read_text(encoding="utf-8") == chosen
    file.write_text(REAL.replace("'C:\\users\\tester\\'", "'D:\\src'"), encoding="utf-8")
    assert not fork_data.ensure_source_dirs(forkdata, user="tester", home_win=HOME, backup_dir=backups)
    # Backups rotate.
    for _ in range(4):
        file.write_text(REAL, encoding="utf-8")
        fork_data.ensure_source_dirs(forkdata, user="tester", home_win=HOME, backup_dir=backups)
    assert len(fork_data.list_backups(backups)) == fork_data.DEFAULT_KEEP


def test_new_file_matches_forks_own_layout() -> None:
    assert fork_data.new_file("Z:\\home\\it's") == (
        'source_dirs = ["Z:\\\\home\\\\it\'s"]\nscan_depth = 5\nignore = []\nrepository = []\n'
    )
    assert fork_data.KEYS == ("source_dirs", "scan_depth", "ignore", "repository")
    text = fork_data.new_file(HOME)
    assert fork_data.source_dirs(text) == [HOME]
    assert fork_data.repositories(text) == []


@pytest.mark.parametrize(
    ("legacy", "folder"),
    [
        # The one-line file fork-linux 1.0.0 wrote over Fork's default: the home goes in.
        ("source_dirs = ['C:\\users\\tester\\']\n", HOME),
        # ... or with a folder already chosen: the folder is kept.
        ("source_dirs = ['Z:\\home\\tester']\n", HOME),
        ("source_dirs = ['D:\\src']\n", "D:\\src"),
    ],
)
def test_ensure_source_dirs_completes_the_file_fork_linux_1_0_0_wrote(
    tmp_path: Path, legacy: str, folder: str
) -> None:
    forkdata = tmp_path / "ForkData"
    forkdata.mkdir()
    file = fork_data.path(forkdata)
    file.write_text(legacy, encoding="utf-8")
    backups = tmp_path / "backups"
    assert fork_data.ensure_source_dirs(forkdata, user="tester", home_win=HOME, backup_dir=backups)
    assert file.read_text(encoding="utf-8") == fork_data.new_file(folder)
    [saved] = fork_data.list_backups(backups)
    assert saved.read_text(encoding="utf-8") == legacy
    assert not fork_data.ensure_source_dirs(forkdata, user="tester", home_win=HOME, backup_dir=backups)


@pytest.mark.parametrize(
    "text",
    [
        "source_dirs = ['a', 'b']\n",
        "source_dirs = []\n",
        "source_dirs = ['D:\\src']\n\n",
        "source_dirs = [ 'D:\\src' ]\n",
        "source_dirs = ['D:\\src']",
        "scan_depth = 5\n",
    ],
)
def test_only_the_exact_one_line_file_counts_as_legacy(text: str) -> None:
    assert fork_data._legacy(text) is None


def test_list_backups_of_a_missing_dir(tmp_path: Path) -> None:
    assert fork_data.list_backups(tmp_path / "none") == []


def test_utcnow_is_aware() -> None:
    assert fork_data._utcnow().tzinfo is not None
    assert os.path.basename(str(fork_data.path(Path("/x")))) == "repositories.toml"
