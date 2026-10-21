// Kata's metrics to the house VictoriaMetrics (CHATS-20). The design, and much of the code, is
// tinycrypt's metrics.c (CRYPT-28; CC0). See metrics.h.
//
// One InfluxDB line-protocol POST a minute to vmauth on Cal (prov's metrics_url + "/write"), plain
// HTTP with basic auth: no TLS on the LAN (house rule), which also costs no internal RAM, and the
// password is write-only and assumed public anyway (it sits in plain flash). vmauth has a user per
// device and pins host_name to it server-side, so a leaked password can only write as this board.
// VictoriaMetrics names the series kata_<field>. Integer fields only; a field with nothing to say
// (rssi when not associated, temp_c with no sensor reading) is left out.
//
//   kata,host_name=<thing>,version=<x.y.z> uptime_s=…i,heap_free=…i,heap_largest=…i,heap_lowest=…i,
//       psram_free=…i,rssi=…i,temp_c=…i,vcore_mv=…i,card_wait_last_ms=…i,card_wait_median_ms=…i,
//       card_crc_mismatches=…i,card_read_retries=…i,worker_running=…i,worker_sick=…i,stalled_s=…i,
//       crashes_in_a_row=…i,messages_total=…i,messages_today=…i,selftest_ok=…i,model_ready=…i
//
// messages_total is the jobs embedded since boot: it restarts at 0 with the board, which rate() reads
// as a counter reset. messages_today is today's (UTC) count.
//
// Never in the way (CHATS-4: internal RAM bottoms out at ~12.8 KB free in an SQS phase): the task's
// stack is in PSRAM and the line is built on it. The POST runs under kata_worker_gate, as statusd's
// replies do, so no phase starts while the HTTP client holds its few KB of internal RAM; a minute
// with a phase under way (or an update, or little internal RAM free) is skipped, not queued. A failed
// POST is dropped, not retried, with one log line per run of failures. Each step times out in 1.5 s,
// so a dead vmauth can't hold the gate past the 5 s a phase waits for it (net_begin).
//
// Each POST is a new connection, and Kata closes first, so a TIME_WAIT pcb sits in internal RAM for
// two minutes: one or two at a time, a few hundred bytes. SO_LINGER (on, for statusd) would make it an RST.
#include "metrics.h"

#include <inttypes.h>
#include <math.h>
#include <stdio.h>
#include <string.h>

#include "esp_app_desc.h"
#include "esp_heap_caps.h"
#include "esp_http_client.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/idf_additions.h"
#include "freertos/task.h"
#include "mbedtls/base64.h"

#include "fleet.h"
#include "heapwatch.h"
#include "kata_worker.h"
#include "metrics_line.h"
#include "model.h"

#define METRICS_EVERY_S 60
#define FIRST_DELAY_MS 45000             // let Wi-Fi, Fleet and the boot self-test settle first
#define METRICS_STACK 6144               // in PSRAM (so free): the HTTP client is the deep part, plus the line and fields
#define METRICS_PRIO 6                   // as statusd: compute tasks (5) would starve anything lower for a pass
#define METRICS_CORE 0                   // away from lwIP's tcpip task (core 1)
#define MIN_INTERNAL (24 * 1024)         // free internal RAM to start a POST (a phase's low is ~12.8 KB)
#define GATE_WAIT_MS 200                 // a phase in progress holds the gate: skip the minute, don't wait
#define TIMEOUT_MS 1500                  // per connect/write/read: their sum stays under net_begin's 5 s gate wait

static const prov_t *prov;
static char url[sizeof(((prov_t *)0)->metrics_url) + 8];
static char auth[6 + 4 * ((sizeof(((prov_t *)0)->metrics_user) + sizeof(((prov_t *)0)->metrics_pass) + 2) / 3) + 1];

static bool post(const char *body, int len, int *status)
{
    esp_http_client_config_t cfg = {
        .url = url,
        .method = HTTP_METHOD_POST,
        .timeout_ms = TIMEOUT_MS,
        .disable_auto_redirect = true,   // the credential goes to vmauth and nowhere else
        .buffer_size = 256,
        .buffer_size_tx = 512,
    };
    esp_http_client_handle_t client = esp_http_client_init(&cfg);
    if (!client) return false;
    esp_http_client_set_header(client, "Content-Type", "text/plain");
    if (auth[0]) esp_http_client_set_header(client, "Authorization", auth);
    esp_http_client_set_post_field(client, body, len);
    esp_err_t err = esp_http_client_perform(client);
    *status = err == ESP_OK ? esp_http_client_get_status_code(client) : -1;
    esp_http_client_cleanup(client);
    return err == ESP_OK && *status / 100 == 2;
}

