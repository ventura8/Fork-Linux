"""Tests for fork_linux.gitconfig: host git config translation, the managed include, env overrides."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from fork_linux import gitconfig
from fork_linux.config import Config
from fork_linux.errors import ForkLinuxError, NotSetUpError, UsageError
from fork_linux.pathmap import PathMap
from fork_linux.paths import Paths
from fork_linux.procrun import Completed, RecordingRunner

USER = "tester"


@pytest.fixture
def paths(xdg: Path) -> Paths:
    """Paths of the fake home, with the Wine user directory created."""
    resolved = Paths.from_env(dict(os.environ))
    resolved.wine_user_dir(USER).mkdir(parents=True)
    return resolved


@pytest.fixture
def pathmap(paths: Paths) -> PathMap:
    """C: = the prefix's drive_c, Z: = /."""
    return PathMap.with_drives({"c": paths.prefix / "drive_c", "z": "/"})


def _translate(text: str, pathmap: PathMap) -> tuple[list[str], list[str]]:
    out, notes = gitconfig.translate(text, pathmap)
    return out.splitlines(), notes


# --- host_config_text ----------------------------------------------------------


def test_host_config_text_reads_xdg_then_home(tmp_path: Path) -> None:
    (tmp_path / ".config" / "git").mkdir(parents=True)
    (tmp_path / ".config" / "git" / "config").write_text("[core]\n\teditor = vim", encoding="utf-8")
    (tmp_path / ".gitconfig").write_text("[user]\n\tname = A\n", encoding="utf-8")
    text = gitconfig.host_config_text(tmp_path)
    assert text == (
        "# from ~/.config/git/config\n[core]\n\teditor = vim\n"
        "# from ~/.gitconfig\n[user]\n\tname = A\n"
    )


def test_host_config_text_missing_files(tmp_path: Path) -> None:
    assert gitconfig.host_config_text(tmp_path) == ""


def test_host_config_text_unreadable(tmp_path: Path) -> None:
    (tmp_path / ".gitconfig").mkdir()
    with pytest.raises(ForkLinuxError, match="cannot read"):
        gitconfig.host_config_text(tmp_path)


# --- value parsing and quoting ---------------------------------------------------


@pytest.mark.parametrize(
    ("rest", "value"),
    [
        (" plain", "plain"),
        ('  "a b"  ', "a b"),
        ("a  b   ", "a  b"),
        ("v # comment", "v"),
        ("v ; comment", "v"),
        ('"x # not a comment"', "x # not a comment"),
        (r'a\tb\nc\bd\\e\"f', 'a\tb\nc\bd\\e"f'),
        ("", ""),
        ("   ", ""),
        ('"open', None),
        (r"bad\q", None),
    ],
)
def test_parse_value(rest: str, value: str | None) -> None:
    assert gitconfig._parse_value([rest], 0, rest) == (value, 0)


def test_parse_value_continuation() -> None:
    lines = ["\thelper = a \\", "  b", "next"]
    assert gitconfig._parse_value(lines, 0, " a \\") == ("a   b", 1)


def test_parse_value_continuation_at_end_of_file() -> None:
    assert gitconfig._parse_value(["x = a\\"], 0, " a\\") == ("a", 0)


@pytest.mark.parametrize(
    ("value", "quoted"),
    [
        ("", ""),
        ("Z:/a b/c", "Z:/a b/c"),
        (" lead", '" lead"'),
        ("trail ", '"trail "'),
        ("a#b", '"a#b"'),
        ('q"\\', '"q\\"\\\\"'),
        ("n\nt\tb\b", '"n\\nt\\tb\\b"'),
    ],
)
def test_quote(value: str, quoted: str) -> None:
    assert gitconfig._quote(value) == quoted


def test_lines_and_newline() -> None:
    assert gitconfig._lines("") == []
    assert gitconfig._lines("a\r\nb") == ["a", "b"]
    assert gitconfig._lines("a\n\n") == ["a", ""]
    assert gitconfig._newline("a\r\nb\r\n") == "\r\n"
    assert gitconfig._newline("a\nb\n") == "\n"


