"""Tests for fork_linux.wine_provider: managed install, system/flatpak/custom Wine and resolve()."""

from __future__ import annotations

import hashlib
import io
import logging
import os
import shutil
import tarfile
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from fork_linux import download, manifest, sandbox, wine_provider
from fork_linux.config import Config
from fork_linux.errors import IntegrityFailed, NotFound, UsageError, WineUnavailable
from fork_linux.manifest import Manifest, WineBuild
from fork_linux.paths import Paths
from fork_linux.procrun import Completed, RecordingRunner, Runner
from fork_linux.winecmd import WineInfo

BUILD_ID = "kron4ek-11.0-staging-wow64"
TOP = "wine-11.0-staging-amd64-wow64"
URL = "https://github.com/Kron4ek/Wine-Builds/releases/download/11.0/wine-11.0-staging-amd64-wow64.tar.xz"
FAKE_WINE = "#!/usr/bin/env python3\nprint('wine-11.0 (Staging)')\n"


def _tar(path: Path, members: dict[str, str | None]) -> Path:
    """Write a tar.xz with ``members`` (name -> text, None for a directory); scripts are 0755."""
    with tarfile.open(path, "w:xz") as tar:
        for name, text in members.items():
            info = tarfile.TarInfo(name)
            if text is None:
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                tar.addfile(info)
                continue
            data = text.encode("utf-8")
            info.size = len(data)
            info.mode = 0o755
            tar.addfile(info, io.BytesIO(data))
    return path


def _wine_tree(**overrides: str | None) -> dict[str, str | None]:
    members: dict[str, str | None] = {
        f"{TOP}/": None,
        f"{TOP}/bin/wine": FAKE_WINE,
        f"{TOP}/bin/wineserver": "#!/bin/sh\nexit 0\n",
        f"{TOP}/lib/wine/i386-windows/": None,
        f"{TOP}/lib/wine/x86_64-unix/ntdll.so": "ELF",
    }
    for name, text in overrides.items():
        key = f"{TOP}/{name.replace('__', '/')}"
        if text == "DELETE":
            members.pop(key)
        else:
            members[key] = text
    return members


def _build(archive: Path, **changes: Any) -> WineBuild:
    data = archive.read_bytes()
    build = WineBuild(
        id=BUILD_ID,
        version="11.0",
        flavor="staging",
        arch="x86_64",
        wow64=True,
        url=URL,
        sha256=hashlib.sha256(data).hexdigest(),
        size=len(data),
        strip_components=1,
        min_glibc="2.27",
        status="known-good",
    )
    return replace(build, **changes)


@pytest.fixture
def paths(xdg: Path) -> Paths:
    return Paths.from_env(os.environ)


@pytest.fixture(autouse=True)
def _no_flatpak_info(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sandbox, "FLATPAK_INFO", tmp_path / "no-flatpak-info")


@pytest.fixture
def archive(tmp_path: Path) -> Path:
    return _tar(tmp_path / "wine.tar.xz", _wine_tree())


class FakeFetch:
    """Stands in for download.fetch: copies a local archive into the cache, records the call."""

    def __init__(self, source: Path) -> None:
        self.source = source
        self.calls: list[dict[str, Any]] = []

    def __call__(self, url: str, dest_name: str, **kwargs: Any) -> Path:
        self.calls.append({"url": url, "dest_name": dest_name, **kwargs})
        dest = Path(kwargs["cache_dir"]) / dest_name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self.source, dest)
        return dest


@pytest.fixture
def fetch(archive: Path, monkeypatch: pytest.MonkeyPatch) -> FakeFetch:
    fake = FakeFetch(archive)
    monkeypatch.setattr(download, "fetch", fake)
    return fake


def _wine_runner(version: str = "wine-11.0 (Staging)\n") -> RecordingRunner:
    return RecordingRunner({"wine": version})


