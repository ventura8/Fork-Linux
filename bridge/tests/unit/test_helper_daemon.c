/*
 * test_helper_daemon.c - in-process unit tests of the daemon itself (bridge/unix/
 * fl_bridge_helper.c): its static functions are compiled into this test (the file is
 * included below, with main() renamed), and system-call / allocation failures are
 * injected through the linker's --wrap (test_sys.h, test_alloc.h). This reaches the
 * error paths the protocol tests (tests/bridge/test_daemon_protocol.py) cannot provoke:
 * EINTR retries, failing pipe2 / fork / poll / send, out of memory, and so on.
 *
 * Built and run by meson (bridge/tests/meson.build, suite bridge-unit). Exit status 0
 * when every check passes. Children forked here leave through _exit().
 */
#define _GNU_SOURCE

int fl_bridge_helper_main(int argc, char **argv);

#define main fl_bridge_helper_main
#include "fl_bridge_helper.c"
#undef main

#include <sys/un.h>

#include "test_alloc.h"
#include "test_sys.h"

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

static char g_tmp[256];

/* ------------------------------------------------------------------------ */
/* helpers                                                                   */
/* ------------------------------------------------------------------------ */

/* Point fd at /dev/null; returns a copy of the old fd for unquiet(). */
static int quiet(int fd)
{
    int saved = dup(fd);
    int nul = open("/dev/null", O_WRONLY | O_CLOEXEC);
    if (nul >= 0) {
        (void)__real_dup2(nul, fd);
        close(nul);
    }
    return saved;
}

static void unquiet(int fd, int saved)
{
    if (saved >= 0) {
        (void)__real_dup2(saved, fd);
        close(saved);
    }
}

static int socket_pair(int sv[2])
{
    return socketpair(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0, sv);
}

static void write_file(const char *path, const char *text, mode_t mode)
{
    int fd = open(path, O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, mode);
    if (fd >= 0) {
        (void)__real_write(fd, text, strlen(text));
        fchmod(fd, mode);
        close(fd);
    }
}

/* Read everything the peer has (until EOF or 200 ms of silence). */
static size_t drain_peer(int fd, uint8_t *buf, size_t cap)
{
    size_t n = 0;
    for (;;) {
        struct pollfd pf = { .fd = fd, .events = POLLIN, .revents = 0 };
        ssize_t r;
        if (__real_poll(&pf, 1, 200) <= 0 || n == cap) {
            return n;
        }
        r = __real_recv(fd, buf + n, cap - n, 0);
        if (r <= 0) {
            return n;
        }
        n += (size_t)r;
    }
}

/* Wait for pid; its exit status (or -1). */
static int wait_status(pid_t pid)
{
    int st = 0;
    if (__real_waitpid(pid, &st, 0) != pid) {
        return -1;
    }
    return WIFEXITED(st) ? WEXITSTATUS(st) : 128 + WTERMSIG(st);
}

static void put_hdr(uint8_t *p, uint8_t type, uint32_t len)
{
    (void)fl_hdr_encode(p, type, len);
}

static struct sess new_sess(int sock)
{
    struct sess s;
    memset(&s, 0, sizeof(s));
    s.sock = sock;
    s.child = -1;
    s.in_fd = -1;
    s.out_fd = -1;
    s.err_fd = -1;
    s.info.start_ms = now_ms();
    s.rx = malloc(RX_CAP);
    s.inb = malloc(STDIN_CAP);
    s.obuf = malloc(HDR_LEN + FL_MAX_CHUNK);
    return s;
}

static void free_sess(struct sess *s)
{
    free(s->rx);
    free(s->inb);
    free(s->obuf);
    close_outputs(s);
    close_stdin(s);
}

/* ------------------------------------------------------------------------ */
/* small utilities                                                           */
/* ------------------------------------------------------------------------ */

static void test_utilities(void)
{
    int p[2];
    uint8_t buf[16];
    char big[1500];
    const char *old_prog = g_prog;
    int saved;

    CHECK(pipe(p) == 0);
    fl_sys_fail(FL_SYS_WRITE, 1, EINTR);
    CHECK(write_all(p[1], "ab", 2) == 0);
    fl_sys_fail(FL_SYS_WRITE, 1, EIO);
    CHECK(write_all(p[1], "ab", 2) == -1);
    close(p[0]);
    close(p[1]);

    CHECK(new_sigpipe() == 0);
    on_signal(SIGCHLD);
    on_signal(SIGTERM);
    fl_sys_fail(FL_SYS_READ, 1, EINTR);
    CHECK(drain_sigpipe() == 1);
    CHECK(drain_sigpipe() == 0);

    fl_sys_fail(FL_SYS_GETRANDOM, 1, EINTR);
    CHECK(random_bytes(buf, sizeof(buf)) == 0);
    fl_sys_fail(FL_SYS_GETRANDOM, 1, EIO);
    CHECK(random_bytes(buf, sizeof(buf)) == -1);

    memset(big, 'x', sizeof(big) - 1);
    big[sizeof(big) - 1] = '\0';
    saved = quiet(2);
    msg4(big, "tail", NULL, NULL);
    msg2("short ", "message");
    unquiet(2, saved);

    g_log_fd = -1;
    log_text("dropped");
    g_log_fd = open("/dev/null", O_WRONLY | O_CLOEXEC);
    log_text(big);
    log_num("number=", 42);
    g_prog = big;
    log_text("prefix too long");
    g_prog = old_prog;
    close(g_log_fd);
    g_log_fd = -1;
    CHECK(1);
}

/* fd juggling at start-up, in a child (it closes every fd >= 3). */
static void test_std_fds(void)
{
    pid_t pid = __real_fork();
    if (pid == 0) {
        close(0);
        ensure_std_fds();
        fl_sys_fail(FL_SYS_OPENDIR, 1, EMFILE);
        close_inherited_fds();
        close_inherited_fds();
        _exit(fcntl(0, F_GETFD) >= 0 ? 0 : 1);
    }
    CHECK(wait_status(pid) == 0);
}

/* ------------------------------------------------------------------------ */
/* socket framing                                                            */
/* ------------------------------------------------------------------------ */

