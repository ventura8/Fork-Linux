"""Tests for the fork-linux CLI: routing, global options, exit codes and the phase-2 commands."""

from __future__ import annotations

import importlib
import json
import logging
import os
import runpy
import subprocess
import sys
from pathlib import Path

import pytest

from fork_linux import APP_NAME, cli, commands, credits, errors
from fork_linux import config as config_mod
from fork_linux.commands import config as config_cmd
from fork_linux.commands import version as version_cmd
from fork_linux.paths import Paths
from fork_linux.procrun import Completed, RecordingRunner, Runner
from fork_linux.state import State

ROOT = Path(__file__).resolve().parents[1]
REAL_IS_ROOT = cli._is_root


@pytest.fixture(autouse=True)
def _isolated(xdg: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Fake HOME/XDG dirs, never root (CI containers run tests as root), no editor variables."""
    monkeypatch.setattr(cli, "_is_root", lambda: False)
    for name in ("VISUAL", "EDITOR", "FORK_LINUX_ALLOW_ROOT"):
        monkeypatch.delenv(name, raising=False)
    for name in list(os.environ):
        if name.startswith("FORK_LINUX_"):
            monkeypatch.delenv(name, raising=False)
    return xdg


def run_cli(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = cli.main(list(argv), prog="fork-linux")
    out, err = capsys.readouterr()
    return code, out, err


def _paths() -> Paths:
    return Paths.from_env()


def _ctx(argv: list[str], runner: Runner | None = None, env: dict[str, str] | None = None) -> cli.AppContext:
    args = cli.build_parser().parse_args(argv)
    return cli.AppContext(args, env=env, runner=runner)


# --- routing, help, version -------------------------------------------------------------------


def test_help_lists_every_command_and_the_credits(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _err = run_cli(capsys, "--help")
    assert code == 0
    assert out.startswith("usage: fork-linux")
    for name in commands.COMMANDS:
        assert name in out
    assert "credits" in out
    assert credits.short_footer() in out
    assert "NOT affiliated" in out
    for flag in ("--json", "--prefix", "--gui", "--no-gui", "--offline", "--verbose", "--quiet", "--allow-root"):
        assert flag in out


def test_command_help_shows_global_options(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _err = run_cli(capsys, "status", "--help")
    assert code == 0
    assert "usage: fork-linux status" in out
    assert "--json" in out


@pytest.mark.parametrize("flag", ["-V", "--version"])
def test_version_flag(capsys: pytest.CaptureFixture[str], flag: str) -> None:
    code, out, _err = run_cli(capsys, flag)
    assert code == 0
    assert out.strip() == cli.version_line()
    assert cli.version_line().startswith("fork-linux ")


def test_no_command_prints_help_and_exits_2(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, err = run_cli(capsys)
    assert code == errors.ExitCode.USAGE == 2
    assert out == ""
    assert "usage: fork-linux" in err


def test_unknown_command_and_option_exit_2(capsys: pytest.CaptureFixture[str]) -> None:
    assert run_cli(capsys, "frobnicate")[0] == 2
    assert run_cli(capsys, "--frobnicate", "version")[0] == 2
    assert run_cli(capsys, "config")[0] == 2  # an action is required


def test_main_reads_sys_argv_by_default(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["/usr/bin/fork-linux", "version"])
    assert cli.main() == 0
    assert capsys.readouterr().out.startswith("fork-linux ")


def test_fork_program_name_dispatches_to_fork_cli(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["--help"], prog="/usr/bin/fork") == 0
    assert capsys.readouterr().out.startswith("usage: fork [OPTIONS]")


def test_fork_program_name_from_sys_argv(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["/home/u/.local/bin/fork", "--version"])
    assert cli.main() == 0
    assert capsys.readouterr().out.strip() == cli.version_line()


def test_exit_status_of_system_exit() -> None:
    assert cli._exit_status(None) == 0
    assert cli._exit_status(2) == 2
    assert cli._exit_status("message") == 1


# --- root refusal -------------------------------------------------------------------------------


def test_root_is_refused(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, xdg: Path) -> None:
    monkeypatch.setattr(cli, "_is_root", lambda: True)
    code, out, err = run_cli(capsys, "version")
    assert code == errors.ExitCode.UNSUPPORTED_ENV == 19
    assert out == ""
    assert "fork-linux: error: refusing to run as root" in err
    assert "hint: " in err and "--allow-root" in err
    assert not (xdg / ".local/state/fork-linux").exists(), "nothing may be written before the root check"


@pytest.mark.parametrize("argv", [["--allow-root", "version"], ["version", "--allow-root"]])
def test_allow_root_flag(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> None:
    monkeypatch.setattr(cli, "_is_root", lambda: True)
    assert run_cli(capsys, *argv)[0] == 0


def test_allow_root_env(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "_is_root", lambda: True)
    monkeypatch.setenv("FORK_LINUX_ALLOW_ROOT", "1")
    assert run_cli(capsys, "version")[0] == 0


def test_is_root_reflects_euid() -> None:
    assert REAL_IS_ROOT() is (os.geteuid() == 0)


# --- error handling -------------------------------------------------------------------------------

ERROR_CASES = [
    errors.ForkLinuxError("plain failure", hint="try again"),
    errors.UsageError("bad usage", hint="see --help"),
    errors.NotSetUpError("not set up", hint="run setup"),
    errors.SetupFailed("dotnet", "winetricks failed", hint="see the log"),
    errors.DownloadFailed("offline", hint="check the network"),
    errors.IntegrityFailed("sha256 mismatch", hint="retry"),
    errors.WineUnavailable("no wine", hint="install wine"),
    errors.ForkRunning("Fork is running", hint="close it"),
    errors.Locked("busy", hint="wait"),
    errors.ChecksFailed("2 checks failed", hint="doctor --fix"),
    errors.Declined("declined", hint="accept the EULA"),
    errors.UnsupportedEnvironment("arm64", hint="x86_64 only"),
    errors.NotFound("no snapshot 7", hint="snapshot list"),
]


@pytest.mark.parametrize("exc", ERROR_CASES, ids=lambda e: type(e).__name__)
def test_errors_map_to_exit_codes_and_print_hints(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, exc: errors.ForkLinuxError
) -> None:
    def fail(args: object, ctx: object) -> int:
        raise exc

    monkeypatch.setattr(version_cmd, "run", fail)
    code, out, err = run_cli(capsys, "version")
    assert code == int(exc.exit_code)
    assert out == ""
    assert f"fork-linux: error: {exc.message}\n" in err
    assert f"hint: {exc.hint}\n" in err


def test_error_without_hint_prints_no_hint_line(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(args: object, ctx: object) -> int:
        raise errors.ForkLinuxError("bare")

    monkeypatch.setattr(version_cmd, "run", fail)
    code, _out, err = run_cli(capsys, "version")
    assert code == 1
    assert err == "fork-linux: error: bare\n"


def test_keyboard_interrupt_exits_130(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    def interrupted(args: object, ctx: object) -> int:
        raise KeyboardInterrupt

    monkeypatch.setattr(version_cmd, "run", interrupted)
    code, _out, err = run_cli(capsys, "version")
    assert code == errors.ExitCode.INTERRUPTED == 130
    assert "interrupted" in err


def test_unexpected_exception_is_logged_with_traceback(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def crash(args: object, ctx: object) -> int:
        raise RuntimeError("kaboom")

    monkeypatch.setattr(version_cmd, "run", crash)
    code, _out, err = run_cli(capsys, "version")
    assert code == 1
    assert "fork-linux: error: unexpected RuntimeError: kaboom" in err
    log_file = _paths().logs_dir / "fork-linux.log"
    assert f"the details are in {log_file}" in err
    assert credits.LINKS["project_issues"] in err
    text = log_file.read_text(encoding="utf-8")
    assert "Traceback" in text and "kaboom" in text
    assert not [h for h in logging.getLogger("fork_linux").handlers if getattr(h, "_fork_linux_handler", False)]


def test_unexpected_exception_with_unusable_paths(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, xdg: Path
) -> None:
    def crash(args: object, ctx: object) -> int:
        raise RuntimeError("kaboom")

    monkeypatch.setattr(version_cmd, "run", crash)
    monkeypatch.setenv("FORK_LINUX_PREFIX", str(xdg / ".wine"))
    code, _out, err = run_cli(capsys, "version")
    assert code == 1
    assert "the details are in" not in err
    assert "please report it" in err


def test_logging_without_a_file_handler(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    logger = logging.getLogger("fork_linux.tests.console_only")
    logger.handlers[:] = [logging.NullHandler()]
    monkeypatch.setattr(cli.logging_setup, "configure", lambda paths, verbose: logger)

    def crash(args: object, ctx: object) -> int:
        raise RuntimeError("kaboom")

    monkeypatch.setattr(version_cmd, "run", crash)
    code, _out, err = run_cli(capsys, "version")
    assert code == 1
    assert "the details are in" not in err


def test_handler_returning_none_means_success(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(version_cmd, "run", lambda args, ctx: None)
    assert run_cli(capsys, "version")[0] == 0


def test_forbidden_prefix_is_a_usage_error(capsys: pytest.CaptureFixture[str], xdg: Path) -> None:
    code, _out, err = run_cli(capsys, "--prefix", str(xdg / ".wine"), "status")
    assert code == 2
    assert "refusing to use" in err
    assert "hint: " in err


# --- AppContext ---------------------------------------------------------------------------------


def test_app_context_flags_and_lazy_members(xdg: Path) -> None:
    ctx = _ctx(["-v", "-v", "status", "-q", "--no-gui", "--offline", "--json"])
    assert ctx.verbosity == 1
    assert ctx.gui is False
    assert ctx.offline is True
    assert ctx.json is True
    assert isinstance(ctx.runner, Runner)
    assert ctx.paths is ctx.paths
    assert ctx.paths.prefix == xdg / ".local/share/fork-linux/prefix"
    assert ctx.config is ctx.config
    assert ctx.env["HOME"] == str(xdg)


def test_app_context_defaults() -> None:
    ctx = _ctx(["version"], env={"HOME": "/nowhere"})
    assert ctx.gui is None
    assert ctx.json is False
    assert ctx.offline is False
    assert ctx.verbosity == 0
    assert ctx.env == {"HOME": "/nowhere"}


def test_prefix_option(xdg: Path) -> None:
    prefix = xdg / ".local/share/fork-linux/other-prefix"
    ctx = _ctx(["status", "--prefix", str(prefix)])
    assert ctx.paths.prefix == prefix


# --- version ------------------------------------------------------------------------------------


def test_version_command(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _err = run_cli(capsys, "version")
    assert code == 0
    lines = out.splitlines()
    assert lines[0] == cli.version_line()
    assert lines[1] == credits.short_footer()


@pytest.mark.parametrize("argv", [["--json", "version"], ["version", "--json"]])
def test_version_json(capsys: pytest.CaptureFixture[str], argv: list[str]) -> None:
    code, out, _err = run_cli(capsys, *argv)
    assert code == 0
    data = json.loads(out)
    assert set(data) == {"fork_linux", "flavor"}
    assert cli.version_line() == f"fork-linux {data['fork_linux']} ({data['flavor']})"


# --- about / credits -------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["about", "credits"])
def test_about_and_credits_alias(capsys: pytest.CaptureFixture[str], name: str) -> None:
    code, out, _err = run_cli(capsys, name)
    assert code == 0
    assert out == credits.render_text()


def test_about_json(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _err = run_cli(capsys, "about", "--json")
    assert code == 0
    data = json.loads(out)
    assert data["app"] == APP_NAME
    assert data["developers"] == list(credits.DEVELOPERS)
    assert data["links"] == credits.LINKS
    assert len(data["prior_art"]) == len(credits.PRIOR_ART)
    assert "NOT affiliated" in data["disclaimer"]


def test_about_gui_uses_zenity(capsys: pytest.CaptureFixture[str]) -> None:
    runner = RecordingRunner(which_map={"zenity": "/usr/bin/zenity"})
    ctx = _ctx(["about", "--gui"], runner=runner, env={"PATH": "/usr/bin", "LD_PRELOAD": "/evil.so"})
    assert ctx.args.func(ctx.args, ctx) == 0
    (call,) = runner.calls
    assert call["argv"][:3] == ["/usr/bin/zenity", "--info", "--no-markup"]
    assert call["argv"][-1] == "--text=" + credits.render_text()
    assert "LD_PRELOAD" not in call["env"]
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    ("which_map", "responses"),
    [({"zenity": None}, {}), ({"zenity": "/usr/bin/zenity"}, {"zenity": Completed([], 5, "", "no display")})],
)
def test_about_gui_falls_back_to_text(
    capsys: pytest.CaptureFixture[str], which_map: dict[str, str | None], responses: dict[str, Completed]
) -> None:
    runner = RecordingRunner(responses, which_map=which_map)
    ctx = _ctx(["about", "--gui"], runner=runner)
    assert ctx.args.func(ctx.args, ctx) == 0
    assert capsys.readouterr().out == credits.render_text()


# --- status ---------------------------------------------------------------------------------------


def test_status_before_setup(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _err = run_cli(capsys, "status")
    assert code == 0
    assert out.startswith(cli.version_line() + "\n")
    assert "not set up - run 'fork-linux setup'" in out
    assert "not installed" in out
    assert "managed - configured, not set up yet" in out
    assert "(not created yet)" in out
    assert "defaults in use" in out
    assert "Packaging" not in out


def test_status_after_setup(capsys: pytest.CaptureFixture[str]) -> None:
    paths = _paths()
    state = State.load(paths.state_file)
    state.set("setup.complete", True)
    state.set("fork.version", "2.23.2")
    state.set("wine.provider", "managed")
    state.set("wine.build", "kron4ek-11.0-staging-wow64")
    state.set_step_marker("preflight", 1, "h")
    state.save()
    paths.config_file.parent.mkdir(parents=True, exist_ok=True)
    paths.config_file.write_text("[wine]\nprovider = system\n", encoding="utf-8")
    code, out, _err = run_cli(capsys, "status")
    assert code == 0
    assert "Setup:     complete" in out
    assert "Fork:      2.23.2" in out
    assert "Wine:      managed (kron4ek-11.0-staging-wow64)\n" in out
    assert "(not created yet)" not in out
    assert "(exists)" in out


def test_status_partial_setup_and_json(capsys: pytest.CaptureFixture[str]) -> None:
    paths = _paths()
    state = State.load(paths.state_file)
    state.set_step_marker("preflight", 1, "a")
    state.set_step_marker("consent", 1, "b")
    state.save()
    code, out, _err = run_cli(capsys, "status")
    assert code == 0
    assert "incomplete (2 steps done)" in out
    code, out, _err = run_cli(capsys, "status", "--json")
    data = json.loads(out)
    assert data["schema"] == 1
    assert data["setup_complete"] is False
    assert data["steps_done"] == ["consent", "preflight"]
    assert data["fork_version"] is None
    assert data["wine_provider"] == "managed"
    assert data["wine_provider_source"] == "config"
    assert data["prefix_exists"] is True
    assert data["config_error"] is None
    assert data["state_file"] == str(paths.state_file)


def test_status_with_broken_config_and_sandbox(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _paths()
    paths.config_file.parent.mkdir(parents=True, exist_ok=True)
    paths.config_file.write_text("garbage without a section\n", encoding="utf-8")
    monkeypatch.setenv("FLATPAK_ID", "io.github.ventura8.ForkLinux")
    code, out, _err = run_cli(capsys, "status")
    assert code == 0
    assert "Wine:      unknown" in out
    assert "error: cannot parse" in out
    assert "Packaging: flatpak" in out


def test_status_with_corrupt_state_exits_13(capsys: pytest.CaptureFixture[str]) -> None:
    paths = _paths()
    paths.state_file.parent.mkdir(parents=True)
    paths.state_file.write_text("{broken", encoding="utf-8")
    code, _out, err = run_cli(capsys, "status")
    assert code == errors.ExitCode.INTEGRITY_FAILED
    assert "hint: move" in err


# --- config ---------------------------------------------------------------------------------------


def test_config_get_set_unset_cycle(capsys: pytest.CaptureFixture[str]) -> None:
    paths = _paths()
    assert run_cli(capsys, "config", "get", "wine.provider") == (0, "managed\n", "")
    code, out, _err = run_cli(capsys, "config", "set", "wine.provider", "system")
    assert (code, out) == (0, "wine.provider = system\n")
    assert "provider = system\n" in paths.config_file.read_text(encoding="utf-8")
    assert run_cli(capsys, "config", "get", "wine.provider")[1] == "system\n"
    code, out, _err = run_cli(capsys, "config", "unset", "wine.provider")
    assert (code, out) == (0, "wine.provider is back to its default: 'managed'\n")
    code, out, _err = run_cli(capsys, "config", "unset", "wine.provider")
    assert code == 0 and "was not set" in out


def test_config_set_value_starting_with_dash_and_spaces(capsys: pytest.CaptureFixture[str]) -> None:
    assert run_cli(capsys, "config", "set", "wine.debug", "-all")[0] == 0
    assert run_cli(capsys, "config", "get", "wine.debug")[1] == "-all\n"
    assert run_cli(capsys, "config", "set", "fork.enforce_settings", "A,", "B")[0] == 0
    assert run_cli(capsys, "config", "get", "fork.enforce_settings")[1] == "A, B\n"


def test_config_json_outputs(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _err = run_cli(capsys, "--json", "config", "get", "display.dpi")
    assert json.loads(out) == {"key": "display.dpi", "value": "auto", "source": "default", "default": "auto"}
    code, out, _err = run_cli(capsys, "--json", "config", "set", "display.dpi", "144")
    assert json.loads(out)["source"] == "file"
    code, out, _err = run_cli(capsys, "--json", "config", "unset", "display.dpi")
    data = json.loads(out)
    assert data["removed"] is True and data["value"] == "auto"
    code, out, _err = run_cli(capsys, "--json", "config", "list")
    rows = json.loads(out)
    assert len(rows) == sum(len(keys) for keys in config_mod.SCHEMA.values())
    code, out, _err = run_cli(capsys, "--json", "config", "path")
    assert json.loads(out) == {"path": str(_paths().config_file), "exists": True}


def test_config_set_errors(capsys: pytest.CaptureFixture[str]) -> None:
    code, _out, err = run_cli(capsys, "config", "set", "wine.provider")
    assert code == 2 and "missing VALUE" in err
    code, _out, err = run_cli(capsys, "config", "set", "wine.renderer", "directx")
    assert code == 2 and "expected gdi, gl, vulkan" in err
    code, _out, err = run_cli(capsys, "config", "get", "wine.colour")
    assert code == 2 and "unknown setting" in err
    code, _out, err = run_cli(capsys, "config", "get", "provider")
    assert code == 2 and "SECTION.KEY" in err


def test_config_set_warns_about_env_override(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FORK_LINUX_WINE_PROVIDER", "flatpak")
    code, out, err = run_cli(capsys, "config", "set", "wine.provider", "system")
    assert code == 0
    assert "$FORK_LINUX_WINE_PROVIDER is set and overrides this value" in err
    assert out == "wine.provider = flatpak\n"


def test_config_get_warns_about_invalid_values(capsys: pytest.CaptureFixture[str]) -> None:
    paths = _paths()
    paths.config_file.parent.mkdir(parents=True, exist_ok=True)
    paths.config_file.write_text("[wine]\nrenderer = directx\nmystery = 1\n", encoding="utf-8")
    code, out, err = run_cli(capsys, "config", "get", "wine.renderer")
    assert code == 0
    assert out == "directx\n"
    assert "invalid value" in err
    code, out, err = run_cli(capsys, "config", "list")
    assert code == 0
    assert "wine.renderer" in out and "directx  (file)" in out
    assert err.count("unknown setting wine.mystery") == 1


def test_config_list_marks_overrides(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FORK_LINUX_UI_PROGRESS", "terminal")
    code, out, _err = run_cli(capsys, "config", "list")
    assert code == 0
    lines = out.splitlines()
    assert len(lines) == sum(len(keys) for keys in config_mod.SCHEMA.values())
    assert any(line.startswith("ui.progress") and line.endswith("= terminal  (env)") for line in lines)
    assert any(line.startswith("wine.build") and line.endswith("=") for line in lines)


def test_config_path(capsys: pytest.CaptureFixture[str]) -> None:
    assert run_cli(capsys, "config", "path") == (0, f"{_paths().config_file}\n", "")


# --- config edit ------------------------------------------------------------------------------------


def test_config_edit_creates_template_and_runs_editor(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[list[str], dict[str, str]]] = []

    def fake_editor(argv: list[str], env: dict[str, str]) -> int:
        calls.append((list(argv), dict(env)))
        Path(argv[-1]).write_text("[wine]\nrenderer = directx\n", encoding="utf-8")
        return 0

    monkeypatch.setattr(config_cmd, "run_interactive", fake_editor)
    monkeypatch.setenv("EDITOR", "code --wait")
    monkeypatch.setenv("LD_LIBRARY_PATH", "/bundle/lib")
    path = _paths().config_file
    code, _out, err = run_cli(capsys, "config", "edit")
    assert code == 0
    ((argv, env),) = calls
    assert argv == ["code", "--wait", str(path)]
    assert "LD_LIBRARY_PATH" not in env
    assert "invalid value 'directx'" in err


def test_config_edit_template_is_written_before_the_editor(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def fake_editor(argv: list[str], env: dict[str, str]) -> int:
        seen.append(Path(argv[-1]).read_text(encoding="utf-8"))
        return 0

    monkeypatch.setattr(config_cmd, "run_interactive", fake_editor)
    monkeypatch.setenv("VISUAL", "myeditor")
    monkeypatch.setenv("EDITOR", "ignored")
    assert cli.main(["config", "edit"], prog="fork-linux") == 0
    assert seen == [config_mod.template()]
    assert oct(_paths().config_file.stat().st_mode & 0o777) == oct(config_mod.FILE_MODE)


def test_config_edit_keeps_existing_file(monkeypatch: pytest.MonkeyPatch) -> None:
    path = _paths().config_file
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# mine\n", encoding="utf-8")
    monkeypatch.setattr(config_cmd, "run_interactive", lambda argv, env: 0)
    monkeypatch.setenv("EDITOR", "vi")
    assert cli.main(["config", "edit"], prog="fork-linux") == 0
    assert path.read_text(encoding="utf-8") == "# mine\n"


def test_config_edit_failures(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("EDITOR", "vi")
    monkeypatch.setattr(config_cmd, "run_interactive", lambda argv, env: 3)
    code, _out, err = run_cli(capsys, "config", "edit")
    assert code == 1 and "exited with status 3" in err

    def broken_editor(argv: list[str], env: dict[str, str]) -> int:
        Path(argv[-1]).write_text("not an ini file\n", encoding="utf-8")
        return 0

    monkeypatch.setattr(config_cmd, "run_interactive", broken_editor)
    code, _out, err = run_cli(capsys, "config", "edit")
    assert code == 2 and "cannot parse" in err

    monkeypatch.setenv("EDITOR", "vi 'unterminated")
    code, _out, err = run_cli(capsys, "config", "edit")
    assert code == 2 and "cannot parse $EDITOR" in err

    monkeypatch.delenv("EDITOR")
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    code, _out, err = run_cli(capsys, "config", "edit")
    assert code == 2 and "no text editor found" in err


def test_editor_argv_fallbacks() -> None:
    found = {"nano": "/usr/bin/nano"}
    assert config_cmd.editor_argv({"VISUAL": "  ", "EDITOR": ""}, found.get) == ["/usr/bin/nano"]
    assert config_cmd.editor_argv({}, {}.get) is None
    assert config_cmd.editor_argv({"EDITOR": "emacs -nw"}, found.get) == ["emacs", "-nw"]


def test_run_interactive_statuses(tmp_path: Path) -> None:
    env = dict(os.environ)
    assert config_cmd.run_interactive([sys.executable, "-c", "raise SystemExit(3)"], env) == 3
    assert config_cmd.run_interactive([str(tmp_path / "missing-editor")], env) == 127
    not_executable = tmp_path / "plain.txt"
    not_executable.write_text("x", encoding="utf-8")
    assert config_cmd.run_interactive([str(not_executable)], env) == 126


# --- python -m fork_linux ------------------------------------------------------------------------------


def test_module_entry_point_in_process(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["fork_linux", "about"])
    with pytest.raises(SystemExit) as info:
        runpy.run_module("fork_linux", run_name="__main__")
    assert info.value.code == 0
    assert credits.LINKS["buy"] in capsys.readouterr().out


def test_importing_main_module_does_not_run_the_cli(capsys: pytest.CaptureFixture[str]) -> None:
    sys.modules.pop("fork_linux.__main__", None)
    module = importlib.import_module("fork_linux.__main__")
    assert module.main is cli.main
    assert capsys.readouterr() == ("", "")


def test_module_entry_point_subprocess(xdg: Path) -> None:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(xdg),
        "PYTHONPATH": str(ROOT / "src"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "FORK_LINUX_ALLOW_ROOT": "1",
    }
    result = subprocess.run(
        [sys.executable, "-m", "fork_linux", "about"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "Dan Pristupov" in result.stdout
    assert credits.LINKS["buy"] in result.stdout
