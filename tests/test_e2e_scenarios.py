"""The E2E scenario registry: every user-facing scenario has an end-to-end test that exists.

Runs in the unit tier (always): ``tests/e2e/scenarios.json`` must be valid, every scenario must
name at least one test, every referenced test must exist (unless another branch provides it and
it is still waiting for its merge), every real-tier test must say which scenarios it covers on a
``Scenarios:`` docstring line, and ``docs/qa/E2E-SCENARIOS.md`` must match the registry.
``scripts/gen-scenarios-doc.py`` (loaded via importlib: the name is hyphenated) holds the rules.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "gen-scenarios-doc.py"


def _load() -> ModuleType:
    name = "fork_linux_gen_scenarios_doc"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


gsd = _load()

# -- the real registry -----------------------------------------------------------------------------


def test_the_registry_and_the_tests_agree() -> None:
    assert gsd.validate(ROOT) == []


def test_the_catalog_is_up_to_date() -> None:
    assert gsd.check(ROOT) == []


def test_every_area_has_scenarios_of_this_repository() -> None:
    entries = gsd.load(ROOT)
    ours = {entry["area"] for entry in entries if entry["provided_by"] == gsd.THIS_BRANCH}
    assert {"setup", "launch", "git", "integration", "bridge", "doctor", "uninstall", "rollback"} <= ours


def _module_order() -> tuple[str, ...]:
    tree = ast.parse((ROOT / "tests" / "e2e" / "conftest.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "MODULE_ORDER" for target in node.targets
        ):
            return tuple(ast.literal_eval(node.value))
    raise AssertionError("tests/e2e/conftest.py has no MODULE_ORDER")


def test_every_real_tier_module_has_its_place_in_the_run_order() -> None:
    modules = sorted(path.name for path in (ROOT / "tests" / "e2e").glob("test_*.py"))
    order = _module_order()
    assert sorted(order) == modules
    assert order[-1] == "test_real_uninstall.py", "purging must come last"


def test_real_tier_modules_do_not_shadow_unit_test_modules() -> None:
    """Without __init__.py files, two test modules with one basename stop pytest's collection."""
    unit = {path.name for path in (ROOT / "tests").glob("test_*.py")}
    real = {path.name for path in (ROOT / "tests" / "e2e").glob("test_*.py")}
    assert not unit & real


# -- a registry and a test tree built for each rule ---------------------------------------------------


def _scenario(sid: str = "S-ONE", **overrides: Any) -> dict[str, Any]:
    entry = {
        "id": sid,
        "area": "setup",
        "title": "A scenario",
        "tiers": ["real-fork"],
        "provided_by": "this-branch",
        "pending_merge": False,
        "tests": ["tests/e2e/test_real_x.py::test_one"],
    }
    entry.update(overrides)
    return entry


REAL_TEST = '''
def test_one():
    """Does one thing.

    Scenarios: S-ONE
    """
'''


def _tree(root: Path, entries: Any, files: dict[str, str] | None = None) -> Path:
    (root / "tests" / "e2e").mkdir(parents=True, exist_ok=True)
    (root / "scripts").mkdir(exist_ok=True)
    (root / "docs" / "qa").mkdir(parents=True, exist_ok=True)
    (root / "scripts" / "ci-packaging-matrix.sh").write_text("FORMATS=(deb tarball)\n", encoding="utf-8")
    (root / "scripts" / "packaging-e2e-install.sh").write_text(
        "assert_installed() {\n\ttrue\n}\ncheck_doctor_json () { true; }\n", encoding="utf-8"
    )
    (root / "tests" / "e2e" / "scenarios.json").write_text(json.dumps(entries), encoding="utf-8")
    for name, text in {"tests/e2e/test_real_x.py": REAL_TEST, **(files or {})}.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text, encoding="utf-8")
    return root


def test_a_consistent_registry_is_valid(tmp_path: Path) -> None:
    assert gsd.validate(_tree(tmp_path, [_scenario()])) == []


def test_the_registry_must_be_a_readable_list(tmp_path: Path) -> None:
    root = _tree(tmp_path, {"not": "a list"})
    assert gsd.validate(root) == ["tests/e2e/scenarios.json: the top level must be a list of scenarios"]
    (root / "tests" / "e2e" / "scenarios.json").write_text("{", encoding="utf-8")
    assert gsd.validate(root)[0].startswith("tests/e2e/scenarios.json: ")
    (root / "tests" / "e2e" / "scenarios.json").unlink()
    assert gsd.validate(root)[0].startswith("tests/e2e/scenarios.json: ")


