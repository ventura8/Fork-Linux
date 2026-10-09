#!/usr/bin/env bash
# ppa-docker.sh - build the signed Launchpad PPA source packages of Fork for Linux (unofficial)
# for Ubuntu 22.04 (jammy), 24.04 (noble) and 26.04 (resolute), and upload them only when asked.
#
# Runs inside docker/Dockerfile.ppa. Versions: VERSION+ppa1~ubuntuNN.NN.1, e.g.
# 0.1.0+ppa1~ubuntu22.04.1 (AGENTS.md §4.9). Each series is built from a private copy of the
# tree whose debian/changelog gets the PPA entry; the repository's changelog is never
# modified.
#
# Usage:
#   ./scripts/ppa-docker.sh --dry-run [SERIES...]   unsigned source packages into
#                                                   artifacts/ppa/<series>/ (no key, no upload)
#   ./scripts/ppa-docker.sh [SERIES...]             signed with GPG_PRIVATE_KEY / GPG_PASSPHRASE;
#                                                   uploaded only with UPLOAD_PPA=1 (release.yml)
# Env: MAINTAINER_NAME, MAINTAINER_EMAIL (defaults: the debian/changelog maintainer),
#      PPA (default ppa:ventura8/fork-linux).
set -euo pipefail

cd "$(dirname "$0")/.."
# shellcheck source=scripts/release-common.sh
. scripts/release-common.sh

DRY_RUN=0
SERIES=()
for arg in "$@"; do
	case "${arg}" in
		--dry-run) DRY_RUN=1 ;;
		jammy | noble | resolute) SERIES+=("${arg}") ;;
		*)
			echo "usage: ppa-docker.sh [--dry-run] [jammy|noble|resolute...]" >&2
			exit 2
			;;
	esac
done
if ((${#SERIES[@]} == 0)); then
	SERIES=(jammy noble resolute)
fi

VERSION="$(fl_read_version)"
PPA="${PPA:-ppa:ventura8/fork-linux}"
maintainer_line="$(sed -n 's/^ -- \(.*\)  .*/\1/p' debian/changelog | head -n 1)"
export DEBFULLNAME="${MAINTAINER_NAME:-${maintainer_line% <*}}"
maintainer_email="${maintainer_line##*<}"
export DEBEMAIL="${MAINTAINER_EMAIL:-${maintainer_email%%>*}}"

series_release() {
	case "$1" in
		jammy) echo 22.04 ;;
		noble) echo 24.04 ;;
		resolute) echo 26.04 ;;
		*) return 1 ;;
	esac
}

SIGN_ARGS=(-us -uc)
if ((DRY_RUN == 0)); then
	: "${GPG_PRIVATE_KEY:?GPG_PRIVATE_KEY is required (or use --dry-run)}"
	: "${GPG_PASSPHRASE?GPG_PASSPHRASE is required (may be empty)}"
	GNUPGHOME="$(mktemp -d /tmp/fl-gnupg.XXXXXX)"
	export GNUPGHOME
	printf '%s\n' "${GPG_PRIVATE_KEY}" | sed 's/\\n/\n/g' | gpg --batch --import
	fingerprint="$(gpg --list-secret-keys --with-colons | awk -F: '/^fpr:/ {print $10; exit}')"
	[[ -n "${fingerprint}" ]] || {
		echo "error: no secret key after import" >&2
		exit 1
	}
	sign_cmd="$(mktemp /tmp/fl-sign.XXXXXX)"
	cat >"${sign_cmd}" <<'SIGN'
#!/bin/sh
exec gpg --batch --pinentry-mode loopback --passphrase "$GPG_PASSPHRASE" "$@"
SIGN
	chmod 0700 "${sign_cmd}"
	SIGN_ARGS=(--sign-backend=gpg "--sign-command=${sign_cmd}" "-k${fingerprint}")
fi

OUT_ROOT="${FL_ARTIFACTS_DIR}/ppa"
for series in "${SERIES[@]}"; do
	release="$(series_release "${series}")"
	ppa_version="${VERSION}+ppa1~ubuntu${release}.1"
	work="$(mktemp -d "/tmp/fl-ppa-${series}.XXXXXX")"
	fl_create_source_tarball "${VERSION}" "${work}/src.tar.gz"
	tar -C "${work}" -xzf "${work}/src.tar.gz"
	src="${work}/fork-linux-${VERSION}"
	echo "==> ${series}: fork-linux ${ppa_version}"
	(
		cd "${src}"
		# The PPA entry, written directly (dch needs distro-info for every series and warns
		# about the directory rename and its own deprecated dpkg API).
		{
			printf 'fork-linux (%s) %s; urgency=medium\n\n' "${ppa_version}" "${series}"
			printf '  * PPA build of %s for Ubuntu %s (%s).\n\n' "${VERSION}" "${release}" "${series}"
			printf ' -- %s <%s>  %s\n\n' "${DEBFULLNAME}" "${DEBEMAIL}" "$(date -R)"
			cat debian/changelog
		} >debian/changelog.ppa
		mv debian/changelog.ppa debian/changelog
		dpkg-parsechangelog --show-field Version | grep -qxF "${ppa_version}"
		dpkg-buildpackage -S -sa -d "${SIGN_ARGS[@]}"
	)
	out="${OUT_ROOT}/${series}"
	rm -rf "${out}"
	mkdir -p "${out}"
	cp "${work}/fork-linux_${ppa_version}"* "${out}/"
	ls -la "${out}"
	if ((DRY_RUN == 0)) && [[ "${UPLOAD_PPA:-0}" == "1" ]]; then
		dput "${PPA}" "${out}/fork-linux_${ppa_version}_source.changes"
	elif ((DRY_RUN)); then
		echo "dry-run: not signed, not uploaded (${PPA})"
	fi
	rm -rf "${work}"
done
