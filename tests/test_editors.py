"""Tests for :mod:`fork_linux.editors` (host IDE discovery and Wine stubs)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from fork_linux import editors
from fork_linux.paths import Paths

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def paths(xdg: Path) -> Paths:
    return Paths.from_env({"HOME": str(xdg), "XDG_DATA_HOME": str(xdg / "data")})


def _exe(directory: Path, name: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def test_discover_finds_ides_on_path(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    _exe(bin_dir, "code")
    _exe(bin_dir, "cursor")
    _exe(bin_dir, "intellij-idea")
    found = editors.discover({"PATH": str(bin_dir)})
    assert [item.id for item in found] == ["code", "cursor", "idea"]
    assert found[0].win_names == ("Code.exe",)
    assert found[1].command.endswith("/cursor")
    assert found[2].label == "IntelliJ IDEA"


def test_discover_keeps_snap_links(tmp_path: Path) -> None:
    """A snap launcher is a link to ``/usr/bin/snap``: the link is the command, not its target."""
    target = _exe(tmp_path / "usr", "snap")
    bin_dir = tmp_path / "snap-bin"
    bin_dir.mkdir()
    (bin_dir / "subl").symlink_to(target)
    found = editors.discover({"PATH": str(bin_dir)})
    assert [(item.id, item.command) for item in found] == [("sublime", str(bin_dir / "subl"))]


def test_discover_finds_flatpak_and_toolbox_launchers(tmp_path: Path) -> None:
    data = tmp_path / "data"
    _exe(data / "flatpak" / "exports" / "bin", "com.google.AntigravityIDE")
    _exe(data / "flatpak" / "exports" / "bin", "dev.zed.Zed")
    _exe(data / "JetBrains" / "Toolbox" / "scripts", "goland")
    found = editors.discover({"PATH": str(tmp_path / "none"), "XDG_DATA_HOME": str(data)})
    assert [item.id for item in found] == ["goland", "zed", "antigravity-ide"]
    home_found = editors.discover({"PATH": str(tmp_path / "none"), "HOME": str(tmp_path / "home")})
    assert home_found == []


def test_discover_skips_missing(tmp_path: Path) -> None:
    assert editors.discover({"PATH": str(tmp_path)}) == []
    # An empty PATH entry is skipped, not read as the current directory.
    _exe(tmp_path / "bin", "cursor")
    assert editors.discover({"PATH": f"::{tmp_path / 'none'}", "HOME": str(tmp_path / "home")}) == []


def test_every_fork_editor_has_its_probe_path(paths: Paths, tmp_path: Path) -> None:
    """Stubs land exactly where Fork 2.23.2 looks (traced with WINEDEBUG=+file)."""
    bin_dir = tmp_path / "bin"
    for item in editors.EDITORS:
        _exe(bin_dir, item.commands[0])
    found = editors.discover({"PATH": str(bin_dir)})
    assert [item.id for item in found] == [item.id for item in editors.EDITORS]
    paths.prefix.mkdir(parents=True)
    launch = tmp_path / "fl-launch.exe"
    launch.write_bytes(b"MZ-launch")
    editors.install(paths, "ada", launch, found)
    local = paths.prefix / "drive_c" / "users" / "ada" / "AppData" / "Local"
    expected = [
        "Programs/Microsoft VS Code/Code.exe",
        "Programs/Microsoft VS Code Insiders/Code - Insiders.exe",
        "Programs/cursor/Cursor.exe",
        "Programs/IntelliJ IDEA/bin/idea64.exe",
        "Programs/PyCharm/bin/pycharm64.exe",
        "Programs/WebStorm/bin/webstorm64.exe",
        "Programs/PhpStorm/bin/phpstorm64.exe",
        "Programs/Rider/bin/rider64.exe",
        "Programs/GoLand/bin/goland64.exe",
        "Programs/Fleet/Fleet.exe",
        "atom/atom.exe",
        "Programs/Zed/Zed.exe",
        "Programs/Antigravity/Antigravity.exe",
        "Programs/Antigravity IDE/Antigravity IDE.exe",
        "Programs/CodeBuddy/CodeBuddy.exe",
    ]
    for rel in expected:
        assert (local / rel).read_bytes() == b"MZ-launch", rel
    assert (paths.prefix / "drive_c" / "Program Files" / "Sublime Text" / "sublime_text.exe").is_file()
    root = editors.editors_win_dir(paths)
    assert sorted(path.name for path in root.iterdir()) == sorted(n for item in found for n in item.win_names)


def test_install_writes_and_removes_stubs(paths: Paths, tmp_path: Path) -> None:
    paths.prefix.mkdir(parents=True)
    launch = tmp_path / "fl-launch.exe"
    launch.write_bytes(b"MZ-launch")
    bin_dir = tmp_path / "bin"
    _exe(bin_dir, "code")
    _exe(bin_dir, "cursor")
    _exe(bin_dir, "idea")
    env = {"PATH": str(bin_dir)}
    found = editors.discover(env)
    assert editors.install(paths, "ada", launch, found) == ["code", "cursor", "idea"]
    root = editors.editors_win_dir(paths)
    assert (root / "Code.exe").read_bytes() == b"MZ-launch"
    assert (root / "Cursor.exe").read_bytes() == b"MZ-launch"
    programs = editors.programs_root(paths, "ada")
    assert (programs / "Microsoft VS Code" / "Code.exe").is_file()
    assert (programs / "cursor" / "Cursor.exe").is_file()
    assert (programs / "IntelliJ IDEA" / "bin" / "idea64.exe").is_file()
    entries = editors.app_path_entries(paths, found)
    assert ("Code.exe", r"C:\fork-linux\editors\Code.exe") in entries
    # Drop cursor and idea: their stubs and the folders we made go away, code stays.
    (bin_dir / "cursor").unlink()
    (bin_dir / "idea").unlink()
    editors.install(paths, "ada", launch, editors.discover(env))
    assert (root / "Code.exe").is_file()
    assert not (root / "Cursor.exe").exists()
    assert not (programs / "cursor").exists()
    assert not (programs / "IntelliJ IDEA").exists()
    assert programs.is_dir()


def test_install_removes_legacy_stubs(paths: Paths, tmp_path: Path) -> None:
    """Stubs v1.0.3 wrote where Fork never looks are removed; a user's own file next to them stays."""
    launch = tmp_path / "fl-launch.exe"
    launch.write_bytes(b"MZ-launch")
    programs = editors.programs_root(paths, "ada")
    legacy = [programs / "IntelliJ IDEA" / "idea64.exe", programs / "Sublime Text" / "subl.exe",
              programs / "Android Studio" / "studio64.exe", programs / "atom" / "atom.exe",
              editors.editors_win_dir(paths) / "studio64.exe"]
    for path in legacy:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"MZ-old")
    keep = programs / "Sublime Text" / "notes.txt"
    keep.write_text("mine\n", encoding="utf-8")
    editors.install(paths, "ada", launch, [])
    assert [path for path in legacy if path.exists()] == []
    assert not (programs / "IntelliJ IDEA").exists()
    assert keep.is_file()


