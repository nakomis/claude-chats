// CHATS-5: the worker (worker/worker.c) end to end on the host: real age jobs (encrypted to a
// TEST-ONLY Kata key) on a fake SQS, a fake model whose vectors are a function of each message's
// own tokens (so a vector that lands on the wrong job shows), and faults on demand.
//
// The invariant checked throughout: a job is always held by at least one queue (the ingest job
// isn't deleted until its result is on the results queue).
#include "age.h"
#include "age_enc.h"
#include "fake_sqs.h"
#include "kw.h"
#include "result.h"
#include "testutil.h"
#include "worker.h"

#include "cJSON.h"

#define INGEST "https://sqs.eu-west-2.amazonaws.com/000000000000/conversation-memory-test"
#define RESULTS "https://sqs.eu-west-2.amazonaws.com/000000000000/conversation-memory-results-test"
#define TAG "esp32s3-mxbai-int8-v1"

// ---- the fake board -------------------------------------------------------------------------
typedef struct {
    fake_sqs_t f;
    sqs_t sqs;
    aws_creds_t creds;
    int net_fail;                 // the next n network phases fail
    bool in_net;
    int bad_passes;               // the next n passes are bad (a CRC mismatch)
    int passes, health_calls, health_ok;
    int last_n_seg, max_n_seg;
    char last_text[1024];         // the last text tokenised
    bool quiet;
} board_t;

static int64_t b_now(void *ctx) { return ((board_t *)ctx)->f.now; }

static int b_net_begin(void *ctx, char *why, size_t n)
{
    board_t *b = ctx;
    CHECK(!b->in_net, "network phases overlap");
    if (b->net_fail > 0) { b->net_fail--; snprintf(why, n, "injected: no Wi-Fi"); return -1; }
    b->in_net = true;
    return 0;
}

static void b_net_end(void *ctx) { ((board_t *)ctx)->in_net = false; }

static uint32_t hash(uint32_t h, uint32_t v) { h ^= v; h *= 16777619u; return h; }

// words -> ids: [CLS] one id per word [SEP], truncated as Ollama does
static int b_tokenise(void *ctx, const char *text, size_t len, int32_t *ids, int max)
{
    board_t *b = ctx;
    snprintf(b->last_text, sizeof b->last_text, "%.*s", (int)len, text);
    int n = 0;
    ids[n++] = 101;
    uint32_t h = 2166136261u;
    bool in = false;
    for (size_t i = 0; i <= len; i++) {
        bool sp = i == len || text[i] == ' ';
        if (!sp) { h = hash(h, (unsigned char)text[i]); in = true; continue; }
        if (in && n < max - 1) ids[n++] = (int32_t)(1000 + h % 28000);
        h = 2166136261u;
        in = false;
    }
    ids[n++] = 102;
    return n;
}

// a message's vector: a function of its ids only
static void vec_of(const int32_t *ids, int n, float *v)
{
    uint32_t h = 2166136261u;
    for (int i = 0; i < n; i++) h = hash(h, (uint32_t)ids[i]);
    for (int d = 0; d < RESULT_DIMS; d++) { h = hash(h, (uint32_t)d); v[d] = (float)(h % 2001) / 1000.0f - 1.0f; }
}

static int b_embed(void *ctx, const int32_t *ids, const int *lens, int n_seg, float *vecs, worker_pass_info_t *info)
{
    board_t *b = ctx;
    CHECK(!b->in_net, "a pass during the network phase");
    b->passes++;
    b->last_n_seg = n_seg;
    if (n_seg > b->max_n_seg) b->max_n_seg = n_seg;
    int t = 0;
    for (int s = 0; s < n_seg; s++) { vec_of(ids + t, lens[s], vecs + (size_t)s * RESULT_DIMS); t += lens[s]; }
    CHECK(t <= 512, "a pass of %d tokens", t);
    b->f.now += 75;                            // a pass takes 75 s
    info->ms = 75000;
    info->card_wait_ms = 60000 + (uint32_t)b->passes;
    if (b->bad_passes > 0) {
        b->bad_passes--;
        info->crc_bad = 1;
        info->why = "section CRC mismatch: wq 3/1";
        return -1;
    }
    return 0;
}

static int b_health(void *ctx)
{
    board_t *b = ctx;
    b->health_calls++;
    return b->health_ok ? 0 : -1;
}

static void b_log(void *ctx, const char *msg)
{
    if (!((board_t *)ctx)->quiet) printf("    %s\n", msg);
}

// ---- jobs -------------------------------------------------------------------------------
static uint8_t kata_pub[32], cal_pub[32], stranger_pub[32];
static age_identity_t kata_id;

// A job as the forwarder writes it, encrypted to the given recipients, enveloped (with a signature
// field, opaque to Kata) and put on the ingest queue. Returns the body.
static char *make_job(board_t *b, const char *uuid, const char *content_json, const uint8_t (*to)[32], int n_to)
{
    jbuf_t job, ct, env;
    jb_init(&job); jb_init(&ct); jb_init(&env);
    jb_printf(&job, "{\"session_id\":\"s\",\"host\":\"example-mac\",\"message_uuid\":\"%s\",\"role\":\"user\",\"author\":\"martin\","
                    "\"content\":%s,\"sequence_num\":1,\"created_at\":\"2026-10-13T15:00:00+00:00\"}", uuid, content_json);
    age_encrypt(to, n_to, (const uint8_t *)job.p, job.n, &ct);
    jb_printf(&env, "{\"v\":1,\"message_uuid\":\"%s\",\"ciphertext\":\"", uuid);
    jb_b64(&env, (const uint8_t *)ct.p, ct.n);
    jb_puts(&env, "\",\"sig_key\":\"example-mac\",\"sig\":\"c2lnbmF0dXJlLWZvci10ZXN0cw==\"}");
    fq_send(&b->f, INGEST, env.p);
    jb_free(&job); jb_free(&ct);
    return jb_take(&env, NULL);
}

