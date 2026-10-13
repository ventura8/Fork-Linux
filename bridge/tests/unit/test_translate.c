/*
 * test_translate.c - fl_translate.c against a fake drive map (C: -> /pfx/drive_c, Z: -> /)
 * and the table-driven vectors in bridge/tests/vectors/{argv,env,out}.tsv.
 *
 * Vector syntax
 *   Fields are TAB-separated; lines that are empty or start with '#' are ignored.
 *   argv.tsv  name  args  expected-args  subcmd  plan  stderr_paths
 *   env.tsv   name  envp  expected-set   expected-unset
 *   out.tsv   name  plan  nul  anchors  input  expected
 *   Lists ("args", "envp", "anchors", ...) separate items with " | "; "<empty>" is an empty
 *   item and "(none)" an empty list; everything else is literal (backslashes, quotes, UTF-8).
 *   An anchor item is "unix=>win". "-" in the subcmd column is the empty subcommand.
 *   plan: none | paths | worktree | remote | origin.
 *   out.tsv input/expected are byte strings with the escapes \n \r \t \0 \\ \xHH.
 */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "fl_translate.h"
#include "tap.h"

#define WINEXEC "/opt/fl/bin/fl-winexec"
#define ASKPASS "/opt/fl/bin/fl-askpass"
#define SSH_ASKPASS "/opt/fl/bin/fl-ssh-askpass"

/* ------------------------------------------------------------------------ */
/* Fake drive map                                                            */
/* ------------------------------------------------------------------------ */

static char g_last_to_unix[4096];
static unsigned g_to_unix_calls;

/* out = a + b with every `from` in b replaced by `to`; -1 when it does not fit. */
static int put2(char *out, size_t outsz, const char *a, const char *b, char from, char to)
{
    size_t la = strlen(a);
    size_t lb = strlen(b);
    if (la + lb + 1u > outsz) {
        return -1;
    }
    memcpy(out, a, la);
    for (size_t i = 0; i < lb; i++) {
        out[la + i] = b[i] == from ? to : b[i];
    }
    out[la + lb] = '\0';
    return 0;
}

static int fake_to_unix(void *ud, const char *in, char *out, size_t outsz)
{
    (void)ud;
    g_to_unix_calls++;
    snprintf(g_last_to_unix, sizeof(g_last_to_unix), "%s", in);
    if (in[0] == 'C' && in[1] == ':') {
        return put2(out, outsz, "/pfx/drive_c", in + 2, '\\', '/');
    }
    if (in[0] == 'Z' && in[1] == ':') {
        return put2(out, outsz, "", in + 2, '\\', '/');
    }
    if (in[0] == 'Y' && in[1] == ':') {
        memset(out, 'a', outsz); /* misbehaving callback: no NUL terminator */
        return 0;
    }
    if (in[0] == 'X' && in[1] == ':') {
        return put2(out, outsz, "relative/path", "", '\\', '/'); /* not absolute */
    }
    return -1;
}

static int fake_to_win(void *ud, const char *in, char *out, size_t outsz)
{
    (void)ud;
    if (strncmp(in, "/nomap", 6) == 0 && (in[6] == '\0' || in[6] == '/')) {
        return -1;
    }
    if (strncmp(in, "/unixns/", 8) == 0) {
        return put2(out, outsz, "\\\\?\\unix", in, '/', '\\');
    }
    if (strncmp(in, "/verbatim/", 10) == 0) {
        return put2(out, outsz, "\\\\?\\Z:", in, '/', '\\');
    }
    if (strncmp(in, "/lower/", 7) == 0) {
        return put2(out, outsz, "z:", in, '/', '\\');
    }
    if (strncmp(in, "/pfx/drive_c", 12) == 0 && (in[12] == '\0' || in[12] == '/')) {
        return put2(out, outsz, "C:", in[12] != '\0' ? in + 12 : "/", '/', '\\');
    }
    return put2(out, outsz, "Z:", in, '/', '\\');
}

