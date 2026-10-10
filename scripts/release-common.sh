#!/usr/bin/env bash
# release-common.sh - shared helpers for the release / packaging scripts of
# Fork for Linux (unofficial). Sourced, never executed.
#
#   fl_read_version                 VERSION (scripts/read-version.py, the single source)
#   fl_app_id                       the app id (fork_linux.APP_ID via scripts/gen-data.py)
#   fl_meson_setup DIR [opts...]    a fresh meson build dir for a package build
#   fl_meson_install DIR DESTDIR    compile + DESTDIR install
#   fl_install_static_helper BUILD ROOT
#                                   replace <ROOT>/lib/fork-linux/fl-bridge-helper with the
#                                   musl -static -no-pie build (portable channels: tarball,
#                                   AppImage, snap; bridge/README.md "Install layout")
#   fl_copy_source_tree DEST        copy the tree as git sees it into DEST
#   fl_git_mount_args               docker -v arguments for a linked worktree's git common dir
#   fl_create_source_tarball VER DEST
#                                   fork-linux-VER.tar.gz of the tree as git sees it (tracked +
#                                   untracked-but-not-ignored; not HEAD, so uncommitted
#                                   packaging fixes are exercised)
#   fl_collect_artifacts_glob GLOB...  copy matches into FL_ARTIFACTS_DIR (fails on none)
#   fl_appimage_squashfs_offset FILE   offset of an AppImage's squashfs (never runs it)
#   fl_render_pkgbuild VER SOURCE SHA256 SRCDIR   packaging/arch/PKGBUILD.in, rendered
#   fl_render_srcinfo PKGBUILD         the .SRCINFO of a rendered PKGBUILD (no makepkg needed)
set -euo pipefail

FL_REPO_ROOT="${FL_REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
FL_ARTIFACTS_DIR="${FL_ARTIFACTS_DIR:-${FL_REPO_ROOT}/artifacts}"
export FL_PORTABLE_PYTHON="/usr/bin/env -S python3"

fl_read_version() {
	python3 "${FL_REPO_ROOT}/scripts/read-version.py"
}

fl_app_id() {
	python3 "${FL_REPO_ROOT}/scripts/gen-data.py" name APP_ID
}

fl_prepare_artifacts_dir() {
	mkdir -p "${FL_ARTIFACTS_DIR}"
}

fl_meson_setup() {
	local build_dir="$1"
	shift
	rm -rf "${build_dir}"
	meson setup "${build_dir}" "${FL_REPO_ROOT}" --buildtype=release -Dbridge=enabled "$@"
}

fl_meson_install() {
	local build_dir="$1" destdir="$2"
	meson compile -C "${build_dir}"
	meson test -C "${build_dir}" --print-errorlogs
	DESTDIR="${destdir}" meson install -C "${build_dir}" --no-rebuild
}

fl_install_static_helper() {
	local build_dir="$1" root="$2"
	local static="${build_dir}/bridge/fl-bridge-helper-static"
	local target="${root}/lib/fork-linux/fl-bridge-helper"
	if [[ ! -f "${static}" ]]; then
		echo "error: ${static} was not built (musl-gcc missing?)" >&2
		return 1
	fi
	[[ -f "${target}" ]] || {
		echo "error: ${target} is missing" >&2
		return 1
	}
	install -m 0755 "${static}" "${target}"
}

# The source tree as git sees it: tracked files plus untracked-but-not-ignored ones (so
# uncommitted packaging fixes are exercised, and ignored build output, caches and secrets
# never are). Without git metadata (an extracted tarball) every file is taken.
fl_source_files() {
	local top
	top="$(git -c safe.directory='*' -C "${FL_REPO_ROOT}" rev-parse --show-toplevel 2>/dev/null || true)"
	# Only the tree's own repository counts: a copy of the tree that lies inside another
	# checkout (a version variant under artifacts/, say) is not that checkout's subdirectory.
	if [[ -n "${top}" && "$(cd "${top}" && pwd -P)" == "$(cd "${FL_REPO_ROOT}" && pwd -P)" ]]; then
		git -c safe.directory='*' -C "${FL_REPO_ROOT}" ls-files -z -co --exclude-standard
	else
		# A linked worktree's .git is a file pointing at the main repository: never ship it,
		# nor Python's byte-code caches. Symbolic links are part of the tree (meson_options.txt).
		(cd "${FL_REPO_ROOT}" && find . \( -path ./.git -o -name __pycache__ \) -prune -o \
			\( -type f -o -type l \) -not -name '*.pyc' -printf '%P\0')
	fi
}

# fl_copy_source_tree DEST: copy the tree as git sees it (fl_source_files) into the existing
# directory DEST, skipping entries git lists but the working tree has deleted.
fl_copy_source_tree() {
	local dest="$1" list
	list="$(mktemp "${TMPDIR:-/tmp}/fl-src-list.XXXXXX")"
	fl_source_files | (cd "${FL_REPO_ROOT}" && while IFS= read -r -d '' f; do
		if [[ -e "${f}" || -L "${f}" ]]; then printf '%s\0' "${f}"; fi
	done) >"${list}"
	if ! tar -C "${FL_REPO_ROOT}" --null -T "${list}" -cf - | tar -C "${dest}" -xf -; then
		rm -f "${list}"
		echo "error: could not copy the source tree" >&2
		return 1
	fi
	rm -f "${list}"
}

