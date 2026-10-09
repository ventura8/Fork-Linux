/*
 * test_proto.c - frame headers, REQ TLV coding (round trips, validation, truncation and a
 * deterministic mutation pass under ASan/UBSan) and EXIT payloads.
 */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "fl_proto.h"
#include "tap.h"

static void test_hdr(void)
{
    static const struct {
        uint8_t type;
        uint32_t min;
        uint32_t max;
        const char *name;
    } t[] = {
        { FL_F_HELLO, 38, 38, "HELLO" },
        { FL_F_CHALLENGE, 64, 64, "CHALLENGE" },
        { FL_F_AUTH, 32, 32, "AUTH" },
        { FL_F_AUTH_OK, 0, 0, "AUTH_OK" },
        { FL_F_REQ, 0, FL_MAX_REQ, "REQ" },
        { FL_F_STDIN, 0, FL_MAX_CHUNK, "STDIN" },
        { FL_F_STDIN_EOF, 0, 0, "STDIN_EOF" },
        { FL_F_SIGNAL, 1, 1, "SIGNAL" },
        { FL_F_SPAWN_OK, 4, FL_MAX_REQ, "SPAWN_OK" },
        { FL_F_SPAWN_ERR, 4, FL_MAX_CHUNK, "SPAWN_ERR" },
        { FL_F_STDOUT, 0, FL_MAX_CHUNK, "STDOUT" },
        { FL_F_STDERR, 0, FL_MAX_CHUNK, "STDERR" },
        { FL_F_EXIT, 5, 5, "EXIT" },
    };
    uint8_t h[5];
    uint8_t type;
    uint32_t len;
    for (size_t i = 0; i < sizeof(t) / sizeof(t[0]); i++) {
        tap_ok(fl_hdr_encode(h, t[i].type, t[i].min) == 0 && fl_hdr_decode(h, &type, &len) == 0 &&
                   type == t[i].type && len == t[i].min,
               "hdr %s at its minimum length", t[i].name);
        tap_ok(fl_hdr_encode(h, t[i].type, t[i].max) == 0 && fl_hdr_decode(h, &type, &len) == 0 && len == t[i].max,
               "hdr %s at its maximum length", t[i].name);
        tap_ok(fl_hdr_encode(h, t[i].type, t[i].max + 1u) == -1, "hdr %s: one byte over the limit is refused",
               t[i].name);
        if (t[i].min > 0) {
            tap_ok(fl_hdr_encode(h, t[i].type, t[i].min - 1u) == -1, "hdr %s: one byte under the minimum is refused",
                   t[i].name);
        }
        h[0] = t[i].type;
        h[1] = 0xff;
        h[2] = 0xff;
        h[3] = 0xff;
        h[4] = 0xff;
        tap_ok(fl_hdr_decode(h, &type, &len) == -1 && type == t[i].type && len == 0xffffffffu,
               "hdr %s: decode refuses a 4 GiB length but reports it", t[i].name);
    }
    tap_ok(fl_hdr_encode(h, FL_F_STDOUT, 0x00010000u) == 0 && h[0] == 11 && h[1] == 0 && h[2] == 1 && h[3] == 0 &&
               h[4] == 0,
           "hdr length is big-endian");
    h[0] = 9;
    h[1] = 0x00;
    h[2] = 0x01;
    h[3] = 0x02;
    h[4] = 0x03;
    tap_ok(fl_hdr_decode(h, &type, &len) == 0 && type == FL_F_SPAWN_OK && len == 0x00010203u,
           "hdr decode reads big-endian");
    tap_ok(fl_hdr_encode(h, 0, 0) == -1, "hdr type 0 is invalid");
    tap_ok(fl_hdr_encode(h, 14, 0) == -1, "hdr type 14 is invalid");
    tap_ok(fl_hdr_encode(h, 255, 0) == -1, "hdr type 255 is invalid");
    memset(h, 0, sizeof(h));
    tap_ok(fl_hdr_decode(h, &type, &len) == -1, "hdr decode refuses type 0");
    h[0] = 14;
    tap_ok(fl_hdr_decode(h, &type, &len) == -1, "hdr decode refuses type 14");
    tap_ok(fl_hdr_encode(NULL, FL_F_STDOUT, 1) == -1, "hdr encode NULL out");
    tap_ok(fl_hdr_decode(NULL, &type, &len) == -1 && fl_hdr_decode(h, NULL, &len) == -1 &&
               fl_hdr_decode(h, &type, NULL) == -1,
           "hdr decode NULL arguments");
}