static struct fl_xlate make_x(void)
{
    struct fl_xlate x;
    memset(&x, 0, sizeof(x));
    x.to_unix = fake_to_unix;
    x.to_win = fake_to_win;
    x.winexec = WINEXEC;
    x.askpass = ASKPASS;
    x.ssh_askpass = SSH_ASKPASS;
    return x;
}

/* ------------------------------------------------------------------------ */
/* Vector parsing                                                            */
/* ------------------------------------------------------------------------ */

struct list {
    size_t n;
    size_t cap;
    char **v; /* NULL-terminated */
};

static void list_free(struct list *l)
{
    for (size_t i = 0; i < l->n; i++) {
        free(l->v[i]);
    }
    free(l->v);
    memset(l, 0, sizeof(*l));
}

static int list_push(struct list *l, const char *s, size_t n)
{
    char *c;
    if (l->n + 1u >= l->cap) {
        size_t cap = l->cap ? l->cap * 2u : 8u;
        char **nv = realloc(l->v, cap * sizeof(char *));
        if (nv == NULL) {
            return -1;
        }
        l->v = nv;
        l->cap = cap;
    }
    c = malloc(n + 1u);
    if (c == NULL) {
        return -1;
    }
    memcpy(c, s, n);
    c[n] = '\0';
    l->v[l->n++] = c;
    l->v[l->n] = NULL;
    return 0;
}

/* " | "-separated items; "<empty>" = "", "(none)" = no items. */
static int parse_list(const char *field, struct list *l)
{
    const char *p = field;
    memset(l, 0, sizeof(*l));
    l->v = calloc(8, sizeof(char *)); /* an empty list is still a NULL-terminated array */
    if (l->v == NULL) {
        return -1;
    }
    l->cap = 8;
    if (strcmp(field, "(none)") == 0) {
        return 0;
    }
    for (;;) {
        const char *sep = strstr(p, " | ");
        size_t n = sep != NULL ? (size_t)(sep - p) : strlen(p);
        int rc = (n == 7 && strncmp(p, "<empty>", 7) == 0) ? list_push(l, "", 0) : list_push(l, p, n);
        if (rc != 0) {
            return -1;
        }
        if (sep == NULL) {
            return 0;
        }
        p = sep + 3;
    }
}

/* The list in vector syntax (inverse of parse_list). */
static char *join(char **v, size_t n)
{
    size_t len = sizeof("(none)");
    char *s;
    for (size_t i = 0; i < n; i++) {
        len += strlen(v[i]) + sizeof(" | <empty>");
    }
    s = malloc(len);
    if (s == NULL) {
        return NULL;
    }
    strcpy(s, n == 0 ? "(none)" : "");
    for (size_t i = 0; i < n; i++) {
        if (i > 0) {
            strcat(s, " | ");
        }
        strcat(s, v[i][0] != '\0' ? v[i] : "<empty>");
    }
    return s;
}

/* Compare two string lists (shown joined with " | " on mismatch). */
static void check_list(char **got, size_t gn, char **want, size_t wn, const char *group, const char *name,
                       const char *what)
{
    char *g = join(got, gn);
    char *w = join(want, wn);
    tap_str_eq(g, w, "%s %s: %s", group, name, what);
    free(g);
    free(w);
}

static size_t unescape(const char *s, char *out)
{
    size_t o = 0;
    for (size_t i = 0; s[i] != '\0'; i++) {
        if (s[i] != '\\' || s[i + 1] == '\0') {
            out[o++] = s[i];
            continue;
        }
        i++;
        switch (s[i]) {
        case 'n':
            out[o++] = '\n';
            break;
        case 'r':
            out[o++] = '\r';
            break;
        case 't':
            out[o++] = '\t';
            break;
        case '0':
            out[o++] = '\0';
            break;
        case '\\':
            out[o++] = '\\';
            break;
        case 'x': {
            unsigned v = 0;
            for (int k = 0; k < 2 && s[i + 1] != '\0'; k++) {
                char c = s[++i];
                v = v * 16u + (unsigned)(c >= 'a' ? c - 'a' + 10 : (c >= 'A' ? c - 'A' + 10 : c - '0'));
            }
            out[o++] = (char)(unsigned char)v;
            break;
        }
        default:
            out[o++] = '\\';
            out[o++] = s[i];
            break;
        }
    }
    out[o] = '\0';
    return o;
}

