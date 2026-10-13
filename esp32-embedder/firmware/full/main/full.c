// HOME-432 step 3: the whole of mxbai-embed-large on the ESP32-S3. Weights streamed off the
// SD card (mxbai.bin from prep_full.py) one matrix at a time; checked against mxtest.bin
// (numpy emulation of the int8 scheme, and fp32 = Ollama). Throwaway spike code.
// CHATS-3: the test texts are tokenised on the device (../../tok) and the ids checked
// against mxtest.bin before each pass.
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "esp_app_desc.h"
#include "esp_cpu.h"
#include "esp_heap_caps.h"
#include "esp_rom_crc.h"
#include "esp_timer.h"
#include "esp_vfs_fat.h"
#include "driver/sdmmc_host.h"
#include "sdmmc_cmd.h"
#include "ff.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"

#include "mxcrc.h"
#include "tok.h"
#include "net.h"
#include "vcore.h"
#include "selftest.h"
#include "longtest.h"

extern int32_t esp_nn_dot_s8_aligned_esp32s3(const int8_t *a, const int8_t *b, int32_t len);

#include "kernels.inc"

// PSRAM buffers own whole 64-byte cache lines. With Wi-Fi/lwIP buffers in PSRAM too, a line
// shared with a DMA buffer can be invalidated under us, losing our dirty data (CHATS-4).
static void *ps(size_t n) { return heap_caps_aligned_alloc(64, (n + 63) & ~(size_t)63, MALLOC_CAP_SPIRAM); }

// ---- SD -------------------------------------------------------------------------------
// ESP32-S3 CAM board: 1-bit SDMMC, CLK 39, CMD 38, D0 40. Reads go through an internal
// bounce buffer (SD DMA straight into PSRAM is ~6x slower).
#define ABOUNCE (16 * 1024)
static uint8_t *abounce[2];               // SD DMA bounce buffers, internal
static int64_t t_sd;
static int crc_bad;

// Sections are read as raw sectors through the file's cluster map (FatFs seeks walk the
// cluster chain: seconds per seek in a 345 MB file). The map is built once at start-up.
static sdmmc_card_t *card;
#define MAXFRAG 256
static uint32_t frag_off[MAXFRAG + 1], frag_lba[MAXFRAG];   // file byte offset where each fragment starts
static int n_frag;

static int raw_open(void)
{
    // used once at start-up: PSRAM, not internal
    FIL *fp = ps(sizeof *fp);
    DWORD *tbl = ps((2 * MAXFRAG + 2) * sizeof *tbl);
    if (!fp || !tbl) { printf("raw_open: alloc failed\n"); return -1; }
    if (f_open(fp, "0:/mxbai.bin", FA_READ) != FR_OK) { printf("f_open failed\n"); return -1; }
    fp->cltbl = tbl;
    tbl[0] = 2 * MAXFRAG + 2;
    FRESULT r = f_lseek(fp, CREATE_LINKMAP);
    if (r != FR_OK) { printf("linkmap failed (%d), need %lu entries\n", r, (unsigned long)tbl[0]); return -1; }
    FATFS *fs = fp->obj.fs;
    uint32_t clus_bytes = fs->csize * 512, pos = 0;
    for (const DWORD *t = tbl + 1; *t; t += 2, n_frag++) {
        frag_off[n_frag] = pos;
        frag_lba[n_frag] = fs->database + (t[1] - 2) * fs->csize;
        pos += t[0] * clus_bytes;
    }
    frag_off[n_frag] = pos;
    printf("mxbai.bin: %d fragments, %u-byte clusters\n", n_frag, (unsigned)clus_bytes);
    if (pos < f_size(fp)) { printf("cluster map short\n"); return -1; }
    f_close(fp);
    heap_caps_free(fp);
    heap_caps_free(tbl);
    return 0;
}

