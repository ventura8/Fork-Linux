/*
 * fl_win.c - shared Win32 helpers for fl-shim.exe and fl-launch.exe
 * (Fork for Linux (unofficial), native-git bridge, Windows side).
 *
 * See fl_win.h for the API and docs/spikes/B2-rendezvous.md for the transport
 * facts this code relies on.
 */
#include "fl_win.h"

#include <bcrypt.h>
#include <ws2tcpip.h>

#include <limits.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "fl_sha256.h"

#define FLW_WPATH_MAX 32768u   /* UTF-16 units in the longest Win32 path */
#define FLW_MSG_MAX 2048u

/* ------------------------------------------------------------------------ */
/* diagnostics                                                               */
/* ------------------------------------------------------------------------ */

static char g_prog[48] = "fl-bridge";

void flw_set_prog(const char *name)
{
    size_t n = strlen(name);
    if (n >= sizeof(g_prog)) {
        n = sizeof(g_prog) - 1;
    }
    memcpy(g_prog, name, n);
    g_prog[n] = '\0';
}

static void write_stderr(const char *p, size_t n)
{
    HANDLE h = GetStdHandle(STD_ERROR_HANDLE);
    if (h == NULL || h == INVALID_HANDLE_VALUE) {
        return;
    }
    while (n > 0) {
        DWORD w = 0;
        DWORD chunk = n > 0x40000000u ? 0x40000000u : (DWORD)n;
        if (!WriteFile(h, p, chunk, &w, NULL) || w == 0) {
            return;
        }
        p += w;
        n -= w;
    }
}

static void vmsg(const char *fmt, va_list ap)
{
    char buf[FLW_MSG_MAX];
    size_t pn = strlen(g_prog);
    size_t used;
    int n;
    memcpy(buf, g_prog, pn);
    buf[pn] = ':';
    buf[pn + 1] = ' ';
    used = pn + 2;
    n = vsnprintf(buf + used, sizeof(buf) - used - 1, fmt, ap);
    if (n < 0) {
        n = 0;
    }
    if ((size_t)n >= sizeof(buf) - used - 1) {
        n = (int)(sizeof(buf) - used - 2);
    }
    used += (size_t)n;
    buf[used++] = '\n';
    write_stderr(buf, used);
}

void flw_msg(const char *fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    vmsg(fmt, ap);
    va_end(ap);
}

/* ------------------------------------------------------------------------ */
/* text                                                                      */
/* ------------------------------------------------------------------------ */

char *flw_utf8n(const wchar_t *w, size_t n)
{
    char *s;
    int need;
    if (n > (size_t)(INT_MAX / 4)) {
        return NULL;
    }
    if (n == 0) {
        s = malloc(1);
        if (s != NULL) {
            s[0] = '\0';
        }
        return s;
    }
    /* WC_ERR_INVALID_CHARS: a lone surrogate fails the conversion instead of silently
     * becoming U+FFFD, which would name a different file on the Unix side. */
    need = WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, w, (int)n, NULL, 0, NULL, NULL);
    if (need <= 0) {
        return NULL;
    }
    s = malloc((size_t)need + 1);
    if (s == NULL) {
        return NULL;
    }
    if (WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, w, (int)n, s, need, NULL, NULL) != need) {
        free(s);
        return NULL;
    }
    s[need] = '\0';
    return s;
}

char *flw_utf8(const wchar_t *w)
{
    return flw_utf8n(w, wcslen(w));
}

wchar_t *flw_utf16(const char *s)
{
    size_t n = strlen(s);
    wchar_t *w;
    int need;
    if (n > (size_t)(INT_MAX / 2)) {
        return NULL;
    }
    if (n == 0) {
        w = malloc(sizeof(wchar_t));
        if (w != NULL) {
            w[0] = L'\0';
        }
        return w;
    }
    need = MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, s, (int)n, NULL, 0);
    if (need <= 0) {
        return NULL;
    }
    w = malloc(((size_t)need + 1) * sizeof(wchar_t));
    if (w == NULL) {
        return NULL;
    }
    if (MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, s, (int)n, w, need) != need) {
        free(w);
        return NULL;
    }
    w[need] = L'\0';
    return w;
}

char *flw_strdup(const char *s)
{
    size_t n = strlen(s) + 1;
    char *d = malloc(n);
    if (d != NULL) {
        memcpy(d, s, n);
    }
    return d;
}

char *flw_getenv(const wchar_t *name)
{
    wchar_t *buf;
    char *r;
    DWORD need;
    DWORD got;
    SetLastError(ERROR_SUCCESS);
    need = GetEnvironmentVariableW(name, NULL, 0);
    if (need == 0) {
        if (GetLastError() == ERROR_ENVVAR_NOT_FOUND) {
            return NULL;
        }
        return flw_strdup("");
    }
    buf = malloc((size_t)need * sizeof(wchar_t));
    if (buf == NULL) {
        return NULL;
    }
    got = GetEnvironmentVariableW(name, buf, need);
    if (got >= need) {
        free(buf);
        return NULL;
    }
    r = flw_utf8n(buf, got);
    SecureZeroMemory(buf, (size_t)need * sizeof(wchar_t));
    free(buf);
    return r;
}

static char ascii_upper(char c)
{
    return (c >= 'a' && c <= 'z') ? (char)(c - 'a' + 'A') : c;
}

/* Case-insensitive ASCII substring search. */
static int contains_ci(const char *hay, const char *needle)
{
    size_t nl = strlen(needle);
    for (const char *p = hay; *p != '\0'; p++) {
        size_t i = 0;
        while (i < nl && p[i] != '\0' && ascii_upper(p[i]) == needle[i]) {
            i++;
        }
        if (i == nl) {
            return 1;
        }
    }
    return 0;
}

int flw_is_secret_name(const char *name)
{
    static const char *const words[] = { "TOKEN", "PASS", "SECRET", "AUTH", "COOKIE", "EXTRAHEADER" };
    for (size_t i = 0; i < sizeof(words) / sizeof(words[0]); i++) {
        if (contains_ci(name, words[i])) {
            return 1;
        }
    }
    return 0;
}

/* ------------------------------------------------------------------------ */
/* byte buffer + JSON                                                        */
/* ------------------------------------------------------------------------ */

void flw_buf_put(struct flw_buf *b, const char *s, size_t n)
{
    if (b->oom || n == 0) {
        return;
    }
    if (n > SIZE_MAX / 2 - b->n) {
        b->oom = 1;
        return;
    }
    if (b->n + n + 1 > b->cap) {
        size_t cap = b->cap ? b->cap : 256;
        char *p;
        while (cap < b->n + n + 1) {
            cap *= 2;
        }
        p = realloc(b->p, cap);
        if (p == NULL) {
            b->oom = 1;
            return;
        }
        b->p = p;
        b->cap = cap;
    }
    memcpy(b->p + b->n, s, n);
    b->n += n;
    b->p[b->n] = '\0';
}

