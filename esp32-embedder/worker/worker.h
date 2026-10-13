// Kata's worker (CHATS-5): takes conversation-memory jobs from the ingest queue, embeds several per
// pass, and hands each to Cal on the results queue, deleting the ingest job only once SQS has
// accepted the result. A job is always in at least one durable queue until Cal commits its row.
//
// Portable: the board's network, model and clock come in through worker_io_t (the firmware's are in
// firmware/full/main/kata_worker.c), and the host tests drive it against a fake SQS.
//
// One step (worker_step) is a network phase and then, if there's work, one pass:
//
//   network phase (MQTT paused, one TLS session: io->net_begin ... io->net_end)
//     1. hand off: SendMessageBatch the finished results (<= 10 and <= 256 KiB a batch; a big one
//        goes alone), then DeleteMessageBatch the ingest jobs SQS accepted. Never delete first.
//        A result SQS didn't take stays, and no pass starts while any is undelivered.
//     2. top up the pool: ReceiveMessage (visibility 900 s; a 20 s long poll only when the pool is
//        empty) while the pool's tokens are under the target and its bytes under the budget.
//        Each job is sorted on arrival:
//          an S3 claim check                 -> unembedded "claim-check" (Kata never touches S3)
//          ApproximateReceiveCount >= 3      -> unembedded "retries" (it may be what crashes Kata)
//          Kata sick                         -> unembedded "kata-unhealthy" (Cal embeds it at once)
//          doesn't decrypt, uuids disagree,
//          or not a job                      -> unembedded "decrypt-failed" (Cal decides)
//          ApproximateReceiveCount == 2      -> pooled, but embedded in a pass of its own
//          otherwise                         -> canonical text, tokenised, pooled
//        A body that isn't an envelope at all can't be handed on: it's left to reach the ingest
//        queue's dead-letter queue. Unembedded results are handed off in the same phase.
//     3. extend the visibility of pooled jobs that could expire before the next phase
//   pass (MQTT up)
//     the planner (batch.h) picks the oldest job and fills the pass up to the target; the pass
//     (io->embed) must come back clean: no section CRC mismatch, every vector finite with a raw norm in
//     Cal's band (10-30). Vectors go raw, never normalised: Cal normalises, and boards compare bit for bit.
//     A bad pass delivers no vectors: its jobs go to Cal as "kata-unhealthy". Three bad passes in
//     a row and Kata is sick: everything goes to Cal straight away (Martin, 13 Oct) until a health
//     check (io->health, every 30 minutes) passes.
//
// Before an OTA (worker_request_park), the next step hands off what's finished, releases the rest
// (visibility 0) and parks; worker_resume() if the update fails.
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "age.h"
#include "kw.h"
#include "pass_info.h"
#include "result.h"
#include "sqs.h"

#define WORKER_VISIBILITY_S 900          // on receive and on extension
#define WORKER_EXTEND_HORIZON_S 600      // extend a job that would be visible again within this
#define WORKER_LONG_POLL_S 20
#define WORKER_IDLE_S 40                 // MQTT's turn after an empty long poll
#define WORKER_RETRY_FIRST_S 30          // after a failed phase or hand-off; doubles
#define WORKER_RETRY_MAX_S 300
#define WORKER_BAD_PASSES 3              // in a row: sick
#define WORKER_HEALTH_EVERY_S 1800       // a sick Kata's health check
#define WORKER_MAX_RECEIVE 3             // ReceiveMessage calls per phase
#define WORKER_SOLO_RECEIVE 2            // a job's second delivery gets a pass to itself
#define WORKER_RETRIES_RECEIVE 3         // from its third, it goes to Cal unembedded
#define WORKER_WORST_BODY 262144         // a receive's worst case per message
#define WORKER_CARD_HISTORY 15           // passes in the card-wait median
#define WORKER_NORM_MIN 10.0             // a raw [CLS] vector's norm, as Cal checks it (results.rs)
#define WORKER_NORM_MAX 30.0
#define WORKER_JSON_DEPTH 32             // envelopes and jobs nest 3 deep; cJSON recurses per level
#define WORKER_STATUS_ERROR_MAX 96       // last_error's share of the heartbeat

