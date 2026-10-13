// Kata's worker: the pool, the network phase and the pass (worker.h).
#include "worker.h"

#include <math.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "batch.h"
#include "cJSON.h"
#include "prep.h"

static const char *const REASONS[5] = { REASON_CLAIM_CHECK, REASON_DECRYPT_FAILED, REASON_RETRIES, REASON_TOO_LONG,
                                        REASON_KATA_UNHEALTHY };

static void wlog(worker_t *w, const char *fmt, ...) __attribute__((format(printf, 2, 3)));
static void wlog(worker_t *w, const char *fmt, ...)
{
    char msg[256];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(msg, sizeof msg, fmt, ap);
    va_end(ap);
    if (w->io.log) w->io.log(w->io.ctx, msg);
}

static int64_t now(worker_t *w) { return w->io.now(w->io.ctx); }

// ---- counters ------------------------------------------------------------------------------
static void roll_today(worker_t *w)
{
    char ymd[11];
    kw_ymd(now(w), ymd);
    if (strcmp(ymd, w->today_ymd)) {
        memset(&w->today, 0, sizeof w->today);
        memcpy(w->today_ymd, ymd, sizeof ymd);
    }
}

#define COUNT(w, field, k) do { (w)->boot.field += (k); (w)->today.field += (k); } while (0)

static void count_reason(worker_t *w, const char *reason)
{
    for (int i = 0; i < 5; i++)
        if (!strcmp(reason, REASONS[i])) { COUNT(w, unembedded[i], 1); return; }
}

// ---- the pool ------------------------------------------------------------------------------
int worker_init(worker_t *w, const worker_cfg_t *cfg, const worker_io_t *io)
{
    memset(w, 0, sizeof *w);
    w->cfg = *cfg;
    w->io = *io;
    if (w->cfg.pool_max < 1) w->cfg.pool_max = 1;
    w->pool = kw_alloc((size_t)w->cfg.pool_max * sizeof *w->pool);
    if (!w->pool) return -1;
    w->state = "idle";
    return 0;
}

static void job_free(job_t *j)
{
    sqs_msg_free(&j->m);
    kw_free(j->ids);
    kw_free(j->result);
    memset(j, 0, sizeof *j);
}

void worker_free(worker_t *w)
{
    for (int i = 0; i < w->n; i++) job_free(&w->pool[i]);
    kw_free(w->pool);
    w->pool = NULL;
    w->n = 0;
}

static void remove_job(worker_t *w, int i)
{
    if (w->pool[i].state == JOB_PENDING) w->pool_tokens -= w->pool[i].n_ids;
    job_free(&w->pool[i]);
    memmove(&w->pool[i], &w->pool[i + 1], (size_t)(w->n - i - 1) * sizeof *w->pool);
    w->n--;
}

static size_t pool_bytes(const worker_t *w)
{
    size_t b = 0;
    for (int i = 0; i < w->n; i++) b += w->pool[i].m.body_len + w->pool[i].result_len + (size_t)w->pool[i].n_ids * 4;
    return b;
}

static int count_state(const worker_t *w, job_state_t s)
{
    int k = 0;
    for (int i = 0; i < w->n; i++) k += w->pool[i].state == s;
    return k;
}

// The job's result, built: its body (and ids) are no longer needed once it's in the result.
static int finish(worker_t *w, job_t *j, const float *vec, const kata_result_t *pass)
{
    kata_result_t r = { 0 };
    if (pass) r = *pass;
    r.embedded = vec != NULL;
    r.message_uuid = j->uuid;
    r.envelope = j->m.body;
    r.envelope_len = j->m.body_len;
    r.embedder = w->cfg.embedder;
    r.vector = vec;
    r.tokens = j->n_ids;
    r.truncated_chars = j->truncated;
    r.reason = vec ? NULL : j->reason;
    r.board = w->cfg.board;
    r.fw = w->cfg.fw;
    r.message_id = j->m.message_id;
    r.receive_count = j->m.receive_count;
    jbuf_t b;
    jb_init(&b);
    if (result_encode(&r, &b)) { jb_free(&b); return -1; }
    if (j->state == JOB_PENDING) w->pool_tokens -= j->n_ids;
    j->result = jb_take(&b, &j->result_len);
    kw_free(j->m.body);
    j->m.body = NULL;
    j->m.body_len = 0;
    kw_free(j->ids);
    j->ids = NULL;
    j->state = JOB_RESULT;
    return 0;
}

