/*
 * rv-helper.c - B2 spike: loopback-TCP rendezvous, Linux side.
 * Built with musl-gcc -static -no-pie (ET_EXEC) so Wine's CreateProcessW execs it.
 *
 *   usage: rv-helper --port N [--token-file F]   per-call: spawned by rv.exe through Wine
 *          rv-helper --daemon PORTFILE           daemon: started outside Wine (by the launcher)
 *   The 256-bit hex token comes from FL_BRIDGE_TOKEN (removed from the env) or, in
 *   per-call mode on Wine 11 (which drops lpEnvironment), from F (read, then unlinked).
 *
 * Per-call: connects to rv.exe on 127.0.0.1:N and sends HELLO(token). Daemon:
 * listens on 127.0.0.1:0, writes the port to PORTFILE and forks one session per
 * connection whose first frame is HELLO(token). A session reads REQ(cwd, argv),
 * fork/execs argv with three pipes (child in its own process group, PDEATHSIG),
 * then runs a poll loop: STDIN frames -> child stdin (non-blocking, buffered),
 * child stdout/stderr -> STDOUT/STDERR frames, and EXIT(code) once both pipes
 * hit EOF and the child is reaped (a SIGCHLD self-pipe wakes poll(); no timers).
 * The final frame is followed by shutdown(SHUT_WR) + drain so the close never
 * turns into a TCP RST. If rv.exe goes away (socket EOF/RDHUP/error) the
 * child's process group gets SIGTERM, then SIGKILL after 3 s.
 */
#define _GNU_SOURCE
#include <arpa/inet.h>
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <poll.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/prctl.h>
#include <sys/socket.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#include "rvproto.h"

#define RXCAP (4u * (RV_HDR + RV_CHUNK))
#define INCAP (1024u * 1024u)

static int sock = -1;
static pid_t child = -1;
static int in_fd = -1;
static int stdin_eof;
static unsigned char rx[RXCAP];
static size_t rxlen;
static unsigned char inb[INCAP];
static size_t inoff;
static size_t inlen;
static unsigned char obuf[RV_HDR + RV_CHUNK];
static int chld_pipe[2] = { -1, -1 };   /* SIGCHLD self-pipe: wakes poll() on child exit */

/* SIGCHLD handler: one byte into the self-pipe. */
static void on_sigchld(int sig)
{
    int saved = errno;
    ssize_t r = write(chld_pipe[1], "c", 1);
    (void)sig;
    (void)r;
    errno = saved;
}

/* Send exactly len bytes (no SIGPIPE); 0 on success. */
static int send_all(const void *buf, size_t len)
{
    const unsigned char *p = buf;
    while (len) {
        ssize_t n = send(sock, p, len, MSG_NOSIGNAL);
        if (n < 0 && errno == EINTR) {
            continue;
        }
        if (n <= 0) {
            return -1;
        }
        p += n;
        len -= (size_t)n;
    }
    return 0;
}

/* Send a frame with a small payload. */
static int send_frame(unsigned type, const void *payload, uint32_t len)
{
    unsigned char buf[RV_HDR + 128];
    if (len > 128) {
        return -1;
    }
    rv_put_hdr(buf, type, len);
    if (len) {
        memcpy(buf + RV_HDR, payload, len);
    }
    return send_all(buf, RV_HDR + len);
}

/* Read exactly len bytes from the socket within timeout_ms; 0 on success. */
static int recv_full(void *buf, size_t len, int timeout_ms)
{
    unsigned char *p = buf;
    while (len) {
        struct pollfd pf = { .fd = sock, .events = POLLIN, .revents = 0 };
        ssize_t n;
        int r = poll(&pf, 1, timeout_ms);
        if (r < 0 && errno == EINTR) {
            continue;
        }
        if (r <= 0) {
            return -1;
        }
        n = recv(sock, p, len, 0);
        if (n < 0 && errno == EINTR) {
            continue;
        }
        if (n <= 0) {
            return -1;
        }
        p += n;
        len -= (size_t)n;
    }
    return 0;
}

/* Sleep for ms milliseconds. */
static void sleep_ms(long ms)
{
    struct timespec ts = { ms / 1000, (ms % 1000) * 1000000L };
    while (nanosleep(&ts, &ts) < 0 && errno == EINTR) {
    }
}

