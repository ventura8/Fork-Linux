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

/* Bounded writer for fl_req_encode(): every record is checked against the capacity before
   anything is written, so a size mismatch sets err instead of overflowing buf. */
struct tlv_writer {
    uint8_t *buf;
    size_t cap;
    size_t pos;
    int err;
};

static void put_tlv(struct tlv_writer *w, uint8_t tag, const void *data, size_t n)
{
    size_t room;
    if (w->err || w->pos > w->cap) {
        w->err = 1;
        return;
    }
    room = w->cap - w->pos;
    if (room < TLV_HDR || n > room - TLV_HDR) {
        w->err = 1;
        return;
    }
    w->buf[w->pos] = tag;
    put_u32(w->buf + w->pos + 1u, (uint32_t)n);
    if (n > 0) {
        memcpy(w->buf + w->pos + TLV_HDR, data, n);
    }
    w->pos += TLV_HDR + n;
}

static void put_list(struct tlv_writer *w, uint8_t tag, char *const *v, size_t n)
{
    for (size_t i = 0; i < n; i++) {
        put_tlv(w, tag, v[i], strlen(v[i]));
    }
}

static int req_shape_ok(const struct fl_req *r)
{
    return r->cwd != NULL && r->argc != 0 && r->argv != NULL && r->argv[0] != NULL && r->argv[0][0] != '\0' &&
           (r->flags & ~(FL_REQ_DETACH | FL_REQ_HOST_HELPER)) == 0u;
}

/* Validates r like fl_req_decode() does and computes the encoded size into *total. */
static int req_size(const struct fl_req *r, size_t *total)
{
    size_t cwdlen = strlen(r->cwd);
    if (!cwd_ok(r->cwd, cwdlen) || add_size(total, cwdlen) != 0 || add_size(total, 4u) != 0) {
        return -1;
    }
    if (list_size(r->argv, r->argc, total, NULL) != 0 || list_size(r->set, r->nset, total, env_set_ok) != 0) {
        return -1;
    }
    if (list_size(r->unset, r->nunset, total, env_unset_ok) != 0) {
        return -1;
    }
    return list_size(r->anchors, r->nanchors, total, NULL);
}

