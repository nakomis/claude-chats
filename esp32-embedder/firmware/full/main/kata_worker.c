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
#include "freertos/semphr.h"
#include "freertos/task.h"

#include "age.h"
#include "creds.h"
#include "fleet.h"
#include "heapwatch.h"
#include "kw.h"
#include "model.h"
#include "net.h"
#include "ota.h"
#include "selftest.h"
#include "sqs.h"
#include "worker.h"

extern const char root_ca_start[] asm("_binary_AmazonRootCA1_pem_start");

#define TARGET_TOKENS 150             // the planner's soft target: the card-bound knee (README)
#define POOL_MAX 40
#define POOL_BUDGET_MAX (1024 * 1024)
#define PSRAM_RESERVE (128 * 1024)    // left for mbedTLS (in PSRAM; one session measured 46 KB) and the heartbeat
#define POOL_PEAK 5                   // a receive's peak is about this many times its bodies: the
                                      // response, cJSON's tree, our copies, the results built from them
#define PHASE_GAP_S 15                // between SQS phases, at least (each costs a TLS handshake or two)
#define CONTROL_EVERY_S 60            // with no worker: how often to look for commands and publish
#define MIN_INTERNAL (24 * 1024)      // free internal RAM a phase needs, before TLS
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
static int64_t phase_end_us;            // when the last phase ended (net_end)
static bool net_ready;                  // net_setup has run: prov, sqs and the commands queue's URL
static char commands_url[200];          // <thing>-commands, beside the ingest queue (CHATS-13)
static SemaphoreHandle_t net_mx;        // one TLS session at a time: the worker's phases, ota.c's publish
static size_t phase_big;                // the largest request or response of this phase

// Why the chip last started (esp_reset_reason), for the heartbeat and the boot log: so a restart
// nobody saw explains itself (CHATS-13).
const char *reset_name(esp_reset_reason_t r)
{
    switch (r) {
    case ESP_RST_POWERON: return "power-on";
    case ESP_RST_SW: return "software";
    case ESP_RST_PANIC: return "panic";
    case ESP_RST_INT_WDT: return "interrupt-watchdog";
    case ESP_RST_TASK_WDT: return "task-watchdog";
    case ESP_RST_WDT: return "watchdog";
    case ESP_RST_DEEPSLEEP: return "deep-sleep";
    case ESP_RST_BROWNOUT: return "brownout";
    case ESP_RST_SDIO: return "sdio";
    case ESP_RST_USB: return "usb";
    case ESP_RST_JTAG: return "jtag";
    case ESP_RST_EFUSE: return "efuse";
    case ESP_RST_PWR_GLITCH: return "power-glitch";
    case ESP_RST_CPU_LOCKUP: return "cpu-lockup";
    case ESP_RST_EXT: return "external";
    default: return "unknown";
    }
}

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
    jb_printf(b, ",\"reset\":\"%s\"", reset_name(esp_reset_reason()));
    jb_printf(b, ",\"vcore_mv\":%d,\"card\":{\"wait_last_s\":%.1f,\"wait_median_s\":%.1f,\"crc_mismatches\":%d,\"read_retries\":%d}",
              vcore, last / 1e3, med / 1e3, crc, retries);
    if (running) {
        jb_puts(b, ",\"worker\":");
        worker_status_json(&w, b);
        heapwatch_json(b);
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
    xSemaphoreTake(net_mx, portMAX_DELAY);
    heapwatch_paused(1);
    unsigned free_int = (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL);
    if (free_int < MIN_INTERNAL) {
        heapwatch_paused(0);
        xSemaphoreGive(net_mx);
        snprintf(why, n, "internal RAM low: %u bytes free", free_int);
        return -1;
    }
    heapwatch_creds(1);
    int rc = creds_refresh(&creds, now(NULL), prov->cred_endpoint, prov->role_alias, prov->thing_name, creds_get, NULL, why, n);
    heapwatch_creds(0);
    if (rc < 0) { heapwatch_paused(0); xSemaphoreGive(net_mx); return -1; }
    if (rc == 1) printf("worker: new credentials, good until %lld\n", (long long)creds.expires);
    return 0;
}

// ---- CHATS-13: Fleet without MQTT -------------------------------------------------------------

// Fleet's commands, which the IoT rule puts on this board's queue: taken (received and deleted) on
// the phase's SQS session, but acted on only after the session has closed and the outbox has gone
// (run_commands). An update starts a task with an 8 KB internal stack, and a TLS session beside a
// new one took internal RAM down to 5.4 KB (14 Oct). A command is acted on once.
#define MAX_COMMANDS 10
static char *pending[MAX_COMMANDS];
static size_t pending_len[MAX_COMMANDS];
static int n_pending;

