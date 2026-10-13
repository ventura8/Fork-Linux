/*
 * fl_sha256.h - SHA-256 (FIPS 180-4), HMAC-SHA256 (RFC 2104), constant-time compare and hex
 * coding for the bridge handshake. Pure C11, no OS dependencies.
 */
#ifndef FL_SHA256_H
#define FL_SHA256_H

#include <stddef.h>
#include <stdint.h>

/* out = SHA-256(d[0..n)). d may be NULL when n == 0. */
void fl_sha256(const void *d, size_t n, uint8_t out[32]);

/* out = HMAC-SHA256(key, msg). Keys longer than 64 bytes are hashed first (RFC 2104). */
void fl_hmac_sha256(const uint8_t *key, size_t klen, const void *msg, size_t mlen, uint8_t out[32]);

/* 1 when a[0..n) == b[0..n), else 0; the running time depends only on n. */
int fl_ct_equal(const void *a, const void *b, size_t n);

/*
 * Decode exactly 2*outlen hex digits (either case) into out[0..outlen). The string must end
 * right there. Returns 0, or -1 (out is zeroed) on a wrong length or a non-hex character.
 */
int fl_hex_decode(const char *hex, uint8_t *out, size_t outlen);

/* Lowercase hex of in[0..n) into out, which must hold 2n+1 bytes (NUL-terminated). */
void fl_hex_encode(const uint8_t *in, size_t n, char *out);

#endif /* FL_SHA256_H */
