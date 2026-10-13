"""scripts/packaging-e2e-state.py: the simulated setup the packaging E2E upgrades over (scenario A2).

The script is loaded with importlib (its name is hyphenated). The main test drives it the way
scripts/packaging-e2e-install.sh does, against this tree's own ``fork-linux status --json`` and
``setup --list-steps --json`` in an isolated home: the simulated setup must look complete to
the release that recorded it, and a release with other shims must re-run ``host_shims`` only.
"""

from __future__ import annotations

import importlib.util
import json
import os
import runpy
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest
from fixtures.cli_run import isolate, run_cli

from fork_linux import resources

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "packaging-e2e-state.py"
MANIFEST = resources.manifest_path()


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("packaging_e2e_state", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


state_tool = _load()


def _cli_json(capsys: pytest.CaptureFixture[str], path: Path, *argv: str) -> Path:
    code, out, err = run_cli(capsys, *argv, "--json")
    assert code == 0, err
    path.write_text(out, encoding="utf-8")
    return path


def _tool(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = state_tool.main([str(arg) for arg in argv])
    out, err = capsys.readouterr()
    return code, out, err


@pytest.fixture
def seeded(xdg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> Path:
    """The simulated setup of this tree, recorded from its own step list; returns the work directory."""
    isolate(monkeypatch)
    shims = tmp_path / "shims"
    shims.mkdir()
    for name in ("fl-shim.exe", "fl-launch.exe", "fl-filedlg.dll"):
        (shims / name).write_bytes(b"MZ shim 0.9.9 " + name.encode())
    monkeypatch.setenv("FORK_LINUX_SHIMS_DIR", str(shims))
    work = tmp_path / "work"
    work.mkdir()
    status = _cli_json(capsys, work / "status.json", "status")
    assert _tool(capsys, "prepare", status, MANIFEST)[0] == 0
    steps = _cli_json(capsys, work / "steps-seed.json", "setup", "--list-steps")
    code, out, _err = _tool(capsys, "record", status, steps)
    assert code == 0
    assert "recorded in" in out
    return work


def test_the_recorded_release_reruns_nothing(seeded: Path, capsys: pytest.CaptureFixture[str]) -> None:
    steps = _cli_json(capsys, seeded / "steps.json", "setup", "--list-steps")
    listed = json.loads(steps.read_text(encoding="utf-8"))
    assert listed["needs_setup"] is False
    assert {row["status"] for row in listed["steps"] if row["id"] == "fork_install"} == {"done"}
    code, out, err = _tool(capsys, "check", steps, "")
    assert (code, err) == (0, "")
    assert out.startswith("ok  next launch re-runs nothing;")
    assert _tool(capsys, "check", steps, "host_shims")[0] == 1


def test_another_release_reruns_only_its_shims(
    seeded: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    for path in (tmp_path / "shims").iterdir():
        path.write_bytes(path.read_bytes().replace(b"0.9.9", b"1.0.0"))
    steps = _cli_json(capsys, seeded / "steps.json", "setup", "--list-steps")
    code, out, err = _tool(capsys, "check", steps, "host_shims")
    assert (code, err) == (0, "")
    assert out.startswith("ok  next launch re-runs host_shims;")
    code, _out, err = _tool(capsys, "check", steps, "")
    assert code == 1
    assert "steps the next launch re-runs: ['host_shims'], expected []" in err


def test_check_reports_downloads_needs_setup_and_a_missing_fork(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    listed = {
        "needs_setup": False,
        "steps": [
            {"id": "wine_runtime", "needs_network": True, "recorded": False, "status": "pending"},
            {"id": "fork_install", "needs_network": False, "recorded": True, "status": "pending"},
            {"id": "finalize", "needs_network": False, "recorded": None, "status": "always"},
        ],
    }
    steps = tmp_path / "steps.json"
    steps.write_text(json.dumps(listed), encoding="utf-8")
    code, _out, err = _tool(capsys, "check", steps, "wine_runtime")
    assert code == 1
    assert "would download again: ['wine_runtime']" in err
    assert "needs_setup is False with ['wine_runtime'] to re-run" in err
    assert "fork_install is 'pending'" in err
    assert "steps the next launch re-runs" not in err


def test_digest_follows_the_seeded_files(seeded: Path, capsys: pytest.CaptureFixture[str]) -> None:
    status = seeded / "status.json"
    code, first, _err = _tool(capsys, "digest", status)
    assert code == 0
    digest, count = first.split()
    assert len(digest) == 64
    assert int(count) == 7  # created-by, state.json, Fork.exe, sq.version, wine, wineserver, .complete
    assert _tool(capsys, "digest", status)[1] == first
    prefix = Path(json.loads(status.read_text(encoding="utf-8"))["prefix"])
    marker = prefix / ".fork-linux" / "created-by"
    marker.write_text(marker.read_text(encoding="utf-8") + "changed\n", encoding="utf-8")
    assert _tool(capsys, "digest", status)[1] != first


def test_markers_and_the_ones_a_remove_cleared(seeded: Path, capsys: pytest.CaptureFixture[str]) -> None:
    status = seeded / "status.json"
    code, out, _err = _tool(capsys, "markers", status)
    assert code == 0
    recorded = out.strip()
    assert "desktop_entry" in recorded.split(",") and "finalize" not in recorded.split(",")
    assert _tool(capsys, "missing", status, recorded)[1] == "\n"
    state_file = Path(json.loads(status.read_text(encoding="utf-8"))["state_file"])
    state = json.loads(state_file.read_text(encoding="utf-8"))
    for step in ("icon", "desktop_entry"):
        del state["steps"][step]
    state_file.write_text(json.dumps(state), encoding="utf-8")
    assert _tool(capsys, "missing", status, recorded)[1] == "desktop_entry,icon\n"
    state_file.write_text(json.dumps({"steps": []}), encoding="utf-8")
    code, _out, err = _tool(capsys, "markers", status)
    assert code == 1
    assert "no step markers" in err


def _default_wine_version() -> str:
    """The version of the manifest's default Wine build (the stand-in reports it)."""
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    return str(data["wine"]["builds"][data["wine"]["default"]]["version"])


def test_the_stand_in_wine_answers_version_only(seeded: Path, capsys: pytest.CaptureFixture[str]) -> None:
    status = seeded / "status.json"
    data_dir = Path(json.loads(status.read_text(encoding="utf-8"))["prefix"]).parent
    (wine,) = data_dir.glob("runtimes/wine/*/bin/wine")
    assert os.access(wine, os.X_OK)
    version = subprocess.run([str(wine), "--version"], capture_output=True, text=True, check=False, timeout=30)
    assert (version.returncode, version.stdout) == (0, f"wine-{_default_wine_version()} (Staging)\n")
    assert _tool(capsys, "digest", status)[0] == 0
    ran = subprocess.run([str(wine), "wineboot", "--init"], capture_output=True, check=False, timeout=30)
    assert ran.returncode == 97
    code, _out, err = _tool(capsys, "digest", status)
    assert code == 1
    assert "Wine was run without a real setup:" in err
    assert "wineboot --init" in err


def test_a_runtime_without_staging_says_so(xdg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USER", "tester")
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    build = manifest["wine"]["builds"][manifest["wine"]["default"]]
    build["flavor"] = "vanilla"
    custom = tmp_path / "manifest.json"
    custom.write_text(json.dumps(manifest), encoding="utf-8")
    status = tmp_path / "status.json"
    prefix = tmp_path / "p" / "prefix"
    status.write_text(json.dumps({
        "prefix": str(prefix),
        "state_file": str(prefix / ".fork-linux" / "state.json"),
        "fork_linux": "1",
    }))
    layout = state_tool.prepare(status, custom)
    wine = layout.runtime(manifest["wine"]["default"]) / "bin" / "wine"
    assert f'echo "wine-{build["version"]}"' in wine.read_text(encoding="utf-8")


def test_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    status = tmp_path / "status.json"
    prefix = tmp_path / "data" / "prefix"
    status.write_text(json.dumps({
        "prefix": str(prefix),
        "state_file": str(prefix / ".fork-linux" / "state.json"),
        "fork_linux": "1",
    }))
    monkeypatch.delenv("USER", raising=False)
    code, _out, err = _tool(capsys, "digest", status)
    assert code == 1
    assert "USER is not set" in err
    monkeypatch.setenv("USER", "tester")
    code, _out, err = _tool(capsys, "digest", status)
    assert code == 1
    assert "nothing seeded under" in err
    listing = tmp_path / "list.json"
    listing.write_text("[]", encoding="utf-8")
    code, _out, err = _tool(capsys, "check", listing, "")
    assert code == 1
    assert "not a JSON object" in err
    listing.write_text(json.dumps({"steps": []}), encoding="utf-8")
    code, _out, err = _tool(capsys, "record", status, listing)
    assert code == 1
    assert "no steps listed" in err
    code, _out, err = _tool(capsys, "prepare", status, tmp_path / "missing.json")
    assert code == 1
    assert "FileNotFoundError" in err
    with pytest.raises(SystemExit):
        state_tool.main([])
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "digest", str(status)])
    with pytest.raises(SystemExit) as exited:
        runpy.run_path(str(SCRIPT), run_name="__main__")
    assert exited.value.code == 1


def test_placeholder_fork_exe_is_a_pe_without_an_icon(tmp_path: Path) -> None:
    """desktop install finds no icon in the placeholder and keeps the neutral one, without a warning."""
    from fork_linux import icon_extract, pe_resources

    data = state_tool.placeholder_exe()
    pe = pe_resources.PEFile(data)
    assert (pe.machine, pe.is_pe32_plus, pe.subsystem) == (pe_resources.MACHINE_AMD64, True, 2)
    assert pe.sections == [] and pe.best_icons() == [] and pe.version_info() == {}
    exe = tmp_path / "Fork.exe"
    exe.write_bytes(data)
    assert icon_extract.extract_icons(exe, tmp_path / "icons", "app") == []
