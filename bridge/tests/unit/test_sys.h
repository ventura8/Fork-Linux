/*
 * test_sys.h - system-call failure injection for test_helper_daemon.c. The test binary
 * is linked with -Wl,--wrap=<fn> for every function below (bridge/tests/meson.build),
 * so the daemon code compiled into it calls __wrap_<fn>, which fails on request and
 * otherwise forwards to the real libc function.
 */
#ifndef FL_TEST_SYS_H
#define FL_TEST_SYS_H

#define _GNU_SOURCE
#include <dirent.h>
#include <poll.h>
#include <sys/socket.h>
#include <sys/types.h>

enum fl_sys {
    FL_SYS_PIPE2,
    FL_SYS_FORK,
    FL_SYS_POLL,
    FL_SYS_RECV,
    FL_SYS_SEND,
    FL_SYS_READ,
    FL_SYS_WRITE,
    FL_SYS_GETRANDOM,
    FL_SYS_WAITPID,
    FL_SYS_DUP2,
    FL_SYS_SETSID,
    FL_SYS_SOCKET,
    FL_SYS_ACCEPT4,
    FL_SYS_OPENDIR,
    FL_SYS_CHDIR,
    FL_SYS_GETCWD,
    FL_SYS_COUNT
};

/* The nth call of fn from now (1 = the next one) fails with errno err; nth <= 0 disarms. */
void fl_sys_fail(enum fl_sys fn, int nth, int err);
/* Disarm everything. */
void fl_sys_reset(void);

int __wrap_pipe2(int fds[2], int flags);
pid_t __wrap_fork(void);
int __wrap_poll(struct pollfd *fds, nfds_t n, int timeout);
ssize_t __wrap_recv(int fd, void *buf, size_t n, int flags);
ssize_t __wrap_send(int fd, const void *buf, size_t n, int flags);
ssize_t __wrap_read(int fd, void *buf, size_t n);
ssize_t __wrap_write(int fd, const void *buf, size_t n);
ssize_t __wrap_getrandom(void *buf, size_t n, unsigned flags);
pid_t __wrap_waitpid(pid_t pid, int *status, int options);
int __wrap_dup2(int a, int b);
pid_t __wrap_setsid(void);
int __wrap_socket(int domain, int type, int protocol);
int __wrap_accept4(int fd, struct sockaddr *addr, socklen_t *len, int flags);
DIR *__wrap_opendir(const char *path);
int __wrap_chdir(const char *path);
char *__wrap_getcwd(char *buf, size_t n);

int __real_pipe2(int fds[2], int flags);
pid_t __real_fork(void);
int __real_poll(struct pollfd *fds, nfds_t n, int timeout);
ssize_t __real_recv(int fd, void *buf, size_t n, int flags);
ssize_t __real_send(int fd, const void *buf, size_t n, int flags);
ssize_t __real_read(int fd, void *buf, size_t n);
ssize_t __real_write(int fd, const void *buf, size_t n);
ssize_t __real_getrandom(void *buf, size_t n, unsigned flags);
pid_t __real_waitpid(pid_t pid, int *status, int options);
int __real_dup2(int a, int b);
pid_t __real_setsid(void);
int __real_socket(int domain, int type, int protocol);
int __real_accept4(int fd, struct sockaddr *addr, socklen_t *len, int flags);
DIR *__real_opendir(const char *path);
int __real_chdir(const char *path);
char *__real_getcwd(char *buf, size_t n);

#endif /* FL_TEST_SYS_H */
