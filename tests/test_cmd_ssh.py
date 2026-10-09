"""Tests for ``fork-linux ssh sync|status``."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from fork_linux import ssh_sync

from fixtures.cli_run import USER, isolate, make_prefix, paths, run_cli, run_json


@pytest.fixture(autouse=True)
def _isolated(xdg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    isolate(monkeypatch)


@pytest.fixture
def host_ssh(xdg: Path) -> Path:
    make_prefix(paths())
    ssh = xdg / ".ssh"
    ssh.mkdir(mode=0o700)
    key = ssh / "id_ed25519"
    key.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\n", encoding="utf-8")
    key.chmod(0o600)
    (ssh / "id_ed25519.pub").write_text("ssh-ed25519 AAAA test\n", encoding="utf-8")
    (ssh / "known_hosts").write_text("", encoding="utf-8")
    (ssh / "config").write_text(
        "Host *\n  IdentityAgent ~/.agent.sock\n  IdentityFile ~/.ssh/id_ed25519\n", encoding="utf-8"
    )
    return ssh


def test_sync_dry_run_then_for_real(capsys: pytest.CaptureFixture[str], host_ssh: Path) -> None:
    code, out, _err = run_cli(capsys, "ssh", "sync", "--dry-run")
    assert code == 0
    assert "would link: " in out and "would write config: " in out
    assert "commented out: IdentityAgent" in out
    wine_ssh = paths().wine_user_dir(USER) / ".ssh"
    assert not wine_ssh.exists()
    code, out, _err = run_cli(capsys, "ssh", "sync")
    assert "linked: " in out and "wrote config: " in out
    assert (wine_ssh / "id_ed25519").is_symlink()
    result = run_json(capsys, "ssh", "sync", "--mode", "copy", "--no-config")
    assert result["dry_run"] is False
    assert any(path.endswith("id_ed25519") for path in result["copied"])
    assert set(result) == {"dry_run", "linked", "copied", "dropped_directives", "config_path", "skipped", "removed"}


def test_sync_reports_nothing(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ssh_sync, "sync", lambda *a, **kw: ssh_sync.SyncReport())
    assert run_cli(capsys, "ssh", "sync")[1] == "nothing to share\n"
    assert run_cli(capsys, "ssh", "sync", "--dry-run")[1] == "nothing would change\n"


def test_sync_lists_every_kind(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    report = ssh_sync.SyncReport(
        linked=[Path("/w/a")], copied=[Path("/w/b")], skipped=[Path("/w/c")], removed=[Path("/w/d")]
    )
    seen: dict[str, Any] = {}

    def fake(*args: Any, **kwargs: Any) -> ssh_sync.SyncReport:
        seen.update(kwargs)
        return report

    monkeypatch.setattr(ssh_sync, "sync", fake)
    out = run_cli(capsys, "ssh", "sync")[1]
    assert out == "linked: /w/a\ncopied: /w/b\nremoved: /w/d\nskipped: /w/c\n"
    assert seen == {"mode": "link", "include_config": True, "dry_run": False}
    out = run_cli(capsys, "ssh", "sync", "--dry-run")[1]
    assert out == "would link: /w/a\nwould copy: /w/b\nwould remove: /w/d\nwould skip: /w/c\n"


def test_status(capsys: pytest.CaptureFixture[str], host_ssh: Path) -> None:
    out = run_cli(capsys, "ssh", "status")[1]
    assert "not shared yet" in out
    run_cli(capsys, "ssh", "sync")
    out = run_cli(capsys, "ssh", "status")[1]
    assert "(mode 0700, ok)" in out and "managed:" in out and "config:" in out
    info = run_json(capsys, "ssh", "status")
    assert info["exists"] is True and info["host_present"] is True


def test_status_problems(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    info = {
        "host_dir": "/h/.ssh",
        "host_present": False,
        "wine_dir": "/w/.ssh",
        "exists": True,
        "mode": "0755",
        "perms_ok": False,
        "managed": ["a"],
        "config": "stale",
        "insecure_files": ["id_rsa"],
        "broken_links": ["gone"],
    }
    monkeypatch.setattr(ssh_sync, "status", lambda *a: info)
    out = run_cli(capsys, "ssh", "status")[1]
    assert "/h/.ssh (missing)" in out
    assert "NOT private" in out and "insecure:    id_rsa" in out and "broken link: gone" in out
