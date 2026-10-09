"""Tests for the host_shims, git_overlay, ssh_sync, icon and desktop_entry setup steps."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from fixtures.fork_tree import install_fork
from fixtures.setup_ctx import make_ctx

from fork_linux import APP_ID, desktop_integration, gitconfig, resources
from fork_linux.bootstrap import Ctx
from fork_linux.procrun import RecordingRunner
from fork_linux.steps import integration


def _env(xdg: Path, tmp_path: Path, **extra: str) -> dict[str, str]:
    data_dirs = tmp_path / "system-share"
    data_dirs.mkdir(exist_ok=True)
    env = dict(os.environ)
    env.update({"XDG_DATA_DIRS": str(data_dirs), "PATH": "/nonexistent"})
    env.update(extra)
    return env


def _drives(ctx: Ctx) -> None:
    dosdevices = ctx.paths.prefix / "dosdevices"
    dosdevices.mkdir(parents=True, exist_ok=True)
    (dosdevices / "c:").symlink_to("../drive_c")
    (dosdevices / "z:").symlink_to("/")
    ctx.paths.wine_user_dir(ctx.user).mkdir(parents=True, exist_ok=True)


# -- host_shims ----------------------------------------------------------------------------------


def test_shims_are_copied(xdg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    shims = tmp_path / "shims"
    shims.mkdir()
    (shims / "fl-launch.exe").write_bytes(b"MZ launch")
    (shims / "fl-shim.exe").write_bytes(b"MZ shim")
    (shims / "readme.txt").write_text("not a shim")
    (shims / "dir.exe").mkdir()
    monkeypatch.setenv(resources.SHIMS_ENV, str(shims))
    ctx = make_ctx()
    assert [path.name for path in integration.shim_sources()] == ["fl-launch.exe", "fl-shim.exe"]
    assert set(integration.shims_inputs(ctx)["shims"]) == {"fl-launch.exe", "fl-shim.exe"}
    assert not integration.verify_shims(ctx)
    integration.run_shims(ctx)
    dest = ctx.paths.fork_linux_win_dir / "bin"
    assert (dest / "fl-launch.exe").read_bytes() == b"MZ launch"
    assert (dest / "fl-shim.exe").stat().st_mode & 0o777 == 0o755
    assert integration.verify_shims(ctx)
    (dest / "fl-shim.exe").write_bytes(b"MZ changed")
    assert not integration.verify_shims(ctx)
    (dest / "fl-shim.exe").unlink()
    (dest / "fl-shim.exe").symlink_to(shims / "fl-shim.exe")
    assert not integration.verify_shims(ctx)


def test_shims_absent(xdg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(resources.SHIMS_ENV, str(tmp_path / "missing"))
    ctx = make_ctx()
    integration.run_shims(ctx)
    assert integration.verify_shims(ctx)
    assert not ctx.paths.fork_linux_win_dir.exists()
    monkeypatch.setattr(resources, "shims_dir", lambda: None)
    assert integration.shim_sources() == []


# -- git_overlay ---------------------------------------------------------------------------------


def test_git_overlay(xdg: Path) -> None:
    (xdg / ".gitconfig").write_text("[user]\n\tname = Tester\n")
    ctx = make_ctx()
    _drives(ctx)
    assert integration.git_inputs(ctx) == {"mode": "translate"}
    assert not integration.verify_git(ctx)
    integration.run_git(ctx)
    assert integration.verify_git(ctx)
    assert gitconfig.overlay_path(ctx.paths, ctx.user).is_file()


def test_git_overlay_off(xdg: Path) -> None:
    ctx = make_ctx(env={**os.environ, "FORK_LINUX_GIT_CONFIG_OVERLAY": "off"})
    integration.run_git(ctx)
    assert integration.verify_git(ctx)
    assert not gitconfig.overlay_path(ctx.paths, ctx.user).exists()


# -- ssh_sync ------------------------------------------------------------------------------------


def test_ssh_sync_auto_without_ssh_dir(xdg: Path) -> None:
    ctx = make_ctx()
    assert integration.ssh_inputs(ctx) == {"sync": "auto", "mode": "link", "wanted": False}
    integration.run_ssh(ctx)
    assert integration.verify_ssh(ctx)
    assert not (ctx.paths.wine_user_dir(ctx.user) / ".ssh").exists()


def test_ssh_sync_shares_keys(xdg: Path) -> None:
    ssh = xdg / ".ssh"
    ssh.mkdir(mode=0o700)
    (ssh / "id_ed25519").write_text("PRIVATE")
    (ssh / "id_ed25519").chmod(0o600)
    (ssh / "id_ed25519.pub").write_text("ssh-ed25519 AAAA")
    ctx = make_ctx()
    ctx.paths.wine_user_dir(ctx.user).mkdir(parents=True)
    assert integration.ssh_inputs(ctx)["wanted"] is True
    assert not integration.verify_ssh(ctx)
    integration.run_ssh(ctx)
    assert integration.verify_ssh(ctx)
    assert (ctx.paths.wine_user_dir(ctx.user) / ".ssh" / "id_ed25519").is_symlink()


def test_ssh_sync_on_and_off(xdg: Path) -> None:
    on = make_ctx(env={**os.environ, "FORK_LINUX_SSH_SYNC": "on"})
    assert integration._ssh_wanted(on)
    off = make_ctx(env={**os.environ, "FORK_LINUX_SSH_SYNC": "off"})
    (xdg / ".ssh").mkdir()
    assert not integration._ssh_wanted(off)


# -- icon ------------------------------------------------------------------------------------------


def test_icon_without_fork(xdg: Path) -> None:
    ctx = make_ctx()
    assert integration.icon_inputs(ctx) == {"exe_sha256": None}
    assert not integration.verify_icon(ctx)
    integration.run_icon(ctx)
    assert ctx.state.get(integration.ICONS_KEY) == [] and integration.verify_icon(ctx)


def test_icon_extracted_from_fork_exe(xdg: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    ctx = make_ctx()
    install_fork(ctx.layout)
    png = tmp_path / "icon.png"
    png.write_bytes(b"png")
    calls: list[dict[str, Any]] = []

    def install(paths: Any, env: Any, **kwargs: Any) -> list[Path]:
        calls.append(kwargs)
        return [png]

    monkeypatch.setattr(integration.desktop_integration, "install", install)
    assert len(integration.icon_inputs(ctx)["exe_sha256"]) == 64
    integration.run_icon(ctx)
    assert calls[0]["fork_exe"] == ctx.layout.exe and calls[0]["menu"] is False and calls[0]["icons"] is True
    assert calls[0]["file_managers"] is None
    assert integration.verify_icon(ctx)
    png.unlink()
    assert not integration.verify_icon(ctx)


def test_icon_extraction_failure_is_not_fatal(xdg: Path, tmp_path: Path) -> None:
    ctx = make_ctx(RecordingRunner(), env=_env(xdg, tmp_path))
    install_fork(ctx.layout)
    integration.run_icon(ctx)
    assert ctx.state.get(integration.ICONS_KEY) == [] and integration.verify_icon(ctx)


# -- desktop_entry -------------------------------------------------------------------------------


def test_desktop_entry_is_installed(xdg: Path, tmp_path: Path) -> None:
    ctx = make_ctx(RecordingRunner(), env=_env(xdg, tmp_path))
    assert integration.desktop_inputs(ctx)["launcher"] == desktop_integration.launcher_command(ctx.env)
    assert not integration.verify_desktop(ctx)
    integration.run_desktop(ctx)
    menu = Path(os.environ["XDG_DATA_HOME"]) / "applications" / f"{APP_ID}.desktop"
    assert menu.is_file() and "StartupWMClass=fork.exe" in menu.read_text()
    assert integration.verify_desktop(ctx)


def test_desktop_entry_packaged(xdg: Path, tmp_path: Path) -> None:
    env = _env(xdg, tmp_path)
    packaged = Path(env["XDG_DATA_DIRS"]) / "applications"
    packaged.mkdir(parents=True)
    (packaged / f"{APP_ID}.desktop").write_text("[Desktop Entry]\n")
    ctx = make_ctx(RecordingRunner(), env=env)
    integration.run_desktop(ctx)
    assert not (Path(os.environ["XDG_DATA_HOME"]) / "applications" / f"{APP_ID}.desktop").exists()
    assert integration.verify_desktop(ctx)
