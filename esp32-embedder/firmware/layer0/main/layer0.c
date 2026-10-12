// HOME-432 step 2: mxbai encoder layer 0 on the ESP32-S3, checked against numpy.
// Weights int8 per row (flash partition, read through the cache), matmul
// inputs int8 per token except K outlier dims kept fp32; the rest in fp32.
// Throwaway spike code.
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "esp_heap_caps.h"
#include "esp_partition.h"
#include "esp_timer.h"
#include "driver/gpio.h"
#include "driver/sdmmc_host.h"
#include "sdmmc_cmd.h"
#include "esp_cpu.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"

extern int32_t esp_nn_dot_s8_aligned_esp32s3(const int8_t *a, const int8_t *b, int32_t len);

#define D      1024
#define FF     4096
#define HEADS  16
#define HD     64
#define XQ_MAX (176 * 1024)   // int8 input chunk kept in internal RAM; also the attention scratch

typedef struct {
    const int8_t *w; const float *ws, *b; const uint16_t *oi; const float *wo;
    int in, out, act;         // act: apply GELU to the output
} mat_t;

static const uint8_t *blob;
static const uint32_t *off;
static int n_tok, K;
#define SEC(i) ((const void *)(blob + off[i]))

static int8_t *xq;            // [tc][in], internal
static float *xs, *xo;        // per token scale; [tc][K] outlier values

typedef struct { int core, t, r; int32_t pie, ref, again; uintptr_t xa, wa; } dbg_t;
static dbg_t dbg[32];
static volatile int dbg_n, dbg_on, mask_irq, one_core;

typedef struct { const mat_t *m; int r0, r1, t0, tc; float *y; SemaphoreHandle_t done; } job_t;

// PIE only ever reads internal SRAM: each core copies a block of weight rows out of
// PSRAM with ordinary loads first. (PIE loads from both cores at once through the
// shared external-memory cache returned occasional garbage dots.)
#define WBUF (32 * 1024)
static int8_t *wbuf[2];
static int64_t cyc_cp[2], cyc_pie[2];

// GELU (tanh form) by linear interpolation in a table over [-6, 6]; |error| < 4e-6
#define GL_N 2048
#define GL_LIM 6.0f
static float glut[GL_N + 1];
static void gelu_init(void)
{
    for (int i = 0; i <= GL_N; i++) {
        float z = -GL_LIM + 2 * GL_LIM * i / GL_N;
        glut[i] = 0.5f * z * (1.0f + tanhf(0.7978845608f * (z + 0.044715f * z * z * z)));
    }
}
static inline float gelu_fast(float z)
{
    if (z >= GL_LIM) return z;
    if (z <= -GL_LIM) return 0;
    float f = (z + GL_LIM) * (GL_N / (2 * GL_LIM));
    int i = (int)f;
    return glut[i] + (f - i) * (glut[i + 1] - glut[i]);
}

static void mm_rows(job_t *j)
{
    const mat_t *m = j->m;
    int8_t *wb = wbuf[xPortGetCoreID()];
    int rb = WBUF / m->in;
    float yb[WBUF / D];                       // one token's outputs for this row block
    for (int rb0 = j->r0; rb0 < j->r1; rb0 += rb) {
        int rb1 = rb0 + rb < j->r1 ? rb0 + rb : j->r1, n = rb1 - rb0;
        int core = xPortGetCoreID();
        uint32_t c0 = esp_cpu_get_cycle_count();
        memcpy(wb, m->w + (size_t)rb0 * m->in, (size_t)n * m->in);
        uint32_t c1 = esp_cpu_get_cycle_count();
        cyc_cp[core] += c1 - c0;
        float sw[WBUF / D], bb[WBUF / D], wo[WBUF / D * 8];
        for (int r = 0; r < n; r++) {
            sw[r] = m->ws[rb0 + r]; bb[r] = m->b[rb0 + r];
            memcpy(wo + r * K, m->wo + (size_t)(rb0 + r) * K, K * sizeof(float));
        }
        for (int t = 0; t < j->tc; t++) {
            const int8_t *xr = xq + (size_t)t * m->in;
            const float *o = xo + (j->t0 + t) * K;
            float s = xs[j->t0 + t];
            for (int r = 0; r < n; r++) {
                int32_t acc = esp_nn_dot_s8_aligned_esp32s3(xr, wb + (size_t)r * m->in, m->in);
                float v = acc * s * sw[r] + bb[r];
                const float *w8 = wo + r * K;
                for (int i = 0; i < K; i++) v += o[i] * w8[i];
                yb[r] = m->act ? gelu_fast(v) : v;
            }
            memcpy(j->y + (size_t)(j->t0 + t) * m->out + rb0, yb, n * sizeof(float));
        }
        cyc_pie[core] += esp_cpu_get_cycle_count() - c1;
    }
}

