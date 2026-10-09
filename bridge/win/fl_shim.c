/*
 * fl_shim.c - fl-shim.exe: the git.exe / bash.exe / sh.exe personas of the
 * EXPERIMENTAL native-git bridge of Fork for Linux (unofficial).
 *
 * Installed (as copies) at C:\fork-linux\gitInstance\{cmd,bin,mingw64\bin}\git.exe,
 * bin\{bash,sh}.exe and usr\bin\{bash,sh}.exe; Fork uses them when the launcher
 * exports FORKGITINSTANCE=C:\fork-linux\gitInstance. The persona is the basename
 * of our own module (GetModuleFileNameW).
 *
 * Modes (FL_BRIDGE_MODE):
 *   bridge (default)  translate argv / environment / cwd to Unix form and run
 *                     /usr/bin/git (or /bin/bash, /bin/sh) through the launcher-
 *                     started daemon on 127.0.0.1:FL_BRIDGE_PORT, with mutual
 *                     HMAC authentication keyed by FL_BRIDGE_TOKEN; stdout is
 *                     translated back to Windows paths for the commands that
 *                     print paths.
 *   record            forward to Fork's bundled git (FL_BRIDGE_BUNDLED_GIT, the
 *                     Windows path of its gitInstance cmd\git.exe) with our std
 *                     handles, and log every call; builds the corpus of what Fork
 *                     really runs.
 * FL_BRIDGE_LOG (Windows or Unix path): append one JSON line per call (both modes).
 *
 * Exit status: the child's; 128 + signal; 127 when it could not be started;
 * 141 when Fork closed our stdout; 125 for bridge failures (missing environment,
 * daemon unreachable, authentication failure, protocol error).
 */
#include "fl_win.h"

#include <stdlib.h>
#include <string.h>

#include "fl_proto.h"
#include "fl_translate.h"

/* MinGW-w64 CRT: never glob the command line (pathspecs such as '*.c' reach git as is). */
int _dowildcard = 0;

enum persona { P_GIT, P_BASH, P_SH };

static const char *const persona_name[] = { "git", "bash", "sh" };
static const char *const persona_unix[] = { "git", "/bin/bash", "/bin/sh" };
static const char *const persona_prog[] = { "fl-shim(git)", "fl-shim(bash)", "fl-shim(sh)" };

/* Everything the exit hook needs to write the JSON log line. */
struct call {
    enum persona persona;
    const char *mode;
    const char *log;
    ULONGLONG t0;
    int argc;
    char **argv;          /* original, UTF-8, argv[0] included */
    char *exe;            /* our module path */
    char *cwd;            /* Windows cwd */
    char *target;         /* record: bundled program */
    char *unix_cwd;       /* bridge */
    size_t xargc;         /* bridge: argv sent to the daemon */
    char **xargv;
    const char *subcmd;
    int out_plan;
    uint32_t pid;
};

static struct call g_call;

/* ------------------------------------------------------------------------ */
/* logging                                                                   */
/* ------------------------------------------------------------------------ */

static int env_selected(const char *name)
{
    static const char *const exact[] = { "FORK_PROCESS_ID", "LANG" };
    static const char *const prefix[] = { "GIT_", "SSH_", "LC_" };
    for (size_t i = 0; i < sizeof(exact) / sizeof(exact[0]); i++) {
        if (_stricmp(name, exact[i]) == 0) {
            return 1;
        }
    }
    for (size_t i = 0; i < sizeof(prefix) / sizeof(prefix[0]); i++) {
        if (_strnicmp(name, prefix[i], strlen(prefix[i])) == 0) {
            return 1;
        }
    }
    return 0;
}

/* Append the "K=V" environment entry p as "K":"V" when K is selected; updates *first. */
static void json_env_entry(struct flw_buf *b, const wchar_t *p, int *first)
{
    char *kv;
    char *eq;
    if (p[0] == L'=') {
        return;
    }
    kv = flw_utf8(p);
    if (kv == NULL) {
        return;
    }
    eq = strchr(kv, '=');
    if (eq != NULL) {
        *eq = '\0';
    }
    if (eq != NULL && env_selected(kv)) {
        if (!*first) {
            flw_buf_put(b, ",", 1);
        }
        *first = 0;
        flw_buf_json(b, kv);
        flw_buf_put(b, ":", 1);
        flw_buf_json_env_value(b, kv, eq + 1);
    }
    free(kv);
}

