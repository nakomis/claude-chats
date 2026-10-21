// Kata's metrics to the house VictoriaMetrics (CHATS-20). See metrics.c.
#pragma once

#include "prov.h"

// Starts the push (once Wi-Fi is up), from prov's metrics_url, metrics_user and metrics_pass. Says so
// and does nothing if there's no URL. Never fails the boot.
void metrics_start(const prov_t *prov);
