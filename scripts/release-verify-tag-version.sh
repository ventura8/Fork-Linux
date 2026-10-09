#!/usr/bin/env bash
# release-verify-tag-version.sh - the first step of release.yml: the release tag must be
# vN.N.N and equal the repo-root VERSION (AGENTS.md §4.7.2), and the release notes for it
# must exist (docs/releases/vN.N.N.md and vN.N.N_github_description.md) before anything is
# built. Exports VERSION to $GITHUB_ENV when run in GitHub Actions.
#
# Usage: GITHUB_REF_NAME=v1.0.0 ./scripts/release-verify-tag-version.sh
#        ./scripts/release-verify-tag-version.sh v1.0.0
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

tag="${1:-${GITHUB_REF_NAME:-}}"
if [[ ! "${tag}" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
	echo "error: release tag must match vN.N.N, got: ${tag:-<empty>}" >&2
	exit 1
fi
tag_version="${tag#v}"
repo_version="$(python3 scripts/read-version.py)"
if [[ "${tag_version}" != "${repo_version}" ]]; then
	echo "error: tag ${tag} does not match VERSION ${repo_version}" >&2
	exit 1
fi
for notes in "docs/releases/v${repo_version}.md" "docs/releases/v${repo_version}_github_description.md"; do
	if [[ ! -s "${notes}" ]]; then
		echo "error: missing release notes ${notes}" >&2
		exit 1
	fi
done
changelog_version="$(sed -n '1s/^fork-linux (\([^)]*\)).*/\1/p' debian/changelog)"
if [[ "${changelog_version}" != "${repo_version}" ]]; then
	echo "error: debian/changelog top entry is ${changelog_version}, VERSION is ${repo_version}" >&2
	exit 1
fi
if [[ -n "${GITHUB_ENV:-}" ]]; then
	echo "VERSION=${repo_version}" >>"${GITHUB_ENV}"
fi
echo "release ${tag}: VERSION, debian/changelog and release notes agree"
