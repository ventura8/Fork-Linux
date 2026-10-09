/*
 * rv.c - B2 spike: loopback-TCP rendezvous, Windows side (MinGW-w64).
 *
 *   usage: rv.exe <program> [args...]
 *
 * Daemon mode (FL_RV_DAEMON_PORT and FL_BRIDGE_TOKEN set, i.e. inherited from the
 * launcher through Fork's environment): connect to 127.0.0.1:FL_RV_DAEMON_PORT,
 * send HELLO(token) and continue at step 4. No Linux process is spawned by Wine.
 *
 * Per-call mode (default): runs <program> through rv-helper (next to rv.exe, or
 * the DOS path in FL_RV_HELPER):
 *   1. listen on 127.0.0.1:0;
 *   2. CreateProcessW the helper with a fresh 256-bit token in an explicit
 *      environment block (never argv) and "--port N" on its command line;
 *      Wine 11 drops that block for Linux children, so the token is also
 *      written to a file in $XDG_RUNTIME_DIR and "--token-file PATH" is added;
 *   3. accept connections until one presents the token (constant-time compare);
 *   4. send REQ(cwd, argv), wait for SPAWN_OK/SPAWN_ERR;
 *   5. relay: a thread pumps our stdin handle into STDIN frames (then STDIN_EOF),
 *      the main thread writes STDOUT/STDERR frames to our handles until EXIT.
 * Exit status: the program's; 127 when it could not be started; 141 when our
 * stdout reader went away; 125 for bridge failures.
 */
#define WIN32_LEAN_AND_MEAN
#include <winsock2.h>
#include <ws2tcpip.h>
#include <windows.h>
#include <bcrypt.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <wchar.h>

#include "rvproto.h"

int _dowildcard = 0;

typedef char *(*unix_name_fn)(const WCHAR *);
typedef WCHAR *(*dos_name_fn)(const char *);

static SOCKET sock = INVALID_SOCKET;
static WCHAR *tokfile_dos; /* per-call token file, removed on every exit path */
static unsigned char inbuf[RV_HDR + RV_CHUNK];
static unsigned char rxbuf[RV_CHUNK];

/* Print a message on our stderr handle (works for GUI-subsystem builds too). */
static void err_msg(const char *fmt, ...)
{
    char buf[1024];
    va_list ap;
    DWORD w;
    int n;
    va_start(ap, fmt);
    n = vsnprintf(buf, sizeof(buf), fmt, ap);
    va_end(ap);
    if (n < 0) {
        return;
    }
    if ((size_t)n >= sizeof(buf)) {
        n = (int)sizeof(buf) - 1;
    }
    WriteFile(GetStdHandle(STD_ERROR_HANDLE), buf, (DWORD)n, &w, NULL);
}

/* Report a bridge failure and exit with 125. */
static void fail(const char *what)
{
    err_msg("rv: %s (win32 %lu, wsa %d)\n", what, GetLastError(), WSAGetLastError());
    if (tokfile_dos) {
        DeleteFileW(tokfile_dos);
    }
    ExitProcess(125);
}

/* Send exactly len bytes; 0 on success. */
static int send_all(SOCKET s, const void *buf, size_t len)
{
    const char *p = buf;
    while (len) {
        int n = send(s, p, len > 0x40000000u ? 0x40000000 : (int)len, 0);
        if (n <= 0) {
            return -1;
        }
        p += n;
        len -= (size_t)n;
    }
    return 0;
}

/* Receive exactly len bytes; 0 on success, -1 on EOF/error. */
static int recv_all(SOCKET s, void *buf, size_t len)
{
    char *p = buf;
    while (len) {
        int n = recv(s, p, len > 0x40000000u ? 0x40000000 : (int)len, 0);
        if (n <= 0) {
            return -1;
        }
        p += n;
        len -= (size_t)n;
    }
    return 0;
}

/* Write exactly len bytes to a handle; 0 on success. */
static int write_all(HANDLE h, const unsigned char *buf, DWORD len)
{
    while (len) {
        DWORD w = 0;
        if (!WriteFile(h, buf, len, &w, NULL)) {
            return -1;
        }
        buf += w;
        len -= w;
    }
    return 0;
}

/* Convert UTF-16 to malloc'd UTF-8. */
static char *to_utf8(const WCHAR *w)
{
    int n = WideCharToMultiByte(CP_UTF8, 0, w, -1, NULL, 0, NULL, NULL);
    char *s = malloc((size_t)n);
    if (!s) {
        fail("out of memory");
    }
    WideCharToMultiByte(CP_UTF8, 0, w, -1, s, n, NULL, NULL);
    return s;
}

