/*
 * probe.c - B1 spike: how does Wine's CreateProcessW start native (non-PE) targets?
 *
 *   usage: probe.exe <ctx> <result.tsv> <logdir> <cwd> <target>...
 *
 * All paths are unix paths (mapped with wine_get_dos_file_name). For every
 * target x creation flag x std-handle mode the probe spawns the target (see
 * target.c / target.sh), waits for the target's own log, collects what came
 * back through the std handles and appends one TSV row to <result.tsv>.
 * Context facts about the probe itself go to <result.tsv>.ctx.
 *
 * Built twice: probe-con.exe (console subsystem) and probe-gui.exe (GUI).
 */
#define WIN32_LEAN_AND_MEAN
#include <winsock2.h>
#include <ws2tcpip.h>
#include <windows.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <wchar.h>

int _dowildcard = 0;

typedef WCHAR *(*dos_name_fn)(const char *);

enum { MODE_NONE, MODE_INVALID, MODE_PIPE, MODE_FILE, MODE_SOCKET, MODE_COUNT };

static const char *const mode_names[MODE_COUNT] = { "none", "invalid", "pipe", "file", "socket" };

struct flagdef {
    const char *name;
    DWORD flags;
};

static const struct flagdef flagdefs[] = {
    { "0", 0 },
    { "NO_WINDOW", CREATE_NO_WINDOW },
    { "DETACHED", DETACHED_PROCESS },
    { "NEW_PGROUP", CREATE_NEW_PROCESS_GROUP },
};

/* Must match expected_args in target.c and the checks in target.sh. */
static const WCHAR *const extra_args[] = {
    L"arg with space", L"q\"uote", L"back\\slash", L"C:\\win\\path", L"tail\\", L"", L"$HOME;`id`",
};

/* Std handles handed to one child plus the parent's ends. */
struct chan {
    int mode;
    HANDLE child[3];
    HANDLE parent[3];   /* MODE_PIPE: parent pipe ends; MODE_FILE: unused */
    SOCKET psock[3];    /* MODE_SOCKET: parent socket ends */
    WCHAR file[3][MAX_PATH];
};

static FILE *result_fp;

/* Convert a UTF-16 string to a malloc'd UTF-8 string. */
static char *to_utf8(const WCHAR *w)
{
    int n = WideCharToMultiByte(CP_UTF8, 0, w, -1, NULL, 0, NULL, NULL);
    char *s = malloc((size_t)n);
    if (s) {
        WideCharToMultiByte(CP_UTF8, 0, w, -1, s, n, NULL, NULL);
    }
    return s;
}

/* Map a unix path to a DOS path (wine_get_dos_file_name, fallback Z:\...). */
static WCHAR *to_dos(const char *unix_path)
{
    static dos_name_fn fn;
    static int init;
    size_t n = strlen(unix_path);
    WCHAR *w;
    if (!init) {
        HMODULE k32 = GetModuleHandleW(L"kernel32.dll");
        init = 1;
        if (k32) {
            fn = (dos_name_fn)(void (*)(void))GetProcAddress(k32, "wine_get_dos_file_name");
        }
    }
    if (fn) {
        WCHAR *r = fn(unix_path);
        if (r) {
            return r;
        }
    }
    w = calloc(n + 3, sizeof(WCHAR));
    if (!w) {
        return NULL;
    }
    w[0] = L'Z';
    w[1] = L':';
    for (size_t i = 0; i < n; i++) {
        w[i + 2] = unix_path[i] == '/' ? L'\\' : (WCHAR)(unsigned char)unix_path[i];
    }
    return w;
}

