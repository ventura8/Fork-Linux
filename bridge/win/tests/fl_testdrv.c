/*
 * fl_testdrv.c - TEST-ONLY driver for the Wine tier of the Windows bridge tests
 * (tests/bridge/test_shims_wine.py). Never installed or shipped.
 *
 * It starts a program the way Fork's .NET Process.Start does (CreateProcessW,
 * bInheritHandles, STARTF_USESTDHANDLES with anonymous pipes, CREATE_NO_WINDOW |
 * CREATE_UNICODE_ENVIRONMENT) and exercises the edge cases a shell cannot produce:
 *
 *   fl-testdrv.exe run <exe> [args...]        relay our stdin/stdout/stderr through pipes;
 *                                             exit with the child's exit code
 *   fl-testdrv.exe hold <exe> [args...]       the child's stdin pipe stays open (never
 *                                             written, never closed) until it exits
 *   fl-testdrv.exe closeout <exe> [args...]   read one chunk of the child's stdout, then
 *                                             close the read end (a reader that went away)
 *   fl-testdrv.exe kill <ms> <exe> [args...]  TerminateProcess the child after <ms>
 *   fl-testdrv.exe surrogate <exe> [args...]  like run, with an extra argument holding a
 *                                             lone UTF-16 surrogate (U+D800)
 *   fl-testdrv.exe nostd <exe> [args...]      no std handles at all (a GUI parent)
 *   fl-testdrv.exe long <exe> [args...]       like run, with 100-character arguments added
 *                                             until the command line is nearly 32767 long
 *
 * Every mode prints "fl-testdrv: rc=<code> ms=<elapsed>" on its stderr when the child
 * is gone. <exe> may be a Unix path (mapped with wine_get_dos_file_name).
 */
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <wchar.h>

int _dowildcard = 0;

#define CMD_CAP 32768u

typedef WCHAR *(CDECL *dos_name_fn)(const char *unix_path);

struct pump {
    HANDLE from;
    HANDLE to;   /* NULL: discard */
    HANDLE done; /* manual-reset event set when the copy is finished */
};

/*
 * Helper threads never return (spike B2 bug 1: a thread returning while the main thread
 * is in ExitProcess can make Wine report exit code 0). They signal `done` and park.
 */
static _Noreturn void park(HANDLE done)
{
    if (done != NULL) {
        SetEvent(done);
    }
    for (;;) {
        Sleep(INFINITE);
    }
}

static WCHAR *to_dos(const WCHAR *path)
{
    HMODULE k32;
    dos_name_fn fn = NULL;
    char u[4096];
    WCHAR *r;
    if (path[0] != L'/') {
        return _wcsdup(path);
    }
    k32 = GetModuleHandleW(L"kernel32.dll");
    if (k32 != NULL) {
        fn = (dos_name_fn)(void (*)(void))GetProcAddress(k32, "wine_get_dos_file_name");
    }
    if (fn == NULL || WideCharToMultiByte(CP_UTF8, 0, path, -1, u, (int)sizeof(u), NULL, NULL) <= 0) {
        return _wcsdup(path);
    }
    r = fn(u);
    if (r == NULL) {
        return _wcsdup(path);
    }
    {
        WCHAR *c = _wcsdup(r);
        HeapFree(GetProcessHeap(), 0, r);
        return c;
    }
}

/* Append arg with CommandLineToArgvW-compatible quoting; 0 on success. */
static int append_quoted(WCHAR *cmd, size_t cap, const WCHAR *arg)
{
    size_t len = wcslen(cmd);
    int needs = arg[0] == L'\0' || wcspbrk(arg, L" \t\n\v\"") != NULL;
    /* worst case: separator, quotes, every char escaped */
    if (len + 3 + 2 * wcslen(arg) + 1 > cap) {
        return -1;
    }
    if (len > 0) {
        cmd[len++] = L' ';
    }
    if (needs) {
        cmd[len++] = L'"';
    }
    for (const WCHAR *p = arg;; p++) {
        size_t bs = 0;
        while (*p == L'\\') {
            p++;
            bs++;
        }
        if (*p == L'\0') {
            for (size_t i = 0; i < (needs ? bs * 2 : bs); i++) {
                cmd[len++] = L'\\';
            }
            break;
        }
        for (size_t i = 0; i < (*p == L'"' ? bs * 2 + 1 : bs); i++) {
            cmd[len++] = L'\\';
        }
        cmd[len++] = *p;
    }
    if (needs) {
        cmd[len++] = L'"';
    }
    cmd[len] = L'\0';
    return 0;
}

