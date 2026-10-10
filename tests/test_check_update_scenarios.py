"""scripts/check-update-scenarios.py: the update scenario matrix stays consistent with the tests.

``test_the_matrix_is_consistent`` is the contract on docs/qa/UPDATE-SCENARIOS.md itself; the
other tests drive the checker on small matrices in a fake repository under ``tmp_path``.
"""

from __future__ import annotations

import importlib.util
import runpy
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check-update-scenarios.py"
NEW = "`tests/test_a.py::test_new`"
HEADER = "| ID | Scenario | Tiers | Expected | Covered by | Planned | Status |\n|---|---|---|---|---|---|---|\n"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_update_scenarios", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cus = _load()


def _row(sid: str, covered: str = "`tests/test_a.py::test_one`", planned: str = "—", status: str = "covered",
         tiers: str = "U") -> str:
    return f"| {sid} | something | {tiers} | it works | {covered} | {planned} | {status} |\n"


def _full(*rows: str) -> str:
    """A matrix holding every required ID (covered) plus ``rows``."""
    required = "".join(_row(sid) for sid in cus.REQUIRED)
    return HEADER + required + "".join(rows)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A fake repository with a few tests and a shell script."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text(
        "def test_one():\n    pass\n\n\ndef test_two():\n    pass\n\n\ndef helper():\n    pass\n", encoding="utf-8"
    )
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "e2e.sh").write_text("#!/bin/sh\ncycle() {\n\t:\n}\n", encoding="utf-8")
    return tmp_path


def _write(repo: Path, text: str) -> Path:
    path = repo / "docs" / "qa" / "UPDATE-SCENARIOS.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# --------------------------------------------------------------------- the real matrix


def test_the_matrix_is_consistent(capsys: pytest.CaptureFixture[str]) -> None:
    assert cus.main([]) == 0, capsys.readouterr().err
    assert "; 0 problem(s)" in capsys.readouterr().out


def test_the_matrix_names_every_required_scenario_and_tier() -> None:
    scenarios = cus.parse((ROOT / cus.MATRIX).read_text(encoding="utf-8"))
    ids = [scenario.id for scenario in scenarios]
    assert set(cus.REQUIRED) <= set(ids)
    assert {tier for scenario in scenarios for tier in scenario.tiers} == cus.TIERS


# --------------------------------------------------------------------- parsing


def test_parse_reads_rows_refs_and_continuations() -> None:
    text = HEADER + _row(
        "F1",
        covered="`tests/test_a.py::test_one`, `::test_two`, `scripts/e2e.sh::cycle`, `::cycle`",
        planned="`::test_three`",
        status="partial",
        tiers="U, R",
    ) + "| UPD-1 | not a scenario row |\n"
    (scenario,) = cus.parse(text)
    assert scenario.id == "F1"
    assert scenario.tiers == ["U", "R"]
    assert scenario.covered == [
        "tests/test_a.py::test_one",
        "tests/test_a.py::test_two",
        "scripts/e2e.sh::cycle",
        "scripts/e2e.sh::cycle",
    ]
    assert scenario.planned == ["::test_three"]
    assert scenario.status == "partial"
    assert scenario.line == 3


def test_parse_keeps_escaped_pipes_inside_cells() -> None:
    text = HEADER + "| A9 | x | U | `a\\|b` | `tests/test_a.py::test_one` | — | covered |\n"
    (scenario,) = cus.parse(text)
    assert scenario.covered == ["tests/test_a.py::test_one"]


def test_parse_rejects_a_row_with_the_wrong_number_of_cells() -> None:
    with pytest.raises(ValueError, match="F2 has 3 cells, expected 7"):
        cus.parse(HEADER + "| F2 | x | U |\n")


# --------------------------------------------------------------------- references