static char *make_job_text(board_t *b, const char *uuid, const char *words)
{
    char content[2048];
    snprintf(content, sizeof content, "\"%s\"", words);
    uint8_t to[2][32];
    memcpy((to)[0], cal_pub, 32);
    memcpy((to)[1], kata_pub, 32);
    return make_job(b, uuid, content, to, 2);
}

static void board_init(board_t *b, worker_t *w, worker_cfg_t *cfg)
{
    memset(b, 0, sizeof *b);
    snprintf(b->creds.access_key, sizeof b->creds.access_key, "ASIATESTONLY");
    snprintf(b->creds.secret_key, sizeof b->creds.secret_key, "secretTESTONLY");
    b->creds.token = kw_strndup("tokenTESTONLY", 13);
    b->creds.expires = 2000000000;
    fq_init(&b->f, &b->creds, 1791914400);
    fq_queue(&b->f, INGEST);
    fq_queue(&b->f, RESULTS);
    sqs_init(&b->sqs, INGEST, &b->creds, fq_now, fq_post, &b->f);
    b->health_ok = 1;
    b->quiet = true;
    worker_cfg_t c = { INGEST, RESULTS, "kata-sandbox-blue", "0.2.0", TAG, &kata_id, 150, 1024 * 1024, 40 };
    if (cfg) c = *cfg;
    worker_io_t io = { b, b_now, b_net_begin, b_net_end, &b->sqs, b_tokenise, b_embed, b_health, b_log };
    CHECK(worker_init(w, &c, &io) == 0, "init");
}

static void board_free(board_t *b, worker_t *w)
{
    worker_free(w);
    sqs_free(&b->sqs);
    fq_free(&b->f);
    creds_clear(&b->creds);
}

// ---- checks ------------------------------------------------------------------------------
static int results(board_t *b) { return fq_queue(&b->f, RESULTS)->n; }

// The result for uuid on the results queue (the latest, if several), parsed; NULL if none.
static cJSON *result_for(board_t *b, const char *uuid)
{
    fq_queue_t *q = fq_queue(&b->f, RESULTS);
    for (int i = q->n - 1; i >= 0; i--) {
        cJSON *r = cJSON_Parse(q->msgs[i].body);
        const char *u = cJSON_GetStringValue(cJSON_GetObjectItem(r, "message_uuid"));
        if (u && !strcmp(u, uuid)) return r;
        cJSON_Delete(r);
    }
    return NULL;
}

// Every job sent is still on the ingest queue or has a result.
static void invariant(board_t *b, const char *const *uuids, int n, const char *when)
{
    fq_queue_t *in = fq_queue(&b->f, INGEST);
    for (int i = 0; i < n; i++) {
        bool held = false;
        for (int k = 0; k < in->n && !held; k++)
            held = !in->msgs[k].deleted && strstr(in->msgs[k].body, uuids[i]);
        cJSON *r = held ? NULL : result_for(b, uuids[i]);
        CHECK(held || r, "%s: job %s is in neither queue", when, uuids[i]);
        cJSON_Delete(r);
    }
}

static void check_embedded(board_t *b, const char *uuid, const char *body, const char *words)
{
    cJSON *r = result_for(b, uuid);
    CHECK(r != NULL, "%s: no result", uuid);
    if (!r) return;
    CHECK(!strcmp(cJSON_GetStringValue(cJSON_GetObjectItem(r, "kind")), "embedded"), "%s: not embedded", uuid);
    CHECK(!strcmp(cJSON_GetStringValue(cJSON_GetObjectItem(r, "embedder")), TAG), "%s: tag", uuid);
    CHECK(!strcmp(cJSON_GetStringValue(cJSON_GetObjectItem(r, "board")), "kata-sandbox-blue"), "%s: board", uuid);
    CHECK(!strcmp(cJSON_GetStringValue(cJSON_GetObjectItem(r, "fw")), "0.2.0"), "%s: fw", uuid);
    // the envelope is the ingest body, byte for byte, signature fields and all
    fq_queue_t *q = fq_queue(&b->f, RESULTS);
    bool verbatim = false;
    for (int i = 0; i < q->n && !verbatim; i++) verbatim = strstr(q->msgs[i].body, body) != NULL;
    CHECK(verbatim, "%s: the envelope isn't spliced verbatim", uuid);
    // the vector is this job's own
    int32_t ids[512];
    board_t tmp;
    int n = b_tokenise(&tmp, words, strlen(words), ids, 512);
    float want[RESULT_DIMS], got[RESULT_DIMS + 1];
    vec_of(ids, n, want);
    const char *v64 = cJSON_GetStringValue(cJSON_GetObjectItem(r, "vector"));
    CHECK(v64 && b64_decode(v64, strlen(v64), (uint8_t *)got, B64_PADDED) == RESULT_DIMS * 4 && !memcmp(got, want, sizeof want),
          "%s: the vector isn't its own", uuid);
    CHECK((int)cJSON_GetNumberValue(cJSON_GetObjectItem(r, "tokens")) == n, "%s: tokens", uuid);
    cJSON_Delete(r);
}

static const char *reason_of(board_t *b, const char *uuid)
{
    static char reason[32];
    cJSON *r = result_for(b, uuid);
    const char *s = r ? cJSON_GetStringValue(cJSON_GetObjectItem(r, "reason")) : NULL;
    snprintf(reason, sizeof reason, "%s", s ? s : r ? "(embedded)" : "(no result)");
    cJSON_Delete(r);
    return reason;
}

