/*
 * fl_win_unit.c - TEST-ONLY unit tests of bridge/win/fl_win.c, run under Wine by
 * tests/bridge/test_shims_wine_coverage.py. Never installed or shipped.
 *
 *   fl-win-unit.exe pure          every self-checking case; prints "fl-win-unit: N checks,
 *                                 F failed" and exits 1 when F > 0
 *   fl-win-unit.exe <exit-case>   one call that ends the process (flw_fail, flw_exit, an
 *                                 allocation failure in an exiting wrapper); the Python
 *                                 test checks the exit status and stderr
 *
 * Allocation failures are injected with ld --wrap=malloc,--wrap=calloc,--wrap=realloc
 * (bridge/tests/meson.build): fail_alloc(k) makes the k-th next allocation return NULL.
 */
#include "fl_win.h"

#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

int _dowildcard = 0;

/* ---- allocation failure injection --------------------------------------- */

void *__real_malloc(size_t n);
void *__real_calloc(size_t n, size_t size);
void *__real_realloc(void *p, size_t n);
void *__wrap_malloc(size_t n);
void *__wrap_calloc(size_t n, size_t size);
void *__wrap_realloc(void *p, size_t n);

static volatile LONG g_fail_in; /* > 0: the g_fail_in-th next allocation fails */

static int alloc_fails(void)
{
    return g_fail_in > 0 && InterlockedDecrement(&g_fail_in) == 0;
}

void *__wrap_malloc(size_t n)
{
    return alloc_fails() ? NULL : __real_malloc(n);
}

void *__wrap_calloc(size_t n, size_t size)
{
    return alloc_fails() ? NULL : __real_calloc(n, size);
}

void *__wrap_realloc(void *p, size_t n)
{
    return alloc_fails() ? NULL : __real_realloc(p, n);
}

static void fail_alloc(LONG k)
{
    InterlockedExchange(&g_fail_in, k);
}

/* ---- checks -------------------------------------------------------------- */

static int g_checks;
static int g_failed;

static void check_at(int ok, int line, const char *what)
{
    g_checks++;
    if (!ok) {
        g_failed++;
        printf("FAIL line %d: %s\n", line, what);
    }
}

#define CHECK(c) check_at((c) ? 1 : 0, __LINE__, #c)

static int streq(const char *a, const char *b)
{
    return a != NULL && b != NULL && strcmp(a, b) == 0;
}

static int starts(const char *s, const char *pfx)
{
    return s != NULL && strncmp(s, pfx, strlen(pfx)) == 0;
}

static int ends(const char *s, const char *sfx)
{
    size_t n;
    size_t m = strlen(sfx);
    if (s == NULL) {
        return 0;
    }
    n = strlen(s);
    return n >= m && strcmp(s + n - m, sfx) == 0;
}

/* flw_buf_json(s) == want (want without the surrounding quotes) */
static int json_is(const char *s, const char *want)
{
    struct flw_buf b = { 0 };
    struct flw_buf w = { 0 };
    int ok;
    flw_buf_json(&b, s);
    flw_buf_put(&w, "\"", 1);
    flw_buf_puts(&w, want);
    flw_buf_put(&w, "\"", 1);
    ok = streq(b.p, w.p);
    if (!ok) {
        printf("  json got %s want %s\n", b.p != NULL ? b.p : "(null)", w.p);
    }
    flw_buf_free(&b);
    flw_buf_free(&w);
    return ok;
}

/* ---- stderr capture ------------------------------------------------------ */

static HANDLE g_saved_err;
static HANDLE g_cap_r;
static HANDLE g_cap_w;

static void cap_begin(void)
{
    g_saved_err = GetStdHandle(STD_ERROR_HANDLE);
    if (!CreatePipe(&g_cap_r, &g_cap_w, NULL, 65536)) {
        g_cap_r = NULL;
        g_cap_w = NULL;
    }
    SetStdHandle(STD_ERROR_HANDLE, g_cap_w);
}

/* What was written to stderr since cap_begin (static buffer). */
static const char *cap_end(void)
{
    static char got[8192];
    DWORD n = 0;
    DWORD avail = 0;
    SetStdHandle(STD_ERROR_HANDLE, g_saved_err);
    CloseHandle(g_cap_w);
    got[0] = '\0';
    if (PeekNamedPipe(g_cap_r, NULL, 0, NULL, &avail, NULL) && avail > 0 &&
        ReadFile(g_cap_r, got, (DWORD)sizeof(got) - 1, &n, NULL)) {
        got[n] = '\0';
    }
    CloseHandle(g_cap_r);
    return got;
}