void flw_buf_puts(struct flw_buf *b, const char *s)
{
    flw_buf_put(b, s, strlen(s));
}

void flw_buf_putu(struct flw_buf *b, unsigned long long v)
{
    char d[24];
    size_t i = sizeof(d);
    do {
        d[--i] = (char)('0' + (int)(v % 10u));
        v /= 10u;
    } while (v != 0 && i > 0);
    flw_buf_put(b, d + i, sizeof(d) - i);
}

void flw_buf_puti(struct flw_buf *b, long long v)
{
    if (v < 0) {
        flw_buf_put(b, "-", 1);
        flw_buf_putu(b, 0ull - (unsigned long long)v);
    } else {
        flw_buf_putu(b, (unsigned long long)v);
    }
}

/* Length of the valid UTF-8 sequence at s (n bytes left), 0 when invalid. */
static size_t utf8_seq(const unsigned char *s, size_t n)
{
    unsigned char c = s[0];
    size_t len;
    unsigned char lo = 0x80;
    unsigned char hi = 0xBF;
    if (c < 0x80) {
        return 1;
    }
    if (c >= 0xC2 && c <= 0xDF) {
        len = 2;
    } else if (c >= 0xE0 && c <= 0xEF) {
        len = 3;
        if (c == 0xE0) {
            lo = 0xA0;
        } else if (c == 0xED) {
            hi = 0x9F;
        }
    } else if (c >= 0xF0 && c <= 0xF4) {
        len = 4;
        if (c == 0xF0) {
            lo = 0x90;
        } else if (c == 0xF4) {
            hi = 0x8F;
        }
    } else {
        return 0;
    }
    if (n < len || s[1] < lo || s[1] > hi) {
        return 0;
    }
    for (size_t i = 2; i < len; i++) {
        if (s[i] < 0x80 || s[i] > 0xBF) {
            return 0;
        }
    }
    return len;
}

void flw_buf_json(struct flw_buf *b, const char *s)
{
    static const char hex[] = "0123456789abcdef";
    const unsigned char *p = (const unsigned char *)s;
    size_t n = strlen(s);
    flw_buf_put(b, "\"", 1);
    while (n > 0) {
        unsigned char c = *p;
        size_t len;
        if (c == '"' || c == '\\') {
            char e[2] = { '\\', (char)c };
            flw_buf_put(b, e, 2);
            p++;
            n--;
            continue;
        }
        if (c < 0x20 || c == 0x7F) {
            char e[6] = { '\\', 'u', '0', '0', hex[c >> 4], hex[c & 0xF] };
            if (c == '\n') {
                flw_buf_put(b, "\\n", 2);
            } else if (c == '\t') {
                flw_buf_put(b, "\\t", 2);
            } else if (c == '\r') {
                flw_buf_put(b, "\\r", 2);
            } else {
                flw_buf_put(b, e, 6);
            }
            p++;
            n--;
            continue;
        }
        len = utf8_seq(p, n);
        if (len == 0) {
            flw_buf_put(b, "\\ufffd", 6);
            p++;
            n--;
            continue;
        }
        flw_buf_put(b, (const char *)p, len);
        p += len;
        n -= len;
    }
    flw_buf_put(b, "\"", 1);
}

void flw_buf_free(struct flw_buf *b)
{
    free(b->p);
    b->p = NULL;
    b->n = 0;
    b->cap = 0;
    b->oom = 0;
}

/* ------------------------------------------------------------------------ */
/* randomness                                                                */
/* ------------------------------------------------------------------------ */

int flw_random(void *buf, size_t n)
{
    if (n > ULONG_MAX) {
        return -1;
    }
    return BCryptGenRandom(NULL, (PUCHAR)buf, (ULONG)n, BCRYPT_USE_SYSTEM_PREFERRED_RNG) == 0 ? 0 : -1;
}

/* ------------------------------------------------------------------------ */
/* Wine path conversion                                                      */
/* ------------------------------------------------------------------------ */

typedef char *(CDECL *unix_name_fn)(const WCHAR *dos);
typedef WCHAR *(CDECL *dos_name_fn)(const char *unix_path);

static unix_name_fn g_unix_fn;
static dos_name_fn g_dos_fn;
static int g_wine_init;

static void wine_init(void)
{
    HMODULE k32;
    if (g_wine_init) {
        return;
    }
    g_wine_init = 1;
    k32 = GetModuleHandleW(L"kernel32.dll");
    if (k32 == NULL) {
        return;
    }
    /* Cast through a generic function pointer: FARPROC -> the real signature. */
    g_unix_fn = (unix_name_fn)(void (*)(void))GetProcAddress(k32, "wine_get_unix_file_name");
    g_dos_fn = (dos_name_fn)(void (*)(void))GetProcAddress(k32, "wine_get_dos_file_name");
}

int flw_have_wine(void)
{
    wine_init();
    return g_unix_fn != NULL && g_dos_fn != NULL;
}

static int is_sep_w(wchar_t c)
{
    return c == L'\\' || c == L'/';
}

static int is_alpha_w(wchar_t c)
{
    return (c >= L'A' && c <= L'Z') || (c >= L'a' && c <= L'z');
}

/* malloc'd GetFullPathNameW(path); NULL on error. */
static wchar_t *full_path_w(const wchar_t *path)
{
    DWORD need = GetFullPathNameW(path, 0, NULL, NULL);
    wchar_t *buf;
    DWORD got;
    if (need == 0 || need > FLW_WPATH_MAX) {
        return NULL;
    }
    buf = malloc((size_t)need * sizeof(wchar_t));
    if (buf == NULL) {
        return NULL;
    }
    got = GetFullPathNameW(path, need, buf, NULL);
    if (got == 0 || got >= need) {
        free(buf);
        return NULL;
    }
    return buf;
}

/* Copy src into out (outsz bytes) with '\' -> '/'; 0 on success. */
static int copy_slashed(char *out, size_t outsz, size_t at, const char *src)
{
    size_t n = strlen(src);
    if (at + n + 1 > outsz) {
        return -1;
    }
    for (size_t i = 0; i < n; i++) {
        out[at + i] = src[i] == '\\' ? '/' : src[i];
    }
    out[at + n] = '\0';
    return 0;
}

/*
 * unix(full[0..cut)) + "/" + tail, where tail = full[cut..] without leading
 * separators. 1 when the prefix mapped (out written or -1 stored in *rc), 0 when
 * the prefix itself does not map.
 */