static void unembedded(worker_t *w, job_t *j, const char *reason)
{
    j->reason = reason;
    if (finish(w, j, NULL, NULL)) {
        // out of memory: leave it pending without ids; it'll be redelivered after its visibility
        wlog(w, "worker: %s: out of memory building its result", j->uuid);
        return;
    }
    count_reason(w, reason);
    wlog(w, "worker: %s -> Cal unembedded (%s, receive %d)", j->uuid, reason, j->m.receive_count);
}

// ---- arrival: what to do with each job --------------------------------------------------------
typedef enum { KEEP, DROP } verdict_t;

// The plaintext job (JSON): its content, prepared and tokenised into j->ids. -1 if it isn't a job
// for this envelope.
static int open_job(worker_t *w, job_t *j, const char *json, size_t len)
{
    jbuf_t clean;
    jb_init(&clean);
    int nuls = prep_json_nuls(json, len, &clean);
    cJSON *job = nuls >= 0 ? cJSON_ParseWithLength(clean.p, clean.n) : NULL;
    if (clean.p) memset(clean.p, 0, clean.n);
    jb_free(&clean);
    const char *uuid = cJSON_GetStringValue(cJSON_GetObjectItem(job, "message_uuid"));
    const char *role = cJSON_GetStringValue(cJSON_GetObjectItem(job, "role"));
    const char *content = cJSON_GetStringValue(cJSON_GetObjectItem(job, "content"));
    int rc = -1;
    if (!uuid || strcmp(uuid, j->uuid)) wlog(w, "worker: %s: the job's uuid disagrees with the envelope's", j->uuid);
    else if (!role || (strcmp(role, "user") && strcmp(role, "assistant"))) wlog(w, "worker: %s: unexpected role", j->uuid);
    else if (!content) wlog(w, "worker: %s: no content", j->uuid);
    else {
        if (nuls > 0) wlog(w, "worker: %s: %d NULs written as \\0, as Cal does", j->uuid, nuls);
        size_t n = prep_truncate(content, strlen(content), PREP_MAX_CHARS, &j->truncated);
        j->ids = kw_alloc(PREP_MAX_TOKENS * sizeof *j->ids);
        int nt = j->ids ? w->io.tokenise(w->io.ctx, content, n, j->ids, PREP_MAX_TOKENS) : -1;
        if (nt < 2) {
            kw_free(j->ids);
            j->ids = NULL;
            rc = -2;                          // the tokeniser failed (out of memory): not the job's fault
        } else {
            j->n_ids = nt;
            rc = 0;
        }
    }
    cJSON_Delete(job);
    return rc;
}