// ---- scenarios --------------------------------------------------------------------------
static void happy_path(void)
{
    printf("-- five jobs, one pass, handed off\n");
    board_t b;
    worker_t w;
    board_init(&b, &w, NULL);
    const char *uuids[5] = { "u-0", "u-1", "u-2", "u-3", "u-4" };
    const char *words[5] = { "the cat sat on the radiator", "shall I carry on", "git push failed permission denied",
                             "a b c d e f g h i j k l m n o p", "Martin asked whether a cheap board could embed" };
    char *bodies[5];
    for (int i = 0; i < 5; i++) bodies[i] = make_job_text(&b, uuids[i], words[i]);
    int wait = worker_step(&w);               // receive all five, one pass
    CHECK(wait == 0 && b.passes == 1 && b.last_n_seg == 5, "one pass of five: wait %d, %d passes, %d segments", wait, b.passes,
          b.last_n_seg);
    CHECK(results(&b) == 0, "nothing handed off before the next phase");
    invariant(&b, uuids, 5, "after the pass");
    wait = worker_step(&w);                   // hand off, then an empty long poll
    CHECK(results(&b) == 5 && fq_live(fq_queue(&b.f, INGEST)) == 0, "handed off and deleted: %d results, %d left", results(&b),
          fq_live(fq_queue(&b.f, INGEST)));
    CHECK(wait == WORKER_IDLE_S, "then idle: %d", wait);
    for (int i = 0; i < 5; i++) check_embedded(&b, uuids[i], bodies[i], words[i]);
    CHECK(w.boot.messages == 5 && w.boot.passes == 1 && w.today.messages == 5, "counts");
    CHECK(b.f.bad_signatures == 0, "signatures");
    // the status object parses, with the fields BD-1 reads
    jbuf_t s;
    jb_init(&s);
    worker_status_json(&w, &s);
    cJSON *st = cJSON_Parse(s.p);
    CHECK(st && cJSON_GetObjectItem(st, "last_pass") && cJSON_GetObjectItem(cJSON_GetObjectItem(st, "passes"), "today"),
          "status: %s", s.p);
    CHECK(st && (int)cJSON_GetNumberValue(cJSON_GetObjectItem(cJSON_GetObjectItem(st, "messages"), "boot")) == 5, "status messages");
    // The heartbeat's MQTT buffer is 1.5 KB (fleet.c), shared with ~450 bytes of board fields: the
    // worker's object, at its fullest (every reason counted, sick, a long error), must stay well under.
    for (int i = 0; i < 5; i++) w.boot.unembedded[i] = 99999;
    w.sick = true;
    w.sick_since = 1791914400;
    memset(w.last_error, 'e', sizeof w.last_error - 1);
    w.last_handoff_at = 1791914400;
    jb_reset(&s);
    worker_status_json(&w, &s);
    printf("    the worker's status object, at its fullest: %zu bytes\n", s.n);
    cJSON *full = cJSON_Parse(s.p);
    CHECK(s.n < 900 && full != NULL, "status too big: %zu", s.n);
    cJSON_Delete(full);
    w.sick = false;
    cJSON_Delete(st);
    jb_free(&s);
    uint32_t last, med;
    worker_card_wait(&w, &last, &med);
    CHECK(last == 60001 && med == 60001, "card wait %u %u", last, med);
    for (int i = 0; i < 5; i++) kw_free(bodies[i]);
    board_free(&b, &w);
}

static void sorting(void)
{
    printf("-- claim checks, strangers, garbage, second and third deliveries\n");
    board_t b;
    worker_t w;
    board_init(&b, &w, NULL);
    // an S3 claim check
    fq_send(&b.f, INGEST, "{\"v\":1,\"message_uuid\":\"u-s3\",\"s3\":{\"bucket\":\"b\",\"key\":\"payloads/u-s3\"}}");
    // encrypted to Cal only (before the forwarder knew Kata)
    uint8_t cal_only[1][32];
    memcpy((cal_only)[0], cal_pub, 32);
    char *b1 = make_job(&b, "u-cal", "\"for Cal only\"", cal_only, 1);
    // the job inside claims another uuid
    {
        jbuf_t job, ct, env;
        jb_init(&job); jb_init(&ct); jb_init(&env);
        jb_puts(&job, "{\"session_id\":\"s\",\"message_uuid\":\"u-other\",\"role\":\"user\",\"content\":\"x\",\"sequence_num\":1,\"created_at\":\"t\"}");
        uint8_t to[1][32];
        memcpy((to)[0], kata_pub, 32);
        age_encrypt(to, 1, (const uint8_t *)job.p, job.n, &ct);
        jb_puts(&env, "{\"v\":1,\"message_uuid\":\"u-liar\",\"ciphertext\":\"");
        jb_b64(&env, (const uint8_t *)ct.p, ct.n);
        jb_puts(&env, "\"}");
        fq_send(&b.f, INGEST, env.p);
        jb_free(&job); jb_free(&ct); jb_free(&env);
    }
    // not an envelope at all
    fq_send(&b.f, INGEST, "this is not JSON");
    // a NUL in the content, and a normal one
    uint8_t both[2][32];
    memcpy((both)[0], cal_pub, 32);
    memcpy((both)[1], kata_pub, 32);
    char *b2 = make_job(&b, "u-nul", "\"before\\u0000after\"", both, 2);
    worker_step(&w);
    CHECK(!strcmp(b.last_text, "before\\0after"), "NUL written as \\0 before tokenising: [%s]", b.last_text);
    worker_step(&w);
    CHECK(!strcmp(reason_of(&b, "u-s3"), "claim-check"), "claim check: %s", reason_of(&b, "u-s3"));
    CHECK(!strcmp(reason_of(&b, "u-cal"), "decrypt-failed"), "Cal-only: %s", reason_of(&b, "u-cal"));
    CHECK(!strcmp(reason_of(&b, "u-liar"), "decrypt-failed"), "liar: %s", reason_of(&b, "u-liar"));
    CHECK(!strcmp(reason_of(&b, "u-nul"), "(embedded)"), "NUL job embedded: %s", reason_of(&b, "u-nul"));
    CHECK(w.boot.invalid == 1 && fq_live(fq_queue(&b.f, INGEST)) == 1, "garbage is left on the queue (for the DLQ): %d live",
          fq_live(fq_queue(&b.f, INGEST)));
    // the claim-check result's envelope is the S3 pointer, verbatim
    fq_queue_t *rq = fq_queue(&b.f, RESULTS);
    bool found = false;
    for (int i = 0; i < rq->n; i++)
        found |= strstr(rq->msgs[i].body, "\"envelope\":{\"v\":1,\"message_uuid\":\"u-s3\",\"s3\":{\"bucket\":\"b\"") != NULL;
    CHECK(found, "claim-check envelope verbatim");
    kw_free(b1);
    kw_free(b2);
    board_free(&b, &w);

    // Second delivery: a pass of its own. Third: straight to Cal.
    board_init(&b, &w, NULL);
    char *x = make_job_text(&b, "u-again", "seen once before");
    char *y = make_job_text(&b, "u-new1", "fresh one");
    char *z = make_job_text(&b, "u-thrice", "seen twice before");
    char *v = make_job_text(&b, "u-new2", "another fresh one");
    fq_queue_t *in = fq_queue(&b.f, INGEST);
    in->msgs[0].receive_count = 1;            // the next receive is its second
    in->msgs[2].receive_count = 2;            // and this one's third
    worker_step(&w);
    CHECK(b.passes == 1 && b.last_n_seg == 1, "the second delivery goes alone: %d segments", b.last_n_seg);
    worker_step(&w);                          // hands it off, then the two fresh ones together
    CHECK(b.passes == 2 && b.last_n_seg == 2, "then the rest: %d segments", b.last_n_seg);
    worker_step(&w);
    CHECK(!strcmp(reason_of(&b, "u-thrice"), "retries"), "third delivery: %s", reason_of(&b, "u-thrice"));
    check_embedded(&b, "u-again", x, "seen once before");
    check_embedded(&b, "u-new1", y, "fresh one");
    check_embedded(&b, "u-new2", v, "another fresh one");
    CHECK(fq_live(in) == 0, "all deleted");
    kw_free(x); kw_free(y); kw_free(z); kw_free(v);
    board_free(&b, &w);
}