static enum fl_outplan plan_of(const char *s, int *ok)
{
    *ok = 1;
    if (strcmp(s, "none") == 0) {
        return FL_OUT_NONE;
    }
    if (strcmp(s, "paths") == 0) {
        return FL_OUT_PATHS_LINES;
    }
    if (strcmp(s, "worktree") == 0) {
        return FL_OUT_WORKTREE;
    }
    if (strcmp(s, "remote") == 0) {
        return FL_OUT_REMOTE;
    }
    if (strcmp(s, "origin") == 0) {
        return FL_OUT_SHOW_ORIGIN;
    }
    *ok = 0;
    return FL_OUT_NONE;
}

/* Read a vectors file; returns the number of data lines handed to fn, -1 if unreadable. */
typedef void (*vector_fn)(char **f, size_t nf, unsigned line);

static int for_each_vector(const char *file, size_t nfields, vector_fn fn)
{
    char path[1024];
    FILE *fp;
    char *data;
    long size;
    int count = 0;
    unsigned line = 0;
    snprintf(path, sizeof(path), "%s/%s", tap_vectors_dir(), file);
    fp = fopen(path, "rb");
    if (fp == NULL) {
        return -1;
    }
    if (fseek(fp, 0, SEEK_END) != 0 || (size = ftell(fp)) < 0 || fseek(fp, 0, SEEK_SET) != 0) {
        fclose(fp);
        return -1;
    }
    data = malloc((size_t)size + 1u);
    if (data == NULL || fread(data, 1, (size_t)size, fp) != (size_t)size) {
        free(data);
        fclose(fp);
        return -1;
    }
    fclose(fp);
    data[size] = '\0';
    for (char *p = data; *p != '\0';) {
        char *nl = strchr(p, '\n');
        char *f[16];
        size_t nf = 0;
        line++;
        if (nl != NULL) {
            *nl = '\0';
        }
        if (p[0] != '\0' && p[0] != '#') {
            char *q = p;
            for (;;) {
                char *tab = strchr(q, '\t');
                if (nf < 16) {
                    f[nf++] = q;
                }
                if (tab == NULL) {
                    break;
                }
                *tab = '\0';
                q = tab + 1;
            }
            if (nf != nfields) {
                tap_ok(0, "%s:%u has %zu fields, want %zu", file, line, nf, nfields);
            } else {
                fn(f, nf, line);
                count++;
            }
        }
        if (nl == NULL) {
            break;
        }
        p = nl + 1;
    }
    free(data);
    return count;
}

/* ------------------------------------------------------------------------ */
/* argv / env vectors                                                        */
/* ------------------------------------------------------------------------ */

static void argv_vector(char **f, size_t nf, unsigned line)
{
    struct fl_xlate x = make_x();
    struct list in;
    struct list want;
    struct fl_strvec out;
    struct fl_cmdinfo info;
    int ok;
    enum fl_outplan plan = plan_of(f[4], &ok);
    (void)nf;
    if (parse_list(f[1], &in) != 0 || parse_list(f[2], &want) != 0 || !ok) {
        tap_ok(0, "argv.tsv:%u unparsable", line);
        return;
    }
    if (tap_ok(fl_translate_argv(&x, (int)in.n, in.v, &out, &info) == 0, "argv %s: translates", f[0])) {
        check_list(out.v, out.n, want.v, want.n, "argv", f[0], "args");
        tap_str_eq(info.subcmd, strcmp(f[3], "-") == 0 ? "" : f[3], "argv %s: subcmd", f[0]);
        tap_ok(info.out_plan == plan, "argv %s: plan %s (got %d)", f[0], f[4], (int)info.out_plan);
        tap_ok(info.stderr_paths == atoi(f[5]), "argv %s: stderr_paths %s", f[0], f[5]);
        tap_ok(out.n == 0 ? out.v == NULL : out.v[out.n] == NULL, "argv %s: out is NULL-terminated", f[0]);
    }
    fl_strvec_free(&out);
    list_free(&in);
    list_free(&want);
}