@pytest.mark.parametrize(
    ("ref", "found"),
    [
        ("tests/test_a.py::test_one", True),
        ("tests/test_a.py::test_one[param-1]", True),
        ("tests/test_a.py::test_three", False),
        ("tests/test_a.py::helpe", False),
        ("tests/test_a.py", True),
        ("tests/test_b.py::test_one", False),
        ("scripts/e2e.sh::cycle", True),
        ("scripts/e2e.sh::assert_markers", False),
        ("scripts/e2e.sh", True),
    ],
)
def test_exists(repo: Path, ref: str, found: bool) -> None:
    assert cus.exists(repo, ref) is found


@pytest.mark.parametrize("ref", ["::test_one", "docs/x.md", "tests/test_a.py::", "tests/a b.py"])
def test_exists_rejects_malformed_references(repo: Path, ref: str) -> None:
    with pytest.raises(ValueError, match="not a test reference"):
        cus.exists(repo, ref)


# --------------------------------------------------------------------- checks


def test_check_clean_matrix(repo: Path) -> None:
    scenarios = cus.parse(_full(_row("F99", planned="`tests/test_a.py::test_new`", status="partial")))
    assert cus.check(repo, scenarios) == []
    problems = cus.check(repo, scenarios, strict=True)
    assert problems == ["F99 (line 26): not covered yet (partial)"]


@pytest.mark.parametrize(
    ("row", "message"),
    [
        (_row("F1"), "duplicate ID"),
        (_row("F98", tiers="U, X"), "unknown tier(s) X (U, P or R)"),
        (_row("F98", status="done"), "status 'done' is not one of covered, partial, planned"),
        (_row("F98", planned=NEW), "status covered does not match its columns (1 covered, 1 planned)"),
        (_row("F98", covered="—", planned=NEW, status="partial"), "status partial does not match"),
        (_row("F98", covered="—", status="planned"), "status planned does not match its columns (0 covered, 0"),
        (_row("F98", covered="`tests/test_a.py::test_gone`"), "covered test does not exist: tests/test_a.py::test_go"),
        (
            _row("F98", planned="`tests/test_a.py::test_two`", status="partial"),
            "planned test already exists (move it to 'Covered by'): tests/test_a.py::test_two",
        ),
        (_row("F98", covered="`docs/notes.md`"), "not a test reference: 'docs/notes.md'"),
    ],
)
def test_check_reports(repo: Path, row: str, message: str) -> None:
    problems = cus.check(repo, cus.parse(_full(row)))
    assert len(problems) == 1, problems
    assert message in problems[0]


def test_check_reports_missing_required_scenarios(repo: Path) -> None:
    problems = cus.check(repo, cus.parse(HEADER + _row("F1")))
    assert "F2: required scenario is missing" in problems
    assert "A12: required scenario is missing" in problems
    assert len(problems) == len(cus.REQUIRED) - 1


# --------------------------------------------------------------------- command line


def test_main_clean_and_strict(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write(repo, _full(_row("F99", covered="—", planned="`tests/test_a.py::test_new`", status="planned")))
    assert cus.main(["--root", str(repo)]) == 0
    assert capsys.readouterr().out == "24 scenarios (23 covered, 0 partial, 1 planned); 0 problem(s)\n"
    assert cus.main(["--root", str(repo), "--strict"]) == 1
    captured = capsys.readouterr()
    assert "error: F99 (line 26): not covered yet (planned)" in captured.err
    assert captured.out.endswith("; 1 problem(s)\n")


def test_main_with_an_explicit_matrix(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    other = repo / "other.md"
    other.write_text(_full(), encoding="utf-8")
    assert cus.main(["--root", str(repo), str(other)]) == 0
    assert "23 scenarios" in capsys.readouterr().out


@pytest.mark.parametrize("text", [None, HEADER + "| F1 | x |\n"])
def test_main_reports_unreadable_or_malformed_matrices(
    repo: Path, capsys: pytest.CaptureFixture[str], text: str | None
) -> None:
    if text is not None:
        _write(repo, text)
    assert cus.main(["--root", str(repo)]) == 1
    assert capsys.readouterr().err.startswith("error: ")


def test_main_module_entry_point(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write(repo, _full())
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--root", str(repo)])
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(str(SCRIPT), run_name="__main__")
    assert exc.value.code == 0