static void bad_passes(void)
{
    printf("-- a bad pass, then sick, then well again\n");
    board_t b;
    worker_t w;
    board_init(&b, &w, NULL);
    char *bodies[8];
    char uuid[8][16];
    for (int i = 0; i < 3; i++) { snprintf(uuid[i], 16, "u-%d", i); bodies[i] = make_job_text(&b, uuid[i], "some words here"); }
    b.bad_passes = 1;
    worker_step(&w);                          // a bad pass: no vectors
    worker_step(&w);
    for (int i = 0; i < 3; i++) CHECK(!strcmp(reason_of(&b, uuid[i]), "kata-unhealthy"), "%s: %s", uuid[i], reason_of(&b, uuid[i]));
    CHECK(w.bad_in_row == 1 && !w.sick, "one bad pass isn't sick");
    // two more bad passes in a row: sick
    b.bad_passes = 2;
    for (int i = 3; i < 5; i++) {
        snprintf(uuid[i], 16, "u-%d", i);
        bodies[i] = make_job_text(&b, uuid[i], "more words");
        worker_step(&w);
        worker_step(&w);
    }
    CHECK(w.sick && w.bad_in_row == 3, "three in a row: sick (%d)", w.bad_in_row);
    // sick: new jobs go straight to Cal, no pass
    int passes = b.passes;
    b.health_ok = 0;
    snprintf(uuid[5], 16, "u-5");
    bodies[5] = make_job_text(&b, uuid[5], "while sick");
    worker_step(&w);
    CHECK(b.passes == passes && !strcmp(reason_of(&b, "u-5"), "kata-unhealthy"), "sick: straight to Cal (%s)", reason_of(&b, "u-5"));
    jbuf_t s;
    jb_init(&s);
    worker_status_json(&w, &s);
    CHECK(strstr(s.p, "\"sick\":true") && strstr(s.p, "\"state\":\"sick\""), "status says sick: %s", s.p);
    jb_free(&s);
    // the health check: failing, then (half an hour later) passing
    b.f.now += WORKER_HEALTH_EVERY_S;
    worker_step(&w);
    CHECK(b.health_calls == 1 && w.sick, "failed health check: still sick");
    b.health_ok = 1;
    b.f.now += WORKER_HEALTH_EVERY_S;
    worker_step(&w);
    CHECK(b.health_calls == 2 && !w.sick, "health check passed: well");
    snprintf(uuid[6], 16, "u-6");
    bodies[6] = make_job_text(&b, uuid[6], "after recovery");
    worker_step(&w);
    worker_step(&w);
    check_embedded(&b, "u-6", bodies[6], "after recovery");
    CHECK(fq_live(fq_queue(&b.f, INGEST)) == 0, "all handed on");
    for (int i = 0; i < 7; i++) kw_free(bodies[i]);
    board_free(&b, &w);
}