int fl_req_encode(const struct fl_req *r, uint8_t **out, size_t *outlen)
{
    size_t total = 0;
    uint8_t flags[4];
    struct tlv_writer w;
    if (out != NULL) {
        *out = NULL;
    }
    if (outlen != NULL) {
        *outlen = 0;
    }
    if (r == NULL || out == NULL || outlen == NULL || !req_shape_ok(r) || req_size(r, &total) != 0) {
        return -1;
    }
    w.buf = malloc(total);
    if (w.buf == NULL) {
        return -1;
    }
    w.cap = total;
    w.pos = 0;
    w.err = 0;
    put_u32(flags, r->flags);
    put_tlv(&w, FL_T_CWD, r->cwd, strlen(r->cwd));
    put_tlv(&w, FL_T_FLAGS, flags, sizeof(flags));
    put_list(&w, FL_T_ARG, r->argv, r->argc);
    put_list(&w, FL_T_ENV_SET, r->set, r->nset);
    put_list(&w, FL_T_ENV_UNSET, r->unset, r->nunset);
    put_list(&w, FL_T_ANCHOR, r->anchors, r->nanchors);
    if (w.err || w.pos != total) {
        free(w.buf);
        return -1;
    }
    *out = w.buf;
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

/* REQ decoder state: the records seen so far (all malloc'd, released by dec_free()). */
struct req_dec {
    struct vec argv;
    struct vec set;
    struct vec unset;
    struct vec anchors;
    char *cwd;
    uint32_t flags;
    int have_flags;
};

static void dec_free(struct req_dec *d)
{
    free(d->cwd);
    vec_free(d->argv.v, d->argv.n);
    vec_free(d->set.v, d->set.n);
    vec_free(d->unset.v, d->unset.n);
    vec_free(d->anchors.v, d->anchors.n);
    memset(d, 0, sizeof(*d));
}

/* The next TLV record at buf[*pos]; 0 with *pos advanced past it, -1 when truncated. */
static int next_tlv(const uint8_t *buf, size_t len, size_t *pos, uint8_t *tag, const uint8_t **data, size_t *n)
{
    size_t p = *pos;
    size_t l;
    if (p > len || len - p < TLV_HDR) {
        return -1;
    }
    *tag = buf[p];
    l = (size_t)get_u32(buf + p + 1u);
    p += TLV_HDR;
    if (l > len - p) {
        return -1;
    }
    *data = buf + p;
    *n = l;
    *pos = p + l;
    return 0;
}

static int dec_cwd(struct req_dec *d, const uint8_t *data, size_t n)
{
    if (d->cwd != NULL || !cwd_ok((const char *)data, n)) {
        return -1;
    }
    d->cwd = malloc(n + 1);
    if (d->cwd == NULL) {
        return -1;
    }
    memcpy(d->cwd, data, n);
    d->cwd[n] = '\0';
    return 0;
}

static int dec_flags(struct req_dec *d, const uint8_t *data, size_t n)
{
    if (d->have_flags || n != 4u) {
        return -1;
    }
    d->flags = get_u32(data);
    d->have_flags = 1;
    return (d->flags & ~(FL_REQ_DETACH | FL_REQ_HOST_HELPER)) != 0u ? -1 : 0;
}

/* Apply one record; 0 ok, -1 invalid (or out of memory). */
static int dec_record(struct req_dec *d, uint8_t tag, const uint8_t *data, size_t n)
{
    if (tag != FL_T_FLAGS && n > 0 && memchr(data, '\0', n) != NULL) {
        return -1;
    }
    switch (tag) {
    case FL_T_CWD:
        return dec_cwd(d, data, n);
    case FL_T_ARG:
        if (d->argv.n == 0 && n == 0) {
            return -1; /* argv[0] must name a program */
        }
        return vec_push(&d->argv, data, n);
    case FL_T_ENV_SET:
        return env_set_ok((const char *)data, n) ? vec_push(&d->set, data, n) : -1;
    case FL_T_ENV_UNSET:
        return env_unset_ok((const char *)data, n) ? vec_push(&d->unset, data, n) : -1;
    case FL_T_FLAGS:
        return dec_flags(d, data, n);
    case FL_T_ANCHOR:
        return vec_push(&d->anchors, data, n);
    default:
        return -1;
    }
}

static int dec_all(struct req_dec *d, const uint8_t *buf, size_t len)
{
    size_t pos = 0;
    while (pos < len) {
        uint8_t tag = 0;
        const uint8_t *data = NULL;
        size_t n = 0;
        if (next_tlv(buf, len, &pos, &tag, &data, &n) != 0 || dec_record(d, tag, data, n) != 0) {
            return -1;
        }
    }
    if (d->cwd == NULL || d->argv.n == 0) {
        return -1;
    }
    if (vec_terminate(&d->set) != 0 || vec_terminate(&d->unset) != 0) {
        return -1;
    }
    return vec_terminate(&d->anchors);
}

int fl_req_decode(const uint8_t *buf, size_t len, struct fl_req *r)
{
    struct req_dec d;
    if (r == NULL) {
        return -1;
    }
    memset(r, 0, sizeof(*r));
    if (buf == NULL || len > FL_MAX_REQ) {
        return -1;
    }
    memset(&d, 0, sizeof(d));
    if (dec_all(&d, buf, len) != 0) {
        dec_free(&d);
        return -1;
    }
    r->cwd = d.cwd;
    r->argc = d.argv.n;
    r->argv = d.argv.v;
    r->nset = d.set.n;
    r->set = d.set.v;
    r->nunset = d.unset.n;
    r->unset = d.unset.v;
    r->flags = d.flags;
    r->nanchors = d.anchors.n;
    r->anchors = d.anchors.v;
    return 0;
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