/* ---- cases ---------------------------------------------------------------- */

static void t_messages(void)
{
    static char big[3000];
    struct flw_buf m = { 0 };
    const char *got;
    HANDLE r;
    HANDLE w;

    flw_set_prog("a-program-name-much-longer-than-the-forty-eight-byte-buffer");
    cap_begin();
    flw_msg("x");
    got = cap_end();
    CHECK(streq(got, "a-program-name-much-longer-than-the-forty-eight: x\n"));

    flw_set_prog("unit");
    cap_begin();
    flw_msg_s("a '", "b", "' c");
    got = cap_end();
    CHECK(streq(got, "unit: a 'b' c\n"));

    cap_begin();
    flw_msg_buf(&m); /* nothing appended: p is NULL */
    got = cap_end();
    CHECK(streq(got, "unit: \n"));

    memset(big, 'y', sizeof(big) - 1);
    cap_begin();
    flw_msg(big);
    got = cap_end();
    CHECK(strlen(got) == 2047u); /* FLW_MSG_MAX - 1: prefix, cut text, newline */
    CHECK(got[strlen(got) - 1] == '\n');

    /* No stderr handle, and one that cannot be written: silently nothing. */
    g_saved_err = GetStdHandle(STD_ERROR_HANDLE);
    SetStdHandle(STD_ERROR_HANDLE, NULL);
    flw_msg("lost");
    SetStdHandle(STD_ERROR_HANDLE, INVALID_HANDLE_VALUE);
    flw_msg("lost");
    if (CreatePipe(&r, &w, NULL, 0)) {
        SetStdHandle(STD_ERROR_HANDLE, r); /* the read end: WriteFile fails */
        flw_msg("lost");
        CloseHandle(r);
        CloseHandle(w);
    }
    SetStdHandle(STD_ERROR_HANDLE, g_saved_err);
}

static void t_text(void)
{
    static const wchar_t lone[] = { L'a', 0xD800, L'b', 0 };
    static const wchar_t pair[] = { 0xD83D, 0xDE00, 0 };
    char *s;
    wchar_t *w;

    s = flw_utf8(L"");
    CHECK(streq(s, ""));
    free(s);
    s = flw_utf8(L"h\u00e9");
    CHECK(streq(s, "h\xc3\xa9"));
    free(s);
    s = flw_utf8(pair);
    CHECK(streq(s, "\xf0\x9f\x98\x80"));
    free(s);
    CHECK(flw_utf8(lone) == NULL);
    CHECK(flw_utf8n(L"x", (size_t)INT_MAX) == NULL);
    fail_alloc(1);
    CHECK(flw_utf8(L"x") == NULL);

    w = flw_utf16("");
    CHECK(w != NULL && w[0] == L'\0');
    free(w);
    w = flw_utf16("h\xc3\xa9");
    CHECK(w != NULL && wcscmp(w, L"h\u00e9") == 0);
    free(w);
    CHECK(flw_utf16("\xff") == NULL);
    CHECK(flw_utf16n("x", (size_t)INT_MAX) == NULL);
    fail_alloc(1);
    CHECK(flw_utf16("x") == NULL);

    fail_alloc(1);
    CHECK(flw_strdup("x") == NULL);
    CHECK(streq(flw_basename("C:\\a/b\\c.exe"), "c.exe"));
    CHECK(streq(flw_basename("plain"), "plain"));

    CHECK(flw_is_secret_name("http.extraHeader"));
    CHECK(flw_is_secret_name("my_token"));
    CHECK(flw_is_secret_name("PASSWORD"));
    CHECK(!flw_is_secret_name("user.name"));
    CHECK(!flw_is_secret_name("TOKE"));
}

static void t_getenv(void)
{
    char *v;
    SetEnvironmentVariableW(L"FLU_UNSET", NULL);
    CHECK(flw_getenv(L"FLU_UNSET") == NULL);
    SetEnvironmentVariableW(L"FLU_EMPTY", L"");
    v = flw_getenv(L"FLU_EMPTY");
    CHECK(streq(v, ""));
    free(v);
    SetEnvironmentVariableW(L"FLU_SET", L"v\u00e9");
    v = flw_getenv(L"FLU_SET");
    CHECK(streq(v, "v\xc3\xa9"));
    free(v);
    fail_alloc(1);
    CHECK(flw_getenv(L"FLU_SET") == NULL);
}

