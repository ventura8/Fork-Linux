#!/usr/bin/env bash
# packaging-e2e-install.sh - live install E2E of one built package of Fork for Linux (unofficial).
#
# Runs as root inside the format's packaging image (the snap cell: inside the systemd
# container of scripts/ci-snap-build.sh) against artifacts/ and the previous release in
# $FL_PREVIOUS_ARTIFACTS_DIR (version $FL_PREVIOUS_VERSION, built by ci-packaging-cell.sh with
# scripts/build-version-variant.sh). Cycle (AGENTS.md §4.9, docs/qa/UPDATE-SCENARIOS.md A2/A8):
#   install N-1 -> assert -> upgrade to N -> assert -> downgrade to N-1 -> assert ->
#   upgrade to N -> assert -> upgrade in place (install N again) -> assert -> remove ->
#   assert removed -> reinstall -> assert
# The program is run as the unprivileged user "tester" (hard rule 9); only check_root_refused
# runs it as root, to check that it refuses. Asserts:
#   * fork-linux --version and fork --version print the version just installed
#   * fork-linux doctor --offline --json works with no Wine present (JSON, exit 0 or 17)
#   * the shims (win64/fl-shim.exe, fl-launch.exe) are PE32+ x86-64
#   * fl-bridge-helper is an x86-64 ELF: ET_EXEC (static musl) for the portable channels
#     (tarball, AppImage, snap); the distro packages and the Flatpak ship the glibc PIE
#     build (bridge/README.md "Install layout"), so ET_DYN is accepted there
#   * the desktop entry validates; metainfo, man pages and completions are installed
#   * the menu entry's command starts the version just installed
#   * deb / rpm / arch: as root, fork-linux setup refuses (exit 19) and creates nothing, not even a
#     bytecode cache in /usr/share/fork-linux
#   * tester's ~/.local/share/fork-linux/E2E_MARKER and ~/.wine/E2E_SENTINEL survive every step
#   * the next launch re-runs only what changed: N-1 leaves a simulated, completed setup in
#     tester's home (scripts/packaging-e2e-state.py: our prefix marker, a placeholder Fork,
#     the managed runtime's marker with stand-in wine scripts, state.json with N-1's own step
#     markers); after every operation 'fork-linux setup --list-steps --json' of the installed
#     release must report nothing to re-run under N-1 and only host_shims (the shims carry the
#     release's version) under N, never a step that downloads; no Wine runs and the simulated
#     setup is never changed (scenario A2) - except by the remove of a per-user install
#     (tarball: uninstall.sh, AppImage: --remove-desktop), which clears exactly the icon and
#     desktop_entry markers, so the reinstalled release re-runs those two as well (A11)
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
PREV_ART="${FL_PREVIOUS_ARTIFACTS_DIR:?set FL_PREVIOUS_ARTIFACTS_DIR to the previous release (ci-packaging-cell.sh does)}"
PREV_VERSION="${FL_PREVIOUS_VERSION:?set FL_PREVIOUS_VERSION}"
# The release the install functions use and the asserts expect (use_release).
REL_ART="${ART}"
REL_VERSION="${VERSION}"
TESTER="tester"
TESTER_HOME="/home/${TESTER}"
MARKER="${TESTER_HOME}/.local/share/fork-linux/E2E_MARKER"
SENTINEL="${TESTER_HOME}/.wine/E2E_SENTINEL"
MARK_VALUE="fork-linux-e2e-${FORMAT}-$$-${RANDOM}"
STAGE="/tmp/fl-e2e-${FORMAT}"
TESTER_RUNTIME="/tmp/fl-e2e-runtime-${TESTER}"
TESTER_SESSION_ENV=()
# The simulated setup (seed_setup_state): the release that wrote it and the digest of its files.
STATE_TOOL="${STAGE}/packaging-e2e-state.py"
SEED_VERSION=""
SEED_DIGEST=""
SEED_MARKERS=""
# The setup markers the remove of this format clears (set per format) and those it did clear.
REMOVE_FORGETS=""
FORGOTTEN=""
# Steps the next launch re-runs because the scenario changed their inputs (appimage_moved).
NEXT_ALSO=""
# A format's own scenario, run by cycle once N is installed over N-1 again (every marker recorded).
AFTER_UPGRADES=""