/* Current directory as a unix path via wine_get_unix_file_name ("" if unmappable). */
static char *unix_cwd(void)
{
    WCHAR dir[MAX_PATH * 4];
    HMODULE k32 = GetModuleHandleW(L"kernel32.dll");
    unix_name_fn fn = NULL;
    char *r;
    if (!GetCurrentDirectoryW(MAX_PATH * 4, dir)) {
        return _strdup("");
    }
    if (k32) {
        fn = (unix_name_fn)(void (*)(void))GetProcAddress(k32, "wine_get_unix_file_name");
    }
    r = fn ? fn(dir) : NULL;
    return r ? r : _strdup("");
}

/* Environment block = ours minus any FL_BRIDGE_TOKEN, plus FL_BRIDGE_TOKEN=<token>. */
static WCHAR *build_env(const char *token)
{
    WCHAR *cur = GetEnvironmentStringsW();
    size_t total = RV_TOKEN_HEX + 32;
    WCHAR *blk;
    WCHAR *out;
    for (const WCHAR *p = cur; *p; p += wcslen(p) + 1) {
        total += wcslen(p) + 1;
    }
    blk = calloc(total + 2, sizeof(WCHAR));
    if (!blk) {
        fail("out of memory");
    }
    out = blk;
    for (const WCHAR *p = cur; *p; p += wcslen(p) + 1) {
        if (_wcsnicmp(p, L"FL_BRIDGE_TOKEN=", 16) == 0) {
            continue;
        }
        wcscpy(out, p);
        out += wcslen(p) + 1;
    }
    swprintf(out, total - (size_t)(out - blk), L"FL_BRIDGE_TOKEN=%hs", token);
    out += wcslen(out) + 1;
    *out = L'\0';
    FreeEnvironmentStringsW(cur);
    return blk;
}

/*
 * Wine 11 ignores lpEnvironment for Linux children (they get the Wine process's
 * own unix environment), so the token also goes into a file in $XDG_RUNTIME_DIR
 * (0700, per user; WINE_HOST_XDG_RUNTIME_DIR on Wine 11); only its path is put
 * on the command line. Returns the DOS
 * path (to delete later) and writes the unix path to unix_out; NULL if unavailable.
 */
static WCHAR *write_token_file(const char *token, char *unix_out, size_t cap)
{
    WCHAR xdg[MAX_PATH];
    char xdg8[MAX_PATH * 3];
    HMODULE k32 = GetModuleHandleW(L"kernel32.dll");
    dos_name_fn fn = NULL;
    unsigned char rnd[8];
    WCHAR *dos;
    HANDLE h;
    DWORD w;
    int r;
    /* Wine 11 exposes host-specific variables as WINE_HOST_<NAME>. */
    DWORD n = GetEnvironmentVariableW(L"XDG_RUNTIME_DIR", xdg, MAX_PATH);
    int debug = GetEnvironmentVariableW(L"FL_RV_DEBUG", NULL, 0) != 0;
    if (n == 0 || n >= MAX_PATH) {
        n = GetEnvironmentVariableW(L"WINE_HOST_XDG_RUNTIME_DIR", xdg, MAX_PATH);
    }
    if (n == 0 || n >= MAX_PATH || !k32 || BCryptGenRandom(NULL, rnd, sizeof(rnd), BCRYPT_USE_SYSTEM_PREFERRED_RNG)) {
        if (debug) {
            err_msg("rv: no token file (XDG_RUNTIME_DIR length %lu)\n", n);
        }
        return NULL;
    }
    fn = (dos_name_fn)(void (*)(void))GetProcAddress(k32, "wine_get_dos_file_name");
    WideCharToMultiByte(CP_UTF8, 0, xdg, -1, xdg8, (int)sizeof(xdg8), NULL, NULL);
    r = snprintf(unix_out, cap, "%s/fl-rv-%lu-%02x%02x%02x%02x%02x%02x%02x%02x.tok", xdg8, GetCurrentProcessId(),
                 rnd[0], rnd[1], rnd[2], rnd[3], rnd[4], rnd[5], rnd[6], rnd[7]);
    if (r < 0 || (size_t)r >= cap) {
        return NULL;
    }
    dos = fn ? fn(unix_out) : NULL;
    if (!dos) {
        if (debug) {
            err_msg("rv: no token file (cannot map %s)\n", unix_out);
        }
        return NULL;
    }
    h = CreateFileW(dos, GENERIC_WRITE, 0, NULL, CREATE_NEW, FILE_ATTRIBUTE_NORMAL, NULL);
    if (h == INVALID_HANDLE_VALUE) {
        if (debug) {
            err_msg("rv: no token file (CreateFileW %ls: %lu)\n", dos, GetLastError());
        }
        return NULL;
    }
    if (!WriteFile(h, token, RV_TOKEN_HEX, &w, NULL) || w != RV_TOKEN_HEX) {
        CloseHandle(h);
        DeleteFileW(dos);
        return NULL;
    }
    CloseHandle(h);
    return dos;
}