static void strict(void)
{
    printf("-- envelopes and jobs Cal's serde would refuse, and deep nesting\n");
    board_t b;
    worker_t w;
    board_init(&b, &w, NULL);
    // envelopes: none of these is one Cal could read; all stay on the queue for its DLQ
    static const char *const BAD_ENV[] = {
        "{\"v\":1,\"message_uuid\":\"u-x1\",\"s3\":{\"bucket\":\"b\",\"key\":\"k\"}} trailing",
        "{\"v\":1,\"message_uuid\":\"u-x2\",\"message_uuid\":\"u-x3\",\"s3\":{\"bucket\":\"b\",\"key\":\"k\"}}",
        "{\"v\":\"1\",\"message_uuid\":\"u-x4\",\"s3\":{\"bucket\":\"b\",\"key\":\"k\"}}",
        "{\"v\":1.5,\"message_uuid\":\"u-x5\",\"s3\":{\"bucket\":\"b\",\"key\":\"k\"}}",
        "{\"v\":1,\"message_uuid\":\"u-x6\",\"s3\":{\"bucket\":\"b\"}}",
        "{\"v\":1,\"message_uuid\":\"u-x7\",\"ciphertext\":5}",
        "{\"v\":1,\"message_uuid\":\"u-x8\",\"sig\":[\"x\"],\"s3\":{\"bucket\":\"b\",\"key\":\"k\"}}",
        "[\"v\",1]",
    };
    int nbad = (int)(sizeof BAD_ENV / sizeof *BAD_ENV);
    for (int i = 0; i < nbad; i++) fq_send(&b.f, INGEST, BAD_ENV[i]);
    // 200 deep: refused before it's parsed (cJSON would recurse 200 times on a 16 KB stack)
    jbuf_t deep;
    jb_init(&deep);
    jb_puts(&deep, "{\"v\":1,\"message_uuid\":\"u-deep\",\"x\":");
    for (int i = 0; i < 200; i++) jb_putc(&deep, '[');
    for (int i = 0; i < 200; i++) jb_putc(&deep, ']');
    jb_puts(&deep, ",\"s3\":{\"bucket\":\"b\",\"key\":\"k\"}}");
    fq_send(&b.f, INGEST, deep.p);
    jb_free(&deep);
    // and the good ones: trailing whitespace, unknown fields and nulls in optional ones are fine
    fq_send(&b.f, INGEST, "{\"v\":1,\"message_uuid\":\"u-ok1\",\"s3\":{\"bucket\":\"b\",\"key\":\"k\"},\"sig\":null,\"future\":{}} \n");
    worker_step(&w);
    worker_step(&w);
    CHECK(w.boot.invalid == nbad + 1, "%d invalid, want %d", w.boot.invalid, nbad + 1);
    CHECK(results(&b) == 1 && !strcmp(reason_of(&b, "u-ok1"), "claim-check"), "only the good one handed on (%d)", results(&b));
    CHECK(fq_live(fq_queue(&b.f, INGEST)) == nbad + 1, "the bad ones stay for the DLQ");
    board_free(&b, &w);

    // jobs: a plaintext Cal's serde would refuse goes to Cal unembedded (Cal decides), not embedded
    board_init(&b, &w, NULL);
    static const char *const BAD_JOB[] = {
        "{\"session_id\":\"s\",\"message_uuid\":\"u-j%d\",\"role\":\"user\",\"content\":\"x\",\"sequence_num\":1,\"created_at\":\"t\"} junk",
        "{\"session_id\":\"s\",\"message_uuid\":\"u-j%d\",\"role\":\"user\",\"content\":\"x\",\"content\":\"y\",\"sequence_num\":1,\"created_at\":\"t\"}",
        "{\"session_id\":\"s\",\"message_uuid\":\"u-j%d\",\"role\":\"user\",\"content\":\"x\",\"sequence_num\":1.5,\"created_at\":\"t\"}",
        "{\"session_id\":\"s\",\"message_uuid\":\"u-j%d\",\"role\":\"user\",\"content\":\"x\",\"created_at\":\"t\"}",
        "{\"session_id\":\"s\",\"message_uuid\":\"u-j%d\",\"role\":\"user\",\"content\":\"x\",\"sequence_num\":1,\"created_at\":\"t\",\"host\":null}",
        "{\"session_id\":\"s\",\"message_uuid\":\"u-j%d\",\"role\":\"user\",\"content\":\"x\",\"sequence_num\":1,\"created_at\":\"t\",\"x\":[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]}",
    };
    int njob = (int)(sizeof BAD_JOB / sizeof *BAD_JOB);
    for (int i = 0; i < njob; i++) {
        char job[512], uuid[16], env_uuid[16];
        snprintf(job, sizeof job, BAD_JOB[i], i);
        snprintf(uuid, sizeof uuid, "u-j%d", i);
        snprintf(env_uuid, sizeof env_uuid, "%s", uuid);
        jbuf_t ct, env;
        jb_init(&ct); jb_init(&env);
        uint8_t to[1][32];
        memcpy(to[0], kata_pub, 32);
        age_encrypt(to, 1, (const uint8_t *)job, strlen(job), &ct);
        jb_printf(&env, "{\"v\":1,\"message_uuid\":\"%s\",\"ciphertext\":\"", env_uuid);
        jb_b64(&env, (const uint8_t *)ct.p, ct.n);
        jb_puts(&env, "\"}");
        fq_send(&b.f, INGEST, env.p);
        jb_free(&ct); jb_free(&env);
    }
    worker_step(&w);
    worker_step(&w);
    CHECK(b.passes == 0, "nothing embedded");
    for (int i = 0; i < njob; i++) {
        char uuid[16];
        snprintf(uuid, sizeof uuid, "u-j%d", i);
        CHECK(!strcmp(reason_of(&b, uuid), "decrypt-failed"), "job %d: %s", i, reason_of(&b, uuid));
    }
    board_free(&b, &w);
}

