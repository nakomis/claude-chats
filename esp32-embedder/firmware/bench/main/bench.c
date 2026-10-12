// HOME-432 spike: how many int8 MAC/s can an ESP32-S3 sustain on an mxbai-sized
// FFN matrix (4096 x 1024)? Throwaway benchmark.
#include <stdio.h>
#include <string.h>

#include "esp_heap_caps.h"
#include "esp_nn.h"
#include "esp_random.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"

#define ROWS   4096          // output channels (FFN up-projection)
#define COLS   1024          // input width (mxbai hidden size)
#define TILE   32            // rows per tile: 32 KB of weights
#define TOKENS 64            // batch size (tokens sharing each tile)

static int8_t *w_psram;      // full matrix, 4 MB, PSRAM
static int8_t *in;           // TOKENS x COLS, internal
static int32_t *bias;

static void fc(const int8_t *x, const int8_t *w, int rows, int8_t *out)
{
    esp_nn_fully_connected_s8(x, 0, COLS, w, 0, bias, out, rows, 0, -10, 1 << 30, -128, 127);
}

static void *ialloc(size_t n) { return heap_caps_aligned_alloc(16, n, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT); }

static double gmacs(double macs, int64_t us) { return macs / us / 1000.0; }

// Weight-stationary batch kernel straight from PSRAM, CHUNK rows per call.
#define CHUNK 256
static int32_t *pc_shift, *pc_mult;
typedef struct { int r0, r1; int8_t *out; SemaphoreHandle_t done; } bjob_t;
static void run_batch(bjob_t *j)
{
    for (int r = j->r0; r < j->r1; r += CHUNK)
        esp_nn_fully_connected_per_ch_s8_batch(in, 0, COLS, w_psram + (size_t)r * COLS, 0, bias,
                                               j->out, CHUNK, 0, pc_shift, pc_mult, -128, 127, TOKENS);
}
static void batch_task(void *arg) { bjob_t *j = arg; run_batch(j); xSemaphoreGive(j->done); vTaskDelete(NULL); }

// Batched layer over a range of tiles: copy each tile once, reuse for all tokens.
typedef struct { int t0, t1; int8_t *tile, *out; SemaphoreHandle_t done; int64_t us; } job_t;

static void run_tiles(job_t *j)
{
    int64_t t = esp_timer_get_time();
    for (int k = j->t0; k < j->t1; k++) {
        memcpy(j->tile, w_psram + (size_t)k * TILE * COLS, TILE * COLS);
        for (int tok = 0; tok < TOKENS; tok++)
            fc(in + tok * COLS, j->tile, TILE, j->out);
    }
    j->us = esp_timer_get_time() - t;
}

static void tile_task(void *arg)
{
    job_t *j = arg;
    run_tiles(j);
    xSemaphoreGive(j->done);
    vTaskDelete(NULL);
}

