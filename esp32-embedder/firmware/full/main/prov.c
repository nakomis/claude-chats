// The board's provisioned settings (CHATS-10). See prov.h.
#include "prov.h"

#include <stdio.h>
#include <string.h>

#include "esp_heap_caps.h"
#include "nvs.h"
#include "nvs_flash.h"

#define PARTITION "prov"
#define NAMESPACE "kata"       // as infra/scripts/provision.sh writes it

static bool get_fixed(nvs_handle_t h, const char *key, char *out, size_t size, bool quiet)
{
    size_t len = size;
    esp_err_t e = nvs_get_str(h, key, out, &len);
    if (e != ESP_OK && !quiet) printf("prov: %s: %s\n", key, esp_err_to_name(e));
    return e == ESP_OK;
}

// A PEM blob, in PSRAM: internal RAM is for the model and the network (CHATS-4).
static char *get_alloc(nvs_handle_t h, const char *key)
{
    size_t len = 0;
    esp_err_t e = nvs_get_str(h, key, NULL, &len);
    char *out = e == ESP_OK ? heap_caps_malloc(len, MALLOC_CAP_SPIRAM) : NULL;
    if (out && nvs_get_str(h, key, out, &len) != ESP_OK) {
        heap_caps_free(out);
        out = NULL;
    }
    if (!out) printf("prov: %s: %s\n", key, e == ESP_OK ? "out of memory" : esp_err_to_name(e));
    return out;
}

bool prov_load(prov_t *prov)
{
    memset(prov, 0, sizeof *prov);
    nvs_handle_t h;
    esp_err_t e = nvs_flash_init_partition(PARTITION);
    if (e == ESP_OK) e = nvs_open_from_partition(PARTITION, NAMESPACE, NVS_READONLY, &h);
    if (e != ESP_OK) {
        printf("prov: not provisioned (%s): run infra/scripts/provision.sh, then flash.sh\n", esp_err_to_name(e));
        return false;
    }
    bool ok = get_fixed(h, "wifi_ssid", prov->wifi_ssid, sizeof prov->wifi_ssid, false);
    ok &= get_fixed(h, "wifi_pass", prov->wifi_password, sizeof prov->wifi_password, false);
    if (!ok) {
        nvs_close(h);
        memset(prov, 0, sizeof *prov);
        printf("prov: incomplete: run infra/scripts/provision.sh\n");
        return false;
    }
    // Identity: all of it or none of it.
    bool id = get_fixed(h, "endpoint", prov->iot_endpoint, sizeof prov->iot_endpoint, false);
    id &= get_fixed(h, "thing", prov->thing_name, sizeof prov->thing_name, false);
    if (id) {
        prov->device_cert = get_alloc(h, "cert");
        prov->device_key = get_alloc(h, "key");
        id = prov->device_cert && prov->device_key;
    }
    get_fixed(h, "cred_ep", prov->cred_endpoint, sizeof prov->cred_endpoint, true);
    get_fixed(h, "role_alias", prov->role_alias, sizeof prov->role_alias, true);
    get_fixed(h, "queue_url", prov->queue_url, sizeof prov->queue_url, true);
    get_fixed(h, "results_url", prov->results_url, sizeof prov->results_url, true);
    // CHATS-16: a list that's there but doesn't fit mustn't read as no list (the whole subnet): "!"
    // fails statusd's parse, and statusd then refuses everyone.
    size_t allow_len = 0;
    if (nvs_get_str(h, "status_allow", NULL, &allow_len) == ESP_OK &&
        !get_fixed(h, "status_allow", prov->status_allow, sizeof prov->status_allow, false))
        strcpy(prov->status_allow, "!");
    size_t age_len = 0;
    if (nvs_get_str(h, "age_id", NULL, &age_len) == ESP_OK) prov->age_id = get_alloc(h, "age_id");
    nvs_close(h);
    if (!id) {
        heap_caps_free(prov->device_cert);
        heap_caps_free(prov->device_key);
        prov->device_cert = prov->device_key = NULL;
        prov->iot_endpoint[0] = prov->thing_name[0] = 0;
        printf("prov: Wi-Fi only, no IoT identity: Fleet can't reach this board until infra/scripts/provision.sh is re-run\n");
    } else {
        prov->has_identity = true;
        printf("prov: provisioned as %s\n", prov->thing_name);
    }
    return true;
}
