// OTA through Fleet (CHATS-10): tinycrypt's embedded/esp32s3/main/ota.c, less the approval. See ota.h.
#include "ota.h"
#include "fleet.h"
#include "kata_worker.h"
#include "model.h"
#include "net.h"

#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/time.h>

#include "cJSON.h"
#include "esp_app_desc.h"
#include "esp_heap_caps.h"
#include "esp_http_client.h"
#include "esp_ota_ops.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "mbedtls/sha256.h"
#include "nvs.h"

extern const char root_ca_start[] asm("_binary_AmazonRootCA1_pem_start");

#define PROJECT "kata"                 // the app descriptor's project_name (CMakeLists.txt) and Fleet's
#define NVS_NAMESPACE "ota"
#define NVS_TARGET "target"            // the version an install restarted into
#define NVS_TARGET_SLOT "target_slot"  // and its slot (a reinstall of the same version rolls back too)
#define READ_CHUNK 4096
#define HTTP_TIMEOUT_MS 15000
#define RESTART_DELAY_MS 2000          // for the last progress message to reach IoT Core first
#define TASK_STACK (8 * 1024)          // internal: the task writes flash

typedef enum { IDLE, WAITING, DOWNLOADING, INSTALLING } phase_t;

static portMUX_TYPE lock = portMUX_INITIALIZER_UNLOCKED;
static volatile phase_t phase = IDLE;
static volatile bool cancel;

// The update being worked on (written by the MQTT task while IDLE, then owned by the OTA task).
static char *job_url;
static char job_version[OTA_VERSION_MAX];
static uint8_t job_sha256[OTA_SHA256_LEN];
// SHA-256 of the last update command's URL. IoT Core redelivers the very same bytes; every trigger
// from Fleet presigns a fresh URL. So the same URL again is a redelivery (or a replay), never a retry.
static uint8_t last_url_hash[32];

// Back-off after a failure (CRYPT-21; ota.h). Under the lock; esp_timer milliseconds.
static int64_t retry_after_ms;
static uint32_t backoff_s;

static nvs_handle_t nvs;
static bool nvs_ok;
// Probation (ota.h): kept once healthy and connected, both under the lock.
static bool on_probation, probation_healthy, probation_connected;
static esp_timer_handle_t verify_deadline;

static const char *running_version(void) { return esp_app_get_description()->version; }
static int64_t now_ms(void) { return esp_timer_get_time() / 1000; }
static unsigned seconds_until(int64_t when_ms) { return (unsigned)((when_ms - now_ms() + 999) / 1000); }

// ---- responses ----------------------------------------------------------------

static bool respond(cJSON *msg)
{
    char *text = cJSON_PrintUnformatted(msg);
    bool sent = text && fleet_respond(text);
    cJSON_free(text);
    cJSON_Delete(msg);
    return sent;
}

static cJSON *message(const char *type)
{
    cJSON *msg = cJSON_CreateObject();
    cJSON_AddStringToObject(msg, "type", type);
    return msg;
}

static void respond_progress(const char *status, int progress, const char *version)
{
    cJSON *msg = message("ota_progress");
    cJSON_AddStringToObject(msg, "status", status);
    cJSON_AddNumberToObject(msg, "progress", progress);
    if (version) cJSON_AddStringToObject(msg, "version", version);
    respond(msg);
}

static bool respond_error(const char *type, const char *text, const char *version)
{
    printf("ota: %s: %s\n", type, text);
    cJSON *msg = message(type);
    cJSON_AddStringToObject(msg, "message", text);
    if (version) cJSON_AddStringToObject(msg, "version", version);
    return respond(msg);
}

static void respond_version(void)
{
    cJSON *msg = message("version");
    cJSON_AddStringToObject(msg, "version", running_version());
    cJSON_AddStringToObject(msg, "project", PROJECT);
    respond(msg);
}

// ---- boot: probation and rollback ------------------------------------------------

static void rollback(const char *why)
{
    printf("ota: new image %s: rolling back\n", why);
    esp_ota_mark_app_invalid_rollback_and_reboot();
    abort();                                  // only if that failed: a reset of a pending image rolls back too
}

static void verify_deadline_fire(void *arg)
{
    (void)arg;
    taskENTER_CRITICAL(&lock);
    bool still = on_probation;
    taskEXIT_CRITICAL(&lock);
    if (still) rollback("wasn't both healthy and on IoT Core by the deadline");
}

