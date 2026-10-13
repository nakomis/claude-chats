// A fake Amazon SQS (fake_sqs.h).
#include "fake_sqs.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "cJSON.h"
#include "mbedtls/md5.h"
#include "sigv4.h"

static char *dup(const char *s) { size_t n = strlen(s); char *d = malloc(n + 1); memcpy(d, s, n + 1); return d; }

void fq_init(fake_sqs_t *f, const aws_creds_t *creds, int64_t now)
{
    memset(f, 0, sizeof *f);
    f->creds = creds;
    f->now = now;
}

void fq_free(fake_sqs_t *f)
{
    for (int i = 0; i < f->nq; i++)
        for (int j = 0; j < f->q[i].n; j++) {
            free(f->q[i].msgs[j].id);
            free(f->q[i].msgs[j].body);
            free(f->q[i].msgs[j].receipt);
        }
    f->nq = 0;
}

fq_queue_t *fq_queue(fake_sqs_t *f, const char *url)
{
    for (int i = 0; i < f->nq; i++)
        if (!strcmp(f->q[i].url, url)) return &f->q[i];
    if (f->nq == FQ_MAX_QUEUES) return NULL;
    fq_queue_t *q = &f->q[f->nq++];
    memset(q, 0, sizeof *q);
    snprintf(q->url, sizeof q->url, "%s", url);
    return q;
}

const char *fq_send(fake_sqs_t *f, const char *url, const char *body)
{
    fq_queue_t *q = fq_queue(f, url);
    if (!q || q->n == FQ_MAX_MSGS) return NULL;
    fq_msg_t *m = &q->msgs[q->n++];
    memset(m, 0, sizeof *m);
    char id[48];
    snprintf(id, sizeof id, "00000000-0000-4000-8000-%012d", ++f->seq);
    m->id = dup(id);
    m->body = dup(body);
    m->visible_at = f->now;
    return m->id;
}

int fq_live(const fq_queue_t *q)
{
    int n = 0;
    for (int i = 0; i < q->n; i++) n += !q->msgs[i].deleted;
    return n;
}

int fq_visible(const fake_sqs_t *f, const fq_queue_t *q)
{
    int n = 0;
    for (int i = 0; i < q->n; i++) n += !q->msgs[i].deleted && q->msgs[i].visible_at <= f->now;
    return n;
}

int64_t fq_now(void *ctx) { return ((fake_sqs_t *)ctx)->now; }

static void md5_hex(const char *s, char out[33])
{
    uint8_t h[16];
    mbedtls_md5((const uint8_t *)s, strlen(s), h);
    for (int i = 0; i < 16; i++) snprintf(out + 2 * i, 3, "%02x", h[i]);
}

static const char *header(const http_req_t *r, const char *name)
{
    for (int i = 0; i < r->n_hdrs; i++)
        if (!strcasecmp(r->hdrs[i].name, name)) return r->hdrs[i].value;
    return NULL;
}

// Signs the request again (everything but Authorization) and compares.
static bool signature_ok(fake_sqs_t *f, const http_req_t *r)
{
    const char *authz = header(r, "Authorization"), *date = header(r, "X-Amz-Date");
    if (!authz || !date || !f->creds) return false;
    sv4_hdr_t h[8];
    int n = 0;
    for (int i = 0; i < r->n_hdrs && n < 8; i++)
        if (strcasecmp(r->hdrs[i].name, "Authorization")) h[n++] = r->hdrs[i];
    const char *region = strstr(authz, "/eu-west-2/") ? "eu-west-2" : "us-east-1";
    sv4_key_t k = { f->creds->access_key, f->creds->secret_key, region, "sqs" };
    sv4_req_t sr = { "POST", r->path, NULL, h, n, (const uint8_t *)r->body, r->body_len };
    char want[320];
    return !sv4_sign(&sr, &k, date, want, sizeof want, NULL, NULL) && !strcmp(want, authz);
}

static int reply(jbuf_t *resp, cJSON *j, int status)
{
    char *s = cJSON_PrintUnformatted(j);
    jb_puts(resp, s);
    free(s);
    cJSON_Delete(j);
    return status;
}