@pytest.mark.parametrize(
    ("entry", "problem"),
    [
        ("not an object", "entry #0: must be an object"),
        ({k: v for k, v in _scenario().items() if k != "title"}, "S-ONE: missing key 'title'"),
        ({**_scenario(), "extra": 1}, "S-ONE: unknown key 'extra'"),
        (_scenario(tests="tests/e2e/test_real_x.py::test_one"), "S-ONE: 'tests' must be a list"),
        (_scenario(notes=3), "S-ONE: 'notes' must be a str"),
        (_scenario(pending_merge="no"), "S-ONE: 'pending_merge' must be a bool"),
        (_scenario("s-lower"), "s-lower: id must look like S-FRESH-MANAGED (or F1 / A1 for the update scenarios)"),
        (_scenario("ZZ-UNKNOWN"), "ZZ-UNKNOWN: area 'setup' does not match the id's namespace"),
        (_scenario("G-ONE"), "G-ONE: area 'setup' does not match the id's namespace"),
        (_scenario(title="  "), "S-ONE: empty title"),
        (_scenario(tests=[]), "S-ONE: no test covers it"),
        (_scenario(tests=["x", "x"]), "S-ONE: tests must be unique strings"),
        (_scenario(tests=[3]), "S-ONE: tests must be unique strings"),
        (_scenario(tiers=["real-fork", "moon"]),
         "S-ONE: tiers must be among real-fork, e2e-wine-docker, de, packaging, unit"),
        (_scenario(tiers=["packaging"]), "S-ONE: tiers ['packaging'] do not match its tests (expected ['real-fork'])"),
        (_scenario(tests=["tests/test_unit.py::test_one"], tiers=["unit"]),
         "S-ONE: tests/test_unit.py::test_one: a unit test; only the update scenarios (F1, A1, ...) may be "
         "unit-only by design"),
        (_scenario(tests=["src/test_unit.py::test_one"]),
         "S-ONE: src/test_unit.py::test_one: not a test reference (tests/...::test_x or "
         "packaging:<cell>:<function>)"),
        (_scenario(provided_by="fix/elsewhere"),
         "S-ONE: provided_by must be one of this-branch, fix/update-tests, fix/de-e2e, fix/font-fallback-472"),
        (_scenario(pending_merge=True), "S-ONE: pending_merge is only for scenarios another branch provides"),
    ],
)
def test_schema_problems(tmp_path: Path, entry: Any, problem: str) -> None:
    assert problem in gsd.validate(_tree(tmp_path, [entry]))


def test_internal_update_scenarios_may_be_unit_only(tmp_path: Path) -> None:
    """Maintainer decision 2026-10-12: internal update scenarios are unit-only by design, listed as such."""
    files = {
        "tests/test_unit.py": "def test_one():\n    pass\n",
        "tests/e2e/test_real_y.py": 'def test_two():\n    """Two.\n\n    Scenarios: A13\n    """\n',
    }
    unit = _scenario("F14", area="fork-update", tiers=["unit"], tests=["tests/test_unit.py::test_one"],
                     notes="unit-only by design")
    mixed = _scenario("A13", area="app-upgrade", tiers=["real-fork", "unit"],
                      tests=["tests/e2e/test_real_y.py::test_two", "tests/test_unit.py::test_one"])
    assert gsd.validate(_tree(tmp_path, [_scenario(), unit, mixed], files)) == []
    wrong = _scenario("F14", area="fork-update", tiers=["real-fork"], tests=["tests/test_unit.py::test_one"])
    assert gsd.validate(_tree(tmp_path, [_scenario(), wrong, mixed], files)) == [
        "F14: tiers ['real-fork'] do not match its tests (expected ['unit'])"
    ]
    assert "**unit** = unit tests only, by design" in gsd.render([_scenario(), unit])


def test_update_scenario_ids_are_short(tmp_path: Path) -> None:
    entry = _scenario("F1", area="fork-update", provided_by="fix/update-tests", pending_merge=True,
                      tests=["tests/e2e/test_fork_update.py::test_f1"])
    assert gsd.validate(_tree(tmp_path, [_scenario(), entry])) == []


