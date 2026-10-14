#pragma once

#include <stddef.h>

#include "kw.h"

// CHATS-5: where internal RAM runs lowest. IDF's "lowest ever" says how low, not when: this keeps a
// low point for each stage of the worker's cycle, from IDF's local-minimum monitor, so the heartbeat
// (and a line on the console at each new overall low) says which stage takes it.
//
// The stage follows from what's going on, set from the worker's task and MQTT's:
//   creds     fetching credentials (the mTLS session to the credentials provider)
//   sqs       the SQS phase (one TLS session: receive, results, deletes, visibility, commands)
//   iotpub    the HTTPS publish of the outbox, after the SQS session closes (CHATS-13)
//   pass      a pass running
//   idle      nothing else
typedef enum { HW_CREDS, HW_SQS, HW_IOTPUB, HW_PASS, HW_IDLE, HW_N } hw_stage_t;

void heapwatch_init(void);
// What changed: each recomputes the stage, closing the last one's window
void heapwatch_paused(int in_phase);     // a network phase begins / ends (kata_worker.c)
void heapwatch_creds(int in_creds);      // around creds_refresh
void heapwatch_pass(int in_pass);        // around a pass
void heapwatch_iotpub(int in_pub);       // around the HTTPS publish
// The lowest internal free since boot, IDF's from before the watch started included
size_t heapwatch_lowest(void);
// ,"heap_low":{"creds":N,...} for the heartbeat
void heapwatch_json(jbuf_t *b);