static void test_framing(void)
{
    int sv[2];
    uint8_t big[300];
    uint8_t in[1024];
    uint8_t type = 0;
    uint32_t len = 0;
    unsigned char sink[64];
    memset(big, 'b', sizeof(big));
    CHECK(socket_pair(sv) == 0);

    fl_sys_fail(FL_SYS_SEND, 1, EINTR);
    CHECK(send_all(sv[0], "x", 1) == 0);
    fl_sys_fail(FL_SYS_SEND, 1, EPIPE);
    CHECK(send_all(sv[0], "x", 1) == -1);
    CHECK(send_frame(sv[0], FL_F_STDOUT, big, sizeof(big)) == 0);
    CHECK(send_frame(sv[0], 0, big, 4) == -1);
    CHECK(send_frame(sv[0], 0, big, sizeof(big)) == -1);
    fl_alloc_fail_at(1);
    CHECK(send_frame(sv[0], FL_F_STDOUT, big, sizeof(big)) == -1);
    fl_alloc_fail_at(0);
    CHECK(drain_peer(sv[1], in, sizeof(in)) == 1 + HDR_LEN + sizeof(big));

    CHECK(recv_exact(sv[0], in, 3, now_ms() - 1) == -1);
    CHECK(__real_write(sv[1], "abc", 3) == 3);
    fl_sys_fail(FL_SYS_POLL, 1, EINTR);
    CHECK(recv_exact(sv[0], in, 3, now_ms() + 1000) == 0);
    fl_sys_fail(FL_SYS_POLL, 1, EBADF);
    CHECK(recv_exact(sv[0], in, 3, now_ms() + 1000) == -1);
    CHECK(recv_exact(sv[0], in, 3, now_ms() + 20) == -1);
    CHECK(__real_write(sv[1], "abc", 3) == 3);
    fl_sys_fail(FL_SYS_RECV, 1, EINTR);
    CHECK(recv_exact(sv[0], in, 3, now_ms() + 1000) == 0);

    put_hdr(in, FL_F_STDOUT, 100);
    CHECK(__real_write(sv[1], in, HDR_LEN) == (ssize_t)HDR_LEN);
    CHECK(recv_frame(sv[0], &type, in, 10, &len, now_ms() + 1000) == -1);
    put_hdr(in, FL_F_STDIN_EOF, 0);
    CHECK(__real_write(sv[1], in, HDR_LEN) == (ssize_t)HDR_LEN);
    CHECK(recv_frame(sv[0], &type, in, 10, &len, now_ms() + 1000) == 0 && type == FL_F_STDIN_EOF && len == 0);

    CHECK(linger_step(sv[0], now_ms() - 1, sink, sizeof(sink)) == 0);
    fl_sys_fail(FL_SYS_POLL, 1, EINTR);
    CHECK(linger_step(sv[0], now_ms() + 1000, sink, sizeof(sink)) == 1);
    CHECK(linger_step(sv[0], now_ms() + 20, sink, sizeof(sink)) == 0);
    CHECK(__real_write(sv[1], "z", 1) == 1);
    fl_sys_fail(FL_SYS_RECV, 1, EINTR);
    CHECK(linger_step(sv[0], now_ms() + 1000, sink, sizeof(sink)) == 1);
    CHECK(linger_step(sv[0], now_ms() + 1000, sink, sizeof(sink)) == 1);

    close(sv[1]);
    CHECK(recv_exact(sv[0], in, 3, now_ms() + 1000) == -1);
    close(sv[0]);
}

/* ------------------------------------------------------------------------ */
/* spawning                                                                  */
/* ------------------------------------------------------------------------ */

static void test_exec_search(void)
{
    char dir[300];
    char path[400];
    char pathvar[700];
    char *argv_empty[] = { "", NULL };
    char *argv_none[] = { "fl-no-such-program-xyz", NULL };
    char *argv_prog[] = { "prog", NULL };
    char *argv_bad[] = { "bad", NULL };
    char *env_none[] = { NULL };
    char *env_path[] = { pathvar, NULL };

    errno = 0;
    exec_search(argv_empty, env_none);
    CHECK(errno == ENOENT);
    exec_search(argv_none, env_none);
    CHECK(errno == ENOENT);

    snprintf(dir, sizeof(dir), "%s/bin", g_tmp);
    mkdir(dir, 0700);
    snprintf(path, sizeof(path), "%s/prog", dir);
    write_file(path, "#!/bin/sh\nexit 0\n", 0644);
    snprintf(path, sizeof(path), "%s/bad", dir);
    write_file(path, "\x01\x02 not a program\n", 0755);
    snprintf(pathvar, sizeof(pathvar), "PATH=rel::/nonexistent:%s", dir);
    exec_search(argv_prog, env_path);
    CHECK(errno == EACCES);
    exec_search(argv_bad, env_path);
    CHECK(errno == ENOEXEC);
}

static void test_records(void)
{
    int p[2];
    struct spawn_rec recs[3] = { { REC_PID, 42 }, { REC_EXEC, 0 }, { REC_CHDIR, 5 } };
    int32_t kind = 0;
    int32_t err = 0;
    pid_t pid = -1;
    int none[2] = { -1, -1 };
    CHECK(pipe(p) == 0);
    fl_sys_fail(FL_SYS_WRITE, 1, EINTR);
    child_report(p[1], recs[0].kind, recs[0].val);
    CHECK(__real_write(p[1], &recs[1], sizeof(recs[1]) * 2) == (ssize_t)(sizeof(recs[1]) * 2));
    close(p[1]);
    fl_sys_fail(FL_SYS_READ, 1, EINTR);
    read_records(p[0], &kind, &err, &pid);
    CHECK(kind == REC_EXEC && err == EINVAL && pid == 42);
    close_pair(none);
    CHECK(none[0] == -1 && none[1] == -1);
}