static void json_env(struct flw_buf *b)
{
    wchar_t *blk = GetEnvironmentStringsW();
    int first = 1;
    flw_buf_put(b, "{", 1);
    if (blk != NULL) {
        for (const wchar_t *p = blk; *p != L'\0'; p += wcslen(p) + 1) {
            json_env_entry(b, p, &first);
        }
        FreeEnvironmentStringsW(blk);
    }
    flw_buf_put(b, "}", 1);
}

static void log_hook(void *ud, UINT code)
{
    struct call *c = ud;
    struct flw_buf b = { 0 };
    flw_buf_puts(&b, "{\"ts\":");
    flw_buf_timestamp(&b);
    flw_buf_puts(&b, ",\"tool\":\"fl-shim\",\"mode\":");
    flw_buf_json(&b, c->mode);
    flw_buf_puts(&b, ",\"persona\":");
    flw_buf_json(&b, persona_name[c->persona]);
    flw_buf_puts(&b, ",\"pid\":");
    flw_buf_putu(&b, GetCurrentProcessId());
    flw_buf_puts(&b, ",\"exe\":");
    flw_buf_json(&b, c->exe != NULL ? c->exe : "");
    flw_buf_puts(&b, ",\"argv\":");
    flw_buf_json_args(&b, (size_t)c->argc, c->argv);
    flw_buf_puts(&b, ",\"cwd\":");
    flw_buf_json(&b, c->cwd != NULL ? c->cwd : "");
    flw_buf_puts(&b, ",\"env\":");
    json_env(&b);
    flw_buf_puts(&b, ",\"stdio\":");
    flw_buf_json_stdio(&b);
    if (c->target != NULL) {
        flw_buf_puts(&b, ",\"target\":");
        flw_buf_json(&b, c->target);
    }
    if (c->unix_cwd != NULL) {
        flw_buf_puts(&b, ",\"unix_cwd\":");
        flw_buf_json(&b, c->unix_cwd);
    }
    if (c->xargv != NULL) {
        flw_buf_puts(&b, ",\"xargv\":");
        flw_buf_json_args(&b, c->xargc, c->xargv);
    }
    if (c->subcmd != NULL) {
        flw_buf_puts(&b, ",\"subcmd\":");
        flw_buf_json_env_value(&b, "subcmd", c->subcmd);
        flw_buf_puts(&b, ",\"out_plan\":");
        flw_buf_puti(&b, c->out_plan);
    }
    if (c->pid != 0) {
        flw_buf_puts(&b, ",\"unix_pid\":");
        flw_buf_putu(&b, c->pid);
    }
    flw_buf_puts(&b, ",\"duration_ms\":");
    flw_buf_putu(&b, GetTickCount64() - c->t0);
    flw_buf_puts(&b, ",\"exit\":");
    flw_buf_putu(&b, code);
    flw_buf_put(&b, "}", 1);
    flw_log_append(c->log, &b);
    flw_buf_free(&b);
}

/* ------------------------------------------------------------------------ */
/* record mode                                                               */
/* ------------------------------------------------------------------------ */

/* malloc'd copy of path without its last n components ("" when none are left). */
static char *strip_components(const char *path, int n)
{
    char *s = flw_strdup(path);
    if (s == NULL) {
        return NULL;
    }
    while (n-- > 0) {
        char *cut = NULL;
        for (char *p = s; *p != '\0'; p++) {
            if (*p == '\\' || *p == '/') {
                cut = p;
            }
        }
        if (cut == NULL) {
            s[0] = '\0';
            break;
        }
        *cut = '\0';
    }
    return s;
}

static int file_exists(const char *path)
{
    wchar_t *w = flw_utf16(path);
    DWORD a;
    if (w == NULL) {
        return 0;
    }
    a = GetFileAttributesW(w);
    free(w);
    return a != INVALID_FILE_ATTRIBUTES && (a & FILE_ATTRIBUTE_DIRECTORY) == 0;
}

static char *join2(const char *a, const char *b)
{
    size_t al = strlen(a);
    size_t bl = strlen(b);
    char *s = malloc(al + bl + 2);
    if (s == NULL) {
        return NULL;
    }
    memcpy(s, a, al);
    s[al] = '\\';
    memcpy(s + al + 1, b, bl + 1);
    return s;
}

