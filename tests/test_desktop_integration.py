"""Tests for fork_linux.desktop_integration: menu entry, icons, file-manager actions, CLI aliases."""

from __future__ import annotations

import ast
import json
import os
import shutil
import stat
import subprocess
import sys
import types
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import pytest
from fixtures import pe_builder as pb

from fork_linux import APP_ID, APP_NAME, credits, resources, sandbox
from fork_linux import desktop_integration as di
from fork_linux.errors import ForkLinuxError, UsageError
from fork_linux.paths import Paths
from fork_linux.procrun import Completed, RecordingRunner

LAUNCHER = "/opt/fork-linux/bin/fork-linux"


@pytest.fixture(autouse=True)
def _no_flatpak(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sandbox, "FLATPAK_INFO", tmp_path / "no-flatpak-info")


@pytest.fixture
def env(xdg: Path, tmp_path: Path) -> dict[str, str]:
    """The test environment with an empty system data dir and an empty PATH."""
    system = tmp_path / "system-share"
    system.mkdir()
    environ = dict(os.environ)
    environ["XDG_DATA_DIRS"] = str(system)
    environ["PATH"] = str(tmp_path / "empty-path")
    return environ


@pytest.fixture
def paths(env: dict[str, str]) -> Paths:
    return Paths.from_env(env)


@pytest.fixture
def runner() -> RecordingRunner:
    """No desktop tools installed unless a test says otherwise."""
    return RecordingRunner(which_map={"update-desktop-database": None, "gtk-update-icon-cache": None})


def _dirs(env: dict[str, str]) -> di._Dirs:
    return di._Dirs.from_env(env)


def _registry(paths: Paths) -> dict[str, dict[str, Any]]:
    data = json.loads(paths.integrations_file.read_text(encoding="utf-8"))
    assert data["schema"] == di.REGISTRY_SCHEMA
    return {entry["path"]: entry for entry in data["entries"]}


def _install(paths: Paths, env: dict[str, str], runner: RecordingRunner, **kwargs: Any) -> list[Path]:
    kwargs.setdefault("exec_cmd", [LAUNCHER])
    return di.install(paths, env, runner=runner, **kwargs)


# --- directories and the launcher ----------------------------------------------------------


def test_dirs_from_env(tmp_path: Path) -> None:
    home = tmp_path / "h"
    env = {
        "HOME": str(home),
        "XDG_DATA_HOME": "relative/ignored",
        "XDG_CONFIG_HOME": str(tmp_path / "cfg"),
        "XDG_BIN_HOME": str(tmp_path / "bin"),
        "XDG_DATA_DIRS": f"relative:{tmp_path / 'sys'}:",
    }
    dirs = di._Dirs.from_env(env)
    assert dirs.data == home / ".local" / "share"
    assert dirs.config == tmp_path / "cfg"
    assert dirs.bin == tmp_path / "bin"
    assert dirs.data_dirs == (tmp_path / "sys",)
    assert dirs.menu_file == home / ".local" / "share" / "applications" / f"{APP_ID}.desktop"
    assert dirs.thunar_uca == tmp_path / "cfg" / "Thunar" / "uca.xml"
    assert dirs.kind_path(di.NAUTILUS_SCRIPT).name == "Open in Fork"


def test_dirs_defaults(xdg: Path) -> None:
    dirs = di._Dirs.from_env({})
    assert dirs.data == xdg / ".local" / "share"
    assert dirs.bin == xdg / ".local" / "bin"
    assert dirs.data_dirs == (Path("/usr/local/share"), Path("/usr/share"))


def test_launcher_command_appimage_and_flatpak() -> None:
    assert di.launcher_command({di.APPIMAGE_ENV: "/apps/Fork.AppImage", "FLATPAK_ID": "x"}) == ["/apps/Fork.AppImage"]
    assert di.launcher_command({"FLATPAK_ID": APP_ID}) == ["flatpak", "run", APP_ID]


def test_launcher_command_path_lookup(tmp_path: Path) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    launcher = bindir / "fork-linux"
    launcher.write_text("#!/bin/sh\n", encoding="utf-8")
    launcher.chmod(0o755)
    assert di.launcher_command({"PATH": str(bindir)}) == [str(launcher)]


def test_launcher_command_falls_back_to_the_installation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "usr"
    (root / "bin").mkdir(parents=True)
    monkeypatch.setattr(resources, "install_root", lambda: root)
    monkeypatch.setattr(resources, "is_source_tree", lambda: False)
    (root / "bin" / "fork-linux.in").write_text("#!@PYTHON@ -I\n", encoding="utf-8")
    assert di.launcher_command({"PATH": str(tmp_path / "none")}) == [str(root / "bin" / "fork-linux")]