def _install_complete(paths: Paths, build: WineBuild, sha: str | None = None) -> Path:
    root = wine_provider.managed_root(paths, build.id)
    (root / "bin").mkdir(parents=True)
    (root / "bin" / "wine").write_text("wine", encoding="utf-8")
    (root / wine_provider.COMPLETE_MARKER).write_text((sha or build.sha256) + "\n", encoding="utf-8")
    return root


# --------------------------------------------------------------------------- managed install


def test_managed_root(paths: Paths) -> None:
    assert wine_provider.managed_root(paths, BUILD_ID) == paths.runtimes_dir / "wine" / BUILD_ID


@pytest.mark.parametrize("bad", ["", "../x", "a/b", ".hidden", "-x", "x" * 65])
def test_managed_root_rejects_unsafe_ids(paths: Paths, bad: str) -> None:
    with pytest.raises(UsageError, match="not a Wine build id"):
        wine_provider.managed_root(paths, bad)


def test_install_managed(paths: Paths, archive: Path, fetch: FakeFetch) -> None:
    build = _build(archive)
    runner = _wine_runner()
    progress = []

    root = wine_provider.install_managed(build, paths, runner, offline=True, progress=progress.append)

    assert root == paths.runtimes_dir / "wine" / BUILD_ID
    assert (root / "bin" / "wine").read_text(encoding="utf-8") == FAKE_WINE
    assert os.access(root / "bin" / "wine", os.X_OK)
    assert (root / wine_provider.COMPLETE_MARKER).read_text(encoding="utf-8") == build.sha256 + "\n"
    assert wine_provider.is_installed(paths, build)
    assert fetch.calls == [
        {
            "url": URL,
            "dest_name": "wine-11.0-staging-amd64-wow64.tar.xz",
            "cache_dir": paths.downloads_dir,
            "sha256": build.sha256,
            "size": build.size,
            "offline": True,
            "progress": progress.append,
            "allowed_hosts": ("github.com",),
        }
    ]
    staging = root.parent / f"{BUILD_ID}.tmp-{os.getpid()}"
    assert runner.argvs == [[str(staging / "bin" / "wine"), "--version"]]
    assert isinstance(runner.calls[0]["env"], dict)
    assert sorted(path.name for path in root.parent.iterdir()) == [BUILD_ID]


