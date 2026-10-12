// WROOM-32 (original ESP32) int8 matmul speed: no SIMD, everything in internal RAM.
// Throwaway HOME-431 spike.
#include <stdio.h>
#include <stdint.h>
#include "esp_heap_caps.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"

#define IN   1024
#define ROWS 48          // 48 KB of weights
#define TOK  8           // 8 KB of inputs
#define REPS 20

static int8_t *w, *x;
static int32_t out[2][TOK][ROWS];
static SemaphoreHandle_t done[2];

static int32_t dot(const int8_t *a, const int8_t *b)
{
    int32_t s0 = 0, s1 = 0, s2 = 0, s3 = 0;
    for (int i = 0; i < IN; i += 4) {
        s0 += a[i] * b[i]; s1 += a[i + 1] * b[i + 1];
        s2 += a[i + 2] * b[i + 2]; s3 += a[i + 3] * b[i + 3];
    }
    return s0 + s1 + s2 + s3;
}

// dot of 32-bit words: 4 int8 per load, unpacked with shifts
static int32_t dot_w(const int8_t *a, const int8_t *b)
{
    const int32_t *pa = (const int32_t *)a, *pb = (const int32_t *)b;
    int32_t s = 0;
    for (int i = 0; i < IN / 4; i++) {
        int32_t u = pa[i], v = pb[i];
        s += (int8_t)u * (int8_t)v + (int8_t)(u >> 8) * (int8_t)(v >> 8)
           + (int8_t)(u >> 16) * (int8_t)(v >> 16) + (u >> 24) * (v >> 24);
    }
    return s;
}

static volatile int use_w;
static void work(int c, int r0, int r1)
{
    for (int rep = 0; rep < REPS; rep++)
        for (int r = r0; r < r1; r++)
            for (int t = 0; t < TOK; t++)
                out[c][t][r] = use_w ? dot_w(x + t * IN, w + r * IN) : dot(x + t * IN, w + r * IN);
}

static void task(void *arg)
{
    int c = (int)(intptr_t)arg;
    work(c, c * ROWS / 2, (c + 1) * ROWS / 2);
    xSemaphoreGive(done[c]);
    vTaskDelete(NULL);
}

void app_main(void)
{
    vTaskDelay(pdMS_TO_TICKS(1000));
    printf("\n== HOME-431: WROOM-32 int8 dot speed ==\n");
    w = heap_caps_aligned_alloc(4, ROWS * IN, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    x = heap_caps_aligned_alloc(4, TOK * IN, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    if (!w || !x) { printf("alloc failed\n"); return; }
    for (int i = 0; i < ROWS * IN; i++) w[i] = (int8_t)((i * 37) % 255 - 127);
    for (int i = 0; i < TOK * IN; i++) x[i] = (int8_t)((i * 91) % 255 - 127);
    done[0] = xSemaphoreCreateBinary(); done[1] = xSemaphoreCreateBinary();
    printf("free internal: %u\n", (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT));
    for (;;) {
        for (use_w = 0; use_w < 2; use_w++) {
            const char *nm = use_w ? "word loads" : "byte loads";
            double macs = (double)REPS * ROWS * TOK * IN;
            int64_t t = esp_timer_get_time();
            work(0, 0, ROWS);
            t = esp_timer_get_time() - t;
            printf("%s, 1 core : %.1f MMAC/s\n", nm, macs / t);
            t = esp_timer_get_time();
            xTaskCreatePinnedToCore(task, "c1", 4096, (void *)1, 5, NULL, 1);
            xTaskCreatePinnedToCore(task, "c0", 4096, (void *)0, 5, NULL, 0);
            xSemaphoreTake(done[0], portMAX_DELAY); xSemaphoreTake(done[1], portMAX_DELAY);
            t = esp_timer_get_time() - t;
            printf("%s, 2 cores: %.1f MMAC/s  (check %ld)\n", nm, macs / t, (long)out[0][3][5]);
        }
        vTaskDelay(pdMS_TO_TICKS(3000));
    }
}
