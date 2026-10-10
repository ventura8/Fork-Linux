#!/usr/bin/env bash
# install.sh - install Fork for Linux (unofficial) from a release tarball.
#
#   curl -fsSL https://github.com/ventura8/Fork-Linux/releases/latest/download/install.sh | bash
#
# Fork for Linux (unofficial) is an unofficial Wine wrapper for the Fork git client by Dan
# Pristupov and Tanya Pristupova; it is not affiliated with them. Fork is paid software:
# https://git-fork.com/buy. This script installs only the wrapper; Fork itself is downloaded
# from cdn.fork.dev by `fork-linux setup` on first run.
#
# Per user by default (no root): the relocatable tree goes to ~/.local/opt/fork-linux, the
# fork-linux / fork commands are linked into ~/.local/bin, manual pages, completions, the
# AppStream file and our icon go under ~/.local/share, and the menu entry is created by
# `fork-linux desktop install`. --system (as root) does the same under /usr/local. Every path
# is recorded in a manifest that uninstall.sh uses to remove exactly what was installed.
# Nothing here ever touches ~/.wine or your Fork data.
#
# The whole script is one function called on the last line, so a truncated download runs
# nothing.

main() {
	set -euo pipefail

	local REPO="ventura8/Fork-Linux"
	local RELEASES="${FORK_LINUX_RELEASES_URL:-https://github.com/${REPO}/releases}"
	local APP_ID="io.github.ventura8.ForkLinux"
	local PACKAGE="fork-linux"
	local MANIFEST_NAME="install-manifest.txt"
	local MARKER_NAME=".fork-linux-install"

	local opt_version="" opt_prefix="" opt_system=0 opt_tarball="" opt_source=0 opt_yes=0
	local opt_deps=1 opt_setup=0 opt_dry=0 opt_desktop=1
	local prefix dest version tarball sums
	FL_INSTALL_TMP=""

	say() { printf '==> %s\n' "$*"; }
	warn() { printf 'warning: %s\n' "$*" >&2; }
	die() {
		printf 'install.sh: error: %s\n' "$*" >&2
		exit 1
	}
	run() {
		if ((opt_dry)); then
			printf 'dry-run: %s\n' "$*"
		else
			"$@"
		fi
	}
	cleanup() {
		# Runs from the EXIT trap, after main's locals are gone: the path is a global.
		if [[ -n "${FL_INSTALL_TMP:-}" && -d "${FL_INSTALL_TMP}" ]]; then
			rm -rf "${FL_INSTALL_TMP}"
		fi
	}
	usage() {
		cat <<'EOF'
Usage: install.sh [OPTIONS]

Install Fork for Linux (unofficial), an unofficial Wine wrapper for the Fork git client.

Options:
  --version VERSION    install this release (default: the latest release)
  --prefix DIR         install under DIR (default: ~/.local, or /usr/local with --system)
  --system             install for all users (run as root)
  --from-tarball FILE  install a downloaded fork-linux-VERSION-x86_64.tar.gz (verified
                       against a SHA256SUMS file next to it when there is one)
  --from-source        build and install the checkout this script lives in (needs meson)
  --yes                do not ask before installing missing distribution packages
  --no-deps            do not check or install distribution packages
  --no-desktop         do not create the menu entry (per-user installs)
  --setup              run "fork-linux setup" afterwards (Wine, .NET and Fork)
  --dry-run            show what would be done, change nothing
  -h, --help           show this help

Fork is made by Dan Pristupov and Tanya Pristupova; this project is not affiliated with
them. Please buy a Fork license: https://git-fork.com/buy
EOF
	}

	while (($# > 0)); do
		case "$1" in
			--version)
				opt_version="${2:?--version needs a value}"
				shift 2
				;;
			--prefix)
				opt_prefix="${2:?--prefix needs a directory}"
				shift 2
				;;
			--system)
				opt_system=1
				shift
				;;
			--from-tarball)
				opt_tarball="${2:?--from-tarball needs a file}"
				shift 2
				;;
			--from-source)
				opt_source=1
				shift
				;;
			--yes | -y)
				opt_yes=1
				shift
				;;
			--no-deps)
				opt_deps=0
				shift
				;;
			--no-desktop)
				opt_desktop=0
				shift
				;;
			--setup)
				opt_setup=1
				shift
				;;
			--dry-run)
				opt_dry=1
				shift
				;;
			-h | --help)
				usage
				return 0
				;;
			*)
				usage >&2
				die "unknown option: $1"
				;;
		esac
	done

	trap cleanup EXIT

	# ------------------------------------------------------------------ preflight
	[[ "$(uname -m)" == "x86_64" ]] || die "Fork for Windows is x86_64 only; this machine is $(uname -m)"
	if ((opt_system)); then
		[[ "$(id -u)" == 0 ]] || die "--system needs root: sudo bash install.sh --system"
		prefix="${opt_prefix:-/usr/local}"
	else
		[[ "$(id -u)" != 0 ]] || die "run install.sh as your normal user (per-user install), or pass --system for all users"
		[[ -n "${HOME:-}" ]] || die "HOME is not set"
		prefix="${opt_prefix:-${HOME}/.local}"
	fi
	[[ "${prefix}" == /* ]] || die "--prefix must be an absolute path"
	dest="${prefix}/opt/${PACKAGE}"
	if [[ -n "${opt_version}" && ! "${opt_version}" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
		die "invalid version: ${opt_version} (expected N.N.N)"
	fi
	if fl_package_owner >/dev/null; then
		die "fork-linux is installed by your package manager ($(fl_package_owner)); update or remove it with that instead"
	fi

	for tool in tar sha256sum mktemp; do
		command -v "${tool}" >/dev/null 2>&1 || die "${tool} is required"
	done
	FL_INSTALL_TMP="$(mktemp -d "${TMPDIR:-/tmp}/fork-linux-install.XXXXXX")"
	local tmp="${FL_INSTALL_TMP}"

	# ------------------------------------------------------------------ get the tarball
	if ((opt_source)); then
		tarball="$(fl_build_from_source)"
	elif [[ -n "${opt_tarball}" ]]; then
		[[ -f "${opt_tarball}" ]] || die "no such tarball: ${opt_tarball}"
		tarball="$(cd "$(dirname "${opt_tarball}")" && pwd)/$(basename "${opt_tarball}")"
		sums="$(dirname "${tarball}")/SHA256SUMS"
		if [[ -f "${sums}" ]]; then
			fl_verify "${tarball}" "${sums}"
		else
			warn "no SHA256SUMS next to ${tarball}: not verified"
		fi
	else
		fl_download
	fi
	version="$(fl_tarball_version "${tarball}")"
	say "Fork for Linux (unofficial) ${version} -> ${dest}"

	# ------------------------------------------------------------------ unpack + dependencies
	tar -C "${tmp}" -xzf "${tarball}"
	local tree="${tmp}/${PACKAGE}-${version}"
	[[ -x "${tree}/bin/fork-linux" && -d "${tree}/share/fork-linux/fork_linux" ]] ||
		die "${tarball} is not a fork-linux release tarball"
	local python
	if ((opt_deps)); then
		fl_install_deps "${tree}"
	fi
	python="$(fl_find_python)" || die "Python 3.10 or newer is required (install python3, or drop --no-deps)"
	say "Python: ${python}"

	# ------------------------------------------------------------------ install
	if [[ -e "${dest}" && ! -e "${dest}/${MARKER_NAME}" ]]; then
		die "${dest} exists but was not created by install.sh; move it away first"
	fi
	if [[ -f "${dest}/${MANIFEST_NAME}" ]]; then
		say "upgrading the existing install in ${dest}"
		fl_remove_manifest_files "${dest}/${MANIFEST_NAME}"
		run rm -rf "${dest}"
	fi
	local manifest="${tmp}/${MANIFEST_NAME}"
	{
		printf '# fork-linux install manifest (install.sh); uninstall.sh removes these paths\n'
		printf 'version\t%s\n' "${version}"
		printf 'mode\t%s\n' "$( ((opt_system)) && echo system || echo user)"
		printf 'tree\t%s\n' "${dest}"
	} >"${manifest}"

	run mkdir -p "${dest}"
	run cp -a "${tree}/." "${dest}/"
	if ((!opt_dry)); then
		printf '%s\n' "${version}" >"${dest}/${MARKER_NAME}"
	fi

	local name
	for name in fork-linux fork; do
		fl_link "${dest}/bin/${name}" "${prefix}/bin/${name}" "${manifest}"
	done
	fl_copy "${dest}/share/man/man1/fork-linux.1" "${prefix}/share/man/man1/fork-linux.1" "${manifest}"
	fl_copy "${dest}/share/man/man1/fork.1" "${prefix}/share/man/man1/fork.1" "${manifest}"
	fl_copy "${dest}/share/bash-completion/completions/fork-linux" \
		"${prefix}/share/bash-completion/completions/fork-linux" "${manifest}"
	fl_link "${prefix}/share/bash-completion/completions/fork-linux" \
		"${prefix}/share/bash-completion/completions/fork" "${manifest}"
	fl_copy "${dest}/share/fish/vendor_completions.d/fork-linux.fish" \
		"${prefix}/share/fish/vendor_completions.d/fork-linux.fish" "${manifest}"
	fl_link "${prefix}/share/fish/vendor_completions.d/fork-linux.fish" \
		"${prefix}/share/fish/vendor_completions.d/fork.fish" "${manifest}"
	fl_copy "${dest}/share/zsh/site-functions/_fork-linux" "${prefix}/share/zsh/site-functions/_fork-linux" "${manifest}"
	fl_copy "${dest}/share/metainfo/${APP_ID}.metainfo.xml" "${prefix}/share/metainfo/${APP_ID}.metainfo.xml" "${manifest}"
	fl_copy "${dest}/share/icons/hicolor/scalable/apps/${APP_ID}.svg" \
		"${prefix}/share/icons/hicolor/scalable/apps/${APP_ID}.svg" "${manifest}"
	fl_copy "${dest}/share/icons/hicolor/symbolic/apps/${APP_ID}-symbolic.svg" \
		"${prefix}/share/icons/hicolor/symbolic/apps/${APP_ID}-symbolic.svg" "${manifest}"
	if ((opt_system)); then
		fl_copy "${dest}/share/applications/${APP_ID}.desktop" \
			"${prefix}/share/applications/${APP_ID}.desktop" "${manifest}"
	elif ((opt_desktop)); then
		# The per-user menu entry belongs to fork-linux's own desktop integration (it records
		# what it writes in ~/.local/share/fork-linux/integrations.json and removes only that).
		say "menu entry: fork-linux desktop install"
		# --file-managers auto (the default): only the file managers installed here get the action.
		run "${python}" -I "${dest}/bin/fork-linux" desktop install ||
			warn "fork-linux desktop install failed; run it again later"
		printf 'desktop\tfork-linux\n' >>"${manifest}"
	fi
	if ((!opt_dry)); then
		install -m 0644 "${manifest}" "${dest}/${MANIFEST_NAME}"
	fi

	say "installed: $("${python}" -I "${dest}/bin/fork-linux" --version 2>/dev/null || echo "fork-linux ${version}")"
	case ":${PATH}:" in
		*":${prefix}/bin:"*) ;;
		*) warn "${prefix}/bin is not on your PATH; add it (e.g. in ~/.profile) to run fork-linux and fork" ;;
	esac
	cat <<EOF

Fork for Linux (unofficial) is installed. Fork is made by Dan Pristupov and Tanya
Pristupova and is not affiliated with this project; please buy a license:
https://git-fork.com/buy

Next: run "fork-linux setup" (or just "fork") to download Wine, .NET and the official
Fork installer. Uninstall: uninstall.sh (add --purge to also delete Fork's prefix).
EOF
	if ((opt_setup && !opt_dry)); then
		"${python}" -I "${dest}/bin/fork-linux" setup
	fi
}

# ---------------------------------------------------------------------- helpers
# (defined at top level but only called from main, after the whole file has been read)

fl_package_owner() {
	if command -v dpkg-query >/dev/null 2>&1 &&
		dpkg-query -W -f='${Status}' fork-linux 2>/dev/null | grep -q 'install ok installed'; then
		echo dpkg
	elif command -v rpm >/dev/null 2>&1 && rpm -q fork-linux >/dev/null 2>&1; then
		echo rpm
	elif command -v pacman >/dev/null 2>&1 && pacman -Q fork-linux >/dev/null 2>&1; then
		echo pacman
	else
		return 1
	fi
}

fl_python_ok() {
	"$1" -I -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1
}

fl_find_python() {
	local name candidate
	if [[ -n "${FORK_LINUX_PYTHON:-}" ]]; then
		fl_python_ok "${FORK_LINUX_PYTHON}" && {
			printf '%s\n' "${FORK_LINUX_PYTHON}"
			return 0
		}
		return 1
	fi
	for name in python3 python3.14 python3.13 python3.12 python3.11 python3.10; do
		candidate="$(command -v "${name}" 2>/dev/null)" || continue
		if fl_python_ok "${candidate}"; then
			printf '%s\n' "${candidate}"
			return 0
		fi
	done
	return 1
}

fl_fetch() {
	local url="$1" out="$2"
	case "${url}" in
		https://*) ;;
		http://127.0.0.1:* | http://localhost:*) ;; # local test servers only
		*) die "refusing to download over a non-HTTPS URL: ${url}" ;;
	esac
	if command -v curl >/dev/null 2>&1; then
		curl -fsSL --retry 3 --proto '=https,http' -o "${out}" "${url}"
	elif command -v wget >/dev/null 2>&1; then
		wget -q -O "${out}" "${url}"
	else
		die "curl or wget is required to download ${url}"
	fi
}

fl_download() {
	local base
	if [[ -n "${opt_version}" ]]; then
		base="${RELEASES}/download/v${opt_version}"
	else
		base="${RELEASES}/latest/download"
	fi
	sums="${tmp}/SHA256SUMS"
	say "downloading ${base}/SHA256SUMS"
	fl_fetch "${base}/SHA256SUMS" "${sums}" || die "cannot download ${base}/SHA256SUMS"
	local name
	name="$(awk '{print $2}' "${sums}" | sed 's/^\*//' | grep -E "^${PACKAGE}-[0-9]+\.[0-9]+\.[0-9]+-x86_64\.tar\.gz$" | head -n 1 || true)"
	[[ -n "${name}" ]] || die "SHA256SUMS lists no ${PACKAGE}-VERSION-x86_64.tar.gz"
	if [[ -n "${opt_version}" && "${name}" != "${PACKAGE}-${opt_version}-x86_64.tar.gz" ]]; then
		die "SHA256SUMS of v${opt_version} lists ${name}"
	fi
	tarball="${tmp}/${name}"
	say "downloading ${base}/${name}"
	fl_fetch "${base}/${name}" "${tarball}" || die "cannot download ${base}/${name}"
	fl_verify "${tarball}" "${sums}"
}

fl_verify() {
	local file="$1" list="$2" name want got
	name="$(basename "${file}")"
	want="$(awk -v n="${name}" '{f=$2; sub(/^\*/, "", f); if (f == n) {print $1; exit}}' "${list}")"
	[[ -n "${want}" ]] || die "$(basename "${list}") has no entry for ${name}"
	got="$(sha256sum "${file}" | awk '{print $1}')"
	if [[ "${got}" != "${want}" ]]; then
		rm -f "${file}"
		die "sha256 mismatch for ${name}: expected ${want}, got ${got} (download deleted)"
	fi
	say "sha256 OK: ${name}"
}

fl_tarball_version() {
	local base
	base="$(basename "$1")"
	if [[ "${base}" =~ ^fork-linux-([0-9]+\.[0-9]+\.[0-9]+)-x86_64\.tar\.gz$ ]]; then
		printf '%s\n' "${BASH_REMATCH[1]}"
	else
		die "unexpected tarball name: ${base} (expected fork-linux-VERSION-x86_64.tar.gz)"
	fi
}

fl_build_from_source() {
	local src
	src="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
	[[ -f "${src}/meson.build" && -f "${src}/VERSION" ]] || die "--from-source: run install.sh from a fork-linux checkout"
	command -v meson >/dev/null 2>&1 || die "--from-source needs meson and ninja"
	local ver root stage
	ver="$(head -n 1 "${src}/VERSION")"
	root="${PACKAGE}-${ver}"
	stage="${tmp}/stage"
	say "building ${ver} from ${src}" >&2
	meson setup "${tmp}/build" "${src}" --prefix="/${root}" -Dflavor=tarball \
		"-Dpython=/usr/bin/env -S python3" -Dtests=false >&2
	meson compile -C "${tmp}/build" >&2
	DESTDIR="${stage}" meson install -C "${tmp}/build" --no-rebuild >&2
	tar -C "${stage}" -czf "${tmp}/${root}-x86_64.tar.gz" "${root}"
	printf '%s\n' "${tmp}/${root}-x86_64.tar.gz"
}

# fl_install_deps TREE: missing Wine host libraries and tools, named by fork_linux.hostdeps
# (the same table `fork-linux doctor` uses), installed with the distribution's package
# manager after confirmation.
fl_install_deps() {
	local tree="$1" pm python line pkg chosen
	local -a sudo=() alt=()
	local -a wanted=()
	if command -v apt-get >/dev/null 2>&1; then
		pm=apt
	elif command -v dnf >/dev/null 2>&1; then
		pm=dnf
	elif command -v zypper >/dev/null 2>&1; then
		pm=zypper
	elif command -v pacman >/dev/null 2>&1; then
		pm=pacman
	else
		warn "no apt-get, dnf, zypper or pacman: install Wine's host libraries yourself (fork-linux doctor lists them)"
		return 0
	fi
	if [[ "$(id -u)" != 0 ]]; then
		if command -v sudo >/dev/null 2>&1; then
			sudo=(sudo)
		elif ((!opt_dry)); then
			warn "sudo not found: install Wine's host libraries as root (fork-linux doctor lists them)"
			return 0
		fi
	fi
	if ! python="$(fl_find_python)"; then
		case "${pm}" in
			pacman) wanted+=(python) ;;
			*) wanted+=(python3) ;;
		esac
	else
		while IFS= read -r line; do
			[[ -n "${line}" ]] || continue
			chosen=""
			IFS='|' read -r -a alt <<<"${line}"
			for pkg in "${alt[@]}"; do
				if fl_pm_has "${pm}" "${pkg}"; then
					chosen="${pkg}"
					break
				fi
			done
			wanted+=("${chosen:-${alt[0]}}")
		done < <(fl_missing_packages "${python}" "${tree}")
	fi
	if ((${#wanted[@]} == 0)); then
		say "host dependencies: all present"
		return 0
	fi
	say "missing distribution packages (${pm}): ${wanted[*]}"
	if ((opt_dry)); then
		printf 'dry-run: would install %s\n' "${wanted[*]}"
		return 0
	fi
	if ((!opt_yes)); then
		local answer=""
		if [[ -r /dev/tty ]]; then
			printf 'Install them now with %s? [y/N] ' "${pm}" >/dev/tty
			read -r answer </dev/tty || answer=""
		fi
		case "${answer}" in
			y | Y | yes | YES) ;;
			*)
				warn "not installing packages; fork-linux doctor will list what is missing"
				return 0
				;;
		esac
	fi
	case "${pm}" in
		apt) "${sudo[@]}" apt-get install -y "${wanted[@]}" ;;
		dnf) "${sudo[@]}" dnf install -y "${wanted[@]}" ;;
		zypper) "${sudo[@]}" zypper --non-interactive install "${wanted[@]}" ;;
		pacman) "${sudo[@]}" pacman -S --needed --noconfirm "${wanted[@]}" ;;
	esac
}

