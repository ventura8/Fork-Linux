#!/usr/bin/env python3
"""Check the packages' runtime dependencies of Fork for Linux (unofficial) against hostdeps.

``fork_linux.hostdeps`` is the single table of the host libraries and tools the managed Wine
runtime needs (``REQUIRED_LIBS``, ``REQUIRED_TOOLS``) and their package names per
distribution family. This script checks that every packaging recipe asks for exactly those:

* debian/control ``Depends`` lists the Debian names (with the ``a | b`` t64 alternatives);
* the RPM specs require every library by soname (``libX.so.N()(64bit)``) and every tool;
* packaging/arch/PKGBUILD.in ``depends`` lists the Arch names;
* none of them depends on the distribution's ``wine`` package (AGENTS.md §4.9).

With ``--available FAMILY`` (inside that distribution's image) it also asks the package
manager whether each name exists. Exit status 0 when consistent, 1 otherwise.
"""

from __future__ import annotations

import argparse
import importlib
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parent.parent
CONTROL = "debian/control"
PKGBUILD_IN = "packaging/arch/PKGBUILD.in"
WINE_DEP = re.compile(r"(?<![\w.-])wine(?:64|32)?(?![\w.-])")


def _hostdeps() -> ModuleType:
    if str(ROOT / "src") not in sys.path:
        sys.path.insert(0, str(ROOT / "src"))
    return importlib.import_module("fork_linux.hostdeps")


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def debian_depends(control: str) -> list[str]:
    """The ``Depends`` entries of the binary package (``a | b`` kept as one entry)."""
    return debian_field(control, "Depends")


def _strip_version(entry: str) -> str:
    """``name (>= 1)`` -> ``name`` (string operations only: no backtracking regex)."""
    while "(" in entry and ")" in entry.split("(", 1)[1]:
        head, rest = entry.split("(", 1)
        entry = head.rstrip() + rest.split(")", 1)[1]
    return entry.strip()


def debian_field(control: str, field: str) -> list[str]:
    """A relationship field of the binary package (``a | b`` kept as one entry)."""
    stanza = control.split("\nPackage: fork-linux\n", 1)[-1].splitlines()
    header = f"{field}:"
    if header not in stanza:
        return []
    entries: list[str] = []
    for line in stanza[stanza.index(header) + 1 :]:
        if not line.startswith(" "):
            break
        entry = _strip_version(line.strip().rstrip(","))
        if entry:
            entries.append(entry.replace(" | ", "|"))
    return entries


def rpm_requires(spec: str) -> list[str]:
    """The ``Requires:`` values of a spec (version constraints dropped)."""
    return rpm_field(spec, "Requires")


def rpm_field(spec: str, field: str) -> list[str]:
    """The values of one dependency tag of a spec (version constraints dropped)."""
    prefix = f"{field}:"
    values = [line[len(prefix) :].strip() for line in spec.splitlines() if line.startswith(prefix)]
    return [value.split()[0] for value in values if value]


def _bash_array(pkgbuild: str, name: str) -> list[str]:
    """The single-quoted items of the PKGBUILD array ``name=( ... )``."""
    start = pkgbuild.find(f"\n{name}=(")
    if start < 0:
        return []
    body = pkgbuild[start + len(name) + 3 :].split(")", 1)[0]
    return [item for index, item in enumerate(body.split("'")) if index % 2]


def arch_depends(pkgbuild: str) -> list[str]:
    """The ``depends=(...)`` names of a PKGBUILD."""
    return _bash_array(pkgbuild, "depends")


def arch_optdepends(pkgbuild: str) -> list[str]:
    """The ``optdepends=(...)`` entries of a PKGBUILD."""
    return _bash_array(pkgbuild, "optdepends")


def _missing(where: str, have: set[str], wanted: dict[str, str]) -> list[str]:
    """``where: lacks X (for name)`` for every wanted package not in ``have``."""
    return [f"{where}: lacks {package} (for {name})" for name, package in wanted.items() if package not in have]


def _declared() -> dict[str, list[str]]:
    """Every dependency name each recipe declares (Depends / Requires / Recommends / Suggests / optdepends)."""
    control = _read(CONTROL)
    pkgbuild = _read(PKGBUILD_IN)
    declared = {
        CONTROL: [
            alt
            for field in ("Depends", "Recommends", "Suggests")
            for entry in debian_field(control, field)
            for alt in entry.split("|")
        ],
        PKGBUILD_IN: [
            *arch_depends(pkgbuild),
            *[opt.split(":")[0] for opt in arch_optdepends(pkgbuild)],
        ],
    }
    for family in ("fedora", "opensuse"):
        rel = f"packaging/rpm/{family}/fork-linux.spec"
        spec = _read(rel)
        declared[rel] = [dep for field in ("Requires", "Recommends", "Suggests") for dep in rpm_field(spec, field)]
    return declared


def problems() -> list[str]:
    """Every inconsistency between the recipes and hostdeps."""
    hostdeps = _hostdeps()
    names = [*hostdeps.REQUIRED_TOOLS, *hostdeps.REQUIRED_LIBS]
    found = _missing(
        f"{CONTROL} Depends",
        set(debian_depends(_read(CONTROL))),
        {name: hostdeps.PACKAGES["debian"][name] for name in names},
    )
    wanted_rpm = {lib: f"{lib}()(64bit)" for lib in hostdeps.REQUIRED_LIBS}
    wanted_rpm.update({tool: tool for tool in hostdeps.REQUIRED_TOOLS})
    for family in ("fedora", "opensuse"):
        rel = f"packaging/rpm/{family}/fork-linux.spec"
        found += _missing(f"{rel} Requires", set(rpm_requires(_read(rel))), wanted_rpm)
    found += _missing(
        "packaging/arch/PKGBUILD.in depends",
        set(arch_depends(_read(PKGBUILD_IN))),
        {name: hostdeps.PACKAGES["arch"][name] for name in names},
    )
    for rel, deps in _declared().items():
        if any(WINE_DEP.fullmatch(dep.strip()) for dep in deps):
            found.append(f"{rel}: depends on the distribution's wine package")
    return found


def _manager_has(family: str, name: str) -> bool:
    commands = {
        "debian": ["apt-cache", "show", name],
        "fedora": ["dnf", "-q", "repoquery", "--whatprovides", name],
        "suse": ["zypper", "-q", "what-provides", name],
        "arch": ["pacman", "-Si", name],
    }
    result = subprocess.run(commands[family], capture_output=True, text=True, check=False)
    return result.returncode == 0 and bool(result.stdout.strip())


def unavailable(family: str) -> list[str]:
    """hostdeps names of ``family`` (first alternative that exists) the package manager lacks."""
    hostdeps = _hostdeps()
    missing = []
    for name in [*hostdeps.REQUIRED_TOOLS, *hostdeps.REQUIRED_LIBS]:
        options = hostdeps.PACKAGES[family][name].split("|")
        if not any(_manager_has(family, option) for option in options):
            missing.append(f"{family}: none of {', '.join(options)} exists (for {name})")
    return missing


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--available", choices=("debian", "fedora", "suse", "arch"))
    args = parser.parse_args(argv)
    found = problems()
    if args.available:
        tool = {"debian": "apt-cache", "fedora": "dnf", "suse": "zypper", "arch": "pacman"}[args.available]
        if shutil.which(tool) is None:
            parser.error(f"--available {args.available} needs {tool}")
        found += unavailable(args.available)
    for line in found:
        print(line, file=sys.stderr)
    if found:
        return 1
    print("check-dep-names.py: dependency names agree with fork_linux.hostdeps")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
