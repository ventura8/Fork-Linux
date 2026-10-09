"""Tests for fork_linux.theme: desktop classification and the dark/light probes."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from fork_linux import theme
from fork_linux.procrun import Completed, RecordingRunner, Runner

FAKES_BIN = Path(__file__).resolve().parent / "fakes" / "bin"
TOOLS = ("dconf", "gsettings", "kreadconfig6", "kreadconfig5", "xfconf-query")
GNOME = "org.gnome.desktop.interface"
CINNAMON = "org.cinnamon.desktop.interface"


def _gsettings(schema: str, key: str) -> tuple[str, ...]:
    return ("gsettings", "get", schema, key)


def _dconf(schema: str, key: str) -> tuple[str, ...]:
    return ("dconf", "read", "/" + schema.replace(".", "/") + "/" + key)


def _kread(tool: str, group: str, key: str) -> tuple[str, ...]:
    return (tool, "--file", "kdeglobals", "--group", group, "--key", key)


XFCE_THEME = ("xfconf-query", "-c", "xsettings", "-p", "/Net/ThemeName")


def _runner(outputs: dict[tuple[str, ...], Any], installed: tuple[str, ...] = TOOLS) -> RecordingRunner:
    """Answers ``outputs`` per argv (anything else fails like a missing key)."""

    def respond(argv: list[str]) -> Any:
        return outputs.get(tuple(argv), Completed(argv, 1, "", "No such key\n"))

    return RecordingRunner(respond, {tool: f"/usr/bin/{tool}" if tool in installed else None for tool in TOOLS})


def _write(home: Path, relative: str, text: str) -> Path:
    path = home / ".config" / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def home(tmp_path: Path) -> Path:
    path = tmp_path / "home"
    path.mkdir()
    return path


# --------------------------------------------------------------------------- desktop


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({"XDG_CURRENT_DESKTOP": "KDE"}, "kde"),
        ({"XDG_CURRENT_DESKTOP": "plasma"}, "kde"),
        ({"XDG_CURRENT_DESKTOP": "XFCE"}, "xfce"),
        ({"DESKTOP_SESSION": "xubuntu"}, "xfce"),
        ({"XDG_CURRENT_DESKTOP": "X-Cinnamon"}, "cinnamon"),
        ({"XDG_CURRENT_DESKTOP": "MATE"}, "mate"),
        ({"XDG_CURRENT_DESKTOP": "Budgie:GNOME"}, "budgie"),
        ({"XDG_CURRENT_DESKTOP": "LXQt"}, "lxqt"),
        ({"DESKTOP_SESSION": "Lubuntu"}, "lxqt"),
        ({"XDG_CURRENT_DESKTOP": "ubuntu:GNOME"}, "gnome"),
        ({"XDG_CURRENT_DESKTOP": "Unity"}, "gnome"),
        ({"XDG_CURRENT_DESKTOP": "pop:GNOME"}, "gnome"),
        ({"XDG_CURRENT_DESKTOP": "", "DESKTOP_SESSION": "gnome"}, "gnome"),
        ({"XDG_CURRENT_DESKTOP": "sway"}, "unknown"),
        ({}, "unknown"),
    ],
)
def test_desktop(env: dict[str, str], expected: str) -> None:
    assert theme.desktop(env) == expected


# --------------------------------------------------------------------------- detect


def test_gtk_theme_env_forces_dark(home: Path) -> None:
    runner = _runner({})
    assert theme.detect({"GTK_THEME": "Adwaita:dark"}, runner, home) == "dark"
    assert runner.calls == []


def test_gtk_theme_env_without_dark_is_not_decisive(home: Path) -> None:
    runner = _runner({_gsettings(GNOME, "color-scheme"): "'prefer-dark'\n"})
    assert theme.detect({"GTK_THEME": "Adwaita"}, runner, home) == "dark"


def test_gnome_dconf_color_scheme(home: Path) -> None:
    runner = _runner({_dconf(GNOME, "color-scheme"): "'prefer-dark'\n"})
    assert theme.detect({"XDG_CURRENT_DESKTOP": "GNOME"}, runner, home) == "dark"
    assert len(runner.calls) == 1


def test_gnome_gsettings_prefer_light(home: Path) -> None:
    runner = _runner({_gsettings(GNOME, "color-scheme"): "'prefer-light'\n"}, installed=("gsettings",))
    assert theme.detect({"XDG_CURRENT_DESKTOP": "GNOME"}, runner, home) == "light"
    assert [argv[0] for argv in runner.argvs] == ["gsettings"]


@pytest.mark.parametrize(("name", "expected"), [("'Yaru-dark'", "dark"), ("'Adwaita'", "light")])
def test_gnome_default_scheme_uses_gtk_theme_name(home: Path, name: str, expected: str) -> None:
    runner = _runner({_gsettings(GNOME, "color-scheme"): "'default'\n", _gsettings(GNOME, "gtk-theme"): name})
    assert theme.detect({"XDG_CURRENT_DESKTOP": "ubuntu:GNOME"}, runner, home) == expected


def test_gnome_dconf_gtk_theme(home: Path) -> None:
    runner = _runner({_dconf(GNOME, "gtk-theme"): "'Pop-dark'\n"})
    assert theme.detect({}, runner, home) == "dark"


def test_nothing_known(home: Path) -> None:
    assert theme.detect({"XDG_CURRENT_DESKTOP": "GNOME"}, _runner({}), home) is None


def test_cinnamon_own_schema(home: Path) -> None:
    runner = _runner({_gsettings(CINNAMON, "gtk-theme"): "'Mint-Y-Dark'\n"})
    assert theme.detect({"XDG_CURRENT_DESKTOP": "X-Cinnamon"}, runner, home) == "dark"


def test_cinnamon_falls_back_to_gnome(home: Path) -> None:
    runner = _runner({_gsettings(GNOME, "color-scheme"): "'prefer-light'\n"})
    assert theme.detect({"XDG_CURRENT_DESKTOP": "X-Cinnamon"}, runner, home) == "light"


def test_kde_kreadconfig6_color_scheme(home: Path) -> None:
    runner = _runner({_kread("kreadconfig6", "General", "ColorScheme"): "BreezeDark\n"})
    assert theme.detect({"XDG_CURRENT_DESKTOP": "KDE"}, runner, home) == "dark"


def test_kde_kreadconfig5_look_and_feel(home: Path) -> None:
    runner = _runner(
        {_kread("kreadconfig5", "KDE", "LookAndFeelPackage"): "org.kde.breeze.desktop\n"},
        installed=("kreadconfig5",),
    )
    assert theme.detect({"XDG_CURRENT_DESKTOP": "KDE"}, runner, home) == "light"


def test_kde_kdeglobals_file(home: Path) -> None:
    _write(home, "kdeglobals", "[General]\nName=x\n\n[KDE]\nLookAndFeelPackage=org.kde.breezedark.desktop\n")
    assert theme.detect({"XDG_CURRENT_DESKTOP": "KDE"}, _runner({}, installed=()), home) == "dark"


def test_kde_kdeglobals_light(home: Path) -> None:
    _write(home, "kdeglobals", "[General]\nColorScheme=BreezeLight\n")
    assert theme.detect({"XDG_CURRENT_DESKTOP": "KDE"}, _runner({}, installed=()), home) == "light"


def test_kde_without_anything_falls_back_to_gnome_then_settings_ini(home: Path) -> None:
    _write(home, "gtk-3.0/settings.ini", "[Settings]\ngtk-theme-name=Breeze-Dark\n")
    runner = _runner({})
    assert theme.detect({"XDG_CURRENT_DESKTOP": "KDE"}, runner, home) == "dark"
    assert any(argv[0] == "gsettings" for argv in runner.argvs)


def test_xfce(home: Path) -> None:
    runner = _runner({XFCE_THEME: "Adwaita-dark\n"})
    assert theme.detect({"XDG_CURRENT_DESKTOP": "XFCE"}, runner, home) == "dark"


def test_xfce_unknown_falls_back(home: Path) -> None:
    runner = _runner({_gsettings(GNOME, "gtk-theme"): "'Greybird'\n"})
    assert theme.detect({"XDG_CURRENT_DESKTOP": "XFCE"}, runner, home) == "light"


def test_mate(home: Path) -> None:
    runner = _runner({_gsettings("org.mate.interface", "gtk-theme"): "'Ambiant-MATE-Dark'\n"})
    assert theme.detect({"XDG_CURRENT_DESKTOP": "MATE"}, runner, home) == "dark"


def test_lxqt_conf(home: Path) -> None:
    _write(home, "lxqt/lxqt.conf", "[General]\nicon_theme=x\ntheme=\ntheme=kvantum-dark\n")
    assert theme.detect({"XDG_CURRENT_DESKTOP": "LXQt"}, _runner({}), home) == "dark"


def test_lxqt_session_conf(home: Path) -> None:
    _write(home, "lxqt/lxqt.conf", "[General]\nicon_theme=x\n")
    _write(home, "lxqt/session.conf", "theme = frost\n")
    assert theme.detect({"XDG_CURRENT_DESKTOP": "LXQt"}, _runner({}), home) == "light"


def test_lxqt_without_config(home: Path) -> None:
    assert theme.detect({"XDG_CURRENT_DESKTOP": "LXQt"}, _runner({}), home) is None


@pytest.mark.parametrize(
    ("files", "expected"),
    [
        ({"gtk-4.0/settings.ini": "[Settings]\ngtk-application-prefer-dark-theme=1\n"}, "dark"),
        (
            {"gtk-4.0/settings.ini": "[Settings]\ngtk-application-prefer-dark-theme=true\ngtk-theme-name=Breeze\n"},
            "dark",
        ),
        ({"gtk-4.0/settings.ini": "[Settings]\ngtk-theme-name='Breeze'\n"}, "light"),
        (
            {
                "gtk-4.0/settings.ini": "[Settings]\ngtk-application-prefer-dark-theme=false\n",
                "gtk-3.0/settings.ini": "[Settings]\ngtk-theme-name=\"Arc-Dark\"\n",
            },
            "dark",
        ),
        ({"gtk-3.0/settings.ini": "[Settings]\ngtk-theme-name=\n"}, None),
    ],
)
def test_gtk_settings_ini(home: Path, files: dict[str, str], expected: str | None) -> None:
    for relative, text in files.items():
        _write(home, relative, text)
    assert theme.detect({"XDG_CURRENT_DESKTOP": "sway"}, _runner({}, installed=()), home) == expected


def test_xdg_config_home_is_respected(home: Path, tmp_path: Path) -> None:
    config = tmp_path / "custom-config"
    (config / "gtk-4.0").mkdir(parents=True)
    (config / "gtk-4.0" / "settings.ini").write_text("gtk-theme-name=Adwaita-dark\n", encoding="utf-8")
    runner = _runner({}, installed=())
    assert theme.detect({"XDG_CONFIG_HOME": str(config)}, runner, home) == "dark"
    assert theme.detect({"XDG_CONFIG_HOME": "relative/config"}, runner, home) is None


def test_unreadable_config_file_is_ignored(home: Path) -> None:
    (home / ".config" / "kdeglobals").mkdir(parents=True)
    assert theme.detect({"XDG_CURRENT_DESKTOP": "KDE"}, _runner({}, installed=()), home) is None


# --------------------------------------------------------------------------- with the fake tools


@pytest.fixture
def isolated_path(tmp_path: Path, fake_bin: Path) -> str:
    """A PATH holding only our fakes and python3, so no host desktop tool can run."""
    bin_dir = tmp_path / "isolated-bin"
    bin_dir.mkdir()
    for fake in FAKES_BIN.iterdir():
        (bin_dir / fake.name).symlink_to(fake)
    (bin_dir / "python3").symlink_to(sys.executable)
    return str(bin_dir)


def test_detect_with_fake_gsettings(isolated_path: str, fake_bin: Path, home: Path) -> None:
    env = {
        "PATH": isolated_path,
        "FL_FAKE_LOG": str(fake_bin),
        "XDG_CURRENT_DESKTOP": "ubuntu:GNOME",
        "FL_FAKE_GSETTINGS": json.dumps({f"{GNOME} color-scheme": "'prefer-dark'"}),
    }
    assert theme.detect(env, Runner(), home) == "dark"
    logged = [json.loads(line)["argv"] for line in fake_bin.read_text(encoding="utf-8").splitlines()]
    assert logged == [["gsettings", "get", GNOME, "color-scheme"]]


def test_detect_with_fake_kde_and_xfce_tools(isolated_path: str, fake_bin: Path, home: Path) -> None:
    base = {"PATH": isolated_path, "FL_FAKE_LOG": str(fake_bin)}
    kde_values = json.dumps({"kdeglobals/General/ColorScheme": "BreezeDark"})
    kde = {**base, "XDG_CURRENT_DESKTOP": "KDE", "FL_FAKE_KREADCONFIG": kde_values}
    assert theme.detect(kde, Runner(), home) == "dark"
    xfce_values = json.dumps({"xsettings:/Net/ThemeName": "Greybird"})
    xfce = {**base, "XDG_CURRENT_DESKTOP": "XFCE", "FL_FAKE_XFCONF": xfce_values}
    assert theme.detect(xfce, Runner(), home) == "light"