def test_install_managed_is_idempotent(paths: Paths, archive: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    build = _build(archive)
    root = _install_complete(paths, build)

    def no_fetch(*args: Any, **kwargs: Any) -> Path:
        raise AssertionError("must not download")

    monkeypatch.setattr(download, "fetch", no_fetch)
    runner = RecordingRunner()
    assert wine_provider.install_managed(build, paths, runner) == root
    assert runner.calls == []


def test_install_managed_replaces_an_incomplete_or_outdated_root(
    paths: Paths, archive: Path, fetch: FakeFetch
) -> None:
    build = _build(archive)
    root = _install_complete(paths, build, sha="0" * 64)
    (root / "stale-file").write_text("old", encoding="utf-8")
    assert not wine_provider.is_installed(paths, build)

    assert wine_provider.install_managed(build, paths, _wine_runner()) == root
    assert not (root / "stale-file").exists()
    assert wine_provider.is_installed(paths, build)


def test_install_managed_when_marker_present_but_wine_missing(paths: Paths, archive: Path, fetch: FakeFetch) -> None:
    build = _build(archive)
    root = _install_complete(paths, build)
    (root / "bin" / "wine").unlink()
    assert not wine_provider.is_installed(paths, build)
    wine_provider.install_managed(build, paths, _wine_runner())
    assert (root / "bin" / "wine").is_file()
    assert len(fetch.calls) == 1


def test_unreadable_or_symlinked_marker_is_not_complete(paths: Paths, archive: Path, tmp_path: Path) -> None:
    build = _build(archive)
    root = _install_complete(paths, build)
    marker = root / wine_provider.COMPLETE_MARKER
    marker.write_bytes(b"\xff\xfe\x00bad")
    assert not wine_provider.is_installed(paths, build)
    marker.unlink()
    real = tmp_path / "elsewhere"
    real.write_text(build.sha256, encoding="utf-8")
    marker.symlink_to(real)
    assert not wine_provider.is_installed(paths, build)
    assert wine_provider.installed_builds(paths) == []


def test_install_managed_archive_name_fallback(paths: Paths, archive: Path, fetch: FakeFetch) -> None:
    build = _build(archive, url="https://github.com/")
    wine_provider.install_managed(build, paths, _wine_runner())
    assert fetch.calls[0]["dest_name"] == f"wine-{BUILD_ID}.tar"


def test_install_managed_cleans_up_after_a_corrupt_archive(
    paths: Paths, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    corrupt = tmp_path / "corrupt.tar.xz"
    corrupt.write_bytes(b"not an archive at all")
    monkeypatch.setattr(download, "fetch", FakeFetch(corrupt))
    build = _build(corrupt)
    with pytest.raises(IntegrityFailed, match="cannot read archive"):
        wine_provider.install_managed(build, paths, _wine_runner())
    store = paths.runtimes_dir / "wine"
    assert list(store.iterdir()) == []


@pytest.mark.parametrize("missing", ["bin__wine", "bin__wineserver"])
def test_install_managed_requires_wine_and_wineserver(
    paths: Paths, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    broken = _tar(tmp_path / "broken.tar.xz", _wine_tree(**{missing: "DELETE"}))
    monkeypatch.setattr(download, "fetch", FakeFetch(broken))
    with pytest.raises(IntegrityFailed, match="has no bin/wine and bin/wineserver") as info:
        wine_provider.install_managed(_build(broken), paths, _wine_runner())
    assert "strip_components" in info.value.hint
    assert list((paths.runtimes_dir / "wine").iterdir()) == []


def test_install_managed_rejects_a_version_mismatch(paths: Paths, archive: Path, fetch: FakeFetch) -> None:
    with pytest.raises(IntegrityFailed, match="reports version 10.0, but the manifest pins 11.0"):
        wine_provider.install_managed(_build(archive), paths, _wine_runner("wine-10.0\n"))
    assert list((paths.runtimes_dir / "wine").iterdir()) == []


def test_install_managed_when_wine_does_not_run(paths: Paths, archive: Path, fetch: FakeFetch) -> None:
    runner = RecordingRunner({"wine": Completed([], 127, "", "libfoo.so: cannot open\n")})
    with pytest.raises(WineUnavailable, match="libfoo"):
        wine_provider.install_managed(_build(archive), paths, runner)
    assert not wine_provider.managed_root(paths, BUILD_ID).exists()


def test_install_managed_runs_the_extracted_wine(paths: Paths, archive: Path, fetch: FakeFetch) -> None:
    root = wine_provider.install_managed(_build(archive), paths, Runner())
    assert wine_provider.installed_builds(paths) == [BUILD_ID]
    assert (root / "lib" / "wine" / "i386-windows").is_dir()


def test_install_managed_version_probe_drops_inherited_wine_vars(
    paths: Paths, archive: Path, fetch: FakeFetch, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WINEPREFIX", str(paths.prefix.parent / "someone-elses-prefix"))
    monkeypatch.setenv("WINESERVER", "/usr/bin/wineserver")
    monkeypatch.setenv("FL_MARK", "kept")
    runner = _wine_runner()
    wine_provider.install_managed(_build(archive), paths, runner)
    env = runner.calls[0]["env"]
    assert not [key for key in env if key.startswith("WINE")]
    assert env["FL_MARK"] == "kept"


def test_install_managed_removes_stale_staging(
    paths: Paths, archive: Path, fetch: FakeFetch, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = paths.runtimes_dir / "wine"
    dead = store / f"{BUILD_ID}.tmp-111"
    alive = store / f"{BUILD_ID}.tmp-222"
    other_user = store / f"{BUILD_ID}.tmp-333"
    odd = store / f"{BUILD_ID}.tmp-abc"
    own = store / f"{BUILD_ID}.tmp-{os.getpid()}"
    for directory in (dead, alive, other_user, odd, own):
        (directory / "bin").mkdir(parents=True)

    real_kill = os.kill

    def fake_kill(pid: int, sig: int) -> None:
        if pid == 111:
            raise ProcessLookupError(pid)
        if pid == 333:
            raise PermissionError(pid)
        if pid == 222:
            return None
        return real_kill(pid, sig)

    monkeypatch.setattr(wine_provider.os, "kill", fake_kill)
    wine_provider.install_managed(_build(archive), paths, _wine_runner())
    assert sorted(path.name for path in store.iterdir()) == sorted([BUILD_ID, alive.name, other_user.name, odd.name])


def test_pid_alive_for_this_process() -> None:
    assert wine_provider._pid_alive(os.getpid())


# --------------------------------------------------------------------------- layout heuristics


@pytest.mark.parametrize(
    ("dirs", "expected"),
    [
        (["lib/wine/i386-windows", "lib/wine/x86_64-unix"], True),
        (["lib/wine/i386-windows", "lib/wine/i386-unix"], False),
        (["lib/wine/x86_64-unix"], False),
        (["lib/x86_64-linux-gnu/wine/i386-windows"], True),
        (["lib64/wine/i386-windows", "lib/i386-linux-gnu/wine/i386-unix"], False),
        ([], False),
    ],
)
def test_is_wow64_layout(tmp_path: Path, dirs: list[str], expected: bool) -> None:
    for relative in dirs:
        (tmp_path / relative).mkdir(parents=True)
    assert wine_provider.is_wow64_layout(tmp_path) is expected


# --------------------------------------------------------------------------- system wine


class WhichRunner(RecordingRunner):
    """RecordingRunner whose which() also records the search path it was given."""

    def __init__(self, responses: dict[str, Any], which_map: dict[str, str | None]) -> None:
        super().__init__(responses, which_map)
        self.which_calls: list[tuple[str, str | None]] = []

    def which(self, name: str, path: str | None = None) -> str | None:
        self.which_calls.append((name, path))
        return super().which(name, path)


def _system_tree(tmp_path: Path, *, server: bool = True) -> Path:
    usr = tmp_path / "usr"
    (usr / "bin").mkdir(parents=True)
    (usr / "bin" / "wine").write_text("", encoding="utf-8")
    if server:
        (usr / "bin" / "wineserver").write_text("", encoding="utf-8")
    return usr


def test_system_wine_not_installed() -> None:
    runner = WhichRunner({}, {"wine": None})
    assert wine_provider.system_wine(runner, {"PATH": "/x"}, "9.0") is None
    assert runner.which_calls == [("wine", "/x")]


def test_system_wine_found(tmp_path: Path) -> None:
    usr = _system_tree(tmp_path)
    (usr / "lib" / "x86_64-linux-gnu" / "wine" / "x86_64-unix").mkdir(parents=True)
    runner = WhichRunner(
        {"wine": "wine-10.0 (Ubuntu 10.0~repack)\n"},
        {"wine": str(usr / "bin" / "wine"), "wineserver": "/usr/bin/wineserver"},
    )
    env = {"PATH": f"{usr / 'bin'}"}
    info = wine_provider.system_wine(runner, env, "9.0")
    assert info == WineInfo(
        provider="system",
        build_id=None,
        root=usr,
        wine=usr / "bin" / "wine",
        wineserver=Path("/usr/bin/wineserver"),
        version="10.0",
        staging=False,
        wow64=False,
    )
    assert runner.which_calls == [("wine", env["PATH"]), ("wineserver", env["PATH"])]
    assert runner.calls[0]["env"] == env


def test_system_wine_wow64_through_symlink(tmp_path: Path) -> None:
    opt = tmp_path / "opt" / "wine-staging"
    (opt / "bin").mkdir(parents=True)
    (opt / "bin" / "wine").write_text("", encoding="utf-8")
    (opt / "lib" / "wine" / "i386-windows").mkdir(parents=True)
    usr = tmp_path / "usr"
    (usr / "bin").mkdir(parents=True)
    (usr / "bin" / "wine").symlink_to(opt / "bin" / "wine")
    (usr / "bin" / "wineserver").write_text("", encoding="utf-8")
    runner = WhichRunner({"wine": "wine-11.0 (Staging)\n"}, {"wine": str(usr / "bin" / "wine"), "wineserver": None})
    info = wine_provider.system_wine(runner, {}, "9.0")
    assert info is not None
    assert info.root == usr and info.wine == usr / "bin" / "wine"
    assert info.wow64 is True


def test_system_wine_wineserver_next_to_wine(tmp_path: Path) -> None:
    usr = _system_tree(tmp_path)
    runner = WhichRunner({"wine": "wine-9.0 (Staging)\n"}, {"wine": str(usr / "bin" / "wine"), "wineserver": None})
    info = wine_provider.system_wine(runner, {}, "9.0")
    assert info is not None
    assert info.wineserver == usr / "bin" / "wineserver"
    assert info.staging is True


def test_system_wine_wineserver_fallbacks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    usr = _system_tree(tmp_path, server=False)
    first, second = tmp_path / "wineserver64", tmp_path / "wineserver"
    second.write_text("", encoding="utf-8")
    monkeypatch.setattr(wine_provider, "SYSTEM_WINESERVER_FALLBACKS", (first, second))
    runner = WhichRunner({"wine": "wine-9.0\n"}, {"wine": str(usr / "bin" / "wine"), "wineserver": None})
    info = wine_provider.system_wine(runner, {}, "9.0")
    assert info is not None and info.wineserver == second


def test_system_wine_without_wineserver(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    usr = _system_tree(tmp_path, server=False)
    monkeypatch.setattr(wine_provider, "SYSTEM_WINESERVER_FALLBACKS", (tmp_path / "none",))
    runner = WhichRunner({"wine": "wine-9.0\n"}, {"wine": str(usr / "bin" / "wine"), "wineserver": None})
    assert wine_provider.system_wine(runner, {}, "9.0") is None
    assert runner.calls == []


def test_system_wine_that_does_not_run(tmp_path: Path) -> None:
    usr = _system_tree(tmp_path)
    runner = WhichRunner({"wine": 1}, {"wine": str(usr / "bin" / "wine"), "wineserver": None})
    assert wine_provider.system_wine(runner, {}, "9.0") is None


def test_system_wine_too_old(tmp_path: Path) -> None:
    usr = _system_tree(tmp_path)
    runner = WhichRunner({"wine": "wine-8.0\n"}, {"wine": str(usr / "bin" / "wine"), "wineserver": None})
    assert wine_provider.system_wine(runner, {}, "9.0") is None


# --------------------------------------------------------------------------- flatpak / custom


def _app_root(tmp_path: Path) -> Path:
    app = tmp_path / "app"
    (app / "bin").mkdir(parents=True)
    (app / "bin" / "wine").write_text("", encoding="utf-8")
    (app / "lib" / "wine" / "i386-windows").mkdir(parents=True)
    return app


def test_flatpak_wine_outside_flatpak(tmp_path: Path) -> None:
    assert wine_provider.flatpak_wine({}, _app_root(tmp_path)) is None


def test_flatpak_wine_without_baseapp(tmp_path: Path) -> None:
    assert wine_provider.flatpak_wine({"FLATPAK_ID": "io.github.ventura8.ForkLinux"}, tmp_path / "app") is None


def test_flatpak_wine_default_root_outside_flatpak() -> None:
    assert wine_provider.flatpak_wine({}) is None


def test_flatpak_wine(tmp_path: Path) -> None:
    app = _app_root(tmp_path)
    info = wine_provider.flatpak_wine({"FLATPAK_ID": "io.github.ventura8.ForkLinux"}, app)
    assert info == WineInfo("flatpak", None, app, app / "bin" / "wine", app / "bin" / "wineserver", "", False, True)


def _custom_tree(tmp_path: Path, *, executable: bool = True, server: bool = True) -> Path:
    root = tmp_path / "opt" / "wine-custom"
    (root / "bin").mkdir(parents=True)
    wine = root / "bin" / "wine"
    wine.write_text("", encoding="utf-8")
    wine.chmod(0o755 if executable else 0o644)
    if server:
        (root / "bin" / "wineserver").write_text("", encoding="utf-8")
    return root


@pytest.mark.parametrize("use_binary", [False, True])
def test_custom_wine(tmp_path: Path, use_binary: bool) -> None:
    root = _custom_tree(tmp_path)
    location = root / "bin" / "wine" if use_binary else root
    runner = RecordingRunner({"wine": "wine-10.5 (Staging)\n"})
    info = wine_provider.custom_wine(runner, {"PATH": "/usr/bin"}, location)
    assert info == WineInfo(
        "custom", None, root, root / "bin" / "wine", root / "bin" / "wineserver", "10.5", True, False
    )
    assert runner.calls[0]["env"] == {"PATH": "/usr/bin"}


@pytest.mark.parametrize(
    "make",
    [
        lambda tmp: _custom_tree(tmp, executable=False),
        lambda tmp: _custom_tree(tmp, server=False),
        lambda tmp: tmp / "missing",
    ],
)
def test_custom_wine_unusable(tmp_path: Path, make: Callable[[Path], Path]) -> None:
    with pytest.raises(WineUnavailable, match="no usable Wine at"):
        wine_provider.custom_wine(RecordingRunner(), {}, make(tmp_path))


# --------------------------------------------------------------------------- resolve


def _manifest(extra_builds: dict[str, dict[str, Any]] | None = None) -> Manifest:
    data = manifest.load().as_dict()
    data["wine"]["builds"].update(extra_builds or {})
    return Manifest(data)


def _config(
    paths: Paths, file_values: dict[tuple[str, str], str] | None = None, env: dict[str, str] | None = None
) -> Config:
    return Config(paths.config_file, file_values or {}, env or {})


def test_resolve_managed_installed(paths: Paths) -> None:
    pinned = _manifest()
    root = _install_complete(paths, pinned.wine_default)
    runner = RecordingRunner()
    info = wine_provider.resolve(_config(paths), pinned, paths, runner, {})
    assert info == wine_provider.managed_info(pinned.wine_default, root)
    assert info.wine == root / "bin" / "wine"
    assert info.staging is True and info.wow64 is True and info.version == "11.0"
    assert runner.calls == []


def test_resolve_managed_not_installed(paths: Paths) -> None:
    with pytest.raises(WineUnavailable, match="is not installed") as info:
        wine_provider.resolve(_config(paths), _manifest(), paths, RecordingRunner(), {})
    assert info.value.hint == "run 'fork-linux setup'"


def test_resolve_managed_installs_when_allowed(paths: Paths, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    def fake_install(build: WineBuild, paths_: Paths, runner: Any, **kwargs: Any) -> Path:
        calls.append({"build": build.id, **kwargs})
        return _install_complete(paths_, build)

    monkeypatch.setattr(wine_provider, "install_managed", fake_install)
    progress = print
    info = wine_provider.resolve(
        _config(paths), _manifest(), paths, RecordingRunner(), {}, install=True, offline=True, progress=progress
    )
    assert info.provider == "managed"
    assert calls == [{"build": BUILD_ID, "offline": True, "progress": progress}]


def test_resolve_managed_configured_build(paths: Paths) -> None:
    other = dict(manifest.load().as_dict()["wine"]["builds"][BUILD_ID], version="10.0", flavor="vanilla", wow64=False)
    pinned = _manifest({"other-10.0": other})
    _install_complete(paths, pinned.wine_build("other-10.0"))
    config = _config(paths, {("wine", "build"): "other-10.0"})
    info = wine_provider.resolve(config, pinned, paths, RecordingRunner(), {})
    assert (info.build_id, info.version, info.staging, info.wow64) == ("other-10.0", "10.0", False, False)


def test_resolve_managed_unknown_build(paths: Paths) -> None:
    with pytest.raises(NotFound):
        wine_provider.resolve(_config(paths, {("wine", "build"): "nope"}), _manifest(), paths, RecordingRunner(), {})


def test_resolve_managed_known_bad_build(paths: Paths) -> None:
    bad = dict(manifest.load().as_dict()["wine"]["builds"][BUILD_ID], status="known-bad")
    config = _config(paths, {("wine", "build"): "bad-1"})
    with pytest.raises(WineUnavailable, match="known-bad") as info:
        wine_provider.resolve(config, _manifest({"bad-1": bad}), paths, RecordingRunner(), {})
    assert "config unset wine.build" in info.value.hint


def _system_runner(tmp_path: Path, version: str) -> RecordingRunner:
    usr = _system_tree(tmp_path)
    return RecordingRunner({"wine": version}, {"wine": str(usr / "bin" / "wine"), "wineserver": None})


def test_resolve_choice_system_overrides_config(paths: Paths, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    runner = _system_runner(tmp_path, "wine-10.0\n")
    with caplog.at_level(logging.WARNING, logger="fork_linux.wine_provider"):
        info = wine_provider.resolve(_config(paths), _manifest(), paths, runner, {}, choice="system")
    assert info.provider == "system"
    assert "not a staging build" in caplog.text


def test_resolve_system_staging_from_config(paths: Paths, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    runner = _system_runner(tmp_path, "wine-10.0 (Staging)\n")
    config = _config(paths, {("wine", "provider"): "system"})
    with caplog.at_level(logging.WARNING, logger="fork_linux.wine_provider"):
        info = wine_provider.resolve(config, _manifest(), paths, runner, {})
    assert info.staging is True
    assert "not a staging build" not in caplog.text


def test_resolve_system_missing(paths: Paths) -> None:
    runner = RecordingRunner({}, {"wine": None})
    with pytest.raises(WineUnavailable, match="no usable system Wine: need 'wine' 9.0 or newer"):
        wine_provider.resolve(_config(paths), _manifest(), paths, runner, {}, choice="system")


def test_resolve_flatpak_choice(paths: Paths, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app = _app_root(tmp_path)
    monkeypatch.setattr(wine_provider, "FLATPAK_ROOT", app)
    runner = RecordingRunner({"wine": "wine-10.0 (Staging)\n"})
    env = {"FLATPAK_ID": "io.github.ventura8.ForkLinux"}
    info = wine_provider.resolve(_config(paths), _manifest(), paths, runner, env, choice="flatpak")
    assert (info.provider, info.root, info.version, info.staging, info.wow64) == ("flatpak", app, "10.0", True, True)
    assert runner.argvs == [[str(app / "bin" / "wine"), "--version"]]


def test_resolve_flatpak_choice_outside_flatpak(paths: Paths) -> None:
    with pytest.raises(WineUnavailable, match="only works inside"):
        wine_provider.resolve(_config(paths), _manifest(), paths, RecordingRunner(), {}, choice="flatpak")


def test_resolve_prefers_flatpak_wine_by_default(paths: Paths, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wine_provider, "FLATPAK_ROOT", _app_root(tmp_path))
    env = {"FLATPAK_ID": "io.github.ventura8.ForkLinux"}
    info = wine_provider.resolve(_config(paths), _manifest(), paths, RecordingRunner({"wine": "wine-10.0\n"}), env)
    assert info.provider == "flatpak"


def test_resolve_flatpak_without_baseapp_falls_back_to_managed(
    paths: Paths, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(wine_provider, "FLATPAK_ROOT", tmp_path / "no-app")
    pinned = _manifest()
    _install_complete(paths, pinned.wine_default)
    env = {"FLATPAK_ID": "io.github.ventura8.ForkLinux"}
    assert wine_provider.resolve(_config(paths), pinned, paths, RecordingRunner(), env).provider == "managed"


@pytest.mark.parametrize(
    ("config_values", "config_env", "choice"),
    [
        ({("wine", "provider"): "managed"}, {}, None),
        ({}, {"FORK_LINUX_WINE_PROVIDER": "managed"}, None),
        ({}, {}, "managed"),
    ],
)
def test_resolve_explicit_managed_inside_flatpak(
    paths: Paths,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    config_values: dict[tuple[str, str], str],
    config_env: dict[str, str],
    choice: str | None,
) -> None:
    monkeypatch.setattr(wine_provider, "FLATPAK_ROOT", _app_root(tmp_path))
    pinned = _manifest()
    _install_complete(paths, pinned.wine_default)
    env = {"FLATPAK_ID": "io.github.ventura8.ForkLinux"}
    config = _config(paths, config_values, config_env)
    assert wine_provider.resolve(config, pinned, paths, RecordingRunner(), env, choice=choice).provider == "managed"


def test_resolve_custom_path(paths: Paths, tmp_path: Path) -> None:
    root = _custom_tree(tmp_path)
    config = _config(paths, {("wine", "provider"): str(root)})
    info = wine_provider.resolve(config, _manifest(), paths, RecordingRunner({"wine": "wine-9.5\n"}), {})
    assert (info.provider, info.root, info.version) == ("custom", root, "9.5")


@pytest.mark.parametrize("choice", ["wine-9", "relative/path", "Managed"])
def test_resolve_unknown_choice(paths: Paths, choice: str) -> None:
    with pytest.raises(UsageError, match="unknown Wine provider"):
        wine_provider.resolve(_config(paths), _manifest(), paths, RecordingRunner(), {}, choice=choice)


def test_resolve_empty_choice_means_config(paths: Paths) -> None:
    pinned = _manifest()
    _install_complete(paths, pinned.wine_default)
    assert wine_provider.resolve(_config(paths), pinned, paths, RecordingRunner(), {}, choice="").provider == "managed"


# --------------------------------------------------------------------------- installed / prune


def test_installed_builds_without_store(paths: Paths) -> None:
    assert wine_provider.installed_builds(paths) == []


def test_installed_builds_when_store_is_a_file(paths: Paths) -> None:
    paths.runtimes_dir.mkdir(parents=True)
    (paths.runtimes_dir / "wine").write_text("", encoding="utf-8")
    assert wine_provider.installed_builds(paths) == []


def _store_with_entries(paths: Paths, archive: Path, tmp_path: Path) -> Path:
    build = _build(archive)
    _install_complete(paths, build)
    _install_complete(paths, replace(build, id="b-old"))
    store = paths.runtimes_dir / "wine"
    (store / "incomplete" / "bin").mkdir(parents=True)
    (store / "loose-file").write_text("", encoding="utf-8")
    outside = tmp_path / "outside-build"
    outside.mkdir()
    (outside / wine_provider.COMPLETE_MARKER).write_text(build.sha256, encoding="utf-8")
    (store / "linked").symlink_to(outside)
    return store


def test_installed_builds(paths: Paths, archive: Path, tmp_path: Path) -> None:
    _store_with_entries(paths, archive, tmp_path)
    assert wine_provider.installed_builds(paths) == ["b-old", BUILD_ID]


def test_prune(paths: Paths, archive: Path, tmp_path: Path) -> None:
    store = _store_with_entries(paths, archive, tmp_path)
    assert wine_provider.prune(paths, {BUILD_ID}) == ["b-old"]
    assert sorted(path.name for path in store.iterdir()) == sorted([BUILD_ID, "incomplete", "loose-file", "linked"])
    assert (tmp_path / "outside-build").is_dir()
    assert wine_provider.prune(paths, set()) == [BUILD_ID]
    assert wine_provider.installed_builds(paths) == []
