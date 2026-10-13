// age decryption, X25519 recipients only (age.h). Follows the spec (age-encryption.org/v1) and
// the reference implementation's strictness, which age's test kit checks.
#include "age.h"
#include "kw.h"

#include <stdbool.h>
#include <string.h>

#include "mbedtls/chachapoly.h"
#include "mbedtls/constant_time.h"
#include "mbedtls/ecp.h"
#include "mbedtls/hkdf.h"
#include "mbedtls/md.h"
#include "mbedtls/platform_util.h"

#define INTRO "age-encryption.org/v1\n"
#define X25519_LABEL "age-encryption.org/v1/X25519"
#define COLS 64                         // a stanza body's full line
#define CHUNK 65536                     // STREAM plaintext chunk
#define TAG 16
#define MAX_STANZAS 64

static int (*rng_fn)(void *, unsigned char *, size_t);
static void *rng_ctx;

void age_set_rng(int (*rng)(void *ctx, unsigned char *out, size_t n), void *ctx)
{
    rng_fn = rng;
    rng_ctx = ctx;
}

const char *age_strerror(age_err_t e)
{
    switch (e) {
    case AGE_OK: return "ok";
    case AGE_ERR_HEADER: return "malformed header";
    case AGE_ERR_NO_MATCH: return "not encrypted to this identity";
    case AGE_ERR_HMAC: return "header MAC mismatch";
    case AGE_ERR_PAYLOAD: return "payload doesn't decrypt";
    case AGE_ERR_NOMEM: return "out of memory";
    default: return "crypto error";
    }
}

// ---- Bech32 (BIP 173), for age's key strings ----------------------------------------------
static const char CHARSET[] = "qpzry9x8gf2tvdw0s3jn54khce6mua7l";

static uint32_t polymod_step(uint32_t chk, uint8_t v)
{
    static const uint32_t G[5] = { 0x3b6a57b2, 0x26508e6d, 0x1ea119fa, 0x3d4233dd, 0x2a1462b3 };
    uint8_t top = (uint8_t)(chk >> 25);
    chk = (chk & 0x1ffffff) << 5 ^ v;
    for (int i = 0; i < 5; i++)
        if (top >> i & 1) chk ^= G[i];
    return chk;
}

// hrp (lower case) and 32 bytes of data, or -1
static int bech32_decode(const char *s, const char *hrp, uint8_t out[32])
{
    size_t n = strlen(s), hl = strlen(hrp);
    bool lower = false, upper = false;
    for (size_t i = 0; i < n; i++) {
        if (s[i] < 33 || s[i] > 126) return -1;
        lower |= s[i] >= 'a' && s[i] <= 'z';
        upper |= s[i] >= 'A' && s[i] <= 'Z';
    }
    if (lower && upper) return -1;
    const char *sep = strrchr(s, '1');
    if (!sep || (size_t)(sep - s) != hl || n - hl - 1 < 6) return -1;
    uint32_t chk = 1;
    for (size_t i = 0; i < hl; i++) {
        char c = s[i] >= 'A' && s[i] <= 'Z' ? (char)(s[i] + 32) : s[i];
        if (c != hrp[i]) return -1;
        chk = polymod_step(chk, (uint8_t)(c >> 5));
    }
    chk = polymod_step(chk, 0);
    for (size_t i = 0; i < hl; i++) chk = polymod_step(chk, (uint8_t)(hrp[i] & 31));
    uint32_t acc = 0;
    int bits = 0;
    size_t o = 0, nd = n - hl - 1;
    for (size_t i = 0; i < nd; i++) {
        char c = sep[1 + i];
        if (c >= 'A' && c <= 'Z') c = (char)(c + 32);
        const char *p = strchr(CHARSET, c);
        if (!p || !c) return -1;
        uint8_t v = (uint8_t)(p - CHARSET);
        chk = polymod_step(chk, v);
        if (i >= nd - 6) continue;            // the checksum
        acc = acc << 5 | v;
        bits += 5;
        if (bits >= 8) {
            bits -= 8;
            if (o == 32) return -1;
            out[o++] = (uint8_t)(acc >> bits);
            acc &= (1u << bits) - 1;
        }
    }
    if (chk != 1 || o != 32 || bits >= 5 || acc) return -1;
    return 0;
}

