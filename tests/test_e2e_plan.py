"""Tests for scripts/e2e-plan.py: the order, roots and template key of a parallel real-tier run.

The helper is loaded with importlib (its name is hyphenated). Contract tests keep its table of
``fresh`` modules, ``tests/e2e/timings.json`` and the CI shards of e2e-wine.yml in step with
``tests/e2e``: every module has a timing, a ``fresh`` module never uses the main prefix, a
``template`` module does, and every CI shard is estimated to finish within the hour.
"""

from __future__ import annotations

import importlib.util
import json
import re
import runpy
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "e2e-plan.py"
E2E = REPO / "tests" / "e2e"
WORKFLOW = REPO / ".github" / "workflows" / "e2e-wine.yml"
# The wall-clock bar of a CI shard and of the whole local tier (maintainer requirement: < 60 min),
# with room for the image build, the bridge builds and a slower runner.
SHARD_BUDGET = 45 * 60
LOCAL_BUDGET = 50 * 60
# The fixtures that set up the main prefix (tests/e2e/conftest.py), and a function's parameters.
MAIN_PREFIX = frozenset({"installed", "closed", "ui"})
SIGNATURE = re.compile(r"^(?:@pytest\.fixture[^\n]*\ndef \w+|def test_\w+)\((.*?)\)", re.MULTILINE | re.DOTALL)