def test_launcher_command_in_a_checkout_runs_the_template(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # E2E: from a checkout the menu entry pointed at <repo>/bin/fork-linux, which is never rendered there.
    root = tmp_path / "repo"
    (root / "bin").mkdir(parents=True)
    monkeypatch.setattr(resources, "install_root", lambda: root)
    monkeypatch.setattr(resources, "is_source_tree", lambda: True)
    none = {"PATH": str(tmp_path / "none")}
    assert di.launcher_command(none) == [str(root / "bin" / "fork-linux")], "no template: the plain path"
    template = root / "bin" / "fork-linux.in"
    template.write_text("#!@PYTHON@ -I\n", encoding="utf-8")
    assert di.launcher_command(none) == [sys.executable, "-I", str(template)]
    (root / "bin" / "fork-linux").write_text("#!/bin/sh\n", encoding="utf-8")
    assert di.launcher_command(none) == [str(root / "bin" / "fork-linux")], "a rendered launcher wins"


def test_launcher_command_defaults_to_os_environ(xdg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(di.APPIMAGE_ENV, "/a/b.AppImage")
    assert di.launcher_command() == ["/a/b.AppImage"]


# --- Exec quoting and templates ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("cmd", "expected"),
    [
        (["/usr/bin/fork-linux"], "/usr/bin/fork-linux"),
        (["/opt/My Apps/fork-linux", "run"], '"/opt/My Apps/fork-linux" run'),
        (["a$b`c\"d"], '"a\\\\$b\\\\`c\\\\"d"'),
        (["back\\slash"], '"back\\\\\\\\slash"'),
        (["100%"], "100%%"),
        (["50% off"], '"50%% off"'),
        ([""], '""'),
        (["tab\there"], '"tab\there"'),
        (["semi;colon", "~home"], '"semi;colon" "~home"'),
    ],
)
def test_desktop_exec(cmd: list[str], expected: str) -> None:
    assert di.desktop_exec(cmd) == expected


def test_desktop_exec_rejects_bad_commands() -> None:
    with pytest.raises(UsageError):
        di.desktop_exec([])
    with pytest.raises(UsageError, match="control character"):
        di.desktop_exec(["new\nline"])


def _exec_split(value: str) -> list[str]:
    """Undo Desktop Entry Exec encoding: key-file unescaping, ``%%``, then quoted-argument rules."""
    text = value.replace("\\\\", "\x00").replace("\x00", "\\").replace("%%", "%")
    args: list[str] = []
    current: list[str] | None = None
    quoted = False
    index = 0
    while index < len(text):
        char = text[index]
        index += 1
        if quoted and char == "\\" and text[index] in '"`$\\':
            current = (current or []) + [text[index]]
            index += 1
        elif char == '"':
            quoted = not quoted
            current = current or []
        elif char == " " and not quoted:
            if current is not None:
                args.append("".join(current))
            current = None
        else:
            current = (current or []) + [char]
    if current is not None:
        args.append("".join(current))
    return args


def test_desktop_exec_round_trips() -> None:
    cmd = ["/opt/a b/fork-linux", 'q"uote', "d$ollar", "back`tick", "bs\\x", "100%", "", "plain"]
    assert _exec_split(di.desktop_exec(cmd)) == cmd


def test_render_template_errors(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for bad in ("", "a/b", ".hidden"):
        with pytest.raises(UsageError):
            di.render_template(bad, {})
    with pytest.raises(ForkLinuxError, match="cannot read the template"):
        di.render_template("no-such-template", {})
    with pytest.raises(ForkLinuxError, match="@COMMENT@, @EXEC@"):
        di.render_template(f"{APP_ID}.desktop.in", {"APP_ID": "x", "APP_NAME": "y", "TRYEXEC": "z"})


def test_render_template_substitutes_once() -> None:
    mapping = {"APP_ID": "@EXEC@", "APP_NAME": "n", "LABEL": "l", "TOOLTIP": "t", "EXEC": "e"}
    text = di.render_template("nemo_action", mapping)
    assert "Icon-Name=@EXEC@" in text
    assert "Exec=e open --from-file-manager %F" in text


# --- registry --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "content",
    ["not json", "[]", '{"entries": 3}', '{"entries": [1, {"path": 2, "kind": "x"}, {"path": "p"}]}'],
)
def test_registry_ignores_bad_content(tmp_path: Path, content: str) -> None:
    path = tmp_path / "integrations.json"
    path.write_text(content, encoding="utf-8")
    assert di._Registry.load(path).entries == {}


def test_registry_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "sub" / "integrations.json"
    registry = di._Registry.load(path)
    assert registry.entries == {}
    registry.save()
    assert not path.exists()
    registry.record(tmp_path / "b", "menu", sha256="1")
    registry.record(tmp_path / "a", "icon", sha256="2")
    registry.save()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    loaded = di._Registry.load(path)
    assert [e["path"] for e in json.loads(path.read_text(encoding="utf-8"))["entries"]] == [
        str(tmp_path / "a"),
        str(tmp_path / "b"),
    ]
    loaded.forget(tmp_path / "a")
    loaded.forget(tmp_path / "missing")
    loaded.entries.clear()
    loaded.save()
    assert not path.exists()


def test_file_state(tmp_path: Path) -> None:
    target = tmp_path / "f"
    assert di._file_state(target, "x") == "missing"
    target.write_bytes(b"data")
    assert di._file_state(target, di._sha256(b"data")) == "ok"
    assert di._file_state(target, "other") == "modified"
    link = tmp_path / "l"
    link.symlink_to(target)
    assert di._file_state(link, di._sha256(b"data")) == "modified"
    assert di._file_state(tmp_path, "x") == "modified"


# --- install ------------------------------------------------------------------------------------------


def test_install_everything(paths: Paths, env: dict[str, str], runner: RecordingRunner, tmp_path: Path) -> None:
    exe = tmp_path / "Fork.exe"
    exe.write_bytes(pb.build_pe())
    dirs = _dirs(env)
    installed = _install(paths, env, runner, fork_exe=exe)
    icons = [dirs.hicolor / f"{n}x{n}" / "apps" / f"{APP_ID}.png" for n in (16, 32, 256)]
    expected = [
        dirs.menu_file,
        *icons,
        dirs.kind_path(di.NAUTILUS),
        dirs.kind_path(di.NAUTILUS_SCRIPT),
        dirs.kind_path(di.NEMO),
        dirs.kind_path(di.FMA),
        dirs.kind_path(di.DOLPHIN),
        dirs.thunar_uca,
    ]
    assert installed == expected
    registry = _registry(paths)
    assert {e["kind"] for e in registry.values()} == {
        di.MENU, di.ICON, di.NAUTILUS, di.NAUTILUS_SCRIPT, di.NEMO, di.FMA, di.DOLPHIN, di.THUNAR,
    }
    for path in expected[:-1]:
        assert registry[str(path)]["sha256"] == di.fsutil.sha256_file(path)
    assert registry[str(dirs.thunar_uca)] == {
        "path": str(dirs.thunar_uca), "kind": di.THUNAR, "unique_id": di.UNIQUE_ID, "created": True,
    }
    assert stat.S_IMODE(dirs.kind_path(di.DOLPHIN).stat().st_mode) == 0o755
    assert stat.S_IMODE(dirs.kind_path(di.NAUTILUS_SCRIPT).stat().st_mode) == 0o755
    assert stat.S_IMODE(dirs.menu_file.stat().st_mode) == 0o644
    # Installing again is a no-op that reports the same files.
    assert _install(paths, env, runner, fork_exe=exe) == expected
    assert sum(1 for child in ET.parse(dirs.thunar_uca).getroot() if di._ours(child)) == 1


def test_menu_entry_content(paths: Paths, env: dict[str, str], runner: RecordingRunner) -> None:
    _install(paths, env, runner, exec_cmd=["/opt/My Apps/fork-linux"], file_managers="none")
    text = _dirs(env).menu_file.read_text(encoding="utf-8")
    lines = text.splitlines()
    assert lines[0] == "[Desktop Entry]"
    assert f"Name={APP_NAME}" in lines
    assert f"Comment={credits.render_desktop_comment()}" in lines
    assert 'Exec="/opt/My Apps/fork-linux" run %F' in lines
    assert "TryExec=/opt/My Apps/fork-linux" in lines
    assert f"Icon={APP_ID}" in lines
    assert "StartupWMClass=fork.exe" in lines
    assert "Categories=Development;RevisionControl;" in lines
    assert "Actions=doctor;about;" in lines
    assert 'Exec="/opt/My Apps/fork-linux" doctor --gui' in lines
    assert 'Exec="/opt/My Apps/fork-linux" about --gui' in lines
    assert "MimeType" not in text
    assert "@" not in text


def test_menu_entry_validates(paths: Paths, env: dict[str, str], runner: RecordingRunner) -> None:
    validator = shutil.which("desktop-file-validate")
    if validator is None:
        pytest.skip("desktop-file-validate is not installed")
    _install(paths, env, runner, exec_cmd=["/opt/My Apps/fork-linux"], file_managers="none")
    result = subprocess.run([validator, str(_dirs(env).menu_file)], capture_output=True, text=True, check=False)
    assert result.returncode == 0 and result.stdout == "", result.stdout + result.stderr


def test_menu_entry_without_absolute_command(paths: Paths, env: dict[str, str], runner: RecordingRunner) -> None:
    env["FLATPAK_ID"] = APP_ID
    di.install(paths, env, runner=runner, file_managers="none", system_desktop_present=False)
    text = _dirs(env).menu_file.read_text(encoding="utf-8")
    assert f"Exec=flatpak run {APP_ID} run %F" in text
    assert "TryExec" not in text


def test_menu_skipped_when_a_package_installed_one(
    paths: Paths, env: dict[str, str], runner: RecordingRunner, tmp_path: Path
) -> None:
    dirs = _dirs(env)
    assert _install(paths, env, runner, file_managers="none") == [dirs.menu_file]
    system = tmp_path / "system-share" / "applications"
    system.mkdir()
    (system / f"{APP_ID}.desktop").write_text("[Desktop Entry]\n", encoding="utf-8")
    # Our earlier personal copy is removed so it does not shadow the packaged one.
    assert _install(paths, env, runner, file_managers="none") == []
    assert not dirs.menu_file.exists()
    assert not paths.integrations_file.exists()
    assert _install(paths, env, runner, file_managers="none", system_desktop_present=True) == []
    assert _install(paths, env, runner, file_managers="none", system_desktop_present=False) == [dirs.menu_file]


def test_own_menu_file_in_data_dirs_is_not_a_system_copy(
    paths: Paths, env: dict[str, str], runner: RecordingRunner
) -> None:
    dirs = _dirs(env)
    env["XDG_DATA_DIRS"] = str(dirs.data)
    _install(paths, env, runner, file_managers="none")
    assert _install(paths, env, runner, file_managers="none") == [dirs.menu_file]
    assert di._system_desktop(_dirs(env)) is None


def test_install_leaves_foreign_files_alone(
    paths: Paths, env: dict[str, str], runner: RecordingRunner, tmp_path: Path
) -> None:
    dirs = _dirs(env)
    dirs.applications.mkdir(parents=True)
    dirs.menu_file.write_text("[Desktop Entry]\nName=Mine\n", encoding="utf-8")
    nemo = dirs.kind_path(di.NEMO)
    nemo.parent.mkdir(parents=True)
    nemo.symlink_to(tmp_path)
    installed = _install(paths, env, runner, file_managers="nemo,dolphin")
    assert installed == [dirs.kind_path(di.DOLPHIN)]
    assert dirs.menu_file.read_text(encoding="utf-8") == "[Desktop Entry]\nName=Mine\n"
    assert nemo.is_symlink()


def test_install_updates_unmodified_files_and_respects_edits(
    paths: Paths, env: dict[str, str], runner: RecordingRunner
) -> None:
    dirs = _dirs(env)
    _install(paths, env, runner, file_managers="nemo")
    nemo = dirs.kind_path(di.NEMO)
    # A new launcher path rewrites our unmodified files.
    _install(paths, env, runner, file_managers="nemo", exec_cmd=["/new/fork-linux"])
    assert "Exec=/new/fork-linux open" in nemo.read_text(encoding="utf-8")
    # A file the user edited is kept.
    nemo.write_text("edited\n", encoding="utf-8")
    assert _install(paths, env, runner, file_managers="nemo") == [dirs.menu_file]
    assert nemo.read_text(encoding="utf-8") == "edited\n"


def test_identical_file_gets_its_mode_fixed(paths: Paths, env: dict[str, str], runner: RecordingRunner) -> None:
    _install(paths, env, runner, menu=False, file_managers="dolphin")
    path = _dirs(env).kind_path(di.DOLPHIN)
    os.chmod(path, 0o600)
    paths.integrations_file.unlink()
    assert _install(paths, env, runner, menu=False, file_managers="dolphin") == [path]
    assert stat.S_IMODE(path.stat().st_mode) == 0o755
    assert str(path) in _registry(paths)


def test_file_manager_templates(paths: Paths, env: dict[str, str], runner: RecordingRunner) -> None:
    cmd = ["/opt/fl dir/fork-linux"]
    _install(paths, env, runner, menu=False, exec_cmd=cmd)
    dirs = _dirs(env)
    nemo = dirs.kind_path(di.NEMO).read_text(encoding="utf-8")
    assert "Name=Open in Fork" in nemo
    assert 'Exec="/opt/fl dir/fork-linux" open --from-file-manager %F' in nemo
    fma = dirs.kind_path(di.FMA).read_text(encoding="utf-8")
    assert "Type=Action" in fma and "Name=Open in Fork" in fma
    assert 'Exec="/opt/fl dir/fork-linux" open --from-file-manager %f' in fma
    dolphin = dirs.kind_path(di.DOLPHIN).read_text(encoding="utf-8")
    assert "X-KDE-ServiceTypes=KonqPopupMenu/Plugin" in dolphin and "ServiceTypes=KonqPopupMenu/Plugin" in dolphin
    assert "MimeType=inode/directory;all/allfiles;" in dolphin
    script = dirs.kind_path(di.NAUTILUS_SCRIPT).read_text(encoding="utf-8")
    assert script.startswith("#!/bin/sh\n")
    assert "exec '/opt/fl dir/fork-linux' open --from-file-manager \"$1\"" in script
    for path in (dirs.kind_path(kind) for kind in (di.NEMO, di.FMA, di.DOLPHIN, di.NAUTILUS_SCRIPT, di.NAUTILUS)):
        assert "@" not in path.read_text(encoding="utf-8").replace("support@", "")


def test_nautilus_script_runs(paths: Paths, env: dict[str, str], runner: RecordingRunner, tmp_path: Path) -> None:
    log = tmp_path / "args.txt"
    fake = tmp_path / "fake fork-linux"
    fake.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > "{log}"\n', encoding="utf-8")
    fake.chmod(0o755)
    _install(paths, env, runner, menu=False, file_managers="nautilus", exec_cmd=[str(fake)])
    script = _dirs(env).kind_path(di.NAUTILUS_SCRIPT)
    subprocess.run([str(script), "my repo", "other"], cwd=tmp_path, check=True)
    assert log.read_text(encoding="utf-8").splitlines() == ["open", "--from-file-manager", "my repo"]
    subprocess.run([str(script)], cwd=tmp_path, check=True)
    assert log.read_text(encoding="utf-8").splitlines() == ["open", "--from-file-manager", str(tmp_path)]


class _FakeLocation:
    def __init__(self, path: str | None) -> None:
        self._path = path

    def get_path(self) -> str | None:
        return self._path


class _FakeFile:
    def __init__(self, path: str | None, *, location: bool = True) -> None:
        self._location = _FakeLocation(path) if location else None

    def get_location(self) -> _FakeLocation | None:
        return self._location


class _FakeMenuItem:
    def __init__(self, **props: str) -> None:
        self.props = props
        self.handlers: list[tuple[Any, ...]] = []

    def connect(self, signal: str, handler: Any, *args: Any) -> None:
        self.handlers.append((signal, handler, *args))


def _load_nautilus_extension(source: str, monkeypatch: pytest.MonkeyPatch, *, has_v4: bool) -> types.ModuleType:
    """Execute the generated extension against fake ``gi`` modules."""
    versions: list[str] = []

    def require_version(namespace: str, version: str) -> None:
        versions.append(version)
        if version == "4.0" and not has_v4:
            raise ValueError("Namespace Nautilus not available for version 4.0")

    gi = types.ModuleType("gi")
    gi.require_version = require_version
    repository = types.ModuleType("gi.repository")

    class GObjectBase:
        pass

    class MenuProvider:
        pass

    repository.GObject = types.SimpleNamespace(GObject=GObjectBase)
    repository.Nautilus = types.SimpleNamespace(MenuProvider=MenuProvider, MenuItem=_FakeMenuItem)
    gi.repository = repository
    monkeypatch.setitem(sys.modules, "gi", gi)
    monkeypatch.setitem(sys.modules, "gi.repository", repository)
    module = types.ModuleType("fork_linux_nautilus")
    exec(compile(source, "fork_linux_nautilus.py", "exec"), module.__dict__)
    module.versions = versions
    return module


@pytest.mark.parametrize("has_v4", [True, False])
def test_nautilus_extension(
    paths: Paths, env: dict[str, str], runner: RecordingRunner, monkeypatch: pytest.MonkeyPatch, has_v4: bool
) -> None:
    cmd = ["/opt/x/fork-linux", "--prefix", "/p q"]
    _install(paths, env, runner, menu=False, file_managers=["nautilus"], exec_cmd=cmd)
    source = _dirs(env).kind_path(di.NAUTILUS).read_text(encoding="utf-8")
    ast.parse(source)
    module = _load_nautilus_extension(source, monkeypatch, has_v4=has_v4)
    assert module.versions == (["4.0"] if has_v4 else ["4.0", "3.0"])
    assert cmd == module.COMMAND
    assert module.LABEL == "Open in Fork"
    assert module.ICON == APP_ID
    provider = module.ForkLinuxMenuProvider()
    window = object()
    # Nautilus 4: get_file_items(files); Nautilus 3: get_file_items(window, files).
    items = provider.get_file_items([_FakeFile("/home/u/repo")])
    assert [item.props["name"] for item in items] == ["ForkLinux::OpenSelection"]
    assert items[0].props["label"] == "Open in Fork"
    assert provider.get_file_items(window, [_FakeFile("/home/u/repo")])[0].handlers[0][2] == "/home/u/repo"
    assert provider.get_file_items([_FakeFile("/a"), _FakeFile("/b")]) == []
    assert provider.get_file_items([_FakeFile(None)]) == []
    assert provider.get_file_items([_FakeFile(None, location=False)]) == []
    background = provider.get_background_items(window, _FakeFile("/home/u/repo"))
    assert [item.props["name"] for item in background] == ["ForkLinux::OpenFolder"]
    assert provider.get_background_items(_FakeFile(None)) == []
    calls: list[tuple[list[str], dict[str, Any]]] = []
    monkeypatch.setattr(module.subprocess, "Popen", lambda argv, **kw: calls.append((argv, kw)))
    signal, handler, path = background[0].handlers[0]
    assert signal == "activate"
    handler(background[0], path)
    assert calls[0][0] == [*cmd, "open", "--from-file-manager", "/home/u/repo"]
    assert calls[0][1]["start_new_session"] is True


def test_thunar_merges_into_an_existing_file(paths: Paths, env: dict[str, str], runner: RecordingRunner) -> None:
    dirs = _dirs(env)
    dirs.thunar_uca.parent.mkdir(parents=True)
    original = (
        '<?xml version="1.0" encoding="UTF-8"?>\n<actions>\n<!-- mine -->\n'
        "<action><name>Terminal</name><unique-id>123-1</unique-id><command>exo-open %f</command></action>\n"
        f"<action><name>old</name><unique-id>{di.UNIQUE_ID}</unique-id><command>old</command></action>\n"
        "</actions>\n"
    )
    dirs.thunar_uca.write_text(original, encoding="utf-8")
    os.chmod(dirs.thunar_uca, 0o600)
    cmd = ["/opt/a&b/fork-linux"]
    assert _install(paths, env, runner, menu=False, file_managers="thunar", exec_cmd=cmd) == [dirs.thunar_uca]
    backup = dirs.thunar_uca.with_name(di.THUNAR_BACKUP)
    assert backup.read_text(encoding="utf-8") == original
    assert stat.S_IMODE(dirs.thunar_uca.stat().st_mode) == 0o600
    text = dirs.thunar_uca.read_text(encoding="utf-8")
    assert text.startswith('<?xml version="1.0" encoding="UTF-8"?>\n<actions>')
    assert "<!-- mine -->" in text
    root = ET.fromstring(text)
    actions = root.findall("action")
    assert [a.findtext("unique-id") for a in actions] == ["123-1", di.UNIQUE_ID]
    assert actions[1].findtext("command") == "'/opt/a&b/fork-linux' open --from-file-manager %f"
    assert actions[1].findtext("name") == "Open in Fork"
    assert actions[1].find("directories") is not None
    assert _registry(paths)[str(dirs.thunar_uca)]["created"] is False
    # Removing takes out only our action and keeps the file.
    assert di.remove(paths, env, runner=runner) == [dirs.thunar_uca]
    root = ET.parse(dirs.thunar_uca).getroot()
    assert [a.findtext("unique-id") for a in root.findall("action")] == ["123-1"]


@pytest.mark.parametrize("content", ["<actions><unclosed></actions>", "<other/>"])
def test_thunar_leaves_unusable_files_alone(
    paths: Paths, env: dict[str, str], runner: RecordingRunner, content: str
) -> None:
    dirs = _dirs(env)
    dirs.thunar_uca.parent.mkdir(parents=True)
    dirs.thunar_uca.write_text(content, encoding="utf-8")
    assert _install(paths, env, runner, menu=False, file_managers="thunar") == []
    assert dirs.thunar_uca.read_text(encoding="utf-8") == content
    assert not paths.integrations_file.exists()


def test_thunar_created_file_is_removed_with_its_backup(
    paths: Paths, env: dict[str, str], runner: RecordingRunner
) -> None:
    dirs = _dirs(env)
    _install(paths, env, runner, menu=False, file_managers="thunar")
    _install(paths, env, runner, menu=False, file_managers="thunar")
    backup = dirs.thunar_uca.with_name(di.THUNAR_BACKUP)
    assert backup.exists()
    assert _registry(paths)[str(dirs.thunar_uca)]["created"] is True
    assert di.remove(paths, env, runner=runner) == [dirs.thunar_uca]
    assert not dirs.thunar_uca.exists() and not backup.exists()


def test_remove_thunar_edge_cases(tmp_path: Path) -> None:
    uca = tmp_path / "uca.xml"
    assert not di._remove_thunar(uca, {"created": True})
    uca.write_text("<broken", encoding="utf-8")
    assert not di._remove_thunar(uca, {"created": True})
    uca.write_text("<actions><action><unique-id>x</unique-id></action></actions>", encoding="utf-8")
    assert not di._remove_thunar(uca, {"created": True})
    uca.write_text(f"<actions><action><unique-id>{di.UNIQUE_ID}</unique-id></action></actions>", encoding="utf-8")
    assert di._remove_thunar(uca, {"created": True})
    assert not uca.exists()


def test_read_uca_unreadable(tmp_path: Path) -> None:
    assert di._read_uca(tmp_path) is None


# --- CLI aliases ----------------------------------------------------------------------------------------


def _launcher(tmp_path: Path) -> Path:
    target = tmp_path / "install" / "bin" / "fork-linux"
    target.parent.mkdir(parents=True)
    target.write_text("#!/bin/sh\n", encoding="utf-8")
    target.chmod(0o755)
    return target


def test_cli_aliases(paths: Paths, env: dict[str, str], runner: RecordingRunner, tmp_path: Path) -> None:
    target = _launcher(tmp_path)
    dirs = _dirs(env)
    installed = _install(paths, env, runner, menu=False, file_managers="none", cli_alias=True, exec_cmd=[str(target)])
    assert installed == [dirs.bin / "fork", dirs.bin / "fork-linux"]
    for link in installed:
        assert os.readlink(link) == str(target)
    registry = _registry(paths)
    fork = str(dirs.bin / "fork")
    assert registry[fork] == {"path": fork, "kind": di.CLI_ALIAS, "target": str(target)}
    # Found through our own link: point at the real file, not at the link itself.
    again = _install(
        paths, env, runner, menu=False, file_managers="none", cli_alias=True, exec_cmd=[str(dirs.bin / "fork-linux")]
    )
    assert again == installed
    assert os.readlink(dirs.bin / "fork") == str(target)
    # A leftover temporary link from an interrupted run is replaced.
    (dirs.bin / ".fork.fork-linux-tmp").symlink_to("/nowhere")
    other = _launcher(tmp_path / "v2")
    _install(paths, env, runner, menu=False, file_managers="none", cli_alias=True, exec_cmd=[str(other)])
    assert os.readlink(dirs.bin / "fork") == str(other)
    assert not os.path.lexists(dirs.bin / ".fork.fork-linux-tmp")
    removed = di.remove(paths, env, runner=runner)
    assert removed == [dirs.bin / "fork", dirs.bin / "fork-linux"]
    assert not os.path.lexists(dirs.bin / "fork")


def test_cli_aliases_respect_foreign_commands(
    paths: Paths, env: dict[str, str], runner: RecordingRunner, tmp_path: Path
) -> None:
    target = _launcher(tmp_path)
    dirs = _dirs(env)
    dirs.bin.mkdir(parents=True)
    (dirs.bin / "fork").write_text("#!/bin/sh\necho my own fork\n", encoding="utf-8")
    (dirs.bin / "fork-linux").symlink_to("/usr/local/other/fork-linux")
    installed = _install(paths, env, runner, menu=False, file_managers="none", cli_alias=True, exec_cmd=[str(target)])
    assert installed == []
    assert (dirs.bin / "fork").read_text(encoding="utf-8").endswith("my own fork\n")


def test_cli_alias_into_install_root_is_ours(
    paths: Paths, env: dict[str, str], runner: RecordingRunner, tmp_path: Path
) -> None:
    dirs = _dirs(env)
    dirs.bin.mkdir(parents=True)
    old = resources.install_root() / "bin" / "old-launcher"
    (dirs.bin / "fork").symlink_to(old)
    target = _launcher(tmp_path)
    installed = _install(paths, env, runner, menu=False, file_managers="none", cli_alias=True, exec_cmd=[str(target)])
    assert dirs.bin / "fork" in installed
    assert os.readlink(dirs.bin / "fork") == str(target)


def test_cli_alias_already_pointing_at_the_launcher(
    paths: Paths, env: dict[str, str], runner: RecordingRunner, tmp_path: Path
) -> None:
    target = _launcher(tmp_path)
    dirs = _dirs(env)
    dirs.bin.mkdir(parents=True)
    (dirs.bin / "fork").symlink_to(os.path.relpath(target, dirs.bin))
    installed = _install(paths, env, runner, menu=False, file_managers="none", cli_alias=True, exec_cmd=[str(target)])
    assert installed == [dirs.bin / "fork", dirs.bin / "fork-linux"]
    assert os.readlink(dirs.bin / "fork") == str(target)


def test_cli_aliases_need_a_single_absolute_command(
    paths: Paths, env: dict[str, str], runner: RecordingRunner
) -> None:
    for cmd in (["flatpak", "run", APP_ID], ["fork-linux"]):
        assert _install(paths, env, runner, menu=False, file_managers="none", cli_alias=True, exec_cmd=cmd) == []


def test_remove_keeps_links_that_changed(
    paths: Paths, env: dict[str, str], runner: RecordingRunner, tmp_path: Path
) -> None:
    target = _launcher(tmp_path)
    dirs = _dirs(env)
    _install(paths, env, runner, menu=False, file_managers="none", cli_alias=True, exec_cmd=[str(target)])
    (dirs.bin / "fork").unlink()
    (dirs.bin / "fork").symlink_to("/usr/local/bin/something-else")
    registry = json.loads(paths.integrations_file.read_text(encoding="utf-8"))
    registry["entries"].append({"path": str(dirs.bin / "orphan"), "kind": di.CLI_ALIAS})
    paths.integrations_file.write_text(json.dumps(registry), encoding="utf-8")
    assert di.remove(paths, env, runner=runner) == [dirs.bin / "fork-linux"]
    assert os.readlink(dirs.bin / "fork") == "/usr/local/bin/something-else"


# --- file manager selection ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("spec", "kinds"),
    [
        (None, []),
        ("", []),
        ("none", []),
        ("NONE", []),
        ("caja, fma", [di.FMA]),
        (["Thunar", "nemo"], [di.THUNAR, di.NEMO]),
        ("all", [di.NAUTILUS, di.NAUTILUS_SCRIPT, di.NEMO, di.FMA, di.DOLPHIN, di.THUNAR]),
    ],
)
def test_kinds(spec: Any, kinds: list[str]) -> None:
    assert di._kinds(spec) == kinds


