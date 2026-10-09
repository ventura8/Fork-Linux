/*
 * test_translate_cov.c - fl_translate.c edge cases that the vector files do not reach
 * (sh-word quoting, config sanitizer corners, output plans, C-quoted paths), and the
 * out-of-memory paths through allocation-failure injection (test_alloc.h).
 *
 * Each case is "name", input, expected result, where the input is a " | "-separated list
 * and the result is printed by describe_*() below (the "args" joined with " | ", then
 * " => plan N"; or "set: ... // unset: ..."; or the escaped output bytes).
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "fl_translate.h"
#include "tap.h"
#include "test_alloc.h"

/* ------------------------------------------------------------------------ */
/* Fake drive map (C: -> /c, Z: -> /) and personas                           */
/* ------------------------------------------------------------------------ */

static int cov_to_unix(void *ud, const char *in, char *out, size_t outsz)
{
    const char *base;
    size_t bl;
    size_t rl;
    (void)ud;
    if (in[0] == 'C') {
        base = "/c";
    } else if (in[0] == 'Z') {
        base = "";
    } else {
        return -1;
    }
    bl = strlen(base);
    rl = strlen(in + 2);
    if (bl + rl + 1u > outsz) {
        return -1;
    }
    memcpy(out, base, bl);
    for (size_t i = 0; i <= rl; i++) {
        out[bl + i] = in[2 + i] == '\\' ? '/' : in[2 + i];
    }
    return 0;
}

static int cov_to_win(void *ud, const char *in, char *out, size_t outsz)
{
    const char *prefix = "Z:";
    size_t pl;
    size_t n = strlen(in);
    (void)ud;
    if (strncmp(in, "/nomap", 6) == 0) {
        return -1;
    }
    if (strncmp(in, "/verbatim", 9) == 0) {
        prefix = "\\\\?\\Z:";
    } else if (strncmp(in, "/bad", 4) == 0) {
        prefix = "\\\\?\\Q";
    } else if (strncmp(in, "/root", 5) == 0) {
        in = "";
        n = 0;
        prefix = "R:";
    }
    pl = strlen(prefix);
    if (pl + n + 1u > outsz) {
        return -1;
    }
    memcpy(out, prefix, pl);
    for (size_t i = 0; i <= n; i++) {
        out[pl + i] = in[i] == '/' ? '\\' : in[i];
    }
    return 0;
}

static struct fl_xlate cov_x(void)
{
    struct fl_xlate x;
    memset(&x, 0, sizeof(x));
    x.to_unix = cov_to_unix;
    x.to_win = cov_to_win;
    x.winexec = "/fl/winexec";
    x.askpass = "/fl/askpass";
    x.ssh_askpass = "/fl/ssh-askpass";
    return x;
}

/* ------------------------------------------------------------------------ */
/* Small string helpers                                                      */
/* ------------------------------------------------------------------------ */

#define MAXITEMS 64

/* Split "a | b | c" into items (copies in buf); "(none)" is the empty list. */
static int split(const char *s, char *buf, size_t bufsz, char **items)
{
    int n = 0;
    size_t len = strlen(s);
    char *p = buf;
    if (len + 1u > bufsz) {
        return -1;
    }
    memcpy(buf, s, len + 1u);
    if (strcmp(buf, "(none)") == 0) {
        items[0] = NULL;
        return 0;
    }
    while (n < MAXITEMS - 1) {
        char *sep = strstr(p, " | ");
        items[n] = p;
        n++;
        if (sep == NULL) {
            break;
        }
        *sep = '\0';
        p = sep + 3;
    }
    items[n] = NULL;
    return n;
}

struct text {
    char buf[8192];
    size_t len;
};

static void text_add(struct text *t, const char *s, size_t n)
{
    for (size_t i = 0; i < n && t->len + 5u < sizeof(t->buf); i++) {
        unsigned char c = (unsigned char)s[i];
        if (c == '\n') {
            t->buf[t->len++] = '\\';
            t->buf[t->len++] = 'n';
        } else if (c == '\t') {
            t->buf[t->len++] = '\\';
            t->buf[t->len++] = 't';
        } else if (c < 0x20u || c >= 0x7fu) {
            static const char hex[] = "0123456789abcdef";
            t->buf[t->len++] = '\\';
            t->buf[t->len++] = 'x';
            t->buf[t->len++] = hex[c >> 4];
            t->buf[t->len++] = hex[c & 15u];
        } else {
            t->buf[t->len++] = (char)c;
        }
    }
    t->buf[t->len] = '\0';
}

static void text_adds(struct text *t, const char *s)
{
    text_add(t, s, strlen(s));
}

static void text_list(struct text *t, const struct fl_strvec *v)
{
    if (v->n == 0) {
        text_adds(t, "(none)");
    }
    for (size_t i = 0; i < v->n; i++) {
        if (i > 0) {
            text_adds(t, " | ");
        }
        text_adds(t, v->v[i]);
    }
}

/* ------------------------------------------------------------------------ */
/* argv                                                                      */
/* ------------------------------------------------------------------------ */

