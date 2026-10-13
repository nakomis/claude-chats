// AWS credentials from the IoT credentials provider (creds.h).
#include "creds.h"

#include <stdio.h>
#include <string.h>

#include "cJSON.h"

void creds_clear(aws_creds_t *c)
{
    if (c->token) {
        memset(c->token, 0, strlen(c->token));
        kw_free(c->token);
    }
    memset(c, 0, sizeof *c);
}

int creds_parse(const char *json, aws_creds_t *c)
{
    cJSON *root = cJSON_Parse(json);
    const cJSON *cr = cJSON_GetObjectItem(root, "credentials");
    const char *ak = cJSON_GetStringValue(cJSON_GetObjectItem(cr, "accessKeyId"));
    const char *sk = cJSON_GetStringValue(cJSON_GetObjectItem(cr, "secretAccessKey"));
    const char *tok = cJSON_GetStringValue(cJSON_GetObjectItem(cr, "sessionToken"));
    const char *exp = cJSON_GetStringValue(cJSON_GetObjectItem(cr, "expiration"));
    int64_t expires = exp ? kw_parse_iso8601(exp) : -1;
    int rc = -1;
    if (ak && sk && tok && *ak && *sk && *tok && expires > 0 && strlen(ak) < sizeof c->access_key &&
        strlen(sk) < sizeof c->secret_key) {
        char *t = kw_strndup(tok, strlen(tok));
        if (t) {
            creds_clear(c);
            snprintf(c->access_key, sizeof c->access_key, "%s", ak);
            snprintf(c->secret_key, sizeof c->secret_key, "%s", sk);
            c->token = t;
            c->expires = expires;
            rc = 0;
        }
    }
    cJSON_Delete(root);
    return rc;
}

bool creds_fresh(const aws_creds_t *c, int64_t now, int margin_s)
{
    return c->token && c->expires > now + margin_s;
}

int creds_refresh(aws_creds_t *c, int64_t now, const char *endpoint, const char *role_alias, const char *thing,
                  creds_get_fn get, void *ctx, char *why, size_t why_len)
{
    if (creds_fresh(c, now, CREDS_REFRESH_S)) return 0;
    if (!endpoint[0] || !role_alias[0] || !thing[0]) {
        snprintf(why, why_len, "no credentials endpoint or role alias provisioned");
        return -1;
    }
    char path[128];
    snprintf(path, sizeof path, "/role-aliases/%s/credentials", role_alias);
    const sv4_hdr_t h[] = { { "x-amzn-iot-thingname", thing } };
    jbuf_t resp;
    jb_init(&resp);
    int status = get(ctx, endpoint, path, h, 1, &resp);
    int rc = -1;
    if (status != 200) {
        // the provider's errors are {"message": "..."}
        cJSON *e = status > 0 && resp.p ? cJSON_Parse(resp.p) : NULL;
        const char *m = cJSON_GetStringValue(cJSON_GetObjectItem(e, "message"));
        snprintf(why, why_len, "credentials provider: %s%d%s%s", status < 0 ? "no response " : "HTTP ", status,
                 m ? ": " : "", m ? m : "");
        cJSON_Delete(e);
    } else if (!resp.p || creds_parse(resp.p, c)) {
        snprintf(why, why_len, "credentials provider: unexpected response");
    } else if (!creds_fresh(c, now, 60)) {
        snprintf(why, why_len, "credentials provider: credentials already expired (is the clock right?)");
    } else {
        rc = 1;
    }
    if (resp.p) memset(resp.p, 0, resp.n);   // it held a secret key
    jb_free(&resp);
    return rc;
}