void app_main(void)
{
    vTaskDelay(pdMS_TO_TICKS(2000));
    printf("\n== HOME-432 S3 int8 matmul bench ==\n");
    printf("free internal %u, PSRAM %u\n", (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL),
           (unsigned)heap_caps_get_free_size(MALLOC_CAP_SPIRAM));

    w_psram = heap_caps_aligned_alloc(16, (size_t)ROWS * COLS, MALLOC_CAP_SPIRAM);
    in = ialloc(TOKENS * COLS);
    bias = ialloc(ROWS * sizeof(int32_t));
    int8_t *tile = ialloc(TILE * COLS), *tile2 = ialloc(TILE * COLS);
    int8_t *out = ialloc(ROWS), *out2 = ialloc(ROWS);
    if (!w_psram || !in || !bias || !tile || !tile2 || !out || !out2) { printf("alloc failed\n"); return; }
    pc_shift = ialloc(ROWS * 4); pc_mult = ialloc(ROWS * 4);
    int8_t *bout = ialloc(TOKENS * CHUNK), *bout2 = ialloc(TOKENS * CHUNK);
    if (!pc_shift || !pc_mult || !bout || !bout2) { printf("alloc failed (batch)\n"); return; }
    for (int i = 0; i < ROWS; i++) { pc_shift[i] = -10; pc_mult[i] = 1 << 30; }
    esp_fill_random(w_psram, (size_t)ROWS * COLS);
    esp_fill_random(in, TOKENS * COLS);
    memset(bias, 0, ROWS * sizeof(int32_t));
    memcpy(tile, w_psram, TILE * COLS);

  for (;;) {   // repeat, so a late-attaching serial monitor still sees a run
    // 1. peak: weights already in internal RAM
    int reps = 2000;
    int64_t t = esp_timer_get_time();
    for (int r = 0; r < reps; r++) fc(in, tile, TILE, out);
    int64_t us = esp_timer_get_time() - t;
    printf("1 peak (SRAM weights, 1 core):        %.2f GMAC/s\n", gmacs((double)reps * TILE * COLS, us));

    // 2. one token, weights streamed straight from PSRAM (memory-bound case)
    t = esp_timer_get_time();
    fc(in, w_psram, ROWS, out);
    us = esp_timer_get_time() - t;
    printf("2 one token from PSRAM (1 core):      %.2f GMAC/s  (%.1f MB/s of weights, %lld ms)\n",
           gmacs((double)ROWS * COLS, us), (double)ROWS * COLS / us, us / 1000);

    // 3. batched: tile copied once, reused for TOKENS tokens
    job_t j = { 0, ROWS / TILE, tile, out, NULL, 0 };
    run_tiles(&j);
    double macs = (double)ROWS * COLS * TOKENS;
    printf("3 batch %d tokens, tiled (1 core):     %.2f GMAC/s  (%lld ms per layer-matrix)\n",
           TOKENS, gmacs(macs, j.us), j.us / 1000);

    // 4. same, split across both cores
    job_t a = { 0, ROWS / TILE / 2, tile, out, xSemaphoreCreateBinary(), 0 };
    job_t b = { ROWS / TILE / 2, ROWS / TILE, tile2, out2, xSemaphoreCreateBinary(), 0 };
    t = esp_timer_get_time();
    xTaskCreatePinnedToCore(tile_task, "c1", 4096, &b, 5, NULL, 1);   // core 1 first:
    xTaskCreatePinnedToCore(tile_task, "c0", 4096, &a, 5, NULL, 0);   // c0 preempts us
    xSemaphoreTake(a.done, portMAX_DELAY);
    xSemaphoreTake(b.done, portMAX_DELAY);
    us = esp_timer_get_time() - t;
    printf("4 batch %d tokens, tiled (2 cores):    %.2f GMAC/s  (%lld ms per layer-matrix)\n",
           TOKENS, gmacs(macs, us), us / 1000);

    // 5/6. weight-stationary batch kernel, weights read from PSRAM through the cache
    bjob_t c = { 0, ROWS, bout, NULL };
    t = esp_timer_get_time();
    run_batch(&c);
    us = esp_timer_get_time() - t;
    printf("5 batch kernel from PSRAM (1 core):   %.2f GMAC/s  (%lld ms)\n", gmacs(macs, us), us / 1000);
    bjob_t d0 = { 0, ROWS / 2, bout, xSemaphoreCreateBinary() };
    bjob_t d1 = { ROWS / 2, ROWS, bout2, xSemaphoreCreateBinary() };
    t = esp_timer_get_time();
    xTaskCreatePinnedToCore(batch_task, "b1", 4096, &d1, 5, NULL, 1);
    xTaskCreatePinnedToCore(batch_task, "b0", 4096, &d0, 5, NULL, 0);
    xSemaphoreTake(d0.done, portMAX_DELAY);
    xSemaphoreTake(d1.done, portMAX_DELAY);
    us = esp_timer_get_time() - t;
    vSemaphoreDelete(d0.done); vSemaphoreDelete(d1.done);
    printf("6 batch kernel from PSRAM (2 cores):  %.2f GMAC/s  (%lld ms)\n", gmacs(macs, us), us / 1000);

    // what that means for mxbai-large (~302M weight MACs per token)
    double g = gmacs(macs, us);   // best case measured last (6)
    printf("=> ~%.1f s per 100-token message, ~%.1f h for 900 messages/day\n",
           302e6 * 100 / (g * 1e9), 302e6 * 100 * 900 / (g * 1e9) / 3600);
    printf("== done ==\n");
    vSemaphoreDelete(a.done);
    vSemaphoreDelete(b.done);
    vTaskDelay(pdMS_TO_TICKS(3000));
  }
}
