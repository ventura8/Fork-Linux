/*
 * fl_launch.c - fl-launch.exe: lets Fork (running under Wine) open host tools of
 * Fork for Linux (unofficial) through the bridge daemon and fork-linux-host.
 *
 *   fl-launch.exe <verb> [args...]
 *
 *   terminal | open | reveal   start the host tool detached and return 0 at once
 *                              (FL_REQ_HOST_HELPER | FL_REQ_DETACH)
 *   diff | merge | edit | run  wait for the host tool and return its exit status
 *                              (FL_REQ_HOST_HELPER)
 *
 * Windows-absolute arguments ("C:\x", "Z:/x", "\\?\...", "file://C:/...") are
 * translated to Unix paths; everything else is passed on unchanged. Transport and
 * authentication are those of fl-shim.exe (FL_BRIDGE_PORT, FL_BRIDGE_TOKEN,
 * FL_BRIDGE_WINEXEC); the daemon prepends its configured host-helper path to argv.
 * Nothing Linux-side is ever started through Wine's CreateProcessW (spike B2: it
 * would inherit Wine-staging's seccomp filter).
 *
 * Exit status: 2 for usage errors, otherwise as fl-shim.exe (child status,
 * 128 + signal, 127 spawn failure, 125 bridge failure).
 */
#include "fl_win.h"

#include <stdlib.h>
#include <string.h>

#include "fl_proto.h"
#include "fl_translate.h"

/* MinGW-w64 CRT: never glob the command line. */
int _dowildcard = 0;

struct verb {
    const char *name;
    uint32_t flags;
};

static const struct verb verbs[] = {
    { "terminal", FL_REQ_HOST_HELPER | FL_REQ_DETACH },
    { "open", FL_REQ_HOST_HELPER | FL_REQ_DETACH },
    { "reveal", FL_REQ_HOST_HELPER | FL_REQ_DETACH },
    { "diff", FL_REQ_HOST_HELPER },
    { "merge", FL_REQ_HOST_HELPER },
    { "edit", FL_REQ_HOST_HELPER },
    { "run", FL_REQ_HOST_HELPER },
};

struct call {
    ULONGLONG t0;
    const char *log;
    int argc;
    char **argv;
    char *cwd;
    char *unix_cwd;
    size_t xargc;
    char **xargv;
};

static struct call g_call;

static void log_hook(void *ud, UINT code)
{
    struct call *c = ud;
    struct flw_buf b = { 0 };
    flw_buf_puts(&b, "{\"ts\":");
    flw_buf_timestamp(&b);
    flw_buf_puts(&b, ",\"tool\":\"fl-launch\",\"mode\":\"launch\",\"pid\":");
    flw_buf_putu(&b, GetCurrentProcessId());
    flw_buf_puts(&b, ",\"argv\":");
    flw_buf_json_args(&b, (size_t)c->argc, c->argv);
    flw_buf_puts(&b, ",\"cwd\":");
    flw_buf_json(&b, c->cwd != NULL ? c->cwd : "");
    if (c->unix_cwd != NULL) {
        flw_buf_puts(&b, ",\"unix_cwd\":");
        flw_buf_json(&b, c->unix_cwd);
    }
    if (c->xargv != NULL) {
        flw_buf_puts(&b, ",\"xargv\":");
        flw_buf_json_args(&b, c->xargc, c->xargv);
    }
    flw_buf_puts(&b, ",\"stdio\":");
    flw_buf_json_stdio(&b);
    flw_buf_puts(&b, ",\"duration_ms\":");
    flw_buf_putu(&b, GetTickCount64() - c->t0);
    flw_buf_puts(&b, ",\"exit\":");
    flw_buf_putu(&b, code);
    flw_buf_put(&b, "}", 1);
    flw_log_append(c->log, &b);
    flw_buf_free(&b);
}

static void usage(void)
{
    flw_msg("usage: fl-launch.exe terminal|open|reveal|diff|merge|edit|run [args...]");
}

int wmain(int argc, wchar_t **wargv); /* -municode entry point */

/* The verb named argv[1]; prints the usage and exits 2 when there is none. */
static const struct verb *find_verb(const struct call *c)
{
    if (c->argc < 2) {
        usage();
        flw_exit(FLW_EXIT_USAGE);
    }
    for (size_t i = 0; i < sizeof(verbs) / sizeof(verbs[0]); i++) {
        if (strcmp(c->argv[1], verbs[i].name) == 0) {
            return &verbs[i];
        }
    }
    flw_msg_s("unknown verb '", c->argv[1], "'");
    usage();
    flw_exit(FLW_EXIT_USAGE);
}

/* The cwd for the host tool: it does not depend on it, so an unmappable one becomes "/". */
static char *launch_cwd(const char *cwd)
{
    char *u = cwd != NULL ? flw_path_to_unix(cwd) : NULL;
    if (u == NULL) {
        u = flw_xstrdup("/");
    }
    return u;
}

/* argv for the daemon: the verb, then the arguments with Windows-absolute paths translated. */
static void launch_args(struct call *c, const struct fl_xlate *x)
{
    struct fl_strvec t = { 0, 0, NULL };
    if (flw_xlate_args(x, c->argc - 2, c->argv + 2, &t) != 0) {
        flw_fail("out of memory");
    }
    c->xargc = t.n + 1;
    c->xargv = flw_xcalloc(c->xargc + 1, sizeof(char *));
    c->xargv[0] = flw_xstrdup(c->argv[1]);
    for (size_t i = 0; i < t.n; i++) {
        c->xargv[i + 1] = t.v[i];
    }
    free(t.v);
}

int wmain(int argc, wchar_t **wargv)
{
    struct call *c = &g_call;
    const struct verb *v;
    struct flw_cfg cfg;
    struct fl_xlate x;
    struct fl_envops ops;
    struct fl_req req;
    struct flw_sink s_out;
    struct flw_sink s_err;
    char **real = NULL;
    size_t nreal = 0;

    memset(c, 0, sizeof(*c));
    memset(&ops, 0, sizeof(ops));
    memset(&req, 0, sizeof(req));
    c->t0 = GetTickCount64();
    flw_set_prog("fl-launch");
    c->argc = argc;
    c->argv = flw_args_utf8(argc, wargv);
    c->cwd = flw_cwd();
    c->log = flw_getenv(L"FL_BRIDGE_LOG");
    if (c->log != NULL && c->log[0] != '\0') {
        flw_set_exit_hook(log_hook, c);
    }
    v = find_verb(c);
    if (flw_load_cfg(&cfg, 0) != 0) {
        flw_exit(FLW_EXIT_BRIDGE);
    }
    if (!flw_have_wine()) {
        flw_fail("wine_get_unix_file_name is unavailable: fl-launch only works under Wine");
    }
    flw_xlate_init(&x, &cfg);
    c->unix_cwd = launch_cwd(c->cwd);
    launch_args(c, &x);
    flw_translate_environ(&x, &ops);

    req.cwd = c->unix_cwd;
    req.argc = c->xargc;
    req.argv = c->xargv;
    req.nset = ops.set.n;
    req.set = ops.set.v;
    req.nunset = ops.unset.n;
    req.unset = ops.unset.v;
    req.flags = v->flags;

    flw_start_call(&cfg, &req);
    (void)flw_wait_spawn(v->name, &real, &nreal);
    flw_sink_init(&s_out, STD_OUTPUT_HANDLE, NULL);
    flw_sink_init(&s_err, STD_ERROR_HANDLE, NULL);
    flw_relay(&s_out, &s_err, (v->flags & FL_REQ_DETACH) == 0);
}