# --- path translation --------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("/home/u/.gitignore", "Z:/home/u/.gitignore"),
        ("/home/u/hooks/", "Z:/home/u/hooks/"),
        ("/", "Z:/"),
        ("~/.gitignore", "~/.gitignore"),
        ("relative/x", "relative/x"),
        (":(optional)/home/u/x", ":(optional)Z:/home/u/x"),
        (":(optional)~/x", ":(optional)~/x"),
    ],
)
def test_translate_path(value: str, expected: str, pathmap: PathMap) -> None:
    assert gitconfig._translate_path(value, pathmap) == expected


def test_translate_path_prefers_the_longest_drive(paths: Paths, pathmap: PathMap) -> None:
    inside = paths.prefix / "drive_c" / "users" / USER / "x"
    assert gitconfig._translate_path(str(inside), pathmap) == f"C:/users/{USER}/x"


@pytest.mark.parametrize(
    ("condition", "expected"),
    [
        ("gitdir:/home/u/work/", "gitdir:Z:/home/u/work/"),
        ("gitdir/i:/home/u/Work", "gitdir/i:Z:/home/u/Work"),
        ("gitdir:/", "gitdir:Z:/"),
        ("gitdir:/**/x/", "gitdir:Z:/**/x/"),
        ("gitdir:/home/u/**", "gitdir:Z:/home/u/**"),
        ("gitdir:/srv/proj-?/", "gitdir:Z:/srv/proj-?/"),
        ("gitdir:/a/[ab]/c", "gitdir:Z:/a/[ab]/c"),
        ("gitdir:~/work/", "gitdir:~/work/"),
        ("gitdir:work/", "gitdir:work/"),
        ("onbranch:main", "onbranch:main"),
        ("hasconfig:remote.*.url:https://x/**", "hasconfig:remote.*.url:https://x/**"),
    ],
)
def test_translate_gitdir(condition: str, expected: str, pathmap: PathMap) -> None:
    assert gitconfig._translate_gitdir(condition, pathmap) == expected


def test_ssh_command_detection() -> None:
    assert gitconfig._ssh_command_is_linux("/usr/bin/ssh -v")
    assert gitconfig._ssh_command_is_linux("ssh -i /home/u/.ssh/key")
    assert not gitconfig._ssh_command_is_linux("ssh -o BatchMode=yes")
    assert gitconfig._ssh_command_is_linux("ssh -o 'unbalanced /x")
    assert not gitconfig._ssh_command_is_linux("ssh 'unbalanced")


def test_drop_reason() -> None:
    assert gitconfig._drop_reason("core.editor", "vim") == gitconfig.DROPPED_KEYS["core.editor"]
    assert gitconfig._drop_reason("gpg.*.program", "x") is not None
    assert gitconfig._drop_reason("core.sshcommand", "/usr/bin/ssh") == gitconfig.SSH_COMMAND_REASON
    assert gitconfig._drop_reason("core.sshcommand", "ssh -v") is None
    assert gitconfig._drop_reason("core.sshcommand", None) is None
    assert gitconfig._drop_reason("user.name", "x") is None


# --- translate ------------------------------------------------------------------------