static int describe_argv(const struct fl_xlate *x, const char *args, struct text *t)
{
    char buf[4096];
    char *items[MAXITEMS];
    struct fl_strvec out;
    struct fl_cmdinfo info;
    int n = split(args, buf, sizeof(buf), items);
    int rc;
    t->len = 0;
    t->buf[0] = '\0';
    if (n < 0) {
        return -1;
    }
    rc = fl_translate_argv(x, n, items, &out, &info);
    if (rc == 0) {
        char tail[96];
        text_list(t, &out);
        snprintf(tail, sizeof(tail), " => %s plan %d%s", info.subcmd, (int)info.out_plan,
                 info.stderr_paths ? " stderr" : "");
        text_adds(t, tail);
    }
    fl_strvec_free(&out);
    return rc;
}

static const char *const k_argv_cases[][2] = {
    { "-c | core.editor='C:\\Program Files\\ed.exe' -w | commit",
      "-c | core.editor='/fl/winexec' 'C:\\Program Files\\ed.exe' -w | commit => commit plan 0 stderr" },
    { "-c | core.editor='C:\\ed.exe | commit", "-c | core.editor='C:\\ed.exe | commit => commit plan 0 stderr" },
    { "-c | core.editor=\"C:/e d.exe\" \"a\\$b\\\\\\\"\\\nc\\x\" | commit", "-c | core.editor='/fl/winexec' 'C:/e d.exe' \"a\\$b\\\\\\\"\\\\nc\\x\" | commit => commit plan 0 stderr" },
    { "-c | core.editor=\"$EDITOR\" | commit", "-c | core.editor=\"$EDITOR\" | commit => commit plan 0 stderr" },
    { "-c | core.editor=`which ed`.exe | commit", "-c | core.editor=`which ed`.exe | commit => commit plan 0 stderr" },
    { "-c | core.editor=$E.exe | commit", "-c | core.editor=$E.exe | commit => commit plan 0 stderr" },
    { "-c | core.editor=\"C:/ed.exe | commit", "-c | core.editor=\"C:/ed.exe | commit => commit plan 0 stderr" },
    { "-c | core.editor=C:/ed.exe \\ | commit", "-c | core.editor='/fl/winexec' 'C:/ed.exe' \\ | commit => commit plan 0 stderr" },
    { "-c | core.editor=C:/ed.exe \\\n-w x\\ y | commit", "-c | core.editor='/fl/winexec' 'C:/ed.exe' \\\\n-w x\\ y | commit => commit plan 0 stderr" },
    { "-c | core.editor=# C:/ed.exe | commit", "-c | core.editor=# C:/ed.exe | commit => commit plan 0 stderr" },
    { "-c | core.editor=C:/ed.exe;ls | commit", "-c | core.editor=C:/ed.exe;ls | commit => commit plan 0 stderr" },
    { "-c | core.editor=  C:/ed.exe\t-w | commit", "-c | core.editor='/fl/winexec' 'C:/ed.exe'\\t-w | commit => commit plan 0 stderr" },
    { "-c | core.editor=vim | commit", "-c | core.editor=vim | commit => commit plan 0 stderr" },
    { "-c | sequence.editor=C:/ed.exe | rebase", "-c | sequence.editor='/fl/winexec' 'C:/ed.exe' | rebase => rebase plan 0 stderr" },
    { "-c | sequence.x.editor=C:/ed.exe | rebase", "-c | sequence.x.editor=/c/ed.exe | rebase => rebase plan 0 stderr" },
    { "-c | credential.helper=C:/gcm.exe | fetch", "-c | credential.helper=!'/fl/winexec' 'C:/gcm.exe' | fetch => fetch plan 0 stderr" },
    { "-c | credential.helper=gcm.exe | fetch", "-c | credential.helper=gcm.exe | fetch => fetch plan 0 stderr" },
    { "-c | credential.helper=!C:/gcm.exe --x | fetch", "-c | credential.helper=!'/fl/winexec' 'C:/gcm.exe' --x | fetch => fetch plan 0 stderr" },
    { "-c | credential.helper=!gcm.exe get | fetch", "-c | credential.helper=!'/fl/winexec' 'gcm.exe' get | fetch => fetch plan 0 stderr" },
    { "-c | credential.helper=!\"$X\" | fetch", "-c | credential.helper=!\"$X\" | fetch => fetch plan 0 stderr" },
    { "-c | credential.helper=manager | fetch", "-c | credential.helper=manager | fetch => fetch plan 0 stderr" },
    { "-c | credential.helper= | fetch", "-c | credential.helper= | fetch => fetch plan 0 stderr" },
    { "-c | core.sshCommand=C:/OpenSSH/ssh.exe -o \"IdentityFile = C:/k\" -i C:/id -iC:/id2 -oUserKnownHostsFile=C:/kh "
      "-F C:/cfg -p 22 -o Port=22 -E $LOG host C:/x -v | fetch",
      "-c | core.sshCommand=ssh -o 'IdentityFile = /c/k' -i /c/id -i/c/id2 -oUserKnownHostsFile=/c/kh -F /c/cfg -p 22 -o Port=22 -E $LOG host /c/x -v | fetch => fetch plan 0 stderr" },
    { "-c | core.sshCommand=ssh -v -oX=1 -i /k host | fetch", "-c | core.sshCommand=ssh -v -oX=1 -i /k host | fetch => fetch plan 0 stderr" },
    { "-c | core.sshCommand=ssh -i C:/k -S | fetch", "-c | core.sshCommand=ssh -i /c/k -S | fetch => fetch plan 0 stderr" },
    { "-c | core.sshCommand=C:/tools/plink.exe -batch | fetch", "-c | core.sshCommand=ssh | fetch => fetch plan 0 stderr" },
    { "-c | core.sshCommand=TortoisePlink.EXE | fetch", "-c | core.sshCommand=ssh | fetch => fetch plan 0 stderr" },
    { "-c | core.sshCommand=C:/tools/other.exe -x | fetch", "-c | core.sshCommand='/fl/winexec' 'C:/tools/other.exe' -x | fetch => fetch plan 0 stderr" },
    { "-c | core.sshCommand=$SSH -i C:/k | fetch", "-c | core.sshCommand=$SSH -i C:/k | fetch => fetch plan 0 stderr" },
    { "-c | core.sshCommand=ssh 'unterminated | fetch", "-c | core.sshCommand=ssh 'unterminated | fetch => fetch plan 0 stderr" },
    { "-c | core.fsmonitor=+1 | status", "status => status plan 0 stderr" },
    { "-c | core.fsmonitor=-0 | status", "-c | core.fsmonitor=-0 | status => status plan 0 stderr" },
    { "-c | core.fsmonitor=12a | status", "-c | core.fsmonitor=12a | status => status plan 0 stderr" },
    { "-c | core.fsmonitor=+ | status", "-c | core.fsmonitor=+ | status => status plan 0 stderr" },
    { "-c | core.fsmonitor=On | status", "status => status plan 0 stderr" },
    { "-c | core.fsmonitor | status", "status => status plan 0 stderr" },
    { "-c | core.fsmonitor=C:/fsm.exe | status", "status => status plan 0 stderr" },
    { "-c | core.fsmonitor=false | status", "-c | core.fsmonitor=false | status => status plan 0 stderr" },
    { "-c | core.askPass=C:/a.exe | fetch", "-c | core.askPass=/fl/askpass | fetch => fetch plan 0 stderr" },
    { "-c | core.askPass=/usr/bin/ksshaskpass | fetch", "-c | core.askPass=/usr/bin/ksshaskpass | fetch => fetch plan 0 stderr" },
    { "-c | http.sslCAInfo=C:/Fork/gitInstance/x/ca.crt | fetch", "fetch => fetch plan 0 stderr" },
    { "-c | http.sslCAPath=C:/certs | fetch", "-c | http.sslCAPath=/c/certs | fetch => fetch plan 0 stderr" },
    { "-c | http.https://x.sslCAInfo=C:/Fork/gitInstance/ca | fetch", "fetch => fetch plan 0 stderr" },
    { "-c | gpg.program=C:/gpg.exe | log", "log => log plan 0 stderr" },
    { "-c | gpg.program=gpg2 | log", "-c | gpg.program=gpg2 | log => log plan 0 stderr" },
    { "-c | ssh.variant=putty | fetch", "fetch => fetch plan 0 stderr" },
    { "-c | ssh.variant=ssh | fetch", "-c | ssh.variant=ssh | fetch => fetch plan 0 stderr" },
    { "-c | http.schannelCheckRevoke=false | fetch", "fetch => fetch plan 0 stderr" },
    { "-c | http.sslBackend=openssl | fetch", "fetch => fetch plan 0 stderr" },
    { "-c | core.longpaths=true | status", "status => status plan 0 stderr" },
    { "-c | core.fscache | status", "status => status plan 0 stderr" },
    { "-c | nodot | status", "-c | nodot | status => status plan 0 stderr" },
    { "-c | include.path=C:/inc | status", "-c | include.path=/c/inc | status => status plan 0 stderr" },
    { "-c | x.y=D:/unmapped | status", "-c | x.y=D:/unmapped | status => status plan 0 stderr" },
    { "clone | --config=core.autocrlf=false | --config=http.sslBackend=schannel | --config=core.hooksPath=C:/h | -c | "
      "core.editor=C:/e.exe | --config | x.y=z | -c | C:/src | C:/dst",
      "clone | --config=core.autocrlf=false | --config=core.hooksPath=/c/h | -c | core.editor='/fl/winexec' 'C:/e.exe' | --config | x.y=z | -c | C:/src | /c/dst => clone plan 0 stderr" },
    { "clone | -c", "clone | -c => clone plan 0 stderr" },
    { "--exec-path=/usr/lib/git-core | version", "--exec-path=/usr/lib/git-core | version => version plan 0" },
    { "--exec-path=C:/git/libexec | version", "version => version plan 0" },
    { "--html-path", "--html-path =>  plan 1" },
    { "commit | -am | C:\\msg", "commit | -am | C:\\msg => commit plan 0 stderr" },
    { "commit | -a1 | C:\\x", "commit | -a1 | /c/x => commit plan 0 stderr" },
    { "log | -S | C:\\x | -G | C:\\y", "log | -S | C:\\x | -G | C:\\y => log plan 0 stderr" },
    { "show | -S | C:\\x", "show | -S | C:\\x => show plan 0 stderr" },
    { "grep | -e | C:\\x", "grep | -e | C:\\x => grep plan 0 stderr" },
    { "merge | -X | C:\\x", "merge | -X | C:\\x => merge plan 0 stderr" },
    { "status | -X | C:\\x", "status | -X | /c/x => status plan 0 stderr" },
    { "log | --author | C:\\x | --grep", "log | --author | C:\\x | --grep => log plan 0 stderr" },
    { "commit | -m", "commit | -m => commit plan 0 stderr" },
    { "-c", "-c =>  plan 0" },
    { "--namespace", "--namespace =>  plan 0" },
    { "--namespace | ns | --super-prefix | C:/p | status", "--namespace | ns | --super-prefix | C:/p | status => status plan 0 stderr" },
    { "--git-dir | C:/r/.git | -C", "--git-dir | /c/r/.git | -C =>  plan 0" },
    { "status | --end-of-options | C:/x | -C | --output=C:/o | --nope=C:/x | --=C:/y", "status | --end-of-options | /c/x | -C | --output=C:/o | --nope=C:/x | --=C:/y => status plan 0 stderr" },
    { "log | -o | C:/x | -o | rel | -F | C:/m", "log | -o | /c/x | -o | rel | -F | /c/m => log plan 0 stderr" },
    { "config | get | core.hooksPath", "config | get | core.hooksPath => config plan 1 stderr" },
    { "config | get", "config | get => config plan 0 stderr" },
    { "config | set | a.b | c", "config | set | a.b | c => config plan 0 stderr" },
    { "config | list", "config | list => config plan 3 stderr" },
    { "config | --get-regexp | remote.*.url", "config | --get-regexp | remote.*.url => config plan 3 stderr" },
    { "config | --get-regexp | user", "config | --get-regexp | user => config plan 0 stderr" },
    { "config | --get-regexp", "config | --get-regexp => config plan 0 stderr" },
    { "config | --regexp | remote | get", "config | --regexp | remote | get => config plan 0 stderr" },
    { "config | -- | core.hooksPath", "config | -- | core.hooksPath => config plan 1 stderr" },
    { "config | --type | path | --get | x.y", "config | --type | path | --get | x.y => config plan 1 stderr" },
    { "config | -t | path | x.y", "config | -t | path | x.y => config plan 1 stderr" },
    { "config | -t", "config | -t => config plan 0 stderr" },
    { "config | --type=path | x.y", "config | --type=path | x.y => config plan 1 stderr" },
    { "config | --path | x.y", "config | --path | x.y => config plan 1 stderr" },
    { "config | -t | bool | a.b", "config | -t | bool | a.b => config plan 0 stderr" },
    { "config | --list | --get", "config | --list | --get => config plan 0 stderr" },
    { "config | -l", "config | -l => config plan 3 stderr" },
    { "config | --unset | remote.o.url", "config | --unset | remote.o.url => config plan 0 stderr" },
    { "config | remote.origin.pushurl", "config | remote.origin.pushurl => config plan 3 stderr" },
    { "config | remote.url", "config | remote.url => config plan 0 stderr" },
    { "config | includeIf.gitdir:C:/x.path", "config | includeIf.gitdir:C:/x.path => config plan 1 stderr" },
    { "config | includeif.path", "config | includeif.path => config plan 0 stderr" },
    { "config | --get-all | core.excludesFile", "config | --get-all | core.excludesFile => config plan 1 stderr" },
    { "config | --all | user.name", "config | --all | user.name => config plan 0 stderr" },
    { "config | --show-origin | --list", "config | --show-origin | --list => config plan 4 stderr" },
    { "config | -f | C:/cfg | a.b | c | d", "config | -f | /c/cfg | a.b | c | d => config plan 0 stderr" },
    { "config | a.b | c", "config | a.b | c => config plan 0 stderr" },
    { "worktree | -- | list", "worktree | -- | list => worktree plan 0 stderr" },
    { "worktree | list", "worktree | list => worktree plan 2 stderr" },
    { "worktree | add | x", "worktree | add | x => worktree plan 0 stderr" },
    { "worktree", "worktree => worktree plan 0 stderr" },
    { "remote | -v", "remote | -v => remote plan 3 stderr" },
    { "remote | --verbose | -- | x", "remote | --verbose | -- | x => remote plan 3 stderr" },
    { "remote | get-url | origin", "remote | get-url | origin => remote plan 3 stderr" },
    { "remote | show", "remote | show => remote plan 0 stderr" },
    { "remote", "remote => remote plan 0 stderr" },
    { "ls-remote | -- | --get-url", "ls-remote | -- | --get-url => ls-remote plan 0 stderr" },
    { "ls-remote | --get-url", "ls-remote | --get-url => ls-remote plan 3 stderr" },
    { "ls-remote", "ls-remote => ls-remote plan 0 stderr" },
    { "branch | -- | --format=%(worktreepath)", "branch | -- | --format=%(worktreepath) => branch plan 0 stderr" },
    { "branch | --format=%(worktreepath) %(refname)", "branch | --format=%(worktreepath) %(refname) => branch plan 2 stderr" },
    { "tag | --format | %(worktreepath)", "tag | --format | %(worktreepath) => tag plan 2 stderr" },
    { "for-each-ref | --format", "for-each-ref | --format => for-each-ref plan 0 stderr" },
    { "for-each-ref | --format=%(refname)", "for-each-ref | --format=%(refname) => for-each-ref plan 0 stderr" },
    { "rev-parse | --show-toplevel", "rev-parse | --show-toplevel => rev-parse plan 1 stderr" },
    { "help", "help => help plan 0" },
    { "-h", "-h => help plan 0" },
};

