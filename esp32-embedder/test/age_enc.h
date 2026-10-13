// age *encryption*, for the worker's tests only (Kata never encrypts): makes jobs for the fake
// ingest queue, encrypted to a TEST-ONLY Kata key. X25519 recipients, the spec's construction,
// mbedTLS's primitives. test_age.c checks the decrypter against age's own test kit, so a file this
// writes that worker/age.c opens is a real age file.
#pragma once

#include <string.h>

#include "kw.h"
#include "mbedtls/chachapoly.h"
#include "mbedtls/ecp.h"
#include "mbedtls/hkdf.h"
#include "mbedtls/md.h"

static uint32_t enc_rng_state = 0x2545f491;
static int enc_rng(void *ctx, unsigned char *out, size_t n)
{
    (void)ctx;
    for (size_t i = 0; i < n; i++) {
        enc_rng_state ^= enc_rng_state << 13; enc_rng_state ^= enc_rng_state >> 17; enc_rng_state ^= enc_rng_state << 5;
        out[i] = (uint8_t)enc_rng_state;
    }
    return 0;
}

static void enc_x25519(uint8_t out[32], const uint8_t k[32], const uint8_t u[32])
{
    mbedtls_ecp_group g;
    mbedtls_mpi d;
    mbedtls_ecp_point P, R;
    mbedtls_ecp_group_init(&g); mbedtls_mpi_init(&d); mbedtls_ecp_point_init(&P); mbedtls_ecp_point_init(&R);
    mbedtls_ecp_group_load(&g, MBEDTLS_ECP_DP_CURVE25519);
    mbedtls_mpi_read_binary_le(&d, k, 32);
    mbedtls_mpi_set_bit(&d, 0, 0); mbedtls_mpi_set_bit(&d, 1, 0); mbedtls_mpi_set_bit(&d, 2, 0);
    mbedtls_mpi_set_bit(&d, 255, 0); mbedtls_mpi_set_bit(&d, 254, 1);
    mbedtls_ecp_point_read_binary(&g, &P, u, 32);
    mbedtls_ecp_mul(&g, &R, &d, &P, enc_rng, NULL);
    size_t ol;
    mbedtls_ecp_point_write_binary(&g, &R, MBEDTLS_ECP_PF_UNCOMPRESSED, &ol, out, 32);
    mbedtls_ecp_point_free(&R); mbedtls_ecp_point_free(&P); mbedtls_mpi_free(&d); mbedtls_ecp_group_free(&g);
}

static void enc_b64raw(jbuf_t *b, const uint8_t *in, size_t n)
{
    size_t start = b->n;
    jb_b64(b, in, n);
    while (b->n > start && b->p[b->n - 1] == '=') b->p[--b->n] = 0;
}

static void enc_hkdf(const uint8_t *salt, size_t sl, const uint8_t *ikm, size_t il, const char *info, uint8_t out[32])
{
    mbedtls_hkdf(mbedtls_md_info_from_type(MBEDTLS_MD_SHA256), salt, sl, ikm, il, (const unsigned char *)info, strlen(info), out, 32);
}

// An age file of pt to each of the n recipients' public keys, appended to out.
static void age_encrypt(const uint8_t (*recipients)[32], int n, const uint8_t *pt, size_t len, jbuf_t *out)
{
    static const uint8_t BASE[32] = { 9 }, ZERO[12];
    uint8_t file_key[16], eph[32], share[32], shared[32], salt[64], wrap[32], body[32], hk[32], mac[32], nonce[16], pk[32];
    enc_rng(NULL, file_key, 16);
    size_t start = out->n;
    jb_puts(out, "age-encryption.org/v1\n");
    for (int r = 0; r < n; r++) {
        enc_rng(NULL, eph, 32);
        enc_x25519(share, eph, BASE);
        enc_x25519(shared, eph, recipients[r]);
        memcpy(salt, share, 32);
        memcpy(salt + 32, recipients[r], 32);
        enc_hkdf(salt, 64, shared, 32, "age-encryption.org/v1/X25519", wrap);
        mbedtls_chachapoly_context cp;
        mbedtls_chachapoly_init(&cp);
        mbedtls_chachapoly_setkey(&cp, wrap);
        mbedtls_chachapoly_encrypt_and_tag(&cp, 16, ZERO, NULL, 0, file_key, body, body + 16);
        mbedtls_chachapoly_free(&cp);
        jb_puts(out, "-> X25519 ");
        enc_b64raw(out, share, 32);
        jb_putc(out, '\n');
        enc_b64raw(out, body, 32);
        jb_putc(out, '\n');
    }
    jb_puts(out, "-> grease-test x\nAAAA\n");   // an unknown stanza, as age's grease
    jb_puts(out, "---");
    enc_hkdf(NULL, 0, file_key, 16, "header", hk);
    mbedtls_md_hmac(mbedtls_md_info_from_type(MBEDTLS_MD_SHA256), hk, 32, (const uint8_t *)out->p + start, out->n - start, mac);
    jb_putc(out, ' ');
    enc_b64raw(out, mac, 32);
    jb_putc(out, '\n');
    enc_rng(NULL, nonce, 16);
    jb_put(out, nonce, 16);
    enc_hkdf(nonce, 16, file_key, 16, "payload", pk);
    mbedtls_chachapoly_context cp;
    mbedtls_chachapoly_init(&cp);
    mbedtls_chachapoly_setkey(&cp, pk);
    size_t pos = 0;
    uint64_t ctr = 0;
    do {
        size_t c = len - pos > 65536 ? 65536 : len - pos;
        uint8_t iv[12] = { 0 };
        for (int i = 0; i < 8; i++) iv[10 - i] = (uint8_t)(ctr >> (8 * i));
        iv[11] = pos + c == len;
        jb_reserve(out, c + 16);
        mbedtls_chachapoly_encrypt_and_tag(&cp, c, iv, NULL, 0, pt + pos, (uint8_t *)out->p + out->n, (uint8_t *)out->p + out->n + c);
        out->n += c + 16;
        out->p[out->n] = 0;
        pos += c;
        ctr++;
    } while (pos < len);
    mbedtls_chachapoly_free(&cp);
    (void)pk;
}
