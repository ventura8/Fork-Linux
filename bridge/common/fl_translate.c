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
static int shell_safe(const char *s)
{
    if (s[0] == '\0') {
        return 0;
    }
    for (const char *p = s; *p != '\0'; p++) {
        if (!is_alpha(*p) && !is_digit(*p) && strchr("_@%+=:,./-", *p) == NULL) {
            return 0;
        }
    }
    return 1;
}

static void sb_add_word(struct sb *b, const char *s)
{
    if (shell_safe(s)) {
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

/*
 * The predicates below take n = strlen(s) (the caller's known length) and check it before
 * reading a byte, so they never look past the terminator of a short string.
 */

/* "X:\" or "X:/" at s. */
static int drive_abs(const char *s, size_t n)
{
    return n >= 3u && is_alpha(s[0]) && s[1] == ':' && is_sep(s[2]);
}

/* "\\?\" at s. */
static int verbatim(const char *s, size_t n)
{
    return n >= 4u && s[0] == '\\' && s[1] == '\\' && s[2] == '?' && s[3] == '\\';
}

/* "unix\" or "unix/" at s (after "\\?\"). */
static int unix_ns(const char *s, size_t n)
{
    return n >= 5u && ci_eqn(s, 4u, "unix") && is_sep(s[4]);
}

/* A drive path, possibly behind "\\?\" (not a URL, not the unix namespace). */
static int win_drive_path(const char *s)
{
    size_t n = strlen(s);
    return drive_abs(s, n) || (verbatim(s, n) && drive_abs(s + 4, n - 4u));
}

/* "X:" alone or followed by a separator ("X:\...", "X:/..."); n == strlen(s). */
static int drive_or_root(const char *s, size_t n)
{
    return n >= 2u && is_alpha(s[0]) && s[1] == ':' && (n == 2u || is_sep(s[2]));
}

/* Length of a "file://" prefix (any case) plus one optional extra '/'; 0 when s has none. */
static size_t file_url_skip(const char *s, size_t n)
{
    if (n < 7u || !ci_eqn(s, 7u, "file://")) {
        return 0;
    }
    return n > 7u && s[7] == '/' ? 8u : 7u;
}

static int is_win_abs_n(const char *s, size_t n)
{
    size_t k;
    if (drive_abs(s, n)) {
        return 1;
    }
    if (verbatim(s, n)) {
        return drive_abs(s + 4, n - 4u) || unix_ns(s + 4, n - 4u);
    }
    k = file_url_skip(s, n);
    return k != 0 && drive_abs(s + k, n - k);
}

int fl_is_win_abs(const char *s)
{
    return s != NULL && is_win_abs_n(s, strlen(s));
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

/* "\\?\unix\<rest>": rest (starting at its separator) with '/' separators, no callback. */
static int unix_ns_to_out(const char *rest, char *out, size_t outsz)
{
    struct sb norm;
    int rc = -1;
    sb_init(&norm);
    add_collapsed(&norm, rest, '/');
    if (!norm.oom && norm.p != NULL && norm.len < outsz) {
        memcpy(out, norm.p, norm.len + 1u);
        rc = 0;
    }
    sb_free(&norm);
    return rc;
}

/* scheme + the callback's result, when it is an absolute Unix path that fits in outsz. */
static int copy_unix_result(const char *buf, size_t bufsz, const char *scheme, char *out, size_t outsz)
{
    size_t sl;
    size_t n;
    if (memchr(buf, '\0', bufsz) == NULL || buf[0] != '/') {
        return -1;
    }
    sl = strlen(scheme);
    n = strlen(buf);
    if (sl + n + 1u > outsz) {
        return -1;
    }
    memcpy(out, scheme, sl);
    memcpy(out + sl, buf, n + 1u);
    return 0;
}

/* The drive path p ("X:\..." after any prefix) normalised and mapped through x->to_unix. */
static int drive_to_unix(const struct fl_xlate *x, const char *p, const char *scheme, char *out, size_t outsz)
{
    struct sb norm;
    char *buf;
    int rc = -1;
    if (x->to_unix == NULL) {
        return -1;
    }
    sb_init(&norm);
    sb_addc(&norm, to_upper(p[0]));
    sb_addc(&norm, ':');
    add_collapsed(&norm, p + 2, '\\');
    buf = norm.oom ? NULL : malloc(XPATH_MAX);
    if (buf != NULL) {
        buf[0] = '\0';
        if (x->to_unix(x->ud, norm.p, buf, XPATH_MAX) == 0) {
            rc = copy_unix_result(buf, XPATH_MAX, scheme, out, outsz);
        }
        free(buf);
    }
    sb_free(&norm);
    return rc;
}

/* fl_win_to_unix() for in[0..n) (n == strlen(in)); x, in and out are valid, outsz > 0. */
static int win_to_unix_n(const struct fl_xlate *x, const char *in, size_t n, char *out, size_t outsz)
{
    size_t k = file_url_skip(in, n);
    int rc = -1;
    if (k != 0) {
        if (drive_abs(in + k, n - k)) {
            rc = drive_to_unix(x, in + k, "file://", out, outsz);
        }
    } else if (verbatim(in, n)) {
        if (unix_ns(in + 4, n - 4u)) {
            rc = unix_ns_to_out(in + 8, out, outsz);
        } else if (drive_abs(in + 4, n - 4u)) {
            rc = drive_to_unix(x, in + 4, "", out, outsz);
        }
    } else if (drive_abs(in, n)) {
        rc = drive_to_unix(x, in, "", out, outsz);
    }
    if (rc != 0) {
        out[0] = '\0';
    }
    return rc;
}

int fl_win_to_unix(const struct fl_xlate *x, const char *in, char *out, size_t outsz)
{
    if (out != NULL && outsz > 0) {
        out[0] = '\0';
    }
    if (x == NULL || in == NULL || out == NULL || outsz == 0) {
        return -1;
    }
    return win_to_unix_n(x, in, strlen(in), out, outsz);
}

/* 1: *res is the malloc'd Unix form of s[0..n) (n == strlen(s)); 0: not a mappable Windows
   path; -1: oom. */
static int xl_path_n(const struct fl_xlate *x, const char *s, size_t n, char **res)
{
    char *buf;
    *res = NULL;
    if (!is_win_abs_n(s, n)) {
        return 0;
    }
    buf = malloc(XPATH_MAX);
    if (buf == NULL) {
        return -1;
    }
    if (win_to_unix_n(x, s, n, buf, XPATH_MAX) != 0) {
        free(buf);
        return 0;
    }
    *res = dup(buf);
    free(buf);
    return *res != NULL ? 1 : -1;
}

static int xl_path(const struct fl_xlate *x, const char *s, char **res)
{
    return xl_path_n(x, s, strlen(s), res);
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

/* Lexer state of sh_split(): the source, the read position and the word being built. */
struct shlex {
    const char *s;
    size_t i;
    struct sb v;
    int dyn;
};

enum { SH_END = 2 }; /* sh_next(): no more words */

/* '...' at l->i: 0 ok, 1 unbalanced. */
static int lex_single(struct shlex *l)
{
    l->i++;
    while (l->s[l->i] != '\0' && l->s[l->i] != '\'') {
        sb_addc(&l->v, l->s[l->i]);
        l->i++;
    }
    if (l->s[l->i] == '\0') {
        return 1;
    }
    l->i++;
    return 0;
}

/* One character (or backslash escape) inside "...". */
static void lex_dq_char(struct shlex *l)
{
    const char *s = l->s;
    size_t i = l->i;
    if (s[i] == '\\' && s[i + 1] != '\0' && strchr("$`\"\\\n", s[i + 1]) != NULL) {
        if (s[i + 1] != '\n') {
            sb_addc(&l->v, s[i + 1]);
        }
        l->i += 2;
        return;
    }
    if (s[i] == '$' || s[i] == '`') {
        l->dyn = 1;
    }
    sb_addc(&l->v, s[i]);
    l->i++;
}

/* "..." at l->i: 0 ok, 1 unbalanced. */
static int lex_double(struct shlex *l)
{
    l->i++;
    while (l->s[l->i] != '\0' && l->s[l->i] != '"') {
        lex_dq_char(l);
    }
    if (l->s[l->i] == '\0') {
        return 1;
    }
    l->i++;
    return 0;
}

/* Unquoted backslash at l->i: 0 ok, 1 at the end of the string. */
static int lex_backslash(struct shlex *l)
{
    char next = l->s[l->i + 1];
    if (next == '\0') {
        return 1;
    }
    if (next != '\n') {
        sb_addc(&l->v, next);
    }
    l->i += 2;
    return 0;
}

/* One unquoted character, quoted run or escape at l->i: 0 ok, 1 unsupported. */
static int lex_char(struct shlex *l)
{
    char c = l->s[l->i];
    if (c == '\'') {
        return lex_single(l);
    }
    if (c == '"') {
        return lex_double(l);
    }
    if (c == '\\') {
        return lex_backslash(l);
    }
    if (strchr("|&;<>()", c) != NULL) {
        return 1;
    }
    if (c == '$' || c == '`') {
        l->dyn = 1;
    }
    sb_addc(&l->v, c);
    l->i++;
    return 0;
}

/* The next word: SH_OK (pushed), SH_END, SH_UNSUPPORTED or SH_OOM. */
static int sh_next(struct shlex *l, size_t max, struct shwords *ws)
{
    size_t start;
    int r = 0;
    while (sh_blank(l->s[l->i])) {
        l->i++;
    }
    if (l->s[l->i] == '\0' || (max != 0 && ws->n >= max)) {
        return SH_END;
    }
    if (l->s[l->i] == '#') {
        return SH_UNSUPPORTED;
    }
    start = l->i;
    l->dyn = 0;
    while (r == 0 && l->s[l->i] != '\0' && !sh_blank(l->s[l->i])) {
        r = lex_char(l);
    }
    if (r != 0) {
        return SH_UNSUPPORTED;
    }
    if (l->v.oom) {
        return SH_OOM;
    }
    return shwords_push(ws, start, l->i, sb_take(&l->v), l->dyn) != 0 ? SH_OOM : SH_OK;
}

/*
 * Split s into at most `max` (0: all) POSIX sh words with quote removal. Control operators
 * (| & ; < > ( )), comments and unbalanced quotes make the value SH_UNSUPPORTED.
 */
static int sh_split(const char *s, size_t max, struct shwords *ws)
{
    struct shlex l;
    int r;
    ws->w = NULL;
    ws->n = 0;
    ws->cap = 0;
    l.s = s;
    l.i = 0;
    l.dyn = 0;
    sb_init(&l.v);
    do {
        r = sh_next(&l, max, ws);
    } while (r == SH_OK);
    sb_free(&l.v);
    if (r == SH_END) {
        return SH_OK;
    }
    shwords_free(ws);
    return r;
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

/* "-iPATH" / "-FPATH" / "-EPATH" / "-SPATH": the attached path translated. */
static int ssh_attached_path(const struct fl_xlate *x, const char *v, char **t)
{
    char *p = NULL;
    int r = xl_path(x, v + 2, &p);
    if (r != 1) {
        return r;
    }
    *t = join3("", v, 2, p);
    free(p);
    return *t != NULL ? 1 : -1;
}

/*
 * One ssh argument word v (not dynamic). kind is what the previous option expects (see
 * ssh_value_kind); *expect receives what this word expects of the next one.
 * 1 changed (*t), 0 keep, -1 oom.
 */
static int ssh_word(const struct fl_xlate *x, const char *v, int kind, int *expect, char **t)
{
    *t = NULL;
    if (kind == 1) {
        return xl_path(x, v, t);
    }
    if (kind == 2) {
        return xl_ssh_opt(x, "", v, t);
    }
    if (kind != 0) {
        return 0;
    }
    *expect = ssh_value_kind(v);
    if (*expect != 0) {
        return 0;
    }
    if (v[0] == '-' && v[1] != '\0' && strchr("iFES", v[1]) != NULL && v[2] != '\0') {
        return ssh_attached_path(x, v, t);
    }
    if (v[0] == '-' && v[1] == 'o' && v[2] != '\0') {
        return xl_ssh_opt(x, "-o", v + 2, t);
    }
    return v[0] != '-' ? xl_path(x, v, t) : 0;
}

/* An ssh command line (native or Windows OpenSSH) with its path arguments translated. */
static int rebuild_ssh(const struct fl_xlate *x, const char *val, const struct shwords *ws, char **res)
{
    struct sb b;
    int changed = 0;
    int expect = 0;
    sb_init(&b);
    if (is_win_program(ws->w[0].val)) {
        sb_adds(&b, "ssh");
        changed = 1;
    } else {
        sb_addn(&b, val + ws->w[0].start, ws->w[0].end - ws->w[0].start);
    }
    for (size_t i = 1; i < ws->n; i++) {
        const struct shword *w = &ws->w[i];
        char *t = NULL;
        int kind = expect;
        int r = 0;
        expect = 0;
        if (!w->dynamic) {
            r = ssh_word(x, w->val, kind, &expect, &t);
        }
        if (r < 0) {
            free(t);
            sb_free(&b);
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
    if (!changed) {
        sb_free(&b);
        return 0;
    }
    *res = sb_take(&b);
    return *res != NULL ? 1 : -1;
}

/* rewrite_ssh() once val split into ws (at least one word, the first one literal). */
static int rewrite_ssh_words(const struct fl_xlate *x, const char *val, const struct shwords *ws, char **res)
{
    const char *prog = ws->w[0].val;
    if (prog_is(prog, "plink") || prog_is(prog, "tortoiseplink")) {
        *res = dup("ssh");
        return *res != NULL ? 1 : -1;
    }
    if (is_win_program(prog) && !prog_is(prog, "ssh")) {
        if (!have(x->winexec)) {
            return 0;
        }
        *res = wrap_winexec(x, "", prog, val + ws->w[0].end);
        return *res != NULL ? 1 : -1;
    }
    return rebuild_ssh(x, val, ws, res);
}

/*
 * core.sshCommand / GIT_SSH_COMMAND: Windows ssh -> native "ssh" with -i/-F/-E/-S/-o paths
 * translated; plink/TortoisePlink -> plain "ssh" (their options mean nothing to OpenSSH);
 * another Windows program is wrapped with winexec. 1 changed, 0 keep, -1 oom.
 */
static int rewrite_ssh(const struct fl_xlate *x, const char *val, char **res)
{
    struct shwords ws;
    int r = sh_split(val, 0, &ws);
    *res = NULL;
    if (r == SH_OOM) {
        return -1;
    }
    if (r == SH_OK && ws.n > 0 && !ws.w[0].dynamic) {
        r = rewrite_ssh_words(x, val, &ws, res);
    } else {
        r = 0;
    }
    shwords_free(&ws);
    return r;
}

/* ======================================================================== */
/* Config sanitizer: git -c, clone's config option and the config environment  */
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
    if (r < 0) {
        return CFG_ERR;
    }
    return r == 1 ? CFG_CHANGED : CFG_KEEP;
}

enum { CFG_NEXT = 3 }; /* no rule of this group applies */

/* Windows-only settings: dropped whatever their value (core.fsmonitor only when enabled). */
static int cfg_windows_only(const struct cfgkey *k, int core, const char *val)
{
    if (sec_is(k, "http") && (var_is(k, "sslbackend") || var_has_prefix(k, "schannel"))) {
        return CFG_DROP;
    }
    if (core && (var_is(k, "fscache") || var_is(k, "longpaths"))) {
        return CFG_DROP;
    }
    if (!core || !var_is(k, "fsmonitor")) {
        return CFG_NEXT;
    }
    if (val == NULL || bool_true(val) || is_win_program(val)) {
        return CFG_DROP;
    }
    return CFG_KEEP;
}

static int cfg_askpass(const struct fl_xlate *x, const char *val, char **newval)
{
    if (!is_win_program(val)) {
        return CFG_KEEP;
    }
    if (!have(x->askpass)) {
        return CFG_DROP;
    }
    *newval = dup(x->askpass);
    return *newval != NULL ? CFG_CHANGED : CFG_ERR;
}

/* Program-valued keys: editors, credential helpers, core.askPass, core.sshCommand. */
static int cfg_program(const struct fl_xlate *x, const struct cfgkey *k, int core, const char *val, char **newval)
{
    int seq_editor = sec_is(k, "sequence") && !k->has_sub && var_is(k, "editor");
    if ((core && var_is(k, "editor")) || seq_editor) {
        return cfg_result(rewrite_editor(x, val, newval));
    }
    if (sec_is(k, "credential") && var_is(k, "helper")) {
        return cfg_result(rewrite_cred_helper(x, val, newval));
    }
    if (core && var_is(k, "askpass")) {
        return cfg_askpass(x, val, newval);
    }
    if (core && var_is(k, "sshcommand")) {
        return cfg_result(rewrite_ssh(x, val, newval));
    }
    return CFG_NEXT;
}

/* Values only Git for Windows understands: plink ssh.variant, gitInstance CA bundles, gpg.exe. */
static int cfg_windows_value(const struct cfgkey *k, const char *val)
{
    if (sec_is(k, "ssh") && !k->has_sub && var_is(k, "variant")) {
        return is_plink_variant(val) ? CFG_DROP : CFG_KEEP;
    }
    if (sec_is(k, "http") && (var_is(k, "sslcainfo") || var_is(k, "sslcapath")) &&
        has_component_ci(val, "gitinstance")) {
        return CFG_DROP;
    }
    if (sec_is(k, "gpg") && var_is(k, "program")) {
        return is_win_program(val) ? CFG_DROP : CFG_KEEP;
    }
    return CFG_NEXT;
}

/*
 * Sanitize one config entry (val NULL: implicit boolean true). CFG_CHANGED stores the new
 * value in *newval (malloc'd).
 */
static int sanitize_cfg(const struct fl_xlate *x, const char *key, size_t keylen, const char *val, char **newval)
{
    struct cfgkey k;
    int core;
    int r;
    *newval = NULL;
    if (parse_key(key, keylen, &k) != 0) {
        return CFG_KEEP;
    }
    core = sec_is(&k, "core") && !k.has_sub;
    r = cfg_windows_only(&k, core, val);
    if (r != CFG_NEXT) {
        return r;
    }
    if (val == NULL || val[0] == '\0') {
        return CFG_KEEP; /* implicit booleans and resets ("credential.helper=") */
    }
    r = cfg_program(x, &k, core, val, newval);
    if (r == CFG_NEXT) {
        r = cfg_windows_value(&k, val);
    }
    if (r != CFG_NEXT) {
        return r;
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

static const char *const k_cfg_value_opts[] = { "-f", "--file", "--blob", "--type", "-t", "--default",
                                                "--comment", "--value", "--url", NULL };
static const char *const k_cfg_actions[] = { "--list", "-l", "--unset", "--unset-all", "--add", "--replace-all",
                                             "--rename-section", "--remove-section", "--edit", "-e",
                                             "--get-color", "--get-colorbool", "--get-urlmatch", NULL };
static const char *const k_cfg_verbs[] = { "get", "set", "unset", "list", "edit", "rename-section",
                                           "remove-section", NULL };

/* What a `git config` command line asks for. */
struct cfgscan {
    const char *pos[3];
    int npos;
    int get;
    int regexp;
    int list;
    int other_action;
    int path_type;
    int show_origin;
};

/* An argument that is not an option value: a mode flag or a positional word. */
static void scan_config_flag(struct cfgscan *c, const char *s)
{
    if (strcmp(s, "--type=path") == 0 || strcmp(s, "--path") == 0) {
        c->path_type = 1;
    } else if (strcmp(s, "--get") == 0 || strcmp(s, "--get-all") == 0 || strcmp(s, "--all") == 0) {
        c->get = 1;
    } else if (strcmp(s, "--get-regexp") == 0 || strcmp(s, "--regexp") == 0) {
        c->regexp = 1;
    } else if (strcmp(s, "--list") == 0 || strcmp(s, "-l") == 0) {
        c->list = 1;
    } else if (in_list(s, k_cfg_actions)) {
        c->other_action = 1;
    } else if (s[0] != '-' && c->npos < 3) {
        c->pos[c->npos] = s;
        c->npos++;
    }
}

/* Classify a[i]; returns the number of arguments it consumes (an option value too). */
static int scan_config_arg(struct cfgscan *c, int n, char **a, int i)
{
    const char *s = a[i];
    if (strcmp(s, "--") == 0) {
        return 1;
    }
    if (strcmp(s, "--show-origin") == 0) {
        c->show_origin = 1;
        return 1;
    }
    if (!in_list(s, k_cfg_value_opts)) {
        scan_config_flag(c, s);
        return 1;
    }
    if ((strcmp(s, "--type") == 0 || strcmp(s, "-t") == 0) && i + 1 < n && strcmp(a[i + 1], "path") == 0) {
        c->path_type = 1;
    }
    return 2;
}

/* The key the command reads (NULL when none); -1 for a verb that prints no values. */
static int config_key(struct cfgscan *c, const char **key)
{
    *key = NULL;
    if (c->npos > 0 && in_list(c->pos[0], k_cfg_verbs)) {
        if (strcmp(c->pos[0], "get") == 0) {
            c->get = 1;
            *key = c->npos > 1 ? c->pos[1] : NULL;
            return 0;
        }
        if (strcmp(c->pos[0], "list") == 0) {
            c->list = 1;
            return 0;
        }
        return -1;
    }
    if (c->npos > 0 && (c->get || c->regexp)) {
        *key = c->pos[0];
    } else if (c->npos == 1 && !c->list && !c->other_action) {
        *key = c->pos[0]; /* implicit get: git config <name> */
        c->get = 1;
    }
    return 0;
}

static enum fl_outplan plan_config(int n, char **a)
{
    struct cfgscan c;
    const char *key;
    int i = 0;
    memset(&c, 0, sizeof(c));
    while (i < n && !c.show_origin) {
        i += scan_config_arg(&c, n, a, i);
    }
    if (c.show_origin) {
        return FL_OUT_SHOW_ORIGIN;
    }
    if (config_key(&c, &key) != 0) {
        return FL_OUT_NONE;
    }
    if (c.regexp) {
        return key != NULL && ci_contains(key, "remote") && ci_contains(key, "url") ? FL_OUT_REMOTE : FL_OUT_NONE;
    }
    if (c.list && !c.get) {
        return FL_OUT_REMOTE; /* only remote.*.url / pushurl values are rewritten */
    }
    if (key == NULL || c.other_action) {
        return FL_OUT_NONE;
    }
    if (key_is_remote_url(key, strlen(key))) {
        return FL_OUT_REMOTE;
    }
    return c.path_type || key_is_path(key) ? FL_OUT_PATHS_LINES : FL_OUT_NONE;
}

/* The first non-option word before "--" (NULL if none); *verbose: -v/--verbose came before it. */
static const char *first_word(int n, char **a, int *verbose)
{
    *verbose = 0;
    for (int i = 0; i < n; i++) {
        if (strcmp(a[i], "--") == 0) {
            return NULL;
        }
        if (a[i][0] != '-') {
            return a[i];
        }
        if (strcmp(a[i], "-v") == 0 || strcmp(a[i], "--verbose") == 0) {
            *verbose = 1;
        }
    }
    return NULL;
}

/* opt appears before any "--". */
static int has_opt(int n, char **a, const char *opt)
{
    for (int i = 0; i < n; i++) {
        if (strcmp(a[i], "--") == 0) {
            return 0;
        }
        if (strcmp(a[i], opt) == 0) {
            return 1;
        }
    }
    return 0;
}

/* --format=%(worktreepath)... or --format %(worktreepath)... before any "--". */
static int format_is_worktree(int n, char **a)
{
    static const char wtp[] = "%(worktreepath)";
    for (int i = 0; i < n; i++) {
        if (strcmp(a[i], "--") == 0) {
            return 0;
        }
        if (has_prefix(a[i], "--format=") && has_prefix(a[i] + 9, wtp)) {
            return 1;
        }
        if (strcmp(a[i], "--format") == 0 && i + 1 < n && has_prefix(a[i + 1], wtp)) {
            return 1;
        }
    }
    return 0;
}

static enum fl_outplan plan_remote(const char *first, int verbose)
{
    if (first != NULL && strcmp(first, "get-url") == 0) {
        return FL_OUT_REMOTE;
    }
    return first == NULL && verbose ? FL_OUT_REMOTE : FL_OUT_NONE;
}

static enum fl_outplan detect_plan(const char *sub, int n, char **a)
{
    int verbose = 0;
    const char *first = first_word(n, a, &verbose);
    if (strcmp(sub, "rev-parse") == 0) {
        return FL_OUT_PATHS_LINES;
    }
    if (strcmp(sub, "worktree") == 0) {
        return first != NULL && strcmp(first, "list") == 0 ? FL_OUT_WORKTREE : FL_OUT_NONE;
    }
    if (strcmp(sub, "remote") == 0) {
        return plan_remote(first, verbose);
    }
    if (strcmp(sub, "ls-remote") == 0) {
        return has_opt(n, a, "--get-url") ? FL_OUT_REMOTE : FL_OUT_NONE;
    }
    if (strcmp(sub, "config") == 0) {
        return plan_config(n, a);
    }
    if (strcmp(sub, "for-each-ref") == 0 || strcmp(sub, "branch") == 0 || strcmp(sub, "tag") == 0) {
        return format_is_worktree(n, a) ? FL_OUT_WORKTREE : FL_OUT_NONE;
    }
    return FL_OUT_NONE;
}

/* --- argument walk ------------------------------------------------------- */

/* Global options with a separate value that is passed through unchanged. */
static const char *const k_global_value_opts[] = { "--namespace", "--super-prefix", "--attr-source",
                                                   "--config-env", "--shallow-file", NULL };
/* Global options with a separate path value. */
static const char *const k_global_path_opts[] = { "-C", "--git-dir", "--work-tree", NULL };
/* git --exec-path / --html-path / ... print a path. */
static const char *const k_global_info_opts[] = { "--exec-path", "--html-path", "--man-path", "--info-path", NULL };

struct argwalk {
    const struct fl_xlate *x;
    int argc;
    char **argv;
    int i;               /* the argument being looked at */
    struct fl_strvec *out;
    const char *sub;     /* the subcommand ("" until known) */
    int global_paths;    /* --exec-path & co. without a subcommand */
    int positional;      /* after "--" / "--end-of-options" */
};

enum { WALK_NEXT = 0, WALK_STOP = 1, WALK_ERR = -1, WALK_UNHANDLED = 2 };

static int has_next(const struct argwalk *w)
{
    return w->i + 1 < w->argc;
}

/* Consume and return the next argument (has_next() must hold). */
static const char *take_next(struct argwalk *w)
{
    w->i++;
    return w->argv[w->i];
}

/* opt, then the next argument translated when it is a Windows path. */
static int push_pair_xl(struct argwalk *w, const char *opt)
{
    int r = sv_add(w->out, opt);
    return r == 0 ? push_xl(w->x, w->out, take_next(w)) : r;
}

/* opt, then the next argument (if any) unchanged. */
static int push_pair_raw(struct argwalk *w, const char *opt)
{
    int r = sv_add(w->out, opt);
    return r == 0 && has_next(w) ? sv_add(w->out, take_next(w)) : r;
}

/* A global option other than the ones that end the global options. 0 ok, -1 oom. */
static int global_opt_value(struct argwalk *w, const char *a)
{
    if (strcmp(a, "-c") == 0 && has_next(w)) {
        return push_config(w->x, w->out, a, take_next(w));
    }
    if (in_list(a, k_global_path_opts) && has_next(w)) {
        return push_pair_xl(w, a);
    }
    if (has_prefix(a, "--git-dir=") || has_prefix(a, "--work-tree=")) {
        return push_opt_value(w->x, w->out, a, (size_t)(strchr(a, '=') - a));
    }
    if (in_list(a, k_global_value_opts) && has_next(w)) {
        return push_pair_raw(w, a);
    }
    if (has_prefix(a, "--exec-path=")) {
        /* Git for Windows' libexec is useless (and harmful) to native git. */
        return fl_is_win_abs(a + 12) ? 0 : sv_add(w->out, a);
    }
    if (in_list(a, k_global_info_opts)) {
        w->global_paths = 1;
    }
    return sv_add(w->out, a);
}

/* git's global options (git.c handle_options): WALK_NEXT, WALK_STOP or WALK_ERR. */
static int global_opt(struct argwalk *w)
{
    const char *a = w->argv[w->i];
    int r;
    if (a[0] != '-') {
        return WALK_STOP;
    }
    if (strcmp(a, "--version") == 0 || strcmp(a, "-v") == 0) {
        w->sub = "version"; /* git turns these into the "version" / "help" commands */
    } else if (strcmp(a, "--help") == 0 || strcmp(a, "-h") == 0) {
        w->sub = "help";
    } else if (strcmp(a, "--") != 0) {
        r = global_opt_value(w, a);
        w->i++;
        return r != 0 ? WALK_ERR : WALK_NEXT;
    }
    r = sv_add(w->out, a);
    w->i++;
    return r != 0 ? WALK_ERR : WALK_STOP;
}

/* clone -c / --config / --config=: WALK_UNHANDLED when a is none of them. */
static int clone_config_arg(struct argwalk *w, const char *a)
{
    char *res = NULL;
    int r;
    if ((strcmp(a, "-c") == 0 || strcmp(a, "--config") == 0) && has_next(w)) {
        return push_config(w->x, w->out, a, take_next(w));
    }
    if (!has_prefix(a, "--config=")) {
        return WALK_UNHANDLED;
    }
    r = sanitize_kv_arg(w->x, a + 9, &res);
    if (r == 1) {
        return sv_add(w->out, a);
    }
    if (r == 2) {
        r = sv_take(w->out, join3("--config=", "", 0, res));
        free(res);
    }
    return r;
}

/* "--name=value": the value is translated for the k_path_eq_opts names. */
static int long_eq_arg(struct argwalk *w, const char *a)
{
    size_t namelen = (size_t)(strchr(a, '=') - a);
    char *name = dupn(a, namelen);
    int r;
    if (name == NULL) {
        return -1;
    }
    r = in_list(name, k_path_eq_opts) ? push_opt_value(w->x, w->out, a, namelen) : sv_add(w->out, a);
    free(name);
    return r;
}

/* One argument after the subcommand. 0 ok, -1 oom. */
static int sub_arg(struct argwalk *w)
{
    const char *a = w->argv[w->i];
    if (w->positional) {
        return push_xl(w->x, w->out, a);
    }
    if (strcmp(a, "--") == 0 || strcmp(a, "--end-of-options") == 0) {
        w->positional = 1;
        return sv_add(w->out, a);
    }
    if (strcmp(w->sub, "clone") == 0) {
        int r = clone_config_arg(w, a);
        if (r != WALK_UNHANDLED) {
            return r;
        }
    }
    if (a[0] == '-' && a[1] == '-' && strchr(a, '=') != NULL) {
        return long_eq_arg(w, a);
    }
    if (never_consumes(w->sub, a)) {
        return push_pair_raw(w, a);
    }
    if (in_list(a, k_path_next_opts) && has_next(w) && fl_is_win_abs(w->argv[w->i + 1])) {
        return push_pair_xl(w, a);
    }
    return push_xl(w->x, w->out, a);
}

/* Walk every argument into w->out; 0 ok, -1 oom. */
static int walk_args(struct argwalk *w)
{
    int r = WALK_NEXT;
    while (r == WALK_NEXT && w->i < w->argc) {
        r = global_opt(w);
    }
    if (r == WALK_ERR) {
        return -1;
    }
    if (w->sub[0] == '\0' && w->i < w->argc) {
        w->sub = w->argv[w->i];
        if (sv_add(w->out, w->sub) != 0) {
            return -1;
        }
        w->i++;
    }
    return 0;
}

static int all_non_null(int argc, char **argv)
{
    for (int k = 0; k < argc; k++) {
        if (argv[k] == NULL) {
            return 0;
        }
    }
    return 1;
}

int fl_translate_argv(const struct fl_xlate *x, int argc, char **argv, struct fl_strvec *out,
                      struct fl_cmdinfo *info)
{
    struct argwalk w;
    int sub_start;
    int r;
    if (out != NULL) {
        out->n = 0;
        out->cap = 0;
        out->v = NULL;
    }
    if (info != NULL) {
        memset(info, 0, sizeof(*info));
    }
    if (x == NULL || out == NULL || info == NULL || argc < 0 || (argc > 0 && argv == NULL) ||
        !all_non_null(argc, argv)) {
        return -1;
    }
    memset(&w, 0, sizeof(w));
    w.x = x;
    w.argc = argc;
    w.argv = argv;
    w.out = out;
    w.sub = "";
    r = walk_args(&w);
    sub_start = w.i;
    while (r == 0 && w.i < argc) {
        r = sub_arg(&w);
        w.i++;
    }
    if (r != 0) {
        fl_strvec_free(out);
        memset(info, 0, sizeof(*info));
        return -1;
    }
    set_subcmd(info, w.sub);
    if (w.sub[0] != '\0') {
        info->out_plan = detect_plan(w.sub, argc - sub_start, argv + sub_start);
    } else if (w.global_paths) {
        info->out_plan = FL_OUT_PATHS_LINES;
    }
    info->stderr_paths = w.sub[0] != '\0' && strcmp(w.sub, "version") != 0 && strcmp(w.sub, "help") != 0;
    return 0;
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
        r = xl_path_n(x, item, i - s, &t);
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

static size_t skip_spaces(const char *v, size_t pos)
{
    while (is_space(v[pos])) {
        pos++;
    }
    return pos;
}

/* One GIT_CONFIG_PARAMETERS entry: the dequoted key (and value) buffers and what they hold. */
struct cfgparam {
    struct sb kb;
    struct sb vb;
    size_t keylen;
    const char *val;
    int has_val;
};

/* Old style 'key=value' or 'key': the value (if any) follows the first '=' of the token. */
static void cp_old_style(struct cfgparam *p)
{
    const char *eq = p->kb.p != NULL ? strchr(p->kb.p, '=') : NULL;
    if (eq != NULL) {
        p->keylen = (size_t)(eq - p->kb.p);
        p->val = eq + 1;
        p->has_val = 1;
    } else {
        p->keylen = p->kb.len;
    }
}

/* New style 'key'='value' (or 'key'= for no value), after the '='. 0 ok, 1 malformed. */
static int cp_new_value(const char *v, size_t *pos, struct cfgparam *p)
{
    p->keylen = p->kb.len;
    if (v[*pos] != '\'') {
        return v[*pos] != '\0' && !is_space(v[*pos]) ? 1 : 0;
    }
    if (sq_step(v, pos, &p->vb) != 0 || p->vb.oom || (v[*pos] != '\0' && !is_space(v[*pos]))) {
        return 1;
    }
    p->val = p->vb.p != NULL ? p->vb.p : "";
    p->has_val = 1;
    return 0;
}

/* Parse the entry at v[*pos] into p (initialised here; free with sb_free). 0 ok, 1 malformed. */
static int cp_parse(const char *v, size_t *pos, struct cfgparam *p)
{
    sb_init(&p->kb);
    sb_init(&p->vb);
    p->keylen = 0;
    p->val = NULL;
    p->has_val = 0;
    if (sq_step(v, pos, &p->kb) != 0 || p->kb.oom) {
        return 1;
    }
    if (v[*pos] == '\0' || is_space(v[*pos])) {
        cp_old_style(p);
        return 0;
    }
    if (v[*pos] != '=') {
        return 1;
    }
    (*pos)++;
    return cp_new_value(v, pos, p);
}

/* Output state of xl_config_parameters(). */
struct cpout {
    struct sb b;
    int first;
    int changed;
};

/* Sanitize one parsed entry and append it (new style) unless dropped. 0 ok, -1 oom. */
static int cp_emit(const struct fl_xlate *x, const struct cfgparam *p, struct cpout *o)
{
    const char *key = p->kb.p != NULL ? p->kb.p : "";
    char *nv = NULL;
    char *k;
    int r = sanitize_cfg(x, key, p->keylen, p->has_val ? p->val : NULL, &nv);
    if (r == CFG_ERR) {
        return -1;
    }
    if (r != CFG_KEEP) {
        o->changed = 1;
    }
    if (r == CFG_DROP) {
        free(nv);
        return 0;
    }
    k = dupn(key, p->keylen);
    if (k == NULL) {
        free(nv);
        return -1;
    }
    if (!o->first) {
        sb_addc(&o->b, ' ');
    }
    o->first = 0;
    sb_add_gitsq(&o->b, k);
    if (r == CFG_CHANGED || p->has_val) {
        sb_addc(&o->b, '=');
        sb_add_gitsq(&o->b, r == CFG_CHANGED ? nv : p->val);
    }
    free(k);
    free(nv);
    return 0;
}

/* Parse and emit the entry at v[*pos], then skip the spaces after it. 0 ok, 1 malformed, -1 oom. */
static int cp_step(const struct fl_xlate *x, const char *v, size_t *pos, struct cpout *o)
{
    struct cfgparam p;
    int rc = cp_parse(v, pos, &p);
    if (rc == 0) {
        rc = cp_emit(x, &p, o);
    }
    sb_free(&p.kb);
    sb_free(&p.vb);
    *pos = skip_spaces(v, *pos);
    return rc;
}

/*
 * GIT_CONFIG_PARAMETERS ('key=value' old style or 'key'='value' new style, space-separated):
 * 1 rewritten into *res (new style; may be "" when everything was dropped), 0 keep (unchanged
 * or unparsable), -1 oom.
 */
static int xl_config_parameters(const struct fl_xlate *x, const char *v, char **res)
{
    struct cpout o;
    size_t pos = skip_spaces(v, 0);
    int rc = 0;
    *res = NULL;
    sb_init(&o.b);
    o.first = 1;
    o.changed = 0;
    while (rc == 0 && v[pos] != '\0') {
        rc = cp_step(x, v, &pos, &o);
    }
    if (rc != 0 || !o.changed) {
        sb_free(&o.b);
        return rc < 0 ? -1 : 0; /* malformed: leave it to git to report */
    }
    *res = sb_take(&o.b);
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

/* The value of GIT_CONFIG_COUNT (the last one, any case), or NULL. */
static const char *cfg_count_value(char **envp)
{
    const char *count = NULL;
    for (size_t i = 0; envp[i] != NULL; i++) {
        const char *eq = strchr(envp[i], '=');
        if (eq != NULL && ci_eqn(envp[i], (size_t)(eq - envp[i]), "GIT_CONFIG_COUNT")) {
            count = eq + 1;
        }
    }
    return count;
}

/* The index of GIT_CONFIG_<prefix><n> in e[0..nl), when it is below count; else -1. */
static long cfg_index(const char *e, size_t nl, const char *prefix, size_t count)
{
    size_t pl = strlen(prefix);
    long idx;
    if (nl <= pl || !ci_eqn(e, pl, prefix)) {
        return -1;
    }
    idx = parse_index(e + pl, nl - pl);
    return idx >= 0 && (size_t)idx < count ? idx : -1;
}

/* Record e when it is a GIT_CONFIG_KEY_<n> / GIT_CONFIG_VALUE_<n> of the group. */
static void cfg_group_note(struct cfg_group *g, const char *e)
{
    const char *eq = strchr(e, '=');
    size_t nl;
    long idx;
    if (eq == NULL) {
        return;
    }
    nl = (size_t)(eq - e);
    idx = cfg_index(e, nl, "GIT_CONFIG_KEY_", g->count);
    if (idx >= 0) {
        g->keys[idx] = eq + 1;
        return;
    }
    idx = cfg_index(e, nl, "GIT_CONFIG_VALUE_", g->count);
    if (idx >= 0) {
        g->vals[idx] = eq + 1;
    }
}

static int cfg_group_scan(char **envp, struct cfg_group *g)
{
    const char *count = cfg_count_value(envp);
    long c;
    memset(g, 0, sizeof(*g));
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
        cfg_group_note(g, envp[i]);
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

/* State of fl_translate_env(). */
struct envwalk {
    const struct fl_xlate *x;
    struct fl_envops *ops;
    struct cfg_group g;
    int ssh_dropped; /* a Windows GIT_SSH is unset (so is GIT_SSH_VARIANT) */
    int target_git;  /* FL_ASKPASS_TARGET is written for GIT_ASKPASS */
    int target_ssh;  /* FL_SSH_ASKPASS_TARGET is written for SSH_ASKPASS */
};

enum { ENV_NEXT = 2 }; /* no rule of this group applies */

static const char *const k_env_ssh_command[] = { "GIT_SSH_COMMAND", NULL };

/* A Windows program in e[0..nl)'s value: note what the later rules need to know. */
static void note_win_program(struct envwalk *w, const char *e, size_t nl)
{
    if (ci_eqn(e, nl, "GIT_SSH")) {
        w->ssh_dropped = 1;
    } else if (ci_eqn(e, nl, "GIT_ASKPASS") && have(w->x->askpass)) {
        w->target_git = 1;
    } else if (ci_eqn(e, nl, "SSH_ASKPASS") && have(w->x->ssh_askpass)) {
        w->target_ssh = 1;
    }
}

/* Store a rewrite result: 1 sets name=t, 0 keeps e unchanged, anything else is an error. */
static int env_store(struct fl_envops *ops, int r, const char *name, const char *t, const char *e)
{
    if (r == 1) {
        return add_set(ops, name, t);
    }
    return r == 0 ? sv_add(&ops->set, e) : -1;
}

/* Variables that are unset (or handled as a group) whatever else applies. */
static int env_unset_rules(struct envwalk *w, const char *e, size_t nl, const char *v)
{
    if (name_in(e, nl, k_env_drop) != NULL || name_has_prefix(e, nl, k_env_drop_prefix)) {
        return add_unset(w->ops, e, nl);
    }
    /* FL_*ASKPASS_TARGET are only ever the personas' input, written below; an inherited one
       must neither duplicate nor (when no persona is used) linger. */
    if (ci_eqn(e, nl, "FL_ASKPASS_TARGET")) {
        return w->target_git ? 0 : add_unset(w->ops, e, nl);
    }
    if (ci_eqn(e, nl, "FL_SSH_ASKPASS_TARGET")) {
        return w->target_ssh ? 0 : add_unset(w->ops, e, nl);
    }
    if (in_cfg_group(&w->g, e, nl)) {
        return ci_eqn(e, nl, "GIT_CONFIG_COUNT") ? emit_cfg_group(w->x, &w->g, w->ops) : 0;
    }
    /* Fork exports GIT_CONFIG_SYSTEM=C:/.../Fork/gitInstance/<ver>/etc/gitconfig (B3 corpus):
       Git for Windows' system config (autocrlf=true, symlinks=false, credential.helper=manager,
       schannel) must not reach native git. */
    if (name_in(e, nl, k_env_bundled) != NULL && fl_is_win_abs(v) && has_component_ci(v, "gitinstance")) {
        return add_unset(w->ops, e, nl);
    }
    return ENV_NEXT;
}

static int env_editor(const struct fl_xlate *x, const char *v, char **res)
{
    *res = NULL;
    return v[0] != '\0' ? rewrite_editor(x, v, res) : 0;
}

static int env_ssh_command(const struct fl_xlate *x, const char *v, char **res)
{
    *res = NULL;
    return v[0] != '\0' ? rewrite_ssh(x, v, res) : 0;
}

typedef int (*env_rewrite_fn)(const struct fl_xlate *x, const char *v, char **res);

struct env_rule {
    const char *const *names;
    env_rewrite_fn fn;
};

static const struct env_rule k_env_rules[] = {
    { k_env_paths, xl_path },
    { k_env_lists, xl_list },
    { k_env_editors, env_editor },
    { k_env_ssh_command, env_ssh_command },
};

/* Variables whose value is rewritten (paths, lists, editors, GIT_SSH_COMMAND). */
static int env_rewrite_rules(struct envwalk *w, const char *e, size_t nl, const char *v)
{
    for (size_t i = 0; i < sizeof(k_env_rules) / sizeof(k_env_rules[0]); i++) {
        const char *canon = name_in(e, nl, k_env_rules[i].names);
        if (canon != NULL) {
            char *t = NULL;
            int r = k_env_rules[i].fn(w->x, v, &t);
            r = env_store(w->ops, r, canon, t, e);
            free(t);
            return r;
        }
    }
    return ENV_NEXT;
}

/* GIT_ASKPASS / SSH_ASKPASS (canon): a Windows program becomes the persona + its target. */
static int env_askpass(struct envwalk *w, const char *e, size_t nl, const char *v, const char *canon)
{
    int git = strcmp(canon, "GIT_ASKPASS") == 0;
    const char *persona = git ? w->x->askpass : w->x->ssh_askpass;
    int r;
    if (!is_win_program(v)) {
        return sv_add(&w->ops->set, e);
    }
    if (!have(persona)) {
        return add_unset(w->ops, e, nl);
    }
    r = add_set(w->ops, canon, persona);
    if (r != 0) {
        return r;
    }
    return add_set(w->ops, git ? "FL_ASKPASS_TARGET" : "FL_SSH_ASKPASS_TARGET", v);
}

static int env_config_parameters(struct envwalk *w, const char *e, size_t nl, const char *v)
{
    char *t = NULL;
    int r = xl_config_parameters(w->x, v, &t);
    if (r == 1 && t[0] == '\0') {
        r = add_unset(w->ops, e, nl);
    } else {
        r = env_store(w->ops, r, "GIT_CONFIG_PARAMETERS", t, e);
    }
    free(t);
    return r;
}

/* askpass, GIT_SSH, GIT_SSH_VARIANT, GIT_CONFIG_PARAMETERS. */
static int env_program_rules(struct envwalk *w, const char *e, size_t nl, const char *v)
{
    const char *canon = name_in(e, nl, k_env_askpass);
    if (canon != NULL) {
        return env_askpass(w, e, nl, v, canon);
    }
    if (ci_eqn(e, nl, "GIT_SSH")) {
        return is_win_program(v) ? add_unset(w->ops, e, nl) : sv_add(&w->ops->set, e);
    }
    if (ci_eqn(e, nl, "GIT_SSH_VARIANT")) {
        return w->ssh_dropped || is_plink_variant(v) ? add_unset(w->ops, e, nl) : sv_add(&w->ops->set, e);
    }
    if (ci_eqn(e, nl, "GIT_CONFIG_PARAMETERS")) {
        return env_config_parameters(w, e, nl, v);
    }
    return ENV_NEXT;
}

/* One "K=V" entry (name e[0..nl), nl > 0). 0 ok, -1 oom. */
static int env_entry(struct envwalk *w, const char *e, size_t nl)
{
    const char *v = e + nl + 1;
    int r = env_unset_rules(w, e, nl, v);
    if (r == ENV_NEXT) {
        r = env_rewrite_rules(w, e, nl, v);
    }
    if (r == ENV_NEXT) {
        r = env_program_rules(w, e, nl, v);
    }
    return r == ENV_NEXT ? sv_add(&w->ops->set, e) : r;
}

static int env_walk(struct envwalk *w, char **envp)
{
    for (size_t i = 0; envp[i] != NULL; i++) {
        const char *eq = strchr(envp[i], '=');
        if (eq != NULL && is_win_program(eq + 1)) {
            note_win_program(w, envp[i], (size_t)(eq - envp[i]));
        }
    }
    for (size_t i = 0; envp[i] != NULL; i++) {
        const char *e = envp[i];
        const char *eq = strchr(e, '=');
        if (eq != NULL && eq != e && env_entry(w, e, (size_t)(eq - e)) != 0) {
            return -1;
        }
    }
    return 0;
}

int fl_translate_env(const struct fl_xlate *x, char **envp, struct fl_envops *ops)
{
    struct envwalk w;
    int rc;
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
    memset(&w, 0, sizeof(w));
    w.x = x;
    w.ops = ops;
    if (cfg_group_scan(envp, &w.g) != 0) {
        cfg_group_free(&w.g);
        return -1;
    }
    rc = env_walk(&w, envp);
    cfg_group_free(&w.g);
    if (rc != 0) {
        fl_strvec_free(&ops->set);
        fl_strvec_free(&ops->unset);
    }
    return rc;
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
    switch (plan) {
    case FL_OUT_PATHS_LINES:
    case FL_OUT_WORKTREE:
    case FL_OUT_REMOTE:
    case FL_OUT_SHOW_ORIGIN:
        o->plan = plan;
        break;
    case FL_OUT_NONE:
    default:
        o->plan = FL_OUT_NONE;
        break;
    }
    o->nul_records = nul_records != 0;
}

int fl_out_add_anchor(struct fl_out *o, const char *unix_path, const char *win_path)
{
    struct sb w;
    const char *p;
    size_t pl;
    size_t ul;
    char *u;
    char *wp;
    if (o == NULL || unix_path == NULL || win_path == NULL || unix_path[0] != '/') {
        return -1;
    }
    p = win_path;
    pl = strlen(p);
    if (verbatim(p, pl)) {
        p += 4;
        pl -= 4u;
    }
    if (!drive_or_root(p, pl)) {
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

/* The anchor whose Unix side is the longest component prefix of p[0..n) (*len: its length). */
static const struct fl_anchor *best_anchor(const struct fl_out *o, const char *p, size_t n, size_t *len)
{
    const struct fl_anchor *best = NULL;
    *len = 0;
    for (size_t i = 0; i < o->nanchors; i++) {
        const struct fl_anchor *a = &o->anchors[i];
        size_t ul = strlen(a->unix_path);
        if (ul <= n && memcmp(p, a->unix_path, ul) == 0 && (ul == n || p[ul] == '/') && (best == NULL || ul > *len)) {
            best = a;
            *len = ul;
        }
    }
    return best;
}

/* A callback result byte equals the caller's (a '\' or '/' result matches a '/' input). */
static int tail_same(char q, char p)
{
    return q == p || (is_sep(q) && p == '/');
}

/* Append the callback result q ("X:..." / "\\?\X:...") for p[0..n); 0 when q is not a drive path. */
static int append_win_result(const char *q, const char *p, size_t n, struct sb *dst)
{
    size_t ql = strlen(q);
    size_t i;
    size_t j = n;
    if (verbatim(q, ql) && ql >= 6u && is_alpha(q[4]) && q[5] == ':') {
        q += 4;
        ql -= 4u;
    }
    if (!drive_or_root(q, ql)) {
        return 0;
    }
    /* Keep the caller's own bytes for the tail the callback merely copied, so a literal '\'
       in a Unix name is not turned into '/'. */
    i = ql;
    while (i > 2 && j > 0 && tail_same(q[i - 1u], p[j - 1u])) {
        i--;
        j--;
    }
    sb_addc(dst, to_upper(q[0]));
    sb_addc(dst, ':');
    for (size_t k = 2; k < i; k++) {
        sb_addc(dst, q[k] == '\\' ? '/' : q[k]);
    }
    sb_addn(dst, p + j, n - j);
    return 1;
}

/* p[0..n) through o->x->to_win; 1 appended, 0 nothing appended (unmappable or oom). */
static int out_path_callback(const struct fl_out *o, const char *p, size_t n, struct sb *dst)
{
    char *in = dupn(p, n);
    char *buf = in != NULL ? malloc(XPATH_MAX) : NULL;
    int ok = 0;
    if (buf != NULL) {
        buf[0] = '\0';
        if (o->x->to_win(o->x->ud, in, buf, XPATH_MAX) == 0 && memchr(buf, '\0', XPATH_MAX) != NULL) {
            ok = append_win_result(buf, p, n, dst);
        }
    }
    free(buf);
    free(in);
    return ok;
}

/* Append the Windows form of the Unix path p[0..n) (p[0] == '/', no NUL inside). */
static void out_path(const struct fl_out *o, const char *p, size_t n, struct sb *dst)
{
    size_t bestlen = 0;
    size_t start = dst->len;
    const struct fl_anchor *best = best_anchor(o, p, n, &bestlen);
    if (best != NULL) {
        sb_adds(dst, best->win_path);
        sb_addn(dst, p + bestlen, n - bestlen);
    } else if (o->x == NULL || o->x->to_win == NULL || !out_path_callback(o, p, n, dst)) {
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

/* rec[i] starts a quoted absolute path "'/...'": 1 with *c the index of the closing quote. */
static int quoted_abs_at(const char *rec, size_t n, size_t i, size_t *c)
{
    const char *close;
    if (rec[i] != '\'' || rec[i + 1u] != '/') {
        return 0;
    }
    close = memchr(rec + i + 1u, '\'', n - i - 1u);
    if (close == NULL) {
        return 0;
    }
    *c = (size_t)(close - rec);
    return abs_path_ok(rec + i + 1u, *c - i - 1u);
}

/* rev-parse & stderr: a whole-line path, else every '<abs path>' quoted inside the line. */
static int tr_paths_lines(const struct fl_out *o, const char *rec, size_t n, struct sb *dst)
{
    size_t last = 0;
    size_t i = 0;
    int any = 0;
    if (abs_path_ok(rec, n)) {
        out_path(o, rec, n, dst);
        return 1;
    }
    while (i + 1u < n) {
        size_t c = 0;
        if (quoted_abs_at(rec, n, i, &c)) {
            sb_addn(dst, rec + last, i + 1u - last);
            out_path(o, rec + i + 1u, c - i - 1u, dst);
            last = c;
            any = 1;
            i = c;
        }
        i++;
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
        if (win.p != NULL) {
            cq_quote(dst, win.p, win.len, raw_high);
        }
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
 * Emit one record (translated when the plan applies) plus its terminator *term (NULL: none,
 * end of stream). `contiguous` says rec[n] is the terminator in the caller's buffer, so a raw
 * record goes out in one call.
 */
static void emit_record(const struct fl_out *o, const char *rec, size_t n, const char *term, int contiguous,
                        fl_emit_fn emit, void *ud)
{
    struct sb t;
    sb_init(&t);
    if (translate_record(o, rec, n, &t) == 1) {
        if (term != NULL) {
            sb_addc(&t, *term);
        }
        if (!t.oom) {
            emit(ud, t.p, t.len);
            sb_free(&t);
            return;
        }
    }
    sb_free(&t);
    if (term != NULL && contiguous) {
        emit(ud, rec, n + 1u);
        return;
    }
    if (n > 0) {
        emit(ud, rec, n);
    }
    if (term != NULL) {
        emit(ud, term, 1);
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

/* Pass d[0..whole) through untranslated: a record over FL_OUT_MAX_RECORD, up to its terminator. */
static void pass_long(struct fl_out *o, const char *d, size_t whole, int found, fl_emit_fn emit, void *ud)
{
    if (o->len > 0) {
        emit(ud, o->buf, o->len);
        o->len = 0;
    }
    emit(ud, d, whole);
    o->skipping = !found;
}

/* A record d[0..t) whose terminator d[t] is in this chunk (plus any buffered start of it). */
static void feed_record(struct fl_out *o, const char *d, size_t t, fl_emit_fn emit, void *ud)
{
    if (o->len == 0) {
        emit_record(o, d, t, d + t, 1, emit, ud);
    } else if (buf_append(o, d, t) != 0) {
        go_raw(o, emit, ud);
        emit(ud, d, t + 1u);
    } else {
        emit_record(o, o->buf, o->len, d + t, 0, emit, ud);
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
    while (i < n && !o->failed) {
        size_t t = find_term(o, data + i, n - i);
        int found = t < n - i;
        size_t whole = found ? t + 1u : n - i;
        /* While skipping nothing is buffered (o->len == 0), so the check below holds then. */
        if (o->skipping || t > FL_OUT_MAX_RECORD - o->len) {
            pass_long(o, data + i, whole, found, emit, ud);
        } else if (found) {
            feed_record(o, data + i, t, emit, ud);
        } else if (buf_append(o, data + i, t) != 0) {
            go_raw(o, emit, ud);
            emit(ud, data + i, t);
        }
        i += whole;
    }
    if (i < n) {
        emit(ud, data + i, n - i); /* out of memory earlier in this chunk: raw from here */
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
            emit_record(o, o->buf, o->len, NULL, 0, emit, ud);
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