/* rv.exe is gone: SIGTERM the child's process group, SIGKILL after 3 s. */
static void kill_child_group(void)
{
    int reaped = 0;
    if (child <= 0) {
        return;
    }
    kill(-child, SIGTERM);
    for (int i = 0; i < 60 && !reaped; i++) {
        if (waitpid(child, NULL, WNOHANG) == child) {
            reaped = 1;
            break;
        }
        sleep_ms(50);
    }
    kill(-child, SIGKILL);
    if (!reaped) {
        waitpid(child, NULL, 0);
    }
}

/* Peer vanished or broke the protocol: clean up and exit. */
static void abort_session(void)
{
    kill_child_group();
    _exit(1);
}

/*
 * Send the final frame, then close without a TCP RST: shutdown(SHUT_WR) and
 * drain until rv.exe closes (or 2 s pass). Closing with unread data (e.g. a
 * STDIN_EOF still in flight) makes Linux send RST, and Wine then fails rv.exe's
 * pending recv with WSAECONNRESET before it has read the EXIT frame.
 */
static void finish(unsigned type, const unsigned char *payload, uint32_t len)
{
    unsigned char sink[4096];
    send_frame(type, payload, len);
    shutdown(sock, SHUT_WR);
    for (;;) {
        struct pollfd pf = { .fd = sock, .events = POLLIN, .revents = 0 };
        int r = poll(&pf, 1, 2000);
        if (r < 0 && errno == EINTR) {
            continue;
        }
        if (r <= 0 || recv(sock, sink, sizeof(sink), 0) <= 0) {
            break;
        }
    }
    close(sock);
}

/* Close every fd >= 3 we may have inherited from the Wine process. */
static void close_inherited_fds(void)
{
    DIR *d = opendir("/proc/self/fd");
    struct dirent *e;
    int self;
    if (!d) {
        return;
    }
    self = dirfd(d);
    while ((e = readdir(d)) != NULL) {
        int n = atoi(e->d_name);
        if (e->d_name[0] != '.' && n > 2 && n != self) {
            close(n);
        }
    }
    closedir(d);
}

/* Move complete frames from rx into the child's stdin buffer. */
static void parse_frames(void)
{
    while (rxlen >= RV_HDR) {
        uint32_t len = rv_get_len(rx);
        unsigned type = rx[0];
        if (len > RV_CHUNK) {
            abort_session();
        }
        if (rxlen < RV_HDR + len) {
            return;
        }
        if (type == RV_STDIN) {
            if (in_fd >= 0) {
                if (INCAP - inoff - inlen < len) {
                    memmove(inb, inb + inoff, inlen);
                    inoff = 0;
                }
                if (INCAP - inlen < len) {
                    return; /* wait until the child drains its stdin */
                }
                memcpy(inb + inoff + inlen, rx + RV_HDR, len);
                inlen += len;
            }
        } else if (type == RV_STDIN_EOF) {
            stdin_eof = 1;
        } else {
            abort_session();
        }
        memmove(rx, rx + RV_HDR + len, rxlen - RV_HDR - len);
        rxlen -= RV_HDR + len;
    }
}

/* Forward one read from a child pipe as a frame; returns -1 when the pipe is done. */
static int forward(int fd, unsigned type)
{
    ssize_t n = read(fd, obuf + RV_HDR, RV_CHUNK);
    if (n > 0) {
        rv_put_hdr(obuf, type, (uint32_t)n);
        if (send_all(obuf, RV_HDR + (size_t)n)) {
            abort_session();
        }
        return 0;
    }
    if (n < 0 && (errno == EINTR || errno == EAGAIN)) {
        return 0;
    }
    return -1;
}

