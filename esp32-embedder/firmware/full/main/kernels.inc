static void spawn(TaskFunction_t f, const char *name, uint32_t stack, void *arg, UBaseType_t prio, TaskHandle_t *h, BaseType_t core)
{
    if (xTaskCreatePinnedToCore(f, name, stack, arg, prio, h, core) != pdPASS) {
        printf("task %s failed to start (internal free %u)\n", name, (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL));
        abort();
    }
}

#include "f16.h"

#define D      1024
#define FF     4096
#define HEADS  16
#define HD     64
#define XQ_MAX (100 * 1024)   // int8 input chunk kept in internal RAM; also the attention scratch
#define MAXTOK 512            // a whole message: Ollama truncates at 512
#define HB     16             // FFN hidden: one int8 scale per token per 16 rows

// Activations in PSRAM, per token (CHATS-4: ~11 KB, so 512 tokens fit):
//   x     fp32 [D]             residual stream; attn_output and ffn_down add straight into it
//   arena 6 KB                 q, k, v fp16 [D] each (o is written over q), or
//                              the FFN hidden: int8 [FF] + fp32 scale per HB rows
//   xq1   int8 [D]             quantised input of the D-wide matmuls
// ffn_down quantises the hidden in place, so its int8 input is the arena itself.
enum { OUT_F16, OUT_ADD, OUT_HBLK };   // matmul output: fp16 rows, added into fp32, block int8
enum { IN_F32, IN_F16, IN_HBLK };      // quantiser input: the same three forms

typedef struct {
    const int8_t *w; const float *ws, *b; const uint16_t *oi; const float *wo;
    int in, out, mode;
    void *y;                  // OUT_F16: uint16_t [n][out]; OUT_ADD: float [n][out]; OUT_HBLK: int8 [n][out]
    float *hs;                // OUT_HBLK: scales [n][out / HB]
    const uint16_t *noi;      // OUT_HBLK: the next matrix's outlier channels, kept exact in hout
} mat_t;

static int n_tok, K;

static int8_t *xq;            // [tc][in], internal
static float *xs, *xo, *hout; // PSRAM: per-token scale, [n][K] outlier inputs, [n][K] hidden outliers

// ---- persistent workers ------------------------------------------------------------
// One task per core, fed through semaphores: creating and deleting tasks for every
// matmul fragmented internal RAM and dipped it by their stacks.
typedef void (*work_fn)(void *);
typedef struct { work_fn fn; void *arg; SemaphoreHandle_t go, done; } worker_t;
static worker_t workers[2];

static void worker_task(void *p)
{
    worker_t *w = p;
    for (;;) {
        xSemaphoreTake(w->go, portMAX_DELAY);
        w->fn(w->arg);
        xSemaphoreGive(w->done);
    }
}

static void workers_start(void)
{
    for (int c = 0; c < 2; c++) {
        workers[c].go = xSemaphoreCreateBinary();
        workers[c].done = xSemaphoreCreateBinary();
        spawn(worker_task, c ? "w1" : "w0", 12288, &workers[c], 5, NULL, c);
    }
}

// a on core 0, b on core 1; returns when both are done
static void run2(work_fn f, void *a, void *b)
{
    workers[1].fn = f; workers[1].arg = b;
    workers[0].fn = f; workers[0].arg = a;
    xSemaphoreGive(workers[1].go);
    xSemaphoreGive(workers[0].go);
    xSemaphoreTake(workers[0].done, portMAX_DELAY);
    xSemaphoreTake(workers[1].done, portMAX_DELAY);
}

typedef struct { const mat_t *m; int r0, r1, t0, tc; } job_t;

// PIE only ever reads internal SRAM: each core copies a block of weight rows out of
// PSRAM with ordinary loads first. (PIE loads from both cores at once through the
// shared external-memory cache returned occasional garbage dots.)
#define WBUF (16 * 1024)
static int8_t *wbuf[2];
static int64_t cyc_cp[2], cyc_pie[2];

