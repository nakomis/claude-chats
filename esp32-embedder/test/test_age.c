// CHATS-5: age decryption (worker/age.c).
//   1. age's test kit (C2SP CCTV, age-testkit/, prepared by testkit.py into build/testkit): every
//      X25519 vector gives exactly the expected outcome (success with the right payload hash and
//      file key, or the right kind of failure).
//   2. Track C's golden fixtures (../host/convmem-wire, from home-infra cal/convmem-wire): the
//      ingest envelope's ciphertext is a real two-recipient age file (Cal, Kata and a grease
//      stanza) with TEST-ONLY identities. Kata's key opens it to job.json, Cal's does too, and a
//      stranger's, or a flipped byte, doesn't.
#include "age.h"
#include "kw.h"
#include "testutil.h"

#include "cJSON.h"
#include "mbedtls/sha256.h"

static int test_rng(void *ctx, unsigned char *out, size_t n)
{
    static uint32_t s = 0x9e3779b9;
    (void)ctx;
    for (size_t i = 0; i < n; i++) { s ^= s << 13; s ^= s >> 17; s ^= s << 5; out[i] = (uint8_t)s; }
    return 0;
}

static void hex(const uint8_t *in, size_t n, char *out)
{
    for (size_t i = 0; i < n; i++) snprintf(out + 2 * i, 3, "%02x", in[i]);
}

static age_err_t expected(const char *e)
{
    if (!strcmp(e, "success")) return AGE_OK;
    if (!strcmp(e, "header failure")) return AGE_ERR_HEADER;
    if (!strcmp(e, "no match")) return AGE_ERR_NO_MATCH;
    if (!strcmp(e, "HMAC failure")) return AGE_ERR_HMAC;
    if (!strcmp(e, "payload failure")) return AGE_ERR_PAYLOAD;
    return (age_err_t)99;
}

static void testkit(void)
{
    char *index = read_file_or_die("testkit/INDEX", NULL);
    int n = 0, by[5] = { 0 };
    for (char *name = strtok(index, "\n"); name; name = strtok(NULL, "\n")) {
        char path[256];
        snprintf(path, sizeof path, "testkit/%s.meta", name);
        char *meta = read_file_or_die(path, NULL);
        snprintf(path, sizeof path, "testkit/%s.age", name);
        size_t len;
        uint8_t *file = (uint8_t *)read_file_or_die(path, &len);
        char expect[32] = "", payload[65] = "", file_key[33] = "";
        char ids[4][80];
        int nid = 0;
        char *save, *line;
        for (line = strtok_r(meta, "\n", &save); line; line = strtok_r(NULL, "\n", &save)) {
            if (!strncmp(line, "expect: ", 8)) snprintf(expect, sizeof expect, "%s", line + 8);
            else if (!strncmp(line, "payload: ", 9)) snprintf(payload, sizeof payload, "%s", line + 9);
            else if (!strncmp(line, "file key: ", 10)) snprintf(file_key, sizeof file_key, "%s", line + 10);
            else if (!strncmp(line, "identity: ", 10) && nid < 4) snprintf(ids[nid++], sizeof ids[0], "%s", line + 10);
        }
        // as age does with several identities: the first that isn't "no match" decides
        age_err_t got = AGE_ERR_NO_MATCH;
        uint8_t *out = NULL, fk[16];
        size_t out_len = 0;
        for (int i = 0; i < nid && got == AGE_ERR_NO_MATCH; i++) {
            age_identity_t id;
            CHECK(age_identity_parse(ids[i], &id) == 0, "%s: identity %s doesn't parse", name, ids[i]);
            got = age_decrypt(&id, file, len, &out, &out_len, fk);
        }
        age_err_t want = expected(expect);
        CHECK(got == want, "%s: expected %s, got %s", name, expect, age_strerror(got));
        if (got == AGE_OK) {
            uint8_t h[32];
            char hh[65], fkh[33];
            mbedtls_sha256(out, out_len, h, 0);
            hex(h, 32, hh);
            hex(fk, 16, fkh);
            CHECK(!strcmp(hh, payload), "%s: payload sha256 %s, want %s", name, hh, payload);
            CHECK(!file_key[0] || !strcmp(fkh, file_key), "%s: file key %s, want %s", name, fkh, file_key);
            kw_free(out);
        }
        if (want >= -4 && want <= 0) by[-want]++;
        n++;
        free(meta);
        free(file);
    }
    free(index);
    printf("age test kit: %d vectors (%d success, %d header, %d no match, %d HMAC, %d payload failures)\n", n, by[0],
           by[1], by[2], by[3], by[4]);
    CHECK(n >= 60, "only %d vectors: was build/testkit made?", n);
}