static int try_prefix(wchar_t *full, size_t cut, char *out, size_t outsz, int *rc)
{
    wchar_t saved = full[cut];
    char *u;
    size_t ul;
    const wchar_t *tail;
    char *tail8;
    full[cut] = L'\0';
    u = g_unix_fn(full);
    full[cut] = saved;
    if (u == NULL) {
        return 0;
    }
    tail = full + cut;
    while (is_sep_w(*tail)) {
        tail++;
    }
    ul = strlen(u);
    *rc = -1;
    if (*tail == L'\0') {
        if (ul + 1 <= outsz) {
            memcpy(out, u, ul + 1);
            *rc = 0;
        }
        HeapFree(GetProcessHeap(), 0, u);
        return 1;
    }
    while (ul > 1 && u[ul - 1] == '/') {
        ul--;
    }
    tail8 = flw_utf8(tail);
    if (tail8 != NULL && ul + 2 <= outsz) {
        memcpy(out, u, ul);
        out[ul] = '/';
        if (ul == 1 && u[0] == '/') {
            ul = 0; /* root: avoid "//" */
        }
        *rc = copy_slashed(out, outsz, ul + 1, tail8);
    }
    free(tail8);
    HeapFree(GetProcessHeap(), 0, u);
    return 1;
}

/* Collapse runs of '/' in place (Wine 11 maps "Z:\\missing" to "//missing"). */
static void collapse_slashes(char *s)
{
    char *w = s;
    for (const char *r = s; *r != '\0'; r++) {
        if (*r == '/' && w > s && w[-1] == '/') {
            continue;
        }
        *w++ = *r;
    }
    *w = '\0';
}

int flw_to_unix_cb(void *ud, const char *in, char *out, size_t outsz)
{
    wchar_t *w;
    wchar_t *full;
    size_t cut;
    int rc = -1;
    (void)ud;
    wine_init();
    if (g_unix_fn == NULL || outsz == 0) {
        return -1;
    }
    w = flw_utf16(in);
    if (w == NULL) {
        return -1;
    }
    if (wcsncmp(w, L"\\\\?\\", 4) == 0) {
        full = w; /* already a verbatim NT-style path: do not let Win32 rewrite it */
        w = NULL;
    } else {
        full = full_path_w(w);
    }
    free(w);
    if (full == NULL) {
        return -1;
    }
    cut = wcslen(full);
    for (;;) {
        size_t p;
        size_t np;
        if (try_prefix(full, cut, out, outsz, &rc)) {
            break;
        }
        /* Longest existing ancestor: drop the last component and retry. */
        p = cut;
        while (p > 0 && !is_sep_w(full[p - 1])) {
            p--;
        }
        if (p == 0) {
            break;
        }
        np = p - 1;
        while (np > 0 && is_sep_w(full[np - 1])) {
            np--;
        }
        if (np == 2 && full[1] == L':') {
            np = 3; /* keep the drive root "X:\" */
        }
        if (np == 0 || np >= cut) {
            break;
        }
        cut = np;
    }
    free(full);
    if (rc == 0) {
        collapse_slashes(out);
    }
    return rc;
}

int flw_to_win_cb(void *ud, const char *in, char *out, size_t outsz)
{
    WCHAR *d;
    char *s;
    size_t n;
    int rc = -1;
    (void)ud;
    wine_init();
    if (g_dos_fn == NULL || in[0] != '/') {
        return -1;
    }
    d = g_dos_fn(in);
    if (d == NULL) {
        return -1;
    }
    if (!(is_alpha_w(d[0]) && d[1] == L':')) {
        HeapFree(GetProcessHeap(), 0, d); /* \\?\unix\...: outside every drive */
        return -1;
    }
    s = flw_utf8(d);
    HeapFree(GetProcessHeap(), 0, d);
    if (s == NULL) {
        return -1;
    }
    s[0] = ascii_upper(s[0]);
    n = strlen(s);
    if (n + 1 <= outsz) {
        rc = copy_slashed(out, outsz, 0, s);
    }
    free(s);
    return rc;
}

char *flw_path_to_unix(const char *win)
{
    size_t cap = strlen(win) * 3 + 8192;
    char *buf = malloc(cap);
    char *r;
    if (buf == NULL) {
        return NULL;
    }
    if (flw_to_unix_cb(NULL, win, buf, cap) != 0) {
        free(buf);
        return NULL;
    }
    r = flw_strdup(buf);
    free(buf);
    return r;
}

static int is_abs_w(const wchar_t *p)
{
    return (is_alpha_w(p[0]) && p[1] == L':' && is_sep_w(p[2])) || (is_sep_w(p[0]) && is_sep_w(p[1]));
}

char *flw_norm_win(const char *path, const char *base)
{
    wchar_t *w = flw_utf16(path);
    wchar_t *full;
    wchar_t *d;
    char *s;
    size_t n;
    if (w == NULL) {
        return NULL;
    }
    if (base != NULL && !is_abs_w(w) && !is_sep_w(w[0]) && !(is_alpha_w(w[0]) && w[1] == L':')) {
        wchar_t *wb = flw_utf16(base);
        size_t bl;
        size_t pl = wcslen(w);
        wchar_t *joined;
        if (wb == NULL) {
            free(w);
            return NULL;
        }
        bl = wcslen(wb);
        joined = malloc((bl + pl + 2) * sizeof(wchar_t));
        if (joined == NULL) {
            free(wb);
            free(w);
            return NULL;
        }
        memcpy(joined, wb, bl * sizeof(wchar_t));
        joined[bl] = L'\\';
        memcpy(joined + bl + 1, w, (pl + 1) * sizeof(wchar_t));
        free(wb);
        free(w);
        w = joined;
    }
    full = full_path_w(w);
    free(w);
    if (full == NULL) {
        return NULL;
    }
    d = full;
    if (wcsncmp(d, L"\\\\?\\", 4) == 0 && is_alpha_w(d[4]) && d[5] == L':') {
        d += 4;
    }
    if (!(is_alpha_w(d[0]) && d[1] == L':' && (d[2] == L'\0' || is_sep_w(d[2])))) {
        free(full);
        return NULL;
    }
    s = flw_utf8(d);
    free(full);
    if (s == NULL) {
        return NULL;
    }
    s[0] = ascii_upper(s[0]);
    for (char *p = s; *p != '\0'; p++) {
        if (*p == '\\') {
            *p = '/';
        }
    }
    n = strlen(s);
    if (n == 2) {
        char *r = realloc(s, 4);
        if (r == NULL) {
            free(s);
            return NULL;
        }
        r[2] = '/';
        r[3] = '\0';
        return r;
    }
    while (n > 3 && s[n - 1] == '/') {
        s[--n] = '\0';
    }
    return s;
}

