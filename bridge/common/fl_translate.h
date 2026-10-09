/*
 * fl_translate.h - git command-line, environment and output translation between Fork's
 * Windows view (C:\..., Z:\...) and native Linux git. Pure, portable C11: no Win32 calls.
 * Path conversions go through callbacks (wine_get_unix_file_name / wine_get_dos_file_name
 * in the shim, fake drive maps in the unit tests). All strings are UTF-8; UTF-16 only
 * exists at the Win32 boundary in the shim.
 */
#ifndef FL_TRANSLATE_H
#define FL_TRANSLATE_H

#include <stddef.h>

/*
 * Path callback: 0 ok (out holds a NUL-terminated result), -1 cannot map.
 *   to_unix receives a normalised Windows path: uppercase drive, '\' separators, no repeated
 *     separators ("C:\Users\me\repo"); it returns an absolute Unix path. Mapping a path that
 *     does not exist (longest existing ancestor + rest) is the callback's job.
 *   to_win receives an absolute Unix path and returns "X:\..." or "X:/..." (a "\\?\X:\..."
 *     prefix is accepted; "\\?\unix\..." counts as "cannot map").
 */
typedef int (*fl_path_fn)(void *ud, const char *in, char *out, size_t outsz);

struct fl_xlate {
    fl_path_fn to_unix;
    fl_path_fn to_win;
    void *ud;
    const char *winexec;     /* Unix path of the fl-winexec persona (NULL: Windows programs are not wrapped) */
    const char *askpass;     /* Unix path of the fl-askpass persona */
    const char *ssh_askpass; /* Unix path of the fl-ssh-askpass persona */
};

/* 1 for 'C:\x', 'c:/x', '\\?\C:\x', '\\?\unix\x', 'file://C:/x' and 'file:///C:/x', else 0.
   "C:x" (drive-relative), UNC paths, "HEAD:file", ":/" and URLs of other schemes are not. */
int fl_is_win_abs(const char *s);

/*
 * Convert one Windows-absolute path (any form fl_is_win_abs() accepts) to Unix form.
 * Normalises separators and prefixes, then calls x->to_unix; "\\?\unix\..." maps directly
 * without the callback, and "file://C:/x" becomes "file:///<unix path>". Returns 0, or -1
 * (not Windows-absolute, unmappable, or does not fit in outsz; out is then "").
 */
int fl_win_to_unix(const struct fl_xlate *x, const char *in, char *out, size_t outsz);

/* A growable string vector; v is NULL-terminated (v[n] == NULL) whenever it is non-NULL. */
struct fl_strvec {
    size_t n, cap;
    char **v;
};
/* Free every string and the array, and zero the struct (NULL and zeroed structs are fine). */
void fl_strvec_free(struct fl_strvec *v);

enum fl_outplan {
    FL_OUT_NONE = 0,        /* pure passthrough, no buffering */
    FL_OUT_PATHS_LINES = 1, /* rev-parse etc.; also stderr: whole-line paths and '<path>' quoted ones */
    FL_OUT_WORKTREE = 2,    /* worktree list (default, --porcelain, -z); %(worktreepath)-first formats */
    FL_OUT_REMOTE = 3,      /* remote -v, remote get-url, ls-remote --get-url, config remote.*.url */
    FL_OUT_SHOW_ORIGIN = 4  /* config --show-origin ("file:<path>") */
};

struct fl_cmdinfo {
    char subcmd[64];         /* first non-option word after git's global options ("" if none) */
    enum fl_outplan out_plan;
    int stderr_paths;        /* 1: pass stderr through an fl_out with FL_OUT_PATHS_LINES */
};

/*
 * Translate git's arguments (argv[0] excluded, UTF-8) for native git. out receives the new
 * argument list (malloc'd strings; free with fl_strvec_free), info the subcommand and the
 * output plan. Rules:
 *   - whole-argument Windows-absolute paths are translated (unmappable ones are kept);
 *   - --opt=<abs> only for the allowlist (--git-dir --work-tree --file --template --reference
 *     --reference-if-able --separate-git-dir --output --output-directory --pathspec-from-file
 *     --contents --ignore-revs-file --index-output --exclude-from --orderfile --resolve-git-dir);
 *   - the value after -C -F -o --git-dir --work-tree --file --output --template --reference;
 *   - never the values of -m --message -e --grep --author --committer -S -G --format --pretty
 *     --date --since --until --before --after -X --strategy-option --notes --trailer --exclude
 *     --glob --decorate-refs*, URLs (other than file://), scp-like user@host:path, refspecs or
 *     pathspec magic; after "--" only whole Windows-absolute arguments;
 *   - "-c key=value" (and clone's -c/--config) goes through the config sanitizer: editors are
 *     wrapped as '<winexec>' '<exe>', credential helpers as !'<winexec>' '<exe>', core.askPass
 *     becomes the askpass persona (git runs it without a shell), core.sshCommand becomes native
 *     ssh; http.sslBackend, http.schannel*, core.fscache, core.longpaths, core.fsmonitor=true,
 *     gitInstance CA bundles, Windows gpg programs and plink ssh.variant are dropped; any other
 *     value is translated when it is a whole Windows-absolute path. Empty values are kept.
 * Returns 0, or -1 (bad arguments or out of memory; out is then freed).
 */
