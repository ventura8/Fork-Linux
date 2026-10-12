/*
 * fl_agent_relay.c - the ssh-agent relay of fl-bridge-helper (see fl_agent_relay.h for the
 * msys socket emulation it speaks and bridge/unix/README.md for the daemon options).
 */
#define _GNU_SOURCE
#include "fl_agent_relay.h"

#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/random.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/time.h>
#include <sys/un.h>
#include <sys/xattr.h>
#include <unistd.h>

/* Wine keeps the DOS attributes it cannot store elsewhere in this extended attribute
 * (Samba's format); 0x4 is FILE_ATTRIBUTE_SYSTEM, which marks a Cygwin / msys socket file. */
#define DOSATTRIB_NAME "user.DOSATTRIB"
#define DOSATTRIB_SYSTEM "0x4"
#define SOCKET_COOKIE "!<socket >"
#define SESSION_BIND "session-bind@openssh.com"
#define TMP_SUFFIX ".tmp"

/* ------------------------------------------------------------------------ */
/* pure helpers                                                              */
/* ------------------------------------------------------------------------ */

static uint32_t le32(const uint8_t *p)
{
    return (uint32_t)p[0] | (uint32_t)p[1] << 8 | (uint32_t)p[2] << 16 | (uint32_t)p[3] << 24;
}

static uint32_t be32(const uint8_t *p)
{
    return (uint32_t)p[0] << 24 | (uint32_t)p[1] << 16 | (uint32_t)p[2] << 8 | (uint32_t)p[3];
}

static void put_le32(uint8_t *p, uint32_t v)
{
    p[0] = (uint8_t)v;
    p[1] = (uint8_t)(v >> 8);
    p[2] = (uint8_t)(v >> 16);
    p[3] = (uint8_t)(v >> 24);
}

size_t fl_agent_socket_text(unsigned port, const uint8_t secret[FL_AGENT_SECRET_LEN], char *out, size_t cap)
{
    int n = snprintf(out, cap, SOCKET_COOKIE "%u s %08x-%08x-%08x-%08x", port, le32(secret), le32(secret + 4),
                     le32(secret + 8), le32(secret + 12));
    if (n < 0 || (size_t)n >= cap) {
        return 0;
    }
    return (size_t)n + 1u;
}

/* Copy the next whitespace-separated field of *p (at most cap - 1 bytes) into out; 0 when there is one. */
static int next_field(const char **p, char *out, size_t cap)
{
    const char *s = *p;
    size_t n = 0;
    while (*s == ' ' || *s == '\t') {
        s++;
    }
    while (*s != '\0' && *s != ' ' && *s != '\t' && *s != '\n') {
        if (n + 1 < cap) {
            out[n++] = *s;
        }
        s++;
    }
    out[n] = '\0';
    *p = s;
    return n == 0 ? -1 : 0;
}

/* The whole of text as a number in base (no sign, no spaces, nothing after it, at most max). */
static int parse_number(const char *text, int base, unsigned long max, unsigned long *out)
{
    char *end = NULL;
    unsigned long v;
    if (text[0] == '\0' || text[0] == '-' || text[0] == '+') {
        return -1;
    }
    errno = 0;
    v = strtoul(text, &end, base);
    if (errno != 0 || *end != '\0' || v > max) {
        return -1;
    }
    *out = v;
    return 0;
}

/* "AAAAAAAA:PPPP" (hex address as the kernel prints it, hex port). */
static int parse_endpoint(char *field, uint32_t *addr, unsigned *port)
{
    char *colon = strchr(field, ':');
    unsigned long a;
    unsigned long p;
    if (colon == NULL) {
        return -1;
    }
    *colon = '\0';
    if (parse_number(field, 16, 0xffffffffUL, &a) != 0 || parse_number(colon + 1, 16, 0xffffUL, &p) != 0) {
        return -1;
    }
    *addr = (uint32_t)a;
    *port = (unsigned)p;
    return 0;
}

/* One /proc/net/tcp line: 0 and *uid when it is the socket local_addr:local_port -> 127.0.0.1:remote_port.
 * Fields: "sl: local rem st tx:rx tr:when retrnsmt uid ..." (the uid is the 8th). */
static int tcp_line_uid(const char *line, uint32_t local_addr, unsigned local_port, unsigned remote_port,
                        uint32_t *uid)
{
    char field[8][32];
    uint32_t la;
    uint32_t ra;
    unsigned lp;
    unsigned rp;
    unsigned long owner;
    for (size_t i = 0; i < 8; i++) {
        if (next_field(&line, field[i], sizeof(field[i])) != 0) {
            return -1;
        }
    }
    if (parse_endpoint(field[1], &la, &lp) != 0 || parse_endpoint(field[2], &ra, &rp) != 0 ||
        parse_number(field[7], 10, 0xffffffffUL, &owner) != 0) {
        return -1;
    }
    if (la != local_addr || lp != local_port || ra != htonl(INADDR_LOOPBACK) || rp != remote_port) {
        return -1;
    }
    *uid = (uint32_t)owner;
    return 0;
}