// The outcome of the install this board restarted for, reported once (to the old image, if the
// new one was rolled back).
static void report_outcome(void)
{
    char target[OTA_VERSION_MAX], slot[17] = "";
    size_t len = sizeof target, slot_len = sizeof slot;
    if (!nvs_ok || nvs_get_str(nvs, NVS_TARGET, target, &len) != ESP_OK) return;
    bool slot_known = nvs_get_str(nvs, NVS_TARGET_SLOT, slot, &slot_len) == ESP_OK;
    bool sent;
    if (strcmp(target, running_version()) == 0 &&
        (!slot_known || strcmp(slot, esp_ota_get_running_partition()->label) == 0)) {
        cJSON *msg = message("ota_complete");
        cJSON_AddStringToObject(msg, "version", target);
        sent = respond(msg);
    } else {
        sent = respond_error("ota_error", "rolled back", target);
    }
    if (!sent) return;                        // not connected after all: next time
    nvs_erase_key(nvs, NVS_TARGET);
    nvs_erase_key(nvs, NVS_TARGET_SLOT);
    nvs_commit(nvs);
}

// Keeps a new image once it's both healthy and connected (from either task).
static void maybe_keep(void)
{
    taskENTER_CRITICAL(&lock);
    bool keep = on_probation && probation_healthy && probation_connected;
    if (keep) on_probation = false;
    taskEXIT_CRITICAL(&lock);
    if (!keep) return;
    esp_timer_stop(verify_deadline);
    esp_err_t e = esp_ota_mark_app_valid_cancel_rollback();
    printf("ota: new image passed its self-test and reached IoT Core: kept (%s)\n", esp_err_to_name(e));
    report_outcome();
}

bool ota_boot(void)
{
    nvs_ok = nvs_open(NVS_NAMESPACE, NVS_READWRITE, &nvs) == ESP_OK;
    const esp_partition_t *running = esp_ota_get_running_partition();
    esp_ota_img_states_t state;
    bool pending = esp_ota_get_state_partition(running, &state) == ESP_OK && state == ESP_OTA_IMG_PENDING_VERIFY;
    printf("ota: running %s %s from %s%s\n", PROJECT, running_version(), running->label,
           pending ? " (new image, on probation)" : "");
    if (!pending) return false;
    on_probation = true;
    const esp_timer_create_args_t args = { .callback = verify_deadline_fire, .name = "ota_verify" };
    if (esp_timer_create(&args, &verify_deadline) != ESP_OK ||
        esp_timer_start_once(verify_deadline, (uint64_t)OTA_VERIFY_DEADLINE_S * 1000000) != ESP_OK)
        rollback("has no probation timer");
    return true;
}

void ota_health(bool healthy)
{
    taskENTER_CRITICAL(&lock);
    bool probation = on_probation;
    if (healthy) probation_healthy = true;
    taskEXIT_CRITICAL(&lock);
    if (!probation) return;
    if (!healthy) rollback("failed its self-test");
    printf("ota: new image passed its self-test%s\n", probation_connected ? "" : "; waiting for IoT Core");
    maybe_keep();
}

void ota_on_connected(void)
{
    respond_version();
    taskENTER_CRITICAL(&lock);
    bool probation = on_probation;
    probation_connected = true;
    taskEXIT_CRITICAL(&lock);
    if (probation) maybe_keep();              // reports the outcome once it's kept
    else report_outcome();
}

// ---- download and checks ------------------------------------------------------------

// "<what> (tls ... errno ... heap free/largest)": a failure says why itself, with how much
// internal RAM there was, since there's usually no serial log to read.
static const char *explain(esp_http_client_handle_t http, const char *what, char *why, size_t why_len)
{
    int tls_code = 0, tls_flags = 0;
    esp_err_t tls = esp_http_client_get_and_clear_last_tls_error(http, &tls_code, &tls_flags);
    snprintf(why, why_len, "download failed: %s (tls %s -0x%x flags 0x%x, errno %d, heap %u/%u)", what,
             esp_err_to_name(tls), (unsigned)-tls_code, (unsigned)tls_flags, esp_http_client_get_errno(http),
             (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL),
             (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL));
    printf("ota: %s\n", why);
    return why;
}

