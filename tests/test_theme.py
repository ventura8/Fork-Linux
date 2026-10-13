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
# A session bus address (never connected to: the tools are fakes).
BUS = {"DBUS_SESSION_BUS_ADDRESS": "tcp:host=127.0.0.1,port=9"}


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


XAPP_PORTAL = "org.x.apps.portal"
DARK_MODE = _gsettings(XAPP_PORTAL, "color-scheme")
CINNAMON_THEME = _gsettings(CINNAMON, "gtk-theme")


@pytest.mark.parametrize(
    ("outputs", "expected"),
    [
        # Themes > Settings > Dark mode "Prefer dark mode" with the light Yaru-cinnamon kept.
        ({_dconf(XAPP_PORTAL, "color-scheme"): "'prefer-dark'\n", CINNAMON_THEME: "'Yaru-cinnamon'\n"}, "dark"),
        # The same through gsettings, and "Prefer light mode" over a dark theme name.
        ({DARK_MODE: "'prefer-dark'\n", CINNAMON_THEME: "'Yaru-cinnamon'\n"}, "dark"),
        ({DARK_MODE: "'prefer-light'\n", CINNAMON_THEME: "'Mint-Y-Dark'\n"}, "light"),
        # "Let applications decide": the theme name decides.
        ({DARK_MODE: "'default'\n", CINNAMON_THEME: "'Yaru-cinnamon-dark'\n"}, "dark"),
    ],
    ids=["dconf-prefer-dark", "gsettings-prefer-dark", "prefer-light", "default-uses-theme"],
)
def test_cinnamon_dark_mode_preference(home: Path, outputs: dict[tuple[str, ...], str], expected: str) -> None:
    assert theme.detect({"XDG_CURRENT_DESKTOP": "X-Cinnamon"}, _runner(outputs), home) == expected


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


WINDOW_BACKGROUND = ("Colors:Window", "BackgroundNormal")


@pytest.mark.parametrize(
    ("colour", "expected"),
    [
        ("35,38,41", "dark"),  # a dark scheme whose name does not say so (Nordic, Dracula, ...)
        ("32,35,38,255", "dark"),  # with alpha
        ("191,191,191", "dark"),  # grey 191: below Plasma's line
        ("192,192,192", "light"),  # grey 192: on it
        ("239,240,241", "light"),
    ],
)
def test_kde_window_background_wins_over_the_names(home: Path, colour: str, expected: str) -> None:
    runner = _runner(
        {
            _kread("kreadconfig6", *WINDOW_BACKGROUND): colour + "\n",
            _kread("kreadconfig6", "General", "ColorScheme"): "Nordic\n",
            _kread("kreadconfig6", "KDE", "LookAndFeelPackage"): "org.kde.breezedark.desktop\n",
        }
    )
    assert theme.detect({"XDG_CURRENT_DESKTOP": "KDE"}, runner, home) == expected
    assert runner.argvs == [list(_kread("kreadconfig6", *WINDOW_BACKGROUND))]


def test_kde_window_background_from_kreadconfig5(home: Path) -> None:
    runner = _runner({_kread("kreadconfig5", *WINDOW_BACKGROUND): "42,46,50\n"}, installed=("kreadconfig5",))
    assert theme.detect({"XDG_CURRENT_DESKTOP": "KDE"}, runner, home) == "dark"


@pytest.mark.parametrize("value", ["#232629", "35,38", "256x,1,1", "1,2,3,4,5"])
def test_kde_a_window_background_that_is_no_colour_falls_back_to_the_names(home: Path, value: str) -> None:
    runner = _runner(
        {
            _kread("kreadconfig6", *WINDOW_BACKGROUND): value + "\n",
            _kread("kreadconfig6", "General", "ColorScheme"): "BreezeDark\n",
        }
    )
    assert theme.detect({"XDG_CURRENT_DESKTOP": "KDE"}, runner, home) == "dark"


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
    assert theme.detect({"XDG_CURRENT_DESKTOP": "XFCE", **BUS}, runner, home) == "dark"


def test_xfce_without_a_session_bus_is_not_asked(home: Path) -> None:
    runner = _runner({XFCE_THEME: "Adwaita-dark\n"})
    assert theme.detect({"XDG_CURRENT_DESKTOP": "XFCE"}, runner, home) is None
    assert all(argv[0] != "xfconf-query" for argv in runner.argvs)


def test_xfce_unknown_falls_back(home: Path) -> None:
    runner = _runner({_gsettings(GNOME, "gtk-theme"): "'Greybird'\n"})
    assert theme.detect({"XDG_CURRENT_DESKTOP": "XFCE"}, runner, home) == "light"


def test_mate(home: Path) -> None:
    runner = _runner({_gsettings("org.mate.interface", "gtk-theme"): "'Ambiant-MATE-Dark'\n"})
    assert theme.detect({"XDG_CURRENT_DESKTOP": "MATE"}, runner, home) == "dark"


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("'Menta'", "light"),
        ("'BlackMATE'", "dark"),
        ("'HighContrastInverse'", "dark"),
        ("'HighContrast'", "light"),
        ("'Yaru-MATE-dark'", "dark"),
    ],
)
def test_mate_stock_themes(home: Path, name: str, expected: str) -> None:
    """MATE's stock dark themes (mate-themes) do not say "dark" in their names."""
    runner = _runner({_gsettings("org.mate.interface", "gtk-theme"): name + "\n"})
    assert theme.detect({"XDG_CURRENT_DESKTOP": "MATE"}, runner, home) == expected