static int strv_eq(char **a, size_t na, char **b, size_t nb)
{
    if (na != nb) {
        return 0;
    }
    for (size_t i = 0; i < na; i++) {
        if (strcmp(a[i], b[i]) != 0) {
            return 0;
        }
    }
    return 1;
}

static void test_req_roundtrip(void)
{
    char *argv[] = { "git", "commit", "-m", "", "msg with spaces", "q\"x", "back\\slash", "J\xc3\xbcrgen \xe2\x82\xac" };
    char *set[] = { "GIT_DIR=/home/u/r/.git", "EMPTY=", "K=V=W" };
    char *unset[] = { "MSYSTEM", "HOME" };
    char *anchors[] = { "/home/u/r", "", "rel/dir" };
    struct fl_req r;
    struct fl_req d;
    uint8_t *buf = NULL;
    size_t len = 0;
    memset(&r, 0, sizeof(r));
    r.cwd = "/home/u/my repo";
    r.argc = sizeof(argv) / sizeof(argv[0]);
    r.argv = argv;
    r.nset = 3;
    r.set = set;
    r.nunset = 2;
    r.unset = unset;
    r.flags = FL_REQ_DETACH | FL_REQ_HOST_HELPER;
    r.nanchors = 3;
    r.anchors = anchors;
    if (!tap_ok(fl_req_encode(&r, &buf, &len) == 0 && buf != NULL && len > 0, "req encode full request")) {
        return;
    }
    tap_ok(buf[0] == FL_T_CWD && buf[1] == 0 && buf[2] == 0 && buf[3] == 0 && buf[4] == 15 &&
               memcmp(buf + 5, "/home/u/my repo", 15) == 0,
           "req: first record is CWD with a big-endian length");
    tap_ok(fl_req_decode(buf, len, &d) == 0, "req decode full request");
    tap_str_eq(d.cwd, r.cwd, "req round trip: cwd");
    tap_ok(strv_eq(d.argv, d.argc, argv, r.argc), "req round trip: argv (empty, quotes, UTF-8)");
    tap_ok(d.argv[d.argc] == NULL, "req: argv is NULL-terminated");
    tap_ok(strv_eq(d.set, d.nset, set, 3) && d.set[3] == NULL, "req round trip: env set");
    tap_ok(strv_eq(d.unset, d.nunset, unset, 2) && d.unset[2] == NULL, "req round trip: env unset");
    tap_ok(strv_eq(d.anchors, d.nanchors, anchors, 3) && d.anchors[3] == NULL,
           "req round trip: anchors (empty and relative kept)");
    tap_ok(d.flags == (FL_REQ_DETACH | FL_REQ_HOST_HELPER), "req round trip: flags");
    fl_req_free(&d);
    tap_ok(d.cwd == NULL && d.argv == NULL && d.argc == 0, "req free zeroes the struct");
    fl_req_free(&d);
    tap_ok(1, "req free twice is harmless");
    free(buf);

    /* minimal request: empty lists still come back as NULL-terminated arrays */
    memset(&r, 0, sizeof(r));
    r.cwd = "/";
    r.argc = 1;
    r.argv = argv;
    tap_ok(fl_req_encode(&r, &buf, &len) == 0 && fl_req_decode(buf, len, &d) == 0, "req minimal round trip");
    tap_ok(d.argc == 1 && strcmp(d.argv[0], "git") == 0 && d.nset == 0 && d.set != NULL && d.set[0] == NULL &&
               d.nunset == 0 && d.unset != NULL && d.unset[0] == NULL && d.nanchors == 0 && d.anchors != NULL &&
               d.anchors[0] == NULL && d.flags == 0,
           "req minimal: empty lists are NULL-terminated arrays, flags 0");
    fl_req_free(&d);
    free(buf);
}

static int encode_fails(struct fl_req *r)
{
    uint8_t *buf = (uint8_t *)1;
    size_t len = 7;
    int rc = fl_req_encode(r, &buf, &len);
    return rc == -1 && buf == NULL && len == 0;
}

