#!/usr/bin/env bash
# uninstall.sh - remove a Fork for Linux (unofficial) install made by install.sh.
#
#   curl -fsSL https://github.com/ventura8/Fork-Linux/releases/latest/download/uninstall.sh | bash
#
# Removes exactly what install.sh recorded in <prefix>/opt/fork-linux/install-manifest.txt:
# links that still point where install.sh left them, files whose content is unchanged, and
# the program tree itself. Before that, `fork-linux uninstall` removes the menu entry, icons,
# file-manager actions and command links that fork-linux created for you. Your Wine prefix,
# Fork, its settings and snapshots are kept unless you pass --purge, which hands over to
# `fork-linux uninstall --purge` (it asks for confirmation; deactivate your Fork license in
# Fork first, each new prefix may use one of its activations). ~/.wine is never touched.
#
# The whole script is one function called on the last line, so a truncated download runs
# nothing.

main() {
	set -euo pipefail

	local PACKAGE="fork-linux"
	local MANIFEST_NAME="install-manifest.txt"
	local MARKER_NAME=".fork-linux-install"
	local opt_prefix="" opt_system=0 opt_purge=0 opt_yes=0 opt_dry=0
	local prefix dest manifest python

	say() { printf '==> %s\n' "$*"; }
	warn() { printf 'warning: %s\n' "$*" >&2; }
	die() {
		printf 'uninstall.sh: error: %s\n' "$*" >&2
		exit 1
	}
	run() {
		if ((opt_dry)); then
			printf 'dry-run: %s\n' "$*"
		else
			"$@"
		fi
	}
	usage() {
		cat <<'EOF'
Usage: uninstall.sh [OPTIONS]

Remove Fork for Linux (unofficial) installed by install.sh.

Options:
  --prefix DIR   the prefix given to install.sh (default: ~/.local, or /usr/local with --system)
  --system       remove the all-users install (run as root)
  --purge        also delete Fork's Wine prefix, runtimes, snapshots, caches and settings
                 (runs "fork-linux uninstall --purge"; never touches ~/.wine)
  --yes          with --purge: do not ask for confirmation
  --dry-run      show what would be removed, change nothing
  -h, --help     show this help
EOF
	}

	while (($# > 0)); do
		case "$1" in
			--prefix)
				opt_prefix="${2:?--prefix needs a directory}"
				shift 2
				;;
			--system)
				opt_system=1
				shift
				;;
			--purge)
				opt_purge=1
				shift
				;;
			--yes | -y)
				opt_yes=1
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

	if ((opt_system)); then
		[[ "$(id -u)" == 0 ]] || die "--system needs root: sudo bash uninstall.sh --system"
		prefix="${opt_prefix:-/usr/local}"
	else
		[[ "$(id -u)" != 0 ]] || die "run uninstall.sh as the user who installed fork-linux, or pass --system"
		[[ -n "${HOME:-}" ]] || die "HOME is not set"
		prefix="${opt_prefix:-${HOME}/.local}"
	fi
	dest="${prefix}/opt/${PACKAGE}"
	manifest="${dest}/${MANIFEST_NAME}"
	if [[ ! -f "${manifest}" || ! -f "${dest}/${MARKER_NAME}" ]]; then
		if command -v dpkg-query >/dev/null 2>&1 &&
			dpkg-query -W -f='${Status}' fork-linux 2>/dev/null | grep -q 'install ok installed'; then
			die "fork-linux was installed with apt/dpkg: sudo apt remove fork-linux"
		fi
		say "no install.sh install of fork-linux under ${prefix}; nothing to do"
		return 0
	fi

	python="$(ul_find_python || true)"
	local cli=("${dest}/bin/fork-linux")
	if [[ -n "${python}" ]]; then
		cli=("${python}" -I "${dest}/bin/fork-linux")
	fi

	# fork-linux's own clean-up first, while the program still exists.
	if ((opt_purge)); then
		say "fork-linux uninstall --purge"
		local purge=(uninstall --purge)
		((opt_yes)) && purge+=(--yes)
		if ((opt_dry)); then
			printf 'dry-run: %s\n' "${cli[*]} ${purge[*]}"
		else
			"${cli[@]}" "${purge[@]}" || die "fork-linux uninstall --purge did not finish; nothing else was removed"
		fi
	elif ((!opt_system)) && grep -q '^desktop	' "${manifest}"; then
		say "fork-linux uninstall (menu entry, icons, file-manager actions)"
		run "${cli[@]}" uninstall || warn "fork-linux uninstall failed; continuing"
	fi

	local kind path extra
	while IFS=$'\t' read -r kind path extra; do
		case "${kind}" in
			link)
				if [[ -L "${path}" && "$(readlink "${path}")" == "${extra}" ]]; then
					run rm -f "${path}"
				elif [[ -e "${path}" || -L "${path}" ]]; then
					warn "keeping ${path}: it no longer points to ${extra}"
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

	# The tree carries our marker; nothing outside it is removed recursively.
	case "${dest}" in
		*/opt/"${PACKAGE}") run rm -rf "${dest}" ;;
		*) die "refusing to remove ${dest}" ;;
	esac
	if ((!opt_dry)); then
		rmdir "${prefix}/opt" 2>/dev/null || true
	fi
	say "fork-linux removed from ${prefix}"
	if ((!opt_purge)); then
		echo "Your Wine prefix, Fork and settings were kept; uninstall.sh --purge (or fork-linux uninstall --purge before uninstalling) deletes them."
	fi
}

ul_find_python() {
	local name candidate
	for name in "${FORK_LINUX_PYTHON:-}" python3 python3.14 python3.13 python3.12 python3.11 python3.10; do
		[[ -n "${name}" ]] || continue
		candidate="$(command -v "${name}" 2>/dev/null)" || continue
		if "${candidate}" -I -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
			printf '%s\n' "${candidate}"
			return 0
		fi
	done
	return 1
}

main "$@"
