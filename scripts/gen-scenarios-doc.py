#!/usr/bin/env python3
"""The E2E scenario registry: validate tests/e2e/scenarios.json and generate its catalog.

``tests/e2e/scenarios.json`` lists every user-facing scenario of Fork for Linux (unofficial)
with the end-to-end tests that cover it. This script checks the registry against the test
tree and writes the human-readable catalog ``docs/qa/E2E-SCENARIOS.md`` from it.
``tests/test_e2e_scenarios.py`` runs the same checks in the unit tier, so a scenario without a
test, a test node that does not exist, or a real-tier test that does not say which scenarios
it covers fails CI.

Usage:
  python3 scripts/gen-scenarios-doc.py validate       # registry and test tree agree
  python3 scripts/gen-scenarios-doc.py write          # regenerate docs/qa/E2E-SCENARIOS.md
  python3 scripts/gen-scenarios-doc.py check          # fail when the catalog is out of date
  python3 scripts/gen-scenarios-doc.py pending        # pending references vs their branches
  python3 scripts/gen-scenarios-doc.py clear-pending  # after the merge: drop every pending flag

Registry entry: ``{"id", "area", "title", "tiers", "provided_by", "pending_merge", "tests"}``
plus an optional ``"notes"``. A test reference is a pytest node id
(``tests/e2e/test_x.py::test_y`` or ``...::test_y[param]``) or a check of a packaging cell
(``packaging:<format>:<function>``: a format of ``scripts/ci-packaging-matrix.sh`` and a shell
function of ``scripts/packaging-e2e-install.sh``). Every real-tier test function (``tests/e2e*/**/test_*.py``) names the
scenarios it covers on a docstring line ``Scenarios: S-FRESH-MANAGED, L-RELAUNCH``.

A scenario provided by another branch carries ``"pending_merge": true`` until that branch is
merged: its references are not resolved before then. Once every reference of a pending entry
exists, the flag is stale and validation fails until it is cleared (``clear-pending``).

Exit status: 0 OK, 1 problems found (printed one per line), 2 usage error.
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = Path("tests/e2e/scenarios.json")
CATALOG = Path("docs/qa/E2E-SCENARIOS.md")
PACKAGING_MATRIX = Path("scripts/ci-packaging-matrix.sh")
PACKAGING_E2E = Path("scripts/packaging-e2e-install.sh")

THIS_BRANCH = "this-branch"
PROVIDERS = (THIS_BRANCH, "fix/update-tests", "fix/de-e2e", "fix/font-fallback-472")
TIERS = ("real-fork", "e2e-wine-docker", "de", "packaging")
# Id namespace -> area; the order is the catalog's.
AREAS = {
    "S": "setup",
    "L": "launch",
    "G": "git",
    "I": "integration",
    "B": "bridge",
    "D": "doctor",
    "U": "uninstall",
    "R": "rollback",
    "F": "fork-update",
    "A": "app-upgrade",
    "DE": "desktop",
    "FF": "font-fallback",
}
AREA_TITLES = {
    "setup": "Setup",
    "launch": "Launch",
    "git": "Git operations through Fork's UI",
    "integration": "OS integration",
    "bridge": "Native-git bridge",
    "doctor": "Doctor and repository fixes",
    "uninstall": "Uninstall",
    "rollback": "Snapshots and rollback",
    "fork-update": "Fork updates",
    "app-upgrade": "Fork for Linux upgrades",
    "desktop": "Desktop environments",
    "font-fallback": "Non-Latin font fallback",
}
ID_RE = re.compile(r"^(?:(?P<ns>[A-Z]{1,2})-[A-Z0-9]+(?:-[A-Z0-9]+)*|(?P<num>[FA])[0-9]{1,2})$")
NODE_RE = re.compile(
    r"^(?P<path>tests/[A-Za-z0-9_/]+\.py)::(?P<func>[A-Za-z_][A-Za-z0-9_]*(?:::[A-Za-z_][A-Za-z0-9_]*)?)"
    r"(?:\[(?P<param>[^\]]+)\])?$"
)
PACKAGING_RE = re.compile(r"^packaging:(?P<cell>[a-z0-9-]+):(?P<check>[a-z_][a-z0-9_]*)$")
SHELL_FUNCTION_RE = re.compile(r"^\s*(?P<name>[a-z_][a-z0-9_]*)\s*\(\)", re.MULTILINE)
DECLARATION_RE = re.compile(r"^\s*Scenarios:\s*(?P<ids>.+?)\s*$", re.MULTILINE)
FORMATS_RE = re.compile(r"^FORMATS=\((?P<cells>[^)]*)\)", re.MULTILINE)
# Real-tier test files: real Fork (tests/e2e) and the desktop tier (tests/e2e_de).
REAL_TIER_DIRS = ("tests/e2e", "tests/e2e_de")
REQUIRED = {
    "id": str, "area": str, "title": str, "tiers": list, "provided_by": str, "pending_merge": bool, "tests": list,
}
OPTIONAL = {"notes": str}


class RegistryError(Exception):
    """The registry file cannot be read at all."""


@dataclass
class TestFunction:
    """One test function found by parsing a test file."""

    path: str
    name: str
    params: set[str] | None
    declared: list[str]
    has_declaration: bool


@dataclass
class TestTree:
    """Every test function under ``tests/`` (by ``path::name``) and the packaging cells."""

    functions: dict[str, TestFunction] = field(default_factory=dict)
    cells: set[str] = field(default_factory=set)
    checks: set[str] = field(default_factory=set)

    def real_tier(self) -> Iterator[TestFunction]:
        """The test functions of the real tiers."""
        for function in self.functions.values():
            if is_real_tier(function.path):
                yield function


def is_real_tier(path: str) -> bool:
    """True for a test file of the real-Fork or desktop tier."""
    return any(path.startswith(directory + "/") for directory in REAL_TIER_DIRS)


def tier_of(reference: str) -> str | None:
    """The tier a test reference runs in, or None when the reference is malformed."""
    if PACKAGING_RE.match(reference):
        return "packaging"
    match = NODE_RE.match(reference)
    if match is None:
        return None
    path = match.group("path")
    if path.startswith("tests/e2e_de/"):
        return "de"
    if path.startswith("tests/e2e/"):
        return "real-fork"
    if path.startswith("tests/bridge/"):
        return "e2e-wine-docker"
    return None


# -- reading the test tree -----------------------------------------------------------------------


def _param_id(value: ast.expr, index: int, argnames: list[str]) -> str | None:
    """pytest's default id of one parametrize value (literals only), or None when unknown."""
    if isinstance(value, ast.Call) and _call_name(value) == "param":
        for keyword in value.keywords:
            if keyword.arg == "id" and isinstance(keyword.value, ast.Constant):
                return str(keyword.value.value)
        if len(value.args) == 1:
            return _param_id(value.args[0], index, argnames)
        return None
    if isinstance(value, ast.Tuple) and len(argnames) > 1:
        parts = [_scalar_id(item, index, name) for item, name in zip(value.elts, argnames, strict=False)]
        return None if None in parts else "-".join(str(part) for part in parts)
    return _scalar_id(value, index, argnames[0] if argnames else "")