static DWORD WINAPI copy_thread(LPVOID arg)
{
    struct pump *p = arg;
    char *buf = malloc(65536u);
    DWORD n = 0;
    if (buf == NULL) {
        park(p->done);
    }
    while (ReadFile(p->from, buf, 65536u, &n, NULL) && n > 0) {
        DWORD off = 0;
        while (p->to != NULL && off < n) {
            DWORD w = 0;
            if (!WriteFile(p->to, buf + off, n - off, &w, NULL) || w == 0) {
                break;
            }
            off += w;
        }
    }
    free(buf);
    park(p->done);
}

/* Write all n bytes of buf to h; 0 on success, -1 when a write fails. */
static int write_all(HANDLE h, const char *buf, DWORD n)
{
    DWORD off = 0;
    while (off < n) {
        DWORD w = 0;
        if (!WriteFile(h, buf + off, n - off, &w, NULL) || w == 0) {
            return -1;
        }
        off += w;
    }
    return 0;
}

static DWORD WINAPI stdin_thread(LPVOID arg)
{
    struct pump *p = arg;
    static char buf[65536];
    DWORD n = 0;
    if (p->from != NULL && p->from != INVALID_HANDLE_VALUE) {
        while (ReadFile(p->from, buf, (DWORD)sizeof(buf), &n, NULL) && n > 0 && write_all(p->to, buf, n) == 0) {
            n = 0;
        }
    }
    CloseHandle(p->to);
    park(NULL);
}

enum mode { M_RUN, M_HOLD, M_CLOSEOUT, M_KILL, M_SURROGATE, M_NOSTD, M_LONG };

/* Parsed command line. */
struct opts {
    enum mode m;
    int first;      /* index of <exe> in argv */
    DWORD kill_ms;
};

/* The child's ends and our ends of its three std pipes. */
struct pipes {
    HANDLE in_r;
    HANDLE in_w;
    HANDLE out_r;
    HANDLE out_w;
    HANDLE err_r;
    HANDLE err_w;
};

/* Helper threads and the events of the ones main joins. */
struct relay {
    struct pump pin;
    struct pump pout;
    struct pump perr;
    HANDLE done[2];
    DWORD ndone;
};

/* 0 and *o filled, or -1 after printing why (usage error, exit 2). */
static int parse_opts(int argc, WCHAR **argv, struct opts *o)
{
    static const struct {
        const WCHAR *name;
        enum mode m;
    } simple[] = {
        { L"run", M_RUN }, { L"hold", M_HOLD }, { L"closeout", M_CLOSEOUT },
        { L"surrogate", M_SURROGATE }, { L"nostd", M_NOSTD }, { L"long", M_LONG },
    };
    if (argc < 3) {
        fwprintf(stderr, L"usage: fl-testdrv.exe run|hold|closeout|kill <ms>|surrogate|nostd|long <exe> [args...]\n");
        return -1;
    }
    o->first = 2;
    o->kill_ms = 0;
    for (size_t i = 0; i < sizeof(simple) / sizeof(simple[0]); i++) {
        if (wcscmp(argv[1], simple[i].name) == 0) {
            o->m = simple[i].m;
            return 0;
        }
    }
    if (wcscmp(argv[1], L"kill") == 0 && argc >= 4) {
        o->m = M_KILL;
        o->kill_ms = (DWORD)wcstoul(argv[2], NULL, 10);
        o->first = 3;
        return 0;
    }
    fwprintf(stderr, L"fl-testdrv: unknown mode\n");
    return -1;
}

/* The child's command line in cmd (CMD_CAP units); 0 on success. */
static int build_cmdline(WCHAR *cmd, int argc, WCHAR **argv, const struct opts *o)
{
    static const WCHAR lone[] = { L'x', (WCHAR)0xD800, L'y', 0 };
    WCHAR *exe = to_dos(argv[o->first]);
    if (exe == NULL || append_quoted(cmd, CMD_CAP, exe) != 0) {
        return -1;
    }
    free(exe);
    for (int i = o->first + 1; i < argc; i++) {
        if (append_quoted(cmd, CMD_CAP, argv[i]) != 0) {
            return -1;
        }
    }
    if (o->m == M_SURROGATE && append_quoted(cmd, CMD_CAP, lone) != 0) {
        return -1;
    }
    if (o->m == M_LONG) {
        WCHAR pad[101];
        int more = 1;
        wmemset(pad, L'a', 100);
        pad[100] = L'\0';
        while (more) {
            more = append_quoted(cmd, CMD_CAP, pad) == 0;
        }
    }
    return 0;
}