static HANDLE quit_ev;       /* set by the main thread when it is about to exit */
static HANDLE stdin_th;      /* the stdin pump thread */

/*
 * Thread: our stdin handle -> STDIN frames, then STDIN_EOF. It then parks on
 * quit_ev and only returns once the main thread asks it to (see finish_exit):
 * a thread that exits on its own while ExitProcess runs can turn the process
 * exit code into 0 under Wine, and closing the socket while this thread is
 * still inside send() produced stray "wine client error" lines on stderr.
 */
static DWORD WINAPI stdin_pump(LPVOID arg)
{
    HANDLE h = GetStdHandle(STD_INPUT_HANDLE);
    int ok = 1;
    (void)arg;
    if (h && h != INVALID_HANDLE_VALUE) {
        for (;;) {
            DWORD n = 0;
            if (!ReadFile(h, inbuf + RV_HDR, RV_CHUNK, &n, NULL) || n == 0) {
                break;
            }
            rv_put_hdr(inbuf, RV_STDIN, n);
            if (send_all(sock, inbuf, RV_HDR + n)) {
                ok = 0;
                break;
            }
        }
    }
    if (ok) {
        rv_put_hdr(inbuf, RV_STDIN_EOF, 0);
        send_all(sock, inbuf, RV_HDR);
    }
    WaitForSingleObject(quit_ev, INFINITE);
    return 0;
}

/* Stop the stdin pump (cancel a blocked ReadFile/send), join it, then exit. */
static void finish_exit(UINT code)
{
    SetEvent(quit_ev);
    CancelSynchronousIo(stdin_th);
    WaitForSingleObject(stdin_th, 500);
    ExitProcess(code); /* the socket is closed by process teardown, after the join */
}

/* Accept connections until one presents the token or the deadline passes. */
static SOCKET accept_helper(SOCKET lst, const char *token)
{
    WCHAR wms[16];
    ULONGLONG limit = 10000;
    ULONGLONG deadline;
    if (GetEnvironmentVariableW(L"FL_RV_ACCEPT_TIMEOUT_MS", wms, 16) && _wtoi(wms) > 0) {
        limit = (ULONGLONG)_wtoi(wms); /* spike convenience: fail fast in run.sh preflights */
    }
    deadline = GetTickCount64() + limit;
    for (;;) {
        ULONGLONG now = GetTickCount64();
        fd_set rs;
        struct timeval tv;
        unsigned char hdr[RV_HDR];
        unsigned char hello[RV_TOKEN_HEX];
        DWORD tmo = 3000;
        BOOL one = TRUE;
        SOCKET s;
        if (now >= deadline) {
            fail("helper did not connect in time");
        }
        tv.tv_sec = (long)((deadline - now) / 1000);
        tv.tv_usec = (long)((deadline - now) % 1000) * 1000;
        FD_ZERO(&rs);
        FD_SET(lst, &rs);
        if (select(0, &rs, NULL, NULL, &tv) <= 0) {
            continue;
        }
        s = accept(lst, NULL, NULL);
        if (s == INVALID_SOCKET) {
            continue;
        }
        setsockopt(s, IPPROTO_TCP, TCP_NODELAY, (const char *)&one, sizeof(one));
        setsockopt(s, SOL_SOCKET, SO_RCVTIMEO, (const char *)&tmo, sizeof(tmo));
        if (recv_all(s, hdr, RV_HDR) == 0 && hdr[0] == RV_HELLO && rv_get_len(hdr) == RV_TOKEN_HEX &&
            recv_all(s, hello, RV_TOKEN_HEX) == 0 &&
            rv_ct_equal(hello, (const unsigned char *)token, RV_TOKEN_HEX)) {
            tmo = 0;
            setsockopt(s, SOL_SOCKET, SO_RCVTIMEO, (const char *)&tmo, sizeof(tmo));
            return s;
        }
        err_msg("rv: rejected a connection without the token\n");
        closesocket(s);
    }
}