def test_ids_are_unique(tmp_path: Path) -> None:
    assert "S-ONE: listed more than once" in gsd.validate(_tree(tmp_path, [_scenario(), _scenario()]))


def test_missing_references_are_named(tmp_path: Path) -> None:
    entries = [
        _scenario(),
        _scenario("S-TWO", tests=["tests/e2e/test_real_x.py::test_gone"]),
        _scenario("S-THREE", tiers=["packaging"], tests=["packaging:snap:assert_installed",
                                                       "packaging:deb:assert_removed"]),
        _scenario("S-FOUR", tests=["tests/e2e/test_real_x.py::test_one[nope]"]),
    ]
    errors = gsd.validate(_tree(tmp_path, entries))
    assert "S-TWO: tests/e2e/test_real_x.py::test_gone: no such test function" in errors
    assert "S-THREE: packaging:snap:assert_installed: no packaging cell 'snap'" in errors
    assert ("S-THREE: packaging:deb:assert_removed: no function 'assert_removed' in "
            "scripts/packaging-e2e-install.sh") in errors
    assert ("S-FOUR: tests/e2e/test_real_x.py::test_one[nope]: no parametrize id 'nope' (known: none)") in errors


def test_packaging_checks_and_wine_tier_tests_resolve(tmp_path: Path) -> None:
    entries = [
        _scenario(),
        _scenario("S-PKG", tiers=["packaging"], tests=["packaging:deb:assert_installed",
                                                     "packaging:tarball:check_doctor_json"]),
        _scenario("B-WINE", area="bridge", tiers=["e2e-wine-docker"], tests=["tests/bridge/test_shims_x.py::test_w"]),
    ]
    files = {"tests/bridge/test_shims_x.py": "def test_w():\n    pass\n"}
    assert gsd.validate(_tree(tmp_path, entries, files)) == []


def test_pending_references_wait_for_their_merge(tmp_path: Path) -> None:
    pending = _scenario("DE-XFCE-MENU", area="desktop", tiers=["de"], provided_by="fix/de-e2e", pending_merge=True,
                        tests=["tests/e2e_de/test_de_integration.py::test_menu[xfce]"])
    root = _tree(tmp_path, [_scenario(), pending])
    assert gsd.validate(root) == []
    # Once the branch is merged its tests exist, and the flag must go.
    (root / "tests" / "e2e_de").mkdir()
    (root / "tests" / "e2e_de" / "test_de_integration.py").write_text(
        'import pytest\n\n@pytest.mark.parametrize("desktop", ["xfce"])\ndef test_menu(desktop):\n'
        '    """Scenarios: DE-XFCE-MENU"""\n', encoding="utf-8")
    assert gsd.validate(root) == [
        "DE-XFCE-MENU: every test exists and names it, so fix/de-e2e is merged: clear pending_merge "
        "(scripts/gen-scenarios-doc.py clear-pending fix/de-e2e)"
    ]
    assert gsd.clear_pending(root, "fix/update-tests") == 0
    assert gsd.clear_pending(root, "fix/de-e2e") == 1
    assert gsd.validate(root) == []


@pytest.mark.parametrize(
    ("source", "problem"),
    [
        ("def test_one():\n    pass\n",
         "tests/e2e/test_real_x.py::test_one: real-tier test without a 'Scenarios: ID, ...' docstring line"),
        ('def test_one():\n    """Scenarios: ,"""\n',
         "tests/e2e/test_real_x.py::test_one: its Scenarios: line names no scenario"),
        ('def test_one():\n    """Scenarios: S-ONE, S-NOPE"""\n',
         "tests/e2e/test_real_x.py::test_one: declares S-NOPE, which is not in tests/e2e/scenarios.json"),
        ('def test_one():\n    """Scenarios: S-ONE, S-TWO"""\n',
         "tests/e2e/test_real_x.py::test_one: declares S-TWO, but the registry does not list this test for it"),
        ('def test_one():\n    """Scenarios: S-TWO"""\n',
         "tests/e2e/test_real_x.py::test_one: the registry lists it for S-ONE, "
         "which its Scenarios: line does not name"),
    ],
)
def test_declarations_and_the_registry_agree_both_ways(tmp_path: Path, source: str, problem: str) -> None:
    entries = [_scenario(), _scenario("S-TWO", tests=["tests/e2e/test_real_y.py::test_two"])]
    files = {"tests/e2e/test_real_x.py": source,
             "tests/e2e/test_real_y.py": 'def test_two():\n    """Scenarios: S-TWO"""\n'}
    assert problem in gsd.validate(_tree(tmp_path, entries, files))


