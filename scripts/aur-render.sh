#!/usr/bin/env bash
# aur-render.sh - render the AUR PKGBUILD and .SRCINFO of Fork for Linux (unofficial) for a
# release tag (dry run: nothing is pushed to the AUR; AGENTS.md hard rule 17).
#
# Usage: ./scripts/aur-render.sh [--tarball FILE] [--out DIR]
#   --tarball FILE  the tag's source archive (default: download
#                   https://github.com/ventura8/Fork-Linux/archive/vVERSION.tar.gz)
#   --out DIR       where PKGBUILD and .SRCINFO go (default: artifacts/aur)
# The archive's top directory is Fork-Linux-VERSION (GitHub's tag archive layout).
set -euo pipefail

cd "$(dirname "$0")/.."
# shellcheck source=scripts/release-common.sh
. scripts/release-common.sh

VERSION="$(fl_read_version)"
TARBALL=""
OUT="${FL_ARTIFACTS_DIR}/aur"
while (($# > 0)); do
	case "$1" in
		--tarball)
			TARBALL="${2:?--tarball needs a file}"
			shift 2
			;;
		--out)
			OUT="${2:?--out needs a directory}"
			shift 2
			;;
		-h | --help)
			sed -n '5,9p' "$0" | sed 's/^# \{0,1\}//'
			exit 0
			;;
		*)
			echo "aur-render.sh: unknown argument: $1" >&2
			exit 2
			;;
	esac
done

URL="https://github.com/ventura8/Fork-Linux/archive/v${VERSION}.tar.gz"
mkdir -p "${OUT}"
if [[ -z "${TARBALL}" ]]; then
	TARBALL="${OUT}/fork-linux-${VERSION}.tar.gz"
	curl -fsSL --proto '=https' --tlsv1.2 -o "${TARBALL}" "${URL}"
fi
SHA256="$(sha256sum "${TARBALL}" | awk '{print $1}')"

fl_render_pkgbuild "${VERSION}" "fork-linux-${VERSION}.tar.gz::${URL}" "${SHA256}" \
	"Fork-Linux-${VERSION}" >"${OUT}/PKGBUILD"
fl_render_srcinfo "${OUT}/PKGBUILD" >"${OUT}/.SRCINFO"
echo "==> ${OUT}/PKGBUILD and ${OUT}/.SRCINFO for ${VERSION} (sha256 ${SHA256})"