fail() {
	echo "E2E FAIL (${FORMAT}): $*" >&2
	exit 1
}

# An "upgrade" between equal versions tests nothing: N-1 must sort strictly below N.
[[ "${PREV_VERSION}" != "${VERSION}" && "$(printf '%s\n' "${VERSION}" "${PREV_VERSION}" | sort -V | head -n 1)" == "${PREV_VERSION}" ]] ||
	fail "the previous release ${PREV_VERSION} is not older than ${VERSION}: the upgrade would test nothing"
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
	install -m 0644 scripts/packaging-e2e-state.py "${STATE_TOOL}"
	install -m 0644 src/fork_linux/data/runtime-manifest.json "${STAGE}/runtime-manifest.json"
	# A session-like runtime directory (flatpak run allocates its instance there).
	install -d -o "${TESTER}" -m 0700 "/tmp/fl-e2e-runtime-${TESTER}"
	if [[ "${FORMAT}" == snap ]]; then
		setup_tester_session
	fi
}

# snapd (2.7x, Ubuntu 26.04) tracks every snap process in a systemd scope and refuses users
# without a systemd user session (forum.snapcraft.io/t/46210). A lingering user manager gives
# the tester one; runuser then talks to it through its runtime dir and session bus.
setup_tester_session() {
	local uid
	uid="$(id -u "${TESTER}")"
	loginctl enable-linger "${TESTER}"
	for _ in $(seq 1 60); do
		[[ -S "/run/user/${uid}/bus" ]] && break
		sleep 1
	done
	[[ -S "/run/user/${uid}/bus" ]] || fail "no systemd user session bus for ${TESTER}"
	TESTER_RUNTIME="/run/user/${uid}"
	TESTER_SESSION_ENV=("DBUS_SESSION_BUS_ADDRESS=unix:path=${TESTER_RUNTIME}/bus")
}

as_tester() {
	runuser -u "${TESTER}" -- env -i \
		HOME="${TESTER_HOME}" USER="${TESTER}" LOGNAME="${TESTER}" LANG=C.UTF-8 \
		PATH="${TESTER_HOME}/.local/bin:/usr/local/bin:/usr/bin:/bin:/snap/bin" \
		XDG_RUNTIME_DIR="${TESTER_RUNTIME}" "${TESTER_SESSION_ENV[@]}" \
		APPIMAGE_EXTRACT_AND_RUN=1 NO_CLEANUP=1 \
		"$@"
}

assert_markers() {
	[[ "$(cat "${MARKER}" 2>/dev/null)" == "${MARK_VALUE}" ]] || fail "${MARKER} was changed or removed"
	[[ "$(cat "${SENTINEL}" 2>/dev/null)" == "${MARK_VALUE}" ]] || fail "${SENTINEL} was changed or removed (~/.wine must never be touched)"
}

# ---------------------------------------------------------------------- the next launch (A2)

state_tool() {
	as_tester python3 -I "${STATE_TOOL}" "$@"
}

# seed_setup_state CMD...: a completed setup as the release just installed (N-1) records it.
seed_setup_state() {
	echo "==> simulated setup of fork-linux ${REL_VERSION} in ${TESTER_HOME}"
	as_tester "$@" status --json >"${STAGE}/status.json" || fail "fork-linux status --json failed"
	state_tool prepare "${STAGE}/status.json" "${STAGE}/runtime-manifest.json" || fail "preparing the simulated setup failed"
	as_tester "$@" setup --list-steps --json >"${STAGE}/steps-seed.json" || fail "fork-linux setup --list-steps --json failed"
	state_tool record "${STAGE}/status.json" "${STAGE}/steps-seed.json" || fail "recording the setup markers failed"
	SEED_VERSION="${REL_VERSION}"
	SEED_DIGEST="$(state_tool digest "${STAGE}/status.json")" || fail "reading the simulated setup failed"
	SEED_MARKERS="$(state_tool markers "${STAGE}/status.json")" || fail "reading the setup markers failed"
}

