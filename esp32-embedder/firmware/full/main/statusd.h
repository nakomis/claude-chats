// CHATS-16: Kata answers GET /json on the LAN, live, with her status. Leia's nginx terminates TLS
// for kata.home.nakomis.com (home-infra HOME-513) and asks over plain HTTP: TLS would cost the board
// a session's worth of internal RAM, which the SQS phases need.
//
// One task, one connection at a time, one route. The reply is the heartbeat's status built now
// (fleet_status_build) plus a "now" object: what she's doing at this moment, and how far a pass has
// got. The task outranks the compute tasks, so a pass pauses for the few milliseconds a reply takes.
// An SQS phase is the one time internal RAM is short: then, below STATUSD_MIN_INTERNAL free, the
// reply is a small 503 with Retry-After instead. No phase starts mid-reply (kata_worker_gate).
//
// Who may ask: the addresses in prov's status_allow, or with none provisioned, any peer on the
// board's own subnet. Anyone else is closed on without a reply.
#pragma once

#include <stdbool.h>

#include "prov.h"

// Starts the task (once Wi-Fi is up). False, having said why, if it couldn't.
bool statusd_start(const prov_t *prov);
