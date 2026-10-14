// Where internal RAM runs lowest (heapwatch.h, CHATS-5).
#include "heapwatch.h"

#include <stdio.h>

#include "esp_heap_caps.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"

static const char *const names[HW_N] = { "creds", "sqs", "iotpub", "pass", "idle" };

static SemaphoreHandle_t mx;             // the worker's task, MQTT's, and the heartbeat's
static int paused, creds, iotpub, pass;
static hw_stage_t stage;
static size_t low[HW_N];                 // 0: the stage hasn't been seen
static size_t boot_low;                  // IDF's lowest ever before the watch started (the model's load)
static size_t overall;

static size_t window_low(void) { return heap_caps_get_minimum_free_size(MALLOC_CAP_INTERNAL); }

// Close the current stage's window (under mx) and start the next one's. Starting the monitor again
// while it runs just measures from now (IDF 5.4's heap_caps.c): never stop it, as the next start
// would allocate, and asserts if internal RAM is short, which is when this matters.
static void restage(void)
{
    size_t l = window_low();
    if (!low[stage] || l < low[stage]) low[stage] = l;
    if (l < overall) {
        overall = l;
        printf("heapwatch: new internal low %u in %s\n", (unsigned)l, names[stage]);
    }
    stage = paused ? (creds ? HW_CREDS : iotpub ? HW_IOTPUB : HW_SQS) : pass ? HW_PASS : HW_IDLE;
    heap_caps_monitor_local_minimum_free_size_start();
}

static void set(int *flag, int v)
{
    if (!mx) return;
    xSemaphoreTake(mx, portMAX_DELAY);
    if (*flag != !!v) {
        *flag = !!v;
        restage();
    }
    xSemaphoreGive(mx);
}

void heapwatch_init(void)
{
    if (mx) return;
    boot_low = overall = window_low();
    mx = xSemaphoreCreateMutex();
    stage = HW_IDLE;
    heap_caps_monitor_local_minimum_free_size_start();
}

void heapwatch_paused(int v) { set(&paused, v); }
void heapwatch_creds(int v) { set(&creds, v); }
void heapwatch_pass(int v) { set(&pass, v); }
void heapwatch_iotpub(int v) { set(&iotpub, v); }

size_t heapwatch_lowest(void)
{
    if (!mx) return window_low();
    xSemaphoreTake(mx, portMAX_DELAY);
    size_t l = window_low();
    if (overall < l) l = overall;
    xSemaphoreGive(mx);
    return l;
}

void heapwatch_json(jbuf_t *b)
{
    if (!mx) return;
    xSemaphoreTake(mx, portMAX_DELAY);
    size_t now_low = window_low();       // the open window counts too
    jb_printf(b, ",\"heap_low\":{\"boot\":%u", (unsigned)boot_low);
    for (int i = 0; i < HW_N; i++) {
        size_t l = low[i];
        if (i == (int)stage && (!l || now_low < l)) l = now_low;
        if (l) jb_printf(b, ",\"%s\":%u", names[i], (unsigned)l);
    }
    jb_putc(b, '}');
    xSemaphoreGive(mx);
}