def test_parametrize_ids_are_read_like_pytest(tmp_path: Path) -> None:
    source = '''
import pytest


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_theme(theme):
    """Scenarios: I-ONE"""


@pytest.mark.parametrize("dpi", [96, 144])
@pytest.mark.parametrize("mode", [True, None])
def test_dpi(dpi, mode):
    """Scenarios: I-ONE"""


@pytest.mark.parametrize(("a", "b"), [("x", 1), pytest.param("y", 2, id="named"), pytest.param(("z", 3))])
def test_pairs(a, b):
    """Scenarios: I-ONE"""


@pytest.mark.parametrize("value", [object(), "a"], ids=["first", "second"])
def test_ids(value):
    """Scenarios: I-ONE"""


@pytest.mark.parametrize("value", [1.5, b"bytes"])
def test_default_ids(value):
    """Scenarios: I-ONE"""


class TestGroup:
    def test_method(self):
        """Scenarios: I-ONE"""
'''
    refs = ["tests/e2e/test_real_p.py::test_theme[dark]", "tests/e2e/test_real_p.py::test_dpi[True-144]",
            "tests/e2e/test_real_p.py::test_pairs[x-1]", "tests/e2e/test_real_p.py::test_pairs[named]",
            "tests/e2e/test_real_p.py::test_ids[second]", "tests/e2e/test_real_p.py::test_default_ids[1.5]",
            "tests/e2e/test_real_p.py::test_default_ids[value1]", "tests/e2e/test_real_p.py::TestGroup::test_method",
            "tests/e2e/test_real_p.py::test_pairs"]
    entries = [_scenario(), _scenario("I-ONE", area="integration", tests=refs)]
    assert gsd.validate(_tree(tmp_path, entries, {"tests/e2e/test_real_p.py": source})) == []
    functions = gsd.read_tree(tmp_path).functions
    assert functions["tests/e2e/test_real_p.py::test_pairs"].params == {"x-1", "named", "z-3"}


def test_unreadable_parametrize_ids_are_reported(tmp_path: Path) -> None:
    source = '''
import pytest

VALUES = ["a"]


@pytest.mark.parametrize("value", VALUES)
def test_dynamic(value):
    """Scenarios: S-TWO"""


@pytest.mark.parametrize("value", [VALUES[0]])
def test_expression(value):
    """Scenarios: S-TWO"""


@pytest.mark.parametrize("value", [1], ids=[str(1)])
def test_computed_ids(value):
    """Scenarios: S-TWO"""


@pytest.mark.parametrize("value")
def test_incomplete(value):
    """Scenarios: S-TWO"""


@pytest.mark.parametrize(("a", "b"), [pytest.param("x", 1)])
def test_param_without_id(a, b):
    """Scenarios: S-TWO"""


@pytest.mark.parametrize(("a", "b"), [("x", VALUES)])
def test_tuple_with_an_expression(a, b):
    """Scenarios: S-TWO"""
'''
    names = ["test_dynamic", "test_expression", "test_computed_ids", "test_incomplete", "test_param_without_id",
             "test_tuple_with_an_expression"]
    refs = [f"tests/e2e/test_real_d.py::{name}[x]" for name in names]
    entries = [_scenario(), _scenario("S-TWO", tests=refs)]
    errors = gsd.validate(_tree(tmp_path, entries, {"tests/e2e/test_real_d.py": source}))
    for ref in refs:
        assert f"S-TWO: {ref}: its parametrize ids cannot be read statically" in errors


def test_files_that_do_not_parse_have_no_tests(tmp_path: Path) -> None:
    root = _tree(tmp_path, [_scenario()], {"tests/e2e/test_real_broken.py": "def test_(:\n"})
    assert not [key for key in gsd.read_tree(root).functions if "broken" in key]