/* Bundled program for the persona: cmd\git.exe itself, or <root>\{usr\bin,bin}\<name>.exe. */
static char *bundled_target(enum persona p, const char *bundled_git)
{
    char *root;
    char *t;
    if (p == P_GIT) {
        return flw_strdup(bundled_git);
    }
    root = strip_components(bundled_git, 2); /* ...\gitInstance\<ver>\cmd\git.exe -> ...\<ver> */
    if (root == NULL || root[0] == '\0') {
        free(root);
        return NULL;
    }
    t = join2(root, p == P_BASH ? "usr\\bin\\bash.exe" : "usr\\bin\\sh.exe");
    if (t != NULL && !file_exists(t)) {
        free(t);
        t = join2(root, p == P_BASH ? "bin\\bash.exe" : "bin\\sh.exe");
    }
    free(root);
    return t;
}

/* The command-line tail after argv[0], parsed with the CRT's argv[0] rule. */
static const wchar_t *cmdline_tail(const wchar_t *cl)
{
    if (*cl == L'"') {
        cl++;
        while (*cl != L'\0' && *cl != L'"') {
            cl++;
        }
        if (*cl == L'"') {
            cl++;
        }
    } else {
        while (*cl != L'\0' && *cl != L' ' && *cl != L'\t') {
            cl++;
        }
    }
    return cl;
}

static int same_file(const char *a, const char *b)
{
    char *na = flw_norm_win(a, NULL);
    char *nb = flw_norm_win(b, NULL);
    int same = na != NULL && nb != NULL && _stricmp(na, nb) == 0;
    free(na);
    free(nb);
    return same;
}

static void make_inheritable(HANDLE h)
{
    if (h != NULL && h != INVALID_HANDLE_VALUE) {
        SetHandleInformation(h, HANDLE_FLAG_INHERIT, HANDLE_FLAG_INHERIT);
    }
}

static _Noreturn void record_mode(struct call *c)
{
    char *bundled = flw_getenv(L"FL_BRIDGE_BUNDLED_GIT");
    wchar_t *wtarget;
    const wchar_t *tail;
    wchar_t *cmd;
    size_t tl;
    size_t cl;
    STARTUPINFOW si;
    PROCESS_INFORMATION pi;
    HANDLE job;
    DWORD code = FLW_EXIT_BRIDGE;
    if (bundled == NULL || bundled[0] == '\0') {
        flw_fail("record mode needs FL_BRIDGE_BUNDLED_GIT (Windows path of Fork's gitInstance cmd\\git.exe)");
    }
    c->target = bundled_target(c->persona, bundled);
    free(bundled);
    if (c->target == NULL) {
        flw_fail_s("cannot derive the bundled ", persona_name[c->persona], " from FL_BRIDGE_BUNDLED_GIT");
    }
    if (c->exe != NULL && same_file(c->exe, c->target)) {
        flw_fail_s("FL_BRIDGE_BUNDLED_GIT points at this shim (", c->target, "): refusing to recurse");
    }
    wtarget = flw_utf16(c->target);
    if (wtarget == NULL) {
        flw_fail("out of memory");
    }
    tail = cmdline_tail(GetCommandLineW());
    tl = wcslen(wtarget);
    cl = wcslen(tail);
    if (tl + cl + 3 > 32767u) {
        flw_fail("command line too long");
    }
    cmd = flw_xmalloc((tl + cl + 3) * sizeof(wchar_t));
    cmd[0] = L'"';
    memcpy(cmd + 1, wtarget, tl * sizeof(wchar_t));
    cmd[tl + 1] = L'"';
    memcpy(cmd + tl + 2, tail, (cl + 1) * sizeof(wchar_t));

    memset(&si, 0, sizeof(si));
    si.cb = sizeof(si);
    si.dwFlags = STARTF_USESTDHANDLES;
    si.hStdInput = GetStdHandle(STD_INPUT_HANDLE);
    si.hStdOutput = GetStdHandle(STD_OUTPUT_HANDLE);
    si.hStdError = GetStdHandle(STD_ERROR_HANDLE);
    make_inheritable(si.hStdInput);
    make_inheritable(si.hStdOutput);
    make_inheritable(si.hStdError);
    memset(&pi, 0, sizeof(pi));
    /* The bundled git, its hooks and helpers never need the bridge token. */
    SetEnvironmentVariableW(L"FL_BRIDGE_TOKEN", NULL);
    /* Kill the bundled program if Fork terminates us (best effort). */
    job = CreateJobObjectW(NULL, NULL);
    if (job != NULL) {
        JOBOBJECT_EXTENDED_LIMIT_INFORMATION li;
        memset(&li, 0, sizeof(li));
        li.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        if (!SetInformationJobObject(job, JobObjectExtendedLimitInformation, &li, (DWORD)sizeof(li))) {
            CloseHandle(job);
            job = NULL;
        }
    }
    if (!CreateProcessW(wtarget, cmd, NULL, NULL, TRUE, CREATE_NO_WINDOW | CREATE_UNICODE_ENVIRONMENT | CREATE_SUSPENDED,
                        NULL, NULL, &si, &pi)) {
        DWORD e = GetLastError();
        struct flw_buf m = { 0 };
        flw_buf_puts(&m, "cannot start the bundled ");
        flw_buf_puts(&m, persona_name[c->persona]);
        flw_buf_puts(&m, " '");
        flw_buf_puts(&m, c->target);
        flw_buf_puts(&m, "' (win32 error ");
        flw_buf_putu(&m, e);
        flw_buf_puts(&m, ")");
        flw_msg_buf(&m);
        flw_exit(FLW_EXIT_SPAWN);
    }
    if (job != NULL) {
        AssignProcessToJobObject(job, pi.hProcess);
    }
    ResumeThread(pi.hThread);
    CloseHandle(pi.hThread);
    free(cmd);
    free(wtarget);
    if (WaitForSingleObject(pi.hProcess, INFINITE) != WAIT_OBJECT_0 || !GetExitCodeProcess(pi.hProcess, &code)) {
        DWORD e = GetLastError();
        struct flw_buf m = { 0 };
        flw_buf_puts(&m, "lost track of the bundled ");
        flw_buf_puts(&m, persona_name[c->persona]);
        flw_buf_puts(&m, " (win32 error ");
        flw_buf_putu(&m, e);
        flw_buf_puts(&m, ")");
        flw_fail_buf(&m);
    }
    CloseHandle(pi.hProcess);
    flw_exit(code); /* the job handle closes at exit; the child is already gone */
}