static void env_vector(char **f, size_t nf, unsigned line)
{
    struct fl_xlate x = make_x();
    struct list in;
    struct list want_set;
    struct list want_unset;
    struct fl_envops ops;
    (void)nf;
    if (parse_list(f[1], &in) != 0 || parse_list(f[2], &want_set) != 0 || parse_list(f[3], &want_unset) != 0) {
        tap_ok(0, "env.tsv:%u unparsable", line);
        return;
    }
    if (tap_ok(fl_translate_env(&x, in.v, &ops) == 0, "env %s: translates", f[0])) {
        check_list(ops.set.v, ops.set.n, want_set.v, want_set.n, "env", f[0], "set");
        check_list(ops.unset.v, ops.unset.n, want_unset.v, want_unset.n, "env", f[0], "unset");
    }
    fl_strvec_free(&ops.set);
    fl_strvec_free(&ops.unset);
    list_free(&in);
    list_free(&want_set);
    list_free(&want_unset);
}

/* ------------------------------------------------------------------------ */
/* out vectors                                                               */
/* ------------------------------------------------------------------------ */

struct sink {
    char *p;
    size_t len;
    size_t cap;
    size_t calls;
    int oom;
};

static void sink_emit(void *ud, const char *p, size_t n)
{
    struct sink *s = ud;
    s->calls++;
    if (n == 0) {
        return;
    }
    if (s->len + n > s->cap) {
        size_t cap = s->cap ? s->cap : 256u;
        char *np;
        while (cap < s->len + n) {
            cap *= 2u;
        }
        np = realloc(s->p, cap);
        if (np == NULL) {
            s->oom = 1;
            return;
        }
        s->p = np;
        s->cap = cap;
    }
    memcpy(s->p + s->len, p, n);
    s->len += n;
}

static int setup_out(struct fl_out *o, const struct fl_xlate *x, enum fl_outplan plan, int nul, const char *anchors)
{
    struct list a;
    int rc = 0;
    fl_out_init(o, x, plan, nul);
    if (parse_list(anchors, &a) != 0) {
        return -1;
    }
    for (size_t i = 0; i < a.n && rc == 0; i++) {
        char *sep = strstr(a.v[i], "=>");
        if (sep == NULL) {
            rc = -1;
            break;
        }
        *sep = '\0';
        rc = fl_out_add_anchor(o, a.v[i], sep + 2);
    }
    list_free(&a);
    return rc;
}

/* Run the stream through fl_out in chunks of `chunk` bytes (0: all at once). */
static void run_out(const struct fl_xlate *x, enum fl_outplan plan, int nul, const char *anchors, const char *in,
                    size_t n, size_t chunk, struct sink *s)
{
    struct fl_out o;
    memset(s, 0, sizeof(*s));
    if (setup_out(&o, x, plan, nul, anchors) != 0) {
        s->oom = 1;
        fl_out_free(&o);
        return;
    }
    if (chunk == 0) {
        fl_out_feed(&o, in, n, sink_emit, s);
    } else {
        for (size_t i = 0; i < n; i += chunk) {
            fl_out_feed(&o, in + i, n - i < chunk ? n - i : chunk, sink_emit, s);
        }
    }
    fl_out_flush(&o, sink_emit, s);
    fl_out_free(&o);
}

static void out_vector(char **f, size_t nf, unsigned line)
{
    static const size_t chunks[] = { 0, 1, 2, 3, 7, 64 };
    struct fl_xlate x = make_x();
    int ok;
    enum fl_outplan plan = plan_of(f[1], &ok);
    char *in = malloc(strlen(f[4]) + 1u);
    char *want = malloc(strlen(f[5]) + 1u);
    size_t in_n;
    size_t want_n;
    (void)nf;
    if (in == NULL || want == NULL || !ok) {
        tap_ok(0, "out.tsv:%u unparsable", line);
        free(in);
        free(want);
        return;
    }
    in_n = unescape(f[4], in);
    want_n = unescape(f[5], want);
    for (size_t c = 0; c < sizeof(chunks) / sizeof(chunks[0]); c++) {
        struct sink s;
        run_out(&x, plan, atoi(f[2]), f[3], in, in_n, chunks[c], &s);
        tap_mem_eq(s.p != NULL ? s.p : "", s.len, want, want_n, "out %s (chunk %zu)", f[0], chunks[c]);
        free(s.p);
    }
    free(in);
    free(want);
}

