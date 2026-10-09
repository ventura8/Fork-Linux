/*
 * test_alloc.c - allocation-failure injection (see test_alloc.h).
 */
#include "test_alloc.h"

static long g_countdown; /* 0: disarmed; else allocations left until the failing one */
static int g_fired;

void fl_alloc_fail_at(long n)
{
    g_countdown = n > 0 ? n : 0;
    g_fired = 0;
}

int fl_alloc_fired(void)
{
    return g_fired;
}

/* 1 when this allocation is the one that must fail. */
static int fail_now(void)
{
    if (g_countdown <= 0) {
        return 0;
    }
    g_countdown--;
    if (g_countdown > 0) {
        return 0;
    }
    g_fired = 1;
    return 1;
}

void *__wrap_malloc(size_t n)
{
    return fail_now() ? NULL : __real_malloc(n);
}

void *__wrap_calloc(size_t n, size_t m)
{
    return fail_now() ? NULL : __real_calloc(n, m);
}

void *__wrap_realloc(void *p, size_t n)
{
    return fail_now() ? NULL : __real_realloc(p, n);
}

long fl_alloc_sweep(void (*fn)(void *ud), void *ud)
{
    long runs = 0;
    for (long k = 1; k < 100000; k++) {
        int fired;
        fl_alloc_fail_at(k);
        fn(ud);
        fired = fl_alloc_fired();
        fl_alloc_fail_at(0);
        if (!fired) {
            break;
        }
        runs++;
    }
    return runs;
}