static void t_buf(void)
{
    struct flw_buf b = { 0 };
    char big[300];
    flw_buf_put(&b, "x", 0);
    CHECK(b.p == NULL);
    memset(big, 'z', sizeof(big));
    flw_buf_put(&b, big, sizeof(big)); /* grows past the first 256 bytes */
    CHECK(b.n == sizeof(big) && b.cap >= sizeof(big) + 1);
    flw_buf_free(&b);

    flw_buf_putu(&b, 0);
    flw_buf_put(&b, " ", 1);
    flw_buf_putu(&b, ULLONG_MAX);
    flw_buf_put(&b, " ", 1);
    flw_buf_puti(&b, -42);
    flw_buf_put(&b, " ", 1);
    flw_buf_puti(&b, LLONG_MIN);
    flw_buf_put(&b, " ", 1);
    flw_buf_puti(&b, 7);
    CHECK(streq(b.p, "0 18446744073709551615 -42 -9223372036854775808 7"));
    flw_buf_free(&b);

    flw_buf_puts(&b, "a");
    flw_buf_put(&b, "b", SIZE_MAX); /* would overflow: oom, nothing read */
    CHECK(b.oom);
    flw_buf_puts(&b, "c"); /* no-op once oom */
    CHECK(streq(b.p, "a"));
    flw_buf_free(&b);
    CHECK(b.p == NULL && b.n == 0 && b.cap == 0 && b.oom == 0);

    fail_alloc(1);
    flw_buf_puts(&b, "x");
    CHECK(b.oom && b.p == NULL);
    flw_buf_free(&b);
}

static void t_json(void)
{
    CHECK(json_is("", ""));
    CHECK(json_is("a\"b\\c", "a\\\"b\\\\c"));
    CHECK(json_is("\n\t\r\x01\x1f\x7f", "\\n\\t\\r\\u0001\\u001f\\u007f"));
    CHECK(json_is("\xc3\xa9", "\xc3\xa9"));
    CHECK(json_is("\xe0\xa0\x80", "\xe0\xa0\x80"));
    CHECK(json_is("\xe2\x82\xac", "\xe2\x82\xac"));
    CHECK(json_is("\xed\x9f\xbf", "\xed\x9f\xbf"));
    CHECK(json_is("\xf0\x90\x80\x80", "\xf0\x90\x80\x80"));
    CHECK(json_is("\xf3\xbf\xbf\xbf", "\xf3\xbf\xbf\xbf"));
    CHECK(json_is("\xf4\x8f\xbf\xbf", "\xf4\x8f\xbf\xbf"));
    /* invalid: each bad byte becomes U+FFFD and the scan resumes after it */
    CHECK(json_is("\x80", "\\ufffd"));
    CHECK(json_is("\xc0\xaf", "\\ufffd\\ufffd"));
    CHECK(json_is("\xe0\x80\x80", "\\ufffd\\ufffd\\ufffd"));
    CHECK(json_is("\xed\xa0\x80", "\\ufffd\\ufffd\\ufffd"));
    CHECK(json_is("\xf0\x80\x80\x80", "\\ufffd\\ufffd\\ufffd\\ufffd"));
    CHECK(json_is("\xf4\x90\x80\x80", "\\ufffd\\ufffd\\ufffd\\ufffd"));
    CHECK(json_is("\xf5", "\\ufffd"));
    CHECK(json_is("\xe2\x82", "\\ufffd\\ufffd"));
    CHECK(json_is("\xe2\x82x", "\\ufffd\\ufffdx"));
    CHECK(json_is("\xf0\x9f\x98", "\\ufffd\\ufffd\\ufffd"));
}

static void t_random(void)
{
    uint8_t a[16];
    uint8_t z[16];
    memset(a, 0, sizeof(a));
    memset(z, 0, sizeof(z));
    CHECK(flw_random(a, sizeof(a)) == 0);
    CHECK(memcmp(a, z, sizeof(a)) != 0);
    CHECK(flw_random(a, (size_t)ULONG_MAX + 1u) == -1);
}