char *flw_cwd(void)
{
    DWORD need = GetCurrentDirectoryW(0, NULL);
    wchar_t *buf;
    DWORD got;
    char *r;
    if (need == 0 || need > FLW_WPATH_MAX) {
        return NULL;
    }
    buf = malloc((size_t)need * sizeof(wchar_t));
    if (buf == NULL) {
        return NULL;
    }
    got = GetCurrentDirectoryW(need, buf);
    if (got == 0 || got >= need) {
        free(buf);
        return NULL;
    }
    r = flw_utf8n(buf, got);
    free(buf);
    return r;
}

char *flw_module_path(void)
{
    DWORD cap = 512;
    for (;;) {
        wchar_t *buf = malloc((size_t)cap * sizeof(wchar_t));
        DWORD got;
        if (buf == NULL) {
            return NULL;
        }
        got = GetModuleFileNameW(NULL, buf, cap);
        if (got == 0) {
            free(buf);
            return NULL;
        }
        if (got < cap) {
            char *r = flw_utf8n(buf, got);
            free(buf);
            return r;
        }
        free(buf);
        if (cap >= FLW_WPATH_MAX) {
            return NULL;
        }
        cap *= 2;
    }
}

const char *flw_basename(const char *path)
{
    const char *b = path;
    for (const char *p = path; *p != '\0'; p++) {
        if (*p == '\\' || *p == '/') {
            b = p + 1;
        }
    }
    return b;
}

/* ------------------------------------------------------------------------ */
/* configuration                                                             */
/* ------------------------------------------------------------------------ */

static int parse_port(const char *s, uint16_t *port)
{
    unsigned long v = 0;
    size_t n = strlen(s);
    if (n == 0 || n > 5) {
        return -1;
    }
    for (size_t i = 0; i < n; i++) {
        if (s[i] < '0' || s[i] > '9') {
            return -1;
        }
        v = v * 10u + (unsigned long)(s[i] - '0');
    }
    if (v == 0 || v > 65535u) {
        return -1;
    }
    *port = (uint16_t)v;
    return 0;
}

int flw_load_cfg(struct flw_cfg *c, int need_winexec)
{
    char *port;
    char *tok;
    int bad;
    memset(c, 0, sizeof(*c));
    port = flw_getenv(L"FL_BRIDGE_PORT");
    if (port == NULL || port[0] == '\0') {
        free(port);
        flw_msg("FL_BRIDGE_PORT is not set: start Fork through fork-linux with the git bridge enabled");
        return -1;
    }
    bad = parse_port(port, &c->port);
    if (bad) {
        flw_msg("FL_BRIDGE_PORT is not a TCP port number: '%s'", port);
    }
    free(port);
    if (bad) {
        return -1;
    }
    tok = flw_getenv(L"FL_BRIDGE_TOKEN");
    if (tok == NULL || tok[0] == '\0') {
        free(tok);
        flw_msg("FL_BRIDGE_TOKEN is not set: start Fork through fork-linux with the git bridge enabled");
        return -1;
    }
    bad = strlen(tok) != 2u * FLW_KEY_LEN || fl_hex_decode(tok, c->key, FLW_KEY_LEN) != 0;
    SecureZeroMemory(tok, strlen(tok));
    free(tok);
    if (bad) {
        SecureZeroMemory(c->key, sizeof(c->key));
        flw_msg("FL_BRIDGE_TOKEN must be %u hex characters", 2u * FLW_KEY_LEN);
        return -1;
    }
    c->winexec = flw_getenv(L"FL_BRIDGE_WINEXEC");
    if (c->winexec != NULL && c->winexec[0] == '\0') {
        free(c->winexec);
        c->winexec = NULL;
    }
    if ((c->winexec == NULL && need_winexec) || (c->winexec != NULL && c->winexec[0] != '/')) {
        flw_msg("FL_BRIDGE_WINEXEC must be the absolute Unix path of the fl-winexec helper");
        flw_cfg_clear(c);
        return -1;
    }
    c->askpass = flw_getenv(L"FL_BRIDGE_ASKPASS");
    c->ssh_askpass = flw_getenv(L"FL_BRIDGE_SSH_ASKPASS");
    if (c->askpass != NULL && c->askpass[0] == '\0') {
        free(c->askpass);
        c->askpass = NULL;
    }
    if (c->ssh_askpass != NULL && c->ssh_askpass[0] == '\0') {
        free(c->ssh_askpass);
        c->ssh_askpass = NULL;
    }
    return 0;
}

void flw_cfg_clear(struct flw_cfg *c)
{
    SecureZeroMemory(c->key, sizeof(c->key));
    free(c->winexec);
    free(c->askpass);
    free(c->ssh_askpass);
    c->winexec = NULL;
    c->askpass = NULL;
    c->ssh_askpass = NULL;
}

void flw_xlate_init(struct fl_xlate *x, const struct flw_cfg *c)
{
    memset(x, 0, sizeof(*x));
    x->to_unix = flw_to_unix_cb;
    x->to_win = flw_to_win_cb;
    x->ud = NULL;
    x->winexec = c->winexec;
    x->askpass = c->askpass;
    x->ssh_askpass = c->ssh_askpass;
}

void flw_free_strv(char **v)
{
    if (v == NULL) {
        return;
    }
    for (size_t i = 0; v[i] != NULL; i++) {
        free(v[i]);
    }
    free(v);
}

char **flw_environ(void)
{
    wchar_t *blk = GetEnvironmentStringsW();
    size_t count = 0;
    size_t i = 0;
    char **v;
    if (blk == NULL) {
        return NULL;
    }
    for (const wchar_t *p = blk; *p != L'\0'; p += wcslen(p) + 1) {
        count++;
    }
    v = calloc(count + 1, sizeof(char *));
    if (v == NULL) {
        FreeEnvironmentStringsW(blk);
        return NULL;
    }
    for (const wchar_t *p = blk; *p != L'\0'; p += wcslen(p) + 1) {
        char *s;
        if (p[0] == L'=' || _wcsnicmp(p, L"FL_BRIDGE_", 10) == 0 || wcschr(p, L'=') == NULL) {
            continue; /* "=C:=C:\x" drive cwds; bridge configuration (token) */
        }
        s = flw_utf8(p);
        if (s == NULL) {
            /* Not representable in UTF-8 (lone surrogate): leave this one variable
             * out rather than fail the call or forward a corrupted value. */
            continue;
        }
        v[i++] = s;
    }
    FreeEnvironmentStringsW(blk);
    return v;
}

/* ------------------------------------------------------------------------ */
/* JSON-line call log                                                        */
/* ------------------------------------------------------------------------ */

