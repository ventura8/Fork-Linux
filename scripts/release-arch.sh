#!/usr/bin/env bash
# release-arch.sh - build the fork-linux Arch package from the working tree into artifacts/
# (makepkg as an unprivileged builder; namcap-free: makepkg's own checks + meson test), and
# check that scripts/aur-render.sh's .SRCINFO renderer agrees with makepkg --printsrcinfo.
#
# Runs inside docker/Dockerfile.arch (archlinux:base-devel, dated tag).
set -euo pipefail

cd "$(dirname "$0")/.."
# shellcheck source=scripts/release-common.sh
. scripts/release-common.sh

VERSION="$(fl_read_version)"
fl_prepare_artifacts_dir

WORK="$(mktemp -d /tmp/fl-makepkg.XXXXXX)"
trap 'rm -rf "${WORK}"' EXIT
TARBALL="fork-linux-${VERSION}.tar.gz"
fl_create_source_tarball "${VERSION}" "${WORK}/${TARBALL}"
SHA256="$(sha256sum "${WORK}/${TARBALL}" | awk '{print $1}')"
fl_render_pkgbuild "${VERSION}" "${TARBALL}" "${SHA256}" "fork-linux-${VERSION}" >"${WORK}/PKGBUILD"

run_as_builder() {
	if [[ "$(id -u)" == 0 ]]; then
		chown -R builder:builder "${WORK}"
		runuser -u builder -- env HOME="${WORK}/home" "$@"
	else
		env HOME="${WORK}/home" "$@"
	fi
}

mkdir -p "${WORK}/home"
echo "==> .SRCINFO renderer vs makepkg --printsrcinfo"
fl_render_srcinfo "${WORK}/PKGBUILD" >"${WORK}/SRCINFO.ours"
(cd "${WORK}" && run_as_builder makepkg --printsrcinfo) >"${WORK}/SRCINFO.makepkg"
diff -u "${WORK}/SRCINFO.makepkg" "${WORK}/SRCINFO.ours"

if [[ "$(id -u)" == 0 ]]; then
	# makepkg -s would need sudo for the builder; install depends + makedepends as root.
	mapfile -t deps < <(awk -F' = ' '$1 ~ /^\t(make)?depends$/ {print $2}' "${WORK}/SRCINFO.ours")
	pacman -S --needed --noconfirm "${deps[@]}"
fi

echo "==> makepkg"
(cd "${WORK}" && run_as_builder env BUILDDIR="${WORK}/build" PKGDEST="${WORK}" SRCDEST="${WORK}" \
	makepkg --noconfirm --cleanbuild)

fl_collect_artifacts_glob "${WORK}/fork-linux-${VERSION}-*-x86_64.pkg.tar.zst"
ls -la "${FL_ARTIFACTS_DIR}/"