def test_the_catalog_lists_every_scenario(tmp_path: Path) -> None:
    pending = _scenario("F1", area="fork-update", title="Pipe | in a title", provided_by="fix/update-tests",
                        pending_merge=True, tests=["tests/e2e/test_fork_update.py::test_f1"], notes="see the branch")
    merged = _scenario("DE-GNOME-MENU", area="desktop", tiers=["de"], provided_by="fix/de-e2e",
                       tests=["tests/e2e_de/test_de.py::test_menu"])
    root = _tree(tmp_path, [_scenario(), pending, merged])
    text = gsd.render(gsd.load(root))
    assert "| S-ONE | A scenario | real-fork | this repository | `tests/e2e/test_real_x.py::test_one` |" in text
    assert "| F1 | Pipe \\| in a title — see the branch | real-fork | `fix/update-tests` (pending merge) |" in text
    assert "| DE-GNOME-MENU | A scenario | de | `fix/de-e2e` |" in text
    assert "| **Total** | 3 | 1 | 1 |" in text
    assert gsd.check(root) == [
        "docs/qa/E2E-SCENARIOS.md is out of date: run 'python3 scripts/gen-scenarios-doc.py write'"
    ]
    gsd.write(root)
    assert gsd.check(root) == []


def test_unknown_areas_sort_last_in_the_catalog() -> None:
    entries = [_scenario(area="elsewhere"), _scenario("S-TWO")]
    text = gsd.render(entries)
    assert text.index("## Setup") < text.index("## elsewhere")


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t",
                        "GIT_COMMITTER_EMAIL": "t@example.invalid", "PATH": "/usr/bin:/bin", "HOME": str(root)})