def test_allowed_ids_match_catalog() -> None:
    assert editors.ALLOWED_IDS == frozenset(item.id for item in editors.EDITORS)


def test_fl_launch_maps_every_stub_name() -> None:
    """``bridge/win/fl_launch.c`` maps exactly the catalog's stub basenames to their ids."""
    source = (ROOT / "bridge" / "win" / "fl_launch.c").read_text(encoding="utf-8")
    table = source[source.index("static const struct editor editors[]"):]
    table = table[: table.index("};")]
    pairs = set(re.findall(r'\{ "([^"]+)", "([^"]+)" \}', table))
    assert pairs == {(name, item.id) for item in editors.EDITORS for name in item.win_names}


def test_inputs_signature_lists_stubs(tmp_path: Path) -> None:
    _exe(tmp_path, "zed")
    signature = editors.inputs_signature(editors.discover({"PATH": str(tmp_path)}))
    assert signature["editors"][0]["stubs"] == [["programs", "Zed\\Zed.exe"]]


# -- icons -----------------------------------------------------------------------------------------


def _png(path: Path, size: int, rgba: tuple[int, int, int, int] = (0, 120, 215, 255)) -> Path:
    from fork_linux import imaging

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(imaging.png_encode(size, size, bytes(rgba) * size * size))
    return path


def _desktop(directory: Path, name: str, exec_line: str, icon: str, extra: str = "") -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(
        f"[Desktop Entry]\nType=Application\nExec={exec_line}\nIcon={icon}\n{extra}"
        f"[Desktop Action new]\nExec=other\nIcon=ignored\n",
        encoding="utf-8",
    )