def test_translate_full_example(pathmap: PathMap) -> None:
    text = (
        "# my config\n"
        "\n"
        "[user]\n"
        "\tname = Ada\n"
        "[core]\n"
        "\teditor = vim\n"
        "\texcludesFile = /home/ada/.gitignore_global\n"
        "\thooksPath = ~/hooks\n"
        "\tattributesFile = /home/ada/attr # comment\n"
        "[includeIf \"gitdir:/home/ada/work/\"]\n"
        "\tpath = /home/ada/.gitconfig-work\n"
        "[includeIf \"onbranch:main\"]\n"
        "\tpath = ~/.main\n"
        "[credential \"https://example.com\"]\n"
        "\thelper = /usr/lib/git-core/git-credential-libsecret \\\n"
        "\t   --verbose\n"
        "[credential]\n"
        "\thelper\n"
        "[gpg]\n"
        "\tprogram = gpg2\n"
        "[gpg \"ssh\"]\n"
        "\tprogram = /usr/bin/ssh-keygen\n"
        "[sequence]\n"
        "\teditor = interactive-rebase-tool\n"
        "[core] sshCommand = /usr/bin/ssh -F /dev/null\n"
        "[commit] template = \"/home/ada/my template.txt\"\n"
        "[init]\n"
        "\ttemplateDir = /usr/share/git-core/templates/\n"
        "[include]\n"
        "\tpath = relative.inc\n"
        "\tpath\n"
    )
    lines, notes = _translate(text, pathmap)
    assert lines == [
        "# my config",
        "",
        "[user]",
        "\tname = Ada",
        "[core]",
        "# fork-linux: dropped (a Linux editor cannot run under Wine): \teditor = vim",
        "\texcludesFile = Z:/home/ada/.gitignore_global",
        "\thooksPath = ~/hooks",
        "\tattributesFile = Z:/home/ada/attr",
        '[includeIf "gitdir:Z:/home/ada/work/"]',
        "\tpath = Z:/home/ada/.gitconfig-work",
        '[includeIf "onbranch:main"]',
        "\tpath = ~/.main",
        '[credential "https://example.com"]',
        "# fork-linux: dropped (Linux credential helpers cannot run under Wine): "
        "\thelper = /usr/lib/git-core/git-credential-libsecret \\",
        "# fork-linux: dropped (Linux credential helpers cannot run under Wine): \t   --verbose",
        "[credential]",
        "# fork-linux: dropped (Linux credential helpers cannot run under Wine): \thelper",
        "[gpg]",
        "# fork-linux: dropped (the Linux gpg cannot run under Wine): \tprogram = gpg2",
        '[gpg "ssh"]',
        "# fork-linux: dropped (the Linux gpg cannot run under Wine): \tprogram = /usr/bin/ssh-keygen",
        "[sequence]",
        "# fork-linux: dropped (a Linux editor cannot run under Wine): \teditor = interactive-rebase-tool",
        "[core]",
        "# fork-linux: dropped (it names a Linux program by absolute path): \tsshCommand = /usr/bin/ssh -F /dev/null",
        "[commit]",
        "\ttemplate = Z:/home/ada/my template.txt",
        "[init]",
        "\ttemplateDir = Z:/usr/share/git-core/templates/",
        "[include]",
        "\tpath = relative.inc",
        "\tpath",
    ]
    assert notes == [
        "core.editor: dropped (a Linux editor cannot run under Wine)",
        "credential.https://example.com.helper: dropped (Linux credential helpers cannot run under Wine)",
        "credential.helper: dropped (Linux credential helpers cannot run under Wine)",
        "gpg.program: dropped (the Linux gpg cannot run under Wine)",
        "gpg.ssh.program: dropped (the Linux gpg cannot run under Wine)",
        "sequence.editor: dropped (a Linux editor cannot run under Wine)",
        "core.sshcommand: dropped (it names a Linux program by absolute path)",
    ]


def test_translate_keeps_ssh_command_without_paths(pathmap: PathMap) -> None:
    lines, notes = _translate("[core]\n\tsshCommand = ssh -o BatchMode=yes\n", pathmap)
    assert lines == ["[core]", "\tsshCommand = ssh -o BatchMode=yes"]
    assert notes == []


def test_translate_header_variants(pathmap: PathMap) -> None:
    text = (
        '[Remote "o\\"x"]\n'
        "\turl = /srv/repo.git\n"
        "[Core.Something]\n"
        "\texcludesFile = /x\n"
        "[includeIf]\n"
        "\tpath = /y\n"
        "  [include]   # trailing comment\n"
        "\tpath = /z\n"
        "[include]   \n"
        '[includeIf "gitdir:/a\\\\b/"]\n'
    )
    lines, notes = _translate(text, pathmap)
    assert lines == [
        '[Remote "o\\"x"]',
        "\turl = /srv/repo.git",
        "[Core.Something]",
        "\texcludesFile = /x",
        "[includeIf]",
        "\tpath = /y",
        "  [include]",
        "  \t# trailing comment",
        "\tpath = Z:/z",
        "[include]",
        '[includeIf "gitdir:Z:/a\\\\b/"]',
    ]
    assert notes == []