static void test_spawn_failures(void)
{
    struct sess s = new_sess(-1);
    char *argv_true[] = { "/bin/true", NULL };
    char *env[] = { "PATH=/usr/bin:/bin", NULL };
    int32_t kind = 0;
    int32_t err = 0;
    pid_t pid = -1;

    for (int k = 1; k <= 4; k++) {
        fl_sys_fail(FL_SYS_PIPE2, k, EMFILE);
        CHECK(spawn_attached(&s, "/", argv_true, env, &kind, &err) == -1 && kind == REC_SETUP && err == EMFILE);
    }
    fl_sys_fail(FL_SYS_FORK, 1, EAGAIN);
    CHECK(spawn_attached(&s, "/", argv_true, env, &kind, &err) == -1 && err == EAGAIN);
    fl_sys_fail(FL_SYS_DUP2, 1, EBADF);
    CHECK(spawn_attached(&s, "/", argv_true, env, &kind, &err) == -1 && kind == REC_SETUP && err == EBADF);
    fl_sys_reset();

    fl_sys_fail(FL_SYS_PIPE2, 1, EMFILE);
    CHECK(spawn_detached("/", argv_true, env, &pid, &kind, &err) == -1 && err == EMFILE);
    fl_sys_fail(FL_SYS_FORK, 1, EAGAIN);
    CHECK(spawn_detached("/", argv_true, env, &pid, &kind, &err) == -1 && err == EAGAIN);
    fl_sys_fail(FL_SYS_SETSID, 1, EPERM);
    CHECK(spawn_detached("/", argv_true, env, &pid, &kind, &err) == -1 && kind == REC_SETUP && err == EPERM);
    fl_sys_reset();
    fl_sys_fail(FL_SYS_FORK, 2, EAGAIN); /* the intermediate's fork */
    CHECK(spawn_detached("/", argv_true, env, &pid, &kind, &err) == -1 && kind == REC_SETUP && err == EAGAIN);
    fl_sys_reset();
    fl_sys_fail(FL_SYS_DUP2, 1, EBADF); /* the grandchild's stdio */
    CHECK(spawn_detached("/", argv_true, env, &pid, &kind, &err) == -1 && kind == REC_SETUP && err == EBADF);
    fl_sys_reset();
    CHECK(spawn_detached("/", argv_true, env, &pid, &kind, &err) == 0 && pid > 0);
    free_sess(&s);
}

static void test_children(void)
{
    struct sess s = new_sess(-1);
    char *argv_true[] = { "/bin/true", NULL };
    char *argv_sleep[] = { "/bin/sleep", "30", NULL };
    char *argv_stubborn[] = { "/bin/sh", "-c", "trap '' TERM; sleep 30", NULL };
    char *env[] = { "PATH=/usr/bin:/bin", NULL };
    int32_t kind = 0;
    int32_t err = 0;
    int64_t until;

    kill_group(&s); /* no child */
    reap_child(&s);

    CHECK(spawn_attached(&s, "/", argv_true, env, &kind, &err) == 0);
    until = now_ms() + 5000;
    fl_sys_fail(FL_SYS_WAITPID, 1, EINTR);
    while (!s.exited && now_ms() < until) {
        reap_child(&s);
        poll(NULL, 0, 10);
    }
    CHECK(s.exited);
    kill_group(&s); /* reaped, group empty: nothing to do */
    reap_child(&s);
    close_outputs(&s);
    close_stdin(&s);

    s.exited = 0;
    CHECK(spawn_attached(&s, "/", argv_sleep, env, &kind, &err) == 0);
    kill_group(&s);
    CHECK(s.exited);
    close_outputs(&s);
    close_stdin(&s);

    s.exited = 0;
    CHECK(spawn_attached(&s, "/", argv_stubborn, env, &kind, &err) == 0);
    poll(NULL, 0, 200); /* let the shell install its trap */
    kill_group(&s);
    CHECK(s.exited);
    close_outputs(&s);
    close_stdin(&s);
    (void)drain_sigpipe();
    free_sess(&s);
}

/* ------------------------------------------------------------------------ */
/* frames and the relay                                                      */
/* ------------------------------------------------------------------------ */

static void test_frames(void)
{
    struct sess s = new_sess(-1);
    int p[2];
    uint8_t one = 15;
    CHECK(pipe(p) == 0);

    CHECK(handle_frame(&s, FL_F_STDIN_EOF, NULL, 1) == -1);
    CHECK(handle_frame(&s, FL_F_SIGNAL, &one, 2) == -1);
    CHECK(handle_frame(&s, FL_F_STDIN, &one, 1) == 0); /* no child stdin: dropped */
    on_signal_frame(&s, FL_SIGPIPE_NO);
    s.child = 1;
    s.exited = 1;
    on_signal_frame(&s, SIGINT); /* exited: not delivered */
    s.child = -1;
    s.exited = 0;

    /* invalid header, and a valid frame type the client may not send */
    put_hdr(s.rx, 0, 0);
    s.rxlen = HDR_LEN;
    CHECK(parse_frames(&s) == -1);
    put_hdr(s.rx, FL_F_STDOUT, 0);
    CHECK(parse_frames(&s) == -1);

    /* backpressure: no room in the stdin buffer keeps the frame */
    s.in_fd = p[1];
    s.inlen = STDIN_CAP - 10;
    put_hdr(s.rx, FL_F_STDIN, 100);
    memset(s.rx + HDR_LEN, 'i', 100);
    s.rxlen = HDR_LEN + 100;
    CHECK(parse_frames(&s) == 0 && s.rxlen == HDR_LEN + 100);
    /* room only after moving the pending bytes to the front */
    s.inoff = STDIN_CAP - 50;
    s.inlen = 10;
    CHECK(parse_frames(&s) == 0 && s.rxlen == 0 && s.inoff == 0 && s.inlen == 110);

    fl_sys_fail(FL_SYS_WRITE, 1, EAGAIN);
    write_stdin(&s);
    CHECK(s.in_fd >= 0);
    fl_sys_fail(FL_SYS_WRITE, 1, EPIPE);
    write_stdin(&s);
    CHECK(s.in_fd < 0);

    s.out_fd = p[0];
    fl_sys_fail(FL_SYS_READ, 1, EINTR);
    CHECK(forward(&s, &s.out_fd, FL_F_STDOUT) == 0 && s.out_fd >= 0);
    fl_sys_fail(FL_SYS_READ, 1, EAGAIN);
    CHECK(forward(&s, &s.out_fd, FL_F_STDOUT) == 0 && s.out_fd >= 0);
    fl_sys_fail(FL_SYS_READ, 1, EIO);
    CHECK(forward(&s, &s.out_fd, FL_F_STDOUT) == 0 && s.out_fd < 0);
    s.err_fd = -1;
    on_signal_frame(&s, FL_SIGPIPE_NO);
    free_sess(&s);
}

