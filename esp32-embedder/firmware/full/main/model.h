// CHATS-10: the model and an update take turns with internal SRAM (CHATS-4).
//
// The model holds ~164 KB of internal RAM between passes: the int8 input tile (100 KB), the two
// per-core weight-row buffers and the two SD bounce buffers (16 KB each). Wi-Fi and MQTT's TLS
// session fit beside it; a second TLS session for the download, and the flash writes, would not
// leave enough to be safe. So an update borrows those buffers: it waits for the pass in progress to
// end, frees them, downloads, and either restarts into the new image or allocates them again.
#pragma once

#include <stdbool.h>

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