static uint32_t sd_read(void *dst, uint32_t off, size_t len, uint32_t crc)
{
    int64_t t = esp_timer_get_time();
    if (off % 512) { printf("SD: unaligned read at %lu\n", (unsigned long)off); abort(); }
    int f = 0;
    for (uint8_t *d = dst; len; ) {
        while (off >= frag_off[f + 1]) f++;
        size_t n = len < ABOUNCE ? len : ABOUNCE;
        if (n > frag_off[f + 1] - off) n = frag_off[f + 1] - off;     // stop at the fragment's end
        size_t ns = (n + 511) / 512;
        uint32_t lba = frag_lba[f] + (off - frag_off[f]) / 512;
        esp_err_t e = sdmmc_read_sectors(card, abounce[0], lba, ns);
        if (e != ESP_OK) e = sdmmc_read_sectors(card, abounce[0], lba, ns);
        if (e != ESP_OK) { printf("SD: read at lba %lu failed: %s\n", (unsigned long)lba, esp_err_to_name(e)); abort(); }
        crc = esp_rom_crc32_le(crc, abounce[0], n);
        memcpy(d, abounce[0], n);
        d += n; len -= n; off += n;
    }
    t_sd += esp_timer_get_time() - t;
    return crc;
}

static void check(const char *what, int a, int b, uint32_t got, uint32_t want)
{
    if (got != want) { crc_bad++; printf("CRC mismatch: %s %d/%d got %08lx want %08lx\n", what, a, b,
                                         (unsigned long)got, (unsigned long)want); }
}

// ---- background reads ---------------------------------------------------------------
// sd task (core 0, high priority) keeps the card busy, alternating two bounce buffers; it mostly
// sleeps on the SD DMA. cp task (core 1) copies each chunk to its destination and CRCs it. Requests
// complete in order. The compute tasks run underneath both.
typedef struct {
    uint8_t *dst; uint32_t off, len;
    uint32_t *acc, own, want; int reset, verify;   // CRC accumulates into *acc; verify at the end
    const char *what; int a, b;
    SemaphoreHandle_t done;
} rreq_t;
typedef struct { rreq_t *r; uint8_t *dst; int bi, n, first, last; } chunk_t;
#define NREQ 32
static rreq_t *reqs;                          // PSRAM
static int req_next;
static QueueHandle_t rq, cq;
static SemaphoreHandle_t bfree[2];
static int64_t t_stall;

static void sd_task(void *arg)
{
    int bi = 0;
    for (;;) {
        rreq_t *r;
        xQueueReceive(rq, &r, portMAX_DELAY);
        uint32_t off = r->off, len = r->len;
        uint8_t *d = r->dst;
        int f = 0, first = 1;
        while (len) {
            while (off >= frag_off[f + 1]) f++;
            size_t n = len < ABOUNCE ? len : ABOUNCE;
            if (n > frag_off[f + 1] - off) n = frag_off[f + 1] - off;
            size_t ns = (n + 511) / 512;
            uint32_t lba = frag_lba[f] + (off - frag_off[f]) / 512;
            xSemaphoreTake(bfree[bi], portMAX_DELAY);
            esp_err_t e = sdmmc_read_sectors(card, abounce[bi], lba, ns);
            if (e != ESP_OK) e = sdmmc_read_sectors(card, abounce[bi], lba, ns);
            if (e != ESP_OK) { printf("SD: read at lba %lu failed: %s\n", (unsigned long)lba, esp_err_to_name(e)); abort(); }
            chunk_t c = { r, d, bi, n, first, len == n };
            xQueueSend(cq, &c, portMAX_DELAY);
            bi ^= 1; first = 0;
            d += n; len -= n; off += n;
        }
    }
}

static void cp_task(void *arg)
{
    for (;;) {
        chunk_t c;
        xQueueReceive(cq, &c, portMAX_DELAY);
        rreq_t *r = c.r;
        if (c.first && r->reset) *r->acc = 0;
        *r->acc = esp_rom_crc32_le(*r->acc, abounce[c.bi], c.n);
        memcpy(c.dst, abounce[c.bi], c.n);
        xSemaphoreGive(bfree[c.bi]);
        if (c.last) {
            if (r->verify) check(r->what, r->a, r->b, *r->acc, r->want);
            xSemaphoreGive(r->done);
        }
    }
}

