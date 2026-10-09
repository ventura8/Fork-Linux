"""Tests for fork_linux.snapshots: snapshot, list, prune and restore Fork's install directory."""

from __future__ import annotations

import json
import os
import shutil
import stat
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from fixtures.fork_tree import install_fork, make_layout, write_settings, write_sq_version
from fork_linux import fork_settings, fsutil
from fork_linux import snapshots as snaps
from fork_linux.errors import ForkLinuxError, IntegrityFailed, NotFound, NotSetUpError, UsageError
from fork_linux.fork_layout import ForkLayout
from fork_linux.paths import Paths
from fork_linux.snapshots import Snapshot

T0 = datetime(2026, 10, 9, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def layout(tmp_path: Path) -> ForkLayout:
    lay = make_layout(tmp_path)
    install_fork(lay)
    write_settings(lay, {"Theme": 1, "Workspaces": []})
    lay.custom_commands_file.write_text('{"commands": []}')
    lay.accounts_file.write_text('{"token": "secret"}')
    lay.forkdata_dir.mkdir(parents=True)
    (lay.forkdata_dir / "repositories.toml").write_text("[[repo]]\npath = 'Z:\\\\src'\n")
    return lay


@pytest.fixture
def paths(layout: ForkLayout) -> Paths:
    return layout.paths


class _Clock:
    """Replaces snapshots._now; each call advances by ``step``."""

    def __init__(self, start: datetime = T0, step: timedelta = timedelta(minutes=1)) -> None:
        self.now = start
        self.step = step

    def __call__(self) -> datetime:
        current = self.now
        self.now += self.step
        return current


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    fake = _Clock()
    monkeypatch.setattr(snaps, "_now", fake)
    return fake


def _upgrade(layout: ForkLayout, version: str) -> None:
    """Simulate a Velopack update: current/ replaced wholesale, new package staged."""
    shutil.rmtree(layout.current_dir)
    layout.current_dir.mkdir()
    layout.exe.write_bytes(b"MZ new Fork.exe " + version.encode() * 10)
    write_sq_version(layout, version)
    (layout.current_dir / "new-only.dll").write_bytes(b"MZ new")
    (layout.packages_dir / f"Fork-{version}-full.nupkg").write_bytes(b"PK " + version.encode())


def _meta(snap: Snapshot) -> dict[str, Any]:
    return json.loads((snap.path / snaps.SNAPSHOT_META).read_text())


def _write_meta(snap: Snapshot, meta: dict[str, Any]) -> None:
    (snap.path / snaps.SNAPSHOT_META).write_text(json.dumps(meta))


# -- create --------------------------------------------------------------------------------


def test_now_is_utc() -> None:
    assert snaps._now().tzinfo is timezone.utc


def test_create_hardlinks_current_and_copies_data(paths: Paths, layout: ForkLayout, clock: _Clock) -> None:
    snap = snaps.create(paths, layout, method="hardlink", reason="manual")
    assert snap == Snapshot(
        id="2.23.2-20261009T120000Z",
        fork_version="2.23.2",
        created="2026-10-09T12:00:00+00:00",
        method="hardlink",
        path=paths.snapshots_dir / "2.23.2-20261009T120000Z",
        with_settings=True,
    )
    root = snap.path
    for directory in (paths.snapshots_dir, root, root / "data"):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert os.stat(root / "current" / "Fork.exe").st_ino == os.stat(layout.exe).st_ino
    assert (root / "current" / "lib" / "net" / "Fork.Core.dll").read_bytes()[:2] == b"MZ"
    copied = root / "data" / "settings.json"
    assert copied.read_text() == layout.settings_file.read_text()
    assert os.stat(copied).st_ino != os.stat(layout.settings_file).st_ino
    assert (root / "data" / "custom-commands.json").exists()
    assert not (root / "data" / "accounts.json").exists()
    toml = root / "data" / "ForkData" / "repositories.toml"
    assert os.stat(toml).st_ino != os.stat(layout.forkdata_dir / "repositories.toml").st_ino
    meta = _meta(snap)
    assert meta["schema"] == 1
    assert meta["reason"] == "manual"
    assert meta["skipped"] == []
    assert meta["with_accounts"] is False
    assert meta["with_settings"] is True
    assert meta["files"]["current/Fork.exe"] == [layout.exe.stat().st_size, layout.exe.stat().st_mtime_ns]
    assert "data/ForkData/repositories.toml" in meta["files"]
    assert "meta.json" not in meta["files"]
    assert stat.S_IMODE((root / snaps.SNAPSHOT_META).stat().st_mode) == 0o600
    assert [p.name for p in paths.snapshots_dir.iterdir()] == [snap.id]


def test_create_copy_method_and_accounts(paths: Paths, layout: ForkLayout, clock: _Clock) -> None:
    snap = snaps.create(paths, layout, method="copy", with_accounts=True)
    assert snap.method == "copy"
    assert os.stat(snap.path / "current" / "Fork.exe").st_ino != os.stat(layout.exe).st_ino
    assert (snap.path / "data" / "accounts.json").read_text() == '{"token": "secret"}'
    assert _meta(snap)["with_accounts"] is True


def test_create_without_data_files(tmp_path: Path, clock: _Clock) -> None:
    layout = make_layout(tmp_path)
    install_fork(layout)
    real = tmp_path / "real-forkdata"
    real.mkdir()
    layout.forkdata_dir.symlink_to(real)
    (layout.current_dir / "link.dll").symlink_to("lib/net/Fork.Core.dll")
    snap = snaps.create(layout.paths, layout)
    assert snap.with_settings is False
    assert sorted(p.name for p in (snap.path / "data").iterdir()) == []
    assert (snap.path / "current" / "link.dll").is_symlink()
    assert "current/link.dll" not in _meta(snap)["files"]


def test_create_skips_large_forkdata(
    paths: Paths, layout: ForkLayout, clock: _Clock, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    (layout.forkdata_dir / "big.bin").write_bytes(b"x" * 64)
    (layout.forkdata_dir / "more.bin").write_bytes(b"y" * 64)
    monkeypatch.setattr(snaps, "FORKDATA_MAX_BYTES", 32)
    snap = snaps.create(paths, layout)
    assert not (snap.path / "data" / "ForkData").exists()
    assert _meta(snap)["skipped"] == ["ForkData"]
    assert "not included" in caplog.text


def test_tree_size(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "a").write_bytes(b"1" * 10)
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b").write_bytes(b"2" * 20)
    (tmp_path / "sub" / "gone").write_bytes(b"3" * 1000)
    (tmp_path / "link").symlink_to(tmp_path / "a")
    real_lstat = os.lstat

    def flaky_lstat(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        if str(path).endswith("gone"):
            raise FileNotFoundError(2, "vanished")
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(snaps.os, "lstat", flaky_lstat)
    assert snaps._tree_size(tmp_path, 1000) == 30
    assert snaps._tree_size(tmp_path, 5) > 5


def test_create_refuses_unknown_method(paths: Paths, layout: ForkLayout) -> None:
    with pytest.raises(UsageError, match="unknown snapshot method"):
        snaps.create(paths, layout, method="reflink")


def test_create_requires_an_installed_fork(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    with pytest.raises(NotSetUpError):
        snaps.create(layout.paths, layout)
    install_fork(layout)
    moved = tmp_path / "moved-current"
    layout.current_dir.rename(moved)
    layout.current_dir.symlink_to(moved)
    with pytest.raises(NotSetUpError):
        snaps.create(layout.paths, layout)


def test_create_same_second_gets_unique_ids(paths: Paths, layout: ForkLayout, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(snaps, "_now", _Clock(step=timedelta(0)))
    first = snaps.create(paths, layout)
    (paths.snapshots_dir / f"{snaps.PARTIAL_PREFIX}2.23.2-20261009T120000Z-2").mkdir()
    second = snaps.create(paths, layout)
    assert (first.id, second.id) == ("2.23.2-20261009T120000Z", "2.23.2-20261009T120000Z-3")
    assert {snap.id for snap in snaps.list_snapshots(paths)} == {first.id, second.id}


def test_create_failure_leaves_nothing_behind(
    paths: Paths, layout: ForkLayout, clock: _Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken_copy(*args: Any, **kwargs: Any) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(snaps.shutil, "copy2", broken_copy)
    with pytest.raises(OSError, match="No space"):
        snaps.create(paths, layout)
    assert list(paths.snapshots_dir.iterdir()) == []


def test_create_hardlink_failure_is_reported(
    paths: Paths, layout: ForkLayout, clock: _Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_links(src: Any, dst: Any) -> None:
        raise OSError(18, "Invalid cross-device link")

    monkeypatch.setattr(fsutil.os, "link", no_links)
    with pytest.raises(ForkLinuxError, match="cannot hard-link"):
        snaps.create(paths, layout, method="hardlink")
    assert list(paths.snapshots_dir.iterdir()) == []
    assert snaps.create(paths, layout, method="auto").method == "copy"


# -- list / find ---------------------------------------------------------------------------


def test_list_snapshots_missing_dir(paths: Paths) -> None:
    assert snaps.list_snapshots(paths) == []


def test_list_snapshots_newest_first(paths: Paths, layout: ForkLayout, clock: _Clock) -> None:
    first = snaps.create(paths, layout)
    _upgrade(layout, "2.24.0")
    second = snaps.create(paths, layout)
    clock.now = T0 - timedelta(days=1)
    oldest = snaps.create(paths, layout)
    assert snaps.list_snapshots(paths) == [second, first, oldest]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda meta: meta.update(schema=2),
        lambda meta: meta.update(id="other"),
        lambda meta: meta.update(fork_version=5),
        lambda meta: meta.update(fork_version="garbage"),
        lambda meta: meta.update(created=17),
        lambda meta: meta.update(created="yesterday"),
        lambda meta: meta.update(created="2026-10-09T12:00:00"),
        lambda meta: meta.update(method="reflink"),
        lambda meta: meta.update(with_settings="yes"),
        lambda meta: meta.update(files=[]),
    ],
)
def test_list_skips_malformed_meta(paths: Paths, layout: ForkLayout, clock: _Clock, mutate: Any) -> None:
    snap = snaps.create(paths, layout)
    meta = _meta(snap)
    mutate(meta)
    _write_meta(snap, meta)
    assert snaps.list_snapshots(paths) == []


@pytest.mark.parametrize("content", [b"{broken", b"[]", b"\xff\xfe\xfa"])
def test_list_skips_unparseable_meta(paths: Paths, layout: ForkLayout, clock: _Clock, content: bytes) -> None:
    snap = snaps.create(paths, layout)
    (snap.path / snaps.SNAPSHOT_META).write_bytes(content)
    assert snaps.list_snapshots(paths) == []


def test_list_skips_odd_entries(
    paths: Paths, layout: ForkLayout, clock: _Clock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    good = snaps.create(paths, layout)
    root = paths.snapshots_dir
    (root / ".partial-2.23.2-x").mkdir()
    (root / "stray-file").write_text("x")
    (root / "no-meta").mkdir()
    meta_dir = root / "meta-is-dir"
    (meta_dir / snaps.SNAPSHOT_META).mkdir(parents=True)
    linked = root / "linked"
    linked.symlink_to(good.path)
    meta_link = root / "meta-link"
    meta_link.mkdir()
    (meta_link / snaps.SNAPSHOT_META).symlink_to(good.path / snaps.SNAPSHOT_META)
    assert snaps.list_snapshots(paths) == [good]
    monkeypatch.setattr(snaps, "META_MAX_BYTES", 8)
    assert snaps.list_snapshots(paths) == []


def test_list_skips_ids_that_delete_would_refuse(paths: Paths, layout: ForkLayout, clock: _Clock) -> None:
    good = snaps.create(paths, layout)
    odd = paths.snapshots_dir / "odd name"
    shutil.copytree(good.path, odd, symlinks=True)
    meta = json.loads((odd / snaps.SNAPSHOT_META).read_text())
    meta["id"] = odd.name
    (odd / snaps.SNAPSHOT_META).write_text(json.dumps(meta))
    assert snaps.list_snapshots(paths) == [good]
    assert snaps.prune(paths, 0, set()) == [good.id]
    assert odd.is_dir()


def test_find(paths: Paths, layout: ForkLayout, clock: _Clock) -> None:
    older = snaps.create(paths, layout)
    newer = snaps.create(paths, layout)
    _upgrade(layout, "2.24.0")
    other = snaps.create(paths, layout)
    assert snaps.find(paths, older.id) == older
    assert snaps.find(paths, "2.23.2") == newer
    assert snaps.find(paths, "2.23.2.0") == newer
    assert snaps.find(paths, "2.24") == other
    with pytest.raises(NotFound, match="no snapshot matches"):
        snaps.find(paths, "2.25.0")
    with pytest.raises(NotFound):
        snaps.find(paths, "nonsense")


# -- delete / prune ---------------------------------------------------------------------------


def test_delete(paths: Paths, layout: ForkLayout, clock: _Clock) -> None:
    snap = snaps.create(paths, layout, method="hardlink")
    snaps.delete(paths, snap.id)
    assert not snap.path.exists()
    assert layout.exe.exists()
    with pytest.raises(NotFound):
        snaps.delete(paths, snap.id)


@pytest.mark.parametrize("bad", ["", ".", "..", "../prefix", ".partial-x", "a/b", None])
def test_delete_rejects_unsafe_ids(paths: Paths, bad: Any) -> None:
    with pytest.raises(UsageError, match="not a snapshot id"):
        snaps.delete(paths, bad)


def test_delete_requires_meta_marker(paths: Paths, tmp_path: Path) -> None:
    unmarked = paths.snapshots_dir / "2.23.2-20261009T120000Z"
    unmarked.mkdir(parents=True)
    with pytest.raises(IntegrityFailed, match="marker"):
        snaps.delete(paths, unmarked.name)
    assert unmarked.exists()
    outside = tmp_path / "outside"
    outside.mkdir()
    (paths.snapshots_dir / "link").symlink_to(outside)
    with pytest.raises(NotFound):
        snaps.delete(paths, "link")
    assert outside.exists()


def test_prune_keeps_newest_and_protected(paths: Paths, layout: ForkLayout, clock: _Clock) -> None:
    oldest = snaps.create(paths, layout)
    _upgrade(layout, "2.24.0")
    middle = snaps.create(paths, layout)
    _upgrade(layout, "2.25.0")
    newest = snaps.create(paths, layout)
    newest_dup = snaps.create(paths, layout)
    protected: set[str] = set()
    with pytest.raises(ValueError, match="negative"):
        snaps.prune(paths, -1, protected)
    deleted = snaps.prune(paths, 1, {"2.23.2", "not-a-version"})
    assert deleted == [newest.id, middle.id]
    assert snaps.list_snapshots(paths) == [newest_dup, oldest]
    assert snaps.prune(paths, 0, set()) == [newest_dup.id, oldest.id]
    assert snaps.list_snapshots(paths) == []


def test_prune_removes_stale_partials(paths: Paths, layout: ForkLayout, clock: _Clock) -> None:
    snap = snaps.create(paths, layout)
    root = paths.snapshots_dir
    stale = root / ".partial-2.23.2-old"
    (stale / "current").mkdir(parents=True)
    old = time.time() - snaps.STALE_PARTIAL_SECONDS - 60
    os.utime(stale, (old, old))
    fresh = root / ".partial-2.23.2-new"
    fresh.mkdir()
    stale_file = root / ".partial-file"
    stale_file.write_text("x")
    os.utime(stale_file, (old, old))
    assert snaps.prune(paths, 5, set()) == []
    assert not stale.exists()
    assert fresh.exists()
    assert stale_file.exists()
    assert snap.path.exists()


def test_prune_without_snapshot_dir(paths: Paths) -> None:
    assert snaps.prune(paths, 2, {"2.23.2"}) == []


def test_ensure_current(paths: Paths, layout: ForkLayout, clock: _Clock) -> None:
    assert snaps.ensure_current(paths, layout, keep=0) is None
    assert not paths.snapshots_dir.exists()
    first = snaps.ensure_current(paths, layout, keep=2)
    assert first is not None
    assert first.fork_version == "2.23.2"
    assert snaps.ensure_current(paths, layout, keep=2) is None
    _upgrade(layout, "2.24.0")
    second = snaps.ensure_current(paths, layout, keep=2, method="copy")
    assert second is not None
    assert second.method == "copy"
    _upgrade(layout, "2.25.0")
    third = snaps.ensure_current(paths, layout, keep=2)
    assert third is not None
    assert snaps.list_snapshots(paths) == [third, second]


def test_ensure_current_without_fork(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    assert snaps.ensure_current(layout.paths, layout, keep=2) is None


# -- restore --------------------------------------------------------------------------------------


def _leftovers(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir() if "fork-linux-" in p.name)


def test_restore_rolls_back_current_and_drops_staged(paths: Paths, layout: ForkLayout, clock: _Clock) -> None:
    snap = snaps.create(paths, layout, method="hardlink")
    original_exe = layout.exe.read_bytes()
    _upgrade(layout, "2.24.0")
    (layout.packages_dir / "Fork-2.25.0-delta.nupkg").write_bytes(b"PK")
    assert layout.installed_version() == "2.24.0"
    snaps.restore(paths, layout, snap)
    assert layout.installed_version() == "2.23.2"
    assert layout.exe.read_bytes() == original_exe
    assert not (layout.current_dir / "new-only.dll").exists()
    assert os.stat(layout.exe).st_ino != os.stat(snap.path / "current" / "Fork.exe").st_ino
    assert sorted(p.name for p in layout.packages_dir.iterdir()) == ["Fork-2.23.2-full.nupkg"]
    assert _leftovers(layout.local_dir) == []
    assert snaps.list_snapshots(paths) == [snap]
    layout.exe.write_bytes(b"MZ changed after restore")
    assert (snap.path / "current" / "Fork.exe").read_bytes() == original_exe


def test_restore_with_settings(paths: Paths, layout: ForkLayout, clock: _Clock) -> None:
    snap = snaps.create(paths, layout, with_accounts=True)
    write_settings(layout, {"Theme": 0, "Changed": True})
    layout.custom_commands_file.write_text("{}")
    layout.accounts_file.write_text('{"token": "new"}')
    (layout.forkdata_dir / "repositories.toml").write_text("changed")
    (layout.forkdata_dir / "extra.toml").write_text("extra")
    snaps.restore(paths, layout, snap, with_settings=True)
    assert json.loads(layout.settings_file.read_text()) == {"Theme": 1, "Workspaces": []}
    assert layout.custom_commands_file.read_text() == '{"commands": []}'
    assert layout.accounts_file.read_text() == '{"token": "secret"}'
    assert sorted(p.name for p in layout.forkdata_dir.iterdir()) == ["repositories.toml"]
    assert "Z:" in (layout.forkdata_dir / "repositories.toml").read_text()
    backups = fork_settings.list_backups(fork_settings.default_backup_dir(paths))
    assert len(backups) == 1
    assert json.loads(backups[0].read_text()) == {"Theme": 0, "Changed": True}
    assert _leftovers(layout.forkdata_dir.parent) == []


def test_restore_with_settings_from_snapshot_without_data(tmp_path: Path, clock: _Clock) -> None:
    layout = make_layout(tmp_path)
    install_fork(layout)
    snap = snaps.create(layout.paths, layout)
    write_settings(layout, {"Theme": 0})
    snaps.restore(layout.paths, layout, snap, with_settings=True)
    assert json.loads(layout.settings_file.read_text()) == {"Theme": 0}
    assert not layout.forkdata_dir.exists()


def test_restore_without_settings_keeps_data(paths: Paths, layout: ForkLayout, clock: _Clock) -> None:
    snap = snaps.create(paths, layout)
    write_settings(layout, {"Theme": 0})
    snaps.restore(paths, layout, snap)
    assert json.loads(layout.settings_file.read_text()) == {"Theme": 0}


def test_restore_when_current_is_missing(paths: Paths, layout: ForkLayout, clock: _Clock) -> None:
    snap = snaps.create(paths, layout)
    shutil.rmtree(layout.current_dir)
    snaps.restore(paths, layout, snap)
    assert layout.installed_version() == "2.23.2"


def test_restore_refuses_foreign_snapshot_paths(
    paths: Paths, layout: ForkLayout, clock: _Clock, tmp_path: Path
) -> None:
    snap = snaps.create(paths, layout)
    copied = tmp_path / "copied-snapshot"
    shutil.copytree(snap.path, copied, symlinks=True)
    foreign = Snapshot(snap.id, snap.fork_version, snap.created, snap.method, copied, snap.with_settings)
    with pytest.raises(IntegrityFailed, match="not a snapshot"):
        snaps.restore(paths, layout, foreign)
    link = paths.snapshots_dir / "alias"
    link.symlink_to(snap.path)
    aliased = Snapshot(snap.id, snap.fork_version, snap.created, snap.method, link, snap.with_settings)
    with pytest.raises(IntegrityFailed, match="not a snapshot"):
        snaps.restore(paths, layout, aliased)


def _damage_and_expect(paths: Paths, layout: ForkLayout, snap: Snapshot, match: str) -> None:
    before = layout.exe.read_bytes()
    with pytest.raises(IntegrityFailed, match=match):
        snaps.restore(paths, layout, snap)
    assert layout.exe.read_bytes() == before
    assert _leftovers(layout.local_dir) == []


def test_restore_detects_changed_and_missing_files(paths: Paths, layout: ForkLayout, clock: _Clock) -> None:
    snap = snaps.create(paths, layout, method="copy")
    (snap.path / "current" / "Fork.exe").write_bytes(b"MZ tampered with a different size")
    _damage_and_expect(paths, layout, snap, "has changed")
    (snap.path / "current" / "Fork.exe").unlink()
    _damage_and_expect(paths, layout, snap, "is missing")
    (snap.path / "current" / "Fork.exe").mkdir()
    _damage_and_expect(paths, layout, snap, "has changed")


@pytest.mark.parametrize(
    "entry",
    [
        {"../outside": [1, 0]},
        {"/etc/passwd": [1, 0]},
        {"current": [1, 0]},
        {"current/./x": [1, 0]},
        {"current//x": [1, 0]},
        {"other/x": [1, 0]},
        {"current/x\u0000y": [1, 0]},
        {"current/x": [1]},
        {"current/x": "1,0"},
        {"current/x": [True, 0]},
        {"current/x": [-1, 0]},
    ],
)
def test_restore_rejects_invalid_inventory(
    paths: Paths, layout: ForkLayout, clock: _Clock, entry: dict[str, Any]
) -> None:
    snap = snaps.create(paths, layout)
    meta = _meta(snap)
    meta["files"].update(entry)
    _write_meta(snap, meta)
    _damage_and_expect(paths, layout, snap, "invalid entry")


def test_restore_requires_meta(paths: Paths, layout: ForkLayout, clock: _Clock) -> None:
    snap = snaps.create(paths, layout)
    (snap.path / snaps.SNAPSHOT_META).unlink()
    _damage_and_expect(paths, layout, snap, "meta.json is missing")
    (snap.path / snaps.SNAPSHOT_META).write_text('{"files": 3}')
    _damage_and_expect(paths, layout, snap, "meta.json is missing")


def test_restore_requires_current_dir(paths: Paths, layout: ForkLayout, clock: _Clock) -> None:
    snap = snaps.create(paths, layout)
    meta = _meta(snap)
    meta["files"] = {key: value for key, value in meta["files"].items() if key.startswith("data/")}
    _write_meta(snap, meta)
    shutil.rmtree(snap.path / "current")
    _damage_and_expect(paths, layout, snap, "current/ directory is missing")


def test_restore_verifies_the_copy(
    paths: Paths, layout: ForkLayout, clock: _Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    snap = snaps.create(paths, layout)
    real_clone = fsutil.clone_tree

    def truncating_clone(src: Path, dst: Path, method: str = "auto") -> str:
        used = real_clone(src, dst, method)
        (Path(dst) / "Fork.exe").write_bytes(b"short")
        return used

    monkeypatch.setattr(snaps.fsutil, "clone_tree", truncating_clone)
    _damage_and_expect(paths, layout, snap, "does not match the snapshot")


def test_restore_refuses_symlinked_current(paths: Paths, layout: ForkLayout, clock: _Clock, tmp_path: Path) -> None:
    snap = snaps.create(paths, layout)
    moved = tmp_path / "moved-current"
    layout.current_dir.rename(moved)
    layout.current_dir.symlink_to(moved)
    with pytest.raises(IntegrityFailed, match="symbolic link"):
        snaps.restore(paths, layout, snap)
    assert (moved / "Fork.exe").exists()


def test_restore_refuses_targets_outside_the_prefix(
    paths: Paths, layout: ForkLayout, clock: _Clock, tmp_path: Path
) -> None:
    snap = snaps.create(paths, layout)
    elsewhere = tmp_path / "elsewhere-local"
    layout.local_dir.rename(elsewhere)
    layout.local_dir.symlink_to(elsewhere)
    with pytest.raises(IntegrityFailed, match="not inside"):
        snaps.restore(paths, layout, snap)
    assert (elsewhere / "current" / "Fork.exe").exists()


def test_restore_rename_failure_puts_current_back(
    paths: Paths, layout: ForkLayout, clock: _Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    snap = snaps.create(paths, layout)
    _upgrade(layout, "2.24.0")
    real_rename = os.rename

    def failing_rename(src: Any, dst: Any) -> None:
        if ".fork-linux-new-" in str(src):
            raise OSError(5, "Input/output error")
        real_rename(src, dst)

    monkeypatch.setattr(snaps.os, "rename", failing_rename)
    with pytest.raises(OSError, match="Input/output"):
        snaps.restore(paths, layout, snap)
    assert layout.installed_version() == "2.24.0"
    assert _leftovers(layout.local_dir) == []
    shutil.rmtree(layout.current_dir)
    with pytest.raises(OSError, match="Input/output"):
        snaps.restore(paths, layout, snap)
    assert not layout.current_dir.exists()
    assert _leftovers(layout.local_dir) == []