def test_translate_condition_quoting(pathmap: PathMap) -> None:
    # A quote after a glob is kept as-is and re-escaped in the header.
    lines, notes = _translate('[includeIf "gitdir:/x/*\\"q/"]\n', pathmap)
    assert lines == ['[includeIf "gitdir:Z:/x/*\\"q/"]']
    assert notes == []
    # A quote in the literal part cannot be mapped to a Windows path.
    lines, notes = _translate('[includeIf "gitdir:/q\\"x/"]\n', pathmap)
    assert lines == ['[includeIf "gitdir:/q\\"x/"]']
    assert len(notes) == 1 and "kept unchanged" in notes[0]


def test_translate_keeps_unreachable_paths_with_a_note(paths: Paths) -> None:
    only_c = PathMap.with_drives({"c": paths.prefix / "drive_c"})
    text = '[includeIf "gitdir:/home/u/w/"]\n\tpath = /home/u/.inc\n'
    lines, notes = _translate(text, only_c)
    assert lines == ['[includeIf "gitdir:/home/u/w/"]', "\tpath = /home/u/.inc"]
    assert len(notes) == 2
    assert notes[0].startswith("includeIf condition 'gitdir:/home/u/w/': kept unchanged (")
    assert notes[1].startswith("includeif.gitdir:/home/u/w/.path = '/home/u/.inc': kept unchanged (")


def test_translate_copies_lines_git_would_reject(pathmap: PathMap) -> None:
    text = "[core]\n\texcludesFile = \"/open\n\tname value\n=orphan\n[broken\n\tflag # set\n"
    lines, notes = _translate(text, pathmap)
    assert lines == ["[core]", '\texcludesFile = "/open', "\tname value", "=orphan", "[broken", "\tflag # set"]
    assert notes == []


def test_translate_empty_text(pathmap: PathMap) -> None:
    assert gitconfig.translate("", pathmap) == ("", [])


def test_translate_crlf_input(pathmap: PathMap) -> None:
    out, _notes = gitconfig.translate("[core]\r\n\texcludesFile = /x\r\n", pathmap)
    assert out == "[core]\n\texcludesFile = Z:/x\n"


# --- managed block ------------------------------------------------------------------------

BLOCK = f"{gitconfig.MARK_BEGIN}\n[include]\n\tpath = {gitconfig.OVERLAY_INCLUDE}\n{gitconfig.MARK_END}\n"


def test_ensure_block_prepends_and_is_idempotent() -> None:
    once = gitconfig.ensure_block("[user]\n\tname = Ada\n")
    assert once == BLOCK + "[user]\n\tname = Ada\n"
    assert gitconfig.ensure_block(once) == once
    assert gitconfig.ensure_block("") == BLOCK


def test_ensure_block_moves_a_block_to_the_top() -> None:
    text = "[user]\n\tname = Ada\n" + BLOCK + "[core]\n\tx = 1\n" + BLOCK
    assert gitconfig.ensure_block(text) == BLOCK + "[user]\n\tname = Ada\n[core]\n\tx = 1\n"


def test_ensure_block_with_a_lost_end_marker() -> None:
    body = f"{gitconfig.MARK_BEGIN}\n[include]\n\tpath = {gitconfig.OVERLAY_INCLUDE}\n[user]\n\tname = A\n"
    assert gitconfig.ensure_block(body) == BLOCK + "[user]\n\tname = A\n"
    foreign = f"{gitconfig.MARK_BEGIN}\n[user]\n\tname = A\n"
    assert gitconfig.ensure_block(foreign) == BLOCK + "[user]\n\tname = A\n"
    assert gitconfig.ensure_block(f"[a]\n{gitconfig.MARK_BEGIN}") == BLOCK + "[a]\n"


def test_ensure_block_keeps_windows_line_endings() -> None:
    result = gitconfig.ensure_block("[user]\r\n\tname = A\r\n")
    assert result == BLOCK.replace("\n", "\r\n") + "[user]\r\n\tname = A\r\n"