fl_pm_has() {
	case "$1" in
		apt) apt-cache show "$2" >/dev/null 2>&1 ;;
		dnf) dnf -q info "$2" >/dev/null 2>&1 ;;
		zypper) zypper -q info "$2" 2>/dev/null | grep -q '^Name' ;;
		pacman) pacman -Si "$2" >/dev/null 2>&1 ;;
		*) return 1 ;;
	esac
}

# One line per missing tool / library: the package alternatives ("a|b") for this distribution.
fl_missing_packages() {
	"$1" -I - "$2/share/fork-linux" <<'EOF'
import shutil
import sys

sys.path.insert(0, sys.argv[1])
from fork_linux import hostdeps

info = hostdeps.distro()
table = hostdeps.PACKAGES.get(info.family, {})
names = [tool for tool in hostdeps.REQUIRED_TOOLS if shutil.which(tool) is None]
if not any(shutil.which(tool) for tool in hostdeps.DOWNLOADERS):
    names.append(hostdeps.DOWNLOADERS[0])
names += hostdeps.missing_libs(hostdeps.REQUIRED_LIBS)
for name in names:
    if name in table:
        print(table[name])
    else:
        print(f"unknown: no {info.family} package known for {name}", file=sys.stderr)
EOF
}

fl_record() {
	local manifest="$1" kind="$2" path="$3" extra="${4:-}"
	if ((!opt_dry)); then
		printf '%s\t%s\t%s\n' "${kind}" "${path}" "${extra}" >>"${manifest}"
	fi
}