/* Anonymous pipes for the child's std handles, our ends not inheritable; 0 on success. */
static int make_pipes(struct pipes *pp, STARTUPINFOW *si)
{
    SECURITY_ATTRIBUTES sa = { sizeof(sa), NULL, TRUE };
    if (!CreatePipe(&pp->in_r, &pp->in_w, &sa, 0) || !CreatePipe(&pp->out_r, &pp->out_w, &sa, 0) ||
        !CreatePipe(&pp->err_r, &pp->err_w, &sa, 0)) {
        fwprintf(stderr, L"fl-testdrv: CreatePipe failed\n");
        return -1;
    }
    SetHandleInformation(pp->in_w, HANDLE_FLAG_INHERIT, 0);
    SetHandleInformation(pp->out_r, HANDLE_FLAG_INHERIT, 0);
    SetHandleInformation(pp->err_r, HANDLE_FLAG_INHERIT, 0);
    si->dwFlags = STARTF_USESTDHANDLES;
    si->hStdInput = pp->in_r;
    si->hStdOutput = pp->out_w;
    si->hStdError = pp->err_w;
    return 0;
}

/* Start a joined copy thread from -> to. */
static void start_copy(struct relay *r, struct pump *p, HANDLE from, HANDLE to)
{
    HANDLE ev = CreateEventW(NULL, TRUE, FALSE, NULL);
    p->from = from;
    p->to = to;
    p->done = ev;
    r->done[r->ndone] = ev;
    r->ndone++;
    CloseHandle(CreateThread(NULL, 0, copy_thread, p, 0, NULL));
}

/* closeout: relay one chunk of the child's stdout, then close the read end. */
static void close_out_after_one_chunk(HANDLE out_r)
{
    static char chunk[4096];
    DWORD n = 0;
    if (ReadFile(out_r, chunk, (DWORD)sizeof(chunk), &n, NULL) && n > 0) {
        DWORD w = 0;
        WriteFile(GetStdHandle(STD_OUTPUT_HANDLE), chunk, n, &w, NULL);
    }
    CloseHandle(out_r);
}

/* After the child started: close its ends and run the stdio helper threads. */
static void start_relay(struct relay *r, const struct pipes *pp, enum mode m)
{
    CloseHandle(pp->in_r);
    CloseHandle(pp->out_w);
    CloseHandle(pp->err_w);
    start_copy(r, &r->perr, pp->err_r, GetStdHandle(STD_ERROR_HANDLE));
    if (m == M_CLOSEOUT) {
        close_out_after_one_chunk(pp->out_r);
    } else {
        start_copy(r, &r->pout, pp->out_r, GetStdHandle(STD_OUTPUT_HANDLE));
    }
    if (m != M_HOLD) {
        r->pin.from = GetStdHandle(STD_INPUT_HANDLE);
        r->pin.to = pp->in_w;
        r->pin.done = NULL;
        /* not joined: it may block on our own stdin */
        CloseHandle(CreateThread(NULL, 0, stdin_thread, &r->pin, 0, NULL));
    }
}

int wmain(int argc, WCHAR **argv);

int wmain(int argc, WCHAR **argv)
{
    static WCHAR cmd[CMD_CAP];
    static struct relay r;
    struct pipes pp;
    STARTUPINFOW si;
    PROCESS_INFORMATION pi;
    struct opts o;
    DWORD code = 0;
    ULONGLONG t0;

    if (parse_opts(argc, argv, &o) != 0 || build_cmdline(cmd, argc, argv, &o) != 0) {
        return 2;
    }
    memset(&pp, 0, sizeof(pp));
    memset(&si, 0, sizeof(si));
    si.cb = sizeof(si);
    if (o.m != M_NOSTD && make_pipes(&pp, &si) != 0) {
        return 2;
    }
    memset(&pi, 0, sizeof(pi));
    t0 = GetTickCount64();
    if (!CreateProcessW(NULL, cmd, NULL, NULL, o.m != M_NOSTD, CREATE_NO_WINDOW | CREATE_UNICODE_ENVIRONMENT, NULL,
                        NULL, &si, &pi)) {
        fwprintf(stderr, L"fl-testdrv: CreateProcessW failed: %lu\n", GetLastError());
        return 2;
    }
    if (o.m != M_NOSTD) {
        start_relay(&r, &pp, o.m);
    }
    if (o.m == M_KILL && WaitForSingleObject(pi.hProcess, o.kill_ms) == WAIT_TIMEOUT) {
        TerminateProcess(pi.hProcess, 99);
    }
    WaitForSingleObject(pi.hProcess, INFINITE);
    GetExitCodeProcess(pi.hProcess, &code);
    if (r.ndone > 0) {
        WaitForMultipleObjects(r.ndone, r.done, TRUE, 5000);
    }
    fwprintf(stderr, L"fl-testdrv: rc=%lu ms=%llu\n", code, GetTickCount64() - t0);
    fflush(stderr);
    /* ExitProcess while every helper thread is parked (never returning). */
    ExitProcess(code);
}
