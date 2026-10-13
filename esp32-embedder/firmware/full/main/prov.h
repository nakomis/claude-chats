// This board's own settings, read from the `prov` NVS partition that infra/scripts/provision.sh
// writes (CHATS-10). Nothing here is in the firmware image, so every board runs the same image and
// a published build holds no secrets. The same scheme as tinycrypt's prov.c.
#pragma once

#include <stdbool.h>

typedef struct {
    char wifi_ssid[33];
    char wifi_password[65];
    // Device identity: the board's IoT Thing and its certificate (none on a board provisioned before
    // device identity existed, which still gets Wi-Fi)
    bool has_identity;
    char iot_endpoint[96];
    char thing_name[48];
    char *device_cert, *device_key;          // PEM, in PSRAM
    // CHATS-5: where the certificate buys AWS credentials (empty if not provisioned; unused so far)
    char cred_endpoint[96];
    char role_alias[48];
} prov_t;

// Reads the partition. False (and a line saying what's missing) if the board has no Wi-Fi settings;
// has_identity says whether it can also reach IoT Core.
bool prov_load(prov_t *prov);
