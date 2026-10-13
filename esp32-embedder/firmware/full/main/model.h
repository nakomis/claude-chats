// CHATS-10: the model and an update take turns with internal SRAM (CHATS-4).
//
// The model holds ~164 KB of internal RAM between passes: the int8 input tile (100 KB), the two
// per-core weight-row buffers and the two SD bounce buffers (16 KB each). Wi-Fi and MQTT's TLS
// session fit beside it; a second TLS session for the download, and the flash writes, would not
// leave enough to be safe. So an update borrows those buffers: it waits for the pass in progress to
// end, frees them, downloads, and either restarts into the new image or allocates them again.
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "pass_info.h"

// Around every pass (embed_in + pass_fwd): blocks while an update holds the buffers.
void model_pass_begin(void);
void model_pass_end(void);

// For the update: waits up to timeout_ms for the pass in progress to end, then frees the
// buffers and holds the model until model_reclaim(). False if the pass didn't end in time.
bool model_lend(int timeout_ms);
// The update failed: allocates the buffers again and lets passes run. If internal RAM is too
// fragmented by then for the 100 KB tile, the board restarts (and comes back with its model).
void model_reclaim(void);

// For the status heartbeat: "embedding", "updating", "checking" (the self-test) or "idle".
const char *model_state(void);

// ---- CHATS-5: the model for the worker (kata_worker.c) ----------------------------------------
// The tokeniser: [CLS] .. [SEP], at most max ids, truncated as Ollama does.
int model_tokenise(const char *text, size_t len, int32_t *ids, int max);
// One pass over n packed messages (lens[i] ids each, at most 512 in all); vecs gets n raw [CLS] rows.
// 0, or -1 if the pass was bad (a section CRC mismatch aborts it: info->why).
int model_embed(const int32_t *ids, const int *lens, int n, float *vecs, worker_pass_info_t *info);
// A sick worker's health check: the short self-test texts in one pass, bit-exact. 0 if healthy.
int model_health(void);
// The card's health since boot: section CRC mismatches and sector reads that needed a retry.
void model_card_health(int *crc_mismatches, int *retries);