def test_kinds_rejects_unknown_names() -> None:
    with pytest.raises(UsageError, match="unknown file manager"):
        di._kinds("nautilus,konqueror")


# --- icons and caches ------------------------------------------------------------------------------------------


def test_icons_skipped_or_failing(paths: Paths, env: dict[str, str], runner: RecordingRunner, tmp_path: Path) -> None:
    bad = tmp_path / "bad.exe"
    bad.write_bytes(b"MZ not really")
    assert _install(paths, env, runner, menu=False, file_managers="none", fork_exe=bad) == []
    assert _install(paths, env, runner, menu=False, file_managers="none", fork_exe=tmp_path / "missing.exe") == []
    assert _install(paths, env, runner, menu=False, file_managers="none", icons=False, fork_exe=bad) == []


def test_refresh_runs_available_tools(paths: Paths, env: dict[str, str], tmp_path: Path) -> None:
    exe = tmp_path / "Fork.exe"
    exe.write_bytes(pb.build_pe())
    dirs = _dirs(env)
    dirs.hicolor.mkdir(parents=True)
    (dirs.hicolor / "icon-theme.cache").write_bytes(b"old")
    runner = RecordingRunner(
        responses={"update-desktop-database": 1, "gtk-update-icon-cache": 0},
        which_map={"update-desktop-database": "/usr/bin/update-desktop-database", "gtk-update-icon-cache": "/x/gtk"},
    )
    di.install(paths, env, runner=runner, exec_cmd=[LAUNCHER], file_managers="none", fork_exe=exe)
    assert runner.argvs == [
        ["update-desktop-database", "-q", str(dirs.applications)],
        ["gtk-update-icon-cache", "-q", "-t", "-f", str(dirs.hicolor)],
    ]
    assert runner.calls[0]["env"]["HOME"] == env["HOME"]