def _scalar_id(value: ast.expr, index: int, argname: str) -> str | None:
    if isinstance(value, ast.Constant):
        if isinstance(value.value, (str, int, float, bool)) or value.value is None:
            return str(value.value)
        return f"{argname}{index}"
    return None


def _call_name(call: ast.Call) -> str:
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def _argnames(node: ast.expr) -> list[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [name.strip() for name in node.value.split(",") if name.strip()]
    if isinstance(node, (ast.List, ast.Tuple)):
        return [elt.value for elt in node.elts if isinstance(elt, ast.Constant) and isinstance(elt.value, str)]
    return []


def _parametrize_ids(call: ast.Call) -> set[str] | None:
    """The ids of one ``@pytest.mark.parametrize(...)``, or None when they cannot be read."""
    if len(call.args) < 2:
        return None
    argnames = _argnames(call.args[0])
    values = call.args[1]
    for keyword in call.keywords:
        if keyword.arg == "ids" and isinstance(keyword.value, (ast.List, ast.Tuple)):
            ids = [_scalar_id(elt, 0, "") for elt in keyword.value.elts]
            return None if None in ids else {str(item) for item in ids}
    if not isinstance(values, (ast.List, ast.Tuple)):
        return None
    ids = [_param_id(value, index, argnames) for index, value in enumerate(values.elts)]
    return None if None in ids else {str(item) for item in ids}


def _params(function: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str] | None:
    """The parametrize ids of a test function: an empty set when it is not parametrized."""
    found: set[str] = set()
    for decorator in function.decorator_list:
        if isinstance(decorator, ast.Call) and _call_name(decorator) == "parametrize":
            ids = _parametrize_ids(decorator)
            if ids is None:
                return None
            # Stacked parametrize decorators multiply; references then name one combination.
            found = ids if not found else {f"{inner}-{outer}" for outer in found for inner in ids}
    return found


def parse_declaration(docstring: str | None) -> tuple[bool, list[str]]:
    """``(has a Scenarios: line, the ids on it)`` of a test function's docstring."""
    if not docstring:
        return False, []
    match = DECLARATION_RE.search(docstring)
    if match is None:
        return False, []
    return True, [item.strip() for item in match.group("ids").split(",") if item.strip()]


def _functions(tree: ast.Module) -> Iterator[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]]:
    """Top-level test functions and test methods of ``Test*`` classes, with their node names."""
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
            yield node.name, node
        elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name.startswith("test"):
                    yield f"{node.name}::{item.name}", item