// Returns NULL on success, else the reason (for ota_error).
static const char *download(const esp_partition_t *slot, char *why, size_t why_len)
{
    const char *err_text = NULL;
    int64_t started = now_ms();               // the deadline includes connecting
    uint8_t *buf = malloc(READ_CHUNK);        // internal (the model's buffers are lent): fast flash writes
    esp_http_client_config_t config = {
        .url = job_url,
        .cert_pem = root_ca_start,
        .timeout_ms = HTTP_TIMEOUT_MS,
        .keep_alive_enable = false,
        // The request line carries the whole presigned URL (~1.5-3 KB with an STS token); the
        // default 512-byte TX buffer can't hold it, and the request fails with a bare ESP_FAIL.
        .buffer_size_tx = (int)strlen(job_url) + 1024,
    };
    esp_http_client_handle_t http = buf ? esp_http_client_init(&config) : NULL;
    esp_ota_handle_t ota = 0;
    mbedtls_sha256_context sha;
    mbedtls_sha256_init(&sha);
    if (!http) { err_text = "download failed: out of memory"; goto out; }
    esp_err_t err = esp_http_client_open(http, 0);
    if (err != ESP_OK) { err_text = explain(http, esp_err_to_name(err), why, why_len); goto out; }
    net_heap("ota: connected to S3");
    int64_t length = esp_http_client_fetch_headers(http);
    int status = esp_http_client_get_status_code(http);
    printf("ota: HTTP %d, %" PRId64 " bytes\n", status, length);
    if (status != 200) {
        snprintf(why, why_len, "download failed: HTTP %d", status);
        err_text = why;
        goto out;
    }
    if (length > (int64_t)slot->size) { err_text = "image too large"; goto out; }
    err = esp_ota_begin(slot, OTA_WITH_SEQUENTIAL_WRITES, &ota);
    if (err != ESP_OK) {
        snprintf(why, why_len, "download failed: %s", esp_err_to_name(err));
        err_text = why;
        ota = 0;
        goto out;
    }
    mbedtls_sha256_starts(&sha, 0);
    size_t total = 0, window_total = 0;
    int last_tenth = -1, stalls = 0;
    // Each read waits at most HTTP_TIMEOUT_MS; these catch a server that trickles bytes instead.
    int64_t window_start = now_ms();
    for (;;) {
        if (cancel) { err_text = "cancelled"; goto out; }
        int64_t now = now_ms();
        if (now - started > (int64_t)OTA_DOWNLOAD_DEADLINE_S * 1000) {
            snprintf(why, why_len, "download failed: over %d s", OTA_DOWNLOAD_DEADLINE_S);
            err_text = why;
            goto out;
        }
        if (now - window_start >= OTA_MIN_RATE_WINDOW_MS) {
            if (total - window_total < OTA_MIN_RATE_BYTES) {
                snprintf(why, why_len, "download failed: too slow (%u bytes in %d s)",
                         (unsigned)(total - window_total), OTA_MIN_RATE_WINDOW_MS / 1000);
                err_text = why;
                goto out;
            }
            window_start = now;
            window_total = total;
        }
        int n = esp_http_client_read(http, (char *)buf, READ_CHUNK);
        if (n == -ESP_ERR_HTTP_EAGAIN && ++stalls <= 3) {
            printf("ota: read timed out after %u bytes: retrying\n", (unsigned)total);
            continue;
        }
        if (n < 0) {
            char what[48];
            snprintf(what, sizeof what, "read %d after %u bytes", n, (unsigned)total);
            err_text = explain(http, what, why, why_len);
            goto out;
        }
        stalls = 0;
        if (n == 0) {
            if (!esp_http_client_is_complete_data_received(http)) {
                err_text = "download failed: connection closed early";
                goto out;
            }
            break;
        }
        total += (size_t)n;
        if (total > slot->size) { err_text = "image too large"; goto out; }
        mbedtls_sha256_update(&sha, buf, (size_t)n);
        err = esp_ota_write(ota, buf, (size_t)n);
        if (err != ESP_OK) {
            snprintf(why, why_len, "download failed: %s", esp_err_to_name(err));
            err_text = why;
            goto out;
        }
        if (length > 0 && (int)(total * 10 / (size_t)length) != last_tenth) {
            last_tenth = (int)(total * 10 / (size_t)length);
            respond_progress("downloading", last_tenth * 10, job_version);
        }
    }
    printf("ota: downloaded %u bytes\n", (unsigned)total);
    net_heap("ota: downloaded");

    uint8_t got[OTA_SHA256_LEN];
    mbedtls_sha256_finish(&sha, got);
    if (memcmp(got, job_sha256, sizeof got) != 0) { err_text = "sha256 mismatch"; goto out; }
    // Checks the image's own integrity and its signature against the running app's signing key.
    err = esp_ota_end(ota);
    ota = 0;
    if (err != ESP_OK) {
        err_text = err == ESP_ERR_OTA_VALIDATE_FAILED ? "bad signature" : "verify failed";
        goto out;
    }
    esp_app_desc_t desc;
    if (esp_ota_get_partition_description(slot, &desc) != ESP_OK || strcmp(desc.project_name, PROJECT) != 0 ||
        strcmp(desc.version, job_version) != 0) {
        err_text = "wrong image";
        goto out;
    }
    if (!ota_version_valid(running_version())) err_text = "running version unknown";   // no downgrade check possible
    else if (ota_version_cmp(job_version, running_version()) < 0) err_text = "downgrade refused";
    else if (ota_version_cmp(job_version, running_version()) == 0) err_text = "already running";

out:
    if (ota) esp_ota_abort(ota);
    mbedtls_sha256_free(&sha);
    if (http) esp_http_client_cleanup(http);
    free(buf);
    return err_text;
}

