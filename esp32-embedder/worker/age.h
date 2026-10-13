// age decryption (CHATS-5): Kata opens the conversation-memory jobs the Macs' forwarders encrypt,
// each to Cal's key and to every Kata board's (one X25519 recipient stanza each, plus grease).
//
// The age v1 format (age-encryption.org/v1, C2SP), X25519 recipients only, no armour: the header's
// stanzas are tried against the board's identity (X25519, then HKDF-SHA-256, then
// ChaCha20-Poly1305 unwraps the 16-byte file key), the header's HMAC is checked, then the payload
// is decrypted in STREAM chunks of 64 KiB (ChaCha20-Poly1305 again). Strict, as age is: canonical
// base64, exact line lengths, and a payload that ends exactly where its last chunk says.
// The crypto is mbedTLS's (CONFIG_MBEDTLS_CHACHA20_C, _POLY1305_C, _CHACHAPOLY_C and _HKDF_C).
//
// Tested against age's own test kit (C2SP CCTV, ../test/age-testkit) and against track C's golden
// fixtures (a real two-recipient file with TEST-ONLY keys, ../host/convmem-wire).
#pragma once

#include <stddef.h>
#include <stdint.h>

typedef struct {
    uint8_t secret[32];   // the X25519 scalar, as encoded (clamped when used)
    uint8_t public[32];   // X25519(secret, 9): part of the HKDF salt
} age_identity_t;

typedef enum {
    AGE_OK = 0,
    AGE_ERR_HEADER = -1,    // a malformed header (or a stanza for us that's malformed)
    AGE_ERR_NO_MATCH = -2,  // well formed, but not encrypted to this identity
    AGE_ERR_HMAC = -3,      // the header's MAC is wrong
    AGE_ERR_PAYLOAD = -4,   // the payload doesn't decrypt (truncated, tampered, trailing data)
    AGE_ERR_NOMEM = -5,
    AGE_ERR_CRYPTO = -6,    // mbedTLS failed for some other reason
} age_err_t;

// The blinding RNG mbedTLS's X25519 needs (it refuses to run without one): esp_fill_random on the
// board. Set once, before the first decryption. Returns 0 on success, as mbedTLS's f_rng does.
void age_set_rng(int (*rng)(void *ctx, unsigned char *out, size_t n), void *ctx);

// "AGE-SECRET-KEY-1..." (Bech32, as age-keygen writes it) -> identity. 0, or -1 if malformed.
int age_identity_parse(const char *s, age_identity_t *id);
// "age1..." -> the recipient's public key (tests compare it with the identity's). 0 or -1.
int age_recipient_parse(const char *s, uint8_t public_key[32]);

// Decrypts an age file. *out (kw_alloc'd, so PSRAM on the board) holds the plaintext, *out_len
// bytes, NUL-terminated for convenience; the caller frees it with kw_free. file_key, if not NULL,
// receives the 16-byte file key (the test kit checks it). Nothing is allocated on failure.
age_err_t age_decrypt(const age_identity_t *id, const uint8_t *in, size_t len, uint8_t **out, size_t *out_len,
                      uint8_t file_key[16]);

const char *age_strerror(age_err_t e);
