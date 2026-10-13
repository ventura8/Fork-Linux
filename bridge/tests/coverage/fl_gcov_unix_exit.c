/*
 * fl_gcov_unix_exit.c - coverage builds only: write the gcov counters of
 * fl-bridge-helper before _exit().
 *
 * Linked into fl-bridge-helper only when meson is configured with -Db_coverage=true
 * (scripts/ci-c-coverage.sh), together with -Wl,--wrap=_exit; release builds never
 * contain it. Each session of the daemon runs in a forked child that leaves through
 * _exit(), as do the spawn helpers after a failed exec. _exit() skips the atexit handler
 * libgcov writes its .gcda files from, so without this the sessions would record
 * nothing. The daemon's source stays untouched.
 */
#include <stdnoreturn.h>

void __gcov_dump(void);              /* libgcov (--coverage) */
noreturn void __real__exit(int code); /* the libc _exit (ld --wrap) */
noreturn void __wrap__exit(int code);

noreturn void __wrap__exit(int code)
{
    __gcov_dump();
    __real__exit(code);
}