def test_refresh_ignores_timeouts(paths: Paths, env: dict[str, str]) -> None:
    def hang(argv: list[str]) -> Completed:
        raise ForkLinuxError("command timed out")

    runner = RecordingRunner(responses=hang, which_map={"update-desktop-database": "/u"})
    di.install(paths, env, runner=runner, exec_cmd=[LAUNCHER], file_managers="none")
    assert len(runner.calls) == 1


def test_refresh_without_cache_only_touches_hicolor(paths: Paths, env: dict[str, str], tmp_path: Path) -> None:
    exe = tmp_path / "Fork.exe"
    exe.write_bytes(pb.build_pe())
    runner = RecordingRunner(which_map={"update-desktop-database": None, "gtk-update-icon-cache": "/x"})
    di.install(paths, env, runner=runner, exec_cmd=[LAUNCHER], menu=False, file_managers="none", fork_exe=exe)
    assert runner.calls == []
    assert not (_dirs(env).hicolor / "icon-theme.cache").exists()


def test_refresh_with_fake_tools(paths: Paths, env: dict[str, str], fake_bin: Path, tmp_path: Path) -> None:
    env["PATH"] = os.environ["PATH"]
    env["FL_FAKE_LOG"] = str(fake_bin)
    di.install(paths, env, exec_cmd=[LAUNCHER], file_managers="none")
    calls = [json.loads(line) for line in fake_bin.read_text(encoding="utf-8").splitlines()]
    assert calls[0]["argv"] == ["update-desktop-database", "-q", str(_dirs(env).applications)]
    assert (_dirs(env).applications / "mimeinfo.cache").exists()
    di.remove(paths, env)
    assert len(fake_bin.read_text(encoding="utf-8").splitlines()) == 2