def read_test_file(root: Path, path: Path) -> list[TestFunction]:
    """The test functions of one file (a file that does not parse has none)."""
    relative = path.relative_to(root).as_posix()
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
    except (OSError, SyntaxError, UnicodeDecodeError):
        return []
    found = []
    for name, node in _functions(tree):
        has, declared = parse_declaration(ast.get_docstring(node))
        found.append(TestFunction(relative, name, _params(node), declared, has))
    return found


def packaging_cells(root: Path) -> set[str]:
    """The packaging formats of ``scripts/ci-packaging-matrix.sh`` (``FORMATS=(...)``)."""
    try:
        text = (root / PACKAGING_MATRIX).read_text(encoding="utf-8")
    except OSError:
        return set()
    match = FORMATS_RE.search(text)
    return set(match.group("cells").split()) if match else set()


def packaging_checks(root: Path) -> set[str]:
    """The shell functions of ``scripts/packaging-e2e-install.sh``."""
    try:
        text = (root / PACKAGING_E2E).read_text(encoding="utf-8")
    except OSError:
        return set()
    return {match.group("name") for match in SHELL_FUNCTION_RE.finditer(text)}


def read_tree(root: Path) -> TestTree:
    """Parse every ``test_*.py`` under ``root/tests``."""
    tree = TestTree(cells=packaging_cells(root), checks=packaging_checks(root))
    for path in sorted((root / "tests").rglob("test_*.py")):
        for function in read_test_file(root, path):
            tree.functions[f"{function.path}::{function.name}"] = function
    return tree


def resolve(reference: str, tree: TestTree) -> str | None:
    """None when ``reference`` exists in ``tree``, else why it does not."""
    cell = PACKAGING_RE.match(reference)
    if cell is not None:
        if cell.group("cell") not in tree.cells:
            return f"no packaging cell {cell.group('cell')!r}"
        return None if cell.group("check") in tree.checks else f"no function {cell.group('check')!r} in {PACKAGING_E2E}"
    match = NODE_RE.match(reference)
    if match is None:
        return "not a pytest node id or packaging:<cell>:<function>"
    function = tree.functions.get(f"{match.group('path')}::{match.group('func')}")
    if function is None:
        return "no such test function"
    param = match.group("param")
    if param is None:
        return None
    if function.params is None:
        return "its parametrize ids cannot be read statically"
    if param not in function.params:
        return f"no parametrize id {param!r} (known: {', '.join(sorted(function.params)) or 'none'})"
    return None


def function_key(reference: str) -> str | None:
    """``path::function`` of a pytest node reference (None for a packaging cell)."""
    match = NODE_RE.match(reference)
    if match is None:
        return None
    return f"{match.group('path')}::{match.group('func')}"


# -- the registry --------------------------------------------------------------------------------


def load(root: Path) -> list[dict[str, Any]]:
    """The registry entries (raises RegistryError when the file is missing or not a JSON list)."""
    path = root / REGISTRY
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RegistryError(f"{REGISTRY}: {exc}") from exc
    if not isinstance(data, list):
        raise RegistryError(f"{REGISTRY}: the top level must be a list of scenarios")
    return data


def _entry_label(index: int, entry: Any) -> str:
    if isinstance(entry, dict) and isinstance(entry.get("id"), str):
        return entry["id"]
    return f"entry #{index}"


