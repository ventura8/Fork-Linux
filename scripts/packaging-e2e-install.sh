#!/usr/bin/env bash
# packaging-e2e-install.sh - live install E2E of one built package of Fork for Linux (unofficial).
#
# Runs as root inside the format's packaging image (the snap cell: inside the systemd
# container of scripts/ci-snap-build.sh) against artifacts/. Cycle (AGENTS.md §4.9):
#   install -> assert -> upgrade (install again) -> assert -> remove -> assert removed ->
#   reinstall -> assert
# The program is always run as the unprivileged user "tester" (hard rule 9). Asserts:
#   * fork-linux --version and fork --version print VERSION
#   * fork-linux doctor --offline --json works with no Wine present (JSON, exit 0 or 17)
#   * the shims (win64/fl-shim.exe, fl-launch.exe) are PE32+ x86-64
#   * fl-bridge-helper is an x86-64 ELF: ET_EXEC (static musl) for the portable channels
#     (tarball, AppImage, snap); the distro packages and the Flatpak ship the glibc PIE
#     build (bridge/README.md "Install layout"), so ET_DYN is accepted there
#   * the desktop entry validates; metainfo, man pages and completions are installed
#   * tester's ~/.local/share/fork-linux/E2E_MARKER and ~/.wine/E2E_SENTINEL survive every step
#
# Usage: ./scripts/packaging-e2e-install.sh <format>
# Formats: deb deb-jammy rpm-fedora rpm-opensuse arch snap appimage flatpak tarball
set -euo pipefail

cd "$(dirname "$0")/.."
# shellcheck source=scripts/release-common.sh
. scripts/release-common.sh

FORMAT="${1:?usage: packaging-e2e-install.sh <format>}"
VERSION="$(fl_read_version)"
APP_ID="$(fl_app_id)"
ART="${FL_ARTIFACTS_DIR}"
TESTER="tester"
TESTER_HOME="/home/${TESTER}"
MARKER="${TESTER_HOME}/.local/share/fork-linux/E2E_MARKER"
SENTINEL="${TESTER_HOME}/.wine/E2E_SENTINEL"
MARK_VALUE="fork-linux-e2e-${FORMAT}-$$-${RANDOM}"
STAGE="/tmp/fl-e2e-${FORMAT}"

fail() {
	echo "E2E FAIL (${FORMAT}): $*" >&2
	exit 1
}

[[ "$(id -u)" == 0 ]] || fail "run as root inside the packaging image (the program itself runs as ${TESTER})"

# ---------------------------------------------------------------------- tester + markers

setup_tester() {
	if ! id "${TESTER}" >/dev/null 2>&1; then
		useradd -m -s /bin/bash "${TESTER}"
	fi
	# Created as the tester, so every parent directory belongs to them.
	runuser -u "${TESTER}" -- mkdir -p "${TESTER_HOME}/.local/share/fork-linux" "${TESTER_HOME}/.wine"
	printf '%s\n' "${MARK_VALUE}" | runuser -u "${TESTER}" -- tee "${MARKER}" "${SENTINEL}" >/dev/null
	rm -rf "${STAGE}"
	install -d -m 0755 "${STAGE}"
	# A session-like runtime directory (flatpak run allocates its instance there).
	install -d -o "${TESTER}" -m 0700 "/tmp/fl-e2e-runtime-${TESTER}"
}

as_tester() {
	runuser -u "${TESTER}" -- env -i \
		HOME="${TESTER_HOME}" USER="${TESTER}" LOGNAME="${TESTER}" LANG=C.UTF-8 \
		PATH="${TESTER_HOME}/.local/bin:/usr/local/bin:/usr/bin:/bin:/snap/bin" \
		XDG_RUNTIME_DIR="/tmp/fl-e2e-runtime-${TESTER}" \
		APPIMAGE_EXTRACT_AND_RUN=1 NO_CLEANUP=1 \
		"$@"
}

assert_markers() {
	[[ "$(cat "${MARKER}" 2>/dev/null)" == "${MARK_VALUE}" ]] || fail "${MARKER} was changed or removed"
	[[ "$(cat "${SENTINEL}" 2>/dev/null)" == "${MARK_VALUE}" ]] || fail "${SENTINEL} was changed or removed (~/.wine must never be touched)"
}

# ---------------------------------------------------------------------- binary checks