# assert_seeded: the simulated setup is unchanged and no Wine ran.
assert_seeded() {
	[[ -n "${SEED_DIGEST}" ]] || return 0
	local now
	now="$(state_tool digest "${STAGE}/status.json")" || fail "the simulated setup in ${TESTER_HOME} is broken"
	[[ "${now}" == "${SEED_DIGEST}" ]] || fail "the simulated setup in ${TESTER_HOME} was changed (${SEED_DIGEST} -> ${now})"
	echo "ok  simulated setup unchanged, no Wine ran"
}

# assert_remove_cleared: the remove cleared exactly the markers of REMOVE_FORGETS (then the
# simulated setup is compared with what is left).
assert_remove_cleared() {
	[[ -n "${SEED_DIGEST}" ]] || return 0
	local cleared
	cleared="$(state_tool missing "${STAGE}/status.json" "${SEED_MARKERS}")" || fail "reading the setup markers failed"
	[[ "${cleared}" == "${REMOVE_FORGETS}" ]] ||
		fail "the remove cleared the setup markers '${cleared}', expected '${REMOVE_FORGETS}'"
	if [[ -n "${cleared}" ]]; then
		FORGOTTEN="${cleared}"
		SEED_DIGEST="$(state_tool digest "${STAGE}/status.json")" || fail "reading the simulated setup failed"
		echo "ok  the remove cleared the ${cleared} markers (the next launch restores them)"
	fi
	assert_seeded
}

# check_next_launch CMD...: what the installed release's next launch would re-run (plus the
# steps in NEXT_ALSO, comma-separated: what the scenario changed on purpose).
check_next_launch() {
	[[ -n "${SEED_VERSION}" ]] || seed_setup_state "$@"
	local changed="${FORGOTTEN}"
	[[ -z "${NEXT_ALSO}" ]] || changed="${changed:+${changed},}${NEXT_ALSO}"
	# fl-shim.exe and fl-launch.exe carry the release's version (VERSIONINFO), so another
	# release than the one that set up copies its shims again; every other step stays done.
	[[ "${REL_VERSION}" == "${SEED_VERSION}" ]] || changed="${changed:+${changed},}host_shims"
	if ! as_tester "$@" setup --list-steps --json >"${STAGE}/steps.json" 2>"${STAGE}/steps.err"; then
		cat "${STAGE}/steps.err" >&2
		fail "fork-linux setup --list-steps --json failed"
	fi
	state_tool check "${STAGE}/steps.json" "${changed}" || fail "the next launch of ${REL_VERSION} would re-run the wrong steps"
	assert_seeded
}