static void extension(void)
{
    printf("-- a visibility extension that fails stops the pass\n");
    board_t b;
    worker_t w;
    board_init(&b, &w, NULL);
    char *q0 = make_job_text(&b, "u-e0", "redelivered");
    char *q1 = make_job_text(&b, "u-e1", "one");
    char *q2 = make_job_text(&b, "u-e2", "two");
    fq_queue(&b.f, INGEST)->msgs[0].receive_count = 1;
    worker_step(&w);                          // q0 alone; q1, q2 pending
    CHECK(b.passes == 1 && b.last_n_seg == 1, "solo");
    b.f.now += 400;                           // q1 and q2 need extending before the next pass
    b.f.fail_target = "AmazonSQS.ChangeMessageVisibilityBatch";
    b.f.fail_target_next = 1;
    int wait = worker_step(&w);
    CHECK(b.passes == 1 && wait > 0, "no pass after a failed extension (wait %d)", wait);
    worker_step(&w);
    CHECK(b.passes == 2 && b.last_n_seg == 2, "then the pass: %d segments", b.last_n_seg);
    worker_step(&w);
    CHECK(fq_live(fq_queue(&b.f, INGEST)) == 0 && results(&b) == 3, "all through");
    kw_free(q0); kw_free(q1); kw_free(q2);
    board_free(&b, &w);

    // jobs whose visibility lapsed (their handles are stale): refused, so they leave the pool
    board_init(&b, &w, NULL);
    q0 = make_job_text(&b, "u-s0", "redelivered");
    q1 = make_job_text(&b, "u-s1", "one");
    fq_queue(&b.f, INGEST)->msgs[0].receive_count = 1;
    worker_step(&w);                          // q0 alone; q1 pending
    b.f.now += 2000;                          // q1's visibility has lapsed
    // The top-up receives q1 again (its second delivery: a pass of its own); the stale copy's
    // extension is refused and it leaves the pool, so q1 is embedded once, not twice.
    worker_step(&w);
    CHECK(b.passes == 2 && b.last_n_seg == 1 && w.n == 1, "the fresh copy alone, the stale one gone: %d passes, %d held",
          b.passes, w.n);
    for (int i = 0; i < 3; i++) worker_step(&w);
    CHECK(fq_live(fq_queue(&b.f, INGEST)) == 0 && results(&b) == 2 && !strcmp(reason_of(&b, "u-s1"), "(embedded)"),
          "through once: %d results", results(&b));
    kw_free(q0); kw_free(q1);
    board_free(&b, &w);
}

// ---- out of memory: every Nth allocation fails, in the worker's code (not the fake SQS's) ------------
static int oom_every, oom_count;
static bool oom_on;
static void *oom_alloc(size_t n) { return oom_on && oom_every && ++oom_count % oom_every == 0 ? NULL : malloc(n); }
static void *oom_realloc(void *p, size_t n) { return oom_on && oom_every && ++oom_count % oom_every == 0 ? NULL : realloc(p, n); }
static int oom_post(void *ctx, const http_req_t *r, jbuf_t *resp)
{
    bool was = oom_on;
    oom_on = false;                           // the fake's own allocations (and the response buffer) don't fail...
    int st = fq_post(ctx, r, resp);
    oom_on = was;
    return st;
}

static void out_of_memory(void)
{
    printf("-- out of memory everywhere: nothing lost, nothing leaked\n");
    static const kw_mem_t failing = { oom_alloc, oom_realloc, free };
    static const kw_mem_t plain = { malloc, realloc, free };
    for (int every = 3; every <= 41; every += 2) {
        board_t b;
        worker_t w;
        board_init(&b, &w, NULL);
        b.sqs.post = oom_post;
        static char uuid_store[20][16];
        const char *uuids[20];
        char *bodies[20];
        for (int i = 0; i < 20; i++) {
            snprintf(uuid_store[i], 16, "u-%02d", i);
            uuids[i] = uuid_store[i];
            char words[48];
            snprintf(words, sizeof words, "job %d with words", i);
            bodies[i] = make_job_text(&b, uuids[i], words);
        }
        kw_set_mem(&failing);
        oom_every = every;
        oom_count = 0;
        oom_on = true;
        for (int s = 0; s < 120; s++) {
            b.f.now += worker_step(&w);
            oom_on = false;
            invariant(&b, uuids, 20, "out of memory");
            oom_on = true;
            if (s == 80) oom_every = 0;       // then memory comes back
        }
        oom_on = false;
        kw_set_mem(&plain);
        int have = 0;
        for (int i = 0; i < 20; i++) {
            cJSON *r = result_for(&b, uuids[i]);
            have += r != NULL;
            cJSON_Delete(r);
        }
        CHECK(have == 20 && fq_live(fq_queue(&b.f, INGEST)) == 0, "every %d: %d of 20 through, %d left", every, have,
              fq_live(fq_queue(&b.f, INGEST)));
        if (every == 3 || every == 41) printf("    every %d allocations failing: %u failures so far, all 20 jobs through\n", every, kw_alloc_failures());
        for (int i = 0; i < 20; i++) kw_free(bodies[i]);
        board_free(&b, &w);
    }
}

static void sick_from_boot(void)
{
    printf("-- a board whose boot self-test failed starts sick\n");
    board_t b;
    worker_t w;
    board_init(&b, &w, NULL);
    worker_set_sick(&w, "the boot self-test failed");
    char *j = make_job_text(&b, "u-boot", "while sick from boot");
    worker_step(&w);
    CHECK(b.passes == 0 && !strcmp(reason_of(&b, "u-boot"), "kata-unhealthy") && fq_live(fq_queue(&b.f, INGEST)) == 0,
          "straight to Cal, no pass: %s", reason_of(&b, "u-boot"));
    b.f.now += WORKER_HEALTH_EVERY_S;
    worker_step(&w);
    CHECK(b.health_calls == 1 && !w.sick, "well after a health check");
    kw_free(j);
    board_free(&b, &w);
}

