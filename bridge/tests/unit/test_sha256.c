/*
 * test_sha256.c - SHA-256 (FIPS 180-4 / NIST CAVS examples), HMAC-SHA256 (RFC 4231 test
 * cases 1-7), constant-time compare and hex coding.
 */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "fl_sha256.h"
#include "tap.h"

static void hex_of(const uint8_t *d, size_t n, char *out)
{
    fl_hex_encode(d, n, out);
}

static void check_sha(const void *data, size_t n, const char *want, const char *name)
{
    uint8_t dg[32];
    char hex[65];
    fl_sha256(data, n, dg);
    hex_of(dg, sizeof(dg), hex);
    tap_str_eq(hex, want, "sha256 %s", name);
}

static void test_nist(void)
{
    static const struct {
        size_t n;
        const char *want;
    } as[] = {
        /* "a" repeated n times: every padding boundary around 55/56/64 and 119/120/128 */
        { 1, "ca978112ca1bbdcafac231b39a23dc4da786eff8147c4e72b9807785afee48bb" },
        { 55, "9f4390f8d30c2dd92ec9f095b65e2b9ae9b0a925a5258e241c9f1e910f734318" },
        { 56, "b35439a4ac6f0948b6d6f9e3c6af0f5f590ce20f1bde7090ef7970686ec6738a" },
        { 57, "f13b2d724659eb3bf47f2dd6af1accc87b81f09f59f2b75e5c0bed6589dfe8c6" },
        { 63, "7d3e74a05d7db15bce4ad9ec0658ea98e3f06eeecf16b4c6fff2da457ddc2f34" },
        { 64, "ffe054fe7ae0cb6dc65c3af9b61d5209f439851db43d0ba5997337df154668eb" },
        { 65, "635361c48bb9eab14198e76ea8ab7f1a41685d6ad62aa9146d301d4f17eb0ae0" },
        { 119, "31eba51c313a5c08226adf18d4a359cfdfd8d2e816b13f4af952f7ea6584dcfb" },
        { 120, "2f3d335432c70b580af0e8e1b3674a7c020d683aa5f73aaaedfdc55af904c21c" },
        { 127, "c57e9278af78fa3cab38667bef4ce29d783787a2f731d4e12200270f0c32320a" },
        { 128, "6836cf13bac400e9105071cd6af47084dfacad4e5e302c94bfed24e013afb73e" },
        { 1000, "41edece42d63e8d9bf515a9ba6932e1c20cbc9f5a5d134645adb5db1b9737ea3" },
    };
    char *a;
    check_sha("", 0, "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855", "empty");
    check_sha(NULL, 0, "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855", "NULL, 0");
    check_sha("abc", 3, "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad", "abc (FIPS 180-4)");
    check_sha("abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq", 56,
              "248d6a61d20638b8e5c026930c3e6039a33ce45964ff2167f6ecedd419db06c1", "448-bit message");
    check_sha("abcdefghbcdefghicdefghijdefghijkefghijklfghijklmghijklmnhijklmnoijklmnopjklmnopqklmnopqrlmnopqrsmnop"
              "qrstnopqrstu",
              112, "cf5b16a778af8380036ce59e7b0492370b249b11e8f07a51afac45037afee9d1", "896-bit message");
    a = malloc(1000000);
    if (tap_ok(a != NULL, "allocate 1,000,000 bytes")) {
        memset(a, 'a', 1000000);
        check_sha(a, 1000000, "cdc76e5c9914fb9281a1c7e284d73e67f1809a48a497200e046d39ccc7112cd0",
                  "one million 'a'");
        for (size_t i = 0; i < sizeof(as) / sizeof(as[0]); i++) {
            char name[48];
            snprintf(name, sizeof(name), "'a' x %zu", as[i].n);
            check_sha(a, as[i].n, as[i].want, name);
        }
        free(a);
    }
}

static void check_hmac(const uint8_t *key, size_t klen, const void *msg, size_t mlen, const char *want, size_t outlen,
                       const char *name)
{
    uint8_t mac[32];
    char hex[65];
    fl_hmac_sha256(key, klen, msg, mlen, mac);
    hex_of(mac, outlen, hex);
    tap_str_eq(hex, want, "hmac-sha256 %s", name);
}

static void test_rfc4231(void)
{
    uint8_t k[131];
    uint8_t d[50];
    memset(k, 0x0b, 20);
    check_hmac(k, 20, "Hi There", 8, "b0344c61d8db38535ca8afceaf0bf12b881dc200c9833da726e9376c2e32cff7", 32,
               "RFC 4231 case 1");
    check_hmac((const uint8_t *)"Jefe", 4, "what do ya want for nothing?", 28,
               "5bdcc146bf60754e6a042426089575c75a003f089d2739839dec58b964ec3843", 32, "RFC 4231 case 2");
    memset(k, 0xaa, 20);
    memset(d, 0xdd, 50);
    check_hmac(k, 20, d, 50, "773ea91e36800e46854db8ebd09181a72959098b3ef8c122d9635514ced565fe", 32,
               "RFC 4231 case 3");
    for (unsigned i = 0; i < 25u; i++) {
        k[i] = (uint8_t)(i + 1u);
    }
    memset(d, 0xcd, 50);
    check_hmac(k, 25, d, 50, "82558a389a443c0ea4cc819899f2083a85f0faa3e578f8077a2e3ff46729665b", 32,
               "RFC 4231 case 4");
    memset(k, 0x0c, 20);
    check_hmac(k, 20, "Test With Truncation", 20, "a3b6167473100ee06e0c796c2955552b", 16,
               "RFC 4231 case 5 (truncated to 128 bits)");
    memset(k, 0xaa, 131);
    check_hmac(k, 131, "Test Using Larger Than Block-Size Key - Hash Key First", 54,
               "60e431591ee0b67f0d8a26aacbf5b77f8e0bc6213728c5140546040f0ee37f54", 32, "RFC 4231 case 6");
    {
        static const char m7[] = "This is a test using a larger than block-size key and a larger than block-size "
                                 "data. The key needs to be hashed before being used by the HMAC algorithm.";
        check_hmac(k, 131, m7, sizeof(m7) - 1u, "9b09ffa71b942fcb27635fbcd5b0e944bfdc63644f0713938a7f51535c3a35e2",
                   32, "RFC 4231 case 7");
    }
    check_hmac(NULL, 0, NULL, 0, "b613679a0814d9ec772f95d778c35fc5ff1697c493715653c6c712144292c5ad", 32,
               "empty key and message");
    memset(k, 'k', 65);
    check_hmac(k, 64, "msg", 3, "d62ad25bb128e96ab6ef43464aaf2bb91b5b85f013381e9ba6be747e8d0911b2", 32,
               "64-byte key (block size, not hashed)");
    check_hmac(k, 65, "msg", 3, "c65585780e821bd312cfaa440c33cb28d6af6b323d4977ae83934834ce589e38", 32,
               "65-byte key (hashed first)");
}