static void put_2(struct flw_buf *b, unsigned v)
{
    char d[2] = { (char)('0' + (int)(v / 10u % 10u)), (char)('0' + (int)(v % 10u)) };
    flw_buf_put(b, d, 2);
}

void flw_buf_timestamp(struct flw_buf *b)
{
    SYSTEMTIME st;
    char ms[3];
    GetSystemTime(&st);
    flw_buf_put(b, "\"", 1);
    flw_buf_putu(b, st.wYear);
    flw_buf_put(b, "-", 1);
    put_2(b, st.wMonth);
    flw_buf_put(b, "-", 1);
    put_2(b, st.wDay);
    flw_buf_put(b, "T", 1);
    put_2(b, st.wHour);
    flw_buf_put(b, ":", 1);
    put_2(b, st.wMinute);
    flw_buf_put(b, ":", 1);
    put_2(b, st.wSecond);
    flw_buf_put(b, ".", 1);
    ms[0] = (char)('0' + (int)(st.wMilliseconds / 100u % 10u));
    ms[1] = (char)('0' + (int)(st.wMilliseconds / 10u % 10u));
    ms[2] = (char)('0' + (int)(st.wMilliseconds % 10u));
    flw_buf_put(b, ms, 3);
    flw_buf_put(b, "Z\"", 2);
}

/* Copy of s with the userinfo of every "scheme://user:pass@host" replaced. */
static char *redact_urls(const char *s)
{
    struct flw_buf b = { 0 };
    const char *p = s;
    for (;;) {
        const char *sch = strstr(p, "://");
        const char *q;
        const char *at = NULL;
        if (sch == NULL) {
            break;
        }
        q = sch + 3;
        while (*q != '\0' && *q != '/' && *q != '?' && *q != '#' && *q != ' ' && *q != '\'' && *q != '"') {
            if (*q == '@') {
                at = q;
            }
            q++;
        }
        flw_buf_put(&b, p, (size_t)(sch + 3 - p));
        if (at != NULL) {
            flw_buf_puts(&b, "<redacted>");
            p = at;
        } else {
            p = sch + 3;
        }
    }
    flw_buf_puts(&b, p);
    if (b.oom) {
        flw_buf_free(&b);
        return NULL;
    }
    if (b.p == NULL) {
        return flw_strdup("");
    }
    return b.p;
}

static void json_redacted(struct flw_buf *b, const char *s)
{
    char *r = redact_urls(s);
    flw_buf_json(b, r != NULL ? r : "<redacted>");
    free(r);
}

void flw_buf_json_args(struct flw_buf *b, size_t argc, char *const *argv)
{
    flw_buf_put(b, "[", 1);
    for (size_t i = 0; i < argc; i++) {
        const char *a = argv[i];
        const char *eq = strchr(a, '=');
        if (i > 0) {
            flw_buf_put(b, ",", 1);
        }
        if (i > 0 && strcmp(argv[i - 1], "-c") == 0 && eq != NULL) {
            char *key = malloc((size_t)(eq - a) + 1);
            int secret = 1;
            if (key != NULL) {
                memcpy(key, a, (size_t)(eq - a));
                key[eq - a] = '\0';
                secret = flw_is_secret_name(key) || flw_is_secret_name(eq + 1);
                if (secret) {
                    struct flw_buf t = { 0 };
                    flw_buf_puts(&t, key);
                    flw_buf_puts(&t, "=<redacted>");
                    flw_buf_json(b, t.oom || t.p == NULL ? "<redacted>" : t.p);
                    flw_buf_free(&t);
                }
                free(key);
            } else {
                flw_buf_json(b, "<redacted>");
            }
            if (secret) {
                continue;
            }
        }
        json_redacted(b, a);
    }
    flw_buf_put(b, "]", 1);
}

void flw_buf_json_env_value(struct flw_buf *b, const char *name, const char *value)
{
    if (flw_is_secret_name(name) || flw_is_secret_name(value)) {
        flw_buf_json(b, "<redacted>");
        return;
    }
    json_redacted(b, value);
}

static const char *file_type_name(DWORD which)
{
    HANDLE h = GetStdHandle(which);
    if (h == NULL || h == INVALID_HANDLE_VALUE) {
        return "none";
    }
    switch (GetFileType(h)) {
    case FILE_TYPE_DISK:
        return "disk";
    case FILE_TYPE_CHAR:
        return "char";
    case FILE_TYPE_PIPE:
        return "pipe";
    case FILE_TYPE_REMOTE:
        return "remote";
    default:
        return "unknown";
    }
}

void flw_buf_json_stdio(struct flw_buf *b)
{
    flw_buf_puts(b, "{\"stdin\":");
    flw_buf_json(b, file_type_name(STD_INPUT_HANDLE));
    flw_buf_puts(b, ",\"stdout\":");
    flw_buf_json(b, file_type_name(STD_OUTPUT_HANDLE));
    flw_buf_puts(b, ",\"stderr\":");
    flw_buf_json(b, file_type_name(STD_ERROR_HANDLE));
    flw_buf_put(b, "}", 1);
}