/* Run argv attached with the relay; the client's frames are pre-written. */
static int relay_run(char **argv, const uint8_t *client, size_t clen, const char **why, enum fl_sys fn, int nth,
                     int err)
{
    int sv[2];
    struct sess s;
    char *env[] = { "PATH=/usr/bin:/bin", NULL };
    int32_t kind = 0;
    int32_t eno = 0;
    int rc = -2;
    uint8_t sink[4096];
    if (socket_pair(sv) != 0) {
        return -2;
    }
    s = new_sess(sv[0]);
    if (clen > 0) {
        (void)__real_send(sv[1], client, clen, MSG_NOSIGNAL);
    }
    if (spawn_attached(&s, "/", argv, env, &kind, &eno) == 0) {
        fl_sys_fail(fn, nth, err);
        rc = relay(&s, why);
        fl_sys_reset();
        kill_group(&s);
    }
    (void)drain_peer(sv[1], sink, sizeof(sink));
    free_sess(&s);
    close(sv[0]);
    close(sv[1]);
    return rc;
}

static void test_relay(void)
{
    char *argv_cat[] = { "/bin/cat", NULL };
    char *argv_out[] = { "/bin/sh", "-c", "sleep 0.2; echo out", NULL };
    char *argv_err[] = { "/bin/sh", "-c", "sleep 0.2; echo err >&2", NULL };
    char *argv_sleep[] = { "/bin/sleep", "5", NULL };
    uint8_t client[64];
    size_t n = 0;
    const char *why = "";

    put_hdr(client, FL_F_STDIN, 5);
    memcpy(client + HDR_LEN, "hello", 5);
    n = HDR_LEN + 5;
    put_hdr(client + n, FL_F_STDIN_EOF, 0);
    n += HDR_LEN;

    CHECK(relay_run(argv_cat, client, n, &why, FL_SYS_COUNT, 0, 0) == 0);
    CHECK(relay_run(argv_cat, client, n, &why, FL_SYS_POLL, 1, EINTR) == 0);
    CHECK(relay_run(argv_cat, client, n, &why, FL_SYS_POLL, 1, EBADF) == -1 && strcmp(why, "poll-error") == 0);
    CHECK(relay_run(argv_sleep, client, n, &why, FL_SYS_RECV, 1, ECONNRESET) == -1 &&
          strcmp(why, "peer-closed") == 0);
    CHECK(relay_run(argv_cat, client, n, &why, FL_SYS_RECV, 1, EINTR) == 0);
    CHECK(relay_run(argv_out, NULL, 0, &why, FL_SYS_SEND, 1, EPIPE) == -1 && strcmp(why, "peer-closed") == 0);
    CHECK(relay_run(argv_err, NULL, 0, &why, FL_SYS_SEND, 1, EPIPE) == -1 && strcmp(why, "peer-closed") == 0);
    memset(client, 0, HDR_LEN); /* frame type 0 */
    CHECK(relay_run(argv_sleep, client, HDR_LEN, &why, FL_SYS_COUNT, 0, 0) == -1 &&
          strcmp(why, "protocol-error") == 0);
}

/* ------------------------------------------------------------------------ */
/* handshake, REQ and the session's replies                                  */
/* ------------------------------------------------------------------------ */

static size_t hello_frame(uint8_t *out, uint8_t cn[FL_NONCE_LEN])
{
    memset(cn, 7, FL_NONCE_LEN);
    put_hdr(out, FL_F_HELLO, FL_HELLO_LEN);
    fl_hello_build(out + HDR_LEN, cn);
    return HDR_LEN + FL_HELLO_LEN;
}

static int handshake_with(const uint8_t *client, size_t n, int shut, enum fl_sys fn, int err, const char **why)
{
    int sv[2];
    int rc;
    uint8_t sink[256];
    if (socket_pair(sv) != 0) {
        return -2;
    }
    (void)__real_send(sv[1], client, n, MSG_NOSIGNAL);
    if (shut) {
        shutdown(sv[1], SHUT_WR);
    }
    fl_sys_fail(fn, 1, err);
    rc = handshake(sv[0], why);
    fl_sys_reset();
    (void)drain_peer(sv[1], sink, sizeof(sink));
    close(sv[0]);
    close(sv[1]);
    return rc;
}

