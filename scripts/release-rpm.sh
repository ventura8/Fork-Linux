#!/usr/bin/env bash
# release-rpm.sh - build the fork-linux RPM for Fedora or openSUSE into artifacts/, then run
# rpmlint (no filters, no rpmlintrc; AGENTS.md §4.9) on the binary and source RPMs.
#
# Usage: ./scripts/release-rpm.sh fedora|opensuse
# Runs inside docker/Dockerfile.rpm.fedora (Fedora 44) or docker/Dockerfile.rpm.opensuse
# (Leap 16.0). The version is passed as -D fl_version from VERSION.
set -euo pipefail

DISTRO="${1:?usage: release-rpm.sh fedora|opensuse}"
case "${DISTRO}" in
	fedora | opensuse) ;;
	*)
		echo "error: unknown distro ${DISTRO} (fedora or opensuse)" >&2
		exit 2
		;;
esac
cd "$(dirname "$0")/.."
# shellcheck source=scripts/release-common.sh
. scripts/release-common.sh

VERSION="$(fl_read_version)"
fl_prepare_artifacts_dir

SPEC="packaging/rpm/${DISTRO}/fork-linux.spec"
RPMTOP="$(mktemp -d /tmp/fl-rpmbuild.XXXXXX)"
trap 'rm -rf "${RPMTOP}"' EXIT
mkdir -p "${RPMTOP}"/{SOURCES,SPECS,RPMS,SRPMS,BUILD}
fl_create_source_tarball "${VERSION}" "${RPMTOP}/SOURCES/fork-linux-${VERSION}.tar.gz"
cp "${SPEC}" "${RPMTOP}/SPECS/fork-linux.spec"

DIST_ARGS=()
if [[ "${DISTRO}" == "opensuse" ]]; then
	# openSUSE does not define %dist; tag the file with the Leap release like Fedora's .fcNN.
	DIST_ARGS=(-D "dist .lp160")
fi

rpmbuild -ba "${RPMTOP}/SPECS/fork-linux.spec" \
	-D "fl_version ${VERSION}" \
	-D "_topdir ${RPMTOP}" \
	"${DIST_ARGS[@]}"

mapfile -t rpms < <(find "${RPMTOP}/RPMS" -name "fork-linux-${VERSION}-*.x86_64.rpm" -type f)
mapfile -t srpms < <(find "${RPMTOP}/SRPMS" -name "fork-linux-${VERSION}-*.src.rpm" -type f)
if ((${#rpms[@]} == 0)); then
	echo "error: no RPM produced for ${VERSION}" >&2
	exit 1
fi

echo "==> rpmlint (no filters of ours; any E: or W: line fails the build)"
lint_out="${RPMTOP}/rpmlint.txt"
rc=0
rpmlint "${rpms[@]}" "${srpms[@]}" "${RPMTOP}/SPECS/fork-linux.spec" >"${lint_out}" 2>&1 || rc=$?
cat "${lint_out}"
# openSUSE's rpmlint scores "badness" and exits 0 below a threshold, so the exit status
# alone is not enough: every error or warning line counts.
if ((rc != 0)) || grep -Eq ': [EW]: ' "${lint_out}"; then
	echo "error: rpmlint reported errors or warnings (exit ${rc})" >&2
	exit 1
fi

cp -a "${rpms[@]}" "${FL_ARTIFACTS_DIR}/"
ls -la "${FL_ARTIFACTS_DIR}/"