static void test_argv_cases(void)
{
    struct fl_xlate x = cov_x();
    struct text t;
    for (size_t i = 0; i < sizeof(k_argv_cases) / sizeof(k_argv_cases[0]); i++) {
        int rc = describe_argv(&x, k_argv_cases[i][0], &t);
        tap_ok(rc == 0, "argv case %zu translates", i);
        tap_str_eq(t.buf, k_argv_cases[i][1], "argv case %zu", i);
    }
}

/* ------------------------------------------------------------------------ */
/* env                                                                       */
/* ------------------------------------------------------------------------ */

static int describe_env(const struct fl_xlate *x, const char *envs, struct text *t)
{
    char buf[4096];
    char *items[MAXITEMS];
    struct fl_envops ops;
    int n = split(envs, buf, sizeof(buf), items);
    int rc;
    t->len = 0;
    t->buf[0] = '\0';
    if (n < 0) {
        return -1;
    }
    rc = fl_translate_env(x, items, &ops);
    if (rc == 0) {
        text_adds(t, "set: ");
        text_list(t, &ops.set);
        text_adds(t, " // unset: ");
        text_list(t, &ops.unset);
    }
    fl_strvec_free(&ops.set);
    fl_strvec_free(&ops.unset);
    return rc;
}

static const char *const k_env_cases[][2] = {
    { "GIT_SSH_COMMAND=C:/ssh.exe -i C:/k | GIT_SSH=C:/plink.exe | GIT_SSH_VARIANT=ssh", "set: GIT_SSH_COMMAND=ssh -i /c/k // unset: GIT_SSH | GIT_SSH_VARIANT" },
    { "GIT_SSH=/usr/bin/ssh | GIT_SSH_VARIANT=plink | GIT_SSH_COMMAND=ssh -v", "set: GIT_SSH=/usr/bin/ssh | GIT_SSH_COMMAND=ssh -v // unset: GIT_SSH_VARIANT" },
    { "GIT_SSH_VARIANT=ssh | GIT_SSH_COMMAND= | GIT_EDITOR= | GIT_SEQUENCE_EDITOR=C:/e.exe", "set: GIT_SSH_VARIANT=ssh | GIT_SSH_COMMAND= | GIT_EDITOR= | GIT_SEQUENCE_EDITOR='/fl/winexec' 'C:/e.exe' // unset: (none)" },
    { "GIT_CONFIG_PARAMETERS='core.editor'='C:/e.exe' 'a.b' 'http.sslbackend=schannel' 'x.y'= 'p.q=C:/v'", "set: GIT_CONFIG_PARAMETERS='core.editor'=''\\''/fl/winexec'\\'' '\\''C:/e.exe'\\''' 'a.b' 'x.y' 'p.q'='/c/v' // unset: (none)" },
    { "GIT_CONFIG_PARAMETERS='http.sslbackend'='schannel'", "set: (none) // unset: GIT_CONFIG_PARAMETERS" },
    { "GIT_CONFIG_PARAMETERS=  'a.b'='c'  ", "set: GIT_CONFIG_PARAMETERS=  'a.b'='c'   // unset: (none)" },
    { "GIT_CONFIG_PARAMETERS='a.b'x", "set: GIT_CONFIG_PARAMETERS='a.b'x // unset: (none)" },
    { "GIT_CONFIG_PARAMETERS='a.b'='c'x", "set: GIT_CONFIG_PARAMETERS='a.b'='c'x // unset: (none)" },
    { "GIT_CONFIG_PARAMETERS='a.b'=c", "set: GIT_CONFIG_PARAMETERS='a.b'=c // unset: (none)" },
    { "GIT_CONFIG_PARAMETERS='a.b'='c", "set: GIT_CONFIG_PARAMETERS='a.b'='c // unset: (none)" },
    { "GIT_CONFIG_PARAMETERS='a.b", "set: GIT_CONFIG_PARAMETERS='a.b // unset: (none)" },
    { "GIT_CONFIG_PARAMETERS=a.b", "set: GIT_CONFIG_PARAMETERS=a.b // unset: (none)" },
    { "GIT_CONFIG_PARAMETERS='it'\\''s.x'='a'\\!'b' 'core.askpass'='C:/a.exe'", "set: GIT_CONFIG_PARAMETERS='it'\\''s.x'='a'\\!'b' 'core.askpass'='/fl/askpass' // unset: (none)" },
    { "GIT_CONFIG_PARAMETERS='a'\\x'", "set: GIT_CONFIG_PARAMETERS='a'\\x' // unset: (none)" },
    { "GIT_CONFIG_COUNT=12 | GIT_CONFIG_KEY_0=a.b | GIT_CONFIG_VALUE_0=0 | GIT_CONFIG_KEY_1=core.fscache | "
      "GIT_CONFIG_VALUE_1=true | GIT_CONFIG_KEY_2=c.d | GIT_CONFIG_VALUE_2=2 | GIT_CONFIG_KEY_3=c.d | "
      "GIT_CONFIG_VALUE_3=3 | GIT_CONFIG_KEY_4=c.d | GIT_CONFIG_VALUE_4=4 | GIT_CONFIG_KEY_5=c.d | "
      "GIT_CONFIG_VALUE_5=5 | GIT_CONFIG_KEY_6=c.d | GIT_CONFIG_VALUE_6=6 | GIT_CONFIG_KEY_7=c.d | "
      "GIT_CONFIG_VALUE_7=7 | GIT_CONFIG_KEY_8=c.d | GIT_CONFIG_VALUE_8=8 | GIT_CONFIG_KEY_9=c.d | "
      "GIT_CONFIG_VALUE_9=9 | GIT_CONFIG_KEY_10=core.hooksPath | GIT_CONFIG_VALUE_10=C:/h | "
      "GIT_CONFIG_KEY_11=c.d | GIT_CONFIG_VALUE_11=11 | git_config_key_12=x.y | GIT_CONFIG_KEY_X=1",
      "set: GIT_CONFIG_KEY_0=a.b | GIT_CONFIG_VALUE_0=0 | GIT_CONFIG_KEY_1=c.d | GIT_CONFIG_VALUE_1=2 | GIT_CONFIG_KEY_2=c.d | GIT_CONFIG_VALUE_2=3 | GIT_CONFIG_KEY_3=c.d | GIT_CONFIG_VALUE_3=4 | GIT_CONFIG_KEY_4=c.d | GIT_CONFIG_VALUE_4=5 | GIT_CONFIG_KEY_5=c.d | GIT_CONFIG_VALUE_5=6 | GIT_CONFIG_KEY_6=c.d | GIT_CONFIG_VALUE_6=7 | GIT_CONFIG_KEY_7=c.d | GIT_CONFIG_VALUE_7=8 | GIT_CONFIG_KEY_8=c.d | GIT_CONFIG_VALUE_8=9 | GIT_CONFIG_KEY_9=core.hooksPath | GIT_CONFIG_VALUE_9=/c/h | GIT_CONFIG_KEY_10=c.d | GIT_CONFIG_VALUE_10=11 | GIT_CONFIG_COUNT=11 | git_config_key_12=x.y | GIT_CONFIG_KEY_X=1 // unset: (none)" },
    { "GIT_CONFIG_COUNT=2 | GIT_CONFIG_KEY_0=a.b | GIT_CONFIG_VALUE_0=1 | GIT_CONFIG_KEY_1=c.d", "set: GIT_CONFIG_COUNT=2 | GIT_CONFIG_KEY_0=a.b | GIT_CONFIG_VALUE_0=1 | GIT_CONFIG_KEY_1=c.d // unset: (none)" },
    { "GIT_CONFIG_COUNT=01 | GIT_CONFIG_KEY_0=a.b | GIT_CONFIG_VALUE_0=1", "set: GIT_CONFIG_COUNT=01 | GIT_CONFIG_KEY_0=a.b | GIT_CONFIG_VALUE_0=1 // unset: (none)" },
    { "GIT_CONFIG_COUNT=99999 | GIT_CONFIG_KEY_0=a.b", "set: GIT_CONFIG_COUNT=99999 | GIT_CONFIG_KEY_0=a.b // unset: (none)" },
    { "GIT_CONFIG_COUNT=x", "set: GIT_CONFIG_COUNT=x // unset: (none)" },
    { "GIT_CONFIG_COUNT=0", "set: GIT_CONFIG_COUNT=0 // unset: (none)" },
    { "FL_ASKPASS_TARGET=old | GIT_ASKPASS=C:/a.exe | FL_SSH_ASKPASS_TARGET=old2 | SSH_ASKPASS=C:/s.exe", "set: GIT_ASKPASS=/fl/askpass | FL_ASKPASS_TARGET=C:/a.exe | SSH_ASKPASS=/fl/ssh-askpass | FL_SSH_ASKPASS_TARGET=C:/s.exe // unset: (none)" },
    { "FL_ASKPASS_TARGET=old | FL_SSH_ASKPASS_TARGET=old2 | GIT_ASKPASS=/usr/bin/x | SSH_ASKPASS=/usr/bin/y", "set: GIT_ASKPASS=/usr/bin/x | SSH_ASKPASS=/usr/bin/y // unset: FL_ASKPASS_TARGET | FL_SSH_ASKPASS_TARGET" },
    { "GIT_ALTERNATE_OBJECT_DIRECTORIES=C:/a;D:/b;rel;Z:/z | GIT_CEILING_DIRECTORIES=/x", "set: GIT_ALTERNATE_OBJECT_DIRECTORIES=/c/a:rel:/z | GIT_CEILING_DIRECTORIES=/x // unset: (none)" },
    { "GIT_CEILING_DIRECTORIES=C:/only | GIT_ALTERNATE_OBJECT_DIRECTORIES=;", "set: GIT_CEILING_DIRECTORIES=/c/only | GIT_ALTERNATE_OBJECT_DIRECTORIES=: // unset: (none)" },
    { "GIT_TEMPLATE_DIR=C:/Fork/gitInstance/t | GIT_CONFIG_SYSTEM=/etc/x | GIT_CONFIG_GLOBAL=C:/g | git_dir=c:\\r",
      "set: GIT_CONFIG_SYSTEM=/etc/x | GIT_CONFIG_GLOBAL=/c/g | GIT_DIR=/c/r // unset: GIT_TEMPLATE_DIR" },
    { "=C:=C:\\x | NOEQ | A=1 | msys2_x=1 | mingw_=1", "set: A=1 | mingw_=1 // unset: MSYS2_X" },
};