# check_menu_command DESKTOP: the menu entry's command (Exec without its file arguments) runs this release.
check_menu_command() {
	local desktop="$1" out
	local -a argv
	mapfile -t argv < <(python3 - "${desktop}" <<'EOF'
import shlex
import sys

exec_line = ""
with open(sys.argv[1], encoding="utf-8") as handle:
    for line in handle:
        if line.startswith("[") and line.strip() != "[Desktop Entry]":
            break
        if line.startswith("Exec="):
            exec_line = line[len("Exec="):].strip()
words = [word for word in shlex.split(exec_line) if word not in ("@@", "@@u") and not (len(word) == 2 and word[0] == "%")]
if words and words[-1] == "run":
    words.pop()
print("\n".join(words))
EOF
	)
	((${#argv[@]} > 0)) || fail "no Exec command in ${desktop}"
	out="$(as_tester "${argv[@]}" --version)" || fail "the menu entry's command '${argv[*]}' failed"
	[[ "${out}" == *"${REL_VERSION}"* ]] || fail "the menu entry's command '${argv[*]}' printed '${out}', expected ${REL_VERSION}"
	echo "ok  menu entry starts fork-linux ${REL_VERSION} (${argv[*]})"
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
	[[ "${out}" == *"fork-linux ${REL_VERSION} "* ]] || fail "fork-linux --version printed '${out}', expected ${REL_VERSION}"
	echo "ok  ${out}"
	check_doctor_json "${cmd[@]}"
	check_binaries "${root}" "${helper_kind}"
	[[ -f "${desktop}" ]] || fail "desktop entry missing: ${desktop}"
	desktop-file-validate "${desktop}" || fail "desktop-file-validate ${desktop}"
	echo "ok  desktop entry ${desktop}"
	check_menu_command "${desktop}"
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
	check_next_launch "${cmd[@]}"
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
	assert_remove_cleared
	echo "ok  removed; markers intact"
}

artifact() {
	local pattern="$1" match
	match="$(compgen -G "${REL_ART}/${pattern}" | head -n 1 || true)"
	[[ -n "${match}" ]] || fail "no artifact ${REL_ART}/${pattern}"
	readlink -f "${match}"
}

# use_release DIR VERSION: the artifacts the install functions take and the version the asserts expect.
use_release() {
	REL_ART="$1"
	REL_VERSION="$2"
}

# ---------------------------------------------------------------------- formats

# upgrade_from_previous INSTALL ASSERT: N-1 installed, then N installed over it with the
# format's own upgrade (scenario A2): --version changes, the markers survive, the menu entry
# and the commands start the new install.
upgrade_from_previous() {
	local install_fn="$1" assert_fn="$2"
	echo "==> E2E install N-1 (${FORMAT} ${PREV_VERSION})"
	use_release "${PREV_ART}" "${PREV_VERSION}"
	"${install_fn}"
	"${assert_fn}"
	echo "==> E2E upgrade N-1 -> N (${FORMAT} ${PREV_VERSION} -> ${VERSION})"
	use_release "${ART}" "${VERSION}"
	"${install_fn}"
	"${assert_fn}"
}

# downgrade_to_previous INSTALL ASSERT: back from N to N-1 and up to N again (scenario A8).
downgrade_to_previous() {
	local install_fn="$1" assert_fn="$2"
	echo "==> E2E downgrade N -> N-1 (${FORMAT} ${VERSION} -> ${PREV_VERSION})"
	use_release "${PREV_ART}" "${PREV_VERSION}"
	"${install_fn}"
	"${assert_fn}"
	echo "==> E2E upgrade N-1 -> N again (${FORMAT})"
	use_release "${ART}" "${VERSION}"
	"${install_fn}"
	"${assert_fn}"
}

# cycle INSTALL REMOVE ASSERT ROOT...: the E2E cycle; ROOTs are checked after the remove.
cycle() {
	local install_fn="$1" remove_fn="$2" assert_fn="$3"
	shift 3
	upgrade_from_previous "${install_fn}" "${assert_fn}"
	downgrade_to_previous "${install_fn}" "${assert_fn}"
	if [[ -n "${AFTER_UPGRADES}" ]]; then
		"${AFTER_UPGRADES}"
	fi
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

# Hard rule 9: run as root, fork-linux refuses (exit 19 = UNSUPPORTED_ENV) before it creates or
# downloads anything; --allow-root lets a command run (containers and CI only).
check_root_refused() {
	local home rc=0 out before
	home="$(mktemp -d "${STAGE}/root-home.XXXXXX")"
	before="$(find /usr/share/fork-linux | sort)"
	env -i HOME="${home}" PATH=/usr/bin:/bin LANG=C.UTF-8 \
		fork-linux setup --accept-fork-eula --no-gui >"${STAGE}/root.out" 2>"${STAGE}/root.err" || rc=$?
	if ((rc != 19)); then
		cat "${STAGE}/root.err" >&2
		fail "fork-linux setup as root exited ${rc} (expected 19 = refused)"
	fi
	grep -q "refusing to run as root" "${STAGE}/root.err" || fail "setup as root did not say why it refused"
	[[ -z "$(ls -A "${home}")" ]] || fail "setup as root created files in its home: $(ls -A "${home}")"
	out="$(env -i HOME="${home}" PATH=/usr/bin:/bin LANG=C.UTF-8 fork-linux --allow-root version)" ||
		fail "fork-linux --allow-root version failed"
	[[ "${out}" == *"fork-linux ${REL_VERSION}"* ]] || fail "fork-linux --allow-root version printed '${out}'"
	# Nothing (no bytecode cache) is written into the packaged tree, so the removal stays clean.
	[[ "$(find /usr/share/fork-linux | sort)" == "${before}" ]] ||
		fail "running as root changed /usr/share/fork-linux: $(diff <(echo "${before}") <(find /usr/share/fork-linux | sort))"
	rm -rf -- "${home:?}"
	echo "ok  refused as root (exit 19); --allow-root runs"
}

system_assert() {
	local helper_kind="$1"
	assert_installed /usr "${helper_kind}" "/usr/share/applications/${APP_ID}.desktop" fork-linux
	local out
	out="$(as_tester fork --version)" || fail "fork --version failed"
	[[ "${out}" == *"${REL_VERSION}"* ]] || fail "fork --version printed '${out}'"
	check_root_refused
}
dyn_assert() { system_assert dyn; }

# installed_version: the version the package manager has installed (empty when none).
installed_version() {
	local status=""
	case "${FORMAT}" in
		deb | deb-jammy)
			status="$(dpkg-query -W -f='${Status} ${Version}' fork-linux 2>/dev/null || true)"
			if [[ "${status}" == "install ok installed "* ]]; then
				printf '%s\n' "${status##* }"
			fi
			;;
		rpm-fedora | rpm-opensuse)
			# rpm -q prints "package ... is not installed" on stdout: ask for the version only when it is.
			if rpm -q fork-linux >/dev/null 2>&1; then
				rpm -q --qf '%{VERSION}' fork-linux
			fi
			;;
	esac
}

deb_install() {
	local deb
	deb="$(artifact "fork-linux_${REL_VERSION}_amd64.deb")"
	cp "${deb}" "${STAGE}/"
	apt-get update -qq
	if [[ "$(installed_version)" == "${REL_VERSION}" ]]; then
		apt-get install -y --reinstall "${STAGE}/$(basename "${deb}")"
	else
		# A newer .deb upgrades; --allow-downgrades lets the downgrade step go back to N-1.
		apt-get install -y --allow-downgrades "${STAGE}/$(basename "${deb}")"
	fi
}
deb_remove() { apt-get purge -y fork-linux; }

fedora_install() {
	local rpm installed
	rpm="$(artifact "fork-linux-${REL_VERSION}-*.fc*.x86_64.rpm")"
	installed="$(installed_version)"
	if [[ -z "${installed}" ]]; then
		dnf install -y --nogpgcheck "${rpm}"
	elif [[ "${installed}" == "${REL_VERSION}" ]]; then
		dnf reinstall -y --nogpgcheck "${rpm}"
	elif [[ "${REL_VERSION}" == "${PREV_VERSION}" ]]; then
		dnf downgrade -y --nogpgcheck "${rpm}"
	else
		dnf upgrade -y --nogpgcheck "${rpm}"
	fi
}
fedora_remove() { dnf remove -y fork-linux; }

opensuse_install() {
	local rpm installed
	rpm="$(artifact "fork-linux-${REL_VERSION}-*.lp*.x86_64.rpm")"
	installed="$(installed_version)"
	if [[ "${installed}" == "${REL_VERSION}" ]]; then
		zypper --non-interactive --no-gpg-checks install --force --allow-unsigned-rpm "${rpm}"
	elif [[ -n "${installed}" && "${REL_VERSION}" == "${PREV_VERSION}" ]]; then
		zypper --non-interactive --no-gpg-checks install --oldpackage --allow-unsigned-rpm "${rpm}"
	else
		zypper --non-interactive --no-gpg-checks install --allow-unsigned-rpm "${rpm}"
	fi
}
opensuse_remove() { zypper --non-interactive remove fork-linux; }

arch_install() {
	local pkg
	pkg="$(artifact "fork-linux-${REL_VERSION}-*-x86_64.pkg.tar.zst")"
	pacman -Sy --noconfirm
	pacman -U --noconfirm "${pkg}"
}
arch_remove() { pacman -Rns --noconfirm fork-linux; }

tarball_install() {
	local tarball
	tarball="$(artifact "fork-linux-${REL_VERSION}-x86_64.tar.gz")"
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
	[[ "${out}" == *"${REL_VERSION}"* ]] || fail "fork --version printed '${out}'"
	[[ -f "${TESTER_HOME}/.local/share/man/man1/fork-linux.1" ]] || fail "per-user man page missing"
}

APPIMAGE_ROOT="${STAGE}/appimage-root"
appimage_install() {
	local image offset
	image="$(artifact "fork-linux-${REL_VERSION}-x86_64.AppImage")"
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
# The update information AppImageUpdate reads (ELF section .upd_info): the GitHub releases zsync.
APPIMAGE_UPDATE_INFO="gh-releases-zsync|ventura8|Fork-Linux|latest|fork-linux-*-x86_64.AppImage.zsync"
check_appimage_update_info() {
	local image="$1"
	python3 - "${image}" "${APPIMAGE_UPDATE_INFO}" <<'EOF'
import struct
import sys

data = open(sys.argv[1], "rb").read()
(shoff,) = struct.unpack_from("<Q", data, 0x28)
shentsize, shnum, shstrndx = struct.unpack_from("<HHH", data, 0x3A)


def section(index: int) -> tuple[int, int, int]:
    base = shoff + index * shentsize
    (name,) = struct.unpack_from("<I", data, base)
    offset, size = struct.unpack_from("<QQ", data, base + 0x18)
    return name, offset, size


_n, names_at, _s = section(shstrndx)
for index in range(shnum):
    name, offset, size = section(index)
    if data[names_at + name:data.index(b"\0", names_at + name)] == b".upd_info":
        found = data[offset:offset + size].split(b"\0", 1)[0].decode()
        if found != sys.argv[2]:
            raise SystemExit(f"update information is {found!r}, expected {sys.argv[2]!r}")
        print(f"ok  AppImage update information: {found}")
        break
else:
    raise SystemExit("the AppImage has no .upd_info section")
EOF
}

appimage_assert() {
	check_appimage_update_info "$(artifact "fork-linux-${REL_VERSION}-x86_64.AppImage")"
	# The stable helper launchers fork-linux writes for Wine and Fork run helpers through this.
	local helper_out
	helper_out="$(as_tester "${TESTER_HOME}/Applications/fork-linux.AppImage" --libexec fork-linux-host --help)" ||
		fail "AppImage --libexec fork-linux-host --help failed"
	[[ "${helper_out}" == *"usage: fork-linux-host"* ]] || fail "AppImage --libexec printed '${helper_out}'"
	if as_tester "${TESTER_HOME}/Applications/fork-linux.AppImage" --libexec fork-linux-handoff >/dev/null 2>&1; then
		fail "AppImage --libexec ran a helper that is not on its list"
	fi
	echo "ok  AppImage --libexec runs the shipped helpers (and only those)"
	assert_installed "${APPIMAGE_ROOT}/usr" static \
		"${TESTER_HOME}/.local/share/applications/${APP_ID}.desktop" "${TESTER_HOME}/.local/bin/fork-linux"
	grep -q "^Exec=.*fork-linux.AppImage" "${TESTER_HOME}/.local/share/applications/${APP_ID}.desktop" ||
		fail "the AppImage menu entry does not start the AppImage"
	local out
	out="$(as_tester "${TESTER_HOME}/.local/bin/fork" --version)" || fail "fork --version (link to the AppImage) failed"
	[[ "${out}" == *"${REL_VERSION}"* ]] || fail "fork --version printed '${out}'"
}

# appimage_moved: (N installed over N-1, every marker recorded) the AppImage file is moved and
# renamed (scenario A9, a name with spaces). Its next start from the new place re-runs
# desktop_entry (the menu entry's command changed) besides what the upgrade itself re-runs;
# the helper paths Wine's registry and Fork's settings keep are the stable launchers in
# <data_dir>/helpers, which follow the file, so no other step re-runs; --install-desktop from
# the new place re-points the menu entry and the fork / fork-linux links.
appimage_moved() {
	local moved="${TESTER_HOME}/Apps/Fork Linux (moved).AppImage" helpers name desktop out
	desktop="${TESTER_HOME}/.local/share/applications/${APP_ID}.desktop"
	helpers="${TESTER_HOME}/.local/share/fork-linux/helpers"
	echo "==> E2E AppImage moved and renamed (${FORMAT} ${REL_VERSION})"
	as_tester mkdir -p "${TESTER_HOME}/Apps"
	as_tester mv "${TESTER_HOME}/Applications/fork-linux.AppImage" "${moved}"
	NEXT_ALSO="desktop_entry"
	check_next_launch env APPIMAGE="${moved}" "${moved}"
	# The launchers Wine's registry and Fork's settings name (fork-linux-host's appears with the bridge).
	for name in fork-linux-terminal fork-linux-explorer fork-linux-open-url; do
		[[ -x "${helpers}/${name}" ]] || fail "no stable helper launcher ${helpers}/${name}"
		grep -qF -- "exec '${moved}' --libexec ${name} " "${helpers}/${name}" ||
			fail "${helpers}/${name} does not run the moved AppImage"
		if grep -qF -- "${TESTER_HOME}/Applications/fork-linux.AppImage" "${helpers}/${name}"; then
			fail "${helpers}/${name} still names the old AppImage path"
		fi
	done
	out="$(as_tester "${moved}" --libexec fork-linux-host --help)" || fail "the moved AppImage's --libexec failed"
	[[ "${out}" == *"usage: fork-linux-host"* ]] || fail "the moved AppImage's --libexec printed '${out}'"
	echo "ok  the stable helper launchers run the moved AppImage"
	as_tester env APPIMAGE="${moved}" "${moved}" --install-desktop
	grep -qF -- "Fork Linux (moved).AppImage" "${desktop}" || fail "the menu entry does not start the moved AppImage"
	check_menu_command "${desktop}"
	[[ "$(readlink "${TESTER_HOME}/.local/bin/fork")" == "${moved}" ]] || fail "${TESTER_HOME}/.local/bin/fork does not lead to the moved AppImage"
	out="$(as_tester "${TESTER_HOME}/.local/bin/fork" --version)" || fail "fork --version (link to the moved AppImage) failed"
	[[ "${out}" == *"${REL_VERSION}"* ]] || fail "fork --version printed '${out}'"
	echo "ok  menu entry and links follow the moved AppImage"
	assert_markers
	assert_seeded
	NEXT_ALSO=""
	# The cycle goes on with a fresh install at the usual place, which points everything back there.
	as_tester rm -f "${moved}"
}

FLATPAK_ROOT="/var/lib/flatpak/app/${APP_ID}/current/active/files"
flatpak_install() {
	local bundle
	bundle="$(artifact "${APP_ID}-${REL_VERSION}-x86_64.flatpak")"
	flatpak remote-add --system --if-not-exists flathub https://dl.flathub.org/repo/flathub.flatpakrepo
	flatpak install --system -y --noninteractive --reinstall "${bundle}"
}
flatpak_remove() { flatpak uninstall --system -y --noninteractive "${APP_ID}"; }
flatpak_assert() {
	assert_installed "${FLATPAK_ROOT}" dyn "/var/lib/flatpak/exports/share/applications/${APP_ID}.desktop" \
		flatpak run "${APP_ID}"
	local out
	out="$(as_tester flatpak run --command=fork "${APP_ID}" --version)" || fail "flatpak fork --version failed"
	[[ "${out}" == *"${REL_VERSION}"* ]] || fail "fork --version printed '${out}'"
}

SNAP_ROOT="/snap/fork-linux/current/usr"
snap_install() {
	local snap_file
	snap_file="$(artifact "fork-linux_${REL_VERSION}_amd64.snap")"
	snap install --dangerous --classic "${snap_file}"
}
snap_remove() { snap remove --purge fork-linux; }
snap_assert() {
	assert_installed "${SNAP_ROOT}" static /var/lib/snapd/desktop/applications/fork-linux_fork-linux.desktop \
		/snap/bin/fork-linux
	local out
	out="$(as_tester /snap/bin/fork-linux.fork --version)" || fail "fork-linux.fork --version failed"
	[[ "${out}" == *"${REL_VERSION}"* ]] || fail "fork --version printed '${out}'"
}

# snap_revision: the revision of fork-linux snapd runs now ("x1", "x2"... for local snaps).
snap_revision() {
	snap list fork-linux | awk '$1 == "fork-linux" { print $3 }'
}

# snap_revert: a refresh the user takes back (scenario A10). N is installed (the cycle ended
# with it); N-1 arrives as a new revision, 'snap revert' goes back to N's revision and
# 'snap revert --revision' forward to N-1's again, then back to N: every revision runs its own
# version, the helper paths fork-linux stored go through /snap/fork-linux/current, so the next
# launch re-runs only what the version change needs, and the user's data stays.
snap_revert() {
	local n_rev n1_rev
	n_rev="$(snap_revision)"
	[[ -n "${n_rev}" ]] || fail "no installed fork-linux snap revision"
	echo "==> E2E snap refresh to N-1 as a new revision (from ${n_rev})"
	use_release "${PREV_ART}" "${PREV_VERSION}"
	snap_install
	n1_rev="$(snap_revision)"
	[[ "${n1_rev}" != "${n_rev}" ]] || fail "installing ${PREV_VERSION} did not create a new revision"
	snap_assert
	echo "==> E2E snap revert (${n1_rev} -> ${n_rev})"
	snap revert fork-linux
	[[ "$(snap_revision)" == "${n_rev}" ]] || fail "snap revert did not go back to revision ${n_rev}"
	use_release "${ART}" "${VERSION}"
	snap_assert
	echo "==> E2E snap revert --revision ${n1_rev}, then ${n_rev}"
	snap revert --revision "${n1_rev}" fork-linux
	use_release "${PREV_ART}" "${PREV_VERSION}"
	snap_assert
	snap revert --revision "${n_rev}" fork-linux
	use_release "${ART}" "${VERSION}"
	snap_assert
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
	tarball)
		REMOVE_FORGETS="desktop_entry,icon"
		cycle tarball_install tarball_remove tarball_assert "${TESTER_HOME}/.local/opt/fork-linux" "${TESTER_HOME}/.local" ;;
	appimage)
		REMOVE_FORGETS="desktop_entry,icon"
		AFTER_UPGRADES=appimage_moved
		cycle appimage_install appimage_remove appimage_assert "${TESTER_HOME}/.local" ;;
	flatpak) cycle flatpak_install flatpak_remove flatpak_assert "${FLATPAK_ROOT}" ;;
	snap)
		cycle snap_install snap_remove snap_assert "${SNAP_ROOT}"
		snap_revert ;;
	*) fail "unknown format ${FORMAT}" ;;
esac
assert_markers
assert_seeded
echo "==> packaging E2E OK: ${FORMAT} ${PREV_VERSION} -> ${VERSION} (upgrade, downgrade, upgrade in place, remove, reinstall)"