/* ------------------------------------------------------------------------ */
/* bridge mode                                                               */
/* ------------------------------------------------------------------------ */

struct anchor {
    char *win;     /* logical Windows path, "C:/a/b" form */
    char *unix_path;
    char *real;    /* realpath reported by the daemon (may be NULL) */
};

struct anchors {
    struct anchor v[8];
    size_t n;
};

static void anchor_add(struct anchors *a, const char *win_norm)
{
    char *u;
    if (win_norm == NULL || a->n >= sizeof(a->v) / sizeof(a->v[0])) {
        return;
    }
    for (size_t i = 0; i < a->n; i++) {
        if (strcmp(a->v[i].win, win_norm) == 0) {
            return;
        }
    }
    u = flw_path_to_unix(win_norm);
    if (u == NULL) {
        return;
    }
    for (size_t n = strlen(u); n > 1 && u[n - 1] == '/'; n--) {
        u[n - 1] = '\0'; /* "/pfx/dosdevices/r:/" -> "/pfx/dosdevices/r:" */
    }
    a->v[a->n].win = flw_strdup(win_norm);
    a->v[a->n].unix_path = u;
    a->v[a->n].real = NULL;
    if (a->v[a->n].win == NULL) {
        free(u);
        return;
    }
    a->n++;
}

/* -C value: follow it in *dir (relative values resolve against the previous one). */
static void chdir_anchor(struct anchors *a, char **dir, const char *val)
{
    char *nd = flw_norm_win(val, *dir);
    if (nd != NULL) {
        free(*dir);
        *dir = nd;
        anchor_add(a, nd);
    }
}

/* Global options whose value is the next argument and names no directory. */
static int takes_plain_value(const char *s)
{
    return strcmp(s, "-c") == 0 || strcmp(s, "--namespace") == 0 || strcmp(s, "--config-env") == 0;
}

/* Handle the global option argv[i]: record its anchor and return how many argv
 * entries it takes (1, or 2 when its value is the next argument). */