# fl_git_mount_args: docker run arguments (one per line) that mount a linked worktree's git
# common dir read-only at its own path, so git sees the tree mounted at /src as a checkout
# (otherwise fl_source_files falls back to every file on disk: artifacts/, logs/, secrets).
# None for a plain checkout or a tree without git.
fl_git_mount_args() {
	local common
	common="$(git -c safe.directory='*' -C "${FL_REPO_ROOT}" rev-parse --path-format=absolute --git-common-dir 2>/dev/null || true)"
	if [[ -n "${common}" && "${common}" != "${FL_REPO_ROOT}/"* ]]; then
		printf '%s\n' -v "${common}:${common}:ro"
	fi
}

fl_create_source_tarball() {
	local version="$1" dest="$2"
	local stage tmp
	# ${TMPDIR:-/tmp}: a /tmp tmpfs with a small per-user quota cannot hold the tree.
	stage="$(mktemp -d "${TMPDIR:-/tmp}/fl-src-${version}.XXXXXX")"
	tmp="$(mktemp "${TMPDIR:-/tmp}/fork-linux-${version}.tar.gz.XXXXXX")"
	mkdir -p "${stage}/fork-linux-${version}"
	if ! fl_copy_source_tree "${stage}/fork-linux-${version}"; then
		rm -rf "${stage}" "${tmp}"
		return 1
	fi
	if ! tar -C "${stage}" --sort=name --owner=0 --group=0 --numeric-owner -czf "${tmp}" "fork-linux-${version}"; then
		rm -rf "${stage}" "${tmp}"
		return 1
	fi
	rm -rf "${stage}"
	chmod 0644 "${tmp}"
	mv "${tmp}" "${dest}"
}

fl_collect_artifacts_glob() {
	local copied=0 pattern match had_nullglob=0
	shopt -q nullglob && had_nullglob=1
	shopt -s nullglob
	for pattern in "$@"; do
		for match in ${pattern}; do
			[[ -e "${match}" ]] || continue
			cp -a "${match}" "${FL_ARTIFACTS_DIR}/"
			copied=1
		done
	done
	((had_nullglob)) || shopt -u nullglob
	if ((copied == 0)); then
		echo "error: no artifacts matched: $*" >&2
		return 1
	fi
}

# Offset of the squashfs appended to a type-2 AppImage (e_shoff + e_shnum * e_shentsize),
# read from the ELF header so the AppImage is never executed.
fl_appimage_squashfs_offset() {
	local appimage="$1"
	python3 - "${appimage}" <<'EOF'
import struct
import sys

with open(sys.argv[1], "rb") as handle:
    header = handle.read(64)
(e_shoff,) = struct.unpack_from("<Q", header, 0x28)
e_shentsize, e_shnum = struct.unpack_from("<HH", header, 0x3A)
print(e_shoff + e_shentsize * e_shnum)
EOF
}

fl_render_pkgbuild() {
	local version="$1" source="$2" sha256="$3" srcdir="$4"
	sed -e "s|@VERSION@|${version}|g" \
		-e "s|@SOURCE@|${source}|g" \
		-e "s|@SHA256@|${sha256}|g" \
		-e "s|@SRCDIR@|${srcdir}|g" \
		"${FL_REPO_ROOT}/packaging/arch/PKGBUILD.in"
}

# .SRCINFO from the PKGBUILD's own arrays (sourced in a subshell, like makepkg does);
# the arch packaging cell compares it with makepkg --printsrcinfo.
fl_render_srcinfo() {
	local pkgbuild="$1"
	(
		pkgname="" pkgver="" pkgrel="" pkgdesc="" url=""
		arch=() license=() depends=() makedepends=() optdepends=() options=() source=() sha256sums=()
		# shellcheck source=/dev/null
		. "${pkgbuild}"
		printf 'pkgbase = %s\n' "${pkgname}"
		printf '\tpkgdesc = %s\n' "${pkgdesc}"
		printf '\tpkgver = %s\n' "${pkgver}"
		printf '\tpkgrel = %s\n' "${pkgrel}"
		printf '\turl = %s\n' "${url}"
		printf '\tarch = %s\n' "${arch[@]}"
		printf '\tlicense = %s\n' "${license[@]}"
		printf '\tmakedepends = %s\n' "${makedepends[@]}"
		printf '\tdepends = %s\n' "${depends[@]}"
		printf '\toptdepends = %s\n' "${optdepends[@]}"
		printf '\toptions = %s\n' "${options[@]}"
		printf '\tsource = %s\n' "${source[@]}"
		printf '\tsha256sums = %s\n' "${sha256sums[@]}"
		printf '\npkgname = %s\n' "${pkgname}"
	)
}
