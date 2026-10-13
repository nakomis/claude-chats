// CHATS-5: the results message (worker/result.c) against track C's golden fixtures
// (../host/convmem-wire, copied from home-infra cal/convmem-wire/fixtures). Each fixture's own
// values go through result_encode, and what comes out must be the same JSON as the fixture (cJSON
// compares them as values: the fixtures are pretty-printed, Kata's output is compact). The envelope
// must be the ingest body byte for byte, whatever it holds.
#include "kw.h"
#include "result.h"
#include "testutil.h"

#include "cJSON.h"

#define FIX "../../host/convmem-wire/"

static const char *str(const cJSON *o, const char *k) { return cJSON_GetStringValue(cJSON_GetObjectItem(o, k)); }
static int num(const cJSON *o, const char *k) { return (int)cJSON_GetNumberValue(cJSON_GetObjectItem(o, k)); }

// Encodes the fixture's values with ingest as the envelope, and compares.
static void against(const char *fixture, const char *ingest_file)
{
    char path[128];
    snprintf(path, sizeof path, FIX "%s", fixture);
    char *text = read_file_or_die(path, NULL);
    cJSON *want = cJSON_Parse(text);
    free(text);
    snprintf(path, sizeof path, FIX "%s", ingest_file);
    size_t env_len;
    char *env = read_file_or_die(path, &env_len);

    kata_result_t r = { 0 };
    r.embedded = !strcmp(str(want, "kind"), "embedded");
    r.message_uuid = str(want, "message_uuid");
    r.envelope = env;
    r.envelope_len = env_len;
    float *vec = NULL;
    if (r.embedded) {
        const char *v64 = str(want, "vector");
        vec = malloc(RESULT_DIMS * 4 + 3);
        CHECK(b64_decode(v64, strlen(v64), (uint8_t *)vec, B64_PADDED) == RESULT_DIMS * 4, "%s: vector", fixture);
        r.embedder = str(want, "embedder");
        r.vector = vec;
        r.tokens = num(want, "tokens");
        r.truncated_chars = cJSON_IsTrue(cJSON_GetObjectItem(want, "truncated_chars"));
    } else {
        r.reason = str(want, "reason");
    }
    r.board = str(want, "board");
    r.fw = str(want, "fw");
    const cJSON *pass = cJSON_GetObjectItem(want, "pass");
    if (cJSON_IsObject(pass)) {
        r.has_pass = true;
        r.pass_n_seg = num(pass, "n_seg");
        r.pass_tokens = num(pass, "tokens");
        r.pass_ms = (uint32_t)num(pass, "ms");
    }
    const cJSON *ingest = cJSON_GetObjectItem(want, "ingest");
    r.message_id = str(ingest, "message_id");
    r.receive_count = num(ingest, "receive_count");

    jbuf_t out;
    jb_init(&out);
    CHECK(result_encode(&r, &out) == 0, "%s: encode", fixture);
    cJSON *got = cJSON_Parse(out.p);
    CHECK(got != NULL, "%s: Kata's output isn't JSON:\n%s", fixture, out.p);
    CHECK(cJSON_Compare(got, want, true), "%s: differs from the fixture:\n%.600s", fixture, out.p);
    CHECK(strstr(out.p, env) != NULL, "%s: the envelope isn't the ingest body verbatim", fixture);
    CHECK(!strchr(out.p, '\n') || strchr(env, '\n'), "%s: not compact", fixture);
    printf("%s: %zu bytes (fixture's values, compact)\n", fixture, out.n);
    jb_free(&out);
    cJSON_Delete(got);
    cJSON_Delete(want);
    free(vec);
    free(env);
}

int main(void)
{
    against("result-embedded.json", "ingest-inline.json");
    against("result-unembedded.json", "ingest-inline.json");
    against("result-claim-check.json", "ingest-claim-check.json");

    // The envelope is opaque: fields Kata has never heard of (the forwarder's signature), odd key
    // order and whitespace all pass through byte for byte.
    const char *env = "{ \"sig\":\"c2ln\" , \"v\":1,\"message_uuid\":\"u-1\",\"ciphertext\":\"YWJj\",\"sig_key\":\"example-mac\",\"future\":{\"x\":[1,2]} }";
    float vec[RESULT_DIMS];
    for (int i = 0; i < RESULT_DIMS; i++) vec[i] = (float)i / 7 - 50;
    kata_result_t r = { .embedded = true, .message_uuid = "u-1", .envelope = env, .envelope_len = strlen(env),
                        .embedder = "esp32s3-mxbai-int8-v1", .vector = vec, .tokens = 9, .board = "kata-sandbox-blue",
                        .fw = "0.2.0", .message_id = "m-1", .receive_count = 2 };
    jbuf_t out;
    jb_init(&out);
    CHECK(result_encode(&r, &out) == 0, "encode");
    CHECK(strstr(out.p, ",\"envelope\":{ \"sig\":\"c2ln\" , \"v\":1,") != NULL, "envelope not verbatim: %.200s", out.p);
    cJSON *got = cJSON_Parse(out.p);
    CHECK(got && !strcmp(str(cJSON_GetObjectItem(got, "envelope"), "sig_key"), "example-mac"), "sig_key lost");
    CHECK(got && cJSON_IsNull(cJSON_GetObjectItem(got, "pass")), "pass should be null");
    // the vector round-trips bit for bit
    uint8_t back[RESULT_DIMS * 4 + 3];
    const char *v64 = got ? str(got, "vector") : "";
    CHECK(b64_decode(v64, strlen(v64), back, B64_PADDED) == RESULT_DIMS * 4 && !memcmp(back, vec, sizeof vec),
          "vector doesn't round-trip");
    cJSON_Delete(got);

    // The size budget (convmem_wire::MAX_INLINE_BYTES_CEILING): a 240,000-byte inline body plus
    // the vector and the rest must stay under SQS's 256 KiB.
    jbuf_t big;
    jb_init(&big);
    jb_puts(&big, "{\"v\":1,\"message_uuid\":\"3f1c2b8e-5d4a-4e7f-9a01-6b2c8d9e0f12\",\"ciphertext\":\"");
    while (big.n < 240000 - 120) jb_putc(&big, 'A');
    jb_puts(&big, "\",\"sig_key\":\"example-mac\",\"sig\":\"");
    for (int i = 0; i < 88; i++) jb_putc(&big, 'B');
    jb_puts(&big, "\"}");
    r.envelope = big.p;
    r.envelope_len = big.n;
    r.message_uuid = "3f1c2b8e-5d4a-4e7f-9a01-6b2c8d9e0f12";
    r.message_id = "0f6c1d1e-2b3a-4c5d-8e9f-a0b1c2d3e4f5";
    r.has_pass = true;
    r.pass_n_seg = 40; r.pass_tokens = 512; r.pass_ms = 288000;
    jb_reset(&out);
    CHECK(result_encode(&r, &out) == 0, "encode big");
    printf("worst case: a %zu-byte envelope makes a %zu-byte result (SQS's limit 262144)\n", big.n, out.n);
    CHECK(out.n < 262144 && out.n - big.n < 7000, "too big: %zu", out.n);
    jb_free(&big);
    jb_free(&out);
    return report("result");
}
