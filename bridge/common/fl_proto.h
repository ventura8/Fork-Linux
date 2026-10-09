/*
 * fl_proto.h - Fork for Linux (unofficial) git bridge: wire protocol shared by
 * fl-shim.exe / fl-launch.exe (Windows side) and fl-bridge-helper --daemon (Linux side).
 *
 * Pure C11 and OS-independent: compiled by MinGW-w64, musl-gcc and the host gcc
 * (native unit tests under ASan/UBSan, bridge/tests/unit/).
 *
 * Frame = 5-byte header (u8 type, u32 big-endian payload length) + payload.
 *
 * Handshake (mutual authentication; the HMAC key is the 32 raw bytes decoded from
 * the 64 hex characters of FL_BRIDGE_TOKEN with fl_hex_decode()):
 *   client -> daemon  HELLO      magic "FLB1"(4) version(u16 BE) client_nonce(32)
 *   daemon -> client  CHALLENGE  server_nonce(32)
 *                                daemon_mac(32) = HMAC(key, "fl-bridge-v1|daemon|" || client_nonce || server_nonce)
 *   client -> daemon  AUTH       client_mac(32) = HMAC(key, "fl-bridge-v1|client|" || client_nonce || server_nonce)
 *   daemon -> client  AUTH_OK    (empty)
 * Then the client sends one REQ and streams STDIN / STDIN_EOF / SIGNAL; the daemon answers
 * SPAWN_OK or SPAWN_ERR, streams STDOUT / STDERR and ends with EXIT.
 *
 * Exit status contract (what the shim returns to Fork): the child's own code; 128+signal when
 * it was killed; 127 spawn failure; 141 when the Windows-side reader closed; 125 bridge failure.
 */
#ifndef FL_PROTO_H
#define FL_PROTO_H

#include <stddef.h>
#include <stdint.h>

#define FL_PROTO_MAGIC "FLB1"
#define FL_PROTO_VERSION 1
#define FL_MAX_CHUNK (64u * 1024u)  /* max STDIN / STDOUT / STDERR payload */
#define FL_MAX_REQ (1024u * 1024u)  /* max REQ (and SPAWN_OK) payload */

enum fl_frame_type {
    FL_F_HELLO = 1,      /* client->daemon: magic(4) ver(u16 BE) client_nonce(32); exactly 38 bytes */
    FL_F_CHALLENGE = 2,  /* daemon->client: server_nonce(32) daemon_mac(32); exactly 64 bytes */
    FL_F_AUTH = 3,       /* client->daemon: client_mac(32); exactly 32 bytes */
    FL_F_AUTH_OK = 4,    /* daemon->client: empty */
    FL_F_REQ = 5,        /* client->daemon: TLV records (fl_req_encode); at most FL_MAX_REQ */
    FL_F_STDIN = 6,      /* client->daemon: raw bytes; at most FL_MAX_CHUNK */
    FL_F_STDIN_EOF = 7,  /* client->daemon: empty */
    FL_F_SIGNAL = 8,     /* client->daemon: u8 signo; exactly 1 byte */
    FL_F_SPAWN_OK = 9,   /* daemon->client: u32 BE pid, then optional NUL-separated realpaths of anchors */
    FL_F_SPAWN_ERR = 10, /* daemon->client: i32 BE errno + message; 4..FL_MAX_CHUNK bytes */
    FL_F_STDOUT = 11,    /* daemon->client: raw bytes; at most FL_MAX_CHUNK */
    FL_F_STDERR = 12,    /* daemon->client: raw bytes; at most FL_MAX_CHUNK */
    FL_F_EXIT = 13       /* daemon->client: u8 kind (0 exited, 1 signaled) + i32 BE code; exactly 5 bytes */
};

/*
 * Frame header. Both functions check the type (1..13) and the payload length against the
 * type's limits: fixed-size frames (HELLO 38, CHALLENGE 64, AUTH 32, AUTH_OK 0, STDIN_EOF 0,
 * SIGNAL 1, EXIT 5) need that exact length; SPAWN_OK needs 4..FL_MAX_REQ, SPAWN_ERR
 * 4..FL_MAX_CHUNK, REQ 0..FL_MAX_REQ, STDIN/STDOUT/STDERR 0..FL_MAX_CHUNK.
 * Return 0, or -1 when the type or length is invalid. fl_hdr_encode() writes nothing on
 * error; fl_hdr_decode() always stores the raw type and length (for diagnostics).
 */
int fl_hdr_encode(uint8_t out[5], uint8_t type, uint32_t len);
int fl_hdr_decode(const uint8_t in[5], uint8_t *type, uint32_t *len);

/*
 * REQ payload: a sequence of TLV records, each u8 tag, u32 BE length, bytes (no NUL inside).
 *   FL_T_CWD        exactly one; the Unix working directory, absolute ('/...')
 *   FL_T_ARG        at least one, in order; argv[0] must not be empty (other args may be)
 *   FL_T_ENV_SET    "K=V" with a non-empty K
 *   FL_T_ENV_UNSET  "K" (non-empty, no '=')
 *   FL_T_FLAGS      at most one; u32 BE of FL_REQ_* bits (unknown bits are rejected); default 0
 *   FL_T_ANCHOR     a Unix path the daemon realpath()s back in SPAWN_OK (any string)
 * Unknown tags are rejected (the protocol version changes instead).
 */
enum fl_req_tag {
    FL_T_CWD = 1,
    FL_T_ARG = 2,
    FL_T_ENV_SET = 3,
    FL_T_ENV_UNSET = 4,
    FL_T_FLAGS = 5,
    FL_T_ANCHOR = 6
};

#define FL_REQ_DETACH 0x1u      /* report EXIT 0 right after a successful exec; child keeps running detached (setsid) */
#define FL_REQ_HOST_HELPER 0x2u /* argv[0] is a fork-linux-host verb: daemon prepends its configured host-helper path */

struct fl_req {
    char *cwd;
    size_t argc;
    char **argv;
    size_t nset;
    char **set;
    size_t nunset;
    char **unset;
    uint32_t flags;
    size_t nanchors;
    char **anchors;
};

/*
 * Encode: validates exactly what fl_req_decode() checks, so every encoded request decodes.
 * Records are written in the order CWD, FLAGS, ARG..., ENV_SET..., ENV_UNSET..., ANCHOR....
 * *out is malloc'd (free() it); returns 0, or -1 (invalid request, payload over FL_MAX_REQ,
 * out of memory) with *out = NULL and *outlen = 0.
 */
int fl_req_encode(const struct fl_req *r, uint8_t **out, size_t *outlen);

/*
 * Decode and validate a REQ payload (at most FL_MAX_REQ bytes). Every string is a malloc'd
 * NUL-terminated copy; argv, set, unset and anchors are additionally NULL-terminated arrays
 * (argv[argc] == NULL, ready for execve). Returns 0, or -1 with *r zeroed. Release with
 * fl_req_free() (also safe on a zeroed struct).
 */
int fl_req_decode(const uint8_t *buf, size_t len, struct fl_req *r);
void fl_req_free(struct fl_req *r);

/*
 * EXIT payload: u8 kind (0 exited, 1 signaled), i32 BE code. kind 0 needs code 0..255
 * (the child's status, or the bridge's 125/127/141); kind 1 needs a signal number 1..127.
 * fl_exit_encode() returns 0 or -1 (nothing written); fl_exit_decode() needs n == 5.
 */
int fl_exit_encode(uint8_t out[5], int kind, int32_t code);
int fl_exit_decode(const uint8_t *p, size_t n, int *kind, int32_t *code);

#endif /* FL_PROTO_H */