/* Daemon mode: connect to the launcher-started rv-helper and authenticate. */
static SOCKET connect_daemon(void)
{
    WCHAR wport[16];
    WCHAR wtok[RV_TOKEN_HEX + 2];
    char token[RV_TOKEN_HEX + 1];
    unsigned char hello[RV_HDR + RV_TOKEN_HEX];
    struct sockaddr_in a;
    BOOL one = TRUE;
    SOCKET s;
    int port;
    if (!GetEnvironmentVariableW(L"FL_RV_DAEMON_PORT", wport, 16) ||
        GetEnvironmentVariableW(L"FL_BRIDGE_TOKEN", wtok, RV_TOKEN_HEX + 2) != RV_TOKEN_HEX) {
        return INVALID_SOCKET;
    }
    port = _wtoi(wport);
    if (port <= 0 || port > 65535) {
        return INVALID_SOCKET;
    }
    WideCharToMultiByte(CP_UTF8, 0, wtok, -1, token, sizeof(token), NULL, NULL);
    s = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (s == INVALID_SOCKET) {
        fail("socket");
    }
    SetHandleInformation((HANDLE)s, HANDLE_FLAG_INHERIT, 0);
    memset(&a, 0, sizeof(a));
    a.sin_family = AF_INET;
    a.sin_port = htons((u_short)port);
    a.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    if (connect(s, (struct sockaddr *)&a, sizeof(a))) {
        fail("connect to the bridge daemon");
    }
    setsockopt(s, IPPROTO_TCP, TCP_NODELAY, (const char *)&one, sizeof(one));
    rv_put_hdr(hello, RV_HELLO, RV_TOKEN_HEX);
    memcpy(hello + RV_HDR, token, RV_TOKEN_HEX);
    if (send_all(s, hello, sizeof(hello))) {
        fail("send HELLO");
    }
    SecureZeroMemory(token, sizeof(token));
    SecureZeroMemory(hello, sizeof(hello));
    return s;
}

