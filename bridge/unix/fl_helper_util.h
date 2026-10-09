/*
 * fl_helper_util.h - pure helpers of fl-bridge-helper, the launcher-started native
 * daemon of the Fork for Linux (unofficial) git bridge, and of its fl-winexec /
 * fl-askpass / fl-ssh-askpass personas.
 *
 * Everything here is free of process-wide side effects (no I/O, no signals, no
 * environment access) so it can be unit-tested natively under ASan/UBSan
 * (bridge/tests/unit/test_helper_*.c). The daemon itself lives in
 * fl_bridge_helper.c; the wire format is the frozen contract in
 * bridge/common/fl_proto.h.
 */
#ifndef FL_HELPER_UTIL_H
#define FL_HELPER_UTIL_H

#include <stddef.h>
#include <stdint.h>

#define FL_TOKEN_HEX_LEN 64u   /* FL_BRIDGE_TOKEN: 64 hex characters ... */
#define FL_KEY_LEN 32u         /* ... decoding to the 256-bit raw HMAC key */
#define FL_NONCE_LEN 32u
#define FL_MAC_LEN 32u
#define FL_HELLO_LEN 38u       /* magic(4) version(u16 BE) client_nonce(32) */
#define FL_CHALLENGE_LEN 64u   /* server_nonce(32) daemon_mac(32) */
#define FL_SIGPIPE_NO 13       /* SIGNAL(13): the Windows-side stdout reader went away */

enum fl_persona {
    FL_PERSONA_HELPER = 0,   /* fl-bridge-helper (or any other name): the daemon CLI */
    FL_PERSONA_WINEXEC,      /* fl-winexec <WinExe> [args...] */
    FL_PERSONA_ASKPASS,      /* fl-askpass <prompt>      (target: FL_ASKPASS_TARGET) */
    FL_PERSONA_SSH_ASKPASS   /* fl-ssh-askpass <prompt>  (target: FL_SSH_ASKPASS_TARGET) */
};

/* Pointer to the basename of path, after the last '/' or '\' (argv[0] may be a DOS path). */
const char *fl_basename_any(const char *path);
/*
 * Make a client-supplied string safe for one space-separated log line: every byte
 * below 0x21 (controls, space) and 0x7f becomes '?', so a crafted argv[0] cannot
 * inject a line break or fake "key=value" fields. In place.
 */
void fl_log_sanitize(char *s);
/* Persona selected by basename(argv[0]); a trailing ".exe" is ignored. */
enum fl_persona fl_persona_from_argv0(const char *argv0);

/*
 * Parse a token: exactly 64 hex digits, optionally followed by "\n" or "\r\n"
 * (a token file written by echo / print). Decodes into the raw 32-byte key.
 * 0 on success, -1 on any other content.
 */
int fl_token_parse(const char *text, size_t len, uint8_t key[FL_KEY_LEN]);

/* Decimal 0..65535 without sign, spaces or trailing garbage; 0 on success. */
int fl_parse_port(const char *s, uint16_t *out);
/* Decimal 1..INT32_MAX without sign, spaces or trailing garbage; 0 on success. */
int fl_parse_pid(const char *s, int32_t *out);

/* Build the HELLO payload (client side; used by tests and future native clients). */
void fl_hello_build(uint8_t out[FL_HELLO_LEN], const uint8_t client_nonce[FL_NONCE_LEN]);
/* Validate a HELLO payload (length, magic "FLB1", version 1); copies the client nonce. */
int fl_hello_parse(const uint8_t *p, size_t n, uint8_t client_nonce[FL_NONCE_LEN]);
/*
 * HMAC-SHA256(key, "fl-bridge-v1|daemon|" || cn || sn) when daemon_side != 0, else
 * HMAC-SHA256(key, "fl-bridge-v1|client|" || cn || sn).
 */
void fl_auth_mac(const uint8_t key[FL_KEY_LEN], int daemon_side, const uint8_t cn[FL_NONCE_LEN],
                 const uint8_t sn[FL_NONCE_LEN], uint8_t out[FL_MAC_LEN]);

/* EXIT frame fields from a waitpid() status: kind 0 = exited (code), 1 = signaled (signo). */
void fl_wait_to_exit(int wstatus, int *kind, int32_t *code);
/* 1 for the signals a SIGNAL frame may deliver to the child's process group. */
int fl_signal_allowed(int signo);

/*
 * SPAWN_OK payload: u32 BE pid, then one NUL-terminated record per anchor, in REQ
 * order ("" when the anchor could not be resolved). malloc'd; 0 on success, -1 when
 * it would exceed max_len or allocation fails.
 */
int fl_spawn_ok_encode(uint32_t pid, char *const *paths, size_t n, size_t max_len, uint8_t **out,
                       size_t *outlen);

/*
 * Child environment: a copy of base ("K=V" strings, NULL-terminated) with the REQ
 * operations applied, unset first, then set ("K=V"; the last one for a key wins).
 * HOME, PATH and TMPDIR always come from base (sets and unsets of them are ignored),
 * LANG is never removed (a set may replace it), FL_BRIDGE_TOKEN is never passed on,
 * and malformed entries (empty key, key without '=', key starting with '=') are
 * ignored. *ignored (may be NULL) counts the ignored operations. The result is a
 * NULL-terminated malloc'd array of malloc'd strings; 0 on success, -1 on ENOMEM.
 */
int fl_env_build(char *const *base, char *const *set, size_t nset, char *const *unset, size_t nunset,
                 char ***out, size_t *ignored);
/* Free an fl_env_build() result (NULL is fine). */
void fl_env_free(char **env);
/* Value of key in a "K=V" array, or NULL. Async-signal-safe (used after fork). */
const char *fl_env_get(char *const *env, const char *key);

/*
 * out = dir[0..dirlen) + "/" + name (no doubled '/'). Async-signal-safe.
 * 0 on success, -1 when it does not fit in outsz.
 */
int fl_path_join(const char *dir, size_t dirlen, const char *name, char *out, size_t outsz);

/*
 * Translate an absolute Unix path to the Windows form handed to a Windows program
 * (backslashes): the longest FL_BRIDGE_ANCHORS entry ("unix=win;unix=win;...")
 * whose unix side is a component prefix of path, where $WINEPREFIX/drive_c counts
 * as one more anchor for "C:" (an explicit anchor wins a tie); else "Z:" + path.
 * Each entry splits at the first '=' that starts a Windows absolute path ("X:",
 * "\\"), else at its first '='. anchors and wineprefix may be NULL. 0 on success,
 * -1 when path is not absolute or the result does not fit.
 */
int fl_unix_to_win(const char *path, const char *anchors, const char *wineprefix, char *out,
                   size_t outsz);

/*
 * WINEDLLOVERRIDES value for a Wine call: cur unchanged when it already mentions
 * winemenubuilder, else cur + ";winemenubuilder.exe=d" (or just that when cur is
 * NULL/empty), so Wine never writes menu entries (AGENTS.md hard rule 12).
 * 0 on success, -1 when it does not fit.
 */
int fl_dlloverrides(const char *cur, char *out, size_t outsz);

#endif /* FL_HELPER_UTIL_H */
