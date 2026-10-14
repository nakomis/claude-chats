// CHATS-4: Wi-Fi station plus one HTTPS request, to measure what the network stack costs
// in internal SRAM next to the model. Credentials come from the prov partition (prov.c, CHATS-10),
// never the build. The station reconnects by itself; MQTT (fleet.c) rides on it.
#include <stdio.h>
#include <string.h>
#include <time.h>

#include "esp_crt_bundle.h"
#include "esp_event.h"
#include "esp_heap_caps.h"
#include "esp_http_client.h"
#include "esp_netif.h"
#include "esp_netif_sntp.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"

#include "heapwatch.h"
#include "net.h"
#include "prov.h"

#define GOT_IP BIT0
static EventGroupHandle_t ev;

static void on_event(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    if (base == WIFI_EVENT && (id == WIFI_EVENT_STA_START || id == WIFI_EVENT_STA_DISCONNECTED)) {
        xEventGroupClearBits(ev, GOT_IP);
        esp_wifi_connect();
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *e = data;
        printf("wifi: got " IPSTR "\n", IP2STR(&e->ip_info.ip));
        xEventGroupSetBits(ev, GOT_IP);
    }
}

void net_heap(const char *when)
{
    printf("heap %s: internal free %u (largest %u, lowest ever %u), PSRAM free %u\n", when,
           (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL),
           (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL),
           (unsigned)heapwatch_lowest(),
           (unsigned)heap_caps_get_free_size(MALLOC_CAP_SPIRAM));
}

int net_up(const prov_t *prov, int timeout_ms)
{
    int64_t t = esp_timer_get_time();
    esp_err_t e;
    ev = xEventGroupCreate();
    if (esp_netif_init() != ESP_OK || esp_event_loop_create_default() != ESP_OK) { printf("wifi: netif init failed\n"); return -1; }
    esp_netif_create_default_wifi_sta();
    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    cfg.nvs_enable = 0;                       // no NVS partition, and nothing worth keeping
    if ((e = esp_wifi_init(&cfg)) != ESP_OK) { printf("wifi: init failed: %s\n", esp_err_to_name(e)); return -1; }
    esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, on_event, NULL);
    esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, on_event, NULL);
    wifi_config_t wc = { 0 };
    memcpy(wc.sta.ssid, prov->wifi_ssid, sizeof wc.sta.ssid);          // 32 + NUL, 64 + NUL: both fit
    memcpy(wc.sta.password, prov->wifi_password, sizeof wc.sta.password);
    esp_wifi_set_mode(WIFI_MODE_STA);
    esp_wifi_set_config(WIFI_IF_STA, &wc);
    if ((e = esp_wifi_start()) != ESP_OK) { printf("wifi: start failed: %s\n", esp_err_to_name(e)); return -1; }
    if (!(xEventGroupWaitBits(ev, GOT_IP, pdFALSE, pdTRUE, pdMS_TO_TICKS(timeout_ms)) & GOT_IP)) {
        printf("wifi: no IP after %d ms\n", timeout_ms);
        return -1;
    }
    printf("wifi: up in %.1f s\n", (esp_timer_get_time() - t) / 1e6);
    // CHATS-5: SigV4 needs the time. SNTP keeps it from here on; net_time_ok says when it's set.
    esp_sntp_config_t sntp = ESP_NETIF_SNTP_DEFAULT_CONFIG("pool.ntp.org");
    if (esp_netif_sntp_init(&sntp) != ESP_OK) printf("sntp: didn't start: the worker can't sign requests\n");
    return 0;
}

int net_https_check(const char *url)
{
    int64_t t = esp_timer_get_time();
    esp_http_client_config_t c = { .url = url, .crt_bundle_attach = esp_crt_bundle_attach, .timeout_ms = 10000 };
    esp_http_client_handle_t h = esp_http_client_init(&c);
    if (!h) { printf("https: client init failed\n"); return -1; }
    esp_err_t e = esp_http_client_perform(h);
    int status = esp_http_client_get_status_code(h);
    esp_http_client_cleanup(h);
    if (e != ESP_OK) { printf("https: %s failed: %s\n", url, esp_err_to_name(e)); return -1; }
    printf("https: %s -> HTTP %d in %.2f s (TLS ok)\n", url, status, (esp_timer_get_time() - t) / 1e6);
    return status;
}


bool net_time_ok(int wait_ms)
{
    if (time(NULL) > 1700000000) return true;   // set already (late 2023 is "set")
    if (wait_ms > 0) esp_netif_sntp_sync_wait(pdMS_TO_TICKS(wait_ms));
    return time(NULL) > 1700000000;
}
