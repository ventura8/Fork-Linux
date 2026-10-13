#!/usr/bin/env bash
# release-deb.sh - build the fork-linux .deb (and its 3.0 (native) source package) into
# artifacts/, then run lintian --fail-on error (no overrides exist; AGENTS.md §4.9).
#
# Runs inside docker/Dockerfile.ppa (Ubuntu 26.04) or docker/Dockerfile.ppa.jammy (22.04).
# The build happens in a private copy of the tree under /tmp, so parallel packaging cells
# never share debian/ or obj-* build directories.
set -euo pipefail

cd "$(dirname "$0")/.."
# shellcheck source=scripts/release-common.sh
. scripts/release-common.sh

VERSION="$(fl_read_version)"
fl_prepare_artifacts_dir

WORK="$(mktemp -d /tmp/fl-deb.XXXXXX)"
trap 'rm -rf "${WORK}"' EXIT
fl_create_source_tarball "${VERSION}" "${WORK}/fork-linux-${VERSION}.tar.gz"
tar -C "${WORK}" -xzf "${WORK}/fork-linux-${VERSION}.tar.gz"

changelog_version="$(dpkg-parsechangelog -l debian/changelog -S Version)"
if [[ "${changelog_version}" != "${VERSION}" ]]; then
	echo "error: debian/changelog is at ${changelog_version}, VERSION is ${VERSION}" >&2
	exit 1
fi

# The changelog names the baseline series; a build on another release (deb-jammy) uses
# that release's codename in its private copy so lintian accepts the .changes.
codename="$(sed -n 's/^VERSION_CODENAME=//p' /etc/os-release | tr -d '"')"
if [[ -n "${codename}" ]]; then
	sed -i "1s/) [a-z]*;/) ${codename};/" "${WORK}/fork-linux-${VERSION}/debian/changelog"
fi

# lintian_policy: the newest Debian Policy this release's lintian knows (empty when unknown).
lintian_policy() {
	python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["releases"][0]["version"])' \
		/usr/share/lintian/data/debian-policy/releases.json 2>/dev/null || true
	return 0
}

# A release whose lintian knows only an older Debian Policy than debian/control declares
# (jammy: 4.6.0.1) gets that one as Standards-Version in its private copy, as a backport to it
# would declare; lintian would otherwise warn newer-standards-version.
policy="$(lintian_policy)"
declared="$(sed -n 's/^Standards-Version: *//p' debian/control)"
if [[ -n "${policy}" ]] && dpkg --compare-versions "${declared}" gt "${policy}"; then
	echo "==> Standards-Version ${declared} -> ${policy} (the newest policy this release's lintian knows)"
	sed -i "s/^Standards-Version: .*/Standards-Version: ${policy}/" "${WORK}/fork-linux-${VERSION}/debian/control"
fi

# Only the .deb is collected (fl_collect_artifacts_glob below), never a -dbgsym package, so none
# is built: jammy's lintian also misreads a PIE's debug file ("elf-error ... program interpreter").
echo "==> dpkg-buildpackage (source + binary, unsigned, no dbgsym) in ${WORK}"
(cd "${WORK}/fork-linux-${VERSION}" &&
	DEB_BUILD_OPTIONS="${DEB_BUILD_OPTIONS:+${DEB_BUILD_OPTIONS} }noautodbgsym" dpkg-buildpackage -us -uc)

echo "==> lintian (any E: tag fails the build; no overrides)"
chmod -R a+rX "${WORK}"
lintian_cmd=(lintian --fail-on error --display-info --pedantic "${WORK}/fork-linux_${VERSION}_amd64.changes")
if [[ "$(id -u)" == 0 ]] && id nobody >/dev/null 2>&1; then
	# lintian refuses to be comfortable as root; it only reads the build results.
	runuser -u nobody -- env HOME=/tmp "${lintian_cmd[@]}"
else
	"${lintian_cmd[@]}"
fi

fl_collect_artifacts_glob "${WORK}/fork-linux_${VERSION}_amd64.deb"
ls -la "${FL_ARTIFACTS_DIR}/"