def _uses_main_prefix(text: str) -> bool:
    """Whether a test or fixture of a module takes one of the main prefix's fixtures."""
    params = {param.split(":")[0].strip() for match in SIGNATURE.finditer(text) for param in match[1].split(",")}
    return bool(params & MAIN_PREFIX)


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("e2e_plan", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


plan_mod = _load()


def _timings(tmp_path: Path, setup: float, modules: dict[str, float], parts: dict[str, dict[str, float]] | None = None
             ) -> Path:
    rows: dict[str, dict[str, object]] = {name: {"seconds": took} for name, took in modules.items()}
    for name, split in (parts or {}).items():
        rows[name]["parts"] = split
    path = tmp_path / "timings.json"
    path.write_text(json.dumps({"setup": setup, "modules": rows}), encoding="utf-8")
    return path


def _t(setup: float, modules: dict[str, float], parts: dict[str, dict[str, float]] | None = None) -> object:
    return plan_mod.Timings(setup, modules, parts or {})


def test_modes() -> None:
    assert plan_mod.mode("test_real_fork.py") == "fresh"
    assert plan_mod.mode("test_fork_update.py") == "fresh"
    assert plan_mod.mode("test_real_bridge.py") == "template"


def test_plan_is_longest_first_and_unknown_modules_lead() -> None:
    timings = _t(0, {"test_a.py": 10.0, "test_b.py": 30.0, "test_real_fork.py": 30.0})
    rows = [(job.what, job.mode, job.seconds, job.args) for job in plan_mod.plan(
        ["test_a.py", "test_b.py", "test_real_fork.py", "test_new.py", "test_a.py"], timings)]
    assert rows == [
        ("tests/e2e/test_new.py", "template", float("inf"), ("tests/e2e/test_new.py",)),
        ("tests/e2e/test_b.py", "template", 30.0, ("tests/e2e/test_b.py",)),
        ("tests/e2e/test_real_fork.py", "fresh", 30.0, ("tests/e2e/test_real_fork.py",)),
        ("tests/e2e/test_a.py", "template", 10.0, ("tests/e2e/test_a.py",)),
    ]


def test_a_long_module_runs_in_parts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(plan_mod, "PARTS", {"test_v.py": {"slow": ("test_x", "test_y"), "other": ("test_z",)}})
    path = "tests/e2e/test_v.py"
    jobs = plan_mod.jobs_of("test_v.py", _t(0, {"test_v.py": 900.0}, {"test_v.py": {"slow": 500.0}}))
    assert jobs == [
        plan_mod.Job(f"{path}[slow]", "template", 500.0, (f"{path}::test_x", f"{path}::test_y")),
        plan_mod.Job(f"{path}[other]", "template", 300.0, (f"{path}::test_z",)),
        plan_mod.Job(f"{path}[rest]", "template", 100.0, (path, f"--deselect={path}::test_x",
                                                          f"--deselect={path}::test_y", f"--deselect={path}::test_z")),
    ]
    # Parts measured longer than the module leave the rest a second.
    assert plan_mod.jobs_of("test_v.py", _t(0, {"test_v.py": 100.0}, {"test_v.py": {"slow": 500.0}}))[-1].seconds == 1.0


def test_estimate_without_a_template() -> None:
    timings = _t(50.0, {"test_real_fork.py": 100.0, "test_fork_update.py": 300.0, "test_wine_pin_upgrade.py": 200.0})
    modules = list(timings.modules)
    assert plan_mod.estimate(modules, 1, timings) == 600.0
    assert plan_mod.estimate(modules, 2, timings) == 300.0
    assert plan_mod.estimate(modules, 3, timings) == 300.0


def test_estimate_waits_for_the_template() -> None:
    timings = _t(60.0, {"test_real_fork.py": 100.0, "test_a.py": 200.0, "test_b.py": 50.0})
    # One job: the template (60), then a (200), fork (100), b (50).
    assert plan_mod.estimate(list(timings.modules), 1, timings) == 410.0
    # Two jobs: fork starts at once beside the template; a starts at 60, b at 100.
    assert plan_mod.estimate(list(timings.modules), 2, timings) == 260.0
    # Only template modules and more jobs than work: they all wait for the template.
    assert plan_mod.estimate(["test_a.py", "test_b.py"], 4, timings) == 260.0


def test_load_timings(tmp_path: Path) -> None:
    path = _timings(tmp_path, 12, {"test_a.py": 3, "test_b.py": 9}, {"test_b.py": {"x": 4}})
    assert plan_mod.load_timings(path) == (12.0, {"test_a.py": 3.0, "test_b.py": 9.0}, {"test_b.py": {"x": 4.0}})


def _tree(tmp_path: Path) -> Path:
    root = tmp_path / "tree"
    for rel in (*plan_mod.KEY_FILES, "src/fork_linux/setup.py", "src/fork_linux/data/m.json"):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(rel, encoding="utf-8")
    (root / "src/fork_linux/__pycache__").mkdir()
    (root / "src/fork_linux/__pycache__/setup.pyc").write_bytes(b"\0")
    return root


def test_template_key_follows_what_setup_depends_on(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    key = plan_mod.template_key(root, "sha256:1", "2.23.2")
    assert re.fullmatch(r"[0-9a-f]{64}", key)
    assert plan_mod.template_key(root, "sha256:1", "2.23.2") == key
    (root / "src/fork_linux/__pycache__/setup.pyc").write_bytes(b"\1")
    assert plan_mod.template_key(root, "sha256:1", "2.23.2") == key, "bytecode does not count"
    assert plan_mod.template_key(root, "sha256:2", "2.23.2") != key
    assert plan_mod.template_key(root, "sha256:1", "2.23.1") != key
    (root / "src/fork_linux/data/m.json").write_text("changed", encoding="utf-8")
    assert plan_mod.template_key(root, "sha256:1", "2.23.2") != key


def test_main_plan(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = _timings(tmp_path, 60, {"test_a.py": 10, "test_real_fork.py": 20})
    assert plan_mod.main(["plan", "--timings", str(path), "tests/e2e/test_a.py", "test_real_fork.py", "test_x.py"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "tests/e2e/test_x.py template unknown tests/e2e/test_x.py",
        "tests/e2e/test_real_fork.py fresh 20 tests/e2e/test_real_fork.py",
        "tests/e2e/test_a.py template 10 tests/e2e/test_a.py"]


def test_main_estimate(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = _timings(tmp_path, 60, {"test_a.py": 10, "test_real_fork.py": 20})
    assert plan_mod.main(["estimate", "--timings", str(path), "--jobs", "1", "test_a.py", "test_real_fork.py"]) == 0
    assert capsys.readouterr().out == "90\n"
    assert plan_mod.main(["estimate", "--timings", str(path), "--jobs", "0", "test_a.py"]) == 2
    assert "--jobs and --remote-jobs must be at least 1" in capsys.readouterr().err


def test_split_ends_both_machines_about_together() -> None:
    timings = _t(60.0, {"test_a.py": 600.0, "test_b.py": 500.0, "test_c.py": 100.0, "test_real_fork.py": 300.0})
    local, remote = plan_mod.split(list(timings.modules), 1, 2, timings)
    assert local == ["test_a.py"]
    assert remote == ["test_b.py", "test_real_fork.py", "test_c.py"]
    assert plan_mod.split(["test_a.py"], 1, 1, timings) == (["test_a.py"], [])


def test_main_split(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = _timings(tmp_path, 60, {"test_a.py": 600, "test_b.py": 500})
    assert plan_mod.main(["split", "--timings", str(path), "--jobs", "1", "--remote-jobs", "1", "test_a.py",
                          "test_b.py"]) == 0
    assert capsys.readouterr().out.splitlines() == ["local test_a.py", "remote test_b.py"]
    assert plan_mod.main(["split", "--timings", str(path), "--jobs", "1", "--remote-jobs", "0", "test_a.py"]) == 2


def test_main_key(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _tree(tmp_path)
    assert plan_mod.main(["key", "--image-id", "sha256:1", "--root", str(root)]) == 0
    assert capsys.readouterr().out.strip() == plan_mod.template_key(root, "sha256:1", "")


def test_runs_as_a_script(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(sys, "argv", ["e2e-plan.py", "plan", "test_real_fork.py"])
    with pytest.raises(SystemExit) as exited:
        runpy.run_path(str(SCRIPT), run_name="__main__")
    assert exited.value.code == 0
    assert capsys.readouterr().out.startswith("tests/e2e/test_real_fork.py fresh ")


# -- contracts --------------------------------------------------------------------------------------

MODULES = sorted(path.name for path in E2E.glob("test_*.py"))


def test_every_module_has_a_timing() -> None:
    timings = plan_mod.load_timings()
    assert sorted(timings.modules) == MODULES
    assert timings.setup > 0
    assert all(value > 0 for value in timings.modules.values())
    for module, parts in timings.parts.items():
        assert sorted(parts) == sorted(plan_mod.PARTS[module])
        assert sum(parts.values()) < timings.modules[module]


def test_parts_name_tests_of_their_module() -> None:
    for module, parts in plan_mod.PARTS.items():
        text = (E2E / module).read_text(encoding="utf-8")
        for tests in parts.values():
            for test in tests:
                assert f"def {test}(" in text, (module, test)


def test_fresh_modules_never_use_the_main_prefix_and_template_modules_do() -> None:
    assert plan_mod.FRESH <= set(MODULES)
    for module in MODULES:
        uses = _uses_main_prefix((E2E / module).read_text(encoding="utf-8"))
        if module in plan_mod.FRESH:
            assert not uses, f"{module} uses the main prefix: it can start from the template"
        else:
            assert uses, f"{module} never uses the main prefix: list it in FRESH"


def test_the_template_is_the_first_setup_test() -> None:
    script = (REPO / "scripts" / "ci-e2e-wine.sh").read_text(encoding="utf-8")
    assert "TEMPLATE_TEST=(tests/e2e/test_real_fork.py -k test_01_setup_completes_and_resumes)" in script
    assert "def test_01_setup_completes_and_resumes(e2e: Any)" in (E2E / "test_real_fork.py").read_text(
        encoding="utf-8")


def _shards() -> list[tuple[str, list[str], int]]:
    text = WORKFLOW.read_text(encoding="utf-8")
    rows = re.findall(r"- shard: (\S+)\n +jobs: (\d+)\n +tests: >-\n((?: {14}\S.*\n)+)", text)
    return [(name, tests.split(), int(jobs)) for name, jobs, tests in rows]


def test_every_ci_shard_finishes_within_the_budget() -> None:
    timings = plan_mod.load_timings()
    shards = _shards()
    assert shards
    for name, tests, jobs in shards:
        took = plan_mod.estimate([Path(test).name for test in tests], jobs, timings)
        assert took <= SHARD_BUDGET, f"{name}: ~{took / 60:.0f} min with {jobs} jobs"


def test_the_whole_tier_finishes_within_the_budget_locally() -> None:
    assert plan_mod.estimate(MODULES, 6, plan_mod.load_timings()) <= LOCAL_BUDGET