static verdict_t arrive(worker_t *w, job_t *j)
{
    j->received_at = now(w);
    j->visible_until = j->received_at + WORKER_VISIBILITY_S;
    // The envelope is opaque except for these three; it's handed on byte for byte.
    cJSON *env = cJSON_ParseWithLength(j->m.body, j->m.body_len);
    const char *uuid = cJSON_GetStringValue(cJSON_GetObjectItem(env, "message_uuid"));
    if (!cJSON_IsObject(env) || !uuid || !*uuid || strlen(uuid) >= sizeof j->uuid) {
        // Not an envelope: no result could carry it. It goes to the ingest DLQ after maxReceiveCount.
        COUNT(w, invalid, 1);
        wlog(w, "worker: message %s isn't an envelope: left for the dead-letter queue", j->m.message_id);
        cJSON_Delete(env);
        return DROP;
    }
    snprintf(j->uuid, sizeof j->uuid, "%s", uuid);
    const char *ct = cJSON_GetStringValue(cJSON_GetObjectItem(env, "ciphertext"));
    bool s3 = cJSON_IsObject(cJSON_GetObjectItem(env, "s3"));
    const char *reason = NULL;
    if (!ct && s3) reason = REASON_CLAIM_CHECK;
    else if (j->m.receive_count >= WORKER_RETRIES_RECEIVE) reason = REASON_RETRIES;
    else if (w->sick) reason = REASON_KATA_UNHEALTHY;
    else if (!ct || !w->cfg.identity) reason = REASON_DECRYPT_FAILED;
    else {
        size_t cl = strlen(ct);
        uint8_t *bin = kw_alloc(cl / 4 * 3 + 3), *pt = NULL;
        long bl = bin ? b64_decode(ct, cl, bin, B64_PADDED) : -1;
        size_t pl = 0;
        age_err_t e = bl < 0 ? AGE_ERR_HEADER : age_decrypt(w->cfg.identity, bin, (size_t)bl, &pt, &pl, NULL);
        kw_free(bin);
        if (!bin) reason = REASON_KATA_UNHEALTHY;   // out of memory: let Cal have it
        else if (e) {
            wlog(w, "worker: %s: %s", j->uuid, bl < 0 ? "ciphertext isn't base64" : age_strerror(e));
            reason = e == AGE_ERR_NOMEM ? REASON_KATA_UNHEALTHY : REASON_DECRYPT_FAILED;
        } else {
            int rc = open_job(w, j, (const char *)pt, pl);
            memset(pt, 0, pl);
            kw_free(pt);
            if (rc == -2) reason = REASON_KATA_UNHEALTHY;
            else if (rc) reason = REASON_DECRYPT_FAILED;
        }
    }
    cJSON_Delete(env);
    if (reason) {
        unembedded(w, j, reason);
        return KEEP;
    }
    j->solo = j->m.receive_count == WORKER_SOLO_RECEIVE;
    j->state = JOB_PENDING;
    w->pool_tokens += j->n_ids;
    return KEEP;
}

// ---- the network phase --------------------------------------------------------------------
// Results to Cal, then the ingest jobs SQS accepted them for deleted.
static void handoff(worker_t *w)
{
    sqs_t *s = w->io.sqs;
    // A result SQS can never take (only if the forwarder broke the 240,000-byte inline ceiling):
    // dropped, so it can't block the pool; the ingest job is left to reach the DLQ.
    for (int i = 0; i < w->n; i++)
        if (w->pool[i].state == JOB_RESULT && w->pool[i].result_len > SQS_MAX_BATCH_BYTES) {
            wlog(w, "worker: %s: a %zu-byte result is too big for SQS: left for the dead-letter queue", w->pool[i].uuid,
                 w->pool[i].result_len);
            remove_job(w, i--);
        }
    bool *tried = kw_alloc((size_t)(w->n ? w->n : 1) * sizeof *tried);
    if (!tried) return;
    memset(tried, 0, (size_t)(w->n ? w->n : 1) * sizeof *tried);
    int sent = 0;
    for (;;) {
        int idx[SQS_MAX_BATCH], k = 0;
        size_t total = 0;
        const char *bodies[SQS_MAX_BATCH];
        size_t lens[SQS_MAX_BATCH];
        for (int i = 0; i < w->n && k < SQS_MAX_BATCH; i++) {
            job_t *j = &w->pool[i];
            if (j->state != JOB_RESULT || tried[i] || total + j->result_len > SQS_MAX_BATCH_BYTES) continue;
            idx[k] = i; bodies[k] = j->result; lens[k] = j->result_len; total += j->result_len; k++;
        }
        if (!k) break;
        bool ok[SQS_MAX_BATCH];
        int rc = sqs_send_batch(s, w->cfg.results_url, bodies, lens, k, ok);
        for (int b = 0; b < k; b++) {
            tried[idx[b]] = true;
            if (ok[b]) { w->pool[idx[b]].state = JOB_DELETE; sent++; }
            else COUNT(w, send_failures, 1);
        }
        if (rc) {
            wlog(w, "worker: hand-off failed: %s", s->last_error);
            snprintf(w->last_error, sizeof w->last_error, "%s", s->last_error);
            break;                            // the network: the rest wait for the next phase
        }
    }
    kw_free(tried);
    if (sent) w->last_handoff_at = now(w);

    // Deletes, ten at a time. A refused entry is a stale receipt handle (the job was redelivered):
    // nothing to retry, and the redelivery costs a duplicate embedding and an idempotent upsert. A
    // call that fails is tried again next phase, three times in all.
    bool *att = kw_alloc((size_t)(w->n ? w->n : 1) * sizeof *att);
    if (!att) return;
    memset(att, 0, (size_t)(w->n ? w->n : 1) * sizeof *att);
    for (;;) {
        int idx[SQS_MAX_BATCH], k = 0;
        const char *rh[SQS_MAX_BATCH];
        for (int i = 0; i < w->n && k < SQS_MAX_BATCH; i++)
            if (w->pool[i].state == JOB_DELETE && !att[i]) { idx[k] = i; rh[k++] = w->pool[i].m.receipt; }
        if (!k) break;
        bool ok[SQS_MAX_BATCH];
        int rc = sqs_delete_batch(s, w->cfg.ingest_url, rh, k, ok);
        for (int b = 0; b < k; b++) {
            att[idx[b]] = true;
            job_t *j = &w->pool[idx[b]];
            if (rc) { j->delete_tries++; continue; }
            if (!ok[b]) { COUNT(w, delete_failures, 1); wlog(w, "worker: %s: delete refused (stale handle)", j->uuid); }
            j->delete_tries = -1;             // done with
        }
        if (rc) {
            COUNT(w, delete_failures, k);
            wlog(w, "worker: delete failed: %s", s->last_error);
            break;
        }
    }
    kw_free(att);
    for (int i = w->n - 1; i >= 0; i--)
        if (w->pool[i].state == JOB_DELETE && (w->pool[i].delete_tries < 0 || w->pool[i].delete_tries >= 3)) remove_job(w, i);
}

