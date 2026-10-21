// The results-queue message (CHATS-5): convmem_wire::KataResult v1, home-infra cal/convmem-wire.
// Kata hands each job to Cal as one of these, and only once SQS has accepted it deletes the ingest
// job: a job is always in at least one durable queue until its row is committed.
//
//   {"v":1,"kind":"embedded"|"unembedded","message_uuid":"…","envelope":{…the ingest body…},
//    "embedder":"esp32s3-mxbai-int8-v1"|null,"vector":"<base64 of 1024 little-endian f32>"|null,
//    "tokens":87|null,"truncated_chars":false|null,"reason":null|"claim-check"|…,
//    "board":"kata-prod-kata","fw":"0.3.0","pass":{"n_seg":3,"tokens":211,"ms":79140}|null,
//    "ingest":{"message_id":"…","receive_count":1}}
//
// The envelope is the ingest message's body exactly as SQS delivered it, spliced in byte for byte:
// never parsed and re-serialised, so fields Kata doesn't know (the forwarder's signature, sig_key
// and sig) reach Cal untouched. The vector is the raw [CLS] row, not normalised (Cal normalises),
// so boards can be compared bit for bit. Checked against track C's golden fixtures
// (../host/convmem-wire).
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "kw.h"

#define RESULT_DIMS 1024
#define RESULT_VERSION 1

// The reasons an unembedded result gives (convmem_wire::reasons).
#define REASON_CLAIM_CHECK "claim-check"        // an S3 claim check: Kata never touches S3
#define REASON_DECRYPT_FAILED "decrypt-failed"  // not for this board's key, a uuid mismatch, or not a job
#define REASON_RETRIES "retries"                // ApproximateReceiveCount >= 3: it may be what crashes Kata
#define REASON_TOO_LONG "too-long"              // a body over WORKER_WORST_BODY: more than Kata's PSRAM can take
#define REASON_KATA_UNHEALTHY "kata-unhealthy"  // a bad pass, or Kata is sick: Cal embeds it at once

typedef struct {
    bool embedded;
    const char *message_uuid;                   // the envelope's
    const char *envelope;                       // the ingest body, verbatim
    size_t envelope_len;
    // embedded
    const char *embedder;
    const float *vector;                        // RESULT_DIMS
    int tokens;
    bool truncated_chars;
    // unembedded
    const char *reason;
    // provenance and diagnostics
    const char *board, *fw;
    bool has_pass;
    int pass_n_seg, pass_tokens;
    uint32_t pass_ms;
    const char *message_id;                     // the ingest message's SQS id
    int receive_count;
} kata_result_t;

// Appends the message (compact JSON) to out. -1 if out of memory.
int result_encode(const kata_result_t *r, jbuf_t *out);