static void faults(void)
{
    printf("-- the results queue refusing, the network failing, an update\n");
    board_t b;
    worker_t w;
    board_init(&b, &w, NULL);
    const char *uuids[4] = { "u-a", "u-b", "u-c", "u-d" };
    char *bodies[4];
    for (int i = 0; i < 3; i++) bodies[i] = make_job_text(&b, uuids[i], "words for a job");
    worker_step(&w);                          // pass done
    // the results queue refuses one entry: it isn't deleted, the others are
    b.f.refuse_body_containing = "\"u-b\"";
    int wait = worker_step(&w);
    CHECK(results(&b) == 2 && fq_live(fq_queue(&b.f, INGEST)) == 1, "two through, one kept: %d %d", results(&b),
          fq_live(fq_queue(&b.f, INGEST)));
    CHECK(wait >= WORKER_RETRY_FIRST_S, "backs off: %d", wait);
    invariant(&b, uuids, 3, "after a refused send");
    // no new pass while a result is undelivered, however long it takes
    bodies[3] = make_job_text(&b, uuids[3], "waiting behind");
    int passes = b.passes;
    for (int i = 0; i < 4; i++) {
        b.f.now += worker_step(&w);
        invariant(&b, uuids, 4, "while refused");
    }
    CHECK(b.passes == passes, "no pass while results are undelivered");
    CHECK(b.f.calls[3] > 0, "visibility extended meanwhile");
    // the refused job's ingest message stays hidden: nobody else gets it
    CHECK(fq_visible(&b.f, fq_queue(&b.f, INGEST)) == 1, "only the new job is visible: %d", fq_visible(&b.f, fq_queue(&b.f, INGEST)));
    b.f.refuse_body_containing = NULL;
    worker_step(&w);
    worker_step(&w);
    worker_step(&w);
    CHECK(fq_live(fq_queue(&b.f, INGEST)) == 0, "all through once the results queue takes them: %d left", fq_live(fq_queue(&b.f, INGEST)));
    check_embedded(&b, "u-b", bodies[1], "words for a job");
    check_embedded(&b, "u-d", bodies[3], "waiting behind");
    // the network failing: back off, doubling
    b.net_fail = 3;
    int w1 = worker_step(&w), w2 = worker_step(&w), w3 = worker_step(&w);
    CHECK(w1 == 30 && w2 == 60 && w3 == 120, "back-off %d %d %d", w1, w2, w3);
    CHECK(worker_step(&w) == WORKER_IDLE_S && w.retry_s == 0, "and back");
    // The delete failing after the send worked: the result is through; the job is deleted next
    // phase, not sent twice, and no pass runs meanwhile.
    char *e = make_job_text(&b, "u-e", "delete trouble");
    worker_step(&w);
    int before = results(&b);
    b.f.fail_target = "AmazonSQS.DeleteMessageBatch";
    b.f.fail_target_next = 1;
    worker_step(&w);
    CHECK(results(&b) == before + 1 && fq_live(fq_queue(&b.f, INGEST)) == 1, "sent, not deleted: %d %d", results(&b) - before,
          fq_live(fq_queue(&b.f, INGEST)));
    worker_step(&w);
    CHECK(results(&b) == before + 1 && fq_live(fq_queue(&b.f, INGEST)) == 0, "deleted next time, sent once: %d %d",
          results(&b) - before, fq_live(fq_queue(&b.f, INGEST)));
    kw_free(e);
    for (int i = 0; i < 4; i++) kw_free(bodies[i]);
    board_free(&b, &w);

    // An update: what's done is handed off, what isn't goes back to the queue, then parked
    board_init(&b, &w, NULL);
    char *p1 = make_job_text(&b, "u-p1", "done before the update");
    worker_step(&w);                          // embedded, not yet handed off
    char *p2 = make_job_text(&b, "u-p2", "arrived just before");
    worker_request_park(&w);                  // the next step hands p1 off and takes nothing new
    wait = worker_step(&w);
    CHECK(worker_is_parked(&w) && w.n == 0, "parked, holding nothing (%d)", w.n);
    check_embedded(&b, "u-p1", p1, "done before the update");
    CHECK(fq_visible(&b.f, fq_queue(&b.f, INGEST)) == 1, "p2 still on the queue for someone");
    passes = b.passes;
    CHECK(worker_step(&w) > 0 && b.passes == passes && b.f.calls[0] == 2, "parked: nothing happens");
    worker_resume(&w);
    worker_step(&w);
    worker_step(&w);
    check_embedded(&b, "u-p2", p2, "arrived just before");
    kw_free(p1); kw_free(p2);
    board_free(&b, &w);

    // Parking with jobs in the pool: a second delivery goes alone, so two fresh jobs are still
    // pending when the update asks. They go back to the queue (visibility 0), not kept.
    board_init(&b, &w, NULL);
    char *q0 = make_job_text(&b, "u-q0", "redelivered");
    char *q1 = make_job_text(&b, "u-q1", "one");
    char *q2 = make_job_text(&b, "u-q2", "two");
    fq_queue(&b.f, INGEST)->msgs[0].receive_count = 1;
    worker_step(&w);                          // receives all three, embeds q0 alone
    CHECK(b.last_n_seg == 1 && w.pool_tokens > 0, "two still pending");
    worker_request_park(&w);
    worker_step(&w);
    CHECK(worker_is_parked(&w) && w.n == 0, "parked holding nothing");
    CHECK(fq_visible(&b.f, fq_queue(&b.f, INGEST)) == 2 && results(&b) == 1, "q1, q2 back on the queue at once; q0 handed off");
    check_embedded(&b, "u-q0", q0, "redelivered");
    kw_free(q0); kw_free(q1); kw_free(q2);
    board_free(&b, &w);
}

