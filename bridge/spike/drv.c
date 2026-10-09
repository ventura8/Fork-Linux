/*
 * drv.c - B1/B2 spike driver: spawns a PE the way .NET's Process.Start does
 * (CreateProcessW, bInheritHandles=TRUE, STARTF_USESTDHANDLES with anonymous pipes,
 * CREATE_NO_WINDOW | CREATE_UNICODE_ENVIRONMENT, async readers on stdout/stderr).
 *
 *   drv.exe run <exe> [args...]          relay own stdin/stdout/stderr through pipes,
 *                                        exit with the child's exit code
 *   drv.exe bench <n> <exe> [args...]    n sequential spawns; prints latency stats (ms)
 *   drv.exe kill <ms> <marker> <exe> [args...]
 *                                        spawn, wait <ms>, create <marker> (unix path),
 *                                        TerminateProcess the child, report wait result
 *
 * <exe> is a unix path (mapped with wine_get_dos_file_name) or a DOS path.
 */
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <wchar.h>

int _dowildcard = 0;

typedef WCHAR *(*dos_name_fn)(const char *);

struct pump {
    HANDLE from;
    HANDLE to;    /* INVALID_HANDLE_VALUE: discard */
    ULONGLONG bytes;
};

struct child {
    PROCESS_INFORMATION pi;
    HANDLE in_w;   /* parent end of the child's stdin */
    HANDLE out_r;
    HANDLE err_r;
};

/* Map a unix path to DOS; DOS paths (X:...) are returned as a copy. */
static WCHAR *to_dos(const WCHAR *path)
{
    static dos_name_fn fn;
    static int init;
    char u[4096];
    if (path[0] != L'/') {
        return _wcsdup(path);
    }
    if (!init) {
        HMODULE k32 = GetModuleHandleW(L"kernel32.dll");
        init = 1;
        if (k32) {
            fn = (dos_name_fn)(void (*)(void))GetProcAddress(k32, "wine_get_dos_file_name");
        }
    }
    WideCharToMultiByte(CP_UTF8, 0, path, -1, u, (int)sizeof(u), NULL, NULL);
    if (fn) {
        WCHAR *r = fn(u);
        if (r) {
            return r;
        }
    }
    return _wcsdup(path);
}

/* Append arg with CommandLineToArgvW-compatible quoting. */
static void append_quoted(WCHAR *cmd, size_t cap, const WCHAR *arg)
{
    size_t len = wcslen(cmd);
    int needs = arg[0] == L'\0' || wcspbrk(arg, L" \t\n\v\"") != NULL;
    if (len && len + 1 < cap) {
        cmd[len++] = L' ';
    }
    if (needs && len + 1 < cap) {
        cmd[len++] = L'"';
    }
    for (const WCHAR *p = arg;; p++) {
        size_t bs = 0;
        while (*p == L'\\') {
            p++;
            bs++;
        }
        if (*p == L'\0') {
            for (size_t i = 0; i < (needs ? bs * 2 : bs) && len + 1 < cap; i++) {
                cmd[len++] = L'\\';
            }
            break;
        }
        for (size_t i = 0; i < (*p == L'"' ? bs * 2 + 1 : bs) && len + 1 < cap; i++) {
            cmd[len++] = L'\\';
        }
        if (len + 1 < cap) {
            cmd[len++] = *p;
        }
    }
    if (needs && len + 1 < cap) {
        cmd[len++] = L'"';
    }
    cmd[len] = L'\0';
}

/* Thread: copy `from` to `to` until EOF/broken pipe. */
static DWORD WINAPI pump_thread(LPVOID arg)
{
    struct pump *p = arg;
    char buf[65536];
    DWORD n;
    while (ReadFile(p->from, buf, sizeof(buf), &n, NULL) && n) {
        DWORD off = 0;
        p->bytes += n;
        while (p->to != INVALID_HANDLE_VALUE && off < n) {
            DWORD w = 0;
            if (!WriteFile(p->to, buf + off, n - off, &w, NULL)) {
                break;
            }
            off += w;
        }
    }
    return 0;
}