# --- sync / remove ----------------------------------------------------------------------------


def _home(tmp_path: Path, text: str | None = "[core]\n\teditor = vim\n\texcludesFile = /home/ada/x\n") -> Path:
    home = tmp_path / "host-home"
    home.mkdir()
    if text is not None:
        (home / ".gitconfig").write_text(text, encoding="utf-8")
    return home


def test_sync_requires_the_wine_user_dir(xdg: Path, pathmap: PathMap, tmp_path: Path) -> None:
    paths = Paths.from_env(dict(os.environ))
    with pytest.raises(NotSetUpError):
        gitconfig.sync(paths, "nobody", _home(tmp_path), pathmap)


def test_sync_writes_overlay_and_block(paths: Paths, pathmap: PathMap, tmp_path: Path) -> None:
    user_dir = paths.wine_user_dir(USER)
    (user_dir / ".gitconfig").write_bytes(b"[user]\n\tname = Ad\xe9\n")
    os.chmod(user_dir / ".gitconfig", 0o644)
    home = _home(tmp_path)
    overlay = gitconfig.overlay_path(paths, USER)
    assert overlay == user_dir / ".config" / "fork-linux" / "gitconfig-host"
    assert gitconfig.sync(paths, USER, home, pathmap) == [overlay, user_dir / ".gitconfig"]
    text = overlay.read_text(encoding="utf-8")
    assert text.startswith(gitconfig.HEADER)
    assert "# note: core.editor: dropped (a Linux editor cannot run under Wine)\n" in text
    assert "\texcludesFile = Z:/home/ada/x\n" in text
    assert stat.S_IMODE(overlay.stat().st_mode) == gitconfig.FILE_MODE
    assert (user_dir / ".gitconfig").read_bytes() == BLOCK.encode() + b"[user]\n\tname = Ad\xe9\n"
    assert stat.S_IMODE((user_dir / ".gitconfig").stat().st_mode) == 0o644
    assert gitconfig.sync(paths, USER, home, pathmap) == []


def test_sync_without_host_config(paths: Paths, pathmap: PathMap, tmp_path: Path) -> None:
    root = tmp_path / "root"
    changed = gitconfig.sync(paths, USER, _home(tmp_path, None), pathmap, host_root=root)
    user_dir = paths.wine_user_dir(USER)
    assert changed == [gitconfig.overlay_path(paths, USER), user_dir / ".gitconfig"]
    assert gitconfig.overlay_path(paths, USER).read_text(encoding="utf-8") == gitconfig.HEADER + (
        gitconfig.MANAGED_HEADER + "[credential]\n\tcredentialStore = dpapi\n"
    )
    assert (user_dir / ".gitconfig").read_text(encoding="utf-8") == BLOCK


def test_sync_replaces_a_symlinked_gitconfig(paths: Paths, pathmap: PathMap, tmp_path: Path) -> None:
    home = _home(tmp_path)
    before = (home / ".gitconfig").read_text(encoding="utf-8")
    link = paths.wine_user_dir(USER) / ".gitconfig"
    link.symlink_to(home / ".gitconfig")
    gitconfig.sync(paths, USER, home, pathmap)
    assert not link.is_symlink()
    assert link.read_text(encoding="utf-8") == BLOCK
    assert (home / ".gitconfig").read_text(encoding="utf-8") == before


def test_remove_undoes_sync(paths: Paths, pathmap: PathMap, tmp_path: Path) -> None:
    user_dir = paths.wine_user_dir(USER)
    (user_dir / ".gitconfig").write_text("[user]\n\tname = A\n", encoding="utf-8")
    gitconfig.sync(paths, USER, _home(tmp_path), pathmap)
    overlay = gitconfig.overlay_path(paths, USER)
    assert gitconfig.remove(paths, USER) == [user_dir / ".gitconfig", overlay]
    assert (user_dir / ".gitconfig").read_text(encoding="utf-8") == "[user]\n\tname = A\n"
    assert not overlay.exists()
    assert gitconfig.remove(paths, USER) == []