static void big_results(void)
{
    printf("-- bodies over Kata's limit go to Cal as too-long, and near SQS's limit one to a batch\n");
    board_t b;
    worker_t w;
    board_init(&b, &w, NULL);
    // ~150 KB of content: a ~200 KB body; only the first 800 characters are embedded
    char *content = malloc(150 * 1024 + 3);
    content[0] = '"';
    for (int i = 1; i <= 150 * 1024; i++) content[i] = i % 6 ? 'x' : ' ';
    content[150 * 1024 + 1] = '"';
    content[150 * 1024 + 2] = 0;
    uint8_t to[1][32];
    memcpy((to)[0], kata_pub, 32);
    char *b1 = make_job(&b, "u-big1", content, to, 1);
    char *b2 = make_job(&b, "u-big2", content, to, 1);
    char *b3 = make_job_text(&b, "u-small", "small one");
    // ~24 KB of content: a body just under WORKER_WORST_BODY, embedded from its first 800 characters
    content[24 * 1024 + 1] = '"';
    content[24 * 1024 + 2] = 0;
    char *b4 = make_job(&b, "u-mid", content, to, 1);
    printf("    %zu- and %zu-byte ingest bodies\n", strlen(b1), strlen(b4));
    CHECK(strlen(b1) > WORKER_WORST_BODY && strlen(b4) < WORKER_WORST_BODY, "body sizes");
    for (int i = 0; i < 4; i++) worker_step(&w);
    CHECK(results(&b) == 4 && fq_live(fq_queue(&b.f, INGEST)) == 0, "all four through: %d", results(&b));
    CHECK(b.f.max_batch_seen <= SQS_MAX_BATCH_BYTES, "a batch of %lld bytes", (long long)b.f.max_batch_seen);
    CHECK(!strcmp(reason_of(&b, "u-big1"), REASON_TOO_LONG) && !strcmp(reason_of(&b, "u-big2"), REASON_TOO_LONG), "too-long");
    cJSON *r = result_for(&b, "u-mid");
    CHECK(r && !strcmp(cJSON_GetStringValue(cJSON_GetObjectItem(r, "kind")), "embedded"), "u-mid embedded");
    CHECK(r && cJSON_IsTrue(cJSON_GetObjectItem(r, "truncated_chars")), "truncated_chars");
    cJSON_Delete(r);
    CHECK(b.f.bad_signatures == 0, "signatures");
    free(content);
    kw_free(b1); kw_free(b2); kw_free(b3); kw_free(b4);
    board_free(&b, &w);
}

static void soak(void)
{
    printf("-- 300 jobs with faults sprinkled in: nothing lost, nothing deleted early\n");
    board_t b;
    worker_t w;
    board_init(&b, &w, NULL);
    static char uuid_store[300][16];
    const char *uuids[300];
    char *bodies[300];
    int sent = 0;
    for (int round = 0; round < 400 && (sent < 300 || fq_live(fq_queue(&b.f, INGEST))); round++) {
        for (int k = 0; k < (round % 3) && sent < 300; k++, sent++) {
            snprintf(uuid_store[sent], 16, "u-%03d", sent);
            uuids[sent] = uuid_store[sent];
            char words[64];
            snprintf(words, sizeof words, "job %d says %d things", sent, sent * 7 % 13);
            bodies[sent] = make_job_text(&b, uuids[sent], words);
        }
        if (round % 17 == 5) b.f.fail_next = 1;
        if (round % 23 == 7) b.f.http_500_next = 1;
        if (round % 29 == 11) b.net_fail = 1;
        if (round % 31 == 13) b.bad_passes = 1;
        int wait = worker_step(&w);
        b.f.now += wait;
        invariant(&b, uuids, sent, "soak");
    }
    CHECK(sent == 300 && fq_live(fq_queue(&b.f, INGEST)) == 0, "drained: %d sent, %d left", sent, fq_live(fq_queue(&b.f, INGEST)));
    int have = 0;
    for (int i = 0; i < sent; i++) {
        cJSON *r = result_for(&b, uuids[i]);
        have += r != NULL;
        cJSON_Delete(r);
    }
    CHECK(have == sent, "every job has a result: %d of %d", have, sent);
    printf("    %d passes (largest %d messages), %d results (duplicates allowed), %d embedded, %d bad passes\n", b.passes,
           b.max_n_seg, results(&b), w.boot.messages, w.boot.bad_passes);
    CHECK(b.f.bad_signatures == 0, "signatures");
    for (int i = 0; i < sent; i++) kw_free(bodies[i]);
    board_free(&b, &w);
}

static int test_rng(void *ctx, unsigned char *out, size_t n) { return enc_rng(ctx, out, n); }

int main(void)
{
    age_set_rng(test_rng, NULL);
    // the TEST-ONLY identities from track C's fixtures, and a stranger
    char *text = read_file_or_die("../../host/convmem-wire/test-identity-kata.txt", NULL);
    char *line = strstr(text, "AGE-SECRET-KEY-1");
    line[74] = 0;
    CHECK(age_identity_parse(line, &kata_id) == 0, "kata identity");
    memcpy(kata_pub, kata_id.public, 32);
    free(text);
    text = read_file_or_die("../../host/convmem-wire/test-identity-cal.txt", NULL);
    line = strstr(text, "AGE-SECRET-KEY-1");
    line[74] = 0;
    age_identity_t cal;
    CHECK(age_identity_parse(line, &cal) == 0, "cal identity");
    memcpy(cal_pub, cal.public, 32);
    free(text);
    (void)stranger_pub;

    // as the firmware does it: cJSON allocates through kw (PSRAM on the board)
    static cJSON_Hooks hooks = { kw_alloc, kw_free };
    cJSON_InitHooks(&hooks);
    happy_path();
    sorting();
    strict();
    extension();
    bad_passes();
    sick_from_boot();
    faults();
    big_results();
    soak();
    out_of_memory();
    return report("worker");
}