def test_pending_references_are_checked_against_their_branch(tmp_path: Path) -> None:
    pending = [
        _scenario("F1", area="fork-update", provided_by="fix/update-tests", pending_merge=True,
                  tests=["tests/e2e/test_fork_update.py::test_f1", "tests/e2e/test_fork_update.py::test_f2"]),
        _scenario("A2", area="app-upgrade", tiers=["packaging"], provided_by="fix/update-tests", pending_merge=True,
                  tests=["packaging:deb:upgrade_from_previous"]),
        _scenario("DE-KDE-MENU", area="desktop", tiers=["de"], provided_by="fix/de-e2e", pending_merge=True,
                  tests=["tests/e2e_de/test_de.py::test_menu"]),
    ]
    root = _tree(tmp_path, [_scenario(), *pending])
    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    _git(root, "checkout", "-q", "-b", "fix/update-tests")
    (root / "tests" / "e2e" / "test_fork_update.py").write_text("def test_f1():\n    pass\n", encoding="utf-8")
    (root / "tests" / "e2e" / "test_bad_syntax.py").write_text("def (\n", encoding="utf-8")
    with open(root / "scripts" / "packaging-e2e-install.sh", "a", encoding="utf-8") as handle:
        handle.write("upgrade_from_previous() { true; }\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "update tests")
    _git(root, "checkout", "-q", "main")
    assert gsd.pending(root) == [
        "F1: tests/e2e/test_fork_update.py::test_f1: present on fix/update-tests",
        "F1: tests/e2e/test_fork_update.py::test_f2: no such test function",
        "A2: packaging:deb:upgrade_from_previous: present on fix/update-tests",
        "DE-KDE-MENU: branch fix/de-e2e does not exist yet",
    ]


def test_the_command_line(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _tree(tmp_path, [_scenario()])
    assert gsd.main(["write"], root=root) == 0
    assert gsd.main(["check"], root=root) == 0
    assert gsd.main(["validate"], root=root) == 0
    assert "validate: OK" in capsys.readouterr().out
    assert gsd.main(["pending"], root=root) == 0
    assert gsd.main(["bogus"], root=root) == 2
    assert gsd.main([], root=root) == 2
    assert "validate" in capsys.readouterr().err
    (root / "tests" / "e2e" / "scenarios.json").write_text(
        json.dumps([_scenario(), _scenario("F1", area="fork-update", provided_by="fix/update-tests",
                                           pending_merge=True, tests=["tests/e2e/test_fork_update.py::test_f1"])]),
        encoding="utf-8")
    assert gsd.main(["check"], root=root) == 1
    assert gsd.main(["pending"], root=root) == 1
    assert "F1: branch fix/update-tests does not exist yet" in capsys.readouterr().out
    assert gsd.main(["declare"], root=root) == 2
    assert gsd.main(["clear-pending", "this-branch"], root=root) == 2
    assert gsd.main(["validate", "fix/update-tests"], root=root) == 2
    assert gsd.main(["declare", "fix/update-tests"], root=root) == 0
    assert "nothing to declare" in capsys.readouterr().out
    assert gsd.main(["clear-pending", "fix/update-tests"], root=root) == 0
    assert "cleared 1 pending flag(s)" in capsys.readouterr().out
    assert gsd.main(["validate"], root=root) == 1
    assert "no such test function" in capsys.readouterr().out


def test_the_script_runs_as_a_program() -> None:
    result = subprocess.run([sys.executable, str(SCRIPT), "validate"], capture_output=True, text=True, timeout=120,
                            check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "validate: OK"


def test_odd_test_sources_are_read_without_failing(tmp_path: Path) -> None:
    source = '''
import functools
import pytest

FACTORIES = [pytest.mark.parametrize]
NAMES = "value"


@functools.lru_cache
@FACTORIES[0]("value", [1])
def test_subscript_decorator(value):
    """Scenarios: S-TWO"""


@pytest.mark.parametrize(NAMES, [pytest.param(1, marks=())])
def test_names_from_a_variable(value):
    """Scenarios: S-TWO"""


class TestGroup:
    LIMIT = 3

    def helper(self):
        return self.LIMIT

    def test_inside(self):
        """Scenarios: S-TWO"""
'''
    refs = ["tests/e2e/test_real_o.py::test_subscript_decorator",
            "tests/e2e/test_real_o.py::test_names_from_a_variable",
            "tests/e2e/test_real_o.py::TestGroup::test_inside"]
    entries = [_scenario(), _scenario("S-TWO", tests=refs)]
    root = _tree(tmp_path, entries, {"tests/e2e/test_real_o.py": source})
    assert gsd.validate(root) == []
    functions = gsd.read_tree(root).functions
    assert functions["tests/e2e/test_real_o.py::test_subscript_decorator"].params == set()
    assert functions["tests/e2e/test_real_o.py::test_names_from_a_variable"].params == {"1"}
    assert "tests/e2e/test_real_o.py::TestGroup::helper" not in functions


def test_malformed_references_and_missing_scripts(tmp_path: Path) -> None:
    root = _tree(tmp_path, [_scenario(), _scenario("S-TWO", tests=["garbage"])])
    assert ("S-TWO: garbage: not a test reference (tests/...::test_x or packaging:<cell>:<function>)"
            in gsd.validate(root))
    tree = gsd.read_tree(root)
    assert gsd.resolve("garbage", tree) == "not a pytest node id or packaging:<cell>:<function>"
    assert gsd.function_key("packaging:deb:assert_installed") is None
    (root / "scripts" / "ci-packaging-matrix.sh").unlink()
    (root / "scripts" / "packaging-e2e-install.sh").unlink()
    assert gsd.packaging_cells(root) == set()
    assert gsd.packaging_checks(root) == set()
    (root / "scripts" / "ci-packaging-matrix.sh").write_text("# no formats\n", encoding="utf-8")
    assert gsd.packaging_cells(root) == set()


def test_a_pending_scenario_may_list_an_undeclared_real_tier_test(tmp_path: Path) -> None:
    pending = _scenario("F1", area="fork-update", provided_by="fix/update-tests", pending_merge=True,
                        tests=["tests/e2e/test_real_x.py::test_one", "tests/e2e/test_fork_update.py::test_f1"])
    assert gsd.validate(_tree(tmp_path, [_scenario(), pending])) == []


# -- after a merge: declare the merged branch's scenarios on its tests --------------------------------


def test_declarations_may_continue_after_a_comma() -> None:
    doc = "Does things.\n\nScenarios: S-ONE,\n    S-TWO, S-THREE\nMore text, here.\n"
    assert gsd.parse_declaration(doc) == (True, ["S-ONE", "S-TWO", "S-THREE"])
    assert gsd.parse_declaration("Scenarios: S-ONE\nS-TWO") == (True, ["S-ONE"])
    assert gsd.parse_declaration("Scenarios: S-ONE,") == (True, ["S-ONE"])
    assert gsd.parse_declaration(None) == (False, [])
    assert gsd.parse_declaration("No declaration.") == (False, [])


def test_declaration_lines_wrap_after_a_comma() -> None:
    assert gsd.declaration_lines(["S-ONE", "S-TWO"], "    ") == ["Scenarios: S-ONE, S-TWO"]
    ids = [f"DE-DESKTOP{number}-FILE-MANAGER-ACTION" for number in range(5)]
    lines = gsd.declaration_lines(ids, "    ")
    assert len(lines) == 2
    assert lines[0].endswith(',')
    assert lines[1].startswith('    DE-')
    assert all(len("    ") + len(line) <= gsd.DECLARATION_WIDTH for line in lines)
    assert gsd.parse_declaration("\n".join(lines)) == (True, ids)


SOURCE = '''import pytest


def test_bare():
    pass


def test_one_line():
    """Does one thing."""


def test_closing_alone():
    """Does a thing.

    In detail.
    """


def test_closing_on_text():
    """Does a thing.

    In detail."""


def test_declared():
    """Does a thing.

    Scenarios: S-ONE,
        S-TWO
    """


def test_declared_alone():
    """Scenarios: S-ONE"""


class TestGroup:
    def test_method(self):
        """In a class."""


def test_untouched():
    """Not wanted."""
'''

DECLARED = '''import pytest


def test_bare():
    """Scenarios: F1"""
    pass


def test_one_line():
    """Does one thing.

    Scenarios: F1
    """


def test_closing_alone():
    """Does a thing.

    In detail.

    Scenarios: F1, F2
    """


def test_closing_on_text():
    """Does a thing.

    In detail.

    Scenarios: F1
    """


def test_declared():
    """Does a thing.

    Scenarios: S-ONE, S-TWO, F1
    """


def test_declared_alone():
    """Scenarios: S-ONE, F1"""


class TestGroup:
    def test_method(self):
        """In a class.

        Scenarios: F1
        """


def test_untouched():
    """Not wanted."""
'''


def test_declare_in_source_writes_every_kind_of_docstring() -> None:
    wanted = {
        "test_bare": ["F1"],
        "test_one_line": ["F1"],
        "test_closing_alone": ["F1", "F2"],
        "test_closing_on_text": ["F1"],
        "test_declared": ["S-ONE", "S-TWO", "F1"],
        "test_declared_alone": ["S-ONE", "F1"],
        "TestGroup::test_method": ["F1"],
    }
    assert gsd.declare_in_source(SOURCE, wanted) == DECLARED
    assert gsd.declare_in_source(DECLARED, wanted) == DECLARED


def test_declare_names_a_merged_branchs_scenarios_on_its_tests(tmp_path: Path) -> None:
    merged = "def test_f1():\n    \"\"\"Updates Fork.\"\"\"\n\n\ndef test_f2():\n    pass\n"
    entries = [
        _scenario(),
        _scenario("F1", area="fork-update", provided_by="fix/update-tests", pending_merge=True,
                  tiers=["real-fork", "e2e-wine-docker", "packaging"],
                  tests=["tests/e2e/test_fork_update.py::test_f1", "tests/e2e/test_real_x.py::test_one",
                         "packaging:deb:assert_installed", "tests/bridge/test_daemon.py::test_watch"]),
        _scenario("F2", area="fork-update", provided_by="fix/update-tests", pending_merge=True,
                  tests=["tests/e2e/test_fork_update.py::test_f2"]),
        _scenario("FF-DOCTOR", area="font-fallback", provided_by="fix/font-fallback-472", pending_merge=True,
                  tests=["tests/e2e/test_real_x.py::test_one"]),
    ]
    files = {"tests/e2e/test_fork_update.py": merged, "tests/bridge/test_daemon.py": "def test_watch():\n    pass\n"}
    root = _tree(tmp_path, entries, files)
    # Merged but not declared yet: the new tests have no Scenarios: line, and the pending flags still hold.
    assert gsd.validate(root) == [
        "tests/e2e/test_fork_update.py::test_f1: real-tier test without a 'Scenarios: ID, ...' docstring line",
        "tests/e2e/test_fork_update.py::test_f2: real-tier test without a 'Scenarios: ID, ...' docstring line",
    ]
    assert gsd.declare(root, "fix/update-tests") == ["tests/e2e/test_fork_update.py", "tests/e2e/test_real_x.py"]
    assert gsd.declare(root, "fix/update-tests") == []
    functions = gsd.read_tree(root).functions
    assert functions["tests/e2e/test_real_x.py::test_one"].declared == ["S-ONE", "F1"]
    assert functions["tests/e2e/test_fork_update.py::test_f2"].declared == ["F2"]
    stale = "every test exists and names it, so fix/update-tests is merged"
    assert [line.split(": ", 1)[0] for line in gsd.validate(root)] == ["F1", "F2"]
    assert all(stale in line for line in gsd.validate(root))
    assert gsd.clear_pending(root, "fix/update-tests") == 2
    assert gsd.validate(root) == []
