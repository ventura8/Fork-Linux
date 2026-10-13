/*
 * fl_shquote.c - POSIX sh single-quoting. See fl_shquote.h.
 */
#include "fl_shquote.h"

#include <limits.h>

int fl_shquote(const char *in, char *out, size_t outsz)
{
    size_t need = 3; /* two quotes + NUL */
    size_t o = 0;
    if (out != NULL && outsz > 0) {
        out[0] = '\0';
    }
    if (in == NULL || out == NULL) {
        return -1;
    }
    for (const char *p = in; *p != '\0'; p++) {
        size_t add = *p == '\'' ? 4u : 1u;
        if (need > (size_t)INT_MAX - add) {
            return -1;
        }
        need += add;
    }
    if (need > outsz) {
        return -1;
    }
    out[o++] = '\'';
    for (const char *p = in; *p != '\0'; p++) {
        if (*p == '\'') {
            out[o++] = '\'';
            out[o++] = '\\';
            out[o++] = '\'';
            out[o++] = '\'';
        } else {
            out[o++] = *p;
        }
    }
    out[o++] = '\'';
    out[o] = '\0';
    return (int)o;
}
