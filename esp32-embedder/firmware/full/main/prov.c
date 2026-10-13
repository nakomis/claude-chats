// The board's provisioned settings (CHATS-10). See prov.h.
#include "prov.h"

#include <stdio.h>
#include <string.h>

#include "nvs.h"
#include "nvs_flash.h"

#define PARTITION "prov"
#define NAMESPACE "embedder"       // as infra/scripts/provision.sh writes it

static bool get_fixed(nvs_handle_t h, const char *key, char *out, size_t size)
{
    size_t len = size;
    esp_err_t e = nvs_get_str(h, key, out, &len);
    if (e != ESP_OK) printf("prov: %s: %s\n", key, esp_err_to_name(e));
    return e == ESP_OK;
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
    bool ok = get_fixed(h, "wifi_ssid", prov->wifi_ssid, sizeof prov->wifi_ssid);
    ok &= get_fixed(h, "wifi_pass", prov->wifi_password, sizeof prov->wifi_password);
    nvs_close(h);
    if (!ok) {
        memset(prov, 0, sizeof *prov);
        printf("prov: incomplete: run infra/scripts/provision.sh\n");
    }
    return ok;
}