static void mm_task(void *arg) { job_t *j = arg; mm_rows(j); xSemaphoreGive(j->done); vTaskDelete(NULL); }

// y[n][out] = x[n][in] . W^T + b, both cores, token chunks sized to fit xq
// Inputs quantised once per matmul (both cores) into xq_all in PSRAM; each token
// chunk is then copied into the internal xq for PIE.
static int8_t *xq_all;
typedef struct { const mat_t *m; const float *x; int t0, t1; SemaphoreHandle_t done; } qjob_t;

static void quant_rows(qjob_t *q)
{
    const mat_t *m = q->m;
    for (int t = q->t0; t < q->t1; t++) {
        const float *xr = q->x + (size_t)t * m->in;
        float mx = 0;
        int prev = 0;
        for (int i = 0; i <= K; i++) {            // max |x| over the gaps between outlier dims
            int end = i < K ? m->oi[i] : m->in;
            for (int j = prev; j < end; j++) { float a = fabsf(xr[j]); mx = a > mx ? a : mx; }
            if (i < K) xo[t * K + i] = xr[end];
            prev = end + 1;
        }
        float s = mx > 0 ? mx / 127.0f : 1.0f, inv = 1.0f / s;
        xs[t] = s;
        int8_t qb[FF];                            // build the row in internal RAM, one copy out to PSRAM
        for (int j = 0; j < m->in; j++) {
            int r;
            __asm__("round.s %0, %1, 0" : "=a"(r) : "f"(xr[j] * inv));   // FPU round to nearest
            qb[j] = (int8_t)r;
        }
        for (int i = 0; i < K; i++) qb[m->oi[i]] = 0;
        memcpy(xq_all + (size_t)t * m->in, qb, m->in);
    }
}

static void quant_task(void *arg) { qjob_t *q = arg; quant_rows(q); xSemaphoreGive(q->done); vTaskDelete(NULL); }

static int64_t t_quant;
static void quantise(const mat_t *m, const float *x)
{
    static SemaphoreHandle_t s0, s1;
    if (!s0) { s0 = xSemaphoreCreateBinary(); s1 = xSemaphoreCreateBinary(); }
    int64_t t = esp_timer_get_time();
    qjob_t a = { m, x, 0, n_tok / 2, s0 }, b = { m, x, n_tok / 2, n_tok, s1 };
    xTaskCreatePinnedToCore(quant_task, "q1", 8192, &b, 5, NULL, 1);
    xTaskCreatePinnedToCore(quant_task, "q0", 8192, &a, 5, NULL, 0);
    xSemaphoreTake(s0, portMAX_DELAY);
    xSemaphoreTake(s1, portMAX_DELAY);
    t_quant += esp_timer_get_time() - t;
}

static void matmul_r(const mat_t *m, float *y, int r0, int r1)
{
    int maxc = XQ_MAX / m->in, passes = (n_tok + maxc - 1) / maxc;
    int chunk = (n_tok + passes - 1) / passes;
    static SemaphoreHandle_t s0, s1;
    if (!s0) { s0 = xSemaphoreCreateBinary(); s1 = xSemaphoreCreateBinary(); }
    for (int t0 = 0; t0 < n_tok; t0 += chunk) {
        int tc = n_tok - t0 < chunk ? n_tok - t0 : chunk;
        memcpy(xq, xq_all + (size_t)t0 * m->in, (size_t)tc * m->in);
        int rm = (r0 + r1) / 2;
        job_t a = { m, r0, rm, t0, tc, y, s0 }, b = { m, rm, r1, t0, tc, y, s1 };
        xTaskCreatePinnedToCore(mm_task, "m1", 4096, &b, 5, NULL, 1);   // core 1 first
        xTaskCreatePinnedToCore(mm_task, "m0", 4096, &a, 5, NULL, 0);
        xSemaphoreTake(s0, portMAX_DELAY);
        xSemaphoreTake(s1, portMAX_DELAY);
    }
}