static void test_handshake(void)
{
    uint8_t buf[256];
    uint8_t cn[FL_NONCE_LEN];
    size_t n = hello_frame(buf, cn);
    const char *why = "";
    int sv[2];
    pid_t pid;

    memset(g_key, 3, sizeof(g_key));
    CHECK(handshake_with(buf, n, 1, FL_SYS_GETRANDOM, EIO, &why) == -1 && strcmp(why, "no-entropy") == 0);
    CHECK(handshake_with(buf, n, 1, FL_SYS_SEND, EPIPE, &why) == -1 && strcmp(why, "send-failed") == 0);
    CHECK(handshake_with(buf, n, 1, FL_SYS_COUNT, 0, &why) == -1 && strcmp(why, "no-auth") == 0);
    put_hdr(buf + n, FL_F_AUTH_OK, 0);
    CHECK(handshake_with(buf, n + HDR_LEN, 1, FL_SYS_COUNT, 0, &why) == -1 && strcmp(why, "bad-auth") == 0);
    put_hdr(buf + n, FL_F_AUTH, FL_MAC_LEN);
    memset(buf + n + HDR_LEN, 0, FL_MAC_LEN);
    CHECK(handshake_with(buf, n + HDR_LEN + FL_MAC_LEN, 1, FL_SYS_COUNT, 0, &why) == -1 &&
          strcmp(why, "bad-mac") == 0);

    /* a real client in a child, and AUTH_OK cannot be sent */
    CHECK(socket_pair(sv) == 0);
    pid = __real_fork();
    if (pid == 0) {
        uint8_t chal[HDR_LEN + FL_CHALLENGE_LEN];
        uint8_t auth[HDR_LEN + FL_MAC_LEN];
        close(sv[0]);
        (void)__real_send(sv[1], buf, n, MSG_NOSIGNAL);
        if (recv_exact(sv[1], chal, sizeof(chal), now_ms() + 5000) != 0) {
            _exit(1);
        }
        put_hdr(auth, FL_F_AUTH, FL_MAC_LEN);
        fl_auth_mac(g_key, 0, cn, chal + HDR_LEN, auth + HDR_LEN);
        (void)__real_send(sv[1], auth, sizeof(auth), MSG_NOSIGNAL);
        _exit(0);
    }
    close(sv[1]);
    fl_sys_fail(FL_SYS_SEND, 2, EPIPE);
    CHECK(handshake(sv[0], &why) == -1 && strcmp(why, "send-failed") == 0);
    fl_sys_reset();
    close(sv[0]);
    CHECK(wait_status(pid) == 0);
}

static void test_read_req(void)
{
    int sv[2];
    uint8_t hdr[HDR_LEN];
    struct fl_req req;
    memset(&req, 0, sizeof(req));
    CHECK(socket_pair(sv) == 0);
    put_hdr(hdr, FL_F_REQ, 10);
    (void)__real_send(sv[1], hdr, sizeof(hdr), MSG_NOSIGNAL);
    fl_alloc_fail_at(1);
    CHECK(read_req(sv[0], &req) == -1);
    fl_alloc_fail_at(0);
    close(sv[0]);
    close(sv[1]);
}

static void test_replies(void)
{
    int sv[2];
    struct sess s;
    struct fl_req req;
    char longcwd[700];
    char longrel[PATH_MAX + 10];
    char *anchors[] = { "/", "rel", "", longrel, NULL };
    char **real;
    uint8_t sink[8192];

    CHECK(socket_pair(sv) == 0);
    shutdown(sv[1], SHUT_WR); /* the lingering close ends at once */
    s = new_sess(sv[0]);
    snprintf(s.info.argv0, sizeof(s.info.argv0), "prog");
    memset(longcwd, 'c', sizeof(longcwd) - 1);
    longcwd[0] = '/';
    longcwd[sizeof(longcwd) - 1] = '\0';
    g_log_fd = open("/dev/null", O_WRONLY | O_CLOEXEC);
    send_spawn_err(&s, REC_CHDIR, ENOENT, longcwd);
    CHECK(drain_peer(sv[1], sink, sizeof(sink)) > HDR_LEN);
    close(sv[1]);

    CHECK(socket_pair(sv) == 0);
    shutdown(sv[1], SHUT_WR); /* the lingering close ends at once */
    s.sock = sv[0];
    CHECK(send_spawn_ok(&s, 77, NULL, 0) == 0);
    send_spawn_err(&s, REC_SETUP, ENOMEM, "/");
    CHECK(drain_peer(sv[1], sink, sizeof(sink)) > HDR_LEN);
    close(sv[1]);

    CHECK(socket_pair(sv) == 0);
    shutdown(sv[1], SHUT_WR); /* the lingering close ends at once */
    s.sock = sv[0];
    send_exit(&s, 5, 0); /* invalid kind: closes the socket */
    close(sv[1]);

    memset(longrel, 'r', sizeof(longrel) - 1);
    longrel[sizeof(longrel) - 1] = '\0';
    memset(&req, 0, sizeof(req));
    req.cwd = "/tmp";
    req.nanchors = 4;
    req.anchors = anchors;
    real = resolve_anchors(&req);
    CHECK(real != NULL && real[0] != NULL && real[1] == NULL && real[2] == NULL && real[3] == NULL);
    free_anchors(real, req.nanchors);
    free_anchors(NULL, 0);
    fl_alloc_fail_at(1);
    CHECK(resolve_anchors(&req) == NULL);
    fl_alloc_fail_at(0);
    close(g_log_fd);
    g_log_fd = -1;
    free_sess(&s);
}

/* serve() with the peer already gone, invalid requests and no memory. */
static void test_serve(void)
{
    int sv[2];
    struct sess s;
    struct fl_req req;
    char *argv_true[] = { "/bin/true", NULL };
    char *argv_sleep[] = { "/bin/sleep", "5", NULL };
    char *none[] = { NULL };
    uint8_t sink[4096];

    memset(&req, 0, sizeof(req));
    req.argv = argv_true;
    req.set = none;
    req.unset = none;
    req.anchors = none;

    CHECK(socket_pair(sv) == 0);
    shutdown(sv[1], SHUT_WR); /* the lingering close ends at once */
    s = new_sess(sv[0]);
    serve(&s, &req); /* no cwd */
    CHECK(drain_peer(sv[1], sink, sizeof(sink)) > 0);
    close(sv[1]);

    CHECK(socket_pair(sv) == 0);
    shutdown(sv[1], SHUT_WR); /* the lingering close ends at once */
    s.sock = sv[0];
    req.cwd = "/";
    req.argc = 0;
    serve(&s, &req); /* no argv */
    close(sv[1]);

    CHECK(socket_pair(sv) == 0);
    shutdown(sv[1], SHUT_WR); /* the lingering close ends at once */
    s.sock = sv[0];
    req.argc = 1;
    req.flags = FL_REQ_HOST_HELPER;
    g_have_host_helper = 0;
    serve(&s, &req); /* no host helper configured */
    close(sv[1]);

    CHECK(socket_pair(sv) == 0);
    shutdown(sv[1], SHUT_WR); /* the lingering close ends at once */
    s.sock = sv[0];
    req.flags = 0;
    fl_alloc_fail_at(1);
    serve(&s, &req); /* out of memory */
    fl_alloc_fail_at(0);
    CHECK(drain_peer(sv[1], sink, sizeof(sink)) > 0);
    close(sv[1]);

    /* the peer is gone before SPAWN_OK: detached and attached */
    CHECK(socket_pair(sv) == 0);
    shutdown(sv[1], SHUT_WR); /* the lingering close ends at once */
    s.sock = sv[0];
    close(sv[1]);
    req.flags = FL_REQ_DETACH;
    serve(&s, &req);
    CHECK(socket_pair(sv) == 0);
    shutdown(sv[1], SHUT_WR); /* the lingering close ends at once */
    s.sock = sv[0];
    close(sv[1]);
    req.flags = 0;
    req.argv = argv_sleep;
    req.argc = 2;
    s.exited = 0;
    serve(&s, &req);
    CHECK(s.exited);
    free_sess(&s);
}

