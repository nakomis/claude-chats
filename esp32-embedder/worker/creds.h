// AWS credentials from the IoT credentials provider (CHATS-5): the board's own certificate (mTLS)
// buys temporary credentials for the role behind its role alias (kata-worker-{env}, one hour), as
// nakostat's AWSAuth getCredentialsWithRoleAlias() does:
//
//   GET https://<cred_ep>/role-aliases/<alias>/credentials      x-amzn-iot-thingname: <thing>
//   -> {"credentials":{"accessKeyId":…,"secretAccessKey":…,"sessionToken":…,"expiration":"…Z"}}
//
// They're cached and fetched again only within CREDS_REFRESH_S of expiring: about once an hour,
// one more TLS handshake (the firmware does it before opening SQS's session, never alongside).
// The TLS is the firmware's (kata_net.c); this is the portable part, host-tested with a fake.
#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "kw.h"
#include "sigv4.h"

#define CREDS_REFRESH_S 600

typedef struct {
    char access_key[32];
    char secret_key[64];
    char *token;            // kw_alloc'd (~1 KB)
    int64_t expires;        // Unix seconds; 0 = none
} aws_creds_t;

// One HTTPS GET with the board's certificate: the HTTP status (body in resp), or -1 if the request
// didn't happen. hdrs are the extra headers to send.
typedef int (*creds_get_fn)(void *ctx, const char *host, const char *path, const sv4_hdr_t *hdrs, int n_hdrs, jbuf_t *resp);

// Parses the provider's response. 0, or -1 if anything's missing or malformed.
int creds_parse(const char *json, aws_creds_t *c);
void creds_clear(aws_creds_t *c);
// Still good for at least margin_s more seconds?
bool creds_fresh(const aws_creds_t *c, int64_t now, int margin_s);

// Fetches new credentials if c isn't fresh for CREDS_REFRESH_S more: 1 = fetched, 0 = the cached
// ones are good, -1 = failed (c keeps whatever it had; why says why).
int creds_refresh(aws_creds_t *c, int64_t now, const char *endpoint, const char *role_alias, const char *thing,
                  creds_get_fn get, void *ctx, char *why, size_t why_len);
