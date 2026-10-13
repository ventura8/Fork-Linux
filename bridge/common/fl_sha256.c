/*
 * fl_sha256.c - SHA-256, HMAC-SHA256, constant-time compare, hex coding. See fl_sha256.h.
 */
#include "fl_sha256.h"

#include <string.h>

struct sha256_ctx {
    uint32_t h[8];
    uint64_t total; /* bytes hashed so far */
    uint8_t buf[64];
    size_t used;
};

static const uint32_t k_round[64] = {
    0x428a2f98u, 0x71374491u, 0xb5c0fbcfu, 0xe9b5dba5u, 0x3956c25bu, 0x59f111f1u, 0x923f82a4u, 0xab1c5ed5u,
    0xd807aa98u, 0x12835b01u, 0x243185beu, 0x550c7dc3u, 0x72be5d74u, 0x80deb1feu, 0x9bdc06a7u, 0xc19bf174u,
    0xe49b69c1u, 0xefbe4786u, 0x0fc19dc6u, 0x240ca1ccu, 0x2de92c6fu, 0x4a7484aau, 0x5cb0a9dcu, 0x76f988dau,
    0x983e5152u, 0xa831c66du, 0xb00327c8u, 0xbf597fc7u, 0xc6e00bf3u, 0xd5a79147u, 0x06ca6351u, 0x14292967u,
    0x27b70a85u, 0x2e1b2138u, 0x4d2c6dfcu, 0x53380d13u, 0x650a7354u, 0x766a0abbu, 0x81c2c92eu, 0x92722c85u,
    0xa2bfe8a1u, 0xa81a664bu, 0xc24b8b70u, 0xc76c51a3u, 0xd192e819u, 0xd6990624u, 0xf40e3585u, 0x106aa070u,
    0x19a4c116u, 0x1e376c08u, 0x2748774cu, 0x34b0bcb5u, 0x391c0cb3u, 0x4ed8aa4au, 0x5b9cca4fu, 0x682e6ff3u,
    0x748f82eeu, 0x78a5636fu, 0x84c87814u, 0x8cc70208u, 0x90befffau, 0xa4506cebu, 0xbef9a3f7u, 0xc67178f2u,
};

static uint32_t rotr(uint32_t x, unsigned n)
{
    return (x >> n) | (x << (32u - n));
}

static void compress(uint32_t h[8], const uint8_t blk[64])
{
    uint32_t w[64];
    uint32_t a = h[0];
    uint32_t b = h[1];
    uint32_t c = h[2];
    uint32_t d = h[3];
    uint32_t e = h[4];
    uint32_t f = h[5];
    uint32_t g = h[6];
    uint32_t hh = h[7];
    for (unsigned i = 0; i < 16u; i++) {
        w[i] = ((uint32_t)blk[4u * i] << 24) | ((uint32_t)blk[4u * i + 1u] << 16) |
               ((uint32_t)blk[4u * i + 2u] << 8) | (uint32_t)blk[4u * i + 3u];
    }
    for (unsigned i = 16; i < 64u; i++) {
        uint32_t s0 = rotr(w[i - 15u], 7) ^ rotr(w[i - 15u], 18) ^ (w[i - 15u] >> 3);
        uint32_t s1 = rotr(w[i - 2u], 17) ^ rotr(w[i - 2u], 19) ^ (w[i - 2u] >> 10);
        w[i] = w[i - 16u] + s0 + w[i - 7u] + s1;
    }
    for (unsigned i = 0; i < 64u; i++) {
        uint32_t s1 = rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25);
        uint32_t ch = (e & f) ^ (~e & g);
        uint32_t t1 = hh + s1 + ch + k_round[i] + w[i];
        uint32_t s0 = rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22);
        uint32_t maj = (a & b) ^ (a & c) ^ (b & c);
        uint32_t t2 = s0 + maj;
        hh = g;
        g = f;
        f = e;
        e = d + t1;
        d = c;
        c = b;
        b = a;
        a = t1 + t2;
    }
    h[0] += a;
    h[1] += b;
    h[2] += c;
    h[3] += d;
    h[4] += e;
    h[5] += f;
    h[6] += g;
    h[7] += hh;
}

static void sha_init(struct sha256_ctx *c)
{
    static const uint32_t iv[8] = {
        0x6a09e667u, 0xbb67ae85u, 0x3c6ef372u, 0xa54ff53au, 0x510e527fu, 0x9b05688cu, 0x1f83d9abu, 0x5be0cd19u,
    };
    memcpy(c->h, iv, sizeof(iv));
    c->total = 0;
    c->used = 0;
}

static void sha_update(struct sha256_ctx *c, const void *data, size_t n)
{
    const uint8_t *p = data;
    c->total += (uint64_t)n;
    while (n > 0) {
        size_t take = 64u - c->used;
        if (take > n) {
            take = n;
        }
        memcpy(c->buf + c->used, p, take);
        c->used += take;
        p += take;
        n -= take;
        if (c->used == 64u) {
            compress(c->h, c->buf);
            c->used = 0;
        }
    }
}