check_binaries() {
	local root="$1" helper_kind="$2"
	python3 - "${root}/lib/fork-linux" "${helper_kind}" <<'EOF'
import struct
import sys
from pathlib import Path

libdir = Path(sys.argv[1])
helper_kind = sys.argv[2]


def pe32plus_x64(path: Path) -> None:
    data = path.read_bytes()
    if data[:2] != b"MZ":
        raise SystemExit(f"{path}: no MZ header")
    (offset,) = struct.unpack_from("<I", data, 0x3C)
    if data[offset:offset + 4] != b"PE\0\0":
        raise SystemExit(f"{path}: no PE signature")
    (machine,) = struct.unpack_from("<H", data, offset + 4)
    (magic,) = struct.unpack_from("<H", data, offset + 24)
    if machine != 0x8664 or magic != 0x20B:
        raise SystemExit(f"{path}: not PE32+ x86-64 (machine {machine:#x}, magic {magic:#x})")
    print(f"ok  {path.name}: PE32+ x86-64")


def elf_type(path: Path) -> int:
    data = path.read_bytes()[:64]
    if data[:4] != b"\x7fELF" or data[4] != 2:
        raise SystemExit(f"{path}: not a 64-bit ELF")
    (e_type, e_machine) = struct.unpack_from("<HH", data, 16)
    if e_machine != 62:
        raise SystemExit(f"{path}: not x86-64 (e_machine {e_machine})")
    return e_type


for name in ("fl-shim.exe", "fl-launch.exe"):
    pe32plus_x64(libdir / "win64" / name)
helper = libdir / "fl-bridge-helper"
kind = elf_type(helper)
allowed = {2} if helper_kind == "static" else {2, 3}
if kind not in allowed:
    raise SystemExit(f"{helper}: ELF type {kind}, expected {'ET_EXEC' if helper_kind == 'static' else 'ET_EXEC or ET_DYN'}")
print(f"ok  fl-bridge-helper: ELF x86-64 {'ET_EXEC' if kind == 2 else 'ET_DYN (PIE)'}")
EOF
}

check_doctor_json() {
	local out="${STAGE}/doctor.json" rc=0
	as_tester "$@" doctor --offline --json >"${out}" 2>"${STAGE}/doctor.err" || rc=$?
	if ((rc != 0 && rc != 17)); then
		cat "${STAGE}/doctor.err" >&2
		fail "doctor --offline --json exited ${rc} (expected 0 or 17 = checks failed)"
	fi
	python3 - "${out}" <<'EOF'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    data = json.load(handle)
checks = data.get("checks")
if not isinstance(checks, list) or not checks:
    raise SystemExit("doctor --json has no checks")
print(f"ok  doctor --offline --json: {len(checks)} checks")
EOF
}

# assert_installed ROOT HELPER_KIND DESKTOP_FILE CMD... (CMD runs fork-linux as tester)
assert_installed() {
	local root="$1" helper_kind="$2" desktop="$3"
	shift 3
	local -a cmd=("$@")
	echo "==> assert installed (${FORMAT})"
	local out
	out="$(as_tester "${cmd[@]}" --version)" || fail "fork-linux --version failed"
	[[ "${out}" == *"fork-linux ${VERSION}"* ]] || fail "fork-linux --version printed '${out}', expected ${VERSION}"
	echo "ok  ${out}"
	check_doctor_json "${cmd[@]}"
	check_binaries "${root}" "${helper_kind}"
	[[ -f "${desktop}" ]] || fail "desktop entry missing: ${desktop}"
	desktop-file-validate "${desktop}" || fail "desktop-file-validate ${desktop}"
	echo "ok  desktop entry ${desktop}"
	local f
	for f in "share/metainfo/${APP_ID}.metainfo.xml" \
		share/bash-completion/completions/fork-linux \
		share/zsh/site-functions/_fork-linux \
		share/fish/vendor_completions.d/fork-linux.fish; do
		[[ -f "${root}/${f}" ]] || fail "missing ${root}/${f}"
	done
	for f in fork-linux.1 fork.1; do
		compgen -G "${root}/share/man/man1/${f}*" >/dev/null || fail "missing man page ${root}/share/man/man1/${f}"
	done
	echo "ok  metainfo, man pages, completions"
	assert_markers
	echo "ok  E2E_MARKER and ~/.wine/E2E_SENTINEL intact"
}

# assert_removed ROOT...: no ROOT/bin/fork-linux is left (file or link).
assert_removed() {
	local root
	echo "==> assert removed (${FORMAT})"
	for root in "$@"; do
		[[ ! -e "${root}/bin/fork-linux" && ! -L "${root}/bin/fork-linux" ]] ||
			fail "${root}/bin/fork-linux still exists after remove"
	done
	assert_markers
	echo "ok  removed; markers intact"
}

artifact() {
	local pattern="$1" match
	match="$(compgen -G "${ART}/${pattern}" | head -n 1 || true)"
	[[ -n "${match}" ]] || fail "no artifact ${ART}/${pattern}"
	readlink -f "${match}"
}

# ---------------------------------------------------------------------- formats