static void test_req_encode_validation(void)
{
    char *argv[] = { "git", "status" };
    char *empty0[] = { "" };
    char *bad_set1[] = { "NOEQUALS" };
    char *bad_set2[] = { "=value" };
    char *bad_unset1[] = { "K=V" };
    char *bad_unset2[] = { "" };
    char *nulls[] = { NULL };
    struct fl_req r;
    char *big;
    memset(&r, 0, sizeof(r));
    r.cwd = "/x";
    r.argc = 2;
    r.argv = argv;
    tap_ok(fl_req_encode(&r, NULL, NULL) == -1, "req encode NULL out");
    tap_ok(encode_fails(NULL), "req encode NULL request");
    r.cwd = NULL;
    tap_ok(encode_fails(&r), "req encode without cwd");
    r.cwd = "relative";
    tap_ok(encode_fails(&r), "req encode with a relative cwd");
    r.cwd = "";
    tap_ok(encode_fails(&r), "req encode with an empty cwd");
    r.cwd = "/x";
    r.argc = 0;
    tap_ok(encode_fails(&r), "req encode with argc 0");
    r.argc = 1;
    r.argv = empty0;
    tap_ok(encode_fails(&r), "req encode with an empty argv[0]");
    r.argv = nulls;
    tap_ok(encode_fails(&r), "req encode with a NULL argv[0]");
    r.argv = argv;
    r.nset = 1;
    r.set = bad_set1;
    tap_ok(encode_fails(&r), "req encode: env set without '='");
    r.set = bad_set2;
    tap_ok(encode_fails(&r), "req encode: env set with an empty name");
    r.set = NULL;
    tap_ok(encode_fails(&r), "req encode: nset > 0 with a NULL array");
    r.nset = 0;
    r.nunset = 1;
    r.unset = bad_unset1;
    tap_ok(encode_fails(&r), "req encode: env unset with '='");
    r.unset = bad_unset2;
    tap_ok(encode_fails(&r), "req encode: empty env unset");
    r.nunset = 0;
    r.nanchors = 1;
    r.anchors = nulls;
    tap_ok(encode_fails(&r), "req encode: NULL anchor");
    r.nanchors = 0;
    r.flags = 0x4u;
    tap_ok(encode_fails(&r), "req encode: unknown flag bit");
    r.flags = 0;
    big = malloc(FL_MAX_REQ);
    if (tap_ok(big != NULL, "allocate a 1 MiB argument")) {
        char *bigv[] = { "git", big };
        memset(big, 'x', FL_MAX_REQ - 1u);
        big[FL_MAX_REQ - 1u] = '\0';
        r.argc = 2;
        r.argv = bigv;
        tap_ok(encode_fails(&r), "req encode: payload over FL_MAX_REQ is refused");
        {
            /* the largest argument that still fits: 1 MiB - CWD(5+2) - FLAGS(5+4) - ARG git(5+3) - ARG hdr(5) */
            uint8_t *buf = NULL;
            size_t len = 0;
            size_t fit = FL_MAX_REQ - 7u - 9u - 8u - 5u;
            struct fl_req d;
            big[fit] = '\0';
            tap_ok(fl_req_encode(&r, &buf, &len) == 0 && len == FL_MAX_REQ, "req encode: exactly FL_MAX_REQ fits");
            tap_ok(buf != NULL && fl_req_decode(buf, len, &d) == 0 && strlen(d.argv[1]) == fit,
                   "req decode: exactly FL_MAX_REQ");
            fl_req_free(&d);
            free(buf);
        }
        free(big);
    }
}

/* Append one TLV record to a test buffer. */
static size_t tlv(uint8_t *b, size_t off, uint8_t tag, const void *d, size_t n)
{
    b[off] = tag;
    b[off + 1] = (uint8_t)(n >> 24);
    b[off + 2] = (uint8_t)(n >> 16);
    b[off + 3] = (uint8_t)(n >> 8);
    b[off + 4] = (uint8_t)n;
    if (n > 0) {
        memcpy(b + off + 5, d, n);
    }
    return off + 5 + n;
}

static int decode_fails(const uint8_t *b, size_t n)
{
    struct fl_req d;
    int rc = fl_req_decode(b, n, &d);
    int zero = d.cwd == NULL && d.argv == NULL && d.set == NULL && d.unset == NULL && d.anchors == NULL &&
               d.argc == 0 && d.flags == 0;
    if (rc == 0) {
        fl_req_free(&d);
    }
    return rc == -1 && zero;
}

