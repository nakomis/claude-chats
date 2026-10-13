// Kata the worker on the board (kata_worker.h, CHATS-5).
#include "kata_worker.h"

#include <stdio.h>
#include <string.h>
#include <time.h>

#include "driver/temperature_sensor.h"
#include "esp_app_desc.h"
#include "esp_crt_bundle.h"
#include "esp_heap_caps.h"
#include "esp_http_client.h"
#include "esp_random.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "age.h"
#include "creds.h"
#include "fleet.h"
#include "kw.h"
#include "model.h"
#include "net.h"
#include "selftest.h"
#include "sqs.h"
#include "worker.h"

extern const char root_ca_start[] asm("_binary_AmazonRootCA1_pem_start");

#define TARGET_TOKENS 150             // the planner's soft target: the card-bound knee (README)
#define POOL_MAX 40
#define POOL_BUDGET_MAX (1024 * 1024)
#define PSRAM_RESERVE (384 * 1024)    // left for mbedTLS (in PSRAM), JSON parsing and the heartbeat
#define POOL_PEAK 5                   // a receive's peak is about this many times its bodies: the
                                      // response, cJSON's tree, our copies, the results built from them
#define MQTT_MIN_S 15                 // MQTT's turn between SQS phases, at least: commands and updates
#define MIN_INTERNAL (24 * 1024)      // free internal RAM a phase needs, MQTT paused, before TLS
#define HTTP_TIMEOUT_MS 30000         // longer than SQS's 20 s long poll
#define HTTP_RX_BUF 2048              // internal RAM, only while a phase runs
#define HTTP_TX_BUF 3072              // the request line and headers: the session token is ~1 KB
#define WATCHDOG_S (15 * 60)          // no step for this long: restart (nothing's lost: see worker.h)

static worker_t w;
static volatile bool running;
static const prov_t *prov;
static aws_creds_t creds;
static age_identity_t identity;
static sqs_t sqs;
static esp_http_client_handle_t sqs_http;
static volatile int64_t last_tick_us;
static temperature_sensor_handle_t tsens;
static int vcore;
static const char *why_not = "starting";
static int64_t mqtt_back_us;            // when MQTT last came back (net_end)
static size_t phase_big;                // the largest request or response of this phase

// ---- the heartbeat's fields (fleet.h; the esp_timer task) ----------------------------------------
static void status_extra(jbuf_t *b)
{
    float t = 0;
    bool have_t = tsens && temperature_sensor_get_celsius(tsens, &t) == ESP_OK;
    int crc = 0, retries = 0;
    model_card_health(&crc, &retries);
    uint32_t last = 0, med = 0;
    if (running) worker_card_wait(&w, &last, &med);
    if (have_t) jb_printf(b, ",\"temp_c\":%.1f", t);
    jb_printf(b, ",\"vcore_mv\":%d,\"card\":{\"wait_last_s\":%.1f,\"wait_median_s\":%.1f,\"crc_mismatches\":%d,\"read_retries\":%d}",
              vcore, last / 1e3, med / 1e3, crc, retries);
    if (running) {
        jb_puts(b, ",\"worker\":");
        worker_status_json(&w, b);
        if (creds.expires) jb_printf(b, ",\"creds_expire_at\":%lld", (long long)creds.expires);
    } else {
        jb_printf(b, ",\"worker\":{\"state\":\"off\",\"why\":\"%s\",\"embedder\":\"%s\"}", why_not, EMBEDDER_TAG);
    }
}

void kata_worker_status_init(int vcore_mv)
{
    vcore = vcore_mv;
    temperature_sensor_config_t tc = TEMPERATURE_SENSOR_CONFIG_DEFAULT(-10, 80);
    if (temperature_sensor_install(&tc, &tsens) != ESP_OK || temperature_sensor_enable(tsens) != ESP_OK) {
        printf("worker: no temperature sensor\n");
        tsens = NULL;
    }
    fleet_set_status_extra(status_extra);
}

// ---- worker_io_t ---------------------------------------------------------------------------
static int64_t now(void *ctx) { (void)ctx; return (int64_t)time(NULL); }

static int rng(void *ctx, unsigned char *out, size_t n)
{
    (void)ctx;
    esp_fill_random(out, n);
    return 0;
}

static esp_err_t on_http(esp_http_client_event_t *e)
{
    if (e->event_id == HTTP_EVENT_ON_DATA && e->user_data) jb_put(e->user_data, e->data, (size_t)e->data_len);
    return ESP_OK;
}

