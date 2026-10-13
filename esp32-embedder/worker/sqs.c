// Amazon SQS, JSON protocol (sqs.h).
#include "sqs.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "cJSON.h"
#include "mbedtls/md5.h"

int sqs_init(sqs_t *s, const char *queue_url, const aws_creds_t *creds, int64_t (*now)(void *), http_post_fn post, void *ctx)
{
    memset(s, 0, sizeof *s);
    const char *h = queue_url && !strncmp(queue_url, "https://", 8) ? queue_url + 8 : NULL;
    const char *slash = h ? strchr(h, '/') : NULL;
    if (!slash || strncmp(h, "sqs.", 4) || (size_t)(slash - h) >= sizeof s->host) return -1;
    snprintf(s->host, sizeof s->host, "%.*s", (int)(slash - h), h);
    const char *r = s->host + 4, *dot = strchr(r, '.');
    if (!dot || strcmp(dot, ".amazonaws.com") || (size_t)(dot - r) >= sizeof s->region) return -1;
    snprintf(s->region, sizeof s->region, "%.*s", (int)(dot - r), r);
    s->creds = creds;
    s->now = now;
    s->post = post;
    s->ctx = ctx;
    jb_init(&s->req);
    jb_init(&s->resp);
    return 0;
}

void sqs_free(sqs_t *s)
{
    jb_free(&s->req);
    jb_free(&s->resp);
}

void sqs_msg_free(sqs_msg_t *m)
{
    kw_free(m->message_id);
    kw_free(m->receipt);
    kw_free(m->body);
    memset(m, 0, sizeof *m);
}

static void md5_hex(const void *data, size_t n, char out[33])
{
    uint8_t h[16];
    mbedtls_md5(data, n, h);
    for (int i = 0; i < 16; i++) snprintf(out + 2 * i, 3, "%02x", h[i]);
}

// After a big call (a receive of large bodies, a batch of results), the buffers go back to PSRAM
// rather than sitting there at their high-water mark.
#define SQS_KEEP_BUF (64 * 1024)
static void shrink(sqs_t *s)
{
    if (s->req.cap > SQS_KEEP_BUF) jb_free(&s->req);
    if (s->resp.cap > SQS_KEEP_BUF) jb_free(&s->resp);
}

// One call: s->req holds the JSON body. The parsed response, or NULL (last_error says why).
static cJSON *call(sqs_t *s, const char *action)
{
    s->last_error[0] = 0;
    s->last_status = -1;
    if (s->req.oom) { snprintf(s->last_error, sizeof s->last_error, "%s: out of memory", action); return NULL; }
    if (!s->creds || !s->creds->token) { snprintf(s->last_error, sizeof s->last_error, "%s: no credentials", action); return NULL; }
    char date[17], target[48], authz[320];
    kw_amz_date(s->now(s->ctx), date);
    snprintf(target, sizeof target, "AmazonSQS.%s", action);
    sv4_hdr_t h[6] = { { "Content-Type", "application/x-amz-json-1.0" },
                       { "Host", s->host },
                       { "X-Amz-Date", date },
                       { "X-Amz-Security-Token", s->creds->token },
                       { "X-Amz-Target", target } };
    const sv4_key_t key = { s->creds->access_key, s->creds->secret_key, s->region, "sqs" };
    const sv4_req_t sr = { "POST", "/", NULL, h, 5, (const uint8_t *)s->req.p, s->req.n };
    if (sv4_sign(&sr, &key, date, authz, sizeof authz, NULL, NULL)) {
        snprintf(s->last_error, sizeof s->last_error, "%s: signing failed", action);
        return NULL;
    }
    h[5] = (sv4_hdr_t){ "Authorization", authz };
    const http_req_t req = { s->host, "/", h, 6, s->req.p, s->req.n };
    jb_reset(&s->resp);
    int status = s->post(s->ctx, &req, &s->resp);
    if (s->resp.oom) { s->last_status = status; snprintf(s->last_error, sizeof s->last_error, "%s: out of memory for the response", action); shrink(s); return NULL; }
    s->last_status = status;
    if (status < 0) { snprintf(s->last_error, sizeof s->last_error, "%s: no response", action); return NULL; }
    cJSON *j = s->resp.p && !s->resp.oom ? cJSON_ParseWithLength(s->resp.p, s->resp.n) : NULL;
    if (status != 200) {
        // {"__type":"com.amazonaws.sqs#QueueDoesNotExist","message":"..."}
        const char *type = cJSON_GetStringValue(cJSON_GetObjectItem(j, "__type"));
        const char *msg = cJSON_GetStringValue(cJSON_GetObjectItem(j, "message"));
        if (!msg) msg = cJSON_GetStringValue(cJSON_GetObjectItem(j, "Message"));
        if (type && strchr(type, '#')) type = strchr(type, '#') + 1;
        snprintf(s->last_error, sizeof s->last_error, "%s: HTTP %d %s%s%s", action, status, type ? type : "",
                 msg ? ": " : "", msg ? msg : "");
        cJSON_Delete(j);
        shrink(s);
        return NULL;
    }
    if (!j) snprintf(s->last_error, sizeof s->last_error, "%s: unparseable response (%zu bytes)", action, s->resp.n);
    shrink(s);                                // the tree holds what it needs
    return j;
}

