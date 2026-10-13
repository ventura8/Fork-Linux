/*
 * fl_testdriver.c - TEST-ONLY driver for the Wine tier of the native-git bridge
 * (tests/bridge/wine/test_wine_bridge.py) and its benchmark. Never installed or shipped.
 *
 * It starts a program the way Fork's .NET Process.Start does (UseShellExecute=false with
 * redirected streams): three anonymous pipes (CreatePipe), STARTF_USESTDHANDLES,
 * bInheritHandles, CREATE_NO_WINDOW | CREATE_UNICODE_ENVIRONMENT, an explicit sorted
 * environment block, an explicit working directory, lpApplicationName NULL and a command
 * line quoted like .NET's PasteArguments; stdout and stderr are drained by asynchronous
 * reader threads while stdin is written by a third.
 *
 *   fl-testdriver.exe run [options] -- <exe> [args...]
 *       one run; prints one JSON object on stdout:
 *       {"rc":N,"ms":F,"pid":N,"killed":B,"timeout":B,"pipes_hung":B,
 *        "stdout_len":N,"stdout_sha256":"..","stdout_b64":"..","stdout_truncated":B,
 *        "stderr_len":N,"stderr_sha256":"..","stderr_b64":"..","stderr_truncated":B}
 *   fl-testdriver.exe bench <N> [options] -- <exe> [args...]
 *       N sequential runs (output read and discarded); prints
 *       {"n":N,"ok":N,"failures":N,"p50_ms":F,"p95_ms":F,"min_ms":F,"max_ms":F,"mean_ms":F}
 *       a run "fails" when the program cannot be started or exits non-zero.
 *
 *   options:
 *     --cwd DIR          working directory of the child
 *     --env K=V          set K in the child's environment (repeatable)
 *     --unset K          remove K from the child's environment (repeatable)
 *     --stdin FILE       write FILE to the child's stdin, then close it (default: close
 *                        stdin at once, as Fork does when it has nothing to send)
 *     --kill-after MS    TerminateProcess the child after MS milliseconds (run only)
 *     --timeout MS       give up waiting after MS ms: TerminateProcess, "timeout":true
 *                        (default 600000)
 *     --max-capture N    bytes of each stream returned as base64 (default 16 MiB); the
 *                        length and sha256 always cover the whole stream
 *
 * <exe>, --cwd and --stdin may be Unix paths (starting with '/'); they are mapped with
 * wine_get_dos_file_name. The driver's own exit status: 0 when a JSON result was
 * printed, 2 usage error, 3 the program could not be started (JSON {"error":..} printed).
 * Reader threads always return on their own and are joined before ExitProcess (spike B2
 * fix 1 concerns threads returning while ExitProcess runs).
 */
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>

#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <wchar.h>

#include "fl_sha256.h"

int _dowildcard = 0;

#define CMD_CAP 32767u
#define MAX_ENV_OPS 256
#define READ_CHUNK 65536u
#define PIPE_DRAIN_MS 10000u

typedef WCHAR *(CDECL *dos_name_fn)(const char *unix_path);

struct opts {
    const WCHAR *cwd;
    const WCHAR *stdin_file;
    const WCHAR *set[MAX_ENV_OPS];
    size_t nset;
    const WCHAR *unset[MAX_ENV_OPS];
    size_t nunset;
    DWORD kill_after; /* 0: never */
    DWORD timeout;
    size_t max_capture;
};

struct sink {
    HANDLE from;
    int keep; /* 0: discard (bench) */
    uint8_t *buf;
    size_t len;
    size_t cap;
    int oom;
};

struct feeder {
    HANDLE from; /* file, or NULL */
    HANDLE to;
};

struct result {
    DWORD rc;
    DWORD pid;
    double ms;
    int killed;
    int timed_out;
    int pipes_hung;
    struct sink out;
    struct sink err;
};

/* ---------------------------------------------------------------- output helpers */