def test_remove_keeps_a_foreign_overlay(paths: Paths) -> None:
    overlay = gitconfig.overlay_path(paths, USER)
    overlay.parent.mkdir(parents=True)
    overlay.write_text("[user]\n\tname = mine\n", encoding="utf-8")
    assert gitconfig.remove(paths, USER) == []
    assert overlay.exists()


# --- env_overrides --------------------------------------------------------------------------------


def _config(tmp_path: Path, **values: str) -> Config:
    return Config(tmp_path / "config.ini", {("git", key): value for key, value in values.items()}, env={})


def test_env_overrides_defaults(tmp_path: Path) -> None:
    assert gitconfig.env_overrides(_config(tmp_path)) == {
        "GIT_CONFIG_COUNT": "2",
        "GIT_CONFIG_KEY_0": "core.filemode",
        "GIT_CONFIG_VALUE_0": "false",
        "GIT_CONFIG_KEY_1": "core.autocrlf",
        "GIT_CONFIG_VALUE_1": "false",
    }


def test_env_overrides_safe_directory(tmp_path: Path) -> None:
    env = gitconfig.env_overrides(_config(tmp_path, env_overrides="core.symlinks = true", safe_directory_all="true"))
    assert env == {
        "GIT_CONFIG_COUNT": "2",
        "GIT_CONFIG_KEY_0": "core.symlinks",
        "GIT_CONFIG_VALUE_0": "true",
        "GIT_CONFIG_KEY_1": "safe.directory",
        "GIT_CONFIG_VALUE_1": "*",
    }


def test_env_overrides_for_native_git_drop_the_bundled_only_keys(tmp_path: Path) -> None:
    config = _config(tmp_path, env_overrides="core.FileMode=false, core.autocrlf=false, core.symlinks=true, a.b=c")
    assert gitconfig.env_overrides(config, native=True, git_version=(2, 53, 0)) == {
        "GIT_CONFIG_COUNT": "2",
        "GIT_CONFIG_KEY_0": "a.b",
        "GIT_CONFIG_VALUE_0": "c",
        "GIT_CONFIG_KEY_1": "worktree.useRelativePaths",
        "GIT_CONFIG_VALUE_1": "true",
    }
    assert gitconfig.env_overrides(_config(tmp_path), native=True) == {}


def test_env_overrides_empty(tmp_path: Path) -> None:
    assert gitconfig.env_overrides(_config(tmp_path, env_overrides="")) == {}


def test_env_overrides_allows_empty_values(tmp_path: Path) -> None:
    env = gitconfig.env_overrides(_config(tmp_path, env_overrides="credential.helper="))
    assert env["GIT_CONFIG_KEY_0"] == "credential.helper" and env["GIT_CONFIG_VALUE_0"] == ""


@pytest.mark.parametrize("item", ["core.filemode", "=x", "nodot=1", ".lead=1", "trail.=1", "core .x=1"])
def test_env_overrides_rejects_bad_entries(tmp_path: Path, item: str) -> None:
    with pytest.raises(UsageError, match=r"invalid git\.env_overrides entry"):
        gitconfig.env_overrides(_config(tmp_path, env_overrides=item))


# --- host home and per-file include bases (Wine's "~" is the Windows user directory) -----------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("~/.gitignore", "Z:/home/ada/.gitignore"),
        ("~", "Z:/home/ada"),
        (":(optional)~/x", ":(optional)Z:/home/ada/x"),
        ("work.inc", "Z:/home/ada/.config/git/work.inc"),
        ("", ""),
        ("~other/x", "~other/x"),
    ],
)
def test_translate_path_with_home_and_base(value: str, expected: str, pathmap: PathMap) -> None:
    home = Path("/home/ada")
    assert gitconfig._translate_path(value, pathmap, home, home / ".config" / "git") == expected