/* ------------------------------------------------------------------------ */
/* Direct API checks                                                         */
/* ------------------------------------------------------------------------ */

static void test_is_win_abs(void)
{
    static const struct {
        const char *s;
        int want;
    } t[] = {
        { "C:\\x", 1 },
        { "c:/x", 1 },
        { "C:\\", 1 },
        { "z:/", 1 },
        { "\\\\?\\C:\\x", 1 },
        { "\\\\?\\unix\\home\\u", 1 },
        { "\\\\?\\UNIX/home", 1 },
        { "file://C:/x", 1 },
        { "file:///c:/x", 1 },
        { "FILE://C:\\x", 1 },
        { "C:", 0 },
        { "C:x", 0 },
        { "C:foo\\bar", 0 },
        { "HEAD:file", 0 },
        { "a:b", 0 },
        { ":/", 0 },
        { ":(top)src", 0 },
        { "\\\\server\\share\\x", 0 },
        { "\\\\?\\UNC\\server\\share", 0 },
        { "\\\\?\\unixy\\x", 0 },
        { "/home/u", 0 },
        { "relative\\path", 0 },
        { "https://example.com/r.git", 0 },
        { "user@host:repo", 0 },
        { "file:///home/u/r", 0 },
        { "file://server/share", 0 },
        { "1:/x", 0 },
        { "", 0 },
    };
    for (size_t i = 0; i < sizeof(t) / sizeof(t[0]); i++) {
        tap_ok(fl_is_win_abs(t[i].s) == t[i].want, "is_win_abs [%s] == %d", t[i].s, t[i].want);
    }
    tap_ok(fl_is_win_abs(NULL) == 0, "is_win_abs NULL");
}

static void test_win_to_unix(void)
{
    struct fl_xlate x = make_x();
    struct fl_xlate nocb = make_x();
    char out[64];
    char tiny[8];
    nocb.to_unix = NULL;
    tap_ok(fl_win_to_unix(&x, "c:/Users//me\\\\repo/", out, sizeof(out)) == 0, "win_to_unix mixed separators");
    tap_str_eq(g_last_to_unix, "C:\\Users\\me\\repo\\", "to_unix gets an uppercase drive and single backslashes");
    tap_str_eq(out, "/pfx/drive_c/Users/me/repo/", "win_to_unix result");
    tap_ok(fl_win_to_unix(&x, "\\\\?\\C:\\long\\p", out, sizeof(out)) == 0 && strcmp(out, "/pfx/drive_c/long/p") == 0,
           "win_to_unix strips \\\\?\\");
    g_to_unix_calls = 0;
    tap_ok(fl_win_to_unix(&x, "\\\\?\\unix\\home\\\\u/x", out, sizeof(out)) == 0 && strcmp(out, "/home/u/x") == 0 &&
               g_to_unix_calls == 0,
           "win_to_unix maps \\\\?\\unix\\ without the callback");
    tap_ok(fl_win_to_unix(&nocb, "\\\\?\\unix\\tmp", out, sizeof(out)) == 0 && strcmp(out, "/tmp") == 0,
           "win_to_unix \\\\?\\unix\\ works without a to_unix callback");
    tap_ok(fl_win_to_unix(&nocb, "C:\\x", out, sizeof(out)) == -1 && out[0] == '\0',
           "win_to_unix drive path needs the callback");
    tap_ok(fl_win_to_unix(&x, "file:///C:/src/r", out, sizeof(out)) == 0 && strcmp(out, "file:///pfx/drive_c/src/r") == 0,
           "win_to_unix file:///C:/ keeps the scheme");
    tap_ok(fl_win_to_unix(&x, "file://Z:/srv", out, sizeof(out)) == 0 && strcmp(out, "file:///srv") == 0,
           "win_to_unix file://Z:/");
    tap_ok(fl_win_to_unix(&x, "D:\\x", out, sizeof(out)) == -1 && out[0] == '\0', "win_to_unix unmapped drive");
    tap_ok(fl_win_to_unix(&x, "Y:\\x", out, sizeof(out)) == -1, "win_to_unix refuses a result without NUL");
    tap_ok(fl_win_to_unix(&x, "X:\\x", out, sizeof(out)) == -1, "win_to_unix refuses a relative result");
    tap_ok(fl_win_to_unix(&x, "relative", out, sizeof(out)) == -1, "win_to_unix refuses a relative path");
    tap_ok(fl_win_to_unix(&x, "C:\\abc", tiny, sizeof(tiny)) == -1 && tiny[0] == '\0',
           "win_to_unix refuses a result that does not fit");
    tap_ok(fl_win_to_unix(&x, "Z:\\abcdef", tiny, 8) == 0 && strcmp(tiny, "/abcdef") == 0,
           "win_to_unix exact fit (7 + NUL)");
    tap_ok(fl_win_to_unix(&x, "\\\\?\\unix\\abcdefgh", tiny, sizeof(tiny)) == -1,
           "win_to_unix unix namespace that does not fit");
    tap_ok(fl_win_to_unix(NULL, "C:\\x", out, sizeof(out)) == -1 && fl_win_to_unix(&x, NULL, out, sizeof(out)) == -1 &&
               fl_win_to_unix(&x, "C:\\x", NULL, 4) == -1 && fl_win_to_unix(&x, "C:\\x", out, 0) == -1,
           "win_to_unix NULL / empty arguments");
}