/* The bridge handshake MACs (fl_proto.h) with a fixed key and nonces: a cross-check vector for
   the daemon and the shim (computed independently with Python's hmac module). */
static void test_handshake_vector(void)
{
    uint8_t key[32];
    uint8_t msg[20 + 64];
    uint8_t mac[32];
    char hex[65];
    tap_ok(fl_hex_decode("00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff", key, 32) == 0,
           "decode the 64-hex test token");
    for (unsigned i = 0; i < 64u; i++) {
        msg[20u + i] = (uint8_t)i; /* client_nonce 00..1f, server_nonce 20..3f */
    }
    memcpy(msg, "fl-bridge-v1|daemon|", 20);
    fl_hmac_sha256(key, 32, msg, sizeof(msg), mac);
    hex_of(mac, 32, hex);
    tap_str_eq(hex, "2b159254eb74ddce465ceee6dd32a56af2844839aab6e858db5a2d287de00a7a", "handshake daemon_mac vector");
    memcpy(msg, "fl-bridge-v1|client|", 20);
    fl_hmac_sha256(key, 32, msg, sizeof(msg), mac);
    hex_of(mac, 32, hex);
    tap_str_eq(hex, "5bbd951c15a7c957c1e94054e0a2c16560f7b48a89ca70df11ec76016bd73a3a", "handshake client_mac vector");
}

static void test_ct_equal(void)
{
    uint8_t a[32];
    uint8_t b[32];
    memset(a, 0x5a, sizeof(a));
    memcpy(b, a, sizeof(b));
    tap_ok(fl_ct_equal(a, b, 32) == 1, "ct_equal: equal buffers");
    tap_ok(fl_ct_equal(a, b, 0) == 1, "ct_equal: zero length is equal");
    b[31] ^= 0x01u;
    tap_ok(fl_ct_equal(a, b, 32) == 0, "ct_equal: last byte differs");
    tap_ok(fl_ct_equal(a, b, 31) == 1, "ct_equal: compares only n bytes");
    b[31] = a[31];
    b[0] ^= 0x80u;
    tap_ok(fl_ct_equal(a, b, 32) == 0, "ct_equal: first byte differs");
}

static void test_hex(void)
{
    uint8_t out[4];
    uint8_t in[4] = { 0x00, 0xab, 0x7f, 0xff };
    char hex[9];
    tap_ok(fl_hex_decode("00ab7fff", out, 4) == 0 && memcmp(out, in, 4) == 0, "hex_decode lowercase");
    tap_ok(fl_hex_decode("00AB7FFF", out, 4) == 0 && memcmp(out, in, 4) == 0, "hex_decode uppercase");
    tap_ok(fl_hex_decode("00aB7fFf", out, 4) == 0 && memcmp(out, in, 4) == 0, "hex_decode mixed case");
    memset(out, 0x11, sizeof(out));
    tap_ok(fl_hex_decode("00ab7f", out, 4) == -1, "hex_decode rejects a short string");
    tap_ok(out[0] == 0 && out[1] == 0 && out[2] == 0 && out[3] == 0, "hex_decode zeroes out on error");
    tap_ok(fl_hex_decode("00ab7fff00", out, 4) == -1, "hex_decode rejects a long string");
    tap_ok(fl_hex_decode("00ab7ffg", out, 4) == -1, "hex_decode rejects a non-hex digit");
    tap_ok(fl_hex_decode("0 ab7fff", out, 4) == -1, "hex_decode rejects a space");
    tap_ok(fl_hex_decode("00ab7ff", out, 4) == -1, "hex_decode rejects an odd length");
    tap_ok(fl_hex_decode(NULL, out, 4) == -1, "hex_decode rejects NULL");
    tap_ok(fl_hex_decode("", out, 0) == 0, "hex_decode: empty string for zero bytes");
    tap_ok(fl_hex_decode("00", out, 0) == -1, "hex_decode: zero bytes wants an empty string");
    fl_hex_encode(in, 4, hex);
    tap_str_eq(hex, "00ab7fff", "hex_encode is lowercase");
    fl_hex_encode(in, 0, hex);
    tap_str_eq(hex, "", "hex_encode of nothing is empty");
}

void test_sha256(void)
{
    test_nist();
    test_rfc4231();
    test_handshake_vector();
    test_ct_equal();
    test_hex();
}
