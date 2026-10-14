"""Contract tests of the desktop E2E tier: recipes, images and checkers stay in step.

Every ``docker/Dockerfile.e2e.de.<key>`` has a recipe in ``tests/e2e_de/desktops.py`` and the
other way round; each image installs Wine's host libraries (``fork_linux.hostdeps``, Debian
names, like docker/Dockerfile.e2e.wine), the tier's own tools and the desktop's packages the
recipe names; every kind a recipe uses has a checker in ``test_de_integration.py``.
"""

from __future__ import annotations

import ast
import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType

import pytest

from fork_linux import hostdeps

REPO = Path(__file__).resolve().parents[1]
E2E_DE = REPO / "tests" / "e2e_de"


def _load_desktops() -> ModuleType:
    """tests/e2e_de/desktops.py (pure data) as the module ``desktops``, as the tier imports it."""
    if "desktops" in sys.modules:
        return sys.modules["desktops"]
    spec = importlib.util.spec_from_file_location("desktops", E2E_DE / "desktops.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["desktops"] = module
    spec.loader.exec_module(module)
    return module


desktops = _load_desktops()

DOCKERFILES = sorted((REPO / "docker").glob("Dockerfile.e2e.de.*"))
# What the tier itself runs in every desktop image (tests/e2e_de, install.sh, the stub app).
TIER_PACKAGES = (
    "python3", "python3-pytest", "python3-gi", "git", "curl", "xvfb", "xdotool", "x11-utils", "imagemagick",
    "dbus-daemon", "libglib2.0-bin", "desktop-file-utils", "xdg-utils", "shared-mime-info", "zenity",
    "cabextract", "unzip", "7zip", "fontconfig", "procps",
)
# Host packages fork-linux's doctor knows that a desktop image does not need (KDE's dialog tool).
NOT_NEEDED = {"kdialog"}


def _packages(path: Path) -> set[str]:
    text = path.read_text(encoding="utf-8")
    block = text.split("apt-get install", 1)[1].split("\n\n", 1)[0]
    return set(re.findall(r"(?<![\w.-])[a-z0-9][a-z0-9.+-]+", block))


def _checker_keys(name: str) -> set[str]:
    """``desktops.X`` keys of the dict assigned to ``name`` in test_de_integration.py."""
    tree = ast.parse((E2E_DE / "test_de_integration.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == name:
            assert isinstance(node.value, ast.Dict)
            return {getattr(desktops, key.attr) for key in node.value.keys if isinstance(key, ast.Attribute)}
    raise AssertionError(f"test_de_integration.py has no {name}")


def test_every_image_has_a_recipe_and_every_recipe_an_image() -> None:
    assert DOCKERFILES, "no desktop images"
    assert sorted(path.name.rsplit(".", 1)[1] for path in DOCKERFILES) == sorted(desktops.DESKTOPS)


def _workflow_matrix(workflow: str, job: str) -> list[str]:
    """The ``desktop`` matrix of ``job`` in ``.github/workflows/<workflow>``."""
    text = (REPO / ".github" / "workflows" / workflow).read_text(encoding="utf-8")
    body = re.search(rf"^  {job}:\n(.*?)(?=^  [\w-]+:\n|\Z)", text, re.MULTILINE | re.DOTALL)
    assert body is not None, f"{workflow} has no job {job}"
    matrix = re.search(r"^\s+desktop: \[(.*?)\]", body.group(1), re.MULTILINE)
    assert matrix is not None, f"{workflow} {job} has no desktop matrix"
    return [item.strip() for item in matrix.group(1).split(",")]


@pytest.mark.parametrize(
    ("workflow", "job"),
    [("check.yml", "e2e-de"), ("e2e-wine.yml", "e2e-de")],
    ids=["integration-on-every-pr", "real-fork-weekly"],
)
def test_every_desktop_is_in_the_ci_matrices(workflow: str, job: str) -> None:
    """A desktop with an image and a recipe runs in CI: its integration tier on every pull request
    (check.yml) and its real-Fork tier with the Xvfb one (e2e-wine.yml), each desktop once."""
    matrix = _workflow_matrix(workflow, job)
    assert len(matrix) == len(set(matrix))
    assert sorted(matrix) == sorted(desktops.DESKTOPS)


def test_the_local_matrix_runs_every_image() -> None:
    """scripts/ci-e2e-de-matrix.sh (the pipeline's e2e-de stage) runs every Dockerfile's desktop."""
    text = (REPO / "scripts" / "ci-e2e-de-matrix.sh").read_text(encoding="utf-8")
    assert '"${ROOT}"/docker/Dockerfile.e2e.de.*' in text
    pipeline = (REPO / "scripts" / "ci-pipeline.sh").read_text(encoding="utf-8")
    assert "./scripts/ci-e2e-de-matrix.sh" in pipeline


@pytest.mark.parametrize("path", DOCKERFILES, ids=lambda p: p.name)
def test_image_tag_and_packages(path: Path) -> None:
    key = path.name.rsplit(".", 1)[1]
    text = path.read_text(encoding="utf-8")
    assert f"# Image tag: fork-linux-e2e-de-{key}:26.04" in text
    assert not re.search('^(COPY|ADD) ', text, re.MULTILINE)
    assert 'latest' not in text
    packages = _packages(path)
    wine_host = {
        alternatives.split("|")[0] for name, alternatives in hostdeps.PACKAGES["debian"].items()
        if alternatives not in NOT_NEEDED and name not in NOT_NEEDED
    }
    assert wine_host <= packages, sorted(wine_host - packages)
    assert set(TIER_PACKAGES) <= packages, sorted(set(TIER_PACKAGES) - packages)
    assert set(desktops.DESKTOPS[key].packages) <= packages


@pytest.mark.parametrize("key", sorted(desktops.DESKTOPS))
def test_recipe_is_complete(key: str) -> None:
    recipe = desktops.DESKTOPS[key]
    assert recipe.key == key
    assert recipe.integration in desktops.INTEGRATION_KINDS
    assert recipe.menu_reader in desktops.MENU_READERS
    assert recipe.browser in desktops.BROWSER_KINDS
    # light and dark, plus light:<how> / dark:<how> for other ways to the same result
    assert {desktops.LIGHT, desktops.DARK} <= set(recipe.themes)
    assert {name.partition(":")[0] for name in recipe.themes} == {desktops.LIGHT, desktops.DARK}
    assert all(recipe.themes.values()), "each theme needs the commands that set it"
    expected = [case.expected for case in recipe.scales]
    assert 100 in expected, 'scaling is checked at 100 and 200 %'
    assert 200 in expected, 'scaling is checked at 100 and 200 %'
    assert len({case.id for case in recipe.scales}) == len(recipe.scales)
    assert recipe.reset, "theme and scale cases start from the desktop's defaults"
    for name in ("XDG_CURRENT_DESKTOP", "DESKTOP_SESSION", "XDG_SESSION_DESKTOP"):
        assert recipe.session_env.get(name), name
    assert recipe.open_in_fork_keys
    assert recipe.open_in_fork_keys[-1] == 'Return'
    assert recipe.settings_daemon
    # The session checks that the desktop's own programs run (dekit.Session.start).
    assert recipe.components
    assert all(recipe.components)
    assert recipe.portal == "" or recipe.portal.startswith("xdg-desktop-portal-")
    # An empty window manager or file-manager service: the session manager starts it.
    for command in (recipe.settings_daemon, recipe.window_manager, recipe.file_manager_service):
        assert all(isinstance(word, str) and word for word in command)


def test_every_kind_in_use_has_a_checker() -> None:
    used_integrations = {recipe.integration for recipe in desktops.DESKTOPS.values()}
    used_readers = {recipe.menu_reader for recipe in desktops.DESKTOPS.values()}
    assert used_integrations <= _checker_keys("INTEGRATION_CHECKS")
    assert used_readers <= _checker_keys("MENU_READERS")


def test_the_tier_is_gated_and_containers_only() -> None:
    for name in ("test_de_integration.py", "test_de_fork.py", "conftest.py"):
        text = (E2E_DE / name).read_text(encoding="utf-8")
        assert "real_tier.enforce()" in text, name
    assert 'os.environ.get("FL_E2E_DE")' in (E2E_DE / "test_de_integration.py").read_text(encoding="utf-8")
    fork = (E2E_DE / "test_de_fork.py").read_text(encoding="utf-8")
    assert 'os.environ.get("FL_E2E_DE_FORK") != "1"' in fork
    assert "allow_module_level=True" in fork