static int global_opt(int argc, char **argv, int i, struct anchors *a, char **dir)
{
    const char *s = argv[i];
    const char *val = NULL;
    int used = 1;
    if (strcmp(s, "-C") == 0) {
        if (i + 1 < argc && argv[i + 1][0] != '\0') {
            chdir_anchor(a, dir, argv[i + 1]);
        }
        return 2;
    }
    if (takes_plain_value(s)) {
        return 2;
    }
    if (strcmp(s, "--git-dir") == 0 || strcmp(s, "--work-tree") == 0) {
        val = i + 1 < argc ? argv[i + 1] : NULL;
        used = 2;
    } else if (strncmp(s, "--git-dir=", 10) == 0) {
        val = s + 10;
    } else if (strncmp(s, "--work-tree=", 12) == 0) {
        val = s + 12;
    }
    if (val != NULL && val[0] != '\0') {
        char *n = flw_norm_win(val, *dir);
        anchor_add(a, n);
        free(n);
    }
    return used;
}

/*
 * Walk git's global options (argv[0] excluded) the way git does: record the
 * anchors (-C, --git-dir, --work-tree) and return the index of the subcommand
 * (argc when there is none). *dir follows -C so relative values resolve right.
 */
static int scan_global(int argc, char **argv, struct anchors *a, char **dir)
{
    int i = 0;
    while (i < argc) {
        const char *s = argv[i];
        if (strcmp(s, "--") == 0) {
            return argc;
        }
        if (s[0] != '-') {
            return i;
        }
        i += global_opt(argc, argv, i, a, dir);
    }
    return argc;
}

/* 1 when the subcommand's own options ask for NUL-terminated records. */
static int wants_nul(int argc, char **argv, int sub)
{
    for (int i = sub + 1; i < argc; i++) {
        if (strcmp(argv[i], "--") == 0) {
            break;
        }
        if (strcmp(argv[i], "-z") == 0 || strcmp(argv[i], "--null") == 0) {
            return 1;
        }
    }
    return 0;
}

static int cmp_anchor_len(const void *pa, const void *pb)
{
    const struct anchor *a = pa;
    const struct anchor *b = pb;
    size_t la = strlen(a->unix_path);
    size_t lb = strlen(b->unix_path);
    if (la < lb) {
        return 1;
    }
    return la > lb ? -1 : 0;
}

static void add_anchors(struct fl_out *o, struct anchors *a)
{
    /* Longest Unix path first, physical (realpath) and logical forms both. */
    qsort(a->v, a->n, sizeof(a->v[0]), cmp_anchor_len);
    for (size_t i = 0; i < a->n; i++) {
        if (a->v[i].real != NULL && a->v[i].real[0] == '/' && strcmp(a->v[i].real, a->v[i].unix_path) != 0) {
            fl_out_add_anchor(o, a->v[i].real, a->v[i].win);
        }
        fl_out_add_anchor(o, a->v[i].unix_path, a->v[i].win);
    }
}

/*
 * "FL_BRIDGE_ANCHORS=unix=win;unix=win" for the fl-winexec persona (bridge/unix),
 * which maps the paths git hands to an editor or askpass program back to the
 * logical Windows paths Fork knows. Entries containing ';' cannot be represented
 * and are skipped. NULL when there is nothing to pass.
 */
static char *anchors_env(const struct anchors *a)
{
    struct flw_buf b = { 0 };
    int any = 0;
    flw_buf_puts(&b, "FL_BRIDGE_ANCHORS=");
    for (size_t i = 0; i < a->n; i++) {
        if (strchr(a->v[i].unix_path, ';') != NULL || strchr(a->v[i].win, ';') != NULL) {
            continue;
        }
        if (any) {
            flw_buf_put(&b, ";", 1);
        }
        any = 1;
        flw_buf_puts(&b, a->v[i].unix_path);
        flw_buf_put(&b, "=", 1);
        flw_buf_puts(&b, a->v[i].win);
    }
    if (!any || b.oom) {
        flw_buf_free(&b);
        return NULL;
    }
    return b.p;
}

/* Output translation chosen from the git subcommand. */
struct outcfg {
    enum fl_outplan plan;
    int nul;
    int stderr_paths;
};

/* GIT_DIR from the environment as an anchor (relative to dir). */
static void git_dir_anchor(struct anchors *anc, const char *dir)
{
    char *gd = flw_getenv(L"GIT_DIR");
    if (gd != NULL && gd[0] != '\0') {
        char *n = flw_norm_win(gd, dir);
        anchor_add(anc, n);
        free(n);
    }
    free(gd);
}