typedef struct {
    void *ctx;
    int64_t (*now)(void *ctx);           // Unix seconds (SNTP)
    // The network phase: pause MQTT, fresh credentials, the SQS session. 0, or -1 with why.
    int (*net_begin)(void *ctx, char *why, size_t why_len);
    void (*net_end)(void *ctx);
    sqs_t *sqs;                          // usable between net_begin and net_end
    // The model. tokenise: [CLS] .. [SEP], at most max ids (truncated as Ollama does): the count, or <0.
    int (*tokenise)(void *ctx, const char *text, size_t len, int32_t *ids, int max);
    // One pass over n_seg packed messages (lens[i] ids each); vecs gets n_seg x RESULT_DIMS floats.
    // 0, or -1 if the pass is bad (info->why says why: a CRC mismatch above all).
    int (*embed)(void *ctx, const int32_t *ids, const int *lens, int n_seg, float *vecs, worker_pass_info_t *info);
    // A sick Kata's health check: 0 if healthy again.
    int (*health)(void *ctx);
    void (*log)(void *ctx, const char *msg);
} worker_io_t;

typedef struct {
    const char *ingest_url, *results_url;
    const char *board, *fw, *embedder;   // kata-prod-green, 0.3.0, esp32s3-mxbai-int8-v1
    const age_identity_t *identity;      // NULL: nothing decrypts (all "decrypt-failed")
    int target_tokens;                   // the planner's soft target (~150: the card-bound knee)
    size_t pool_budget;                  // bytes of bodies and results held in the pool
    int pool_max;                        // jobs
} worker_cfg_t;

typedef enum { JOB_PENDING, JOB_RESULT, JOB_DELETE } job_state_t;

typedef struct {
    sqs_msg_t m;                         // body freed once the result is built
    char uuid[64];
    int64_t received_at, visible_until;
    job_state_t state;
    bool solo;
    int32_t *ids;
    int n_ids;
    bool truncated;
    char *result;                        // the results-queue message
    size_t result_len;
    const char *reason;                  // unembedded, or NULL
    int delete_tries;
} job_t;

typedef struct {
    int passes, messages;                // passes run; jobs embedded
    int unembedded[5];                   // by reason: claim-check, decrypt-failed, retries, too-long, kata-unhealthy
    int bad_passes;
    int invalid;                         // bodies that weren't envelopes (left for the DLQ)
    int send_failures, delete_failures, phase_failures;
} worker_counts_t;

typedef struct {
    worker_cfg_t cfg;
    worker_io_t io;
    job_t *pool;                         // [cfg.pool_max]
    int n;
    size_t pool_bytes;
    int pool_tokens;                     // pending jobs' tokens
    bool sick;
    int bad_in_row;
    int64_t sick_since, next_health;
    int retry_s;                         // current back-off
    volatile bool park_requested, parked;
    const char *state;                   // idle, receiving, embedding, handing-off, sick, parked
    worker_counts_t boot, today;
    char today_ymd[11];
    struct {
        int n_seg, tokens;
        uint32_t ms, card_wait_ms;
        int64_t at;
    } last_pass;
    uint32_t card_wait[WORKER_CARD_HISTORY];
    int n_card;
    int64_t last_handoff_at;
    char last_error[160];
} worker_t;

// -1 if out of memory.
int worker_init(worker_t *w, const worker_cfg_t *cfg, const worker_io_t *io);
void worker_free(worker_t *w);

// One network phase and at most one pass. Returns the seconds to wait (with MQTT up) before the
// next step: 0 to go straight on.
int worker_step(worker_t *w);

// Start sick (the boot self-test failed): everything goes to Cal until a health check passes.
void worker_set_sick(worker_t *w, const char *why);

// For an update (CHATS-10): from another task. The next step hands off, releases the rest, and parks.
void worker_request_park(worker_t *w);
bool worker_is_parked(const worker_t *w);
void worker_resume(worker_t *w);

// The "worker" object of the status heartbeat (HOME-441), appended to out.
void worker_status_json(worker_t *w, jbuf_t *out);
// The card's wait per pass: the last, and the median of the last WORKER_CARD_HISTORY (0 if none).
void worker_card_wait(const worker_t *w, uint32_t *last_ms, uint32_t *median_ms);