static void begin(sqs_t *s, const char *queue_url)
{
    jb_reset(&s->req);
    jb_puts(&s->req, "{\"QueueUrl\":");
    jb_json_str(&s->req, queue_url, strlen(queue_url));
}

int sqs_receive(sqs_t *s, const char *queue_url, int max, int wait_s, int visibility_s, sqs_msg_t **msgs, int *n)
{
    *msgs = NULL;
    *n = 0;
    max = max < 1 ? 1 : max > 10 ? 10 : max;
    begin(s, queue_url);
    jb_printf(&s->req, ",\"MaxNumberOfMessages\":%d,\"WaitTimeSeconds\":%d,\"VisibilityTimeout\":%d,"
                       "\"MessageSystemAttributeNames\":[\"ApproximateReceiveCount\"]}",
              max, wait_s, visibility_s);
    cJSON *j = call(s, "ReceiveMessage");
    if (!j) return -1;
    const cJSON *arr = cJSON_GetObjectItem(j, "Messages"), *m;
    int count = cJSON_GetArraySize(arr);
    sqs_msg_t *out = count ? kw_alloc((size_t)count * sizeof *out) : NULL;
    if (count && !out) { cJSON_Delete(j); snprintf(s->last_error, sizeof s->last_error, "ReceiveMessage: out of memory"); return -1; }
    int k = 0;
    cJSON_ArrayForEach(m, arr) {
        const char *id = cJSON_GetStringValue(cJSON_GetObjectItem(m, "MessageId"));
        const char *rh = cJSON_GetStringValue(cJSON_GetObjectItem(m, "ReceiptHandle"));
        const char *body = cJSON_GetStringValue(cJSON_GetObjectItem(m, "Body"));
        const char *md5 = cJSON_GetStringValue(cJSON_GetObjectItem(m, "MD5OfBody"));
        const char *rc = cJSON_GetStringValue(cJSON_GetObjectItem(cJSON_GetObjectItem(m, "Attributes"), "ApproximateReceiveCount"));
        if (!id || !rh || !body) continue;
        size_t bl = strlen(body);           // SQS bodies can't hold a NUL (it's not a valid XML character)
        char got[33];
        md5_hex(body, bl, got);
        if (md5 && strcmp(md5, got)) {
            printf("sqs: message %s: body MD5 %s, SQS says %s: dropped\n", id, got, md5);
            continue;
        }
        sqs_msg_t *o = &out[k];
        o->message_id = kw_strndup(id, strlen(id));
        o->receipt = kw_strndup(rh, strlen(rh));
        o->body = kw_strndup(body, bl);
        o->body_len = bl;
        o->receive_count = rc ? atoi(rc) : 1;
        if (!o->message_id || !o->receipt || !o->body) { sqs_msg_free(o); continue; }   // comes back later
        // SQS never sends more than asked; if it did, the caller's pool has no room for the rest: they
        // stay in flight and come back after visibility_s.
        if (++k == max) break;
    }
    cJSON_Delete(j);
    if (!k) { kw_free(out); out = NULL; }
    *msgs = out;
    *n = k;
    return 0;
}