@pytest.mark.parametrize(
    ("condition", "expected"),
    [
        ("gitdir:~/work/", "gitdir:Z:/home/ada/work/"),
        ("gitdir/i:~/Work", "gitdir/i:Z:/home/ada/Work"),
        ("gitdir:work/", "gitdir:work/"),
        ("onbranch:~/x", "onbranch:~/x"),
        ("gitdir", "gitdir"),
    ],
)
def test_translate_gitdir_with_home(condition: str, expected: str, pathmap: PathMap) -> None:
    assert gitconfig._translate_gitdir(condition, pathmap, Path("/home/ada")) == expected


def test_translate_with_home_and_base(pathmap: PathMap) -> None:
    text = (
        '[includeIf "gitdir:~/work/"]\n'
        "\tpath = work.inc\n"
        "[core]\n"
        "\texcludesFile = ~/.gitignore\n"
        "\thooksPath = hooks\n"
    )
    out, notes = gitconfig.translate(text, pathmap, home=Path("/home/ada"), base=Path("/home/ada"))
    assert out.splitlines() == [
        '[includeIf "gitdir:Z:/home/ada/work/"]',
        "\tpath = Z:/home/ada/work.inc",
        "[core]",
        "\texcludesFile = Z:/home/ada/.gitignore",
        # Only include paths are relative to the file; other relative paths are kept.
        "\thooksPath = hooks",
    ]
    assert notes == []


def test_sync_translates_each_host_file_against_the_host_home(
    paths: Paths, pathmap: PathMap, tmp_path: Path
) -> None:
    home = tmp_path / "host-home"
    (home / ".config" / "git").mkdir(parents=True)
    # No trailing newline and no section at the end: the next file must not inherit it.
    (home / ".config" / "git" / "config").write_text("[include]\n\tpath = extra.inc", encoding="utf-8")
    (home / ".gitconfig").write_text(
        '[includeIf "gitdir:~/work/"]\n\tpath = .gitconfig-work\n[core]\n\texcludesFile = ~/.gitignore\n',
        encoding="utf-8",
    )
    root = tmp_path / "root"
    (root / "home").mkdir(parents=True)
    gitconfig.sync(paths, USER, home, pathmap, host_root=root)
    text = gitconfig.overlay_path(paths, USER).read_text(encoding="utf-8")
    z_home = "Z:" + str(home)
    assert text == (
        gitconfig.HEADER
        + "# from ~/.config/git/config\n"
        + f"[include]\n\tpath = {z_home}/.config/git/extra.inc\n"
        + "# from ~/.gitconfig\n"
        + f'[includeIf "gitdir:{z_home}/work/"]\n\tpath = {z_home}/.gitconfig-work\n'
        + f"[core]\n\texcludesFile = {z_home}/.gitignore\n"
        + gitconfig.MANAGED_HEADER
        + '[url "file:///Z:/home/"]\n\tinsteadOf = file:///home/\n'
        + '[url "Z:/home/"]\n\tinsteadOf = /home/\n'
        + "[credential]\n\tcredentialStore = dpapi\n"
    )


# --- managed block, git version -----------------------------------------------------------------


def test_rewrite_tops_lists_existing_directories(tmp_path: Path) -> None:
    for name in ("mnt", "home", "nothere-file"):
        (tmp_path / name).mkdir()
    (tmp_path / "srv").write_text("", encoding="utf-8")
    assert gitconfig.rewrite_tops(tmp_path) == ["home", "mnt"]


def test_managed_text_skips_unreachable_tops(tmp_path: Path) -> None:
    only_home = PathMap.with_drives({"c": tmp_path / "c", "h": "/home"})
    text = gitconfig.managed_text(only_home, ["home", "mnt"])
    assert '[url "file:///H:/"]\n\tinsteadOf = file:///home/\n[url "H:/"]\n\tinsteadOf = /home/\n' in text
    assert "mnt" not in text
    assert text.endswith("[credential]\n\tcredentialStore = dpapi\n")


def _git_runner(stdout: str = "git version 2.53.0\n", code: int = 0) -> RecordingRunner:
    return RecordingRunner({"git": Completed([], code, stdout, "")})


