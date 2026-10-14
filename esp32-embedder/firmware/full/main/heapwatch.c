// Where internal RAM runs lowest (heapwatch.h, CHATS-5).
#include "heapwatch.h"

#include <stdio.h>

#include "esp_heap_caps.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "esp_timer.h"

static const char *const names[HW_N] = { "creds", "sqs", "mqtt_tls", "pass", "idle", "iotpub" };

static SemaphoreHandle_t mx;             // the worker's task, MQTT's, and the heartbeat's
static int paused, connected, creds, pass, iotpub;
static hw_stage_t stage;
static size_t low[HW_N];                 // 0: the stage hasn't been seen
static size_t boot_low;                  // IDF's lowest ever before the watch started (the model's load)
static size_t overall;

static size_t window_low(void) { return heap_caps_get_minimum_free_size(MALLOC_CAP_INTERNAL); }

// ---- the heap map (CHATS-13 spike): which blocks split internal RAM -------------------------------
// The walker holds the heap's lock while it calls back, so the callback only records (into a buffer
// taken from PSRAM at init); the printing comes after.
#define MAP_MAX 400
#define MAP_EVERY_US (5LL * 60 * 1000000)
typedef struct { uint32_t addr, size; bool used; } map_block_t;
static map_block_t *map;
static int map_n, map_heaps;
static uint32_t map_heap_start[8], map_heap_end[8];
static size_t largest_low = SIZE_MAX;
static int64_t last_map_us;

static bool map_cb(walker_heap_into_t h, walker_block_info_t b, void *u)
{
    (void)u;
    if (map_heaps == 0 || map_heap_start[map_heaps - 1] != (uint32_t)h.start) {
        if (map_heaps < 8) { map_heap_start[map_heaps] = (uint32_t)h.start; map_heap_end[map_heaps] = (uint32_t)h.end; }
        map_heaps++;
    }
    if (map_n < MAP_MAX) map[map_n++] = (map_block_t){ (uint32_t)(uintptr_t)b.ptr, (uint32_t)b.size, b.used };
    return true;
}

// One line per heap: "u<size>" used, "F<size>" free (sizes in bytes), in address order, runs of small
// used blocks folded into "u<n>x<total>" so the line stays readable.
static void map_print(const char *why, size_t largest)
{
    map_n = map_heaps = 0;
    heap_caps_walk(MALLOC_CAP_INTERNAL, map_cb, NULL);
    printf("heapmap: %s: largest free %u, internal free %u, %d blocks%s\n", why, (unsigned)largest,
           (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL), map_n, map_n == MAP_MAX ? " (truncated)" : "");
    int h = -1;
    for (int i = 0; i < map_n;) {
        int hi = 0;
        while (hi < 8 && hi < map_heaps && !(map[i].addr >= map_heap_start[hi] && map[i].addr < map_heap_end[hi])) hi++;
        if (hi != h) {
            if (h >= 0) printf("\n");
            h = hi;
            if (hi < 8) printf("heapmap: heap %08lx-%08lx:", (unsigned long)map_heap_start[hi], (unsigned long)map_heap_end[hi]);
        }
        if (!map[i].used) {
            printf(" F%lu@%05lx", (unsigned long)map[i].size, (unsigned long)(map[i].addr & 0xfffff));
            i++;
            continue;
        }
        // a run of used blocks: fold the small ones
        int j = i;
        uint32_t total = 0;
        while (j < map_n && map[j].used && map[j].size < 1024) { total += map[j].size; j++; }
        if (j - i >= 3) { printf(" u%dx%lu", j - i, (unsigned long)total); i = j; continue; }
        printf(" u%lu", (unsigned long)map[i].size);
        i++;
    }
    printf("\n");
}

static void map_check(void)
{
    if (!map) return;
    size_t largest = heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL);
    int64_t now = esp_timer_get_time();
    if (largest < largest_low && now - last_map_us >= MAP_EVERY_US) {
        largest_low = largest;
        last_map_us = now;
        map_print("new low for the largest free block", largest);
    }
}

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
    map_check();
    stage = paused ? (creds ? HW_CREDS : iotpub ? HW_IOTPUB : HW_SQS) : !connected ? HW_MQTT_TLS : pass ? HW_PASS : HW_IDLE;
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
    map = heap_caps_malloc(MAP_MAX * sizeof *map, MALLOC_CAP_SPIRAM);
    stage = HW_MQTT_TLS;                 // not connected yet: the first handshake is MQTT's
    heap_caps_monitor_local_minimum_free_size_start();
}

void heapwatch_paused(int v) { set(&paused, v); }
void heapwatch_connected(int v) { set(&connected, v); }
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

void heapwatch_map(const char *why)
{
    if (!mx || !map) return;
    xSemaphoreTake(mx, portMAX_DELAY);
    size_t largest = heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL);
    if (largest < largest_low) largest_low = largest;
    last_map_us = esp_timer_get_time();
    map_print(why, largest);
    xSemaphoreGive(mx);
}
