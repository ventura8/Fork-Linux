"""Tests for fork_linux.fsutil (100% branch coverage gate): writes, deletes, extraction, cloning."""

from __future__ import annotations

import errno
import hashlib
import io
import os
import shutil
import stat
import tarfile
from pathlib import Path

import pytest

from fork_linux import fsutil
from fork_linux.errors import ExitCode, ForkLinuxError, IntegrityFailed, UsageError


def _mode(path: Path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


# --------------------------------------------------------------------------- atomic_write


def test_atomic_write_bytes_and_str(tmp_path: Path) -> None:
    target = tmp_path / "out.bin"
    fsutil.atomic_write(target, b"\x00\x01")
    assert target.read_bytes() == b"\x00\x01"
    assert _mode(target) == 0o600
    fsutil.atomic_write(target, "héllo", mode=0o644)
    assert target.read_text(encoding="utf-8") == "héllo"
    assert _mode(target) == 0o644
    assert sorted(p.name for p in tmp_path.iterdir()) == ["out.bin"], "no temp files left behind"


def test_atomic_write_creates_parents(tmp_path: Path) -> None:
    target = tmp_path / "a" / "b" / "c.txt"
    fsutil.atomic_write(target, "x")
    assert target.read_text(encoding="utf-8") == "x"


def test_atomic_write_replaces_symlink_instead_of_following(tmp_path: Path) -> None:
    victim = tmp_path / "victim.txt"
    victim.write_text("original", encoding="utf-8")
    link = tmp_path / "link.txt"
    link.symlink_to(victim)
    fsutil.atomic_write(link, "new")
    assert not link.is_symlink()
    assert link.read_text(encoding="utf-8") == "new"
    assert victim.read_text(encoding="utf-8") == "original"


def test_atomic_write_failure_keeps_original_and_cleans_temp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "keep.txt"
    target.write_text("old", encoding="utf-8")

    def boom(src: str, dst: object) -> None:
        raise OSError(errno.EIO, "simulated")

    monkeypatch.setattr(fsutil.os, "replace", boom)
    with pytest.raises(OSError, match="simulated"):
        fsutil.atomic_write(target, "new")
    assert target.read_text(encoding="utf-8") == "old"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["keep.txt"]


def test_atomic_write_handles_names_near_name_max(tmp_path: Path) -> None:
    target = tmp_path / ("n" * 250 + ".reg")
    fsutil.atomic_write(target, b"long")
    assert target.read_bytes() == b"long"
    assert [p.name for p in tmp_path.iterdir()] == [target.name]


def test_fsync_dir_ignores_errors(tmp_path: Path) -> None:
    fsutil._fsync_dir(tmp_path / "missing")
    fsutil._fsync_dir(tmp_path)


# --------------------------------------------------------------------------- ensure_dir / sha256


def test_ensure_dir_creates_with_mode(tmp_path: Path) -> None:
    target = tmp_path / "x" / "y"
    assert fsutil.ensure_dir(target) == target
    assert target.is_dir()
    assert _mode(target) == 0o700


def test_ensure_dir_fixes_existing_mode(tmp_path: Path) -> None:
    target = tmp_path / "loose"
    target.mkdir()
    os.chmod(target, 0o777)
    fsutil.ensure_dir(target, 0o700)
    assert _mode(target) == 0o700


def test_ensure_dir_mode_survives_restrictive_umask(tmp_path: Path) -> None:
    old = os.umask(0o077)
    try:
        fsutil.ensure_dir(tmp_path / "shared", 0o755)
    finally:
        os.umask(old)
    assert _mode(tmp_path / "shared") == 0o755


def test_sha256_file(tmp_path: Path) -> None:
    data = os.urandom(5000)
    target = tmp_path / "blob"
    target.write_bytes(data)
    assert fsutil.sha256_file(target, chunk=7) == hashlib.sha256(data).hexdigest()
    empty = tmp_path / "empty"
    empty.write_bytes(b"")
    digest = fsutil.sha256_file(empty)
    assert digest == hashlib.sha256(b"").hexdigest()
    assert digest == digest.lower()


# --------------------------------------------------------------------------- safe_rmtree


def _tree(root: Path, outside: Path) -> None:
    (root / "sub" / "deep").mkdir(parents=True)
    (root / "sub" / "deep" / "f.txt").write_text("x", encoding="utf-8")
    (root / "top.txt").write_text("y", encoding="utf-8")
    (root / "sub" / "out-link").symlink_to(outside)
    (root / "dir-link").symlink_to(outside, target_is_directory=True)


def test_safe_rmtree_removes_tree_without_following_symlinks(xdg: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "precious").write_text("keep", encoding="utf-8")
    root = tmp_path / "victim"
    _tree(root, outside)
    fsutil.safe_rmtree(root)
    assert not root.exists()
    assert (outside / "precious").read_text(encoding="utf-8") == "keep"


def test_safe_rmtree_missing_path_is_a_no_op(xdg: Path, tmp_path: Path) -> None:
    fsutil.safe_rmtree(tmp_path / "nope", marker=tmp_path / "nope" / "marker")


def test_safe_rmtree_deletes_a_plain_file(xdg: Path, tmp_path: Path) -> None:
    target = tmp_path / "file.txt"
    target.write_text("x", encoding="utf-8")
    fsutil.safe_rmtree(target)
    assert not target.exists()


def test_safe_rmtree_refuses_symlink_root(xdg: Path, tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (real / "f").write_text("x", encoding="utf-8")
    link = tmp_path / "link"
    link.symlink_to(real)
    with pytest.raises(IntegrityFailed, match="symbolic link") as info:
        fsutil.safe_rmtree(link)
    assert info.value.exit_code == ExitCode.INTEGRITY_FAILED
    assert (real / "f").exists()


def test_safe_rmtree_refuses_root_home_and_ancestors(xdg: Path) -> None:
    for target in (Path("/"), xdg, xdg.parent, xdg / ".." / xdg.name):
        with pytest.raises(IntegrityFailed, match="refusing"):
            fsutil.safe_rmtree(target)
    assert xdg.is_dir()


def test_safe_rmtree_refuses_forbidden_and_their_ancestors(xdg: Path, tmp_path: Path) -> None:
    guarded = tmp_path / "data" / "prefix"
    guarded.mkdir(parents=True)
    with pytest.raises(IntegrityFailed):
        fsutil.safe_rmtree(guarded, forbidden=[guarded])
    with pytest.raises(IntegrityFailed):
        fsutil.safe_rmtree(tmp_path / "data", forbidden=[guarded])
    sibling = tmp_path / "data" / "snapshots"
    sibling.mkdir()
    fsutil.safe_rmtree(sibling, forbidden=[guarded])
    assert not sibling.exists()
    assert guarded.is_dir()


def test_safe_rmtree_never_deletes_inside_dot_wine(xdg: Path, tmp_path: Path) -> None:
    wine = xdg / ".wine"
    (wine / "drive_c" / "users").mkdir(parents=True)
    marker = wine / ".fork-linux" / "created-by"
    marker.parent.mkdir()
    marker.write_text("fork-linux", encoding="utf-8")
    for target in (wine, wine / "drive_c", wine / "drive_c" / "users"):
        with pytest.raises(IntegrityFailed, match="inside") as info:
            fsutil.safe_rmtree(target, marker=marker)
        assert "never modifies ~/.wine" in info.value.hint
    assert (wine / "drive_c" / "users").is_dir()


def test_safe_rmtree_follows_a_symlinked_dot_wine_to_its_target(xdg: Path, tmp_path: Path) -> None:
    real = tmp_path / "real-wine"
    (real / "drive_c").mkdir(parents=True)
    (xdg / ".wine").symlink_to(real, target_is_directory=True)
    with pytest.raises(IntegrityFailed, match="inside"):
        fsutil.safe_rmtree(real / "drive_c")
    assert (real / "drive_c").is_dir()
    sibling = tmp_path / "real-wine-2"
    sibling.mkdir()
    fsutil.safe_rmtree(sibling)
    assert not sibling.exists()


def test_safe_rmtree_marker_rules(xdg: Path, tmp_path: Path) -> None:
    root = tmp_path / "prefix"
    meta = root / ".fork-linux"
    meta.mkdir(parents=True)
    marker = meta / "created-by"
    with pytest.raises(IntegrityFailed, match="marker") as info:
        fsutil.safe_rmtree(root, marker=marker)
    assert "only deletes" in info.value.hint
    elsewhere = tmp_path / "elsewhere"
    elsewhere.write_text("x", encoding="utf-8")
    marker.symlink_to(elsewhere)
    with pytest.raises(IntegrityFailed, match="marker"):
        fsutil.safe_rmtree(root, marker=marker)
    marker.unlink()
    marker.write_text("fork-linux", encoding="utf-8")
    fsutil.safe_rmtree(root, marker=marker)
    assert not root.exists()
    assert elsewhere.exists()


def test_safe_rmtree_without_home_and_passwd_entry(
    xdg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_entry(uid: int) -> object:
        raise KeyError(uid)

    monkeypatch.delenv("HOME")
    monkeypatch.setattr(fsutil.pwd, "getpwuid", no_entry)
    target = tmp_path / "t"
    (target / "x").mkdir(parents=True)
    fsutil.safe_rmtree(target)
    assert not target.exists()


def test_safe_rmtree_handles_read_only_directories(xdg: Path, tmp_path: Path) -> None:
    root = tmp_path / "ro"
    locked = root / "locked"
    (locked / "inner").mkdir(parents=True)
    (locked / "inner" / "f").write_text("x", encoding="utf-8")
    (locked / "g").write_text("y", encoding="utf-8")
    os.chmod(locked / "inner", 0o000)
    os.chmod(locked, 0o500)
    fsutil.safe_rmtree(root)
    assert not root.exists()


def test_safe_rmtree_retry_grants_access_only_inside_tree(
    xdg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    os.chmod(outside, 0o500)
    root = tmp_path / "tree"
    (root / "locked").mkdir(parents=True)
    (root / "open").mkdir()
    (root / "link").symlink_to(outside, target_is_directory=True)
    os.chmod(root / "locked", 0o500)
    real_rmtree = shutil.rmtree
    calls: list[str] = []

    def flaky(path: object, *args: object, **kwargs: object) -> None:
        calls.append(str(path))
        if len(calls) == 1:
            raise PermissionError(errno.EACCES, "simulated", str(path))
        real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(fsutil.shutil, "rmtree", flaky)
    fsutil.safe_rmtree(root)
    assert len(calls) == 2
    assert not root.exists()
    assert _mode(outside) == 0o500, "symlinked directories outside the tree are never chmodded"
    os.chmod(outside, 0o700)


# --------------------------------------------------------------------------- safe_extract helpers


def _file(name: str, data: bytes = b"data", mode: int = 0o644) -> tuple[tarfile.TarInfo, bytes | None]:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mode = mode
    return info, data


def _dir(name: str, mode: int = 0o755) -> tuple[tarfile.TarInfo, bytes | None]:
    info = tarfile.TarInfo(name)
    info.type = tarfile.DIRTYPE
    info.mode = mode
    return info, None


def _link(name: str, target: str, kind: bytes = tarfile.SYMTYPE) -> tuple[tarfile.TarInfo, bytes | None]:
    info = tarfile.TarInfo(name)
    info.type = kind
    info.linkname = target
    return info, None


def _special(name: str, kind: bytes) -> tuple[tarfile.TarInfo, bytes | None]:
    info = tarfile.TarInfo(name)
    info.type = kind
    info.devmajor, info.devminor = 1, 3
    return info, None


def _make_tar(path: Path, members: list[tuple[tarfile.TarInfo, bytes | None]], compression: str = "") -> Path:
    with tarfile.open(path, f"w:{compression}" if compression else "w") as tar:
        for info, data in members:
            tar.addfile(info, io.BytesIO(data) if data is not None else None)
    return path


def _assert_rejected(tmp_path: Path, members: list[tuple[tarfile.TarInfo, bytes | None]], match: str) -> Path:
    archive = _make_tar(tmp_path / "evil.tar", members)
    dest = tmp_path / "dest"
    with pytest.raises(IntegrityFailed, match=match) as info:
        fsutil.safe_extract(archive, dest)
    assert info.value.exit_code == ExitCode.INTEGRITY_FAILED
    return dest


# --------------------------------------------------------------------------- safe_extract


@pytest.mark.parametrize("compression", ["", "gz", "xz", "bz2"])
def test_safe_extract_supported_compressions(tmp_path: Path, compression: str) -> None:
    archive = _make_tar(
        tmp_path / f"a.tar.{compression or 'plain'}",
        [_dir("pkg"), _file("pkg/a.txt", b"alpha"), _dir("pkg/bin"), _file("pkg/bin/tool", b"#!/bin/sh\n", 0o755),
         _link("pkg/alias", "a.txt"), _link("pkg/bin/up", "../a.txt")],
        compression,
    )
    dest = tmp_path / "out"
    extracted = fsutil.safe_extract(archive, dest)
    assert extracted == [dest / "pkg", dest / "pkg/a.txt", dest / "pkg/bin", dest / "pkg/bin/tool",
                         dest / "pkg/alias", dest / "pkg/bin/up"]
    assert (dest / "pkg/a.txt").read_bytes() == b"alpha"
    assert _mode(dest / "pkg/bin/tool") == 0o755
    assert os.readlink(dest / "pkg/alias") == "a.txt"
    assert (dest / "pkg/bin/up").read_bytes() == b"alpha"


def test_safe_extract_rejects_absolute_member(tmp_path: Path) -> None:
    dest = _assert_rejected(tmp_path, [_file("ok.txt"), _file("/etc/evil")], "absolute name")
    assert not dest.exists(), "validation happens before anything is written"


@pytest.mark.parametrize("name", ["../evil", "a/../../evil", "a/b/../../../evil", ".."])
def test_safe_extract_rejects_dotdot_traversal(tmp_path: Path, name: str) -> None:
    dest = _assert_rejected(tmp_path, [_file(name)], r"'\.\.'")
    assert not (tmp_path / "evil").exists()
    assert not dest.exists()


@pytest.mark.parametrize("target", ["../outside", "a/../../outside", "sub/../../x"])
def test_safe_extract_rejects_escaping_symlink(tmp_path: Path, target: str) -> None:
    _assert_rejected(tmp_path, [_link("link", target)], "outside the destination")


@pytest.mark.parametrize("target", ["/etc/passwd", ""])
def test_safe_extract_rejects_absolute_or_empty_symlink(tmp_path: Path, target: str) -> None:
    _assert_rejected(tmp_path, [_link("link", target)], "empty or absolute target")


def test_safe_extract_rejects_nested_escaping_symlink(tmp_path: Path) -> None:
    _assert_rejected(tmp_path, [_dir("a"), _link("a/link", "../../x")], "outside the destination")


@pytest.mark.parametrize("order", ["link-first", "link-last"])
def test_safe_extract_normalises_symlink_chains(tmp_path: Path, order: str) -> None:
    members = [_link("a", "."), _link("x", "a/a/a/../../..")]
    archive = _make_tar(tmp_path / "a.tar", members if order == "link-first" else members[::-1])
    dest = tmp_path / "out"
    fsutil.safe_extract(archive, dest)
    assert os.readlink(dest / "x") == ".", "inner '..' is collapsed so the chain cannot climb out"
    assert os.path.realpath(dest / "x") == os.path.realpath(dest)


def test_safe_extract_rejects_symlink_escaping_via_existing_link(tmp_path: Path) -> None:
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "p").symlink_to(".")
    archive = _make_tar(tmp_path / "a.tar", [_link("p/l", "..")])
    with pytest.raises(IntegrityFailed, match="resolves outside"):
        fsutil.safe_extract(archive, dest)


def test_safe_extract_rejects_symlink_placed_through_existing_link(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "evil").symlink_to(outside)
    archive = _make_tar(tmp_path / "a.tar", [_link("evil/l", "x")])
    with pytest.raises(IntegrityFailed, match="resolves outside"):
        fsutil.safe_extract(archive, dest)
    assert not os.path.lexists(outside / "l")


def test_verify_symlinks_removes_escaping_link(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "file").write_text("x", encoding="utf-8")
    (root / "ok").symlink_to("file")
    (root / "bad").symlink_to(tmp_path)
    plan = [_file("file")[0], _link("ok", "file")[0], _link("bad", "..")[0]]
    with pytest.raises(IntegrityFailed, match="chain resolves outside"):
        fsutil._verify_symlinks(plan, root)
    assert not os.path.lexists(root / "bad")
    assert os.path.lexists(root / "ok")


def test_safe_extract_rejects_writing_through_archive_symlink(tmp_path: Path) -> None:
    _assert_rejected(tmp_path, [_dir("sub"), _link("lnk", "sub"), _file("lnk/f.txt")], "through a symbolic link")


def test_safe_extract_rejects_overwriting_archive_symlink(tmp_path: Path) -> None:
    _assert_rejected(tmp_path, [_file("t"), _link("s", "t"), _file("s", b"replaced")], "through a symbolic link")


@pytest.mark.parametrize(
    "members",
    [
        [_dir("d"), _link("d", ".")],
        [_file("d/f"), _link("d", ".")],
    ],
    ids=["declared-dir", "implied-dir"],
)
def test_safe_extract_rejects_symlink_replacing_directory(
    tmp_path: Path, members: list[tuple[tarfile.TarInfo, bytes | None]]
) -> None:
    _assert_rejected(tmp_path, members, "replace a directory")


def test_safe_extract_never_writes_through_existing_hardlink(tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("precious", encoding="utf-8")
    dest = tmp_path / "dest"
    dest.mkdir()
    os.link(outside, dest / "victim")
    archive = _make_tar(
        tmp_path / "a.tar", [_file("victim", b"overwritten"), _link("again", "victim", tarfile.LNKTYPE)]
    )
    fsutil.safe_extract(archive, dest)
    assert (dest / "victim").read_bytes() == b"overwritten"
    assert outside.read_text(encoding="utf-8") == "precious"


def test_safe_extract_redeclared_file_and_hardlink(tmp_path: Path) -> None:
    archive = _make_tar(
        tmp_path / "a.tar",
        [_file("f", b"v1"), _link("h", "f", tarfile.LNKTYPE), _link("h", "f", tarfile.LNKTYPE), _file("f", b"v2")],
    )
    dest = tmp_path / "out"
    fsutil.safe_extract(archive, dest)
    assert (dest / "f").read_bytes() == b"v2"
    assert (dest / "h").read_bytes() == b"v1"


def test_safe_extract_allows_redeclared_symlink(tmp_path: Path) -> None:
    archive = _make_tar(tmp_path / "a.tar", [_file("a"), _file("b"), _link("s", "a"), _link("s", "b")])
    dest = tmp_path / "out"
    fsutil.safe_extract(archive, dest)
    assert os.readlink(dest / "s") == "b"


def test_safe_extract_rejects_existing_symlink_in_destination(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "evil").symlink_to(outside)
    archive = _make_tar(tmp_path / "a.tar", [_file("evil/x")])
    with pytest.raises(IntegrityFailed, match="resolves outside"):
        fsutil.safe_extract(archive, dest)
    assert not (outside / "x").exists()


@pytest.mark.parametrize("target", ["../../etc/passwd", "/etc/passwd", "missing", "later"])
def test_safe_extract_rejects_escaping_or_dangling_hardlink(tmp_path: Path, target: str) -> None:
    _assert_rejected(tmp_path, [_link("h", target, tarfile.LNKTYPE), _file("later")], "")


def test_safe_extract_rejects_hardlink_through_archive_symlink(tmp_path: Path) -> None:
    _assert_rejected(
        tmp_path,
        [_dir("d"), _file("d/f"), _link("s", "d"), _link("h", "s/f", tarfile.LNKTYPE)],
        "hard link must point to a regular file",
    )


def test_safe_extract_hardlink_inside_destination(tmp_path: Path) -> None:
    archive = _make_tar(tmp_path / "a.tar", [_file("f", b"same"), _link("h", "f", tarfile.LNKTYPE)])
    dest = tmp_path / "out"
    fsutil.safe_extract(archive, dest)
    assert (dest / "h").read_bytes() == b"same"
    assert os.stat(dest / "h").st_ino == os.stat(dest / "f").st_ino


@pytest.mark.parametrize(
    ("kind", "label"),
    [(tarfile.CHRTYPE, "char"), (tarfile.BLKTYPE, "block"), (tarfile.FIFOTYPE, "fifo")],
)
def test_safe_extract_rejects_devices_and_fifos(tmp_path: Path, kind: bytes, label: str) -> None:
    dest = _assert_rejected(tmp_path, [_file("ok"), _special(f"node-{label}", kind)], "special files")
    assert not dest.exists()


def test_safe_extract_rejects_device_even_when_stripped(tmp_path: Path) -> None:
    archive = _make_tar(tmp_path / "a.tar", [_special("dev", tarfile.CHRTYPE)])
    with pytest.raises(IntegrityFailed):
        fsutil.safe_extract(archive, tmp_path / "out", strip_components=1)


def test_safe_extract_strips_setuid_setgid_sticky_and_world_write(tmp_path: Path) -> None:
    archive = _make_tar(
        tmp_path / "a.tar",
        [
            _file("suid", b"x", 0o4755),
            _file("sgid", b"x", 0o2755),
            _file("world", b"x", 0o666),
            _file("groupexec", b"x", 0o654),
            _file("noperm", b"x", 0o000),
            _dir("sticky", 0o1777),
        ],
    )
    dest = tmp_path / "out"
    fsutil.safe_extract(archive, dest)
    assert _mode(dest / "suid") == 0o755
    assert _mode(dest / "sgid") == 0o755
    assert _mode(dest / "world") == 0o644
    assert _mode(dest / "groupexec") == 0o644
    assert _mode(dest / "noperm") == 0o600
    sticky = _mode(dest / "sticky")
    assert not sticky & (stat.S_ISVTX | stat.S_IWOTH)
    assert sticky & stat.S_IRWXU == stat.S_IRWXU


def test_safe_extract_strip_components(tmp_path: Path) -> None:
    archive = _make_tar(
        tmp_path / "wine.tar.xz",
        [
            _dir("wine-11.0"),
            _dir("wine-11.0/bin"),
            _file("wine-11.0/bin/wine", b"elf", 0o755),
            _link("wine-11.0/bin/wine64", "wine"),
            _link("wine-11.0/bin/wineserver", "wine-11.0/bin/wine", tarfile.LNKTYPE),
            _file("README-top-level", b"skipped"),
        ],
        "xz",
    )
    dest = tmp_path / "runtime"
    extracted = fsutil.safe_extract(archive, dest, strip_components=1)
    assert extracted == [dest / "bin", dest / "bin/wine", dest / "bin/wine64", dest / "bin/wineserver"]
    assert (dest / "bin/wine").read_bytes() == b"elf"
    assert os.readlink(dest / "bin/wine64") == "wine"
    assert os.stat(dest / "bin/wineserver").st_ino == os.stat(dest / "bin/wine").st_ino
    assert not (dest / "README-top-level").exists()
    assert not (dest / "wine-11.0").exists()


def test_safe_extract_strip_components_rejects_hardlink_to_skipped_member(tmp_path: Path) -> None:
    archive = _make_tar(tmp_path / "a.tar", [_file("top"), _link("d/h", "top", tarfile.LNKTYPE)])
    with pytest.raises(IntegrityFailed, match="hard link"):
        fsutil.safe_extract(archive, tmp_path / "out", strip_components=1)


def test_safe_extract_skips_dot_entries(tmp_path: Path) -> None:
    archive = _make_tar(tmp_path / "a.tar.gz", [_dir("."), _dir("./d"), _file("./d//x", b"1")], "gz")
    dest = tmp_path / "out"
    assert fsutil.safe_extract(archive, dest) == [dest / "d", dest / "d/x"]
    assert (dest / "d/x").read_bytes() == b"1"


def test_safe_extract_negative_strip_is_a_usage_error(tmp_path: Path) -> None:
    with pytest.raises(UsageError):
        fsutil.safe_extract(tmp_path / "x.tar", tmp_path / "out", strip_components=-1)


def test_safe_extract_corrupt_archive(tmp_path: Path) -> None:
    junk = tmp_path / "junk.tar.xz"
    junk.write_bytes(os.urandom(4096))
    with pytest.raises(IntegrityFailed, match="cannot read archive") as info:
        fsutil.safe_extract(junk, tmp_path / "out")
    assert "truncated or corrupt" in info.value.hint
    with pytest.raises(IntegrityFailed, match="cannot read archive"):
        fsutil.safe_extract(tmp_path / "missing.tar", tmp_path / "out")


@pytest.mark.parametrize("compression", ["xz", "gz"])
def test_safe_extract_truncated_archive(tmp_path: Path, compression: str) -> None:
    members = [_file(f"f{i}", os.urandom(20000)) for i in range(5)]
    whole = _make_tar(tmp_path / f"whole.tar.{compression}", members, compression)
    data = whole.read_bytes()
    cut = tmp_path / f"cut.tar.{compression}"
    cut.write_bytes(data[: len(data) // 2])
    with pytest.raises(IntegrityFailed, match="cannot read archive"):
        fsutil.safe_extract(cut, tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_safe_extract_maps_extraction_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    archive = _make_tar(tmp_path / "a.tar", [_file("f")])

    def broken(self: tarfile.TarFile, *args: object, **kwargs: object) -> None:
        raise tarfile.ExtractError("simulated")

    monkeypatch.setattr(tarfile.TarFile, "extract", broken)
    with pytest.raises(IntegrityFailed, match="simulated"):
        fsutil.safe_extract(archive, tmp_path / "out")


def test_safe_extract_reports_tarfile_filter_rejections_as_unsafe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Refused(Exception):
        pass

    def refuse(self: tarfile.TarFile, *args: object, **kwargs: object) -> None:
        raise Refused("blocked by filter")

    archive = _make_tar(tmp_path / "a.tar", [_file("f")])
    monkeypatch.setattr(fsutil, "_FILTER_ERRORS", (Refused,))
    monkeypatch.setattr(tarfile.TarFile, "extract", refuse)
    with pytest.raises(IntegrityFailed, match="rejected by the tarfile filter: blocked by filter") as info:
        fsutil.safe_extract(archive, tmp_path / "out")
    assert "tampered" in info.value.hint


@pytest.mark.parametrize(
    ("members", "match"),
    [
        ([_dir("a"), _file("a")], "replace a directory"),
        ([_file("a/x"), _file("a")], "replace a directory"),
        ([_file("a/x"), _link("a", "a/x", tarfile.LNKTYPE)], "replace a directory"),
        ([_file("a"), _dir("a")], "replace a file with a directory"),
        ([_file("a"), _link("h", "a", tarfile.LNKTYPE), _dir("h")], "replace a file with a directory"),
        ([_file("a"), _file("a/x")], "parent directory is a file"),
        ([_file("a"), _link("h", "a", tarfile.LNKTYPE), _file("h/x")], "parent directory is a file"),
    ],
    ids=["dir-then-file", "implied-dir-then-file", "implied-dir-then-hardlink", "file-then-dir",
         "hardlink-then-dir", "below-file", "below-hardlink"],
)
def test_safe_extract_rejects_member_type_clashes(
    tmp_path: Path, members: list[tuple[tarfile.TarInfo, bytes | None]], match: str
) -> None:
    dest = _assert_rejected(tmp_path, members, match)
    assert not dest.exists(), "validation happens before anything is written"


def test_safe_extract_allows_redeclared_directories_and_files(tmp_path: Path) -> None:
    archive = _make_tar(tmp_path / "a.tar", [_dir("a"), _file("a/x", b"1"), _dir("a"), _file("a/x", b"2")])
    dest = tmp_path / "out"
    fsutil.safe_extract(archive, dest)
    assert (dest / "a" / "x").read_bytes() == b"2"


@pytest.mark.parametrize(
    ("member", "pax"),
    [(_file("ok"), {"path": "a\x00b"}), (_link("ok", "x"), {"linkpath": "x\x00y"})],
    ids=["name", "link-target"],
)
def test_safe_extract_rejects_nul_bytes(
    tmp_path: Path, member: tuple[tarfile.TarInfo, bytes | None], pax: dict[str, str]
) -> None:
    info, data = member
    info.pax_headers = pax
    archive = tmp_path / "nul.tar"
    with tarfile.open(archive, "w", format=tarfile.PAX_FORMAT) as tar:
        tar.addfile(info, io.BytesIO(data) if data is not None else None)
    dest = tmp_path / "dest"
    with pytest.raises(IntegrityFailed, match="NUL byte"):
        fsutil.safe_extract(archive, dest)
    assert not dest.exists()


def test_extract_options_follow_tarfile_capabilities(monkeypatch: pytest.MonkeyPatch) -> None:
    def sane(member: tarfile.TarInfo, dest: str) -> tarfile.TarInfo:
        return member

    def buggy(member: tarfile.TarInfo, dest: str) -> tarfile.TarInfo:
        raise tarfile.FilterError(member)

    monkeypatch.setattr(tarfile, "data_filter", sane, raising=False)
    assert fsutil._extract_options() == {"filter": "data"}
    monkeypatch.setattr(tarfile, "data_filter", buggy)
    assert fsutil._extract_options() == {"filter": "tar"}
    monkeypatch.delattr(tarfile, "data_filter")
    assert fsutil._extract_options() == {}


def test_real_data_filter_probe_matches_relative_link_behaviour(tmp_path: Path) -> None:
    if not hasattr(tarfile, "data_filter"):
        assert fsutil._extract_options() == {}
        return
    archive = _make_tar(tmp_path / "a.tar", [_dir("d"), _file("f"), _link("d/up", "../f")])
    dest = tmp_path / "out"
    dest.mkdir()
    with tarfile.open(archive) as tar:
        member = tar.getmember("d/up")
        try:
            tarfile.data_filter(member, str(dest))
            accepted = True
        except tarfile.FilterError:
            accepted = False
    assert fsutil._data_filter_handles_relative_links() is accepted


def test_safe_extract_with_tar_filter_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fsutil, "_data_filter_handles_relative_links", lambda: False)
    archive = _make_tar(tmp_path / "a.tar", [_dir("d"), _file("f", b"1", 0o4755), _link("d/up", "../f")])
    dest = tmp_path / "out"
    fsutil.safe_extract(archive, dest)
    assert (dest / "d/up").read_bytes() == b"1"
    assert _mode(dest / "f") == 0o755


def test_safe_extract_without_tarfile_filter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fsutil, "_extract_options", dict)
    archive = _make_tar(tmp_path / "a.tar", [_file("suid", b"x", 0o4755), _dir("d", 0o1777)])
    dest = tmp_path / "out"
    fsutil.safe_extract(archive, dest)
    assert _mode(dest / "suid") == 0o755
    assert not _mode(dest / "d") & stat.S_ISVTX


# --------------------------------------------------------------------------- clone_tree


def _source_tree(root: Path) -> None:
    (root / "sub").mkdir(parents=True)
    (root / "a.txt").write_text("a", encoding="utf-8")
    (root / "sub" / "b.bin").write_bytes(b"b")
    (root / "link").symlink_to("a.txt")
    os.mkfifo(root / "fifo")
    os.chmod(root / "sub", 0o750)


def test_clone_tree_auto_hardlinks(tmp_path: Path) -> None:
    src = tmp_path / "src"
    _source_tree(src)
    dst = tmp_path / "snapshots" / "one"
    assert fsutil.clone_tree(src, dst) == "hardlink"
    assert os.stat(dst / "a.txt").st_ino == os.stat(src / "a.txt").st_ino
    assert (dst / "sub" / "b.bin").read_bytes() == b"b"
    assert os.readlink(dst / "link") == "a.txt"
    assert not os.path.lexists(dst / "fifo"), "special files are skipped"
    assert _mode(dst / "sub") == 0o750


def test_clone_tree_copy(tmp_path: Path) -> None:
    src = tmp_path / "src"
    _source_tree(src)
    dst = tmp_path / "dst"
    assert fsutil.clone_tree(src, dst, method="copy") == "copy"
    assert os.stat(dst / "a.txt").st_ino != os.stat(src / "a.txt").st_ino
    assert (dst / "a.txt").read_text(encoding="utf-8") == "a"


def test_clone_tree_copy_of_empty_tree_reports_copy(tmp_path: Path) -> None:
    (tmp_path / "empty").mkdir()
    assert fsutil.clone_tree(tmp_path / "empty", tmp_path / "e2", method="copy") == "copy"
    assert fsutil.clone_tree(tmp_path / "empty", tmp_path / "e3") == "hardlink"


def _refuse_links(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_link(src: object, dst: object, *args: object, **kwargs: object) -> None:
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr(fsutil.os, "link", no_link)


def test_clone_tree_auto_falls_back_to_copy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src = tmp_path / "src"
    _source_tree(src)
    _refuse_links(monkeypatch)
    dst = tmp_path / "dst"
    assert fsutil.clone_tree(src, dst, method="auto") == "copy"
    assert (dst / "sub" / "b.bin").read_bytes() == b"b"


def test_clone_tree_hardlink_only_fails_loudly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src = tmp_path / "src"
    _source_tree(src)
    _refuse_links(monkeypatch)
    with pytest.raises(ForkLinuxError, match="cannot hard-link") as info:
        fsutil.clone_tree(src, tmp_path / "dst", method="hardlink")
    assert "method=auto" in info.value.hint


def test_clone_tree_rejects_bad_arguments(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    with pytest.raises(UsageError, match="unknown clone method"):
        fsutil.clone_tree(src, tmp_path / "d1", method="reflink")
    with pytest.raises(UsageError, match="not a directory"):
        fsutil.clone_tree(tmp_path / "missing", tmp_path / "d2")
    (tmp_path / "src-link").symlink_to(src)
    with pytest.raises(UsageError, match="not a directory"):
        fsutil.clone_tree(tmp_path / "src-link", tmp_path / "d3")
    (tmp_path / "exists").mkdir()
    with pytest.raises(FileExistsError):
        fsutil.clone_tree(src, tmp_path / "exists")
    with pytest.raises(UsageError, match="into itself"):
        fsutil.clone_tree(src, src / "snapshot")
    with pytest.raises(UsageError, match="into itself"):
        fsutil.clone_tree(src, src)
    assert list(src.iterdir()) == []


# --------------------------------------------------------------------------- disk_free


def test_disk_free_existing_and_missing_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert fsutil.disk_free(tmp_path) > 0
    probes: list[Path] = []
    real = shutil.disk_usage

    def spy(path: Path) -> object:
        probes.append(Path(path))
        return real(path)

    monkeypatch.setattr(fsutil.shutil, "disk_usage", spy)
    assert fsutil.disk_free(tmp_path / "not" / "yet" / "there") > 0
    assert probes == [tmp_path]