/* Append arg to cmd (capacity cap) using CommandLineToArgvW-compatible quoting. */
static void append_quoted(WCHAR *cmd, size_t cap, const WCHAR *arg)
{
    size_t len = wcslen(cmd);
    int needs = arg[0] == L'\0' || wcspbrk(arg, L" \t\n\v\"") != NULL;
    if (len && len + 1 < cap) {
        cmd[len++] = L' ';
    }
    if (!needs) {
        for (const WCHAR *p = arg; *p && len + 1 < cap; p++) {
            cmd[len++] = *p;
        }
        cmd[len] = L'\0';
        return;
    }
    if (len + 1 < cap) {
        cmd[len++] = L'"';
    }
    for (const WCHAR *p = arg;; p++) {
        size_t bs = 0;
        while (*p == L'\\') {
            p++;
            bs++;
        }
        if (*p == L'\0') {
            for (size_t i = 0; i < bs * 2 && len + 1 < cap; i++) {
                cmd[len++] = L'\\';
            }
            break;
        }
        if (*p == L'"') {
            for (size_t i = 0; i < bs * 2 + 1 && len + 1 < cap; i++) {
                cmd[len++] = L'\\';
            }
        } else {
            for (size_t i = 0; i < bs && len + 1 < cap; i++) {
                cmd[len++] = L'\\';
            }
        }
        if (len + 1 < cap) {
            cmd[len++] = *p;
        }
    }
    if (len + 1 < cap) {
        cmd[len++] = L'"';
    }
    cmd[len] = L'\0';
}

/* Build an environment block: current env minus FL_TOKEN/HOME, plus the given values. */
static WCHAR *build_env(const WCHAR *token)
{
    WCHAR *cur = GetEnvironmentStringsW();
    size_t total = 0;
    WCHAR *blk;
    WCHAR *out;
    for (const WCHAR *p = cur; *p; p += wcslen(p) + 1) {
        total += wcslen(p) + 1;
    }
    total += wcslen(token) + 64;
    blk = calloc(total + 2, sizeof(WCHAR));
    if (!blk) {
        FreeEnvironmentStringsW(cur);
        return NULL;
    }
    out = blk;
    for (const WCHAR *p = cur; *p; p += wcslen(p) + 1) {
        if (_wcsnicmp(p, L"FL_TOKEN=", 9) == 0 || _wcsnicmp(p, L"HOME=", 5) == 0) {
            continue;
        }
        wcscpy(out, p);
        out += wcslen(p) + 1;
    }
    swprintf(out, total - (size_t)(out - blk), L"FL_TOKEN=%ls", token);
    out += wcslen(out) + 1;
    wcscpy(out, L"HOME=C:\\fakehome");
    out += wcslen(out) + 1;
    *out = L'\0';
    FreeEnvironmentStringsW(cur);
    return blk;
}

/* Create a connected loopback TCP pair; the child end is non-overlapped. */
static int tcp_pair(SOCKET *parent, SOCKET *child)
{
    struct sockaddr_in a;
    int alen = sizeof(a);
    SOCKET l = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    SOCKET c;
    SOCKET s;
    if (l == INVALID_SOCKET) {
        return -1;
    }
    memset(&a, 0, sizeof(a));
    a.sin_family = AF_INET;
    a.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    if (bind(l, (struct sockaddr *)&a, sizeof(a)) || listen(l, 1) ||
        getsockname(l, (struct sockaddr *)&a, &alen)) {
        closesocket(l);
        return -1;
    }
    c = WSASocketW(AF_INET, SOCK_STREAM, IPPROTO_TCP, NULL, 0, 0);
    if (c == INVALID_SOCKET || connect(c, (struct sockaddr *)&a, sizeof(a))) {
        closesocket(l);
        return -1;
    }
    s = accept(l, NULL, NULL);
    closesocket(l);
    if (s == INVALID_SOCKET) {
        closesocket(c);
        return -1;
    }
    SetHandleInformation((HANDLE)c, HANDLE_FLAG_INHERIT, HANDLE_FLAG_INHERIT);
    SetHandleInformation((HANDLE)s, HANDLE_FLAG_INHERIT, 0);
    *parent = s;
    *child = c;
    return 0;
}