static void take_commands(void)
{
    sqs_msg_t *msgs = NULL;
    int n = 0;
    if (sqs_receive(&sqs, commands_url, MAX_COMMANDS - n_pending, 0, 60, &msgs, &n)) {
        printf("fleet: commands: %s\n", sqs.last_error);
        return;
    }
    const char *receipts[MAX_COMMANDS];
    for (int i = 0; i < n; i++) {
        receipts[i] = msgs[i].receipt;
        pending_len[n_pending] = msgs[i].body_len;
        pending[n_pending++] = msgs[i].body;     // ours now (PSRAM)
        msgs[i].body = NULL;
    }
    bool ok[MAX_COMMANDS];
    if (n && sqs_delete_batch(&sqs, commands_url, receipts, n, ok)) printf("fleet: commands: %s\n", sqs.last_error);
    for (int i = 0; i < n; i++) sqs_msg_free(&msgs[i]);
    kw_free(msgs);
}

// No session open: hand each command to ota.c. True if any came.
static bool run_commands(void)
{
    if (!n_pending) return false;
    for (int i = 0; i < n_pending; i++) {
        printf("fleet: command (%u bytes)\n", (unsigned)pending_len[i]);
        ota_handle_command(pending[i], pending_len[i]);
        kw_free(pending[i]);
    }
    n_pending = 0;
    return true;
}

// One HTTPS publish (SigV4, service iotdata) on the keep-alive session *hc: the HTTP status, or -1.
// The topic goes in the path with its slashes as they are, which IoT Core takes, so the canonical
// URI needs none of SigV4's double-encoding.
static int iot_post(esp_http_client_handle_t *hc, const char *topic, bool retain, const char *body, jbuf_t *resp)
{
    char path[128], date[17], authz[320], url[256];
    const char *query = retain ? "qos=1&retain=true" : "qos=1";
    snprintf(path, sizeof path, "/topics/%s", topic);
    kw_amz_date(now(NULL), date);
    sv4_hdr_t h[4] = { { "Content-Type", "application/json" },
                       { "Host", prov->iot_endpoint },
                       { "X-Amz-Date", date },
                       { "X-Amz-Security-Token", creds.token } };
    const sv4_key_t key = { creds.access_key, creds.secret_key, sqs.region, "iotdata" };
    const sv4_req_t sr = { "POST", path, query, h, 4, (const uint8_t *)body, strlen(body) };
    if (sv4_sign(&sr, &key, date, authz, sizeof authz, NULL, NULL)) return -1;
    snprintf(url, sizeof url, "https://%s%s?%s", prov->iot_endpoint, path, query);
    if (!*hc) {
        esp_http_client_config_t c = {
            .url = url,
            .method = HTTP_METHOD_POST,
            .crt_bundle_attach = esp_crt_bundle_attach,
            .timeout_ms = 15000,
            .buffer_size = HTTP_RX_BUF,
            .buffer_size_tx = HTTP_TX_BUF,
            .event_handler = on_http,
        };
        if (!(*hc = esp_http_client_init(&c))) return -1;
    } else {
        esp_http_client_set_url(*hc, url);
    }
    esp_http_client_set_user_data(*hc, resp);
    jb_reset(resp);
    for (int i = 0; i < 4; i++)
        if (i != 1) esp_http_client_set_header(*hc, h[i].name, h[i].value);
    esp_http_client_set_header(*hc, "Authorization", authz);
    esp_http_client_set_post_field(*hc, body, (int)strlen(body));
    esp_err_t e = esp_http_client_perform(*hc);
    esp_http_client_set_post_field(*hc, NULL, 0);
    if (e != ESP_OK) {
        esp_http_client_close(*hc);
        return -1;
    }
    return esp_http_client_get_status_code(*hc);
}

// The outbox (fleet.c), after the SQS session has closed: never two sessions at once. What fails
// goes back for the next phase. Needs valid credentials (a phase has just refreshed them).
static void publish_outbox(void)
{
    char *status = NULL, *msgs[FLEET_OUTBOX];
    int n = fleet_take(&status, msgs, FLEET_OUTBOX);
    if (!status && !n) return;
    heapwatch_iotpub(1);
    esp_http_client_handle_t hc = NULL;
    jbuf_t resp;
    jb_init(&resp);
    int sent = 0, st = 0;
    for (; sent < n; sent++) {
        if ((st = iot_post(&hc, fleet_responses_topic(), false, msgs[sent], &resp)) != 200) break;
        kw_free(msgs[sent]);
    }
    if (sent == n && status) {
        if ((st = iot_post(&hc, fleet_status_topic(), true, status, &resp)) == 200) {
            kw_free(status);
            status = NULL;
        }
    }
    if (hc) esp_http_client_cleanup(hc);
    heapwatch_iotpub(0);
    if (st != 200) {
        printf("fleet: publish failed: HTTP %d %.*s\n", st, (int)(resp.n < 200 ? resp.n : 200), resp.p ? resp.p : "");
        fleet_put_back(status, msgs + sent, n - sent);
    } else {
        static bool first = true;
        if (first) {
            first = false;
            ota_on_connected();               // reached IoT Core: half of what keeps a new image
            jb_free(&resp);
            publish_outbox();                 // and its version response goes now, not a phase later
            return;
        }
    }
    jb_free(&resp);
}