/* fork/exec argv with pipes; returns 0 or the errno of the failure. */
static int spawn(const char *cwd, char **argv, int *out_fd, int *err_fd)
{
    int in[2], out[2], err[2], ex[2];
    pid_t parent = getpid();
    int e = 0;
    ssize_t r;
    struct sigaction sa;
    if (pipe2(in, O_CLOEXEC) || pipe2(out, O_CLOEXEC) || pipe2(err, O_CLOEXEC) || pipe2(ex, O_CLOEXEC) ||
        pipe2(chld_pipe, O_CLOEXEC | O_NONBLOCK)) {
        return errno;
    }
    memset(&sa, 0, sizeof(sa));
    sa.sa_handler = on_sigchld;
    sa.sa_flags = SA_RESTART | SA_NOCLDSTOP;
    sigemptyset(&sa.sa_mask);
    sigaction(SIGCHLD, &sa, NULL);
    child = fork();
    if (child < 0) {
        return errno;
    }
    if (child == 0) {
        sigset_t none;
        setpgid(0, 0);
        prctl(PR_SET_PDEATHSIG, SIGKILL);
        if (getppid() != parent) {
            _exit(125);
        }
        dup2(in[0], 0);
        dup2(out[1], 1);
        dup2(err[1], 2);
        signal(SIGPIPE, SIG_DFL);
        sigemptyset(&none);
        sigprocmask(SIG_SETMASK, &none, NULL);
        if (cwd[0] && chdir(cwd) != 0) {
            e = errno;
        } else {
            execvp(argv[0], argv);
            e = errno;
        }
        r = write(ex[1], &e, sizeof(e));
        (void)r;
        _exit(127);
    }
    setpgid(child, child);
    close(in[0]);
    close(out[1]);
    close(err[1]);
    close(ex[1]);
    do {
        r = read(ex[0], &e, sizeof(e));
    } while (r < 0 && errno == EINTR);
    close(ex[0]);
    if (r == (ssize_t)sizeof(e)) {
        close(in[1]);
        close(out[0]);
        close(err[0]);
        waitpid(child, NULL, 0);
        child = -1;
        return e ? e : EINVAL;
    }
    in_fd = in[1];
    fcntl(in_fd, F_SETFL, O_NONBLOCK);
    *out_fd = out[0];
    *err_fd = err[0];
    return 0;
}