/* Prepare the std handles for one case; stdin always carries "IN-<case>\n". */
static int chan_open(struct chan *ch, int mode, const char *logdir, const char *case_id)
{
    SECURITY_ATTRIBUTES sa = { sizeof(sa), NULL, TRUE };
    char line[256];
    DWORD done;
    int linelen = snprintf(line, sizeof(line), "IN-%s\n", case_id);
    memset(ch, 0, sizeof(*ch));
    ch->mode = mode;
    for (int i = 0; i < 3; i++) {
        ch->child[i] = INVALID_HANDLE_VALUE;
        ch->parent[i] = INVALID_HANDLE_VALUE;
        ch->psock[i] = INVALID_SOCKET;
    }
    if (mode == MODE_PIPE) {
        HANDLE r;
        HANDLE w;
        for (int i = 0; i < 3; i++) {
            if (!CreatePipe(&r, &w, &sa, 0)) {
                return -1;
            }
            ch->child[i] = i == 0 ? r : w;
            ch->parent[i] = i == 0 ? w : r;
            SetHandleInformation(ch->parent[i], HANDLE_FLAG_INHERIT, 0);
        }
        WriteFile(ch->parent[0], line, (DWORD)linelen, &done, NULL);
    } else if (mode == MODE_FILE) {
        static const char *const ext[3] = { "stdin", "stdout", "stderr" };
        for (int i = 0; i < 3; i++) {
            char path[1024];
            WCHAR *dos;
            snprintf(path, sizeof(path), "%s/%s.%s", logdir, case_id, ext[i]);
            dos = to_dos(path);
            wcsncpy(ch->file[i], dos, MAX_PATH - 1);
            if (i == 0) {
                HANDLE h = CreateFileW(dos, GENERIC_WRITE, 0, NULL, CREATE_ALWAYS, 0, NULL);
                WriteFile(h, line, (DWORD)linelen, &done, NULL);
                CloseHandle(h);
                ch->child[i] = CreateFileW(dos, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE, &sa,
                                           OPEN_EXISTING, 0, NULL);
            } else {
                ch->child[i] = CreateFileW(dos, GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE, &sa,
                                           CREATE_ALWAYS, 0, NULL);
            }
            if (ch->child[i] == INVALID_HANDLE_VALUE) {
                return -1;
            }
        }
    } else if (mode == MODE_SOCKET) {
        for (int i = 0; i < 3; i++) {
            SOCKET c;
            if (tcp_pair(&ch->psock[i], &c)) {
                return -1;
            }
            ch->child[i] = (HANDLE)c;
        }
        send(ch->psock[0], line, linelen, 0);
        shutdown(ch->psock[0], SD_SEND);
    }
    return 0;
}

/* Close the child ends in the parent after CreateProcessW. */
static void chan_close_child(struct chan *ch)
{
    for (int i = 0; i < 3; i++) {
        if (ch->child[i] == INVALID_HANDLE_VALUE) {
            continue;
        }
        if (ch->mode == MODE_SOCKET) {
            closesocket((SOCKET)ch->child[i]);
        } else {
            CloseHandle(ch->child[i]);
        }
        ch->child[i] = INVALID_HANDLE_VALUE;
    }
    if (ch->mode == MODE_PIPE && ch->parent[0] != INVALID_HANDLE_VALUE) {
        CloseHandle(ch->parent[0]);
        ch->parent[0] = INVALID_HANDLE_VALUE;
    }
}