void flw_log_append(const char *path, struct flw_buf *b)
{
    wchar_t *wpath = NULL;
    HANDLE h;
    OVERLAPPED ov;
    int locked;
    DWORD w = 0;
    if (path == NULL || path[0] == '\0') {
        return;
    }
    flw_buf_put(b, "\n", 1);
    if (b->oom || b->n > 0x7FFFFFFFu) {
        return;
    }
    if (path[0] == '/') {
        wine_init();
        if (g_dos_fn != NULL) {
            WCHAR *d = g_dos_fn(path);
            if (d != NULL) {
                size_t n = wcslen(d) + 1;
                wpath = malloc(n * sizeof(wchar_t));
                if (wpath != NULL) {
                    memcpy(wpath, d, n * sizeof(wchar_t));
                }
                HeapFree(GetProcessHeap(), 0, d);
            }
        }
    } else {
        wpath = flw_utf16(path);
    }
    if (wpath == NULL) {
        return;
    }
    h = CreateFileW(wpath, FILE_APPEND_DATA, FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, NULL,
                    OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
    free(wpath);
    if (h == INVALID_HANDLE_VALUE) {
        return;
    }
    /* Serialise writers (parallel git calls) on a lock byte far past any content. */
    memset(&ov, 0, sizeof(ov));
    ov.Offset = 0xFFFFFFF0u;
    ov.OffsetHigh = 0x7FFFFFFFu;
    locked = LockFileEx(h, LOCKFILE_EXCLUSIVE_LOCK, 0, 1, 0, &ov) != 0;
    WriteFile(h, b->p, (DWORD)b->n, &w, NULL);
    if (locked) {
        UnlockFileEx(h, 0, 1, 0, &ov);
    }
    CloseHandle(h);
}

/* ------------------------------------------------------------------------ */
/* transport                                                                 */
/* ------------------------------------------------------------------------ */

static SOCKET g_sock = INVALID_SOCKET;
static CRITICAL_SECTION g_send_lock;
static uint8_t g_tx[5 + FL_MAX_CHUNK];
static uint8_t g_rx[FL_MAX_CHUNK];
static uint8_t *g_big;
static size_t g_big_cap;

static HANDLE g_quit;    /* manual-reset: the main thread is exiting */
static HANDLE g_pump;    /* stdin pump thread */
static uint8_t g_inbuf[FL_MAX_CHUNK];

/* What the pump is doing, so flw_exit knows whether waiting for it can help. */
enum { PUMP_BUSY = 0, PUMP_READING = 1, PUMP_SENDING = 2 };
static volatile LONG g_pump_phase;
/* Join protocol: the pump returns only while flw_exit is still waiting for it; once
 * flw_exit gives up (abandoned) the pump parks forever and never races ExitProcess. */
static CRITICAL_SECTION g_join_lock;
static int g_pump_abandoned;
static int g_pump_returning;

static flw_exit_hook g_hook;
static void *g_hook_ud;
static volatile LONG g_exiting;

static int send_all(const void *buf, size_t len)
{
    const char *p = buf;
    while (len > 0) {
        int chunk = len > 0x40000000u ? 0x40000000 : (int)len;
        int n = send(g_sock, p, chunk, 0);
        if (n <= 0) {
            return -1;
        }
        p += n;
        len -= (size_t)n;
    }
    return 0;
}

static int recv_all(void *buf, size_t len)
{
    char *p = buf;
    while (len > 0) {
        int chunk = len > 0x40000000u ? 0x40000000 : (int)len;
        int n = recv(g_sock, p, chunk, 0);
        if (n <= 0) {
            return -1;
        }
        p += n;
        len -= (size_t)n;
    }
    return 0;
}

static void set_rcv_timeout(DWORD ms)
{
    setsockopt(g_sock, SOL_SOCKET, SO_RCVTIMEO, (const char *)&ms, (int)sizeof(ms));
}

int flw_connect(uint16_t port)
{
    WSADATA wsa;
    struct sockaddr_in a;
    BOOL one = TRUE;
    if (WSAStartup(MAKEWORD(2, 2), &wsa) != 0) {
        return -1;
    }
    g_sock = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (g_sock == INVALID_SOCKET) {
        return -1;
    }
    SetHandleInformation((HANDLE)g_sock, HANDLE_FLAG_INHERIT, 0);
    memset(&a, 0, sizeof(a));
    a.sin_family = AF_INET;
    a.sin_port = htons(port);
    a.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    if (connect(g_sock, (const struct sockaddr *)&a, (int)sizeof(a)) != 0) {
        return -1;
    }
    setsockopt(g_sock, IPPROTO_TCP, TCP_NODELAY, (const char *)&one, (int)sizeof(one));
    set_rcv_timeout(FLW_HANDSHAKE_MS);
    InitializeCriticalSection(&g_send_lock);
    return 0;
}

int flw_send_frame(uint8_t type, const void *payload, uint32_t len)
{
    uint8_t hdr[5];
    int rc;
    if (fl_hdr_encode(hdr, type, len) != 0) {
        return -1;
    }
    EnterCriticalSection(&g_send_lock);
    if (len <= FL_MAX_CHUNK) {
        memcpy(g_tx, hdr, sizeof(hdr));
        if (len > 0) {
            memcpy(g_tx + sizeof(hdr), payload, len);
        }
        rc = send_all(g_tx, sizeof(hdr) + len);
    } else {
        rc = send_all(hdr, sizeof(hdr));
        if (rc == 0) {
            rc = send_all(payload, len);
        }
    }
    LeaveCriticalSection(&g_send_lock);
    return rc;
}

int flw_recv_frame(uint8_t *type, const uint8_t **payload, uint32_t *len)
{
    uint8_t hdr[5];
    uint8_t *dst = g_rx;
    if (recv_all(hdr, sizeof(hdr)) != 0 || fl_hdr_decode(hdr, type, len) != 0 || *len > FL_MAX_REQ) {
        return -1;
    }
    if (*len > sizeof(g_rx)) {
        if (*len > g_big_cap) {
            uint8_t *p = realloc(g_big, *len);
            if (p == NULL) {
                return -1;
            }
            g_big = p;
            g_big_cap = *len;
        }
        dst = g_big;
    }
    if (*len > 0 && recv_all(dst, *len) != 0) {
        return -1;
    }
    *payload = dst;
    return 0;
}

static void mac(const uint8_t key[FLW_KEY_LEN], const char *label, const uint8_t *cn, const uint8_t *sn,
                uint8_t out[FLW_MAC_LEN])
{
    uint8_t msg[32 + 2 * FLW_NONCE_LEN];
    size_t ll = strlen(label);
    memcpy(msg, label, ll);
    memcpy(msg + ll, cn, FLW_NONCE_LEN);
    memcpy(msg + ll + FLW_NONCE_LEN, sn, FLW_NONCE_LEN);
    fl_hmac_sha256(key, FLW_KEY_LEN, msg, ll + 2 * FLW_NONCE_LEN, out);
    SecureZeroMemory(msg, sizeof(msg));
}

int flw_handshake(const uint8_t key[FLW_KEY_LEN])
{
    static const char magic[] = FL_PROTO_MAGIC;
    uint8_t hello[4 + 2 + FLW_NONCE_LEN];
    uint8_t cn[FLW_NONCE_LEN];
    uint8_t sn[FLW_NONCE_LEN];
    uint8_t want[FLW_MAC_LEN];
    uint8_t cmac[FLW_MAC_LEN];
    const uint8_t *p;
    uint8_t type;
    uint32_t len;
    int ok;
    if (flw_random(cn, sizeof(cn)) != 0) {
        flw_msg("BCryptGenRandom failed");
        return -1;
    }
    memcpy(hello, magic, 4);
    hello[4] = (uint8_t)((FL_PROTO_VERSION >> 8) & 0xFF);
    hello[5] = (uint8_t)(FL_PROTO_VERSION & 0xFF);
    memcpy(hello + 6, cn, sizeof(cn));
    if (flw_send_frame(FL_F_HELLO, hello, (uint32_t)sizeof(hello)) != 0) {
        flw_msg("cannot send HELLO to the bridge daemon");
        return -1;
    }
    if (flw_recv_frame(&type, &p, &len) != 0 || type != FL_F_CHALLENGE || len != FLW_NONCE_LEN + FLW_MAC_LEN) {
        flw_msg("the bridge daemon did not answer HELLO with a challenge");
        return -1;
    }
    memcpy(sn, p, FLW_NONCE_LEN);
    mac(key, "fl-bridge-v1|daemon|", cn, sn, want);
    ok = fl_ct_equal(want, p + FLW_NONCE_LEN, FLW_MAC_LEN);
    SecureZeroMemory(want, sizeof(want));
    if (!ok) {
        flw_msg("authentication failed: the bridge daemon did not prove FL_BRIDGE_TOKEN (stale token in the "
                "environment, or another process on the port); refusing to talk to it");
        return -1;
    }
    mac(key, "fl-bridge-v1|client|", cn, sn, cmac);
    ok = flw_send_frame(FL_F_AUTH, cmac, (uint32_t)sizeof(cmac)) == 0;
    SecureZeroMemory(cmac, sizeof(cmac));
    if (!ok) {
        flw_msg("cannot send AUTH to the bridge daemon");
        return -1;
    }
    if (flw_recv_frame(&type, &p, &len) != 0 || type != FL_F_AUTH_OK || len != 0) {
        flw_msg("the bridge daemon rejected our token (FL_BRIDGE_TOKEN mismatch)");
        return -1;
    }
    return 0;
}

int flw_send_req(const struct fl_req *r)
{
    uint8_t *buf = NULL;
    size_t len = 0;
    int rc;
    if (fl_req_encode(r, &buf, &len) != 0 || len > FL_MAX_REQ) {
        free(buf);
        flw_msg("cannot encode the request (invalid entry, or larger than %u bytes)", (unsigned)FL_MAX_REQ);
        return -1;
    }
    rc = flw_send_frame(FL_F_REQ, buf, (uint32_t)len);
    free(buf);
    if (rc != 0) {
        flw_msg("cannot send the request to the bridge daemon");
    }
    return rc;
}

static uint32_t get_u32(const uint8_t *p)
{
    return ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16) | ((uint32_t)p[2] << 8) | (uint32_t)p[3];
}