static void t_paths(void)
{
    char out[4096];
    char *s;
    static char longp[40000];

    CHECK(flw_have_wine());
    CHECK(flw_to_unix_cb(NULL, "C:\\windows", out, 0) == -1);
    CHECK(flw_to_unix_cb(NULL, "C:\\\xff", out, sizeof(out)) == -1);
    CHECK(flw_to_unix_cb(NULL, "", out, sizeof(out)) == -1);
    CHECK(flw_to_unix_cb(NULL, "C:\\windows", out, sizeof(out)) == 0);
    CHECK(out[0] == '/' && ends(out, "/windows"));
    CHECK(flw_to_unix_cb(NULL, "C:\\windows", out, 4) == -1);
    CHECK(flw_to_unix_cb(NULL, "C:\\no-such-dir-fl\\a\\b.txt", out, sizeof(out)) == 0);
    CHECK(ends(out, "/no-such-dir-fl/a/b.txt"));
    CHECK(flw_to_unix_cb(NULL, "C:\\no-such-dir-fl\\a\\b.txt", out, 24) == -1);
    CHECK(flw_to_unix_cb(NULL, "C:\\windows\\no-such-fl", out, 8) == -1);
    CHECK(flw_to_unix_cb(NULL, "\\\\?\\C:\\windows\\\\\\no-such-fl", out, sizeof(out)) == 0);
    CHECK(ends(out, "/windows/no-such-fl"));
    CHECK(flw_to_unix_cb(NULL, "Z:\\", out, sizeof(out)) == 0);
    CHECK(streq(out, "/"));
    CHECK(flw_to_unix_cb(NULL, "Z:\\no-such-root-fl", out, sizeof(out)) == 0);
    CHECK(streq(out, "/no-such-root-fl"));
    CHECK(flw_to_unix_cb(NULL, "Z:\\tmp\\\\x", out, sizeof(out)) == 0);
    CHECK(flw_to_unix_cb(NULL, "\\\\no-such-server\\share\\x", out, sizeof(out)) == -1);
    memset(longp, 'a', sizeof(longp) - 1);
    longp[0] = 'C';
    longp[1] = ':';
    longp[2] = '\\';
    CHECK(flw_to_unix_cb(NULL, longp, out, sizeof(out)) == -1);
    fail_alloc(2); /* the tail's UTF-8 copy */
    CHECK(flw_to_unix_cb(NULL, "C:\\windows\\no-such-fl", out, sizeof(out)) == -1);
    fail_alloc(2); /* GetFullPathNameW's buffer */
    CHECK(flw_to_unix_cb(NULL, "C:\\windows", out, sizeof(out)) == -1);

    CHECK(flw_to_win_cb(NULL, "relative", out, sizeof(out)) == -1);
    CHECK(flw_to_win_cb(NULL, "/tmp", out, sizeof(out)) == 0);
    CHECK(streq(out, "Z:/tmp"));
    CHECK(flw_to_win_cb(NULL, "/tmp", out, 4) == -1);
    fail_alloc(1);
    CHECK(flw_to_win_cb(NULL, "/tmp", out, sizeof(out)) == -1);

    s = flw_path_to_unix("C:\\windows");
    CHECK(s != NULL && ends(s, "/windows"));
    free(s);
    CHECK(flw_path_to_unix("\\\\no-such-server\\share") == NULL);
    fail_alloc(1);
    CHECK(flw_path_to_unix("C:\\windows") == NULL);
}

static void t_norm(void)
{
    char *s;
    s = flw_norm_win("c:\\a\\..\\b\\", NULL);
    CHECK(streq(s, "C:/b"));
    free(s);
    s = flw_norm_win("sub\\x", "C:\\base");
    CHECK(streq(s, "C:/base/sub/x"));
    free(s);
    s = flw_norm_win("D:\\abs", "C:\\base");
    CHECK(streq(s, "D:/abs"));
    free(s);
    s = flw_norm_win("\\rooted", "C:\\base");
    CHECK(s != NULL && ends(s, ":/rooted"));
    free(s);
    s = flw_norm_win("d:rel", "C:\\base");
    CHECK(s != NULL && starts(s, "D:/"));
    free(s);
    s = flw_norm_win("\\\\?\\C:\\x\\", NULL);
    CHECK(streq(s, "C:/x"));
    free(s);
    s = flw_norm_win("\\\\?\\C:", NULL);
    CHECK(streq(s, "C:/"));
    free(s);
    s = flw_norm_win("C:\\", NULL);
    CHECK(streq(s, "C:/"));
    free(s);
    CHECK(flw_norm_win("\\\\server\\share\\x", NULL) == NULL);
    CHECK(flw_norm_win("\\\\?\\unix\\tmp", NULL) == NULL);
    CHECK(flw_norm_win("\xff", NULL) == NULL);
    CHECK(flw_norm_win("x", "\xff") == NULL);
    CHECK(flw_norm_win("", NULL) == NULL);
    fail_alloc(3); /* the joined path */
    CHECK(flw_norm_win("x", "C:\\base") == NULL);
    fail_alloc(3); /* the UTF-8 result */
    CHECK(flw_norm_win("C:\\x", NULL) == NULL);
    fail_alloc(4); /* growing "C:" to "C:/" */
    CHECK(flw_norm_win("\\\\?\\C:", NULL) == NULL);

    s = flw_cwd();
    CHECK(s != NULL && s[1] == ':');
    free(s);
    fail_alloc(1);
    CHECK(flw_cwd() == NULL);
    s = flw_module_path();
    CHECK(s != NULL && ends(s, ".exe"));
    free(s);
    fail_alloc(1);
    CHECK(flw_module_path() == NULL);
}