static void test_req_decode_validation(void)
{
    uint8_t b[256];
    size_t n;
    static const uint8_t flags_ok[4] = { 0, 0, 0, 1 };
    static const uint8_t flags_bad[4] = { 0, 0, 0, 8 };
    struct fl_req d;

    n = tlv(b, 0, FL_T_CWD, "/w", 2);
    n = tlv(b, n, FL_T_ARG, "git", 3);
    tap_ok(fl_req_decode(b, n, &d) == 0, "req decode: CWD + ARG is enough");
    fl_req_free(&d);
    tap_ok(decode_fails(b, 0), "req decode: empty payload");
    tap_ok(decode_fails(NULL, 0), "req decode: NULL buffer");
    tap_ok(fl_req_decode(b, n, NULL) == -1, "req decode: NULL request");
    tap_ok(decode_fails(b, n + 2), "req decode: trailing partial header");
    tap_ok(decode_fails(b, FL_MAX_REQ + 1u), "req decode: over FL_MAX_REQ");

    n = tlv(b, 0, FL_T_ARG, "git", 3);
    tap_ok(decode_fails(b, n), "req decode: no CWD");
    n = tlv(b, 0, FL_T_CWD, "/w", 2);
    tap_ok(decode_fails(b, n), "req decode: no ARG");
    n = tlv(b, n, FL_T_CWD, "/v", 2);
    n = tlv(b, n, FL_T_ARG, "git", 3);
    tap_ok(decode_fails(b, n), "req decode: two CWD records");
    n = tlv(b, 0, FL_T_CWD, "w", 1);
    n = tlv(b, n, FL_T_ARG, "git", 3);
    tap_ok(decode_fails(b, n), "req decode: relative CWD");
    n = tlv(b, 0, FL_T_CWD, "", 0);
    n = tlv(b, n, FL_T_ARG, "git", 3);
    tap_ok(decode_fails(b, n), "req decode: empty CWD");
    n = tlv(b, 0, FL_T_CWD, "/w", 2);
    n = tlv(b, n, FL_T_ARG, "", 0);
    tap_ok(decode_fails(b, n), "req decode: empty argv[0]");
    n = tlv(b, 0, FL_T_CWD, "/w", 2);
    n = tlv(b, n, FL_T_ARG, "git", 3);
    n = tlv(b, n, FL_T_ARG, "", 0);
    tap_ok(fl_req_decode(b, n, &d) == 0 && d.argc == 2 && d.argv[1][0] == '\0', "req decode: empty later arg is fine");
    fl_req_free(&d);
    n = tlv(b, 0, FL_T_CWD, "/w", 2);
    n = tlv(b, n, FL_T_ARG, "g\0t", 3);
    tap_ok(decode_fails(b, n), "req decode: NUL inside a string");
    n = tlv(b, 0, FL_T_CWD, "/w\0", 3);
    n = tlv(b, n, FL_T_ARG, "git", 3);
    tap_ok(decode_fails(b, n), "req decode: NUL inside CWD");
    n = tlv(b, 0, FL_T_CWD, "/w", 2);
    n = tlv(b, n, FL_T_ARG, "git", 3);
    n = tlv(b, n, 7, "x", 1);
    tap_ok(decode_fails(b, n), "req decode: unknown tag 7");
    n = tlv(b, 0, FL_T_CWD, "/w", 2);
    n = tlv(b, n, FL_T_ARG, "git", 3);
    n = tlv(b, n, 0, "x", 1);
    tap_ok(decode_fails(b, n), "req decode: tag 0");
    n = tlv(b, 0, FL_T_CWD, "/w", 2);
    n = tlv(b, n, FL_T_ARG, "git", 3);
    n = tlv(b, n, FL_T_FLAGS, flags_ok, 3);
    tap_ok(decode_fails(b, n), "req decode: 3-byte FLAGS");
    n = tlv(b, 0, FL_T_CWD, "/w", 2);
    n = tlv(b, n, FL_T_ARG, "git", 3);
    n = tlv(b, n, FL_T_FLAGS, flags_ok, 4);
    n = tlv(b, n, FL_T_FLAGS, flags_ok, 4);
    tap_ok(decode_fails(b, n), "req decode: FLAGS twice");
    n = tlv(b, 0, FL_T_CWD, "/w", 2);
    n = tlv(b, n, FL_T_ARG, "git", 3);
    n = tlv(b, n, FL_T_FLAGS, flags_bad, 4);
    tap_ok(decode_fails(b, n), "req decode: unknown flag bit");
    n = tlv(b, 0, FL_T_CWD, "/w", 2);
    n = tlv(b, n, FL_T_ARG, "git", 3);
    n = tlv(b, n, FL_T_ENV_SET, "NOEQ", 4);
    tap_ok(decode_fails(b, n), "req decode: ENV_SET without '='");
    n = tlv(b, 0, FL_T_CWD, "/w", 2);
    n = tlv(b, n, FL_T_ARG, "git", 3);
    n = tlv(b, n, FL_T_ENV_SET, "=x", 2);
    tap_ok(decode_fails(b, n), "req decode: ENV_SET with an empty name");
    n = tlv(b, 0, FL_T_CWD, "/w", 2);
    n = tlv(b, n, FL_T_ARG, "git", 3);
    n = tlv(b, n, FL_T_ENV_UNSET, "A=B", 3);
    tap_ok(decode_fails(b, n), "req decode: ENV_UNSET with '='");
    n = tlv(b, 0, FL_T_CWD, "/w", 2);
    n = tlv(b, n, FL_T_ARG, "git", 3);
    n = tlv(b, n, FL_T_ENV_UNSET, "", 0);
    tap_ok(decode_fails(b, n), "req decode: empty ENV_UNSET");
    /* a length that runs past the end of the payload */
    n = tlv(b, 0, FL_T_CWD, "/w", 2);
    n = tlv(b, n, FL_T_ARG, "git", 3);
    b[n - 4] = 9; /* ARG claims 9 bytes, 3 present */
    tap_ok(decode_fails(b, n), "req decode: record length past the end");
    n = tlv(b, 0, FL_T_CWD, "/w", 2);
    b[1] = 0xff; /* CWD claims ~4 GiB */
    tap_ok(decode_fails(b, n), "req decode: 4 GiB record length");
}