static void test_argv_errors(void)
{
    struct fl_xlate x = make_x();
    struct fl_strvec out;
    struct fl_cmdinfo info;
    char *args[] = { "status", NULL };
    tap_ok(fl_translate_argv(NULL, 1, args, &out, &info) == -1, "translate_argv NULL xlate");
    tap_ok(fl_translate_argv(&x, 2, args, &out, &info) == -1 && out.v == NULL, "translate_argv NULL element");
    tap_ok(fl_translate_argv(&x, 1, NULL, &out, &info) == -1, "translate_argv NULL argv");
    tap_ok(fl_translate_argv(&x, -1, args, &out, &info) == -1, "translate_argv negative argc");
    tap_ok(fl_translate_argv(&x, 1, args, NULL, &info) == -1 && fl_translate_argv(&x, 1, args, &out, NULL) == -1,
           "translate_argv NULL outputs");
    tap_ok(fl_translate_argv(&x, 0, NULL, &out, &info) == 0 && out.n == 0 && info.subcmd[0] == '\0' &&
               info.out_plan == FL_OUT_NONE && info.stderr_paths == 0,
           "translate_argv with no arguments");
    fl_strvec_free(&out);
    fl_strvec_free(NULL);
    tap_ok(1, "strvec_free(NULL) is harmless");
}

static void test_env_errors(void)
{
    struct fl_xlate x = make_x();
    struct fl_xlate bare;
    struct fl_envops ops;
    char *envp[] = { "GIT_ASKPASS=C:\\Fork\\Fork.RI.exe", "GIT_EDITOR=C:/Fork/Fork.RI.exe", "SSH_ASKPASS=C:\\a.exe",
                     NULL };
    tap_ok(fl_translate_env(NULL, envp, &ops) == -1, "translate_env NULL xlate");
    tap_ok(fl_translate_env(&x, envp, NULL) == -1, "translate_env NULL ops");
    tap_ok(fl_translate_env(&x, NULL, &ops) == 0 && ops.set.n == 0 && ops.unset.n == 0, "translate_env NULL envp");
    memset(&bare, 0, sizeof(bare));
    bare.to_unix = fake_to_unix;
    /* without personas, Windows askpass programs are not forwarded and editors stay as they are */
    if (tap_ok(fl_translate_env(&bare, envp, &ops) == 0, "translate_env without personas")) {
        tap_ok(ops.set.n == 1 && strcmp(ops.set.v[0], "GIT_EDITOR=C:/Fork/Fork.RI.exe") == 0,
               "without winexec the editor is kept");
        tap_ok(ops.unset.n == 2 && strcmp(ops.unset.v[0], "GIT_ASKPASS") == 0 &&
                   strcmp(ops.unset.v[1], "SSH_ASKPASS") == 0,
               "without personas Windows askpass programs are unset");
    }
    fl_strvec_free(&ops.set);
    fl_strvec_free(&ops.unset);
}