/* Serve one authenticated connection on `sock`: REQ, spawn, relay, EXIT. */
static int session(void)
{
    unsigned char hdr[RV_HDR];
    uint32_t reqlen;
    char *req;
    char **cargv;
    size_t cargc = 0;
    int out_fd = -1;
    int err_fd = -1;
    int e;

    /* REQ: cwd \0 argv0 \0 argv1 \0 ... */
    if (recv_full(hdr, RV_HDR, 10000) || hdr[0] != RV_REQ) {
        return 125;
    }
    reqlen = rv_get_len(hdr);
    if (reqlen == 0 || reqlen > RV_MAX_REQ) {
        return 125;
    }
    req = malloc((size_t)reqlen + 1);
    cargv = calloc((size_t)reqlen + 1, sizeof(char *));
    if (!req || !cargv || recv_full(req, reqlen, 10000)) {
        return 125;
    }
    req[reqlen] = '\0';
    for (char *p = req + strlen(req) + 1; p < req + reqlen; p += strlen(p) + 1) {
        cargv[cargc++] = p;
    }
    cargv[cargc] = NULL;
    e = cargc ? spawn(req, cargv, &out_fd, &err_fd) : EINVAL;
    if (e) {
        unsigned char eb[4];
        rv_put_i32(eb, e);
        finish(RV_SPAWN_ERR, eb, 4);
        return 0;
    }
    if (send_frame(RV_SPAWN_OK, NULL, 0)) {
        abort_session();
    }

    for (;;) {
        struct pollfd p[5];
        int np = 0;
        int is = -1, ii = -1, io = -1, ie = -1, ic = -1;
        p[np].fd = chld_pipe[0];
        p[np].events = POLLIN;
        p[np].revents = 0;
        ic = np++;
        p[np].fd = sock;
        p[np].events = (short)(POLLRDHUP | (rxlen < RXCAP ? POLLIN : 0));
        p[np].revents = 0;
        is = np++;
        if (in_fd >= 0 && inlen > 0) {
            p[np].fd = in_fd;
            p[np].events = POLLOUT;
            p[np].revents = 0;
            ii = np++;
        }
        if (out_fd >= 0) {
            p[np].fd = out_fd;
            p[np].events = POLLIN;
            p[np].revents = 0;
            io = np++;
        }
        if (err_fd >= 0) {
            p[np].fd = err_fd;
            p[np].events = POLLIN;
            p[np].revents = 0;
            ie = np++;
        }
        if (poll(p, (nfds_t)np, -1) < 0) {
            if (errno == EINTR) {
                continue;
            }
            abort_session();
        }
        if (p[ic].revents & POLLIN) {
            char drain[64];
            while (read(chld_pipe[0], drain, sizeof(drain)) > 0) {
            }
        }
        if (p[is].revents & (POLLERR | POLLHUP | POLLRDHUP)) {
            abort_session(); /* rv.exe exited or was killed */
        }
        if (p[is].revents & POLLIN) {
            ssize_t n = recv(sock, rx + rxlen, RXCAP - rxlen, 0);
            if (n == 0 || (n < 0 && errno != EINTR && errno != EAGAIN)) {
                abort_session();
            }
            if (n > 0) {
                rxlen += (size_t)n;
            }
        }
        parse_frames();
        if (ii >= 0 && (p[ii].revents & (POLLOUT | POLLERR | POLLHUP))) {
            ssize_t w = write(in_fd, inb + inoff, inlen);
            if (w > 0) {
                inoff += (size_t)w;
                inlen -= (size_t)w;
                if (inlen == 0) {
                    inoff = 0;
                }
            } else if (w < 0 && errno != EAGAIN && errno != EINTR) {
                close(in_fd); /* child closed its stdin (EPIPE): drop the rest */
                in_fd = -1;
                inlen = 0;
                inoff = 0;
            }
            parse_frames();
        }
        if (in_fd >= 0 && stdin_eof && inlen == 0 && rxlen == 0) {
            close(in_fd);
            in_fd = -1;
        }
        if (io >= 0 && (p[io].revents & (POLLIN | POLLHUP | POLLERR)) && forward(out_fd, RV_STDOUT)) {
            close(out_fd);
            out_fd = -1;
        }
        if (ie >= 0 && (p[ie].revents & (POLLIN | POLLHUP | POLLERR)) && forward(err_fd, RV_STDERR)) {
            close(err_fd);
            err_fd = -1;
        }
        if (out_fd < 0 && err_fd < 0) {
            int st;
            if (waitpid(child, &st, WNOHANG) == child) {
                unsigned char cb[4];
                int code = WIFEXITED(st) ? WEXITSTATUS(st) : WIFSIGNALED(st) ? 128 + WTERMSIG(st) : 255;
                rv_put_i32(cb, code);
                finish(RV_EXIT, cb, 4);
                return 0;
            }
        }
    }
}

/* Per-call mode: connect back to rv.exe on 127.0.0.1:port and authenticate. */
static int run_percall(long port, const char *token)
{
    struct sockaddr_in a;
    int one = 1;
    memset(&a, 0, sizeof(a));
    a.sin_family = AF_INET;
    a.sin_port = htons((uint16_t)port);
    a.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    if (getenv("FL_RV_TEST_BADTOKEN")) {
        /* Spike self-test: an intruder with a wrong token must be dropped by rv.exe. */
        unsigned char bad[RV_HDR + RV_TOKEN_HEX];
        char c;
        sock = socket(AF_INET, SOCK_STREAM | SOCK_CLOEXEC, 0);
        if (sock >= 0 && connect(sock, (struct sockaddr *)&a, sizeof(a)) == 0) {
            rv_put_hdr(bad, RV_HELLO, RV_TOKEN_HEX);
            memset(bad + RV_HDR, '0', RV_TOKEN_HEX);
            if (send_all(bad, sizeof(bad)) == 0 && recv_full(&c, 1, 5000) == 0) {
                return 125; /* rv.exe talked to an unauthenticated peer */
            }
        }
        close(sock);
    }
    sock = socket(AF_INET, SOCK_STREAM | SOCK_CLOEXEC, 0);
    if (sock < 0 || connect(sock, (struct sockaddr *)&a, sizeof(a)) != 0) {
        return 125;
    }
    setsockopt(sock, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));
    if (send_frame(RV_HELLO, token, RV_TOKEN_HEX)) {
        return 125;
    }
    return session();
}

/*
 * Daemon mode (started by the launcher, outside Wine): listen on 127.0.0.1:0,
 * publish the port in <portfile>, fork one session per connection whose first
 * frame is HELLO(token). Nothing is spawned by Wine, so no seccomp inheritance.
 */
