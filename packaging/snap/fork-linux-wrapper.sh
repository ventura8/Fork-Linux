#!/bin/sh
# snap-wrapper.sh - start fork-linux (or fork) from the classic snap on the host's Python.
#
# Usage (snapcraft.yaml apps): snap-wrapper.sh fork-linux|fork [ARGS...]
# The tree lives in $SNAP/usr; the program name selects fork-linux or the fork shortcut
# (argv[0] dispatch). Python >= 3.10 comes from the host (find-python.sh).
set -eu

[ -n "${SNAP:-}" ] || {
	echo "snap-wrapper.sh: SNAP is not set (run fork-linux from the snap)" >&2
	exit 2
}
# shellcheck source=packaging/common/find-python.sh
. "${SNAP}/usr/lib/fork-linux/find-python.sh"
PYTHON=$(fl_find_python) || exit 19

name=${1:-fork-linux}
[ $# -gt 0 ] && shift
case "${name}" in
	fork | fork-linux) ;;
	*)
		echo "snap-wrapper.sh: unknown command ${name}" >&2
		exit 2
		;;
esac
exec "${PYTHON}" -I "${SNAP}/usr/bin/${name}" "$@"