// ---- X25519 (mbedTLS) ---------------------------------------------------------------------
// out = X25519(scalar, u). -1 on any failure, including a low-order u (mbedTLS refuses those, and
// an all-zero result is refused here, as age requires).
static int x25519(uint8_t out[32], const uint8_t scalar[32], const uint8_t u[32])
{
    if (!rng_fn) return -1;
    mbedtls_ecp_group grp;
    mbedtls_mpi d;
    mbedtls_ecp_point P, R;
    mbedtls_ecp_group_init(&grp);
    mbedtls_mpi_init(&d);
    mbedtls_ecp_point_init(&P);
    mbedtls_ecp_point_init(&R);
    int rc = mbedtls_ecp_group_load(&grp, MBEDTLS_ECP_DP_CURVE25519);
    if (!rc) rc = mbedtls_mpi_read_binary_le(&d, scalar, 32);
    // RFC 7748 clamping
    if (!rc) rc = mbedtls_mpi_set_bit(&d, 0, 0);
    if (!rc) rc = mbedtls_mpi_set_bit(&d, 1, 0);
    if (!rc) rc = mbedtls_mpi_set_bit(&d, 2, 0);
    if (!rc) rc = mbedtls_mpi_set_bit(&d, 255, 0);
    if (!rc) rc = mbedtls_mpi_set_bit(&d, 254, 1);
    if (!rc) rc = mbedtls_ecp_point_read_binary(&grp, &P, u, 32);   // masks the top bit (RFC 7748)
    if (!rc) rc = mbedtls_ecp_mul(&grp, &R, &d, &P, rng_fn, rng_ctx);
    size_t olen = 0;
    if (!rc) rc = mbedtls_ecp_point_write_binary(&grp, &R, MBEDTLS_ECP_PF_UNCOMPRESSED, &olen, out, 32);   // u, little-endian
    if (!rc && olen != 32) rc = -1;
    mbedtls_ecp_point_free(&R);
    mbedtls_ecp_point_free(&P);
    mbedtls_mpi_free(&d);
    mbedtls_ecp_group_free(&grp);
    if (rc) return -1;
    uint8_t z = 0;
    for (int i = 0; i < 32; i++) z |= out[i];
    return z ? 0 : -1;
}

int age_identity_parse(const char *s, age_identity_t *id)
{
    static const uint8_t BASE[32] = { 9 };
    if (bech32_decode(s, "age-secret-key-", id->secret)) return -1;
    if (x25519(id->public, id->secret, BASE)) {
        mbedtls_platform_zeroize(id, sizeof *id);
        return -1;
    }
    return 0;
}

int age_recipient_parse(const char *s, uint8_t public_key[32]) { return bech32_decode(s, "age", public_key); }

// ---- the header ---------------------------------------------------------------------------
typedef struct {
    const char *type;
    size_t type_len;
    const char *arg[2];                   // the first two arguments (X25519 has exactly one)
    size_t arg_len[2];
    int n_args;
    uint8_t *body;
    size_t body_len;
} stanza_t;

static bool vchar(char c) { return c >= 0x21 && c <= 0x7e; }

// One stanza from p (just past "-> "); *pp moves past its body. -1 if malformed.
static int parse_stanza(const char **pp, const char *end, stanza_t *st)
{
    const char *p = *pp, *nl = memchr(p, '\n', (size_t)(end - p));
    if (!nl) return -1;
    memset(st, 0, sizeof *st);
    int n = -1;                           // -1 = the type, then the arguments
    for (const char *q = p; q < nl;) {
        const char *sp = memchr(q, ' ', (size_t)(nl - q)), *e = sp ? sp : nl;
        if (e == q) return -1;            // an empty argument (or a doubled space)
        for (const char *c = q; c < e; c++)
            if (!vchar(*c)) return -1;
        if (n < 0) { st->type = q; st->type_len = (size_t)(e - q); }
        else if (n < 2) { st->arg[n] = q; st->arg_len[n] = (size_t)(e - q); }
        n++;
        if (!sp) break;
        q = sp + 1;
        if (q == nl) return -1;           // a trailing space
    }
    if (n < 0) return -1;                 // no type
    st->n_args = n;
    // The body: lines of exactly COLS characters, then one shorter (maybe empty) line.
    const char *b = nl + 1, *body_start = b;
    size_t chars = 0;
    for (;;) {
        const char *l = memchr(b, '\n', (size_t)(end - b));
        if (!l) return -1;
        size_t len = (size_t)(l - b);
        if (len > COLS) return -1;
        chars += len;
        b = l + 1;
        if (len < COLS) break;
    }
    // copy out the base64 without the newlines, then decode it strictly
    char *raw = kw_alloc(chars + 1);
    st->body = kw_alloc(chars * 3 / 4 + 3);
    if (!raw || !st->body) { kw_free(raw); kw_free(st->body); st->body = NULL; return -2; }
    size_t o = 0;
    for (const char *c = body_start; c < b; c++)
        if (*c != '\n') raw[o++] = *c;
    long dl = b64_decode(raw, o, st->body, B64_RAW_STRICT);
    kw_free(raw);
    if (dl < 0) { kw_free(st->body); st->body = NULL; return -1; }
    st->body_len = (size_t)dl;
    *pp = b;
    return 0;
}