/* session_main() without a self-pipe, in a child. */
static void test_session_main(void)
{
    int sv[2];
    pid_t pid;
    struct sockaddr_in peer;
    memset(&peer, 0, sizeof(peer));
    CHECK(socket_pair(sv) == 0);
    pid = __real_fork();
    if (pid == 0) {
        fl_sys_fail(FL_SYS_PIPE2, 1, EMFILE);
        session_main(sv[0], &peer);
    }
    CHECK(wait_status(pid) == 125);
    close(sv[0]);
    close(sv[1]);
}

/* ------------------------------------------------------------------------ */
/* daemon start-up                                                           */
/* ------------------------------------------------------------------------ */

static void test_tokens(void)
{
    char path[400];
    char hex[80];
    int saved = quiet(2);
    memset(hex, 'a', 64);
    hex[64] = '\n';
    hex[65] = '\0';

    snprintf(path, sizeof(path), "%s/missing-token", g_tmp);
    CHECK(load_token_file(path) == -1);
    CHECK(load_token_file(g_tmp) == -1); /* a directory */
    snprintf(path, sizeof(path), "%s/token", g_tmp);
    write_file(path, hex, 0640);
    CHECK(load_token_file(path) == -1); /* group-readable (and unlinked) */
    write_file(path, "not hex\n", 0600);
    CHECK(load_token_file(path) == -1);
    write_file(path, hex, 0600);
    fl_sys_fail(FL_SYS_READ, 1, EINTR);
    CHECK(load_token_file(path) == 0);

    unsetenv("FL_BRIDGE_TOKEN");
    CHECK(load_token_env(1) == -1);
    CHECK(load_token_env(0) == 0);
    setenv("FL_BRIDGE_TOKEN", "nothex", 1);
    CHECK(load_token_env(1) == -1);
    setenv("FL_BRIDGE_TOKEN", "nothex", 1);
    CHECK(load_token_env(0) == 0 && getenv("FL_BRIDGE_TOKEN") == NULL);
    unquiet(2, saved);
}

static int parse(int argc, char **argv, struct opts *o)
{
    int saved1 = quiet(1);
    int saved2 = quiet(2);
    int rc = parse_opts(argc, argv, o);
    unquiet(1, saved1);
    unquiet(2, saved2);
    return rc;
}

static void test_options(void)
{
    struct opts o;
    char *ok[] = { "x", "--daemon", "--port=0", "--log=/tmp/l", "--token-file", "/t", "--host-helper=/bin/sh",
                   "--parent-pid", "1", NULL };
    char *missing[] = { "x", "--daemon", "--port", NULL };
    char *empty_log[] = { "x", "--log=", NULL };
    char *empty_token[] = { "x", "--token-file=", NULL };
    char *missing_token[] = { "x", "--token-file", NULL };
    char *relative_helper[] = { "x", "--host-helper", "rel", NULL };
    char *bad_pid[] = { "x", "--parent-pid=0", NULL };
    char *unknown[] = { "x", "--portx", NULL };
    char *help[] = { "x", "-h", NULL };
    char *help2[] = { "x", "--help", NULL };
    char *version[] = { "x", "--version", NULL };
    char *nothing[] = { "x", NULL };
    CHECK(parse(9, ok, &o) == 0 && o.daemon && o.port == 0 && o.parent_pid == 1 && strcmp(o.token_file, "/t") == 0);
    CHECK(parse(3, missing, &o) == 2);
    CHECK(parse(2, empty_log, &o) == 2);
    CHECK(parse(2, empty_token, &o) == 2);
    CHECK(parse(2, missing_token, &o) == 2);
    CHECK(parse(3, relative_helper, &o) == 2);
    CHECK(parse(2, bad_pid, &o) == 2);
    CHECK(parse(2, unknown, &o) == 2);
    CHECK(parse(2, help, &o) == 1);
    CHECK(parse(2, help2, &o) == 1);
    CHECK(parse(2, version, &o) == 1);
    CHECK(parse(1, nothing, &o) == 2);
}

static pid_t dead_pid(void)
{
    pid_t pid = __real_fork();
    if (pid == 0) {
        _exit(0);
    }
    (void)wait_status(pid);
    return pid;
}

static void test_prepare(void)
{
    struct opts o;
    char cwd[PATH_MAX];
    char hex[65];
    int saved = quiet(2);
    memset(hex, 'b', 64);
    hex[64] = '\0';
    CHECK(getcwd(cwd, sizeof(cwd)) != NULL);
    memset(&o, 0, sizeof(o));

    unsetenv("FL_BRIDGE_TOKEN");
    CHECK(daemon_prepare(&o) == -1);
    setenv("FL_BRIDGE_TOKEN", hex, 1);
    o.host_helper = "/nonexistent/helper";
    CHECK(daemon_prepare(&o) == -1);
    setenv("FL_BRIDGE_TOKEN", hex, 1);
    o.host_helper = "/bin/sh";
    o.log = "/nonexistent/dir/log";
    CHECK(daemon_prepare(&o) == -1);
    setenv("FL_BRIDGE_TOKEN", hex, 1);
    o.log = "/dev/null";
    o.parent_pid = (int32_t)dead_pid();
    CHECK(daemon_prepare(&o) == -1);
    close(g_log_fd);
    setenv("FL_BRIDGE_TOKEN", hex, 1);
    o.log = NULL;
    o.parent_pid = 0;
    fl_sys_fail(FL_SYS_CHDIR, 1, EACCES);
    CHECK(daemon_prepare(&o) == -1);
    setenv("FL_BRIDGE_TOKEN", hex, 1);
    CHECK(daemon_prepare(&o) == 0);
    CHECK(chdir(cwd) == 0);
    g_log_fd = -1;
    g_have_host_helper = 0;
    unquiet(2, saved);
}

