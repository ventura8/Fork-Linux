#!/usr/bin/env bash
# Run SonarQube Cloud analysis for Fork for Linux (unofficial).
#
# The local entrypoint. CI scans with SonarSource/sonarqube-scan-action as a step
# in the `coverage` job of .github/workflows/check.yml (the action derives the
# pull-request parameters from the Actions context), but calls this script with
# --check-token first so an expired token fails with a message that says so.
#
# Usage:
#   ./scripts/ci-sonar.sh                # validate the token, then scan
#   ./scripts/ci-sonar.sh --check-token  # only validate the token
#
# The scanner runs in Docker (sonarsource/sonar-scanner-cli, version-pinned —
# never ":latest", AGENTS.md §4.8), so nothing has to be installed on the host.
# Analysis settings live in sonar-project.properties at the repo root.
#
# Required:
#   SONAR_TOKEN   — SonarQube Cloud user/project token. Locally: export it, or
#                   put it in .sonar-token at the repo root (gitignored).
#                   In CI: the SONAR_TOKEN repository secret.
#
# Optional:
#   FL_SONAR_COVERAGE=1  — regenerate artifacts/coverage/coverage.xml with pytest on
#                          the host first (otherwise an existing report is reused).
#   FL_SONAR_IMAGE       — override the pinned scanner image.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCANNER_IMAGE="${FL_SONAR_IMAGE:-sonarsource/sonar-scanner-cli:12.2.0.4256_8.1.0}"
COVERAGE_XML="${ROOT}/artifacts/coverage/coverage.xml"
COMPILE_DB="${ROOT}/build-sonar/compile_commands.json"
SONAR_HOST="https://sonarcloud.io"
PROJECT_KEY="ventura8_Fork-Linux"

cd "${ROOT}"

load_token() {
	if [[ -z "${SONAR_TOKEN:-}" && -f "${ROOT}/.sonar-token" ]]; then
		SONAR_TOKEN="$(tr -d '[:space:]' <"${ROOT}/.sonar-token")"
		export SONAR_TOKEN
		echo "==> SONAR_TOKEN loaded from .sonar-token"
	fi
	if [[ -z "${SONAR_TOKEN:-}" ]]; then
		echo "error: SONAR_TOKEN is not set." >&2
		echo "       Create a token at ${SONAR_HOST}/account/security and either" >&2
		echo "       'export SONAR_TOKEN=...' or write it to ${ROOT}/.sonar-token" >&2
		exit 2
	fi
}

# An expired or revoked token otherwise surfaces deep inside the scanner as a
# generic failure. /api/authentication/validate answers {"valid":true|false}.
# If the service cannot be reached at all, warn and let the scan decide.
check_token() {
	local answer message
	# The token goes to curl on stdin (-K -), not argv, so it never shows up in ps.
	if ! answer="$(printf 'header = "Authorization: Bearer %s"\n' "${SONAR_TOKEN}" |
		curl --silent --show-error --fail --max-time 20 -K - "${SONAR_HOST}/api/authentication/validate" 2>&1)"; then
		echo "warning: could not reach ${SONAR_HOST} to validate SONAR_TOKEN (${answer}); continuing." >&2
		return 0
	fi
	if [[ "${answer}" == *'"valid":true'* ]]; then
		echo "==> SONAR_TOKEN is valid"
		return 0
	fi
	message="SONAR_TOKEN is invalid or expired. Create a new one at ${SONAR_HOST}/account/security, then update .sonar-token (local) and the SONAR_TOKEN repository secret (CI)."
	if [[ -n "${GITHUB_ACTIONS:-}" ]]; then
		echo "::error title=SonarQube Cloud token::${message}"
	fi
	echo "error: ${message}" >&2
	exit 2
}

ensure_coverage() {
	if [[ "${FL_SONAR_COVERAGE:-0}" == "1" ]]; then
		echo "==> refreshing Python coverage (pytest --cov-report=xml)"
		mkdir -p "$(dirname "${COVERAGE_XML}")"
		python3 -m pytest -q -p no:cacheprovider --cov=fork_linux --cov-branch \
			--cov-report="xml:${COVERAGE_XML}" tests
	fi
	if [[ ! -f "${COVERAGE_XML}" ]]; then
		echo "==> note: ${COVERAGE_XML#"${ROOT}"/} is missing — analysing without Python coverage."
		echo "    Run FL_SONAR_COVERAGE=1 $0 first."
	fi
}

# Sonar's C analyser needs a compile database; a native meson build gives one for
# bridge/common, bridge/unix and the native unit tests (no MinGW needed).
ensure_compile_db() {
	if [[ -f "${COMPILE_DB}" ]]; then
		return 0
	fi
	if command -v meson >/dev/null 2>&1 && [[ -f "${ROOT}/meson.build" ]]; then
		echo "==> generating build-sonar/compile_commands.json (meson, native only)"
		meson setup "${ROOT}/build-sonar" -Dbridge=disabled >/dev/null ||
			echo "warning: meson setup failed; C sources will be skipped by the scanner." >&2
	else
		echo "==> note: no meson; C sources will be skipped by the scanner."
	fi
}

run_scanner() {
	local version
	version="$(python3 "${ROOT}/scripts/read-version.py")"
	echo "==> sonar-scanner ${SCANNER_IMAGE} (project version ${version})"
	# Mounted at the SAME absolute path as on the host: the meson compile database and
	# coverage.xml carry absolute paths, and Sonar matches them literally.
	docker run --rm \
		--user "$(id -u):$(id -g)" \
		--env SONAR_TOKEN \
		--env "SONAR_SCANNER_OPTS=-Dsonar.projectVersion=${version} -Dsonar.projectBaseDir=${ROOT}" \
		--volume "${ROOT}:${ROOT}" \
		--workdir "${ROOT}" \
		"${SCANNER_IMAGE}"
}

load_token
check_token
if [[ "${1:-}" == "--check-token" ]]; then
	exit 0
fi
ensure_coverage
ensure_compile_db
run_scanner
echo "==> analysis submitted; results: ${SONAR_HOST}/project/overview?id=${PROJECT_KEY}"
