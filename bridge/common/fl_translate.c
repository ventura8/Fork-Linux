/*
 * fl_translate.c - argv / environment / output translation for the git bridge.
 * See fl_translate.h for the rules. Pure C11 (no POSIX or Win32 calls).
 */
#include "fl_translate.h"

#include <stdlib.h>
#include <string.h>

#include "fl_shquote.h"

#define XPATH_MAX 32768u /* scratch size for one path passed through a callback */

/* ======================================================================== */
/* Small helpers                                                             */
/* ======================================================================== */

static int is_alpha(char c)
{
    return (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z');
}

static int is_digit(char c)
{
    return c >= '0' && c <= '9';
}

static int is_hex_lower(char c)
{
    return is_digit(c) || (c >= 'a' && c <= 'f');
}

static char to_lower(char c)
{
    return (c >= 'A' && c <= 'Z') ? (char)(c - 'A' + 'a') : c;
}

static char to_upper(char c)
{
    return (c >= 'a' && c <= 'z') ? (char)(c - 'a' + 'A') : c;
}

static int is_sep(char c)
{
    return c == '\\' || c == '/';
}

/* Case-insensitive (ASCII) equality of a[0..n) and the NUL-terminated b. */
static int ci_eqn(const char *a, size_t n, const char *b)
{
    for (size_t i = 0; i < n; i++) {
        if (b[i] == '\0' || to_lower(a[i]) != to_lower(b[i])) {
            return 0;
        }
    }
    return b[n] == '\0';
}

static int ci_eq(const char *a, const char *b)
{
    return ci_eqn(a, strlen(a), b);
}

/* s starts with p (case-insensitive); never reads past the end of s. */
static int ci_prefix(const char *s, const char *p)
{
    for (; *p != '\0'; s++, p++) {
        if (*s == '\0' || to_lower(*s) != to_lower(*p)) {
            return 0;
        }
    }
    return 1;
}

static int has_prefix(const char *s, const char *p)
{
    return strncmp(s, p, strlen(p)) == 0;
}

static int has_suffix_ci(const char *s, const char *suf)
{
    size_t n = strlen(s);
    size_t m = strlen(suf);
    return n >= m && ci_eqn(s + n - m, m, suf);
}

static int in_list(const char *s, const char *const *list)
{
    for (size_t i = 0; list[i] != NULL; i++) {
        if (strcmp(s, list[i]) == 0) {
            return 1;
        }
    }
    return 0;
}

static char *dupn(const char *s, size_t n)
{
    char *r = malloc(n + 1);
    if (r == NULL) {
        return NULL;
    }
    if (n > 0) {
        memcpy(r, s, n);
    }
    r[n] = '\0';
    return r;
}

static char *dup(const char *s)
{
    return dupn(s, strlen(s));
}

/* ------------------------------------------------------------------------ */
/* Growable string buffer: an allocation failure sets oom and turns every    */
/* later append into a no-op, so callers check once at the end.              */
/* ------------------------------------------------------------------------ */

struct sb {
    char *p;
    size_t len;
    size_t cap;
    int oom;
};

static void sb_init(struct sb *b)
{
    b->p = NULL;
    b->len = 0;
    b->cap = 0;
    b->oom = 0;
}

static void sb_free(struct sb *b)
{
    free(b->p);
    sb_init(b);
}

static int sb_grow(struct sb *b, size_t add)
{
    size_t need;
    size_t cap;
    char *np;
    if (b->oom) {
        return -1;
    }
    if (add > ((size_t)-1) - b->len - 1u) {
        b->oom = 1;
        return -1;
    }
    need = b->len + add + 1u;
    if (need <= b->cap) {
        return 0;
    }
    cap = b->cap ? b->cap : 64u;
    while (cap < need) {
        cap = cap > ((size_t)-1) / 2u ? need : cap * 2u;
    }
    np = realloc(b->p, cap);
    if (np == NULL) {
        b->oom = 1;
        return -1;
    }
    b->p = np;
    b->cap = cap;
    return 0;
}

static void sb_addn(struct sb *b, const char *s, size_t n)
{
    if (sb_grow(b, n) != 0) {
        return;
    }
    if (n > 0) {
        memcpy(b->p + b->len, s, n);
    }
    b->len += n;
    b->p[b->len] = '\0';
}

static void sb_adds(struct sb *b, const char *s)
{
    sb_addn(b, s, strlen(s));
}

static void sb_addc(struct sb *b, char c)
{
    sb_addn(b, &c, 1);
}

/* Append s as one POSIX sh single-quoted word. */
static void sb_addq(struct sb *b, const char *s)
{
    size_t need = 3;
    int n;
    for (const char *p = s; *p != '\0'; p++) {
        need += *p == '\'' ? 4u : 1u;
    }
    if (sb_grow(b, need) != 0) {
        return;
    }
    n = fl_shquote(s, b->p + b->len, b->cap - b->len);
    if (n < 0) {
        b->oom = 1;
        return;
    }
    b->len += (size_t)n;
}

/* Append s bare when it only holds shell-safe characters, else single-quoted. */
static void sb_add_word(struct sb *b, const char *s)
{
    int safe = s[0] != '\0';
    for (const char *p = s; *p != '\0' && safe; p++) {
        char c = *p;
        safe = is_alpha(c) || is_digit(c) || strchr("_@%+=:,./-", c) != NULL;
    }
    if (safe) {
        sb_adds(b, s);
    } else {
        sb_addq(b, s);
    }
}

/* git's sq_quote_buf(): '...' with ' and ! escaped as '\'' and '\!'. */
static void sb_add_gitsq(struct sb *b, const char *s)
{
    sb_addc(b, '\'');
    for (const char *p = s; *p != '\0'; p++) {
        if (*p == '\'' || *p == '!') {
            sb_adds(b, "'\\");
            sb_addc(b, *p);
            sb_addc(b, '\'');
        } else {
            sb_addc(b, *p);
        }
    }
    sb_addc(b, '\'');
}

/* Detach the string (NULL on oom; "" when nothing was appended). */
static char *sb_take(struct sb *b)
{
    char *r;
    if (b->oom) {
        sb_free(b);
        return NULL;
    }
    if (b->p == NULL) {
        return dup("");
    }
    r = b->p;
    sb_init(b);
    return r;
}

/* ------------------------------------------------------------------------ */
/* String vectors                                                            */
/* ------------------------------------------------------------------------ */

void fl_strvec_free(struct fl_strvec *v)
{
    if (v == NULL) {
        return;
    }
    if (v->v != NULL) {
        for (size_t i = 0; i < v->n; i++) {
            free(v->v[i]);
        }
        free(v->v);
    }
    v->n = 0;
    v->cap = 0;
    v->v = NULL;
}

/* Append s (ownership taken; freed on failure). */
static int sv_take(struct fl_strvec *v, char *s)
{
    if (s == NULL) {
        return -1;
    }
    if (v->n + 1u >= v->cap) {
        size_t cap = v->cap ? v->cap * 2u : 8u;
        char **nv;
        if (cap > ((size_t)-1) / sizeof(char *)) {
            free(s);
            return -1;
        }
        nv = realloc(v->v, cap * sizeof(char *));
        if (nv == NULL) {
            free(s);
            return -1;
        }
        v->v = nv;
        v->cap = cap;
    }
    v->v[v->n++] = s;
    v->v[v->n] = NULL;
    return 0;
}

static int sv_add(struct fl_strvec *v, const char *s)
{
    return sv_take(v, dup(s));
}

static int sv_contains(const struct fl_strvec *v, const char *s)
{
    for (size_t i = 0; i < v->n; i++) {
        if (strcmp(v->v[i], s) == 0) {
            return 1;
        }
    }
    return 0;
}

/* ======================================================================== */
/* Windows paths                                                             */
/* ======================================================================== */

/* "X:\" or "X:/" at s. */
static int drive_abs(const char *s)
{
    return is_alpha(s[0]) && s[1] == ':' && is_sep(s[2]);
}

/* "\\?\" at s. */
static int verbatim(const char *s)
{
    return s[0] == '\\' && s[1] == '\\' && s[2] == '?' && s[3] == '\\';
}

/* "unix\" or "unix/" at s (after "\\?\"). */
static int unix_ns(const char *s)
{
    return ci_prefix(s, "unix") && is_sep(s[4]);
}

/* A drive path, possibly behind "\\?\" (not a URL, not the unix namespace). */
static int win_drive_path(const char *s)
{
    return drive_abs(s) || (verbatim(s) && drive_abs(s + 4));
}

int fl_is_win_abs(const char *s)
{
    const char *r;
    if (s == NULL) {
        return 0;
    }
    if (drive_abs(s)) {
        return 1;
    }
    if (verbatim(s)) {
        return drive_abs(s + 4) || unix_ns(s + 4);
    }
    if (ci_prefix(s, "file://")) {
        r = s + 7;
        if (*r == '/') {
            r++;
        }
        return drive_abs(r);
    }
    return 0;
}

/* Append p[0..) with every run of separators collapsed into one `sep`. */
static void add_collapsed(struct sb *b, const char *p, char sep)
{
    int prev = 0;
    for (; *p != '\0'; p++) {
        if (is_sep(*p)) {
            if (!prev) {
                sb_addc(b, sep);
            }
            prev = 1;
        } else {
            sb_addc(b, *p);
            prev = 0;
        }
    }
}

int fl_win_to_unix(const struct fl_xlate *x, const char *in, char *out, size_t outsz)
{
    struct sb norm;
    const char *p;
    const char *scheme = "";
    char *buf;
    int rc = -1;
    if (out != NULL && outsz > 0) {
        out[0] = '\0';
    }
    if (x == NULL || in == NULL || out == NULL || outsz == 0) {
        return -1;
    }
    p = in;
    sb_init(&norm);
    if (ci_prefix(p, "file://")) {
        scheme = "file://";
        p += 7;
        if (*p == '/') {
            p++;
        }
        if (!drive_abs(p)) {
            return -1;
        }
    } else if (verbatim(p)) {
        p += 4;
        if (unix_ns(p)) {
            add_collapsed(&norm, p + 4, '/');
            if (!norm.oom && norm.len + 1u <= outsz) {
                memcpy(out, norm.p, norm.len + 1u);
                rc = 0;
            }
            sb_free(&norm);
            return rc;
        }
        if (!drive_abs(p)) {
            return -1;
        }
    } else if (!drive_abs(p)) {
        return -1;
    }
    if (x->to_unix == NULL) {
        return -1;
    }
    sb_addc(&norm, to_upper(p[0]));
    sb_addc(&norm, ':');
    add_collapsed(&norm, p + 2, '\\');
    buf = norm.oom ? NULL : malloc(XPATH_MAX);
    if (buf != NULL) {
        buf[0] = '\0';
        if (x->to_unix(x->ud, norm.p, buf, XPATH_MAX) == 0 && memchr(buf, '\0', XPATH_MAX) != NULL &&
            buf[0] == '/') {
            size_t sl = strlen(scheme);
            size_t n = strlen(buf);
            if (sl + n + 1u <= outsz) {
                memcpy(out, scheme, sl);
                memcpy(out + sl, buf, n + 1u);
                rc = 0;
            }
        }
        free(buf);
    }
    sb_free(&norm);
    if (rc != 0) {
        out[0] = '\0';
    }
    return rc;
}

/* 1: *res is the malloc'd Unix form of s; 0: s is not a mappable Windows path; -1: oom. */
static int xl_path(const struct fl_xlate *x, const char *s, char **res)
{
    char *buf;
    *res = NULL;
    if (!fl_is_win_abs(s)) {
        return 0;
    }
    buf = malloc(XPATH_MAX);
    if (buf == NULL) {
        return -1;
    }
    if (fl_win_to_unix(x, s, buf, XPATH_MAX) != 0) {
        free(buf);
        return 0;
    }
    *res = dup(buf);
    free(buf);
    return *res != NULL ? 1 : -1;
}

/* Push s, translated when it is a whole Windows-absolute path. */
static int push_xl(const struct fl_xlate *x, struct fl_strvec *out, const char *s)
{
    char *t;
    int r = xl_path(x, s, &t);
    if (r < 0) {
        return -1;
    }
    return r == 1 ? sv_take(out, t) : sv_add(out, s);
}

/* ======================================================================== */
/* sh words (editor / credential helper / ssh command values)                */
/* ======================================================================== */

struct shword {
    size_t start; /* raw span in the source string */
    size_t end;
    char *val;    /* the word after quote removal */
    int dynamic;  /* holds an unquoted or double-quoted $ or ` (value not literal) */
};

struct shwords {
    struct shword *w;
    size_t n;
    size_t cap;
};

enum { SH_OOM = -1, SH_OK = 0, SH_UNSUPPORTED = 1 };

static void shwords_free(struct shwords *ws)
{
    for (size_t i = 0; i < ws->n; i++) {
        free(ws->w[i].val);
    }
    free(ws->w);
    ws->w = NULL;
    ws->n = 0;
    ws->cap = 0;
}

static int sh_blank(char c)
{
    return c == ' ' || c == '\t' || c == '\n';
}

static int shwords_push(struct shwords *ws, size_t start, size_t end, char *val, int dynamic)
{
    if (val == NULL) {
        return -1;
    }
    if (ws->n == ws->cap) {
        size_t cap = ws->cap ? ws->cap * 2u : 8u;
        struct shword *nw;
        if (cap > ((size_t)-1) / sizeof(struct shword)) {
            free(val);
            return -1;
        }
        nw = realloc(ws->w, cap * sizeof(struct shword));
        if (nw == NULL) {
            free(val);
            return -1;
        }
        ws->w = nw;
        ws->cap = cap;
    }
    ws->w[ws->n].start = start;
    ws->w[ws->n].end = end;
    ws->w[ws->n].val = val;
    ws->w[ws->n].dynamic = dynamic;
    ws->n++;
    return 0;
}

/*
 * Split s into at most `max` (0: all) POSIX sh words with quote removal. Control operators
 * (| & ; < > ( )), comments and unbalanced quotes make the value SH_UNSUPPORTED.
 */
static int sh_split(const char *s, size_t max, struct shwords *ws)
{
    struct sb v;
    size_t i = 0;
    ws->w = NULL;
    ws->n = 0;
    ws->cap = 0;
    sb_init(&v);
    for (;;) {
        size_t start;
        int dyn = 0;
        while (sh_blank(s[i])) {
            i++;
        }
        if (s[i] == '\0' || (max != 0 && ws->n >= max)) {
            return SH_OK;
        }
        if (s[i] == '#') {
            goto unsupported;
        }
        start = i;
        while (s[i] != '\0' && !sh_blank(s[i])) {
            char c = s[i];
            if (c == '\'') {
                i++;
                while (s[i] != '\0' && s[i] != '\'') {
                    sb_addc(&v, s[i++]);
                }
                if (s[i] == '\0') {
                    goto unsupported;
                }
                i++;
            } else if (c == '"') {
                i++;
                while (s[i] != '\0' && s[i] != '"') {
                    if (s[i] == '\\' && s[i + 1] != '\0' && strchr("$`\"\\\n", s[i + 1]) != NULL) {
                        if (s[i + 1] != '\n') {
                            sb_addc(&v, s[i + 1]);
                        }
                        i += 2;
                    } else {
                        if (s[i] == '$' || s[i] == '`') {
                            dyn = 1;
                        }
                        sb_addc(&v, s[i++]);
                    }
                }
                if (s[i] == '\0') {
                    goto unsupported;
                }
                i++;
            } else if (c == '\\') {
                if (s[i + 1] == '\0') {
                    goto unsupported;
                }
                if (s[i + 1] != '\n') {
                    sb_addc(&v, s[i + 1]);
                }
                i += 2;
            } else if (strchr("|&;<>()", c) != NULL) {
                goto unsupported;
            } else {
                if (c == '$' || c == '`') {
                    dyn = 1;
                }
                sb_addc(&v, c);
                i++;
            }
        }
        if (v.oom || shwords_push(ws, start, i, sb_take(&v), dyn) != 0) {
            sb_free(&v);
            shwords_free(ws);
            return SH_OOM;
        }
    }
unsupported:
    sb_free(&v);
    shwords_free(ws);
    return SH_UNSUPPORTED;
}

/* A program only Windows can run: a drive path, or a bare name ending in .exe/.com/.bat/.cmd. */
static int is_win_program(const char *w)
{
    if (w[0] == '\0') {
        return 0;
    }
    if (win_drive_path(w)) {
        return 1;
    }
    if (w[0] == '/') {
        return 0;
    }
    return has_suffix_ci(w, ".exe") || has_suffix_ci(w, ".com") || has_suffix_ci(w, ".bat") ||
           has_suffix_ci(w, ".cmd");
}

static const char *base_name(const char *w)
{
    const char *b = w;
    for (const char *p = w; *p != '\0'; p++) {
        if (is_sep(*p)) {
            b = p + 1;
        }
    }
    return b;
}

/* basename(w) is `name` or `name.exe` (case-insensitive). */
static int prog_is(const char *w, const char *name)
{
    const char *b = base_name(w);
    size_t bl = strlen(b);
    size_t n = strlen(name);
    if (ci_eqn(b, bl, name)) {
        return 1;
    }
    return bl == n + 4u && ci_prefix(b, name) && ci_eq(b + n, ".exe");
}

/* bang q(winexec) ' ' q(prog) rest */
static char *wrap_winexec(const struct fl_xlate *x, const char *bang, const char *prog, const char *rest)
{
    struct sb b;
    sb_init(&b);
    sb_adds(&b, bang);
    sb_addq(&b, x->winexec);
    sb_addc(&b, ' ');
    sb_addq(&b, prog);
    sb_adds(&b, rest);
    return sb_take(&b);
}

static int have(const char *s)
{
    return s != NULL && s[0] != '\0';
}

/* Shell-command editors (core.editor, sequence.editor, GIT_EDITOR, ...). 1 changed, 0 keep, -1 oom. */
static int rewrite_editor(const struct fl_xlate *x, const char *val, char **res)
{
    struct shwords ws;
    int r;
    *res = NULL;
    if (!have(x->winexec)) {
        return 0;
    }
    r = sh_split(val, 1, &ws);
    if (r == SH_OOM) {
        return -1;
    }
    if (r == SH_OK && ws.n == 1 && !ws.w[0].dynamic && is_win_program(ws.w[0].val)) {
        *res = wrap_winexec(x, "", ws.w[0].val, val + ws.w[0].end);
        r = *res != NULL ? 1 : -1;
    } else {
        r = 0;
    }
    shwords_free(&ws);
    return r;
}

/* credential.helper: "!cmd" is a shell command; an absolute path is run as a command too. */
static int rewrite_cred_helper(const struct fl_xlate *x, const char *val, char **res)
{
    struct shwords ws;
    const char *cmd = val;
    int bang = val[0] == '!';
    int r;
    *res = NULL;
    if (!have(x->winexec)) {
        return 0;
    }
    if (bang) {
        cmd++;
    }
    r = sh_split(cmd, 1, &ws);
    if (r == SH_OOM) {
        return -1;
    }
    if (r == SH_OK && ws.n == 1 && !ws.w[0].dynamic &&
        (bang ? is_win_program(ws.w[0].val) : win_drive_path(ws.w[0].val))) {
        *res = wrap_winexec(x, "!", ws.w[0].val, cmd + ws.w[0].end);
        r = *res != NULL ? 1 : -1;
    } else {
        r = 0;
    }
    shwords_free(&ws);
    return r;
}

/* prefix + head + translated tail, or NULL on oom. */
static char *join3(const char *prefix, const char *head, size_t headlen, const char *tail)
{
    struct sb b;
    sb_init(&b);
    sb_adds(&b, prefix);
    sb_addn(&b, head, headlen);
    sb_adds(&b, tail);
    return sb_take(&b);
}

/* ssh "-o Key=Value" / "Key Value": translate a Windows-absolute value. 1 changed, 0 keep, -1 oom. */
static int xl_ssh_opt(const struct fl_xlate *x, const char *prefix, const char *kv, char **res)
{
    size_t v = 0;
    char *t;
    int r;
    *res = NULL;
    while (kv[v] != '\0' && kv[v] != '=' && !sh_blank(kv[v])) {
        v++;
    }
    while (sh_blank(kv[v])) {
        v++;
    }
    if (kv[v] == '=') {
        v++;
    }
    while (sh_blank(kv[v])) {
        v++;
    }
    r = xl_path(x, kv + v, &t);
    if (r != 1) {
        return r;
    }
    *res = join3(prefix, kv, v, t);
    free(t);
    return *res != NULL ? 1 : -1;
}

/* Number of following words an ssh option consumes as its value: 1 path, 2 -o, 3 other. */
static int ssh_value_kind(const char *v)
{
    static const char *const paths[] = { "-i", "-F", "-E", "-S", NULL };
    static const char *const others[] = { "-b", "-B", "-c", "-D", "-e", "-I", "-J", "-l", "-L", "-m", "-O",
                                          "-p", "-P", "-Q", "-R", "-W", "-w", NULL };
    if (in_list(v, paths)) {
        return 1;
    }
    if (strcmp(v, "-o") == 0) {
        return 2;
    }
    return in_list(v, others) ? 3 : 0;
}

/*
 * core.sshCommand / GIT_SSH_COMMAND: Windows ssh -> native "ssh" with -i/-F/-E/-S/-o paths
 * translated; plink/TortoisePlink -> plain "ssh" (their options mean nothing to OpenSSH);
 * another Windows program is wrapped with winexec. 1 changed, 0 keep, -1 oom.
 */
static int rewrite_ssh(const struct fl_xlate *x, const char *val, char **res)
{
    struct shwords ws;
    struct sb b;
    const char *prog;
    int changed = 0;
    int expect = 0;
    int r = sh_split(val, 0, &ws);
    *res = NULL;
    if (r == SH_OOM) {
        return -1;
    }
    if (r != SH_OK || ws.n == 0 || ws.w[0].dynamic) {
        shwords_free(&ws);
        return 0;
    }
    prog = ws.w[0].val;
    if (prog_is(prog, "plink") || prog_is(prog, "tortoiseplink")) {
        shwords_free(&ws);
        *res = dup("ssh");
        return *res != NULL ? 1 : -1;
    }
    if (is_win_program(prog) && !prog_is(prog, "ssh")) {
        r = 0;
        if (have(x->winexec)) {
            *res = wrap_winexec(x, "", prog, val + ws.w[0].end);
            r = *res != NULL ? 1 : -1;
        }
        shwords_free(&ws);
        return r;
    }
    sb_init(&b);
    if (is_win_program(prog)) {
        sb_adds(&b, "ssh");
        changed = 1;
    } else {
        sb_addn(&b, val + ws.w[0].start, ws.w[0].end - ws.w[0].start);
    }
    for (size_t i = 1; i < ws.n; i++) {
        const struct shword *w = &ws.w[i];
        const char *v = w->val;
        char *t = NULL;
        int kind = expect;
        r = 0;
        expect = 0;
        if (!w->dynamic) {
            if (kind == 1) {
                r = xl_path(x, v, &t);
            } else if (kind == 2) {
                r = xl_ssh_opt(x, "", v, &t);
            } else if (kind == 0) {
                int k = ssh_value_kind(v);
                if (k != 0) {
                    expect = k;
                } else if (v[0] == '-' && v[1] != '\0' && strchr("iFES", v[1]) != NULL && v[2] != '\0') {
                    char *p = NULL;
                    r = xl_path(x, v + 2, &p);
                    if (r == 1) {
                        t = join3("", v, 2, p);
                        free(p);
                        r = t != NULL ? 1 : -1;
                    }
                } else if (v[0] == '-' && v[1] == 'o' && v[2] != '\0') {
                    r = xl_ssh_opt(x, "-o", v + 2, &t);
                } else if (v[0] != '-') {
                    r = xl_path(x, v, &t);
                }
            }
        }
        if (r < 0) {
            sb_free(&b);
            shwords_free(&ws);
            return -1;
        }
        sb_addc(&b, ' ');
        if (r == 1) {
            sb_add_word(&b, t);
            changed = 1;
        } else {
            sb_addn(&b, val + w->start, w->end - w->start);
        }
        free(t);
    }
    shwords_free(&ws);
    if (!changed) {
        sb_free(&b);
        return 0;
    }
    *res = sb_take(&b);
    return *res != NULL ? 1 : -1;
}

/* ======================================================================== */
/* Config sanitizer (-c, clone --config, GIT_CONFIG_PARAMETERS, GIT_CONFIG_*) */
/* ======================================================================== */

enum { CFG_ERR = -1, CFG_KEEP = 0, CFG_DROP = 1, CFG_CHANGED = 2 };

struct cfgkey {
    const char *sec;
    size_t seclen;
    int has_sub;
    const char *var;
    size_t varlen;
};

static int parse_key(const char *key, size_t keylen, struct cfgkey *k)
{
    const char *first = memchr(key, '.', keylen);
    const char *last = NULL;
    if (first == NULL) {
        return -1;
    }
    for (size_t i = keylen; i > 0; i--) {
        if (key[i - 1u] == '.') {
            last = key + i - 1u;
            break;
        }
    }
    k->sec = key;
    k->seclen = (size_t)(first - key);
    k->has_sub = last != first;
    k->var = last + 1;
    k->varlen = keylen - (size_t)(last + 1 - key);
    return 0;
}

static int sec_is(const struct cfgkey *k, const char *s)
{
    return ci_eqn(k->sec, k->seclen, s);
}

static int var_is(const struct cfgkey *k, const char *s)
{
    return ci_eqn(k->var, k->varlen, s);
}

static int var_has_prefix(const struct cfgkey *k, const char *p)
{
    size_t n = strlen(p);
    return k->varlen >= n && ci_eqn(k->var, n, p);
}

/* git's boolean "true": true/yes/on (any case) or a non-zero integer. */
static int bool_true(const char *v)
{
    int nonzero = 0;
    size_t i = 0;
    if (ci_eq(v, "true") || ci_eq(v, "yes") || ci_eq(v, "on")) {
        return 1;
    }
    if (v[0] == '-' || v[0] == '+') {
        i = 1;
    }
    if (v[i] == '\0') {
        return 0;
    }
    for (; v[i] != '\0'; i++) {
        if (!is_digit(v[i])) {
            return 0;
        }
        nonzero |= v[i] != '0';
    }
    return nonzero;
}

/* p has a path component equal to comp (case-insensitive; both separators). */
static int has_component_ci(const char *p, const char *comp)
{
    size_t n = strlen(comp);
    size_t i = 0;
    while (p[i] != '\0') {
        size_t s = i;
        while (p[i] != '\0' && !is_sep(p[i])) {
            i++;
        }
        if (i - s == n && ci_eqn(p + s, n, comp)) {
            return 1;
        }
        while (is_sep(p[i])) {
            i++;
        }
    }
    return 0;
}

static int is_plink_variant(const char *v)
{
    return ci_eq(v, "plink") || ci_eq(v, "putty") || ci_eq(v, "tortoiseplink");
}

static int cfg_result(int r)
{
    return r < 0 ? CFG_ERR : (r == 1 ? CFG_CHANGED : CFG_KEEP);
}

/*
 * Sanitize one config entry (val NULL: implicit boolean true). CFG_CHANGED stores the new
 * value in *newval (malloc'd).
 */
static int sanitize_cfg(const struct fl_xlate *x, const char *key, size_t keylen, const char *val, char **newval)
{
    struct cfgkey k;
    int core;
    *newval = NULL;
    if (parse_key(key, keylen, &k) != 0) {
        return CFG_KEEP;
    }
    core = sec_is(&k, "core") && !k.has_sub;
    /* Windows-only settings: dropped whatever their value. */
    if (sec_is(&k, "http") && (var_is(&k, "sslbackend") || var_has_prefix(&k, "schannel"))) {
        return CFG_DROP;
    }
    if (core && (var_is(&k, "fscache") || var_is(&k, "longpaths"))) {
        return CFG_DROP;
    }
    if (core && var_is(&k, "fsmonitor")) {
        return val == NULL || bool_true(val) || is_win_program(val) ? CFG_DROP : CFG_KEEP;
    }
    if (val == NULL || val[0] == '\0') {
        return CFG_KEEP; /* implicit booleans and resets ("credential.helper=") */
    }
    if ((core && var_is(&k, "editor")) || (sec_is(&k, "sequence") && !k.has_sub && var_is(&k, "editor"))) {
        return cfg_result(rewrite_editor(x, val, newval));
    }
    if (sec_is(&k, "credential") && var_is(&k, "helper")) {
        return cfg_result(rewrite_cred_helper(x, val, newval));
    }
    if (core && var_is(&k, "askpass")) {
        if (!is_win_program(val)) {
            return CFG_KEEP;
        }
        if (!have(x->askpass)) {
            return CFG_DROP;
        }
        *newval = dup(x->askpass);
        return *newval != NULL ? CFG_CHANGED : CFG_ERR;
    }
    if (core && var_is(&k, "sshcommand")) {
        return cfg_result(rewrite_ssh(x, val, newval));
    }
    if (sec_is(&k, "ssh") && !k.has_sub && var_is(&k, "variant")) {
        return is_plink_variant(val) ? CFG_DROP : CFG_KEEP;
    }
    if (sec_is(&k, "http") && (var_is(&k, "sslcainfo") || var_is(&k, "sslcapath")) &&
        has_component_ci(val, "gitinstance")) {
        return CFG_DROP;
    }
    if (sec_is(&k, "gpg") && var_is(&k, "program")) {
        return is_win_program(val) ? CFG_DROP : CFG_KEEP;
    }
    /* Path-typed keys (core.hooksPath, include.path, safe.directory, ...) and every other key:
       translated only when the whole value is a Windows-absolute path. */
    return cfg_result(xl_path(x, val, newval));
}

/* "-c key[=value]": 1 keep as is, 0 drop, 2 replaced by *res, -1 oom. */
static int sanitize_kv_arg(const struct fl_xlate *x, const char *kv, char **res)
{
    const char *eq = strchr(kv, '=');
    size_t keylen = eq != NULL ? (size_t)(eq - kv) : strlen(kv);
    char *nv = NULL;
    int r = sanitize_cfg(x, kv, keylen, eq != NULL ? eq + 1 : NULL, &nv);
    struct sb b;
    *res = NULL;
    if (r == CFG_ERR) {
        return -1;
    }
    if (r == CFG_DROP) {
        return 0;
    }
    if (r == CFG_KEEP) {
        return 1;
    }
    sb_init(&b);
    sb_addn(&b, kv, keylen);
    sb_addc(&b, '=');
    sb_adds(&b, nv);
    free(nv);
    *res = sb_take(&b);
    return *res != NULL ? 2 : -1;
}

/* ======================================================================== */
/* argv                                                                      */
/* ======================================================================== */

static const char *const k_path_eq_opts[] = {
    "--git-dir", "--work-tree", "--file", "--template", "--reference", "--reference-if-able",
    "--separate-git-dir", "--output", "--output-directory", "--pathspec-from-file", "--contents",
    "--ignore-revs-file", "--index-output", "--exclude-from", "--orderfile", "--resolve-git-dir", NULL,
};

static const char *const k_path_next_opts[] = {
    "-C", "-F", "-o", "--git-dir", "--work-tree", "--file", "--output", "--template", "--reference", NULL,
};

/* Long options whose separate value is never translated (--pretty and --notes only take =value). */
static const char *const k_never_long[] = {
    "--message", "--grep", "--author", "--committer", "--format", "--date", "--since", "--until", "--before",
    "--after", "--strategy-option", "--trailer", "--exclude", "--glob", "--decorate-refs",
    "--decorate-refs-exclude", NULL,
};

static const char *const k_cmds_m[] = { "commit", "merge", "tag", "notes", "stash", "revert", "cherry-pick",
                                        "commit-tree", NULL };
static const char *const k_cmds_e[] = { "grep", NULL };
static const char *const k_cmds_sg[] = { "log", "show", "whatchanged", "diff", "diff-tree", "diff-index",
                                         "diff-files", "rev-list", NULL };
static const char *const k_cmds_x[] = { "merge", "rebase", "pull", "cherry-pick", "revert", NULL };

/* The option consumes the next argument, which is never translated. */
static int never_consumes(const char *sub, const char *a)
{
    size_t n = strlen(a);
    if (in_list(a, k_never_long)) {
        return 1;
    }
    if (n > 2u && a[0] == '-' && a[1] != '-') {
        /* A short-option cluster ("-am"): parse-options gives its last letter the next
           argument as the value, so "-am <msg>" is "-a -m <msg>". */
        char last[3];
        for (size_t i = 1; i < n; i++) {
            if (!is_alpha(a[i])) {
                return 0;
            }
        }
        last[0] = '-';
        last[1] = a[n - 1u];
        last[2] = '\0';
        return never_consumes(sub, last);
    }
    if (strcmp(a, "-m") == 0) {
        return in_list(sub, k_cmds_m);
    }
    if (strcmp(a, "-e") == 0) {
        return in_list(sub, k_cmds_e);
    }
    if (strcmp(a, "-S") == 0 || strcmp(a, "-G") == 0) {
        return in_list(sub, k_cmds_sg);
    }
    if (strcmp(a, "-X") == 0) {
        return in_list(sub, k_cmds_x);
    }
    return 0;
}

/* Push "-c" + sanitized value (both dropped when the entry is dropped). */
static int push_config(const struct fl_xlate *x, struct fl_strvec *out, const char *opt, const char *kv)
{
    char *res;
    int r = sanitize_kv_arg(x, kv, &res);
    if (r < 0) {
        return -1;
    }
    if (r == 0) {
        return 0;
    }
    if (sv_add(out, opt) != 0) {
        free(res);
        return -1;
    }
    return r == 2 ? sv_take(out, res) : sv_add(out, kv);
}

/* "--opt=value" with the value translated. */
static int push_opt_value(const struct fl_xlate *x, struct fl_strvec *out, const char *a, size_t namelen)
{
    char *t;
    char *joined;
    int r = xl_path(x, a + namelen + 1u, &t);
    if (r < 0) {
        return -1;
    }
    if (r == 0) {
        return sv_add(out, a);
    }
    joined = join3("", a, namelen + 1u, t);
    free(t);
    return sv_take(out, joined);
}

static void set_subcmd(struct fl_cmdinfo *info, const char *s)
{
    size_t n = strlen(s);
    if (n >= sizeof(info->subcmd)) {
        n = sizeof(info->subcmd) - 1u;
    }
    memcpy(info->subcmd, s, n);
    info->subcmd[n] = '\0';
}

/* --- output plan ---------------------------------------------------------- */

static const char *const k_path_keys[] = {
    "core.hookspath", "core.excludesfile", "core.attributesfile", "commit.template", "include.path",
    "init.templatedir", "gpg.ssh.allowedsignersfile", "user.signingkey", "safe.directory", NULL,
};

static int key_is_remote_url(const char *key, size_t n)
{
    struct cfgkey k;
    return parse_key(key, n, &k) == 0 && sec_is(&k, "remote") && k.has_sub &&
           (var_is(&k, "url") || var_is(&k, "pushurl"));
}

static int key_is_path(const char *key)
{
    struct cfgkey k;
    for (size_t i = 0; k_path_keys[i] != NULL; i++) {
        if (ci_eq(key, k_path_keys[i])) {
            return 1;
        }
    }
    return parse_key(key, strlen(key), &k) == 0 && sec_is(&k, "includeif") && k.has_sub && var_is(&k, "path");
}

static int ci_contains(const char *s, const char *needle)
{
    for (; *s != '\0'; s++) {
        if (ci_prefix(s, needle)) {
            return 1;
        }
    }
    return 0;
}

static enum fl_outplan plan_config(int n, char **a)
{
    static const char *const value_opts[] = { "-f", "--file", "--blob", "--type", "-t", "--default",
                                              "--comment", "--value", "--url", NULL };
    static const char *const actions[] = { "--list", "-l", "--unset", "--unset-all", "--add", "--replace-all",
                                           "--rename-section", "--remove-section", "--edit", "-e",
                                           "--get-color", "--get-colorbool", "--get-urlmatch", NULL };
    static const char *const verbs[] = { "get", "set", "unset", "list", "edit", "rename-section",
                                         "remove-section", NULL };
    const char *pos[3] = { NULL, NULL, NULL };
    int npos = 0;
    int get = 0;
    int regexp = 0;
    int list = 0;
    int other_action = 0;
    int path_type = 0;
    const char *key = NULL;
    for (int i = 0; i < n; i++) {
        const char *s = a[i];
        if (strcmp(s, "--") == 0) {
            continue;
        }
        if (strcmp(s, "--show-origin") == 0) {
            return FL_OUT_SHOW_ORIGIN;
        }
        if (in_list(s, value_opts)) {
            if ((strcmp(s, "--type") == 0 || strcmp(s, "-t") == 0) && i + 1 < n && strcmp(a[i + 1], "path") == 0) {
                path_type = 1;
            }
            i++;
            continue;
        }
        if (strcmp(s, "--type=path") == 0 || strcmp(s, "--path") == 0) {
            path_type = 1;
        } else if (strcmp(s, "--get") == 0 || strcmp(s, "--get-all") == 0 || strcmp(s, "--all") == 0) {
            get = 1;
        } else if (strcmp(s, "--get-regexp") == 0 || strcmp(s, "--regexp") == 0) {
            regexp = 1;
        } else if (strcmp(s, "--list") == 0 || strcmp(s, "-l") == 0) {
            list = 1;
        } else if (in_list(s, actions)) {
            other_action = 1;
        } else if (s[0] != '-' && npos < 3) {
            pos[npos++] = s;
        }
    }
    if (npos > 0 && in_list(pos[0], verbs)) {
        if (strcmp(pos[0], "get") == 0) {
            get = 1;
            key = npos > 1 ? pos[1] : NULL;
        } else if (strcmp(pos[0], "list") == 0) {
            list = 1;
        } else {
            return FL_OUT_NONE;
        }
    } else if (npos > 0 && (get || regexp)) {
        key = pos[0];
    } else if (npos == 1 && !list && !other_action) {
        key = pos[0]; /* implicit get: git config <name> */
        get = 1;
    }
    if (regexp) {
        return key != NULL && ci_contains(key, "remote") && ci_contains(key, "url") ? FL_OUT_REMOTE : FL_OUT_NONE;
    }
    if (list && !get) {
        return FL_OUT_REMOTE; /* only remote.*.url / pushurl values are rewritten */
    }
    if (key == NULL || other_action) {
        return FL_OUT_NONE;
    }
    if (key_is_remote_url(key, strlen(key))) {
        return FL_OUT_REMOTE;
    }
    return path_type || key_is_path(key) ? FL_OUT_PATHS_LINES : FL_OUT_NONE;
}

static enum fl_outplan detect_plan(const char *sub, int n, char **a)
{
    const char *first = NULL;
    int verbose = 0;
    for (int i = 0; i < n && first == NULL; i++) {
        if (strcmp(a[i], "--") == 0) {
            break;
        }
        if (a[i][0] != '-') {
            first = a[i];
        } else if (strcmp(a[i], "-v") == 0 || strcmp(a[i], "--verbose") == 0) {
            verbose = 1;
        }
    }
    if (strcmp(sub, "rev-parse") == 0) {
        return FL_OUT_PATHS_LINES;
    }
    if (strcmp(sub, "worktree") == 0) {
        return first != NULL && strcmp(first, "list") == 0 ? FL_OUT_WORKTREE : FL_OUT_NONE;
    }
    if (strcmp(sub, "remote") == 0) {
        if (first != NULL && strcmp(first, "get-url") == 0) {
            return FL_OUT_REMOTE;
        }
        return first == NULL && verbose ? FL_OUT_REMOTE : FL_OUT_NONE;
    }
    if (strcmp(sub, "ls-remote") == 0) {
        for (int i = 0; i < n && strcmp(a[i], "--") != 0; i++) {
            if (strcmp(a[i], "--get-url") == 0) {
                return FL_OUT_REMOTE;
            }
        }
        return FL_OUT_NONE;
    }
    if (strcmp(sub, "config") == 0) {
        return plan_config(n, a);
    }
    if (strcmp(sub, "for-each-ref") == 0 || strcmp(sub, "branch") == 0 || strcmp(sub, "tag") == 0) {
        for (int i = 0; i < n && strcmp(a[i], "--") != 0; i++) {
            const char *f = NULL;
            if (has_prefix(a[i], "--format=")) {
                f = a[i] + 9;
            } else if (strcmp(a[i], "--format") == 0 && i + 1 < n) {
                f = a[i + 1];
            }
            if (f != NULL && has_prefix(f, "%(worktreepath)")) {
                return FL_OUT_WORKTREE;
            }
        }
    }
    return FL_OUT_NONE;
}

int fl_translate_argv(const struct fl_xlate *x, int argc, char **argv, struct fl_strvec *out,
                      struct fl_cmdinfo *info)
{
    int i = 0;
    int sub_start;
    int positional = 0;
    int global_paths = 0;
    const char *sub = "";
    if (out != NULL) {
        out->n = 0;
        out->cap = 0;
        out->v = NULL;
    }
    if (info != NULL) {
        memset(info, 0, sizeof(*info));
    }
    if (x == NULL || out == NULL || info == NULL || argc < 0 || (argc > 0 && argv == NULL)) {
        return -1;
    }
    for (int k = 0; k < argc; k++) {
        if (argv[k] == NULL) {
            return -1;
        }
    }
    /* git's global options (git.c handle_options). */
    for (; i < argc; i++) {
        const char *a = argv[i];
        int r = 0;
        if (a[0] != '-') {
            break;
        }
        if (strcmp(a, "--version") == 0 || strcmp(a, "-v") == 0 || strcmp(a, "--help") == 0 ||
            strcmp(a, "-h") == 0) {
            /* git turns these into the "version" / "help" commands */
            sub = strcmp(a, "--version") == 0 || strcmp(a, "-v") == 0 ? "version" : "help";
            if (sv_add(out, a) != 0) {
                goto fail;
            }
            i++;
            break;
        }
        if (strcmp(a, "--") == 0) {
            if (sv_add(out, a) != 0) {
                goto fail;
            }
            i++;
            break;
        }
        if (strcmp(a, "-c") == 0 && i + 1 < argc) {
            r = push_config(x, out, a, argv[++i]);
        } else if ((strcmp(a, "-C") == 0 || strcmp(a, "--git-dir") == 0 || strcmp(a, "--work-tree") == 0) &&
                   i + 1 < argc) {
            r = sv_add(out, a);
            if (r == 0) {
                r = push_xl(x, out, argv[++i]);
            }
        } else if (has_prefix(a, "--git-dir=") || has_prefix(a, "--work-tree=")) {
            r = push_opt_value(x, out, a, (size_t)(strchr(a, '=') - a));
        } else if ((strcmp(a, "--namespace") == 0 || strcmp(a, "--super-prefix") == 0 ||
                    strcmp(a, "--attr-source") == 0 || strcmp(a, "--config-env") == 0 ||
                    strcmp(a, "--shallow-file") == 0) && i + 1 < argc) {
            r = sv_add(out, a);
            if (r == 0) {
                r = sv_add(out, argv[++i]);
            }
        } else if (has_prefix(a, "--exec-path=")) {
            /* Git for Windows' libexec is useless (and harmful) to native git. */
            r = fl_is_win_abs(a + 12) ? 0 : sv_add(out, a);
        } else {
            if (strcmp(a, "--exec-path") == 0 || strcmp(a, "--html-path") == 0 || strcmp(a, "--man-path") == 0 ||
                strcmp(a, "--info-path") == 0) {
                global_paths = 1;
            }
            r = sv_add(out, a);
        }
        if (r != 0) {
            goto fail;
        }
    }
    if (sub[0] == '\0' && i < argc) {
        sub = argv[i];
        if (sv_add(out, sub) != 0) {
            goto fail;
        }
        i++;
    }
    sub_start = i;
    for (; i < argc; i++) {
        const char *a = argv[i];
        int r;
        if (positional) {
            r = push_xl(x, out, a);
        } else if (strcmp(a, "--") == 0 || strcmp(a, "--end-of-options") == 0) {
            positional = 1;
            r = sv_add(out, a);
        } else if (strcmp(sub, "clone") == 0 && (strcmp(a, "-c") == 0 || strcmp(a, "--config") == 0) &&
                   i + 1 < argc) {
            r = push_config(x, out, a, argv[++i]);
        } else if (strcmp(sub, "clone") == 0 && has_prefix(a, "--config=")) {
            char *res = NULL;
            r = sanitize_kv_arg(x, a + 9, &res);
            if (r == 1) {
                r = sv_add(out, a);
            } else if (r == 2) {
                r = sv_take(out, join3("--config=", "", 0, res));
                free(res);
            }
        } else if (a[0] == '-' && a[1] == '-' && strchr(a, '=') != NULL) {
            size_t namelen = (size_t)(strchr(a, '=') - a);
            char *name = dupn(a, namelen);
            if (name == NULL) {
                goto fail;
            }
            r = in_list(name, k_path_eq_opts) ? push_opt_value(x, out, a, namelen) : sv_add(out, a);
            free(name);
        } else if (never_consumes(sub, a)) {
            r = sv_add(out, a);
            if (r == 0 && i + 1 < argc) {
                r = sv_add(out, argv[++i]);
            }
        } else if (in_list(a, k_path_next_opts) && i + 1 < argc && fl_is_win_abs(argv[i + 1])) {
            r = sv_add(out, a);
            if (r == 0) {
                r = push_xl(x, out, argv[++i]);
            }
        } else {
            r = push_xl(x, out, a);
        }
        if (r < 0) {
            goto fail;
        }
    }
    set_subcmd(info, sub);
    if (sub[0] != '\0') {
        info->out_plan = detect_plan(sub, argc - sub_start, argv + sub_start);
    } else if (global_paths) {
        info->out_plan = FL_OUT_PATHS_LINES;
    }
    info->stderr_paths = sub[0] != '\0' && strcmp(sub, "version") != 0 && strcmp(sub, "help") != 0;
    return 0;
fail:
    fl_strvec_free(out);
    memset(info, 0, sizeof(*info));
    return -1;
}

/* ======================================================================== */
/* Environment                                                               */
/* ======================================================================== */

static const char *const k_env_paths[] = {
    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_COMMON_DIR", "GIT_CONFIG",
    "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_TEMPLATE_DIR", NULL,
};

/* Unset when they point into a Git for Windows instance (a "gitInstance" path component). */
static const char *const k_env_bundled[] = { "GIT_CONFIG_SYSTEM", "GIT_TEMPLATE_DIR", NULL };

static const char *const k_env_lists[] = { "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_CEILING_DIRECTORIES", NULL };

static const char *const k_env_drop[] = {
    "GIT_EXEC_PATH", "MSYSTEM", "MSYS", "CHERE_INVOKING", "PLINK_PROTOCOL", "HOME", "PATH", "TEMP", "TMP",
    "FL_BRIDGE_TOKEN", NULL,
};

static const char *const k_env_drop_prefix[] = { "MSYS2_", "MINGW_", "MSYSTEM_", NULL };

static const char *const k_env_editors[] = { "GIT_EDITOR", "GIT_SEQUENCE_EDITOR", NULL };

static const char *const k_env_askpass[] = { "GIT_ASKPASS", "SSH_ASKPASS", NULL };

/* The canonical (table) spelling of name[0..n) when it is in list (case-insensitive), else NULL. */
static const char *name_in(const char *name, size_t n, const char *const *list)
{
    for (size_t i = 0; list[i] != NULL; i++) {
        if (ci_eqn(name, n, list[i])) {
            return list[i];
        }
    }
    return NULL;
}

static int name_has_prefix(const char *name, size_t n, const char *const *list)
{
    for (size_t i = 0; list[i] != NULL; i++) {
        size_t pl = strlen(list[i]);
        if (n > pl && ci_eqn(name, pl, list[i])) {
            return 1;
        }
    }
    return 0;
}

static int add_unset(struct fl_envops *ops, const char *name, size_t n)
{
    char *u = dupn(name, n);
    if (u == NULL) {
        return -1;
    }
    for (size_t i = 0; i < n; i++) {
        u[i] = to_upper(u[i]);
    }
    if (sv_contains(&ops->unset, u)) {
        free(u);
        return 0;
    }
    return sv_take(&ops->unset, u);
}

static int add_set(struct fl_envops *ops, const char *name, const char *value)
{
    struct sb b;
    sb_init(&b);
    sb_adds(&b, name);
    sb_addc(&b, '=');
    sb_adds(&b, value);
    return sv_take(&ops->set, sb_take(&b));
}

/* ';'-separated Windows list -> ':'-separated with Windows-absolute entries translated. */
static int xl_list(const struct fl_xlate *x, const char *v, char **res)
{
    struct sb b;
    size_t i = 0;
    int emitted = 0;
    *res = NULL;
    if (strchr(v, ';') == NULL && !fl_is_win_abs(v)) {
        return 0;
    }
    sb_init(&b);
    for (;;) {
        size_t s = i;
        char *item;
        char *t;
        int r;
        while (v[i] != '\0' && v[i] != ';') {
            i++;
        }
        item = dupn(v + s, i - s);
        if (item == NULL) {
            sb_free(&b);
            return -1;
        }
        r = xl_path(x, item, &t);
        if (r < 0) {
            free(item);
            sb_free(&b);
            return -1;
        }
        if (r == 1 || strchr(item, ':') == NULL) {
            /* An entry that stays Windows-looking ("D:\x" on an unmapped drive) would split
               into two bogus entries at its ':' in the Unix list, so it is left out. */
            if (emitted) {
                sb_addc(&b, ':');
            }
            sb_adds(&b, r == 1 ? t : item);
            emitted = 1;
        }
        free(t);
        free(item);
        if (v[i] == '\0') {
            break;
        }
        i++;
    }
    *res = sb_take(&b);
    return *res != NULL ? 1 : -1;
}

/* git's sq_dequote_step(): parse one '...' token at s[*pos]. 0 ok, 1 malformed. */
static int sq_step(const char *s, size_t *pos, struct sb *out)
{
    size_t i = *pos;
    if (s[i] != '\'') {
        return 1;
    }
    for (;;) {
        char c = s[++i];
        if (c == '\0') {
            return 1;
        }
        if (c != '\'') {
            sb_addc(out, c);
            continue;
        }
        if (s[i + 1] == '\\' && (s[i + 2] == '\'' || s[i + 2] == '!') && s[i + 3] == '\'') {
            sb_addc(out, s[i + 2]);
            i += 3;
            continue;
        }
        *pos = i + 1;
        return 0;
    }
}

static int is_space(char c)
{
    return c == ' ' || c == '\t' || c == '\n' || c == '\r' || c == '\v' || c == '\f';
}

/*
 * GIT_CONFIG_PARAMETERS ('key=value' old style or 'key'='value' new style, space-separated):
 * 1 rewritten into *res (new style; may be "" when everything was dropped), 0 keep (unchanged
 * or unparsable), -1 oom.
 */
static int xl_config_parameters(const struct fl_xlate *x, const char *v, char **res)
{
    struct sb outb;
    size_t pos = 0;
    int changed = 0;
    int first = 1;
    *res = NULL;
    sb_init(&outb);
    while (is_space(v[pos])) {
        pos++;
    }
    while (v[pos] != '\0') {
        struct sb kb;
        struct sb vb;
        const char *key;
        size_t keylen;
        const char *val = NULL;
        char *nv = NULL;
        int r;
        int has_val = 0;
        sb_init(&kb);
        sb_init(&vb);
        if (sq_step(v, &pos, &kb) != 0 || kb.oom) {
            goto bad;
        }
        if (v[pos] == '\0' || is_space(v[pos])) {
            /* old style 'key=value' or 'key' */
            const char *eq = kb.p != NULL ? strchr(kb.p, '=') : NULL;
            if (eq != NULL) {
                keylen = (size_t)(eq - kb.p);
                val = eq + 1;
                has_val = 1;
            } else {
                keylen = kb.len;
            }
        } else if (v[pos] == '=') {
            pos++;
            keylen = kb.len;
            if (v[pos] == '\'') {
                if (sq_step(v, &pos, &vb) != 0 || vb.oom || (v[pos] != '\0' && !is_space(v[pos]))) {
                    goto bad;
                }
                val = vb.p != NULL ? vb.p : "";
                has_val = 1;
            } else if (v[pos] != '\0' && !is_space(v[pos])) {
                goto bad;
            }
        } else {
            goto bad;
        }
        key = kb.p != NULL ? kb.p : "";
        r = sanitize_cfg(x, key, keylen, has_val ? val : NULL, &nv);
        if (r == CFG_ERR) {
            sb_free(&kb);
            sb_free(&vb);
            sb_free(&outb);
            return -1;
        }
        if (r != CFG_KEEP) {
            changed = 1;
        }
        if (r != CFG_DROP) {
            char *k = dupn(key, keylen);
            if (k == NULL) {
                free(nv);
                sb_free(&kb);
                sb_free(&vb);
                sb_free(&outb);
                return -1;
            }
            if (!first) {
                sb_addc(&outb, ' ');
            }
            first = 0;
            sb_add_gitsq(&outb, k);
            if (r == CFG_CHANGED || has_val) {
                sb_addc(&outb, '=');
                sb_add_gitsq(&outb, r == CFG_CHANGED ? nv : val);
            }
            free(k);
        }
        free(nv);
        sb_free(&kb);
        sb_free(&vb);
        while (is_space(v[pos])) {
            pos++;
        }
        continue;
    bad:
        sb_free(&kb);
        sb_free(&vb);
        sb_free(&outb);
        return 0; /* leave it to git to report */
    }
    if (!changed) {
        sb_free(&outb);
        return 0;
    }
    *res = sb_take(&outb);
    return *res != NULL ? 1 : -1;
}

/* GIT_CONFIG_COUNT / GIT_CONFIG_KEY_<n> / GIT_CONFIG_VALUE_<n>. */
#define CFG_GROUP_MAX 4096u

struct cfg_group {
    int valid;
    size_t count;
    const char **keys;
    const char **vals;
};

/* Parses a decimal without leading zeros (git prints "%d"); -1 if s[0..n) is not one. */
static long parse_index(const char *s, size_t n)
{
    long v = 0;
    if (n == 0 || n > 6 || (n > 1 && s[0] == '0')) {
        return -1;
    }
    for (size_t i = 0; i < n; i++) {
        if (!is_digit(s[i])) {
            return -1;
        }
        v = v * 10 + (long)(s[i] - '0');
    }
    return v;
}

static int cfg_group_scan(char **envp, struct cfg_group *g)
{
    const char *count = NULL;
    long c;
    memset(g, 0, sizeof(*g));
    for (size_t i = 0; envp[i] != NULL; i++) {
        const char *eq = strchr(envp[i], '=');
        if (eq != NULL && ci_eqn(envp[i], (size_t)(eq - envp[i]), "GIT_CONFIG_COUNT")) {
            count = eq + 1;
        }
    }
    if (count == NULL) {
        return 0;
    }
    c = parse_index(count, strlen(count));
    if (c < 0 || (unsigned long)c > CFG_GROUP_MAX) {
        return 0;
    }
    g->count = (size_t)c;
    g->keys = calloc(g->count + 1u, sizeof(char *));
    g->vals = calloc(g->count + 1u, sizeof(char *));
    if (g->keys == NULL || g->vals == NULL) {
        return -1;
    }
    for (size_t i = 0; envp[i] != NULL; i++) {
        const char *e = envp[i];
        const char *eq = strchr(e, '=');
        size_t nl;
        long idx;
        if (eq == NULL) {
            continue;
        }
        nl = (size_t)(eq - e);
        if (nl > 15 && ci_eqn(e, 15, "GIT_CONFIG_KEY_")) {
            idx = parse_index(e + 15, nl - 15);
            if (idx >= 0 && (size_t)idx < g->count) {
                g->keys[idx] = eq + 1;
            }
        } else if (nl > 17 && ci_eqn(e, 17, "GIT_CONFIG_VALUE_")) {
            idx = parse_index(e + 17, nl - 17);
            if (idx >= 0 && (size_t)idx < g->count) {
                g->vals[idx] = eq + 1;
            }
        }
    }
    g->valid = 1;
    for (size_t i = 0; i < g->count; i++) {
        if (g->keys[i] == NULL || g->vals[i] == NULL) {
            g->valid = 0;
        }
    }
    return 0;
}

static void cfg_group_free(struct cfg_group *g)
{
    free(g->keys);
    free(g->vals);
    memset(g, 0, sizeof(*g));
}

/* 1 when e[0..nl) is GIT_CONFIG_COUNT or a KEY_<n>/VALUE_<n> covered by the group. */
static int in_cfg_group(const struct cfg_group *g, const char *e, size_t nl)
{
    long idx = -1;
    if (!g->valid) {
        return 0;
    }
    if (ci_eqn(e, nl, "GIT_CONFIG_COUNT")) {
        return 1;
    }
    if (nl > 15 && ci_eqn(e, 15, "GIT_CONFIG_KEY_")) {
        idx = parse_index(e + 15, nl - 15);
    } else if (nl > 17 && ci_eqn(e, 17, "GIT_CONFIG_VALUE_")) {
        idx = parse_index(e + 17, nl - 17);
    }
    return idx >= 0 && (size_t)idx < g->count;
}

/* Decimal text of v (no leading zeros) into buf[32]. */
static void fmt_size(size_t v, char buf[32])
{
    char tmp[32];
    size_t len = 0;
    do {
        tmp[len++] = (char)('0' + (int)(v % 10u));
        v /= 10u;
    } while (v > 0 && len < sizeof(tmp));
    for (size_t j = 0; j < len; j++) {
        buf[j] = tmp[len - 1u - j];
    }
    buf[len] = '\0';
}

static int add_set_indexed(struct fl_envops *ops, const char *prefix, const char *num, const char *value)
{
    struct sb b;
    sb_init(&b);
    sb_adds(&b, prefix);
    sb_adds(&b, num);
    sb_addc(&b, '=');
    sb_adds(&b, value);
    return sv_take(&ops->set, sb_take(&b));
}

/* Sanitize the group and re-emit it densely renumbered (dropped entries close the gap). */
static int emit_cfg_group(const struct fl_xlate *x, const struct cfg_group *g, struct fl_envops *ops)
{
    size_t kept = 0;
    char num[32];
    for (size_t i = 0; i < g->count; i++) {
        char *nv = NULL;
        int r = sanitize_cfg(x, g->keys[i], strlen(g->keys[i]), g->vals[i], &nv);
        if (r == CFG_ERR) {
            return -1;
        }
        if (r == CFG_DROP) {
            continue;
        }
        fmt_size(kept, num);
        if (add_set_indexed(ops, "GIT_CONFIG_KEY_", num, g->keys[i]) != 0 ||
            add_set_indexed(ops, "GIT_CONFIG_VALUE_", num, r == CFG_CHANGED ? nv : g->vals[i]) != 0) {
            free(nv);
            return -1;
        }
        free(nv);
        kept++;
    }
    fmt_size(kept, num);
    return add_set(ops, "GIT_CONFIG_COUNT", num);
}

int fl_translate_env(const struct fl_xlate *x, char **envp, struct fl_envops *ops)
{
    struct cfg_group g;
    int ssh_dropped = 0;
    int target_git = 0;
    int target_ssh = 0;
    if (ops == NULL) {
        return -1;
    }
    memset(ops, 0, sizeof(*ops));
    if (x == NULL) {
        return -1;
    }
    if (envp == NULL) {
        return 0;
    }
    if (cfg_group_scan(envp, &g) != 0) {
        cfg_group_free(&g);
        return -1;
    }
    for (size_t i = 0; envp[i] != NULL; i++) {
        const char *eq = strchr(envp[i], '=');
        size_t nl = eq != NULL ? (size_t)(eq - envp[i]) : 0;
        if (eq == NULL || !is_win_program(eq + 1)) {
            continue;
        }
        if (ci_eqn(envp[i], nl, "GIT_SSH")) {
            ssh_dropped = 1;
        } else if (ci_eqn(envp[i], nl, "GIT_ASKPASS") && have(x->askpass)) {
            target_git = 1;
        } else if (ci_eqn(envp[i], nl, "SSH_ASKPASS") && have(x->ssh_askpass)) {
            target_ssh = 1;
        }
    }
    for (size_t i = 0; envp[i] != NULL; i++) {
        const char *e = envp[i];
        const char *eq = strchr(e, '=');
        const char *v;
        const char *canon;
        size_t nl;
        char *t = NULL;
        int r = 0;
        if (eq == NULL || eq == e) {
            continue;
        }
        nl = (size_t)(eq - e);
        v = eq + 1;
        if (name_in(e, nl, k_env_drop) != NULL || name_has_prefix(e, nl, k_env_drop_prefix)) {
            r = add_unset(ops, e, nl);
        } else if (ci_eqn(e, nl, "FL_ASKPASS_TARGET") || ci_eqn(e, nl, "FL_SSH_ASKPASS_TARGET")) {
            /* Only ever the personas' input, written below; an inherited one must neither
               duplicate nor (when no persona is used) linger. */
            int git = ci_eqn(e, nl, "FL_ASKPASS_TARGET");
            r = (git ? target_git : target_ssh) ? 0 : add_unset(ops, e, nl);
        } else if (in_cfg_group(&g, e, nl)) {
            if (ci_eqn(e, nl, "GIT_CONFIG_COUNT")) {
                r = emit_cfg_group(x, &g, ops);
            }
        } else if (name_in(e, nl, k_env_bundled) != NULL && fl_is_win_abs(v) &&
                   has_component_ci(v, "gitinstance")) {
            /* Fork exports GIT_CONFIG_SYSTEM=C:/.../Fork/gitInstance/<ver>/etc/gitconfig
               (B3 corpus): Git for Windows' system config (autocrlf=true, symlinks=false,
               credential.helper=manager, schannel) must not reach native git. */
            r = add_unset(ops, e, nl);
        } else if ((canon = name_in(e, nl, k_env_paths)) != NULL) {
            r = xl_path(x, v, &t);
            r = r == 1 ? add_set(ops, canon, t) : (r == 0 ? sv_add(&ops->set, e) : -1);
        } else if ((canon = name_in(e, nl, k_env_lists)) != NULL) {
            r = xl_list(x, v, &t);
            r = r == 1 ? add_set(ops, canon, t) : (r == 0 ? sv_add(&ops->set, e) : -1);
        } else if ((canon = name_in(e, nl, k_env_editors)) != NULL) {
            r = v[0] != '\0' ? rewrite_editor(x, v, &t) : 0;
            r = r == 1 ? add_set(ops, canon, t) : (r == 0 ? sv_add(&ops->set, e) : -1);
        } else if ((canon = name_in(e, nl, k_env_askpass)) != NULL) {
            int git = strcmp(canon, "GIT_ASKPASS") == 0;
            const char *persona = git ? x->askpass : x->ssh_askpass;
            if (!is_win_program(v)) {
                r = sv_add(&ops->set, e);
            } else if (!have(persona)) {
                r = add_unset(ops, e, nl);
            } else {
                r = add_set(ops, canon, persona);
                if (r == 0) {
                    r = add_set(ops, git ? "FL_ASKPASS_TARGET" : "FL_SSH_ASKPASS_TARGET", v);
                }
            }
        } else if (ci_eqn(e, nl, "GIT_SSH")) {
            r = is_win_program(v) ? add_unset(ops, e, nl) : sv_add(&ops->set, e);
        } else if (ci_eqn(e, nl, "GIT_SSH_VARIANT")) {
            r = ssh_dropped || is_plink_variant(v) ? add_unset(ops, e, nl) : sv_add(&ops->set, e);
        } else if (ci_eqn(e, nl, "GIT_SSH_COMMAND")) {
            r = v[0] != '\0' ? rewrite_ssh(x, v, &t) : 0;
            r = r == 1 ? add_set(ops, "GIT_SSH_COMMAND", t) : (r == 0 ? sv_add(&ops->set, e) : -1);
        } else if (ci_eqn(e, nl, "GIT_CONFIG_PARAMETERS")) {
            r = xl_config_parameters(x, v, &t);
            if (r == 1 && t[0] == '\0') {
                r = add_unset(ops, e, nl);
            } else {
                r = r == 1 ? add_set(ops, "GIT_CONFIG_PARAMETERS", t) : (r == 0 ? sv_add(&ops->set, e) : -1);
            }
        } else {
            r = sv_add(&ops->set, e);
        }
        free(t);
        if (r != 0) {
            cfg_group_free(&g);
            fl_strvec_free(&ops->set);
            fl_strvec_free(&ops->unset);
            return -1;
        }
    }
    cfg_group_free(&g);
    return 0;
}

/* ======================================================================== */
/* Output                                                                    */
/* ======================================================================== */

void fl_out_init(struct fl_out *o, const struct fl_xlate *x, enum fl_outplan plan, int nul_records)
{
    if (o == NULL) {
        return;
    }
    memset(o, 0, sizeof(*o));
    o->x = x;
    o->plan = ((int)plan >= (int)FL_OUT_NONE && (int)plan <= (int)FL_OUT_SHOW_ORIGIN) ? plan : FL_OUT_NONE;
    o->nul_records = nul_records != 0;
}

int fl_out_add_anchor(struct fl_out *o, const char *unix_path, const char *win_path)
{
    struct sb w;
    const char *p;
    size_t ul;
    char *u;
    char *wp;
    if (o == NULL || unix_path == NULL || win_path == NULL || unix_path[0] != '/') {
        return -1;
    }
    p = win_path;
    if (verbatim(p)) {
        p += 4;
    }
    if (!(is_alpha(p[0]) && p[1] == ':' && (p[2] == '\0' || is_sep(p[2])))) {
        return -1;
    }
    sb_init(&w);
    sb_addc(&w, to_upper(p[0]));
    sb_addc(&w, ':');
    add_collapsed(&w, p + 2, '/');
    while (!w.oom && w.len > 2 && w.p[w.len - 1u] == '/') {
        w.p[--w.len] = '\0';
    }
    wp = sb_take(&w);
    ul = strlen(unix_path);
    while (ul > 0 && unix_path[ul - 1u] == '/') {
        ul--;
    }
    u = dupn(unix_path, ul);
    if (wp == NULL || u == NULL) {
        free(wp);
        free(u);
        return -1;
    }
    if (o->nanchors == o->anchors_cap) {
        size_t cap = o->anchors_cap ? o->anchors_cap * 2u : 8u;
        struct fl_anchor *na;
        if (cap > ((size_t)-1) / sizeof(struct fl_anchor)) {
            free(wp);
            free(u);
            return -1;
        }
        na = realloc(o->anchors, cap * sizeof(struct fl_anchor));
        if (na == NULL) {
            free(wp);
            free(u);
            return -1;
        }
        o->anchors = na;
        o->anchors_cap = cap;
    }
    o->anchors[o->nanchors].unix_path = u;
    o->anchors[o->nanchors].win_path = wp;
    o->nanchors++;
    return 0;
}

/* Append the Windows form of the Unix path p[0..n) (p[0] == '/', no NUL inside). */
static void out_path(const struct fl_out *o, const char *p, size_t n, struct sb *dst)
{
    const struct fl_anchor *best = NULL;
    size_t bestlen = 0;
    size_t start = dst->len;
    for (size_t i = 0; i < o->nanchors; i++) {
        const struct fl_anchor *a = &o->anchors[i];
        size_t ul = strlen(a->unix_path);
        if (ul <= n && memcmp(p, a->unix_path, ul) == 0 && (ul == n || p[ul] == '/') &&
            (best == NULL || ul > bestlen)) {
            best = a;
            bestlen = ul;
        }
    }
    if (best != NULL) {
        sb_adds(dst, best->win_path);
        sb_addn(dst, p + bestlen, n - bestlen);
    } else if (o->x != NULL && o->x->to_win != NULL) {
        char *in = dupn(p, n);
        char *buf = in != NULL ? malloc(XPATH_MAX) : NULL;
        int ok = 0;
        if (buf != NULL) {
            buf[0] = '\0';
            if (o->x->to_win(o->x->ud, in, buf, XPATH_MAX) == 0 && memchr(buf, '\0', XPATH_MAX) != NULL) {
                const char *q = buf;
                if (verbatim(q) && is_alpha(q[4]) && q[5] == ':') {
                    q += 4;
                }
                if (is_alpha(q[0]) && q[1] == ':' && (q[2] == '\0' || is_sep(q[2]))) {
                    /* Keep the caller's own bytes for the tail the callback merely copied, so a
                       literal '\' in a Unix name is not turned into '/'. */
                    size_t i = strlen(q);
                    size_t j = n;
                    while (i > 2 && j > 0 && (q[i - 1u] == p[j - 1u] || (is_sep(q[i - 1u]) && p[j - 1u] == '/'))) {
                        i--;
                        j--;
                    }
                    sb_addc(dst, to_upper(q[0]));
                    sb_addc(dst, ':');
                    for (size_t k = 2; k < i; k++) {
                        sb_addc(dst, q[k] == '\\' ? '/' : q[k]);
                    }
                    sb_addn(dst, p + j, n - j);
                    ok = 1;
                }
            }
        }
        free(buf);
        free(in);
        if (!ok) {
            sb_adds(dst, "Z:");
            sb_addn(dst, p, n);
        }
    } else {
        sb_adds(dst, "Z:");
        sb_addn(dst, p, n);
    }
    if (!dst->oom && dst->len == start + 2u) {
        sb_addc(dst, '/'); /* a drive root: "C:" -> "C:/" */
    }
}

static int abs_path_ok(const char *p, size_t n)
{
    return n > 0 && p[0] == '/' && memchr(p, '\0', n) == NULL;
}

/* rev-parse & stderr: a whole-line path, else every '<abs path>' quoted inside the line. */
static int tr_paths_lines(const struct fl_out *o, const char *rec, size_t n, struct sb *dst)
{
    size_t last = 0;
    int any = 0;
    if (abs_path_ok(rec, n)) {
        out_path(o, rec, n, dst);
        return 1;
    }
    for (size_t i = 0; i + 1u < n; i++) {
        const char *close;
        size_t c;
        if (rec[i] != '\'' || rec[i + 1u] != '/') {
            continue;
        }
        close = memchr(rec + i + 1u, '\'', n - i - 1u);
        if (close == NULL) {
            break;
        }
        c = (size_t)(close - rec);
        if (!abs_path_ok(rec + i + 1u, c - i - 1u)) {
            continue;
        }
        sb_addn(dst, rec + last, i + 1u - last);
        out_path(o, rec + i + 1u, c - i - 1u, dst);
        last = c;
        any = 1;
        i = c;
    }
    if (!any) {
        return 0;
    }
    sb_addn(dst, rec + last, n - last);
    return 1;
}

/* Default `git worktree list` line: "<path><spaces><hash> [branch]" / "(detached HEAD)" / "(bare)". */
static size_t wt_default_split(const char *rec, size_t n)
{
    for (size_t i = n - 1u; i > 0; i--) {
        size_t j;
        size_t k;
        if (rec[i] != ' ' || rec[i - 1u] == ' ') {
            continue;
        }
        j = i;
        while (j < n && rec[j] == ' ') {
            j++;
        }
        if (n - j >= 6u && memcmp(rec + j, "(bare)", 6) == 0) {
            return i;
        }
        k = j;
        while (k < n && is_hex_lower(rec[k])) {
            k++;
        }
        if (k - j >= 4u && k < n && rec[k] == ' ') {
            while (k < n && rec[k] == ' ') {
                k++;
            }
            if (k < n && (rec[k] == '[' || rec[k] == '(')) {
                return i;
            }
        }
    }
    return 0;
}

static int tr_quoted_path(const struct fl_out *o, const char *p, size_t n, size_t *consumed, struct sb *dst);

static int tr_worktree(const struct fl_out *o, const char *rec, size_t n, struct sb *dst)
{
    size_t end;
    if (n > 9u && memcmp(rec, "worktree ", 9) == 0 && abs_path_ok(rec + 9, n - 9u)) {
        sb_addn(dst, rec, 9);
        out_path(o, rec + 9, n - 9u, dst);
        return 1;
    }
    if (n > 1u && rec[0] == '"' && rec[1] == '/') {
        /* core.quotePath: a path with non-ASCII bytes, '"', '\\' or control characters is
           C-quoted; Git for Windows prints "Z:/..." inside the same quotes. */
        struct sb tmp;
        size_t used = 0;
        int ok;
        end = wt_default_split(rec, n);
        sb_init(&tmp);
        ok = tr_quoted_path(o, rec, end != 0 ? end : n, &used, &tmp) && !tmp.oom &&
             (end != 0 ? used == end : (used == n || rec[used] == '\t'));
        if (ok) {
            sb_addn(dst, tmp.p, tmp.len);
            sb_addn(dst, rec + used, n - used);
        }
        sb_free(&tmp);
        return ok;
    }
    if (!(n > 0 && rec[0] == '/')) {
        return 0;
    }
    end = wt_default_split(rec, n);
    if (end == 0) {
        const char *tab = memchr(rec, '\t', n);
        end = tab != NULL ? (size_t)(tab - rec) : n;
    }
    if (!abs_path_ok(rec, end)) {
        return 0;
    }
    out_path(o, rec, end, dst);
    sb_addn(dst, rec + end, n - end);
    return 1;
}

/* A remote URL: a Unix path or file:///path. 1 appended, 0 not a local path. */
static int tr_url(const struct fl_out *o, const char *p, size_t n, struct sb *dst)
{
    if (abs_path_ok(p, n)) {
        out_path(o, p, n, dst);
        return 1;
    }
    if (n > 8u && ci_eqn(p, 7, "file://") && abs_path_ok(p + 7, n - 7u)) {
        sb_adds(dst, "file:///");
        out_path(o, p + 7, n - 7u, dst);
        return 1;
    }
    return 0;
}

static int tr_remote(const struct fl_out *o, const char *rec, size_t n, struct sb *dst)
{
    const char *tab = memchr(rec, '\t', n);
    size_t us;
    size_t ue;
    if (tab != NULL) {
        /* remote -v: "name<TAB>url (fetch)" [+ " [filter]"] */
        us = (size_t)(tab - rec) + 1u;
        ue = n;
        for (size_t i = n; i > us; i--) {
            size_t p = i - 1u;
            if ((n - p >= 8u && memcmp(rec + p, " (fetch)", 8) == 0) ||
                (n - p >= 7u && memcmp(rec + p, " (push)", 7) == 0)) {
                ue = p;
                break;
            }
        }
    } else if (n > 7u && ci_eqn(rec, 7, "remote.")) {
        /* config --list / --get-regexp: "remote.<name>.url=<url>", "... <url>", -z "...\n<url>" */
        size_t k = 0;
        while (k < n && rec[k] != '=' && rec[k] != ' ' && rec[k] != '\n') {
            k++;
        }
        if (k == n || !key_is_remote_url(rec, k)) {
            return 0;
        }
        us = k + 1u;
        ue = n;
    } else {
        us = 0;
        ue = n;
    }
    sb_addn(dst, rec, us);
    if (!tr_url(o, rec + us, ue - us, dst)) {
        return 0;
    }
    sb_addn(dst, rec + ue, n - ue);
    return 1;
}

/* git's quote_c_style() body, without the surrounding quotes. raw_high: keep bytes >= 0x80 as
   they are (core.quotePath=false), else octal-escape them (the default). */
static void cq_quote(struct sb *dst, const char *s, size_t n, int raw_high)
{
    static const char esc[] = "abtnvfr";
    for (size_t i = 0; i < n; i++) {
        unsigned char c = (unsigned char)s[i];
        if (c >= 7u && c <= 13u) {
            sb_addc(dst, '\\');
            sb_addc(dst, esc[c - 7u]);
        } else if (c == '"' || c == '\\') {
            sb_addc(dst, '\\');
            sb_addc(dst, (char)c);
        } else if (raw_high && c >= 0x80u) {
            sb_addc(dst, (char)c);
        } else if (c < 0x20u || c >= 0x7fu) {
            sb_addc(dst, '\\');
            sb_addc(dst, (char)('0' + ((c >> 6) & 7u)));
            sb_addc(dst, (char)('0' + ((c >> 3) & 7u)));
            sb_addc(dst, (char)('0' + (c & 7u)));
        } else {
            sb_addc(dst, (char)c);
        }
    }
}

/* Unquote a C-style quoted string starting at p[0] == '"'. 0 ok (*end after the closing quote). */
static int cq_unquote(const char *p, size_t n, size_t *end, struct sb *dst)
{
    size_t i = 1;
    while (i < n) {
        char c = p[i];
        if (c == '"') {
            *end = i + 1u;
            return 0;
        }
        if (c != '\\') {
            sb_addc(dst, c);
            i++;
            continue;
        }
        if (i + 1u >= n) {
            return -1;
        }
        c = p[i + 1u];
        if (c >= '0' && c <= '3') {
            unsigned v;
            if (i + 3u >= n || p[i + 2u] < '0' || p[i + 2u] > '7' || p[i + 3u] < '0' || p[i + 3u] > '7') {
                return -1;
            }
            v = ((unsigned)(c - '0') << 6) | ((unsigned)(p[i + 2u] - '0') << 3) | (unsigned)(p[i + 3u] - '0');
            sb_addc(dst, (char)(unsigned char)v);
            i += 4;
            continue;
        }
        switch (c) {
        case 'a':
            sb_addc(dst, '\a');
            break;
        case 'b':
            sb_addc(dst, '\b');
            break;
        case 't':
            sb_addc(dst, '\t');
            break;
        case 'n':
            sb_addc(dst, '\n');
            break;
        case 'v':
            sb_addc(dst, '\v');
            break;
        case 'f':
            sb_addc(dst, '\f');
            break;
        case 'r':
            sb_addc(dst, '\r');
            break;
        case '"':
        case '\\':
            sb_addc(dst, c);
            break;
        default:
            return -1;
        }
        i += 2;
    }
    return -1;
}

/* p[0] == '"': a C-quoted absolute Unix path. Appends the quoted Windows form (quoted the same
   way: raw or octal-escaped bytes >= 0x80, as the input had them) and sets *consumed to the
   length up to and including the closing quote. 1 appended, 0 not a quoted absolute path. */
static int tr_quoted_path(const struct fl_out *o, const char *p, size_t n, size_t *consumed, struct sb *dst)
{
    struct sb path;
    struct sb win;
    size_t end = 0;
    int ok;
    sb_init(&path);
    sb_init(&win);
    ok = n > 0 && p[0] == '"' && cq_unquote(p, n, &end, &path) == 0 && !path.oom && abs_path_ok(path.p, path.len);
    if (ok) {
        int raw_high = 0;
        for (size_t i = 1; i + 1u < end; i++) {
            if ((unsigned char)p[i] >= 0x80u) {
                raw_high = 1;
                break;
            }
        }
        out_path(o, path.p, path.len, &win);
        sb_addc(dst, '"');
        cq_quote(dst, win.p != NULL ? win.p : "", win.len, raw_high);
        sb_addc(dst, '"');
        *consumed = end;
    }
    sb_free(&path);
    sb_free(&win);
    return ok;
}

static int tr_show_origin(const struct fl_out *o, const char *rec, size_t n, struct sb *dst)
{
    static const char *const scopes[] = { "system", "global", "local", "worktree", "command", "submodule",
                                          "unknown", NULL };
    size_t off = 0;
    size_t p;
    if (!(n >= 5u && memcmp(rec, "file:", 5) == 0)) {
        const char *tab = memchr(rec, '\t', n);
        if (tab == NULL || name_in(rec, (size_t)(tab - rec), scopes) == NULL) {
            return 0;
        }
        off = (size_t)(tab - rec) + 1u;
        if (!(n - off >= 5u && memcmp(rec + off, "file:", 5) == 0)) {
            return 0;
        }
    }
    p = off + 5u;
    if (p < n && rec[p] == '"') {
        struct sb q;
        size_t end = 0;
        int ok;
        sb_init(&q);
        ok = tr_quoted_path(o, rec + p, n - p, &end, &q) && !q.oom;
        if (ok) {
            sb_addn(dst, rec, p);
            sb_addn(dst, q.p, q.len);
            sb_addn(dst, rec + p + end, n - p - end);
        }
        sb_free(&q);
        return ok;
    }
    {
        const char *tab = memchr(rec + p, '\t', n - p);
        size_t end = tab != NULL ? (size_t)(tab - rec) : n;
        if (!abs_path_ok(rec + p, end - p)) {
            return 0;
        }
        sb_addn(dst, rec, p);
        out_path(o, rec + p, end - p, dst);
        sb_addn(dst, rec + end, n - end);
        return 1;
    }
}

static int translate_record(const struct fl_out *o, const char *rec, size_t n, struct sb *dst)
{
    switch (o->plan) {
    case FL_OUT_PATHS_LINES:
        return tr_paths_lines(o, rec, n, dst);
    case FL_OUT_WORKTREE:
        return tr_worktree(o, rec, n, dst);
    case FL_OUT_REMOTE:
        return tr_remote(o, rec, n, dst);
    case FL_OUT_SHOW_ORIGIN:
        return tr_show_origin(o, rec, n, dst);
    case FL_OUT_NONE:
    default:
        return 0;
    }
}

/*
 * Emit one record (translated when the plan applies) plus its terminator. `contiguous` says
 * rec[n] is the terminator in the caller's buffer, so a raw record goes out in one call.
 */
static void emit_record(const struct fl_out *o, const char *rec, size_t n, int has_term, char term, int contiguous,
                        fl_emit_fn emit, void *ud)
{
    struct sb t;
    sb_init(&t);
    if (translate_record(o, rec, n, &t) == 1) {
        if (has_term) {
            sb_addc(&t, term);
        }
        if (!t.oom) {
            emit(ud, t.p, t.len);
            sb_free(&t);
            return;
        }
    }
    sb_free(&t);
    if (has_term && contiguous) {
        emit(ud, rec, n + 1u);
        return;
    }
    if (n > 0) {
        emit(ud, rec, n);
    }
    if (has_term) {
        emit(ud, &term, 1);
    }
}

static size_t find_term(const struct fl_out *o, const char *d, size_t n)
{
    const char *p;
    if (o->nul_records) {
        p = memchr(d, '\0', n);
        return p != NULL ? (size_t)(p - d) : n;
    }
    if (o->plan == FL_OUT_PATHS_LINES) {
        for (size_t i = 0; i < n; i++) {
            if (d[i] == '\n' || d[i] == '\r') {
                return i;
            }
        }
        return n;
    }
    p = memchr(d, '\n', n);
    return p != NULL ? (size_t)(p - d) : n;
}

static int buf_append(struct fl_out *o, const char *d, size_t n)
{
    if (n == 0) {
        return 0;
    }
    if (o->len + n > o->cap) {
        size_t cap = o->cap ? o->cap : 256u;
        char *nb;
        while (cap < o->len + n) {
            cap *= 2u;
        }
        nb = realloc(o->buf, cap);
        if (nb == NULL) {
            return -1;
        }
        o->buf = nb;
        o->cap = cap;
    }
    memcpy(o->buf + o->len, d, n);
    o->len += n;
    return 0;
}

/* Out of memory: give up translating, flush what is pending raw. */
static void go_raw(struct fl_out *o, fl_emit_fn emit, void *ud)
{
    o->failed = 1;
    if (o->len > 0) {
        emit(ud, o->buf, o->len);
        o->len = 0;
    }
}

void fl_out_feed(struct fl_out *o, const char *data, size_t n, fl_emit_fn emit, void *ud)
{
    size_t i = 0;
    if (o == NULL || emit == NULL || data == NULL || n == 0) {
        return;
    }
    if (o->plan == FL_OUT_NONE || o->failed) {
        if (o->len > 0) {
            emit(ud, o->buf, o->len);
            o->len = 0;
        }
        emit(ud, data, n);
        return;
    }
    while (i < n) {
        size_t t;
        if (o->failed) {
            emit(ud, data + i, n - i); /* out of memory earlier in this chunk: raw from here */
            return;
        }
        t = find_term(o, data + i, n - i);
        int found = t < n - i;
        size_t whole = found ? t + 1u : n - i;
        if (o->skipping) {
            emit(ud, data + i, whole);
            o->skipping = !found;
            i += whole;
            continue;
        }
        if (t > FL_OUT_MAX_RECORD - o->len) {
            /* over-long record: everything up to its terminator passes through untranslated */
            if (o->len > 0) {
                emit(ud, o->buf, o->len);
                o->len = 0;
            }
            emit(ud, data + i, whole);
            o->skipping = !found;
            i += whole;
            continue;
        }
        if (!found) {
            if (buf_append(o, data + i, t) != 0) {
                go_raw(o, emit, ud);
                emit(ud, data + i, t);
            }
            return;
        }
        if (o->len == 0) {
            emit_record(o, data + i, t, 1, data[i + t], 1, emit, ud);
        } else if (buf_append(o, data + i, t) != 0) {
            go_raw(o, emit, ud);
            emit(ud, data + i, whole);
        } else {
            emit_record(o, o->buf, o->len, 1, data[i + t], 0, emit, ud);
            o->len = 0;
        }
        i += whole;
    }
}

void fl_out_flush(struct fl_out *o, fl_emit_fn emit, void *ud)
{
    if (o == NULL || emit == NULL) {
        return;
    }
    if (o->len > 0) {
        if (o->failed || o->plan == FL_OUT_NONE) {
            emit(ud, o->buf, o->len);
        } else {
            emit_record(o, o->buf, o->len, 0, '\0', 0, emit, ud);
        }
        o->len = 0;
    }
    o->skipping = 0;
}

void fl_out_free(struct fl_out *o)
{
    if (o == NULL) {
        return;
    }
    free(o->buf);
    for (size_t i = 0; i < o->nanchors; i++) {
        free(o->anchors[i].unix_path);
        free(o->anchors[i].win_path);
    }
    free(o->anchors);
    memset(o, 0, sizeof(*o));
}