int wmain(int argc, WCHAR **argv)
{
    WSADATA wsa;
    SOCKET lst;
    struct sockaddr_in a;
    int alen = sizeof(a);
    unsigned char rnd[32];
    char token[RV_TOKEN_HEX + 1];
    WCHAR helper[MAX_PATH * 4];
    WCHAR cmd[MAX_PATH * 8 + 64];
    char tokfile[MAX_PATH * 3 + 64] = "";
    WCHAR *env;
    STARTUPINFOW si;
    PROCESS_INFORMATION pi;
    char *cwd;
    char **args;
    size_t reqlen;
    unsigned char *req;
    unsigned char *p;
    unsigned char hdr[RV_HDR];
    HANDLE hout = GetStdHandle(STD_OUTPUT_HANDLE);
    HANDLE herr = GetStdHandle(STD_ERROR_HANDLE);
    DWORD hlen;

    if (argc < 2) {
        err_msg("usage: rv.exe <program> [args...]\n");
        return 2;
    }
    if (WSAStartup(MAKEWORD(2, 2), &wsa)) {
        fail("WSAStartup");
    }
    sock = connect_daemon();
    if (sock != INVALID_SOCKET) {
        goto request;
    }

    /* 1. listener on 127.0.0.1:0 */
    lst = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (lst == INVALID_SOCKET) {
        fail("socket");
    }
    SetHandleInformation((HANDLE)lst, HANDLE_FLAG_INHERIT, 0);
    memset(&a, 0, sizeof(a));
    a.sin_family = AF_INET;
    a.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    if (bind(lst, (struct sockaddr *)&a, sizeof(a)) || listen(lst, 8) ||
        getsockname(lst, (struct sockaddr *)&a, &alen)) {
        fail("bind/listen 127.0.0.1:0");
    }

    /* 2. token + helper */
    if (BCryptGenRandom(NULL, rnd, sizeof(rnd), BCRYPT_USE_SYSTEM_PREFERRED_RNG) != 0) {
        fail("BCryptGenRandom");
    }
    for (size_t i = 0; i < sizeof(rnd); i++) {
        snprintf(token + i * 2, 3, "%02x", rnd[i]);
    }
    SecureZeroMemory(rnd, sizeof(rnd));
    env = build_env(token);
    hlen = GetEnvironmentVariableW(L"FL_RV_HELPER", helper, MAX_PATH * 4);
    if (hlen == 0 || hlen >= MAX_PATH * 4) {
        WCHAR *slash;
        hlen = GetModuleFileNameW(NULL, helper, MAX_PATH * 4);
        if (hlen == 0 || hlen >= MAX_PATH * 4 - 16) {
            fail("GetModuleFileNameW");
        }
        slash = wcsrchr(helper, L'\\');
        wcscpy(slash ? slash + 1 : helper, L"rv-helper");
    }
    tokfile_dos = write_token_file(token, tokfile, sizeof(tokfile));
    if (tokfile_dos) {
        swprintf(cmd, sizeof(cmd) / sizeof(cmd[0]), L"\"%ls\" --port %u --token-file \"%hs\"", helper,
                 (unsigned)ntohs(a.sin_port), tokfile);
    } else {
        swprintf(cmd, sizeof(cmd) / sizeof(cmd[0]), L"\"%ls\" --port %u", helper, (unsigned)ntohs(a.sin_port));
    }
    memset(&si, 0, sizeof(si));
    si.cb = sizeof(si);
    memset(&pi, 0, sizeof(pi));
    if (!CreateProcessW(NULL, cmd, NULL, NULL, FALSE, DETACHED_PROCESS | CREATE_UNICODE_ENVIRONMENT, env, NULL,
                        &si, &pi)) {
        fail("CreateProcessW(rv-helper)");
    }
    if (pi.hProcess) {
        CloseHandle(pi.hProcess);
    }
    if (pi.hThread) {
        CloseHandle(pi.hThread);
    }
    SecureZeroMemory(env, wcslen(env) * sizeof(WCHAR));
    free(env);

    /* 3. authenticated connection */
    sock = accept_helper(lst, token);
    closesocket(lst);
    if (tokfile_dos) {
        DeleteFileW(tokfile_dos); /* normally already unlinked by the helper */
        tokfile_dos = NULL;
    }
    SecureZeroMemory(token, sizeof(token));

    /* 4. request: cwd \0 argv[1] \0 argv[2] \0 ... */
request:
    cwd = unix_cwd();
    args = calloc((size_t)argc, sizeof(char *));
    if (!args) {
        fail("out of memory");
    }
    reqlen = strlen(cwd) + 1;
    for (int i = 1; i < argc; i++) {
        args[i] = to_utf8(argv[i]);
        reqlen += strlen(args[i]) + 1;
    }
    if (reqlen > RV_MAX_REQ) {
        fail("request too large");
    }
    req = malloc(RV_HDR + reqlen);
    if (!req) {
        fail("out of memory");
    }
    rv_put_hdr(req, RV_REQ, (uint32_t)reqlen);
    p = req + RV_HDR;
    memcpy(p, cwd, strlen(cwd) + 1);
    p += strlen(cwd) + 1;
    for (int i = 1; i < argc; i++) {
        memcpy(p, args[i], strlen(args[i]) + 1);
        p += strlen(args[i]) + 1;
    }
    if (send_all(sock, req, RV_HDR + reqlen)) {
        fail("send REQ");
    }
    if (recv_all(sock, hdr, RV_HDR)) {
        fail("helper closed before SPAWN_OK");
    }
    if (hdr[0] == RV_SPAWN_ERR && rv_get_len(hdr) == 4) {
        unsigned char e[4];
        if (recv_all(sock, e, 4) == 0) {
            err_msg("rv: cannot run '%s': errno %d\n", args[1], (int)rv_get_i32(e));
        }
        ExitProcess(127);
    }
    if (hdr[0] != RV_SPAWN_OK || rv_get_len(hdr) != 0) {
        fail("protocol error (expected SPAWN_OK)");
    }

    /* 5. relay */
    quit_ev = CreateEventW(NULL, TRUE, FALSE, NULL);
    stdin_th = CreateThread(NULL, 0, stdin_pump, NULL, 0, NULL);
    if (!quit_ev || !stdin_th) {
        fail("CreateThread");
    }
    for (;;) {
        uint32_t len;
        if (recv_all(sock, hdr, RV_HDR)) {
            fail("helper connection lost before EXIT");
        }
        len = rv_get_len(hdr);
        if (len > RV_CHUNK || recv_all(sock, rxbuf, len)) {
            fail("bad frame");
        }
        if (hdr[0] == RV_STDOUT) {
            if (write_all(hout, rxbuf, len)) {
                finish_exit(141);
            }
        } else if (hdr[0] == RV_STDERR) {
            write_all(herr, rxbuf, len);
        } else if (hdr[0] == RV_EXIT && len == 4) {
            finish_exit((UINT)rv_get_i32(rxbuf));
        } else {
            fail("unexpected frame");
        }
    }
}
