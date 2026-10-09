"""Tests for fork_linux.config: defaults, config.ini, FORK_LINUX_* overrides and validation."""

from __future__ import annotations

import configparser
import logging
import stat
from pathlib import Path

import pytest

from fork_linux import config as config_mod
from fork_linux import resources
from fork_linux.config import SCHEMA, Config
from fork_linux.errors import ForkLinuxError, UsageError
from fork_linux.paths import Paths

CONTRACT = {
    "wine": {
        "provider": "managed",
        "build": "",
        "debug": "-all",
        "driver": "auto",
        "renderer": "gdi",
        "extra_dll_overrides": "",
    },
    "fork": {
        "channel": "known-good",
        "update_policy": "auto",
        "enforce_settings": "UpdateSubmodulesOnCheckout, DisableHardwareAcceleration",
    },
    "display": {"dpi": "auto", "theme": "follow"},
    "ssh": {"sync": "auto", "mode": "link", "agent_bridge": "off"},
    "git": {
        "config_overlay": "translate",
        "env_overrides": "core.filemode=false, core.autocrlf=false",
        "safe_directory_all": "false",
        "bridge": "off",
    },
    "integration": {"terminal": "auto", "open_files_natively": "true"},
    "snapshots": {"keep": "2", "method": "auto"},
    "ui": {"progress": "auto"},
}


@pytest.fixture
def paths(xdg: Path) -> Paths:
    return Paths.from_env()


def _write(paths: Paths, text: str) -> Path:
    paths.config_file.parent.mkdir(parents=True, exist_ok=True)
    paths.config_file.write_text(text, encoding="utf-8")
    return paths.config_file


# --- schema and defaults.ini --------------------------------------------------------


def test_schema_defaults_match_the_contract() -> None:
    assert {s: {k: spec.default for k, spec in keys.items()} for s, keys in SCHEMA.items()} == CONTRACT


def test_every_default_is_valid_and_documented() -> None:
    for section, keys in SCHEMA.items():
        for key, spec in keys.items():
            assert config_mod.problem(spec, spec.default) is None, f"{section}.{key}"
            assert spec.doc.endswith("."), f"{section}.{key}"
            assert spec.kind in {"str", "list", "bool", "int", "choice", "id"}


def test_packaged_defaults_ini_is_generated_from_the_schema() -> None:
    path = resources.data_path(config_mod.DEFAULTS_FILE)
    assert path.read_text(encoding="utf-8") == config_mod.render_defaults(), (
        "regenerate src/fork_linux/data/defaults.ini from config.render_defaults()"
    )


def test_defaults_ini_lists_every_key_commented_in_its_section() -> None:
    text = config_mod.render_defaults()
    parser = configparser.ConfigParser(interpolation=None)
    parser.read_string(text)
    assert parser.sections() == list(SCHEMA)
    for section in SCHEMA:
        assert parser.items(section) == []  # everything is commented out
    for section, keys in SCHEMA.items():
        body = text.split(f"[{section}]\n", 1)[1].split("\n[", 1)[0]
        for key, spec in keys.items():
            assert f"# {key} = {spec.default}".rstrip() + "\n" in body
    assert all(len(line) <= 90 for line in text.splitlines())


def test_template_prefers_the_packaged_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    custom = tmp_path / "defaults.ini"
    custom.write_text("# packaged\n", encoding="utf-8")
    monkeypatch.setattr(resources, "data_path", lambda *parts: custom)
    assert config_mod.template() == "# packaged\n"
    monkeypatch.setattr(resources, "data_path", lambda *parts: tmp_path / "missing.ini")
    assert config_mod.template() == config_mod.render_defaults()


# --- loading -------------------------------------------------------------------------


def test_missing_file_gives_defaults(paths: Paths) -> None:
    config = Config.load(paths, env={})
    assert config.path == paths.config_file
    for section, keys in CONTRACT.items():
        for key, value in keys.items():
            assert config.get(section, key) == value
            assert config.source(section, key) == "default"
    assert config.unknown == []
    assert config.problems() == []


