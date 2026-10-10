#!/usr/bin/env bash
# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  fl-spike.sh — Phase 0 feasibility spikes for Fork for Linux (unofficial) ║
# ║                                                                          ║
# ║  Runs each spike in a fully isolated scratch root (fake HOME, XDG dirs,  ║
# ║  WINEPREFIX) so the user's ~/.wine, ~/Desktop and caches are untouched.  ║
# ║                                                                          ║
# ║  Usage: FL_SPIKE_ROOT=/scratch/dir scripts/spike/fl-spike.sh <spike…>    ║
# ║    spikes: s1 (wine runtime) s2 (dotnet) s3 (fork install) s4 (render)   ║
# ║            all (s1..s4 in order)                                         ║
# ╚══════════════════════════════════════════════════════════════════════════╝
set -euo pipefail

# Containers only (AGENTS.md hard rule 18): the spikes start Wine and the official Fork, e.g.
#   scripts/e2e-docker.sh --name spike --keep shell -- env FL_SPIKE_ROOT=/e2e/spike \
#       scripts/spike/fl-spike.sh s1
if [[ ! -e /.dockerenv && ! -e /run/.containerenv ]]; then
	printf 'fl-spike: refusing to run Wine / Fork on the host (AGENTS.md hard rule 18); use\n' >&2
	printf '  scripts/e2e-docker.sh --name spike --keep shell -- env FL_SPIKE_ROOT=/e2e/spike scripts/spike/fl-spike.sh %s\n' "${1:-all}" >&2
	exit 2
fi

