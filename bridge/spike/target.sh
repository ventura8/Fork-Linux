#!/bin/sh
# target.sh - B1 spike target (#!/bin/sh script); same report format as target.c.
# usage: target.sh <logfile> <case-id> [expected extra args...]
# Writes key=value lines + END to <logfile>, OUT-<case> to stdout, ERR-<case> to
# stderr, and exits 7.
log=$1
case_id=$2

argv_ok=0
if [ "$#" -eq 9 ] && [ "$3" = 'arg with space' ] && [ "$4" = 'q"uote' ] \
    && [ "$5" = 'back\slash' ] && [ "$6" = 'C:\win\path' ] && [ "$7" = "tail\\" ] \
    && [ "$8" = '' ] && [ "$9" = "\$HOME;\`id\`" ]; then
    argv_ok=1
fi

# key=value line; printf (unlike dash's echo) keeps backslashes in C:\fakehome.
kv() { printf '%s=%s\n' "$1" "$2"; }

# /proc/<pid>/stat: pid (comm) state ppid pgrp session ...
read -r _pid _comm _state ppid pgid sid _rest < "/proc/$$/stat"

# "ls -l /proc/self/fd" equivalent, captured before any redirection below.
fds=$(for p in "/proc/$$/fd/"*; do
    kv "fd.${p##*/}" "$(readlink "$p" 2> /dev/null || echo '<closed>')"
done)

{
    kv variant script
    kv case "$case_id"
    kv argc "$(($# + 1))"
    i=0
    for a in "$0" "$@"; do
        kv "argv[$i]" "$a"
        i=$((i + 1))
    done
    kv argv_ok "$argv_ok"
    kv pid "$$"
    kv ppid "$ppid"
    kv pgid "$pgid"
    kv sid "$sid"
    if [ "$sid" = "$$" ]; then kv session_leader 1; else kv session_leader 0; fi
    if (: < /dev/tty) 2> /dev/null; then kv ctty yes; else kv ctty no; fi
    kv cwd "$(pwd -P)"
    kv env.PATH "${PATH-<unset>}"
    kv env.HOME "${HOME-<unset>}"
    kv env.FL_TOKEN "${FL_TOKEN-<unset>}"
    kv env.WINEPATH "${WINEPATH-<unset>}"
    kv env.WINEHOME "${WINEHOME-<unset>}"
    kv env.TEMP "${TEMP-<unset>}"
    sed -n 's/^\(Seccomp[_a-z]*\|NoNewPrivs\):[[:space:]]*/status.\1=/p' "/proc/$$/status"
    printf '%s\n' "$fds"
    if [ -t 0 ]; then
        kv stdin.read "<tty>"
    else
        data=$(timeout 0.3 head -c 63 2> /dev/null | tr '\r\n' '  ')
        kv stdin.read "${data:-<eof>}"
    fi
} >> "$log" 2>&1

if printf 'OUT-%s\n' "$case_id"; then kv OUT.write ok >> "$log"; else kv OUT.write failed >> "$log"; fi
if printf 'ERR-%s\n' "$case_id" >&2; then kv ERR.write ok >> "$log"; else kv ERR.write failed >> "$log"; fi
echo "END" >> "$log"
exit 7