def test_file_values_override_defaults(paths: Paths) -> None:
    _write(paths, "[wine]\nprovider = system\nDEBUG = +seh\n[snapshots]\nkeep: 5\n")
    config = Config.load(paths, env={})
    assert config.get("wine", "provider") == "system"
    assert config.source("wine", "provider") == "file"
    assert config.get("wine", "debug") == "+seh"
    assert config.getint("snapshots", "keep") == 5
    assert config.source("wine", "driver") == "default"


def test_env_overrides_file(paths: Paths) -> None:
    _write(paths, "[wine]\nprovider = system\n")
    env = {"FORK_LINUX_WINE_PROVIDER": " flatpak ", "FORK_LINUX_GIT_BRIDGE": "on", "FORK_LINUX_UI_PROGRESS": "  "}
    config = Config.load(paths, env=env)
    assert config.get("wine", "provider") == "flatpak"
    assert config.source("wine", "provider") == "env"
    assert config.getbool("git", "bridge") is True
    assert config.source("ui", "progress") == "default"  # blank variables are ignored


def test_load_defaults_to_os_environ(paths: Paths, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FORK_LINUX_DISPLAY_THEME", "dark")
    assert Config.load(paths).get("display", "theme") == "dark"


def test_env_var_name() -> None:
    assert config_mod.env_var("wine", "extra_dll_overrides") == "FORK_LINUX_WINE_EXTRA_DLL_OVERRIDES"


def test_unknown_settings_are_reported_not_fatal(paths: Paths, caplog: pytest.LogCaptureFixture) -> None:
    _write(paths, "[wine]\nprovider = system\nfuture_key = 1\n[newsection]\nx = 1\n[empty]\n[DEFAULT]\nd = 1\n")
    with caplog.at_level(logging.WARNING, logger="fork_linux"):
        config = Config.load(paths, env={})
    assert config.get("wine", "provider") == "system"
    assert config.unknown == ["wine.future_key", "newsection.x", "[empty]", "DEFAULT.d"]
    assert "ignoring unknown setting wine.future_key" in caplog.text
    problems = config.problems()
    assert any("wine.future_key" in p for p in problems)
    assert len(problems) == 4
    assert config.problems(include_unknown=False) == []


def test_parse_error_is_a_usage_error_with_hint(paths: Paths) -> None:
    _write(paths, "no section header\n")
    with pytest.raises(UsageError) as info:
        Config.load(paths, env={})
    assert "cannot parse" in info.value.message
    assert "config edit" in info.value.hint


def test_duplicate_option_is_a_parse_error(paths: Paths) -> None:
    _write(paths, "[wine]\nprovider = a\nprovider = b\n")
    with pytest.raises(UsageError):
        Config.load(paths, env={})


def test_unreadable_file_is_a_usage_error(paths: Paths) -> None:
    _write(paths, "")
    paths.config_file.write_bytes(b"[wine]\nprovider = \xff\n")
    with pytest.raises(UsageError, match="cannot read"):
        Config.load(paths, env={})


def test_values_keep_percent_signs_and_multiline(paths: Paths) -> None:
    _write(paths, "[wine]\ndebug = %warn\n[git]\nenv_overrides = a=1,\n  b=2\n")
    config = Config.load(paths, env={})
    assert config.get("wine", "debug") == "%warn"
    assert config.getlist("git", "env_overrides") == ["a=1", "b=2"]


# --- typed getters and validation ---------------------------------------------------------


def test_getlist_and_getbool_defaults(paths: Paths) -> None:
    config = Config.load(paths, env={})
    assert config.getlist("fork", "enforce_settings") == ["UpdateSubmodulesOnCheckout", "DisableHardwareAcceleration"]
    assert config.getlist("wine", "extra_dll_overrides") == []
    assert config.getbool("integration", "open_files_natively") is True
    assert config.getbool("ssh", "agent_bridge") is False
    assert config.getint("snapshots", "keep") == 2


@pytest.mark.parametrize(
    ("section", "key", "value"),
    [
        ("wine", "provider", "/opt/wine/bin/wine"),
        ("wine", "provider", "system"),
        ("display", "dpi", "144"),
        ("display", "dpi", "auto"),
        ("git", "bridge", "YES"),
        ("snapshots", "keep", "0"),
        ("snapshots", "method", "hardlink"),
        ("integration", "terminal", "kitty"),
        ("wine", "build", ""),
        ("wine", "build", "kron4ek-11.0-staging-wow64"),
        ("wine", "build", "wine_10.0+custom"),
    ],
)
def test_valid_values(section: str, key: str, value: str) -> None:
    assert config_mod.problem(SCHEMA[section][key], value) is None


@pytest.mark.parametrize(
    ("section", "key", "value", "fragment"),
    [
        ("wine", "provider", "relative/wine", "or an absolute path"),
        ("wine", "renderer", "directx", "expected gdi, gl, vulkan"),
        ("display", "dpi", "12", "or a number from 72 to 480"),
        ("display", "dpi", "huge", "or a number from 72 to 480"),
        ("git", "bridge", "maybe", "expected true or false"),
        ("snapshots", "keep", "two", "whole number"),
        ("snapshots", "keep", "-1", "from 0 to 100"),
        ("snapshots", "keep", "101", "from 0 to 100"),
        ("wine", "build", "../../../.wine", "expected an id"),
        ("wine", "build", "a/b", "expected an id"),
        ("wine", "build", ".hidden", "expected an id"),
        ("wine", "build", "has space", "expected an id"),
    ],
)
def test_invalid_values(section: str, key: str, value: str, fragment: str) -> None:
    why = config_mod.problem(SCHEMA[section][key], value)
    assert why is not None and fragment in why


def test_open_ended_int_ranges() -> None:
    at_least = config_mod.KeySpec("1", "Doc.", kind="int", minimum=1)
    assert config_mod.problem(at_least, "100000") is None
    assert config_mod.problem(at_least, "0") == "expected a number of at least 1"
    up_to = config_mod.KeySpec("1", "Doc.", kind="int", maximum=9)
    assert config_mod.problem(up_to, "-5") is None
    assert config_mod.problem(up_to, "10") == "expected a number up to 9"
    unbounded = config_mod.KeySpec("1", "Doc.", kind="int")
    assert config_mod.problem(unbounded, "-99") is None


def test_invalid_file_value_raises_on_get_with_fix_hint(paths: Paths) -> None:
    _write(paths, "[wine]\nrenderer = directx\n[ssh]\nsync = maybe\n")
    config = Config.load(paths, env={})
    assert config.raw("wine", "renderer") == "directx"
    with pytest.raises(UsageError) as info:
        config.get("wine", "renderer")
    assert str(paths.config_file) in info.value.message
    assert "config set wine.renderer" in info.value.hint
    assert len(config.problems()) == 2


def test_invalid_env_value_names_the_variable(paths: Paths) -> None:
    config = Config.load(paths, env={"FORK_LINUX_SNAPSHOTS_KEEP": "lots"})
    with pytest.raises(UsageError) as info:
        config.getint("snapshots", "keep")
    assert "$FORK_LINUX_SNAPSHOTS_KEEP" in info.value.message
    assert "unset FORK_LINUX_SNAPSHOTS_KEEP" in info.value.hint
    assert "FORK_LINUX_SNAPSHOTS_KEEP" in config.problems()[0]


def test_typed_getters_on_the_wrong_kind_raise_usage_error(paths: Paths) -> None:
    config = Config.load(paths, env={"FORK_LINUX_SSH_SYNC": "auto"})
    with pytest.raises(UsageError, match="not true or false"):
        config.getbool("ssh", "sync")
    with pytest.raises(UsageError, match="not a number"):
        config.getint("display", "dpi")


@pytest.mark.parametrize("dotted", ["wine.nope", "nosuch.key"])
def test_unknown_keys_are_usage_errors(paths: Paths, dotted: str) -> None:
    config = Config.load(paths, env={})
    section, _, key = dotted.partition(".")
    for call in (config.get, config.raw, config.source, config.unset):
        with pytest.raises(UsageError) as info:
            call(section, key)
        assert "config list" in info.value.hint
    with pytest.raises(UsageError):
        config.set(section, key, "x")


def test_parse_key() -> None:
    assert config_mod.parse_key("wine.provider") == ("wine", "provider")
    assert config_mod.parse_key(" Wine.Provider ") == ("wine", "provider")
    with pytest.raises(UsageError, match="SECTION.KEY"):
        config_mod.parse_key("provider")
    with pytest.raises(UsageError, match="unknown setting"):
        config_mod.parse_key("wine.colour")


def test_items_cover_every_setting(paths: Paths) -> None:
    _write(paths, "[wine]\nprovider = system\n")
    items = Config.load(paths, env={}).items()
    assert len(items) == sum(len(keys) for keys in SCHEMA.values())
    assert items[0] == ("wine", "provider", "system", "file")
    assert ("ui", "progress", "auto", "default") in items


def test_constructor_without_env() -> None:
    config = Config(Path("/nonexistent/config.ini"))
    assert config.get("wine", "provider") == "managed"


# --- set / unset ---------------------------------------------------------------------


def test_set_on_missing_file_seeds_the_commented_template(paths: Paths) -> None:
    config = Config.load(paths, env={})
    config.set("wine", "provider", " system ")
    text = paths.config_file.read_text(encoding="utf-8")
    assert text.startswith(config_mod.render_defaults().split("[wine]")[0])
    assert "# provider = managed\nprovider = system\n" in text
    assert stat.S_IMODE(paths.config_file.stat().st_mode) == 0o600
    assert config.get("wine", "provider") == "system"
    assert config.source("wine", "provider") == "file"
    reloaded = Config.load(paths, env={})
    assert reloaded.get("wine", "provider") == "system"
    assert reloaded.problems() == []


def test_set_preserves_user_comments(paths: Paths) -> None:
    _write(paths, "# mine\n[wine]\n# my note\nprovider = system\n")
    Config.load(paths, env={}).set("wine", "provider", "managed")
    assert paths.config_file.read_text(encoding="utf-8") == "# mine\n[wine]\n# my note\nprovider = managed\n"


def test_set_rejects_invalid_values_without_writing(paths: Paths) -> None:
    config = Config.load(paths, env={})
    with pytest.raises(UsageError, match="expected gdi, gl, vulkan"):
        config.set("wine", "renderer", "directx")
    with pytest.raises(UsageError, match="single line"):
        config.set("wine", "debug", "a\nb")
    with pytest.raises(UsageError, match="single line"):
        config.set("wine", "debug", "a\rb")
    assert not paths.config_file.exists()


def test_set_empty_value_is_allowed_for_strings(paths: Paths) -> None:
    config = Config.load(paths, env={})
    config.set("wine", "debug", "")
    assert Config.load(paths, env={}).get("wine", "debug") == ""


def test_set_write_failure_is_a_fork_linux_error(paths: Paths, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: object, **kwargs: object) -> None:
        raise PermissionError("read-only")

    monkeypatch.setattr(config_mod.config_edit, "set_option", boom)
    with pytest.raises(ForkLinuxError, match="cannot write"):
        Config.load(paths, env={}).set("wine", "debug", "+seh")


def test_unset(paths: Paths) -> None:
    _write(paths, "[wine]\nprovider = system\n")
    config = Config.load(paths, env={})
    assert config.unset("wine", "provider") is True
    assert config.get("wine", "provider") == "managed"
    assert config.source("wine", "provider") == "default"
    assert config.unset("wine", "provider") is False
    assert paths.config_file.read_text(encoding="utf-8") == "[wine]\n"


def test_unset_without_a_file(paths: Paths) -> None:
    assert Config.load(paths, env={}).unset("wine", "provider") is False
    assert not paths.config_file.exists()


def test_unset_write_failure_is_a_fork_linux_error(paths: Paths, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: object, **kwargs: object) -> bool:
        raise PermissionError("read-only")

    monkeypatch.setattr(config_mod.config_edit, "unset_option", boom)
    with pytest.raises(ForkLinuxError, match="cannot write"):
        Config.load(paths, env={}).unset("wine", "debug")
