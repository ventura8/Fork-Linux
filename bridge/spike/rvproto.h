/*
 * rvproto.h - B2 spike: frame format shared by rv.exe (Windows) and rv-helper (Linux).
 *
 * Every frame is a 5-byte header followed by `len` payload bytes:
 *   byte 0     type (RV_*)
 *   bytes 1-4  payload length, unsigned 32-bit big-endian
 *
 * Direction  Type        Payload
 * helper->rv HELLO       64 hex chars of the token from FL_BRIDGE_TOKEN
 * rv->helper REQ         cwd '\0' argv[0] '\0' argv[1] '\0' ...  (UTF-8, unix paths)
 * helper->rv SPAWN_OK    empty
 * helper->rv SPAWN_ERR   4-byte big-endian errno of the failed execvp/chdir
 * rv->helper STDIN       raw bytes for the child's stdin
 * rv->helper STDIN_EOF   empty; the helper closes the child's stdin
 * helper->rv STDOUT      raw bytes from the child's stdout
 * helper->rv STDERR      raw bytes from the child's stderr
 * helper->rv EXIT        4-byte big-endian exit code (128 + signal when killed)
 *
 * Pure C11, no OS headers: compiled by both MinGW-w64 and musl-gcc.
 */
#ifndef RVPROTO_H
#define RVPROTO_H

#include <stdint.h>

enum {
    RV_HELLO = 1,
    RV_REQ = 2,
    RV_STDIN = 3,
    RV_STDIN_EOF = 4,
    RV_SPAWN_OK = 5,
    RV_SPAWN_ERR = 6,
    RV_STDOUT = 7,
    RV_STDERR = 8,
    RV_EXIT = 9
};

#define RV_HDR 5u
#define RV_TOKEN_HEX 64u
#define RV_CHUNK 65536u              /* max STDIN/STDOUT/STDERR payload */
#define RV_MAX_REQ (1024u * 1024u)   /* max REQ payload */

/* Write a frame header for `type` with a `len`-byte payload into h[0..4]. */
static inline void rv_put_hdr(unsigned char *h, unsigned type, uint32_t len)
{
    h[0] = (unsigned char)type;
    h[1] = (unsigned char)(len >> 24);
    h[2] = (unsigned char)(len >> 16);
    h[3] = (unsigned char)(len >> 8);
    h[4] = (unsigned char)len;
}

/* Read the payload length from a frame header. */
static inline uint32_t rv_get_len(const unsigned char *h)
{
    return ((uint32_t)h[1] << 24) | ((uint32_t)h[2] << 16) | ((uint32_t)h[3] << 8) | (uint32_t)h[4];
}

/* Encode a 32-bit signed value big-endian into p[0..3]. */
static inline void rv_put_i32(unsigned char *p, int32_t v)
{
    uint32_t u = (uint32_t)v;
    p[0] = (unsigned char)(u >> 24);
    p[1] = (unsigned char)(u >> 16);
    p[2] = (unsigned char)(u >> 8);
    p[3] = (unsigned char)u;
}

/* Decode a 32-bit signed big-endian value from p[0..3]. */
static inline int32_t rv_get_i32(const unsigned char *p)
{
    uint32_t u = ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16) | ((uint32_t)p[2] << 8) | (uint32_t)p[3];
    return (int32_t)u;
}

/* Constant-time comparison of two n-byte buffers; returns 1 when equal. */
static inline int rv_ct_equal(const unsigned char *a, const unsigned char *b, unsigned n)
{
    unsigned char diff = 0;
    for (unsigned i = 0; i < n; i++) {
        diff |= (unsigned char)(a[i] ^ b[i]);
    }
    return diff == 0;
}

#endif /* RVPROTO_H */