int fl_agent_tcp_uid(const char *text, uint32_t local_addr, unsigned local_port, unsigned remote_port, uint32_t *uid)
{
    const char *line = text;
    while (line != NULL && *line != '\0') {
        const char *next = strchr(line, '\n');
        if (tcp_line_uid(line, local_addr, local_port, remote_port, uid) == 0) {
            return 0;
        }
        line = next != NULL ? next + 1 : NULL;
    }
    return -1;
}

int fl_agent_request_allowed(const uint8_t *msg, size_t len)
{
    size_t name_len = sizeof(SESSION_BIND) - 1u;
    if (len == 0) {
        return 0;
    }
    if (msg[0] == FL_AGENTC_REQUEST_IDENTITIES || msg[0] == FL_AGENTC_SIGN_REQUEST) {
        return 1;
    }
    /* EXTENSION: string name (u32 BE length + bytes), then the extension's own data. */
    return msg[0] == FL_AGENTC_EXTENSION && len >= 5u + name_len && be32(msg + 1) == name_len &&
           memcmp(msg + 5, SESSION_BIND, name_len) == 0;
}

/* ------------------------------------------------------------------------ */
/* the socket file                                                           */
/* ------------------------------------------------------------------------ */

static void set_why(char *why, size_t cap, const char *what, int err)
{
    snprintf(why, cap, "%s: %s", what, strerror(err));
}

/* 0 when dir (the socket file's directory) is a real directory of ours, mode 0700 or stricter. */
static int private_dir(const char *file, char *why, size_t cap)
{
    char dir[PATH_MAX];
    struct stat st;
    const char *slash = strrchr(file, '/');
    size_t len = slash == NULL ? 0 : (size_t)(slash - file);
    if (len == 0) {
        snprintf(why, cap, "the socket file needs an absolute path in a directory");
        return -1;
    }
    memcpy(dir, file, len);
    dir[len] = '\0';
    if (lstat(dir, &st) != 0) {
        set_why(why, cap, "socket file directory", errno);
        return -1;
    }
    if (!S_ISDIR(st.st_mode) || st.st_uid != geteuid() || (st.st_mode & 077) != 0) {
        snprintf(why, cap, "the socket file directory is not a private (0700) directory of this user");
        return -1;
    }
    return 0;
}

/* 0 when agent is an absolute path to a socket that fits sun_path (copied into r). */
static int agent_socket(struct fl_agent_relay *r, const char *agent, char *why, size_t cap)
{
    struct stat st;
    if (agent[0] != '/' || strlen(agent) >= sizeof(r->agent)) {
        snprintf(why, cap, "the agent socket needs an absolute path shorter than %zu bytes", sizeof(r->agent));
        return -1;
    }
    if (stat(agent, &st) != 0) {
        set_why(why, cap, "agent socket", errno);
        return -1;
    }
    if (!S_ISSOCK(st.st_mode)) {
        snprintf(why, cap, "the agent path is not a socket");
        return -1;
    }
    memcpy(r->agent, agent, strlen(agent) + 1u);
    return 0;
}

static int listen_loopback(struct fl_agent_relay *r, char *why, size_t cap)
{
    struct sockaddr_in a;
    socklen_t alen = sizeof(a);
    r->lst = socket(AF_INET, SOCK_STREAM | SOCK_CLOEXEC | SOCK_NONBLOCK, 0);
    if (r->lst < 0) {
        set_why(why, cap, "socket", errno);
        return -1;
    }
    memset(&a, 0, sizeof(a));
    a.sin_family = AF_INET;
    a.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    if (bind(r->lst, (struct sockaddr *)&a, sizeof(a)) != 0 || listen(r->lst, 16) != 0 ||
        getsockname(r->lst, (struct sockaddr *)&a, &alen) != 0) {
        set_why(why, cap, "listen on 127.0.0.1", errno);
        return -1;
    }
    r->port = ntohs(a.sin_port);
    return 0;
}

static int draw_secret(uint8_t *secret, char *why, size_t cap)
{
    size_t got = 0;
    while (got < FL_AGENT_SECRET_LEN) {
        ssize_t n = getrandom(secret + got, FL_AGENT_SECRET_LEN - got, 0);
        if (n < 0 && errno == EINTR) {
            continue;
        }
        if (n <= 0) {
            set_why(why, cap, "getrandom", errno);
            return -1;
        }
        got += (size_t)n;
    }
    return 0;
}

