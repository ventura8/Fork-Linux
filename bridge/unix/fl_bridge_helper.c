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
#include <stdarg.h>
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

/* "<prog>: <message>\n" on stderr. */
static void msg(const char *fmt, ...) __attribute__((format(printf, 1, 2)));
static void msg(const char *fmt, ...)
{
    char buf[1024];
    va_list ap;
    int n = snprintf(buf, sizeof(buf), "%s: ", g_prog);
    if (n < 0) {
        return;
    }
    va_start(ap, fmt);
    vsnprintf(buf + n, sizeof(buf) - (size_t)n - 1, fmt, ap);
    va_end(ap);
    n = (int)strlen(buf);
    buf[n++] = '\n';
    (void)write_all(2, buf, (size_t)n);
}

/* One line in the --log file: "<UTC ISO-8601> fl-bridge-helper[pid] <message>". No secrets. */
static void log_line(const char *fmt, ...) __attribute__((format(printf, 1, 2)));
static void log_line(const char *fmt, ...)
{
    char buf[1024];
    struct timespec ts;
    struct tm tm;
    size_t n;
    va_list ap;
    if (g_log_fd < 0) {
        return;
    }
    clock_gettime(CLOCK_REALTIME, &ts);
    gmtime_r(&ts.tv_sec, &tm);
    n = strftime(buf, sizeof(buf), "%Y-%m-%dT%H:%M:%S", &tm);
    n += (size_t)snprintf(buf + n, sizeof(buf) - n, ".%03ldZ %s[%ld] ", ts.tv_nsec / 1000000L, g_prog,
                          (long)getpid());
    if (n >= sizeof(buf) - 2) {
        return;
    }
    va_start(ap, fmt);
    vsnprintf(buf + n, sizeof(buf) - n - 1, fmt, ap);
    va_end(ap);
    n = strlen(buf);
    buf[n++] = '\n';
    (void)write_all(g_log_fd, buf, n);
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
    struct dirent *e;
    int self;
    if (d == NULL) {
        for (int fd = 3; fd < 4096; fd++) {
            close(fd);
        }
        return;
    }
    self = dirfd(d);
    while ((e = readdir(d)) != NULL) {
        char *end;
        long n = strtol(e->d_name, &end, 10);
        if (e->d_name[0] != '.' && *end == '\0' && n > 2 && n != self && n <= INT_MAX) {
            close((int)n);
        }
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
static void finish(int sock, uint8_t type, const void *payload, uint32_t len)
{
    unsigned char sink[4096];
    int64_t deadline = now_ms() + LINGER_MS;
    (void)send_frame(sock, type, payload, len);
    shutdown(sock, SHUT_WR);
    for (;;) {
        struct pollfd pf = { .fd = sock, .events = POLLIN, .revents = 0 };
        int64_t left = deadline - now_ms();
        ssize_t n;
        int r;
        if (left <= 0) {
            break;
        }
        r = poll(&pf, 1, (int)left);
        if (r < 0 && errno == EINTR) {
            continue;
        }
        if (r <= 0) {
            break;
        }
        n = recv(sock, sink, sizeof(sink), 0);
        if (n < 0 && errno == EINTR) {
            continue;
        }
        if (n <= 0) {
            break;
        }
    }
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

static void close_pair(int p[2])
{
    if (p[0] >= 0) {
        close(p[0]);
    }
    if (p[1] >= 0) {
        close(p[1]);
    }
    p[0] = p[1] = -1;
}

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
    char peer[64];
    char argv0[64];
    size_t argc;
    size_t env_ignored;
    uint32_t flags;
    int64_t start_ms;
};

/*
 * Attached spawn: child in its own process group with PR_SET_PDEATHSIG(SIGKILL),
 * stdio on three O_CLOEXEC pipes, exec errors reported on a fourth.
 * 0 on success, else *err_kind / *err_no describe the failure.
 */
static int spawn_attached(struct sess *s, const char *cwd, char **argv, char **envp, int32_t *err_kind,
                          int32_t *err_no)
{
    int in[2] = { -1, -1 }, out[2] = { -1, -1 }, er[2] = { -1, -1 }, rep[2] = { -1, -1 };
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
        while (waitpid(pid, NULL, 0) < 0 && errno == EINTR) {
        }
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
    while (waitpid(mid, NULL, 0) < 0 && errno == EINTR) {
    }
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
        while (waitpid(s->child, &st, 0) < 0 && errno == EINTR) {
        }
        s->exited = 1;
        s->wstatus = st;
    }
}

/* The one log line of a session (names and numbers only, never values or data). */
static void session_log(const struct sess *s, const char *result)
{
    log_line("session peer=%s argv0=%s argc=%zu flags=0x%x env-ignored=%zu result=%s ms=%lld", s->peer,
             s->argv0[0] ? s->argv0 : "-", s->argc, (unsigned)s->flags, s->env_ignored, result,
             (long long)(now_ms() - s->start_ms));
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

/* Consume complete frames from rx; -1 on a protocol violation. */
static int parse_frames(struct sess *s)
{
    size_t off = 0;
    while (s->rxlen - off >= HDR_LEN) {
        const uint8_t *h = s->rx + off;
        uint8_t type;
        uint32_t len;
        if (fl_hdr_decode(h, &type, &len) != 0 || len > FL_MAX_CHUNK) {
            return -1;
        }
        if (type != FL_F_STDIN && type != FL_F_STDIN_EOF && type != FL_F_SIGNAL) {
            return -1;
        }
        if (s->rxlen - off - HDR_LEN < len) {
            break;
        }
        if (type == FL_F_STDIN) {
            if (s->in_fd >= 0 && !s->stdin_eof && len > 0) {
                if (STDIN_CAP - s->inlen < len) {
                    break; /* backpressure: wait until the child drains its stdin */
                }
                if (STDIN_CAP - s->inoff - s->inlen < len) {
                    memmove(s->inb, s->inb + s->inoff, s->inlen);
                    s->inoff = 0;
                }
                memcpy(s->inb + s->inoff + s->inlen, h + HDR_LEN, len);
                s->inlen += len;
            }
        } else if (type == FL_F_STDIN_EOF) {
            if (len != 0) {
                return -1;
            }
            s->stdin_eof = 1;
        } else {
            if (len != 1) {
                return -1;
            }
            on_signal_frame(s, h[HDR_LEN]);
        }
        off += HDR_LEN + len;
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

/*
 * The relay: socket -> child stdin (non-blocking, 1 MiB buffer, backpressure),
 * child stdout/stderr -> frames, SIGNAL frames -> killpg, SIGCHLD self-pipe ->
 * reap. Once the child has exited its pipes are drained for at most
 * DRAIN_GRACE_MS (a grandchild may keep them open). 0 when the child finished
 * (s->wstatus valid), -1 when the session ended otherwise (*why says how).
 */
static int relay(struct sess *s, const char **why)
{
    for (;;) {
        struct pollfd p[5];
        nfds_t np = 0;
        int ii = -1, io = -1, ie = -1;
        int timeout = -1;
        int r;
        p[np++] = (struct pollfd){ .fd = g_sigpipe[0], .events = POLLIN, .revents = 0 };
        p[np++] = (struct pollfd){ .fd = s->sock,
                                   .events = (short)(POLLRDHUP | (s->rxlen < RX_CAP ? POLLIN : 0)),
                                   .revents = 0 };
        if (s->in_fd >= 0 && s->inlen > 0) {
            ii = (int)np;
            p[np++] = (struct pollfd){ .fd = s->in_fd, .events = POLLOUT, .revents = 0 };
        }
        if (s->out_fd >= 0) {
            io = (int)np;
            p[np++] = (struct pollfd){ .fd = s->out_fd, .events = POLLIN, .revents = 0 };
        }
        if (s->err_fd >= 0) {
            ie = (int)np;
            p[np++] = (struct pollfd){ .fd = s->err_fd, .events = POLLIN, .revents = 0 };
        }
        if (s->exited) {
            int64_t left = s->exit_ms + DRAIN_GRACE_MS - now_ms();
            timeout = left > 0 ? (int)left : 0;
        }
        r = poll(p, np, timeout);
        if (r < 0) {
            if (errno == EINTR) {
                continue;
            }
            *why = "poll-error";
            kill_group(s);
            return -1;
        }
        if (p[0].revents != 0) {
            (void)drain_sigpipe();
            reap_child(s);
        }
        if (p[1].revents & (POLLERR | POLLHUP | POLLRDHUP)) {
            *why = "peer-closed";
            kill_group(s);
            return -1;
        }
        if (p[1].revents & POLLIN) {
            ssize_t n = recv(s->sock, s->rx + s->rxlen, RX_CAP - s->rxlen, 0);
            if (n == 0 || (n < 0 && errno != EINTR && errno != EAGAIN)) {
                *why = "peer-closed";
                kill_group(s);
                return -1;
            }
            if (n > 0) {
                s->rxlen += (size_t)n;
            }
        }
        if (parse_frames(s) != 0) {
            *why = "protocol-error";
            kill_group(s);
            return -1;
        }
        if (ii >= 0 && s->in_fd >= 0 && (p[ii].revents & (POLLOUT | POLLERR | POLLHUP))) {
            write_stdin(s);
            if (parse_frames(s) != 0) {
                *why = "protocol-error";
                kill_group(s);
                return -1;
            }
        }
        if (s->in_fd >= 0 && s->stdin_eof && s->inlen == 0) {
            close_stdin(s);
        }
        if (io >= 0 && s->out_fd >= 0 && (p[io].revents & (POLLIN | POLLHUP | POLLERR)) &&
            forward(s, &s->out_fd, FL_F_STDOUT) != 0) {
            *why = "peer-closed";
            kill_group(s);
            return -1;
        }
        if (ie >= 0 && s->err_fd >= 0 && (p[ie].revents & (POLLIN | POLLHUP | POLLERR)) &&
            forward(s, &s->err_fd, FL_F_STDERR) != 0) {
            *why = "peer-closed";
            kill_group(s);
            return -1;
        }
        if (s->exited && ((s->out_fd < 0 && s->err_fd < 0) || now_ms() >= s->exit_ms + DRAIN_GRACE_MS)) {
            close_outputs(s);
            close_stdin(s);
            return 0;
        }
    }
}

/* Mutual authentication; 0 on success, else *why names the failure. */
static int handshake(int sock, const char **why)
{
    int64_t deadline = now_ms() + AUTH_TIMEOUT_MS;
    uint8_t buf[64];
    uint8_t cn[FL_NONCE_LEN], sn[FL_NONCE_LEN], mac[FL_MAC_LEN];
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

static void send_spawn_err(struct sess *s, int32_t kind, int32_t err, const char *cwd)
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
        n = snprintf((char *)payload + 4, MSG_MAX, "cannot run '%s': %s", s->argv0, strerror(err));
    } else {
        n = snprintf((char *)payload + 4, MSG_MAX, "cannot start '%s': %s", s->argv0, strerror(err));
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

static int send_spawn_ok(struct sess *s, pid_t pid, char **real, size_t n)
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

static void send_exit(struct sess *s, int kind, int32_t code)
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

/* Run one REQ after authentication. */
static void serve(struct sess *s, struct fl_req *req)
{
    char **argv = NULL;
    char **envp = NULL;
    char **real = NULL;
    size_t ignored = 0;
    int32_t err_kind = 0, err_no = 0;
    const char *why = "";
    size_t extra = (req->flags & FL_REQ_HOST_HELPER) ? 1 : 0;

    s->argc = req->argc;
    s->flags = req->flags;
    snprintf(s->argv0, sizeof(s->argv0), "%s", req->argc ? fl_basename_any(req->argv[0]) : "");
    fl_log_sanitize(s->argv0);
    if (req->cwd == NULL || req->argc == 0) {
        send_spawn_err(s, REC_SETUP, EINVAL, "");
        return;
    }
    if (extra && !g_have_host_helper) {
        send_spawn_err(s, REC_EXEC, ENOENT, req->cwd);
        return;
    }
    argv = calloc(req->argc + extra + 1, sizeof(char *));
    if (argv == NULL || fl_env_build(environ, req->set, req->nset, req->unset, req->nunset, &envp, &ignored) != 0 ||
        (real = resolve_anchors(req)) == NULL) {
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
    s->env_ignored = ignored;

    if (req->flags & FL_REQ_DETACH) {
        pid_t pid;
        if (spawn_detached(req->cwd, argv, envp, &pid, &err_kind, &err_no) != 0) {
            send_spawn_err(s, err_kind, err_no, req->cwd);
        } else if (send_spawn_ok(s, pid, real, req->nanchors) != 0) {
            close(s->sock);
            session_log(s, "peer-closed");
        } else {
            char result[48];
            uint8_t payload[5];
            snprintf(result, sizeof(result), "detached:%ld", (long)pid);
            if (fl_exit_encode(payload, 0, 0) == 0) {
                finish(s->sock, FL_F_EXIT, payload, sizeof(payload));
            } else {
                close(s->sock);
            }
            session_log(s, result);
        }
    } else if (spawn_attached(s, req->cwd, argv, envp, &err_kind, &err_no) != 0) {
        send_spawn_err(s, err_kind, err_no, req->cwd);
    } else if (send_spawn_ok(s, s->child, real, req->nanchors) != 0) {
        kill_group(s);
        close(s->sock);
        session_log(s, "peer-closed");
    } else if (relay(s, &why) != 0) {
        close(s->sock);
        session_log(s, why);
    } else {
        int kind;
        int32_t code;
        fl_wait_to_exit(s->wstatus, &kind, &code);
        send_exit(s, kind, code);
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
    s.in_fd = s.out_fd = s.err_fd = -1;
    s.start_ms = now_ms();
    inet_ntop(AF_INET, &peer->sin_addr, addr, sizeof(addr));
    snprintf(s.peer, sizeof(s.peer), "%s:%u", addr, (unsigned)ntohs(peer->sin_port));

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
        s.argc = req.argc;
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
        msg("cannot open token file %s: %s", path, strerror(errno));
        return -1;
    }
    if (fstat(fd, &st) != 0 || !S_ISREG(st.st_mode) || st.st_uid != geteuid()) {
        msg("token file %s must be a regular file owned by the current user", path);
        close(fd);
        return -1;
    }
    if ((st.st_mode & 077) != 0) {
        msg("token file %s must not be accessible by group or others (chmod 600)", path);
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
        msg("token file %s does not hold a 64-character hex token", path);
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
    *val = argv[++*i];
    return 1;
}

/* 0 ok, 1 exit 0 (help/version printed), 2 usage error. */
static int parse_opts(int argc, char **argv, struct opts *o)
{
    memset(o, 0, sizeof(*o));
    for (int i = 1; i < argc; i++) {
        const char *v = NULL;
        if (strcmp(argv[i], "--daemon") == 0) {
            o->daemon = 1;
        } else if (strcmp(argv[i], "--help") == 0 || strcmp(argv[i], "-h") == 0) {
            usage(1);
            return 1;
        } else if (strcmp(argv[i], "--version") == 0) {
            static const char v1[] = "fl-bridge-helper " FL_VERSION " (Fork for Linux (unofficial) git bridge, protocol FLB1 v1)\n";
            (void)write_all(1, v1, sizeof(v1) - 1);
            return 1;
        } else if (opt_value("--token-file", argc, argv, &i, &v)) {
            if (v == NULL || v[0] == '\0') {
                msg("--token-file needs a path");
                return 2;
            }
            o->token_file = v;
        } else if (opt_value("--port", argc, argv, &i, &v)) {
            if (v == NULL || fl_parse_port(v, &o->port) != 0) {
                msg("--port needs a number 0..65535");
                return 2;
            }
        } else if (opt_value("--host-helper", argc, argv, &i, &v)) {
            if (v == NULL || v[0] != '/') {
                msg("--host-helper needs an absolute path");
                return 2;
            }
            o->host_helper = v;
        } else if (opt_value("--parent-pid", argc, argv, &i, &v)) {
            if (v == NULL || fl_parse_pid(v, &o->parent_pid) != 0) {
                msg("--parent-pid needs a positive pid");
                return 2;
            }
        } else if (opt_value("--log", argc, argv, &i, &v)) {
            if (v == NULL || v[0] == '\0') {
                msg("--log needs a path");
                return 2;
            }
            o->log = v;
        } else {
            msg("unknown argument: %s", argv[i]);
            usage(2);
            return 2;
        }
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

static int run_daemon(const struct opts *o)
{
    struct sockaddr_in a;
    socklen_t alen = sizeof(a);
    pid_t orig_ppid = getppid();
    int lst;
    int one = 1;
    int nul;
    int nsessions = 0;
    int64_t next_parent_check;
    char line[32];
    int n;

    ensure_std_fds();
    close_inherited_fds();
    if (load_token_env(o->token_file == NULL) != 0) {
        return 1;
    }
    if (o->token_file != NULL && load_token_file(o->token_file) != 0) {
        return 1;
    }
    if (o->host_helper != NULL) {
        if (realpath(o->host_helper, g_host_helper) == NULL || access(g_host_helper, X_OK) != 0) {
            msg("host helper %s is not an executable file: %s", o->host_helper, strerror(errno));
            return 1;
        }
        g_have_host_helper = 1;
    }
    if (o->log != NULL) {
        g_log_fd = open(o->log, O_WRONLY | O_CREAT | O_APPEND | O_CLOEXEC | O_NOFOLLOW | O_NOCTTY, 0600);
        if (g_log_fd < 0) {
            msg("cannot open log %s: %s", o->log, strerror(errno));
            return 1;
        }
    }
    if (parent_gone(o->parent_pid)) {
        msg("parent pid %ld does not exist", (long)o->parent_pid);
        return 1;
    }
    /* Every relative path is resolved by now: never keep the launcher's cwd busy. */
    if (chdir("/") != 0) {
        msg("chdir /: %s", strerror(errno));
        return 1;
    }

    lst = socket(AF_INET, SOCK_STREAM | SOCK_CLOEXEC | SOCK_NONBLOCK, 0);
    memset(&a, 0, sizeof(a));
    a.sin_family = AF_INET;
    a.sin_port = htons(o->port);
    a.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    if (lst < 0) {
        msg("socket: %s", strerror(errno));
        return 1;
    }
    if (o->port != 0) {
        setsockopt(lst, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
    }
    if (bind(lst, (struct sockaddr *)&a, sizeof(a)) != 0 || listen(lst, 128) != 0 ||
        getsockname(lst, (struct sockaddr *)&a, &alen) != 0) {
        msg("cannot listen on 127.0.0.1:%u: %s", (unsigned)o->port, strerror(errno));
        close(lst);
        return 1;
    }
    if (new_sigpipe() != 0) {
        msg("pipe: %s", strerror(errno));
        close(lst);
        return 1;
    }
    signal(SIGPIPE, SIG_IGN);
    set_handler(SIGTERM, on_signal);
    set_handler(SIGINT, on_signal);
    set_handler(SIGHUP, on_signal);
    set_handler(SIGCHLD, on_signal);
    if (setsid() < 0 && errno != EPERM) {
        msg("setsid: %s", strerror(errno));
    }
    prctl(PR_SET_PDEATHSIG, SIGTERM);
    if (getppid() != orig_ppid) {
        msg("parent exited during start-up");
        close(lst);
        return 1;
    }

    /* Publish the port, then stdin/stdout become /dev/null so the reader sees EOF. */
    n = snprintf(line, sizeof(line), "FL_BRIDGE_PORT=%u\n", (unsigned)ntohs(a.sin_port));
    if (n < 0 || write_all(1, line, (size_t)n) != 0) {
        msg("cannot write the port to stdout");
        close(lst);
        return 1;
    }
    nul = open("/dev/null", O_RDWR | O_CLOEXEC);
    if (nul >= 0) {
        dup2(nul, 0);
        dup2(nul, 1);
        close(nul);
    }
    log_line("daemon start port=%u pid=%ld parent=%ld host-helper=%s", (unsigned)ntohs(a.sin_port),
             (long)getpid(), (long)(o->parent_pid ? o->parent_pid : orig_ppid),
             g_have_host_helper ? "yes" : "no");

    next_parent_check = now_ms() + PARENT_POLL_MS;
    for (;;) {
        struct pollfd p[2] = { { .fd = lst, .events = POLLIN, .revents = 0 },
                               { .fd = g_sigpipe[0], .events = POLLIN, .revents = 0 } };
        int timeout = -1;
        int r;
        if (o->parent_pid > 0) {
            int64_t left = next_parent_check - now_ms();
            timeout = left > 0 ? (int)left : 0;
        }
        r = poll(p, 2, timeout);
        if (r < 0 && errno != EINTR) {
            log_line("daemon exit reason=poll-error errno=%d", errno);
            break;
        }
        if (o->parent_pid > 0 && now_ms() >= next_parent_check) {
            if (parent_gone(o->parent_pid)) {
                log_line("daemon exit reason=parent-gone");
                break;
            }
            next_parent_check = now_ms() + PARENT_POLL_MS;
        }
        if (r > 0 && p[1].revents != 0) {
            int st;
            int term = drain_sigpipe();
            while (waitpid(-1, &st, WNOHANG) > 0) {
                nsessions--;
            }
            if (term) {
                log_line("daemon exit reason=signal");
                break;
            }
        }
        if (r > 0 && (p[0].revents & POLLIN)) {
            struct sockaddr_in peer;
            socklen_t plen = sizeof(peer);
            sigset_t block, old;
            pid_t pid;
            int c = accept4(lst, (struct sockaddr *)&peer, &plen, SOCK_CLOEXEC);
            if (c < 0) {
                if (errno == EMFILE || errno == ENFILE || errno == ENOBUFS || errno == ENOMEM) {
                    log_line("daemon accept failed errno=%d", errno);
                    poll(NULL, 0, 100);
                }
                continue;
            }
            if (nsessions >= MAX_SESSIONS) {
                log_line("session refused: %d sessions running", nsessions);
                close(c);
                continue;
            }
            sigemptyset(&block);
            sigaddset(&block, SIGCHLD);
            sigaddset(&block, SIGTERM);
            sigaddset(&block, SIGINT);
            sigaddset(&block, SIGHUP);
            sigprocmask(SIG_BLOCK, &block, &old);
            pid = fork();
            if (pid == 0) {
                close(lst);
                prctl(PR_SET_PDEATHSIG, 0);
                session_main(c, &peer);
            }
            sigprocmask(SIG_SETMASK, &old, NULL);
            if (pid < 0) {
                log_line("daemon fork failed errno=%d", errno);
            } else {
                nsessions++;
            }
            close(c);
        }
    }
    close(lst);
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

static int persona_main(enum fl_persona p, int argc, char **argv)
{
    static char wine_argv0[] = "wine";
    const char *target;
    const char *prefix = getenv("WINEPREFIX");
    const char *wine = getenv("FL_WINE");
    const char *anchors = getenv("FL_BRIDGE_ANCHORS");
    const char *xprefix;
    char *real_prefix;
    char dll[4096];
    char **nargv;
    int first;
    int nargs;
    int ok = 1;

    g_prog = p == FL_PERSONA_WINEXEC ? "fl-winexec" : p == FL_PERSONA_ASKPASS ? "fl-askpass" : "fl-ssh-askpass";
    if (p == FL_PERSONA_WINEXEC) {
        if (argc < 2 || argv[1][0] == '\0') {
            msg("usage: fl-winexec <WinExe> [args...]");
            return 2;
        }
        target = argv[1];
        first = 2;
    } else {
        const char *var = p == FL_PERSONA_ASKPASS ? "FL_ASKPASS_TARGET" : "FL_SSH_ASKPASS_TARGET";
        target = getenv(var);
        if (target == NULL || target[0] == '\0') {
            msg("%s is not set", var);
            return 127;
        }
        first = 1;
    }
    if (prefix == NULL || prefix[0] != '/') {
        msg("WINEPREFIX is not set to an absolute path; cannot run %s", target);
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
    xprefix = real_prefix ? real_prefix : prefix;
    nargv[0] = wine_argv0;
    nargv[1] = target[0] == '/' ? winexec_arg(target, anchors, xprefix) : strdup(target);
    ok = nargv[1] != NULL;
    for (int i = 0; ok && i < nargs; i++) {
        const char *a = argv[first + i];
        nargv[i + 2] = p == FL_PERSONA_WINEXEC ? winexec_arg(a, anchors, xprefix) : strdup(a);
        ok = nargv[i + 2] != NULL;
    }
    free(real_prefix);
    if (!ok) {
        msg("out of memory");
    } else if (wine != NULL && wine[0] != '\0') {
        execv(wine, nargv);
        msg("cannot run %s (FL_WINE): %s", wine, strerror(errno));
    } else {
        execvp("wine", nargv);
        msg("cannot run wine (FL_WINE is not set and wine is not on PATH): %s", strerror(errno));
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