static void setenv8(const wchar_t *name, const wchar_t *value)
{
    SetEnvironmentVariableW(name, value);
}

static int cfg_fails(const wchar_t *name, const wchar_t *value, int need_winexec, const char *needle)
{
    struct flw_cfg c;
    const char *got;
    int rc;
    setenv8(name, value);
    cap_begin();
    rc = flw_load_cfg(&c, need_winexec);
    got = cap_end();
    if (rc == 0) {
        flw_cfg_clear(&c);
    }
    return rc == -1 && strstr(got, needle) != NULL;
}

static void t_cfg(void)
{
    static const wchar_t tok[] = L"00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff";
    struct flw_cfg c;
    struct fl_xlate x;

    setenv8(L"FL_BRIDGE_TOKEN", tok);
    setenv8(L"FL_BRIDGE_WINEXEC", L"/usr/bin/true");
    CHECK(cfg_fails(L"FL_BRIDGE_PORT", NULL, 0, "FL_BRIDGE_PORT is not set"));
    CHECK(cfg_fails(L"FL_BRIDGE_PORT", L"", 0, "FL_BRIDGE_PORT is not set"));
    CHECK(cfg_fails(L"FL_BRIDGE_PORT", L"123456", 0, "not a TCP port number: '123456'"));
    CHECK(cfg_fails(L"FL_BRIDGE_PORT", L"12a", 0, "not a TCP port"));
    CHECK(cfg_fails(L"FL_BRIDGE_PORT", L"0", 0, "not a TCP port"));
    CHECK(cfg_fails(L"FL_BRIDGE_PORT", L"65536", 0, "not a TCP port"));
    setenv8(L"FL_BRIDGE_PORT", L"65535");
    CHECK(cfg_fails(L"FL_BRIDGE_TOKEN", NULL, 0, "FL_BRIDGE_TOKEN is not set"));
    CHECK(cfg_fails(L"FL_BRIDGE_TOKEN", L"", 0, "FL_BRIDGE_TOKEN is not set"));
    CHECK(cfg_fails(L"FL_BRIDGE_TOKEN", L"abc", 0, "must be 64 hex characters"));
    CHECK(cfg_fails(L"FL_BRIDGE_TOKEN", L"zz112233445566778899aabbccddeeff00112233445566778899aabbccddeeff", 0,
                    "must be 64 hex characters"));
    setenv8(L"FL_BRIDGE_TOKEN", tok);
    CHECK(cfg_fails(L"FL_BRIDGE_WINEXEC", L"", 1, "FL_BRIDGE_WINEXEC must be"));
    CHECK(cfg_fails(L"FL_BRIDGE_WINEXEC", NULL, 1, "FL_BRIDGE_WINEXEC must be"));
    CHECK(cfg_fails(L"FL_BRIDGE_WINEXEC", L"relative", 0, "FL_BRIDGE_WINEXEC must be"));

    setenv8(L"FL_BRIDGE_WINEXEC", NULL);
    setenv8(L"FL_BRIDGE_ASKPASS", L"");
    setenv8(L"FL_BRIDGE_SSH_ASKPASS", L"");
    CHECK(flw_load_cfg(&c, 0) == 0);
    CHECK(c.port == 65535 && c.key[0] == 0x00 && c.key[1] == 0x11 && c.key[31] == 0xff);
    CHECK(c.winexec == NULL && c.askpass == NULL && c.ssh_askpass == NULL);
    flw_cfg_clear(&c);

    setenv8(L"FL_BRIDGE_WINEXEC", L"/w");
    setenv8(L"FL_BRIDGE_ASKPASS", L"/a");
    setenv8(L"FL_BRIDGE_SSH_ASKPASS", L"/s");
    CHECK(flw_load_cfg(&c, 1) == 0);
    flw_xlate_init(&x, &c);
    CHECK(streq(x.winexec, "/w") && streq(x.askpass, "/a") && streq(x.ssh_askpass, "/s"));
    CHECK(x.to_unix == flw_to_unix_cb && x.to_win == flw_to_win_cb && x.ud == NULL);
    flw_cfg_clear(&c);
    CHECK(c.winexec == NULL && c.key[0] == 0);
}

