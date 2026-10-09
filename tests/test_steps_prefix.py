"""Tests for the Wine runtime, winetricks, prefix, registry, winver and .NET setup steps."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

import pytest
from fixtures.fork_tree import install_fork
from fixtures.setup_ctx import FakeWine, add_values, make_ctx, wine_info, write_release

from fork_linux import bootstrap
from fork_linux import manifest as manifest_mod
from fork_linux import winetricks as winetricks_mod
from fork_linux.bootstrap import Ctx
from fork_linux.errors import (
    ForkLinuxError,
    IntegrityFailed,
    SetupFailed,
    WineUnavailable,
)
from fork_linux.manifest import Manifest
from fork_linux.procrun import RecordingRunner
from fork_linux.steps import dotnet, prefix, runtime
from fork_linux.winecmd import WineInfo


def _with_winetricks(ctx: Ctx, content: bytes) -> None:
    """Point the manifest's winetricks pin at ``content``."""
    data = ctx.manifest.as_dict()
    data["winetricks"]["sha256"] = hashlib.sha256(content).hexdigest()
    data["winetricks"]["size"] = len(content)
    ctx.manifest = Manifest(data)


def _fake(ctx: Ctx, **kwargs: Any) -> FakeWine:
    fake = FakeWine(ctx.paths.prefix, **kwargs)
    ctx.runner = fake.runner()
    ctx.cache[runtime._WINETRICKS_CACHE] = Path("/opt/winetricks")
    bootstrap.init_prefix_meta(ctx)
    return fake


# -- wine_runtime / winetricks ---------------------------------------------------------------


def test_wine_inputs(xdg: Path) -> None:
    ctx = make_ctx()
    default = ctx.manifest.wine_default
    assert runtime.wine_inputs(ctx) == {"provider": "managed", "build": default.id, "sha256": default.sha256}
    ctx.wine_choice = "/opt/wine"
    assert runtime.wine_inputs(ctx) == {"provider": "/opt/wine"}