/* git persona: anchors from the global options and GIT_DIR, translated argv, output plan. */
static void git_args(struct call *c, const struct fl_xlate *x, struct anchors *anc, char **dir,
                     struct fl_strvec *targs, struct fl_cmdinfo *info, struct outcfg *oc)
{
    int sub = scan_global(c->argc - 1, c->argv + 1, anc, dir);
    if (fl_translate_argv(x, c->argc - 1, c->argv + 1, targs, info) != 0) {
        flw_fail("cannot translate the git arguments to Unix form");
    }
    info->subcmd[sizeof(info->subcmd) - 1] = '\0';
    if (sub < c->argc - 1) {
        oc->nul = wants_nul(c->argc - 1, c->argv + 1, sub);
    }
    oc->plan = info->out_plan;
    oc->stderr_paths = info->stderr_paths;
    c->subcmd = info->subcmd;
    c->out_plan = (int)oc->plan;
    git_dir_anchor(anc, *dir);
}

/* argv sent to the daemon: the Unix program, then the translated arguments (taken over). */
static char **unix_argv(enum persona p, const struct fl_strvec *targs)
{
    char **argv = flw_xcalloc(targs->n + 2, sizeof(char *));
    argv[0] = flw_xstrdup(persona_unix[p]);
    for (size_t i = 0; i < targs->n; i++) {
        argv[i + 1] = targs->v[i];
    }
    return argv;
}

/* The request's environment: the translated set plus FL_BRIDGE_ANCHORS (last one wins). */
static void req_env(struct fl_req *req, const struct fl_envops *ops, const struct anchors *anc)
{
    size_t nset = 0;
    char **set = flw_xcalloc(ops->set.n + 2, sizeof(char *));
    for (size_t i = 0; i < ops->set.n; i++) {
        set[nset] = ops->set.v[i];
        nset++;
    }
    set[nset] = anchors_env(anc);
    if (set[nset] != NULL) {
        nset++;
    }
    req->nset = nset;
    req->set = set;
    req->nunset = ops->unset.n;
    req->unset = ops->unset.v;
}

/* The anchors' Unix paths in the request, for the daemon to resolve. */
static void req_anchors(struct fl_req *req, const struct anchors *anc)
{
    req->anchors = flw_xcalloc(anc->n, sizeof(char *));
    for (size_t i = 0; i < anc->n; i++) {
        req->anchors[i] = anc->v[i].unix_path;
    }
    req->nanchors = anc->n;
}

/* cwd (Windows and Unix forms) of a bridge call; exits 125 when unmappable. */
static void bridge_cwd(struct call *c)
{
    if (c->cwd == NULL) {
        flw_fail("cannot read the current directory");
    }
    c->unix_cwd = flw_path_to_unix(c->cwd);
    if (c->unix_cwd == NULL) {
        flw_fail_s("cannot map the current directory '", c->cwd, "' to a Unix path");
    }
}

/* Relay with the output translation of oc; never returns. */
static _Noreturn void bridge_relay(const struct fl_xlate *x, const struct outcfg *oc, struct anchors *anc)
{
    struct fl_out o_out;
    struct fl_out o_err;
    struct flw_sink s_out;
    struct flw_sink s_err;
    fl_out_init(&o_out, x, oc->plan, oc->nul);
    if (oc->plan != FL_OUT_NONE) {
        add_anchors(&o_out, anc);
    }
    flw_sink_init(&s_out, STD_OUTPUT_HANDLE, &o_out);
    if (oc->stderr_paths) {
        fl_out_init(&o_err, x, FL_OUT_PATHS_LINES, 0);
        add_anchors(&o_err, anc);
        flw_sink_init(&s_err, STD_ERROR_HANDLE, &o_err);
    } else {
        flw_sink_init(&s_err, STD_ERROR_HANDLE, NULL);
    }
    flw_relay(&s_out, &s_err, 1);
}

