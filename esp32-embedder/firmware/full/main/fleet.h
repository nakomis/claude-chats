// Kata on Fleet (CHATS-10): MQTT over TLS to AWS IoT Core with the board's own certificate (prov.h),
// as tinycrypt's notifier.c does it. Subscribes to kata/<thing>/commands (ota.c handles them),
// answers on kata/<thing>/responses, and sends a heartbeat on kata/<thing>/status every
// FLEET_STATUS_S, which keeps the board's lastSeen and status live in Fleet.
//
// Small on purpose (CHATS-4): a 2 KB receive buffer (Fleet sends at most 1.5 KB in one message and
// chunks anything longer), a 512-byte send buffer, and mbedTLS in PSRAM. What it costs in internal
// RAM is printed (net_heap) once it's connected.
#pragma once

#include <stdbool.h>

#include "prov.h"

#define FLEET_STATUS_S 60

// Starts the MQTT client (Wi-Fi must be up: net_up). False if the board has no IoT identity.
// prov must outlive it.
bool fleet_start(const prov_t *prov);
// Publishes a response (QoS 1) on kata/<thing>/responses; false if not connected.
bool fleet_respond(const char *json);
// The self-test's verdict, for the heartbeat: "bit-exact", "cos", "failed" or "not run".
void fleet_set_selftest(const char *verdict);