/* Collect what arrived on the parent's end of std stream i (1 or 2), up to cap-1 bytes. */
static void chan_collect(struct chan *ch, int i, char *buf, size_t cap)
{
    size_t got = 0;
    buf[0] = '\0';
    if (ch->mode == MODE_PIPE) {
        for (int tries = 0; tries < 20 && got + 1 < cap; tries++) {
            DWORD avail = 0;
            DWORD n = 0;
            if (!PeekNamedPipe(ch->parent[i], NULL, 0, NULL, &avail, NULL)) {
                snprintf(buf + got, cap - got, "%s<peek err %lu>", got ? " " : "", GetLastError());
                return;
            }
            if (!avail) {
                Sleep(10);
                continue;
            }
            if (avail > cap - 1 - got) {
                avail = (DWORD)(cap - 1 - got);
            }
            if (ReadFile(ch->parent[i], buf + got, avail, &n, NULL)) {
                got += n;
                buf[got] = '\0';
            }
        }
    } else if (ch->mode == MODE_FILE) {
        HANDLE h = CreateFileW(ch->file[i], GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE, NULL,
                               OPEN_EXISTING, 0, NULL);
        DWORD n = 0;
        if (h != INVALID_HANDLE_VALUE) {
            if (ReadFile(h, buf, (DWORD)(cap - 1), &n, NULL)) {
                buf[n] = '\0';
            }
            CloseHandle(h);
        }
    } else if (ch->mode == MODE_SOCKET) {
        while (got + 1 < cap) {
            fd_set rs;
            struct timeval tv = { 0, 200000 };
            int n;
            FD_ZERO(&rs);
            FD_SET(ch->psock[i], &rs);
            if (select(0, &rs, NULL, NULL, &tv) <= 0) {
                break;
            }
            n = recv(ch->psock[i], buf + got, (int)(cap - 1 - got), 0);
            if (n <= 0) {
                break;
            }
            got += (size_t)n;
            buf[got] = '\0';
        }
    }
    for (char *p = buf; *p; p++) {
        if (*p == '\n' || *p == '\r' || *p == '\t') {
            *p = ' ';
        }
    }
}

/* Release the parent's ends. */
static void chan_free(struct chan *ch)
{
    chan_close_child(ch);
    for (int i = 0; i < 3; i++) {
        if (ch->parent[i] != INVALID_HANDLE_VALUE) {
            CloseHandle(ch->parent[i]);
        }
        if (ch->psock[i] != INVALID_SOCKET) {
            closesocket(ch->psock[i]);
        }
    }
}

/* Read a whole small file (DOS path) into buf; returns bytes read. */
static size_t slurp(const WCHAR *path, char *buf, size_t cap)
{
    HANDLE h = CreateFileW(path, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                           NULL, OPEN_EXISTING, 0, NULL);
    DWORD n = 0;
    buf[0] = '\0';
    if (h == INVALID_HANDLE_VALUE) {
        return 0;
    }
    if (!ReadFile(h, buf, (DWORD)(cap - 1), &n, NULL)) {
        n = 0;
    }
    buf[n] = '\0';
    CloseHandle(h);
    return n;
}

/* Copy the value of "key=" from a key=value log into out ("-" when absent). */
static void log_field(const char *log, const char *key, char *out, size_t cap)
{
    size_t kl = strlen(key);
    const char *p = log;
    snprintf(out, cap, "-");
    while (p && *p) {
        if (strncmp(p, key, kl) == 0 && p[kl] == '=') {
            const char *v = p + kl + 1;
            size_t n = strcspn(v, "\n");
            if (n >= cap) {
                n = cap - 1;
            }
            memcpy(out, v, n);
            out[n] = '\0';
            for (char *q = out; *q; q++) {
                if (*q == '\t') {
                    *q = ' ';
                }
            }
            return;
        }
        p = strchr(p, '\n');
        if (p) {
            p++;
        }
    }
}

