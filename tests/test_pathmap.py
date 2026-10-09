"""Tests for fork_linux.pathmap: dosdevices-based Unix <-> Windows path conversion."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from fork_linux.errors import NotSetUpError, UsageError
from fork_linux.pathmap import PathMap


def _make_prefix(root: Path, data_dir: Path) -> Path:
    """Build a fake prefix whose dosdevices look like a real Wine prefix's."""
    prefix = root / "prefix"
    (prefix / "drive_c" / "users" / "tester").mkdir(parents=True)
    dosdevices = prefix / "dosdevices"
    dosdevices.mkdir()
    os.symlink("../drive_c", dosdevices / "c:")
    os.symlink("/", dosdevices / "z:")
    os.symlink(str(data_dir), dosdevices / "d:")
    os.symlink("/dev/null", dosdevices / "c::")
    os.symlink("/dev/null", dosdevices / "com1")
    os.symlink(str(root / "missing-cdrom"), dosdevices / "x:")
    os.symlink("../drive_c", dosdevices / "Y:")
    (dosdevices / "unc").mkdir()
    (dosdevices / "e:").mkdir()
    return prefix


@pytest.fixture
def layout(tmp_path: Path) -> tuple[Path, Path, PathMap]:
    """(prefix, data dir, PathMap.from_prefix(prefix)) on a real symlink tree."""
    data = tmp_path / "data"
    data.mkdir()
    prefix = _make_prefix(tmp_path, data)
    return prefix, data, PathMap.from_prefix(prefix)


def test_from_prefix_reads_drive_symlinks(layout: tuple[Path, Path, PathMap]) -> None:
    prefix, data, pmap = layout
    assert pmap.drives() == {
        "c": Path(os.path.realpath(prefix / "drive_c")),
        "d": Path(os.path.realpath(data)),
        "x": Path(os.path.realpath(prefix.parent / "missing-cdrom")),
        "z": Path("/"),
    }


