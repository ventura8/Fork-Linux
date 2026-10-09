/*
 * fl_shquote.h - POSIX sh single-quoting. Pure C11.
 */
#ifndef FL_SHQUOTE_H
#define FL_SHQUOTE_H

#include <stddef.h>

/*
 * Quote `in` as one POSIX sh word: the result is wrapped in single quotes and every ' inside
 * becomes '\'' (so "it's" -> 'it'\''s', "" -> ''). Writes a NUL-terminated string into
 * out[0..outsz). Returns the number of bytes written (excluding the NUL), or -1 when an
 * argument is NULL or the result does not fit (out is then "" if outsz > 0).
 */
int fl_shquote(const char *in, char *out, size_t outsz);

#endif /* FL_SHQUOTE_H */
