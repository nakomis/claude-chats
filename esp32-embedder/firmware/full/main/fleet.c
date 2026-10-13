// Kata on Fleet: MQTT to IoT Core (CHATS-10). See fleet.h. tinycrypt's notifier.c, less the presence
// push; Wi-Fi is net.c's.
#include "fleet.h"
#include "model.h"
#include "net.h"
#include "ota.h"

#include <stdio.h>
#include <string.h>

#include "esp_app_desc.h"
#include "esp_heap_caps.h"
#include "esp_timer.h"
#include "mqtt_client.h"

extern const char root_ca_start[] asm("_binary_AmazonRootCA1_pem_start");

#define MQTT_BUFFER_SIZE 2048     // in: one Fleet command (<= 1.5 KB, longer URLs come in chunks)
#define MQTT_OUT_BUFFER_SIZE 512  // out: our messages are a few hundred bytes at most

static const prov_t *prov;
static char commands_topic[sizeof prov->thing_name + 16];
static char responses_topic[sizeof prov->thing_name + 16];
static char status_topic[sizeof prov->thing_name + 16];
static esp_mqtt_client_handle_t mqtt;
static volatile bool connected;
static int subscribe_msg_id = -1;
static const char *volatile selftest = "not run";
static esp_timer_handle_t status_timer;
static bool heap_logged;

void fleet_set_selftest(const char *verdict) { selftest = verdict; }

// The heartbeat (esp_timer task): what Fleet shows as the board's status.
static void send_status(void *arg)
{
    (void)arg;
    if (!connected) return;
    static char msg[320];
    snprintf(msg, sizeof msg,
             "{\"type\":\"status\",\"version\":\"%s\",\"state\":\"%s\",\"selftest\":\"%s\",\"uptime_s\":%lld,"
             "\"internal_free\":%u,\"internal_largest\":%u,\"internal_lowest\":%u,\"psram_free\":%u}",
             esp_app_get_description()->version, model_state(), selftest, (long long)(esp_timer_get_time() / 1000000),
             (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL),
             (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL),
             (unsigned)heap_caps_get_minimum_free_size(MALLOC_CAP_INTERNAL),
             (unsigned)heap_caps_get_free_size(MALLOC_CAP_SPIRAM));
    esp_mqtt_client_enqueue(mqtt, status_topic, msg, 0, 0, 0, true);   // QoS 0: the next one is a minute away
}

static void mqtt_event(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    esp_mqtt_event_handle_t event = data;
    switch ((esp_mqtt_event_id_t)id) {
    case MQTT_EVENT_CONNECTED:
        connected = true;
        printf("fleet: connected to IoT Core as %s\n", prov->thing_name);
        if (!heap_logged) { net_heap("mqtt connected"); heap_logged = true; }
        // A clean session each time, so subscribe on every connect.
        subscribe_msg_id = esp_mqtt_client_subscribe(mqtt, commands_topic, 1);
        break;
    case MQTT_EVENT_DISCONNECTED:
        connected = false;
        printf("fleet: disconnected\n");
        break;
    case MQTT_EVENT_SUBSCRIBED:
        // A SUBACK can refuse (0x80): then Kata can't take updates, and a new image isn't kept.
        if (event->msg_id != subscribe_msg_id) break;
        if (event->error_handle->error_type == MQTT_ERROR_TYPE_SUBSCRIBE_FAILED) {
            printf("fleet: subscription to %s refused\n", commands_topic);
            break;
        }
        printf("fleet: listening on %s\n", commands_topic);
        ota_on_connected();
        send_status(NULL);
        break;
    case MQTT_EVENT_DATA:
        // Commands only (the one subscription). One that didn't fit the buffer arrives in pieces:
        // refuse it rather than parse a fragment.
        if (event->current_data_offset != 0) break;
        if (event->total_data_len != event->data_len) {
            printf("fleet: command of %d bytes doesn't fit the %d-byte buffer\n", event->total_data_len, MQTT_BUFFER_SIZE);
            fleet_respond("{\"type\":\"error\",\"message\":\"Command too long\"}");
            break;
        }
        ota_handle_command(event->data, (size_t)event->data_len);
        break;
    case MQTT_EVENT_ERROR:
        if (event->error_handle->error_type == MQTT_ERROR_TYPE_TCP_TRANSPORT)
            printf("fleet: transport error: tls 0x%x, stack 0x%x, errno %d (internal free %u)\n",
                   event->error_handle->esp_tls_last_esp_err, event->error_handle->esp_tls_stack_err,
                   event->error_handle->esp_transport_sock_errno, (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL));
        else
            printf("fleet: MQTT error type %d, connect return code %d\n", event->error_handle->error_type,
                   event->error_handle->connect_return_code);
        break;
    default:
        break;
    }
}

bool fleet_start(const prov_t *p)
{
    prov = p;
    if (!prov->has_identity) {
        printf("fleet: no IoT identity in prov: not connecting (run infra/scripts/provision.sh)\n");
        return false;
    }
    snprintf(commands_topic, sizeof commands_topic, "kata/%s/commands", prov->thing_name);
    snprintf(responses_topic, sizeof responses_topic, "kata/%s/responses", prov->thing_name);
    snprintf(status_topic, sizeof status_topic, "kata/%s/status", prov->thing_name);

    static char uri[sizeof prov->iot_endpoint + 16];
    snprintf(uri, sizeof uri, "mqtts://%s:8883", prov->iot_endpoint);
    const esp_mqtt_client_config_t config = {
        .broker = {
            .address.uri = uri,
            .verification.certificate = root_ca_start,
        },
        .credentials = {
            .client_id = prov->thing_name,
            .authentication = { .certificate = prov->device_cert, .key = prov->device_key },
        },
        .session.keepalive = 60,
        .network.reconnect_timeout_ms = 5000,
        .buffer = { .size = MQTT_BUFFER_SIZE, .out_size = MQTT_OUT_BUFFER_SIZE },
    };
    mqtt = esp_mqtt_client_init(&config);
    if (!mqtt) { printf("fleet: esp_mqtt_client_init failed\n"); return false; }
    const esp_timer_create_args_t timer = { .callback = send_status, .name = "fleet_status" };
    if (esp_mqtt_client_register_event(mqtt, ESP_EVENT_ANY_ID, mqtt_event, NULL) != ESP_OK ||
        esp_timer_create(&timer, &status_timer) != ESP_OK ||
        esp_timer_start_periodic(status_timer, (uint64_t)FLEET_STATUS_S * 1000000) != ESP_OK ||
        esp_mqtt_client_start(mqtt) != ESP_OK) {
        printf("fleet: MQTT client didn't start\n");
        return false;
    }
    printf("fleet: connecting to %s as %s\n", prov->iot_endpoint, prov->thing_name);
    return true;
}

bool fleet_respond(const char *json)
{
    if (!mqtt || !connected) {
        printf("fleet: not connected: response dropped: %s\n", json);
        return false;
    }
    int msg_id = esp_mqtt_client_enqueue(mqtt, responses_topic, json, 0, 1, 0, true);
    printf("fleet: response queued (msg %d): %s\n", msg_id, json);
    return msg_id >= 0;
}
