// CHATS-16: Kata's LAN status endpoint. See statusd.h; the HTTP itself is worker/httpreq.c.
#include "statusd.h"

#include <errno.h>
#include <stdio.h>
#include <string.h>
#include <time.h>

#include "esp_heap_caps.h"
#include "esp_netif.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/idf_additions.h"
#include "freertos/task.h"
#include "lwip/inet.h"
#include "lwip/sockets.h"

#include "fleet.h"
#include "httpreq.h"
#include "kata_worker.h"
#include "kw.h"
#include "model.h"

#define STATUSD_PORT 80
#define STATUSD_STACK 6144               // in PSRAM: jb_printf's vsnprintf is the deep part
#define STATUSD_PRIO 6                   // above the compute tasks (kernels.inc: 5), below lwIP and Wi-Fi
#define STATUSD_CORE 0                   // away from lwIP's tcpip task (core 1)
#define STATUSD_MIN_INTERNAL (20 * 1024) // mid-phase, below this free a 503 (the phase's low is ~12.8 KB)
#define STATUSD_RETRY_S 5
#define HEAD_DEADLINE_MS 3000      // the whole request head, however it trickles in
#define SEND_TIMEOUT_S 2
#define BACKLOG 4                  // waiting connections: a few pcbs, so two viewers don't look like a dead board

static uint32_t allow[HTTPREQ_MAX_ALLOW];
static int n_allow;                      // -1: status_allow didn't parse, so nobody may ask
static char *rx;                         // the request head, PSRAM
static unsigned served, busy, refused;   // since boot, in every reply

static bool send_all(int s, const char *p, size_t n)
{
    while (n) {
        int r = send(s, p, n, 0);
        if (r <= 0) return false;
        p += r;
        n -= (size_t)r;
    }
    return true;
}

static void reply(int s, int status, const char *type, const char *body, size_t len, int retry_s)
{
    char head[256];
    size_t hn = httpreq_head(head, sizeof head, status, type, len, retry_s);
    if (hn && send_all(s, head, hn)) send_all(s, body, len);
}

static void reply_busy(int s, const char *why, unsigned free_int)
{
    char body[160];
    int n = snprintf(body, sizeof body, "{\"busy\":\"%s\",\"retry_after_s\":%d,\"internal_free\":%u}", why, STATUSD_RETRY_S,
                     free_int);
    busy++;
    reply(s, 503, "application/json", body, (size_t)n, STATUSD_RETRY_S);
}

// What she's doing at this moment, appended to the heartbeat's fields. Gated: in_phase is stable.
static void now_json(jbuf_t *b, int64_t t0)
{
    const char *model = model_state();
    bool phase = kata_worker_in_phase();
    jb_printf(b, ",\"now\":{\"state\":\"%s\",\"model\":\"%s\",\"worker\":\"%s\",\"in_phase\":%s",
              phase ? "sqs" : model, model, kata_worker_state(), phase ? "true" : "false");
    time_t t = time(NULL);
    if (t > 1700000000) jb_printf(b, ",\"served_at\":%lld", (long long)t);   // once SNTP has set the clock
    int unit, units;
    uint32_t elapsed_ms;
    if (model_progress(&unit, &units, &elapsed_ms)) {
        double elapsed = elapsed_ms / 1e3;
        jb_printf(b, ",\"pass\":{\"unit\":%d,\"units\":%d,\"elapsed_s\":%.1f", unit, units, elapsed);
        if (unit > 0) jb_printf(b, ",\"eta_s\":%.0f", elapsed * (units - unit) / unit);
        jb_putc(b, '}');
    }
    jb_printf(b, ",\"requests\":{\"served\":%u,\"busy\":%u,\"refused\":%u}", served + 1, busy, refused);
    jb_printf(b, ",\"build_ms\":%.1f}}", (double)(esp_timer_get_time() - t0) / 1e3);
}

static void serve_json(int s)
{
    if (!kata_worker_gate(1000)) {
        reply_busy(s, "gate", (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL));
        return;
    }
    int64_t t0 = esp_timer_get_time();
    unsigned free_int = (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL);
    if (kata_worker_in_phase() && free_int < STATUSD_MIN_INTERNAL) {
        reply_busy(s, "sqs", free_int);
    } else {
        jbuf_t b;                                     // PSRAM (kw.h)
        jb_init(&b);
        if (!fleet_status_build(&b, 1000)) {
            reply_busy(s, "status", free_int);
        } else {
            now_json(&b, t0);
            if (b.oom) {
                reply_busy(s, "memory", free_int);
            } else {
                served++;
                reply(s, 200, "application/json", b.p, b.n, 0);
                if (served == 1)   // how deep the status build goes (the stack is a guess)
                    printf("statusd: first reply, %u bytes; stack high-water mark %u bytes free\n", (unsigned)b.n,
                           (unsigned)uxTaskGetStackHighWaterMark(NULL));
            }
        }
        jb_free(&b);
    }
    kata_worker_ungate();
}

