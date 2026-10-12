#!/usr/bin/env bash
# build-version-variant.sh - build Fork for Linux (unofficial) packages that claim another
# version (an "N-1" such as 0.9.9, or an "N+1"), for the upgrade and downgrade tests of
# docs/qa/UPDATE-SCENARIOS.md (A1, A2, A8, A10), without editing the real tree.
#
# Usage: ./scripts/build-version-variant.sh [--tree-only] [--date YYYY-MM-DD] VERSION OUTDIR [FORMAT...]
#
#   1. copies the tree as git sees it (tracked + untracked-but-not-ignored, like the release
#      scripts' source tarball) into a private directory under ${TMPDIR:-/tmp};
#   2. scripts/version-variant.py rewrites the copy: VERSION, the debian/changelog and RPM
#      %changelog top entries, the metainfo <release> and docs/releases/vVERSION*.md stubs,
#      so the version contract (AGENTS.md §4.7.2) holds for VERSION;
#   3. runs each FORMAT's release script inside the copy with FL_ARTIFACTS_DIR=OUTDIR.
#
# Run it where those release scripts run: inside the format's packaging image (the snap
# through docker/snap-entrypoint.sh), e.g. from scripts/ci-packaging-cell.sh. --tree-only
# stops after step 2 and moves the variant tree to OUTDIR/fork-linux-VERSION. The copy is
# always deleted at exit; the real tree is never written.
#
# Formats: deb deb-jammy rpm-fedora rpm-opensuse arch snap appimage flatpak tarball
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FL_REPO_ROOT="${ROOT}"
# shellcheck source=scripts/release-common.sh
. "${ROOT}/scripts/release-common.sh"

usage() {
	echo "usage: build-version-variant.sh [--tree-only] [--date YYYY-MM-DD] VERSION OUTDIR [FORMAT...]" >&2
	exit 2
}

# release_command FORMAT: the release script (and its arguments) that builds FORMAT.
release_command() {
	case "$1" in
		deb | deb-jammy) echo "./scripts/release-deb.sh" ;;
		rpm-fedora) echo "./scripts/release-rpm.sh fedora" ;;
		rpm-opensuse) echo "./scripts/release-rpm.sh opensuse" ;;
		arch) echo "./scripts/release-arch.sh" ;;
		tarball | appimage | flatpak | snap) echo "./scripts/release-portable.sh $1" ;;
		*) return 1 ;;
	esac
	return 0
}

TREE_ONLY=0
DATE_ARGS=()
while (($# > 0)); do
	case "$1" in
		--tree-only) TREE_ONLY=1 ;;
		--date)
			(($# >= 2)) || usage
			DATE_ARGS=(--date "$2")
			shift
			;;
		--help | -h) usage ;;
		--*)
			echo "error: unknown option $1" >&2
			usage
			;;
		*) break ;;
	esac
	shift
done
(($# >= 2)) || usage
VARIANT="$1"
OUTDIR="$2"
shift 2
FORMATS=("$@")

[[ "${VARIANT}" =~ ^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ ]] || {
	echo "error: VERSION must be N.N.N, got: ${VARIANT}" >&2
	exit 2
}
if ((TREE_ONLY && ${#FORMATS[@]} > 0)); then
	echo "error: --tree-only builds nothing; drop the formats" >&2
	usage
fi
if ((TREE_ONLY == 0 && ${#FORMATS[@]} == 0)); then
	echo "error: name at least one format to build (or pass --tree-only)" >&2
	usage
fi
for fmt in "${FORMATS[@]}"; do
	release_command "${fmt}" >/dev/null || {
		echo "error: unknown format ${fmt}" >&2
		exit 2
	}
done

mkdir -p "${OUTDIR}"
OUT="$(cd "${OUTDIR}" && pwd)"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/fl-variant-${VARIANT}.XXXXXX")"
trap 'rm -rf "${WORK:?}"' EXIT
# Readable by all: release-deb.sh runs lintian as "nobody" from inside the tree.
chmod 0755 "${WORK}"
TREE="${WORK}/fork-linux-${VARIANT}"
mkdir -m 0755 "${TREE}"

echo "==> copying the tree (as git sees it) to ${TREE}"
fl_copy_source_tree "${TREE}"
echo "==> rewriting the copy for version ${VARIANT}"
python3 "${ROOT}/scripts/version-variant.py" "${TREE}" "${VARIANT}" "${DATE_ARGS[@]}"

if ((TREE_ONLY)); then
	dest="${OUT}/fork-linux-${VARIANT}"
	if [[ -e "${dest}" || -L "${dest}" ]]; then
		echo "error: ${dest} already exists" >&2
		exit 1
	fi
	mv "${TREE}" "${dest}"
	echo "==> variant tree ${dest}"
	exit 0
fi

for fmt in "${FORMATS[@]}"; do
	read -r -a cmd <<<"$(release_command "${fmt}")"
	echo "==> building ${fmt} ${VARIANT} into ${OUT}"
	# The copy has no git metadata of its own; GIT_CEILING_DIRECTORIES keeps git from taking
	# a checkout around ${TMPDIR} for it.
	(cd "${TREE}" && env -u FL_REPO_ROOT FL_ARTIFACTS_DIR="${OUT}" GIT_CEILING_DIRECTORIES="${WORK}" "${cmd[@]}")
done
echo "==> version variant ${VARIANT}: ${FORMATS[*]} in ${OUT}"
