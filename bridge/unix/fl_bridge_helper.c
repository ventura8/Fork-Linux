/*
 * fl_bridge_helper.c - native side of the Fork for Linux (unofficial) git bridge.
 *
 * One multi-call binary, persona chosen by basename(argv[0]):
 *
 *   fl-bridge-helper --daemon [--token-file FILE] [--port N] [--host-helper PATH]
 *                    [--parent-pid PID] [--log FILE]
 *       The launcher-started daemon (outside Wine: wine-staging's seccomp filter
 *       makes Wine-spawned Linux helpers unusable, docs/spikes/B2-rendezvous.md).
 *       Binds 127.0.0.1:N (0 = ephemeral), prints "FL_BRIDGE_PORT=<n>\n" on stdout
 *       and closes stdout, then forks one session per connection:
 *         HELLO -> CHALLENGE(server_nonce, daemon_mac) -> AUTH(client_mac) -> AUTH_OK
 *         (mutual HMAC-SHA256 keyed with the 256-bit FL_BRIDGE_TOKEN, 5 s budget),
 *         REQ -> SPAWN_OK | SPAWN_ERR, then the STDIN / STDOUT / STDERR / SIGNAL relay
 *         and a final EXIT followed by a lingering close.
 *       Exits on SIGTERM / SIGINT / SIGHUP, when its parent dies (PR_SET_PDEATHSIG)
 *       or when --parent-pid disappears (checked every 2 s).
 *
 *   fl-winexec <WinExe> [args...]       re-enter Wine: wine <WinExe> <args as Windows paths>
 *   fl-askpass <prompt>                 wine $FL_ASKPASS_TARGET <prompt>
 *   fl-ssh-askpass <prompt>             wine $FL_SSH_ASKPASS_TARGET <prompt>
 *
 * Frames and the REQ encoding are the frozen contract of bridge/common/fl_proto.h.
 * The spike B2 fixes kept here: a SIGCHLD self-pipe in the poll() set instead of
 * timers (fix 3) and the lingering close after the last frame (fix 2).
 * See bridge/unix/README.md for the CLI and lifecycle.
 */
#define _GNU_SOURCE
#include <arpa/inet.h>
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <poll.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/prctl.h>
#include <sys/random.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#include "fl_helper_util.h"
#include "fl_proto.h"
#include "fl_sha256.h"

#ifndef FL_VERSION
#define FL_VERSION "unknown"
#endif

extern char **environ;

#define HDR_LEN 5u
#define AUTH_TIMEOUT_MS 5000
#define REQ_TIMEOUT_MS 10000
#define KILL_GRACE_MS 3000
#define DRAIN_GRACE_MS 2000
#define LINGER_MS 2000
#define PARENT_POLL_MS 2000
#define MAX_SESSIONS 256
#define STDIN_CAP (1024u * 1024u)
#define RX_CAP (4u * (HDR_LEN + FL_MAX_CHUNK))
#define PIPE_SIZE (256 * 1024)
#define MSG_MAX 512u

static const char *g_prog = "fl-bridge-helper";
static int g_log_fd = -1;
static int g_sigpipe[2] = { -1, -1 };   /* self-pipe: 'C' = SIGCHLD, 'T' = terminate */
static uint8_t g_key[FL_KEY_LEN];
static char g_host_helper[PATH_MAX];
static int g_have_host_helper;

/* ------------------------------------------------------------------------ */
/* small utilities                                                           */
/* ------------------------------------------------------------------------ */

static int64_t now_ms(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (int64_t)ts.tv_sec * 1000 + (int64_t)(ts.tv_nsec / 1000000);
}

static void wipe(void *p, size_t n)
{
    explicit_bzero(p, n);
}