static void test_env_cases(void)
{
    struct fl_xlate x = cov_x();
    struct text t;
    for (size_t i = 0; i < sizeof(k_env_cases) / sizeof(k_env_cases[0]); i++) {
        int rc = describe_env(&x, k_env_cases[i][0], &t);
        tap_ok(rc == 0, "env case %zu translates", i);
        tap_str_eq(t.buf, k_env_cases[i][1], "env case %zu", i);
    }
}

/* ------------------------------------------------------------------------ */
/* output                                                                    */
/* ------------------------------------------------------------------------ */

struct outbuf {
    char buf[8192];
    size_t len;
};

static void out_emit(void *ud, const char *p, size_t n)
{
    struct outbuf *b = ud;
    if (n > sizeof(b->buf) - b->len) {
        n = sizeof(b->buf) - b->len;
    }
    memcpy(b->buf + b->len, p, n);
    b->len += n;
}

/* Feed `in` in chunks of `chunk` bytes (0: whole) through a fresh fl_out; describe the output. */
static void describe_out(const struct fl_xlate *x, enum fl_outplan plan, int nul, const char *in, size_t n,
                         size_t chunk, struct text *t)
{
    struct fl_out o;
    struct outbuf b;
    b.len = 0;
    fl_out_init(&o, x, plan, nul);
    (void)fl_out_add_anchor(&o, "/anch", "C:\\Anch");
    for (size_t i = 0; i < n;) {
        size_t k = chunk == 0 || n - i < chunk ? n - i : chunk;
        fl_out_feed(&o, in + i, k, out_emit, &b);
        i += k;
    }
    fl_out_flush(&o, out_emit, &b);
    fl_out_free(&o);
    t->len = 0;
    t->buf[0] = '\0';
    text_add(t, b.buf, b.len);
}