// all_pending: every pending job (to release them, for an update); otherwise every job still held
// that could be visible again before the next phase.
static void visibility(worker_t *w, bool all_pending, int seconds)
{
    int64_t t = now(w);
    bool *att = kw_alloc((size_t)(w->n ? w->n : 1) * sizeof *att);
    if (!att) return;
    memset(att, 0, (size_t)(w->n ? w->n : 1) * sizeof *att);
    for (;;) {
        int idx[SQS_MAX_BATCH], k = 0;
        const char *rh[SQS_MAX_BATCH];
        for (int i = 0; i < w->n && k < SQS_MAX_BATCH; i++) {
            job_t *j = &w->pool[i];
            bool want = all_pending ? j->state == JOB_PENDING
                                    : j->visible_until < t + WORKER_EXTEND_HORIZON_S;
            if (want && !att[i]) { idx[k] = i; rh[k++] = j->m.receipt; }
        }
        if (!k) break;
        bool ok[SQS_MAX_BATCH];
        int rc = sqs_visibility_batch(w->io.sqs, w->cfg.ingest_url, rh, k, seconds, ok);
        for (int b = 0; b < k; b++) {
            job_t *j = &w->pool[idx[b]];
            att[idx[b]] = true;
            if (!rc && ok[b]) j->visible_until = t + seconds;
            else if (!rc) wlog(w, "worker: %s: visibility refused (stale handle: it may come back to someone)", j->uuid);
        }
        if (rc) { wlog(w, "worker: visibility failed: %s", w->io.sqs->last_error); break; }
    }
    kw_free(att);
}

// Returns how many messages arrived.
static int top_up(worker_t *w)
{
    int arrived = 0;
    for (int r = 0; r < WORKER_MAX_RECEIVE; r++) {
        if (w->pool_tokens >= w->cfg.target_tokens) break;
        size_t used = pool_bytes(w);
        size_t room = used < w->cfg.pool_budget ? (w->cfg.pool_budget - used) / WORKER_WORST_BODY : 0;
        int max = (int)(room < 10 ? room : 10);
        if (max > w->cfg.pool_max - w->n) max = w->cfg.pool_max - w->n;
        if (max < 1) break;
        sqs_msg_t *msgs;
        int got;
        if (sqs_receive(w->io.sqs, w->cfg.ingest_url, max, w->n ? 0 : WORKER_LONG_POLL_S, WORKER_VISIBILITY_S, &msgs, &got)) {
            wlog(w, "worker: receive failed: %s", w->io.sqs->last_error);
            snprintf(w->last_error, sizeof w->last_error, "%s", w->io.sqs->last_error);
            break;
        }
        for (int i = 0; i < got; i++) {
            job_t *j = &w->pool[w->n++];
            memset(j, 0, sizeof *j);
            j->m = msgs[i];
            arrived++;
            // dropped: not an envelope, or out of memory part-way (it's redelivered after its visibility)
            if (arrive(w, j) == DROP || (j->state == JOB_PENDING && !j->ids)) remove_job(w, w->n - 1);
        }
        kw_free(msgs);
        if (!got) break;
    }
    return arrived;
}