# cycle INSTALL REMOVE ASSERT ROOT...: the E2E cycle; ROOTs are checked after the remove.
cycle() {
	local install_fn="$1" remove_fn="$2" assert_fn="$3"
	shift 3
	echo "==> E2E install (${FORMAT})"
	"${install_fn}"
	"${assert_fn}"
	echo "==> E2E upgrade in place (${FORMAT})"
	"${install_fn}"
	"${assert_fn}"
	echo "==> E2E remove (${FORMAT})"
	"${remove_fn}"
	assert_removed "$@"
	echo "==> E2E reinstall (${FORMAT})"
	"${install_fn}"
	"${assert_fn}"
}

system_assert() {
	local helper_kind="$1"
	assert_installed /usr "${helper_kind}" "/usr/share/applications/${APP_ID}.desktop" fork-linux
	local out
	out="$(as_tester fork --version)" || fail "fork --version failed"
	[[ "${out}" == *"${VERSION}"* ]] || fail "fork --version printed '${out}'"
}
dyn_assert() { system_assert dyn; }

deb_install() {
	local deb
	deb="$(artifact "fork-linux_${VERSION}_amd64.deb")"
	cp "${deb}" "${STAGE}/"
	apt-get update -qq
	if dpkg -s fork-linux >/dev/null 2>&1; then
		apt-get install -y --reinstall "${STAGE}/$(basename "${deb}")"
	else
		apt-get install -y "${STAGE}/$(basename "${deb}")"
	fi
}
deb_remove() { apt-get purge -y fork-linux; }

fedora_install() {
	local rpm
	rpm="$(artifact "fork-linux-${VERSION}-*.fc*.x86_64.rpm")"
	if rpm -q fork-linux >/dev/null 2>&1; then
		dnf reinstall -y --nogpgcheck "${rpm}"
	else
		dnf install -y --nogpgcheck "${rpm}"
	fi
}
fedora_remove() { dnf remove -y fork-linux; }

opensuse_install() {
	local rpm
	rpm="$(artifact "fork-linux-${VERSION}-*.lp*.x86_64.rpm")"
	if rpm -q fork-linux >/dev/null 2>&1; then
		zypper --non-interactive --no-gpg-checks install --force --allow-unsigned-rpm "${rpm}"
	else
		zypper --non-interactive --no-gpg-checks install --allow-unsigned-rpm "${rpm}"
	fi
}
opensuse_remove() { zypper --non-interactive remove fork-linux; }

arch_install() {
	local pkg
	pkg="$(artifact "fork-linux-${VERSION}-*-x86_64.pkg.tar.zst")"
	pacman -Sy --noconfirm
	pacman -U --noconfirm "${pkg}"
}
arch_remove() { pacman -Rns --noconfirm fork-linux; }

tarball_install() {
	local tarball
	tarball="$(artifact "fork-linux-${VERSION}-x86_64.tar.gz")"
	cp "${tarball}" "${STAGE}/"
	(cd "${STAGE}" && sha256sum "$(basename "${tarball}")" >SHA256SUMS)
	cp install.sh uninstall.sh "${STAGE}/"
	chmod -R a+rX "${STAGE}"
	# --yes: the dependency step runs; without sudo it only reports what is missing.
	as_tester bash "${STAGE}/install.sh" --from-tarball "${STAGE}/$(basename "${tarball}")" --yes
}
tarball_remove() { as_tester bash "${STAGE}/uninstall.sh"; }
tarball_assert() {
	assert_installed "${TESTER_HOME}/.local/opt/fork-linux" static \
		"${TESTER_HOME}/.local/share/applications/${APP_ID}.desktop" "${TESTER_HOME}/.local/bin/fork-linux"
	local out
	out="$(as_tester "${TESTER_HOME}/.local/bin/fork" --version)" || fail "fork --version failed"
	[[ "${out}" == *"${VERSION}"* ]] || fail "fork --version printed '${out}'"
	[[ -f "${TESTER_HOME}/.local/share/man/man1/fork-linux.1" ]] || fail "per-user man page missing"
}