// Builds the line; its length, or -1.
static int build_line(char *out, size_t cap)
{
    kata_worker_metrics_t m;
    kata_worker_metrics(&m);
    int crc = 0, retries = 0;
    model_card_health(&crc, &retries);
    const char *st = fleet_selftest();
    wifi_ap_record_t ap;
    bool assoc = esp_wifi_sta_get_ap_info(&ap) == ESP_OK;

    metrics_field_t f[24];
    size_t n = 0;
#define FIELD(name, v) f[n++] = (metrics_field_t){ name, (int64_t)(v) }
    FIELD("uptime_s", esp_timer_get_time() / 1000000);
    FIELD("heap_free", heap_caps_get_free_size(MALLOC_CAP_INTERNAL));
    FIELD("heap_largest", heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL));
    FIELD("heap_lowest", heapwatch_lowest());
    FIELD("psram_free", heap_caps_get_free_size(MALLOC_CAP_SPIRAM));
    if (assoc) FIELD("rssi", ap.rssi);
    if (m.have_temp) FIELD("temp_c", lroundf(m.temp_c));
    FIELD("vcore_mv", m.vcore_mv);
    FIELD("card_wait_last_ms", m.card_wait_last_ms);
    FIELD("card_wait_median_ms", m.card_wait_median_ms);
    FIELD("card_crc_mismatches", crc);
    FIELD("card_read_retries", retries);
    FIELD("worker_running", m.running);
    FIELD("worker_sick", m.sick);
    FIELD("stalled_s", m.stalled_s);
    FIELD("crashes_in_a_row", model_crash_streak());
    FIELD("messages_total", m.messages_boot);
    FIELD("messages_today", m.messages_today);
    // "cos" passes too: the NOT VERIFIED fallback for a build whose CRCs aren't recorded yet.
    FIELD("selftest_ok", !strcmp(st, "bit-exact") || !strcmp(st, "cos"));
    FIELD("model_ready", model_loaded());
#undef FIELD
    return metrics_line(out, cap, "kata", prov->thing_name, esp_app_get_description()->version, f, n);
}

static void metrics_task(void *arg)
{
    (void)arg;
    bool failing = false;
    vTaskDelay(pdMS_TO_TICKS(FIRST_DELAY_MS));
    for (;;) {
        // No phase starts while the gate is held, so the check and the POST can't straddle one.
        if (kata_worker_gate(GATE_WAIT_MS)) {
            if (!kata_worker_in_phase() && strcmp(model_state(), "updating") &&
                heap_caps_get_free_size(MALLOC_CAP_INTERNAL) >= MIN_INTERNAL) {
                char line[640];              // on this task's PSRAM stack; a full line is about 450
                int len = build_line(line, sizeof line);
                int status = 0;
                if (len < 0) printf("metrics: the line didn't build or fit\n");
                else if (post(line, len, &status)) {
                    if (failing) printf("metrics: sending again\n");
                    failing = false;
                } else if (!failing) {
                    failing = true;          // once per run of failures: Cal reboots, and that's fine
                    printf("metrics: POST %s failed (HTTP %d): dropping metrics until it works\n", url, status);
                }
            }
            kata_worker_ungate();
        }
        vTaskDelay(pdMS_TO_TICKS(METRICS_EVERY_S * 1000));
    }
}

void metrics_start(const prov_t *p)
{
    if (!p || !p->metrics_url[0] || !p->thing_name[0]) {
        printf("metrics: no metrics URL (or Thing) in prov: metrics off\n");
        return;
    }
    prov = p;
    snprintf(url, sizeof url, "%s/write", p->metrics_url);
    auth[0] = 0;
    if (p->metrics_user[0]) {
        char creds[sizeof p->metrics_user + sizeof p->metrics_pass + 1];
        int n = snprintf(creds, sizeof creds, "%s:%s", p->metrics_user, p->metrics_pass);
        size_t olen = 0;
        memcpy(auth, "Basic ", 6);
        if (mbedtls_base64_encode((unsigned char *)auth + 6, sizeof auth - 6, &olen, (unsigned char *)creds, (size_t)n) != 0)
            auth[0] = 0;
        memset(creds, 0, sizeof creds);
    }
    if (xTaskCreatePinnedToCoreWithCaps(metrics_task, "metrics", METRICS_STACK, NULL, METRICS_PRIO, NULL, METRICS_CORE,
                                        MALLOC_CAP_SPIRAM) != pdPASS)
        printf("metrics: no task: metrics off\n");
    else
        printf("metrics: every %d s to %s as %s\n", METRICS_EVERY_S, url, p->metrics_user[0] ? p->metrics_user : "(no user)");
}
