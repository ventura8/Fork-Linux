"""Contract tests for the SonarQube Cloud setup (sonar-project.properties + scripts/ci-sonar.sh)."""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PROPS = REPO / "sonar-project.properties"
SCRIPT = REPO / "scripts" / "ci-sonar.sh"


def _properties() -> dict[str, str]:
    """Parse the .properties file (continuation lines joined)."""
    joined = re.sub(r"\\\n\s*", "", PROPS.read_text(encoding="utf-8"))
    props: dict[str, str] = {}
    for line in joined.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            key, _, value = line.partition("=")
            props[key.strip()] = value.strip()
    return props


def test_project_identity() -> None:
    props = _properties()
    assert props["sonar.organization"] == "ventura8"
    assert props["sonar.projectKey"] == "ventura8_Fork-Linux"
    assert props["sonar.host.url"] == "https://sonarcloud.io"
    assert "sonar.projectVersion" not in props  # passed from VERSION by the script


def test_source_and_test_paths_exist() -> None:
    props = _properties()
    for key in ("sonar.sources", "sonar.tests"):
        for path in props[key].split(","):
            assert (REPO / path.strip()).exists(), f"{key}: {path} does not exist"


def test_python_versions_cover_supported_range() -> None:
    versions = [v.strip() for v in _properties()["sonar.python.version"].split(",")]
    assert versions[0] == "3.10"
    assert _properties()["sonar.python.coverage.reportPaths"] == "artifacts/coverage/coverage.xml"


def test_no_unscoped_issue_ignores() -> None:
    props = _properties()
    ids = [i for i in props.get("sonar.issue.ignore.multicriteria", "").split(",") if i]
    for ident in ids:
        assert props[f"sonar.issue.ignore.multicriteria.{ident}.ruleKey"]
        resource = props[f"sonar.issue.ignore.multicriteria.{ident}.resourceKey"]
        assert resource not in ("**", "**/*"), f"{ident} is not scoped to a path"


def test_scanner_image_is_pinned_and_token_never_in_argv() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    image = re.search(r"sonarsource/sonar-scanner-cli:([^}\"\s]+)", text)
    assert image is not None
    assert image.group(1) not in ("latest", "")
    assert re.fullmatch(r"[0-9][0-9._]+", image.group(1))
    assert "-K -" in text  # token passed to curl on stdin
    assert "--env SONAR_TOKEN" in text
    assert "SONAR_TOKEN=$" not in text
    assert SCRIPT.stat().st_mode & 0o111
