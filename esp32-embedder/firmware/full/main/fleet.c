// Kata on Fleet (CHATS-10, CHATS-13): the outbox the worker publishes over HTTPS, and the heartbeat.
// See fleet.h.
#include "fleet.h"
#include "heapwatch.h"
#include "model.h"

#include <stdio.h>
#include <string.h>

#include "esp_app_desc.h"
#include "esp_heap_caps.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"

#include "kw.h"

#define STATUS_MAX 2048          // a heartbeat is about 1 KB

static const prov_t *prov;
static char responses_topic[sizeof prov->thing_name + 16];
static char status_topic[sizeof prov->thing_name + 16];
static const char *volatile selftest = "not run";
static esp_timer_handle_t status_timer;
static SemaphoreHandle_t status_mx;      // the heartbeat runs from the esp_timer task and the worker's
static void (*status_extra)(jbuf_t *);

// The outbox (PSRAM strings): the latest status, and the responses in order. Filled from the worker's
// task, ota.c's and the esp_timer task; emptied by the worker's publish.
static char *out_status;
static char *out[FLEET_OUTBOX];
static int out_n;
static portMUX_TYPE out_lock = portMUX_INITIALIZER_UNLOCKED;

void fleet_set_selftest(const char *verdict) { selftest = verdict; }
void fleet_set_status_extra(void (*extra)(jbuf_t *out)) { status_extra = extra; }

// The heartbeat (esp_timer task): what Fleet shows as the board's status, and (CHATS-5, HOME-441)
// what BD-1 reads: retained, so IoT Core's GetRetainedMessage has the latest between heartbeats.
// Built every FLEET_STATUS_S; only the latest waits for the publish.
// The worker adds its own fields (status_extra: card health, passes, sick, temperature, voltage).
// The status's fields, without the closing brace (the caller may add its own). Caller holds status_mx.
static void build_status(jbuf_t *b)
{
    jb_printf(b, "{\"type\":\"status\",\"version\":\"%s\",\"state\":\"%s\",\"selftest\":\"%s\",\"uptime_s\":%lld,"
                 "\"internal_free\":%u,\"internal_largest\":%u,\"internal_lowest\":%u,\"psram_free\":%u",
              esp_app_get_description()->version, model_state(), selftest, (long long)(esp_timer_get_time() / 1000000),
              (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL),
              (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL),
              (unsigned)heapwatch_lowest(),
              (unsigned)heap_caps_get_free_size(MALLOC_CAP_SPIRAM));
    if (status_extra) status_extra(b);
}

bool fleet_status_build(jbuf_t *b, int wait_ms)
{
    if (!status_mx || xSemaphoreTake(status_mx, pdMS_TO_TICKS(wait_ms)) != pdTRUE) return false;
    build_status(b);
    xSemaphoreGive(status_mx);
    return true;
}

static void send_status(void *arg)
{
    (void)arg;
    if (!status_mx || xSemaphoreTake(status_mx, pdMS_TO_TICKS(2000)) != pdTRUE) return;
    jbuf_t b;                                 // PSRAM (kw.h)
    jb_init(&b);
    build_status(&b);
    jb_putc(&b, '}');
    if (b.oom) printf("fleet: out of memory for the status\n");
    else if (b.n > STATUS_MAX) printf("fleet: status of %u bytes is too big: not sent\n", (unsigned)b.n);
    else {
        char *s = kw_strndup(b.p, b.n);
        taskENTER_CRITICAL(&out_lock);
        char *old = out_status;
        out_status = s;
        taskEXIT_CRITICAL(&out_lock);
        kw_free(old);
    }
    jb_free(&b);
    xSemaphoreGive(status_mx);
}

void fleet_status_now(void) { send_status(NULL); }

bool fleet_start(const prov_t *p)
{
    prov = p;
    status_mx = xSemaphoreCreateMutex();
    if (!prov->has_identity) {
        printf("fleet: no IoT identity in prov: Fleet can't reach this board (run infra/scripts/provision.sh)\n");
        return false;
    }
    snprintf(responses_topic, sizeof responses_topic, "kata/%s/responses", prov->thing_name);
    snprintf(status_topic, sizeof status_topic, "kata/%s/status", prov->thing_name);
    const esp_timer_create_args_t timer = { .callback = send_status, .name = "fleet_status" };
    if (esp_timer_create(&timer, &status_timer) != ESP_OK ||
        esp_timer_start_periodic(status_timer, (uint64_t)FLEET_STATUS_S * 1000000) != ESP_OK) {
        printf("fleet: no heartbeat timer\n");
        return false;
    }
    send_status(NULL);
    printf("fleet: %s: commands from SQS, responses and status over HTTPS (CHATS-13)\n", prov->thing_name);
    return true;
}

bool fleet_respond(const char *json)
{
    char *copy = kw_strndup(json, strlen(json));
    if (!copy) {
        printf("fleet: out of memory: response dropped: %s\n", json);
        return false;
    }
    char *dropped = NULL;
    taskENTER_CRITICAL(&out_lock);
    if (out_n == FLEET_OUTBOX) {
        dropped = out[0];
        memmove(out, out + 1, (FLEET_OUTBOX - 1) * sizeof *out);
        out_n--;
    }
    out[out_n++] = copy;
    taskEXIT_CRITICAL(&out_lock);
    if (dropped) printf("fleet: outbox full: dropped %s\n", dropped);
    kw_free(dropped);
    printf("fleet: response queued: %s\n", json);
    return true;
}

int fleet_take(char **status, char **taken, int max)
{
    taskENTER_CRITICAL(&out_lock);
    *status = out_status;
    out_status = NULL;
    int n = out_n < max ? out_n : max;
    memcpy(taken, out, n * sizeof *taken);
    memmove(out, out + n, (out_n - n) * sizeof *out);
    out_n -= n;
    taskEXIT_CRITICAL(&out_lock);
    return n;
}

void fleet_put_back(char *status, char **back, int n)
{
    char *drop_status = NULL, *dropped[FLEET_OUTBOX];
    int n_dropped = 0;
    taskENTER_CRITICAL(&out_lock);
    if (status && !out_status) out_status = status;
    else drop_status = status;
    // Ahead of anything newer; if that overflows, the newest put-back ones go (they'll be stale).
    int room = FLEET_OUTBOX - out_n, keep = n < room ? n : room;
    memmove(out + keep, out, out_n * sizeof *out);
    memcpy(out, back, keep * sizeof *out);
    out_n += keep;
    for (int i = keep; i < n; i++) dropped[n_dropped++] = back[i];
    taskEXIT_CRITICAL(&out_lock);
    kw_free(drop_status);
    for (int i = 0; i < n_dropped; i++) kw_free(dropped[i]);
}

const char *fleet_responses_topic(void) { return responses_topic; }
const char *fleet_status_topic(void) { return status_topic; }