/* Thread: copy our stdin into the child's stdin pipe, then close it. */
static DWORD WINAPI stdin_thread(LPVOID arg)
{
    struct pump *p = arg;
    char buf[65536];
    DWORD n;
    if (p->from && p->from != INVALID_HANDLE_VALUE) {
        while (ReadFile(p->from, buf, sizeof(buf), &n, NULL) && n) {
            DWORD off = 0;
            while (off < n) {
                DWORD w = 0;
                if (!WriteFile(p->to, buf + off, n - off, &w, NULL)) {
                    goto out;
                }
                off += w;
            }
        }
    }
out:
    CloseHandle(p->to);
    return 0;
}

/* Spawn argv[0..] like .NET: pipes for all three std handles, CREATE_NO_WINDOW. */
static int spawn(struct child *c, int argc, WCHAR **argv)
{
    SECURITY_ATTRIBUTES sa = { sizeof(sa), NULL, TRUE };
    HANDLE in_r, out_w, err_w;
    STARTUPINFOW si;
    WCHAR cmd[32768] = L"";
    WCHAR *exe = to_dos(argv[0]);
    BOOL ok;
    append_quoted(cmd, 32768, exe);
    for (int i = 1; i < argc; i++) {
        append_quoted(cmd, 32768, argv[i]);
    }
    free(exe);
    if (!CreatePipe(&in_r, &c->in_w, &sa, 0) || !CreatePipe(&c->out_r, &out_w, &sa, 0) ||
        !CreatePipe(&c->err_r, &err_w, &sa, 0)) {
        return -1;
    }
    SetHandleInformation(c->in_w, HANDLE_FLAG_INHERIT, 0);
    SetHandleInformation(c->out_r, HANDLE_FLAG_INHERIT, 0);
    SetHandleInformation(c->err_r, HANDLE_FLAG_INHERIT, 0);
    memset(&si, 0, sizeof(si));
    si.cb = sizeof(si);
    si.dwFlags = STARTF_USESTDHANDLES;
    si.hStdInput = in_r;
    si.hStdOutput = out_w;
    si.hStdError = err_w;
    memset(&c->pi, 0, sizeof(c->pi));
    ok = CreateProcessW(NULL, cmd, NULL, NULL, TRUE, CREATE_NO_WINDOW | CREATE_UNICODE_ENVIRONMENT, NULL, NULL,
                        &si, &c->pi);
    CloseHandle(in_r);
    CloseHandle(out_w);
    CloseHandle(err_w);
    if (!ok) {
        fwprintf(stderr, L"drv: CreateProcessW(%ls) failed: %lu\n", cmd, GetLastError());
        CloseHandle(c->in_w);
        CloseHandle(c->out_r);
        CloseHandle(c->err_r);
        return -1;
    }
    return 0;
}

/* Close every handle of a finished child. */
static void child_close(struct child *c)
{
    CloseHandle(c->out_r);
    CloseHandle(c->err_r);
    CloseHandle(c->pi.hProcess);
    CloseHandle(c->pi.hThread);
}

static int cmp_double(const void *a, const void *b)
{
    double x = *(const double *)a;
    double y = *(const double *)b;
    return (x > y) - (x < y);
}

/* drv run: relay stdio, return the child's exit code. */
static int cmd_run(int argc, WCHAR **argv)
{
    struct child c;
    struct pump pin, pout, perr;
    HANDLE th[3];
    DWORD code = 255;
    if (spawn(&c, argc, argv)) {
        return 255;
    }
    pin.from = GetStdHandle(STD_INPUT_HANDLE);
    pin.to = c.in_w;
    pout.from = c.out_r;
    pout.to = GetStdHandle(STD_OUTPUT_HANDLE);
    pout.bytes = 0;
    perr.from = c.err_r;
    perr.to = GetStdHandle(STD_ERROR_HANDLE);
    perr.bytes = 0;
    th[0] = CreateThread(NULL, 0, stdin_thread, &pin, 0, NULL);
    th[1] = CreateThread(NULL, 0, pump_thread, &pout, 0, NULL);
    th[2] = CreateThread(NULL, 0, pump_thread, &perr, 0, NULL);
    WaitForSingleObject(c.pi.hProcess, INFINITE);
    WaitForMultipleObjects(2, th + 1, TRUE, INFINITE);
    GetExitCodeProcess(c.pi.hProcess, &code);
    child_close(&c);
    return (int)code;
}

