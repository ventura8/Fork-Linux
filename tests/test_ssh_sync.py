"""Tests for fork_linux.ssh_sync: ~/.ssh sharing with the Wine user and ssh config translation."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from fork_linux import ssh_sync
from fork_linux.errors import ForkLinuxError, NotSetUpError, UsageError
from fork_linux.paths import Paths

USER = "tester"


@pytest.fixture
def paths(xdg: Path) -> Paths:
    """Paths of the fake home, with the Wine user directory created."""
    resolved = Paths.from_env(dict(os.environ))
    resolved.wine_user_dir(USER).mkdir(parents=True)
    return resolved


@pytest.fixture
def host(tmp_path: Path) -> Path:
    """A host home with two key pairs, a key without .pub, known_hosts and a certificate."""
    home = tmp_path / "host"
    ssh = home / ".ssh"
    ssh.mkdir(parents=True, mode=0o700)
    for name in ("id_ed25519", "id_rsa"):
        (ssh / name).write_text(f"PRIVATE {name}\n", encoding="utf-8")
        (ssh / f"{name}.pub").write_text(f"ssh-ed25519 AAAA {name}\n", encoding="utf-8")
    (ssh / "id_ed25519-cert.pub").write_text("cert\n", encoding="utf-8")
    (ssh / "id_lonely").write_text("no pub\n", encoding="utf-8")
    (ssh / "id_dir").mkdir()
    (ssh / "id_dir.pub").write_text("pub of a directory\n", encoding="utf-8")
    (ssh / "known_hosts").write_text("github.com ssh-ed25519 AAAA\n", encoding="utf-8")
    return home


def _wine_ssh(paths: Paths) -> Path:
    return paths.wine_user_dir(USER) / ".ssh"


def _config(host: Path, text: str) -> Path:
    path = host / ".ssh" / "config"
    path.write_text(text, encoding="utf-8")
    return path


def _body(text: str) -> list[str]:
    """The generated config without its two header lines and the blank line."""
    lines = text.splitlines()
    assert lines[0] == ssh_sync.HEADER
    assert lines[1].startswith("# Source: ")
    assert lines[2] == ""
    return lines[3:]


# --- helpers ----------------------------------------------------------------------


def test_split_and_quote_args() -> None:
    assert ssh_sync._split_args('a "b c" d') == ["a", "b c", "d"]
    assert ssh_sync._split_args('a "b c') == ["a", '"b', "c"]
    assert ssh_sync._quote_arg("plain") == "plain"
    assert ssh_sync._quote_arg("a b") == '"a b"'
    assert ssh_sync._quote_arg('q"\\') == '"q\\"\\\\"'
    assert ssh_sync._quote_arg("") == '""'


@pytest.mark.parametrize(
    ("value", "kept"),
    [
        ("ssh -W %h:%p bastion", True),
        ("/usr/bin/ssh -W %h:%p bastion", True),
        ("none", True),
        ("NONE", True),
        ("nc -X connect -x proxy:8080 %h %p", False),
        ("", False),
    ],
)
def test_is_ssh_proxy(value: str, kept: bool) -> None:
    assert ssh_sync._is_ssh_proxy(value) is kept


def test_has_exec() -> None:
    assert ssh_sync._has_exec('host x exec "test -f /tmp/x"')
    assert ssh_sync._has_exec("!EXEC true")
    assert not ssh_sync._has_exec("host x user y")


def test_translator_paths(tmp_path: Path) -> None:
    home = tmp_path / "h"
    translator = ssh_sync._Translator(home)
    assert translator.path("~/.ssh/id_x") == "~/.ssh/id_x"
    assert translator.path("%d/.ssh/keys/../id_y") == "~/.ssh/id_y"
    assert translator.path("${HOME}/.ssh/sub/k") == "~/.ssh/sub/k"
    assert translator.path("~") == f"/z{home}"
    assert translator.path(f"{home}/keys/k") == f"/z{home}/keys/k"
    assert translator.path("/etc/ssh/ssh_known_hosts") == "/z/etc/ssh/ssh_known_hosts"
    assert translator.path("none") == "none"
    assert translator.path("/dev/null") == "/dev/null"
    assert translator.path("relative/key") == "relative/key"
    assert translator.path("~other/key") == "~other/key"
    assert translator.path(f"{home}/.ssh") == f"/z{home}/.ssh"


# --- translate_config ------------------------------------------------------------------


def test_translate_config_directives(host: Path) -> None:
    home = str(host)
    config = _config(
        host,
        "# top comment\n"
        "\n"
        "Host\n"
        "Host github.com\n"
        "    IdentityFile ~/.ssh/id_ed25519\n"
        f"    IdentityFile {home}/keys/work key\n"
        '    IdentityFile "~/.ssh/my key"\n'
        "    CertificateFile=~/.ssh/id_ed25519-cert.pub\n"
        f"    UserKnownHostsFile ~/.ssh/known_hosts {home}/other_hosts /dev/null\n"
        "    GlobalKnownHostsFile none\n"
        "    IdentityAgent ~/.1password/agent.sock\n"
        "    UseKeychain yes\n"
        "    ControlMaster auto\n"
        "    ControlPath ~/.ssh/cm-%r@%h:%p\n"
        "    ControlPersist 10m\n"
        "    ProxyCommand nc -X 5 -x proxy:1080 %h %p\n"
        "    ProxyCommand ssh -W %h:%p jump\n"
        "    ProxyJump bastion\n"
        "    User git\n"
        "    IdentityFile relative_key\n"
        "    IdentitiesOnly yes\n",
    )
    text, dropped, referenced = ssh_sync.translate_config(config, host)
    assert _body(text) == [
        "# top comment",
        "",
        "Host",
        "Host github.com",
        "    IdentityFile ~/.ssh/id_ed25519",
        f"    IdentityFile /z{home}/keys/work",
        '    IdentityFile "~/.ssh/my key"',
        "    CertificateFile=~/.ssh/id_ed25519-cert.pub",
        f"    UserKnownHostsFile ~/.ssh/known_hosts /z{home}/other_hosts /dev/null",
        "    GlobalKnownHostsFile none",
        "    # fork-linux: dropped (the agent socket of the Linux session is not reachable from Wine): "
        "IdentityAgent ~/.1password/agent.sock",
        "    # fork-linux: dropped (macOS only): UseKeychain yes",
        "    # fork-linux: dropped (connection sharing needs Unix sockets that Wine's ssh cannot use): "
        "ControlMaster auto",
        "    # fork-linux: dropped (connection sharing needs Unix sockets that Wine's ssh cannot use): "
        "ControlPath ~/.ssh/cm-%r@%h:%p",
        "    # fork-linux: dropped (connection sharing needs Unix sockets that Wine's ssh cannot use): "
        "ControlPersist 10m",
        "    # fork-linux: dropped (ProxyCommand runs a Linux command): ProxyCommand nc -X 5 -x proxy:1080 %h %p",
        "    ProxyCommand ssh -W %h:%p jump",
        "    ProxyJump bastion",
        "    User git",
        "    IdentityFile relative_key",
        "    IdentitiesOnly yes",
    ]
    assert [reason for _line, reason in dropped] == [
        ssh_sync.DROPPED["identityagent"],
        "macOS only",
        ssh_sync.DROPPED["controlmaster"],
        ssh_sync.DROPPED["controlpath"],
        ssh_sync.DROPPED["controlpersist"],
        ssh_sync.PROXY_REASON,
    ]
    assert dropped[0][0] == "IdentityAgent ~/.1password/agent.sock"
    assert referenced == {
        host / ".ssh" / "id_ed25519",
        host / "keys" / "work",
        host / ".ssh" / "my key",
        host / ".ssh" / "id_ed25519-cert.pub",
    }


def test_translate_config_match_exec_block(host: Path) -> None:
    config = _config(
        host,
        "Host a\n"
        "  User x\n"
        'Match host b exec "test -f /tmp/vpn"\n'
        "  ProxyJump vpn\n"
        "  Include extra\n"
        "\n"
        "Match host c\n"
        "  User y\n",
    )
    text, dropped, _referenced = ssh_sync.translate_config(config, host)
    assert _body(text) == [
        "Host a",
        "  User x",
        f'# fork-linux: dropped ({ssh_sync.MATCH_EXEC_REASON}): Match host b exec "test -f /tmp/vpn"',
        f"  # fork-linux: dropped ({ssh_sync.IN_MATCH_EXEC_REASON}): ProxyJump vpn",
        f"  # fork-linux: dropped ({ssh_sync.IN_MATCH_EXEC_REASON}): Include extra",
        "",
        "Match host c",
        "  User y",
    ]
    assert [line for line, _reason in dropped] == [
        'Match host b exec "test -f /tmp/vpn"',
        "ProxyJump vpn",
        "Include extra",
    ]


def test_translate_config_inlines_includes(host: Path) -> None:
    ssh = host / ".ssh"
    (ssh / "conf.d").mkdir()
    (ssh / "conf.d" / "10-work").write_text("Host work\n  IdentityFile ~/.ssh/id_rsa\n", encoding="utf-8")
    (ssh / "conf.d" / "20-plain").write_text("Compression yes\n", encoding="utf-8")
    (ssh / "conf.d" / "30-dir").mkdir()
    absolute = host / "abs.conf"
    absolute.write_text("ServerAliveInterval 30\n", encoding="utf-8")
    config = _config(
        host,
        "Include conf.d/*\n"
        "User global\n"
        "Host top\n"
        f"  Include {absolute} ~/missing-*\n"
        "  Include conf.d/10-work\n"
        "  Port 2222\n",
    )
    text, _dropped, referenced = ssh_sync.translate_config(config, host)
    work = ssh / "conf.d" / "10-work"
    plain = ssh / "conf.d" / "20-plain"
    assert _body(text) == [
        f"# fork-linux: begin Include {work}",
        "Host work",
        "  IdentityFile ~/.ssh/id_rsa",
        f"# fork-linux: end Include {work}",
        "Host *",
        f"# fork-linux: begin Include {plain}",
        "Compression yes",
        f"# fork-linux: end Include {plain}",
        "User global",
        "Host top",
        f"# fork-linux: begin Include {absolute}",
        "ServerAliveInterval 30",
        f"# fork-linux: end Include {absolute}",
        "# fork-linux: Include ~/missing-* matched no file",
        f"# fork-linux: begin Include {work}",
        "Host work",
        "  IdentityFile ~/.ssh/id_rsa",
        f"# fork-linux: end Include {work}",
        "Host top",
        "  Port 2222",
    ]
    assert referenced == {ssh / "id_rsa"}


def test_translate_config_include_loops_and_depth(host: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ssh = host / ".ssh"
    (ssh / "a").write_text("Include b\nUser a\n", encoding="utf-8")
    (ssh / "b").write_text("Include a\nUser b\n", encoding="utf-8")
    config = _config(host, "Include a\n")
    text, _dropped, _referenced = ssh_sync.translate_config(config, host)
    assert _body(text) == [
        f"# fork-linux: begin Include {ssh / 'a'}",
        f"# fork-linux: begin Include {ssh / 'b'}",
        f"# fork-linux: skipped Include {ssh / 'a'} (include loop or too deep)",
        "User b",
        f"# fork-linux: end Include {ssh / 'b'}",
        "User a",
        f"# fork-linux: end Include {ssh / 'a'}",
    ]
    monkeypatch.setattr(ssh_sync, "MAX_INCLUDE_DEPTH", 1)
    text, _dropped, _referenced = ssh_sync.translate_config(config, host)
    assert _body(text) == [f"# fork-linux: skipped Include {ssh / 'a'} (include loop or too deep)"]


def test_translate_config_unreadable_include(host: Path) -> None:
    secret = host / ".ssh" / "secret.conf"
    secret.write_text("User x\n", encoding="utf-8")
    secret.chmod(0)
    try:
        if os.access(secret, os.R_OK):
            pytest.skip("running with privileges that ignore file permissions")
        text, _dropped, _referenced = ssh_sync.translate_config(_config(host, "Include secret.conf\n"), host)
    finally:
        secret.chmod(0o600)
    body = _body(text)
    assert body[0] == f"# fork-linux: begin Include {secret}"
    assert body[1].startswith(f"# fork-linux: cannot read {secret}: ")
    assert body[2] == f"# fork-linux: end Include {secret}"


# --- host keys and manifest -----------------------------------------------------------------


def test_host_keys(host: Path, tmp_path: Path) -> None:
    ssh = host / ".ssh"
    (ssh / "keys").mkdir()
    (ssh / "keys" / "work").write_text("PRIVATE work\n", encoding="utf-8")
    (ssh / "keys" / "work.pub").write_text("pub\n", encoding="utf-8")
    outside = tmp_path / "outside_key"
    outside.write_text("x\n", encoding="utf-8")
    keys = ssh_sync._host_keys(ssh, [ssh / "keys" / "work", ssh / "missing", outside])
    assert [rel for rel, _path in keys] == [
        "id_ed25519",
        "id_ed25519-cert.pub",
        "id_ed25519.pub",
        "id_rsa",
        "id_rsa.pub",
        "keys/work",
        "keys/work.pub",
    ]
    assert dict(keys)["keys/work"] == ssh / "keys" / "work"
    assert ssh_sync._host_keys(tmp_path / "nowhere" / ".ssh", []) == []


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (None, set()),
        ("not json", set()),
        ("[1, 2]", set()),
        ('{"files": "x"}', set()),
        ('{"files": ["id_a", 3, "", "/abs", "../up", "a//b", "keys/k"]}', {"id_a", "keys/k"}),
    ],
)
def test_read_manifest(tmp_path: Path, content: str | None, expected: set[str]) -> None:
    if content is not None:
        (tmp_path / ssh_sync.MANIFEST).write_text(content, encoding="utf-8")
    assert ssh_sync._read_manifest(tmp_path) == expected


def test_place_link_replaces_a_leftover_temp(tmp_path: Path) -> None:
    source = tmp_path / "src"
    source.write_text("x", encoding="utf-8")
    dest = tmp_path / "d" / "key"
    dest.parent.mkdir()
    (dest.parent / ".key.fork-linux-tmp").write_text("stale", encoding="utf-8")
    ssh_sync._place_link(source, dest)
    assert os.readlink(dest) == str(source)
    assert not (dest.parent / ".key.fork-linux-tmp").exists()


# --- sync -----------------------------------------------------------------------------------------


def test_sync_rejects_unknown_mode(paths: Paths, host: Path) -> None:
    with pytest.raises(UsageError):
        ssh_sync.sync(paths, USER, host, mode="hardlink")


def test_sync_requires_the_wine_user(xdg: Path, host: Path) -> None:
    paths = Paths.from_env(dict(os.environ))
    with pytest.raises(NotSetUpError):
        ssh_sync.sync(paths, "ghost", host)


def test_sync_link_mode(paths: Paths, host: Path) -> None:
    _config(host, "Host x\n  IdentityFile ~/.ssh/id_rsa\n  ControlMaster auto\n")
    report = ssh_sync.sync(paths, USER, host)
    wine = _wine_ssh(paths)
    assert stat.S_IMODE(wine.stat().st_mode) == 0o700
    names = ["id_ed25519", "id_ed25519-cert.pub", "id_ed25519.pub", "id_rsa", "id_rsa.pub", "known_hosts"]
    assert report.linked == [wine / name for name in names]
    assert report.copied == []
    assert report.config_path == wine / "config"
    assert report.dropped_directives == [("ControlMaster auto", ssh_sync.DROPPED["controlmaster"])]
    for name in names:
        assert os.readlink(wine / name) == str(host / ".ssh" / name)
    assert (wine / "config").read_text(encoding="utf-8").startswith(ssh_sync.HEADER + "\n")
    assert stat.S_IMODE((wine / "config").stat().st_mode) == 0o600
    manifest = json.loads((wine / ssh_sync.MANIFEST).read_text(encoding="utf-8"))
    assert manifest == {"schema": 1, "files": sorted([*names, "config"])}
    # A second run changes nothing but still reports the same state.
    again = ssh_sync.sync(paths, USER, host)
    assert again.linked == report.linked and again.removed == [] and again.skipped == []


def test_sync_copy_mode_then_link_mode(paths: Paths, host: Path) -> None:
    report = ssh_sync.sync(paths, USER, host, mode="copy", include_config=False)
    wine = _wine_ssh(paths)
    assert report.linked == [wine / "known_hosts"]
    assert report.copied == [
        wine / name for name in ("id_ed25519", "id_ed25519-cert.pub", "id_ed25519.pub", "id_rsa", "id_rsa.pub")
    ]
    assert report.config_path is None
    assert not (wine / "id_rsa").is_symlink()
    assert stat.S_IMODE((wine / "id_rsa").stat().st_mode) == 0o600
    assert stat.S_IMODE((wine / "id_rsa.pub").stat().st_mode) == 0o644
    assert (wine / "id_rsa").read_text(encoding="utf-8") == "PRIVATE id_rsa\n"
    ssh_sync.sync(paths, USER, host, mode="link", include_config=False)
    assert (wine / "id_rsa").is_symlink()


def test_sync_replaces_a_symlinked_ssh_dir(paths: Paths, host: Path) -> None:
    wine = _wine_ssh(paths)
    wine.symlink_to(host / ".ssh")
    ssh_sync.sync(paths, USER, host)
    assert wine.is_dir() and not wine.is_symlink()
    assert sorted(p.name for p in (host / ".ssh").iterdir()) == [
        "id_dir", "id_dir.pub", "id_ed25519", "id_ed25519-cert.pub", "id_ed25519.pub", "id_lonely",
        "id_rsa", "id_rsa.pub", "known_hosts",
    ]


def test_sync_refuses_a_file_named_ssh(paths: Paths, host: Path) -> None:
    _wine_ssh(paths).write_text("odd", encoding="utf-8")
    with pytest.raises(ForkLinuxError, match="not a directory"):
        ssh_sync.sync(paths, USER, host)


def test_sync_never_overwrites_foreign_files(paths: Paths, host: Path, tmp_path: Path) -> None:
    wine = _wine_ssh(paths)
    wine.mkdir(mode=0o700)
    (wine / "id_rsa").write_text("generated inside Fork\n", encoding="utf-8")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.write_text("x", encoding="utf-8")
    (wine / "id_ed25519").symlink_to(elsewhere)
    (wine / "id_rsa.pub").symlink_to(host / ".ssh" / "id_ed25519.pub")
    report = ssh_sync.sync(paths, USER, host)
    assert report.skipped == [wine / "id_ed25519", wine / "id_rsa"]
    assert (wine / "id_rsa").read_text(encoding="utf-8") == "generated inside Fork\n"
    assert os.readlink(wine / "id_ed25519") == str(elsewhere)
    # A link into ~/.ssh is ours to repoint.
    assert os.readlink(wine / "id_rsa.pub") == str(host / ".ssh" / "id_rsa.pub")


def test_sync_removes_what_the_host_no_longer_has(paths: Paths, host: Path) -> None:
    _config(host, "Host x\n")
    ssh_sync.sync(paths, USER, host, mode="copy")
    wine = _wine_ssh(paths)
    for name in ("id_rsa", "id_rsa.pub", "config"):
        (host / ".ssh" / name).unlink()
    (wine / "keys").mkdir()
    manifest = json.loads((wine / ssh_sync.MANIFEST).read_text(encoding="utf-8"))
    manifest["files"] += ["keys", "gone"]
    (wine / ssh_sync.MANIFEST).write_text(json.dumps(manifest), encoding="utf-8")
    dry = ssh_sync.sync(paths, USER, host, dry_run=True)
    assert dry.removed == [wine / "config", wine / "id_rsa", wine / "id_rsa.pub"]
    assert (wine / "id_rsa").exists()
    report = ssh_sync.sync(paths, USER, host)
    assert report.removed == [wine / "config", wine / "id_rsa", wine / "id_rsa.pub"]
    assert not (wine / "id_rsa").exists() and not (wine / "config").exists()
    assert (wine / "keys").is_dir()


def test_sync_never_writes_or_deletes_through_a_linked_subdirectory(
    paths: Paths, host: Path, tmp_path: Path
) -> None:
    ssh = host / ".ssh"
    (ssh / "keys").mkdir()
    (ssh / "keys" / "work").write_text("PRIVATE work\n", encoding="utf-8")
    _config(host, "IdentityFile ~/.ssh/keys/work\n")
    ssh_sync.sync(paths, USER, host, mode="copy")
    wine = _wine_ssh(paths)
    assert (wine / "keys" / "work").read_text(encoding="utf-8") == "PRIVATE work\n"
    # The managed subdirectory is swapped for a link to somewhere else.
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "work").write_text("not ours\n", encoding="utf-8")
    for child in (wine / "keys").iterdir():
        child.unlink()
    (wine / "keys").rmdir()
    (wine / "keys").symlink_to(elsewhere)
    report = ssh_sync.sync(paths, USER, host, mode="copy")
    assert report.skipped == [wine / "keys" / "work"]
    assert (elsewhere / "work").read_text(encoding="utf-8") == "not ours\n"
    # Even once the key is gone from the host, the file behind the link is not deleted.
    (ssh / "keys" / "work").unlink()
    manifest = json.loads((wine / ssh_sync.MANIFEST).read_text(encoding="utf-8"))
    manifest["files"].append("keys/work")
    (wine / ssh_sync.MANIFEST).write_text(json.dumps(manifest), encoding="utf-8")
    assert ssh_sync.sync(paths, USER, host, mode="copy").removed == []
    assert (elsewhere / "work").exists()


def test_sync_keeps_a_config_that_is_no_longer_generated(paths: Paths, host: Path) -> None:
    _config(host, "Host x\n")
    ssh_sync.sync(paths, USER, host)
    wine = _wine_ssh(paths)
    (wine / "config").write_text("Host mine\n", encoding="utf-8")
    report = ssh_sync.sync(paths, USER, host, include_config=False)
    assert report.removed == []
    assert (wine / "config").read_text(encoding="utf-8") == "Host mine\n"


def test_sync_regenerates_an_edited_generated_config(paths: Paths, host: Path) -> None:
    _config(host, "Host x\n")
    ssh_sync.sync(paths, USER, host)
    wine = _wine_ssh(paths)
    (wine / "config").write_text("edited\n", encoding="utf-8")
    ssh_sync.sync(paths, USER, host)
    assert (wine / "config").read_text(encoding="utf-8").startswith(ssh_sync.HEADER)
    assert not (wine / ssh_sync.CONFIG_BACKUP).exists()


def test_sync_backs_up_a_hand_written_config(paths: Paths, host: Path) -> None:
    _config(host, "Host x\n")
    wine = _wine_ssh(paths)
    wine.mkdir(mode=0o700)
    (wine / "config").write_text("Host handmade\n", encoding="utf-8")
    report = ssh_sync.sync(paths, USER, host)
    assert report.config_path == wine / "config"
    assert (wine / ssh_sync.CONFIG_BACKUP).read_text(encoding="utf-8") == "Host handmade\n"
    assert (wine / "config").read_text(encoding="utf-8").startswith(ssh_sync.HEADER)


def test_sync_skips_a_hand_written_config_when_a_backup_exists(paths: Paths, host: Path) -> None:
    _config(host, "Host x\n")
    wine = _wine_ssh(paths)
    wine.mkdir(mode=0o700)
    (wine / "config").write_text("Host handmade\n", encoding="utf-8")
    (wine / ssh_sync.CONFIG_BACKUP).write_text("older\n", encoding="utf-8")
    report = ssh_sync.sync(paths, USER, host)
    assert report.config_path is None
    assert report.skipped == [wine / "config"]
    assert (wine / "config").read_text(encoding="utf-8") == "Host handmade\n"


def test_sync_replaces_a_symlinked_config(paths: Paths, host: Path) -> None:
    host_config = _config(host, "Host x\n")
    wine = _wine_ssh(paths)
    wine.mkdir(mode=0o700)
    (wine / "config").symlink_to(host_config)
    ssh_sync.sync(paths, USER, host)
    assert not (wine / "config").is_symlink()
    assert host_config.read_text(encoding="utf-8") == "Host x\n"


def test_sync_dry_run_writes_nothing(paths: Paths, host: Path) -> None:
    _config(host, "Host x\n  UseKeychain yes\n")
    report = ssh_sync.sync(paths, USER, host, mode="copy", dry_run=True)
    wine = _wine_ssh(paths)
    assert not wine.exists()
    assert report.config_path == wine / "config"
    assert len(report.copied) == 5 and report.linked == [wine / "known_hosts"]
    assert report.dropped_directives == [("UseKeychain yes", "macOS only")]


def test_sync_without_host_ssh(paths: Paths, tmp_path: Path) -> None:
    report = ssh_sync.sync(paths, USER, tmp_path / "empty-home")
    assert report == ssh_sync.SyncReport()
    assert _wine_ssh(paths).is_dir()


# --- status ------------------------------------------------------------------------------------------


def test_status_before_sync(paths: Paths, host: Path) -> None:
    result = ssh_sync.status(paths, USER, host)
    assert result == {
        "host_dir": str(host / ".ssh"),
        "host_present": True,
        "wine_dir": str(_wine_ssh(paths)),
        "exists": False,
        "is_symlink": False,
        "dir_ok": False,
        "mode": None,
        "perms_ok": False,
        "insecure_files": [],
        "managed": [],
        "broken_links": [],
        "config": "none",
        "stale_config": False,
    }


def test_status_symlinked_dir(paths: Paths, host: Path) -> None:
    _wine_ssh(paths).symlink_to(host / ".ssh")
    result = ssh_sync.status(paths, USER, host)
    assert result["exists"] and result["is_symlink"] and not result["dir_ok"]


def test_status_after_sync(paths: Paths, host: Path) -> None:
    host_config = _config(host, "Host x\n")
    ssh_sync.sync(paths, USER, host, mode="copy")
    wine = _wine_ssh(paths)
    result = ssh_sync.status(paths, USER, host)
    assert result["dir_ok"] and result["perms_ok"] and result["mode"] == "0700"
    assert result["config"] == "current" and not result["stale_config"]
    assert "id_rsa" in result["managed"]
    # Problems: a readable private copy, a broken link, a stale config.
    os.chmod(wine / "id_rsa", 0o644)
    (wine / "dangling").symlink_to(host / ".ssh" / "nope")
    (wine / "sub").mkdir()
    (wine / "sub" / "dangling-dir").symlink_to(host / "nodir")
    host_config.write_text("Host y\n", encoding="utf-8")
    result = ssh_sync.status(paths, USER, host)
    assert result["insecure_files"] == ["id_rsa"] and not result["perms_ok"] and result["dir_ok"]
    assert result["broken_links"] == ["dangling", "sub/dangling-dir"]
    assert result["config"] == "stale" and result["stale_config"]
    host_config.unlink()
    assert ssh_sync.status(paths, USER, host)["config"] == "stale"
    (wine / "config").write_text("Host mine\n", encoding="utf-8")
    assert ssh_sync.status(paths, USER, host)["config"] == "foreign"


def test_status_missing_config_and_skipped_entries(paths: Paths, host: Path) -> None:
    ssh_sync.sync(paths, USER, host, include_config=False)
    wine = _wine_ssh(paths)
    _config(host, "Host x\n")
    manifest = json.loads((wine / ssh_sync.MANIFEST).read_text(encoding="utf-8"))
    manifest["files"].append("vanished")
    (wine / ssh_sync.MANIFEST).write_text(json.dumps(manifest), encoding="utf-8")
    result = ssh_sync.status(paths, USER, host)
    assert result["config"] == "missing"
    assert result["insecure_files"] == []
    assert result["perms_ok"]


def test_insecure_skips_public_links_and_known_hosts(tmp_path: Path) -> None:
    (tmp_path / "k.pub").write_text("x", encoding="utf-8")
    (tmp_path / "known_hosts").write_text("x", encoding="utf-8")
    (tmp_path / "link").symlink_to(tmp_path / "k.pub")
    for name in ("k.pub", "known_hosts"):
        os.chmod(tmp_path / name, 0o644)
    assert ssh_sync._insecure(tmp_path, ["k.pub", "known_hosts", "link", "missing"]) == []


def test_generated_handles_unreadable_paths(tmp_path: Path) -> None:
    assert not ssh_sync._generated(tmp_path / "missing")


def test_translate_config_drops_linux_commands_and_libraries(host: Path) -> None:
    config = _config(
        host,
        "Host *\n"
        "    KnownHostsCommand /usr/bin/fetch-keys %H\n"
        "    PermitLocalCommand yes\n"
        "    LocalCommand notify-send connected\n"
        "    PKCS11Provider /usr/lib/x86_64-linux-gnu/opensc-pkcs11.so\n"
        "    SecurityKeyProvider internal\n",
    )
    text, dropped, _referenced = ssh_sync.translate_config(config, host)
    assert "    PermitLocalCommand yes" in text.splitlines()
    assert [reason for _line, reason in dropped] == [
        ssh_sync.DROPPED["knownhostscommand"],
        ssh_sync.DROPPED["localcommand"],
        ssh_sync.DROPPED["pkcs11provider"],
        ssh_sync.DROPPED["securitykeyprovider"],
    ]
    assert f"    {ssh_sync.DROP_PREFIX} (it loads a Linux library): SecurityKeyProvider internal" in text.splitlines()