struct out_case {
    enum fl_outplan plan;
    const char *in;
    const char *want;
};

static const struct out_case k_out_cases[] = {
    { FL_OUT_WORKTREE, "\"/home/u/a\\tb\\a\\b\\v\\f\\r\\n\\\"\\\\\\303\\251\" 1234abcd [main]\n", "\"Z:/home/u/a\\tb\\a\\b\\v\\f\\r\\n\\\"\\\\\\303\\251\" 1234abcd [main]\\n" },
    { FL_OUT_WORKTREE, "\"/home/u/\xc3\xa9\" 1234abcd [main]\n", "\"Z:/home/u/\\xc3\\xa9\" 1234abcd [main]\\n" },
    { FL_OUT_WORKTREE, "\"/x\\q\" abcd1234 [m]\n", "\"/x\\q\" abcd1234 [m]\\n" },
    { FL_OUT_WORKTREE, "\"/x\\1\" abcd1234 [m]\n", "\"/x\\1\" abcd1234 [m]\\n" },
    { FL_OUT_WORKTREE, "\"/x\\19\" abcd1234 [m]\n", "\"/x\\19\" abcd1234 [m]\\n" },
    { FL_OUT_WORKTREE, "\"/x\\\n", "\"/x\\\\n" },
    { FL_OUT_WORKTREE, "\"/x\" abcd [m]\n", "\"Z:/x\" abcd [m]\\n" },
    { FL_OUT_WORKTREE, "\"/x\"\tmore\n", "\"Z:/x\"\\tmore\\n" },
    { FL_OUT_WORKTREE, "\"/x\"y\n", "\"/x\"y\\n" },
    { FL_OUT_WORKTREE, "\"rel\" abcd1234 [m]\n", "\"rel\" abcd1234 [m]\\n" },
    { FL_OUT_WORKTREE, "worktree /anch/sub\nworktree rel\n", "worktree C:/Anch/sub\\nworktree rel\\n" },
    { FL_OUT_WORKTREE, "/home/u  (bare)\n/home/v  1234abcd (detached HEAD)\n/home/w 12 x\n/a b  abcd12345\n", "Z:/home/u  (bare)\\nZ:/home/v  1234abcd (detached HEAD)\\nZ:/home/w 12 x\\nZ:/a b  abcd12345\\n" },
    { FL_OUT_WORKTREE, "/home/u\tx\nrel\n", "Z:/home/u\\tx\\nrel\\n" },
    { FL_OUT_SHOW_ORIGIN, "file:\"/home/u/.gitconfig\"\tx=y\n", "file:\"Z:/home/u/.gitconfig\"\\tx=y\\n" },
    { FL_OUT_SHOW_ORIGIN, "global\tfile:/home/u/c\tx\nbogus\tfile:/x\nlocal\tblob:x\tx\n", "global\\tfile:Z:/home/u/c\\tx\\nbogus\\tfile:/x\\nlocal\\tblob:x\\tx\\n" },
    { FL_OUT_SHOW_ORIGIN, "file:relative\tx\nfile:\"rel\"\tx\nnotab\n", "file:relative\\tx\\nfile:\"rel\"\\tx\\nnotab\\n" },
    { FL_OUT_REMOTE, "origin\t/home/u/r (fetch)\norigin\t/home/u/r (push)\norigin\t/x [blob:none]\n", "origin\\tZ:/home/u/r (fetch)\\norigin\\tZ:/home/u/r (push)\\norigin\\tZ:/x [blob:none]\\n" },
    { FL_OUT_REMOTE, "remote.origin.url=file:///home/u/r\nremote.o.url /x\nremote.o.fetch=x\nremote.\n", "remote.origin.url=file:///Z:/home/u/r\\nremote.o.url Z:/x\\nremote.o.fetch=x\\nremote.\\n" },
    { FL_OUT_REMOTE, "https://x\nfile:///\nFILE:///y\n/z\nremote.x.url\n", "https://x\\nfile:///\\nfile:///Z:/y\\nZ:/z\\nremote.x.url\\n" },
    { FL_OUT_PATHS_LINES, "fatal: '/home/u/x' and '/home/v' \n'/no close\n'rel'\nx '/' y '/a' z\n", "fatal: 'Z:/home/u/x' and 'Z:/home/v' \\n'/no close\\n'rel'\\nx 'Z:/' y 'Z:/a' z\\n" },
    { FL_OUT_PATHS_LINES, "/verbatim/x\n/bad/x\n/root\n/nomap/q\n/anch\n/anchx\n/a\\b\n", "Z:/verbatim/x\\nZ:/bad/x\\nR:/\\nZ:/nomap/q\\nC:/Anch\\nZ:/anchx\\nZ:/a\\b\\n" },
    { FL_OUT_PATHS_LINES, "/x\r/y\r\n", "Z:/x\\x0dZ:/y\\x0d\\n" },
};