/* drv bench: n sequential spawns, stdout/stderr drained, stdin closed at once. */
static int cmd_bench(int argc, WCHAR **argv)
{
    int n = _wtoi(argv[0]);
    double *ms;
    LARGE_INTEGER f;
    int failures = 0;
    double sum = 0;
    if (n <= 0) {
        return 2;
    }
    ms = calloc((size_t)n, sizeof(double));
    if (!ms) {
        return 1;
    }
    QueryPerformanceFrequency(&f);
    for (int i = 0; i < n; i++) {
        struct child c;
        struct pump pout = { 0 }, perr = { 0 };
        HANDLE th[2];
        DWORD code = 255;
        LARGE_INTEGER t0, t1;
        QueryPerformanceCounter(&t0);
        if (spawn(&c, argc - 1, argv + 1)) {
            return 1;
        }
        CloseHandle(c.in_w);
        pout.from = c.out_r;
        pout.to = INVALID_HANDLE_VALUE;
        perr.from = c.err_r;
        perr.to = INVALID_HANDLE_VALUE;
        th[0] = CreateThread(NULL, 0, pump_thread, &pout, 0, NULL);
        th[1] = CreateThread(NULL, 0, pump_thread, &perr, 0, NULL);
        WaitForSingleObject(c.pi.hProcess, INFINITE);
        WaitForMultipleObjects(2, th, TRUE, INFINITE);
        GetExitCodeProcess(c.pi.hProcess, &code);
        QueryPerformanceCounter(&t1);
        CloseHandle(th[0]);
        CloseHandle(th[1]);
        child_close(&c);
        if (code != 0) {
            failures++;
        }
        ms[i] = (double)(t1.QuadPart - t0.QuadPart) * 1000.0 / (double)f.QuadPart;
        sum += ms[i];
    }
    qsort(ms, (size_t)n, sizeof(double), cmp_double);
    printf("n=%d min=%.2f p50=%.2f p95=%.2f max=%.2f mean=%.2f nonzero_exit=%d\n", n, ms[0], ms[n / 2],
           ms[(n * 95) / 100 < n ? (n * 95) / 100 : n - 1], ms[n - 1], sum / n, failures);
    free(ms);
    return failures ? 1 : 0;
}

/* drv kill: spawn, sleep, drop a marker file, TerminateProcess. */
static int cmd_kill(int argc, WCHAR **argv)
{
    struct child c;
    struct pump pout = { 0 }, perr = { 0 };
    DWORD delay = (DWORD)_wtoi(argv[0]);
    WCHAR *marker = to_dos(argv[1]);
    HANDLE h;
    DWORD wr;
    DWORD code = 0;
    if (spawn(&c, argc - 2, argv + 2)) {
        return 1;
    }
    pout.from = c.out_r;
    pout.to = GetStdHandle(STD_OUTPUT_HANDLE);
    perr.from = c.err_r;
    perr.to = GetStdHandle(STD_ERROR_HANDLE);
    CloseHandle(CreateThread(NULL, 0, pump_thread, &pout, 0, NULL));
    CloseHandle(CreateThread(NULL, 0, pump_thread, &perr, 0, NULL));
    Sleep(delay);
    h = CreateFileW(marker, GENERIC_WRITE, 0, NULL, CREATE_ALWAYS, 0, NULL);
    CloseHandle(h);
    if (!TerminateProcess(c.pi.hProcess, 99)) {
        printf("drv: TerminateProcess failed: %lu\n", GetLastError());
        return 1;
    }
    wr = WaitForSingleObject(c.pi.hProcess, 5000);
    GetExitCodeProcess(c.pi.hProcess, &code);
    printf("drv: terminated wait=%s exit=%lu\n", wr == WAIT_OBJECT_0 ? "SIGNALED" : "TIMEOUT", code);
    fflush(stdout);
    return 0;
}

int wmain(int argc, WCHAR **argv)
{
    if (argc >= 3 && wcscmp(argv[1], L"run") == 0) {
        return cmd_run(argc - 2, argv + 2);
    }
    if (argc >= 4 && wcscmp(argv[1], L"bench") == 0) {
        return cmd_bench(argc - 2, argv + 2);
    }
    if (argc >= 5 && wcscmp(argv[1], L"kill") == 0) {
        return cmd_kill(argc - 2, argv + 2);
    }
    fwprintf(stderr, L"usage: drv.exe run|bench <n>|kill <ms> <marker> <exe> [args...]\n");
    return 2;
}