uint32_t flw_wait_spawn(const char *what, char ***real, size_t *nreal)
{
    const uint8_t *p;
    uint8_t type;
    uint32_t len;
    uint32_t pid;
    size_t count = 0;
    *real = NULL;
    *nreal = 0;
    if (flw_recv_frame(&type, &p, &len) != 0) {
        flw_fail("the bridge daemon closed the connection before starting '%s'", what);
    }
    if (type == FL_F_SPAWN_ERR) {
        int32_t err = len >= 4 ? (int32_t)get_u32(p) : 0;
        char msg[512];
        size_t ml = len > 4 ? len - 4 : 0;
        if (ml >= sizeof(msg)) {
            ml = sizeof(msg) - 1;
        }
        for (size_t i = 0; i < ml; i++) {
            char c = (char)p[4 + i];
            /* No control characters (terminal escapes) from the daemon reach our stderr. */
            msg[i] = ((unsigned char)c < 0x20 || c == 0x7F) ? ' ' : c;
        }
        msg[ml] = '\0';
        flw_msg("cannot run '%s': %s (errno %d)", what, ml > 0 ? msg : "spawn failed", (int)err);
        flw_exit(FLW_EXIT_SPAWN);
    }
    if (type != FL_F_SPAWN_OK || len < 4) {
        flw_fail("protocol error: expected SPAWN_OK from the bridge daemon, got frame type %u", (unsigned)type);
    }
    pid = get_u32(p);
    /* NUL-separated realpaths of the REQ anchors, in order ("" = unresolved). */
    for (uint32_t i = 4; i < len; i++) {
        if (p[i] == 0 || i + 1 == len) {
            count++;
        }
    }
    if (count > 0) {
        char **v = calloc(count, sizeof(char *));
        uint32_t start = 4;
        size_t k = 0;
        if (v == NULL) {
            flw_fail("out of memory");
        }
        for (uint32_t i = 4; i < len && k < count; i++) {
            if (p[i] == 0 || i + 1 == len) {
                uint32_t end = p[i] == 0 ? i : i + 1;
                char *s = malloc((size_t)(end - start) + 1);
                if (s == NULL) {
                    flw_fail("out of memory");
                }
                memcpy(s, p + start, (size_t)(end - start));
                s[end - start] = '\0';
                v[k++] = s;
                start = i + 1;
            }
        }
        *real = v;
        *nreal = k;
    }
    set_rcv_timeout(0); /* the relay may legitimately idle for a long time */
    return pid;
}

/* ------------------------------------------------------------------------ */
/* relay                                                                     */
/* ------------------------------------------------------------------------ */

void flw_sink_init(struct flw_sink *s, DWORD std_handle, struct fl_out *out)
{
    s->h = GetStdHandle(std_handle);
    s->discard = s->h == NULL || s->h == INVALID_HANDLE_VALUE;
    s->broken = 0;
    s->out = out;
}

static void sink_emit(void *ud, const char *p, size_t n)
{
    struct flw_sink *s = ud;
    if (s->discard || s->broken) {
        return;
    }
    while (n > 0) {
        DWORD w = 0;
        DWORD chunk = n > 0x40000000u ? 0x40000000u : (DWORD)n;
        if (!WriteFile(s->h, p, chunk, &w, NULL) || w == 0) {
            s->broken = 1;
            return;
        }
        p += w;
        n -= w;
    }
}

static void sink_feed(struct flw_sink *s, const uint8_t *p, uint32_t n)
{
    if (s->out != NULL) {
        fl_out_feed(s->out, (const char *)p, n, sink_emit, s);
    } else {
        sink_emit(s, (const char *)p, n);
    }
}

static void sink_flush(struct flw_sink *s)
{
    if (s->out != NULL) {
        fl_out_flush(s->out, sink_emit, s);
    }
}

/*
 * Stdin pump (B2 fix 1): our stdin -> STDIN frames, then STDIN_EOF. It never exits
 * by itself: after EOF it parks on g_quit, and it returns only while flw_exit is
 * still joining it (see pump_finish); otherwise it parks forever. A thread that
 * returns while ExitProcess runs can turn the process exit code into 0 under Wine.
 */
static int quitting(void)
{
    return WaitForSingleObject(g_quit, 0) == WAIT_OBJECT_0;
}

