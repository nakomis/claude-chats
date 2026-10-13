#pragma once

// Bring Wi-Fi up as a station; 0 once there's an IP, -1 on failure or timeout.
int net_up(int timeout_ms);
// One HTTPS GET with the certificate bundle; the HTTP status, or -1 if TLS or the request failed.
int net_https_check(const char *url);
// Print internal (free, largest block, lowest ever) and PSRAM free.
void net_heap(const char *when);