// The job is cleared before the phase goes back to IDLE: from then on the MQTT task may start the
// next one (start_update), which sets job_url itself.
static void finish(void)
{
    char *url = job_url;
    job_url = NULL;
    free(url);
    taskENTER_CRITICAL(&lock);
    phase = IDLE;
    cancel = false;
    taskEXIT_CRITICAL(&lock);
}

// One update, start to finish; the task (and its 8 KB of internal stack) goes when it's done.
static void ota_task(void *arg)
{
    (void)arg;
    static char why[160];
    const char *failed = NULL;
    const esp_partition_t *slot = esp_ota_get_next_update_partition(NULL);
    printf("ota: update to %s: into %s, once the model is free\n", job_version, slot ? slot->label : "(none)");

    // 1. CHATS-5: the worker hands off what it has finished and gives back what it hasn't (no job is
    // held through an update: CHATS-10's criterion 4), then the model's internal buffers (model.h).
    bool lent = false, parked = false;
    if (strcmp(model_state(), "idle") != 0) respond_progress("waiting for the current pass", 0, job_version);
    int64_t wait_start = now_ms();
    parked = kata_worker_park(OTA_MODEL_WAIT_S * 1000);
    int waited = (int)((now_ms() - wait_start) / 1000);
    for (; parked && !cancel && waited < OTA_MODEL_WAIT_S; waited += 5)
        if ((lent = model_lend(5000))) break;
    if (!slot) failed = "no update slot";
    else if (cancel) failed = "cancelled";
    else if (!parked) failed = "busy: the worker didn't hand its jobs back";
    else if (!lent) failed = "busy: the model didn't come free";
    else {
        taskENTER_CRITICAL(&lock);
        if (!cancel) phase = DOWNLOADING;
        taskEXIT_CRITICAL(&lock);
        net_heap("ota: model lent");
        respond_progress("downloading", 0, job_version);
        failed = download(slot, why, sizeof why);
    }

    // 2. Install: the slot, the record of what was installed, and a restart into it.
    if (!failed) {
        taskENTER_CRITICAL(&lock);
        bool cancelled = cancel;
        if (!cancelled) phase = INSTALLING;
        taskEXIT_CRITICAL(&lock);
        if (cancelled) failed = "cancelled";
    }
    if (!failed) {
        esp_err_t err = nvs_ok ? nvs_set_str(nvs, NVS_TARGET, job_version) : ESP_ERR_INVALID_STATE;
        if (err == ESP_OK) err = nvs_set_str(nvs, NVS_TARGET_SLOT, slot->label);
        if (err == ESP_OK) err = nvs_commit(nvs);
        if (err == ESP_OK) err = esp_ota_set_boot_partition(slot);   // checks the image (and signature) again
        if (err == ESP_OK) {
            respond_progress("restarting", 100, job_version);
            vTaskDelay(pdMS_TO_TICKS(RESTART_DELAY_MS));
            printf("ota: restarting into %s\n", job_version);
            esp_restart();
        }
        if (nvs_ok) {
            nvs_erase_key(nvs, NVS_TARGET);
            nvs_erase_key(nvs, NVS_TARGET_SLOT);
            nvs_commit(nvs);
        }
        failed = "install failed";
    }

    // 3. It failed: back off, give the model its buffers back, the worker its queue, and say why.
    if (lent) model_reclaim();
    kata_worker_resume();
    taskENTER_CRITICAL(&lock);
    backoff_s = backoff_s == 0 ? OTA_BACKOFF_FIRST_S : backoff_s >= OTA_BACKOFF_MAX_S / 2 ? OTA_BACKOFF_MAX_S : backoff_s * 2;
    retry_after_ms = now_ms() + (int64_t)backoff_s * 1000;
    taskEXIT_CRITICAL(&lock);
    if (strcmp(failed, "cancelled") == 0) respond_progress("Update cancelled", 0, NULL);
    else respond_error("ota_error", failed, job_version);
    net_heap("ota: model back");
    finish();
    vTaskDelete(NULL);
}