static void test_out_api(void)
{
    struct fl_xlate x = make_x();
    struct fl_out o;
    struct sink s;
    static const char raw[] = "no newline /home/u\n\0\r partial";
    fl_out_init(&o, &x, FL_OUT_PATHS_LINES, 0);
    tap_ok(fl_out_add_anchor(&o, "relative", "C:\\x") == -1, "anchor: Unix side must be absolute");
    tap_ok(fl_out_add_anchor(&o, "/x", "relative") == -1, "anchor: Windows side must have a drive");
    tap_ok(fl_out_add_anchor(&o, "/x", "C:relative") == -1, "anchor: drive-relative refused");
    tap_ok(fl_out_add_anchor(&o, "/x", "\\\\server\\share") == -1, "anchor: UNC refused");
    tap_ok(fl_out_add_anchor(NULL, "/x", "C:\\x") == -1 && fl_out_add_anchor(&o, NULL, "C:\\x") == -1 &&
               fl_out_add_anchor(&o, "/x", NULL) == -1,
           "anchor: NULL arguments");
    tap_ok(fl_out_add_anchor(&o, "/x/", "\\\\?\\c:\\y\\\\") == 0 && o.nanchors == 1 &&
               strcmp(o.anchors[0].unix_path, "/x") == 0 && strcmp(o.anchors[0].win_path, "C:/y") == 0,
           "anchor: normalised (trailing separators, \\\\?\\, case, slashes)");
    tap_ok(fl_out_add_anchor(&o, "/", "Z:\\") == 0 && strcmp(o.anchors[1].unix_path, "") == 0 &&
               strcmp(o.anchors[1].win_path, "Z:") == 0,
           "anchor: roots");
    fl_out_free(&o);
    fl_out_free(&o);
    tap_ok(o.anchors == NULL && o.nanchors == 0, "fl_out_free twice is harmless");

    /* FL_OUT_NONE: every feed is passed straight on, nothing is held back */
    memset(&s, 0, sizeof(s));
    fl_out_init(&o, &x, FL_OUT_NONE, 0);
    for (size_t i = 0; i < sizeof(raw) - 1u; i += 5) {
        fl_out_feed(&o, raw + i, sizeof(raw) - 1u - i < 5 ? sizeof(raw) - 1u - i : 5, sink_emit, &s);
    }
    tap_ok(s.calls == (sizeof(raw) - 1u + 4u) / 5u, "NONE: one emit per feed (no buffering)");
    fl_out_flush(&o, sink_emit, &s);
    tap_mem_eq(s.p, s.len, raw, sizeof(raw) - 1u, "NONE: bytes unchanged");
    fl_out_free(&o);
    free(s.p);

    /* an unknown plan value degrades to passthrough */
    memset(&s, 0, sizeof(s));
    fl_out_init(&o, &x, (enum fl_outplan)42, 0);
    fl_out_feed(&o, "/home/u\n", 8, sink_emit, &s);
    fl_out_flush(&o, sink_emit, &s);
    tap_mem_eq(s.p, s.len, "/home/u\n", 8, "unknown plan is passthrough");
    fl_out_free(&o);
    free(s.p);

    /* no callbacks at all: final fallback "Z:" + path */
    memset(&s, 0, sizeof(s));
    fl_out_init(&o, NULL, FL_OUT_PATHS_LINES, 0);
    fl_out_feed(&o, "/pfx/drive_c/x\n/\n", 17, sink_emit, &s);
    fl_out_flush(&o, sink_emit, &s);
    tap_mem_eq(s.p, s.len, "Z:/pfx/drive_c/x\nZ:/\n", 21, "no xlate: Z: fallback");
    fl_out_free(&o);
    free(s.p);

    /* NULL arguments are ignored */
    fl_out_init(NULL, &x, FL_OUT_NONE, 0);
    fl_out_feed(NULL, "x", 1, sink_emit, &s);
    fl_out_flush(NULL, sink_emit, &s);
    fl_out_free(NULL);
    tap_ok(1, "fl_out NULL arguments are ignored");
}