static int error(jbuf_t *resp, int status, const char *type, const char *msg)
{
    cJSON *j = cJSON_CreateObject();
    char t[96];
    snprintf(t, sizeof t, "com.amazonaws.sqs#%s", type);
    cJSON_AddStringToObject(j, "__type", t);
    cJSON_AddStringToObject(j, "message", msg);
    return reply(resp, j, status);
}

static fq_msg_t *by_receipt(fq_queue_t *q, const char *rh, bool *stale)
{
    *stale = false;
    for (int i = 0; i < q->n; i++) {
        if (q->msgs[i].receipt && !strcmp(q->msgs[i].receipt, rh)) return &q->msgs[i];
        // a receipt from an earlier receive of this message: "rh-<id>-<n>"
        size_t il = strlen(q->msgs[i].id);
        if (!strncmp(rh, "rh-", 3) && !strncmp(rh + 3, q->msgs[i].id, il) && rh[3 + il] == '-') { *stale = true; return &q->msgs[i]; }
    }
    return NULL;
}

int fq_post(void *ctx, const http_req_t *r, jbuf_t *resp)
{
    fake_sqs_t *f = ctx;
    if (f->fail_next > 0) { f->fail_next--; return -1; }
    if (f->http_500_next > 0) { f->http_500_next--; return error(resp, 500, "InternalError", "injected"); }
    if (!signature_ok(f, r)) { f->bad_signatures++; return error(resp, 403, "SignatureDoesNotMatch", "bad signature"); }
    const char *target = header(r, "X-Amz-Target");
    if (f->fail_target && target && !strcmp(target, f->fail_target) && f->fail_target_next > 0) { f->fail_target_next--; return -1; }
    cJSON *in = cJSON_ParseWithLength(r->body, r->body_len);
    const char *url = cJSON_GetStringValue(cJSON_GetObjectItem(in, "QueueUrl"));
    fq_queue_t *q = url ? fq_queue(f, url) : NULL;
    if (!target || !in || !q) { cJSON_Delete(in); return error(resp, 400, "InvalidParameterValue", "bad request"); }
    cJSON *out = cJSON_CreateObject();
    const cJSON *e;

    if (!strcmp(target, "AmazonSQS.ReceiveMessage")) {
        f->calls[0]++;
        int max = (int)cJSON_GetNumberValue(cJSON_GetObjectItem(in, "MaxNumberOfMessages"));
        int wait = (int)cJSON_GetNumberValue(cJSON_GetObjectItem(in, "WaitTimeSeconds"));
        int vis = (int)cJSON_GetNumberValue(cJSON_GetObjectItem(in, "VisibilityTimeout"));
        if (max < 1 || max > 10) { cJSON_Delete(in); cJSON_Delete(out); return error(resp, 400, "InvalidParameterValue", "max"); }
        if (!fq_visible(f, q)) f->now += wait;   // a long poll that finds nothing
        cJSON *arr = cJSON_AddArrayToObject(out, "Messages");
        int got = 0;
        for (int i = 0; i < q->n && got < max; i++) {
            fq_msg_t *m = &q->msgs[i];
            if (m->deleted || m->visible_at > f->now) continue;
            m->receive_count++;
            m->visible_at = f->now + vis;
            m->in_flight = true;
            char rh[96], md5[33], rc[16];
            snprintf(rh, sizeof rh, "rh-%s-%d", m->id, m->receive_count);
            free(m->receipt);
            m->receipt = dup(rh);
            md5_hex(m->body, md5);
            if (f->corrupt_md5) md5[0] = md5[0] == '0' ? '1' : '0';
            snprintf(rc, sizeof rc, "%d", m->receive_count);
            cJSON *o = cJSON_CreateObject();
            cJSON_AddStringToObject(o, "MessageId", m->id);
            cJSON_AddStringToObject(o, "ReceiptHandle", rh);
            cJSON_AddStringToObject(o, "MD5OfBody", md5);
            cJSON_AddStringToObject(o, "Body", m->body);
            cJSON_AddStringToObject(cJSON_AddObjectToObject(o, "Attributes"), "ApproximateReceiveCount", rc);
            cJSON_AddItemToArray(arr, o);
            got++;
        }
        if (!got) cJSON_DeleteItemFromObject(out, "Messages");   // as SQS: no Messages key at all
    } else if (!strcmp(target, "AmazonSQS.SendMessageBatch")) {
        f->calls[1]++;
        int64_t total = 0;
        cJSON_ArrayForEach(e, cJSON_GetObjectItem(in, "Entries")) total += (int64_t)strlen(cJSON_GetStringValue(cJSON_GetObjectItem(e, "MessageBody")));
        if (total > f->max_batch_seen) f->max_batch_seen = total;
        if (total > SQS_MAX_BATCH_BYTES) { cJSON_Delete(in); cJSON_Delete(out); return error(resp, 400, "BatchRequestTooLong", "too long"); }
        cJSON *ok = cJSON_AddArrayToObject(out, "Successful"), *bad = cJSON_AddArrayToObject(out, "Failed");
        cJSON_ArrayForEach(e, cJSON_GetObjectItem(in, "Entries")) {
            const char *id = cJSON_GetStringValue(cJSON_GetObjectItem(e, "Id"));
            const char *body = cJSON_GetStringValue(cJSON_GetObjectItem(e, "MessageBody"));
            cJSON *o = cJSON_CreateObject();
            cJSON_AddStringToObject(o, "Id", id);
            if (f->refuse_body_containing && strstr(body, f->refuse_body_containing)) {
                cJSON_AddStringToObject(o, "Code", "InternalError");
                cJSON_AddBoolToObject(o, "SenderFault", false);
                cJSON_AddItemToArray(bad, o);
                continue;
            }
            const char *mid = fq_send(f, url, body);
            char md5[33];
            md5_hex(body, md5);
            cJSON_AddStringToObject(o, "MessageId", mid ? mid : "?");
            cJSON_AddStringToObject(o, "MD5OfMessageBody", md5);
            cJSON_AddItemToArray(ok, o);
        }
    } else if (!strcmp(target, "AmazonSQS.DeleteMessageBatch") || !strcmp(target, "AmazonSQS.ChangeMessageVisibilityBatch")) {
        bool del = !strcmp(target, "AmazonSQS.DeleteMessageBatch");
        f->calls[del ? 2 : 3]++;
        cJSON *ok = cJSON_AddArrayToObject(out, "Successful"), *bad = cJSON_AddArrayToObject(out, "Failed");
        cJSON_ArrayForEach(e, cJSON_GetObjectItem(in, "Entries")) {
            const char *id = cJSON_GetStringValue(cJSON_GetObjectItem(e, "Id"));
            const char *rh = cJSON_GetStringValue(cJSON_GetObjectItem(e, "ReceiptHandle"));
            bool stale;
            fq_msg_t *m = rh ? by_receipt(q, rh, &stale) : NULL;
            cJSON *o = cJSON_CreateObject();
            cJSON_AddStringToObject(o, "Id", id);
            if (del) {
                // SQS: a stale handle may succeed without deleting (the design's "silently doesn't")
                if (m && !stale) { m->deleted = true; m->in_flight = false; }
                if (m) { cJSON_AddItemToArray(ok, o); continue; }
            } else if (m && !stale && !m->deleted && m->visible_at > f->now) {
                m->visible_at = f->now + (int)cJSON_GetNumberValue(cJSON_GetObjectItem(e, "VisibilityTimeout"));
                cJSON_AddItemToArray(ok, o);
                continue;
            }
            cJSON_AddStringToObject(o, "Code", "ReceiptHandleIsInvalid");
            cJSON_AddBoolToObject(o, "SenderFault", true);
            cJSON_AddItemToArray(bad, o);
        }
    } else {
        f->calls[4]++;
        cJSON_Delete(in);
        cJSON_Delete(out);
        return error(resp, 400, "UnsupportedOperation", target);
    }
    cJSON_Delete(in);
    return reply(resp, out, 200);
}
