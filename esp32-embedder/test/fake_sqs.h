// A fake Amazon SQS for the worker's host tests: queues in memory, SQS's JSON protocol, and every
// request's SigV4 signature checked by signing it again. Visibility timeouts, receive counts and
// receipt handles behave as SQS's do (a stale receipt handle "deletes" successfully but doesn't),
// on a clock the test controls. Faults can be injected.
#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "creds.h"
#include "sqs.h"

#define FQ_MAX_MSGS 512
#define FQ_MAX_QUEUES 2

typedef struct {
    char *id, *body, *receipt;
    int receive_count;
    int64_t visible_at;
    bool deleted, in_flight;
} fq_msg_t;

typedef struct {
    char url[128];
    fq_msg_t msgs[FQ_MAX_MSGS];
    int n;
} fq_queue_t;

typedef struct {
    fq_queue_t q[FQ_MAX_QUEUES];
    int nq;
    int64_t now;                        // the clock: Unix seconds
    const aws_creds_t *creds;           // to check signatures with
    int calls[5];                       // Receive, Send, Delete, Visibility, other
    int bad_signatures;
    // faults
    int fail_next;                      // the next n calls get no response
    int http_500_next;                  // the next n calls get HTTP 500
    const char *refuse_body_containing; // SendMessageBatch entries with this in their body fail
    bool corrupt_md5;                   // ReceiveMessage reports a wrong MD5OfBody
    int64_t max_batch_seen;             // the largest SendMessageBatch total, in bytes
    int seq;
} fake_sqs_t;

void fq_init(fake_sqs_t *f, const aws_creds_t *creds, int64_t now);
void fq_free(fake_sqs_t *f);
fq_queue_t *fq_queue(fake_sqs_t *f, const char *url);      // made on first use
// Puts a message on a queue as a producer would; returns its id.
const char *fq_send(fake_sqs_t *f, const char *url, const char *body);
int fq_live(const fq_queue_t *q);                           // not deleted
int fq_visible(const fake_sqs_t *f, const fq_queue_t *q);   // receivable now
// The sqs.h transport, with ctx = the fake_sqs_t.
int fq_post(void *ctx, const http_req_t *req, jbuf_t *resp);
int64_t fq_now(void *ctx);