static DWORD WINAPI stdin_pump(LPVOID arg)
{
    HANDLE h = GetStdHandle(STD_INPUT_HANDLE);
    int ok = 1;
    (void)arg;
    if (h != NULL && h != INVALID_HANDLE_VALUE) {
        for (;;) {
            DWORD n = 0;
            BOOL got;
            if (quitting()) {
                ok = 0;
                break;
            }
            InterlockedExchange(&g_pump_phase, PUMP_READING);
            got = ReadFile(h, g_inbuf, (DWORD)sizeof(g_inbuf), &n, NULL);
            InterlockedExchange(&g_pump_phase, PUMP_BUSY);
            if (!got || n == 0) {
                break;
            }
            if (quitting()) {
                ok = 0;
                break;
            }
            InterlockedExchange(&g_pump_phase, PUMP_SENDING);
            got = flw_send_frame(FL_F_STDIN, g_inbuf, n) == 0;
            InterlockedExchange(&g_pump_phase, PUMP_BUSY);
            if (!got) {
                ok = 0;
                break;
            }
        }
    }
    if (ok && !quitting()) {
        InterlockedExchange(&g_pump_phase, PUMP_SENDING);
        flw_send_frame(FL_F_STDIN_EOF, NULL, 0);
        InterlockedExchange(&g_pump_phase, PUMP_BUSY);
    }
    WaitForSingleObject(g_quit, INFINITE);
    EnterCriticalSection(&g_join_lock);
    if (!g_pump_abandoned) {
        g_pump_returning = 1;
        LeaveCriticalSection(&g_join_lock);
        return 0; /* flw_exit is waiting on our handle */
    }
    LeaveCriticalSection(&g_join_lock);
    for (;;) {
        Sleep(INFINITE); /* flw_exit is in ExitProcess: never race it */
    }
}

/*
 * Stop the pump before ExitProcess (B2 fixes 1 and 4): set the quit event, cancel its
 * blocking ReadFile / send with CancelSynchronousIo, and join it. A ReadFile on a
 * handle Wine cannot cancel (a Unix pipe inherited from a shell) is not waited for
 * beyond FLW_READ_JOIN_MS: the pump is then abandoned (it will never return, never
 * send) and ExitProcess reaps it. A pump inside send() gets up to FLW_JOIN_MS, so the
 * socket is not torn down under a send in progress.
 */
static void pump_finish(void)
{
    ULONGLONG start = GetTickCount64();
    DWORD step = 1;
    int joined = 0;
    SetEvent(g_quit);
    for (;;) {
        ULONGLONG el;
        CancelSynchronousIo(g_pump);
        if (WaitForSingleObject(g_pump, step) == WAIT_OBJECT_0) {
            joined = 1;
            break;
        }
        el = GetTickCount64() - start;
        if (el >= FLW_JOIN_MS || (el >= FLW_READ_JOIN_MS && g_pump_phase == PUMP_READING)) {
            break;
        }
        if (step < 16) {
            step *= 2;
        }
    }
    if (!joined) {
        EnterCriticalSection(&g_join_lock);
        if (g_pump_returning) {
            LeaveCriticalSection(&g_join_lock);
            WaitForSingleObject(g_pump, INFINITE); /* it is returning right now */
        } else {
            g_pump_abandoned = 1;
            LeaveCriticalSection(&g_join_lock);
        }
    }
}

/* Best effort SIGNAL frame that never blocks behind a pump stuck in send(). */
static void try_send_signal(uint8_t signo)
{
    ULONGLONG deadline = GetTickCount64() + 200u;
    DWORD tmo = 1000;
    uint8_t fr[6];
    while (!TryEnterCriticalSection(&g_send_lock)) {
        if (GetTickCount64() >= deadline) {
            return;
        }
        Sleep(5);
    }
    setsockopt(g_sock, SOL_SOCKET, SO_SNDTIMEO, (const char *)&tmo, (int)sizeof(tmo));
    if (fl_hdr_encode(fr, FL_F_SIGNAL, 1) == 0) {
        fr[5] = signo;
        send_all(fr, sizeof(fr));
    }
    LeaveCriticalSection(&g_send_lock);
}

_Noreturn void flw_relay(struct flw_sink *out, struct flw_sink *err, int pump_stdin)
{
    g_quit = CreateEventW(NULL, TRUE, FALSE, NULL);
    if (g_quit == NULL) {
        flw_fail("CreateEvent failed (win32 error %lu)", GetLastError());
    }
    if (pump_stdin) {
        InitializeCriticalSection(&g_join_lock);
        g_pump = CreateThread(NULL, 0, stdin_pump, NULL, 0, NULL);
        if (g_pump == NULL) {
            flw_fail("CreateThread failed (win32 error %lu)", GetLastError());
        }
    } else if (flw_send_frame(FL_F_STDIN_EOF, NULL, 0) != 0) {
        flw_fail("connection to the bridge daemon lost");
    }
    for (;;) {
        const uint8_t *p;
        uint8_t type;
        uint32_t n;
        int kind;
        int32_t code;
        if (flw_recv_frame(&type, &p, &n) != 0) {
            flw_fail("connection to the bridge daemon lost before the exit status");
        }
        switch (type) {
        case FL_F_STDOUT:
            sink_feed(out, p, n);
            if (out->broken) {
                /* Fork closed its reader: SIGPIPE-equivalent for the child, 141 for us. */
                try_send_signal(FLW_SIGPIPE);
                flw_exit(FLW_EXIT_SIGPIPE);
            }
            break;
        case FL_F_STDERR:
            sink_feed(err, p, n);
            break;
        case FL_F_EXIT:
            if (fl_exit_decode(p, n, &kind, &code) != 0) {
                flw_fail("protocol error: malformed EXIT frame");
            }
            sink_flush(out);
            sink_flush(err);
            if (kind == 0 && code >= 0 && code <= 255) {
                flw_exit((UINT)code);
            }
            if (kind == 1 && code > 0 && code < 128) {
                flw_exit((UINT)(128 + code));
            }
            flw_fail("protocol error: EXIT kind %d code %ld", kind, (long)code);
        default:
            flw_fail("protocol error: unexpected frame type %u from the bridge daemon", (unsigned)type);
        }
    }
}

/* ------------------------------------------------------------------------ */
/* exit                                                                      */
/* ------------------------------------------------------------------------ */

void flw_set_exit_hook(flw_exit_hook fn, void *ud)
{
    g_hook = fn;
    g_hook_ud = ud;
}

_Noreturn void flw_exit(UINT code)
{
    if (InterlockedExchange(&g_exiting, 1) == 0 && g_hook != NULL) {
        g_hook(g_hook_ud, code);
    }
    if (g_pump != NULL) {
        pump_finish(); /* B2 fix 1; the socket is left to process teardown (fix 4) */
    }
    ExitProcess(code);
}

_Noreturn void flw_fail(const char *fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    vmsg(fmt, ap);
    va_end(ap);
    flw_exit(FLW_EXIT_BRIDGE);
}
