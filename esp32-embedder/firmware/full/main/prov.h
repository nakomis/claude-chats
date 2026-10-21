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
    // CHATS-5: where the certificate buys AWS credentials, the queues, and the board's age identity
    // (each empty if not provisioned: then the worker doesn't start, and says why)
    char cred_endpoint[96];
    char role_alias[48];
    char queue_url[160];              // conversation-memory's ingest queue
    char results_url[160];            // and its results queue
    char *age_id;                     // "AGE-SECRET-KEY-1...", in PSRAM; NULL if none
    // CHATS-16: who may ask statusd for GET /json (httpreq.h's status_allow: addresses, comma-
    // separated). Empty if not provisioned: then any peer on the board's own subnet.
    char status_allow[80];
    // CHATS-20: where Kata pushes her metrics (metrics.c): vmauth on Cal, basic auth as her own user.
    // Empty url: no metrics (provision.sh writes none with METRICS_URL=none, or if the password can't be read).
    char metrics_url[128];
    char metrics_user[48];
    char metrics_pass[65];
} prov_t;

// Reads the partition. False (and a line saying what's missing) if the board has no Wi-Fi settings;
// has_identity says whether it can also reach IoT Core.
bool prov_load(prov_t *prov);
