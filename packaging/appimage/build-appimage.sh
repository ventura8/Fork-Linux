#!/usr/bin/env bash
# build-appimage.sh - build fork-linux-<VERSION>-x86_64.AppImage (+ .zsync) into artifacts/.
#
# The AppDir is the relocatable meson install (prefix /usr, shebangs "/usr/bin/env -S python3",
# flavor appimage) with the static musl fl-bridge-helper (ET_EXEC), find-python.sh, AppRun, and
# the desktop entry + our neutral icon at the AppDir root. No Python, no Wine, no Fork inside:
# about 1-2 MB. Update information: GitHub releases zsync.
# Runs inside docker/Dockerfile.release (appimagetool pinned there).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/release-common.sh
. "${HERE}/../../scripts/release-common.sh"

VERSION="$(fl_read_version)"
APP_ID="$(fl_app_id)"
APPIMAGETOOL="${APPIMAGETOOL:-appimagetool}"
WORK="$(mktemp -d /tmp/fl-appimage.XXXXXX)"
trap 'rm -rf "${WORK}"' EXIT
APPDIR="${WORK}/AppDir"
OUTPUT="${FL_ARTIFACTS_DIR}/fork-linux-${VERSION}-x86_64.AppImage"
UPDATE_INFO="gh-releases-zsync|ventura8|Fork-Linux|latest|fork-linux-*-x86_64.AppImage.zsync"

command -v "${APPIMAGETOOL}" >/dev/null 2>&1 || {
	echo "error: appimagetool not found (set APPIMAGETOOL=...)" >&2
	exit 1
}
fl_prepare_artifacts_dir

fl_meson_setup "${WORK}/build" --prefix=/usr -Dflavor=appimage -Dpython="${FL_PORTABLE_PYTHON}" -Dtests=false
fl_meson_install "${WORK}/build" "${APPDIR}"
fl_install_static_helper "${WORK}/build" "${APPDIR}/usr"
install -m 0755 "${FL_REPO_ROOT}/packaging/common/find-python.sh" "${APPDIR}/usr/lib/fork-linux/find-python.sh"
install -m 0755 "${HERE}/AppRun" "${APPDIR}/AppRun"
# appimagetool wants the desktop entry and its icon at the AppDir root.
cp "${APPDIR}/usr/share/applications/${APP_ID}.desktop" "${APPDIR}/${APP_ID}.desktop"
cp "${APPDIR}/usr/share/icons/hicolor/scalable/apps/${APP_ID}.svg" "${APPDIR}/${APP_ID}.svg"
ln -s "${APP_ID}.svg" "${APPDIR}/.DirIcon"

(cd "${FL_ARTIFACTS_DIR}" && ARCH=x86_64 "${APPIMAGETOOL}" --no-appstream -u "${UPDATE_INFO}" "${APPDIR}" "${OUTPUT}")
ls -la "${OUTPUT}" "${OUTPUT}.zsync"
echo "Built AppImage: ${OUTPUT}"
