/*
 * fl_helper_util.c - pure helpers of fl-bridge-helper (see fl_helper_util.h).
 */
#define _POSIX_C_SOURCE 200809L

#include "fl_helper_util.h"

#include <signal.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>

#include "fl_proto.h"
#include "fl_sha256.h"

/* Wipe a buffer in a way the compiler cannot drop as a dead store. */
static void wipe(void *p, size_t n)
{
    volatile unsigned char *v = p;
    while (n--) {
        *v++ = 0;
    }
}

static int is_hex(char c)
{
    return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') || (c >= 'A' && c <= 'F');
}

static int is_alpha(char c)
{
    return (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z');
}

static char lower(char c)
{
    return (c >= 'A' && c <= 'Z') ? (char)(c - 'A' + 'a') : c;
}

const char *fl_basename_any(const char *path)
{
    const char *base = path;
    for (const char *p = path; *p; p++) {
        if (*p == '/' || *p == '\\') {
            base = p + 1;
        }
    }
    return base;
}

void fl_log_sanitize(char *s)
{
    for (; s && *s; s++) {
        unsigned char u = (unsigned char)*s;
        if (u <= 0x20u || u == 0x7fu) {
            *s = '?';
        }
    }
}

/* 1 when name equals want, optionally followed by ".exe" (any case). */
static int name_is(const char *name, const char *want)
{
    size_t n = strlen(want);
    if (strncmp(name, want, n) != 0) {
        return 0;
    }
    name += n;
    return name[0] == '\0' || (name[0] == '.' && lower(name[1]) == 'e' && lower(name[2]) == 'x' &&
                               lower(name[3]) == 'e' && name[4] == '\0');
}

enum fl_persona fl_persona_from_argv0(const char *argv0)
{
    const char *b;
    if (argv0 == NULL) {
        return FL_PERSONA_HELPER;
    }
    b = fl_basename_any(argv0);
    if (name_is(b, "fl-winexec")) {
        return FL_PERSONA_WINEXEC;
    }
    if (name_is(b, "fl-askpass")) {
        return FL_PERSONA_ASKPASS;
    }
    if (name_is(b, "fl-ssh-askpass")) {
        return FL_PERSONA_SSH_ASKPASS;
    }
    return FL_PERSONA_HELPER;
}

int fl_token_parse(const char *text, size_t len, uint8_t key[FL_KEY_LEN])
{
    char hex[FL_TOKEN_HEX_LEN + 1];
    int rc;
    if (text == NULL || len < FL_TOKEN_HEX_LEN) {
        return -1;
    }
    if (len == FL_TOKEN_HEX_LEN + 1) {
        if (text[FL_TOKEN_HEX_LEN] != '\n') {
            return -1;
        }
    } else if (len == FL_TOKEN_HEX_LEN + 2) {
        if (text[FL_TOKEN_HEX_LEN] != '\r' || text[FL_TOKEN_HEX_LEN + 1] != '\n') {
            return -1;
        }
    } else if (len != FL_TOKEN_HEX_LEN) {
        return -1;
    }
    for (size_t i = 0; i < FL_TOKEN_HEX_LEN; i++) {
        if (!is_hex(text[i])) {
            return -1;
        }
    }
    memcpy(hex, text, FL_TOKEN_HEX_LEN);
    hex[FL_TOKEN_HEX_LEN] = '\0';
    rc = fl_hex_decode(hex, key, FL_KEY_LEN) == 0 ? 0 : -1;
    wipe(hex, sizeof(hex));
    return rc;
}

/* Parse 1..maxdigits decimal digits into *out; -1 on anything else or > max. */
static int parse_decimal(const char *s, size_t maxdigits, unsigned long long max, unsigned long long *out)
{
    unsigned long long v = 0;
    size_t n = 0;
    if (s == NULL || s[0] == '\0') {
        return -1;
    }
    for (; s[n]; n++) {
        if (s[n] < '0' || s[n] > '9' || n >= maxdigits) {
            return -1;
        }
        v = v * 10u + (unsigned long long)(s[n] - '0');
    }
    if (v > max) {
        return -1;
    }
    *out = v;
    return 0;
}

int fl_parse_port(const char *s, uint16_t *out)
{
    unsigned long long v;
    if (parse_decimal(s, 5, 65535u, &v) != 0) {
        return -1;
    }
    *out = (uint16_t)v;
    return 0;
}

int fl_parse_pid(const char *s, int32_t *out)
{
    unsigned long long v;
    if (parse_decimal(s, 10, (unsigned long long)INT32_MAX, &v) != 0 || v == 0) {
        return -1;
    }
    *out = (int32_t)v;
    return 0;
}

void fl_hello_build(uint8_t out[FL_HELLO_LEN], const uint8_t client_nonce[FL_NONCE_LEN])
{
    memcpy(out, FL_PROTO_MAGIC, 4);
    out[4] = (uint8_t)((FL_PROTO_VERSION >> 8) & 0xffu);
    out[5] = (uint8_t)(FL_PROTO_VERSION & 0xffu);
    memcpy(out + 6, client_nonce, FL_NONCE_LEN);
}

int fl_hello_parse(const uint8_t *p, size_t n, uint8_t client_nonce[FL_NONCE_LEN])
{
    unsigned version;
    if (p == NULL || n != FL_HELLO_LEN || memcmp(p, FL_PROTO_MAGIC, 4) != 0) {
        return -1;
    }
    version = ((unsigned)p[4] << 8) | (unsigned)p[5];
    if (version != (unsigned)FL_PROTO_VERSION) {
        return -1;
    }
    memcpy(client_nonce, p + 6, FL_NONCE_LEN);
    return 0;
}

void fl_auth_mac(const uint8_t key[FL_KEY_LEN], int daemon_side, const uint8_t cn[FL_NONCE_LEN],
                 const uint8_t sn[FL_NONCE_LEN], uint8_t out[FL_MAC_LEN])
{
    static const char daemon_label[] = "fl-bridge-v1|daemon|";
    static const char client_label[] = "fl-bridge-v1|client|";
    uint8_t msg[sizeof(daemon_label) - 1 + 2 * FL_NONCE_LEN];
    size_t ln = sizeof(daemon_label) - 1;
    memcpy(msg, daemon_side ? daemon_label : client_label, ln);
    memcpy(msg + ln, cn, FL_NONCE_LEN);
    memcpy(msg + ln + FL_NONCE_LEN, sn, FL_NONCE_LEN);
    fl_hmac_sha256(key, FL_KEY_LEN, msg, sizeof(msg), out);
}

void fl_wait_to_exit(int wstatus, int *kind, int32_t *code)
{
    if (WIFEXITED(wstatus)) {
        *kind = 0;
        *code = (int32_t)WEXITSTATUS(wstatus);
    } else if (WIFSIGNALED(wstatus)) {
        *kind = 1;
        *code = (int32_t)WTERMSIG(wstatus);
    } else {
        *kind = 0;   /* stopped/continued never reach here (no WUNTRACED); be defensive */
        *code = 125;
    }
}

int fl_signal_allowed(int signo)
{
    return signo == SIGINT || signo == SIGTERM || signo == SIGKILL || signo == SIGHUP;
}

int fl_spawn_ok_encode(uint32_t pid, char *const *paths, size_t n, size_t max_len, uint8_t **out,
                       size_t *outlen)
{
    size_t total = 4;
    uint8_t *buf;
    size_t off = 4;
    for (size_t i = 0; i < n; i++) {
        size_t l = paths[i] ? strlen(paths[i]) : 0;
        if (l + 1 > max_len || total > max_len - (l + 1)) {
            return -1;
        }
        total += l + 1;
    }
    if (total > max_len) {
        return -1;
    }
    buf = malloc(total);
    if (buf == NULL) {
        return -1;
    }
    buf[0] = (uint8_t)(pid >> 24);
    buf[1] = (uint8_t)(pid >> 16);
    buf[2] = (uint8_t)(pid >> 8);
    buf[3] = (uint8_t)pid;
    for (size_t i = 0; i < n; i++) {
        size_t l = paths[i] ? strlen(paths[i]) : 0;
        if (l) {
            memcpy(buf + off, paths[i], l);
        }
        off += l;
        buf[off++] = 0;
    }
    *out = buf;
    *outlen = total;
    return 0;
}

/* Length of the key of a "K=V" entry; 0 when there is no '=' or the key is empty. */
static size_t key_len(const char *kv)
{
    const char *eq = strchr(kv, '=');
    return eq ? (size_t)(eq - kv) : 0;
}

static int key_eq(const char *a, size_t alen, const char *b)
{
    return strlen(b) == alen && memcmp(a, b, alen) == 0;
}

/* Keys the daemon always provides itself (the Windows-side values are meaningless here). */
static int key_fixed(const char *k, size_t klen)
{
    return key_eq(k, klen, "HOME") || key_eq(k, klen, "PATH") || key_eq(k, klen, "TMPDIR");
}

static int key_secret(const char *k, size_t klen)
{
    return key_eq(k, klen, "FL_BRIDGE_TOKEN");
}

static int unset_valid(const char *u)
{
    return u != NULL && u[0] != '\0' && strchr(u, '=') == NULL;
}

void fl_env_free(char **env)
{
    if (env == NULL) {
        return;
    }
    for (size_t i = 0; env[i]; i++) {
        free(env[i]);
    }
    free(env);
}

/* Number of unset operations that are ignored (malformed, HOME/PATH/TMPDIR, LANG). */
static size_t count_ignored_unsets(char *const *unset, size_t nunset)
{
    size_t skipped = 0;
    for (size_t i = 0; i < nunset; i++) {
        size_t ul = unset_valid(unset[i]) ? strlen(unset[i]) : 0;
        if (ul == 0 || key_fixed(unset[i], ul) || key_eq(unset[i], ul, "LANG")) {
            skipped++;
        }
    }
    return skipped;
}

/* 1 when the base entry with key kv[0..kl) is not passed on (secret, or removed by an unset). */
static int base_dropped(const char *kv, size_t kl, char *const *unset, size_t nunset)
{
    if (kl == 0 || key_secret(kv, kl)) {
        return 1;
    }
    if (key_fixed(kv, kl) || key_eq(kv, kl, "LANG")) {
        return 0;
    }
    for (size_t j = 0; j < nunset; j++) {
        if (unset_valid(unset[j]) && key_eq(kv, kl, unset[j])) {
            return 1;
        }
    }
    return 0;
}

/* Index of the entry of env[0..n) with key kv[0..kl) ("K=" included), or n. */
static size_t find_key(char *const *env, size_t n, const char *kv, size_t kl)
{
    for (size_t j = 0; j < n; j++) {
        if (key_len(env[j]) == kl && memcmp(env[j], kv, kl + 1) == 0) {
            return j;
        }
    }
    return n;
}

/* Apply one set operation to env[0..*n); 0 applied, 1 ignored, -1 out of memory. */
static int apply_set(char **env, size_t *n, const char *kv)
{
    size_t kl = kv ? key_len(kv) : 0;
    size_t j;
    char *copy;
    if (kl == 0 || key_fixed(kv, kl) || key_secret(kv, kl)) {
        return 1;
    }
    copy = strdup(kv);
    if (copy == NULL) {
        return -1;
    }
    j = find_key(env, *n, kv, kl);
    if (j < *n) {
        free(env[j]);
        env[j] = copy;
    } else {
        env[*n] = copy;
        (*n)++;
    }
    return 0;
}

int fl_env_build(char *const *base, char *const *set, size_t nset, char *const *unset, size_t nunset,
                 char ***out, size_t *ignored)
{
    size_t nbase = 0;
    size_t n = 0;
    size_t skipped;
    char **env;
    while (base && base[nbase]) {
        nbase++;
    }
    if (nset > ((size_t)-1) / sizeof(char *) - nbase - 1) {
        return -1;
    }
    env = calloc(nbase + nset + 1, sizeof(char *));
    if (env == NULL) {
        return -1;
    }
    skipped = count_ignored_unsets(unset, nunset);
    for (size_t i = 0; i < nbase; i++) {
        if (base_dropped(base[i], key_len(base[i]), unset, nunset)) {
            continue;
        }
        env[n] = strdup(base[i]);
        if (env[n] == NULL) {
            fl_env_free(env);
            return -1;
        }
        n++;
    }
    for (size_t i = 0; i < nset; i++) {
        int r = apply_set(env, &n, set[i]);
        if (r < 0) {
            fl_env_free(env);
            return -1;
        }
        skipped += (size_t)r;
    }
    env[n] = NULL;
    *out = env;
    if (ignored) {
        *ignored = skipped;
    }
    return 0;
}

const char *fl_env_get(char *const *env, const char *key)
{
    size_t kl = strlen(key);
    for (size_t i = 0; env && env[i]; i++) {
        if (strncmp(env[i], key, kl) == 0 && env[i][kl] == '=') {
            return env[i] + kl + 1;
        }
    }
    return NULL;
}

int fl_path_join(const char *dir, size_t dirlen, const char *name, char *out, size_t outsz)
{
    size_t nl = strlen(name);
    int slash = dirlen > 0 && dir[dirlen - 1] == '/';
    size_t need = dirlen + (slash ? 0u : 1u) + nl + 1;
    if (need > outsz || need < nl) {
        return -1;
    }
    memcpy(out, dir, dirlen);
    if (!slash) {
        out[dirlen++] = '/';
    }
    memcpy(out + dirlen, name, nl + 1);
    return 0;
}

/* Bounded string builder used by fl_unix_to_win. */
struct sbuf {
    char *p;
    size_t n;
    size_t cap;
    int overflow;
};

static void sb_putc(struct sbuf *b, char c)
{
    if (b->n + 1 >= b->cap) {
        b->overflow = 1;
        return;
    }
    b->p[b->n++] = c;
    b->p[b->n] = '\0';
}

/*
 * Emit "<win>\<rest>": win with '/' turned into '\' and trailing separators
 * dropped, then each component of rest (a Unix tail) preceded by one '\'.
 * A bare drive ("C:") with no tail becomes "C:\".
 */
static int join_win(const char *win, size_t winlen, const char *rest, char *out, size_t outsz)
{
    struct sbuf b = { out, 0, outsz, 0 };
    const char *r = rest;
    out[0] = '\0';
    while (winlen > 0 && (win[winlen - 1] == '\\' || win[winlen - 1] == '/')) {
        winlen--;
    }
    for (size_t i = 0; i < winlen; i++) {
        sb_putc(&b, win[i] == '/' ? '\\' : win[i]);
    }
    for (;;) {
        while (*r == '/') {
            r++;
        }
        if (*r == '\0') {
            break;
        }
        sb_putc(&b, '\\');
        while (*r != '\0' && *r != '/') {
            sb_putc(&b, *r++);
        }
    }
    if (b.n == 2 && out[1] == ':') {
        sb_putc(&b, '\\');
    }
    return b.overflow ? -1 : 0;
}

/* 1 when s starts like a Windows absolute path: a drive ("X:") or a UNC prefix (two separators). */
static int win_abs_start(const char *s, const char *end)
{
    if (end - s >= 2 && is_alpha(s[0]) && s[1] == ':') {
        return 1;
    }
    return end - s >= 2 && (s[0] == '\\' || s[0] == '/') && (s[1] == '\\' || s[1] == '/');
}

/* 1 when the unix prefix [u, u+ulen) is a component prefix of path. */
static int component_prefix(const char *path, const char *u, size_t ulen)
{
    if (ulen == 1 && u[0] == '/') {
        return 1;
    }
    return strncmp(path, u, ulen) == 0 && (path[ulen] == '\0' || path[ulen] == '/');
}

/* One FL_BRIDGE_ANCHORS entry e[0..end): its unix side and its windows side. */
struct anchor_entry {
    const char *unix_path;
    size_t ulen;
    const char *win;
    size_t winlen;
};

/* Split at the first '=' that starts a Windows absolute path, else the first '='. NULL if none. */
static const char *anchor_split(const char *e, const char *end)
{
    const char *first = NULL;
    for (const char *q = e; q < end; q++) {
        if (*q != '=') {
            continue;
        }
        if (win_abs_start(q + 1, end)) {
            return q;
        }
        if (first == NULL) {
            first = q;
        }
    }
    return first;
}

/* Parse the entry e[0..end); 0 when it is a usable "/unix=win" pair. */
static int anchor_parse(const char *e, const char *end, struct anchor_entry *a)
{
    const char *eq = anchor_split(e, end);
    if (eq == NULL || e[0] != '/' || eq + 1 >= end) {
        return -1;
    }
    a->unix_path = e;
    a->ulen = (size_t)(eq - e);
    while (a->ulen > 1 && e[a->ulen - 1] == '/') {
        a->ulen--;
    }
    a->win = eq + 1;
    a->winlen = (size_t)(end - (eq + 1));
    return 0;
}

/* The longest-matching anchor of the list for path; 0 when one matched (*best set). */
static int best_anchor_entry(const char *path, const char *anchors, struct anchor_entry *best)
{
    int found = 0;
    const char *e = anchors;
    while (e != NULL && *e != '\0') {
        const char *end = strchr(e, ';');
        struct anchor_entry a = {NULL, 0, NULL, 0};
        if (end == NULL) {
            end = e + strlen(e);
        }
        if (anchor_parse(e, end, &a) == 0 && component_prefix(path, a.unix_path, a.ulen) &&
            (!found || a.ulen > best->ulen)) {
            *best = a;
            found = 1;
        }
        e = *end ? end + 1 : end;
    }
    return found ? 0 : -1;
}

/* Length of "$WINEPREFIX/drive_c" when path lies inside it (the implicit "C:" anchor), else 0. */
static size_t drive_c_len(const char *path, const char *wineprefix)
{
    static const char dc[] = "/drive_c";
    size_t pl;
    size_t dl;
    if (wineprefix == NULL || wineprefix[0] != '/') {
        return 0;
    }
    pl = strlen(wineprefix);
    while (pl > 1 && wineprefix[pl - 1] == '/') {
        pl--;
    }
    dl = pl + sizeof(dc) - 1;
    if (pl > 1 && strncmp(path, wineprefix, pl) == 0 && strncmp(path + pl, dc, sizeof(dc) - 1) == 0 &&
        (path[dl] == '\0' || path[dl] == '/')) {
        return dl;
    }
    return 0;
}

int fl_unix_to_win(const char *path, const char *anchors, const char *wineprefix, char *out,
                   size_t outsz)
{
    struct anchor_entry best = {NULL, 0, NULL, 0};
    int have_best;
    size_t dl;
    if (path == NULL || path[0] != '/' || out == NULL || outsz == 0) {
        return -1;
    }
    have_best = best_anchor_entry(path, anchors, &best) == 0;
    /* $WINEPREFIX/drive_c is an implicit "C:" anchor taking part in the longest match. */
    dl = drive_c_len(path, wineprefix);
    if (dl > 0 && (!have_best || dl > best.ulen)) {
        return join_win("C:", 2, path + dl, out, outsz);
    }
    if (have_best) {
        const char *rest = (best.ulen == 1) ? path : path + best.ulen;
        return join_win(best.win, best.winlen, rest, out, outsz);
    }
    return join_win("Z:", 2, path, out, outsz);
}

/* 1 when s[0..n) contains "winemenubuilder" (any case). */
static int mentions_menubuilder(const char *s, size_t n)
{
    static const char needle[] = "winemenubuilder";
    size_t m = sizeof(needle) - 1;
    for (size_t i = 0; i + m <= n; i++) {
        size_t k = 0;
        while (k < m && lower(s[i + k]) == needle[k]) {
            k++;
        }
        if (k == m) {
            return 1;
        }
    }
    return 0;
}

int fl_dlloverrides(const char *cur, char *out, size_t outsz)
{
    static const char add[] = "winemenubuilder.exe=d";
    size_t cl = cur ? strlen(cur) : 0;
    if (cl > 0 && mentions_menubuilder(cur, cl)) {
        if (cl + 1 > outsz) {
            return -1;
        }
        memcpy(out, cur, cl + 1);
        return 0;
    }
    if (cl == 0) {
        if (sizeof(add) > outsz) {
            return -1;
        }
        memcpy(out, add, sizeof(add));
        return 0;
    }
    if (cl + 1 + sizeof(add) > outsz) {
        return -1;
    }
    memcpy(out, cur, cl);
    out[cl] = ';';
    memcpy(out + cl + 1, add, sizeof(add));
    return 0;
}