def test_refresh_defaults(paths: Paths, monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert di.install(paths, exec_cmd=[LAUNCHER], file_managers="none") == [_dirs(env).menu_file]
    assert di.status(paths)["installed"]
    assert di.remove(paths) == [_dirs(env).menu_file]


# --- remove and status ------------------------------------------------------------------------------------------------


def test_remove_only_removes_unmodified_files(
    paths: Paths, env: dict[str, str], runner: RecordingRunner, tmp_path: Path
) -> None:
    exe = tmp_path / "Fork.exe"
    exe.write_bytes(pb.build_pe())
    dirs = _dirs(env)
    _install(paths, env, runner, fork_exe=exe, file_managers="nemo,fma")
    dirs.kind_path(di.NEMO).write_text("changed\n", encoding="utf-8")
    dirs.kind_path(di.FMA).unlink()
    removed = di.remove(paths, env, runner=runner)
    icons = [dirs.hicolor / f"{n}x{n}" / "apps" / f"{APP_ID}.png" for n in (16, 256, 32)]
    assert sorted(removed) == sorted([dirs.menu_file, *icons])
    assert dirs.kind_path(di.NEMO).read_text(encoding="utf-8") == "changed\n"
    assert not paths.integrations_file.exists()
    assert di.remove(paths, env, runner=runner) == []


def test_remove_never_leaves_the_user_dirs(
    paths: Paths, env: dict[str, str], runner: RecordingRunner, tmp_path: Path
) -> None:
    outside = tmp_path / "elsewhere" / "important.txt"
    outside.parent.mkdir()
    outside.write_text("keep me", encoding="utf-8")
    registry = di._Registry.load(paths.integrations_file)
    registry.record(outside, di.MENU, sha256=di.fsutil.sha256_file(outside))
    registry.record(tmp_path / "elsewhere" / "fork", di.CLI_ALIAS, target="/x")
    registry.save()
    assert di.remove(paths, env, runner=runner) == []
    assert outside.read_text(encoding="utf-8") == "keep me"
    assert not paths.integrations_file.exists()


def test_status(paths: Paths, env: dict[str, str], runner: RecordingRunner, tmp_path: Path) -> None:
    empty = di.status(paths, env)
    assert empty["installed"] is False and empty["entries"] == [] and empty["ok"] is True
    assert empty["system_desktop"] is None
    target = _launcher(tmp_path)
    dirs = _dirs(env)
    _install(paths, env, runner, file_managers="nemo,thunar", cli_alias=True, exec_cmd=[str(target)])
    result = di.status(paths, env)
    assert result["installed"] and result["ok"]
    assert result["launcher"] == di.launcher_command(env)
    assert {e["kind"]: e["state"] for e in result["entries"]} == {
        di.MENU: "ok", di.NEMO: "ok", di.THUNAR: "ok", di.CLI_ALIAS: "ok",
    }
    dirs.kind_path(di.NEMO).write_text("x", encoding="utf-8")
    (dirs.bin / "fork").unlink()
    (dirs.bin / "fork-linux").unlink()
    (dirs.bin / "fork-linux").write_text("mine", encoding="utf-8")
    dirs.thunar_uca.write_text("<actions/>", encoding="utf-8")
    states = {e["path"]: e["state"] for e in di.status(paths, env)["entries"]}
    assert states[str(dirs.kind_path(di.NEMO))] == "modified"
    assert states[str(dirs.bin / "fork")] == "missing"
    assert states[str(dirs.bin / "fork-linux")] == "foreign"
    assert states[str(dirs.thunar_uca)] == "missing"
    dirs.thunar_uca.unlink()
    assert {e["path"]: e["state"] for e in di.status(paths, env)["entries"]}[str(dirs.thunar_uca)] == "missing"
    system = tmp_path / "system-share" / "applications"
    system.mkdir()
    (system / f"{APP_ID}.desktop").write_text("[Desktop Entry]\n", encoding="utf-8")
    assert di.status(paths, env)["system_desktop"] == str(system / f"{APP_ID}.desktop")
    assert di.status(paths, env)["ok"] is False


def test_cli_aliases_keep_the_mode_of_an_existing_bin_dir(
    paths: Paths, env: dict[str, str], runner: RecordingRunner, tmp_path: Path
) -> None:
    target = _launcher(tmp_path)
    dirs = _dirs(env)
    dirs.bin.mkdir(parents=True)
    dirs.bin.chmod(0o700)
    _install(paths, env, runner, menu=False, file_managers="none", cli_alias=True, exec_cmd=[str(target)])
    assert os.readlink(dirs.bin / "fork") == str(target)
    assert stat.S_IMODE(dirs.bin.stat().st_mode) == 0o700