/* Run one case and append one TSV row. */
static void run_case(const char *ctx, const char *tname, const WCHAR *tdos, const struct flagdef *fd,
                     int mode, int null_cwd, const char *logdir, const WCHAR *cwd_dos)
{
    char case_id[256];
    char logpath[1024];
    WCHAR *log_dos;
    WCHAR cmd[8192] = L"";
    WCHAR tmp[1024];
    WCHAR token[300];
    WCHAR *env;
    STARTUPINFOW si;
    PROCESS_INFORMATION pi;
    struct chan ch;
    BOOL ok;
    DWORD gle = 0;
    DWORD wait;
    DWORD wait_gle = 0;
    DWORD code = 0;
    LARGE_INTEGER f, t0, t1;
    double spawn_ms;
    ULONGLONG start;
    int ran = 0;
    ULONGLONG ran_ms = 0;
    static char logbuf[65536];
    char out_got[512], err_got[512];
    char v[16][512];
    static const char *const keys[16] = { "argv_ok", "argv[0]", "session_leader", "ctty", "ppid", "cwd",
                                          "env.PATH", "env.HOME", "env.WINEHOME", "env.FL_TOKEN", "fd.0",
                                          "fd.1", "fd.2", "stdin.read", "OUT.write", "ERR.write" };

    snprintf(case_id, sizeof(case_id), "%s.%s.%s.%s%s", ctx, tname, fd->name, mode_names[mode],
             null_cwd ? ".nullcwd" : "");
    snprintf(logpath, sizeof(logpath), "%s/%s.log", logdir, case_id);
    log_dos = to_dos(logpath);
    DeleteFileW(log_dos);

    append_quoted(cmd, 8192, tdos);
    MultiByteToWideChar(CP_UTF8, 0, logpath, -1, tmp, 1024);
    append_quoted(cmd, 8192, tmp);
    MultiByteToWideChar(CP_UTF8, 0, case_id, -1, tmp, 1024);
    append_quoted(cmd, 8192, tmp);
    for (size_t i = 0; i < sizeof(extra_args) / sizeof(extra_args[0]); i++) {
        append_quoted(cmd, 8192, extra_args[i]);
    }
    swprintf(token, 300, L"tok-%hs", case_id);
    env = build_env(token);

    if (chan_open(&ch, mode, logdir, case_id)) {
        fprintf(result_fp, "%s\t%s\t%s\t%s\t%s\tchan_open failed\n", ctx, tname, fd->name, mode_names[mode],
                null_cwd ? "null" : "explicit");
        chan_free(&ch);
        free(env);
        return;
    }
    memset(&si, 0, sizeof(si));
    si.cb = sizeof(si);
    if (mode != MODE_NONE) {
        si.dwFlags = STARTF_USESTDHANDLES;
        si.hStdInput = ch.child[0];
        si.hStdOutput = ch.child[1];
        si.hStdError = ch.child[2];
    }
    memset(&pi, 0, sizeof(pi));
    QueryPerformanceFrequency(&f);
    QueryPerformanceCounter(&t0);
    ok = CreateProcessW(NULL, cmd, NULL, NULL, mode >= MODE_PIPE, fd->flags | CREATE_UNICODE_ENVIRONMENT, env,
                        null_cwd ? NULL : cwd_dos, &si, &pi);
    if (!ok) {
        gle = GetLastError();
    }
    QueryPerformanceCounter(&t1);
    spawn_ms = (double)(t1.QuadPart - t0.QuadPart) * 1000.0 / (double)f.QuadPart;
    chan_close_child(&ch);

    start = GetTickCount64();
    if (ok) {
        while (GetTickCount64() - start < 2500) {
            if (slurp(log_dos, logbuf, sizeof(logbuf)) && strstr(logbuf, "\nEND\n")) {
                ran = 1;
                ran_ms = GetTickCount64() - start;
                break;
            }
            Sleep(10);
        }
    }
    if (!ran) {
        slurp(log_dos, logbuf, sizeof(logbuf));
    }
    wait = WaitForSingleObject(pi.hProcess, 1000);
    if (wait == WAIT_FAILED) {
        wait_gle = GetLastError();
    }
    if (!GetExitCodeProcess(pi.hProcess, &code)) {
        code = 0xFFFFFFFFu;
    }
    chan_collect(&ch, 1, out_got, sizeof(out_got));
    chan_collect(&ch, 2, err_got, sizeof(err_got));
    for (int k = 0; k < 16; k++) {
        log_field(logbuf, keys[k], v[k], sizeof(v[k]));
    }
    fprintf(result_fp,
            "%s\t%s\t%s\t%s\t%s\t%d\t%lu\t%p\t%lu\t%s\t%lu\t%lu\t%.2f\t%d\t%llu"
            "\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n",
            ctx, tname, fd->name, mode_names[mode], null_cwd ? "null" : "explicit", ok ? 1 : 0, gle,
            (void *)pi.hProcess, pi.dwProcessId,
            wait == WAIT_OBJECT_0 ? "SIGNALED" : wait == WAIT_TIMEOUT ? "TIMEOUT" : "FAILED", wait_gle,
            code, spawn_ms, ran, ran_ms, v[0], v[1], v[2], v[3], v[4], v[5], v[6], v[7], v[8], v[9], v[10],
            v[11], v[12], v[13], v[14], v[15], out_got[0] ? out_got : "-", err_got[0] ? err_got : "-");
    fflush(result_fp);
    if (pi.hProcess) {
        CloseHandle(pi.hProcess);
    }
    if (pi.hThread) {
        CloseHandle(pi.hThread);
    }
    chan_free(&ch);
    free(env);
}

