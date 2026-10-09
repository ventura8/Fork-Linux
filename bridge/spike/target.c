/*
 * target.c - B1 spike target (native Linux ELF).
 *
 * Built three ways by build.sh: dynamic PIE (glibc), static -no-pie (musl, ET_EXEC)
 * and static-pie (musl, ET_DYN without PT_INTERP). probe.exe starts it through
 * Wine's CreateProcessW and this program records what it was given:
 *
 *   usage: target <logfile> <case-id> [expected extra args...]
 *
 * The log is key=value lines terminated by "END". It then writes OUT-<case> to
 * stdout and ERR-<case> to stderr (so the probe can see which std handles reach
 * us) and exits with code 7 (so the probe can test GetExitCodeProcess).
 */
#define _GNU_SOURCE
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <poll.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#ifndef TARGET_VARIANT
#define TARGET_VARIANT "unknown"
#endif

extern char **environ;

/* Extra arguments probe.exe passes after <logfile> <case-id>; must match probe.c. */
static const char *const expected_args[] = {
    "arg with space", "q\"uote", "back\\slash", "C:\\win\\path", "tail\\", "", "$HOME;`id`",
};

/* Print one env var (or <unset>). */
static void log_env(FILE *f, const char *name)
{
    const char *v = getenv(name);
    fprintf(f, "env.%s=%s\n", name, v ? v : "<unset>");
}

/* Describe fd n via /proc/self/fd/n (the "ls -l /proc/self/fd" view). */
static void fd_target(int n, char *buf, size_t len)
{
    char path[64];
    ssize_t r;
    snprintf(path, sizeof(path), "/proc/self/fd/%d", n);
    r = readlink(path, buf, len - 1);
    if (r < 0) {
        snprintf(buf, len, "<closed:%s>", strerror(errno));
        return;
    }
    buf[r] = '\0';
}

/* List every open fd (except the directory stream used for listing). */
static void log_fds(FILE *f)
{
    DIR *d = opendir("/proc/self/fd");
    struct dirent *e;
    char buf[PATH_MAX];
    int self;
    if (!d) {
        fprintf(f, "fds=<opendir failed: %s>\n", strerror(errno));
        return;
    }
    self = dirfd(d);
    while ((e = readdir(d)) != NULL) {
        int n;
        if (e->d_name[0] == '.') {
            continue;
        }
        n = atoi(e->d_name);
        if (n == self) {
            continue;
        }
        fd_target(n, buf, sizeof(buf));
        fprintf(f, "fd.%d=%s\n", n, buf);
    }
    closedir(d);
}

/* Log the seccomp / no_new_privs state we inherited (Wine-staging installs a filter). */
static void log_status(FILE *f)
{
    FILE *s = fopen("/proc/self/status", "r");
    char line[256];
    if (!s) {
        return;
    }
    while (fgets(line, sizeof(line), s)) {
        char *colon = strchr(line, ':');
        char *v;
        if (!colon || (strncmp(line, "Seccomp", 7) != 0 && strncmp(line, "NoNewPrivs", 10) != 0)) {
            continue;
        }
        *colon = '\0';
        v = colon + 1;
        v += strspn(v, " \t");
        v[strcspn(v, "\n")] = '\0';
        fprintf(f, "status.%s=%s\n", line, v);
    }
    fclose(s);
}

/* Read whatever arrives on stdin within 300 ms (printable form). */
static void log_stdin(FILE *f)
{
    struct pollfd p = { .fd = 0, .events = POLLIN, .revents = 0 };
    char buf[64];
    int pr = poll(&p, 1, 300);
    ssize_t n;
    if (pr < 0) {
        fprintf(f, "stdin.read=<poll error: %s>\n", strerror(errno));
        return;
    }
    if (pr == 0) {
        fprintf(f, "stdin.read=<timeout>\n");
        return;
    }
    if (p.revents & POLLNVAL) {
        fprintf(f, "stdin.read=<closed fd>\n");
        return;
    }
    n = read(0, buf, sizeof(buf) - 1);
    if (n < 0) {
        fprintf(f, "stdin.read=<read error: %s>\n", strerror(errno));
        return;
    }
    if (n == 0) {
        fprintf(f, "stdin.read=<eof>\n");
        return;
    }
    buf[n] = '\0';
    for (ssize_t i = 0; i < n; i++) {
        if (buf[i] == '\n' || buf[i] == '\r') {
            buf[i] = ' ';
        }
    }
    fprintf(f, "stdin.read=%s\n", buf);
}

/* Write a marker line to fd and record the outcome. */
static void write_marker(FILE *f, int fd, const char *tag, const char *case_id)
{
    char line[256];
    int len = snprintf(line, sizeof(line), "%s-%s\n", tag, case_id);
    ssize_t w = write(fd, line, (size_t)len);
    fprintf(f, "%s.write=%s\n", tag, w == (ssize_t)len ? "ok" : strerror(errno));
}

int main(int argc, char **argv)
{
    FILE *f;
    char cwd[PATH_MAX];
    size_t nexp = sizeof(expected_args) / sizeof(expected_args[0]);
    int argv_ok;
    const char *case_id;
    int tty;

    if (argc < 3) {
        fprintf(stderr, "usage: %s <logfile> <case-id> [args...]\n", argv[0]);
        return 2;
    }
    case_id = argv[2];
    signal(SIGPIPE, SIG_IGN);
    f = fopen(argv[1], "a");
    if (!f) {
        return 3;
    }
    fprintf(f, "variant=%s\ncase=%s\nargc=%d\n", TARGET_VARIANT, case_id, argc);
    for (int i = 0; i < argc; i++) {
        fprintf(f, "argv[%d]=%s\n", i, argv[i]);
    }
    argv_ok = (size_t)argc == 3 + nexp;
    for (size_t i = 0; argv_ok && i < nexp; i++) {
        argv_ok = strcmp(argv[3 + i], expected_args[i]) == 0;
    }
    fprintf(f, "argv_ok=%d\n", argv_ok);
    fprintf(f, "pid=%ld\nppid=%ld\npgid=%ld\nsid=%ld\n", (long)getpid(), (long)getppid(),
            (long)getpgrp(), (long)getsid(0));
    fprintf(f, "session_leader=%d\n", getsid(0) == getpid());
    tty = open("/dev/tty", O_RDONLY | O_NOCTTY);
    fprintf(f, "ctty=%s\n", tty >= 0 ? "yes" : "no");
    if (tty >= 0) {
        close(tty);
    }
    fprintf(f, "cwd=%s\n", getcwd(cwd, sizeof(cwd)) ? cwd : "<getcwd failed>");
    log_env(f, "PATH");
    log_env(f, "HOME");
    log_env(f, "FL_TOKEN");
    log_env(f, "WINEPATH");
    log_env(f, "WINEHOME");
    log_env(f, "TEMP");
    log_env(f, "WINEPREFIX");
    log_env(f, "WINELOADERNOEXEC");
    fprintf(f, "env.names=");
    for (char **e = environ; *e; e++) {
        const char *eq = strchr(*e, '=');
        int n = eq ? (int)(eq - *e) : (int)strlen(*e);
        fprintf(f, "%s%.*s", e == environ ? "" : ",", n, *e);
    }
    fprintf(f, "\n");
    log_status(f);
    log_fds(f);
    log_stdin(f);
    write_marker(f, 1, "OUT", case_id);
    write_marker(f, 2, "ERR", case_id);
    fprintf(f, "END\n");
    fclose(f);
    return 7;
}