static int backoff(worker_t *w)
{
    w->retry_s = w->retry_s ? (w->retry_s * 2 > WORKER_RETRY_MAX_S ? WORKER_RETRY_MAX_S : w->retry_s * 2) : WORKER_RETRY_FIRST_S;
    return w->retry_s;
}

// ---- the pass -----------------------------------------------------------------------------
// Finite, and a raw norm in Cal's band (results.rs: real mxbai [CLS] rows measure 15.1-19.1).
// Outside it Cal would refuse the vector into the results DLQ; as a bad pass, Cal embeds the jobs.
static bool vector_ok(const float *v)
{
    double ss = 0;
    for (int i = 0; i < RESULT_DIMS; i++) {
        if (!isfinite(v[i])) return false;
        ss += (double)v[i] * v[i];
    }
    return ss >= WORKER_NORM_MIN * WORKER_NORM_MIN && ss <= WORKER_NORM_MAX * WORKER_NORM_MAX;
}

static void go_sick(worker_t *w)
{
    w->sick = true;
    w->sick_since = now(w);
    w->next_health = w->sick_since + WORKER_HEALTH_EVERY_S;
    wlog(w, "worker: %d bad passes in a row: SICK. Every job goes to Cal until a health check passes", w->bad_in_row);
    for (int i = 0; i < w->n; i++)
        if (w->pool[i].state == JOB_PENDING) unembedded(w, &w->pool[i], REASON_KATA_UNHEALTHY);
}

static void pass(worker_t *w)
{
    int pend[64], lens[64], pick[64], n = 0;
    for (int i = 0; i < w->n && n < 64; i++)
        if (w->pool[i].state == JOB_PENDING && w->pool[i].ids) pend[n++] = i;
    if (!n) return;
    // The oldest always goes; a job on its second delivery goes alone (it mustn't take others down
    // with it if it's what crashed the last pass), and nothing joins it.
    int np;
    if (w->pool[pend[0]].solo) {
        np = 1;
        pick[0] = 0;
    } else {
        for (int k = 0; k < n; k++) lens[k] = w->pool[pend[k]].solo ? PREP_MAX_TOKENS + 1 : w->pool[pend[k]].n_ids;
        np = batch_plan(lens, n, PREP_MAX_TOKENS, w->cfg.target_tokens, pick);
    }
    int32_t *ids = kw_alloc(PREP_MAX_TOKENS * sizeof *ids);
    float *vecs = kw_alloc((size_t)np * RESULT_DIMS * sizeof *vecs);
    if (!ids || !vecs) { kw_free(ids); kw_free(vecs); wlog(w, "worker: out of memory for a pass"); return; }
    int seg[64], tokens = 0;
    for (int k = 0; k < np; k++) {
        job_t *j = &w->pool[pend[pick[k]]];
        memcpy(ids + tokens, j->ids, (size_t)j->n_ids * sizeof *ids);
        tokens += seg[k] = j->n_ids;
    }
    worker_pass_info_t info = { 0 };
    int rc = w->io.embed(w->io.ctx, ids, seg, np, vecs, &info);
    for (int k = 0; !rc && k < np; k++)
        if (!vector_ok(vecs + (size_t)k * RESULT_DIMS)) { rc = -1; info.why = "a vector isn't finite, or its norm is outside 10-30"; }
    w->last_pass.n_seg = np;
    w->last_pass.tokens = tokens;
    w->last_pass.ms = info.ms;
    w->last_pass.card_wait_ms = info.card_wait_ms;
    w->last_pass.at = now(w);
    w->card_wait[w->n_card++ % WORKER_CARD_HISTORY] = info.card_wait_ms;
    COUNT(w, passes, 1);
    if (!rc) {
        kata_result_t stats = { .has_pass = true, .pass_n_seg = np, .pass_tokens = tokens, .pass_ms = info.ms };
        for (int k = 0; k < np; k++) {
            job_t *j = &w->pool[pend[pick[k]]];
            if (finish(w, j, vecs + (size_t)k * RESULT_DIMS, &stats)) wlog(w, "worker: %s: out of memory for its result", j->uuid);
            else COUNT(w, messages, 1);
        }
        w->bad_in_row = 0;
        wlog(w, "worker: pass: %d messages, %d tokens, %.1f s (%.1f s on the card)", np, tokens, info.ms / 1e3,
             info.card_wait_ms / 1e3);
    } else {
        // No vector from a bad pass is delivered; its jobs go to Cal now.
        COUNT(w, bad_passes, 1);
        w->bad_in_row++;
        wlog(w, "worker: BAD PASS (%s): its %d jobs go to Cal", info.why ? info.why : "?", np);
        snprintf(w->last_error, sizeof w->last_error, "bad pass: %s", info.why ? info.why : "?");
        for (int k = 0; k < np; k++) unembedded(w, &w->pool[pend[pick[k]]], REASON_KATA_UNHEALTHY);
        if (w->bad_in_row >= WORKER_BAD_PASSES && !w->sick) go_sick(w);
    }
    kw_free(ids);
    kw_free(vecs);
}