def test_find_icon_from_the_desktop_entry(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    data = tmp_path / "data"
    system = tmp_path / "system"
    code = _exe(bin_dir, "code")
    _exe(bin_dir, "cursor")
    _exe(bin_dir, "zed")
    _exe(bin_dir, "subl")
    _exe(bin_dir, "atom")
    _exe(bin_dir, "fleet")
    snap_icon = _png(tmp_path / "snap" / "vscode.png", 32)
    _desktop(data / "applications", "code.desktop", f"{code} --force-user-env %F", str(snap_icon))
    _desktop(system / "applications", "cursor.desktop", '/opt/cursor/cursor "%F', "co.anysphere.cursor")
    themed = _png(system / "icons" / "hicolor" / "256x256" / "apps" / "co.anysphere.cursor.png", 256)
    _png(system / "icons" / "hicolor" / "32x32" / "apps" / "co.anysphere.cursor.png", 32)
    _desktop(system / "applications", "zed.desktop", "zed %U", "zed.png")
    pixmap = _png(system / "pixmaps" / "zed.png", 48)
    _desktop(system / "applications", "subl.desktop", "subl", "/opt/subl/icon.svg")
    _desktop(system / "applications", "subl2.desktop", "subl", "/opt/subl/missing.png")
    _desktop(system / "applications", "atom.desktop", "atom", "no-such-icon")
    _desktop(system / "applications", "fleet.desktop", "fleet", "", "NoDisplay=true\n")
    (system / "applications" / "huge.desktop").write_bytes(b"#" * (editors.MAX_DESKTOP_FILE + 1))
    (system / "applications" / "broken.desktop").symlink_to(tmp_path / "missing")
    env = {"PATH": str(bin_dir), "XDG_DATA_HOME": str(data), "XDG_DATA_DIRS": f"{system}:{tmp_path / 'none'}"}
    icons = {item.id: editors.find_icon(item, env) for item in editors.discover(env)}
    assert icons == {"code": str(snap_icon), "cursor": str(themed), "sublime": None, "atom": None,
                     "zed": str(pixmap), "fleet": None}
    assert editors.data_dirs({}) == []
    assert editors.data_dirs({"HOME": "/h", "XDG_DATA_DIRS": "/s"}) == [
        "/h/.local/share", "/s", "/h/.local/share/flatpak/exports/share"]


def test_stub_payload_falls_back_to_the_plain_launcher(tmp_path: Path) -> None:
    from fixtures.pe_builder import build_pe

    from fork_linux.pe_resources import PEFile

    launch = build_pe()
    good = _png(tmp_path / "good.png", 32)
    assert PEFile(editors.stub_payload(launch, str(good))).group_icons()[0].entries[0].width == 16
    assert editors.stub_payload(launch, None) == launch
    assert editors.stub_payload(launch, str(tmp_path / "missing.png")) == launch
    (tmp_path / "bad.png").write_bytes(b"not a png")
    assert editors.stub_payload(launch, str(tmp_path / "bad.png")) == launch
    big = tmp_path / "big.png"
    big.write_bytes(b"\x00" * (editors.MAX_ICON_FILE + 1))
    assert editors.stub_payload(launch, str(big)) == launch


def test_install_gives_each_stub_its_icon(paths: Paths, tmp_path: Path) -> None:
    from fixtures.pe_builder import build_pe

    from fork_linux.pe_resources import PEFile

    paths.prefix.mkdir(parents=True)
    launch = tmp_path / "fl-launch.exe"
    launch.write_bytes(build_pe())
    bin_dir = tmp_path / "bin"
    zed = _exe(bin_dir, "zed")
    _exe(bin_dir, "cursor")
    data = tmp_path / "data"
    _desktop(data / "applications", "dev.zed.Zed.desktop", str(zed), str(_png(tmp_path / "zed.png", 64)))
    env = {"PATH": str(bin_dir), "XDG_DATA_HOME": str(data), "XDG_DATA_DIRS": str(tmp_path / "none")}
    editors.install(paths, "ada", launch, editors.discover(env), env)
    zed_stub = editors.programs_root(paths, "ada") / "Zed" / "Zed.exe"
    assert [entry.width for entry in PEFile.from_path(zed_stub).group_icons()[0].entries] == [16, 20, 24, 32, 40, 48,
                                                                                               64]
    assert (editors.editors_win_dir(paths) / "Zed.exe").read_bytes() == zed_stub.read_bytes()
    assert (editors.programs_root(paths, "ada") / "cursor" / "Cursor.exe").read_bytes() == launch.read_bytes()
    signature = editors.inputs_signature(editors.discover(env), env)
    assert [item["icon"] for item in signature["editors"]] == [None, str(tmp_path / "zed.png")]