static void net_end(void *ctx)
{
    (void)ctx;
    take_commands();
    if (sqs_http) {
        esp_http_client_cleanup(sqs_http);
        sqs_http = NULL;
    }
    publish_outbox();
    // Commands last, with no session open; what they answer goes out now. (An update's task then
    // waits for the worker to park, in its next step.)
    if (run_commands()) publish_outbox();
    // The first few phases, any that moved a big body (the bring-up check: a ~40 KB inline job), then
    // about hourly. The low point is the one to watch.
    static int phases;
    if (phases++ < 3 || phase_big > 64 * 1024 || phases % 60 == 0) {
        printf("worker: phase moved up to %u bytes in one request or response\n", (unsigned)phase_big);
        net_heap("worker: after an SQS phase");
    }
    heapwatch_paused(0);
    phase_end_us = esp_timer_get_time();
    xSemaphoreGive(net_mx);
}

static int tokenise(void *ctx, const char *text, size_t len, int32_t *ids, int max)
{
    (void)ctx;
    return model_tokenise(text, len, ids, max);
}

static int embed(void *ctx, const int32_t *ids, const int *lens, int n, float *vecs, worker_pass_info_t *info)
{
    (void)ctx;
    heapwatch_pass(1);
    int rc = model_embed(ids, lens, n, vecs, info);
    heapwatch_pass(0);
    return rc;
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

// ---- the network, for the worker and for Fleet ----------------------------------------------------

// What a phase needs, once: the provisioned endpoints, the SQS client and the commands queue's URL,
// which sits beside the ingest queue (<base>/<thing>-commands, made by the device stack). NULL, or
// why the board can't reach AWS.
static const char *net_setup(const prov_t *p, bool online)
{
    if (net_ready) return NULL;
    prov = p;
    const char *why = !online ? "offline"
                      : !p->has_identity ? "no IoT identity provisioned"
                      : !p->cred_endpoint[0] || !p->role_alias[0] ? "no credentials endpoint provisioned"
                      : !p->queue_url[0] || !p->results_url[0] ? "no queues provisioned"
                      : NULL;
    if (why) return why;
    if (sqs_init(&sqs, p->queue_url, &creds, now, sqs_post, NULL)) return "the queue URL isn't SQS's";
    const char *slash = strrchr(p->queue_url, '/');
    snprintf(commands_url, sizeof commands_url, "%.*s/%s-commands", (int)(slash - p->queue_url), p->queue_url, p->thing_name);
    if (!net_mx && !(net_mx = xSemaphoreCreateMutex())) return "out of memory";
    net_ready = true;
    return NULL;
}

// A phase with no jobs in it: commands in, the outbox out.
static void control_phase(void)
{
    char why[160];
    if (net_begin(NULL, why, sizeof why)) {
        printf("fleet: no network phase: %s\n", why);
        return;
    }
    net_end(NULL);
}

void kata_worker_hello(const prov_t *p, bool online)
{
    const char *why = net_setup(p, online);
    if (why) {
        printf("fleet: Fleet can't reach this board: %s (infra/scripts/provision.sh)\n", why);
        return;
    }
    fleet_status_now();
    control_phase();
}

void kata_worker_publish(void)
{
    if (net_ready) control_phase();
}

void kata_worker_control_loop(void)
{
    if (!net_ready) return;
    printf("fleet: no worker: looking for commands every %d s\n", CONTROL_EVERY_S);
    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(CONTROL_EVERY_S * 1000));
        control_phase();
    }
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
    if ((why_not = net_setup(p, online))) {
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
        // At least PHASE_GAP_S between phases, however busy the queue (a sick board forwarding a
        // backlog has no passes to space them out): each one costs a TLS handshake or two.
        while (esp_timer_get_time() - phase_end_us < (int64_t)PHASE_GAP_S * 1000000) {
            vTaskDelay(pdMS_TO_TICKS(500));
            last_tick_us = esp_timer_get_time();
        }
        last_tick_us = esp_timer_get_time();
        int wait = worker_step(&w);
        ota_spawn_due(worker_is_parked(&w));  // an update's task starts once nothing else is on the network
        fleet_status_now();                   // after a pass: published at the end of the next phase
        // An update asking the worker to park cuts the wait short
        for (int s = 0; s < wait && !(w.park_requested && !w.parked); s++) {
            vTaskDelay(pdMS_TO_TICKS(1000));
            last_tick_us = esp_timer_get_time();
        }
    }
}

bool kata_worker_request_park(void)
{
    if (!running) return false;
    worker_request_park(&w);
    return true;
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