static int run_daemon(const char *portfile, const char *token)
{
    struct sockaddr_in a;
    socklen_t alen = sizeof(a);
    int one = 1;
    char tmp[4096];
    FILE *f;
    int lst = socket(AF_INET, SOCK_STREAM | SOCK_CLOEXEC, 0);
    memset(&a, 0, sizeof(a));
    a.sin_family = AF_INET;
    a.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    if (lst < 0 || bind(lst, (struct sockaddr *)&a, sizeof(a)) || listen(lst, 64) ||
        getsockname(lst, (struct sockaddr *)&a, &alen)) {
        return 125;
    }
    snprintf(tmp, sizeof(tmp), "%s.tmp", portfile);
    f = fopen(tmp, "w");
    if (!f || fprintf(f, "%u\n", (unsigned)ntohs(a.sin_port)) < 0 || fclose(f) || rename(tmp, portfile)) {
        return 125;
    }
    prctl(PR_SET_PDEATHSIG, SIGTERM);
    signal(SIGCHLD, SIG_IGN); /* sessions are reaped automatically */
    for (;;) {
        int c = accept4(lst, NULL, NULL, SOCK_CLOEXEC);
        pid_t pid;
        if (c < 0) {
            if (errno == EINTR || errno == ECONNABORTED) {
                continue;
            }
            return 125;
        }
        pid = fork();
        if (pid == 0) {
            unsigned char hdr[RV_HDR];
            unsigned char hello[RV_TOKEN_HEX];
            close(lst);
            signal(SIGCHLD, SIG_DFL);
            prctl(PR_SET_PDEATHSIG, 0);
            setsid();
            sock = c;
            setsockopt(sock, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));
            if (recv_full(hdr, RV_HDR, 3000) || hdr[0] != RV_HELLO || rv_get_len(hdr) != RV_TOKEN_HEX ||
                recv_full(hello, RV_TOKEN_HEX, 3000) ||
                !rv_ct_equal(hello, (const unsigned char *)token, RV_TOKEN_HEX)) {
                _exit(1);
            }
            _exit(session());
        }
        close(c);
    }
}

int main(int argc, char **argv)
{
    const char *tok = getenv("FL_BRIDGE_TOKEN");
    char token[RV_TOKEN_HEX];
    int daemon_mode;
    int have_token = 0;
    int nul;
    long port = 0;

    daemon_mode = argc == 3 && strcmp(argv[1], "--daemon") == 0;
    if (!daemon_mode && !((argc == 3 || (argc == 5 && strcmp(argv[3], "--token-file") == 0)) &&
                          strcmp(argv[1], "--port") == 0)) {
        return 125;
    }
    if (!daemon_mode) {
        port = strtol(argv[2], NULL, 10);
    }
    if (tok && strlen(tok) == RV_TOKEN_HEX) {
        memcpy(token, tok, RV_TOKEN_HEX);
        have_token = 1;
    }
    unsetenv("FL_BRIDGE_TOKEN");
    if (!daemon_mode && argc == 5) {
        int fd = open(argv[4], O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
        char buf[RV_TOKEN_HEX + 1];
        ssize_t n = fd >= 0 ? read(fd, buf, sizeof(buf)) : -1;
        if (fd >= 0) {
            close(fd);
        }
        unlink(argv[4]);
        if (!have_token && n == (ssize_t)RV_TOKEN_HEX) {
            memcpy(token, buf, RV_TOKEN_HEX);
            have_token = 1;
        }
    }
    if ((!daemon_mode && (port <= 0 || port > 65535)) || !have_token) {
        return 125;
    }

    /* Detach from whatever Wine gave us: /dev/null stdio, no inherited fds, own session. */
    signal(SIGPIPE, SIG_IGN);
    nul = open("/dev/null", O_RDWR);
    if (nul >= 0) {
        dup2(nul, 0);
        dup2(nul, 1);
        if (!getenv("FL_RV_DEBUG")) {
            dup2(nul, 2);
        }
    }
    close_inherited_fds();
    if (daemon_mode) {
        return run_daemon(argv[2], token);
    }
    setsid();
    return run_percall(port, token);
}
