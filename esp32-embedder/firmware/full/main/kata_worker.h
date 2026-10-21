// Kata the worker on the board (CHATS-5): worker.h's state machine with the board's network, model
// and clock. See worker.h for what a step does, and the README's "The worker" for the whole story.
//
// One TLS session at a time (CHATS-4: internal RAM), and no MQTT (CHATS-13): in each phase the
// credentials provider, SQS (jobs, then Fleet's commands) and IoT Core's HTTPS publish (the outbox:
// responses and the status heartbeat) take turns, each session closed before the next opens.
#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "esp_system.h"
#include "prov.h"

// esp_reset_reason as a word ("power-on", "software", "brownout", "task-watchdog"…).
const char *reset_name(esp_reset_reason_t r);

// Before Fleet starts: the temperature sensor, and the worker's fields in the heartbeat.
void kata_worker_status_init(int vcore_mv);

// CHATS-20: the worker's numbers for the metrics line (metrics.c).
typedef struct {
    int vcore_mv;
    bool have_temp;
    float temp_c;
    bool running, sick;                  // sick is false when not running
    int messages_boot, messages_today;   // jobs embedded since boot, and today (UTC)
    int stalled_s;                       // worker.h: how long work has been held with no pass or hand-off
    uint32_t card_wait_last_ms, card_wait_median_ms;
} kata_worker_metrics_t;
void kata_worker_metrics(kata_worker_metrics_t *m);

// At boot, before the self-test (CHATS-13): one phase with no jobs, so Fleet sees the board within
// seconds (its version and status) and any waiting command is read.
void kata_worker_hello(const prov_t *prov, bool online);
// A phase with no jobs, now (ota.c, before a download: its progress reaches Fleet).
void kata_worker_publish(void);
// With no worker (no model, or not provisioned for it): a phase with no jobs every minute, forever,
// so Fleet can still reach and update the board. Returns at once if the board can't reach AWS.
void kata_worker_control_loop(void);

// The worker loop, in the calling task (app_main's, after the self-test). Returns only if the board
// can't work (not online, not provisioned for it), having said why. healthy: the boot self-test's
// verdict; a board that failed it starts sick (everything to Cal until a health check passes).
void kata_worker_run(const prov_t *prov, bool online, bool healthy);

// For an update (ota.c): hands off what's finished, releases what isn't, and stops taking jobs.
// True once parked (or if there's no worker), false if that took longer than timeout_ms.
bool kata_worker_park(int timeout_ms);
// For an update (ota.c): asks the worker to park without waiting. False if there's no worker.
bool kata_worker_request_park(void);
// The update failed: back to work.
void kata_worker_resume(void);

// ---- CHATS-16: for statusd (the LAN status endpoint) ---------------------------------------------
// Held for a whole reply: no SQS phase starts while it's held (net_begin waits on it, briefly). False
// if it wasn't free within wait_ms.
bool kata_worker_gate(int wait_ms);
void kata_worker_ungate(void);
// Is an SQS phase (TLS sessions, the internal-RAM low point) under way? Stable while gated: it can only
// turn true under the gate.
bool kata_worker_in_phase(void);
// The worker's own state: worker.h's ("idle", "receiving", "embedding", "handing-off", "sick",
// "parked"), or "off" if it never started.
const char *kata_worker_state(void);