def _lxqt_env(home: Path) -> dict[str, str]:
    """An LXQt session whose system directories are under ``home`` (never the host's /etc/xdg)."""
    return {
        "XDG_CURRENT_DESKTOP": "LXQt",
        "XDG_CONFIG_DIRS": f"relative/xdg:{home / 'xdg-Lubuntu'}:{home / 'xdg'}",
        "XDG_DATA_DIRS": str(home / "share"),
    }


def _write_under(home: Path, relative: str, text: str) -> None:
    path = home / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _lubuntu(home: Path) -> None:
    """Lubuntu 26.04's defaults: the kvantum Qt style with its light "Lubuntu" Kvantum theme."""
    _write_under(home, "xdg-Lubuntu/lxqt/lxqt.conf", "[General]\ntheme=Lubuntu Arc\n\n[Qt]\nstyle=kvantum\n")
    _write_under(home, "xdg-Lubuntu/Kvantum/kvantum.kvconfig", "[General]\ntheme=Lubuntu\n")
    lubuntu = "[%General]\nx=1\n[GeneralColors]\nwindow.color=#f5f6f7\n"
    _write_under(home, "share/Kvantum/Lubuntu/Lubuntu.kvconfig", lubuntu)
    _write_under(home, "share/Kvantum/KvArcDark/KvArcDark.kvconfig", "[GeneralColors]\nwindow.color=#383c4a\n")


def test_lxqt_conf(home: Path) -> None:
    _write(home, "lxqt/lxqt.conf", "[General]\nicon_theme=x\ntheme=\ntheme=kvantum-dark\n")
    assert theme.detect(_lxqt_env(home), _runner({}), home) == "dark"


def test_lxqt_session_conf(home: Path) -> None:
    _write(home, "lxqt/lxqt.conf", "[General]\nicon_theme=x\n")
    _write(home, "lxqt/session.conf", "theme = frost\n")
    assert theme.detect(_lxqt_env(home), _runner({}), home) == "light"


def test_lxqt_without_config(home: Path) -> None:
    assert theme.detect(_lxqt_env(home), _runner({}), home) is None


# GNOME's gtk-theme on Lubuntu 26.04 names a theme with dark header bars only.
LUBUNTU_GSETTINGS = {_gsettings(GNOME, "gtk-theme"): "'Arc-Darker'\n"}


@pytest.mark.parametrize(
    ("files", "expected"),
    [
        # Lubuntu as installed: light, whatever GNOME's gtk-theme (Arc-Darker) says.
        ({}, "light"),
        # Kvantum Manager: a dark theme, the installed one or the user's own copy of it.
        ({"Kvantum/kvantum.kvconfig": "[General]\ntheme=KvArcDark\n"}, "dark"),
        (
            {
                "Kvantum/kvantum.kvconfig": "theme=KvArcDark#\n",
                "Kvantum/KvArcDark#/KvArcDark#.kvconfig": "[GeneralColors]\nwindow.color=#fafafa\n",
            },
            "light",
        ),
        # A Kvantum theme without a readable window colour: its name decides.
        ({"Kvantum/kvantum.kvconfig": "[General]\ntheme=KvGnomeDark\n"}, "dark"),
        ({"Kvantum/kvantum.kvconfig": "[General]\ntheme=KvFlat\n"}, "light"),
        # Only the panel's LXQt theme is dark: the applications stay light.
        ({"lxqt/lxqt.conf": "[General]\ntheme=dark\n"}, "light"),
        # LXQt Appearance > Widget Style: Fusion with the Dark (or Light) palette.
        ({"lxqt/lxqt.conf": "[Qt]\nstyle=Fusion\n[Palette]\nwindow_color=#232323\n"}, "dark"),
        ({"lxqt/lxqt.conf": "[Qt]\nstyle=Fusion\n[Palette]\nwindow_color=#efefef\n"}, "light"),
        # Fusion with a palette that is not #rrggbb: LXQt's theme= decides, as before.
        ({"lxqt/lxqt.conf": "[General]\ntheme=dark\n[Qt]\nstyle=Fusion\n[Palette]\nwindow_color=#123\n"}, "dark"),
        ({"lxqt/lxqt.conf": "[Qt]\nstyle=Fusion\n[Palette]\nwindow_color=#gggggg\n"}, "light"),
    ],
)
def test_lxqt_applications_colours(home: Path, files: dict[str, str], expected: str) -> None:
    _lubuntu(home)
    for relative, text in files.items():
        _write(home, relative, text)
    runner = _runner(LUBUNTU_GSETTINGS)
    assert theme.detect(_lxqt_env(home), runner, home) == expected


def test_lxqt_kvantum_without_any_config_is_kvantums_default(home: Path) -> None:
    _write(home, "lxqt/lxqt.conf", "[Qt]\nstyle=kvantum\n")
    assert theme.detect(_lxqt_env(home), _runner(LUBUNTU_GSETTINGS), home) == "light"


def test_lxqt_system_dirs_default_to_etc_xdg(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Path] = []
    monkeypatch.setattr(theme, "_read", lambda path: seen.append(path) or "")
    assert theme.detect({"XDG_CURRENT_DESKTOP": "LXQt"}, _runner({}), home) is None
    assert Path("/etc/xdg/lxqt/lxqt.conf") in seen


def test_ini_section_keys_before_any_section_are_general() -> None:
    content = "theme=a\n[Qt]\nstyle=x\n[General]\ntheme=b\nicon='i'\n"
    assert theme._ini_section(content, "General") == {"theme": "a", "icon": "i"}
    assert theme._ini_section(content, "Qt") == {"style": "x"}


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
    xfce = {**base, **BUS, "XDG_CURRENT_DESKTOP": "XFCE", "FL_FAKE_XFCONF": xfce_values}
    assert theme.detect(xfce, Runner(), home) == "light"