// Weights staged flash -> PSRAM in blocks of <= STAGE bytes before the matmul (the
// real thing streams SD -> PSRAM). Two cores reading flash-mapped weights with PIE
// loads at once returns occasional garbage dots; PSRAM is clean.
#define STAGE (2 * 1024 * 1024)
static int8_t *stage;
static int64_t t_copy;
static void matmul_staged(const mat_t *m, const float *x, float *y)
{
    quantise(m, x);
    int rb = STAGE / m->in;
    for (int r0 = 0; r0 < m->out; r0 += rb) {
        int r1 = r0 + rb < m->out ? r0 + rb : m->out;
        int64_t t = esp_timer_get_time();
        memcpy(stage, m->w + (size_t)r0 * m->in, (size_t)(r1 - r0) * m->in);
        t_copy += esp_timer_get_time() - t;
        mat_t ms = *m;
        ms.w = stage - (size_t)r0 * m->in;    // so ms.w + r * in lands in the stage for r in [r0, r1)
        matmul_r(&ms, y, r0, r1);
    }
}

static void layernorm(float *x, const float *w, const float *b)
{
    for (int t = 0; t < n_tok; t++) {
        float *r = x + (size_t)t * D, m = 0, v = 0;
        for (int j = 0; j < D; j++) m += r[j];
        m /= D;
        for (int j = 0; j < D; j++) { float d = r[j] - m; v += d * d; }
        float inv = 1.0f / sqrtf(v / D + 1e-12f);
        for (int j = 0; j < D; j++) r[j] = (r[j] - m) * inv * w[j] + b[j];
    }
}

// heads split across the cores; each head's q, k, v copied to internal RAM first
typedef struct { const float *q, *k, *v; float *o, *scr, *sc; int h0, h1; SemaphoreHandle_t done; } att_t;

// e^x for x <= 0: 2^(x log2 e) split into integer and fraction, degree-6 series; rel err < 1e-5
static inline float fast_exp(float x)
{
    if (x < -87.0f) return 0;
    float t = x * 1.442695041f;
    int ki = (int)t; if ((float)ki > t) ki--;
    float f = t - ki;
    float p = 1.0f + f * (0.693147181f + f * (0.240226507f + f * (0.0555041087f + f * (0.00961812911f
                    + f * (0.00133335581f + f * 0.000154035304f)))));
    union { float f; int32_t i; } u = { .f = p };
    u.i += ki << 23;
    return u.f;
}