// GELU (tanh form) by linear interpolation in a table over [-6, 6]; |error| < 4e-6
#define GL_N 2048
#define GL_LIM 6.0f
static float *glut;                           // in PSRAM: 8 KB, stays in cache
static void gelu_init(void)
{
    glut = heap_caps_aligned_alloc(64, ((GL_N + 1) * sizeof(float) + 63) & ~63u, MALLOC_CAP_SPIRAM);
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

static inline int8_t q8(float v)
{
    int r;
    __asm__("round.s %0, %1, 0" : "=a"(r) : "f"(v));   // FPU round to nearest
    return (int8_t)r;
}

// One row block's outputs for one token, in yb[0 .. n), go to their destination.
static void store_rows(const mat_t *m, int t, int rb0, int n, float *yb)
{
    if (m->mode == OUT_F16) {
        uint16_t *d = (uint16_t *)m->y + (size_t)t * m->out + rb0;
        for (int r = 0; r < n; r++) d[r] = f2h(yb[r]);
    } else if (m->mode == OUT_ADD) {
        float *d = (float *)m->y + (size_t)t * m->out + rb0;
        for (int r = 0; r < n; r++) d[r] += yb[r];
    } else {                                  // OUT_HBLK: n == HB rows, rb0 a multiple of HB
        for (int i = 0; i < K; i++) {
            int c = m->noi[i] - rb0;
            if (c >= 0 && c < n) { hout[t * K + i] = yb[c]; yb[c] = 0; }
        }
        float mx = 0;
        for (int r = 0; r < n; r++) { float a = fabsf(yb[r]); mx = a > mx ? a : mx; }
        float s = mx > 0 ? mx / 127.0f : 1.0f, inv = 1.0f / s;
        int8_t qb[HB];
        for (int r = 0; r < n; r++) qb[r] = q8(yb[r] * inv);
        memcpy((int8_t *)m->y + (size_t)t * m->out + rb0, qb, n);
        m->hs[(size_t)t * (m->out / HB) + rb0 / HB] = s;
    }
}

static void mm_rows(void *arg)
{
    job_t *j = arg;
    const mat_t *m = j->m;
    int core = xPortGetCoreID();
    int8_t *wb = wbuf[core];
    int rb = WBUF / m->in;                    // 16 rows at in = D, 4 at FF
    float yb[WBUF / D], sw[WBUF / D], bb[WBUF / D], wo[WBUF / D * 8];
    for (int rb0 = j->r0; rb0 < j->r1; rb0 += rb) {
        int rb1 = rb0 + rb < j->r1 ? rb0 + rb : j->r1, n = rb1 - rb0;
        uint32_t c0 = esp_cpu_get_cycle_count();
        memcpy(wb, m->w + (size_t)rb0 * m->in, (size_t)n * m->in);
        uint32_t c1 = esp_cpu_get_cycle_count();
        cyc_cp[core] += c1 - c0;
        for (int r = 0; r < n; r++) {
            sw[r] = m->ws[rb0 + r]; bb[r] = m->b[rb0 + r];
            memcpy(wo + r * K, m->wo + (size_t)(rb0 + r) * K, K * sizeof(float));
        }
        // No task switch on this core while PIE and FPU state are live: with Wi-Fi up (more
        // switches), core 0's results came out intermittently corrupted, which points at the lazy
        // coprocessor save/restore. Interrupts stay on, so DMA and the network keep running.
        vTaskSuspendAll();
        for (int t = 0; t < j->tc; t++) {
            const int8_t *xr = xq + (size_t)t * m->in;
            const float *o = xo + (j->t0 + t) * K;
            float s = xs[j->t0 + t];
            for (int r = 0; r < n; r++) {
                int32_t acc = esp_nn_dot_s8_aligned_esp32s3(xr, wb + (size_t)r * m->in, m->in);
                float v = acc * s * sw[r] + bb[r];
                const float *w8 = wo + r * K;
                for (int i = 0; i < K; i++) v += o[i] * w8[i];
                yb[r] = m->mode == OUT_HBLK ? gelu_fast(v) : v;
            }
            store_rows(m, j->t0 + t, rb0, n, yb);
        }
        xTaskResumeAll();
        cyc_pie[core] += esp_cpu_get_cycle_count() - c1;
    }
}

// y[n][out] = x[n][in] . W^T + b, both cores, token chunks sized to fit xq. x8 is the
// int8 input in PSRAM (from quantise); each chunk is copied into the internal xq for PIE.
static void matmul_r(const mat_t *m, const int8_t *x8, int r0, int r1)
{
    int maxc = XQ_MAX / m->in, passes = (n_tok + maxc - 1) / maxc;
    int chunk = (n_tok + passes - 1) / passes;
    for (int t0 = 0; t0 < n_tok; t0 += chunk) {
        int tc = n_tok - t0 < chunk ? n_tok - t0 : chunk;
        memcpy(xq, x8 + (size_t)t0 * m->in, (size_t)tc * m->in);
        int rm = ((r0 + r1) / 2) / HB * HB;   // the split stays on an HB boundary
        job_t a = { m, r0, rm, t0, tc }, b = { m, rm, r1, t0, tc };
        run2(mm_rows, &a, &b);
    }
}

// ---- quantise a matmul's input, once per matmul, both cores ---------------------------
// Per token: K outlier channels kept fp32 (xo), the rest int8 with one scale (xs).
typedef struct { const mat_t *m; int kind; const void *x; const float *hs; int8_t *dst; int t0, t1; } qjob_t;

static void quant_rows(void *arg)
{
    qjob_t *q = arg;
    const mat_t *m = q->m;
    int in = m->in;
    float row[D];                             // an fp32/fp16 input row (in = D), internal
    int8_t qb[FF];                            // built here, one copy out to PSRAM
    for (int t = q->t0; t < q->t1; t++) {
        float mx = 0, s, inv;
        if (q->kind == IN_HBLK) {             // block int8; outlier channels are 0 there, exact in hout
            const int8_t *h = (const int8_t *)q->x + (size_t)t * in;
            const float *bs = q->hs + (size_t)t * (in / HB);
            for (int b = 0; b < in / HB; b++) {
                int bm = 0;
                for (int j = b * HB; j < (b + 1) * HB; j++) { int a = h[j] < 0 ? -h[j] : h[j]; bm = a > bm ? a : bm; }
                float a = bm * bs[b];
                mx = a > mx ? a : mx;
            }
            s = mx > 0 ? mx / 127.0f : 1.0f; inv = 1.0f / s;
            for (int j = 0; j < in; j++) qb[j] = q8(h[j] * bs[j / HB] * inv);
            for (int i = 0; i < K; i++) xo[t * K + i] = hout[t * K + i];
        } else {
            if (q->kind == IN_F32) {
                memcpy(row, (const float *)q->x + (size_t)t * in, in * sizeof(float));
            } else {
                const uint16_t *h = (const uint16_t *)q->x + (size_t)t * in;
                for (int j = 0; j < in; j++) row[j] = h2f(h[j]);
            }
            int prev = 0;
            for (int i = 0; i <= K; i++) {    // max |x| over the gaps between outlier dims
                int end = i < K ? m->oi[i] : in;
                for (int j = prev; j < end; j++) { float a = fabsf(row[j]); mx = a > mx ? a : mx; }
                if (i < K) xo[t * K + i] = row[end];
                prev = end + 1;
            }
            s = mx > 0 ? mx / 127.0f : 1.0f; inv = 1.0f / s;
            for (int j = 0; j < in; j++) qb[j] = q8(row[j] * inv);
        }
        xs[t] = s;
        for (int i = 0; i < K; i++) qb[m->oi[i]] = 0;
        memcpy(q->dst + (size_t)t * in, qb, in);   // may be the IN_HBLK row itself: already read
    }
}

static int64_t t_quant;
static void quantise(const mat_t *m, int kind, const void *x, const float *hs, int8_t *dst)
{
    int64_t t = esp_timer_get_time();
    qjob_t a = { m, kind, x, hs, dst, 0, n_tok / 2 }, b = { m, kind, x, hs, dst, n_tok / 2, n_tok };
    run2(quant_rows, &a, &b);
    t_quant += esp_timer_get_time() - t;
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

// ---- attention ----------------------------------------------------------------------
// Heads split across the cores. Blocked, with an online softmax (as in FlashAttention),
// so the internal scratch is fixed whatever the length: a block of QB queries is held
// in internal RAM while the keys and values stream past in chunks of KC. q, k, v are
// fp16 in PSRAM; o is written over q (each query's q is read before its o is written).
#define QB 64
#define KC 32
typedef struct { uint16_t *q; const uint16_t *k, *v; float *scr; int h0, h1; } att_t;

static void att_heads(void *arg)
{
    att_t *a = arg;
    int n = n_tok;
    float *qb = a->scr, *acc = qb + QB * HD, *kb = acc + QB * HD, *vb = kb + KC * HD, *mx = vb + KC * HD, *l = mx + QB;
    for (int h = a->h0; h < a->h1; h++) {
        for (int q0 = 0; q0 < n; q0 += QB) {
            int nq = n - q0 < QB ? n - q0 : QB;
            for (int i = 0; i < nq; i++) {
                const uint16_t *s = a->q + (size_t)(q0 + i) * D + h * HD;
                for (int d = 0; d < HD; d++) qb[i * HD + d] = h2f(s[d]) * 0.125f;   // 1 / sqrt(HD)
                mx[i] = -1e30f; l[i] = 0;
            }
            memset(acc, 0, (size_t)nq * HD * sizeof(float));
            for (int k0 = 0; k0 < n; k0 += KC) {
                int nk = n - k0 < KC ? n - k0 : KC;
                for (int j = 0; j < nk; j++) {
                    const uint16_t *ks = a->k + (size_t)(k0 + j) * D + h * HD, *vs = a->v + (size_t)(k0 + j) * D + h * HD;
                    for (int d = 0; d < HD; d++) { kb[j * HD + d] = h2f(ks[d]); vb[j * HD + d] = h2f(vs[d]); }
                }
                for (int i = 0; i < nq; i++) {
                    const float *qi = qb + i * HD;
                    float sc[KC], m = mx[i];
                    int j = 0;
                    for (; j + 4 <= nk; j += 4) {     // four keys at once: q[d] loaded once
                        const float *k_0 = kb + j * HD, *k_1 = k_0 + HD, *k_2 = k_1 + HD, *k_3 = k_2 + HD;
                        float s0 = 0, s1 = 0, s2 = 0, s3 = 0;
                        for (int d = 0; d < HD; d++) {
                            float qd = qi[d];
                            s0 += qd * k_0[d]; s1 += qd * k_1[d]; s2 += qd * k_2[d]; s3 += qd * k_3[d];
                        }
                        sc[j] = s0; sc[j + 1] = s1; sc[j + 2] = s2; sc[j + 3] = s3;
                    }
                    for (; j < nk; j++) {
                        const float *kj = kb + j * HD;
                        float s = 0;
                        for (int d = 0; d < HD; d++) s += qi[d] * kj[d];
                        sc[j] = s;
                    }
                    for (j = 0; j < nk; j++) if (sc[j] > m) m = sc[j];
                    float corr = fast_exp(mx[i] - m), sum = 0;
                    for (j = 0; j < nk; j++) { sc[j] = fast_exp(sc[j] - m); sum += sc[j]; }
                    mx[i] = m;
                    l[i] = l[i] * corr + sum;
                    float *ai = acc + i * HD;
                    for (int d = 0; d < HD; d += 8) {          // eight output dims held in registers
                        float a0 = ai[d] * corr, a1 = ai[d + 1] * corr, a2 = ai[d + 2] * corr, a3 = ai[d + 3] * corr;
                        float a4 = ai[d + 4] * corr, a5 = ai[d + 5] * corr, a6 = ai[d + 6] * corr, a7 = ai[d + 7] * corr;
                        const float *vp = vb + d;
                        for (j = 0; j < nk; j++, vp += HD) {
                            float w = sc[j];
                            a0 += w * vp[0]; a1 += w * vp[1]; a2 += w * vp[2]; a3 += w * vp[3];
                            a4 += w * vp[4]; a5 += w * vp[5]; a6 += w * vp[6]; a7 += w * vp[7];
                        }
                        ai[d] = a0; ai[d + 1] = a1; ai[d + 2] = a2; ai[d + 3] = a3;
                        ai[d + 4] = a4; ai[d + 5] = a5; ai[d + 6] = a6; ai[d + 7] = a7;
                    }
                }
            }
            for (int i = 0; i < nq; i++) {
                uint16_t *o = a->q + (size_t)(q0 + i) * D + h * HD;
                float inv = 1.0f / l[i];
                for (int d = 0; d < HD; d++) o[d] = f2h(acc[i * HD + d] * inv);
            }
        }
    }
}

// per core: q block and accumulators [QB][HD], k and v chunks [KC][HD], running max and sum [QB]
#define ATT_SCR ((2 * QB * HD + 2 * KC * HD + 2 * QB) * sizeof(float))
static float *att_scr[2];
static void attention(uint16_t *q, const uint16_t *k, const uint16_t *v)
{
    att_t a = { q, k, v, att_scr[0], 0, HEADS / 2 }, b = { q, k, v, att_scr[1], HEADS / 2, HEADS };
    run2(att_heads, &a, &b);
}

static double cosv(const float *a, const float *b, int n)
{
    double ab = 0, aa = 0, bb = 0;
    for (int i = 0; i < n; i++) { ab += (double)a[i] * b[i]; aa += (double)a[i] * a[i]; bb += (double)b[i] * b[i]; }
    return ab / sqrt(aa * bb);
}