/* Every truncation that does not end on a record boundary must fail; boundaries after the
   first ARG decode to a consistent prefix. */
static void test_req_truncation(void)
{
    char *argv[] = { "git", "log", "--format=%H" };
    char *set[] = { "A=1", "B=2" };
    char *anchors[] = { "/a" };
    struct fl_req r;
    uint8_t *buf = NULL;
    size_t len = 0;
    int bad = 0;
    int boundaries_ok = 1;
    memset(&r, 0, sizeof(r));
    r.cwd = "/w";
    r.argc = 3;
    r.argv = argv;
    r.nset = 2;
    r.set = set;
    r.nanchors = 1;
    r.anchors = anchors;
    if (!tap_ok(fl_req_encode(&r, &buf, &len) == 0, "req truncation: encode")) {
        return;
    }
    for (size_t cut = 0; cut < len; cut++) {
        size_t pos = 0;
        int boundary = 0;
        while (pos < cut) {
            uint32_t l = ((uint32_t)buf[pos + 1] << 24) | ((uint32_t)buf[pos + 2] << 16) |
                         ((uint32_t)buf[pos + 3] << 8) | buf[pos + 4];
            pos += 5u + l;
            if (pos == cut) {
                boundary = 1;
            }
        }
        if (!boundary) {
            if (!decode_fails(buf, cut)) {
                bad++;
            }
        } else {
            struct fl_req d;
            if (fl_req_decode(buf, cut, &d) == 0) {
                boundaries_ok &= d.argc >= 1 && strcmp(d.cwd, "/w") == 0;
                fl_req_free(&d);
            }
        }
    }
    tap_ok(bad == 0, "req truncation: every mid-record cut is refused (%d accepted)", bad);
    tap_ok(boundaries_ok, "req truncation: record-boundary cuts decode consistently");
    free(buf);
}