static void test_listener(void)
{
    struct sockaddr_in a;
    struct sockaddr_in b;
    int saved = quiet(2);
    int lst;
    fl_sys_fail(FL_SYS_SOCKET, 1, EMFILE);
    CHECK(open_listener(0, &a) == -1);
    lst = open_listener(0, &a);
    CHECK(lst >= 0);
    CHECK(open_listener(ntohs(a.sin_port), &b) == -1); /* in use */
    close(lst);
    unquiet(2, saved);
}

/* The accept loop's pieces, with a real listener. */
static void test_accept_loop(void)
{
    struct daemon_state d;
    struct sockaddr_in a;
    int c;
    int saved = quiet(2);
    memset(&d, 0, sizeof(d));
    g_log_fd = open("/dev/null", O_WRONLY | O_CLOEXEC);
    d.lst = open_listener(0, &a);
    CHECK(d.lst >= 0);
    CHECK(new_sigpipe() == 0);

    d.parent_pid = getpid();
    d.next_parent_check = 0;
    CHECK(parent_check(&d) == 0 && d.next_parent_check > 0);

    fl_sys_fail(FL_SYS_ACCEPT4, 1, EMFILE);
    daemon_accept(&d);
    daemon_accept(&d); /* nothing pending (EAGAIN) */

    c = socket(AF_INET, SOCK_STREAM | SOCK_CLOEXEC, 0);
    CHECK(connect(c, (struct sockaddr *)&a, sizeof(a)) == 0);
    d.nsessions = MAX_SESSIONS;
    poll(NULL, 0, 50);
    daemon_accept(&d); /* refused: too many sessions */
    close(c);

    c = socket(AF_INET, SOCK_STREAM | SOCK_CLOEXEC, 0);
    CHECK(connect(c, (struct sockaddr *)&a, sizeof(a)) == 0);
    d.nsessions = 0;
    poll(NULL, 0, 50);
    fl_sys_fail(FL_SYS_FORK, 1, EAGAIN);
    daemon_accept(&d); /* fork failed */
    CHECK(d.nsessions == 0);
    close(c);

    fl_sys_fail(FL_SYS_POLL, 1, EBADF);
    CHECK(daemon_step(&d) == 0);
    d.parent_pid = (int32_t)dead_pid();
    d.next_parent_check = 0;
    CHECK(daemon_step(&d) == 0);

    close(d.lst);
    close(g_log_fd);
    g_log_fd = -1;
    unquiet(2, saved);
}

/* run_daemon() and the start-up steps that change the process, in children. */
static void test_run_daemon(void)
{
    struct opts o;
    char hex[65];
    pid_t pid;
    memset(hex, 'c', 64);
    hex[64] = '\0';
    memset(&o, 0, sizeof(o));
    o.daemon = 1;

    pid = __real_fork();
    if (pid == 0) {
        (void)quiet(2);
        setenv("FL_BRIDGE_TOKEN", hex, 1);
        fl_sys_fail(FL_SYS_SOCKET, 1, EMFILE);
        _exit(run_daemon(&o));
    }
    CHECK(wait_status(pid) == 1);

    pid = __real_fork();
    if (pid == 0) {
        (void)quiet(2);
        setenv("FL_BRIDGE_TOKEN", hex, 1);
        fl_sys_fail(FL_SYS_PIPE2, 1, EMFILE);
        _exit(run_daemon(&o));
    }
    CHECK(wait_status(pid) == 1);

    pid = __real_fork();
    if (pid == 0) {
        (void)quiet(2);
        fl_sys_fail(FL_SYS_SETSID, 1, EINVAL);
        _exit(daemon_signals(1) == -1 ? 0 : 1); /* "parent exited during start-up" */
    }
    CHECK(wait_status(pid) == 0);

    pid = __real_fork();
    if (pid == 0) {
        (void)quiet(1);
        (void)quiet(2);
        fl_sys_fail(FL_SYS_WRITE, 1, EIO);
        _exit(publish_port(1234) == -1 && publish_port(1234) == 0 ? 0 : 1);
    }
    CHECK(wait_status(pid) == 0);

    /* a daemon that exits on SIGTERM */
    pid = __real_fork();
    if (pid == 0) {
        (void)quiet(1);
        (void)quiet(2);
        setenv("FL_BRIDGE_TOKEN", hex, 1);
        o.parent_pid = getppid();
        _exit(run_daemon(&o));
    }
    poll(NULL, 0, 500);
    kill(pid, SIGTERM);
    CHECK(wait_status(pid) == 0);
}

/* ------------------------------------------------------------------------ */
/* personas                                                                  */
/* ------------------------------------------------------------------------ */

