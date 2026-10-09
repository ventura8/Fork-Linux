"""Tests for the doctor checks (:mod:`fork_linux.doctor`): one fake prefix, no Wine, no network."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from fork_linux import bootstrap, doctor, feeds, procs, wine_provider
from fork_linux import manifest as manifest_mod
from fork_linux.config import Config
from fork_linux.doctor import Check, DoctorCtx, Result
from fork_linux.errors import ForkLinuxError, Locked, UsageError
from fork_linux.paths import Paths
from fork_linux.procrun import Completed, RecordingRunner
from fork_linux.state import State
from fork_linux.winecmd import WineInfo

from fixtures.cli_run import make_prefix
from fixtures.fork_tree import install_fork, write_settings
from fixtures.setup_ctx import add_values, write_release

USER = "tester"
GUID = "0f8fad5b-d9cb-469f-a165-70867728950e"
FONT_KEY = "Software\\Wine\\Fonts\\Replacements"


@pytest.fixture(autouse=True)
def _isolated(xdg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USER", USER)
    for name in list(os.environ):
        if name.startswith("FORK_LINUX_"):
            monkeypatch.delenv(name, raising=False)
    for name in ("DISPLAY", "WAYLAND_DISPLAY", "XDG_SESSION_TYPE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("FORK_LINUX_DISPLAY_DPI", "96")


def wine_info(root: Path, *, provider: str = "custom", version: str = "11.0", staging: bool = True) -> WineInfo:
    return WineInfo(
        provider=provider,
        build_id=None,
        root=root,
        wine=root / "bin" / "wine",
        wineserver=root / "bin" / "wineserver",
        version=version,
        staging=staging,
        wow64=True,
    )


def make(
    runner: RecordingRunner | None = None,
    *,
    env: dict[str, str] | None = None,
    manifest: manifest_mod.Manifest | None = None,
    wine: WineInfo | None = None,
    **kwargs: Any,
) -> DoctorCtx:
    environ = dict(os.environ)
    environ.update(env or {})
    paths = Paths.from_env(environ)
    return DoctorCtx(
        paths=paths,
        config=Config.load(paths, environ),
        manifest=manifest_mod.load() if manifest is None else manifest,
        runner=RecordingRunner() if runner is None else runner,
        env=environ,
        state=State.load(paths.state_file),
        user=USER,
        _wine=wine,
        **kwargs,
    )


def prefix(ctx: DoctorCtx) -> Path:
    """A booted fake prefix (hives with #arch=win64, drives, marker); mode 0700."""
    make_prefix(ctx.paths)
    for hive in ("system.reg", "user.reg"):
        add_values(ctx.paths.prefix, hive, "Software\\Wine", [])
    os.chmod(ctx.paths.prefix, 0o700)
    return ctx.paths.prefix


def user_values(ctx: DoctorCtx, key: str, *lines: str) -> None:
    add_values(ctx.paths.prefix, "user.reg", key, list(lines))


def run(ctx: DoctorCtx, check_id: str) -> Result:
    return doctor.run_check(ctx, doctor.get(check_id))


def manifest_with(change: Any) -> manifest_mod.Manifest:
    data = manifest_mod.load().as_dict()
    change(data)
    return manifest_mod.Manifest(data)


# -- framework -----------------------------------------------------------------------------------


def test_check_ids_are_unique_and_grouped() -> None:
    ids = [check.id for check in doctor.CHECKS]
    assert len(ids) == len(set(ids)) >= 35
    assert all(check.id.split(".")[0] == check.group for check in doctor.CHECKS)
    from fork_linux import steps

    for check in doctor.CHECKS:
        assert set(check.fix_steps) <= set(steps.STEP_IDS)
    with pytest.raises(KeyError):
        doctor.get("nope")


def test_select_by_flags_ids_and_groups() -> None:
    plain = doctor.select(doctor.CHECKS)
    assert "git.selftest" not in {c.id for c in plain}
    assert "network.reach" not in {c.id for c in plain}
    every = doctor.select(doctor.CHECKS, deep=True, network=True)
    assert len(every) == len(doctor.CHECKS)
    chosen = doctor.select(doctor.CHECKS, only=["wine", "git.selftest"])
    assert [c.id for c in chosen] == ["wine.present", "wine.version", "wine.staging", "wine.wineserver", "git.selftest"]
    with pytest.raises(UsageError, match="bogus"):
        doctor.select(doctor.CHECKS, only=["bogus"])


def test_run_check_turns_exceptions_into_failures() -> None:
    ctx = make()

    def boom(_ctx: DoctorCtx) -> Result:
        raise RuntimeError("kaput")

    def usage(_ctx: DoctorCtx) -> Result:
        raise UsageError("bad", hint="do this")

    def weird(_ctx: DoctorCtx) -> Result:
        return Result("meh", "x")

    assert doctor.run_check(ctx, Check("a.b", "t", "a", boom)) == Result(
        "fail", "the check could not run: RuntimeError: kaput"
    )
    assert doctor.run_check(ctx, Check("a.b", "t", "a", usage)) == Result("fail", "bad", "do this")
    assert doctor.run_check(ctx, Check("a.b", "t", "a", weird)).status == "fail"


def test_report_json_and_summary() -> None:
    fixable = Check("x.one", "One", "x", lambda _c: Result("ok", ""), fix_steps=("registry",))
    plain = Check("x.two", "Two", "x", lambda _c: Result("fail", "d", "h"))
    results = [(fixable, Result("ok", "fine")), (plain, Result("fail", "d", "h"))]
    report = doctor.report_json(results)
    assert report["schema"] == 1
    assert report["summary"] == {"ok": 1, "warn": 0, "fail": 1, "info": 0}
    assert report["checks"][1] == {
        "id": "x.two",
        "title": "Two",
        "group": "x",
        "status": "fail",
        "detail": "d",
        "hint": "h",
        "fixable": False,
    }
    assert report["checks"][0]["fixable"] is True
    assert doctor.failed(results) == ["x.two"]
    json.dumps(report)


def test_run_checks_with_nothing_installed_never_raises() -> None:
    ctx = make(runner=RecordingRunner(which_map={"fork": None, "desktop-file-validate": None}))
    results = doctor.run_checks(ctx, deep=True, network=False)
    by_id = {check.id: result for check, result in results}
    assert ctx.deep
    assert not ctx.network
    assert by_id["wine.present"].status == "fail"
    assert "not installed" in by_id["wine.present"].detail
    assert by_id["prefix.exists"].status == "fail"
    assert by_id["fork.installed"].status == "fail"
    assert by_id["git.selftest"].status == "info"
    assert "network.reach" not in by_id
    assert all(result.status in doctor.STATUSES for result in by_id.values())


# -- context -------------------------------------------------------------------------------------


class _App:
    def __init__(self, env: dict[str, str], *, allow_root: bool = False) -> None:
        self.env = env
        self.paths = Paths.from_env(env)
        self.config = Config.load(self.paths, env)
        self.runner = RecordingRunner()
        self.offline = True
        self.args = type("Args", (), {"allow_root": allow_root})()


def test_from_app_reads_state_and_user() -> None:
    env = dict(os.environ)
    ctx = DoctorCtx.from_app(_App(env))
    assert ctx.user == USER
    assert ctx.offline
    assert not ctx.allow_root
    assert ctx.state_error == ""
    assert ctx.user_error == ""
    assert ctx.layout.user == USER
    assert ctx.layout is ctx.layout
    assert ctx.host_home == Path(env["HOME"])
    assert ctx.data_home == ctx.paths.data_dir.parent
    assert DoctorCtx.from_app(_App(env, allow_root=True)).allow_root


def test_from_app_survives_corrupt_state_and_bad_user() -> None:
    env = dict(os.environ)
    env["USER"] = "a/b"
    app = _App(env)
    app.paths.state_file.parent.mkdir(parents=True)
    app.paths.state_file.write_text("{not json", encoding="utf-8")
    ctx = DoctorCtx.from_app(app)
    assert "unusable" in ctx.state_error
    assert "a/b" in ctx.user_error
    assert ctx.user == ""
    with pytest.raises(UsageError):
        assert ctx.layout is None
    assert run(ctx, "env.user").status == "fail"
    assert run(ctx, "fork.installed").status == "fail"
    prefix(ctx)
    result = run(ctx, "prefix.exists")
    assert result.status == "fail"
    assert "unusable" in result.detail
    assert run(ctx, "desktop.entry").status == "fail"


def test_wine_is_resolved_once_and_reset() -> None:
    ctx = make()
    assert ctx.wine() is None
    assert "not installed" in ctx.wine_error()
    info = wine_info(ctx.paths.data_dir / "w")
    ctx._wine_error = None
    ctx._wine = info
    assert ctx.wine() is info
    assert ctx.wine_error() == ""
    boot = ctx.boot
    assert boot is ctx.boot
    assert boot.wine() is info
    assert boot.state is ctx.state
    ctx.reset()
    assert ctx.wine() is None
    assert ctx.boot.state is ctx.state
    make_prefix(ctx.paths)
    assert set(ctx.pathmap.drives()) == {"c", "z"}


# -- env -----------------------------------------------------------------------------------------


def test_env_arch(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = make()
    monkeypatch.setattr(doctor, "_machine", lambda: "x86_64")
    assert run(ctx, "env.arch") == Result("ok", "x86_64")
    monkeypatch.setattr(doctor, "_machine", lambda: "aarch64")
    assert run(ctx, "env.arch").status == "fail"
    monkeypatch.setattr(doctor, "_machine", lambda: "")
    assert "unknown" in run(ctx, "env.arch").detail


def test_env_python(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = make()
    assert run(ctx, "env.python").status == "ok"
    monkeypatch.setattr(doctor, "_python", lambda: (3, 9, 18))
    assert run(ctx, "env.python") == Result("fail", "Python 3.9.18 is too old", "install Python 3.10 or newer")


def test_env_user(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "_euid", lambda: 1000)
    assert run(make(), "env.user") == Result("ok", "tester (C:\\users\\tester)")
    monkeypatch.setattr(doctor, "_euid", lambda: 0)
    assert run(make(), "env.user").status == "fail"
    assert run(make(allow_root=True), "env.user").status == "warn"


@pytest.mark.parametrize(
    ("env", "status", "words"),
    [
        ({"DISPLAY": ":0"}, "ok", "X11 display :0"),
        ({"DISPLAY": ":96", "XDG_SESSION_TYPE": "wayland"}, "ok", "X11 display :96"),
        ({"XDG_SESSION_TYPE": "x11"}, "warn", "x11 declared by XDG_SESSION_TYPE"),
        ({"DISPLAY": ":1", "WAYLAND_DISPLAY": "wayland-0"}, "ok", "XWayland"),
        ({"WAYLAND_DISPLAY": "wayland-0"}, "fail", "without XWayland"),
        ({"WAYLAND_DISPLAY": "wayland-0", "FORK_LINUX_WINE_DRIVER": "wayland"}, "warn", "experimental"),
        ({}, "warn", "no graphical session"),
    ],
)
def test_env_display(env: dict[str, str], status: str, words: str) -> None:
    result = run(make(env=env), "env.display")
    assert result.status == status
    assert words in result.detail


def test_env_disk(monkeypatch: pytest.MonkeyPatch) -> None:
    from fork_linux import fsutil

    ctx = make()
    gib = 1024**3
    monkeypatch.setattr(fsutil, "disk_free", lambda _p: 10 * gib)
    assert run(ctx, "env.disk").status == "ok"
    monkeypatch.setattr(fsutil, "disk_free", lambda _p: 1 * gib)
    assert run(ctx, "env.disk").status == "fail"
    prefix(ctx)
    assert run(ctx, "env.disk").status == "ok"


# -- host ----------------------------------------------------------------------------------------


def test_host_tools() -> None:
    have = {
        name: f"/usr/bin/{name}"
        for name in ("cabextract", "unzip", "7z", "zenity", "kdialog", "xdg-open", "git", "xdotool", "fc-list")
    }
    assert run(make(RecordingRunner(which_map=have)), "host.tools").status == "ok"
    missing_optional = dict(have, kdialog=None)
    result = run(make(RecordingRunner(which_map=missing_optional)), "host.tools")
    assert result.status == "info"
    assert "kdialog" in result.detail
    result = run(make(RecordingRunner(which_map=dict(have, cabextract=None))), "host.tools")
    assert result.status == "fail"
    assert result.detail == "missing: cabextract"


def _loader(missing: set[str]) -> Any:
    def load(soname: str) -> object:
        if soname in missing:
            raise OSError(soname)
        return object()

    return load


def _managed_runtime(ctx: DoctorCtx) -> WineInfo:
    build = ctx.manifest.wine_default
    root = wine_provider.managed_root(ctx.paths, build.id)
    (root / "bin").mkdir(parents=True)
    (root / "bin" / "wine").write_text("#!/bin/sh\n", encoding="utf-8")
    (root / "bin" / "wineserver").write_text("#!/bin/sh\n", encoding="utf-8")
    (root / wine_provider.COMPLETE_MARKER).write_text(build.sha256 + "\n", encoding="utf-8")
    unix = root / "lib" / "wine" / "x86_64-unix"
    unix.mkdir(parents=True)
    (unix / "winex11.so").write_bytes(b"\x7fELF")
    return wine_provider.managed_info(build, root)


def test_host_libs() -> None:
    ctx = make(lib_loader=_loader(set()))
    result = run(ctx, "host.libs")
    assert result.status == "ok"
    assert "ldd" not in result.detail
    result = run(make(lib_loader=_loader({"libvulkan.so.1"})), "host.libs")
    assert result.status == "info"
    assert "libvulkan.so.1" in result.detail
    result = run(make(lib_loader=_loader({"libX11.so.6"})), "host.libs")
    assert result.status == "fail"
    assert "libX11.so.6" in result.detail
    assert result.hint


def test_host_libs_scans_the_managed_runtime() -> None:
    runner = RecordingRunner({"ldd": "libfoo.so.1 => not found\nlibavcodec.so.61 => not found\n"})
    ctx = make(runner, lib_loader=_loader(set()))
    ctx._wine = _managed_runtime(ctx)
    result = run(ctx, "host.libs")
    assert result.status == "fail"
    assert result.detail == "the Wine runtime needs: libfoo.so.1"
    assert runner.argvs[0][0] == "ldd"
    clean = make(RecordingRunner({"ldd": ""}), lib_loader=_loader(set()))
    clean._wine = _managed_runtime_existing(clean)
    assert "scanned with ldd" in run(clean, "host.libs").detail


def _managed_runtime_existing(ctx: DoctorCtx) -> WineInfo:
    build = ctx.manifest.wine_default
    return wine_provider.managed_info(build, wine_provider.managed_root(ctx.paths, build.id))


def test_host_fonts() -> None:
    fc = {"fc-list": "/usr/bin/fc-list"}
    good = RecordingRunner({"fc-list": "Noto Sans\nNoto Sans Mono\n"}, which_map=fc)
    assert run(make(good), "host.fonts") == Result("ok", "Segoe UI -> Noto Sans, Consolas -> Noto Sans Mono")
    sans_only = RecordingRunner({"fc-list": "DejaVu Sans\n"}, which_map=fc)
    result = run(make(sans_only), "host.fonts")
    assert result.status == "warn"
    assert "monospace" in result.detail
    assert "Segoe" not in result.detail
    mono_only = RecordingRunner({"fc-list": "Noto Sans Mono\n"}, which_map=fc)
    result = run(make(mono_only), "host.fonts")
    assert "Segoe UI replacement" in result.detail
    assert "monospace" not in result.detail
    none = RecordingRunner({"fc-list": "Comic Neue\n"}, which_map=fc)
    assert "Segoe UI replacement" in run(make(none), "host.fonts").detail
    missing = RecordingRunner(which_map={"fc-list": None})
    assert "not available" in run(make(missing), "host.fonts").detail
    failing = RecordingRunner({"fc-list": 1}, which_map=fc)
    assert "not available" in run(make(failing), "host.fonts").detail

    def raises(_argv: list[str]) -> Any:
        raise ForkLinuxError("timeout")

    assert "not available" in run(make(RecordingRunner({"fc-list": raises}, which_map=fc)), "host.fonts").detail


# -- wine ----------------------------------------------------------------------------------------


def test_wine_present_and_version_managed(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = make()
    assert run(ctx, "wine.present").status == "fail"
    assert run(ctx, "wine.version") == Result("info", "skipped: no usable Wine")
    assert run(ctx, "wine.staging").status == "info"
    assert run(ctx, "wine.wineserver").status == "info"
    _managed_runtime(ctx)
    ctx = make()
    present = run(ctx, "wine.present")
    assert present.status == "ok"
    assert "managed kron4ek" in present.detail
    assert "(Staging)" in present.detail
    assert "sha256 matches" in run(ctx, "wine.version").detail
    assert run(ctx, "wine.staging").status == "ok"
    monkeypatch.setattr(wine_provider, "is_installed", lambda _p, _b: False)
    assert run(ctx, "wine.version").status == "fail"


@pytest.mark.parametrize(
    ("version", "status"),
    [("9.0", "ok"), ("10.2", "ok"), ("8.0", "fail"), ("", "fail")],
)
def test_wine_version_system(version: str, status: str) -> None:
    ctx = make(wine=wine_info(Path("/usr"), provider="system", version=version, staging=False))
    assert run(ctx, "wine.version").status == status
    present = run(ctx, "wine.present")
    assert present.status == "ok"
    assert "Staging" not in present.detail
    staging = run(ctx, "wine.staging")
    assert staging.status == "warn"
    assert "55138" in staging.detail


def _proc(root: Path, pid: int, environ: dict[str, str] | None, exe: str | None = None) -> None:
    entry = root / str(pid)
    entry.mkdir(parents=True)
    if environ is not None:
        (entry / "environ").write_bytes(b"\0".join(f"{k}={v}".encode() for k, v in environ.items()) + b"\0")
    (entry / "cmdline").write_bytes(b"wine\0")
    if exe is not None:
        (entry / "exe").symlink_to(exe)


def test_wineserver_mismatch(tmp_path: Path) -> None:
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    ctx = make(proc_root=proc_root)
    info = wine_info(tmp_path / "ours")
    ctx._wine = info
    pfx = str(ctx.paths.prefix)
    assert run(ctx, "wine.wineserver").status == "ok"
    _proc(proc_root, 101, {"WINEPREFIX": pfx, "WINESERVER": str(info.wineserver)})
    _proc(proc_root, 102, {"WINEPREFIX": pfx, "WINESERVER": "relative/wineserver"})
    _proc(proc_root, 103, {"WINEPREFIX": "/elsewhere", "WINESERVER": "/opt/other/bin/wineserver"})
    _proc(proc_root, 104, {"WINEPREFIX": pfx}, exe="/usr/bin/python3")
    _proc(proc_root, 107, {"WINEPREFIX": pfx}, exe=str(tmp_path / "ours" / "lib" / "wine" / "wineserver64"))
    assert run(ctx, "wine.wineserver").status == "ok"
    _proc(proc_root, 105, {"WINEPREFIX": pfx, "WINESERVER": "/opt/other/bin/wineserver"})
    _proc(proc_root, 106, {"WINEPREFIX": pfx}, exe=str(tmp_path / "other" / "wineserver64"))
    result = run(ctx, "wine.wineserver")
    assert result.status == "fail"
    assert "pid 105 (/opt/other/bin/wineserver)" in result.detail
    assert "pid 106" in result.detail
    assert doctor.foreign_wineservers(ctx, info) == [
        (105, "/opt/other/bin/wineserver"),
        (106, str(tmp_path / "other" / "wineserver64")),
    ]


def test_wineserver_process_vanishing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    proc_root = tmp_path / "proc"
    _proc(proc_root, 200, None)
    ctx = make(proc_root=proc_root, wine=wine_info(tmp_path / "ours"))
    monkeypatch.setattr(procs, "prefix_pids", lambda _prefix, proc_root: [200])
    assert doctor.foreign_wineservers(ctx, ctx.wine()) == []


# -- prefix --------------------------------------------------------------------------------------


def test_prefix_exists() -> None:
    ctx = make()
    assert run(ctx, "prefix.exists").status == "fail"
    make_prefix(ctx.paths)
    result = run(ctx, "prefix.exists")
    assert result.status == "fail"
    assert "system.reg is missing" in result.detail
    pfx = prefix(ctx)
    assert run(ctx, "prefix.exists") == Result("ok", f"{pfx} (win64)")
    os.chmod(pfx, 0o755)
    assert run(ctx, "prefix.exists").status == "warn"
    ctx.paths.created_by_marker.unlink()
    assert "created-by" in run(ctx, "prefix.exists").detail
    (pfx / "user.reg").write_text("WINE REGISTRY Version 2\n#arch=win32\n", encoding="utf-8")
    assert "user.reg" in run(ctx, "prefix.exists").detail


def test_prefix_checks_skip_without_prefix() -> None:
    ctx = make()
    for check_id in (
        "prefix.dotnet",
        "prefix.corefonts",
        "prefix.font_replacements",
        "prefix.avalon",
        "prefix.appdefaults",
        "prefix.dpi",
        "git.overlay",
    ):
        assert run(ctx, check_id) == Result("info", "skipped: the Wine prefix does not exist yet"), check_id


def test_prefix_dotnet() -> None:
    ctx = make()
    pfx = prefix(ctx)
    assert run(ctx, "prefix.dotnet").detail == ".NET Framework 4.x is not installed"
    write_release(pfx, 400000)
    assert "older than" in run(ctx, "prefix.dotnet").detail
    write_release(pfx, 528049)
    assert run(ctx, "prefix.dotnet") == Result("ok", ".NET Framework release 528049 (>= 461808)")


def test_prefix_corefonts() -> None:
    ctx = make()
    pfx = prefix(ctx)
    assert run(ctx, "prefix.corefonts").status == "fail"
    fonts = pfx / "drive_c" / "windows" / "Fonts"
    fonts.mkdir(parents=True)
    (fonts / "ARIAL.TTF").write_bytes(b"x")
    # An interrupted corefonts (E2E): Arial is there but winetricks never finished the verb.
    assert run(ctx, "prefix.corefonts").status == "warn"
    (pfx / "winetricks.log").write_text("arial\ncorefonts\n", encoding="utf-8")
    assert run(ctx, "prefix.corefonts") == Result("ok", "core fonts installed (ARIAL.TTF)")


def test_prefix_font_replacements() -> None:
    runner = RecordingRunner({"fc-list": "Noto Sans\nDejaVu Sans Mono\n"})
    ctx = make(runner)
    prefix(ctx)
    result = run(ctx, "prefix.font_replacements")
    assert result.status == "warn"
    assert "Segoe UI" in result.detail
    assert "Consolas" in result.detail
    lines = [f'"{name}"="Noto Sans"' for name in ("Segoe UI", "Segoe UI Semibold", "Segoe UI Light")]
    user_values(ctx, FONT_KEY, *lines, '"Consolas"="DejaVu Sans Mono"')
    result = run(ctx, "prefix.font_replacements")
    assert result.status == "warn"
    assert result.detail.endswith("Segoe UI Symbol")
    user_values(ctx, FONT_KEY, '"Segoe UI Symbol"="Noto Sans"')
    result = run(ctx, "prefix.font_replacements")
    assert result.status == "ok"
    assert "Consolas -> DejaVu Sans Mono" in result.detail


def test_prefix_avalon_and_appdefaults() -> None:
    ctx = make()
    prefix(ctx)
    avalon = run(ctx, "prefix.avalon")
    assert avalon.status == "fail"
    assert "DisableHWAcceleration is None" in avalon.detail
    user_values(ctx, "Software\\Microsoft\\Avalon.Graphics", '"DisableHWAcceleration"=dword:00000001')
    assert run(ctx, "prefix.avalon") == Result("ok", "WPF hardware acceleration off")
    user_values(ctx, "Software\\Wine\\AppDefaults\\Fork.exe", '"Version"="win10"')
    assert "'win10', expected 'win7'" in run(ctx, "prefix.appdefaults").detail
    user_values(ctx, "Software\\Wine\\AppDefaults\\Fork.exe", '"Version"="win7"')
    assert run(ctx, "prefix.appdefaults").status == "ok"


LNK = b"L\0\0\0\x01\x14\x02\0" + b"\0" * 68


def _menu_text(prefix_path: Path) -> str:
    return f'[Desktop Entry]\nExec=env WINEPREFIX="{prefix_path}" wine C:\\\\users\\\\x\\\\Fork.lnk\n'


def _leftovers(ctx: DoctorCtx) -> list[Path]:
    wine_apps = ctx.data_home / "applications" / "wine" / "Programs"
    wine_apps.mkdir(parents=True)
    menu = wine_apps / "Fork.desktop"
    menu.write_text(_menu_text(ctx.paths.prefix), encoding="utf-8")
    (wine_apps / "Other.desktop").write_text(_menu_text(ctx.paths.prefix), encoding="utf-8")
    (wine_apps / "Fork-link.desktop").symlink_to(menu)
    desktop = ctx.host_home / "Desktop"
    desktop.mkdir()
    lnk = desktop / "Fork.lnk"
    lnk.write_bytes(LNK)
    return [menu, lnk]


def test_prefix_menubuilder(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = make()
    assert run(ctx, "prefix.menubuilder").status == "info"
    leftovers = _leftovers(ctx)
    # Without our prefix, a desktop Fork.lnk cannot be ours; the menu entry names our prefix.
    assert doctor.menubuilder_leftovers(ctx) == leftovers[:1]
    assert run(ctx, "prefix.menubuilder").status == "warn"
    prefix(ctx)
    os.utime(ctx.paths.created_by_marker, (1000, 1000))
    assert doctor.menubuilder_leftovers(ctx) == leftovers
    result = run(ctx, "prefix.menubuilder")
    assert result.status == "fail"
    assert "not disabled" in result.detail
    ran: list[Any] = []
    monkeypatch.setattr(bootstrap, "run_steps", lambda boot, only: ran.append(only) or list(only))
    message = doctor.fix_menubuilder(ctx)
    assert message == f"deleted {leftovers[0]}; deleted {leftovers[1]}; re-ran setup step registry"
    assert ran == [["registry"]]
    assert not any(path.exists() for path in leftovers)
    assert (ctx.data_home / "applications" / "wine" / "Programs" / "Other.desktop").exists()
    user_values(ctx, "Software\\Wine\\DllOverrides", '"winemenubuilder.exe"=""')
    assert run(ctx, "prefix.menubuilder") == Result("ok", "winemenubuilder disabled; no leftovers")
    assert doctor.fix_menubuilder(ctx) == "nothing to do"
    (ctx.host_home / "Desktop" / "Fork.lnk").mkdir()
    assert doctor.menubuilder_leftovers(ctx) == []


def test_menubuilder_leaves_other_prefixes_and_older_links_alone(tmp_path: Path) -> None:
    ctx = make()
    prefix(ctx)
    wine_apps = ctx.data_home / "applications" / "wine" / "Programs"
    wine_apps.mkdir(parents=True)
    (wine_apps / "Fork.desktop").write_text(_menu_text(tmp_path / ".wine"), encoding="utf-8")
    (wine_apps / "Fork (2).desktop").write_text("[Desktop Entry]\nExec=wine Fork.exe\n", encoding="utf-8")
    escaped = str(ctx.paths.prefix).replace("/", "\\/")
    (wine_apps / "Fork escaped.desktop").write_text(f'Exec=env WINEPREFIX="{escaped}" wine x\n', encoding="utf-8")
    desktop = ctx.host_home / "Desktop"
    desktop.mkdir()
    lnk = desktop / "Fork.lnk"
    lnk.write_bytes(LNK)
    os.utime(lnk, (1000, 1000))
    (wine_apps / "Fork dir.desktop").mkdir()
    assert doctor.menubuilder_leftovers(ctx) == [wine_apps / "Fork escaped.desktop"]
    lnk.write_bytes(b"not a shell link")
    assert lnk not in doctor.menubuilder_leftovers(ctx)


def test_menubuilder_ignores_a_linked_wine_menu(tmp_path: Path) -> None:
    ctx = make()
    target = tmp_path / "elsewhere"
    target.mkdir()
    (target / "Fork.desktop").write_text("x", encoding="utf-8")
    (ctx.data_home / "applications").mkdir(parents=True)
    (ctx.data_home / "applications" / "wine").symlink_to(target)
    assert doctor.menubuilder_leftovers(ctx) == []


def test_prefix_dpi() -> None:
    ctx = make()
    prefix(ctx)
    assert run(ctx, "prefix.dpi").status == "warn"
    user_values(ctx, "Control Panel\\Desktop", '"LogPixels"=dword:00000060')
    assert run(ctx, "prefix.dpi") == Result("ok", "96 DPI")


# -- fork ----------------------------------------------------------------------------------------


def test_fork_installed() -> None:
    ctx = make()
    assert run(ctx, "fork.installed").status == "fail"
    install_fork(ctx.layout, "2.23.2")
    assert run(ctx, "fork.installed").detail.startswith("Fork 2.23.2 in ")


def test_fork_checks_skip_without_fork() -> None:
    ctx = make()
    for check_id in ("fork.version", "fork.integrity", "fork.gitinstance", "fork.pending_update"):
        assert run(ctx, check_id) == Result("info", "skipped: Fork is not installed"), check_id


def _bad_manifest(version: str) -> manifest_mod.Manifest:
    return manifest_with(lambda data: data["fork"].__setitem__("known_bad", {version: "crashes on start"}))


def test_fork_version_statuses() -> None:
    ctx = make()
    install_fork(ctx.layout, "2.23.2")
    assert run(ctx, "fork.version") == Result("ok", "Fork 2.23.2 is known-good")
    install_fork(ctx.layout, "2.24.0")
    result = run(ctx, "fork.version")
    assert result.status == "warn"
    assert "2.24.0 is untested" in result.detail
    ctx = make(manifest=_bad_manifest("2.24.0"))
    result = run(ctx, "fork.version")
    assert result.status == "fail"
    assert "crashes on start" in result.detail


def test_fork_version_testing_status() -> None:
    def change(data: dict[str, Any]) -> None:
        entry = dict(data["fork"]["versions"]["2.23.2"], status="testing")
        data["fork"]["versions"]["2.24.0"] = entry

    ctx = make(manifest=manifest_with(change))
    install_fork(ctx.layout, "2.24.0")
    assert "2.24.0 is testing" in run(ctx, "fork.version").detail


def _feed(*version_list: str) -> str:
    assets = [
        {
            "PackageId": "Fork",
            "Version": v,
            "Type": "Full",
            "FileName": f"Fork-{v}-full.nupkg",
            "SHA256": "a" * 64,
            "Size": 10,
        }
        for v in version_list
    ]
    return json.dumps({"Assets": assets})


def test_fork_version_with_network(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = make()
    ctx.network = True
    install_fork(ctx.layout, "2.23.2")
    monkeypatch.setattr(feeds, "fetch_feed", lambda *a, **k: _feed("2.23.2", "2.24.1"))
    assert run(ctx, "fork.version").detail == "Fork 2.23.2 is known-good; Fork 2.24.1 is available"
    monkeypatch.setattr(feeds, "fetch_feed", lambda *a, **k: _feed("2.23.2"))
    assert run(ctx, "fork.version").detail == "Fork 2.23.2 is known-good"

    def offline(*_a: Any, **_k: Any) -> str:
        raise ForkLinuxError("no network")

    monkeypatch.setattr(feeds, "fetch_feed", offline)
    assert run(ctx, "fork.version").detail.endswith("Fork's release feed is unavailable")


def test_fork_integrity() -> None:
    from fork_linux import fsutil

    ctx = make()
    install_fork(ctx.layout, "2.24.0")
    assert run(ctx, "fork.integrity").status == "info"
    install_fork(ctx.layout, "2.23.2")
    result = run(ctx, "fork.integrity")
    assert result.status == "fail"
    assert "does not match" in result.detail
    # Fork updated itself (fork-linux installed 2.23.1): Velopack's delta-rebuilt package differs (spike S9).
    ctx.state.set("fork.version", "2.23.1")
    result = run(ctx, "fork.integrity")
    assert result.status == "info"
    assert "rebuilt by Fork's own updater" in result.detail
    ctx.state.set("fork.version", "2.23.2")
    assert run(ctx, "fork.integrity").status == "fail"
    ctx.state.set("fork.version", "not-a-version")
    assert run(ctx, "fork.integrity").status == "fail"
    package = ctx.layout.packages_dir / "Fork-2.23.2-full.nupkg"
    sha = fsutil.sha256_file(package)

    def pin(data: dict[str, Any]) -> None:
        data["fork"]["versions"]["2.23.2"]["full_nupkg_sha256"] = sha.upper()

    good = make(manifest=manifest_with(pin))
    assert run(good, "fork.integrity").status == "ok"
    package.unlink()
    assert run(good, "fork.integrity").status == "warn"


def test_fork_gitinstance() -> None:
    ctx = make()
    install_fork(ctx.layout)
    assert run(ctx, "fork.gitinstance").status == "info"
    # E2E: settings.json is seeded before the first start; only fork.log shows that Fork ran.
    write_settings(ctx.layout, {"Guid": GUID})
    ctx.layout.logs_dir.mkdir(parents=True, exist_ok=True)
    ctx.layout.fork_log.write_text("Found fast exit hook: --veloapp-install\n", encoding="utf-8")
    assert run(ctx, "fork.gitinstance").status == "info"
    ctx.layout.fork_log.write_text("Start IPC server Fork_Pipe32_Default\n", encoding="utf-8")
    assert run(ctx, "fork.gitinstance").status == "warn"
    git = ctx.layout.gitinstance_dir / "2.50.1" / "cmd"
    git.mkdir(parents=True)
    (git / "git.exe").write_bytes(b"MZ")
    assert run(ctx, "fork.gitinstance") == Result("ok", "bundled git 2.50.1")


def test_fork_settings() -> None:
    ctx = make()
    assert run(ctx, "fork.settings").status == "info"
    write_settings(
        ctx.layout, {"Guid": GUID, "UpdateSubmodulesOnCheckout": False, "DisableHardwareAcceleration": True, "Theme": 1}
    )
    assert run(ctx, "fork.settings") == Result("ok", "4 settings, enforced values in place")
    write_settings(ctx.layout, {"Guid": "nope", "UpdateSubmodulesOnCheckout": True})
    result = run(ctx, "fork.settings")
    assert result.status == "warn"
    assert "no valid Guid" in result.detail
    assert "UpdateSubmodulesOnCheckout is True" in result.detail
    assert "DisableHardwareAcceleration is None" in result.detail
    ctx.layout.settings_file.write_text("{broken", encoding="utf-8")
    assert run(ctx, "fork.settings").status == "fail"
    real = ctx.layout.settings_file.with_name("real.json")
    real.write_text("{}", encoding="utf-8")
    ctx.layout.settings_file.unlink()
    ctx.layout.settings_file.symlink_to(real)
    assert "symbolic link" in run(ctx, "fork.settings").detail


def test_fork_settings_ignores_keys_without_defaults() -> None:
    ctx = make(env={"FORK_LINUX_FORK_ENFORCE_SETTINGS": "Theme"})
    write_settings(ctx.layout, {"Guid": GUID, "Theme": 0})
    assert run(ctx, "fork.settings").status == "ok"


def test_fork_pending_update(monkeypatch: pytest.MonkeyPatch) -> None:
    from fork_linux import snapshots

    ctx = make()
    install_fork(ctx.layout, "2.23.2")
    assert run(ctx, "fork.pending_update") == Result("ok", "no staged Fork update")
    for version in ("2.24.0", "2.23.10"):
        (ctx.layout.packages_dir / f"Fork-{version}-full.nupkg").write_bytes(b"PK")
    (ctx.layout.packages_dir / "Fork-2.24.0-delta.nupkg").write_bytes(b"PK")
    result = run(ctx, "fork.pending_update")
    assert result.status == "warn"
    assert "2.23.10, 2.24.0" in result.detail
    assert "no snapshot" in result.detail
    snap = snapshots.Snapshot("s1", "2.23.2", "2026-01-01T00:00:00+00:00", "copy", ctx.paths.snapshots_dir, False)
    odd = snapshots.Snapshot("s0", "weird", "2026-01-01T00:00:00+00:00", "copy", ctx.paths.snapshots_dir, False)
    monkeypatch.setattr(snapshots, "list_snapshots", lambda _paths: [odd, snap])
    assert run(ctx, "fork.pending_update").status == "info"
    pinned = make(env={"FORK_LINUX_FORK_UPDATE_POLICY": "pinned"})
    result = run(pinned, "fork.pending_update")
    assert result.status == "warn"
    assert "pinned" in result.detail


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("all good\n", []),
        ("COMException 0x88980406\n", ["WPF render thread failure (0x88980406)"]),
        ("TypeLoadException: Windows.UI.Notifications\n", ["Fork tried to load Windows 10 (WinRT) types"]),
        ("TypeLoadException: Foo\n", []),
        (
            "NotificationManager failed: Windows.Data.Xml.Dom\n",
            ["Windows notifications are unavailable under Wine (fork-dev/TrackerWin#2862)"],
        ),
        ("schannel: handshake failed\n", ["TLS (SSL/schannel) errors"]),
    ],
)
def test_scan_fork_log(text: str, expected: list[str]) -> None:
    assert [what for what, _hint in doctor.scan_fork_log(text)] == expected


def test_fork_log_signatures(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = make()
    assert run(ctx, "fork.log_signatures") == Result("info", "no fork.log yet")
    log = ctx.layout.fork_log
    log.parent.mkdir(parents=True)
    log.write_text("started\n", encoding="utf-8")
    assert run(ctx, "fork.log_signatures").status == "ok"
    monkeypatch.setattr(doctor, "LOG_TAIL_BYTES", 64)
    log.write_text("0x88980406 early\n" + "x" * 200 + "\nSSL error\n", encoding="utf-8")
    result = run(ctx, "fork.log_signatures")
    assert result.status == "warn"
    assert "TLS" in result.detail
    assert "0x88980406" not in result.detail
    log.unlink()
    os.mkfifo(log)
    assert run(ctx, "fork.log_signatures").status == "info"


# -- ssh / git / bridge --------------------------------------------------------------------------


def test_ssh_checks_when_not_wanted() -> None:
    ctx = make()
    assert run(ctx, "ssh.dir") == Result("info", "no ~/.ssh to share")
    assert run(ctx, "ssh.config") == Result("info", "no ~/.ssh to share")
    off = make(env={"FORK_LINUX_SSH_SYNC": "off"})
    assert "off" in run(off, "ssh.dir").detail


def test_ssh_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    from fork_linux import ssh_sync

    ctx = make(env={"FORK_LINUX_SSH_SYNC": "on"})
    assert run(ctx, "ssh.dir").status == "info"
    assert run(ctx, "ssh.config").status == "info"
    prefix(ctx)
    base = {
        "exists": True,
        "is_symlink": False,
        "perms_ok": True,
        "mode": "0700",
        "insecure_files": [],
        "broken_links": [],
        "managed": ["id_ed25519"],
        "wine_dir": "/w/.ssh",
        "config": "none",
    }
    status: dict[str, Any] = dict(base)
    monkeypatch.setattr(ssh_sync, "status", lambda *_a: status)
    assert run(ctx, "ssh.dir") == Result("ok", "1 file(s) shared in /w/.ssh")
    status.update(exists=False)
    assert "does not exist" in run(ctx, "ssh.dir").detail
    status.update(exists=True, is_symlink=True)
    assert "symbolic link" in run(ctx, "ssh.dir").detail
    status.update(is_symlink=False, perms_ok=False, mode="0755", broken_links=["id_rsa"])
    assert run(ctx, "ssh.dir").detail == "mode 0755; broken links: id_rsa"
    status.update(insecure_files=["id_ed25519"], broken_links=[])
    assert run(ctx, "ssh.dir").detail == "mode 0755; readable by others: id_ed25519"
    for state, level in (
        ("none", "ok"),
        ("current", "ok"),
        ("foreign", "info"),
        ("stale", "warn"),
        ("missing", "warn"),
    ):
        status["config"] = state
        assert run(ctx, "ssh.config").status == level, state


def test_git_overlay() -> None:
    ctx = make()
    prefix(ctx)
    result = run(ctx, "git.overlay")
    assert result.status == "warn"
    assert "core.filemode=false, core.autocrlf=false" in result.detail
    from fork_linux import gitconfig

    overlay = gitconfig.overlay_path(ctx.paths, USER)
    overlay.parent.mkdir(parents=True)
    overlay.write_text("# x\n", encoding="utf-8")
    (ctx.paths.wine_user_dir(USER) / ".gitconfig").write_text(gitconfig.ensure_block(""), encoding="utf-8")
    assert "predates" in run(ctx, "git.overlay").detail
    overlay.write_text("# x\n" + gitconfig.MANAGED_HEADER, encoding="utf-8")
    assert run(ctx, "git.overlay").status == "ok"
    ctx.paths.config_file.parent.mkdir(parents=True, exist_ok=True)
    ctx.paths.config_file.write_text("[git]\nconfig_overlay = off\nenv_overrides =\n", encoding="utf-8")
    assert run(make(), "git.overlay") == Result("info", "git config overlay is off; no env overrides")
    bad = make(env={"FORK_LINUX_GIT_ENV_OVERRIDES": "nonsense"})
    assert run(bad, "git.overlay").status == "fail"


class GitFake:
    """Answers the bundled git's commands run through ``wine``."""

    def __init__(self, *, status: str = "", fail: str = "") -> None:
        self.status = status
        self.fail = fail

    def __call__(self, argv: list[str]) -> Any:
        args = argv[2:]
        if self.fail and self.fail in args:
            return Completed(argv, 1, "", "fatal: broken\n")
        if args == ["--version"]:
            return "git version 2.50.1.windows.1\n"
        if "status" in args:
            return self.status
        return 0


def _git_ready(ctx: DoctorCtx) -> None:
    prefix(ctx)
    install_fork(ctx.layout)
    git = ctx.layout.gitinstance_dir / "2.50.1" / "cmd"
    git.mkdir(parents=True)
    (git / "git.exe").write_bytes(b"MZ")


def test_git_selftest_skips() -> None:
    ctx = make()
    assert run(ctx, "git.selftest") == Result("info", "skipped: no usable Wine")
    ctx = make(wine=wine_info(Path("/opt/wine")))
    assert "not unpacked" in run(ctx, "git.selftest").detail


def test_git_selftest_runs_bundled_git(tmp_path: Path) -> None:
    runner = RecordingRunner({"wine": GitFake()})
    ctx = make(runner, wine=wine_info(tmp_path / "wine"))
    _git_ready(ctx)
    result = run(ctx, "git.selftest")
    assert result == Result(
        "ok", "git version 2.50.1.windows.1 works; a fresh repository is clean", doctor.HOOK_LIMITATION
    )
    git_win = "C:\\users\\tester\\AppData\\Local\\Fork\\gitInstance\\2.50.1\\cmd\\git.exe"
    assert all(argv[1] == git_win for argv in runner.argvs)
    assert [argv[2] for argv in runner.argvs][:2] == ["--version", "init"]
    env = runner.calls[-1]["env"]
    assert env["GIT_CONFIG_KEY_0"] == "core.filemode"
    assert env["WINEPREFIX"] == str(ctx.paths.prefix)
    repo_win = runner.argvs[1][-1]
    assert repo_win.startswith("Z:\\")
    assert not list(ctx.paths.cache_dir.glob("doctor-git-*"))


def test_git_selftest_problems(tmp_path: Path) -> None:
    dirty = make(RecordingRunner({"wine": GitFake(status=" M run.sh\n")}), wine=wine_info(tmp_path / "w"))
    _git_ready(dirty)
    result = run(dirty, "git.selftest")
    assert result.status == "warn"
    assert "M run.sh" in result.detail
    plain = make(RecordingRunner({"wine": GitFake()}), wine=wine_info(tmp_path / "w", staging=False))
    assert run(plain, "git.selftest").status == "warn"
    broken = make(RecordingRunner({"wine": GitFake(fail="commit")}), wine=wine_info(tmp_path / "w"))
    result = run(broken, "git.selftest")
    assert result.status == "fail"
    assert "git -c" in result.detail
    assert "fatal: broken" in result.detail
    silent = make(RecordingRunner({"wine": GitFake(fail="--version")}), wine=wine_info(tmp_path / "w"))
    assert run(silent, "git.selftest").status == "fail"
    assert not list(silent.paths.cache_dir.glob("doctor-git-*"))


def test_git_selftest_failure_without_output(tmp_path: Path) -> None:
    ctx = make(RecordingRunner({"wine": 3}), wine=wine_info(tmp_path / "w"))
    _git_ready(ctx)
    assert run(ctx, "git.selftest").detail.endswith("git --version exited with 3")


def test_bridge_status(monkeypatch: pytest.MonkeyPatch) -> None:
    from fork_linux import bridge

    ctx = make()
    info: dict[str, Any] = {
        "enabled": False,
        "available": False,
        "ready": False,
        "reason": "not built",
        "mode": "bridge",
        "git_version": "2.53.0",
        "git_recommended": True,
        "daemon": {},
    }
    seen: list[Any] = []
    monkeypatch.setattr(bridge, "status", lambda _ctx, session=None: seen.append(session) or info)
    assert run(ctx, "bridge.status").status == "info"
    assert seen == [None], "no session.json: no daemon"
    info.update(enabled=True)
    assert run(ctx, "bridge.status") == Result("fail", "not built", "run 'fork-linux git-bridge disable'")
    info.update(available=True, reason="the shims are not installed in the prefix")
    result = run(ctx, "bridge.status")
    assert result.status == "warn"
    assert "falls back to its bundled git: the shims" in result.detail
    assert "fork-linux setup" in result.hint
    info.update(ready=True, reason="")
    assert run(ctx, "bridge.status") == Result(
        "ok", "native-git bridge enabled (experimental): Linux git 2.53.0; starts with Fork"
    )
    info.update(daemon={"pid": 7, "port": 4242}, mode="record")
    detail = run(ctx, "bridge.status").detail
    assert detail.endswith("record mode: Fork's bundled git 2.53.0; daemon running (pid 7, port 4242)")
    info.update(git_recommended=False)
    result = run(ctx, "bridge.status")
    assert result.status == "warn"
    assert "2.50 or newer" in result.hint
    info.update(enabled=False, ready=False, reason="invalid value")
    assert run(ctx, "bridge.status").status == "warn"


# -- desktop -------------------------------------------------------------------------------------


def _menu(ctx: DoctorCtx) -> Path:
    from fork_linux import desktop_integration

    menu = ctx.data_home / "applications" / f"{desktop_integration.APP_ID}.desktop"
    menu.parent.mkdir(parents=True, exist_ok=True)
    menu.write_text("[Desktop Entry]\n", encoding="utf-8")
    return menu


def test_desktop_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    no_validator = RecordingRunner(which_map={"desktop-file-validate": None})
    ctx = make(no_validator, env={"XDG_DATA_DIRS": "/nonexistent"})
    assert run(ctx, "desktop.entry").status == "fail"
    menu = _menu(ctx)
    assert run(ctx, "desktop.entry") == Result("ok", f"menu entry {menu}")
    install_fork(ctx.layout)
    result = run(ctx, "desktop.entry")
    assert result.status == "warn"
    assert "icon" in result.detail
    icon = ctx.data_home / "icons" / "hicolor" / "48x48" / "apps" / "io.github.ventura8.ForkLinux.png"
    icon.parent.mkdir(parents=True)
    icon.write_bytes(b"png")
    assert run(ctx, "desktop.entry").status == "ok"
    ctx.state.set("desktop.icons", [str(icon), 3])
    assert run(ctx, "desktop.entry").status == "ok"
    ctx.state.set("desktop.icons", [str(icon) + ".gone"])
    assert run(ctx, "desktop.entry").status == "warn"


def test_desktop_entry_validation() -> None:
    validate = {"desktop-file-validate": "/usr/bin/desktop-file-validate"}
    bad = RecordingRunner({"desktop-file-validate": "file: error: missing key\n"}, which_map=validate)
    ctx = make(bad, env={"XDG_DATA_DIRS": "/nonexistent"})
    menu = _menu(ctx)
    result = run(ctx, "desktop.entry")
    assert result.status == "warn"
    assert "missing key" in result.detail
    assert bad.argvs == [["/usr/bin/desktop-file-validate", str(menu)]]
    failing = RecordingRunner({"desktop-file-validate": Completed([], 1, "", "boom\n")}, which_map=validate)
    assert "boom" in run(make(failing, env={"XDG_DATA_DIRS": "/nonexistent"}), "desktop.entry").detail
    good = RecordingRunner({"desktop-file-validate": ""}, which_map=validate)
    assert run(make(good, env={"XDG_DATA_DIRS": "/nonexistent"}), "desktop.entry").status == "ok"


def test_desktop_entry_from_a_package(tmp_path: Path) -> None:
    system = tmp_path / "usr-share"
    (system / "applications").mkdir(parents=True)
    packaged = system / "applications" / "io.github.ventura8.ForkLinux.desktop"
    packaged.write_text("[Desktop Entry]\n", encoding="utf-8")
    ctx = make(RecordingRunner(which_map={"desktop-file-validate": None}), env={"XDG_DATA_DIRS": str(system)})
    assert run(ctx, "desktop.entry") == Result("ok", f"menu entry {packaged}")


def test_fix_desktop(monkeypatch: pytest.MonkeyPatch) -> None:
    ran: list[Any] = []
    monkeypatch.setattr(bootstrap, "run_steps", lambda boot, only: ran.append(only))
    assert doctor.fix_desktop(make()) == "re-ran setup steps icon, desktop_entry"
    assert ran == [["icon", "desktop_entry"]]


def test_desktop_cli(tmp_path: Path) -> None:
    assert run(make(RecordingRunner(which_map={"fork": None})), "desktop.cli").status == "info"
    other = tmp_path / "fork"
    other.write_text("#!/bin/sh\necho spoon\n", encoding="utf-8")
    result = run(make(RecordingRunner(which_map={"fork": str(other)})), "desktop.cli")
    assert result.status == "warn"
    assert str(other) in result.detail
    wrapper = tmp_path / "wrapper"
    wrapper.write_text("#!/bin/sh\n# Fork for Linux (unofficial)\nexec x\n", encoding="utf-8")
    assert run(make(RecordingRunner(which_map={"fork": str(wrapper)})), "desktop.cli").status == "ok"
    named = tmp_path / "bin" / "fork-linux"
    named.parent.mkdir()
    named.write_text("", encoding="utf-8")
    link = tmp_path / "bin" / "fork"
    link.symlink_to(named)
    assert run(make(RecordingRunner(which_map={"fork": str(link)})), "desktop.cli").status == "ok"
    gone = tmp_path / "gone"
    assert run(make(RecordingRunner(which_map={"fork": str(gone)})), "desktop.cli").status == "warn"
    fifo = tmp_path / "fifo-fork"
    os.mkfifo(fifo)
    assert run(make(RecordingRunner(which_map={"fork": str(fifo)})), "desktop.cli").status == "warn"


def test_desktop_cli_in_the_install_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from fork_linux import resources

    root = tmp_path / "root"
    (root / "bin").mkdir(parents=True)
    launcher = root / "bin" / "fork"
    launcher.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(resources, "install_root", lambda: root)
    assert run(make(RecordingRunner(which_map={"fork": str(launcher)})), "desktop.cli").status == "ok"


def test_license_reminder() -> None:
    ctx = make()
    assert run(ctx, "license.reminder").detail == "Fork license activations are tied to this Wine prefix"
    pfx = prefix(ctx)
    add_values(pfx, "system.reg", "Software\\Microsoft\\Cryptography", ['"MachineGuid"="abc-123"'])
    result = run(ctx, "license.reminder")
    assert result.status == "info"
    assert "MachineGuid abc-123" in result.detail
    assert "deactivate" in result.hint


# -- network -------------------------------------------------------------------------------------


def test_network_reach(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = make()
    targets = [url for _what, url in doctor.network_targets(ctx)]
    assert targets[0] == "https://cdn.fork.dev/win/Fork-2.23.2.exe"
    assert targets[1].startswith("https://git-fork.com/")
    assert targets[2].startswith("https://github.com/")
    asked: list[str] = []
    monkeypatch.setattr(doctor, "_head_ok", lambda url: asked.append(url) or True)
    assert run(ctx, "network.reach").status == "ok"
    assert asked == targets
    monkeypatch.setattr(doctor, "_head_ok", lambda url: "github" not in url)
    result = run(ctx, "network.reach")
    assert result.status == "warn"
    assert result.detail == "unreachable: managed Wine (github.com)"
    assert run(make(offline=True), "network.reach") == Result("info", "skipped: --offline")


def test_head_ok_seam(monkeypatch: pytest.MonkeyPatch) -> None:
    from fork_linux import download

    seen: list[Any] = []
    monkeypatch.setattr(download, "head_ok", lambda url, timeout: seen.append((url, timeout)) or False)
    assert doctor._head_ok("https://cdn.fork.dev/x") is False
    assert seen == [("https://cdn.fork.dev/x", doctor.HEAD_TIMEOUT)]


# -- fix -----------------------------------------------------------------------------------------


def test_fix_runs_steps_in_setup_order_and_rechecks(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = make()
    pfx = prefix(ctx)
    ran: list[Any] = []

    def fake_run_steps(boot: bootstrap.Ctx, only: list[str]) -> list[str]:
        ran.append(only)
        user_values(ctx, "Software\\Microsoft\\Avalon.Graphics", '"DisableHWAcceleration"=dword:00000001')
        write_release(pfx, 528049)
        return ["preflight", *only, "finalize"]

    monkeypatch.setattr(bootstrap, "run_steps", fake_run_steps)
    results = doctor.run_checks(ctx, only=["prefix.dotnet", "prefix.avalon", "prefix.appdefaults", "env.arch"])
    assert [c.id for c, r in results if r.status == "fail"] == ["prefix.dotnet", "prefix.avalon", "prefix.appdefaults"]
    report = doctor.fix(ctx, results)
    assert ran == [["registry", "dotnet"]]
    assert report.actions == ["re-ran setup steps: registry, dotnet"]
    assert report.errors == []
    statuses = {check.id: result.status for check, result in report.results}
    assert statuses == {"prefix.dotnet": "ok", "prefix.avalon": "ok", "prefix.appdefaults": "fail", "env.arch": "ok"}


def test_fix_collects_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = make()

    def locked(boot: bootstrap.Ctx, only: list[str]) -> list[str]:
        raise Locked("setup is running", hint="wait")

    def broken_fixer(_ctx: DoctorCtx) -> str:
        raise OSError("read-only file system")

    monkeypatch.setattr(bootstrap, "run_steps", locked)
    failing = Check("x.steps", "Steps", "x", lambda _c: Result("fail", "still"), fix_steps=("registry",))
    warn = Check("x.fixer", "Fixer", "x", lambda _c: Result("warn", "still"), fixer=broken_fixer)
    fine = Check("x.fine", "Fine", "x", lambda _c: Result("ok", ""), fix_steps=("registry",))
    unfixable = Check("x.no", "No", "x", lambda _c: Result("fail", "nope"))
    results = [(c, c.run(ctx)) for c in (failing, warn, fine, unfixable)]
    report = doctor.fix(ctx, results)
    assert report.errors == ["x.fixer: read-only file system", "setup is running (wait)"]
    assert report.actions == []
    good = Check("x.good", "Good", "x", lambda _c: Result("ok", "fixed"), fixer=lambda _c: "did it")
    report = doctor.fix(ctx, [(good, Result("fail", "broken"))])
    assert report.actions == ["x.good: did it"]
    assert report.results[0][1].detail == "fixed"


def test_fix_errors_without_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = make()

    def broken(boot: bootstrap.Ctx, only: list[str]) -> list[str]:
        raise ForkLinuxError("step exploded")

    monkeypatch.setattr(bootstrap, "run_steps", broken)
    check = Check("x.steps", "Steps", "x", lambda _c: Result("fail", "still"), fix_steps=("registry",))
    assert doctor.fix(ctx, [(check, Result("fail", "x"))]).errors == ["step exploded"]


def test_fix_with_nothing_to_do() -> None:
    ctx = make()
    check = Check("x.ok", "Ok", "x", lambda _c: Result("ok", ""), fix_steps=("registry",))
    results = [(check, Result("ok", ""))]
    report = doctor.fix(ctx, results)
    assert report.results == results
    assert report.actions == []
    assert report.errors == []


def test_newest_git_instance_sorts_numerically() -> None:
    names = ["2.9.0.windows.1", "2.50.1.windows.1", "2.50.1.windows.2"]
    assert max(names, key=doctor._version_key) == "2.50.1.windows.2"
    assert doctor._version_key("2.10-rc") > doctor._version_key("2.9")


# -- fork tools, source folder, redirects ----------------------------------------------------------

DEAD = {"Type": "Custom", "ApplicationPath": "C:\\fork-linux\\bin\\fl-launch.exe", "Arguments": "terminal"}


def _libexec(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *names: str) -> Path:
    libexec = tmp_path / "libexec"
    libexec.mkdir(exist_ok=True)
    for name in names:
        (libexec / name).write_text("#!/bin/sh\n", encoding="utf-8")
        (libexec / name).chmod(0o755)
    monkeypatch.setenv("FORK_LINUX_LIBEXEC_DIR", str(libexec))
    return libexec


def test_fork_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _libexec(tmp_path, monkeypatch)
    ctx = make()
    assert run(ctx, "fork.tools").status == "info"
    ctx.layout.settings_file.parent.mkdir(parents=True, exist_ok=True)
    ctx.layout.settings_file.write_text("{broken", encoding="utf-8")
    assert run(ctx, "fork.tools").status == "info"
    write_settings(ctx.layout, {"Guid": GUID, "ShellTool": None})
    assert run(ctx, "fork.tools") == Result("ok", "no unusable tool configured")
    write_settings(ctx.layout, {"Guid": GUID, "ShellTool": {"Type": "GitBash"}})
    assert "Git Bash" in run(ctx, "fork.tools").detail
    write_settings(
        ctx.layout, {"Guid": GUID, "ShellTool": {"Type": "Custom", "ApplicationPath": "Z:\\x\\fork-linux-terminal"}}
    )
    assert run(ctx, "fork.tools") == Result("ok", "terminal: Z:\\x\\fork-linux-terminal")
    write_settings(ctx.layout, {"Guid": GUID, "ShellTool": DEAD, "MergeTool": {**DEAD, "Arguments": "merge"}})
    result = run(ctx, "fork.tools")
    assert result.status == "warn"
    assert result.detail.startswith("ShellTool, MergeTool run(s) fl-launch.exe")
    monkeypatch.setattr(doctor.bridge, "host_actions_active", lambda _ctx: True)
    assert run(ctx, "fork.tools").status == "ok"


def test_fix_fork_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    libexec = _libexec(tmp_path, monkeypatch)
    ctx = make()
    prefix(ctx)
    write_settings(ctx.layout, {"Guid": GUID, "ShellTool": DEAD, "MergeTool": {**DEAD, "Arguments": "merge"}})
    assert doctor.fix_fork_tools(ctx) == "reset ShellTool, MergeTool"
    data = json.loads(ctx.layout.settings_file.read_text(encoding="utf-8"))
    assert data["ShellTool"] is None
    assert data["MergeTool"] == {"Type": "Custom", "ApplicationPath": "", "Arguments": ""}
    assert doctor.fix_fork_tools(ctx) == "nothing to do"
    _libexec(tmp_path, monkeypatch, "fork-linux-terminal")
    write_settings(ctx.layout, {"Guid": GUID, "ShellTool": DEAD})
    doctor.fix_fork_tools(ctx)
    shell = json.loads(ctx.layout.settings_file.read_text(encoding="utf-8"))["ShellTool"]
    assert shell["ApplicationPath"] == "Z:" + str(libexec / "fork-linux-terminal").replace("/", "\\")
    # Our entry in Fork's tool lists is dead too without the daemon; the user's entry stays.
    from fork_linux import fork_tools

    user = {"Type": "Custom", "Name": "Mine", "Path": "C:\\x.exe", "Arguments": ""}
    ours = fork_tools.list_entry(fork_tools.DIFF_ARGUMENTS)
    write_settings(ctx.layout, {"Guid": GUID, "ExternalDiffTools": [user, ours], "ExternalMergeTools": "odd"})
    result = run(ctx, "fork.tools")
    assert result.status == "warn"
    assert result.detail.startswith("ExternalDiffTools run(s) fl-launch.exe")
    assert doctor.fix_fork_tools(ctx) == "reset ExternalDiffTools"
    assert json.loads(ctx.layout.settings_file.read_text(encoding="utf-8"))["ExternalDiffTools"] == [user]
    monkeypatch.setattr(doctor.bridge, "host_actions_active", lambda _ctx: True)
    write_settings(ctx.layout, {"Guid": GUID, "ExternalDiffTools": [ours]})
    assert run(ctx, "fork.tools").status == "ok"


def test_source_dirs(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = make()
    prefix(ctx)
    assert run(ctx, "fork.source_dirs").status == "info"
    toml = ctx.layout.forkdata_dir / "repositories.toml"
    toml.parent.mkdir(parents=True)
    toml.write_text(f"source_dirs = ['C:\\users\\{USER}\\']\nscan_depth = 5\n", encoding="utf-8")
    result = run(ctx, "fork.source_dirs")
    assert result.status == "warn"
    assert "inside the Wine prefix" in result.detail
    assert doctor.fix_source_dirs(ctx).startswith("source folder set to Z:\\")
    assert run(ctx, "fork.source_dirs").status == "ok"
    assert doctor.fix_source_dirs(ctx) == "nothing to do"
    toml.write_text("scan_depth = 5\n", encoding="utf-8")
    assert run(ctx, "fork.source_dirs") == Result("ok", "source folder: (not set)")


def test_integration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from fork_linux.steps import integration

    _libexec(tmp_path, monkeypatch)
    ctx = make()
    assert run(ctx, "prefix.integration").status == "info"
    prefix(ctx)
    result = run(ctx, "prefix.integration")
    assert result.status == "warn"
    assert "redirects are missing" in result.detail
    lines = [
        '@="' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'
        for _key, _name, value in integration.open_expected()
    ]
    for (key, _name, _value), line in zip(integration.open_expected(), lines, strict=True):
        add_values(ctx.paths.prefix, "system.reg", key.replace("HKLM\\", ""), [line])
    result = run(ctx, "prefix.integration")
    assert result.status == "info"
    assert "fork-linux-explorer is not installed" in result.detail
    assert "no H: drive" in result.detail
    libexec = _libexec(tmp_path, monkeypatch, "fork-linux-explorer")
    win = "Z:" + str(libexec / "fork-linux-explorer").replace("/", "\\")
    for name in integration.EXPLORER_NAMES:
        key = integration.APP_PATHS_KEY.replace("HKLM\\", "") + "\\" + name
        add_values(ctx.paths.prefix, "system.reg", key, [f'@="{win}"'.replace("\\", "\\\\")])
    (ctx.paths.prefix / "dosdevices" / "h:").symlink_to(ctx.host_home)
    result = run(ctx, "prefix.integration")
    assert result.status == "ok"
    assert f"H: -> {ctx.host_home}" in result.detail


# -- repositories ----------------------------------------------------------------------------------


def _git_repo(path: Path, *, link: bool = True) -> Path:
    import subprocess

    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x"}

    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True, env=env)

    path.mkdir(parents=True)
    git("init", "-q")
    (path / "t").write_text("t\n", encoding="utf-8")
    if link:
        (path / "l").symlink_to("t")
    git("add", "-A")
    git("commit", "-qm", "init")
    hook = path / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\n", encoding="utf-8")
    hook.chmod(0o755)
    (path / ".gitmodules").write_text("", encoding="utf-8")
    git("remote", "add", "o", "/opt/r.git")
    return path


def _known(ctx: DoctorCtx, *repos_: Path) -> None:
    ctx.layout.forkdata_dir.mkdir(parents=True, exist_ok=True)
    entries = "".join(
        "[[repository]]\npath = '" + "Z:" + str(repo).replace("/", "\\") + "'\n" for repo in repos_
    )
    (ctx.layout.forkdata_dir / "repositories.toml").write_text(entries, encoding="utf-8")


def _real_git(ctx: DoctorCtx) -> DoctorCtx:
    from fork_linux.procrun import Runner

    ctx.runner = Runner()
    return ctx


def test_repo_checks(xdg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _real_git(make(env={"GIT_CONFIG_NOSYSTEM": "1"}))
    assert run(ctx, "repo.hooks") == Result("info", "no repositories opened in Fork yet")
    prefix(ctx)
    assert run(ctx, "repo.hooks").status == "info"
    messy = _git_repo(xdg / "src" / "messy")
    _known(ctx, messy)
    hooks = run(ctx, "repo.hooks")
    assert hooks.status == "warn"
    assert "messy (pre-commit)" in hooks.detail
    assert "git-bridge enable" in hooks.hint
    assert "enable the git bridge" in run(ctx, "repo.submodules").hint
    from fork_linux import bridge

    with monkeypatch.context() as patch:
        patch.setattr(bridge, "check", lambda _ctx: bridge.Readiness(True, [], [], (2, 53, 0)))
        for name in ("repo.hooks", "repo.submodules"):
            result = run(ctx, name)
            assert result.status == "ok", name
            assert "the git bridge runs Fork's git with Linux git" in result.detail, name
        assert run(ctx, "repo.symlinks").status == "warn", "the bridge does not fix Fork's own status view"
    assert "messy (l)" in run(ctx, "repo.symlinks").detail
    assert "messy" in run(ctx, "repo.submodules").detail
    assert "/opt/r.git" in run(ctx, "repo.remotes").detail
    assert run(ctx, "repo.filemode").status == "warn"
    from fork_linux import gitconfig

    overlay = gitconfig.overlay_path(ctx.paths, USER)
    overlay.parent.mkdir(parents=True)
    overlay.write_text('[url "Z:/opt/"]\n\tinsteadOf = /opt/\n', encoding="utf-8")
    ctx.reset()
    assert run(ctx, "repo.remotes") == Result("ok", "1 repositories checked")


def test_repo_checks_without_git() -> None:
    ctx = make(RecordingRunner(which_map={"git": None}))
    assert run(ctx, "repo.filemode") == Result("info", "skipped: git is not installed on this computer")
    assert doctor.fix_repo_filemode(ctx) == "nothing to do"


def test_repo_fixers_ask_first(xdg: Path) -> None:
    from fixtures.setup_ctx import FakeUI

    declined = FakeUI(answer=False)
    ctx = _real_git(make(env={"GIT_CONFIG_NOSYSTEM": "1"}, ui=declined))
    prefix(ctx)
    messy = _git_repo(xdg / "src" / "messy")
    _known(ctx, messy)
    assert doctor.fix_repo_filemode(ctx) == "skipped (not confirmed)"
    assert "Set core.filemode = false" in declined.kinds("confirm")[0][1]
    ctx.ui = FakeUI(answer=True)
    assert doctor.fix_repo_filemode(ctx) == f"{messy}: core.filemode = false"
    assert doctor.fix_repo_filemode(ctx) == "nothing to do"
    assert doctor.fix_repo_symlinks(ctx) == f"{messy}: skip-worktree for l"
    assert run(ctx, "repo.symlinks").status == "ok"
