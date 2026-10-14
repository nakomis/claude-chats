// Over-the-air updates through Fleet (CHATS-10): tinycrypt's ota.c (CRYPT-14, CRYPT-21) for Kata,
// with the board as the trust anchor and its self-test as the health check. No approval step:
// nobody is there to approve, so the image's own checks decide.
//
// Fleet only says "please update": ota_update {url, version, sha256} (or url_chunks) on
// kata/<thing>/commands. Kata then
//   1. lends the model's internal buffers to the update (model.h), once the pass in progress ends;
//   2. downloads the image over HTTPS (Amazon Root CA 1) into the spare slot, hashing as it goes;
//   3. refuses it unless its SHA-256 is Fleet's, its RSA-3072 signature is by the key that signed
//      the running app (esp_ota_end, CONFIG_SECURE_SIGNED_ON_UPDATE_NO_SECURE_BOOT), it says it is
//      kata at that version, and that version is newer than the running one;
//   4. switches slots and restarts. On any failure it takes its buffers back and carries on.
// The new image is on probation until its self-test PASSES (every test run, every vector bit-exact
// against selftest.h, no CRC or tokeniser mismatches) and it has subscribed to its commands on IoT
// Core, within OTA_VERIFY_DEADLINE_S. A failure, a crash or the deadline rolls back to the old
// image, which reports ota_error "rolled back".
// Progress goes back on kata/<thing>/responses as Fleet expects (ota_progress, ota_error,
// ota_complete, and a version message on every connect).
#pragma once

#include <stdbool.h>
#include <stddef.h>

#include "ota_cmd.h"

// A new image's self-test is the 7-vector run in full.c (~15 min at 240 MHz), then it must be on
// IoT Core: both within this, from boot.
#define OTA_VERIFY_DEADLINE_S 1800
// The fallback for a build whose CRCs were zeroed until re-recorded (the arithmetic changed): each
// unrecorded vector's cosine against Ollama must reach this. Healthy boards give 0.99937-0.99978 on
// the seven (README, CHATS-4); a broken one gives far less.
#define HEALTH_MIN_COS 0.999f

// The longest an update waits for the pass in progress (a 512-token pass is ~290 s). Fleet's
// presigned URL lasts 10 minutes, and the download has to start within them.
#define OTA_MODEL_WAIT_S 420
// What a misbehaving Fleet (or anyone who can publish to the commands topic) can cost (CRYPT-21):
// each update pauses embedding and erases the spare slot. Kept in RAM: a reboot starts afresh.
#define OTA_DOWNLOAD_DEADLINE_S 300
#define OTA_MIN_RATE_WINDOW_MS 30000  // at least OTA_MIN_RATE_BYTES in every window, or it's abandoned
#define OTA_MIN_RATE_BYTES (32 * 1024)
#define OTA_BACKOFF_FIRST_S 30  // after a failed update; doubles each time, up to OTA_BACKOFF_MAX_S
#define OTA_BACKOFF_MAX_S 900

// Early in app_main, after nvs_flash_init: is this image new and on probation? Starts the deadline.
bool ota_boot(void);
// After the self-test (only matters on probation): a failure rolls back at once; a pass is half of
// what keeps the image (the other half is ota_on_connected).
void ota_health(bool healthy);

// From kata_worker.c, once a publish has reached IoT Core (CHATS-13): reports the version, and the outcome of
// the last update once it's known.
void ota_on_connected(void);
// From kata_worker.c (a phase, CHATS-13): one command from Fleet, off the board's SQS queue.
void ota_handle_command(const char *data, size_t len);
// CHATS-13: from the worker's loop: starts a requested update's task once the worker has parked (or
// has taken too long to).
void ota_spawn_due(bool worker_parked);
