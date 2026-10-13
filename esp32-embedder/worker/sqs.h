// Amazon SQS over its JSON protocol (CHATS-5): the four calls Kata's worker makes, signed with
// SigV4 and the credentials provider's temporary credentials. Each call is one HTTPS POST to the
// queue's regional endpoint ("/", X-Amz-Target: AmazonSQS.<Action>, application/x-amz-json-1.0);
// the firmware keeps one TLS session alive across a network phase (kata_net.c), and the host tests
// use a fake SQS (../test/fake_sqs.c).
//
// Integrity: a received body is checked against its MD5OfBody, and a sent one against the
// MD5OfMessageBody SQS reports back, as the AWS SDKs do.
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "creds.h"
#include "kw.h"
#include "sigv4.h"

#define SQS_MAX_BATCH 10                // entries per *Batch call
#define SQS_MAX_BATCH_BYTES 262144      // SendMessageBatch: the whole batch's bodies, 256 KiB

// One request: POST https://<host><path> with these headers (Authorization included) and body.
typedef struct {
    const char *host, *path;
    const sv4_hdr_t *hdrs;
    int n_hdrs;
    const char *body;
    size_t body_len;
} http_req_t;

// The transport: the HTTP status, with the response body in resp; or -1 if no response came.
typedef int (*http_post_fn)(void *ctx, const http_req_t *req, jbuf_t *resp);

typedef struct {
    http_post_fn post;
    void *ctx;
    const aws_creds_t *creds;
    int64_t (*now)(void *ctx);              // Unix seconds, for x-amz-date
    char region[24];
    char host[64];                          // sqs.<region>.amazonaws.com
    jbuf_t req, resp;                       // reused between calls
    int last_status;                        // the last call's HTTP status (-1: no response)
    char last_error[160];                   // and what went wrong
} sqs_t;

// A received message. All strings kw_alloc'd; sqs_msg_free frees them.
typedef struct {
    char *message_id, *receipt;
    char *body;                             // the body exactly as sent, NUL-terminated
    size_t body_len;
    int receive_count;                      // ApproximateReceiveCount
} sqs_msg_t;

// Region and host from a queue URL (https://sqs.<region>.amazonaws.com/<account>/<name>). -1 if
// it's not one.
int sqs_init(sqs_t *s, const char *queue_url, const aws_creds_t *creds, int64_t (*now)(void *), http_post_fn post, void *ctx);
void sqs_free(sqs_t *s);
void sqs_msg_free(sqs_msg_t *m);

// ReceiveMessage: up to max (1..10) messages, waiting up to wait_s for the first, each hidden for
// visibility_s. *msgs (kw_alloc'd array, free each with sqs_msg_free, then kw_free it) holds *n.
// Messages whose body fails its MD5 are dropped (they come back after visibility_s). 0 or -1.
int sqs_receive(sqs_t *s, const char *queue_url, int max, int wait_s, int visibility_s, sqs_msg_t **msgs, int *n);

// SendMessageBatch: n (<= SQS_MAX_BATCH) bodies whose total is <= SQS_MAX_BATCH_BYTES; ok[i] says
// which SQS accepted (and whose MD5 matched). 0 if the call worked (whatever ok[] says), -1 if not
// (then every ok[i] is false).
int sqs_send_batch(sqs_t *s, const char *queue_url, const char *const *bodies, const size_t *lens, int n, bool *ok);
// DeleteMessageBatch and ChangeMessageVisibilityBatch, likewise for receipt handles.
int sqs_delete_batch(sqs_t *s, const char *queue_url, const char *const *receipts, int n, bool *ok);
int sqs_visibility_batch(sqs_t *s, const char *queue_url, const char *const *receipts, int n, int visibility_s, bool *ok);
