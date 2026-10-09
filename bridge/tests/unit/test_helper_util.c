/*
 * test_helper_util.c - native unit tests for the pure helpers of fl-bridge-helper
 * (bridge/unix/fl_helper_util.c): personas, token / number parsing, the HELLO and
 * HMAC handshake pieces, EXIT mapping, the SPAWN_OK encoder and the child
 * environment builder. Path translation lives in test_helper_paths.c.
 *
 * Build (any C11 compiler, ideally with -fsanitize=address,undefined):
 *   cc -std=c11 -Ibridge/common -Ibridge/unix bridge/tests/unit/test_helper_util.c \
 *      bridge/unix/fl_helper_util.c bridge/common/fl_proto.c bridge/common/fl_sha256.c
 * Exit status 0 when every check passes.
 */
#define _POSIX_C_SOURCE 200809L

#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <unistd.h>

#include "fl_helper_util.h"
#include "fl_proto.h"
#include "fl_sha256.h"

static int g_checks;
static int g_failures;

#define CHECK(cond)                                                                        \
    do {                                                                                   \
        g_checks++;                                                                        \
        if (!(cond)) {                                                                     \
            g_failures++;                                                                  \
            fprintf(stderr, "%s:%d: CHECK failed: %s\n", __FILE__, __LINE__, #cond);       \
        }                                                                                  \
    } while (0)

#define CHECK_STR(got, want)                                                               \
    do {                                                                                   \
        const char *g_ = (got);                                                            \
        const char *w_ = (want);                                                           \
        g_checks++;                                                                        \
        if (g_ == NULL || strcmp(g_, w_) != 0) {                                           \
            g_failures++;                                                                  \
            fprintf(stderr, "%s:%d: got \"%s\", want \"%s\"\n", __FILE__, __LINE__,        \
                    g_ ? g_ : "(null)", w_);                                               \
        }                                                                                  \
    } while (0)

static void hex32(const char *hex, uint8_t out[32])
{
    if (fl_hex_decode(hex, out, 32) != 0) {
        fprintf(stderr, "bad test vector %s\n", hex);
        exit(2);
    }
}

static void test_personas(void)
{
    CHECK(fl_persona_from_argv0(NULL) == FL_PERSONA_HELPER);
    CHECK(fl_persona_from_argv0("fl-bridge-helper") == FL_PERSONA_HELPER);
    CHECK(fl_persona_from_argv0("/usr/libexec/fork-linux/fl-winexec") == FL_PERSONA_WINEXEC);
    CHECK(fl_persona_from_argv0("Z:\\home\\u\\bin\\fl-askpass") == FL_PERSONA_ASKPASS);
    CHECK(fl_persona_from_argv0("fl-ssh-askpass.EXE") == FL_PERSONA_SSH_ASKPASS);
    CHECK(fl_persona_from_argv0("fl-winexec.exe") == FL_PERSONA_WINEXEC);
    CHECK(fl_persona_from_argv0("fl-winexec2") == FL_PERSONA_HELPER);
    CHECK(fl_persona_from_argv0("fl-winexec.ex") == FL_PERSONA_HELPER);
    CHECK(fl_persona_from_argv0("xfl-askpass") == FL_PERSONA_HELPER);
    CHECK(fl_persona_from_argv0("") == FL_PERSONA_HELPER);
    CHECK_STR(fl_basename_any("a/b\\c/d"), "d");
    CHECK_STR(fl_basename_any("C:\\x\\"), "");
    CHECK_STR(fl_basename_any("plain"), "plain");
}

static void test_log_sanitize(void)
{
    char a[] = "git\nresult=ok x\x7f\x01";
    char b[] = "git-upload-pack";
    char c[] = "";
    fl_log_sanitize(a);
    fl_log_sanitize(b);
    fl_log_sanitize(c);
    fl_log_sanitize(NULL);
    CHECK(strcmp(a, "git?result=ok?x??") == 0);
    CHECK(strcmp(b, "git-upload-pack") == 0);
    CHECK(c[0] == '\0');
}

static void test_token(void)
{
    static const char tok[] = "000102030405060708090a0b0c0d0e0f101112131415161718191A1B1C1D1E1F";
    char buf[80];
    uint8_t key[FL_KEY_LEN];
    uint8_t want[FL_KEY_LEN];
    for (unsigned i = 0; i < FL_KEY_LEN; i++) {
        want[i] = (uint8_t)i;
    }
    memset(key, 0xee, sizeof(key));
    CHECK(fl_token_parse(tok, 64, key) == 0 && memcmp(key, want, 32) == 0);
    snprintf(buf, sizeof(buf), "%s\n", tok);
    CHECK(fl_token_parse(buf, 65, key) == 0 && memcmp(key, want, 32) == 0);
    snprintf(buf, sizeof(buf), "%s\r\n", tok);
    CHECK(fl_token_parse(buf, 66, key) == 0);
    snprintf(buf, sizeof(buf), "%s\n\n", tok);
    CHECK(fl_token_parse(buf, 66, key) != 0);
    snprintf(buf, sizeof(buf), "%s ", tok);
    CHECK(fl_token_parse(buf, 65, key) != 0);
    snprintf(buf, sizeof(buf), "%s00", tok);
    CHECK(fl_token_parse(buf, 66, key) != 0);
    CHECK(fl_token_parse(tok, 63, key) != 0);
    CHECK(fl_token_parse(NULL, 64, key) != 0);
    memcpy(buf, tok, 64);
    buf[10] = 'g';
    CHECK(fl_token_parse(buf, 64, key) != 0);
}

static void test_numbers(void)
{
    uint16_t port = 1;
    int32_t pid = 0;
    CHECK(fl_parse_port("0", &port) == 0 && port == 0);
    CHECK(fl_parse_port("65535", &port) == 0 && port == 65535);
    CHECK(fl_parse_port("65536", &port) != 0);
    CHECK(fl_parse_port("", &port) != 0);
    CHECK(fl_parse_port("-1", &port) != 0);
    CHECK(fl_parse_port("+80", &port) != 0);
    CHECK(fl_parse_port("80 ", &port) != 0);
    CHECK(fl_parse_port("000080", &port) != 0);
    CHECK(fl_parse_port(NULL, &port) != 0);
    CHECK(fl_parse_pid("1", &pid) == 0 && pid == 1);
    CHECK(fl_parse_pid("2147483647", &pid) == 0 && pid == 2147483647);
    CHECK(fl_parse_pid("2147483648", &pid) != 0);
    CHECK(fl_parse_pid("99999999999", &pid) != 0);
    CHECK(fl_parse_pid("0", &pid) != 0);
    CHECK(fl_parse_pid("12a", &pid) != 0);
}

static void test_handshake_pieces(void)
{
    uint8_t key[32], cn[32], sn[32], mac[32], want[32], hello[FL_HELLO_LEN], got[32];
    for (unsigned i = 0; i < 32; i++) {
        key[i] = (uint8_t)i;
    }
    memset(cn, 0xaa, sizeof(cn));
    memset(sn, 0x55, sizeof(sn));
    /* Vectors from Python: hmac.new(bytes(range(32)), b"fl-bridge-v1|<side>|" + cn + sn, sha256). */
    fl_auth_mac(key, 1, cn, sn, mac);
    hex32("fe94f25b9aad26971e0e02bf72d927a6a4d0d3b2e75364a32167d3021e90d85c", want);
    CHECK(memcmp(mac, want, 32) == 0);
    fl_auth_mac(key, 0, cn, sn, mac);
    hex32("f5e40d08c2970239878edf77b111e20d2a7fc45da11b442b7ebcb45145af68e2", want);
    CHECK(memcmp(mac, want, 32) == 0);

    fl_hello_build(hello, cn);
    CHECK(memcmp(hello, "FLB1\x00\x01", 6) == 0);
    memset(got, 0, sizeof(got));
    CHECK(fl_hello_parse(hello, sizeof(hello), got) == 0 && memcmp(got, cn, 32) == 0);
    CHECK(fl_hello_parse(hello, sizeof(hello) - 1, got) != 0);
    CHECK(fl_hello_parse(NULL, sizeof(hello), got) != 0);
    hello[5] = 2;
    CHECK(fl_hello_parse(hello, sizeof(hello), got) != 0);
    hello[5] = 1;
    hello[0] = 'X';
    CHECK(fl_hello_parse(hello, sizeof(hello), got) != 0);
}

static int status_of(int how)
{
    int st = 0;
    pid_t pid = fork();
    if (pid == 0) {
        if (how >= 0) {
            _exit(how);
        }
        raise(-how);
        _exit(99);
    }
    waitpid(pid, &st, 0);
    return st;
}

static void test_exit_mapping(void)
{
    int kind = -1;
    int32_t code = -1;
    fl_wait_to_exit(status_of(42), &kind, &code);
    CHECK(kind == 0 && code == 42);
    fl_wait_to_exit(status_of(0), &kind, &code);
    CHECK(kind == 0 && code == 0);
    fl_wait_to_exit(status_of(-SIGTERM), &kind, &code);
    CHECK(kind == 1 && code == SIGTERM);
    fl_wait_to_exit(status_of(-SIGKILL), &kind, &code);
    CHECK(kind == 1 && code == SIGKILL);
    CHECK(fl_signal_allowed(SIGINT) && fl_signal_allowed(SIGTERM) && fl_signal_allowed(SIGKILL) &&
          fl_signal_allowed(SIGHUP));
    CHECK(!fl_signal_allowed(SIGSTOP) && !fl_signal_allowed(SIGUSR1) && !fl_signal_allowed(SIGPIPE) &&
          !fl_signal_allowed(0) && !fl_signal_allowed(255));
}

static void test_spawn_ok(void)
{
    char *paths[] = { "/home/u/repo", NULL, "" , "/x" };
    uint8_t *out = NULL;
    size_t n = 0;
    static const uint8_t want[] = { 0x00, 0x01, 0x02, 0x03, '/', 'h', 'o', 'm', 'e', '/', 'u', '/', 'r', 'e', 'p', 'o', 0,
                                    0, 0, '/', 'x', 0 };
    CHECK(fl_spawn_ok_encode(0x010203u, paths, 4, FL_MAX_REQ, &out, &n) == 0);
    CHECK(n == sizeof(want) && out != NULL && memcmp(out, want, n) == 0);
    free(out);
    out = NULL;
    CHECK(fl_spawn_ok_encode(7u, NULL, 0, FL_MAX_REQ, &out, &n) == 0 && n == 4 && out[3] == 7);
    free(out);
    CHECK(fl_spawn_ok_encode(7u, paths, 4, sizeof(want) - 1, &out, &n) != 0);
    CHECK(fl_spawn_ok_encode(7u, paths, 1, 4, &out, &n) != 0);
}

static void test_env(void)
{
    char *base[] = { "HOME=/home/u", "PATH=/usr/bin:/bin", "TMPDIR=/tmp/u", "LANG=C.UTF-8", "KEEP=1",
                     "DROP=2", "FL_BRIDGE_TOKEN=secret", "NOVALUE", "=C:=junk", NULL };
    char *set[] = { "KEEP=3", "NEW=4", "HOME=C:\\users\\u", "PATH=C:\\Windows", "TMPDIR=C:\\t",
                    "FL_BRIDGE_TOKEN=x", "=bad", "noeq", "NEW=5", "LANG=de_DE.UTF-8", "EMPTY=" };
    char *unset[] = { "DROP", "HOME", "PATH", "LANG", "TMPDIR", "MISSING", "bad=key", "" };
    char **env = NULL;
    size_t ignored = 0;
    size_t n = 0;
    CHECK(fl_env_build(base, set, sizeof(set) / sizeof(set[0]), unset, sizeof(unset) / sizeof(unset[0]), &env,
                       &ignored) == 0);
    while (env && env[n]) {
        n++;
    }
    CHECK_STR(fl_env_get(env, "HOME"), "/home/u");
    CHECK_STR(fl_env_get(env, "PATH"), "/usr/bin:/bin");
    CHECK_STR(fl_env_get(env, "TMPDIR"), "/tmp/u");
    CHECK_STR(fl_env_get(env, "LANG"), "de_DE.UTF-8");
    CHECK_STR(fl_env_get(env, "KEEP"), "3");
    CHECK_STR(fl_env_get(env, "NEW"), "5");
    CHECK_STR(fl_env_get(env, "EMPTY"), "");
    CHECK(fl_env_get(env, "DROP") == NULL);
    CHECK(fl_env_get(env, "FL_BRIDGE_TOKEN") == NULL);
    CHECK(fl_env_get(env, "NOVALUE") == NULL);
    CHECK(fl_env_get(env, "") == NULL);
    /* HOME PATH TMPDIR LANG KEEP NEW EMPTY */
    CHECK(n == 7);
    /* sets: HOME PATH TMPDIR FL_BRIDGE_TOKEN "=bad" "noeq"; unsets: HOME PATH LANG TMPDIR "bad=key" "" */
    CHECK(ignored == 12);
    fl_env_free(env);

    env = NULL;
    CHECK(fl_env_build(NULL, NULL, 0, NULL, 0, &env, NULL) == 0 && env != NULL && env[0] == NULL);
    fl_env_free(env);
    fl_env_free(NULL);
    CHECK(fl_env_get(NULL, "X") == NULL);
    {
        char *e[] = { "PATHX=1", "PAT=2", "PATH=3", NULL };
        CHECK_STR(fl_env_get(e, "PATH"), "3");
    }
}

int main(void)
{
    test_personas();
    test_log_sanitize();
    test_token();
    test_numbers();
    test_handshake_pieces();
    test_exit_mapping();
    test_spawn_ok();
    test_env();
    if (g_failures) {
        fprintf(stderr, "test_helper_util: %d of %d checks failed\n", g_failures, g_checks);
        return 1;
    }
    printf("test_helper_util: %d checks passed\n", g_checks);
    return 0;
}