APPIMAGE_ROOT="${STAGE}/appimage-root"
appimage_install() {
	local image offset
	image="$(artifact "fork-linux-${VERSION}-x86_64.AppImage")"
	install -d -o "${TESTER}" "${TESTER_HOME}/Applications"
	install -m 0755 -o "${TESTER}" "${image}" "${TESTER_HOME}/Applications/fork-linux.AppImage"
	as_tester env APPIMAGE="${TESTER_HOME}/Applications/fork-linux.AppImage" \
		"${TESTER_HOME}/Applications/fork-linux.AppImage" --install-desktop
	# The file checks look inside the image (never executed for this): its squashfs payload.
	rm -rf "${APPIMAGE_ROOT}"
	offset="$(fl_appimage_squashfs_offset "${image}")"
	unsquashfs -q -o "${offset}" -d "${APPIMAGE_ROOT}" "${image}" >/dev/null
}
appimage_remove() {
	as_tester env APPIMAGE="${TESTER_HOME}/Applications/fork-linux.AppImage" \
		"${TESTER_HOME}/Applications/fork-linux.AppImage" --remove-desktop
	rm -f "${TESTER_HOME}/Applications/fork-linux.AppImage"
	rm -rf "${APPIMAGE_ROOT}"
}
appimage_assert() {
	assert_installed "${APPIMAGE_ROOT}/usr" static \
		"${TESTER_HOME}/.local/share/applications/${APP_ID}.desktop" "${TESTER_HOME}/.local/bin/fork-linux"
	grep -q "^Exec=.*fork-linux.AppImage" "${TESTER_HOME}/.local/share/applications/${APP_ID}.desktop" ||
		fail "the AppImage menu entry does not start the AppImage"
	local out
	out="$(as_tester "${TESTER_HOME}/.local/bin/fork" --version)" || fail "fork --version (link to the AppImage) failed"
	[[ "${out}" == *"${VERSION}"* ]] || fail "fork --version printed '${out}'"
}

FLATPAK_ROOT="/var/lib/flatpak/app/${APP_ID}/current/active/files"
flatpak_install() {
	local bundle
	bundle="$(artifact "${APP_ID}-${VERSION}-x86_64.flatpak")"
	flatpak remote-add --system --if-not-exists flathub https://dl.flathub.org/repo/flathub.flatpakrepo
	flatpak install --system -y --noninteractive --reinstall "${bundle}"
}
flatpak_remove() { flatpak uninstall --system -y --noninteractive "${APP_ID}"; }
flatpak_assert() {
	assert_installed "${FLATPAK_ROOT}" dyn "/var/lib/flatpak/exports/share/applications/${APP_ID}.desktop" \
		flatpak run "${APP_ID}"
	local out
	out="$(as_tester flatpak run --command=fork "${APP_ID}" --version)" || fail "flatpak fork --version failed"
	[[ "${out}" == *"${VERSION}"* ]] || fail "fork --version printed '${out}'"
}

SNAP_ROOT="/snap/fork-linux/current/usr"
snap_install() {
	local snap_file
	snap_file="$(artifact "fork-linux_${VERSION}_amd64.snap")"
	snap install --dangerous --classic "${snap_file}"
}
snap_remove() { snap remove --purge fork-linux; }
snap_assert() {
	assert_installed "${SNAP_ROOT}" static /var/lib/snapd/desktop/applications/fork-linux_fork-linux.desktop \
		/snap/bin/fork-linux
	local out
	out="$(as_tester /snap/bin/fork-linux.fork --version)" || fail "fork-linux.fork --version failed"
	[[ "${out}" == *"${VERSION}"* ]] || fail "fork --version printed '${out}'"
}

# Container base images drop documentation at install time (dpkg path-exclude, dnf
# tsflags=nodocs, zypper excludedocs, pacman NoExtract); a real system keeps man pages, so
# the E2E does too.
enable_docs() {
	rm -f /etc/dpkg/dpkg.cfg.d/excludes
	if [[ -f /etc/dnf/dnf.conf ]]; then
		sed -i '/^tsflags=nodocs/d' /etc/dnf/dnf.conf
	fi
	if [[ -f /etc/zypp/zypp.conf ]]; then
		sed -i 's/^rpm.install.excludedocs.*/rpm.install.excludedocs = no/' /etc/zypp/zypp.conf
	fi
	if [[ -f /etc/pacman.conf ]]; then
		sed -i '/^NoExtract/d' /etc/pacman.conf
	fi
}

enable_docs
setup_tester
case "${FORMAT}" in
	deb | deb-jammy) cycle deb_install deb_remove dyn_assert /usr ;;
	rpm-fedora) cycle fedora_install fedora_remove dyn_assert /usr ;;
	rpm-opensuse) cycle opensuse_install opensuse_remove dyn_assert /usr ;;
	arch) cycle arch_install arch_remove dyn_assert /usr ;;
	tarball) cycle tarball_install tarball_remove tarball_assert "${TESTER_HOME}/.local/opt/fork-linux" "${TESTER_HOME}/.local" ;;
	appimage) cycle appimage_install appimage_remove appimage_assert "${TESTER_HOME}/.local" ;;
	flatpak) cycle flatpak_install flatpak_remove flatpak_assert "${FLATPAK_ROOT}" ;;
	snap) cycle snap_install snap_remove snap_assert "${SNAP_ROOT}" ;;
	*) fail "unknown format ${FORMAT}" ;;
esac
assert_markers
echo "==> packaging E2E OK: ${FORMAT} ${VERSION} (install, upgrade, remove, reinstall)"