// ---- commands (MQTT task) -------------------------------------------------------------

static void start_update(const ota_cmd *cmd)
{
    char *url = strdup(cmd->url);
    if (!url) { respond_error("ota_error", "out of memory", cmd->version); return; }
    // IoT Core redelivers a command whose PUBACK was slow, during the update or after it has failed:
    // the same URL again is that, not a new request. No reply, on purpose: an ota_error carrying that
    // version would show Fleet a running update as failed. Every Fleet trigger presigns a new URL.
    uint8_t url_hash[32];
    mbedtls_sha256((const unsigned char *)url, strlen(url), url_hash, 0);
    if (memcmp(url_hash, last_url_hash, sizeof url_hash) == 0) {
        printf("ota: update to %s with the same URL as the last: a redelivery, ignored\n", cmd->version);
        free(url);
        return;
    }
    // Refused before touching the model when the answer can only be no.
    if (ota_version_valid(running_version()) && ota_version_cmp(cmd->version, running_version()) <= 0) {
        free(url);
        respond_error("ota_error", ota_version_cmp(cmd->version, running_version()) == 0 ? "already running" : "downgrade refused",
                      cmd->version);
        return;
    }
    int64_t now = now_ms(), backoff_until = 0;
    taskENTER_CRITICAL(&lock);
    bool probation = on_probation, busy = phase != IDLE;
    if (!busy && !probation && now < retry_after_ms) backoff_until = retry_after_ms;
    else if (!busy && !probation) { phase = WAITING; cancel = false; }
    taskEXIT_CRITICAL(&lock);
    if (busy || probation || backoff_until) {
        char why[48];
        if (busy) snprintf(why, sizeof why, "busy");
        else if (probation) snprintf(why, sizeof why, "busy: checking this image first");
        else snprintf(why, sizeof why, "busy: retry in %u s", seconds_until(backoff_until));
        free(url);
        respond_error("ota_error", why, cmd->version);
        return;
    }
    memcpy(last_url_hash, url_hash, sizeof last_url_hash);
    job_url = url;
    snprintf(job_version, sizeof job_version, "%s", cmd->version);
    memcpy(job_sha256, cmd->sha256, sizeof job_sha256);
    if (xTaskCreate(ota_task, "ota", TASK_STACK, NULL, 4, NULL) != pdPASS) {
        finish();
        respond_error("ota_error", "out of memory", cmd->version);
    }
}

static void cancel_update(void)
{
    taskENTER_CRITICAL(&lock);
    phase_t now = phase;
    if (now == WAITING || now == DOWNLOADING) cancel = true;
    taskEXIT_CRITICAL(&lock);
    if (now == INSTALLING) respond_progress("Installing; too late to cancel", 100, NULL);
    else if (now == IDLE) respond_progress("Update cancelled", 0, NULL);
    // else the task answers once it has stopped
}

void ota_handle_command(const char *data, size_t len)
{
    static ota_chunks chunks;
    static ota_cmd *cmd;                      // 8 KB: in PSRAM, not internal RAM or the MQTT task's stack
    if (!cmd && !(cmd = heap_caps_malloc(sizeof *cmd, MALLOC_CAP_SPIRAM))) {
        respond_error("error", "out of memory", NULL);
        return;
    }
    ota_cmd_parse(&chunks, data, len, (uint32_t)(esp_timer_get_time() / 1000), cmd);
    switch (cmd->kind) {
    case OTA_CMD_GET_VERSION:
        respond_version();
        break;
    case OTA_CMD_PING: {
        struct timeval tv;
        gettimeofday(&tv, NULL);
        cJSON *msg = message("pong");
        cJSON_AddNumberToObject(msg, "timestamp", (double)tv.tv_sec * 1000 + tv.tv_usec / 1000);
        respond(msg);
        break;
    }
    case OTA_CMD_CANCEL:
        cancel_update();
        break;
    case OTA_CMD_CHUNK: {
        char status[48];
        snprintf(status, sizeof status, "Chunk %d/%d received", cmd->chunk_index + 1, cmd->total_chunks);
        respond_progress(status, 0, NULL);    // no version: an ack can't reopen a finished update
        break;
    }
    case OTA_CMD_UPDATE:
        printf("ota: update to %s requested\n", cmd->version);
        start_update(cmd);
        break;
    case OTA_CMD_ERROR:
        respond_error(cmd->ota_error ? "ota_error" : "error", cmd->message, NULL);
        break;
    }
}
