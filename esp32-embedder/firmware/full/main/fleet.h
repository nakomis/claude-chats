// Kata on Fleet (CHATS-10, CHATS-13): what Fleet sends the board and what the board tells Fleet,
// without MQTT. Next to the model, internal RAM can't hold an MQTT session beside the worker's SQS
// one, and stopping and starting MQTT round every SQS phase fragmented it (CHATS-13). So:
//
//   in:  Fleet publishes a command on kata/<thing>/commands; an IoT rule (infra, CommandsRule) puts
//        it on the board's SQS queue, <thing>-commands, which kata_worker.c reads in each SQS phase
//        and hands to ota.c.
//   out: ota.c's responses and the status heartbeat wait here, in an outbox, until kata_worker.c
//        publishes them over HTTPS (SigV4, service iotdata) after the phase, to the same topics as
//        before: kata/<thing>/responses, and kata/<thing>/status (retained, for BD-1's HOME-441).
//
// So a command is answered at the board's next phase: seconds when it's idle, up to a pass when
// it's busy. Fleet and BD-1 see the same topics as they did over MQTT.
#pragma once

#include <stdbool.h>

#include "kw.h"
#include "prov.h"

#define FLEET_STATUS_S 60
#define FLEET_OUTBOX 16          // responses waiting for the next publish; beyond that the oldest go

// The topics, and the heartbeat timer. False if the board has no IoT identity.
bool fleet_start(const prov_t *prov);
// Queues a response for kata/<thing>/responses (published after the next phase). False only if out
// of memory.
bool fleet_respond(const char *json);
// The self-test's verdict, for the heartbeat: "bit-exact", "cos", "failed" or "not run".
void fleet_set_selftest(const char *verdict);

// CHATS-5: the worker's fields, appended to every heartbeat (",\"key\":value..." into out).
void fleet_set_status_extra(void (*extra)(jbuf_t *out));
// A heartbeat now (after a state change): it replaces the one waiting to be published.
void fleet_status_now(void);
// CHATS-16: the heartbeat's status, built now, into b without its closing brace (statusd.c adds its
// own fields, then closes it). False if the status lock wasn't free within wait_ms (or Fleet never
// started): b is untouched.
bool fleet_status_build(jbuf_t *b, int wait_ms);

// For the publisher (kata_worker.c): take what's waiting (the caller kw_frees each): the latest
// status, or NULL, and up to max responses, oldest first. Returns how many responses.
int fleet_take(char **status, char **out, int max);
// Puts back what couldn't be published, ahead of anything queued since (a status only if no newer
// one has been queued).
void fleet_put_back(char *status, char **out, int n);
// "kata/<thing>/responses" and "kata/<thing>/status".
const char *fleet_responses_topic(void);
const char *fleet_status_topic(void);
