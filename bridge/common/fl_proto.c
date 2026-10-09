/*
 * fl_proto.c - frame headers, REQ (TLV) and EXIT payload coding. See fl_proto.h.
 */
#include "fl_proto.h"

#include <stdlib.h>
#include <string.h>

#define HDR_LEN 5u
#define TLV_HDR 5u

struct type_limit {
    uint32_t min;
    uint32_t max;
};

/* Indexed by frame type; entry 0 is unused (type 0 is invalid). */
static const struct type_limit k_limits[] = {
    { 1u, 0u },                    /* 0: invalid (min > max) */
    { 38u, 38u },                  /* HELLO */
    { 64u, 64u },                  /* CHALLENGE */
    { 32u, 32u },                  /* AUTH */
    { 0u, 0u },                    /* AUTH_OK */
    { 0u, FL_MAX_REQ },            /* REQ */
    { 0u, FL_MAX_CHUNK },          /* STDIN */
    { 0u, 0u },                    /* STDIN_EOF */
    { 1u, 1u },                    /* SIGNAL */
    { 4u, FL_MAX_REQ },            /* SPAWN_OK */
    { 4u, FL_MAX_CHUNK },          /* SPAWN_ERR */
    { 0u, FL_MAX_CHUNK },          /* STDOUT */
    { 0u, FL_MAX_CHUNK },          /* STDERR */
    { 5u, 5u },                    /* EXIT */
};

static int len_ok(uint8_t type, uint32_t len)
{
    if (type == 0u || (size_t)type >= sizeof(k_limits) / sizeof(k_limits[0])) {
        return 0;
    }
    return len >= k_limits[type].min && len <= k_limits[type].max;
}

static void put_u32(uint8_t *p, uint32_t v)
{
    p[0] = (uint8_t)(v >> 24);
    p[1] = (uint8_t)(v >> 16);
    p[2] = (uint8_t)(v >> 8);
    p[3] = (uint8_t)v;
}

static uint32_t get_u32(const uint8_t *p)
{
    return ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16) | ((uint32_t)p[2] << 8) | (uint32_t)p[3];
}

int fl_hdr_encode(uint8_t out[5], uint8_t type, uint32_t len)
{
    if (out == NULL || !len_ok(type, len)) {
        return -1;
    }
    out[0] = type;
    put_u32(out + 1, len);
    return 0;
}

int fl_hdr_decode(const uint8_t in[5], uint8_t *type, uint32_t *len)
{
    uint8_t t;
    uint32_t l;
    if (in == NULL || type == NULL || len == NULL) {
        return -1;
    }
    t = in[0];
    l = get_u32(in + 1);
    *type = t;
    *len = l;
    return len_ok(t, l) ? 0 : -1;
}

/* ------------------------------------------------------------------------ */
/* REQ                                                                       */
/* ------------------------------------------------------------------------ */

static int env_set_ok(const char *s, size_t n)
{
    const char *eq = memchr(s, '=', n);
    return eq != NULL && eq != s;
}

static int env_unset_ok(const char *s, size_t n)
{
    return n > 0 && memchr(s, '=', n) == NULL;
}

static int cwd_ok(const char *s, size_t n)
{
    return n > 0 && s[0] == '/';
}

/* Adds one TLV record's size to *total; -1 on overflow or when over FL_MAX_REQ. */
static int add_size(size_t *total, size_t n)
{
    if (n > FL_MAX_REQ || *total > FL_MAX_REQ || FL_MAX_REQ - *total < TLV_HDR + n) {
        return -1;
    }
    *total += TLV_HDR + n;
    return 0;
}

static int list_size(char *const *v, size_t n, size_t *total, int (*check)(const char *, size_t))
{
    if (n > 0 && v == NULL) {
        return -1;
    }
    for (size_t i = 0; i < n; i++) {
        size_t l;
        if (v[i] == NULL) {
            return -1;
        }
        l = strlen(v[i]);
        if ((check != NULL && !check(v[i], l)) || add_size(total, l) != 0) {
            return -1;
        }
    }
    return 0;
}

static uint8_t *put_tlv(uint8_t *p, uint8_t tag, const void *data, size_t n)
{
    p[0] = tag;
    put_u32(p + 1, (uint32_t)n);
    if (n > 0) {
        memcpy(p + TLV_HDR, data, n);
    }
    return p + TLV_HDR + n;
}