static bool is(const char *s, size_t n, const char *lit) { return n == strlen(lit) && !memcmp(s, lit, n); }

// Tries one X25519 stanza: 1 = it unwrapped the file key, 0 = not ours, <0 = an error.
static int try_x25519(const stanza_t *st, const age_identity_t *id, uint8_t file_key[16])
{
    if (st->n_args != 1 || st->arg_len[0] != 43 || st->body_len != 32) return AGE_ERR_HEADER;
    uint8_t share[33], shared[32], salt[64], key[32];
    if (b64_decode(st->arg[0], 43, share, B64_RAW_STRICT) != 32) return AGE_ERR_HEADER;
    if (x25519(shared, id->secret, share)) return AGE_ERR_HEADER;   // a low-order share
    memcpy(salt, share, 32);
    memcpy(salt + 32, id->public, 32);
    int rc = mbedtls_hkdf(mbedtls_md_info_from_type(MBEDTLS_MD_SHA256), salt, 64, shared, 32,
                          (const unsigned char *)X25519_LABEL, strlen(X25519_LABEL), key, 32);
    mbedtls_platform_zeroize(shared, sizeof shared);
    if (rc) return AGE_ERR_CRYPTO;
    static const uint8_t zero_nonce[12];
    mbedtls_chachapoly_context cp;
    mbedtls_chachapoly_init(&cp);
    rc = mbedtls_chachapoly_setkey(&cp, key);
    if (!rc) rc = mbedtls_chachapoly_auth_decrypt(&cp, 16, zero_nonce, NULL, 0, st->body + 16, st->body, file_key);
    mbedtls_chachapoly_free(&cp);
    mbedtls_platform_zeroize(key, sizeof key);
    if (rc == MBEDTLS_ERR_CHACHAPOLY_AUTH_FAILED) return 0;
    return rc ? AGE_ERR_CRYPTO : 1;
}

static int hkdf32(const uint8_t *salt, size_t salt_len, const uint8_t ikm[16], const char *info, uint8_t out[32])
{
    return mbedtls_hkdf(mbedtls_md_info_from_type(MBEDTLS_MD_SHA256), salt, salt_len, ikm, 16,
                        (const unsigned char *)info, strlen(info), out, 32);
}

// ---- the payload: STREAM ------------------------------------------------------------------
static age_err_t payload(const uint8_t file_key[16], const uint8_t *in, size_t len, uint8_t **out, size_t *out_len)
{
    if (len < 16) return AGE_ERR_HEADER;   // the nonce is part of the header, as far as age is concerned
    uint8_t key[32];
    if (hkdf32(in, 16, file_key, "payload", key)) return AGE_ERR_CRYPTO;
    in += 16;
    len -= 16;
    if (len < TAG) { mbedtls_platform_zeroize(key, sizeof key); return AGE_ERR_PAYLOAD; }   // not even one chunk
    uint8_t *pt = kw_alloc(len + 1);
    if (!pt) { mbedtls_platform_zeroize(key, sizeof key); return AGE_ERR_NOMEM; }
    mbedtls_chachapoly_context cp;
    mbedtls_chachapoly_init(&cp);
    age_err_t err = mbedtls_chachapoly_setkey(&cp, key) ? AGE_ERR_CRYPTO : AGE_OK;
    size_t pos = 0, o = 0;
    for (uint64_t counter = 0; !err && pos < len; counter++) {
        size_t n = len - pos > CHUNK + TAG ? CHUNK + TAG : len - pos;
        bool last = pos + n == len;
        // Only the first chunk may be empty (an empty file), and every chunk carries a tag.
        if (n < TAG || (n == TAG && counter > 0)) { err = AGE_ERR_PAYLOAD; break; }
        uint8_t nonce[12] = { 0 };
        for (int i = 0; i < 8; i++) nonce[10 - i] = (uint8_t)(counter >> (8 * i));
        nonce[11] = last;
        int rc = mbedtls_chachapoly_auth_decrypt(&cp, n - TAG, nonce, NULL, 0, in + pos + n - TAG, in + pos, pt + o);
        if (rc) { err = rc == MBEDTLS_ERR_CHACHAPOLY_AUTH_FAILED ? AGE_ERR_PAYLOAD : AGE_ERR_CRYPTO; break; }
        pos += n;
        o += n - TAG;
    }
    mbedtls_chachapoly_free(&cp);
    mbedtls_platform_zeroize(key, sizeof key);
    if (err) {
        mbedtls_platform_zeroize(pt, o);
        kw_free(pt);
        return err;
    }
    pt[o] = 0;
    *out = pt;
    *out_len = o;
    return AGE_OK;
}