// The Successful / Failed lists of a *Batch response, by entry id ("0".."9"), into ok[].
static void batch_outcome(sqs_t *s, const cJSON *j, int n, bool *ok, const char *const *bodies, const size_t *lens)
{
    const cJSON *e;
    cJSON_ArrayForEach(e, cJSON_GetObjectItem(j, "Successful")) {
        const char *id = cJSON_GetStringValue(cJSON_GetObjectItem(e, "Id"));
        int i = id ? atoi(id) : -1;
        if (i < 0 || i >= n) continue;
        if (bodies) {
            const char *md5 = cJSON_GetStringValue(cJSON_GetObjectItem(e, "MD5OfMessageBody"));
            char want[33];
            md5_hex(bodies[i], lens[i], want);
            if (!md5 || strcmp(md5, want)) {
                printf("sqs: sent message %d: SQS's MD5 %s, ours %s: not counted as sent\n", i, md5 ? md5 : "(none)", want);
                continue;
            }
        }
        ok[i] = true;
    }
    cJSON_ArrayForEach(e, cJSON_GetObjectItem(j, "Failed")) {
        const char *id = cJSON_GetStringValue(cJSON_GetObjectItem(e, "Id"));
        const char *code = cJSON_GetStringValue(cJSON_GetObjectItem(e, "Code"));
        snprintf(s->last_error, sizeof s->last_error, "entry %s failed: %s", id ? id : "?", code ? code : "?");
    }
}

int sqs_send_batch(sqs_t *s, const char *queue_url, const char *const *bodies, const size_t *lens, int n, bool *ok)
{
    size_t total = 0;
    for (int i = 0; i < n; i++) { ok[i] = false; total += lens[i]; }
    if (n < 1 || n > SQS_MAX_BATCH || total > SQS_MAX_BATCH_BYTES) {
        snprintf(s->last_error, sizeof s->last_error, "SendMessageBatch: %d entries, %zu bytes: too many", n, total);
        return -1;
    }
    begin(s, queue_url);
    jb_puts(&s->req, ",\"Entries\":[");
    for (int i = 0; i < n; i++) {
        jb_printf(&s->req, "%s{\"Id\":\"%d\",\"MessageBody\":", i ? "," : "", i);
        jb_json_str(&s->req, bodies[i], lens[i]);
        jb_putc(&s->req, '}');
    }
    jb_puts(&s->req, "]}");
    cJSON *j = call(s, "SendMessageBatch");
    if (!j) return -1;
    batch_outcome(s, j, n, ok, bodies, lens);
    cJSON_Delete(j);
    return 0;
}

static int receipt_batch(sqs_t *s, const char *action, const char *queue_url, const char *const *receipts, int n,
                         int visibility_s, bool *ok)
{
    for (int i = 0; i < n; i++) ok[i] = false;
    if (n < 1 || n > SQS_MAX_BATCH) return -1;
    begin(s, queue_url);
    jb_puts(&s->req, ",\"Entries\":[");
    for (int i = 0; i < n; i++) {
        jb_printf(&s->req, "%s{\"Id\":\"%d\",\"ReceiptHandle\":", i ? "," : "", i);
        jb_json_str(&s->req, receipts[i], strlen(receipts[i]));
        if (visibility_s >= 0) jb_printf(&s->req, ",\"VisibilityTimeout\":%d", visibility_s);
        jb_putc(&s->req, '}');
    }
    jb_puts(&s->req, "]}");
    cJSON *j = call(s, action);
    if (!j) return -1;
    batch_outcome(s, j, n, ok, NULL, NULL);
    cJSON_Delete(j);
    return 0;
}

int sqs_delete_batch(sqs_t *s, const char *queue_url, const char *const *receipts, int n, bool *ok)
{
    return receipt_batch(s, "DeleteMessageBatch", queue_url, receipts, n, -1, ok);
}

int sqs_visibility_batch(sqs_t *s, const char *queue_url, const char *const *receipts, int n, int visibility_s, bool *ok)
{
    return receipt_batch(s, "ChangeMessageVisibilityBatch", queue_url, receipts, n, visibility_s, ok);
}
