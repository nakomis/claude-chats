// What a pass reports back to the worker (worker.h), on its own so the model (firmware/full/main/
// model.h) can use it without the worker's types.
#pragma once

#include <stdint.h>

typedef struct {
    uint32_t ms;                         // the pass, end to end
    uint32_t card_wait_ms;               // of which waiting on the SD card
    int crc_bad;                         // section CRC mismatches in this pass
    const char *why;                     // set when the pass is bad
} worker_pass_info_t;