// The identity from a fixture file: its AGE-SECRET-KEY-1 line, and the public key in its comment.
static void fixture_identity(const char *path, char *secret, char *public_key)
{
    char *text = read_file_or_die(path, NULL), *save;
    for (char *line = strtok_r(text, "\n", &save); line; line = strtok_r(NULL, "\n", &save)) {
        if (!strncmp(line, "AGE-SECRET-KEY-1", 16)) snprintf(secret, 100, "%s", line);
        const char *pk = strstr(line, "public key: ");
        if (pk) snprintf(public_key, 100, "%s", pk + 12);
    }
    free(text);
}

static void fixtures(void)
{
    char *env_text = read_file_or_die("../../host/convmem-wire/ingest-inline.json", NULL);
    cJSON *env = cJSON_Parse(env_text);
    free(env_text);
    const char *ct64 = cJSON_GetStringValue(cJSON_GetObjectItem(env, "ciphertext"));
    CHECK(ct64 != NULL, "ingest-inline.json has no ciphertext");
    if (!ct64) { cJSON_Delete(env); return; }
    size_t ctl = strlen(ct64);
    uint8_t *ct = malloc(ctl);
    long n = b64_decode(ct64, ctl, ct, B64_PADDED);
    CHECK(n > 0, "the ciphertext isn't padded base64");
    size_t job_len;
    char *job = read_file_or_die("../../host/convmem-wire/job.json", &job_len);
    while (job_len && job[job_len - 1] == '\n') job[--job_len] = 0;

    static const char *const WHO[] = { "kata", "cal" };
    for (int w = 0; w < 2; w++) {
        char path[128], secret[100] = "", public_key[100] = "";
        snprintf(path, sizeof path, "../../host/convmem-wire/test-identity-%s.txt", WHO[w]);
        fixture_identity(path, secret, public_key);
        age_identity_t id;
        uint8_t pk[32];
        CHECK(age_identity_parse(secret, &id) == 0, "%s: identity doesn't parse", WHO[w]);
        CHECK(age_recipient_parse(public_key, pk) == 0 && !memcmp(pk, id.public, 32),
              "%s: X25519(secret, 9) isn't the public key in the comment", WHO[w]);
        uint8_t *out = NULL;
        size_t out_len = 0;
        age_err_t e = age_decrypt(&id, ct, (size_t)n, &out, &out_len, NULL);
        CHECK(e == AGE_OK, "%s: fixture doesn't decrypt: %s", WHO[w], age_strerror(e));
        if (e == AGE_OK) {
            CHECK(out_len == job_len && !memcmp(out, job, job_len), "%s: plaintext isn't job.json:\n%.*s", WHO[w],
                  (int)out_len, out);
            kw_free(out);
        }
        // one flipped bit anywhere in the payload, or in the header's MAC, and it's refused
        uint8_t *bad = malloc((size_t)n);
        memcpy(bad, ct, (size_t)n);
        bad[n - 5] ^= 1;
        CHECK(age_decrypt(&id, bad, (size_t)n, &out, &out_len, NULL) == AGE_ERR_PAYLOAD, "%s: tampered payload", WHO[w]);
        memcpy(bad, ct, (size_t)n);
        char *dashes = strstr((char *)bad, "\n--- ");
        CHECK(dashes != NULL, "no MAC line");
        if (dashes) {
            dashes[6] = dashes[6] == 'A' ? 'B' : 'A';
            age_err_t te = age_decrypt(&id, bad, (size_t)n, &out, &out_len, NULL);
            CHECK(te == AGE_ERR_HMAC || te == AGE_ERR_HEADER, "%s: tampered MAC gave %s", WHO[w], age_strerror(te));
        }
        free(bad);
    }
    // a key it wasn't encrypted to (bytes 1..32, Bech32-encoded here: a TEST-ONLY key)
    age_identity_t stranger;
    CHECK(age_identity_parse("AGE-SECRET-KEY-1QYPQXPQ9QCRSSZG2PVXQ6RS0ZQG3YYC5Z5TPWXQERGD3C8G7RUSQGPQYEE", &stranger) == 0,
          "stranger's identity doesn't parse");
    uint8_t *out = NULL;
    size_t out_len = 0;
    CHECK(age_decrypt(&stranger, ct, (size_t)n, &out, &out_len, NULL) == AGE_ERR_NO_MATCH, "a stranger matched");
    // malformed identities
    age_identity_t id;
    CHECK(age_identity_parse("AGE-SECRET-KEY-1QYPQXPQ9QCRSSZG2PVXQ6RS0ZQG3YYC5Z5TPWXQERGD3C8G7RUSQGPQYEF", &id) == -1, "bad checksum");
    CHECK(age_identity_parse("age-secret-key-1QYPQXPQ9QCRSSZG2PVXQ6RS0ZQG3YYC5Z5TPWXQERGD3C8G7RUSQGPQYEE", &id) == -1, "mixed case");
    CHECK(age_identity_parse("AGE-SECRET-KEY-1", &id) == -1, "empty");
    free(ct);
    free(job);
    cJSON_Delete(env);
}

int main(void)
{
    age_set_rng(test_rng, NULL);
    testkit();
    fixtures();
    return report("age");
}
