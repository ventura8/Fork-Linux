/*
 * test_agent_relay.c - native unit tests of the ssh-agent relay (bridge/unix/fl_agent_relay.c):
 * the msys socket file text, the /proc/net/tcp owner lookup, the request filter, writing and
 * removing the socket file, and whole relay connections against a fake agent (a forked
 * child on a Unix socket), with the msys client's handshake played here.
 *
 * System-call failures are injected through the linker's --wrap (the wrappers below;
 * bridge/tests/meson.build); allocation failures through test_alloc.c. _FORTIFY_SOURCE would
 * route read() to __read_chk, around the wrap, so it is dropped before any header.
 * Built and run by meson (suite bridge-unit). Exit status 0 when every check passes.
 */
#undef _FORTIFY_SOURCE
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/random.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <sys/wait.h>
#include <sys/xattr.h>
#include <unistd.h>

/* The relay is compiled into this test (as test_helper_daemon.c does with the daemon): the
 * #undef above must reach it, or a distribution's -Wp,-D_FORTIFY_SOURCE=3 would route read()
 * to __read_chk, around the wrap. */
#include "fl_agent_relay.c"
#include "test_alloc.h"

static int g_checks;
static int g_failures;

#define CHECK(cond)                                                                                                \
    do {                                                                                                           \
        g_checks++;                                                                                                \
        if (!(cond)) {                                                                                             \
            g_failures++;                                                                                          \
            fprintf(stderr, "%s:%d: CHECK failed: %s\n", __FILE__, __LINE__, #cond);                               \
        }                                                                                                          \
    } while (0)

/* ------------------------------------------------------------------------ */
/* failure injection                                                         */
/* ------------------------------------------------------------------------ */

enum wrapped { W_SOCKET, W_LISTEN, W_GETRANDOM, W_WRITE, W_FSETXATTR, W_RENAME, W_READ, W_SEND, W_COUNT };

static int g_left[W_COUNT];
static int g_err[W_COUNT];

/* The nth call of fn from now (1 = the next one) fails with err (EINTR: once, then works). */
static void fail_at(enum wrapped fn, int nth, int err)
{
    g_left[fn] = nth;
    g_err[fn] = err;
}

static void reset(void)
{
    memset(g_left, 0, sizeof(g_left));
}

static int fails(enum wrapped fn)
{
    if (g_left[fn] <= 0 || --g_left[fn] > 0) {
        return 0;
    }
    errno = g_err[fn];
    return 1;
}

int __real_socket(int domain, int type, int protocol);
int __real_listen(int fd, int backlog);
ssize_t __real_getrandom(void *buf, size_t n, unsigned flags);
ssize_t __real_write(int fd, const void *buf, size_t n);
int __real_fsetxattr(int fd, const char *name, const void *value, size_t size, int flags);
int __real_rename(const char *a, const char *b);
ssize_t __real_read(int fd, void *buf, size_t n);
ssize_t __real_send(int fd, const void *buf, size_t n, int flags);
int __wrap_socket(int domain, int type, int protocol);
int __wrap_listen(int fd, int backlog);
ssize_t __wrap_getrandom(void *buf, size_t n, unsigned flags);
ssize_t __wrap_write(int fd, const void *buf, size_t n);
int __wrap_fsetxattr(int fd, const char *name, const void *value, size_t size, int flags);
int __wrap_rename(const char *a, const char *b);
ssize_t __wrap_read(int fd, void *buf, size_t n);
ssize_t __wrap_send(int fd, const void *buf, size_t n, int flags);

int __wrap_socket(int domain, int type, int protocol)
{
    return fails(W_SOCKET) ? -1 : __real_socket(domain, type, protocol);
}

int __wrap_listen(int fd, int backlog)
{
    return fails(W_LISTEN) ? -1 : __real_listen(fd, backlog);
}

ssize_t __wrap_getrandom(void *buf, size_t n, unsigned flags)
{
    if (fails(W_GETRANDOM)) {
        return errno == 0 ? 0 : -1;
    }
    return __real_getrandom(buf, n, flags);
}

ssize_t __wrap_write(int fd, const void *buf, size_t n)
{
    return fails(W_WRITE) ? -1 : __real_write(fd, buf, n);
}

/* 1 when the scratch directory's file system has no user extended attributes (some build
 * chroots): the relay's own check is then simulated as passing, and the attribute itself is
 * not looked at. */
static int g_no_xattr;

int __wrap_fsetxattr(int fd, const char *name, const void *value, size_t size, int flags)
{
    int rc;
    if (fails(W_FSETXATTR)) {
        return -1;
    }
    rc = __real_fsetxattr(fd, name, value, size, flags);
    if (rc != 0 && errno == EOPNOTSUPP) {
        g_no_xattr = 1;
        rc = 0;
    }
    return rc;
}

int __wrap_rename(const char *a, const char *b)
{
    return fails(W_RENAME) ? -1 : __real_rename(a, b);
}

ssize_t __wrap_read(int fd, void *buf, size_t n)
{
    return fails(W_READ) ? -1 : __real_read(fd, buf, n);
}

ssize_t __wrap_send(int fd, const void *buf, size_t n, int flags)
{
    return fails(W_SEND) ? -1 : __real_send(fd, buf, n, flags);
}

/* ------------------------------------------------------------------------ */
/* helpers                                                                   */
/* ------------------------------------------------------------------------ */

static char g_dir[1024];       /* private 0700 scratch directory */
static char g_file[PATH_MAX];  /* the socket file inside it */
static char g_agent[64];       /* the fake agent's socket */
static pid_t g_agent_pid = -1;

static void put_be32(uint8_t *p, uint32_t v)
{
    p[0] = (uint8_t)(v >> 24);
    p[1] = (uint8_t)(v >> 16);
    p[2] = (uint8_t)(v >> 8);
    p[3] = (uint8_t)v;
}

static int sendall(int fd, const void *buf, size_t n)
{
    return __real_send(fd, buf, n, MSG_NOSIGNAL) == (ssize_t)n ? 0 : -1;
}

static int recvall(int fd, void *buf, size_t n)
{
    size_t got = 0;
    while (got < n) {
        ssize_t k = recv(fd, (char *)buf + got, n - got, 0);
        if (k <= 0) {
            return -1;
        }
        got += (size_t)k;
    }
    return 0;
}

/* Send one agent message (length prefix added). */
static int send_msg(int fd, const uint8_t *body, uint32_t len)
{
    uint8_t hdr[4];
    put_be32(hdr, len);
    return sendall(fd, hdr, 4) == 0 && sendall(fd, body, len) == 0 ? 0 : -1;
}

/* Receive one agent message; its first body byte (the type), or -1. */
static int recv_type(int fd)
{
    uint8_t hdr[4];
    uint8_t body[64];
    uint32_t len;
    if (recvall(fd, hdr, 4) != 0) {
        return -1;
    }
    len = (uint32_t)hdr[0] << 24 | (uint32_t)hdr[1] << 16 | (uint32_t)hdr[2] << 8 | hdr[3];
    if (len == 0 || len > sizeof(body) || recvall(fd, body, len) != 0) {
        return -1;
    }
    return body[0];
}

/*
 * The fake agent: answers REQUEST_IDENTITIES with IDENTITIES_ANSWER (12, no keys), SIGN with
 * SIGN_RESPONSE (14), EXTENSION with SUCCESS (6); a request whose second byte is 0xEE makes it
 * close the connection without an answer, 0xEF makes it answer with a zero length.
 */
static void agent_conn(int c)
{
    uint8_t hdr[4];
    uint8_t body[512];
    for (;;) {
        uint32_t len;
        uint8_t answer[5] = { 0, 0, 0, 1, 0 };
        if (recvall(c, hdr, 4) != 0) {
            break;
        }
        len = (uint32_t)hdr[0] << 24 | (uint32_t)hdr[1] << 16 | (uint32_t)hdr[2] << 8 | hdr[3];
        if (len > sizeof(body) || recvall(c, body, len) != 0) {
            break;
        }
        if (len > 1 && body[1] == 0xEE) {
            break;
        }
        if (len > 1 && body[1] == 0xEF) {
            answer[3] = 0;
            (void)sendall(c, answer, 4);
            continue;
        }
        answer[4] = body[0] == 11 ? 12 : body[0] == 13 ? 14 : 6;
        (void)sendall(c, answer, sizeof(answer));
    }
    close(c);
}

static void start_agent(void)
{
    struct sockaddr_un a;
    int s = socket(AF_UNIX, SOCK_STREAM, 0);
    memset(&a, 0, sizeof(a));
    a.sun_family = AF_UNIX;
    snprintf(a.sun_path, sizeof(a.sun_path), "%s", g_agent);
    unlink(g_agent);
    if (s < 0 || bind(s, (struct sockaddr *)&a, sizeof(a)) != 0 || listen(s, 8) != 0) {
        perror("fake agent");
        exit(1);
    }
    g_agent_pid = fork();
    if (g_agent_pid == 0) {
        for (;;) {
            int c = accept(s, NULL, NULL);
            if (c < 0) {
                _exit(0);
            }
            if (fork() == 0) {
                close(s);
                agent_conn(c);
                _exit(0);
            }
            close(c);
        }
    }
    close(s);
}

static void stop_agent(void)
{
    if (g_agent_pid > 0) {
        kill(g_agent_pid, SIGKILL);
        waitpid(g_agent_pid, NULL, 0);
        g_agent_pid = -1;
    }
}

/* What a client does on the connection (the relay serves the other end in a child). */
typedef void (*client_fn)(int sock, const struct fl_agent_relay *r);

/* Called in run_conn after the accept, before the relay serves the connection. */
static void (*g_before_serve)(const struct sockaddr_in *peer, const struct fl_agent_relay *r);

struct outcome {
    char why[64];
    unsigned requests;
    unsigned refused;
};

/*
 * Connect to r, serve the accepted end with fl_agent_serve in a forked child and run client
 * on this end; the child's result in *out. The injected failures armed before apply to the
 * child (fork copies them); they are disarmed here afterwards.
 */
static void run_conn(const struct fl_agent_relay *r, client_fn client, struct outcome *out)
{
    struct sockaddr_in a;
    struct sockaddr_in peer;
    socklen_t plen = sizeof(peer);
    int cl = __real_socket(AF_INET, SOCK_STREAM | SOCK_CLOEXEC, 0);
    int pfd[2];
    int c;
    pid_t pid;
    memset(out, 0, sizeof(*out));
    memset(&a, 0, sizeof(a));
    a.sin_family = AF_INET;
    a.sin_port = htons((uint16_t)r->port);
    a.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    if (cl < 0 || connect(cl, (struct sockaddr *)&a, sizeof(a)) != 0 || pipe(pfd) != 0) {
        perror("run_conn");
        exit(1);
    }
    do {
        c = accept4(r->lst, (struct sockaddr *)&peer, &plen, SOCK_CLOEXEC);
    } while (c < 0 && errno == EAGAIN);
    if (g_before_serve != NULL) {
        g_before_serve(&peer, r);
    }
    pid = fork();
    if (pid == 0) {
        struct fl_agent_result res;
        struct outcome o;
        close(cl);
        close(pfd[0]);
        fl_agent_serve(r, c, &peer, &res);
        memset(&o, 0, sizeof(o));
        snprintf(o.why, sizeof(o.why), "%s", res.why);
        o.requests = res.requests;
        o.refused = res.refused;
        (void)__real_write(pfd[1], &o, sizeof(o));
        exit(0);
    }
    reset();
    close(c);
    close(pfd[1]);
    client(cl, r);
    close(cl);
    if (__real_read(pfd[0], out, sizeof(*out)) != (ssize_t)sizeof(*out)) {
        snprintf(out->why, sizeof(out->why), "(no result)");
    }
    close(pfd[0]);
    waitpid(pid, NULL, 0);
}

static void client_handshake(int s, const struct fl_agent_relay *r)
{
    uint8_t got[16];
    uint8_t cred[12] = { 0 };
    CHECK(sendall(s, r->secret, 16) == 0);
    CHECK(recvall(s, got, 16) == 0 && memcmp(got, r->secret, 16) == 0);
    CHECK(sendall(s, cred, 12) == 0);
    CHECK(recvall(s, cred, 12) == 0);
}

static int open_relay(struct fl_agent_relay *r)
{
    char why[256] = "";
    int rc = fl_agent_relay_open(r, g_file, g_agent, why, sizeof(why));
    if (rc != 0) {
        fprintf(stderr, "relay: %s\n", why);
    }
    return rc;
}

/* ------------------------------------------------------------------------ */
/* pure helpers                                                              */
/* ------------------------------------------------------------------------ */

static void test_socket_text(void)
{
    const uint8_t secret[16] = { 0x01, 0x02, 0x03, 0x04, 0xff, 0xee, 0xdd, 0xcc,
                                 0, 0, 0, 0x80, 0x10, 0x20, 0x30, 0x40 };
    char out[FL_AGENT_TEXT_MAX];
    static const char want[] = "!<socket >40123 s 04030201-ccddeeff-80000000-40302010";
    CHECK(fl_agent_socket_text(40123, secret, out, sizeof(out)) == sizeof(want));
    CHECK(strcmp(out, want) == 0);
    CHECK(fl_agent_socket_text(65535, secret, out, 20) == 0);
}

static void test_tcp_uid(void)
{
    static const char text[] =
        "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
        "   0: 0100007F:9C40 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 1 1\n"
        "   1: 0100007F:D2A4 0100007F:9C41 01 00000000:00000000 00:00000000 00000000  4242        0 2 1\n"
        "   2: 0100007F:D2A4 0100007F:9C40 01 00000000:00000000 00:00000000 00000000  1234        0 3 1";
    uint32_t uid = 0;
    uint32_t lo = htonl(INADDR_LOOPBACK);
    CHECK(fl_agent_tcp_uid(text, lo, 0xD2A4, 0x9C40, &uid) == 0 && uid == 1234);
    CHECK(fl_agent_tcp_uid(text, lo, 0xD2A4, 0x9C41, &uid) == 0 && uid == 4242);
    CHECK(fl_agent_tcp_uid(text, lo, 0xD2A5, 0x9C40, &uid) == -1);
    CHECK(fl_agent_tcp_uid(text, htonl(0x7F000002), 0xD2A4, 0x9C40, &uid) == -1);
    /* The listener's own line has a 0.0.0.0 remote: never the peer. */
    CHECK(fl_agent_tcp_uid(text, lo, 0x9C40, 0, &uid) == -1);
    CHECK(fl_agent_tcp_uid("", lo, 1, 2, &uid) == -1);
    /* Malformed lines never match. */
    CHECK(fl_agent_tcp_uid("   2: 0100007F:D2A4 0100007F:9C40 01", lo, 0xD2A4, 0x9C40, &uid) == -1);
    CHECK(fl_agent_tcp_uid("   2: 0100007FD2A4 0100007F:9C40 01 0:0 0:0 0 1234", lo, 0xD2A4, 0x9C40, &uid) == -1);
    CHECK(fl_agent_tcp_uid("   2: 0100007G:D2A4 0100007F:9C40 01 0:0 0:0 0 1234", lo, 0xD2A4, 0x9C40, &uid) == -1);
    CHECK(fl_agent_tcp_uid("   2: 0100007F:1D2A4 0100007F:9C40 01 0:0 0:0 0 1234", lo, 0xD2A4, 0x9C40, &uid) == -1);
    CHECK(fl_agent_tcp_uid("   2: 0100007F: 0100007F:9C40 01 0:0 0:0 0 1234", lo, 0xD2A4, 0x9C40, &uid) == -1);
    CHECK(fl_agent_tcp_uid("   2: 0100007F:D2A4 0100007F 01 0:0 0:0 0 1234", lo, 0xD2A4, 0x9C40, &uid) == -1);
    CHECK(fl_agent_tcp_uid("   2: 0100007F:D2A4 0100007F:9C40 01 0:0 0:0 0 -1234", lo, 0xD2A4, 0x9C40, &uid) == -1);
    CHECK(fl_agent_tcp_uid("   2: 0100007F:D2A4 0100007F:9C40 01 0:0 0:0 0 +1234", lo, 0xD2A4, 0x9C40, &uid) == -1);
    CHECK(fl_agent_tcp_uid("   2: 0100007F:D2A4 0100007F:9C40 01 0:0 0:0 0 12x4", lo, 0xD2A4, 0x9C40, &uid) == -1);
    CHECK(fl_agent_tcp_uid("   2: 0100007F:D2A4 0100007F:9C40 01 0:0 0:0 0 99999999999999999999999", lo, 0xD2A4,
                           0x9C40, &uid) == -1);
    CHECK(fl_agent_tcp_uid("   2: 0100007F:D2A4 0100007F:9C40 01 0:0 0:0 0 4294967296", lo, 0xD2A4, 0x9C40,
                           &uid) == -1);
    CHECK(fl_agent_tcp_uid("\t2:\t0100007F:D2A4 0100007F:9C40 01 0:0 0:0 0 77 0 3 1", lo, 0xD2A4, 0x9C40,
                           &uid) == 0 && uid == 77);
    CHECK(fl_agent_tcp_uid("   2: 0100007F:D2A4 0100007F:9C40 01 0:0 0:0 0 "
                           "777777777777777777777777777777777777777777", lo, 0xD2A4, 0x9C40, &uid) == -1);
}

static void test_request_filter(void)
{
    uint8_t ext[5 + 24 + 4] = { 27 };
    uint8_t add[] = { 17, 0, 0, 0, 0 };
    uint8_t other[5 + 5] = { 27, 0, 0, 0, 5, 'q', 'u', 'e', 'r', 'y' };
    put_be32(ext + 1, 24);
    memcpy(ext + 5, "session-bind@openssh.com", 24);
    CHECK(fl_agent_request_allowed((const uint8_t *)"\x0b", 1) == 1);
    CHECK(fl_agent_request_allowed((const uint8_t *)"\x0d\x00", 2) == 1);
    CHECK(fl_agent_request_allowed(ext, sizeof(ext)) == 1);
    CHECK(fl_agent_request_allowed(ext, 5 + 23) == 0);
    put_be32(ext + 1, 23);
    CHECK(fl_agent_request_allowed(ext, sizeof(ext)) == 0);
    put_be32(ext + 1, 24);
    ext[5] = 'S';
    CHECK(fl_agent_request_allowed(ext, sizeof(ext)) == 0);
    CHECK(fl_agent_request_allowed(other, sizeof(other)) == 0);
    CHECK(fl_agent_request_allowed(add, sizeof(add)) == 0);
    CHECK(fl_agent_request_allowed(add, 0) == 0);
    CHECK(fl_agent_request_allowed((const uint8_t *)"\x16", 1) == 0); /* LOCK */
}

/* ------------------------------------------------------------------------ */
/* the socket file                                                           */
/* ------------------------------------------------------------------------ */

static int open_fails(const char *file, const char *agent)
{
    struct fl_agent_relay r;
    char why[256] = "";
    int rc = fl_agent_relay_open(&r, file, agent, why, sizeof(why));
    CHECK(why[0] != '\0');
    CHECK(r.lst == -1);
    CHECK(access(g_file, F_OK) != 0);
    return rc;
}

static void test_open_refusals(void)
{
    char longpath[PATH_MAX + 8];
    char path[PATH_MAX];
    memset(longpath, 'a', sizeof(longpath) - 1);
    longpath[0] = '/';
    longpath[sizeof(longpath) - 1] = '\0';
    CHECK(open_fails(longpath, g_agent) == -1);
    CHECK(open_fails(g_file, "relative.sock") == -1);
    longpath[150] = '\0';
    CHECK(open_fails(g_file, longpath) == -1); /* longer than sun_path */
    snprintf(path, sizeof(path), "%s/missing.sock", g_dir);
    CHECK(open_fails(g_file, path) == -1);
    CHECK(open_fails(g_file, g_dir) == -1); /* not a socket */
    CHECK(open_fails("relative-file", g_agent) == -1);
    snprintf(path, sizeof(path), "%s/no/such/agent.sock", g_dir);
    CHECK(open_fails(path, g_agent) == -1);
    CHECK(open_fails("/tmp/fl-agent-test.sock", g_agent) == -1); /* not our directory */
    chmod(g_dir, 0755);
    CHECK(open_fails(g_file, g_agent) == -1);
    chmod(g_dir, 0700);
    snprintf(path, sizeof(path), "%s/notadir", g_dir);
    close(open(path, O_WRONLY | O_CREAT | O_CLOEXEC, 0600));
    snprintf(path, sizeof(path), "%s/notadir/agent.sock", g_dir);
    CHECK(open_fails(path, g_agent) == -1);
}

static void test_open_failures(void)
{
    static const struct {
        enum wrapped fn;
        int err;
    } cases[] = {
        { W_SOCKET, EMFILE }, { W_LISTEN, EADDRINUSE }, { W_GETRANDOM, ENOSYS }, { W_GETRANDOM, 0 },
        { W_WRITE, EIO },     { W_WRITE, 0 },           { W_FSETXATTR, ENOTSUP },
        { W_RENAME, EXDEV },
    };
    char tmp[PATH_MAX + 8];
    for (size_t i = 0; i < sizeof(cases) / sizeof(cases[0]); i++) {
        fail_at(cases[i].fn, 1, cases[i].err);
        if (open_fails(g_file, g_agent) != -1) {
            fprintf(stderr, "  open did not fail with failure case %zu\n", i);
            g_failures++;
        }
        reset();
    }
    /* A leftover temporary file does not stop the next start. */
    snprintf(tmp, sizeof(tmp), "%s.tmp", g_file);
    close(open(tmp, O_WRONLY | O_CREAT | O_CLOEXEC, 0644));
    /* ... but a directory in its place does. */
    unlink(tmp);
    mkdir(tmp, 0700);
    CHECK(open_fails(g_file, g_agent) == -1);
    rmdir(tmp);
}

static void test_open_and_close(void)
{
    struct fl_agent_relay r;
    struct stat st;
    char text[FL_AGENT_TEXT_MAX];
    char want[FL_AGENT_TEXT_MAX];
    char attr[16] = "";
    int fd;
    ssize_t n;
    /* getrandom interrupted once: retried. */
    fail_at(W_GETRANDOM, 1, EINTR);
    CHECK(open_relay(&r) == 0);
    reset();
    CHECK(r.lst >= 0 && r.port > 0);
    CHECK(lstat(g_file, &st) == 0 && S_ISREG(st.st_mode) && (st.st_mode & 0777) == 0600);
    fd = open(g_file, O_RDONLY | O_CLOEXEC);
    n = __real_read(fd, text, sizeof(text));
    close(fd);
    CHECK(n > 0 && (size_t)n == fl_agent_socket_text(r.port, r.secret, want, sizeof(want)));
    CHECK(n > 0 && memcmp(text, want, (size_t)n) == 0);
    CHECK(g_no_xattr || (getxattr(g_file, "user.DOSATTRIB", attr, sizeof(attr) - 1) == 3 && strcmp(attr, "0x4") == 0));
    fl_agent_relay_close(&r);
    CHECK(r.lst == -1 && access(g_file, F_OK) != 0);
    fl_agent_relay_close(&r); /* twice: nothing left to do */

    /* A file that replaced ours is not removed. */
    CHECK(open_relay(&r) == 0);
    {
        char other[PATH_MAX];
        snprintf(other, sizeof(other), "%s/other", g_dir);
        close(open(other, O_WRONLY | O_CREAT | O_CLOEXEC, 0600));
        CHECK(rename(other, g_file) == 0);
    }
    fl_agent_relay_close(&r);
    CHECK(access(g_file, F_OK) == 0);
    /* An existing file is replaced; one that vanished is fine too. */
    CHECK(open_relay(&r) == 0);
    unlink(g_file);
    fl_agent_relay_close(&r);
}

/* ------------------------------------------------------------------------ */
/* connections                                                               */
/* ------------------------------------------------------------------------ */

static void client_full(int s, const struct fl_agent_relay *r)
{
    static const uint8_t list[] = { 11 };
    static const uint8_t sign[] = { 13, 0, 0, 0, 0 };
    static const uint8_t add[] = { 17, 0, 0, 0, 0 };
    uint8_t bind[5 + 24] = { 27 };
    put_be32(bind + 1, 24);
    memcpy(bind + 5, "session-bind@openssh.com", 24);
    client_handshake(s, r);
    CHECK(send_msg(s, list, sizeof(list)) == 0 && recv_type(s) == 12);
    CHECK(send_msg(s, add, sizeof(add)) == 0 && recv_type(s) == FL_AGENT_FAILURE);
    CHECK(send_msg(s, sign, sizeof(sign)) == 0 && recv_type(s) == 14);
    CHECK(send_msg(s, bind, sizeof(bind)) == 0 && recv_type(s) == 6);
}

/* Wait until the relay closes (the socket stays ours meanwhile, so its owner is readable). */
static void drain(int s)
{
    char buf[64];
    shutdown(s, SHUT_WR);
    while (recv(s, buf, sizeof(buf), 0) > 0) {
    }
}

static void client_silent(int s, const struct fl_agent_relay *r)
{
    (void)r;
    drain(s);
}

static void client_wrong_secret(int s, const struct fl_agent_relay *r)
{
    uint8_t bad[16];
    memcpy(bad, r->secret, 16);
    bad[15] ^= 1;
    CHECK(sendall(s, bad, 16) == 0);
    CHECK(recv(s, bad, 1, 0) == 0);
}

static void client_secret_only(int s, const struct fl_agent_relay *r)
{
    uint8_t got[16];
    CHECK(sendall(s, r->secret, 16) == 0);
    CHECK(recvall(s, got, 16) == 0);
}

static void client_send_secret(int s, const struct fl_agent_relay *r)
{
    (void)sendall(s, r->secret, 16);
    drain(s);
}

static void client_handshake_only(int s, const struct fl_agent_relay *r)
{
    client_handshake(s, r);
}

static void client_zero_length(int s, const struct fl_agent_relay *r)
{
    static const uint8_t zero[4] = { 0 };
    client_handshake(s, r);
    CHECK(sendall(s, zero, 4) == 0);
}

static void client_too_long(int s, const struct fl_agent_relay *r)
{
    uint8_t hdr[4];
    client_handshake(s, r);
    put_be32(hdr, FL_AGENT_MSG_MAX + 1u);
    CHECK(sendall(s, hdr, 4) == 0);
}

static void client_short_body(int s, const struct fl_agent_relay *r)
{
    uint8_t hdr[5];
    client_handshake(s, r);
    put_be32(hdr, 10);
    hdr[4] = 11;
    CHECK(sendall(s, hdr, 5) == 0);
    shutdown(s, SHUT_WR);
}

static void client_short_header(int s, const struct fl_agent_relay *r)
{
    client_handshake(s, r);
    CHECK(sendall(s, "\0\0", 2) == 0);
    shutdown(s, SHUT_WR);
}

static void client_agent_closes(int s, const struct fl_agent_relay *r)
{
    static const uint8_t list[] = { 11, 0xEE };
    client_handshake(s, r);
    CHECK(send_msg(s, list, sizeof(list)) == 0 && recv_type(s) == -1);
}

static void client_agent_zero(int s, const struct fl_agent_relay *r)
{
    static const uint8_t list[] = { 11, 0xEF };
    client_handshake(s, r);
    CHECK(send_msg(s, list, sizeof(list)) == 0 && recv_type(s) == -1);
}

static void client_one_list(int s, const struct fl_agent_relay *r)
{
    static const uint8_t list[] = { 11 };
    client_handshake(s, r);
    CHECK(send_msg(s, list, sizeof(list)) == 0);
    shutdown(s, SHUT_WR);
}

static void client_one_refused(int s, const struct fl_agent_relay *r)
{
    static const uint8_t lock[] = { 22, 0, 0, 0, 0 };
    client_handshake(s, r);
    CHECK(send_msg(s, lock, sizeof(lock)) == 0);
    shutdown(s, SHUT_WR);
}

static void expect(const struct fl_agent_relay *r, client_fn client, const char *why, unsigned requests,
                   unsigned refused)
{
    struct outcome o;
    run_conn(r, client, &o);
    if (strcmp(o.why, why) != 0 || o.requests != requests || o.refused != refused) {
        fprintf(stderr, "  got %s requests=%u refused=%u, want %s %u %u\n", o.why, o.requests, o.refused, why,
                requests, refused);
    }
    CHECK(strcmp(o.why, why) == 0);
    CHECK(o.requests == requests);
    CHECK(o.refused == refused);
}

static void test_connections(void)
{
    struct fl_agent_relay r;
    char fake[PATH_MAX];
    FILE *f;
    CHECK(open_relay(&r) == 0);
    expect(&r, client_full, "done", 3, 1);
    expect(&r, client_silent, "auth-failed:no-secret", 0, 0);
    expect(&r, client_wrong_secret, "auth-failed:secret", 0, 0);
    expect(&r, client_secret_only, "auth-failed:handshake", 0, 0);
    expect(&r, client_zero_length, "bad-message", 0, 0);
    expect(&r, client_too_long, "bad-message", 0, 0);
    expect(&r, client_short_body, "bad-message", 0, 0);
    expect(&r, client_short_header, "bad-message", 0, 0);
    expect(&r, client_agent_closes, "agent-gone", 1, 0);
    expect(&r, client_agent_zero, "agent-gone", 1, 0);
    /* Interrupted reads and sends are retried. */
    fail_at(W_READ, 1, EINTR);
    fail_at(W_SEND, 1, EINTR);
    expect(&r, client_full, "done", 3, 1);
    /* A failing send: to the client in the handshake, to the agent, back to the client. */
    fail_at(W_SEND, 1, EPIPE);
    expect(&r, client_send_secret, "auth-failed:handshake", 0, 0);
    fail_at(W_SEND, 3, EPIPE);
    expect(&r, client_one_list, "agent-gone", 1, 0);
    fail_at(W_SEND, 4, EPIPE);
    expect(&r, client_one_list, "client-gone", 1, 0);
    fail_at(W_SEND, 3, EPIPE);
    expect(&r, client_one_refused, "client-gone", 0, 1);
    fail_at(W_READ, 1, EIO);
    expect(&r, client_send_secret, "auth-failed:no-secret", 0, 0);
    /* No memory for the message buffer; no socket for the agent. */
    fl_alloc_fail_at(1);
    expect(&r, client_handshake_only, "out-of-memory", 0, 0);
    fl_alloc_fail_at(0);
    fail_at(W_SOCKET, 1, EMFILE);
    expect(&r, client_handshake_only, "agent-unreachable", 0, 0);
    /* The agent went away. */
    stop_agent();
    unlink(g_agent);
    expect(&r, client_handshake_only, "agent-unreachable", 0, 0);
    start_agent();

    /* /proc/net/tcp without the peer's line, or none at all. */
    snprintf(fake, sizeof(fake), "%s/net-tcp", g_dir);
    f = fopen(fake, "we");
    if (f != NULL) {
        fclose(f);
    }
    r.net_tcp = fake;
    expect(&r, client_silent, "auth-failed:peer-uid", 0, 0);
    snprintf(fake, sizeof(fake), "%s/no-net-tcp", g_dir);
    expect(&r, client_silent, "auth-failed:peer-uid", 0, 0);
    r.net_tcp = "/proc/net/tcp";
    fl_agent_relay_close(&r);
}

/* A /proc/net/tcp whose line for the peer names another owner. */
static char g_other_tcp[PATH_MAX];

static void other_owner(const struct sockaddr_in *peer, const struct fl_agent_relay *r)
{
    FILE *f = fopen(g_other_tcp, "we");
    if (f != NULL) {
        fprintf(f, "  sl  local_address rem_address   st\n"
                   "   1: %08X:%04X %08X:%04X 01 00000000:00000000 00:00000000 00000000 %u 0 1 1\n",
                peer->sin_addr.s_addr, ntohs(peer->sin_port), htonl(INADDR_LOOPBACK), r->port,
                (unsigned)geteuid() + 1u);
        fclose(f);
    }
}

static void test_other_owner(void)
{
    struct fl_agent_relay r;
    struct outcome o;
    CHECK(open_relay(&r) == 0);
    snprintf(g_other_tcp, sizeof(g_other_tcp), "%s/other-tcp", g_dir);
    unlink(g_other_tcp);
    r.net_tcp = g_other_tcp;
    g_before_serve = other_owner;
    run_conn(&r, client_silent, &o);
    g_before_serve = NULL;
    CHECK(strcmp(o.why, "auth-failed:peer-uid") == 0);
    fl_agent_relay_close(&r);
}

int main(void)
{
    char cwd[512];
    signal(SIGPIPE, SIG_IGN);
    if (getcwd(cwd, sizeof(cwd)) == NULL) {
        return 1;
    }
    snprintf(g_dir, sizeof(g_dir), "%s/agent-relay-test-%ld", cwd, (long)getpid());
    if (mkdir(g_dir, 0700) != 0) {
        perror(g_dir);
        return 1;
    }
    snprintf(g_file, sizeof(g_file), "%s/agent.sock", g_dir);
    snprintf(g_agent, sizeof(g_agent), "/tmp/fl-agent-test-%ld.sock", (long)getpid());
    start_agent();

    test_socket_text();
    test_tcp_uid();
    test_request_filter();
    test_open_refusals();
    test_open_failures();
    test_open_and_close();
    test_connections();
    test_other_owner();

    stop_agent();
    unlink(g_agent);
    {
        char path[PATH_MAX];
        static const char *const names[] = { "notadir", "net-tcp", "other-tcp", "agent.sock" };
        for (size_t i = 0; i < sizeof(names) / sizeof(names[0]); i++) {
            snprintf(path, sizeof(path), "%s/%s", g_dir, names[i]);
            unlink(path);
        }
        rmdir(g_dir);
    }
    printf("%d checks, %d failed\n", g_checks, g_failures);
    if (g_failures == 0) {
        printf("all %d checks passed\n", g_checks);
    }
    return g_failures == 0 ? 0 : 1;
}