static void t_environ(void)
{
    static const wchar_t lone[] = { L'x', 0xDC00, 0 };
    char **v;
    size_t n = 0;
    int saw_set = 0;
    int saw_bridge = 0;
    int saw_lone = 0;

    flw_free_strv(NULL, 0);
    SetEnvironmentVariableW(L"FLU_KEEP", L"1");
    SetEnvironmentVariableW(L"FL_BRIDGE_SECRET_FLU", L"1");
    SetEnvironmentVariableW(L"FLU_LONE", lone);
    v = flw_environ(&n);
    CHECK(v != NULL);
    CHECK(v == NULL || v[n] == NULL);
    for (size_t i = 0; v != NULL && i < n; i++) {
        saw_set |= strcmp(v[i], "FLU_KEEP=1") == 0;
        saw_bridge |= starts(v[i], "FL_BRIDGE_");
        saw_lone |= starts(v[i], "FLU_LONE=");
        CHECK(v[i][0] != '=');
    }
    CHECK(saw_set && !saw_bridge && !saw_lone);
    flw_free_strv(v, n);
    SetEnvironmentVariableW(L"FLU_LONE", NULL);
    fail_alloc(1);
    CHECK(flw_environ(&n) == NULL);
}

static int args_json_is(size_t argc, char *const *argv, const char *want)
{
    struct flw_buf b = { 0 };
    int ok;
    flw_buf_put(&b, "#", 1); /* allocated up front: injected failures hit the code under test */
    flw_buf_json_args(&b, argc, argv);
    ok = b.p != NULL && streq(b.p + 1, want);
    if (!ok) {
        printf("  args got %s want #%s\n", b.p != NULL ? b.p : "(null)", want);
    }
    flw_buf_free(&b);
    return ok;
}

static void t_log_json(void)
{
    static char *const a1[] = { "git", "-c", "http.extraHeader=Bearer x", "-c", "user.name=bob", "-c",
                                "a=hunter-token", "-c", "noeq", "https://u:p@h/x?y", "http://h/x", "x://", "" };
    static char *const a2[] = { "-c", "user.token=x" };
    struct flw_buf b = { 0 };
    CHECK(args_json_is(sizeof(a1) / sizeof(a1[0]), a1,
                       "[\"git\",\"-c\",\"http.extraHeader=<redacted>\",\"-c\",\"user.name=bob\",\"-c\","
                       "\"a=<redacted>\",\"-c\",\"noeq\",\"https://<redacted>@h/x?y\",\"http://h/x\",\"x://\",\"\"]"));
    fail_alloc(3); /* the key copy (1: the "#" of args_json_is, 2: the "-c" copy) */
    CHECK(args_json_is(2, a2, "[\"-c\",\"<redacted>\"]"));
    fail_alloc(4); /* the "key=<redacted>" text */
    CHECK(args_json_is(2, a2, "[\"-c\",\"<redacted>\"]"));
    fail_alloc(2); /* the redacted copy of a plain argument */
    CHECK(args_json_is(1, a2, "[\"<redacted>\"]"));

    flw_buf_json_env_value(&b, "GIT_ASKPASS_TOKEN", "x");
    flw_buf_json_env_value(&b, "X", "my-password");
    flw_buf_json_env_value(&b, "U", "ssh://me:pw@h/r");
    CHECK(streq(b.p, "\"<redacted>\"\"<redacted>\"\"ssh://<redacted>@h/r\""));
    flw_buf_free(&b);

    flw_buf_timestamp(&b);
    CHECK(b.n == 26 && b.p[0] == '"' && b.p[5] == '-' && b.p[11] == 'T' && b.p[20] == '.' && b.p[24] == 'Z');
    flw_buf_free(&b);
}