/* Describe one of our own std handles. */
static void describe_handle(FILE *f, const char *name, DWORD which)
{
    HANDLE h = GetStdHandle(which);
    DWORD mode = 0;
    DWORD type = (h && h != INVALID_HANDLE_VALUE) ? GetFileType(h) : 0;
    fprintf(f, "%s=%p type=%lu console=%d\n", name, (void *)h, type,
            (h && h != INVALID_HANDLE_VALUE && GetConsoleMode(h, &mode)) ? 1 : 0);
}

int wmain(int argc, WCHAR **argv)
{
    WSADATA wsa;
    char *ctx;
    char *result;
    char *logdir;
    char *cwd;
    WCHAR *cwd_dos;
    WCHAR *res_dos;
    char ctxpath[1024];
    FILE *cf;
    WCHAR curdir[MAX_PATH];

    if (argc < 6) {
        fwprintf(stderr, L"usage: %ls <ctx> <result.tsv> <logdir> <cwd> <target>...\n", argv[0]);
        return 2;
    }
    WSAStartup(MAKEWORD(2, 2), &wsa);
    ctx = to_utf8(argv[1]);
    result = to_utf8(argv[2]);
    logdir = to_utf8(argv[3]);
    cwd = to_utf8(argv[4]);
    cwd_dos = to_dos(cwd);
    res_dos = to_dos(result);
    result_fp = _wfopen(res_dos, L"a");
    if (!result_fp) {
        return 3;
    }
    snprintf(ctxpath, sizeof(ctxpath), "%s.ctx", result);
    cf = _wfopen(to_dos(ctxpath), L"a");
    if (cf) {
        GetCurrentDirectoryW(MAX_PATH, curdir);
        fprintf(cf, "[%s]\nconsole_window=%p\ncurdir=%ls\n", ctx, (void *)GetConsoleWindow(), curdir);
        describe_handle(cf, "stdin", STD_INPUT_HANDLE);
        describe_handle(cf, "stdout", STD_OUTPUT_HANDLE);
        describe_handle(cf, "stderr", STD_ERROR_HANDLE);
        fclose(cf);
    }
    for (int t = 5; t < argc; t++) {
        char *tunix = to_utf8(argv[t]);
        WCHAR *tdos = to_dos(tunix);
        const char *base = strrchr(tunix, '/');
        base = base ? base + 1 : tunix;
        for (size_t fi = 0; fi < sizeof(flagdefs) / sizeof(flagdefs[0]); fi++) {
            for (int m = 0; m < MODE_COUNT; m++) {
                run_case(ctx, base, tdos, &flagdefs[fi], m, 0, logdir, cwd_dos);
            }
        }
        run_case(ctx, base, tdos, &flagdefs[0], MODE_NONE, 1, logdir, cwd_dos);
        free(tunix);
    }
    fclose(result_fp);
    WSACleanup();
    return 0;
}
