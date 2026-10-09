/*
 * fl_win.h - shared Win32 helpers for the Windows side of the native-git bridge
 * of Fork for Linux (unofficial): fl-shim.exe (git.exe / bash.exe / sh.exe
 * personas) and fl-launch.exe.
 *
 * Text is UTF-8 everywhere inside the bridge; UTF-16 appears only at the Win32
 * boundary (wmain argv, the environment block, file and path APIs).
 *
 * Transport (frozen contract, bridge/common/fl_proto.h): one loopback TCP
 * connection per call to the launcher-started daemon on 127.0.0.1:FL_BRIDGE_PORT,
 * mutual HMAC-SHA256 challenge/response keyed with FL_BRIDGE_TOKEN, then REQ,
 * SPAWN_OK | SPAWN_ERR, and the STDIN / STDOUT / STDERR / SIGNAL / EXIT relay.
 *
 * Fixes from spike B2 (docs/spikes/B2-rendezvous.md) that this code keeps:
 *   1. the stdin pump thread never exits on its own: flw_exit() sets the quit
 *      event, cancels its blocking I/O, joins it (500 ms cap; 20 ms while it is
 *      blocked in a read Wine cannot cancel) and only then calls ExitProcess; a pump
 *      that was not joined in time never returns, so it cannot race ExitProcess and
 *      Wine cannot turn the exit code into 0;
 *   4. the socket is never closed while the pump thread may still be inside
 *      send(): it is left to process teardown, after the join.
 */
#ifndef FL_WIN_H
#define FL_WIN_H

#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <winsock2.h>
#include <windows.h>

#include <stddef.h>
#include <stdint.h>
#include <wchar.h>

#include "fl_proto.h"
#include "fl_translate.h"

/* Exit-status contract (fl_proto.h). */
#define FLW_EXIT_USAGE 2u
#define FLW_EXIT_BRIDGE 125u
#define FLW_EXIT_SPAWN 127u
#define FLW_EXIT_SIGPIPE 141u
#define FLW_SIGPIPE 13u

#define FLW_KEY_LEN 32u          /* raw HMAC key: FL_BRIDGE_TOKEN is its 64-char hex form */
#define FLW_NONCE_LEN 32u
#define FLW_MAC_LEN 32u
#define FLW_HANDSHAKE_MS 10000u  /* receive timeout until SPAWN_OK / SPAWN_ERR */
#define FLW_JOIN_MS 500u         /* cap on joining the stdin pump thread (B2 fix 1) */
#define FLW_READ_JOIN_MS 20u     /* ... when it is blocked in a ReadFile Wine cannot cancel */

/* ---- diagnostics -------------------------------------------------------- */

/* Set the program name used as the prefix of every message ("fl-shim", ...). */
void flw_set_prog(const char *name);
/* Print "<prog>: <message>\n" on our stderr handle (works in GUI-subsystem builds). */
void flw_msg(const char *fmt, ...);

/* ---- text --------------------------------------------------------------- */

/* malloc'd UTF-8 copy of a NUL-terminated UTF-16 string; NULL on error, including a lone
 * surrogate (never silently replaced by U+FFFD: it would name a different file). */
char *flw_utf8(const wchar_t *w);
/* malloc'd UTF-8 copy of n UTF-16 units (no terminator needed); NULL on error. */
char *flw_utf8n(const wchar_t *w, size_t n);
/* malloc'd UTF-16 copy of a NUL-terminated UTF-8 string; NULL on error. */
wchar_t *flw_utf16(const char *s);
/* malloc'd copy of s; NULL on error. */
char *flw_strdup(const char *s);
/* malloc'd UTF-8 value of an environment variable; NULL when unset. */
char *flw_getenv(const wchar_t *name);
/* Case-insensitive ASCII search: 1 when name contains TOKEN, PASS, SECRET, AUTH, COOKIE or
 * EXTRAHEADER (http.extraHeader carries bearer tokens). */
int flw_is_secret_name(const char *name);