// Closes with a reset rather than a FIN: no TIME_WAIT pcb (2 minutes of internal RAM) for a peer
// that's been refused or has gone quiet.
static void drop(int s)
{
    struct linger l = { .l_onoff = 1, .l_linger = 0 };
    setsockopt(s, SOL_SOCKET, SO_LINGER, &l, sizeof l);
}

static void handle(int s, uint32_t peer)
{
    esp_netif_ip_info_t ip = { 0 };
    esp_netif_t *sta = esp_netif_get_handle_from_ifkey("WIFI_STA_DEF");
    if (sta) esp_netif_get_ip_info(sta, &ip);
    if (n_allow < 0 || !httpreq_peer_ok(peer, ntohl(ip.ip.addr), ntohl(ip.netmask.addr), allow, n_allow)) {
        refused++;
        drop(s);                                      // closed without a word
        return;
    }
    // The whole head within HEAD_DEADLINE_MS, not each recv: a byte every second would otherwise hold
    // the one connection for half an hour.
    int64_t deadline = esp_timer_get_time() + HEAD_DEADLINE_MS * 1000LL;
    size_t n = 0;
    httpreq_route_t r = HTTPREQ_INCOMPLETE;
    while (r == HTTPREQ_INCOMPLETE) {
        int64_t left_us = deadline - esp_timer_get_time();
        if (left_us <= 0) { drop(s); return; }
        struct timeval tv = { .tv_sec = left_us / 1000000, .tv_usec = left_us % 1000000 };
        setsockopt(s, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof tv);
        int got = recv(s, rx + n, HTTPREQ_MAX_HEAD - n, 0);
        if (got <= 0) { drop(s); return; }            // gone, or out of time
        n += (size_t)got;
        r = httpreq_route(rx, n);
    }
    if (r == HTTPREQ_JSON) {
        serve_json(s);
        return;
    }
    static const char *const msg[] = { [HTTPREQ_NOT_FOUND] = "not found: GET /json\n",
                                       [HTTPREQ_BAD_METHOD] = "method not allowed: GET /json\n",
                                       [HTTPREQ_BAD] = "bad request\n" };
    reply(s, httpreq_status(r), "text/plain", msg[r], strlen(msg[r]), 0);
}

static void statusd_task(void *arg)
{
    (void)arg;
    for (;;) {
        int ls = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
        struct sockaddr_in a = { .sin_family = AF_INET, .sin_port = htons(STATUSD_PORT), .sin_addr.s_addr = htonl(INADDR_ANY) };
        int on = 1;
        if (ls < 0 || setsockopt(ls, SOL_SOCKET, SO_REUSEADDR, &on, sizeof on) || bind(ls, (struct sockaddr *)&a, sizeof a) ||
            listen(ls, BACKLOG)) {
            int e = errno;
            printf("statusd: can't listen on port %d (errno %d): trying again in a minute\n", STATUSD_PORT, e);
            if (ls >= 0) close(ls);
            vTaskDelay(pdMS_TO_TICKS(60000));
            continue;
        }
        printf("statusd: GET /json on port %d, for %s\n", STATUSD_PORT,
               n_allow < 0 ? "nobody (status_allow didn't parse)" : n_allow ? "status_allow's addresses" : "this subnet");
        for (;;) {
            struct sockaddr_in peer;
            socklen_t pl = sizeof peer;
            int s = accept(ls, (struct sockaddr *)&peer, &pl);
            if (s < 0) {                              // the listener's gone bad: make a new one
                int e = errno;
                printf("statusd: accept failed (errno %d): listening again\n", e);
                break;
            }
            struct timeval tv = { .tv_sec = SEND_TIMEOUT_S };
            setsockopt(s, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof tv);
            handle(s, ntohl(peer.sin_addr.s_addr));
            close(s);                                 // a plain close: SHUT_RD could turn it into a reset mid-reply
        }
        close(ls);
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
}

bool statusd_start(const prov_t *prov)
{
    n_allow = httpreq_parse_allow(prov->status_allow, allow, HTTPREQ_MAX_ALLOW);
    if (n_allow < 0) printf("statusd: status_allow \"%s\" didn't parse: refusing everyone\n", prov->status_allow);
    rx = heap_caps_malloc(HTTPREQ_MAX_HEAD, MALLOC_CAP_SPIRAM);
    if (!rx) {
        printf("statusd: out of memory: not starting\n");
        return false;
    }
    if (xTaskCreatePinnedToCoreWithCaps(statusd_task, "statusd", STATUSD_STACK, NULL, STATUSD_PRIO, NULL, STATUSD_CORE,
                                        MALLOC_CAP_SPIRAM) != pdPASS) {
        printf("statusd: no task: not starting\n");
        heap_caps_free(rx);
        rx = NULL;
        return false;
    }
    return true;
}
