/*
 * test_main.c - TAP runner for the bridge/common unit tests.
 * Usage: fl-unit-tests <vectors-dir>
 */
#include <stdarg.h>
#include <stdio.h>
#include <string.h>

#include "tap.h"

static unsigned g_count;
static unsigned g_failed;
static const char *g_vectors = "bridge/tests/vectors";

static void vreport(int cond, const char *fmt, va_list ap)
{
    g_count++;
    if (!cond) {
        g_failed++;
    }
    printf("%s %u - ", cond ? "ok" : "not ok", g_count);
    vprintf(fmt, ap);
    putchar('\n');
}

int tap_ok(int cond, const char *fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    vreport(cond, fmt, ap);
    va_end(ap);
    return cond;
}

void tap_diag(const char *fmt, ...)
{
    va_list ap;
    fputs("# ", stdout);
    va_start(ap, fmt);
    vprintf(fmt, ap);
    va_end(ap);
    putchar('\n');
}

/* Print buf[0..n) with C escapes for anything that is not printable ASCII. */
static void put_escaped(const char *label, const void *buf, size_t n)
{
    const unsigned char *p = buf;
    printf("#   %s: ", label);
    if (p == NULL) {
        fputs("(null)\n", stdout);
        return;
    }
    putchar('"');
    for (size_t i = 0; i < n; i++) {
        unsigned c = p[i];
        if (c == '\\' || c == '"') {
            printf("\\%c", (char)c);
        } else if (c == '\n') {
            fputs("\\n", stdout);
        } else if (c == '\t') {
            fputs("\\t", stdout);
        } else if (c == '\r') {
            fputs("\\r", stdout);
        } else if (c < 0x20u || c >= 0x7fu) {
            printf("\\x%02x", c);
        } else {
            putchar((int)c);
        }
    }
    fputs("\"\n", stdout);
}

int tap_mem_eq(const void *got, size_t gn, const void *want, size_t wn, const char *fmt, ...)
{
    va_list ap;
    int cond;
    if (got == NULL || want == NULL) {
        cond = got == want;
    } else {
        cond = gn == wn && (gn == 0 || memcmp(got, want, gn) == 0);
    }
    va_start(ap, fmt);
    vreport(cond, fmt, ap);
    va_end(ap);
    if (!cond) {
        put_escaped("got ", got, gn);
        put_escaped("want", want, wn);
    }
    return cond;
}

int tap_str_eq(const char *got, const char *want, const char *fmt, ...)
{
    va_list ap;
    int cond = (got == NULL || want == NULL) ? got == want : strcmp(got, want) == 0;
    va_start(ap, fmt);
    vreport(cond, fmt, ap);
    va_end(ap);
    if (!cond) {
        put_escaped("got ", got, got != NULL ? strlen(got) : 0);
        put_escaped("want", want, want != NULL ? strlen(want) : 0);
    }
    return cond;
}

const char *tap_vectors_dir(void)
{
    return g_vectors;
}

int main(int argc, char **argv)
{
    if (argc > 1) {
        g_vectors = argv[1];
    }
    setvbuf(stdout, NULL, _IOLBF, 0);
    test_sha256();
    test_shquote();
    test_proto();
    test_translate();
    test_translate_cov();
    printf("1..%u\n", g_count);
    if (g_failed > 0) {
        tap_diag("%u of %u checks failed", g_failed, g_count);
        return 1;
    }
    return 0;
}