static uint8_t *put_list(uint8_t *p, uint8_t tag, char *const *v, size_t n)
{
    for (size_t i = 0; i < n; i++) {
        p = put_tlv(p, tag, v[i], strlen(v[i]));
    }
    return p;
}

int fl_req_encode(const struct fl_req *r, uint8_t **out, size_t *outlen)
{
    size_t total = 0;
    uint8_t flags[4];
    uint8_t *buf;
    uint8_t *p;
    if (out != NULL) {
        *out = NULL;
    }
    if (outlen != NULL) {
        *outlen = 0;
    }
    if (r == NULL || out == NULL || outlen == NULL || r->cwd == NULL || r->argc == 0 || r->argv == NULL ||
        r->argv[0] == NULL || r->argv[0][0] == '\0' || (r->flags & ~(FL_REQ_DETACH | FL_REQ_HOST_HELPER)) != 0u) {
        return -1;
    }
    if (!cwd_ok(r->cwd, strlen(r->cwd)) || add_size(&total, strlen(r->cwd)) != 0 || add_size(&total, 4u) != 0 ||
        list_size(r->argv, r->argc, &total, NULL) != 0 || list_size(r->set, r->nset, &total, env_set_ok) != 0 ||
        list_size(r->unset, r->nunset, &total, env_unset_ok) != 0 ||
        list_size(r->anchors, r->nanchors, &total, NULL) != 0) {
        return -1;
    }
    buf = malloc(total);
    if (buf == NULL) {
        return -1;
    }
    put_u32(flags, r->flags);
    p = put_tlv(buf, FL_T_CWD, r->cwd, strlen(r->cwd));
    p = put_tlv(p, FL_T_FLAGS, flags, sizeof(flags));
    p = put_list(p, FL_T_ARG, r->argv, r->argc);
    p = put_list(p, FL_T_ENV_SET, r->set, r->nset);
    p = put_list(p, FL_T_ENV_UNSET, r->unset, r->nunset);
    p = put_list(p, FL_T_ANCHOR, r->anchors, r->nanchors);
    if ((size_t)(p - buf) != total) {
        free(buf);
        return -1;
    }
    *out = buf;
    *outlen = total;
    return 0;
}

struct vec {
    char **v;
    size_t n;
    size_t cap;
};

/* Appends a NUL-terminated copy of s[0..n); keeps the array NULL-terminated. */
static int vec_push(struct vec *v, const uint8_t *s, size_t n)
{
    char *copy;
    if (v->n + 1 >= v->cap) {
        size_t cap = v->cap ? v->cap * 2 : 8;
        char **nv;
        if (cap > ((size_t)-1) / sizeof(char *)) {
            return -1;
        }
        nv = realloc(v->v, cap * sizeof(char *));
        if (nv == NULL) {
            return -1;
        }
        v->v = nv;
        v->cap = cap;
    }
    copy = malloc(n + 1);
    if (copy == NULL) {
        return -1;
    }
    if (n > 0) {
        memcpy(copy, s, n);
    }
    copy[n] = '\0';
    v->v[v->n++] = copy;
    v->v[v->n] = NULL;
    return 0;
}

/* Gives an empty list its NULL-terminated one-slot array. */
static int vec_terminate(struct vec *v)
{
    if (v->v != NULL) {
        return 0;
    }
    v->v = calloc(1, sizeof(char *));
    if (v->v == NULL) {
        return -1;
    }
    v->cap = 1;
    return 0;
}

static void vec_free(char **v, size_t n)
{
    if (v == NULL) {
        return;
    }
    for (size_t i = 0; i < n; i++) {
        free(v[i]);
    }
    free(v);
}