: "${FL_SPIKE_ROOT:?set FL_SPIKE_ROOT to a scratch directory}"
[[ "${FL_SPIKE_ROOT}" == /?* && "/${FL_SPIKE_ROOT}/" != */../* ]] ||
	{ printf 'fl-spike: FL_SPIKE_ROOT must be an absolute path below / without ..\n' >&2; exit 2; }

readonly WINE_BUILD_ID="wine-11.0-staging-amd64-wow64"
readonly WINE_URL="https://github.com/Kron4ek/Wine-Builds/releases/download/11.0/${WINE_BUILD_ID}.tar.xz"
readonly WINE_SHA256="e6538a417dd2e7f738ad77addf74bfd9e45c9d970653674f93bde2df29055f2f"
readonly WINETRICKS_VERSION="20260125"
readonly WINETRICKS_URL="https://raw.githubusercontent.com/Winetricks/winetricks/${WINETRICKS_VERSION}/src/winetricks"
readonly FORK_VERSION="${FL_SPIKE_FORK_VERSION:-2.23.2}"
readonly FORK_URL="https://cdn.fork.dev/win/Fork-${FORK_VERSION}.exe"
readonly FORK_FEED_URL="https://git-fork.com/update/win/releases.win.json"

ROOT="$(mkdir -p "$FL_SPIKE_ROOT" && cd "$FL_SPIKE_ROOT" && pwd)"
readonly ROOT
readonly DL="$ROOT/downloads"          # untrusted downloads only
readonly RUNTIME="$ROOT/runtimes/$WINE_BUILD_ID"
readonly RESULTS="$ROOT/results"
readonly SPIKE_DISPLAY="${FL_SPIKE_DISPLAY:-:97}"

export HOME="$ROOT/home"
export XDG_CONFIG_HOME="$HOME/.config"
export XDG_DATA_HOME="$HOME/.local/share"
export XDG_CACHE_HOME="$HOME/.cache"
export XDG_STATE_HOME="$HOME/.local/state"
export WINEPREFIX="$ROOT/prefix"
export WINEARCH=win64
export WINEDEBUG="${FL_SPIKE_WINEDEBUG:--all}"
export WINEDLLOVERRIDES="winemenubuilder.exe=d"
export WINETRICKS_LATEST_VERSION_CHECK=disabled

mkdir -p "$DL" "$RESULTS" "$HOME" "$XDG_CONFIG_HOME" "$XDG_DATA_HOME" "$XDG_CACHE_HOME" "$XDG_STATE_HOME"

log() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*" | tee -a "$RESULTS/spike.log" >&2; }
result() { printf '%s=%s\n' "$1" "$2" | tee -a "$RESULTS/results.env" >&2; }
die() { log "FATAL: $*"; exit 1; }

fetch() { # url dest
	local url="$1" dest="$2"
	if [[ -s "$dest" ]]; then
		log "cached: $dest"
		return 0
	fi
	log "download: $url"
	curl --proto '=https' --tlsv1.2 -fL --retry 3 --retry-delay 2 -o "$dest.part" "$url"
	mv "$dest.part" "$dest"
}

sha256_of() { sha256sum "$1" | awk '{print $1}'; }

use_wine() {
	export PATH="$RUNTIME/bin:$PATH"
	export WINE="$RUNTIME/bin/wine"
	export WINESERVER="$RUNTIME/bin/wineserver"
	export WINELOADER="$RUNTIME/bin/wine"
}

start_xvfb() {
	if xdpyinfo -display "$SPIKE_DISPLAY" >/dev/null 2>&1; then
		export DISPLAY="$SPIKE_DISPLAY"
		return 0
	fi
	log "starting Xvfb on $SPIKE_DISPLAY"
	Xvfb "$SPIKE_DISPLAY" -screen 0 1600x1000x24 -nolisten tcp >"$RESULTS/xvfb.log" 2>&1 &
	echo $! >"$RESULTS/xvfb.pid"
	export DISPLAY="$SPIKE_DISPLAY"
	local _
	for _ in $(seq 1 50); do
		xdpyinfo -display "$SPIKE_DISPLAY" >/dev/null 2>&1 && return 0
		sleep 0.2
	done
	die "Xvfb did not start"
}

reg_query() { # key value -> prints data or empty
	wine reg query "$1" /v "$2" 2>/dev/null | tr -d '\r' | awk -v v="$2" '$1==v {print $NF}'
}

# ── S1: managed Wine runtime ────────────────────────────────────────────────
spike_s1() {
	log "S1: Kron4ek ${WINE_BUILD_ID}"
	local tarball="$DL/${WINE_BUILD_ID}.tar.xz" got
	fetch "$WINE_URL" "$tarball"
	got="$(sha256_of "$tarball")"
	[[ "$got" == "$WINE_SHA256" ]] || die "wine tarball sha256 mismatch: $got"
	result S1_WINE_SHA256_OK yes
	if [[ ! -x "$RUNTIME/bin/wine" ]]; then
		mkdir -p "$RUNTIME.tmp"
		tar -xJf "$tarball" -C "$RUNTIME.tmp" --strip-components=1 --no-same-owner
		mv "$RUNTIME.tmp" "$RUNTIME"
	fi
	use_wine
	result S1_WINE_VERSION "$(wine --version)"
	log "ldd scan for missing host libraries"
	local missing
	missing="$(find "$RUNTIME/lib" -name '*.so*' -path '*x86_64-unix*' -exec ldd {} + 2>/dev/null | awk '/not found/ {print $1}' | sort -u | tr '\n' ' ')"
	result S1_MISSING_LIBS "${missing:-none}"
	start_xvfb
	local t0=$SECONDS
	WINEDLLOVERRIDES="mscoree,mshtml=;winemenubuilder.exe=d" wine wineboot --init >"$RESULTS/s1-wineboot.log" 2>&1
	wineserver -w
	result S1_WINEBOOT_SECONDS "$((SECONDS - t0))"
	[[ -f "$WINEPREFIX/system.reg" && -f "$WINEPREFIX/user.reg" ]] || die "prefix not created"
	grep -q '^#arch=win64' "$WINEPREFIX/system.reg" && result S1_PREFIX_ARCH win64
	result S1_DESKTOP_IS_SYMLINK "$([[ -L "$WINEPREFIX/drive_c/users/$USER/Desktop" ]] && echo yes || echo no)"
	log "S1 done"
}

# ── S2: .NET Framework + fonts + registry tweaks ────────────────────────────
spike_s2() {
	use_wine
	start_xvfb
	log "S2: winetricks ${WINETRICKS_VERSION}"
	local wt="$ROOT/runtimes/winetricks-${WINETRICKS_VERSION}"
	fetch "$WINETRICKS_URL" "$wt"
	chmod 0755 "$wt"
	result S2_WINETRICKS_SHA256 "$(sha256_of "$wt")"
	result S2_WINETRICKS_SIZE "$(stat -c %s "$wt")"
	local verb="${FL_SPIKE_DOTNET:-dotnet48}" t0=$SECONDS rc=0
	log "S2: winetricks -q $verb (this takes a while)"
	"$wt" -q "$verb" >"$RESULTS/s2-$verb.log" 2>&1 || rc=$?
	wineserver -w || true
	result S2_DOTNET_VERB "$verb"
	result S2_DOTNET_RC "$rc"
	result S2_DOTNET_SECONDS "$((SECONDS - t0))"
	result S2_DOTNET_RELEASE "$(reg_query 'HKLM\Software\Microsoft\NET Framework Setup\NDP\v4\Full' Release)"
	t0=$SECONDS
	rc=0
	"$wt" -q corefonts >"$RESULTS/s2-corefonts.log" 2>&1 || rc=$?
	result S2_COREFONTS_RC "$rc"
	result S2_COREFONTS_SECONDS "$((SECONDS - t0))"
	log "S2: registry tweaks"
	local reg="$ROOT/fl-tweaks.reg" repl="Noto Sans"
	fc-list : family | grep -qx 'Noto Sans' || repl="DejaVu Sans"
	{
		printf 'Windows Registry Editor Version 5.00\r\n\r\n'
		printf '[HKEY_CURRENT_USER\\Software\\Microsoft\\Avalon.Graphics]\r\n"DisableHWAcceleration"=dword:00000001\r\n\r\n'
		printf '[HKEY_CURRENT_USER\\Software\\Wine\\AppDefaults\\Fork.exe]\r\n"Version"="win7"\r\n\r\n'
		printf '[HKEY_CURRENT_USER\\Software\\Wine\\DllOverrides]\r\n"winemenubuilder.exe"=""\r\n\r\n'
		printf '[HKEY_CURRENT_USER\\Software\\Wine\\WineDbg]\r\n"ShowCrashDialog"=dword:00000000\r\n\r\n'
		printf '[HKEY_CURRENT_USER\\Software\\Wine\\Fonts\\Replacements]\r\n'
		printf '"Segoe UI"="%s"\r\n"Segoe UI Semibold"="%s"\r\n"Segoe UI Light"="%s"\r\n\r\n' "$repl" "$repl" "$repl"
		printf '[HKEY_CURRENT_USER\\Control Panel\\Desktop]\r\n"FontSmoothing"="2"\r\n"FontSmoothingType"=dword:00000002\r\n\r\n'
	} | iconv -f UTF-8 -t UTF-16LE | { printf '\xff\xfe'; cat; } >"$reg"
	wine regedit /S "$(winepath -w "$reg")" >"$RESULTS/s2-regedit.log" 2>&1
	wineserver -w
	result S2_AVALON "$(reg_query 'HKCU\Software\Microsoft\Avalon.Graphics' DisableHWAcceleration)"
	result S2_FONT_REPLACEMENT "$repl"
	log "S2 done"
}

# ── S3: official Fork installer ─────────────────────────────────────────────
spike_s3() {
	use_wine
	start_xvfb
	log "S3: Fork ${FORK_VERSION} installer"
	local exe="$DL/Fork-${FORK_VERSION}.exe" feed="$DL/releases.win.json"
	fetch "$FORK_URL" "$exe"
	result S3_INSTALLER_SHA256 "$(sha256_of "$exe")"
	result S3_INSTALLER_SIZE "$(stat -c %s "$exe")"
	head -c2 "$exe" | grep -q 'MZ' && result S3_INSTALLER_MZ yes
	rm -f "$feed"
	fetch "$FORK_FEED_URL" "$feed"
	local t0=$SECONDS rc=0
	local localapp="$WINEPREFIX/drive_c/users/$USER/AppData/Local"
	timeout 900 wine "$(winepath -w "$exe")" --silent >"$RESULTS/s3-install.log" 2>&1 || rc=$?
	result S3_INSTALL_RC "$rc"
	result S3_INSTALL_SECONDS "$((SECONDS - t0))"
	sleep 5
	result S3_FORK_AUTOLAUNCHED "$(pgrep -fa 'Fork.exe' >/dev/null && echo yes || echo no)"
	find "$localapp/Fork" -maxdepth 2 >"$RESULTS/s3-layout.txt" 2>/dev/null || true
	result S3_CURRENT_EXE "$([[ -f "$localapp/Fork/current/Fork.exe" ]] && echo yes || echo no)"
	[[ -f "$localapp/Fork/current/sq.version" ]] && cp "$localapp/Fork/current/sq.version" "$RESULTS/s3-sq.version"
	ls -la "$localapp/Fork/packages" >"$RESULTS/s3-packages.txt" 2>&1 || true
	local nupkg
	nupkg="$(find "$localapp/Fork/packages" -name "*-full.nupkg" 2>/dev/null | head -n1 || true)"
	if [[ -n "$nupkg" ]]; then
		local nup_sha feed_sha
		nup_sha="$(sha256_of "$nupkg" | tr '[:lower:]' '[:upper:]')"
		feed_sha="$(python3 -I -c 'import json,sys
d=json.load(open(sys.argv[1],encoding="utf-8-sig"))
name=sys.argv[2]
print(next((a["SHA256"] for a in d["Assets"] if a["FileName"]==name),""))' "$feed" "$(basename "$nupkg")")"
		result S3_NUPKG "$(basename "$nupkg")"
		result S3_NUPKG_SHA_MATCHES_FEED "$([[ -n "$feed_sha" && "$nup_sha" == "${feed_sha^^}" ]] && echo yes || echo "no ($nup_sha vs $feed_sha)")"
	else
		result S3_NUPKG none
	fi
	find "$WINEPREFIX/drive_c" -iname '*.lnk' 2>/dev/null >"$RESULTS/s3-lnk.txt" || true
	find "$HOME" -maxdepth 4 -iname '*fork*' 2>/dev/null >"$RESULTS/s3-home-fork-files.txt" || true
	log "S3: stopping wineserver (auto-launched Fork, if any)"
	wineserver -k || true
	sleep 2
	log "S3 done"
}

# ── S4: render Fork under Xvfb and screenshot ───────────────────────────────
spike_s4() {
	use_wine
	start_xvfb
	log "S4: launching Fork under Xvfb $DISPLAY"
	local current="$WINEPREFIX/drive_c/users/$USER/AppData/Local/Fork/current"
	[[ -f "$current/Fork.exe" ]] || die "Fork not installed (run s3)"
	local repo="$ROOT/repos/demo"
	if [[ ! -d "$repo/.git" ]]; then
		mkdir -p "$repo"
		git -C "$repo" init -q
		git -C "$repo" -c user.name=spike -c user.email=spike@example.invalid commit -q --allow-empty -m "spike: hello from Linux"
	fi
	(cd "$current" && WINEDEBUG="${FL_SPIKE_FORK_WINEDEBUG:-err+all,fixme-all}" \
		wine "C:\\users\\${USER}\\AppData\\Local\\Fork\\current\\Fork.exe" "$(winepath -w "$repo")" \
		>"$RESULTS/s4-fork-stdout.log" 2>"$RESULTS/s4-fork-wine.log") &
	local pid=$! wid="" _
	for _ in $(seq 1 120); do
		wid="$(xdotool search --onlyvisible --name 'Fork|demo' 2>/dev/null | head -n1 || true)"
		[[ -n "$wid" ]] && break
		kill -0 "$pid" 2>/dev/null || break
		sleep 2
	done
	result S4_WINDOW_FOUND "$([[ -n "$wid" ]] && echo yes || echo no)"
	if [[ -n "$wid" ]]; then
		result S4_WINDOW_NAME "$(xdotool getwindowname "$wid" 2>/dev/null || true)"
		xprop -id "$wid" WM_CLASS 2>/dev/null | tee "$RESULTS/s4-wmclass.txt" >/dev/null || true
		sleep 15
	fi
	import -display "$DISPLAY" -window root "$RESULTS/s4-screen.png" 2>/dev/null || true
	if [[ -f "$RESULTS/s4-screen.png" ]]; then
		result S4_SCREEN_STDDEV "$(magick "$RESULTS/s4-screen.png" -format '%[fx:standard_deviation]' info: 2>/dev/null || true)"
	fi
	result S4_FORK_STILL_RUNNING "$(kill -0 "$pid" 2>/dev/null && echo yes || echo no)"
	grep -E 'Exception|err:' "$RESULTS/s4-fork-wine.log" | head -n 40 >"$RESULTS/s4-errors.txt" || true
	local forklog="$WINEPREFIX/drive_c/users/$USER/AppData/Local/Fork/logs/fork.log"
	[[ -f "$forklog" ]] && tail -n 80 "$forklog" >"$RESULTS/s4-fork.log.tail"
	[[ -n "${FL_SPIKE_KEEP_RUNNING:-}" ]] || wineserver -k || true
	log "S4 done"
}

main() {
	local s
	[[ $# -gt 0 ]] || set -- all
	for s in "$@"; do
		case "$s" in
		s1) spike_s1 ;;
		s2) spike_s2 ;;
		s3) spike_s3 ;;
		s4) spike_s4 ;;
		all) spike_s1; spike_s2; spike_s3; spike_s4 ;;
		*) die "unknown spike: $s" ;;
		esac
	done
	log "results in $RESULTS"
}

main "$@"
