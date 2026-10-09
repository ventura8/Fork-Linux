#!/usr/bin/env bash
# packaging-smoke-verify.sh - check that a packaging cell produced its artifact(s) for VERSION
# under artifacts/ (FL_ARTIFACTS_DIR), non-empty and amd64 / x86_64 only. Runs on the host
# after the build and before the live E2E (scripts/packaging-e2e-install.sh).
#
# Usage: ./scripts/packaging-smoke-verify.sh <format>
# Formats: deb deb-jammy rpm-fedora rpm-opensuse arch snap appimage flatpak tarball
set -euo pipefail

cd "$(dirname "$0")/.."
# shellcheck source=scripts/release-common.sh
. scripts/release-common.sh

FORMAT="${1:?usage: packaging-smoke-verify.sh <format>}"
VERSION="$(fl_read_version)"
APP_ID="$(fl_app_id)"
ART="${FL_ARTIFACTS_DIR}"

expect() {
	local pattern="$1" match found=0
	for match in ${ART}/${pattern}; do
		[[ -s "${match}" ]] || continue
		echo "ok  $(basename "${match}") ($(wc -c <"${match}") bytes)"
		found=1
	done
	if ((found == 0)); then
		echo "error: ${FORMAT}: no artifact matches ${ART}/${pattern}" >&2
		exit 1
	fi
}

reject() {
	local pattern="$1" match
	for match in ${ART}/${pattern}; do
		if [[ -e "${match}" ]]; then
			echo "error: ${FORMAT}: unexpected artifact $(basename "${match}") (amd64 / x86_64 only)" >&2
			exit 1
		fi
	done
}

case "${FORMAT}" in
	deb | deb-jammy)
		expect "fork-linux_${VERSION}_amd64.deb"
		reject "fork-linux_*_{arm64,i386,armhf,all}.deb"
		;;
	rpm-fedora) expect "fork-linux-${VERSION}-*.fc*.x86_64.rpm" ;;
	rpm-opensuse) expect "fork-linux-${VERSION}-*.lp*.x86_64.rpm" ;;
	arch) expect "fork-linux-${VERSION}-*-x86_64.pkg.tar.zst" ;;
	snap) expect "fork-linux_${VERSION}_amd64.snap" ;;
	appimage)
		expect "fork-linux-${VERSION}-x86_64.AppImage"
		expect "fork-linux-${VERSION}-x86_64.AppImage.zsync"
		;;
	flatpak) expect "${APP_ID}-${VERSION}-x86_64.flatpak" ;;
	tarball)
		expect "fork-linux-${VERSION}-x86_64.tar.gz"
		tar -tzf "${ART}/fork-linux-${VERSION}-x86_64.tar.gz" >"${ART}/.tarball-list"
		for f in bin/fork-linux bin/fork lib/fork-linux/fl-bridge-helper lib/fork-linux/win64/fl-shim.exe \
			lib/fork-linux/win64/fl-launch.exe share/fork-linux/fork_linux/_build.py \
			"share/applications/${APP_ID}.desktop" "share/metainfo/${APP_ID}.metainfo.xml"; do
			grep -qx "fork-linux-${VERSION}/${f}" "${ART}/.tarball-list" || {
				echo "error: tarball lacks fork-linux-${VERSION}/${f}" >&2
				exit 1
			}
		done
		rm -f "${ART}/.tarball-list"
		echo "ok  tarball layout"
		;;
	*)
		echo "error: unknown format ${FORMAT}" >&2
		exit 2
		;;
esac
echo "==> smoke verify OK: ${FORMAT} ${VERSION}"
