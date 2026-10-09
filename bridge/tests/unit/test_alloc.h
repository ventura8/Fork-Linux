/*
 * test_alloc.h - allocation-failure injection for the native unit tests. The test binaries
 * are linked with -Wl,--wrap=malloc,--wrap=calloc,--wrap=realloc (bridge/tests/meson.build,
 * run.sh), so every allocation made by the code under test goes through test_alloc.c, which
 * can make exactly one chosen allocation fail.
 */
#ifndef FL_TEST_ALLOC_H
#define FL_TEST_ALLOC_H

#include <stddef.h>

/* Make the n-th allocation from now fail (1 = the next one); n <= 0 disarms. */
void fl_alloc_fail_at(long n);
/* 1 when the armed failure has happened since the last fl_alloc_fail_at(). */
int fl_alloc_fired(void);

/*
 * Run fn(ud) once per allocation it makes, failing that allocation (1st, 2nd, ...), until a
 * run completes without reaching the failure; fn must cope with every failure (no crash, no
 * leak). Returns the number of runs in which a failure was injected.
 */
long fl_alloc_sweep(void (*fn)(void *ud), void *ud);

/* The wrapped allocators (defined in test_alloc.c, called through the linker's --wrap). */
void *__wrap_malloc(size_t n);
void *__wrap_calloc(size_t n, size_t m);
void *__wrap_realloc(void *p, size_t n);
void *__real_malloc(size_t n);
void *__real_calloc(size_t n, size_t m);
void *__real_realloc(void *p, size_t n);

#endif /* FL_TEST_ALLOC_H */