def schema_errors(entries: list[Any]) -> list[str]:
    """Shape problems: keys, types, id format, area, tiers, provider and pending flag."""
    errors: list[str] = []
    for index, entry in enumerate(entries):
        label = _entry_label(index, entry)
        if not isinstance(entry, dict):
            errors.append(f"{label}: must be an object")
            continue
        problems = _shape_problems(entry) or _value_problems(entry)
        errors.extend(f"{label}: {problem}" for problem in problems)
    return errors


def _shape_problems(entry: Mapping[str, Any]) -> list[str]:
    problems = [f"missing key {key!r}" for key in REQUIRED if key not in entry]
    problems += [f"unknown key {key!r}" for key in entry if key not in REQUIRED and key not in OPTIONAL]
    for key, kind in {**REQUIRED, **OPTIONAL}.items():
        if key in entry and not isinstance(entry[key], kind):
            problems.append(f"{key!r} must be a {kind.__name__}")
    return problems


def _value_problems(entry: Mapping[str, Any]) -> list[str]:
    problems: list[str] = []
    match = ID_RE.match(entry["id"])
    if match is None:
        problems.append("id must look like S-FRESH-MANAGED (or F1 / A1 for the update scenarios)")
    elif AREAS.get(match.group("ns") or match.group("num")) != entry["area"]:
        problems.append(f"area {entry['area']!r} does not match the id's namespace")
    if not entry["title"].strip():
        problems.append("empty title")
    problems += _list_problems(entry)
    if entry["provided_by"] not in PROVIDERS:
        problems.append(f"provided_by must be one of {', '.join(PROVIDERS)}")
    elif entry["provided_by"] == THIS_BRANCH and entry["pending_merge"]:
        problems.append("pending_merge is only for scenarios another branch provides")
    return problems


def _list_problems(entry: Mapping[str, Any]) -> list[str]:
    problems: list[str] = []
    tests = entry["tests"]
    if not tests:
        problems.append("no test covers it")
    if any(not isinstance(test, str) for test in tests) or len(set(map(str, tests))) != len(tests):
        problems.append("tests must be unique strings")
        return problems
    tiers = entry["tiers"]
    if any(tier not in TIERS for tier in tiers):
        problems.append(f"tiers must be among {', '.join(TIERS)}")
    derived = [tier_of(test) for test in tests]
    for test, tier in zip(tests, derived, strict=True):
        if tier is None:
            problems.append(f"{test}: not an end-to-end test (tests/e2e*, tests/bridge or packaging:<cell>:<function>)")
    expected = sorted({tier for tier in derived if tier is not None}, key=TIERS.index)
    if None not in derived and list(tiers) != expected:
        problems.append(f"tiers {tiers} do not match its tests (expected {expected})")
    return problems


def duplicate_ids(entries: Iterable[Mapping[str, Any]]) -> list[str]:
    """Ids listed more than once."""
    seen: set[str] = set()
    duplicates: list[str] = []
    for entry in entries:
        scenario = entry["id"]
        if scenario in seen and scenario not in duplicates:
            duplicates.append(scenario)
        seen.add(scenario)
    return [f"{scenario}: listed more than once" for scenario in duplicates]


def reference_errors(entries: Iterable[Mapping[str, Any]], tree: TestTree) -> list[str]:
    """References that do not exist (pending entries excepted) and stale pending flags."""
    errors: list[str] = []
    for entry in entries:
        missing = {test: resolve(test, tree) for test in entry["tests"]}
        missing = {test: why for test, why in missing.items() if why is not None}
        if entry["pending_merge"]:
            if not missing:
                errors.append(
                    f"{entry['id']}: every test exists, so {entry['provided_by']} is merged: "
                    "clear pending_merge (scripts/gen-scenarios-doc.py clear-pending)"
                )
            continue
        errors.extend(f"{entry['id']}: {test}: {why}" for test, why in missing.items())
    return errors


def declaration_errors(entries: Iterable[Mapping[str, Any]], tree: TestTree) -> list[str]:
    """Real-tier tests and the registry must agree both ways on which scenarios a test covers."""
    entries = list(entries)
    known = {entry["id"]: entry for entry in entries}
    listed: dict[str, set[str]] = {}
    for entry in entries:
        for test in entry["tests"]:
            key = function_key(test)
            if key is not None:
                listed.setdefault(key, set()).add(entry["id"])
    errors: list[str] = []
    for function in tree.real_tier():
        key = f"{function.path}::{function.name}"
        errors.extend(_function_declaration_errors(key, function, known, listed.get(key, set())))
    for key, ids in sorted(listed.items()):
        function = tree.functions.get(key)
        if function is None or not is_real_tier(function.path):
            continue
        for scenario in sorted(ids - set(function.declared)):
            if not known[scenario]["pending_merge"]:
                errors.append(f"{key}: the registry lists it for {scenario}, which its Scenarios: line does not name")
    return errors


