/*
 * test_shquote.c - POSIX sh single-quoting.
 */
#include <string.h>

#include "fl_shquote.h"
#include "tap.h"

static void check(const char *in, const char *want)
{
    char out[256];
    int n = fl_shquote(in, out, sizeof(out));
    tap_ok(n == (int)strlen(want), "shquote length of [%s]", in);
    tap_str_eq(out, want, "shquote [%s]", in);
}

void test_shquote(void)
{
    char small[8];
    check("", "''");
    check("abc", "'abc'");
    check("a b\tc", "'a b\tc'");
    check("it's", "'it'\\''s'");
    check("'", "''\\'''");
    check("''", "''\\'''\\'''");
    check("C:\\Program Files\\Fork\\Fork.RI.exe", "'C:\\Program Files\\Fork\\Fork.RI.exe'");
    check("$HOME;`id` \"x\" \\n", "'$HOME;`id` \"x\" \\n'");
    check("J\xc3\xbcrgen \xe2\x82\xac", "'J\xc3\xbcrgen \xe2\x82\xac'");
    check("-n", "'-n'");
    /* exact fit: "abcde" needs 5 + 2 quotes + NUL = 8 bytes */
    tap_ok(fl_shquote("abcde", small, sizeof(small)) == 7 && strcmp(small, "'abcde'") == 0,
           "shquote fits exactly (outsz = need)");
    memset(small, 'x', sizeof(small));
    tap_ok(fl_shquote("abcdef", small, sizeof(small)) == -1, "shquote: one byte short fails");
    tap_ok(small[0] == '\0', "shquote: out is empty after a failure");
    memset(small, 'x', sizeof(small));
    tap_ok(fl_shquote("a'b", small, 7) == -1, "shquote: escaped quote counted (needs 9)");
    tap_ok(fl_shquote(NULL, small, sizeof(small)) == -1, "shquote: NULL input");
    tap_ok(fl_shquote("a", NULL, 0) == -1, "shquote: NULL output");
    tap_ok(fl_shquote("", small, 2) == -1, "shquote: empty needs 3 bytes");
    tap_ok(fl_shquote("", small, 3) == 2 && strcmp(small, "''") == 0, "shquote: empty fits in 3 bytes");
}