static const char *stdio_kind(HANDLE h)
{
    static char got[128];
    struct flw_buf b = { 0 };
    HANDLE saved = GetStdHandle(STD_INPUT_HANDLE);
    SetStdHandle(STD_INPUT_HANDLE, h);
    flw_buf_json_stdio(&b);
    SetStdHandle(STD_INPUT_HANDLE, saved);
    got[0] = '\0';
    if (b.p != NULL && b.n < sizeof(got)) {
        memcpy(got, b.p, b.n + 1);
    }
    flw_buf_free(&b);
    return got;
}

static void t_stdio(void)
{
    HANDLE ev = CreateEventW(NULL, TRUE, FALSE, NULL);
    HANDLE con = CreateFileW(L"NUL", GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE, NULL, OPEN_EXISTING, 0, NULL);
    HANDLE file = CreateFileW(L"C:\\windows\\win.ini", GENERIC_READ, FILE_SHARE_READ, NULL, OPEN_EXISTING, 0, NULL);
    HANDLE r;
    HANDLE w;
    CHECK(starts(stdio_kind(NULL), "{\"stdin\":\"none\""));
    CHECK(starts(stdio_kind(INVALID_HANDLE_VALUE), "{\"stdin\":\"none\""));
    CHECK(starts(stdio_kind(ev), "{\"stdin\":\"unknown\""));
    CHECK(starts(stdio_kind(con), "{\"stdin\":\"char\""));
    CHECK(starts(stdio_kind(file), "{\"stdin\":\"disk\""));
    if (CreatePipe(&r, &w, NULL, 0)) {
        CHECK(starts(stdio_kind(r), "{\"stdin\":\"pipe\""));
        CloseHandle(r);
        CloseHandle(w);
    }
    CloseHandle(ev);
    CloseHandle(con);
    CloseHandle(file);
}

static void t_log_append(const char *dir_unix)
{
    struct flw_buf b = { 0 };
    HANDLE h;
    DWORD n = 0;
    char got[64];

    flw_buf_puts(&b, "x");
    flw_log_append(NULL, &b);
    flw_log_append("", &b);
    CHECK(b.n == 1);
    b.oom = 1;
    flw_log_append("C:\\never-written-fl.log", &b);
    flw_buf_free(&b);
    CHECK(GetFileAttributesW(L"C:\\never-written-fl.log") == INVALID_FILE_ATTRIBUTES);

    flw_buf_puts(&b, "first");
    flw_log_append("C:\\no-such-dir-fl\\x.log", &b); /* cannot be created: ignored */
    flw_buf_free(&b);
    flw_buf_puts(&b, "\xff"); /* a Windows path that is not UTF-8: ignored */
    flw_log_append("C:\\\xff.log", &b);
    flw_buf_free(&b);

    DeleteFileW(L"C:\\fl-unit.log");
    flw_buf_puts(&b, "one");
    flw_log_append("C:\\fl-unit.log", &b);
    flw_buf_free(&b);
    if (dir_unix != NULL) {
        struct flw_buf path = { 0 };
        flw_buf_puts(&path, dir_unix);
        flw_buf_puts(&path, "/fl-unit.log");
        flw_buf_puts(&b, "two");
        flw_log_append(path.p, &b);
        flw_buf_free(&b);
        flw_buf_free(&path);
    }
    h = CreateFileW(L"C:\\fl-unit.log", GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE, NULL, OPEN_EXISTING, 0, NULL);
    CHECK(h != INVALID_HANDLE_VALUE);
    memset(got, 0, sizeof(got));
    ReadFile(h, got, (DWORD)sizeof(got) - 1, &n, NULL);
    CloseHandle(h);
    CHECK(streq(got, "one\n"));
}

static void t_transport_local(void)
{
    struct fl_req r;
    char *argv[1] = { "" };
    const char *got;
    memset(&r, 0, sizeof(r));
    CHECK(flw_send_frame(99, NULL, 0) == -1); /* invalid type: rejected before any I/O */
    r.cwd = "/";
    r.argc = 1;
    r.argv = argv; /* empty argv[0]: not encodable */
    cap_begin();
    CHECK(flw_send_req(&r) == -1);
    got = cap_end();
    CHECK(strstr(got, "cannot encode the request (invalid entry, or larger than 1048576 bytes)") != NULL);
    CHECK(flw_connect(1) == -1); /* nothing listens on port 1 */
}

