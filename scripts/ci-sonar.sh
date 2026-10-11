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
#   FL_SONAR_COVERAGE=1  — regenerate the coverage reports first (otherwise existing
#                          ones are reused): artifacts/coverage/coverage.xml with pytest
#                          on the host, and artifacts/coverage/c-coverage.xml with
#                          scripts/ci-c-coverage.sh (native unit + daemon tests and the
#                          Wine tier against --coverage builds of the bridge, in Docker),
#                          then gate them per file (every file >= 90%, enforced by
#                          scripts/check-coverage.py) before anything is uploaded.
#   FL_SONAR_IMAGE       — override the pinned scanner image.
#   FL_SONAR_BRANCH      — the branch to file the analysis under (default: the checked-out
#                          branch; a detached HEAD needs it). Any branch other than main is
#                          sent as sonar.branch.name, so a local scan never replaces main's.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCANNER_IMAGE="${FL_SONAR_IMAGE:-sonarsource/sonar-scanner-cli:12.2.0.4256_8.1.0}"
COVERAGE_XML="${ROOT}/artifacts/coverage/coverage.xml"
C_COVERAGE_XML="${ROOT}/artifacts/coverage/c-coverage.xml"
SONAR_HOST="https://sonarcloud.io"
PROJECT_KEY="ventura8_Fork-Linux"

cd "${ROOT}"

load_token() {
	# A pasted secret often carries a trailing newline or spaces; the API rejects those.
	SONAR_TOKEN="$(printf '%s' "${SONAR_TOKEN:-}" | tr -d '[:space:]')"
	if [[ -z "${SONAR_TOKEN}" && -f "${ROOT}/.sonar-token" ]]; then
		SONAR_TOKEN="$(tr -d '[:space:]' <"${ROOT}/.sonar-token")"
		export SONAR_TOKEN
		echo "==> SONAR_TOKEN loaded from .sonar-token"
	fi
	export SONAR_TOKEN
	if [[ -z "${SONAR_TOKEN}" ]]; then
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
		# The runner reads workflow commands from stderr as well as stdout.
		echo "::error title=SonarQube Cloud token::${message}" >&2
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
		echo "==> refreshing C coverage (scripts/ci-c-coverage.sh)"
		"${ROOT}/scripts/ci-c-coverage.sh"
		echo "==> gate: every file >= 90% (scripts/check-coverage.py) before the scan"
		python3 "${ROOT}/scripts/check-coverage.py" --min 90 --min-python-line 100 --min-python-branch 100
	fi
	if [[ ! -f "${COVERAGE_XML}" ]]; then
		echo "==> note: ${COVERAGE_XML#"${ROOT}"/} is missing — analysing without Python coverage."
		echo "    Run FL_SONAR_COVERAGE=1 $0 first."
	fi
}

run_scanner() {
	local version opts branch common_dir
	version="$(python3 "${ROOT}/scripts/read-version.py")"
	opts="-Dsonar.projectVersion=${version} -Dsonar.projectBaseDir=${ROOT}"
	# Without sonar.branch.name the analysis replaces main's: name every other branch,
	# and refuse a detached HEAD unless FL_SONAR_BRANCH says which branch it is.
	branch="${FL_SONAR_BRANCH:-$(git -C "${ROOT}" symbolic-ref --short -q HEAD || true)}"
	if [[ -z "${branch}" ]]; then
		echo "error: detached HEAD; set FL_SONAR_BRANCH to the branch this analysis belongs to." >&2
		exit 2
	fi
	if [[ "${branch}" != "main" ]]; then
		opts="${opts} -Dsonar.branch.name=${branch}"
	fi
	# A linked worktree keeps its git data in the main checkout: mount it read-only so
	# the scanner can read SCM data (new-code detection, blame).
	common_dir="$(git -C "${ROOT}" rev-parse --path-format=absolute --git-common-dir)"
	# The generic C coverage report is optional: without it Sonar would abort the scan.
	if [[ ! -f "${C_COVERAGE_XML}" ]]; then
		echo "==> note: ${C_COVERAGE_XML#"${ROOT}"/} is missing — analysing without C coverage."
		opts="${opts} -Dsonar.coverageReportPaths="
	fi
	echo "==> sonar-scanner ${SCANNER_IMAGE} (project version ${version}, branch ${branch})"
	# Mounted at the SAME absolute path as on the host: the coverage reports and
	# coverage.xml carry absolute paths, and Sonar matches them literally.
	docker run --rm \
		--user "$(id -u):$(id -g)" \
		--env SONAR_TOKEN \
		--env "SONAR_SCANNER_OPTS=${opts}" \
		--volume "${ROOT}:${ROOT}" \
		--volume "${common_dir}:${common_dir}:ro" \
		--workdir "${ROOT}" \
		"${SCANNER_IMAGE}"
}

load_token
check_token
if [[ "${1:-}" == "--check-token" ]]; then
	exit 0
fi
ensure_coverage
run_scanner
echo "==> analysis submitted; results: ${SONAR_HOST}/project/overview?id=${PROJECT_KEY}"