// SQS: one session per phase, kept alive between its requests (sqs.h's transport)
static int sqs_post(void *ctx, const http_req_t *r, jbuf_t *resp)
{
    (void)ctx;
    if (!sqs_http) {
        char url[96];
        snprintf(url, sizeof url, "https://%s/", r->host);
        esp_http_client_config_t c = {
            .url = url,
            .method = HTTP_METHOD_POST,
            .crt_bundle_attach = esp_crt_bundle_attach,
            .timeout_ms = HTTP_TIMEOUT_MS,
            .buffer_size = HTTP_RX_BUF,
            .buffer_size_tx = HTTP_TX_BUF,
            .event_handler = on_http,
        };
        sqs_http = esp_http_client_init(&c);
        if (!sqs_http) { printf("worker: SQS client init failed\n"); return -1; }
    }
    esp_http_client_set_user_data(sqs_http, resp);
    if (r->body_len > phase_big) phase_big = r->body_len;
    for (int i = 0; i < r->n_hdrs; i++)
        if (strcasecmp(r->hdrs[i].name, "Host")) esp_http_client_set_header(sqs_http, r->hdrs[i].name, r->hdrs[i].value);
    esp_http_client_set_post_field(sqs_http, r->body, (int)r->body_len);
    esp_err_t e = esp_http_client_perform(sqs_http);
    esp_http_client_set_post_field(sqs_http, NULL, 0);
    if (e != ESP_OK) {
        printf("worker: SQS request failed: %s (internal free %u)\n", esp_err_to_name(e),
               (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL));
        esp_http_client_close(sqs_http);    // the next request reconnects
        return -1;
    }
    if (resp->n > phase_big) phase_big = resp->n;
    return esp_http_client_get_status_code(sqs_http);
}

// The credentials provider: mTLS with the board's certificate, one request, closed (creds.h)
static int creds_get(void *ctx, const char *host, const char *path, const sv4_hdr_t *h, int nh, jbuf_t *resp)
{
    (void)ctx;
    char url[256];
    snprintf(url, sizeof url, "https://%s%s", host, path);
    esp_http_client_config_t c = {
        .url = url,
        .cert_pem = root_ca_start,
        .client_cert_pem = prov->device_cert,
        .client_key_pem = prov->device_key,
        .timeout_ms = 15000,
        .buffer_size = HTTP_RX_BUF,
        .event_handler = on_http,
        .user_data = resp,
    };
    esp_http_client_handle_t hc = esp_http_client_init(&c);
    if (!hc) return -1;
    for (int i = 0; i < nh; i++) esp_http_client_set_header(hc, h[i].name, h[i].value);
    esp_err_t e = esp_http_client_perform(hc);
    int status = e == ESP_OK ? esp_http_client_get_status_code(hc) : -1;
    if (e != ESP_OK) printf("worker: credentials provider: %s\n", esp_err_to_name(e));
    esp_http_client_cleanup(hc);
    return status;
}

static int net_begin(void *ctx, char *why, size_t n)
{
    (void)ctx;
    phase_big = 0;
    if (!net_time_ok(5000)) { snprintf(why, n, "no time from SNTP yet (SigV4 needs it)"); return -1; }
    fleet_pause();
    unsigned free_int = (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL);
    if (free_int < MIN_INTERNAL) {
        fleet_resume();
        snprintf(why, n, "internal RAM low: %u bytes free with MQTT paused", free_int);
        return -1;
    }
    int rc = creds_refresh(&creds, now(NULL), prov->cred_endpoint, prov->role_alias, prov->thing_name, creds_get, NULL, why, n);
    if (rc < 0) { fleet_resume(); return -1; }
    if (rc == 1) printf("worker: new credentials, good until %lld\n", (long long)creds.expires);
    return 0;
}

static void net_end(void *ctx)
{
    (void)ctx;
    if (sqs_http) {
        esp_http_client_cleanup(sqs_http);
        sqs_http = NULL;
    }
    // The first few phases, any that moved a big body (the bring-up check: a ~200 KB inline job), then
    // about hourly. The low point is the one to watch.
    static int phases;
    if (phases++ < 3 || phase_big > 64 * 1024 || phases % 60 == 0) {
        printf("worker: phase moved up to %u bytes in one request or response\n", (unsigned)phase_big);
        net_heap("worker: after an SQS phase");
    }
    fleet_resume();
    mqtt_back_us = esp_timer_get_time();
}

static int tokenise(void *ctx, const char *text, size_t len, int32_t *ids, int max)
{
    (void)ctx;
    return model_tokenise(text, len, ids, max);
}

static int embed(void *ctx, const int32_t *ids, const int *lens, int n, float *vecs, worker_pass_info_t *info)
{
    (void)ctx;
    return model_embed(ids, lens, n, vecs, info);
}

static int health(void *ctx)
{
    (void)ctx;
    return model_health();
}

static void wlog(void *ctx, const char *msg)
{
    (void)ctx;
    printf("%s\n", msg);
}