static void sha_final(struct sha256_ctx *c, uint8_t out[32])
{
    uint64_t bits = c->total * 8u;
    c->buf[c->used++] = 0x80u;
    if (c->used > 56u) {
        memset(c->buf + c->used, 0, 64u - c->used);
        compress(c->h, c->buf);
        c->used = 0;
    }
    memset(c->buf + c->used, 0, 56u - c->used);
    for (unsigned i = 0; i < 8u; i++) {
        c->buf[56u + i] = (uint8_t)(bits >> (56u - 8u * i));
    }
    compress(c->h, c->buf);
    for (unsigned i = 0; i < 8u; i++) {
        out[4u * i] = (uint8_t)(c->h[i] >> 24);
        out[4u * i + 1u] = (uint8_t)(c->h[i] >> 16);
        out[4u * i + 2u] = (uint8_t)(c->h[i] >> 8);
        out[4u * i + 3u] = (uint8_t)c->h[i];
    }
}

/* Zeroes key material in a way the optimiser cannot drop. */
static void wipe(void *p, size_t n)
{
    volatile uint8_t *v = p;
    while (n-- > 0) {
        *v++ = 0;
    }
}

void fl_sha256(const void *d, size_t n, uint8_t out[32])
{
    struct sha256_ctx c;
    sha_init(&c);
    if (n > 0) {
        sha_update(&c, d, n);
    }
    sha_final(&c, out);
    wipe(&c, sizeof(c));
}

void fl_hmac_sha256(const uint8_t *key, size_t klen, const void *msg, size_t mlen, uint8_t out[32])
{
    struct sha256_ctx c;
    uint8_t k0[64];
    uint8_t pad[64];
    uint8_t inner[32];
    memset(k0, 0, sizeof(k0));
    if (klen > sizeof(k0)) {
        fl_sha256(key, klen, k0);
    } else if (klen > 0) {
        memcpy(k0, key, klen);
    }
    for (unsigned i = 0; i < 64u; i++) {
        pad[i] = (uint8_t)(k0[i] ^ 0x36u);
    }
    sha_init(&c);
    sha_update(&c, pad, sizeof(pad));
    if (mlen > 0) {
        sha_update(&c, msg, mlen);
    }
    sha_final(&c, inner);
    for (unsigned i = 0; i < 64u; i++) {
        pad[i] = (uint8_t)(k0[i] ^ 0x5cu);
    }
    sha_init(&c);
    sha_update(&c, pad, sizeof(pad));
    sha_update(&c, inner, sizeof(inner));
    sha_final(&c, out);
    wipe(&c, sizeof(c));
    wipe(k0, sizeof(k0));
    wipe(pad, sizeof(pad));
    wipe(inner, sizeof(inner));
}

int fl_ct_equal(const void *a, const void *b, size_t n)
{
    const uint8_t *x = a;
    const uint8_t *y = b;
    uint32_t diff = 0;
    for (size_t i = 0; i < n; i++) {
        diff |= (uint32_t)(x[i] ^ y[i]);
    }
    /* Branch-free: (diff - 1) >> 8 has bit 0 set only when diff == 0 (diff <= 0xff). */
    return (int)(((diff - 1u) >> 8) & 1u);
}

static int hex_val(char c)
{
    if (c >= '0' && c <= '9') {
        return c - '0';
    }
    if (c >= 'a' && c <= 'f') {
        return c - 'a' + 10;
    }
    if (c >= 'A' && c <= 'F') {
        return c - 'A' + 10;
    }
    return -1;
}

int fl_hex_decode(const char *hex, uint8_t *out, size_t outlen)
{
    if (hex == NULL || (out == NULL && outlen > 0) || outlen > ((size_t)-1 - 1u) / 2u) {
        return -1;
    }
    for (size_t i = 0; i < outlen; i++) {
        int hi;
        int lo;
        /* hex[2i] is only read when every earlier character was a hex digit, so a short
           string stops at its NUL (hex_val('\0') == -1) before reading past it. */
        hi = hex_val(hex[2u * i]);
        lo = hi < 0 ? -1 : hex_val(hex[2u * i + 1u]);
        if (hi < 0 || lo < 0) {
            wipe(out, outlen);
            return -1;
        }
        out[i] = (uint8_t)((hi << 4) | lo);
    }
    if (hex[2u * outlen] != '\0') {
        wipe(out, outlen);
        return -1;
    }
    return 0;
}

void fl_hex_encode(const uint8_t *in, size_t n, char *out)
{
    static const char digits[] = "0123456789abcdef";
    if (out == NULL) {
        return;
    }
    for (size_t i = 0; i < n; i++) {
        out[2u * i] = digits[in[i] >> 4];
        out[2u * i + 1u] = digits[in[i] & 0x0fu];
    }
    out[2u * n] = '\0';
}
