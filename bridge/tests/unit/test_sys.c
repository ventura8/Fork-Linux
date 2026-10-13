/*
 * test_sys.c - system-call failure injection (see test_sys.h).
 */
#include "test_sys.h"

#include <errno.h>
#include <stddef.h>

static int g_left[FL_SYS_COUNT]; /* calls left until the failing one; 0: disarmed */
static int g_err[FL_SYS_COUNT];

void fl_sys_fail(enum fl_sys fn, int nth, int err)
{
    if ((int)fn < 0 || fn >= FL_SYS_COUNT) {
        return; /* FL_SYS_COUNT: inject nothing */
    }
    g_left[fn] = nth > 0 ? nth : 0;
    g_err[fn] = err;
}

void fl_sys_reset(void)
{
    for (int i = 0; i < FL_SYS_COUNT; i++) {
        g_left[i] = 0;
    }
}

/* 1 (with errno set) when this call of fn must fail. */
static int fail(enum fl_sys fn)
{
    if (g_left[fn] <= 0) {
        return 0;
    }
    g_left[fn]--;
    if (g_left[fn] > 0) {
        return 0;
    }
    errno = g_err[fn];
    return 1;
}

int __wrap_pipe2(int fds[2], int flags)
{
    return fail(FL_SYS_PIPE2) ? -1 : __real_pipe2(fds, flags);
}

pid_t __wrap_fork(void)
{
    return fail(FL_SYS_FORK) ? -1 : __real_fork();
}

int __wrap_poll(struct pollfd *fds, nfds_t n, int timeout)
{
    return fail(FL_SYS_POLL) ? -1 : __real_poll(fds, n, timeout);
}

ssize_t __wrap_recv(int fd, void *buf, size_t n, int flags)
{
    return fail(FL_SYS_RECV) ? -1 : __real_recv(fd, buf, n, flags);
}

ssize_t __wrap_send(int fd, const void *buf, size_t n, int flags)
{
    return fail(FL_SYS_SEND) ? -1 : __real_send(fd, buf, n, flags);
}

ssize_t __wrap_read(int fd, void *buf, size_t n)
{
    return fail(FL_SYS_READ) ? -1 : __real_read(fd, buf, n);
}

ssize_t __wrap_write(int fd, const void *buf, size_t n)
{
    return fail(FL_SYS_WRITE) ? -1 : __real_write(fd, buf, n);
}

ssize_t __wrap_getrandom(void *buf, size_t n, unsigned flags)
{
    return fail(FL_SYS_GETRANDOM) ? -1 : __real_getrandom(buf, n, flags);
}

pid_t __wrap_waitpid(pid_t pid, int *status, int options)
{
    return fail(FL_SYS_WAITPID) ? -1 : __real_waitpid(pid, status, options);
}

int __wrap_dup2(int a, int b)
{
    return fail(FL_SYS_DUP2) ? -1 : __real_dup2(a, b);
}

pid_t __wrap_setsid(void)
{
    return fail(FL_SYS_SETSID) ? -1 : __real_setsid();
}

int __wrap_socket(int domain, int type, int protocol)
{
    return fail(FL_SYS_SOCKET) ? -1 : __real_socket(domain, type, protocol);
}

int __wrap_accept4(int fd, struct sockaddr *addr, socklen_t *len, int flags)
{
    return fail(FL_SYS_ACCEPT4) ? -1 : __real_accept4(fd, addr, len, flags);
}

DIR *__wrap_opendir(const char *path)
{
    return fail(FL_SYS_OPENDIR) ? NULL : __real_opendir(path);
}

int __wrap_chdir(const char *path)
{
    return fail(FL_SYS_CHDIR) ? -1 : __real_chdir(path);
}

char *__wrap_getcwd(char *buf, size_t n)
{
    return fail(FL_SYS_GETCWD) ? NULL : __real_getcwd(buf, n);
}