def _function_declaration_errors(
    key: str, function: TestFunction, known: Mapping[str, Mapping[str, Any]], listed: set[str]
) -> list[str]:
    if not function.has_declaration:
        return [f"{key}: real-tier test without a 'Scenarios: ID, ...' docstring line"]
    if not function.declared:
        return [f"{key}: its Scenarios: line names no scenario"]
    errors = []
    for scenario in function.declared:
        if scenario not in known:
            errors.append(f"{key}: declares {scenario}, which is not in {REGISTRY}")
        elif scenario not in listed:
            errors.append(f"{key}: declares {scenario}, but the registry does not list this test for it")
    return errors


def validate(root: Path) -> list[str]:
    """Every registry problem, one line each (empty when the registry and the tests agree)."""
    try:
        entries = load(root)
    except RegistryError as exc:
        return [str(exc)]
    errors = schema_errors(entries)
    if errors:
        return errors
    errors = duplicate_ids(entries)
    tree = read_tree(root)
    errors += reference_errors(entries, tree)
    errors += declaration_errors(entries, tree)
    return errors


# -- the catalog ---------------------------------------------------------------------------------


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def _provider(entry: Mapping[str, Any]) -> str:
    if entry["provided_by"] == THIS_BRANCH:
        return "this repository"
    suffix = " (pending merge)" if entry["pending_merge"] else ""
    return f"`{entry['provided_by']}`{suffix}"


def _tests_cell(entry: Mapping[str, Any]) -> str:
    return "<br>".join(f"`{_cell(test)}`" for test in entry["tests"])


def _area_order(entry: Mapping[str, Any]) -> int:
    return list(AREA_TITLES).index(entry["area"]) if entry["area"] in AREA_TITLES else len(AREA_TITLES)


def render(entries: list[Mapping[str, Any]]) -> str:
    """The Markdown catalog of ``entries``."""
    by_area: dict[str, list[Mapping[str, Any]]] = {}
    for entry in sorted(entries, key=_area_order):
        by_area.setdefault(entry["area"], []).append(entry)
    lines = [
        "# End-to-end scenarios",
        "",
        "<!-- Generated by scripts/gen-scenarios-doc.py from tests/e2e/scenarios.json: do not edit. -->",
        "",
        "Every user-facing scenario of Fork for Linux (unofficial) and the end-to-end tests that cover",
        "it. The registry is `tests/e2e/scenarios.json`; `tests/test_e2e_scenarios.py` fails when a",
        "scenario has no test, a referenced test does not exist, or a real-tier test does not name its",
        "scenarios on a `Scenarios:` docstring line. Tiers: **real-fork** = `tests/e2e` (the official",
        "Fork under Wine, `scripts/e2e-docker.sh pytest tests/e2e`, CI `scripts/ci-e2e-wine.sh`);",
        "**e2e-wine-docker** = `tests/bridge` with `FL_REAL_WINE=1` (`FL_CI_STAGE=bridge",
        "./scripts/ci-docker.sh`); **de** = `tests/e2e_de` (one container per desktop);",
        "**packaging** = a function of `scripts/packaging-e2e-install.sh` in a packaging cell",
        "(`packaging:<format>:<function>`).",
        "",
        "| Area | Scenarios | In this repository | Pending merge |",
        "|---|---|---|---|",
    ]
    for area, items in by_area.items():
        here = sum(1 for item in items if item["provided_by"] == THIS_BRANCH)
        pending = sum(1 for item in items if item["pending_merge"])
        lines.append(f"| {AREA_TITLES.get(area, area)} | {len(items)} | {here} | {pending} |")
    lines.append(f"| **Total** | {len(entries)} | "
                 f"{sum(1 for item in entries if item['provided_by'] == THIS_BRANCH)} | "
                 f"{sum(1 for item in entries if item['pending_merge'])} |")
    for area, items in by_area.items():
        lines += ["", f"## {AREA_TITLES.get(area, area)}", "", "| Id | Scenario | Tiers | Provided by | Tests |",
                  "|---|---|---|---|---|"]
        for item in items:
            title = _cell(item["title"]) + (f" — {_cell(item['notes'])}" if item.get("notes") else "")
            lines.append(f"| {item['id']} | {title} | {', '.join(item['tiers'])} | {_provider(item)} | "
                         f"{_tests_cell(item)} |")
    return "\n".join(lines) + "\n"