static void att_heads(att_t *a)
{
    int n = n_tok;
    float *qh = a->scr, *kh = qh + n * HD, *vh = kh + n * HD, *sc = a->sc;
    for (int h = a->h0; h < a->h1; h++) {
        for (int i = 0; i < n; i++) {
            memcpy(qh + i * HD, a->q + (size_t)i * D + h * HD, HD * sizeof(float));
            memcpy(kh + i * HD, a->k + (size_t)i * D + h * HD, HD * sizeof(float));
            memcpy(vh + i * HD, a->v + (size_t)i * D + h * HD, HD * sizeof(float));
        }
        for (int i = 0; i < n; i++) {
            const float *qi = qh + i * HD;
            float mx = -1e30f;
            int j = 0;
            for (; j + 4 <= n; j += 4) {              // four keys at once: q[d] loaded once
                const float *k0 = kh + j * HD, *k1 = k0 + HD, *k2 = k1 + HD, *k3 = k2 + HD;
                float s0 = 0, s1 = 0, s2 = 0, s3 = 0;
                for (int d = 0; d < HD; d++) {
                    float qd = qi[d];
                    s0 += qd * k0[d]; s1 += qd * k1[d]; s2 += qd * k2[d]; s3 += qd * k3[d];
                }
                sc[j] = s0 * 0.125f; sc[j + 1] = s1 * 0.125f; sc[j + 2] = s2 * 0.125f; sc[j + 3] = s3 * 0.125f;
            }
            for (; j < n; j++) {
                const float *kj = kh + j * HD;
                float s = 0;
                for (int d = 0; d < HD; d++) s += qi[d] * kj[d];
                sc[j] = s * 0.125f;
            }
            for (j = 0; j < n; j++) if (sc[j] > mx) mx = sc[j];
            float sum = 0;
            for (j = 0; j < n; j++) { sc[j] = fast_exp(sc[j] - mx); sum += sc[j]; }
            float inv = 1.0f / sum, *oi = a->o + (size_t)i * D + h * HD, acc[HD];
            for (int d = 0; d < HD; d += 8) {          // eight output dims held in registers
                float a0 = 0, a1 = 0, a2 = 0, a3 = 0, a4 = 0, a5 = 0, a6 = 0, a7 = 0;
                const float *vp = vh + d;
                for (j = 0; j < n; j++, vp += HD) {
                    float w = sc[j];
                    a0 += w * vp[0]; a1 += w * vp[1]; a2 += w * vp[2]; a3 += w * vp[3];
                    a4 += w * vp[4]; a5 += w * vp[5]; a6 += w * vp[6]; a7 += w * vp[7];
                }
                acc[d] = a0 * inv; acc[d + 1] = a1 * inv; acc[d + 2] = a2 * inv; acc[d + 3] = a3 * inv;
                acc[d + 4] = a4 * inv; acc[d + 5] = a5 * inv; acc[d + 6] = a6 * inv; acc[d + 7] = a7 * inv;
            }
            memcpy(oi, acc, sizeof acc);
        }
    }
}

static void att_task(void *arg) { att_t *a = arg; att_heads(a); xSemaphoreGive(a->done); vTaskDelete(NULL); }

static float *att_scr[2], *att_sc[2];
static void attention(const float *q, const float *k, const float *v, float *o)
{
    static SemaphoreHandle_t s0, s1;
    if (!s0) { s0 = xSemaphoreCreateBinary(); s1 = xSemaphoreCreateBinary(); }
    att_t a = { q, k, v, o, att_scr[0], att_sc[0], 0, HEADS / 2, s0 };
    att_t b = { q, k, v, o, att_scr[1], att_sc[1], HEADS / 2, HEADS, s1 };
    xTaskCreatePinnedToCore(att_task, "a1", 4096, &b, 5, NULL, 1);
    xTaskCreatePinnedToCore(att_task, "a0", 4096, &a, 5, NULL, 0);
    xSemaphoreTake(s0, portMAX_DELAY);
    xSemaphoreTake(s1, portMAX_DELAY);
}

static double cosv(const float *a, const float *b, int n)
{
    double ab = 0, aa = 0, bb = 0;
    for (int i = 0; i < n; i++) { ab += (double)a[i] * b[i]; aa += (double)a[i] * a[i]; bb += (double)b[i] * b[i]; }
    return ab / sqrt(aa * bb);
}

// ---- self-test helpers (file scope: nested functions need stack trampolines) ----
#define ST_ROWS 1024
static const mat_t *st_m;
static const int8_t *st_w;
static volatile int st_bad[2];
static SemaphoreHandle_t st_d0, st_d1;

static void st_check(int c)
{
    int nb = 0;
    for (int pass = 0; pass < 20; pass++)
        for (int r = c; r < ST_ROWS; r += 2) {
            const int8_t *wr = st_w + (size_t)r * st_m->in;
            int32_t ref = 0;
            for (int j = 0; j < st_m->in; j++) ref += xq[j] * wr[j];
            if (esp_nn_dot_s8_aligned_esp32s3(xq, wr, st_m->in) != ref) nb++;
        }
    st_bad[c] = nb;
}

static void st_task(void *arg)
{
    int c = (int)(intptr_t)arg;
    st_check(c);
    xSemaphoreGive(c ? st_d1 : st_d0);
    vTaskDelete(NULL);
}