age_err_t age_decrypt(const age_identity_t *id, const uint8_t *in, size_t len, uint8_t **out, size_t *out_len,
                      uint8_t file_key_out[16])
{
    const char *p = (const char *)in, *end = p + len;
    size_t il = strlen(INTRO);
    if (len < il || memcmp(p, INTRO, il)) return AGE_ERR_HEADER;
    p += il;
    stanza_t *st = kw_alloc(MAX_STANZAS * sizeof *st);
    if (!st) return AGE_ERR_NOMEM;
    int ns = 0;
    age_err_t err = AGE_OK;
    const char *mac_end = NULL;
    uint8_t mac[33];
    // stanzas, then "--- <MAC>"
    for (;;) {
        if (end - p >= 3 && !memcmp(p, "---", 3)) {
            const char *nl = memchr(p, '\n', (size_t)(end - p));
            if (!nl || nl - p != 3 + 1 + 43 || p[3] != ' ' || b64_decode(p + 4, 43, mac, B64_RAW_STRICT) != 32) {
                err = AGE_ERR_HEADER;
                break;
            }
            mac_end = p + 3;
            p = nl + 1;
            break;
        }
        if (end - p < 3 || memcmp(p, "-> ", 3) || ns == MAX_STANZAS) { err = AGE_ERR_HEADER; break; }
        p += 3;
        int rc = parse_stanza(&p, end, &st[ns]);
        if (rc) { err = rc == -2 ? AGE_ERR_NOMEM : AGE_ERR_HEADER; break; }
        ns++;
    }
    if (!err && ns == 0) err = AGE_ERR_HEADER;

    // The file key from the first X25519 stanza that opens with this identity. A passphrase (scrypt)
    // stanza must be a file's only one.
    uint8_t file_key[16];
    bool found = false;
    for (int i = 0; !err && i < ns; i++)
        if (is(st[i].type, st[i].type_len, "scrypt") && ns > 1) err = AGE_ERR_HEADER;
    for (int i = 0; !err && !found && i < ns; i++) {
        if (!is(st[i].type, st[i].type_len, "X25519")) continue;   // grease, other people's types
        int rc = try_x25519(&st[i], id, file_key);
        if (rc < 0) err = (age_err_t)rc;
        else found = rc == 1;
    }
    for (int i = 0; i < ns; i++) kw_free(st[i].body);
    kw_free(st);
    if (!err && !found) err = AGE_ERR_NO_MATCH;
    if (err) return err;

    // The header's MAC: HMAC-SHA-256 under HKDF(file key, "header"), over everything up to "---".
    uint8_t hkey[32], want[32];
    if (hkdf32(NULL, 0, file_key, "header", hkey) ||
        mbedtls_md_hmac(mbedtls_md_info_from_type(MBEDTLS_MD_SHA256), hkey, 32, in, (size_t)(mac_end - (const char *)in),
                        want)) {
        err = AGE_ERR_CRYPTO;
    } else if (mbedtls_ct_memcmp(want, mac, 32)) {
        err = AGE_ERR_HMAC;
    }
    mbedtls_platform_zeroize(hkey, sizeof hkey);
    if (!err) err = payload(file_key, (const uint8_t *)p, (size_t)(end - p), out, out_len);
    if (!err && file_key_out) memcpy(file_key_out, file_key, 16);
    mbedtls_platform_zeroize(file_key, sizeof file_key);
    return err;
}