/* Write text[0..len) to a fresh tmp (0600, O_EXCL, never through a link) with the SYSTEM attribute. */
static int write_tmp(const char *tmp, const char *text, size_t len, char *why, size_t cap)
{
    int fd = open(tmp, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC | O_NOCTTY, 0600);
    int err = 0;
    const char *what = "";
    if (fd < 0) {
        set_why(why, cap, "create the socket file", errno);
        return -1;
    }
    if (write(fd, text, len) != (ssize_t)len) {
        err = errno != 0 ? errno : EIO;
        what = "write the socket file";
    } else if (fsetxattr(fd, DOSATTRIB_NAME, DOSATTRIB_SYSTEM, sizeof(DOSATTRIB_SYSTEM) - 1u, 0) != 0) {
        err = errno;
        what = "mark the socket file (extended attributes)";
    }
    close(fd);
    if (err != 0) {
        set_why(why, cap, what, err);
        unlink(tmp);
        return -1;
    }
    return 0;
}

static int write_file(struct fl_agent_relay *r, char *why, size_t cap)
{
    char text[FL_AGENT_TEXT_MAX];
    char tmp[PATH_MAX + sizeof(TMP_SUFFIX)];
    size_t len = fl_agent_socket_text(r->port, r->secret, text, sizeof(text));
    snprintf(tmp, sizeof(tmp), "%s" TMP_SUFFIX, r->file);
    unlink(tmp);
    if (write_tmp(tmp, text, len, why, cap) != 0) {
        return -1;
    }
    if (rename(tmp, r->file) != 0) {
        set_why(why, cap, "rename the socket file", errno);
        unlink(tmp);
        return -1;
    }
    return 0;
}

int fl_agent_relay_open(struct fl_agent_relay *r, const char *file, const char *agent, char *why, size_t whycap)
{
    memset(r, 0, sizeof(*r));
    r->lst = -1;
    r->net_tcp = "/proc/net/tcp";
    if (strlen(file) >= sizeof(r->file)) {
        snprintf(why, whycap, "the socket file path is too long");
        return -1;
    }
    memcpy(r->file, file, strlen(file) + 1u);
    if (agent_socket(r, agent, why, whycap) != 0 || private_dir(file, why, whycap) != 0 ||
        listen_loopback(r, why, whycap) != 0 || draw_secret(r->secret, why, whycap) != 0 ||
        write_file(r, why, whycap) != 0) {
        if (r->lst >= 0) {
            close(r->lst);
        }
        explicit_bzero(r->secret, sizeof(r->secret));
        r->lst = -1;
        r->file[0] = '\0';
        return -1;
    }
    return 0;
}

/* 1 when r->file still holds exactly the text this relay wrote (its secret is unique). */
static int file_is_ours(const struct fl_agent_relay *r)
{
    char want[FL_AGENT_TEXT_MAX];
    char got[FL_AGENT_TEXT_MAX + 1];
    size_t len = fl_agent_socket_text(r->port, r->secret, want, sizeof(want));
    ssize_t n;
    int fd = open(r->file, O_RDONLY | O_NOFOLLOW | O_CLOEXEC | O_NOCTTY);
    if (fd < 0) {
        return 0;
    }
    n = read(fd, got, sizeof(got));
    close(fd);
    return n == (ssize_t)len && memcmp(got, want, len) == 0;
}

void fl_agent_relay_close(struct fl_agent_relay *r)
{
    if (r->lst >= 0) {
        close(r->lst);
        r->lst = -1;
    }
    if (r->file[0] != '\0' && file_is_ours(r)) {
        unlink(r->file);
    }
    r->file[0] = '\0';
    explicit_bzero(r->secret, sizeof(r->secret));
}

/* ------------------------------------------------------------------------ */
/* one connection                                                            */
/* ------------------------------------------------------------------------ */

/* Read exactly n bytes: 0 done, 1 EOF before the first byte, -1 error or EOF midway. */
static int read_exact(int fd, uint8_t *buf, size_t n)
{
    size_t got = 0;
    while (got < n) {
        ssize_t k = read(fd, buf + got, n - got);
        if (k < 0 && errno == EINTR) {
            continue;
        }
        if (k <= 0) {
            return k == 0 && got == 0 ? 1 : -1;
        }
        got += (size_t)k;
    }
    return 0;
}

static int write_exact(int fd, const uint8_t *buf, size_t n)
{
    while (n > 0) {
        ssize_t k = send(fd, buf, n, MSG_NOSIGNAL);
        if (k < 0 && errno == EINTR) {
            continue;
        }
        if (k <= 0) {
            return -1;
        }
        buf += k;
        n -= (size_t)k;
    }
    return 0;
}