def test_from_prefix_ignores_lookalike_entries(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    prefix = _make_prefix(tmp_path, data)
    evil = tmp_path / "evil"
    evil.mkdir()
    os.symlink(str(evil), prefix / "dosdevices" / "c:\n")
    os.symlink(str(evil), prefix / "dosdevices" / "c: ")
    os.symlink(str(evil), prefix / "dosdevices" / "\u00e9:")
    pmap = PathMap.from_prefix(prefix)
    assert pmap.drives()["c"] == Path(os.path.realpath(prefix / "drive_c"))
    assert set(pmap.drives()) == {"c", "d", "x", "z"}
    assert pmap.unix_to_win(evil / "f").startswith("Z:\\")


def test_from_prefix_dosdevices_is_a_file(tmp_path: Path) -> None:
    (tmp_path / "dosdevices").write_text("not a directory")
    with pytest.raises(NotSetUpError):
        PathMap.from_prefix(tmp_path)


def test_drives_returns_a_copy(layout: tuple[Path, Path, PathMap]) -> None:
    pmap = layout[2]
    pmap.drives().clear()
    assert "c" in pmap.drives()


def test_from_prefix_without_dosdevices(tmp_path: Path) -> None:
    with pytest.raises(NotSetUpError) as info:
        PathMap.from_prefix(tmp_path)
    assert info.value.hint


def test_unix_to_win_inside_drive_c(layout: tuple[Path, Path, PathMap]) -> None:
    prefix, _, pmap = layout
    assert pmap.unix_to_win(prefix / "drive_c" / "users" / "tester") == "C:\\users\\tester"
    assert pmap.unix_to_win(str(prefix / "drive_c")) == "C:\\"
    assert pmap.unix_to_win(f"{prefix}/drive_c/users/../windows/") == "C:\\windows"


def test_unix_to_win_falls_back_to_z(layout: tuple[Path, Path, PathMap]) -> None:
    pmap = layout[2]
    assert pmap.unix_to_win("/home/u/My Repo") == "Z:\\home\\u\\My Repo"
    assert pmap.unix_to_win("/") == "Z:\\"
    assert pmap.unix_to_win("//double/slash") == "Z:\\double\\slash"


def test_unix_to_win_longest_prefix_wins(layout: tuple[Path, Path, PathMap]) -> None:
    _, data, pmap = layout
    assert pmap.unix_to_win(data / "repo") == "D:\\repo"
    sibling = Path(str(data) + "base") / "x"
    assert pmap.unix_to_win(sibling).startswith("Z:\\")
    assert pmap.unix_to_win(sibling).endswith("\\" + data.name + "base\\x")


def test_unix_to_win_relative_paths_use_cwd(
    layout: tuple[Path, Path, PathMap], monkeypatch: pytest.MonkeyPatch
) -> None:
    prefix, _, pmap = layout
    monkeypatch.chdir(prefix / "drive_c" / "users")
    assert pmap.unix_to_win("tester") == "C:\\users\\tester"
    assert pmap.unix_to_win(Path("..") / "windows") == "C:\\windows"


def test_unix_to_win_does_not_resolve_symlinks(tmp_path: Path, layout: tuple[Path, Path, PathMap]) -> None:
    prefix, _, pmap = layout
    alias = tmp_path / "alias-to-c"
    os.symlink(prefix / "drive_c", alias)
    converted = pmap.unix_to_win(alias / "users")
    assert converted.startswith("Z:\\")
    assert converted.endswith("\\alias-to-c\\users")


def test_prefix_reached_through_symlink_matches_both_spellings(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    real = tmp_path / "real"
    real.mkdir()
    prefix = _make_prefix(real, data)
    alias = tmp_path / "alias"
    os.symlink(real, alias)
    pmap = PathMap.from_prefix(alias / "prefix")
    assert pmap.drives()["c"] == Path(os.path.realpath(prefix / "drive_c"))
    assert pmap.unix_to_win(alias / "prefix" / "drive_c" / "x") == "C:\\x"
    assert pmap.unix_to_win(prefix / "drive_c" / "x") == "C:\\x"


def test_unix_to_win_unmapped_path() -> None:
    pmap = PathMap.with_drives({"c": "/srv/prefix/drive_c"})
    with pytest.raises(UsageError) as info:
        pmap.unix_to_win("/etc/hosts")
    assert "no drive maps it" in str(info.value)
    assert info.value.hint


def test_unix_to_win_empty_path() -> None:
    with pytest.raises(UsageError):
        PathMap.with_drives({"z": "/"}).unix_to_win("")


@pytest.mark.parametrize(
    "name", ["back\\slash", "co:lon", "star*", "what?", 'qu"ote', "pi|pe", "<lt", "gt>", "ctl\x01"]
)
def test_unix_to_win_rejects_unrepresentable_names(name: str) -> None:
    pmap = PathMap.with_drives({"z": "/"})
    with pytest.raises(UsageError) as info:
        pmap.unix_to_win(f"/home/u/{name}/repo")
    assert info.value.hint


def test_unix_to_win_rejects_undecodable_names() -> None:
    pmap = PathMap.with_drives({"z": "/"})
    raw = os.fsdecode(b"/home/u/caf\xe9/repo")
    with pytest.raises(UsageError) as info:
        pmap.unix_to_win(raw)
    assert "UTF-8" in str(info.value)
    assert info.value.hint


def test_unix_to_win_errors_do_not_echo_control_sequences() -> None:
    with pytest.raises(UsageError) as info:
        PathMap.with_drives({"z": "/"}).unix_to_win("/tmp/\x1b[31mred")
    assert "\x1b" not in str(info.value)
    with pytest.raises(UsageError) as info:
        PathMap.with_drives({"c": "/srv/c"}).unix_to_win("/tmp/\x1b[31mred")
    assert "\x1b" not in str(info.value)


def test_unix_to_win_collapses_leading_double_slash() -> None:
    pmap = PathMap.with_drives({"d": "//srv/data", "z": "/"})
    assert pmap.drives()["d"] == Path("/srv/data")
    assert pmap.unix_to_win("//srv/data/repo") == "D:\\repo"
    assert pmap.unix_to_win("/srv/data/repo") == "D:\\repo"
    assert pmap.unix_to_win("///srv/data") == "D:\\"


def test_unix_to_win_relative_path_without_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gone = tmp_path / "gone"
    gone.mkdir()
    monkeypatch.chdir(gone)
    gone.rmdir()
    pmap = PathMap.with_drives({"z": "/"})
    try:
        with pytest.raises(UsageError) as info:
            pmap.unix_to_win("relative/repo")
        assert pmap.unix_to_win("/abs/repo") == "Z:\\abs\\repo"
    finally:
        os.chdir(tmp_path)
    assert info.value.hint


def test_unix_to_win_tie_prefers_lowest_letter() -> None:
    pmap = PathMap.with_drives({"e": "/srv", "d": "/srv", "z": "/"})
    assert pmap.unix_to_win("/srv/x") == "D:\\x"


def test_with_drives_normalizes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    pmap = PathMap.with_drives({"C:": "prefix/drive_c/", "Z": Path("/")})
    assert pmap.drives() == {"c": tmp_path / "prefix" / "drive_c", "z": Path("/")}
    assert pmap.unix_to_win(tmp_path / "prefix" / "drive_c" / "a") == "C:\\a"


@pytest.mark.parametrize("key", ["cc", "", "1", "c::", "\u00e4", "c\n"])
def test_with_drives_rejects_bad_letters(key: str) -> None:
    with pytest.raises(ValueError):
        PathMap.with_drives({key: "/"})


@pytest.fixture
def simple() -> PathMap:
    """A map with C: at /srv/prefix/drive_c and Z: at /."""
    return PathMap.with_drives({"c": "/srv/prefix/drive_c", "z": "/"})


@pytest.mark.parametrize(
    ("winpath", "expected"),
    [
        ("C:\\users\\u", "/srv/prefix/drive_c/users/u"),
        ("c:/users/u", "/srv/prefix/drive_c/users/u"),
        ("Z:\\home\\u", "/home/u"),
        ("z:\\home\\u\\", "/home/u"),
        ("C:", "/srv/prefix/drive_c"),
        ("C:\\", "/srv/prefix/drive_c"),
        ("C:\\..\\..\\windows", "/srv/prefix/drive_c/windows"),
        ("C:\\a\\.\\b\\\\c\\..\\d", "/srv/prefix/drive_c/a/b/d"),
        ("\\\\?\\C:\\x", "/srv/prefix/drive_c/x"),
        ("\\\\.\\C:\\x", "/srv/prefix/drive_c/x"),
        ("\\??\\C:\\x", "/srv/prefix/drive_c/x"),
        ("//?/C:/x", "/srv/prefix/drive_c/x"),
        ("\\\\?\\unix\\home\\u", "/home/u"),
        ("\\\\?\\UNIX\\home\\u\\..\\v", "/home/v"),
        ("\\??\\unix\\", "/"),
    ],
)
def test_win_to_unix(simple: PathMap, winpath: str, expected: str) -> None:
    assert simple.win_to_unix(winpath) == Path(expected)


@pytest.mark.parametrize(
    "winpath",
    [
        "\\\\server\\share\\x",
        "//server/share",
        "\\\\?\\UNC\\server\\share",
        "\\\\?\\unix",
        "relative\\path",
        "\\rooted\\no\\drive",
        "C:relative",
        "",
        "Q:\\unmapped",
        "C:\n",
        "C:\\a\x00b",
        "C:\\a\tb",
        "\\\\?\\unix\\home\nx",
    ],
)
def test_win_to_unix_rejects(simple: PathMap, winpath: str) -> None:
    with pytest.raises(UsageError):
        simple.win_to_unix(winpath)


def test_round_trip(layout: tuple[Path, Path, PathMap]) -> None:
    prefix, data, pmap = layout
    real_c = Path(os.path.realpath(prefix / "drive_c"))
    for path in [real_c / "users" / "tester", Path(os.path.realpath(data)) / "repo", Path("/home/u/src/fork")]:
        assert pmap.win_to_unix(pmap.unix_to_win(path)) == path


def test_repr(simple: PathMap) -> None:
    assert repr(simple) == "PathMap({c: '/srv/prefix/drive_c', z: '/'})"