static int write_all(int fd, const void *buf, size_t len)
{
    const char *p = buf;
    while (len > 0) {
        ssize_t n = write(fd, p, len);
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

/* One stderr line under construction: at most sizeof(buf) - 2 bytes of text, then '\n'. */
struct line {
    char buf[1024];
    size_t n;
};

/* Append s to the line (truncating at its limit); NULL is skipped. */
static void put_text(struct line *ln, const char *s)
{
    const size_t lim = sizeof(ln->buf) - 2u;
    size_t l;
    if (s == NULL || ln->n >= lim) {
        return;
    }
    l = strnlen(s, lim - ln->n);
    memcpy(ln->buf + ln->n, s, l);
    ln->n += l;
}

/* "<prog>: <a><b><c><d>\n" on stderr (NULL parts are skipped); at most 1 KiB per line. */
static void msg4(const char *a, const char *b, const char *c, const char *d)
{
    struct line ln;
    ln.n = 0;
    put_text(&ln, g_prog);
    put_text(&ln, ": ");
    put_text(&ln, a);
    put_text(&ln, b);
    put_text(&ln, c);
    put_text(&ln, d);
    ln.buf[ln.n] = '\n';
    ln.n++;
    (void)write_all(2, ln.buf, ln.n);
}

static void msg(const char *text)
{
    msg4(text, NULL, NULL, NULL);
}

static void msg2(const char *a, const char *b)
{
    msg4(a, b, NULL, NULL);
}

/* One line in the --log file: "<UTC ISO-8601> fl-bridge-helper[pid] <text>". No secrets. */
static void log_text(const char *text)
{
    struct line ln;
    struct timespec ts;
    struct tm tm;
    int k;
    if (g_log_fd < 0) {
        return;
    }
    clock_gettime(CLOCK_REALTIME, &ts);
    gmtime_r(&ts.tv_sec, &tm);
    ln.n = strftime(ln.buf, sizeof(ln.buf), "%Y-%m-%dT%H:%M:%S", &tm);
    k = snprintf(ln.buf + ln.n, sizeof(ln.buf) - ln.n, ".%03ldZ %s[%ld] ", ts.tv_nsec / 1000000L, g_prog,
        (long)getpid());
    if (k < 0) {
        return;
    }
    ln.n += (size_t)k;
    if (ln.n >= sizeof(ln.buf) - 2) {
        return;
    }
    put_text(&ln, text);
    ln.buf[ln.n] = '\n';
    ln.n++;
    (void)write_all(g_log_fd, ln.buf, ln.n);
}

/* log_text() of "<prefix><number>". */
static void log_num(const char *prefix, long v)
{
    char line[256];
    snprintf(line, sizeof(line), "%s%ld", prefix, v);
    log_text(line);
}

static void on_signal(int sig)
{
    int saved = errno;
    char c = sig == SIGCHLD ? 'C' : 'T';
    ssize_t r = write(g_sigpipe[1], &c, 1);
    (void)r;
    errno = saved;
}

static int set_handler(int sig, void (*fn)(int))
{
    struct sigaction sa;
    memset(&sa, 0, sizeof(sa));
    sa.sa_handler = fn;
    sa.sa_flags = SA_RESTART | (sig == SIGCHLD ? SA_NOCLDSTOP : 0);
    sigemptyset(&sa.sa_mask);
    return sigaction(sig, &sa, NULL);
}

/* Read and discard everything in the self-pipe; returns 1 when a 'T' was seen. */
static int drain_sigpipe(void)
{
    char buf[64];
    int term = 0;
    for (;;) {
        ssize_t n = read(g_sigpipe[0], buf, sizeof(buf));
        if (n < 0 && errno == EINTR) {
            continue;
        }
        if (n <= 0) {
            break;
        }
        for (ssize_t i = 0; i < n; i++) {
            term |= buf[i] == 'T';
        }
    }
    return term;
}

static int new_sigpipe(void)
{
    return pipe2(g_sigpipe, O_CLOEXEC | O_NONBLOCK);
}

static int random_bytes(void *buf, size_t n)
{
    unsigned char *p = buf;
    while (n > 0) {
        ssize_t r = getrandom(p, n, 0);
        if (r < 0 && errno == EINTR) {
            continue;
        }
        if (r <= 0) {
            return -1;
        }
        p += r;
        n -= (size_t)r;
    }
    return 0;
}

/* Make sure fds 0..2 exist (a launcher may start us with them closed). */
static void ensure_std_fds(void)
{
    for (int fd = 0; fd <= 2; fd++) {
        if (fcntl(fd, F_GETFD) < 0 && errno == EBADF) {
            int n = open("/dev/null", O_RDWR);
            if (n >= 0 && n != fd) {
                dup2(n, fd);
                close(n);
            }
        }
    }
}

/* Close every inherited fd >= 3. */
static void close_inherited_fds(void)
{
    DIR *d = opendir("/proc/self/fd");
    const struct dirent *e;
    int self;
    if (d == NULL) {
        for (int fd = 3; fd < 4096; fd++) {
            close(fd);
        }
        return;
    }
    self = dirfd(d);
    e = readdir(d);
    while (e != NULL) {
        char *end;
        long n = strtol(e->d_name, &end, 10);
        if (e->d_name[0] != '.' && *end == '\0' && n > 2 && n != self && n <= INT_MAX) {
            close((int)n);
        }
        e = readdir(d);
    }
    closedir(d);
}

/* ------------------------------------------------------------------------ */
/* socket framing                                                            */
/* ------------------------------------------------------------------------ */

static int send_all(int sock, const void *buf, size_t len)
{
    const unsigned char *p = buf;
    while (len > 0) {
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

static int send_frame(int sock, uint8_t type, const void *payload, uint32_t len)
{
    uint8_t small[HDR_LEN + 256];
    uint8_t *buf = small;
    int rc;
    if (len > sizeof(small) - HDR_LEN) {
        buf = malloc(HDR_LEN + (size_t)len);
        if (buf == NULL) {
            return -1;
        }
    }
    if (fl_hdr_encode(buf, type, len) != 0) {
        if (buf != small) {
            free(buf);
        }
        return -1;
    }
    if (len > 0) {
        memcpy(buf + HDR_LEN, payload, len);
    }
    rc = send_all(sock, buf, HDR_LEN + (size_t)len);
    if (buf != small) {
        free(buf);
    }
    return rc;
}

/* Receive exactly len bytes before the monotonic deadline; 0 on success. */
static int recv_exact(int sock, void *buf, size_t len, int64_t deadline)
{
    unsigned char *p = buf;
    while (len > 0) {
        struct pollfd pf = { .fd = sock, .events = POLLIN, .revents = 0 };
        int64_t left = deadline - now_ms();
        ssize_t n;
        int r;
        if (left <= 0) {
            return -1;
        }
        r = poll(&pf, 1, (int)left);
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

/* Receive one frame whose payload fits in cap bytes; 0 on success. */
static int recv_frame(int sock, uint8_t *type, uint8_t *buf, size_t cap, uint32_t *len, int64_t deadline)
{
    uint8_t hdr[HDR_LEN];
    if (recv_exact(sock, hdr, HDR_LEN, deadline) != 0 || fl_hdr_decode(hdr, type, len) != 0 ||
        *len > cap) {
        return -1;
    }
    return *len ? recv_exact(sock, buf, *len, deadline) : 0;
}

/*
 * Send the last frame, then close without a TCP RST (spike B2 fix 2): shutdown
 * the write side and drain until the peer closes, at most LINGER_MS. Closing with
 * unread data (a STDIN_EOF still in flight) makes Linux send RST, and Wine then
 * fails the shim's pending recv before it has read EXIT.
 */
/* One wait-and-drain round of the lingering close; 1 to keep draining, 0 when done. */
static int linger_step(int sock, int64_t deadline, unsigned char *sink, size_t sinksz)
{
    struct pollfd pf = { .fd = sock, .events = POLLIN, .revents = 0 };
    int64_t left = deadline - now_ms();
    ssize_t n;
    int r;
    if (left <= 0) {
        return 0;
    }
    r = poll(&pf, 1, (int)left);
    if (r < 0 && errno == EINTR) {
        return 1;
    }
    if (r <= 0) {
        return 0;
    }
    n = recv(sock, sink, sinksz, 0);
    if (n < 0 && errno == EINTR) {
        return 1;
    }
    return n > 0;
}

static void finish(int sock, uint8_t type, const void *payload, uint32_t len)
{
    unsigned char sink[4096];
    int64_t deadline = now_ms() + LINGER_MS;
    int more;
    (void)send_frame(sock, type, payload, len);
    shutdown(sock, SHUT_WR);
    do {
        more = linger_step(sock, deadline, sink, sizeof(sink));
    } while (more);
    close(sock);
}

/* ------------------------------------------------------------------------ */
/* spawning                                                                  */
/* ------------------------------------------------------------------------ */

struct spawn_rec {
    int32_t kind;
    int32_t val;
};

enum { REC_CHDIR = 1, REC_EXEC = 2, REC_PID = 3, REC_SETUP = 4 };

/* After fork: report a record on the exec-status pipe (O_CLOEXEC: EOF on exec). */
static void child_report(int fd, int32_t kind, int32_t val)
{
    struct spawn_rec r = { kind, val };
    ssize_t w;
    do {
        w = write(fd, &r, sizeof(r));
    } while (w < 0 && errno == EINTR);
}

static void child_reset_signals(void)
{
    static const int sigs[] = { SIGPIPE, SIGCHLD, SIGTERM, SIGINT, SIGHUP, SIGQUIT };
    struct sigaction sa;
    sigset_t none;
    memset(&sa, 0, sizeof(sa));
    sa.sa_handler = SIG_DFL;
    sigemptyset(&sa.sa_mask);
    for (size_t i = 0; i < sizeof(sigs) / sizeof(sigs[0]); i++) {
        sigaction(sigs[i], &sa, NULL);
    }
    sigemptyset(&none);
    sigprocmask(SIG_SETMASK, &none, NULL);
}

/*
 * execve() argv[0] directly when it contains '/', else search the child's PATH
 * (the daemon's own PATH). Empty and relative PATH entries are skipped so a repo
 * cannot plant a "git" in the cwd, and ENOEXEC is reported instead of falling back
 * to /bin/sh: nothing ever runs through a shell. Returns with errno set.
 */
static void exec_search(char *const argv[], char *const envp[])
{
    const char *file = argv[0];
    const char *path;
    char buf[PATH_MAX];
    int saw_eacces = 0;
    if (file[0] == '\0') {
        errno = ENOENT;
        return;
    }
    if (strchr(file, '/') != NULL) {
        execve(file, argv, envp);
        return;
    }
    path = fl_env_get(envp, "PATH");
    if (path == NULL) {
        path = "/usr/local/bin:/usr/bin:/bin";
    }
    for (const char *p = path;;) {
        const char *end = strchr(p, ':');
        size_t len = end ? (size_t)(end - p) : strlen(p);
        if (len > 0 && p[0] == '/' && fl_path_join(p, len, file, buf, sizeof(buf)) == 0) {
            execve(buf, argv, envp);
            if (errno == EACCES) {
                saw_eacces = 1;
            } else if (errno != ENOENT && errno != ENOTDIR && errno != ELOOP && errno != ENAMETOOLONG) {
                return;
            }
        }
        if (end == NULL) {
            break;
        }
        p = end + 1;
    }
    errno = saw_eacces ? EACCES : ENOENT;
}

static void child_exec(const char *cwd, char *const argv[], char *const envp[], int rep)
{
    if (chdir(cwd) != 0) {
        child_report(rep, REC_CHDIR, errno);
        _exit(127);
    }
    exec_search(argv, envp);
    child_report(rep, REC_EXEC, errno);
    _exit(127);
}

/* Collect the exec-status records until EOF. */
static void read_records(int fd, int32_t *err_kind, int32_t *err_no, pid_t *pid)
{
    for (;;) {
        struct spawn_rec r;
        ssize_t n = read(fd, &r, sizeof(r));
        if (n < 0 && errno == EINTR) {
            continue;
        }
        if (n != (ssize_t)sizeof(r)) {
            break;
        }
        if (r.kind == REC_PID) {
            *pid = (pid_t)r.val;
        } else if (*err_kind == 0) {
            *err_kind = r.kind;
            *err_no = r.val ? r.val : EINVAL;
        }
    }
    close(fd);
}

/* waitpid() for pid, retried on EINTR. */
static pid_t wait_blocking(pid_t pid, int *status)
{
    pid_t w;
    do {
        w = waitpid(pid, status, 0);
    } while (w < 0 && errno == EINTR);
    return w;
}

static void close_pair(int p[2])
{
    if (p[0] >= 0) {
        close(p[0]);
    }
    if (p[1] >= 0) {
        close(p[1]);
    }
    p[0] = -1;
    p[1] = -1;
}

/* What the session's log line reports (names and numbers only). */
struct sess_info {
    char peer[64];
    char argv0[64];
    size_t argc;
    size_t env_ignored;
    uint32_t flags;
    int64_t start_ms;
};

struct sess {
    int sock;
    pid_t child;   /* attached child, also its process-group id; -1 when none */
    int exited;
    int wstatus;
    int64_t exit_ms;
    int in_fd;
    int out_fd;
    int err_fd;
    int stdin_eof;
    uint8_t *rx;
    size_t rxlen;
    uint8_t *inb;
    size_t inoff;
    size_t inlen;
    uint8_t *obuf;
    struct sess_info info;
};

/*
 * Attached spawn: child in its own process group with PR_SET_PDEATHSIG(SIGKILL),
 * stdio on three O_CLOEXEC pipes, exec errors reported on a fourth.
 * 0 on success, else *err_kind / *err_no describe the failure.
 */
static int spawn_attached(struct sess *s, const char *cwd, char **argv, char **envp, int32_t *err_kind,
                          int32_t *err_no)
{
    int in[2] = { -1, -1 };
    int out[2] = { -1, -1 };
    int er[2] = { -1, -1 };
    int rep[2] = { -1, -1 };
    pid_t self = getpid();
    pid_t pid;
    pid_t unused = -1;
    if (pipe2(in, O_CLOEXEC) || pipe2(out, O_CLOEXEC) || pipe2(er, O_CLOEXEC) || pipe2(rep, O_CLOEXEC)) {
        *err_kind = REC_SETUP;
        *err_no = errno;
        close_pair(in);
        close_pair(out);
        close_pair(er);
        close_pair(rep);
        return -1;
    }
    pid = fork();
    if (pid < 0) {
        *err_kind = REC_SETUP;
        *err_no = errno;
        close_pair(in);
        close_pair(out);
        close_pair(er);
        close_pair(rep);
        return -1;
    }
    if (pid == 0) {
        setpgid(0, 0);
        prctl(PR_SET_PDEATHSIG, SIGKILL);
        if (getppid() != self) {
            _exit(125);
        }
        if (dup2(in[0], 0) < 0 || dup2(out[1], 1) < 0 || dup2(er[1], 2) < 0) {
            child_report(rep[1], REC_SETUP, errno);
            _exit(127);
        }
        child_reset_signals();
        child_exec(cwd, argv, envp, rep[1]);
    }
    setpgid(pid, pid);   /* also done by the child; whichever runs first wins */
    close(in[0]);
    close(out[1]);
    close(er[1]);
    close(rep[1]);
    *err_kind = 0;
    *err_no = 0;
    read_records(rep[0], err_kind, err_no, &unused);
    if (*err_kind != 0) {
        close(in[1]);
        close(out[0]);
        close(er[0]);
        (void)wait_blocking(pid, NULL);
        return -1;
    }
    s->child = pid;
    s->in_fd = in[1];
    s->out_fd = out[0];
    s->err_fd = er[0];
    fcntl(s->in_fd, F_SETFL, O_NONBLOCK);
    fcntl(s->out_fd, F_SETFL, O_NONBLOCK);
    fcntl(s->err_fd, F_SETFL, O_NONBLOCK);
    (void)fcntl(s->out_fd, F_SETPIPE_SZ, PIPE_SIZE);
    (void)fcntl(s->err_fd, F_SETPIPE_SZ, PIPE_SIZE);
    return 0;
}

/*
 * Detached spawn (FL_REQ_DETACH): double fork, the intermediate calls setsid and
 * exits, the grandchild gets /dev/null on stdin/stdout (stderr stays the daemon's,
 * i.e. the launcher's log) and no PDEATHSIG, so it outlives the session.
 * 0 on success with *pid set, else *err_kind / *err_no.
 */
static int spawn_detached(const char *cwd, char **argv, char **envp, pid_t *pid, int32_t *err_kind,
                          int32_t *err_no)
{
    int rep[2] = { -1, -1 };
    pid_t mid;
    *pid = -1;
    if (pipe2(rep, O_CLOEXEC)) {
        *err_kind = REC_SETUP;
        *err_no = errno;
        return -1;
    }
    mid = fork();
    if (mid < 0) {
        *err_kind = REC_SETUP;
        *err_no = errno;
        close_pair(rep);
        return -1;
    }
    if (mid == 0) {
        pid_t g;
        if (setsid() < 0) {
            child_report(rep[1], REC_SETUP, errno);
            _exit(127);
        }
        g = fork();
        if (g < 0) {
            child_report(rep[1], REC_SETUP, errno);
            _exit(127);
        }
        if (g == 0) {
            int nul = open("/dev/null", O_RDWR | O_CLOEXEC);
            if (nul < 0 || dup2(nul, 0) < 0 || dup2(nul, 1) < 0) {
                child_report(rep[1], REC_SETUP, errno);
                _exit(127);
            }
            child_reset_signals();
            child_exec(cwd, argv, envp, rep[1]);
        }
        child_report(rep[1], REC_PID, (int32_t)g);
        _exit(0);
    }
    close(rep[1]);
    *err_kind = 0;
    *err_no = 0;
    read_records(rep[0], err_kind, err_no, pid);
    (void)wait_blocking(mid, NULL);
    if (*err_kind == 0 && *pid <= 0) {
        *err_kind = REC_SETUP;
        *err_no = ECHILD;
    }
    return *err_kind ? -1 : 0;
}

/* ------------------------------------------------------------------------ */
/* session                                                                   */
/* ------------------------------------------------------------------------ */

static void reap_child(struct sess *s)
{
    int st;
    pid_t w;
    if (s->child <= 0 || s->exited) {
        return;
    }
    do {
        w = waitpid(s->child, &st, WNOHANG);
    } while (w < 0 && errno == EINTR);
    if (w == s->child) {
        s->exited = 1;
        s->wstatus = st;
        s->exit_ms = now_ms();
    }
}

/*
 * The shim went away: SIGTERM the child's process group, wait until the group is
 * empty (the child is reaped through the SIGCHLD self-pipe), SIGKILL whatever is
 * left after KILL_GRACE_MS.
 */
static void kill_group(struct sess *s)
{
    int64_t deadline;
    if (s->child <= 0) {
        return;
    }
    reap_child(s);
    /* Already reaped and its group is empty: the pid may belong to someone else now. */
    if (s->exited && kill(-s->child, 0) < 0 && errno == ESRCH) {
        return;
    }
    kill(-s->child, SIGTERM);
    deadline = now_ms() + KILL_GRACE_MS;
    for (;;) {
        struct pollfd pf = { .fd = g_sigpipe[0], .events = POLLIN, .revents = 0 };
        int64_t left;
        reap_child(s);
        if (s->exited && kill(-s->child, 0) < 0 && errno == ESRCH) {
            return;
        }
        left = deadline - now_ms();
        if (left <= 0) {
            break;
        }
        if (poll(&pf, 1, (int)(left < 50 ? left : 50)) > 0) {
            (void)drain_sigpipe();
        }
    }
    kill(-s->child, SIGKILL);
    if (!s->exited) {
        int st = 0;
        (void)wait_blocking(s->child, &st);
        s->exited = 1;
        s->wstatus = st;
    }
}

/* The one log line of a session (names and numbers only, never values or data). */
static void session_log(const struct sess *s, const char *result)
{
    char line[1024];
    snprintf(line, sizeof(line), "session peer=%s argv0=%s argc=%zu flags=0x%x env-ignored=%zu result=%s ms=%lld",
             s->info.peer, s->info.argv0[0] ? s->info.argv0 : "-", s->info.argc, (unsigned)s->info.flags,
             s->info.env_ignored, result, (long long)(now_ms() - s->info.start_ms));
    log_text(line);
}

/* SIGNAL frame: deliver an allowed signal to the child's process group. */
static void on_signal_frame(struct sess *s, int signo)
{
    if (signo == FL_SIGPIPE_NO) {
        /* The Windows-side stdout reader is gone: close our read end so the child
         * gets EPIPE / SIGPIPE on its next write, exactly as with a closed pipe. */
        if (s->out_fd >= 0) {
            close(s->out_fd);
            s->out_fd = -1;
        }
        return;
    }
    if (fl_signal_allowed(signo) && s->child > 0 && !s->exited) {
        kill(-s->child, signo);
    }
}

/* Queue a STDIN payload for the child; 0 consumed (or dropped), 1 no room yet (backpressure). */
static int queue_stdin(struct sess *s, const uint8_t *d, uint32_t len)
{
    if (s->in_fd < 0 || s->stdin_eof || len == 0) {
        return 0;
    }
    if (STDIN_CAP - s->inlen < len) {
        return 1; /* backpressure: wait until the child drains its stdin */
    }
    if (STDIN_CAP - s->inoff - s->inlen < len) {
        memmove(s->inb, s->inb + s->inoff, s->inlen);
        s->inoff = 0;
    }
    memcpy(s->inb + s->inoff + s->inlen, d, len);
    s->inlen += len;
    return 0;
}

/* One complete client frame; 0 consumed, 1 wait, -1 protocol violation. */
static int handle_frame(struct sess *s, uint8_t type, const uint8_t *payload, uint32_t len)
{
    if (type == FL_F_STDIN) {
        return queue_stdin(s, payload, len);
    }
    if (type == FL_F_STDIN_EOF) {
        if (len != 0) {
            return -1;
        }
        s->stdin_eof = 1;
        return 0;
    }
    if (len != 1) {
        return -1;
    }
    on_signal_frame(s, payload[0]);
    return 0;
}

/* The frame header at rx[off]: 0 a complete frame, 1 incomplete, -1 protocol violation. */
static int next_frame(const struct sess *s, size_t off, uint8_t *type, uint32_t *len)
{
    if (s->rxlen - off < HDR_LEN) {
        return 1;
    }
    if (fl_hdr_decode(s->rx + off, type, len) != 0 || *len > FL_MAX_CHUNK) {
        return -1;
    }
    if (*type != FL_F_STDIN && *type != FL_F_STDIN_EOF && *type != FL_F_SIGNAL) {
        return -1;
    }
    return s->rxlen - off - HDR_LEN < *len ? 1 : 0;
}

/* Consume complete frames from rx; -1 on a protocol violation. */
static int parse_frames(struct sess *s)
{
    size_t off = 0;
    int r = 0;
    while (r == 0) {
        uint8_t type = 0;
        uint32_t len = 0;
        r = next_frame(s, off, &type, &len);
        if (r == 0) {
            r = handle_frame(s, type, s->rx + off + HDR_LEN, len);
        }
        if (r == 0) {
            off += HDR_LEN + len;
        }
    }
    if (r < 0) {
        return -1;
    }
    if (off > 0) {
        memmove(s->rx, s->rx + off, s->rxlen - off);
        s->rxlen -= off;
    }
    return 0;
}

static void close_stdin(struct sess *s)
{
    if (s->in_fd >= 0) {
        close(s->in_fd);
        s->in_fd = -1;
    }
    s->inoff = 0;
    s->inlen = 0;
}

static void write_stdin(struct sess *s)
{
    ssize_t w = write(s->in_fd, s->inb + s->inoff, s->inlen);
    if (w > 0) {
        s->inoff += (size_t)w;
        s->inlen -= (size_t)w;
        if (s->inlen == 0) {
            s->inoff = 0;
        }
    } else if (w < 0 && errno != EAGAIN && errno != EINTR) {
        close_stdin(s); /* the child closed its stdin (EPIPE): drop the rest */
    }
}

/* Forward one read of a child pipe as a frame; -1 when the socket is gone. */
static int forward(struct sess *s, int *fd, uint8_t type)
{
    ssize_t n = read(*fd, s->obuf + HDR_LEN, FL_MAX_CHUNK);
    if (n > 0) {
        if (fl_hdr_encode(s->obuf, type, (uint32_t)n) != 0) {
            return -1;
        }
        return send_all(s->sock, s->obuf, HDR_LEN + (size_t)n);
    }
    if (n < 0 && (errno == EINTR || errno == EAGAIN)) {
        return 0;
    }
    close(*fd);
    *fd = -1;
    return 0;
}

static void close_outputs(struct sess *s)
{
    if (s->out_fd >= 0) {
        close(s->out_fd);
        s->out_fd = -1;
    }
    if (s->err_fd >= 0) {
        close(s->err_fd);
        s->err_fd = -1;
    }
}

/* The poll() set of one relay round, with the slot of each optional fd (-1: not polled). */
struct relay_set {
    struct pollfd p[5];
    nfds_t n;
    int ii;
    int io;
    int ie;
};

static int relay_add(struct relay_set *rs, int fd, short events)
{
    int idx = (int)rs->n;
    rs->p[rs->n] = (struct pollfd){ .fd = fd, .events = events, .revents = 0 };
    rs->n++;
    return idx;
}

static void relay_fill(const struct sess *s, struct relay_set *rs)
{
    rs->n = 0;
    rs->ii = -1;
    rs->io = -1;
    rs->ie = -1;
    (void)relay_add(rs, g_sigpipe[0], POLLIN);
    (void)relay_add(rs, s->sock, (short)(POLLRDHUP | (s->rxlen < RX_CAP ? POLLIN : 0)));
    if (s->in_fd >= 0 && s->inlen > 0) {
        rs->ii = relay_add(rs, s->in_fd, POLLOUT);
    }
    if (s->out_fd >= 0) {
        rs->io = relay_add(rs, s->out_fd, POLLIN);
    }
    if (s->err_fd >= 0) {
        rs->ie = relay_add(rs, s->err_fd, POLLIN);
    }
}

/* poll() timeout: none while the child runs, the rest of the drain grace once it exited. */
static int relay_timeout(const struct sess *s)
{
    int64_t left;
    if (!s->exited) {
        return -1;
    }
    left = s->exit_ms + DRAIN_GRACE_MS - now_ms();
    return left > 0 ? (int)left : 0;
}

/* The socket's events: read into rx and parse. NULL ok, else why the session ends. */
static const char *relay_socket(struct sess *s, short revents)
{
    if (revents & (POLLERR | POLLHUP | POLLRDHUP)) {
        return "peer-closed";
    }
    if (revents & POLLIN) {
        ssize_t n = recv(s->sock, s->rx + s->rxlen, RX_CAP - s->rxlen, 0);
        if (n == 0 || (n < 0 && errno != EINTR && errno != EAGAIN)) {
            return "peer-closed";
        }
        if (n > 0) {
            s->rxlen += (size_t)n;
        }
    }
    return parse_frames(s) != 0 ? "protocol-error" : NULL;
}

/* Feed the child's stdin (then re-parse frames held back by backpressure); close it at EOF. */
static const char *relay_stdin(struct sess *s, const struct relay_set *rs)
{
    if (rs->ii >= 0 && s->in_fd >= 0 && (rs->p[rs->ii].revents & (POLLOUT | POLLERR | POLLHUP))) {
        write_stdin(s);
        if (parse_frames(s) != 0) {
            return "protocol-error";
        }
    }
    if (s->in_fd >= 0 && s->stdin_eof && s->inlen == 0) {
        close_stdin(s);
    }
    return NULL;
}

static int output_ready(const struct relay_set *rs, int idx, int fd)
{
    return idx >= 0 && fd >= 0 && (rs->p[idx].revents & (POLLIN | POLLHUP | POLLERR)) != 0;
}

static const char *relay_outputs(struct sess *s, const struct relay_set *rs)
{
    if (output_ready(rs, rs->io, s->out_fd) && forward(s, &s->out_fd, FL_F_STDOUT) != 0) {
        return "peer-closed";
    }
    if (output_ready(rs, rs->ie, s->err_fd) && forward(s, &s->err_fd, FL_F_STDERR) != 0) {
        return "peer-closed";
    }
    return NULL;
}

static const char *relay_events(struct sess *s, const struct relay_set *rs)
{
    const char *fail;
    if (rs->p[0].revents != 0) {
        (void)drain_sigpipe();
        reap_child(s);
    }
    fail = relay_socket(s, rs->p[1].revents);
    if (fail == NULL) {
        fail = relay_stdin(s, rs);
    }
    if (fail == NULL) {
        fail = relay_outputs(s, rs);
    }
    return fail;
}

/* One poll round: 1 keep going, 0 the child finished, -1 the session ended (*why). */
static int relay_step(struct sess *s, const char **why)
{
    struct relay_set rs;
    const char *fail;
    int r;
    relay_fill(s, &rs);
    r = poll(rs.p, rs.n, relay_timeout(s));
    if (r < 0 && errno == EINTR) {
        return 1;
    }
    fail = r < 0 ? "poll-error" : relay_events(s, &rs);
    if (fail != NULL) {
        *why = fail;
        kill_group(s);
        return -1;
    }
    if (s->exited && ((s->out_fd < 0 && s->err_fd < 0) || now_ms() >= s->exit_ms + DRAIN_GRACE_MS)) {
        close_outputs(s);
        close_stdin(s);
        return 0;
    }
    return 1;
}

/*
 * The relay: socket -> child stdin (non-blocking, 1 MiB buffer, backpressure),
 * child stdout/stderr -> frames, SIGNAL frames -> killpg, SIGCHLD self-pipe ->
 * reap. Once the child has exited its pipes are drained for at most
 * DRAIN_GRACE_MS (a grandchild may keep them open). 0 when the child finished
 * (s->wstatus valid), -1 when the session ended otherwise (*why says how).
 */
static int relay(struct sess *s, const char **why)
{
    int r;
    do {
        r = relay_step(s, why);
    } while (r > 0);
    return r;
}

/* Mutual authentication; 0 on success, else *why names the failure. */
static int handshake(int sock, const char **why)
{
    int64_t deadline = now_ms() + AUTH_TIMEOUT_MS;
    uint8_t buf[64];
    uint8_t cn[FL_NONCE_LEN];
    uint8_t sn[FL_NONCE_LEN];
    uint8_t mac[FL_MAC_LEN];
    uint8_t chal[FL_CHALLENGE_LEN];
    uint8_t type;
    uint32_t len;
    int rc = -1;
    if (recv_frame(sock, &type, buf, sizeof(buf), &len, deadline) != 0) {
        *why = "no-hello";
    } else if (type != FL_F_HELLO || fl_hello_parse(buf, len, cn) != 0) {
        *why = "bad-hello";
    } else if (random_bytes(sn, sizeof(sn)) != 0) {
        *why = "no-entropy";
    } else {
        fl_auth_mac(g_key, 1, cn, sn, mac);
        memcpy(chal, sn, FL_NONCE_LEN);
        memcpy(chal + FL_NONCE_LEN, mac, FL_MAC_LEN);
        if (send_frame(sock, FL_F_CHALLENGE, chal, FL_CHALLENGE_LEN) != 0) {
            *why = "send-failed";
        } else if (recv_frame(sock, &type, buf, sizeof(buf), &len, deadline) != 0) {
            *why = "no-auth";
        } else if (type != FL_F_AUTH || len != FL_MAC_LEN) {
            *why = "bad-auth";
        } else {
            fl_auth_mac(g_key, 0, cn, sn, mac);
            if (!fl_ct_equal(buf, mac, FL_MAC_LEN)) {
                *why = "bad-mac";
            } else if (send_frame(sock, FL_F_AUTH_OK, NULL, 0) != 0) {
                *why = "send-failed";
            } else {
                rc = 0;
            }
        }
    }
    wipe(mac, sizeof(mac));
    wipe(chal, sizeof(chal));
    wipe(buf, sizeof(buf));
    return rc;
}

/* Receive and decode REQ; 0 on success. */
static int read_req(int sock, struct fl_req *req)
{
    int64_t deadline = now_ms() + REQ_TIMEOUT_MS;
    uint8_t hdr[HDR_LEN];
    uint8_t type;
    uint32_t len;
    uint8_t *buf;
    int rc;
    if (recv_exact(sock, hdr, HDR_LEN, deadline) != 0 || fl_hdr_decode(hdr, &type, &len) != 0 ||
        type != FL_F_REQ || len == 0 || len > FL_MAX_REQ) {
        return -1;
    }
    buf = malloc(len);
    if (buf == NULL) {
        return -1;
    }
    rc = recv_exact(sock, buf, len, deadline) == 0 && fl_req_decode(buf, len, req) == 0 ? 0 : -1;
    free(buf);
    return rc;
}

static void send_spawn_err(const struct sess *s, int32_t kind, int32_t err, const char *cwd)
{
    uint8_t payload[4 + MSG_MAX];
    uint32_t u = (uint32_t)err;
    int n;
    char result[48];
    payload[0] = (uint8_t)(u >> 24);
    payload[1] = (uint8_t)(u >> 16);
    payload[2] = (uint8_t)(u >> 8);
    payload[3] = (uint8_t)u;
    if (kind == REC_CHDIR) {
        n = snprintf((char *)payload + 4, MSG_MAX, "cannot change to directory '%s': %s", cwd, strerror(err));
    } else if (kind == REC_EXEC) {
        n = snprintf((char *)payload + 4, MSG_MAX, "cannot run '%s': %s", s->info.argv0, strerror(err));
    } else {
        n = snprintf((char *)payload + 4, MSG_MAX, "cannot start '%s': %s", s->info.argv0, strerror(err));
    }
    if (n < 0) {
        n = 0;
    } else if ((size_t)n >= MSG_MAX) {
        n = (int)MSG_MAX - 1;
    }
    finish(s->sock, FL_F_SPAWN_ERR, payload, (uint32_t)(4 + n));
    snprintf(result, sizeof(result), "spawn-error:%d", (int)err);
    session_log(s, result);
}

/* Realpaths of the REQ anchors (relative ones against cwd); NULL entries when unresolved. */
static char **resolve_anchors(const struct fl_req *req)
{
    char **real = calloc(req->nanchors ? req->nanchors : 1, sizeof(char *));
    if (real == NULL) {
        return NULL;
    }
    for (size_t i = 0; i < req->nanchors; i++) {
        const char *a = req->anchors[i];
        if (a[0] == '/') {
            real[i] = realpath(a, NULL);
        } else if (a[0] != '\0') {
            char joined[PATH_MAX];
            if (fl_path_join(req->cwd, strlen(req->cwd), a, joined, sizeof(joined)) == 0) {
                real[i] = realpath(joined, NULL);
            }
        }
    }
    return real;
}

static void free_anchors(char **real, size_t n)
{
    if (real == NULL) {
        return;
    }
    for (size_t i = 0; i < n; i++) {
        free(real[i]);
    }
    free(real);
}

static int send_spawn_ok(const struct sess *s, pid_t pid, char **real, size_t n)
{
    uint8_t *payload = NULL;
    size_t plen = 0;
    int rc;
    if (real == NULL || fl_spawn_ok_encode((uint32_t)pid, real, n, FL_MAX_REQ, &payload, &plen) != 0) {
        /* Too large for one frame: report the pid alone (every anchor unresolved). */
        uint32_t u = (uint32_t)pid;
        uint8_t only[4] = { (uint8_t)(u >> 24), (uint8_t)(u >> 16), (uint8_t)(u >> 8), (uint8_t)u };
        return send_frame(s->sock, FL_F_SPAWN_OK, only, sizeof(only));
    }
    rc = send_frame(s->sock, FL_F_SPAWN_OK, payload, (uint32_t)plen);
    free(payload);
    return rc;
}

static void send_exit(const struct sess *s, int kind, int32_t code)
{
    uint8_t payload[5];
    char result[48];
    if (fl_exit_encode(payload, kind, code) != 0) {
        close(s->sock);
        session_log(s, "exit-encode-failed");
        return;
    }
    finish(s->sock, FL_F_EXIT, payload, sizeof(payload));
    snprintf(result, sizeof(result), "%s:%d", kind ? "signal" : "exit", (int)code);
    session_log(s, result);
}

/* FL_REQ_DETACH: spawn, report SPAWN_OK, then EXIT 0 at once (the child keeps running). */
static void serve_detached(const struct sess *s, const struct fl_req *req, char **argv, char **envp, char **real)
{
    pid_t pid;
    int32_t err_kind = 0;
    int32_t err_no = 0;
    char result[48];
    uint8_t payload[5];
    if (spawn_detached(req->cwd, argv, envp, &pid, &err_kind, &err_no) != 0) {
        send_spawn_err(s, err_kind, err_no, req->cwd);
        return;
    }
    if (send_spawn_ok(s, pid, real, req->nanchors) != 0) {
        close(s->sock);
        session_log(s, "peer-closed");
        return;
    }
    snprintf(result, sizeof(result), "detached:%ld", (long)pid);
    if (fl_exit_encode(payload, 0, 0) == 0) {
        finish(s->sock, FL_F_EXIT, payload, sizeof(payload));
    } else {
        close(s->sock);
    }
    session_log(s, result);
}

/* Attached: spawn, report SPAWN_OK, relay until the child exits, then EXIT. */
static void serve_attached(struct sess *s, const struct fl_req *req, char **argv, char **envp, char **real)
{
    int32_t err_kind = 0;
    int32_t err_no = 0;
    const char *why = "";
    int kind;
    int32_t code;
    if (spawn_attached(s, req->cwd, argv, envp, &err_kind, &err_no) != 0) {
        send_spawn_err(s, err_kind, err_no, req->cwd);
    } else if (send_spawn_ok(s, s->child, real, req->nanchors) != 0) {
        kill_group(s);
        close(s->sock);
        session_log(s, "peer-closed");
    } else if (relay(s, &why) != 0) {
        close(s->sock);
        session_log(s, why);
    } else {
        fl_wait_to_exit(s->wstatus, &kind, &code);
        send_exit(s, kind, code);
    }
}

/* Run one REQ after authentication. */
static void serve(struct sess *s, const struct fl_req *req)
{
    char **argv;
    char **envp = NULL;
    char **real = NULL;
    size_t ignored = 0;
    size_t extra = (req->flags & FL_REQ_HOST_HELPER) ? 1 : 0;

    s->info.argc = req->argc;
    s->info.flags = req->flags;
    snprintf(s->info.argv0, sizeof(s->info.argv0), "%s", req->argc ? fl_basename_any(req->argv[0]) : "");
    fl_log_sanitize(s->info.argv0);
    if (req->cwd == NULL || req->argc == 0) {
        send_spawn_err(s, REC_SETUP, EINVAL, "");
        return;
    }
    if (extra && !g_have_host_helper) {
        send_spawn_err(s, REC_EXEC, ENOENT, req->cwd);
        return;
    }
    argv = calloc(req->argc + extra + 1, sizeof(char *));
    if (argv != NULL && fl_env_build(environ, req->set, req->nset, req->unset, req->nunset, &envp, &ignored) == 0) {
        real = resolve_anchors(req);
    }
    if (real == NULL) {
        free(argv);
        fl_env_free(envp);
        send_spawn_err(s, REC_SETUP, ENOMEM, req->cwd);
        return;
    }
    if (extra) {
        argv[0] = g_host_helper;
    }
    for (size_t i = 0; i < req->argc; i++) {
        argv[i + extra] = req->argv[i];
    }
    s->info.env_ignored = ignored;
    if (req->flags & FL_REQ_DETACH) {
        serve_detached(s, req, argv, envp, real);
    } else {
        serve_attached(s, req, argv, envp, real);
    }
    free_anchors(real, req->nanchors);
    fl_env_free(envp);
    free(argv);
}

/* A forked session process: authenticate, read REQ, serve it. Never returns. */
static _Noreturn void session_main(int sock, const struct sockaddr_in *peer)
{
    struct sess s;
    struct fl_req req;
    const char *why = "";
    int one = 1;
    sigset_t none;
    char addr[INET_ADDRSTRLEN] = "?";

    memset(&s, 0, sizeof(s));
    s.sock = sock;
    s.child = -1;
    s.in_fd = -1;
    s.out_fd = -1;
    s.err_fd = -1;
    s.info.start_ms = now_ms();
    inet_ntop(AF_INET, &peer->sin_addr, addr, sizeof(addr));
    snprintf(s.info.peer, sizeof(s.info.peer), "%s:%u", addr, (unsigned)ntohs(peer->sin_port));

    /* Signals were blocked by the daemon around fork(): install the session's own
     * self-pipe, restore default termination, then unblock. */
    close_pair(g_sigpipe);
    if (new_sigpipe() != 0) {
        _exit(125);
    }
    signal(SIGTERM, SIG_DFL);
    signal(SIGINT, SIG_DFL);
    signal(SIGHUP, SIG_DFL);
    set_handler(SIGCHLD, on_signal);
    sigemptyset(&none);
    sigprocmask(SIG_SETMASK, &none, NULL);
    setsid();
    setsockopt(sock, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));

    if (handshake(sock, &why) != 0) {
        char result[48];
        close(sock);
        snprintf(result, sizeof(result), "auth-failed:%s", why);
        session_log(&s, result);
        _exit(1);
    }
    memset(&req, 0, sizeof(req));
    if (read_req(sock, &req) != 0) {
        close(sock);
        session_log(&s, "bad-request");
        _exit(1);
    }
    s.rx = malloc(RX_CAP);
    s.inb = malloc(STDIN_CAP);
    s.obuf = malloc(HDR_LEN + FL_MAX_CHUNK);
    if (s.rx == NULL || s.inb == NULL || s.obuf == NULL) {
        s.info.argc = req.argc;
        send_spawn_err(&s, REC_SETUP, ENOMEM, req.cwd ? req.cwd : "");
    } else {
        serve(&s, &req);
    }
    fl_req_free(&req);
    free(s.rx);
    free(s.inb);
    free(s.obuf);
    _exit(0);
}

/* ------------------------------------------------------------------------ */
/* daemon                                                                    */
/* ------------------------------------------------------------------------ */

struct opts {
    int daemon;
    const char *token_file;
    const char *host_helper;
    const char *log;
    uint16_t port;
    int32_t parent_pid;
};

/* Read the token file (regular, ours, mode 0600 or stricter), then unlink it. */
static int load_token_file(const char *path)
{
    char buf[128];
    struct stat st;
    ssize_t n;
    int rc;
    int fd = open(path, O_RDONLY | O_CLOEXEC | O_NOFOLLOW | O_NOCTTY);
    if (fd < 0) {
        msg4("cannot open token file ", path, ": ", strerror(errno));
        return -1;
    }
    if (fstat(fd, &st) != 0 || !S_ISREG(st.st_mode) || st.st_uid != geteuid()) {
        msg4("token file ", path, " must be a regular file owned by the current user", NULL);
        close(fd);
        return -1;
    }
    if ((st.st_mode & 077) != 0) {
        msg4("token file ", path, " must not be accessible by group or others (chmod 600)", NULL);
        close(fd);
        unlink(path);
        return -1;
    }
    do {
        n = read(fd, buf, sizeof(buf));
    } while (n < 0 && errno == EINTR);
    close(fd);
    unlink(path);
    rc = n > 0 && fl_token_parse(buf, (size_t)n, g_key) == 0 ? 0 : -1;
    wipe(buf, sizeof(buf));
    if (rc != 0) {
        msg4("token file ", path, " does not hold a 64-character hex token", NULL);
    }
    return rc;
}

/* FL_BRIDGE_TOKEN: parse it (when no token file was given), then scrub it from memory
 * (which also blanks /proc/<pid>/environ) and from the environment. */
static int load_token_env(int need)
{
    char *tok = getenv("FL_BRIDGE_TOKEN");
    int rc = need ? -1 : 0;
    if (tok != NULL) {
        size_t len = strlen(tok);
        if (need) {
            rc = fl_token_parse(tok, len, g_key);
            if (rc != 0) {
                msg("FL_BRIDGE_TOKEN is not a 64-character hex token");
            }
        }
        wipe(tok, len);
        unsetenv("FL_BRIDGE_TOKEN");
    } else if (need) {
        msg("no token: pass --token-file FILE or set FL_BRIDGE_TOKEN");
    }
    return rc;
}

static void usage(int fd)
{
    static const char text[] =
        "usage: fl-bridge-helper --daemon [--token-file FILE] [--port N] [--host-helper PATH]\n"
        "                        [--parent-pid PID] [--log FILE]\n"
        "       fl-bridge-helper --version | --help\n"
        "personas (by argv[0]): fl-winexec <WinExe> [args...], fl-askpass <prompt>,\n"
        "                       fl-ssh-askpass <prompt>\n"
        "The token (64 hex chars) comes from --token-file (read, then unlinked) or\n"
        "FL_BRIDGE_TOKEN (scrubbed after reading). See bridge/unix/README.md.\n";
    (void)write_all(fd, text, sizeof(text) - 1);
}

/* "--name VALUE" or "--name=VALUE"; 1 when argv[*i] is this option (value in *val). */
static int opt_value(const char *name, int argc, char **argv, int *i, const char **val)
{
    size_t n = strlen(name);
    const char *a = argv[*i];
    if (strncmp(a, name, n) != 0) {
        return 0;
    }
    if (a[n] == '=') {
        *val = a + n + 1;
        return 1;
    }
    if (a[n] != '\0') {
        return 0;
    }
    if (*i + 1 >= argc) {
        *val = NULL;
        return 1;
    }
    *i += 1;
    *val = argv[*i];
    return 1;
}

static int set_token_file(struct opts *o, const char *v)
{
    if (v == NULL || v[0] == '\0') {
        return -1;
    }
    o->token_file = v;
    return 0;
}

static int set_port(struct opts *o, const char *v)
{
    return v == NULL || fl_parse_port(v, &o->port) != 0 ? -1 : 0;
}

static int set_host_helper(struct opts *o, const char *v)
{
    if (v == NULL || v[0] != '/') {
        return -1;
    }
    o->host_helper = v;
    return 0;
}

static int set_parent_pid(struct opts *o, const char *v)
{
    return v == NULL || fl_parse_pid(v, &o->parent_pid) != 0 ? -1 : 0;
}

static int set_log(struct opts *o, const char *v)
{
    if (v == NULL || v[0] == '\0') {
        return -1;
    }
    o->log = v;
    return 0;
}

/* The daemon's options that take a value, with the message for a bad one. */
struct opt_def {
    const char *name;
    int (*set)(struct opts *o, const char *v);
    const char *bad;
};

static const struct opt_def k_opt_defs[] = {
    { "--token-file", set_token_file, "--token-file needs a path" },
    { "--port", set_port, "--port needs a number 0..65535" },
    { "--host-helper", set_host_helper, "--host-helper needs an absolute path" },
    { "--parent-pid", set_parent_pid, "--parent-pid needs a positive pid" },
    { "--log", set_log, "--log needs a path" },
};

/* One option at argv[*i] (*i is left on its last word). -1 go on, 1 exit 0, 2 usage error. */
static int parse_one_opt(int argc, char **argv, int *i, struct opts *o)
{
    static const char version[] =
        "fl-bridge-helper " FL_VERSION " (Fork for Linux (unofficial) git bridge, protocol FLB1 v1)\n";
    const char *a = argv[*i];
    if (strcmp(a, "--daemon") == 0) {
        o->daemon = 1;
        return -1;
    }
    if (strcmp(a, "--help") == 0 || strcmp(a, "-h") == 0) {
        usage(1);
        return 1;
    }
    if (strcmp(a, "--version") == 0) {
        (void)write_all(1, version, sizeof(version) - 1);
        return 1;
    }
    for (size_t k = 0; k < sizeof(k_opt_defs) / sizeof(k_opt_defs[0]); k++) {
        const char *v = NULL;
        if (opt_value(k_opt_defs[k].name, argc, argv, i, &v)) {
            if (k_opt_defs[k].set(o, v) != 0) {
                msg(k_opt_defs[k].bad);
                return 2;
            }
            return -1;
        }
    }
    msg2("unknown argument: ", a);
    usage(2);
    return 2;
}

/* 0 ok, 1 exit 0 (help/version printed), 2 usage error. */
static int parse_opts(int argc, char **argv, struct opts *o)
{
    int i = 1;
    int rc = -1;
    memset(o, 0, sizeof(*o));
    while (rc < 0 && i < argc) {
        rc = parse_one_opt(argc, argv, &i, o);
        i++;
    }
    if (rc >= 0) {
        return rc;
    }
    if (!o->daemon) {
        usage(2);
        return 2;
    }
    return 0;
}

static int parent_gone(int32_t pid)
{
    return pid > 0 && kill((pid_t)pid, 0) < 0 && errno == ESRCH;
}

/* Tokens, host helper, log, parent check and cwd: everything before the socket. 0 ok. */
static int daemon_prepare(const struct opts *o)
{
    if (load_token_env(o->token_file == NULL) != 0) {
        return -1;
    }
    if (o->token_file != NULL && load_token_file(o->token_file) != 0) {
        return -1;
    }
    if (o->host_helper != NULL) {
        if (realpath(o->host_helper, g_host_helper) == NULL || access(g_host_helper, X_OK) != 0) {
            msg4("host helper ", o->host_helper, " is not an executable file: ", strerror(errno));
            return -1;
        }
        g_have_host_helper = 1;
    }
    if (o->log != NULL) {
        g_log_fd = open(o->log, O_WRONLY | O_CREAT | O_APPEND | O_CLOEXEC | O_NOFOLLOW | O_NOCTTY, 0600);
        if (g_log_fd < 0) {
            msg4("cannot open log ", o->log, ": ", strerror(errno));
            return -1;
        }
    }
    if (parent_gone(o->parent_pid)) {
        char pid[32];
        snprintf(pid, sizeof(pid), "%ld", (long)o->parent_pid);
        msg4("parent pid ", pid, " does not exist", NULL);
        return -1;
    }
    /* Every relative path is resolved by now: never keep the launcher's cwd busy. */
    if (chdir("/") != 0) {
        msg2("chdir /: ", strerror(errno));
        return -1;
    }
    return 0;
}

/* Listen on 127.0.0.1:port; the bound address in *a. The socket, or -1. */
static int open_listener(uint16_t port, struct sockaddr_in *a)
{
    socklen_t alen = sizeof(*a);
    int one = 1;
    int lst = socket(AF_INET, SOCK_STREAM | SOCK_CLOEXEC | SOCK_NONBLOCK, 0);
    memset(a, 0, sizeof(*a));
    a->sin_family = AF_INET;
    a->sin_port = htons(port);
    a->sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    if (lst < 0) {
        msg2("socket: ", strerror(errno));
        return -1;
    }
    if (port != 0) {
        setsockopt(lst, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
    }
    if (bind(lst, (struct sockaddr *)a, sizeof(*a)) != 0 || listen(lst, 128) != 0 ||
        getsockname(lst, (struct sockaddr *)a, &alen) != 0) {
        char where[32];
        snprintf(where, sizeof(where), "%u", (unsigned)port);
        msg4("cannot listen on 127.0.0.1:", where, ": ", strerror(errno));
        close(lst);
        return -1;
    }
    return lst;
}

/* Self-pipe, handlers, own session, PDEATHSIG; 0 ok (the caller closes lst on error). */
static int daemon_signals(pid_t orig_ppid)
{
    if (new_sigpipe() != 0) {
        msg2("pipe: ", strerror(errno));
        return -1;
    }
    signal(SIGPIPE, SIG_IGN);
    set_handler(SIGTERM, on_signal);
    set_handler(SIGINT, on_signal);
    set_handler(SIGHUP, on_signal);
    set_handler(SIGCHLD, on_signal);
    if (setsid() < 0 && errno != EPERM) {
        msg2("setsid: ", strerror(errno));
    }
    prctl(PR_SET_PDEATHSIG, SIGTERM);
    if (getppid() != orig_ppid) {
        msg("parent exited during start-up");
        return -1;
    }
    return 0;
}

/* Publish the port, then stdin/stdout become /dev/null so the reader sees EOF. 0 ok. */
static int publish_port(unsigned port)
{
    char line[32];
    int nul;
    int n = snprintf(line, sizeof(line), "FL_BRIDGE_PORT=%u\n", port);
    if (n < 0 || write_all(1, line, (size_t)n) != 0) {
        msg("cannot write the port to stdout");
        return -1;
    }
    nul = open("/dev/null", O_RDWR | O_CLOEXEC);
    if (nul >= 0) {
        dup2(nul, 0);
        dup2(nul, 1);
        close(nul);
    }
    return 0;
}

/* The accept loop's state. */
struct daemon_state {
    int lst;
    int nsessions;
    int32_t parent_pid;
    int64_t next_parent_check;
};

/* 1 when the --parent-pid check is due and the parent is gone. */
static int parent_check(struct daemon_state *d)
{
    if (d->parent_pid <= 0 || now_ms() < d->next_parent_check) {
        return 0;
    }
    if (parent_gone(d->parent_pid)) {
        return 1;
    }
    d->next_parent_check = now_ms() + PARENT_POLL_MS;
    return 0;
}

/* The self-pipe fired: reap finished sessions; 1 when a termination signal arrived. */
static int daemon_reap(struct daemon_state *d)
{
    int st;
    int term = drain_sigpipe();
    while (waitpid(-1, &st, WNOHANG) > 0) {
        d->nsessions--;
    }
    return term;
}

/* Fork the session process for the accepted connection c. */
static void fork_session(struct daemon_state *d, int c, const struct sockaddr_in *peer)
{
    sigset_t block;
    sigset_t old;
    pid_t pid;
    sigemptyset(&block);
    sigaddset(&block, SIGCHLD);
    sigaddset(&block, SIGTERM);
    sigaddset(&block, SIGINT);
    sigaddset(&block, SIGHUP);
    sigprocmask(SIG_BLOCK, &block, &old);
    pid = fork();
    if (pid == 0) {
        close(d->lst);
        prctl(PR_SET_PDEATHSIG, 0);
        session_main(c, peer);
    }
    sigprocmask(SIG_SETMASK, &old, NULL);
    if (pid < 0) {
        log_num("daemon fork failed errno=", (long)errno);
    } else {
        d->nsessions++;
    }
    close(c);
}

static void daemon_accept(struct daemon_state *d)
{
    struct sockaddr_in peer;
    socklen_t plen = sizeof(peer);
    int c;
    memset(&peer, 0, sizeof(peer));
    c = accept4(d->lst, (struct sockaddr *)&peer, &plen, SOCK_CLOEXEC);
    if (c < 0) {
        int e = errno;
        if (e == EMFILE || e == ENFILE || e == ENOBUFS || e == ENOMEM) {
            log_num("daemon accept failed errno=", (long)e);
            poll(NULL, 0, 100);
        }
        return;
    }
    if (d->nsessions >= MAX_SESSIONS) {
        char line[64];
        snprintf(line, sizeof(line), "session refused: %d sessions running", d->nsessions);
        log_text(line);
        close(c);
        return;
    }
    fork_session(d, c, &peer);
}

/* One round of the accept loop; 0 when the daemon must exit. */
static int daemon_step(struct daemon_state *d)
{
    struct pollfd p[2] = { { .fd = d->lst, .events = POLLIN, .revents = 0 },
                           { .fd = g_sigpipe[0], .events = POLLIN, .revents = 0 } };
    int timeout = -1;
    int r;
    if (d->parent_pid > 0) {
        int64_t left = d->next_parent_check - now_ms();
        timeout = left > 0 ? (int)left : 0;
    }
    r = poll(p, 2, timeout);
    if (r < 0 && errno != EINTR) {
        log_num("daemon exit reason=poll-error errno=", (long)errno);
        return 0;
    }
    if (parent_check(d)) {
        log_text("daemon exit reason=parent-gone");
        return 0;
    }
    if (r > 0 && p[1].revents != 0 && daemon_reap(d)) {
        log_text("daemon exit reason=signal");
        return 0;
    }
    if (r > 0 && (p[0].revents & POLLIN)) {
        daemon_accept(d);
    }
    return 1;
}

static int run_daemon(const struct opts *o)
{
    struct sockaddr_in a;
    struct daemon_state d;
    pid_t orig_ppid = getppid();
    char line[160];
    int running;

    ensure_std_fds();
    close_inherited_fds();
    if (daemon_prepare(o) != 0) {
        return 1;
    }
    memset(&d, 0, sizeof(d));
    d.parent_pid = o->parent_pid;
    d.lst = open_listener(o->port, &a);
    if (d.lst < 0) {
        return 1;
    }
    if (daemon_signals(orig_ppid) != 0 || publish_port((unsigned)ntohs(a.sin_port)) != 0) {
        close(d.lst);
        return 1;
    }
    snprintf(line, sizeof(line), "daemon start port=%u pid=%ld parent=%ld host-helper=%s",
             (unsigned)ntohs(a.sin_port), (long)getpid(), (long)(o->parent_pid ? o->parent_pid : orig_ppid),
             g_have_host_helper ? "yes" : "no");
    log_text(line);

    d.next_parent_check = now_ms() + PARENT_POLL_MS;
    do {
        running = daemon_step(&d);
    } while (running);
    close(d.lst);
    wipe(g_key, sizeof(g_key));
    return 0;
}

/* ------------------------------------------------------------------------ */
/* personas: fl-winexec, fl-askpass, fl-ssh-askpass                          */
/* ------------------------------------------------------------------------ */

/*
 * Windows form of a fl-winexec argument: absolute Unix paths (except a single
 * component that does not exist, i.e. a "/switch"), and relative ones that name
 * an existing file (editors get ".git/COMMIT_EDITMSG"), are translated; everything
 * else (options, Windows paths, text) is passed unchanged. malloc'd.
 */
static char *winexec_arg(const char *arg, const char *anchors, const char *prefix)
{
    char abs[PATH_MAX];
    char win[PATH_MAX + 8];
    char *real;
    struct stat st;
    int rc;
    if (arg[0] == '/') {
        /* "/w", "/n:3": a single component that does not exist is a Windows switch, not a path. */
        if (strchr(arg + 1, '/') == NULL && lstat(arg, &st) != 0) {
            return strdup(arg);
        }
        if (strlen(arg) >= sizeof(abs)) {
            return strdup(arg);
        }
        memcpy(abs, arg, strlen(arg) + 1);
    } else if (arg[0] != '\0' && arg[0] != '-' && lstat(arg, &st) == 0) {
        char cwd[PATH_MAX];
        if (getcwd(cwd, sizeof(cwd)) == NULL || fl_path_join(cwd, strlen(cwd), arg, abs, sizeof(abs)) != 0) {
            return strdup(arg);
        }
    } else {
        return strdup(arg);
    }
    /* Anchors map physical Unix paths: prefer the realpath when the file exists. */
    real = realpath(abs, NULL);
    rc = fl_unix_to_win(real ? real : abs, anchors, prefix, win, sizeof(win));
    if (rc != 0 && real != NULL) {
        rc = fl_unix_to_win(abs, anchors, prefix, win, sizeof(win));
    }
    free(real);
    return strdup(rc == 0 ? win : arg);
}

static void free_argv(char **v, int n)
{
    if (v == NULL) {
        return;
    }
    for (int i = 1; i < n; i++) {
        free(v[i]);
    }
    free(v);
}

static const char *persona_name(enum fl_persona p)
{
    if (p == FL_PERSONA_WINEXEC) {
        return "fl-winexec";
    }
    return p == FL_PERSONA_ASKPASS ? "fl-askpass" : "fl-ssh-askpass";
}

/* The Windows program to run and the index of its first argument; 0, or the exit code. */
static int persona_target(enum fl_persona p, int argc, char **argv, const char **target, int *first)
{
    const char *var;
    if (p == FL_PERSONA_WINEXEC) {
        if (argc < 2 || argv[1][0] == '\0') {
            msg("usage: fl-winexec <WinExe> [args...]");
            return 2;
        }
        *target = argv[1];
        *first = 2;
        return 0;
    }
    var = p == FL_PERSONA_ASKPASS ? "FL_ASKPASS_TARGET" : "FL_SSH_ASKPASS_TARGET";
    *target = getenv(var);
    if (*target == NULL || (*target)[0] == '\0') {
        msg2(var, " is not set");
        return 127;
    }
    *first = 1;
    return 0;
}

/* nargv[1..nargs+1]: the target and the arguments (fl-winexec translates paths). 0, or -1 (oom). */
static int fill_wine_argv(char **nargv, enum fl_persona p, const char *target, int nargs, char **args,
                          const char *anchors, const char *xprefix)
{
    nargv[1] = target[0] == '/' ? winexec_arg(target, anchors, xprefix) : strdup(target);
    if (nargv[1] == NULL) {
        return -1;
    }
    for (int i = 0; i < nargs; i++) {
        const char *a = args[i];
        nargv[i + 2] = p == FL_PERSONA_WINEXEC ? winexec_arg(a, anchors, xprefix) : strdup(a);
        if (nargv[i + 2] == NULL) {
            return -1;
        }
    }
    return 0;
}

/* exec $FL_WINE (or wine from PATH); only returns on failure, after the message. */
static void exec_wine(char **nargv)
{
    const char *wine = getenv("FL_WINE");
    if (wine != NULL && wine[0] != '\0') {
        execv(wine, nargv);
        msg4("cannot run ", wine, " (FL_WINE): ", strerror(errno));
        return;
    }
    execvp("wine", nargv);
    msg2("cannot run wine (FL_WINE is not set and wine is not on PATH): ", strerror(errno));
}

static int persona_main(enum fl_persona p, int argc, char **argv)
{
    static char wine_argv0[] = "wine";
    const char *target = NULL;
    const char *prefix = getenv("WINEPREFIX");
    const char *anchors = getenv("FL_BRIDGE_ANCHORS");
    char *real_prefix;
    char dll[4096];
    char **nargv;
    int first = 1;
    int nargs;
    int rc;

    g_prog = persona_name(p);
    rc = persona_target(p, argc, argv, &target, &first);
    if (rc != 0) {
        return rc;
    }
    if (prefix == NULL || prefix[0] != '/') {
        msg2("WINEPREFIX is not set to an absolute path; cannot run ", target);
        return 127;
    }
    if (fl_dlloverrides(getenv("WINEDLLOVERRIDES"), dll, sizeof(dll)) != 0 ||
        setenv("WINEDLLOVERRIDES", dll, 1) != 0 || setenv("WINEDEBUG", "-all", 0) != 0) {
        msg("cannot prepare the Wine environment");
        return 127;
    }
    nargs = argc > first ? argc - first : 0;
    nargv = calloc((size_t)nargs + 3, sizeof(char *));
    if (nargv == NULL) {
        msg("out of memory");
        return 127;
    }
    real_prefix = realpath(prefix, NULL);
    nargv[0] = wine_argv0;
    rc = fill_wine_argv(nargv, p, target, nargs, argv + first, anchors, real_prefix ? real_prefix : prefix);
    free(real_prefix);
    if (rc != 0) {
        msg("out of memory");
    } else {
        exec_wine(nargv);
    }
    free_argv(nargv, nargs + 2);
    return 127;
}

int main(int argc, char **argv)
{
    enum fl_persona p = fl_persona_from_argv0(argc > 0 ? argv[0] : NULL);
    struct opts o;
    int rc;
    if (p != FL_PERSONA_HELPER) {
        return persona_main(p, argc, argv);
    }
    rc = parse_opts(argc, argv, &o);
    if (rc == 1) {
        return 0;
    }
    if (rc != 0) {
        return 2;
    }
    return run_daemon(&o);
}
