#!/usr/bin/env python3
"""Local stand-in for the SonarCloud rules a scan of this repository has flagged, without uploading.

usage: scripts/sonar-local.py [--root DIR] [--properties FILE] [--max-complexity N]
                              [--min-lines N] [--min-tokens N] [CHECK ...]

A real SonarCloud analysis uploads to the project (publishing) and SonarCloud refuses to
serve the results of a branch other than main to this organisation, so a branch can only be
checked before its pull request with this script. It applies, to the files SonarCloud
analyses (sonar.sources minus sonar.exclusions in sonar-project.properties, the same scope
scripts/check-coverage.py reads):

  complexity    python:S3776, cognitive complexity of every Python function at most
                --max-complexity (default 15), counted with SonarSource's rules: +1 and the
                nesting level for if / ternary / for / while / except / match, +1 for elif
                and else, +1 for each sequence of like boolean operators; nested functions,
                lambdas and classes raise the nesting level and count in the enclosing
                function
  shell         shelldre:S7682, every shell function ends with an explicit return or exit
                (or exec); shelldre:S131, every case has a '*)' default branch
  duplication   duplicated blocks, as SonarSource's CPD sees them for most languages:
                --min-lines (default 10) or more successive non-blank, non-comment lines
                (whitespace-normalised) holding --min-tokens (default 100) or more tokens,
                repeated in Python, shell or C files, within a file or across files

Without CHECK every check runs. Exit status: 0 nothing found, 1 findings (listed as
"path:line: rule: message"), 2 bad input. Python 3.10, standard library only.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.util
import re
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parent.parent
CHECKS = ("complexity", "shell", "duplication")
MAX_COMPLEXITY = 15
MIN_LINES = 10
MIN_TOKENS = 100

_SHEBANG_SHELL = re.compile(r"^#!.*(/|env )(ba|da)?sh(\s|$)")
_FUNCTION = re.compile(r"^(?P<indent>\s*)(?:function\s+)?(?P<name>[A-Za-z_][\w:.-]*)\s*\(\)\s*\{(?P<rest>.*)$")
_HEREDOC = re.compile(r"<<-?\s*(?P<quote>['\"]?)(?P<word>[A-Za-z_]\w*)(?P=quote)")
_TERMINAL = re.compile(r"^(?:return|exit|exec)\b")
_CASE = re.compile(r"^\s*case\s.+\sin\s*$")
_ESAC = re.compile(r"^\s*esac\b")
_DEFAULT_BRANCH = re.compile(r"^\s*\(?\*\)")
_TOKEN = re.compile(r"\w+|[^\w\s]")


class InputError(Exception):
    """sonar-project.properties cannot be read (exit status 2)."""


@dataclass(frozen=True)
class Finding:
    """One problem at ``path:line``."""

    path: str
    line: int
    rule: str
    message: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.rule}: {self.message}"


def _load_coverage_module() -> ModuleType:
    """scripts/check-coverage.py, for its reading of sonar-project.properties (one scope, one parser)."""
    name = "fork_linux_sonar_local_check_coverage"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name("check-coverage.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses look their module up while the class is built
    spec.loader.exec_module(module)
    return module


def analysed_files(root: Path, properties: Path) -> list[str]:
    """Repository files SonarCloud analyses: in sonar.sources, not excluded, not ignored by git."""
    cov = _load_coverage_module()
    try:
        props = cov.parse_properties(properties.read_text(encoding="utf-8"))
        scope = cov.Scope.from_properties(props)
    except (OSError, cov.InputError) as exc:
        raise InputError(f"{properties}: {exc}") from exc

    def analysed(path: str) -> bool:
        if not scope.in_sources(path) or cov.matches_any(path, scope.exclusions):
            return False
        return not scope.inclusions or cov.matches_any(path, scope.inclusions)

    return [path for path in cov.repository_files(root) if analysed(path)]


def language(root: Path, path: str) -> str | None:
    """``python``, ``shell``, ``c`` or None for a file this script does not check."""
    suffix = Path(path).suffix
    if suffix == ".py":
        return "python"
    if suffix in (".c", ".h"):
        return "c"
    if suffix in (".sh", ".bash"):
        return "shell"
    if suffix:
        return None
    try:
        with open(root / path, encoding="utf-8", errors="replace") as handle:
            first = handle.readline()
    except OSError:
        return None
    return "shell" if _SHEBANG_SHELL.match(first) else None


# -- python:S3776 cognitive complexity ----------------------------------------------------------


class _Complexity(ast.NodeVisitor):
    """The cognitive complexity of one function body (SonarSource's rules for Python)."""

    def __init__(self) -> None:
        self.score = 0
        self.nesting = 0

    def _nested(self, nodes: list[ast.stmt] | list[ast.AST]) -> None:
        self.nesting += 1
        for node in nodes:
            self.visit(node)
        self.nesting -= 1

    def _structure(self) -> None:
        self.score += 1 + self.nesting

    def visit_If(self, node: ast.If) -> None:
        self._structure()
        self._if_rest(node)

    def _if_rest(self, node: ast.If) -> None:
        self.visit(node.test)
        self._nested(node.body)
        orelse = node.orelse
        if len(orelse) == 1 and isinstance(orelse[0], ast.If) and orelse[0].col_offset == node.col_offset:
            self.score += 1  # elif
            self._if_rest(orelse[0])
        elif orelse:
            self.score += 1  # else
            self._nested(orelse)

    def _loop(self, node: ast.For | ast.AsyncFor | ast.While) -> None:
        self._structure()
        for child in (node.test,) if isinstance(node, ast.While) else (node.target, node.iter):
            self.visit(child)
        self._nested(node.body)
        if node.orelse:
            self.score += 1
            self._nested(node.orelse)

    visit_For = visit_AsyncFor = visit_While = _loop

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        self._structure()
        if node.type is not None:
            self.visit(node.type)
        self._nested(node.body)

    def visit_IfExp(self, node: ast.IfExp) -> None:
        self._structure()
        self._nested([node.test, node.body, node.orelse])

    def visit_Match(self, node: ast.Match) -> None:
        self._structure()
        self.visit(node.subject)
        self._nested(node.cases)

    def visit_BoolOp(self, node: ast.BoolOp, outer: type[ast.boolop] | None = None) -> None:
        if type(node.op) is not outer:
            self.score += 1
        for value in node.values:
            if isinstance(value, ast.BoolOp):
                self.visit_BoolOp(value, type(node.op))
            else:
                self.visit(value)

    def _scope(self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.Lambda) -> None:
        body = node.body if isinstance(node.body, list) else [node.body]
        self._nested(body)

    visit_FunctionDef = visit_AsyncFunctionDef = visit_ClassDef = visit_Lambda = _scope


def function_complexity(node: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    """The cognitive complexity of a function, nested functions included."""
    counter = _Complexity()
    for statement in node.body:
        counter.visit(statement)
    return counter.score


def _functions(tree: ast.AST) -> Iterator[ast.FunctionDef | ast.AsyncFunctionDef]:
    """Module-level functions and methods (nested functions count in their enclosing function)."""
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node
        elif isinstance(node, ast.ClassDef):
            yield from _functions(node)


def check_complexity(path: str, text: str, limit: int) -> list[Finding]:
    """python:S3776 findings of one Python file."""
    try:
        tree = ast.parse(text, filename=path)
    except SyntaxError as exc:
        return [Finding(path, exc.lineno or 1, "python:syntax", str(exc.msg))]
    except ValueError as exc:  # Python 3.10 and 3.11: a NUL byte in the source
        return [Finding(path, 1, "python:syntax", str(exc))]
    findings = []
    for node in _functions(tree):
        score = function_complexity(node)
        if score > limit:
            message = f"cognitive complexity of {node.name} is {score} (more than {limit})"
            findings.append(Finding(path, node.lineno, "python:S3776", message))
    return findings


# -- shelldre:S7682 / S131 ----------------------------------------------------------------------


def _code_lines(text: str) -> list[tuple[int, str]]:
    """``(line number, line)`` of shell code: heredoc bodies, blank lines and comments left out."""
    lines: list[tuple[int, str]] = []
    terminator: tuple[str, bool] | None = None
    for number, line in enumerate(text.splitlines(), start=1):
        if terminator is not None:
            word, tabs = terminator
            if (line.lstrip("\t") if tabs else line) == word:
                terminator = None
            continue
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        lines.append((number, line))
        heredoc = _HEREDOC.search(line.replace("<<<", "   "))
        if heredoc is not None:
            terminator = (heredoc.group("word"), "<<-" in line)
    return lines


def _one_liner_ends_explicitly(rest: str) -> bool:
    """For ``name() { a; b; }`` on one line: whether the last command returns or exits."""
    body = rest.strip()
    body = body[:-1].rstrip() if body.endswith("}") else body
    last = body.rstrip(";").split(";")[-1].strip()
    return bool(_TERMINAL.match(last))


def _function_end(lines: list[tuple[int, str]], start: int, indent: str) -> int | None:
    """Index of the line closing the function opened at ``lines[start]`` (same indentation), or None."""
    for index in range(start + 1, len(lines)):
        line = lines[index][1]
        if line.rstrip() == indent + "}" or line.rstrip() == indent + "};":
            return index
    return None


def _missing_returns(path: str, lines: list[tuple[int, str]]) -> list[Finding]:
    findings = []
    for index, (number, line) in enumerate(lines):
        match = _FUNCTION.match(line)
        if match is None:
            continue
        name, rest = match.group("name"), match.group("rest").strip()
        if rest:
            explicit = _one_liner_ends_explicitly(rest)
        else:
            end = _function_end(lines, index, match.group("indent"))
            explicit = end is None or end == index + 1 or bool(_TERMINAL.match(lines[end - 1][1].strip()))
        if not explicit:
            message = f"function {name} does not end with an explicit return or exit"
            findings.append(Finding(path, number, "shelldre:S7682", message))
    return findings


def _cases_without_default(path: str, lines: list[tuple[int, str]]) -> list[Finding]:
    findings = []
    stack: list[list[int]] = []  # [line of 'case', has a default]
    for number, line in lines:
        if _CASE.match(line):
            stack.append([number, 0])
        elif _ESAC.match(line) and stack:
            opened, has_default = stack.pop()
            if not has_default:
                findings.append(Finding(path, opened, "shelldre:S131", "case without a '*)' default branch"))
        elif stack and _DEFAULT_BRANCH.match(line):
            stack[-1][1] = 1
    return findings


def check_shell(path: str, text: str) -> list[Finding]:
    """shelldre:S7682 and shelldre:S131 findings of one shell file."""
    lines = _code_lines(text)
    return _missing_returns(path, lines) + _cases_without_default(path, lines)


# -- duplicated blocks ---------------------------------------------------------------------------


def _normalised(text: str, lang: str) -> list[tuple[int, str]]:
    """``(line number, line)`` without blank lines and comment-only lines, whitespace collapsed."""
    comment = "//" if lang == "c" else "#"
    out = []
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = " ".join(line.split())
        if stripped and not stripped.startswith(comment) and not stripped.startswith(("/*", "*")):
            out.append((number, stripped))
    return out


def _stretches(matches: dict[int, tuple[str, int]], min_lines: int) -> list[tuple[int, int, str, int]]:
    """Merge window matches of one file into ``(first, end, other path, other first)`` line-index ranges.

    A window starting at index ``i`` that repeats the one at ``(other, j)`` continues the previous
    stretch when that one matched ``(other, j - 1)`` at ``i - 1``.
    """
    stretches: list[tuple[int, int, str, int]] = []
    for index in sorted(matches):
        other, other_index = matches[index]
        if stretches:
            first, end, last_other, last_first = stretches[-1]
            step = end - min_lines + 1  # the window right after the stretch's last one
            if index == step and (last_other, other_index) == (other, last_first + step - first):
                stretches[-1] = (first, index + min_lines, other, last_first)
                continue
        stretches.append((index, index + min_lines, other, other_index))
    return stretches


def check_duplication(files: dict[str, tuple[str, str]], min_lines: int, min_tokens: int) -> list[Finding]:
    """Stretches of at least ``min_lines`` normalised lines and ``min_tokens`` tokens that occur twice or more.

    One finding per later copy; the first copy (in path order) is the one named.
    """
    normalised = {path: _normalised(text, lang) for path, (text, lang) in sorted(files.items())}
    seen: dict[str, tuple[str, int]] = {}
    matches: dict[str, dict[int, tuple[str, int]]] = {}
    for path, lines in normalised.items():
        lang = files[path][1]
        for index in range(len(lines) - min_lines + 1):
            window = "\n".join(line for _number, line in lines[index : index + min_lines])
            key = hashlib.sha256(f"{lang}\0{window}".encode()).hexdigest()
            other, other_index = seen.setdefault(key, (path, index))
            if other != path or other_index + min_lines <= index:
                matches.setdefault(path, {})[index] = (other, other_index)
    findings = []
    for path, found in sorted(matches.items()):
        lines = normalised[path]
        for first, end, other, other_index in _stretches(found, min_lines):
            tokens = sum(len(_TOKEN.findall(line)) for _number, line in lines[first:end])
            if tokens >= min_tokens:
                where = f"{other}:{normalised[other][other_index][0]}"
                message = f"{end - first} lines ({tokens} tokens) duplicated from {where}"
                findings.append(Finding(path, lines[first][0], "common:duplicated-block", message))
    return findings


# -- command line ---------------------------------------------------------------------------------


def run(root: Path, files: list[str], checks: list[str], limit: int, min_lines: int, min_tokens: int) -> list[Finding]:
    """Every finding of ``checks`` over ``files`` (repository-relative paths)."""
    findings: list[Finding] = []
    sources: dict[str, tuple[str, str]] = {}
    for path in files:
        lang = language(root, path)
        if lang is None:
            continue
        text = (root / path).read_text(encoding="utf-8", errors="surrogateescape")
        sources[path] = (text, lang)
        if lang == "python" and "complexity" in checks:
            findings += check_complexity(path, text, limit)
        if lang == "shell" and "shell" in checks:
            findings += check_shell(path, text)
    if "duplication" in checks:
        findings += check_duplication(sources, min_lines, min_tokens)
    return findings


def main(argv: list[str] | None = None) -> int:
    """Run the checks; print the findings; 0 when there are none, 1 otherwise, 2 on bad input."""
    parser = argparse.ArgumentParser(prog="sonar-local.py", description=__doc__.split("\n\n")[0])
    parser.add_argument("--root", type=Path, default=ROOT, help="repository root (default: this checkout)")
    parser.add_argument("--properties", type=Path, help="default: <root>/sonar-project.properties")
    parser.add_argument("--max-complexity", type=int, default=MAX_COMPLEXITY, metavar="N")
    parser.add_argument("--min-lines", type=int, default=MIN_LINES, metavar="N")
    parser.add_argument("--min-tokens", type=int, default=MIN_TOKENS, metavar="N")
    parser.add_argument("checks", nargs="*", metavar="CHECK", help=", ".join(CHECKS))
    args = parser.parse_args(argv)
    unknown = sorted(set(args.checks) - set(CHECKS))
    if unknown:
        parser.error(f"unknown check(s): {', '.join(unknown)} (choose from {', '.join(CHECKS)})")
    root = args.root.resolve()
    checks = args.checks or list(CHECKS)
    try:
        files = analysed_files(root, args.properties or root / "sonar-project.properties")
    except InputError as exc:
        print(f"sonar-local.py: {exc}", file=sys.stderr)
        return 2
    findings = run(root, files, checks, args.max_complexity, args.min_lines, args.min_tokens)
    for finding in findings:
        print(finding)
    print(f"sonar-local: {len(files)} files, checks: {', '.join(checks)}; {len(findings)} finding(s)")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
