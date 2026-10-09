#!/usr/bin/env bash
# release-portable.sh - build the portable channels of Fork for Linux (unofficial) into
# artifacts/: the relocatable tarball, the AppImage, the Flatpak bundle and the snap.
#
# Usage: ./scripts/release-portable.sh tarball|appimage|flatpak|snap
#   tarball   fork-linux-VERSION-x86_64.tar.gz: fork-linux-VERSION/{bin,lib,share} (install.sh
#             installs it; shebangs "/usr/bin/env -S python3", static fl-bridge-helper)
#   appimage  fork-linux-VERSION-x86_64.AppImage (+ .zsync)   packaging/appimage/build-appimage.sh
#   flatpak   io.github.ventura8.ForkLinux-VERSION-x86_64.flatpak (flatpak-builder, BaseApp
#             org.winehq.Wine wow64-25.08; runtimes from Flathub)
#   snap      fork-linux_VERSION_amd64.snap (core24, classic; inside docker/Dockerfile.snap)
# tarball / appimage / flatpak run inside docker/Dockerfile.release.
set -euo pipefail

cd "$(dirname "$0")/.."
# shellcheck source=scripts/release-common.sh
. scripts/release-common.sh

VERSION="$(fl_read_version)"
MODE="${1:?usage: release-portable.sh tarball|appimage|flatpak|snap}"
fl_prepare_artifacts_dir

build_tarball() {
	local work stage root out
	work="$(mktemp -d /tmp/fl-tarball.XXXXXX)"
	stage="${work}/stage"
	root="fork-linux-${VERSION}"
	out="${FL_ARTIFACTS_DIR}/fork-linux-${VERSION}-x86_64.tar.gz"
	echo "==> tarball ${out}"
	fl_meson_setup "${work}/build" --prefix="/${root}" -Dflavor=tarball -Dpython="${FL_PORTABLE_PYTHON}" -Dtests=false
	fl_meson_install "${work}/build" "${stage}"
	fl_install_static_helper "${work}/build" "${stage}/${root}"
	install -m 0755 packaging/common/find-python.sh "${stage}/${root}/lib/fork-linux/find-python.sh"
	install -D -m 0644 LICENSE "${stage}/${root}/share/doc/fork-linux/LICENSE"
	install -m 0644 README.md "${stage}/${root}/share/doc/fork-linux/README.md"
	tar -C "${stage}" --sort=name --owner=0 --group=0 --numeric-owner \
		--mtime="@${SOURCE_DATE_EPOCH:-0}" -czf "${out}" "${root}"
	rm -rf "${work}"
	ls -la "${out}"
}

build_appimage() {
	echo "==> AppImage"
	APPIMAGETOOL="${APPIMAGETOOL:-/usr/local/bin/appimagetool}" packaging/appimage/build-appimage.sh
}

build_flatpak() {
	local app_id work arch bundle
	app_id="$(fl_app_id)"
	work="${FL_ARTIFACTS_DIR}/.flatpak-work"
	arch="$(uname -m)"
	bundle="${FL_ARTIFACTS_DIR}/${app_id}-${VERSION}-${arch}.flatpak"
	echo "==> Flatpak ${bundle}"
	rm -rf "${work}"
	mkdir -p "${work}"
	cp "packaging/flatpak/${app_id}.yml" "${work}/${app_id}.yml"
	fl_create_source_tarball "${VERSION}" "${work}/fork-linux-src.tar.gz"
	flatpak remote-add --user --if-not-exists flathub https://dl.flathub.org/repo/flathub.flatpakrepo
	flatpak-builder --user --install-deps-from=flathub --disable-rofiles-fuse --force-clean \
		--state-dir="${work}/state" --repo="${work}/repo" "${work}/build" "${work}/${app_id}.yml"
	flatpak build-bundle "${work}/repo" "${bundle}" "${app_id}"
	rm -rf "${work}"
	ls -la "${bundle}"
}

build_snap() {
	local tarball="packaging/snap/fork-linux-src.tar.gz"
	echo "==> snap"
	rm -f "${tarball}"
	# A clean tarball, not the live tree: craft-parts would otherwise copy packaging/ (its own
	# output directory) into the part.
	fl_create_source_tarball "${VERSION}" "${tarball}"
	(
		cd packaging/snap
		rm -rf parts stage prime overlay
		snapcraft pack --destructive-mode --output "${FL_ARTIFACTS_DIR}/fork-linux_${VERSION}_amd64.snap"
		rm -rf parts stage prime overlay
	)
	rm -f "${tarball}"
	ls -la "${FL_ARTIFACTS_DIR}/fork-linux_${VERSION}_amd64.snap"
}

case "${MODE}" in
	tarball) build_tarball ;;
	appimage) build_appimage ;;
	flatpak) build_flatpak ;;
	snap) build_snap ;;
	*)
		echo "usage: release-portable.sh tarball|appimage|flatpak|snap" >&2
		exit 2
		;;
esac