// same scheme as matmul, one core, no PIE
static void matmul_scalar(const mat_t *m, const float *x, float *y)
{
    int8_t *q = malloc(m->in);
    float *o = malloc(K * sizeof(float));
    for (int t = 0; t < n_tok; t++) {
        const float *xr = x + (size_t)t * m->in;
        float mx = 0;
        int oi = 0;
        for (int j = 0; j < m->in; j++) {
            if (oi < K && m->oi[oi] == j) { o[oi++] = xr[j]; continue; }
            if (fabsf(xr[j]) > mx) mx = fabsf(xr[j]);
        }
        float s = mx > 0 ? mx / 127.0f : 1.0f, inv = 1.0f / s;
        oi = 0;
        for (int j = 0; j < m->in; j++) {
            if (oi < K && m->oi[oi] == j) { q[j] = 0; oi++; continue; }
            q[j] = (int8_t)lrintf(xr[j] * inv);
        }
        for (int r = 0; r < m->out; r++) {
            const int8_t *wr = m->w + (size_t)r * m->in;
            int32_t acc = 0;
            for (int j = 0; j < m->in; j++) acc += q[j] * wr[j];
            float v = acc * s * m->ws[r] + m->b[r];
            for (int i = 0; i < K; i++) v += o[i] * m->wo[(size_t)r * K + i];
            y[(size_t)t * m->out + r] = v;
        }
    }
    free(q); free(o);
}

// Read-only SD speed test: raw sector reads, no filesystem, nothing written.
// ESP32-S3 CAM board: 1-bit SDMMC, CLK 39, CMD 38, D0 40 (from BootBoots).
static void sd_bench(void)
{
    sdmmc_host_t host = SDMMC_HOST_DEFAULT();
    host.max_freq_khz = SDMMC_FREQ_HIGHSPEED;
    sdmmc_slot_config_t slot = SDMMC_SLOT_CONFIG_DEFAULT();
    slot.width = 1; slot.clk = 39; slot.cmd = 38; slot.d0 = 40;
    slot.flags |= SDMMC_SLOT_FLAG_INTERNAL_PULLUP;
    static sdmmc_card_t card;
    if (sdmmc_host_init() != ESP_OK || sdmmc_host_init_slot(host.slot, &slot) != ESP_OK ||
        sdmmc_card_init(&host, &card) != ESP_OK) { printf("SD: init failed\n"); sdmmc_host_deinit(); return; }
    sdmmc_card_print_info(stdout, &card);
    const int CH = 64 * 1024, N = 256;            // 16 MB in 64 KB reads
    const char *where[2] = { "internal", "PSRAM" };
    for (int w = 0; w < 2; w++) {
        void *buf = heap_caps_aligned_alloc(64, CH, w ? MALLOC_CAP_SPIRAM : (MALLOC_CAP_DMA | MALLOC_CAP_INTERNAL));
        if (!buf) { printf("SD: buf alloc failed\n"); continue; }
        int64_t t = esp_timer_get_time();
        esp_err_t e = ESP_OK;
        for (int i = 0; i < N && e == ESP_OK; i++) e = sdmmc_read_sectors(&card, buf, 2048 + i * (CH / 512), CH / 512);
        t = esp_timer_get_time() - t;
        printf("SD: 16 MB raw read into %s: %s, %.2f MB/s\n", where[w], esp_err_to_name(e), 16.0 * 1048576 / t);
        free(buf);
    }
    sdmmc_host_deinit();
}

