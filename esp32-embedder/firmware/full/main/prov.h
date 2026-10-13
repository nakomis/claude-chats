// This board's own settings, read from the `prov` NVS partition that infra/scripts/provision.sh
// writes (CHATS-10). Nothing here is in the firmware image, so every board runs the same image and
// a published build holds no secrets. The same scheme as tinycrypt's prov.c.
//
// Device identity (CHATS-10 part 2) adds the IoT endpoint, Thing name, certificate and private key.
#pragma once

#include <stdbool.h>

typedef struct {
    char wifi_ssid[33];
    char wifi_password[65];
} prov_t;

// Reads the partition. False (and a line saying what's missing) if the board isn't provisioned.
bool prov_load(prov_t *prov);