static void t_xlate(void)
{
    static char *const in[] = { "-x", "C:\\windows", "C:\\windows\\no-such-fl" };
    struct flw_cfg c;
    struct fl_xlate x;
    struct fl_strvec out = { 0, 0, NULL };
    struct fl_envops ops;
    memset(&c, 0, sizeof(c));
    flw_xlate_init(&x, &c);
    CHECK(flw_xlate_args(&x, 3, in, &out) == 0);
    CHECK(out.n == 3 && streq(out.v[0], "-x") && out.v[1][0] == '/' && ends(out.v[2], "/no-such-fl"));
    fl_strvec_free(&out);
    fail_alloc(1);
    CHECK(flw_xlate_args(&x, 3, in, &out) == -1);
    fl_strvec_free(&out);
    fail_alloc(2);
    CHECK(flw_xlate_args(&x, 3, in, &out) == -1);
    fl_strvec_free(&out);
    fail_alloc(3);
    CHECK(flw_xlate_args(&x, 3, in, &out) == -1);
    fl_strvec_free(&out);
    memset(&ops, 0, sizeof(ops));
    flw_translate_environ(&x, &ops);
    CHECK(ops.set.n > 0);
    fl_strvec_free(&ops.set);
    fl_strvec_free(&ops.unset);
}

static int run_pure(const char *dir_unix)
{
    t_messages();
    t_text();
    t_getenv();
    t_buf();
    t_json();
    t_random();
    t_paths();
    t_norm();
    t_cfg();
    t_environ();
    t_log_json();
    t_stdio();
    t_log_append(dir_unix);
    t_transport_local();
    t_xlate();
    printf("fl-win-unit: %d checks, %d failed\n", g_checks, g_failed);
    fflush(stdout);
    return g_failed > 0 ? 1 : 0;
}

/* ---- cases that end the process ------------------------------------------ */

static void hook(void *ud, UINT code)
{
    printf("hook %s %u\n", (const char *)ud, code);
    fflush(stdout);
}

static void run_exit_case(const char *name)
{
    static wchar_t lone[] = { L'a', 0xD800, 0 };
    wchar_t *wargv[2] = { L"ok", lone };
    struct fl_xlate x;
    struct flw_cfg c;
    struct fl_envops ops;
    struct flw_buf m = { 0 };
    flw_set_prog("unit");
    memset(&c, 0, sizeof(c));
    memset(&ops, 0, sizeof(ops));
    flw_xlate_init(&x, &c);
    if (strcmp(name, "exit-hook") == 0) {
        flw_set_exit_hook(hook, "ran");
        flw_exit(7);
    } else if (strcmp(name, "fail") == 0) {
        flw_fail("plain failure");
    } else if (strcmp(name, "fail-s") == 0) {
        flw_fail_s("pre<", "mid", ">post");
    } else if (strcmp(name, "fail-n") == 0) {
        flw_fail_n("n=", -12, ".");
    } else if (strcmp(name, "fail-buf") == 0) {
        flw_buf_puts(&m, "built");
        flw_fail_buf(&m);
    } else if (strcmp(name, "xmalloc") == 0) {
        fail_alloc(1);
        (void)flw_xmalloc(1);
    } else if (strcmp(name, "xcalloc") == 0) {
        fail_alloc(1);
        (void)flw_xcalloc(1, 1);
    } else if (strcmp(name, "xstrdup") == 0) {
        fail_alloc(1);
        (void)flw_xstrdup("x");
    } else if (strcmp(name, "args-ok") == 0) {
        char **v = flw_args_utf8(1, wargv);
        printf("args %s %d\n", v[0], v[1] == NULL);
        fflush(stdout);
        flw_exit(0);
    } else if (strcmp(name, "args-bad") == 0) {
        (void)flw_args_utf8(2, wargv);
    } else if (strcmp(name, "environ-oom") == 0) {
        fail_alloc(1);
        flw_translate_environ(&x, &ops);
    }
    printf("unknown case %s\n", name);
    fflush(stdout);
    ExitProcess(3);
}

int wmain(int argc, wchar_t **argv);

int wmain(int argc, wchar_t **argv)
{
    char name[64];
    char dir[1024];
    if (argc < 2 || WideCharToMultiByte(CP_UTF8, 0, argv[1], -1, name, (int)sizeof(name), NULL, NULL) <= 0) {
        printf("usage: fl-win-unit.exe pure [unix-dir] | <exit-case>\n");
        return 2;
    }
    if (strcmp(name, "pure") == 0) {
        int have_dir = argc >= 3 && WideCharToMultiByte(CP_UTF8, 0, argv[2], -1, dir, (int)sizeof(dir), NULL, NULL) > 0;
        return run_pure(have_dir ? dir : NULL);
    }
    run_exit_case(name);
    return 3;
}