/* A record of exactly FL_OUT_MAX_RECORD bytes is translated; one byte more passes through. */
static void test_out_oversized(void)
{
    struct fl_xlate x = make_x();
    size_t big = FL_OUT_MAX_RECORD;
    size_t n = (big + 1u) + 1u + 8u + (big) + 1u + 8u;
    char *in = malloc(n);
    char *want = malloc(n + 16u);
    size_t w = 0;
    size_t i = 0;
    static const size_t chunks[] = { 0, 1, 4096, 65536, 1000003 };
    if (!tap_ok(in != NULL && want != NULL, "oversized: allocate")) {
        free(in);
        free(want);
        return;
    }
    /* record 1: '/' + (big) 'a' = big + 1 bytes -> untranslated */
    in[i++] = '/';
    memset(in + i, 'a', big);
    i += big;
    in[i++] = '\n';
    memcpy(in + i, "/home/u\n", 8); /* translated again after the long record */
    i += 8;
    /* record 2: '/' + (big - 1) 'b' = exactly big bytes -> translated */
    in[i++] = '/';
    memset(in + i, 'b', big - 1u);
    i += big - 1u;
    in[i++] = '\n';
    memcpy(in + i, "/home/v\n", 8);
    i += 8;
    n = i;
    memcpy(want, in, big + 2u);
    w = big + 2u;
    memcpy(want + w, "Z:/home/u\n", 10);
    w += 10;
    memcpy(want + w, "Z:/", 3);
    w += 3;
    memset(want + w, 'b', big - 1u);
    w += big - 1u;
    want[w++] = '\n';
    memcpy(want + w, "Z:/home/v\n", 10);
    w += 10;
    for (size_t c = 0; c < sizeof(chunks) / sizeof(chunks[0]); c++) {
        struct sink s;
        run_out(&x, FL_OUT_PATHS_LINES, 0, "(none)", in, n, chunks[c], &s);
        tap_mem_eq(s.p, s.len, want, w, "oversized records (chunk %zu)", chunks[c]);
        free(s.p);
    }
    /* an over-long final record without a terminator also passes through */
    {
        struct sink s;
        run_out(&x, FL_OUT_PATHS_LINES, 0, "(none)", in, big + 1u, 4096, &s);
        tap_mem_eq(s.p, s.len, in, big + 1u, "oversized unterminated tail passes through");
        free(s.p);
    }
    /* -z records: same limit */
    {
        struct sink s;
        in[big + 1u] = '\0';
        run_out(&x, FL_OUT_PATHS_LINES, 1, "(none)", in, big + 2u, 777, &s);
        tap_mem_eq(s.p, s.len, in, big + 2u, "oversized NUL record passes through");
        free(s.p);
    }
    free(in);
    free(want);
}

void test_translate(void)
{
    int n;
    test_is_win_abs();
    test_win_to_unix();
    test_argv_errors();
    test_env_errors();
    test_out_api();
    test_out_oversized();
    n = for_each_vector("argv.tsv", 6, argv_vector);
    tap_ok(n > 0, "argv.tsv loaded (%d vectors)", n);
    n = for_each_vector("env.tsv", 4, env_vector);
    tap_ok(n > 0, "env.tsv loaded (%d vectors)", n);
    n = for_each_vector("out.tsv", 6, out_vector);
    tap_ok(n > 0, "out.tsv loaded (%d vectors)", n);
}