// ---- a step -------------------------------------------------------------------------------
int worker_step(worker_t *w)
{
    roll_today(w);
    if (w->parked) { w->state = "parked"; return 5; }
    // A sick Kata checks itself every half hour (no network needed: it's a pass).
    if (w->sick && !w->park_requested && now(w) >= w->next_health) {
        w->state = "checking";
        if (w->io.health(w->io.ctx) == 0) {
            w->sick = false;
            w->bad_in_row = 0;
            wlog(w, "worker: health check passed: no longer sick");
        } else {
            w->next_health = now(w) + WORKER_HEALTH_EVERY_S;
            wlog(w, "worker: health check failed: still sick");
        }
    }
    char why[160];
    w->state = "handing-off";
    if (w->io.net_begin(w->io.ctx, why, sizeof why)) {
        COUNT(w, phase_failures, 1);
        snprintf(w->last_error, sizeof w->last_error, "%s", why);
        wlog(w, "worker: no network phase: %s", why);
        w->state = w->sick ? "sick" : "idle";
        return backoff(w);
    }
    handoff(w);
    if (w->park_requested) {
        // For an update: what's finished is handed off, what isn't goes back to the queue.
        visibility(w, true, 0);
        for (int i = w->n - 1; i >= 0; i--)
            if (w->pool[i].state == JOB_PENDING) remove_job(w, i);
        w->io.net_end(w->io.ctx);
        if (w->n == 0) {
            w->parked = true;
            w->state = "parked";
            wlog(w, "worker: parked for an update: nothing held");
            return 0;
        }
        w->state = "handing-off";
        return 5;                             // results still undelivered: try again shortly
    }
    if (count_state(w, JOB_RESULT) || count_state(w, JOB_DELETE)) {
        // Undelivered results: keep everything hidden, and no new pass until they're through.
        visibility(w, false, WORKER_VISIBILITY_S);
        w->io.net_end(w->io.ctx);
        w->state = w->sick ? "sick" : "handing-off";
        return backoff(w);
    }
    w->state = "receiving";
    int arrived = top_up(w);
    if (count_state(w, JOB_RESULT)) handoff(w);
    visibility(w, false, WORKER_VISIBILITY_S);
    w->io.net_end(w->io.ctx);
    w->retry_s = 0;
    if (count_state(w, JOB_PENDING)) {
        w->state = "embedding";
        pass(w);
        w->state = w->sick ? "sick" : "idle";
        return 0;                             // straight on: hand the results off
    }
    w->state = w->sick ? "sick" : "idle";
    if (count_state(w, JOB_RESULT) || count_state(w, JOB_DELETE)) return 0;
    return arrived ? 5 : WORKER_IDLE_S;      // more may be waiting (a sick Kata forwarding, say)
}

