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

static DWORD WINAPI stdin_thread(LPVOID arg)
{
    struct pump *p = arg;
    static char buf[65536];
    DWORD n = 0;
    if (p->from != NULL && p->from != INVALID_HANDLE_VALUE) {
        while (ReadFile(p->from, buf, (DWORD)sizeof(buf), &n, NULL) && n > 0) {
            DWORD off = 0;
            while (off < n) {
                DWORD w = 0;
                if (!WriteFile(p->to, buf + off, n - off, &w, NULL) || w == 0) {
                    CloseHandle(p->to);
                    park(NULL);
                }
                off += w;
            }
        }
    }
    CloseHandle(p->to);
    park(NULL);
}

enum mode { M_RUN, M_HOLD, M_CLOSEOUT, M_KILL, M_SURROGATE, M_NOSTD };

int wmain(int argc, WCHAR **argv);

int wmain(int argc, WCHAR **argv)
{
    static WCHAR cmd[CMD_CAP];
    static const WCHAR lone[] = { L'x', (WCHAR)0xD800, L'y', 0 };
    SECURITY_ATTRIBUTES sa = { sizeof(sa), NULL, TRUE };
    HANDLE in_r = NULL, in_w = NULL, out_r = NULL, out_w = NULL, err_r = NULL, err_w = NULL;
    STARTUPINFOW si;
    PROCESS_INFORMATION pi;
    struct pump pin, pout, perr;
    HANDLE done[2];
    DWORD ndone = 0;
    enum mode m;
    int first;
    DWORD kill_ms = 0;
    DWORD code = 0;
    ULONGLONG t0;
    WCHAR *exe;

    if (argc < 3) {
        fwprintf(stderr, L"usage: fl-testdrv.exe run|hold|closeout|kill <ms>|surrogate|nostd <exe> [args...]\n");
        return 2;
    }
    first = 2;
    if (wcscmp(argv[1], L"run") == 0) {
        m = M_RUN;
    } else if (wcscmp(argv[1], L"hold") == 0) {
        m = M_HOLD;
    } else if (wcscmp(argv[1], L"closeout") == 0) {
        m = M_CLOSEOUT;
    } else if (wcscmp(argv[1], L"surrogate") == 0) {
        m = M_SURROGATE;
    } else if (wcscmp(argv[1], L"nostd") == 0) {
        m = M_NOSTD;
    } else if (wcscmp(argv[1], L"kill") == 0 && argc >= 4) {
        m = M_KILL;
        kill_ms = (DWORD)wcstoul(argv[2], NULL, 10);
        first = 3;
    } else {
        fwprintf(stderr, L"fl-testdrv: unknown mode\n");
        return 2;
    }
    exe = to_dos(argv[first]);
    if (exe == NULL || append_quoted(cmd, CMD_CAP, exe) != 0) {
        return 2;
    }
    free(exe);
    for (int i = first + 1; i < argc; i++) {
        if (append_quoted(cmd, CMD_CAP, argv[i]) != 0) {
            return 2;
        }
    }
    if (m == M_SURROGATE && append_quoted(cmd, CMD_CAP, lone) != 0) {
        return 2;
    }

    memset(&si, 0, sizeof(si));
    si.cb = sizeof(si);
    if (m != M_NOSTD) {
        if (!CreatePipe(&in_r, &in_w, &sa, 0) || !CreatePipe(&out_r, &out_w, &sa, 0) ||
            !CreatePipe(&err_r, &err_w, &sa, 0)) {
            fwprintf(stderr, L"fl-testdrv: CreatePipe failed\n");
            return 2;
        }
        SetHandleInformation(in_w, HANDLE_FLAG_INHERIT, 0);
        SetHandleInformation(out_r, HANDLE_FLAG_INHERIT, 0);
        SetHandleInformation(err_r, HANDLE_FLAG_INHERIT, 0);
        si.dwFlags = STARTF_USESTDHANDLES;
        si.hStdInput = in_r;
        si.hStdOutput = out_w;
        si.hStdError = err_w;
    }
    memset(&pi, 0, sizeof(pi));
    t0 = GetTickCount64();
    if (!CreateProcessW(NULL, cmd, NULL, NULL, m != M_NOSTD, CREATE_NO_WINDOW | CREATE_UNICODE_ENVIRONMENT, NULL,
                        NULL, &si, &pi)) {
        fwprintf(stderr, L"fl-testdrv: CreateProcessW failed: %lu\n", GetLastError());
        return 2;
    }
    if (m != M_NOSTD) {
        CloseHandle(in_r);
        CloseHandle(out_w);
        CloseHandle(err_w);
        perr.from = err_r;
        perr.to = GetStdHandle(STD_ERROR_HANDLE);
        perr.done = done[ndone++] = CreateEventW(NULL, TRUE, FALSE, NULL);
        CloseHandle(CreateThread(NULL, 0, copy_thread, &perr, 0, NULL));
        if (m == M_CLOSEOUT) {
            static char chunk[4096];
            DWORD n = 0;
            if (ReadFile(out_r, chunk, (DWORD)sizeof(chunk), &n, NULL) && n > 0) {
                DWORD w = 0;
                WriteFile(GetStdHandle(STD_OUTPUT_HANDLE), chunk, n, &w, NULL);
            }
            CloseHandle(out_r);
        } else {
            pout.from = out_r;
            pout.to = GetStdHandle(STD_OUTPUT_HANDLE);
            pout.done = done[ndone++] = CreateEventW(NULL, TRUE, FALSE, NULL);
            CloseHandle(CreateThread(NULL, 0, copy_thread, &pout, 0, NULL));
        }
        if (m != M_HOLD) {
            pin.from = GetStdHandle(STD_INPUT_HANDLE);
            pin.to = in_w;
            pin.done = NULL;
            /* not joined: it may block on our own stdin */
            CloseHandle(CreateThread(NULL, 0, stdin_thread, &pin, 0, NULL));
        }
    }
    if (m == M_KILL) {
        if (WaitForSingleObject(pi.hProcess, kill_ms) == WAIT_TIMEOUT) {
            TerminateProcess(pi.hProcess, 99);
        }
    }
    WaitForSingleObject(pi.hProcess, INFINITE);
    GetExitCodeProcess(pi.hProcess, &code);
    if (ndone > 0) {
        WaitForMultipleObjects(ndone, done, TRUE, 5000);
    }
    fwprintf(stderr, L"fl-testdrv: rc=%lu ms=%llu\n", code, GetTickCount64() - t0);
    fflush(stderr);
    /* ExitProcess while every helper thread is parked (never returning). */
    ExitProcess(code);
}