// ---- the loop ------------------------------------------------------------------------------
static void watchdog(void *arg)
{
    (void)arg;
    if (esp_timer_get_time() - last_tick_us > (int64_t)WATCHDOG_S * 1000000) {
        printf("worker: no step for %d minutes: restarting (every job is still on a queue)\n", WATCHDOG_S / 60);
        esp_restart();
    }
}

void kata_worker_run(const prov_t *p, bool online, bool healthy)
{
    prov = p;
    why_not = !online ? "offline"
              : !p->has_identity ? "no IoT identity provisioned"
              : !p->cred_endpoint[0] || !p->role_alias[0] ? "no credentials endpoint provisioned"
              : !p->queue_url[0] || !p->results_url[0] ? "no queues provisioned"
              : NULL;
    if (why_not) {
        printf("worker: not starting: %s (infra/scripts/provision.sh)\n", why_not);
        return;
    }
    age_set_rng(rng, NULL);
    const age_identity_t *id = NULL;
    if (p->age_id && age_identity_parse(p->age_id, &identity) == 0) id = &identity;
    else printf("worker: no usable age identity: every job goes to Cal as decrypt-failed (age-identity.sh, provision.sh)\n");
    if (p->age_id) {                          // parsed: the text form isn't needed again
        kw_wipe_free(p->age_id, strlen(p->age_id));
        ((prov_t *)p)->age_id = NULL;
    }
    if (sqs_init(&sqs, p->queue_url, &creds, now, sqs_post, NULL)) {
        why_not = "the queue URL isn't SQS's";
        printf("worker: not starting: %s: %s\n", why_not, p->queue_url);
        return;
    }
    // The pool's budget: what PSRAM can spare, as a receive's worst case (several bodies at once,
    // the response, cJSON's copy, ours, and the results built from them) is about POOL_PEAK times.
    size_t free_ps = heap_caps_get_free_size(MALLOC_CAP_SPIRAM);
    size_t budget = free_ps > PSRAM_RESERVE ? (free_ps - PSRAM_RESERVE) / POOL_PEAK : 0;
    if (budget > POOL_BUDGET_MAX) budget = POOL_BUDGET_MAX;
    if (budget < WORKER_WORST_BODY) {
        why_not = "not enough PSRAM for one job";
        printf("worker: not starting: %s (%u free)\n", why_not, (unsigned)free_ps);
        return;
    }
    const worker_cfg_t cfg = { p->queue_url, p->results_url, p->thing_name, esp_app_get_description()->version, EMBEDDER_TAG,
                               id, TARGET_TOKENS, budget, POOL_MAX };
    const worker_io_t io = { NULL, now, net_begin, net_end, &sqs, tokenise, embed, health, wlog };
    if (worker_init(&w, &cfg, &io)) { why_not = "out of memory"; printf("worker: not starting: out of memory\n"); return; }
    if (!healthy) worker_set_sick(&w, "the boot self-test failed");
    printf("worker: %s as %s, tag %s; pool budget %u KB (PSRAM free %u KB), target %d tokens\n", w.sick ? "starting SICK" : "starting",
           p->thing_name, EMBEDDER_TAG, (unsigned)(budget / 1024), (unsigned)(free_ps / 1024), TARGET_TOKENS);
    last_tick_us = esp_timer_get_time();
    running = true;
    esp_timer_handle_t wd;
    const esp_timer_create_args_t wa = { .callback = watchdog, .name = "worker_wd" };
    if (esp_timer_create(&wa, &wd) == ESP_OK) esp_timer_start_periodic(wd, 60ULL * 1000000);
    for (;;) {
        // MQTT gets at least MQTT_MIN_S between SQS phases, however busy the queue (a sick board
        // forwarding a backlog has no passes to give it time), so commands and updates get through.
        while (esp_timer_get_time() - mqtt_back_us < (int64_t)MQTT_MIN_S * 1000000) {
            vTaskDelay(pdMS_TO_TICKS(500));
            last_tick_us = esp_timer_get_time();
        }
        last_tick_us = esp_timer_get_time();
        int wait = worker_step(&w);
        fleet_status_now();                   // after a pass (the heartbeat also goes on every reconnect)
        // MQTT's turn; an update asking the worker to park cuts it short
        for (int s = 0; s < wait && !(w.park_requested && !w.parked); s++) {
            vTaskDelay(pdMS_TO_TICKS(1000));
            last_tick_us = esp_timer_get_time();
        }
    }
}

bool kata_worker_park(int timeout_ms)
{
    if (!running) return true;
    worker_request_park(&w);
    for (int t = 0; t < timeout_ms && !worker_is_parked(&w); t += 500) vTaskDelay(pdMS_TO_TICKS(500));
    return worker_is_parked(&w);
}

void kata_worker_resume(void)
{
    if (running) worker_resume(&w);
}
