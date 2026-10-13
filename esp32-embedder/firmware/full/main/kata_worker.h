// Kata the worker on the board (CHATS-5): worker.h's state machine with the board's network, model
// and clock. See worker.h for what a step does, and the README's "The worker" for the whole story.
//
// One TLS session at a time (CHATS-4: internal RAM): MQTT steps aside for each SQS phase (fleet_pause),
// the credentials provider and SQS take turns, and the session is closed before MQTT comes back.
#pragma once

#include <stdbool.h>

#include "prov.h"

// Before Fleet starts: the temperature sensor, and the worker's fields in the heartbeat.
void kata_worker_status_init(int vcore_mv);

// The worker loop, in the calling task (app_main's, after the self-test). Returns only if the board
// can't work (not online, not provisioned for it), having said why. healthy: the boot self-test's
// verdict; a board that failed it starts sick (everything to Cal until a health check passes).
void kata_worker_run(const prov_t *prov, bool online, bool healthy);

// For an update (ota.c): hands off what's finished, releases what isn't, and stops taking jobs.
// True once parked (or if there's no worker), false if that took longer than timeout_ms.
bool kata_worker_park(int timeout_ms);
// The update failed: back to work.
void kata_worker_resume(void);
