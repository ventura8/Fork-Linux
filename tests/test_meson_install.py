"""meson setup / compile / test + DESTDIR install: the installed layout of Fork for Linux (unofficial).

Checks the tree every package is built from (AGENTS.md §3, bridge/README.md "Install
layout"): launchers, the Python package with the generated _build.py, the host helpers, the
bridge helper, desktop entry, metainfo, icons, man pages, completions and the optional
system-wide file-manager actions. Needs meson, ninja and a C compiler (skipped otherwise).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from fork_linux import APP_ID

ROOT = Path(__file__).resolve().parents[1]
VERSION = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
PREFIX = "/usr"


@pytest.fixture(scope="module")
def installed(tmp_path_factory: pytest.TempPathFactory) -> Path:
    for tool in ("meson", "ninja", "cc"):
        if shutil.which(tool) is None:
            pytest.skip(f"{tool} is not installed (the compat and lint stages run this)")
    work = tmp_path_factory.mktemp("meson")
    build = work / "build"
    dest = work / "dest"
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    subprocess.run(
        ["meson", "setup", str(build), str(ROOT), f"--prefix={PREFIX}", "-Dbridge=disabled", "-Dtests=false",
         "-Dfile_manager_actions=true", "-Dflavor=test", "-Dpython=/usr/bin/python3"],
        check=True, capture_output=True, env=env,
    )
    subprocess.run(["meson", "compile", "-C", str(build)], check=True, capture_output=True, env=env)
    result = subprocess.run(["meson", "test", "-C", str(build), "--suite", "data", "--print-errorlogs"],
                            capture_output=True, text=True, env=env, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    subprocess.run(["meson", "install", "-C", str(build), "--no-rebuild", "--quiet"],
                   check=True, capture_output=True, env={**env, "DESTDIR": str(dest)})
    return dest / PREFIX.lstrip("/")


def _executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def test_launchers(installed: Path) -> None:
    launcher = installed / "bin" / "fork-linux"
    assert _executable(launcher)
    assert launcher.read_text(encoding="utf-8").splitlines()[0] == "#!/usr/bin/python3 -I"
    assert os.readlink(installed / "bin" / "fork") == "fork-linux"


def test_launcher_runs_from_the_install(installed: Path) -> None:
    out = subprocess.run([shutil.which("python3") or "python3", "-B", "-I", str(installed / "bin" / "fork-linux"),
                          "--version"], capture_output=True, text=True, check=True).stdout
    assert out.strip() == f"fork-linux {VERSION} (test)"


def test_python_package_is_complete(installed: Path) -> None:
    package = installed / "share" / "fork-linux" / "fork_linux"
    source = ROOT / "src" / "fork_linux"
    wanted = {
        str(p.relative_to(source))
        for p in source.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts and ".ruff_cache" not in p.parts and p.name != "_build.py"
    }
    have = {str(p.relative_to(package)) for p in package.rglob("*") if p.is_file()}
    assert wanted <= have
    assert not [p for p in have if "__pycache__" in p or p.endswith(".pyc")]
    build = (package / "_build.py").read_text(encoding="utf-8")
    assert f'VERSION = "{VERSION}"' in build
    assert 'FLAVOR = "test"' in build
    assert _executable(package / "data" / "templates" / "nautilus-script.in")


def test_host_helpers(installed: Path) -> None:
    libexec = installed / "lib" / "fork-linux"
    for name in ("fork-linux-host", "fork-linux-open-url", "fork-linux-explorer", "fork-linux-terminal",
                 "fork-linux-handoff"):
        assert _executable(libexec / name), name
    assert (libexec / "fork-linux-host").read_text(encoding="utf-8").startswith("#!/usr/bin/python3 -I\n")
    assert _executable(libexec / "fl-bridge-helper")
    for persona in ("fl-winexec", "fl-askpass", "fl-ssh-askpass"):
        assert os.readlink(libexec / persona) == "fl-bridge-helper"


def test_desktop_data(installed: Path) -> None:
    share = installed / "share"
    assert (share / "applications" / f"{APP_ID}.desktop").is_file()
    assert (share / "metainfo" / f"{APP_ID}.metainfo.xml").is_file()
    assert (share / "icons" / "hicolor" / "scalable" / "apps" / f"{APP_ID}.svg").is_file()
    assert (share / "icons" / "hicolor" / "symbolic" / "apps" / f"{APP_ID}-symbolic.svg").is_file()
    for page in ("fork-linux.1", "fork.1"):
        assert (share / "man" / "man1" / page).is_file()


def test_completions(installed: Path) -> None:
    share = installed / "share"
    assert (share / "bash-completion" / "completions" / "fork-linux").is_file()
    assert os.readlink(share / "bash-completion" / "completions" / "fork") == "fork-linux"
    assert (share / "zsh" / "site-functions" / "_fork-linux").is_file()
    assert (share / "fish" / "vendor_completions.d" / "fork-linux.fish").is_file()
    assert os.readlink(share / "fish" / "vendor_completions.d" / "fork.fish") == "fork-linux.fish"


def test_system_file_manager_actions(installed: Path) -> None:
    share = installed / "share"
    nemo = share / "nemo" / "actions" / "fork-linux-open.nemo_action"
    dolphin = share / "kio" / "servicemenus" / "fork-linux-open.desktop"
    fma = share / "file-manager" / "actions" / "fork-linux-open.desktop"
    nautilus = share / "nautilus-python" / "extensions" / "fork_linux_nautilus.py"
    for path in (nemo, dolphin, fma, nautilus):
        text = path.read_text(encoding="utf-8")
        assert "Installed system-wide by the fork-linux package" in text
        assert "fork-linux open --from-file-manager" in text or "'fork-linux'" in text
    assert _executable(dolphin)


def test_no_wine_and_no_fork_binaries_installed(installed: Path) -> None:
    for path in installed.rglob("*"):
        assert path.suffix.lower() not in {".exe", ".dll", ".msi", ".nupkg"} or "win64" in path.parts
        assert "wine" != path.name