static void put_bytes(const char *s, size_t n)
{
    HANDLE h = GetStdHandle(STD_OUTPUT_HANDLE);
    while (n > 0) {
        DWORD w = 0;
        DWORD want = n > 0x10000000u ? 0x10000000u : (DWORD)n;
        if (h == NULL || h == INVALID_HANDLE_VALUE || !WriteFile(h, s, want, &w, NULL) || w == 0) {
            return;
        }
        s += w;
        n -= w;
    }
}

static void put_str(const char *s)
{
    put_bytes(s, strlen(s));
}

static void put_fmt(const char *fmt, ...)
{
    char line[512];
    int n;
    va_list ap;
    va_start(ap, fmt);
    n = vsnprintf(line, sizeof(line), fmt, ap);
    va_end(ap);
    if (n > 0) {
        put_bytes(line, (size_t)n < sizeof(line) ? (size_t)n : sizeof(line) - 1u);
    }
}

static void put_b64(const uint8_t *d, size_t n)
{
    static const char tbl[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    char out[4096];
    size_t o = 0;
    for (size_t i = 0; i < n; i += 3) {
        uint32_t v = (uint32_t)d[i] << 16;
        size_t rem = n - i;
        if (rem > 1) {
            v |= (uint32_t)d[i + 1] << 8;
        }
        if (rem > 2) {
            v |= d[i + 2];
        }
        out[o++] = tbl[(v >> 18) & 63u];
        out[o++] = tbl[(v >> 12) & 63u];
        out[o++] = rem > 1 ? tbl[(v >> 6) & 63u] : '=';
        out[o++] = rem > 2 ? tbl[v & 63u] : '=';
        if (o + 4 > sizeof(out)) {
            put_bytes(out, o);
            o = 0;
        }
    }
    put_bytes(out, o);
}

static void put_stream(const char *name, const struct sink *s, size_t max_capture)
{
    uint8_t digest[32];
    char hex[65];
    size_t shown = s->len < max_capture ? s->len : max_capture;
    fl_sha256(s->buf, s->len, digest);
    fl_hex_encode(digest, sizeof(digest), hex);
    put_fmt(",\"%s_len\":%llu,\"%s_sha256\":\"%s\",\"%s_truncated\":%s,\"%s_b64\":\"", name,
            (unsigned long long)s->len, name, hex, name, shown < s->len ? "true" : "false", name);
    put_b64(s->buf, shown);
    put_str("\"");
}

static void fail_json(const char *what, DWORD gle)
{
    put_fmt("{\"error\":\"%s\",\"gle\":%lu}\n", what, (unsigned long)gle);
}

/* ---------------------------------------------------------------- paths, quoting, env */

/* Unix path ('/'...) -> Windows path via wine_get_dos_file_name; else a copy. */
static WCHAR *to_dos(const WCHAR *path)
{
    HMODULE k32;
    dos_name_fn fn = NULL;
    char u[4096];
    WCHAR *r;
    WCHAR *c;
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
    c = _wcsdup(r);
    HeapFree(GetProcessHeap(), 0, r);
    return c;
}

/* Append arg with CommandLineToArgvW-compatible quoting (.NET PasteArguments); 0 ok. */
static int append_quoted(WCHAR *cmd, size_t cap, const WCHAR *arg)
{
    size_t len = wcslen(cmd);
    int needs = arg[0] == L'\0' || wcspbrk(arg, L" \t\n\v\"") != NULL;
    if (len + 3u + 2u * wcslen(arg) + 1u > cap) {
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
            for (size_t i = 0; i < (needs ? bs * 2u : bs); i++) {
                cmd[len++] = L'\\';
            }
            break;
        }
        for (size_t i = 0; i < (*p == L'"' ? bs * 2u + 1u : bs); i++) {
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

static size_t name_len(const WCHAR *kv)
{
    const WCHAR *eq = wcschr(kv + (kv[0] == L'=' ? 1 : 0), L'=');
    return eq != NULL ? (size_t)(eq - kv) : wcslen(kv);
}

static int same_name(const WCHAR *a, const WCHAR *b)
{
    size_t la = name_len(a);
    return la == name_len(b) && _wcsnicmp(a, b, la) == 0;
}

static int cmp_env(const void *pa, const void *pb)
{
    const WCHAR *a = *(const WCHAR *const *)pa;
    const WCHAR *b = *(const WCHAR *const *)pb;
    size_t la = name_len(a);
    size_t lb = name_len(b);
    int c = _wcsnicmp(a, b, la < lb ? la : lb);
    if (c != 0) {
        return c;
    }
    return la < lb ? -1 : (la > lb ? 1 : 0);
}

/* Our environment with the --unset / --env edits, sorted like .NET does; NULL on error. */
static WCHAR *build_env(const struct opts *o)
{
    WCHAR *cur = GetEnvironmentStringsW();
    const WCHAR **vars;
    size_t n = 0;
    size_t cap = o->nset + 1u;
    size_t total = 1;
    WCHAR *blk;
    WCHAR *w;
    if (cur == NULL) {
        return NULL;
    }
    for (const WCHAR *p = cur; *p != L'\0'; p += wcslen(p) + 1u) {
        cap++;
    }
    vars = calloc(cap, sizeof(*vars));
    if (vars == NULL) {
        FreeEnvironmentStringsW(cur);
        return NULL;
    }
    for (const WCHAR *p = cur; *p != L'\0'; p += wcslen(p) + 1u) {
        int drop = 0;
        for (size_t i = 0; i < o->nunset && !drop; i++) {
            drop = same_name(p, o->unset[i]);
        }
        for (size_t i = 0; i < o->nset && !drop; i++) {
            drop = same_name(p, o->set[i]);
        }
        if (!drop) {
            vars[n++] = p;
        }
    }
    for (size_t i = 0; i < o->nset; i++) {
        vars[n++] = o->set[i];
    }
    qsort(vars, n, sizeof(*vars), cmp_env);
    for (size_t i = 0; i < n; i++) {
        total += wcslen(vars[i]) + 1u;
    }
    blk = calloc(total + 1u, sizeof(WCHAR));
    if (blk != NULL) {
        w = blk;
        for (size_t i = 0; i < n; i++) {
            size_t l = wcslen(vars[i]);
            memcpy(w, vars[i], l * sizeof(WCHAR));
            w += l + 1u;
        }
    }
    free(vars);
    FreeEnvironmentStringsW(cur);
    return blk;
}

/* ---------------------------------------------------------------- pipe threads */

static DWORD WINAPI reader_thread(LPVOID arg)
{
    struct sink *s = arg;
    uint8_t scratch[4096];
    for (;;) {
        DWORD n = 0;
        uint8_t *dst = scratch;
        DWORD want = (DWORD)sizeof(scratch);
        if (s->keep && !s->oom) {
            if (s->cap - s->len < READ_CHUNK) {
                size_t ncap = s->cap == 0 ? 4u * READ_CHUNK : s->cap * 2u;
                uint8_t *nb = realloc(s->buf, ncap);
                if (nb == NULL) {
                    s->oom = 1;
                } else {
                    s->buf = nb;
                    s->cap = ncap;
                }
            }
            if (!s->oom) {
                dst = s->buf + s->len;
                want = READ_CHUNK;
            }
        }
        if (!ReadFile(s->from, dst, want, &n, NULL) || n == 0) {
            break;
        }
        if (dst != scratch) {
            s->len += n;
        }
    }
    return 0;
}

static DWORD WINAPI feeder_thread(LPVOID arg)
{
    struct feeder *f = arg;
    static uint8_t buf[READ_CHUNK];
    int broken = 0;
    if (f->from != NULL) {
        DWORD n = 0;
        while (!broken && ReadFile(f->from, buf, (DWORD)sizeof(buf), &n, NULL) && n > 0) {
            DWORD off = 0;
            while (off < n) {
                DWORD w = 0;
                if (!WriteFile(f->to, buf + off, n - off, &w, NULL) || w == 0) {
                    broken = 1; /* the child closed its stdin */
                    break;
                }
                off += w;
            }
        }
    }
    CloseHandle(f->to);
    return 0;
}

/* Wait for a reader thread; if it is still blocked after `ms`, cancel its read. */
static int join_thread(HANDLE t, DWORD ms)
{
    int hung = 0;
    if (t == NULL) {
        return 0;
    }
    if (WaitForSingleObject(t, ms) == WAIT_TIMEOUT) {
        hung = 1;
        for (int i = 0; i < 50 && WaitForSingleObject(t, 20) == WAIT_TIMEOUT; i++) {
            CancelSynchronousIo(t);
        }
    }
    CloseHandle(t);
    return hung;
}

/* ---------------------------------------------------------------- one run */

/* Returns 0 (res filled), or -1 with GetLastError() of the failing step in *gle. */
static int run_once(const WCHAR *cmdline, const WCHAR *cwd, const WCHAR *envblk, const struct opts *o, int keep,
                    struct result *res, const char **what, DWORD *gle)
{
    static WCHAR cmd[CMD_CAP];
    SECURITY_ATTRIBUTES sa = { sizeof(sa), NULL, TRUE };
    HANDLE in_r = NULL, in_w = NULL, out_r = NULL, out_w = NULL, err_r = NULL, err_w = NULL;
    HANDLE tin = NULL, tout = NULL, terr = NULL;
    HANDLE fin = NULL;
    STARTUPINFOW si;
    PROCESS_INFORMATION pi;
    struct feeder feed;
    LARGE_INTEGER f, t0, t1;
    DWORD wait;

    memset(res, 0, sizeof(*res));
    wcscpy(cmd, cmdline); /* CreateProcessW may write into it */
    if (o->stdin_file != NULL) {
        WCHAR *p = to_dos(o->stdin_file);
        fin = p != NULL ? CreateFileW(p, GENERIC_READ, FILE_SHARE_READ, NULL, OPEN_EXISTING, 0, NULL)
                        : INVALID_HANDLE_VALUE;
        free(p);
        if (fin == INVALID_HANDLE_VALUE) {
            *what = "cannot open --stdin file";
            *gle = GetLastError();
            return -1;
        }
    }
    if (!CreatePipe(&in_r, &in_w, &sa, 0) || !CreatePipe(&out_r, &out_w, &sa, 0) ||
        !CreatePipe(&err_r, &err_w, &sa, 0)) {
        *what = "CreatePipe failed";
        *gle = GetLastError();
        return -1;
    }
    SetHandleInformation(in_w, HANDLE_FLAG_INHERIT, 0);
    SetHandleInformation(out_r, HANDLE_FLAG_INHERIT, 0);
    SetHandleInformation(err_r, HANDLE_FLAG_INHERIT, 0);
    memset(&si, 0, sizeof(si));
    si.cb = sizeof(si);
    si.dwFlags = STARTF_USESTDHANDLES;
    si.hStdInput = in_r;
    si.hStdOutput = out_w;
    si.hStdError = err_w;
    memset(&pi, 0, sizeof(pi));

    QueryPerformanceFrequency(&f);
    QueryPerformanceCounter(&t0);
    if (!CreateProcessW(NULL, cmd, NULL, NULL, TRUE, CREATE_NO_WINDOW | CREATE_UNICODE_ENVIRONMENT,
                        (LPVOID)(uintptr_t)envblk, cwd, &si, &pi)) {
        *what = "CreateProcessW failed";
        *gle = GetLastError();
        CloseHandle(in_r);
        CloseHandle(in_w);
        CloseHandle(out_r);
        CloseHandle(out_w);
        CloseHandle(err_r);
        CloseHandle(err_w);
        if (fin != NULL) {
            CloseHandle(fin);
        }
        return -1;
    }
    CloseHandle(in_r);
    CloseHandle(out_w);
    CloseHandle(err_w);
    res->pid = pi.dwProcessId;

    res->out.from = out_r;
    res->out.keep = keep;
    res->err.from = err_r;
    res->err.keep = keep;
    tout = CreateThread(NULL, 0, reader_thread, &res->out, 0, NULL);
    terr = CreateThread(NULL, 0, reader_thread, &res->err, 0, NULL);
    if (fin != NULL) {
        feed.from = fin;
        feed.to = in_w;
        tin = CreateThread(NULL, 0, feeder_thread, &feed, 0, NULL);
    } else {
        CloseHandle(in_w); /* nothing to send: EOF at once */
    }

    if (o->kill_after > 0 && WaitForSingleObject(pi.hProcess, o->kill_after) == WAIT_TIMEOUT) {
        TerminateProcess(pi.hProcess, 1);
        res->killed = 1;
    }
    wait = WaitForSingleObject(pi.hProcess, o->timeout);
    if (wait == WAIT_TIMEOUT) {
        TerminateProcess(pi.hProcess, 1);
        res->timed_out = 1;
        WaitForSingleObject(pi.hProcess, 5000);
    }
    GetExitCodeProcess(pi.hProcess, &res->rc);
    res->pipes_hung = join_thread(tout, PIPE_DRAIN_MS) | join_thread(terr, PIPE_DRAIN_MS);
    QueryPerformanceCounter(&t1);
    res->pipes_hung |= join_thread(tin, PIPE_DRAIN_MS);
    res->ms = (double)(t1.QuadPart - t0.QuadPart) * 1000.0 / (double)f.QuadPart;
    CloseHandle(out_r);
    CloseHandle(err_r);
    if (fin != NULL) {
        CloseHandle(fin);
    }
    CloseHandle(pi.hThread);
    CloseHandle(pi.hProcess);
    return 0;
}

/* ---------------------------------------------------------------- main */

static void usage(void)
{
    fputws(L"usage: fl-testdriver.exe run [options] -- <exe> [args...]\n"
           L"       fl-testdriver.exe bench <N> [options] -- <exe> [args...]\n"
           L"options: --cwd DIR --env K=V --unset K --stdin FILE --kill-after MS --timeout MS --max-capture N\n",
           stderr);
}

static int cmp_double(const void *pa, const void *pb)
{
    double a = *(const double *)pa;
    double b = *(const double *)pb;
    return a < b ? -1 : (a > b ? 1 : 0);
}

static double percentile(const double *sorted, size_t n, double p)
{
    double rank = p * (double)(n - 1u);
    size_t lo = (size_t)rank;
    size_t hi = lo + 1u < n ? lo + 1u : lo;
    double frac = rank - (double)lo;
    return sorted[lo] + (sorted[hi] - sorted[lo]) * frac;
}

/* Parse the options up to "--"; returns the index of <exe>, or -1. */
static int parse_opts(int argc, WCHAR **argv, int i, struct opts *o)
{
    for (; i < argc; i++) {
        const WCHAR *a = argv[i];
        int has_val = i + 1 < argc;
        if (wcscmp(a, L"--") == 0) {
            return i + 1 < argc ? i + 1 : -1;
        }
        if (!has_val) {
            return -1;
        }
        if (wcscmp(a, L"--cwd") == 0) {
            o->cwd = argv[++i];
        } else if (wcscmp(a, L"--stdin") == 0) {
            o->stdin_file = argv[++i];
        } else if (wcscmp(a, L"--env") == 0 && o->nset < MAX_ENV_OPS && wcschr(argv[i + 1] + 1, L'=') != NULL) {
            o->set[o->nset++] = argv[++i];
        } else if (wcscmp(a, L"--unset") == 0 && o->nunset < MAX_ENV_OPS) {
            o->unset[o->nunset++] = argv[++i];
        } else if (wcscmp(a, L"--kill-after") == 0) {
            o->kill_after = (DWORD)wcstoul(argv[++i], NULL, 10);
        } else if (wcscmp(a, L"--timeout") == 0) {
            o->timeout = (DWORD)wcstoul(argv[++i], NULL, 10);
        } else if (wcscmp(a, L"--max-capture") == 0) {
            o->max_capture = (size_t)wcstoull(argv[++i], NULL, 10);
        } else {
            return -1;
        }
    }
    return -1;
}

int wmain(int argc, WCHAR **argv);

int wmain(int argc, WCHAR **argv)
{
    static WCHAR cmdline[CMD_CAP];
    struct opts o;
    int bench;
    unsigned long n = 1;
    int first;
    int exe_i;
    WCHAR *exe;
    WCHAR *cwd = NULL;
    WCHAR *envblk;
    const char *what = "";
    DWORD gle = 0;

    memset(&o, 0, sizeof(o));
    o.timeout = 600000u;
    o.max_capture = 16u * 1024u * 1024u;
    if (argc < 3) {
        usage();
        return 2;
    }
    bench = wcscmp(argv[1], L"bench") == 0;
    if (!bench && wcscmp(argv[1], L"run") != 0) {
        usage();
        return 2;
    }
    first = 2;
    if (bench) {
        n = wcstoul(argv[2], NULL, 10);
        if (n == 0 || n > 1000000ul) {
            usage();
            return 2;
        }
        first = 3;
    }
    exe_i = parse_opts(argc, argv, first, &o);
    if (exe_i < 0) {
        usage();
        return 2;
    }
    exe = to_dos(argv[exe_i]);
    if (exe == NULL || append_quoted(cmdline, CMD_CAP, exe) != 0) {
        return 2;
    }
    free(exe);
    for (int i = exe_i + 1; i < argc; i++) {
        if (append_quoted(cmdline, CMD_CAP, argv[i]) != 0) {
            fputws(L"fl-testdriver: command line too long\n", stderr);
            return 2;
        }
    }
    if (o.cwd != NULL) {
        cwd = to_dos(o.cwd);
    }
    envblk = build_env(&o);
    if (envblk == NULL) {
        fail_json("cannot build the environment block", GetLastError());
        return 3;
    }

    if (!bench) {
        struct result r;
        if (run_once(cmdline, cwd, envblk, &o, 1, &r, &what, &gle) != 0) {
            fail_json(what, gle);
            return 3;
        }
        put_fmt("{\"rc\":%lu,\"ms\":%.3f,\"pid\":%lu,\"killed\":%s,\"timeout\":%s,\"pipes_hung\":%s,\"oom\":%s",
                (unsigned long)r.rc, r.ms, (unsigned long)r.pid, r.killed ? "true" : "false",
                r.timed_out ? "true" : "false", r.pipes_hung ? "true" : "false",
                (r.out.oom || r.err.oom) ? "true" : "false");
        put_stream("stdout", &r.out, o.max_capture);
        put_stream("stderr", &r.err, o.max_capture);
        put_str("}\n");
        free(r.out.buf);
        free(r.err.buf);
    } else {
        double *ms = calloc(n, sizeof(double));
        unsigned long ok = 0;
        unsigned long failures = 0;
        double sum = 0.0;
        unsigned long first_bad_rc = 0;
        if (ms == NULL) {
            fail_json("out of memory", 0);
            return 3;
        }
        o.kill_after = 0;
        for (unsigned long i = 0; i < n; i++) {
            struct result r;
            if (run_once(cmdline, cwd, envblk, &o, 0, &r, &what, &gle) != 0) {
                put_fmt("{\"error\":\"%s\",\"gle\":%lu,\"iteration\":%lu}\n", what, (unsigned long)gle, i);
                free(ms);
                return 3;
            }
            if (r.rc != 0 || r.timed_out || r.pipes_hung) {
                if (failures == 0) {
                    first_bad_rc = r.rc;
                }
                failures++;
            } else {
                ok++;
            }
            ms[i] = r.ms;
            sum += r.ms;
        }
        qsort(ms, n, sizeof(double), cmp_double);
        put_fmt("{\"n\":%lu,\"ok\":%lu,\"failures\":%lu,\"first_bad_rc\":%lu,\"p50_ms\":%.3f,\"p95_ms\":%.3f,"
                "\"min_ms\":%.3f,\"max_ms\":%.3f,\"mean_ms\":%.3f}\n",
                n, ok, failures, first_bad_rc, percentile(ms, n, 0.50), percentile(ms, n, 0.95), ms[0],
                ms[n - 1u], sum / (double)n);
        free(ms);
    }
    free(envblk);
    free(cwd);
    return 0;
}