def test_host_git_version_probes_and_caches(tmp_path: Path) -> None:
    git = tmp_path / "bin" / "git"
    git.parent.mkdir()
    git.write_text("#!/bin/sh\n", encoding="utf-8")
    runner = _git_runner()
    runner.which_map["git"] = str(git)
    cache = tmp_path / "cache"
    assert gitconfig.host_git_version(runner, {"PATH": "/x"}, cache) == (2, 53, 0)
    assert gitconfig.host_git_version(runner, {"PATH": "/x"}, cache) == (2, 53, 0)
    assert len(runner.calls) == 1
    git.write_text("#!/bin/sh\n# changed\n", encoding="utf-8")
    runner.responses = {"git": Completed([], 0, "git version 2.34\n", "")}
    assert gitconfig.host_git_version(runner, {}, cache) == (2, 34, 0)


def test_host_git_version_without_git(tmp_path: Path) -> None:
    runner = _git_runner()
    runner.which_map["git"] = None
    assert gitconfig.host_git_version(runner, {}, tmp_path) is None
    runner.which_map["git"] = str(tmp_path / "vanished")
    assert gitconfig.host_git_version(runner, {}, tmp_path) is None


@pytest.mark.parametrize("response", [Completed([], 1, "", "boom"), Completed([], 0, "nonsense", "")])
def test_host_git_version_unusable_answers(tmp_path: Path, response: Completed) -> None:
    git = tmp_path / "git"
    git.write_text("", encoding="utf-8")
    runner = RecordingRunner({"git": response}, which_map={"git": str(git)})
    assert gitconfig.host_git_version(runner, {}, tmp_path / "c") is None
    assert not (tmp_path / "c").exists()


def test_host_git_version_timeout_and_unwritable_cache(tmp_path: Path) -> None:
    git = tmp_path / "git"
    git.write_text("", encoding="utf-8")

    def boom(argv: list[str]) -> Completed:
        raise ForkLinuxError("timed out")

    runner = RecordingRunner({"git": boom}, which_map={"git": str(git)})
    assert gitconfig.host_git_version(runner, {}, tmp_path) is None
    blocker = tmp_path / "blocker"
    blocker.write_text("", encoding="utf-8")
    ok = RecordingRunner({"git": "git version 2.48.1\n"}, which_map={"git": str(git)})
    assert gitconfig.host_git_version(ok, {}, blocker / "sub") == (2, 48, 1)


def test_host_git_version_ignores_a_corrupt_cache(tmp_path: Path) -> None:
    git = tmp_path / "git"
    git.write_text("", encoding="utf-8")
    (tmp_path / gitconfig.GIT_VERSION_CACHE).write_text("[1, 2]", encoding="utf-8")
    runner = RecordingRunner({"git": "git version 2.50.0\n"}, which_map={"git": str(git)})
    assert gitconfig.host_git_version(runner, {}, tmp_path) == (2, 50, 0)
    (tmp_path / gitconfig.GIT_VERSION_CACHE).write_text("{not json", encoding="utf-8")
    assert gitconfig.host_git_version(runner, {}, tmp_path) == (2, 50, 0)


@pytest.mark.parametrize(
    ("version", "added"),
    [((2, 48, 0), True), ((2, 53, 1), True), ((3, 0, 0), True), ((2, 47, 9), False), (None, False)],
)
def test_env_overrides_relative_worktrees(tmp_path: Path, version: tuple[int, int, int] | None, added: bool) -> None:
    env = gitconfig.env_overrides(_config(tmp_path, env_overrides="core.filemode=false"), git_version=version)
    pairs = {env[f"GIT_CONFIG_KEY_{n}"]: env[f"GIT_CONFIG_VALUE_{n}"] for n in range(int(env["GIT_CONFIG_COUNT"]))}
    assert ("worktree.useRelativePaths" in pairs) is added


def test_env_overrides_keeps_the_users_worktree_choice(tmp_path: Path) -> None:
    config = _config(tmp_path, env_overrides="worktree.useRelativePaths=false")
    env = gitconfig.env_overrides(config, git_version=(2, 50, 0))
    assert env == {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "worktree.useRelativePaths", "GIT_CONFIG_VALUE_0": "false"}
