// Just enough of ESP-IDF and FreeRTOS for kernels.inc to compile on the host, for test_att.c.
// Nothing here runs: the tests call the kernels directly, on one thread.
#pragma once
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

typedef void (*TaskFunction_t)(void *);
typedef void *TaskHandle_t, *SemaphoreHandle_t;
typedef int BaseType_t;
typedef unsigned UBaseType_t;
typedef int portMUX_TYPE;
#define pdPASS 1
#define portMAX_DELAY 0xffffffffu
#define portMUX_INITIALIZER_UNLOCKED 0
#define portENTER_CRITICAL(m) ((void)(m))
#define portEXIT_CRITICAL(m) ((void)(m))
#define MALLOC_CAP_INTERNAL 1
#define MALLOC_CAP_SPIRAM 2

static inline BaseType_t xTaskCreatePinnedToCore(TaskFunction_t f, const char *n, uint32_t s, void *a, UBaseType_t p,
                                                 TaskHandle_t *h, BaseType_t c)
{ (void)f; (void)n; (void)s; (void)a; (void)p; (void)h; (void)c; return 0; }
static inline SemaphoreHandle_t xSemaphoreCreateBinary(void) { return NULL; }
static inline BaseType_t xSemaphoreTake(SemaphoreHandle_t s, uint32_t t) { (void)s; (void)t; return 1; }
static inline BaseType_t xSemaphoreGive(SemaphoreHandle_t s) { (void)s; return 1; }
static inline size_t heap_caps_get_free_size(uint32_t caps) { (void)caps; return 0; }
static inline void *heap_caps_aligned_alloc(size_t a, size_t n, uint32_t caps) { (void)caps; return aligned_alloc(a, (n + a - 1) / a * a); }
static inline uint32_t esp_cpu_get_cycle_count(void) { return 0; }
static inline int xPortGetCoreID(void) { return 0; }
static inline int64_t esp_timer_get_time(void) { return 0; }

static inline int32_t esp_nn_dot_s8_aligned_esp32s3(const int8_t *a, const int8_t *b, int32_t len)
{
    int32_t s = 0;
    for (int32_t i = 0; i < len; i++) s += a[i] * b[i];
    return s;
}