static void test_out_cases(void)
{
    struct fl_xlate x = cov_x();
    struct text t;
    struct text t1;
    for (size_t i = 0; i < sizeof(k_out_cases) / sizeof(k_out_cases[0]); i++) {
        const char *in = k_out_cases[i].in;
        size_t n = strlen(in);
        describe_out(&x, k_out_cases[i].plan, 0, in, n, 0, &t);
        describe_out(&x, k_out_cases[i].plan, 0, in, n, 1, &t1);
        tap_str_eq(t.buf, k_out_cases[i].want, "out case %zu", i);
        tap_str_eq(t1.buf, t.buf, "out case %zu byte by byte", i);
    }
}

/* ------------------------------------------------------------------------ */
/* Out of memory: every allocation of every case fails once                  */
/* ------------------------------------------------------------------------ */

static void oom_argv(void *ud)
{
    struct fl_xlate x = cov_x();
    struct text t;
    (void)describe_argv(&x, ud, &t);
}

static void oom_env(void *ud)
{
    struct fl_xlate x = cov_x();
    struct text t;
    (void)describe_env(&x, ud, &t);
}

static void oom_out(void *ud)
{
    const struct out_case *c = ud;
    struct fl_xlate x = cov_x();
    struct text t;
    describe_out(&x, c->plan, 0, c->in, strlen(c->in), 3, &t);
}