/* Deterministic xorshift mutation of a valid request: decode must never crash or leak. */
static void test_req_mutations(void)
{
    char *argv[] = { "git", "-C", "/home/u/repo", "status", "--porcelain=v2", "-z" };
    char *set[] = { "GIT_DIR=/home/u/repo/.git", "LANG=C.UTF-8" };
    char *unset[] = { "HOME" };
    char *anchors[] = { "/home/u/repo" };
    struct fl_req r;
    uint8_t *buf = NULL;
    uint8_t *m;
    size_t len = 0;
    uint32_t s = 0x9e3779b9u;
    unsigned accepted = 0;
    memset(&r, 0, sizeof(r));
    r.cwd = "/home/u/repo";
    r.argc = 6;
    r.argv = argv;
    r.nset = 2;
    r.set = set;
    r.nunset = 1;
    r.unset = unset;
    r.nanchors = 1;
    r.anchors = anchors;
    if (!tap_ok(fl_req_encode(&r, &buf, &len) == 0, "req mutations: encode")) {
        return;
    }
    m = malloc(len);
    if (m == NULL) {
        free(buf);
        tap_ok(0, "req mutations: allocate");
        return;
    }
    for (unsigned iter = 0; iter < 20000u; iter++) {
        struct fl_req d;
        unsigned flips;
        memcpy(m, buf, len);
        s ^= s << 13;
        s ^= s >> 17;
        s ^= s << 5;
        flips = 1u + (s % 4u);
        for (unsigned f = 0; f < flips; f++) {
            s ^= s << 13;
            s ^= s >> 17;
            s ^= s << 5;
            m[s % len] = (uint8_t)(s >> 8);
        }
        if (fl_req_decode(m, len - (s % 3u == 0 ? (s >> 3) % len : 0u), &d) == 0) {
            accepted++;
            if (d.argc == 0 || d.argv[d.argc] != NULL || d.cwd[0] != '/') {
                tap_ok(0, "req mutations: accepted request violates the invariants");
            }
            fl_req_free(&d);
        }
    }
    tap_ok(1, "req mutations: 20000 mutated payloads decoded without a crash (%u accepted)", accepted);
    free(m);
    free(buf);
}

static void test_exit(void)
{
    uint8_t p[5];
    int kind = -1;
    int32_t code = -1;
    tap_ok(fl_exit_encode(p, 0, 141) == 0 && p[0] == 0 && p[1] == 0 && p[2] == 0 && p[3] == 0 && p[4] == 141,
           "exit encode (0, 141) layout");
    tap_ok(fl_exit_decode(p, 5, &kind, &code) == 0 && kind == 0 && code == 141, "exit decode (0, 141)");
    tap_ok(fl_exit_encode(p, 1, 15) == 0 && p[0] == 1 && p[4] == 15, "exit encode signaled 15");
    tap_ok(fl_exit_decode(p, 5, &kind, &code) == 0 && kind == 1 && code == 15, "exit decode signaled 15");
    tap_ok(fl_exit_encode(p, 0, 0) == 0 && fl_exit_encode(p, 0, 255) == 0, "exit codes 0 and 255");
    tap_ok(fl_exit_encode(p, 1, 1) == 0 && fl_exit_encode(p, 1, 127) == 0, "signals 1 and 127");
    tap_ok(fl_exit_encode(p, 0, -1) == -1, "exit code -1 refused");
    tap_ok(fl_exit_encode(p, 0, 256) == -1, "exit code 256 refused");
    tap_ok(fl_exit_encode(p, 1, 0) == -1, "signal 0 refused");
    tap_ok(fl_exit_encode(p, 1, 128) == -1, "signal 128 refused");
    tap_ok(fl_exit_encode(p, 2, 0) == -1, "kind 2 refused");
    tap_ok(fl_exit_encode(NULL, 0, 0) == -1, "exit encode NULL");
    p[0] = 0;
    p[1] = 0xff;
    p[2] = 0xff;
    p[3] = 0xff;
    p[4] = 0xff;
    tap_ok(fl_exit_decode(p, 5, &kind, &code) == -1, "exit decode code -1 refused");
    p[0] = 2;
    p[1] = 0;
    p[2] = 0;
    p[3] = 0;
    p[4] = 0;
    tap_ok(fl_exit_decode(p, 5, &kind, &code) == -1, "exit decode kind 2 refused");
    p[0] = 0;
    tap_ok(fl_exit_decode(p, 4, &kind, &code) == -1 && fl_exit_decode(p, 6, &kind, &code) == -1,
           "exit decode needs exactly 5 bytes");
    tap_ok(fl_exit_decode(NULL, 5, &kind, &code) == -1 && fl_exit_decode(p, 5, NULL, &code) == -1 &&
               fl_exit_decode(p, 5, &kind, NULL) == -1,
           "exit decode NULL arguments");
}

void test_proto(void)
{
    tap_ok(strcmp(FL_PROTO_MAGIC, "FLB1") == 0 && FL_PROTO_VERSION == 1 && FL_MAX_CHUNK == 65536u &&
               FL_MAX_REQ == 1048576u,
           "protocol constants");
    test_hdr();
    test_req_roundtrip();
    test_req_encode_validation();
    test_req_decode_validation();
    test_req_truncation();
    test_req_mutations();
    test_exit();
}