void worker_set_sick(worker_t *w, const char *why)
{
    w->bad_in_row = WORKER_BAD_PASSES;
    snprintf(w->last_error, sizeof w->last_error, "%s", why);
    go_sick(w);
    w->state = "sick";
}

void worker_request_park(worker_t *w) { w->park_requested = true; }
bool worker_is_parked(const worker_t *w) { return w->parked; }

void worker_resume(worker_t *w)
{
    w->park_requested = false;
    w->parked = false;
    w->state = w->sick ? "sick" : "idle";
}

// ---- status -------------------------------------------------------------------------------
void worker_card_wait(const worker_t *w, uint32_t *last_ms, uint32_t *median_ms)
{
    int n = w->n_card < WORKER_CARD_HISTORY ? w->n_card : WORKER_CARD_HISTORY;
    *last_ms = n ? w->card_wait[(w->n_card - 1) % WORKER_CARD_HISTORY] : 0;
    uint32_t v[WORKER_CARD_HISTORY];
    memcpy(v, w->card_wait, (size_t)n * sizeof *v);
    for (int i = 1; i < n; i++)                // insertion sort: 15 at most
        for (int k = i; k > 0 && v[k - 1] > v[k]; k--) { uint32_t t = v[k]; v[k] = v[k - 1]; v[k - 1] = t; }
    *median_ms = !n ? 0 : n % 2 ? v[n / 2] : (v[n / 2 - 1] + v[n / 2]) / 2;
}

// From another task (the heartbeat): reads only, so a count may be one step stale.
void worker_status_json(worker_t *w, jbuf_t *b)
{
    int ub = 0, ut = 0;
    for (int i = 0; i < 5; i++) { ub += w->boot.unembedded[i]; ut += w->today.unembedded[i]; }
    jb_printf(b, "{\"state\":\"%s\",\"sick\":%s,\"embedder\":", w->state, w->sick ? "true" : "false");
    jb_json_str(b, w->cfg.embedder, strlen(w->cfg.embedder));
    jb_printf(b, ",\"pool\":{\"n\":%d,\"tokens\":%d},\"passes\":{\"boot\":%d,\"today\":%d},"
                 "\"messages\":{\"boot\":%d,\"today\":%d},\"unembedded\":{\"boot\":%d,\"today\":%d",
              w->n, w->pool_tokens, w->boot.passes, w->today.passes, w->boot.messages, w->today.messages, ub, ut);
    for (int i = 0; i < 5; i++)
        if (w->boot.unembedded[i]) jb_printf(b, ",\"%s\":%d", REASONS[i], w->boot.unembedded[i]);
    jb_printf(b, "},\"bad_passes\":{\"boot\":%d,\"in_row\":%d},\"invalid\":%d,\"failures\":{\"phase\":%d,\"send\":%d,\"delete\":%d}",
              w->boot.bad_passes, w->bad_in_row, w->boot.invalid, w->boot.phase_failures, w->boot.send_failures,
              w->boot.delete_failures);
    if (w->last_pass.at)
        jb_printf(b, ",\"last_pass\":{\"n_seg\":%d,\"tokens\":%d,\"ms\":%lu,\"card_wait_ms\":%lu,\"at\":%lld}", w->last_pass.n_seg,
                  w->last_pass.tokens, (unsigned long)w->last_pass.ms, (unsigned long)w->last_pass.card_wait_ms,
                  (long long)w->last_pass.at);
    else jb_puts(b, ",\"last_pass\":null");
    if (w->last_handoff_at) jb_printf(b, ",\"last_handoff_at\":%lld", (long long)w->last_handoff_at);
    else jb_puts(b, ",\"last_handoff_at\":null");
    if (w->sick) jb_printf(b, ",\"sick_since\":%lld", (long long)w->sick_since);
    if (w->last_error[0]) {
        jb_puts(b, ",\"last_error\":");
        jb_json_str(b, w->last_error, strnlen(w->last_error, sizeof w->last_error - 1));   // racy reads stay bounded
    }
    jb_putc(b, '}');
}