/* Growable byte buffer; on allocation failure `oom` is set and appends become no-ops. */
struct flw_buf {
    char *p;
    size_t n;
    size_t cap;
    int oom;
};
void flw_buf_put(struct flw_buf *b, const char *s, size_t n);
void flw_buf_puts(struct flw_buf *b, const char *s);
void flw_buf_putu(struct flw_buf *b, unsigned long long v);
void flw_buf_puti(struct flw_buf *b, long long v);
/* Append s as a JSON string literal (quotes included); invalid UTF-8 becomes U+FFFD. */
void flw_buf_json(struct flw_buf *b, const char *s);
void flw_buf_free(struct flw_buf *b);

/* ---- randomness --------------------------------------------------------- */

/* Fill buf with n bytes from BCryptGenRandom; 0 on success. */
int flw_random(void *buf, size_t n);

/* ---- Wine path conversion ----------------------------------------------- */

/* 1 when kernel32 exports wine_get_unix_file_name / wine_get_dos_file_name. */
int flw_have_wine(void);
/*
 * fl_path_fn: Windows path (UTF-8, '\' or '/') -> Unix path. The longest existing
 * ancestor is mapped with wine_get_unix_file_name and the missing tail is appended
 * as given (separators turned into '/'). 0 on success, -1 when it cannot be mapped.
 */
int flw_to_unix_cb(void *ud, const char *in, char *out, size_t outsz);
/*
 * fl_path_fn: Unix path -> Windows path in Git for Windows form ("C:/a/b": upper-case
 * drive letter, forward slashes) via wine_get_dos_file_name. -1 when the path is
 * outside every drive (\\?\unix\...) or Wine is unavailable.
 */
int flw_to_win_cb(void *ud, const char *in, char *out, size_t outsz);
/* malloc'd Unix path for a Windows path (flw_to_unix_cb); NULL when unmappable. */
char *flw_path_to_unix(const char *win);
/*
 * Normalise a Windows path for display and anchors: GetFullPathNameW (resolves
 * "." / ".." and relative paths against `base`, or the process cwd when base is
 * NULL), strips a \\?\ prefix in front of a drive, upper-cases the drive letter,
 * turns '\' into '/' and drops a trailing '/' (except "C:/"). malloc'd; NULL when
 * the result is not a drive path.
 */
char *flw_norm_win(const char *path, const char *base);
/* malloc'd UTF-8 current directory (Win32 form, as returned by GetCurrentDirectoryW). */
char *flw_cwd(void);
/* malloc'd UTF-8 path of our own module (GetModuleFileNameW). */
char *flw_module_path(void);
/* Pointer to the basename of a path, after the last '\' or '/'. */
const char *flw_basename(const char *path);

/* ---- configuration from the environment --------------------------------- */

struct flw_cfg {
    uint16_t port;              /* FL_BRIDGE_PORT */
    uint8_t key[FLW_KEY_LEN];   /* FL_BRIDGE_TOKEN, hex-decoded */
    char *winexec;              /* FL_BRIDGE_WINEXEC (unix path of the fl-winexec persona) or NULL */
    char *askpass;              /* FL_BRIDGE_ASKPASS (optional) */
    char *ssh_askpass;          /* FL_BRIDGE_SSH_ASKPASS (optional) */
};
/* Read FL_BRIDGE_PORT / _TOKEN / _WINEXEC / _ASKPASS / _SSH_ASKPASS; prints why and
 * returns -1 when a required one is missing or malformed. FL_BRIDGE_WINEXEC is
 * required when need_winexec is set (fl-shim: Windows editors and credential
 * helpers must be wrapped), otherwise optional (NULL: not wrapped). */
int flw_load_cfg(struct flw_cfg *c, int need_winexec);
void flw_cfg_clear(struct flw_cfg *c);
/* Translation context backed by the Wine path callbacks and cfg. */
void flw_xlate_init(struct fl_xlate *x, const struct flw_cfg *c);

