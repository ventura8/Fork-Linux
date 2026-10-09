/*
 * tap.h - tiny TAP (Test Anything Protocol) harness for the bridge's native unit tests.
 * Every check prints "ok N - name" or "not ok N - name" (plus "# " diagnostics); test_main
 * prints the "1..N" plan at the end and exits non-zero when anything failed.
 */
#ifndef FL_TAP_H
#define FL_TAP_H

#include <stddef.h>

#if defined(__GNUC__)
#define TAP_PRINTF(fmt, args) __attribute__((format(printf, fmt, args)))
#else
#define TAP_PRINTF(fmt, args)
#endif

/* Record one check; returns cond. */
int tap_ok(int cond, const char *fmt, ...) TAP_PRINTF(2, 3);
/* A "# ..." diagnostic line. */
void tap_diag(const char *fmt, ...) TAP_PRINTF(1, 2);
/* Strings / byte buffers equal (NULL only equals NULL); prints both escaped on mismatch. */
int tap_str_eq(const char *got, const char *want, const char *fmt, ...) TAP_PRINTF(3, 4);
int tap_mem_eq(const void *got, size_t gn, const void *want, size_t wn, const char *fmt, ...) TAP_PRINTF(5, 6);
/* Directory holding argv.tsv, env.tsv and out.tsv (argv[1] of the test binary). */
const char *tap_vectors_dir(void);

/* Test groups (one per source file). */
void test_proto(void);
void test_sha256(void);
void test_shquote(void);
void test_translate(void);

#endif /* FL_TAP_H */