/* 0 when /proc/net/tcp says the peer's end of the connection belongs to this user. */
static int peer_is_ours(const struct fl_agent_relay *r, const struct sockaddr_in *peer)
{
    char line[512];
    uint32_t uid = 0;
    int found = -1;
    FILE *f = fopen(r->net_tcp, "re");
    if (f == NULL) {
        return -1;
    }
    while (found != 0 && fgets(line, sizeof(line), f) != NULL) {
        found = fl_agent_tcp_uid(line, peer->sin_addr.s_addr, ntohs(peer->sin_port), r->port, &uid);
    }
    fclose(f);
    return found == 0 && uid == (uint32_t)geteuid() ? 0 : -1;
}

/* The msys handshake (fl_agent_relay.h); 0 when the peer knew the secret. */
static int handshake(const struct fl_agent_relay *r, int c, const char **why)
{
    struct timeval tv = { .tv_sec = FL_AGENT_HANDSHAKE_MS / 1000, .tv_usec = 0 };
    struct timeval none = { .tv_sec = 0, .tv_usec = 0 };
    uint8_t got[FL_AGENT_SECRET_LEN];
    uint8_t cred[FL_AGENT_CRED_LEN];
    uint8_t diff = 0;
    setsockopt(c, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
    if (read_exact(c, got, sizeof(got)) != 0) {
        *why = "auth-failed:no-secret";
        return -1;
    }
    for (size_t i = 0; i < sizeof(got); i++) {
        diff |= (uint8_t)(got[i] ^ r->secret[i]);
    }
    if (diff != 0) {
        *why = "auth-failed:secret";
        return -1;
    }
    put_le32(cred, (uint32_t)getpid());
    put_le32(cred + 4, (uint32_t)geteuid());
    put_le32(cred + 8, (uint32_t)getegid());
    if (write_exact(c, r->secret, sizeof(got)) != 0 || read_exact(c, got, sizeof(cred)) != 0 ||
        write_exact(c, cred, sizeof(cred)) != 0) {
        *why = "auth-failed:handshake";
        return -1;
    }
    setsockopt(c, SOL_SOCKET, SO_RCVTIMEO, &none, sizeof(none));
    return 0;
}

static int connect_agent(const struct fl_agent_relay *r)
{
    struct sockaddr_un a;
    int s = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
    if (s < 0) {
        return -1;
    }
    memset(&a, 0, sizeof(a));
    a.sun_family = AF_UNIX;
    memcpy(a.sun_path, r->agent, strlen(r->agent) + 1u);
    if (connect(s, (struct sockaddr *)&a, sizeof(a)) != 0) {
        close(s);
        return -1;
    }
    return s;
}

/* Read one length-prefixed agent message into buf (prefix included); its body length, 0 at EOF, -1 on error. */
static long read_message(int fd, uint8_t *buf)
{
    uint32_t len;
    int rc = read_exact(fd, buf, 4);
    if (rc != 0) {
        return rc == 1 ? 0 : -1;
    }
    len = be32(buf);
    if (len == 0 || len > FL_AGENT_MSG_MAX || read_exact(fd, buf + 4, len) != 0) {
        return -1;
    }
    return (long)len;
}

/* Relay requests from c to the agent socket a until c closes; the reason it ended. */
static const char *relay(int c, int a, uint8_t *buf, struct fl_agent_result *res)
{
    static const uint8_t failure[5] = { 0, 0, 0, 1, FL_AGENT_FAILURE };
    for (;;) {
        long len = read_message(c, buf);
        if (len <= 0) {
            return len == 0 ? "done" : "bad-message";
        }
        if (!fl_agent_request_allowed(buf + 4, (size_t)len)) {
            res->refused++;
            if (write_exact(c, failure, sizeof(failure)) != 0) {
                return "client-gone";
            }
            continue;
        }
        res->requests++;
        if (write_exact(a, buf, (size_t)len + 4u) != 0) {
            return "agent-gone";
        }
        len = read_message(a, buf);
        if (len <= 0) {
            return "agent-gone";
        }
        if (write_exact(c, buf, (size_t)len + 4u) != 0) {
            return "client-gone";
        }
    }
}

void fl_agent_serve(const struct fl_agent_relay *r, int c, const struct sockaddr_in *peer,
                    struct fl_agent_result *res)
{
    int a;
    uint8_t *buf;
    res->requests = 0;
    res->refused = 0;
    if (peer_is_ours(r, peer) != 0) {
        res->why = "auth-failed:peer-uid";
        close(c);
        return;
    }
    if (handshake(r, c, &res->why) != 0) {
        close(c);
        return;
    }
    a = connect_agent(r);
    buf = malloc(FL_AGENT_MSG_MAX + 4u);
    if (a < 0 || buf == NULL) {
        res->why = a < 0 ? "agent-unreachable" : "out-of-memory";
    } else {
        res->why = relay(c, a, buf, res);
    }
    free(buf);
    if (a >= 0) {
        close(a);
    }
    close(c);
}