static void test_winexec_arg(void)
{
    char cwd[PATH_MAX];
    char file[400];
    char longabs[PATH_MAX + 20];
    char *r;
    CHECK(getcwd(cwd, sizeof(cwd)) != NULL);
    memset(longabs, 'l', sizeof(longabs) - 1);
    longabs[0] = '/';
    longabs[sizeof(longabs) - 1] = '\0';
    longabs[5] = '/';

    r = winexec_arg("/w", NULL, NULL);
    CHECK(r != NULL && strcmp(r, "/w") == 0);
    free(r);
    r = winexec_arg("/tmp", NULL, NULL);
    CHECK(r != NULL && strcmp(r, "Z:\\tmp") == 0);
    free(r);
    r = winexec_arg("/nonexistent-dir/x", "/nonexistent-dir=D:\\n", NULL);
    CHECK(r != NULL && strcmp(r, "D:\\n\\x") == 0);
    free(r);
    r = winexec_arg(longabs, NULL, NULL);
    CHECK(r != NULL && strcmp(r, longabs) == 0);
    free(r);
    r = winexec_arg("-x", NULL, NULL);
    CHECK(r != NULL && strcmp(r, "-x") == 0);
    free(r);
    r = winexec_arg("", NULL, NULL);
    CHECK(r != NULL && r[0] == '\0');
    free(r);

    snprintf(file, sizeof(file), "%s/COMMIT_EDITMSG", g_tmp);
    write_file(file, "msg\n", 0600);
    CHECK(chdir(g_tmp) == 0);
    r = winexec_arg("COMMIT_EDITMSG", NULL, NULL);
    CHECK(r != NULL && strncmp(r, "Z:\\", 3) == 0);
    free(r);
    fl_sys_fail(FL_SYS_GETCWD, 1, ERANGE);
    r = winexec_arg("COMMIT_EDITMSG", NULL, NULL);
    CHECK(r != NULL && strcmp(r, "COMMIT_EDITMSG") == 0);
    free(r);
    CHECK(chdir(cwd) == 0);
}

static void test_personas(void)
{
    char *winexec_usage[] = { "fl-winexec", NULL };
    char *winexec_args[] = { "fl-winexec", "C:\\x.exe", "/tmp", "-y", NULL };
    char *askpass[] = { "fl-askpass", "Password:", NULL };
    char *ssh_askpass[] = { "fl-ssh-askpass", "Password:", NULL };
    char *version[] = { "fl-bridge-helper", "--version", NULL };
    char *bad[] = { "fl-bridge-helper", "--bogus", NULL };
    char *nothing[] = { "fl-bridge-helper", NULL };
    char empty[300];
    char longdll[5000];
    const char *old_path = getenv("PATH");
    char *saved_path = old_path != NULL ? strdup(old_path) : NULL;
    int saved1 = quiet(1);
    int saved2 = quiet(2);

    snprintf(empty, sizeof(empty), "%s/empty", g_tmp);
    mkdir(empty, 0700);
    CHECK(strcmp(persona_name(FL_PERSONA_SSH_ASKPASS), "fl-ssh-askpass") == 0);

    unsetenv("WINEPREFIX");
    unsetenv("FL_ASKPASS_TARGET");
    unsetenv("FL_SSH_ASKPASS_TARGET");
    unsetenv("FL_WINE");
    CHECK(fl_bridge_helper_main(1, winexec_usage) == 2);
    CHECK(fl_bridge_helper_main(2, askpass) == 127);
    CHECK(fl_bridge_helper_main(2, ssh_askpass) == 127);
    setenv("FL_ASKPASS_TARGET", "C:\\askpass.exe", 1);
    CHECK(fl_bridge_helper_main(2, askpass) == 127); /* no WINEPREFIX */
    setenv("WINEPREFIX", g_tmp, 1);

    memset(longdll, 'd', sizeof(longdll) - 1);
    longdll[sizeof(longdll) - 1] = '\0';
    setenv("WINEDLLOVERRIDES", longdll, 1);
    CHECK(fl_bridge_helper_main(2, askpass) == 127); /* overrides too long */
    unsetenv("WINEDLLOVERRIDES");

    fl_alloc_fail_at(1);
    CHECK(fl_bridge_helper_main(2, askpass) == 127); /* out of memory */
    fl_alloc_fail_at(0);

    setenv("FL_WINE", "/nonexistent/wine", 1);
    CHECK(fl_bridge_helper_main(2, askpass) == 127);
    CHECK(fl_bridge_helper_main(4, winexec_args) == 127);
    unsetenv("FL_WINE");
    setenv("PATH", empty, 1);
    CHECK(fl_bridge_helper_main(4, winexec_args) == 127); /* no wine on PATH */
    if (saved_path != NULL) {
        setenv("PATH", saved_path, 1);
    }
    free(saved_path);

    CHECK(fl_bridge_helper_main(2, version) == 0);
    CHECK(fl_bridge_helper_main(2, bad) == 2);
    CHECK(fl_bridge_helper_main(1, nothing) == 2);
    free_argv(NULL, 0);
    unquiet(1, saved1);
    unquiet(2, saved2);
    g_prog = "fl-bridge-helper";
}

static void cleanup_tmp(void)
{
    static const char *const files[] = { "bin/prog", "bin/bad", "token", "COMMIT_EDITMSG", NULL };
    static const char *const dirs[] = { "bin", "empty", NULL };
    char path[400];
    for (size_t i = 0; files[i] != NULL; i++) {
        snprintf(path, sizeof(path), "%s/%s", g_tmp, files[i]);
        (void)unlink(path);
    }
    for (size_t i = 0; dirs[i] != NULL; i++) {
        snprintf(path, sizeof(path), "%s/%s", g_tmp, dirs[i]);
        (void)rmdir(path);
    }
    (void)rmdir(g_tmp);
}

int main(void)
{
    const char *base = getenv("TMPDIR");
    signal(SIGPIPE, SIG_IGN);
    snprintf(g_tmp, sizeof(g_tmp), "%s/fl-helper-daemon-XXXXXX", base != NULL && base[0] == '/' ? base : "/tmp");
    if (mkdtemp(g_tmp) == NULL) {
        perror("mkdtemp");
        return 1;
    }
    test_utilities(); /* creates the self-pipe */
    set_handler(SIGCHLD, on_signal);
    test_std_fds();
    test_framing();
    test_exec_search();
    test_records();
    test_spawn_failures();
    test_children();
    test_frames();
    test_relay();
    test_handshake();
    test_read_req();
    test_replies();
    test_serve();
    test_session_main();
    test_tokens();
    test_options();
    test_prepare();
    test_listener();
    test_accept_loop();
    test_run_daemon();
    test_winexec_arg();
    test_personas();
    cleanup_tmp();
    if (g_failures) {
        fprintf(stderr, "test_helper_daemon: %d of %d checks failed\n", g_failures, g_checks);
        return 1;
    }
    printf("test_helper_daemon: %d checks passed\n", g_checks);
    return 0;
}