// queue a read; acc NULL = a section checked on its own
static rreq_t *rd(void *dst, uint32_t off, uint32_t len, uint32_t *acc, int reset, int verify, uint32_t want,
                  const char *what, int a, int b)
{
    rreq_t *r = &reqs[req_next++ % NREQ];
    SemaphoreHandle_t done = r->done;
    xSemaphoreTake(done, 0);                  // slots nobody waited on are left given
    *r = (rreq_t){ dst, off, len, acc, 0, want, reset, verify, what, a, b, done };
    if (!acc) { r->acc = &r->own; r->reset = 1; }
    xQueueSend(rq, &r, portMAX_DELAY);
    return r;
}

static void wait_rd(rreq_t *r)
{
    int64_t t = esp_timer_get_time();
    xSemaphoreTake(r->done, portMAX_DELAY);
    t_stall += esp_timer_get_time() - t;
}

static void reader_start(void)
{
    reqs = ps(NREQ * sizeof *reqs);
    memset(reqs, 0, NREQ * sizeof *reqs);
    rq = xQueueCreate(NREQ, sizeof(rreq_t *));
    cq = xQueueCreate(4, sizeof(chunk_t));
    for (int i = 0; i < NREQ; i++) reqs[i].done = xSemaphoreCreateBinary();
    for (int i = 0; i < 2; i++) {
        bfree[i] = xSemaphoreCreateBinary();
        xSemaphoreGive(bfree[i]);
    }
    spawn(sd_task, "sd", 3072, NULL, 10, NULL, 0);
    spawn(cp_task, "cp", 3072, NULL, 9, NULL, 1);
}

// ---- model ----------------------------------------------------------------------------
static uint32_t goff[7], loff[24];
static uint32_t *hdr, (*ltab)[34];              // PSRAM
static int n_layer, n_vocab;
static float *tok_s, *type0, *en_w, *en_b;
static float *x, *nrm[2], *mws[2], *mb[2], *mwo[2];
static uint8_t *arena;                       // [MAXTOK][6 KB]: q, k, v fp16, or the FFN hidden (kernels.inc)
static int8_t *xq1;                          // [MAXTOK][D] int8 input of the D-wide matmuls
static uint16_t (*allo)[6][8];               // every matrix's outlier channels, read at start-up
static int8_t *stage[2];
#define HALF (512 * 1024)                    // weights are read in 512 KB blocks, double-buffered
#define ARENA_TOK (6 * 1024)
#define MAXUNITS (24 * 6 * 6)

// The pass as a flat list of units (layer, matrix, row block). While unit u computes, unit u+1's
// reads are in flight: its block, plus its matrix's small sections (and the layer's norms) if it
// starts one.
static const int MIN[6] = { D, D, D, D, D, FF }, MOUT[6] = { D, D, D, D, FF, D };
typedef struct { int li, mi, j, nb, r0, r1; } unit_t;
static unit_t *units;                         // [MAXUNITS], PSRAM
static int n_units;
static uint32_t wq_acc[2];
static rreq_t *ureq[2][6];
static int n_ureq[2];

static void plan(void)
{
    n_units = 0;
    for (int li = 0; li < n_layer; li++)
        for (int mi = 0; mi < 6; mi++) {
            int rb = HALF / MIN[mi], nb = (MOUT[mi] + rb - 1) / rb;
            for (int j = 0; j < nb; j++) {
                int r0 = j * rb, r1 = r0 + rb < MOUT[mi] ? r0 + rb : MOUT[mi];
                units[n_units++] = (unit_t){ li, mi, j, nb, r0, r1 };
            }
        }
}

