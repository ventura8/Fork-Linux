#!/bin/sh
# find-python.sh - print the host Python 3 interpreter (>= 3.10) Fork for Linux (unofficial) runs on.
#
# Used by the portable channels that do not bundle Python (AppImage AppRun, snap wrapper,
# install.sh): $FORK_LINUX_PYTHON first, then python3 and versioned python3.N names on
# PATH, then /usr/bin/python3. Prints the absolute path and exits 0, or prints a hint on
# stderr and exits 1. POSIX sh; can be sourced (defines fl_find_python) or executed.

fl_python_ok() {
	"$1" -I -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1
}

fl_find_python() {
	if [ -n "${FORK_LINUX_PYTHON:-}" ]; then
		if fl_python_ok "${FORK_LINUX_PYTHON}"; then
			printf '%s\n' "${FORK_LINUX_PYTHON}"
			return 0
		fi
		echo "fork-linux: FORK_LINUX_PYTHON=${FORK_LINUX_PYTHON} is not Python >= 3.10" >&2
		return 1
	fi
	for name in python3 python3.14 python3.13 python3.12 python3.11 python3.10; do
		candidate=$(command -v "$name" 2>/dev/null) || continue
		if fl_python_ok "$candidate"; then
			printf '%s\n' "$candidate"
			return 0
		fi
	done
	if [ -x /usr/bin/python3 ] && fl_python_ok /usr/bin/python3; then
		printf '%s\n' /usr/bin/python3
		return 0
	fi
	echo "fork-linux: Python 3.10 or newer is required; install python3 with your package manager" >&2
	return 1
}

case "${0##*/}" in
	find-python.sh) fl_find_python ;;
esac