fl_link() {
	local target="$1" link="$2" manifest="$3"
	if [[ -e "${link}" || -L "${link}" ]]; then
		if [[ ! -L "${link}" ]]; then
			warn "leaving ${link} alone: it is not a link install.sh made"
			return 0
		fi
		run rm -f "${link}"
	fi
	run mkdir -p "$(dirname "${link}")"
	run ln -s "${target}" "${link}"
	fl_record "${manifest}" link "${link}" "${target}"
}

fl_copy() {
	local src="$1" dst="$2" manifest="$3" sum=""
	[[ -f "${src}" || ${opt_dry} -eq 1 ]] || return 0
	if [[ -e "${dst}" && ! -L "${dst}" ]] && ! fl_manifest_has "${dst}"; then
		if ! cmp -s "${src}" "${dst}"; then
			warn "leaving ${dst} alone: it was not installed by install.sh"
			return 0
		fi
	fi
	run mkdir -p "$(dirname "${dst}")"
	run install -m 0644 "${src}" "${dst}"
	if ((!opt_dry)); then
		sum="$(sha256sum "${dst}" | awk '{print $1}')"
	fi
	fl_record "${manifest}" file "${dst}" "${sum}"
}

fl_manifest_has() {
	[[ -f "${dest}/${MANIFEST_NAME}" ]] && awk -F '\t' -v p="$1" '$2 == p {found=1} END {exit !found}' "${dest}/${MANIFEST_NAME}"
}

# Remove what an earlier manifest recorded: links that still point where we left them and
# files whose content is unchanged; anything else is reported and kept.
fl_remove_manifest_files() {
	local manifest="$1" kind path extra
	while IFS=$'\t' read -r kind path extra; do
		case "${kind}" in
			link)
				if [[ -L "${path}" && "$(readlink "${path}")" == "${extra}" ]]; then
					run rm -f "${path}"
				fi
				;;
			file)
				if [[ -f "${path}" && ! -L "${path}" ]]; then
					if [[ "$(sha256sum "${path}" | awk '{print $1}')" == "${extra}" ]]; then
						run rm -f "${path}"
					else
						warn "keeping ${path}: it was changed after install.sh wrote it"
					fi
				fi
				;;
		esac
	done <"${manifest}"
}

main "$@"