static void issue(int u)
{
    unit_t *t = &units[u];
    int p = u & 1, mp = (t->li * 6 + t->mi) & 1, s = t->mi * 5, out = MOUT[t->mi], in = MIN[t->mi];
    uint32_t base = loff[t->li];
    n_ureq[p] = 0;
    if (t->mi == 0 && t->j == 0)
        for (int i = 0; i < 4; i++)
            ureq[p][n_ureq[p]++] = rd(nrm[t->li & 1] + i * D, base + ltab[t->li][30 + i], D * 4, NULL, 1, 1,
                                      layer_crc[t->li][30 + i], "norm", t->li, i);
    if (t->j == 0) {
        // the four small sections share one wait: only the last is kept
        rd(mws[mp], base + ltab[t->li][s + 1], out * 4, NULL, 1, 1, layer_crc[t->li][s + 1], "ws", t->li, t->mi);
        rd(mb[mp], base + ltab[t->li][s + 2], out * 4, NULL, 1, 1, layer_crc[t->li][s + 2], "b", t->li, t->mi);
        ureq[p][n_ureq[p]++] = rd(mwo[mp], base + ltab[t->li][s + 4], out * K * 4, NULL, 1, 1,
                                  layer_crc[t->li][s + 4], "wo", t->li, t->mi);
    }
    ureq[p][n_ureq[p]++] = rd(stage[p], base + ltab[t->li][s] + (uint32_t)t->r0 * in, (uint32_t)(t->r1 - t->r0) * in,
                              &wq_acc[mp], t->j == 0, t->j == t->nb - 1, layer_crc[t->li][s], "wq", t->li, t->mi);
}

static int64_t t_att;
static void pass_fwd(void)
{
    size_t nd = (size_t)n_tok * D;
    uint16_t *q = (uint16_t *)arena, *k = q + nd, *v = k + nd;      // fp16; o is written over q
    int8_t *h = (int8_t *)arena;                                     // FFN hidden: int8 [n][FF] ...
    float *hs = (float *)(arena + (size_t)n_tok * FF);               // ... and its scales [n][FF / HB]
    void *yout[6] = { q, k, v, x, h, x };
    static const int mode[6] = { OUT_F16, OUT_F16, OUT_F16, OUT_ADD, OUT_HBLK, OUT_ADD };
    issue(0);
    for (int u = 0; u < n_units; u++) {
        unit_t *t = &units[u];
        int p = u & 1, mp = (t->li * 6 + t->mi) & 1, mi = t->mi;
        if (u + 1 < n_units) issue(u + 1);       // u - 1 is finished, so its buffers take u + 1's reads
        for (int i = 0; i < n_ureq[p]; i++) wait_rd(ureq[p][i]);
        mat_t m = { stage[p] - (size_t)t->r0 * MIN[mi], mws[mp], mb[mp], allo[t->li][mi], mwo[mp], MIN[mi], MOUT[mi],
                    mode[mi], yout[mi], hs, allo[t->li][5] };
        if (t->j == 0) {
            if (mi == 3) quantise(&m, IN_F16, q, NULL, xq1);
            else if (mi == 5) quantise(&m, IN_HBLK, h, hs, h);            // in place
            else quantise(&m, IN_F32, x, NULL, xq1);
        }
        matmul_r(&m, mi == 5 ? h : xq1, t->r0, t->r1);
        if (t->j < t->nb - 1) continue;
        float *nw = nrm[t->li & 1];
        if (mi == 2) {
            int64_t ta = esp_timer_get_time();
            attention(q, k, v);
            t_att += esp_timer_get_time() - ta;
        } else if (mi == 3) {
            layernorm(x, nw, nw + D);
        } else if (mi == 5) {
            layernorm(x, nw + 2 * D, nw + 3 * D);
        }
    }
}

// token + position + type embeddings, then layernorm
static void embed_in(const uint32_t *ids)
{
    int8_t row[D];
    float *pos = (float *)arena;             // the arena is free here
    sd_read(pos, goff[2], (size_t)n_tok * D * 4, 0);
    for (int t = 0; t < n_tok; t++) {
        sd_read(row, goff[0] + ids[t] * D, D, 0);
        float s = tok_s[ids[t]], *r = x + (size_t)t * D;
        for (int j = 0; j < D; j++) r[j] = row[j] * s + pos[(size_t)t * D + j] + type0[j];
    }
    layernorm(x, en_w, en_b);
}