// Which GPIOs sit on an externally pulled net? Input + internal pull-down: reads 1 => external
// pull-up; input + internal pull-up: reads 0 => external pull-down. Read-only.
static void pin_probe(void)
{
    const int pins[] = { 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 21,
                         38, 39, 40, 41, 42, 45, 46, 47 };
    printf("pin probe (pd->1 = pulled up, pu->0 = pulled down):\n");
    for (int i = 0; i < sizeof pins / sizeof pins[0]; i++) {
        int g = pins[i];
        gpio_reset_pin(g);
        gpio_set_direction(g, GPIO_MODE_INPUT);
        gpio_set_pull_mode(g, GPIO_PULLDOWN_ONLY);
        esp_rom_delay_us(2000);
        int pd = gpio_get_level(g);
        gpio_set_pull_mode(g, GPIO_PULLUP_ONLY);
        esp_rom_delay_us(2000);
        int pu = gpio_get_level(g);
        gpio_set_pull_mode(g, GPIO_FLOATING);
        printf("  GPIO%-2d pd->%d pu->%d%s\n", g, pd, pu, pd ? "   <-- pulled up" : (!pu ? "   <-- pulled down" : ""));
    }
}

static void *ps(size_t n) { return heap_caps_aligned_alloc(16, n, MALLOC_CAP_SPIRAM); }