def write(root: Path) -> None:
    """Regenerate the catalog."""
    (root / CATALOG).write_text(render(load(root)), encoding="utf-8")


def check(root: Path) -> list[str]:
    """Catalog drift: empty when the file matches the registry."""
    try:
        current = (root / CATALOG).read_text(encoding="utf-8")
    except OSError:
        current = ""
    if current != render(load(root)):
        return [f"{CATALOG} is out of date: run 'python3 scripts/gen-scenarios-doc.py write'"]
    return []


# -- pending references --------------------------------------------------------------------------


def _git_tree(root: Path, ref: str) -> TestTree | None:
    """The test tree of a git ref (``git show``), or None when the ref does not exist."""
    probe = subprocess.run(["git", "-C", str(root), "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
                           capture_output=True, text=True, check=False)
    if probe.returncode != 0:
        return None
    listing = subprocess.run(["git", "-C", str(root), "ls-tree", "-r", "--name-only", ref, "--", "tests", "scripts"],
                             capture_output=True, text=True, check=True).stdout.split()
    tree = TestTree()
    for name in listing:
        text = subprocess.run(["git", "-C", str(root), "show", f"{ref}:{name}"],
                              capture_output=True, text=True, check=False).stdout
        if name == PACKAGING_MATRIX.as_posix():
            match = FORMATS_RE.search(text)
            tree.cells = set(match.group("cells").split()) if match else set()
        elif name == PACKAGING_E2E.as_posix():
            tree.checks = {match.group("name") for match in SHELL_FUNCTION_RE.finditer(text)}
        elif re.search(r"(^|/)test_[^/]*\.py$", name) and name.startswith("tests/"):
            tree.functions.update(_parse_text(name, text))
    return tree


def _parse_text(name: str, text: str) -> dict[str, TestFunction]:
    try:
        module = ast.parse(text, filename=name)
    except SyntaxError:
        return {}
    found = {}
    for func_name, node in _functions(module):
        has, declared = parse_declaration(ast.get_docstring(node))
        found[f"{name}::{func_name}"] = TestFunction(name, func_name, _params(node), declared, has)
    return found


def pending(root: Path) -> list[str]:
    """One line per pending scenario: which of its references its branch already has."""
    lines = []
    trees: dict[str, TestTree | None] = {}
    for entry in load(root):
        if not entry["pending_merge"]:
            continue
        branch = entry["provided_by"]
        if branch not in trees:
            trees[branch] = _git_tree(root, branch)
        tree = trees[branch]
        if tree is None:
            lines.append(f"{entry['id']}: branch {branch} does not exist yet")
            continue
        for test in entry["tests"]:
            why = resolve(test, tree)
            lines.append(f"{entry['id']}: {test}: {'present on ' + branch if why is None else why}")
    return lines


def clear_pending(root: Path) -> int:
    """Drop every pending flag (after the final merge); return how many were cleared."""
    entries = load(root)
    cleared = 0
    for entry in entries:
        if entry.get("pending_merge"):
            entry["pending_merge"] = False
            cleared += 1
    (root / REGISTRY).write_text(json.dumps(entries, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return cleared


def main(argv: list[str] | None = None, root: Path = ROOT) -> int:
    """Command line entry point."""
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1 or args[0] not in ("validate", "write", "check", "pending", "clear-pending"):
        print(__doc__.split("\n\n")[2], file=sys.stderr)
        return 2
    command = args[0]
    if command == "write":
        write(root)
        print(f"wrote {CATALOG}")
        return 0
    if command == "clear-pending":
        print(f"cleared {clear_pending(root)} pending flag(s); now run 'write' and 'validate'")
        return 0
    problems = {"validate": validate, "check": check, "pending": pending}[command](root)
    for line in problems:
        print(line)
    if command == "pending":
        return 1 if problems else 0
    if not problems:
        print(f"{command}: OK")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