def test_run_wine_installs_and_records(xdg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = make_ctx(with_wine=False, wine_choice="system", offline=True)
    info = WineInfo("system", None, Path("/usr"), Path("/usr/bin/wine"), Path("/usr/bin/wineserver"),
                    "9.0", False, False)
    calls: list[dict[str, Any]] = []

    def resolve(*args: Any, **kwargs: Any) -> WineInfo:
        calls.append(kwargs)
        return info

    monkeypatch.setattr(runtime.wine_provider, "resolve", resolve)
    runtime.run_wine(ctx)
    assert calls[0] == {"choice": "system", "install": True, "offline": True}
    assert ctx.wine() is info
    assert ctx.state.get("wine.provider") == "system" and ctx.state.get("wine.build") is None
    assert ctx.state.get("wine.version") == "9.0" and ctx.state.get("wine.staging") is False
    assert ctx.state.get("wine.root") == "/usr"
    assert runtime.verify_wine(ctx)
    assert calls[-1]["install"] is False


def test_verify_wine_fails_without_wine(xdg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def resolve(*args: Any, **kwargs: Any) -> WineInfo:
        raise WineUnavailable("none")

    monkeypatch.setattr(runtime.wine_provider, "resolve", resolve)
    assert not runtime.verify_wine(make_ctx())


def test_winetricks_step(xdg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = make_ctx()
    content = b"#!/bin/sh\necho winetricks\n"
    _with_winetricks(ctx, content)
    assert runtime.winetricks_inputs(ctx)["sha256"] == hashlib.sha256(content).hexdigest()
    assert runtime.winetricks_inputs(ctx)["mode"] == "managed"
    script = winetricks_mod.managed_path(ctx.manifest, ctx.paths)
    calls: list[dict[str, Any]] = []

    def ensure(manifest: Manifest, paths: Any, runner: Any, **kwargs: Any) -> Path:
        calls.append(kwargs)
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_bytes(content)
        script.chmod(0o755)
        return script

    monkeypatch.setattr(runtime.winetricks, "ensure", ensure)
    assert not runtime.verify_winetricks(ctx)
    runtime.run_winetricks(ctx)
    assert runtime.winetricks_path(ctx) == script and len(calls) == 1
    assert calls[0] == {"mode": "managed", "offline": False}
    assert runtime.verify_winetricks(ctx)
    script.chmod(0o644)
    assert not runtime.verify_winetricks(ctx)
    script.chmod(0o755)
    script.write_bytes(b"tampered")
    assert not runtime.verify_winetricks(ctx)


# -- prefix_init / shell_folders ------------------------------------------------------------------


def test_prefix_init_boots_without_mono_and_gecko(xdg: Path) -> None:
    ctx = make_ctx()
    _fake(ctx)
    assert not prefix.verify_prefix_init(ctx)
    prefix.run_prefix_init(ctx)
    wine_call, server_call = ctx.runner.calls
    assert wine_call["argv"][1:] == ["wineboot", "--init"]
    assert wine_call["env"]["WINEDLLOVERRIDES"] == "mscoree,mshtml=;winemenubuilder.exe=d"
    assert server_call["argv"][1:] == ["-w"]
    assert prefix.verify_prefix_init(ctx)
    assert (ctx.paths.prefix.stat().st_mode & 0o777) == 0o700
    assert prefix.prefix_inputs(ctx) == {"wine": runtime.wine_inputs(ctx)}


def test_prefix_init_failure(xdg: Path) -> None:
    ctx = make_ctx()
    _fake(ctx, fail=("wineboot",))
    with pytest.raises(SetupFailed, match="wineboot exited with code 1"):
        prefix.run_prefix_init(ctx)


def test_prefix_verify_needs_win64_hives(xdg: Path) -> None:
    ctx = make_ctx()
    ctx.paths.prefix.mkdir(parents=True)
    for name in prefix.HIVES:
        (ctx.paths.prefix / name).write_text("WINE REGISTRY Version 2\n#arch=win32\n")
    assert not prefix.verify_prefix_init(ctx)


def test_shell_folders_replace_only_the_desktop_link(xdg: Path, tmp_path: Path) -> None:
    ctx = make_ctx()
    linux_desktop = tmp_path / "LinuxDesktop"
    linux_desktop.mkdir()
    (linux_desktop / "keep.txt").write_text("mine")
    user_dir = ctx.paths.wine_user_dir(ctx.user)
    user_dir.mkdir(parents=True)
    (user_dir / "Desktop").symlink_to(linux_desktop)
    assert not prefix.verify_shell_folders(ctx)
    prefix.run_shell_folders(ctx)
    assert prefix.verify_shell_folders(ctx)
    assert (linux_desktop / "keep.txt").read_text() == "mine"
    prefix.run_shell_folders(ctx)
    assert prefix.verify_shell_folders(ctx)


def test_shell_folders_refuse_a_file(xdg: Path) -> None:
    ctx = make_ctx()
    user_dir = ctx.paths.wine_user_dir(ctx.user)
    user_dir.mkdir(parents=True)
    (user_dir / "Desktop").write_text("odd")
    with pytest.raises(SetupFailed, match="not a directory"):
        prefix.run_shell_folders(ctx)


# -- registry / winver ---------------------------------------------------------------------------


def test_registry_batch_is_imported_and_verified(xdg: Path) -> None:
    ctx = make_ctx()
    _fake(ctx)
    prefix.run_prefix_init(ctx)
    assert not prefix.verify_registry(ctx)
    prefix.run_registry(ctx)
    assert prefix.verify_registry(ctx)
    text = prefix.registry_batch(ctx).render_text()
    for needed in ('"winemenubuilder.exe"=""', '"DisableHWAcceleration"=dword:00000001', '"Version"="win7"',
                   '"ShowCrashDialog"=dword:00000000', '"renderer"="gdi"', '"FontSmoothing"="2"',
                   '"FontSmoothingType"=dword:00000002'):
        assert needed in text
    handler = prefix.url_handler()
    assert prefix.registry_inputs(ctx) == {"renderer": "gdi", "url_handler": str(handler) if handler else ""}
    regedit = next(call for call in ctx.runner.calls if call["argv"][1:2] == ["regedit"])
    assert regedit["argv"][2:] == ["/S", "C:\\fork-linux\\tmp\\registry.reg"]


def test_registry_routes_links_through_our_handler(
    xdg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    libexec = tmp_path / "libexec"
    libexec.mkdir()
    handler = libexec / prefix.URL_HANDLER
    handler.write_text("#!/bin/sh\n", encoding="utf-8")
    handler.chmod(0o755)
    monkeypatch.setenv("FORK_LINUX_LIBEXEC_DIR", str(libexec))
    ctx = make_ctx()
    expected = prefix.registry_expected(ctx)
    assert (prefix.WINEBROWSER_KEY, "Browsers", f"{handler},xdg-open") in expected
    assert (prefix.WINEBROWSER_KEY, "Mailers", f"{handler},xdg-email") in expected
    assert prefix.registry_inputs(ctx)["url_handler"] == str(handler)


def test_registry_without_handler_leaves_winebrowser_default(
    xdg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FORK_LINUX_LIBEXEC_DIR", str(tmp_path / "missing"))
    ctx = make_ctx()
    assert prefix.url_handler() is None
    assert all(key != prefix.WINEBROWSER_KEY for key, _n, _v in prefix.registry_expected(ctx))
    assert prefix.registry_inputs(ctx)["url_handler"] == ""


def test_registry_default_renderer_is_left_alone(xdg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = make_ctx()
    monkeypatch.setattr(prefix, "_renderer", lambda c: "default")
    keys = [key for key, _name, _value in prefix.registry_expected(ctx)]
    assert prefix.DIRECT3D_KEY not in keys


def test_registry_import_failure_is_an_error(xdg: Path) -> None:
    ctx = make_ctx()
    _fake(ctx, fail=("regedit",))
    with pytest.raises(ForkLinuxError, match="importing registry settings failed"):
        prefix.run_registry(ctx)


def test_reg_value_handles_missing_and_broken_hives(xdg: Path) -> None:
    ctx = make_ctx()
    assert prefix.reg_value(ctx, prefix.WINE_KEY, "Version") is None
    ctx.paths.prefix.mkdir(parents=True)
    (ctx.paths.prefix / "user.reg").write_text("not a hive\n")
    assert prefix.reg_value(ctx, prefix.WINE_KEY, "Version") is None


def test_winver(xdg: Path) -> None:
    ctx = make_ctx()
    fake = _fake(ctx)
    assert prefix.verify_winver(ctx)
    prefix.run_winver(ctx)
    assert fake.verbs == ["win10"] and prefix.verify_winver(ctx)
    add_values(ctx.paths.prefix, "user.reg", "Software\\Wine", ['"Version"="win7"'])
    assert not prefix.verify_winver(ctx)
    winetricks_call = ctx.runner.calls[-1]
    assert winetricks_call["argv"] == ["/opt/winetricks", "-q", "win10"]
    assert winetricks_call["env"]["W_OPT_UNATTENDED"] == "1"


def test_winver_failure(xdg: Path) -> None:
    ctx = make_ctx()
    _fake(ctx, fail=("win10",))
    with pytest.raises(SetupFailed, match="winetricks win10 exited with code 1") as caught:
        prefix.run_winver(ctx)
    assert caught.value.step == "winver"


# -- dotnet --------------------------------------------------------------------------------------


def test_dotnet_verbs_and_inputs(xdg: Path) -> None:
    ctx = make_ctx()
    assert dotnet.verbs(ctx) == ["dotnet48", "dotnet472"]
    ctx.dotnet = "dotnet472"
    assert dotnet.verbs(ctx) == ["dotnet472"]
    assert dotnet.inputs(ctx) == {"min_release": 461808}


def test_dotnet_installs_the_first_verb(xdg: Path) -> None:
    ctx = make_ctx()
    fake = _fake(ctx)
    assert not dotnet.verify(ctx)
    dotnet.run(ctx)
    assert fake.verbs == ["dotnet48"] and dotnet.verify(ctx)
    assert dotnet.release(ctx) == 528049 and ctx.state.get(dotnet.VERB_KEY) == "dotnet48"
    dotnet.run(ctx)
    assert fake.verbs == ["dotnet48"]


def test_dotnet_release_must_be_a_number(xdg: Path) -> None:
    ctx = make_ctx()
    add_values(ctx.paths.prefix, "system.reg", "Software\\Microsoft\\NET Framework Setup\\NDP\\v4\\Full",
               ['"Release"="528049"'])
    assert dotnet.release(ctx) is None and not dotnet.verify(ctx)


def test_dotnet_rebuilds_the_prefix_and_tries_the_next_verb(xdg: Path) -> None:
    ctx = make_ctx()
    fake = _fake(ctx, fail=("dotnet48",))
    for step in prefix.BOOT_STEPS:
        bootstrap.run_step(ctx, step)
    ctx.state.set("consent.fork_eula", "yes")
    ctx.state.set_step_marker("consent", 1, "c" * 64)
    ctx.state.set_step_marker("fonts", 1, "f" * 64)
    (ctx.paths.prefix / "leftover").write_text("half-installed .NET")
    dotnet.run(ctx)
    assert fake.verbs == ["dotnet48", "dotnet472"]
    assert ctx.state.get(dotnet.VERB_KEY) == "dotnet472"
    assert not (ctx.paths.prefix / "leftover").exists()
    assert ctx.paths.created_by_marker.is_file()
    assert ctx.state.step_marker("fonts") is None and ctx.state.step_marker("consent") is not None
    assert all(bootstrap.is_done(step, ctx) for step in prefix.BOOT_STEPS)
    assert ["-k"] in [call["argv"][1:] for call in ctx.runner.calls]
    assert ctx.state.get("consent.fork_eula") == "yes"
    assert ("warn", "the .NET Framework installation failed; rebuilding the Wine prefix and trying again") \
        in ctx.ui.events


def test_dotnet_low_release_counts_as_failure(xdg: Path) -> None:
    ctx = make_ctx()
    ctx.dotnet = "dotnet48"
    _fake(ctx, release=400000)
    with pytest.raises(SetupFailed, match="release is 400000, below the required 461808"):
        dotnet.run(ctx)


def test_dotnet_reports_a_missing_release(xdg: Path) -> None:
    ctx = make_ctx()
    ctx.dotnet = "dotnet48"
    fake = _fake(ctx)
    fake.release = 0
    ctx.runner = RecordingRunner({"winetricks": 0})
    with pytest.raises(SetupFailed, match="release is missing"):
        dotnet.run(ctx)


def test_dotnet_never_rebuilds_once_fork_is_installed(xdg: Path) -> None:
    ctx = make_ctx()
    fake = _fake(ctx, fail=("dotnet48",))
    install_fork(ctx.layout)
    with pytest.raises(SetupFailed, match="exited with code 1") as caught:
        dotnet.run(ctx)
    assert "--reset" in caught.value.hint
    assert fake.verbs == ["dotnet48"] and ctx.layout.is_installed()


def test_dotnet_gives_up_after_every_verb(xdg: Path) -> None:
    ctx = make_ctx()
    fake = _fake(ctx, fail=("dotnet48", "dotnet472"))
    with pytest.raises(SetupFailed, match="no .NET Framework could be installed") as caught:
        dotnet.run(ctx)
    assert "dotnet48: " in caught.value.message and "dotnet472: " in caught.value.message
    assert fake.verbs == ["dotnet48", "dotnet472"]


def test_dotnet_rebuild_refuses_a_prefix_without_marker(xdg: Path) -> None:
    ctx = make_ctx()
    _fake(ctx, fail=("dotnet48",))
    os.unlink(ctx.paths.created_by_marker)
    with pytest.raises(IntegrityFailed, match="marker"):
        dotnet.run(ctx)


def test_write_release_helper(xdg: Path) -> None:
    ctx = make_ctx()
    write_release(ctx.paths.prefix, 461808)
    assert dotnet.verify(ctx)


def test_manifest_still_loads() -> None:
    assert manifest_mod.load().dotnet_min_release == 461808
    assert wine_info(Path("/x")).provider == "custom"