int fl_req_decode(const uint8_t *buf, size_t len, struct fl_req *r)
{
    struct vec argv = { NULL, 0, 0 };
    struct vec set = { NULL, 0, 0 };
    struct vec unset = { NULL, 0, 0 };
    struct vec anchors = { NULL, 0, 0 };
    char *cwd = NULL;
    uint32_t flags = 0;
    int have_flags = 0;
    size_t pos = 0;
    if (r == NULL) {
        return -1;
    }
    memset(r, 0, sizeof(*r));
    if (buf == NULL || len > FL_MAX_REQ) {
        return -1;
    }
    while (pos < len) {
        uint8_t tag;
        uint32_t n32;
        size_t n;
        const uint8_t *d;
        int rc = 0;
        if (len - pos < TLV_HDR) {
            goto fail;
        }
        tag = buf[pos];
        n32 = get_u32(buf + pos + 1);
        pos += TLV_HDR;
        if ((size_t)n32 > len - pos) {
            goto fail;
        }
        n = (size_t)n32;
        d = buf + pos;
        pos += n;
        if (tag != FL_T_FLAGS && n > 0 && memchr(d, '\0', n) != NULL) {
            goto fail;
        }
        switch (tag) {
        case FL_T_CWD:
            if (cwd != NULL || !cwd_ok((const char *)d, n)) {
                goto fail;
            }
            cwd = malloc(n + 1);
            if (cwd == NULL) {
                goto fail;
            }
            memcpy(cwd, d, n);
            cwd[n] = '\0';
            break;
        case FL_T_ARG:
            if (argv.n == 0 && n == 0) {
                goto fail; /* argv[0] must name a program */
            }
            rc = vec_push(&argv, d, n);
            break;
        case FL_T_ENV_SET:
            rc = env_set_ok((const char *)d, n) ? vec_push(&set, d, n) : -1;
            break;
        case FL_T_ENV_UNSET:
            rc = env_unset_ok((const char *)d, n) ? vec_push(&unset, d, n) : -1;
            break;
        case FL_T_FLAGS:
            if (have_flags || n != 4u) {
                goto fail;
            }
            flags = get_u32(d);
            have_flags = 1;
            if ((flags & ~(FL_REQ_DETACH | FL_REQ_HOST_HELPER)) != 0u) {
                goto fail;
            }
            break;
        case FL_T_ANCHOR:
            rc = vec_push(&anchors, d, n);
            break;
        default:
            goto fail;
        }
        if (rc != 0) {
            goto fail;
        }
    }
    if (cwd == NULL || argv.n == 0 || vec_terminate(&set) != 0 || vec_terminate(&unset) != 0 ||
        vec_terminate(&anchors) != 0) {
        goto fail;
    }
    r->cwd = cwd;
    r->argc = argv.n;
    r->argv = argv.v;
    r->nset = set.n;
    r->set = set.v;
    r->nunset = unset.n;
    r->unset = unset.v;
    r->flags = flags;
    r->nanchors = anchors.n;
    r->anchors = anchors.v;
    return 0;
fail:
    free(cwd);
    vec_free(argv.v, argv.n);
    vec_free(set.v, set.n);
    vec_free(unset.v, unset.n);
    vec_free(anchors.v, anchors.n);
    memset(r, 0, sizeof(*r));
    return -1;
}

void fl_req_free(struct fl_req *r)
{
    if (r == NULL) {
        return;
    }
    free(r->cwd);
    vec_free(r->argv, r->argc);
    vec_free(r->set, r->nset);
    vec_free(r->unset, r->nunset);
    vec_free(r->anchors, r->nanchors);
    memset(r, 0, sizeof(*r));
}

/* ------------------------------------------------------------------------ */
/* EXIT                                                                      */
/* ------------------------------------------------------------------------ */

static int exit_ok(int kind, int32_t code)
{
    return (kind == 0 && code >= 0 && code <= 255) || (kind == 1 && code >= 1 && code <= 127);
}

int fl_exit_encode(uint8_t out[5], int kind, int32_t code)
{
    if (out == NULL || !exit_ok(kind, code)) {
        return -1;
    }
    out[0] = (uint8_t)kind;
    put_u32(out + 1, (uint32_t)code);
    return 0;
}

int fl_exit_decode(const uint8_t *p, size_t n, int *kind, int32_t *code)
{
    int k;
    int32_t c;
    if (p == NULL || n != 5u || kind == NULL || code == NULL || p[0] > 1u) {
        return -1;
    }
    k = (int)p[0];
    c = (int32_t)get_u32(p + 1);
    if (!exit_ok(k, c)) {
        return -1;
    }
    *kind = k;
    *code = c;
    return 0;
}