/*
 * The process environment as a NULL-terminated array of malloc'd UTF-8 "K=V"
 * strings, without the per-drive "=C:=..." entries, without FL_BRIDGE_*
 * (bridge configuration, including the token, never reaches the Unix side) and
 * without variables that are not valid UTF-16 (lone surrogates).
 * NULL on error; free with flw_free_strv.
 */
char **flw_environ(void);
void flw_free_strv(char **v);

/* ---- JSON-line call log (FL_BRIDGE_LOG) --------------------------------- */

/* Append "<ts>" (ISO-8601 UTC, milliseconds) for the current time. */
void flw_buf_timestamp(struct flw_buf *b);
/* Append a JSON array of args with credentials redacted (URL userinfo, and the
 * value of "-c key=value" when key looks secret). */
void flw_buf_json_args(struct flw_buf *b, size_t argc, char *const *argv);
/* Append an env value with credentials redacted (secret-looking name or value). */
void flw_buf_json_env_value(struct flw_buf *b, const char *name, const char *value);
/* Append {"stdin":"pipe",...} from GetFileType of the three std handles. */
void flw_buf_json_stdio(struct flw_buf *b);
/* Append one line (b->p, b->n, a '\n' is added) to the log at path (Windows or Unix
 * path). Best effort: failures are ignored. */
void flw_log_append(const char *path, struct flw_buf *b);

/* ---- transport ---------------------------------------------------------- */

/* Connect to 127.0.0.1:port (TCP_NODELAY, not inheritable); 0 on success. */
int flw_connect(uint16_t port);
/* Mutual HMAC-SHA256 authentication (HELLO, CHALLENGE, AUTH, AUTH_OK); 0 on success. */
int flw_handshake(const uint8_t key[FLW_KEY_LEN]);
/* Send one frame; serialised against the stdin pump. 0 on success. */
int flw_send_frame(uint8_t type, const void *payload, uint32_t len);
/* Receive one frame. *payload is valid until the next call. 0 on success. */
int flw_recv_frame(uint8_t *type, const uint8_t **payload, uint32_t *len);
/* Encode and send REQ; 0 on success, -1 (message printed) when too large or lost. */
int flw_send_req(const struct fl_req *r);

/*
 * Wait for SPAWN_OK. On SPAWN_ERR prints the daemon's message and exits 127; on
 * any other failure exits 125. Returns the child's pid and, in *real (malloc'd
 * array of nreal malloc'd strings, "" when unresolved), the realpaths the daemon
 * reported for the REQ anchors, in order.
 */
uint32_t flw_wait_spawn(const char *what, char ***real, size_t *nreal);

/* Where STDOUT / STDERR payloads go; `out` may be NULL for raw passthrough. */
struct flw_sink {
    HANDLE h;
    int discard;  /* the std handle was absent: drop the data */
    int broken;   /* a write failed */
    struct fl_out *out;
};
void flw_sink_init(struct flw_sink *s, DWORD std_handle, struct fl_out *out);

/*
 * Run the relay until EXIT and exit with the child's status (128 + signal when
 * it was killed). pump_stdin = 0 sends STDIN_EOF at once instead of reading our
 * stdin. A broken stdout sends SIGNAL(SIGPIPE) and exits 141. Never returns.
 */
_Noreturn void flw_relay(struct flw_sink *out, struct flw_sink *err, int pump_stdin);

/* ---- exit --------------------------------------------------------------- */

typedef void (*flw_exit_hook)(void *ud, UINT code);
/* Register a hook that flw_exit runs once, before stopping the pump (logging). */
void flw_set_exit_hook(flw_exit_hook fn, void *ud);
/* Run the exit hook, stop and join the stdin pump (B2 fix 1), ExitProcess(code). */
_Noreturn void flw_exit(UINT code);
/* Print "<prog>: <message>\n" and flw_exit(125). */
_Noreturn void flw_fail(const char *fmt, ...);

#endif /* FL_WIN_H */