static void *in(size_t n) { return heap_caps_aligned_alloc(16, n, MALLOC_CAP_INTERNAL | MALLOC_CAP_DMA); }

// the texts prep_full.py made mxtest.bin from, in the same order
static const char *const TEXTS[] = {
    "The cat sat on the radiator all afternoon.",
    "Shall I carry on with the kernel?",
    "git push failed: permission denied (publickey)",
    "Martin asked whether a cheap ESP32-S3 could take over the embedding job that Cal does today. "
    "The plan streams the weights of mxbai-embed-large from an SD card, one matrix at a time, and "
    "keeps a batch of messages in PSRAM so that each weight is used many times. Earlier benchmarks "
    "measured about one billion multiply-accumulates per second across both cores, which suggests "
    "roughly thirty seconds for a typical message. This test checks the arithmetic of one encoder "
    "layer against a reference computed on the Mac.",
    "  File \"/usr/lib/python3.11/site-packages/requests/adapters.py\", line 519, in send\n"
    "    raise ConnectionError(e, request=request)\nrequests.exceptions.ConnectionError: "
    "HTTPConnectionPool(host='localhost', port=11434): Max retries exceeded with url: /api/embed "
    "(Caused by NewConnectionError('<urllib3.connection.HTTPConnection object at 0x10f3c2d90>: "
    "Failed to establish a new connection: [Errno 61] Connection refused'))",
};
#define N_TEXTS (int)(sizeof TEXTS / sizeof *TEXTS)
static tok_vocab vocab;

// vocab section (CRC-checked) -> hash table in PSRAM; the raw section is freed afterwards
static int vocab_load(void)
{
    int64_t t = esp_timer_get_time();
    char *raw = ps(VOCAB_LEN);
    if (!raw) { printf("vocab: alloc failed\n"); return -1; }
    check("vocab", 0, 6, sd_read(raw, goff[6], VOCAB_LEN, 0), glob_crc[6]);
    int r = tok_init(&vocab, raw, VOCAB_LEN, n_vocab, ps, heap_caps_free);
    heap_caps_free(raw);
    if (r) { printf("vocab: tok_init failed\n"); return -1; }
    printf("vocab: %d tokens, longest %d bytes, loaded in %.2f s; PSRAM free %u\n", vocab.n, vocab.max_len,
           (esp_timer_get_time() - t) / 1e6, (unsigned)heap_caps_get_free_size(MALLOC_CAP_SPIRAM));
    return 0;
}



_Static_assert(N_SELFTEST == N_TEXTS + N_LONG, "selftest.h: one CRC per test text and long text");
static int st_pass, st_fail, st_new, st_done[N_SELFTEST];
// the bit-exact check of one output vector (the [CLS] row of x) against selftest.h
static void selftest(int i)
{
    uint32_t c = esp_rom_crc32_le(0, (const uint8_t *)x, D * 4), want = i < N_SELFTEST ? SELFTEST_CRC[i] : 0;
    if (i < N_SELFTEST) st_done[i] = 1;
    if (!want) st_new++;
    else if (c == want) st_pass++;
    else st_fail++;
    printf("  selftest %d: vector crc %08lx, %s\n", i, (unsigned long)c, !want ? "not recorded" : c == want ? "bit-exact" : "WRONG");
}

