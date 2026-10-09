#!/usr/bin/env bash
# snap-entrypoint.sh - inside docker/Dockerfile.snap (systemd PID 1): wait for snapd, install
# the pinned snapcraft revision (packaging/snap/SNAPCRAFT_REVISION), refresh apt (snapcraft
# --destructive-mode resolves build-packages with apt), then run the given command.
set -euo pipefail

for _ in $(seq 1 90); do
	if snap version >/dev/null 2>&1 && snap wait system seed.loaded >/dev/null 2>&1; then
		break
	fi
	sleep 1
done

if ! snap list snapcraft >/dev/null 2>&1; then
	revision="$(cat /etc/fork-linux/SNAPCRAFT_REVISION)"
	snap install snapcraft --classic --revision="${revision}"
fi
apt-get update -qq

export PATH="/snap/bin:${PATH}"
exec "$@"