int fl_translate_argv(const struct fl_xlate *x, int argc, char **argv, struct fl_strvec *out,
                      struct fl_cmdinfo *info);

/*
 * Translate the Windows environment (UTF-8 "K=V" strings, NULL-terminated) into operations for
 * the daemon: set ("K=V") and unset ("K"). Kept variables (FORK_*, LANG, LC_*, everything not
 * listed below) are copied to set unchanged. GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE
 * GIT_OBJECT_DIRECTORY GIT_COMMON_DIR GIT_CONFIG GIT_CONFIG_GLOBAL GIT_CONFIG_SYSTEM
 * GIT_TEMPLATE_DIR are translated when Windows-absolute, except that GIT_CONFIG_SYSTEM and
 * GIT_TEMPLATE_DIR pointing into a Git for Windows instance (a "gitInstance" path component;
 * Fork exports its bundled etc/gitconfig) are unset; GIT_ALTERNATE_OBJECT_DIRECTORIES and
 * GIT_CEILING_DIRECTORIES are split on ';' and joined with ':'; GIT_EDITOR / GIT_SEQUENCE_EDITOR
 * Windows programs are wrapped with winexec; GIT_ASKPASS / SSH_ASKPASS Windows programs become
 * the personas plus FL_ASKPASS_TARGET / FL_SSH_ASKPASS_TARGET; a Windows GIT_SSH is unset (with
 * GIT_SSH_VARIANT); GIT_SSH_COMMAND gets a native ssh; GIT_CONFIG_PARAMETERS and
 * GIT_CONFIG_COUNT/KEY_n/VALUE_n go through the config sanitizer. GIT_EXEC_PATH MSYSTEM MSYS
 * MSYS2_* MINGW_* MSYSTEM_* CHERE_INVOKING PLINK_PROTOCOL HOME PATH TEMP TMP and FL_BRIDGE_TOKEN
 * are not forwarded and are listed in unset (the daemon provides the Unix HOME/PATH/TMPDIR).
 * Names are matched case-insensitively (Windows semantics). Entries without '=' or with an empty
 * name ("=C:=C:\...") are skipped. *ops is overwritten; free both vectors with fl_strvec_free.
 * Returns 0, or -1 (bad arguments or out of memory; ops is then freed).
 */
struct fl_envops {
    struct fl_strvec set, unset;
};
int fl_translate_env(const struct fl_xlate *x, char **envp, struct fl_envops *ops);

/*
 * Output translation (git's Unix paths back to Fork's Windows view). The output form is
 * "Z:/home/u/repo": uppercase drive, forward slashes, like Git for Windows. For each path:
 *   1. the anchor with the longest matching Unix prefix (logical Windows path <-> physical Unix
 *      path pairs, e.g. the realpath of the cwd and its Windows form),
 *   2. else x->to_win,
 *   3. else "Z:" + the Unix path.
 * Records end at '\n' (and also '\r' for FL_OUT_PATHS_LINES so stderr progress is never held
 * back), or at '\0' when nul_records is set; they are buffered across chunk boundaries, so the
 * output does not depend on how the stream is split. Records longer than FL_OUT_MAX_RECORD bytes
 * pass through untranslated. FL_OUT_NONE is pure passthrough with no buffering. If memory runs
 * out, the stream continues untranslated. Nothing in struct fl_out needs allocating up front;
 * release it with fl_out_free().
 */
#define FL_OUT_MAX_RECORD (1024u * 1024u)

struct fl_anchor {
    char *unix_path; /* no trailing '/' ("" is the root) */
    char *win_path;  /* "X:/dir" form, no trailing '/' ("X:" is a drive root) */
};

struct fl_out {
    const struct fl_xlate *x;
    enum fl_outplan plan;
    int nul_records;
    int skipping;    /* inside an over-long record: copy through to its terminator */
    int failed;      /* out of memory: pure passthrough from then on */
    char *buf;       /* the pending partial record */
    size_t len;
    size_t cap;
    struct fl_anchor *anchors;
    size_t nanchors;
    size_t anchors_cap;
};

typedef void (*fl_emit_fn)(void *ud, const char *p, size_t n);

void fl_out_init(struct fl_out *o, const struct fl_xlate *x, enum fl_outplan plan, int nul_records);
/* unix_path must be absolute; win_path "X:\..." / "X:/..." / "\\?\X:\...". Returns 0 or -1. */
int fl_out_add_anchor(struct fl_out *o, const char *unix_path, const char *win_path);
void fl_out_feed(struct fl_out *o, const char *data, size_t n, fl_emit_fn emit, void *ud);
/* Emit the pending partial record (translated) at end of stream. */
void fl_out_flush(struct fl_out *o, fl_emit_fn emit, void *ud);
void fl_out_free(struct fl_out *o);

#endif /* FL_TRANSLATE_H */