void app_main(void)
{
    vcore_boost(1250);                       // 240 MHz needs >= 1.25 V on some chips: see vcore.c
    vTaskDelay(pdMS_TO_TICKS(1500));
    printf("\n== HOME-432 step 3: whole mxbai on the S3, weights from SD ==\n");
    const esp_app_desc_t *app = esp_app_get_description();   // version.txt, or CI's x.y.z (CHATS-10)
    printf("%s %s, built %s %s with ESP-IDF %s\n", app->project_name, app->version, app->date, app->time, app->idf_ver);
    printf("ROM crc32 check: %08lx (want cbf43926)\n", (unsigned long)esp_rom_crc32_le(0, (const uint8_t *)"123456789", 9));

    sdmmc_host_t host = SDMMC_HOST_DEFAULT();
    host.max_freq_khz = SDMMC_FREQ_HIGHSPEED;
    sdmmc_slot_config_t slot = SDMMC_SLOT_CONFIG_DEFAULT();
    slot.width = 1; slot.clk = 39; slot.cmd = 38; slot.d0 = 40;
    slot.flags |= SDMMC_SLOT_FLAG_INTERNAL_PULLUP;
    esp_vfs_fat_sdmmc_mount_config_t mc = { .format_if_mount_failed = false, .max_files = 2 };
    if (esp_vfs_fat_sdmmc_mount("/sd", &host, &slot, &mc, &card) != ESP_OK) { printf("SD mount failed\n"); return; }
    FILE *ft = fopen("/sd/mxtest.bin", "rb");
    if (!ft) { printf("open failed\n"); return; }
    abounce[0] = in(ABOUNCE); abounce[1] = in(ABOUNCE);
    if (raw_open()) return;

    hdr = ps(1024);
    ltab = ps(24 * sizeof *ltab);
    units = ps(MAXUNITS * sizeof *units);
    uint32_t *want = ps(512 * 4);
    int32_t *ids = ps(512 * 4);
    if (!hdr || !ltab || !units || !want || !ids) { printf("alloc failed (tables)\n"); return; }
    sd_read(hdr, 0, 1024, 0);
    if (memcmp(hdr, "MXB1", 4)) { printf("bad magic\n"); return; }
    n_layer = hdr[2]; K = hdr[3]; n_vocab = hdr[4];
    memcpy(goff, hdr + 8, sizeof goff);
    memcpy(loff, hdr + 15, sizeof loff);
    printf("%d layers, K=%d, vocab %d\n", n_layer, K, n_vocab);

    printf("before buffers: internal free %u (largest %u), PSRAM free %u (largest %u)\n",
           (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL), (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL),
           (unsigned)heap_caps_get_free_size(MALLOC_CAP_SPIRAM), (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_SPIRAM));
    if (vocab_load()) return;                // first: its temporary copy leaves a hole the small buffers reuse
    xq = heap_caps_aligned_alloc(16, XQ_MAX, MALLOC_CAP_INTERNAL);
    wbuf[0] = heap_caps_aligned_alloc(16, WBUF, MALLOC_CAP_INTERNAL);
    wbuf[1] = heap_caps_aligned_alloc(16, WBUF, MALLOC_CAP_INTERNAL);
    x = ps((size_t)MAXTOK * D * sizeof(float)); arena = ps((size_t)MAXTOK * ARENA_TOK); xq1 = ps((size_t)MAXTOK * D);
    for (int i = 0; i < 2; i++) {
        stage[i] = ps(HALF); mws[i] = ps(FF * 4); mb[i] = ps(FF * 4); mwo[i] = ps(FF * 8 * 4); nrm[i] = ps(4 * D * 4);
        if (!stage[i] || !mws[i] || !mb[i] || !mwo[i] || !nrm[i]) { printf("PSRAM alloc failed (pair %d)\n", i); return; }
    }
    xs = ps(MAXTOK * sizeof(float)); xo = ps(MAXTOK * K * sizeof(float)); hout = ps(MAXTOK * K * sizeof(float));
    tok_s = ps(n_vocab * 4); type0 = ps(D * 4); en_w = ps(D * 4); en_b = ps(D * 4); allo = ps(24 * sizeof *allo);
    if (!xq || !xs || !xo || !hout || !wbuf[0] || !wbuf[1] || !abounce[0] || !abounce[1] || !x || !arena || !xq1 ||
        !tok_s || !type0 || !en_w || !en_b || !allo || K > 8 || 2 * ATT_SCR > XQ_MAX) {
        printf("alloc failed: xq %p xs %p xo %p hout %p wbuf %p %p abounce %p %p x %p arena %p xq1 %p tok_s %p allo %p\n",
               xq, xs, xo, hout, wbuf[0], wbuf[1], abounce[0], abounce[1], x, arena, xq1, tok_s, allo);
        return;
    }
    printf("free: internal %u, PSRAM %u\n", (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL),
           (unsigned)heap_caps_get_free_size(MALLOC_CAP_SPIRAM));
    check("tok_s", 0, 1, sd_read(tok_s, goff[1], n_vocab * 4, 0), glob_crc[1]);
    check("type0", 0, 3, sd_read(type0, goff[3], D * 4, 0), glob_crc[3]);
    check("en_w", 0, 4, sd_read(en_w, goff[4], D * 4, 0), glob_crc[4]);
    check("en_b", 0, 5, sd_read(en_b, goff[5], D * 4, 0), glob_crc[5]);
    att_scr[0] = (float *)xq;                 // xq is idle during attention: the blocked scratch, per core
    att_scr[1] = (float *)(xq + ATT_SCR);
    gelu_init();
    for (int li = 0; li < n_layer; li++) {
        sd_read(ltab[li], loff[li], sizeof *ltab, 0);
        for (int mi = 0; mi < 6; mi++)        // outlier channels, all up front: ffn_up needs ffn_down's
            check("oi", li, mi, sd_read(allo[li][mi], loff[li] + ltab[li][mi * 5 + 3], K * 2, 0), layer_crc[li][mi * 5 + 3]);
    }
    plan();
    workers_start();
    reader_start();
    printf("%d units per pass\n", n_units);

    uint32_t th[3];
    fread(th, 4, 3, ft);
    if (memcmp(th, "MXT1", 4)) { printf("bad test magic\n"); return; }
    int tok_bad = 0;
    float *ref0 = ps(D * 4);                  // test 0's Ollama vector, for the Wi-Fi check at the end
    if (!ref0) { printf("alloc failed: ref0\n"); return; }
    memset(ref0, 0, D * 4);
    for (int ti = 0; ti < (int)th[1]; ti++) {
        uint32_t n;
        fread(&n, 4, 1, ft);
        fread(want, 4, n, ft);
        float *emu = ps(D * 4), *ref = ps(D * 4);
        fread(emu, 4, D, ft);
        fread(ref, 4, D, ft);
        if (ti >= N_TEXTS) { printf("test %d: no text\n", ti); heap_caps_free(emu); heap_caps_free(ref); continue; }
        int64_t tt = esp_timer_get_time();
        int nt = tok_encode(&vocab, TEXTS[ti], strlen(TEXTS[ti]), ids, 512, NULL);
        tt = esp_timer_get_time() - tt;
        int same = nt == (int)n && !memcmp(ids, want, n * 4);
        tok_bad += !same;
        printf("test %d: tokenised %u bytes -> %d ids in %lld us; %s mxtest.bin\n", ti, (unsigned)strlen(TEXTS[ti]),
               nt, (long long)tt, same ? "same as" : "DIFFERENT FROM");
        if (!same) {
            for (int i = 0; i < (nt > (int)n ? nt : (int)n) && i < 512; i++)
                if (i >= nt || i >= (int)n || ids[i] != (int32_t)want[i]) {
                    printf("  first difference at %d: got %ld, want %ld\n", i, i < nt ? (long)ids[i] : -1L,
                           i < (int)n ? (long)want[i] : -1L);
                    break;
                }
        }
        if (!same) { heap_caps_free(emu); heap_caps_free(ref); continue; }   // emu and ref belong to mxtest.bin's ids
        if (n > MAXTOK) { printf("test %d: %lu tokens > %d, skipped\n", ti, (unsigned long)n, MAXTOK); heap_caps_free(emu); heap_caps_free(ref); continue; }
        n_tok = n;
        t_sd = t_att = t_quant = t_stall = 0;
        cyc_cp[0] = cyc_cp[1] = cyc_pie[0] = cyc_pie[1] = 0;
        int64_t t0 = esp_timer_get_time();
        embed_in((const uint32_t *)ids);
        int64_t te = esp_timer_get_time();
        pass_fwd();
        printf("  embeddings in %.2f s; pass %.1f s, of which %.1f s waiting on the card\n", (te - t0) / 1e6,
               (esp_timer_get_time() - te) / 1e6, t_stall / 1e6);
        int64_t total = esp_timer_get_time() - t0;
        printf("test %d: %3d tokens  cos vs emulation %.6f  vs Ollama %.6f  |  %.1f s total: sync SD %.1f s, "
               "stalled %.1f s (attention %.1f, quantise %.1f)%s\n",
               ti, n_tok, cosv(x, emu, D), cosv(x, ref, D), total / 1e6, t_sd / 1e6, t_stall / 1e6,
               t_att / 1e6, t_quant / 1e6, crc_bad ? "  CRC ERRORS" : "");
        selftest(ti);
        if (ti == 0) memcpy(ref0, ref, D * 4);
        heap_caps_free(emu); heap_caps_free(ref);
    }
    // CHATS-4: whole long messages in one pass (references from host/gen_long.py)
    for (int li = 0; li < N_LONG; li++) {
        int nt = tok_encode(&vocab, LONG[li].text, strlen(LONG[li].text), ids, MAXTOK, NULL);
        if (nt < 1 || nt != LONG[li].n_tok) { printf("long %d: %d tokens, host says %d\n", li, nt, LONG[li].n_tok); tok_bad++; continue; }
        n_tok = nt;
        t_sd = t_att = t_quant = t_stall = 0;
        int64_t t0 = esp_timer_get_time();
        embed_in((const uint32_t *)ids);
        pass_fwd();
        printf("long %d: %3d tokens  cos vs emulation %.6f  vs Ollama %.6f  |  %.1f s total, stalled %.1f s "
               "(attention %.1f, quantise %.1f)%s\n", li, n_tok, cosv(x, LONG[li].emu, D), cosv(x, LONG[li].ref, D),
               (esp_timer_get_time() - t0) / 1e6, t_stall / 1e6, t_att / 1e6, t_quant / 1e6, crc_bad ? "  CRC ERRORS" : "");
        selftest(5 + li);
        net_heap("after long pass");
    }

    // CHATS-4: Wi-Fi and TLS next to the model, then test 0 three times with the radio up and an HTTPS
    // request between passes: the network and a pass share internal RAM and both cores.
    net_heap("before wifi");
    if (net_up(20000) == 0) {
        net_heap("wifi up");
        net_https_check("https://sqs.eu-west-2.amazonaws.com/");
        net_heap("after https");
        for (int r = 0; r < 3; r++) {             // with Wi-Fi up, a corrupted pass shows as cos well below 0.999
            n_tok = tok_encode(&vocab, TEXTS[0], strlen(TEXTS[0]), ids, MAXTOK, NULL);
            if (n_tok < 1) { printf("wifi up, pass %d: tokeniser failed\n", r); tok_bad++; break; }
            embed_in((const uint32_t *)ids);
            pass_fwd();
            printf("wifi up, pass %d: test 0 cos vs Ollama %.6f\n", r, cosv(x, ref0, D));
            selftest(0);                          // the conditions the corruption was found under
            net_https_check("https://sqs.eu-west-2.amazonaws.com/");
        }
        net_https_check("https://sqs.eu-west-2.amazonaws.com/");
        net_heap("end");
    }
    int st_skipped = 0;
    for (int i = 0; i < N_SELFTEST; i++) st_skipped += !st_done[i];
    if (st_fail || st_skipped || crc_bad || tok_bad)
        printf("\n!!! SELF-TEST FAILED: %d vectors wrong, %d tests not run, %d CRC and %d tokeniser mismatches. "
               "Don't use this board's results. !!!\n\n", st_fail, st_skipped, crc_bad, tok_bad);
    else if (st_new)
        printf("\n!!! SELF-TEST NOT VERIFIED: %d vectors have no recorded CRC (selftest.h; host/selftest_crcs.py). !!!\n\n", st_new);
    else printf("self-test PASSED: %d vectors bit-exact\n", st_pass);
    printf("== done (%d CRC mismatches, %d tokeniser mismatches) ==\n", crc_bad, tok_bad);
}