/* A record split over several feeds is buffered: its later pieces need buffer growth. */
static void oom_out_buffered(void *ud)
{
    static const char rec[] = "'/home/u/one' and '/home/u/two' and a long tail that keeps growing past 256 bytes "
                              "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
                              "yyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyy"
                              "zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz\n"
                              "/home/v\n/home/w";
    struct fl_xlate x = cov_x();
    struct text t;
    (void)ud;
    describe_out(&x, FL_OUT_PATHS_LINES, 0, rec, sizeof(rec) - 1u, 40, &t);
}

static void test_oom(void)
{
    long runs = 0;
    for (size_t i = 0; i < sizeof(k_argv_cases) / sizeof(k_argv_cases[0]); i++) {
        runs += fl_alloc_sweep(oom_argv, (void *)k_argv_cases[i][0]);
    }
    tap_ok(runs > 0, "argv: %ld allocation failures survived", runs);
    runs = 0;
    for (size_t i = 0; i < sizeof(k_env_cases) / sizeof(k_env_cases[0]); i++) {
        runs += fl_alloc_sweep(oom_env, (void *)k_env_cases[i][0]);
    }
    tap_ok(runs > 0, "env: %ld allocation failures survived", runs);
    runs = 0;
    for (size_t i = 0; i < sizeof(k_out_cases) / sizeof(k_out_cases[0]); i++) {
        runs += fl_alloc_sweep(oom_out, (void *)&k_out_cases[i]);
    }
    runs += fl_alloc_sweep(oom_out_buffered, NULL);
    tap_ok(runs > 0, "out: %ld allocation failures survived", runs);
}

void test_translate_cov(void)
{
    test_argv_cases();
    test_env_cases();
    test_out_cases();
    test_oom();
}