void app_main(void)
{
    vTaskDelay(pdMS_TO_TICKS(1500));
    printf("\n== HOME-432 step 2: mxbai layer 0 on the S3 ==\n");
    pin_probe();
    sd_bench();
    const esp_partition_t *p = esp_partition_find_first(ESP_PARTITION_TYPE_DATA, 0x40, "layer0");
    esp_partition_mmap_handle_t h;
    if (!p || esp_partition_mmap(p, 0, p->size, ESP_PARTITION_MMAP_DATA, (const void **)&blob, &h) != ESP_OK) {
        printf("mmap failed\n");
        return;
    }
    if (memcmp(blob, "MXL0", 4)) { printf("bad blob magic\n"); return; }
    n_tok = ((const uint32_t *)blob)[1];
    K = ((const uint32_t *)blob)[2];
    off = (const uint32_t *)blob + 4;
    printf("%d tokens, K=%d outlier dims\n", n_tok, K);

    const char *names[6] = { "Q", "K", "V", "O", "up", "down" };
    mat_t M[6];
    for (int i = 0; i < 6; i++) {
        int s = i * 5, in = i == 5 ? FF : D, out = i == 4 ? FF : D;
        M[i] = (mat_t){ SEC(s), SEC(s + 1), SEC(s + 2), SEC(s + 3), SEC(s + 4), in, out };
    }
    const float *aon_w = SEC(30), *aon_b = SEC(31), *lon_w = SEC(32), *lon_b = SEC(33);
    const float *x0 = SEC(34), *y_emu = SEC(35), *y_ref = SEC(36);

    xq = heap_caps_aligned_alloc(16, XQ_MAX, MALLOC_CAP_INTERNAL);
    xs = heap_caps_malloc(512 * sizeof(float), MALLOC_CAP_INTERNAL);
    xo = heap_caps_malloc(512 * K * sizeof(float), MALLOC_CAP_INTERNAL);
    size_t nd = (size_t)n_tok * D * sizeof(float);
    float *x = ps(nd), *q = ps(nd), *k = ps(nd), *v = ps(nd), *o = ps(nd), *hbuf = ps((size_t)n_tok * FF * 4);
    float *sc = heap_caps_malloc(512 * sizeof(float), MALLOC_CAP_INTERNAL);
    stage = ps(STAGE);
    xq_all = ps((size_t)n_tok * FF);
    wbuf[0] = heap_caps_aligned_alloc(16, WBUF, MALLOC_CAP_INTERNAL);
    wbuf[1] = heap_caps_aligned_alloc(16, WBUF, MALLOC_CAP_INTERNAL);
    if (!wbuf[0] || !wbuf[1]) { printf("wbuf alloc failed\n"); return; }
    size_t scr = (size_t)3 * n_tok * HD * sizeof(float);
    if (2 * scr > XQ_MAX) { printf("too many tokens for the attention scratch\n"); return; }
    att_scr[0] = (float *)xq;                 // xq is idle during attention
    att_scr[1] = (float *)(xq + scr);
    att_sc[0] = sc;
    att_sc[1] = heap_caps_malloc(n_tok * sizeof(float), MALLOC_CAP_INTERNAL);
    if (!att_scr[1] || !att_sc[1]) { printf("attention alloc failed\n"); return; }
    M[4].act = 1;
    gelu_init();
    printf("internal heap free: %u bytes\n", (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL));
    if (!xq || !xs || !xo || !x || !q || !k || !v || !o || !hbuf || !sc || !stage || !xq_all) { printf("alloc failed\n"); return; }

  for (;;) {
    memcpy(x, x0, nd);
    t_copy = 0; t_quant = 0;
    int64_t t_mm[6], t0 = esp_timer_get_time(), t;
    t = esp_timer_get_time(); matmul_staged(&M[0], x, q); t_mm[0] = esp_timer_get_time() - t;
    t = esp_timer_get_time(); matmul_staged(&M[1], x, k); t_mm[1] = esp_timer_get_time() - t;
    t = esp_timer_get_time(); matmul_staged(&M[2], x, v); t_mm[2] = esp_timer_get_time() - t;
    t = esp_timer_get_time(); attention(q, k, v, o); int64_t t_att = esp_timer_get_time() - t;
    t = esp_timer_get_time(); matmul_staged(&M[3], o, q); t_mm[3] = esp_timer_get_time() - t;   // q reused as attn-out
    for (size_t i = 0; i < (size_t)n_tok * D; i++) x[i] += q[i];
    layernorm(x, aon_w, aon_b);
    t = esp_timer_get_time(); matmul_staged(&M[4], x, hbuf); t_mm[4] = esp_timer_get_time() - t;
    int64_t t_gelu = 0;                       // folded into the ffn_up epilogue
    t = esp_timer_get_time(); matmul_staged(&M[5], hbuf, q); t_mm[5] = esp_timer_get_time() - t;
    for (size_t i = 0; i < (size_t)n_tok * D; i++) x[i] += q[i];
    layernorm(x, lon_w, lon_b);
    int64_t total = esp_timer_get_time() - t0;

    double emin = 1, rmin = 1, esum = 0;
    for (int i = 0; i < n_tok; i++) {
        double ce = cosv(x + (size_t)i * D, y_emu + (size_t)i * D, D);
        double cr = cosv(x + (size_t)i * D, y_ref + (size_t)i * D, D);
        if (ce < 0.99) printf("  token %d: cos %.4f\n", i, ce);
        if (ce < emin) emin = ce;
        if (cr < rmin) rmin = cr;
        esum += ce;
    }
    printf("vs numpy int8 emulation: min %.6f mean %.6f | vs numpy fp32: min %.6f\n", emin, esum / n_tok, rmin);
    double macs = 0;
    for (int i = 0; i < 6; i++) {
        double mm = (double)n_tok * M[i].in * M[i].out;
        macs += mm;
        printf("  %-4s %6lld ms  %.2f GMAC/s\n", names[i], t_mm[i] / 1000, mm / t_mm[i] / 1000);
    }
    printf("  quantising matmul inputs: %lld ms\n", t_quant / 1000);
    printf("  inside matmuls, per core: weight copy PSRAM->SRAM %lld/%lld ms, PIE+epilogue %lld/%lld ms\n",
           cyc_cp[0] / 240000, cyc_cp[1] / 240000, cyc_pie[0] / 240000, cyc_pie[1] / 240000);
    cyc_cp[0] = cyc_cp[1] = cyc_pie[0] = cyc_pie[1] = 0;
    printf("  (matmul times include flash->PSRAM staging: %lld ms in all)\n", t_copy / 1000);
    printf("  attention %lld ms, GELU %lld ms, other %lld ms\n", t_att / 1000, t_gelu / 1000,
           (total - t_att - t_gelu - t_mm[0] - t_mm[1] - t_mm[2] - t_mm[3] - t_mm[4] - t_mm[5]) / 1000);
    printf("layer total %lld ms for %d tokens (matmuls %.2f GMAC/s overall)\n", total / 1000, n_tok,
           macs / (t_mm[0] + t_mm[1] + t_mm[2] + t_mm[3] + t_mm[4] + t_mm[5]) / 1000);
    printf("=> 24 layers: ~%.1f s for this %d-token message\n", total * 24 / 1e6, n_tok);
    printf("== done ==\n");
    vTaskDelay(pdMS_TO_TICKS(3000));
  }
}