static _Noreturn void bridge_mode(struct call *c)
{
    struct flw_cfg cfg;
    struct fl_xlate x;
    struct fl_strvec targs = { 0, 0, NULL };
    struct fl_cmdinfo info;
    struct fl_envops ops;
    struct anchors anc;
    struct fl_req req;
    struct outcfg oc = { FL_OUT_NONE, 0, 0 };
    char **real = NULL;
    char *dir;
    size_t nreal = 0;

    memset(&info, 0, sizeof(info));
    memset(&ops, 0, sizeof(ops));
    memset(&anc, 0, sizeof(anc));
    memset(&req, 0, sizeof(req));
    if (flw_load_cfg(&cfg, 1) != 0) {
        flw_exit(FLW_EXIT_BRIDGE);
    }
    if (!flw_have_wine()) {
        flw_fail("wine_get_unix_file_name is unavailable: the bridge only works under Wine");
    }
    flw_xlate_init(&x, &cfg);
    bridge_cwd(c);

    /* argv; anchors = logical Windows path <-> Unix path of cwd, -C, --git-dir,
     * --work-tree and GIT_DIR */
    dir = flw_norm_win(c->cwd, NULL);
    anchor_add(&anc, dir);
    if (c->persona == P_GIT) {
        git_args(c, &x, &anc, &dir, &targs, &info, &oc);
    } else if (flw_xlate_args(&x, c->argc - 1, c->argv + 1, &targs) != 0) {
        /* bash / sh personas: translate whole-argument Windows-absolute paths only */
        flw_fail_s("cannot translate the ", persona_name[c->persona], " arguments");
    }
    free(dir);
    c->xargv = unix_argv(c->persona, &targs);
    c->xargc = targs.n + 1;

    flw_translate_environ(&x, &ops);
    req.cwd = c->unix_cwd;
    req.argc = c->xargc;
    req.argv = c->xargv;
    req_env(&req, &ops, &anc);
    req.flags = 0;
    if (anc.n > 0 && (oc.plan != FL_OUT_NONE || oc.stderr_paths)) {
        req_anchors(&req, &anc);
    }

    flw_start_call(&cfg, &req);
    c->pid = flw_wait_spawn(c->xargv[0], &real, &nreal);
    for (size_t i = 0; i < nreal && i < req.nanchors; i++) {
        anc.v[i].real = real[i];
    }
    bridge_relay(&x, &oc, &anc);
}

/* ------------------------------------------------------------------------ */

static int persona_of(const char *exe, enum persona *p)
{
    const char *b = flw_basename(exe);
    if (_stricmp(b, "git.exe") == 0) {
        *p = P_GIT;
    } else if (_stricmp(b, "bash.exe") == 0) {
        *p = P_BASH;
    } else if (_stricmp(b, "sh.exe") == 0) {
        *p = P_SH;
    } else {
        return -1;
    }
    return 0;
}

int wmain(int argc, wchar_t **wargv); /* -municode entry point */

int wmain(int argc, wchar_t **wargv)
{
    struct call *c = &g_call;
    char *mode;
    memset(c, 0, sizeof(*c));
    c->t0 = GetTickCount64();
    flw_set_prog("fl-shim");
    c->exe = flw_module_path();
    if (c->exe == NULL || persona_of(c->exe, &c->persona) != 0) {
        flw_msg_s("unknown persona '", c->exe != NULL ? flw_basename(c->exe) : "?",
                  "': install this program as git.exe, bash.exe or sh.exe");
        flw_exit(FLW_EXIT_BRIDGE);
    }
    flw_set_prog(persona_prog[c->persona]);
    if (argc < 1) {
        flw_fail("empty command line");
    }
    c->argc = argc;
    c->argv = flw_args_utf8(argc, wargv);
    c->cwd = flw_cwd();
    c->log = flw_getenv(L"FL_BRIDGE_LOG");
    mode = flw_getenv(L"FL_BRIDGE_MODE");
    if (mode == NULL || mode[0] == '\0' || strcmp(mode, "bridge") == 0) {
        c->mode = "bridge";
    } else if (strcmp(mode, "record") == 0) {
        c->mode = "record";
    } else {
        flw_msg_s("FL_BRIDGE_MODE must be 'bridge' or 'record', not '", mode, "'");
        flw_exit(FLW_EXIT_BRIDGE);
    }
    free(mode);
    if (c->log != NULL && c->log[0] != '\0') {
        flw_set_exit_hook(log_hook, c);
    }
    if (strcmp(c->mode, "record") == 0) {
        record_mode(c);
    }
    bridge_mode(c);
}
