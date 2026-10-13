// CHATS-5: SigV4 (worker/sigv4.c) against AWS's SigV4 test suite (sigv4/, from botocore's copy;
// Apache 2.0, see sigv4/LICENSE and NOTICE). For each vector: the canonical request, the string to
// sign and the Authorization header, byte for byte. Path normalisation (the suite's normalize-path
// group) is S3's business, not SQS's, and isn't vendored.
//
// The suite signs as AKIDEXAMPLE in us-east-1, service "service", at 20150830T123600Z.
#include "kw.h"
#include "sigv4.h"
#include "testutil.h"

static const char *const VECTORS[] = {
    "get-header-key-duplicate", "get-header-value-multiline", "get-header-value-order", "get-header-value-trim",
    "get-unreserved", "get-utf8", "get-vanilla", "get-vanilla-empty-query-key", "get-vanilla-query",
    "get-vanilla-query-order-encoded", "get-vanilla-query-order-key", "get-vanilla-query-order-key-case",
    "get-vanilla-query-order-value", "get-vanilla-query-unreserved", "get-vanilla-utf8-query",
    "get-vanilla-with-session-token", "post-header-key-case", "post-header-key-sort", "post-header-value-case",
    "post-sts-token/post-sts-header-after", "post-sts-token/post-sts-header-before", "post-vanilla",
    "post-vanilla-empty-query-value", "post-vanilla-query", "post-x-www-form-urlencoded",
    "post-x-www-form-urlencoded-parameters",
};

#define MAXH 16

typedef struct {
    char method[8], path[512], query[512];
    sv4_hdr_t h[MAXH];
    int nh;
    char *body;
    size_t body_len;
    char *store;                              // owns the strings
    char *owned[MAXH * 2];                    // and the values
    int n_owned;
} req_t;

static void req_free(req_t *r)
{
    for (int i = 0; i < r->n_owned; i++) free(r->owned[i]);
    free(r->store);
}

// "GET /path?query HTTP/1.1", then "Name:value" lines (a line starting with whitespace continues
// the previous value), then a blank line and the body.
static int parse_req(char *text, req_t *r)
{
    memset(r, 0, sizeof *r);
    r->store = text;
    char *line = text, *nl = strchr(line, '\n');
    if (!nl) return -1;
    *nl = 0;
    char target[512];
    if (sscanf(line, "%7s %511s", r->method, target) != 2) return -1;
    char *q = strchr(target, '?');
    if (q) { *q = 0; snprintf(r->query, sizeof r->query, "%s", q + 1); }
    snprintf(r->path, sizeof r->path, "%s", target);
    line = nl + 1;
    while (*line && *line != '\n') {
        nl = strchr(line, '\n');
        size_t n = nl ? (size_t)(nl - line) : strlen(line);
        if ((*line == ' ' || *line == '\t') && r->nh) {
            // continuation: append "\n" + line to the previous value
            const char *prev = r->h[r->nh - 1].value;
            char *v = malloc(strlen(prev) + n + 2);
            snprintf(v, strlen(prev) + n + 2, "%s\n%.*s", prev, (int)n, line);
            r->h[r->nh - 1].value = v;
            r->owned[r->n_owned++] = v;
        } else {
            char *colon = memchr(line, ':', n);
            if (!colon || r->nh == MAXH) return -1;
            *colon = 0;
            r->h[r->nh].name = line;
            char *v = malloc(n);
            memcpy(v, colon + 1, n - (size_t)(colon + 1 - line));
            v[n - (size_t)(colon + 1 - line)] = 0;
            r->h[r->nh++].value = v;
            r->owned[r->n_owned++] = v;
        }
        if (!nl) { line += n; break; }
        line = nl + 1;
    }
    if (*line == '\n') {
        r->body = line + 1;
        r->body_len = strlen(r->body);
    }
    return 0;
}

int main(void)
{
    const sv4_key_t key = { "AKIDEXAMPLE", "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY", "us-east-1", "service" };
    int n = (int)(sizeof VECTORS / sizeof *VECTORS);
    for (int i = 0; i < n; i++) {
        const char *name = strrchr(VECTORS[i], '/') ? strrchr(VECTORS[i], '/') + 1 : VECTORS[i];
        char path[256];
        req_t r;
        snprintf(path, sizeof path, "../sigv4/%s/%s.req", VECTORS[i], name);
        CHECK(parse_req(read_file_or_die(path, NULL), &r) == 0, "%s: unparseable", name);
        // The session-token vector's token is in its signed request, not its request.
        if (strstr(name, "session-token")) {
            r.h[r.nh].name = "X-Amz-Security-Token";
            r.h[r.nh++].value = "6e86291e8372ff2a2260956d9b8aae1d763fbf315fa00fa31553b73ebf194267";
        }
        const char *date = NULL;
        for (int h = 0; h < r.nh; h++)
            if (!strcmp(r.h[h].name, "X-Amz-Date")) date = r.h[h].value;
        sv4_req_t req = { r.method, r.path, r.query[0] ? r.query : NULL, r.h, r.nh, (const uint8_t *)r.body, r.body_len };
        char authz[512];
        jbuf_t creq, sts;
        jb_init(&creq);
        jb_init(&sts);
        int rc = sv4_sign(&req, &key, date ? date : "", authz, sizeof authz, &creq, &sts);
        CHECK(rc == 0, "%s: sign failed", name);
        snprintf(path, sizeof path, "../sigv4/%s/%s.creq", VECTORS[i], name);
        char *want_creq = read_file_or_die(path, NULL);
        snprintf(path, sizeof path, "../sigv4/%s/%s.sts", VECTORS[i], name);
        char *want_sts = read_file_or_die(path, NULL);
        snprintf(path, sizeof path, "../sigv4/%s/%s.authz", VECTORS[i], name);
        char *want_authz = read_file_or_die(path, NULL);
        CHECK(creq.p && !strcmp(creq.p, want_creq), "%s: canonical request\n--- got\n%s\n--- want\n%s", name, creq.p, want_creq);
        CHECK(sts.p && !strcmp(sts.p, want_sts), "%s: string to sign\n--- got\n%s\n--- want\n%s", name, sts.p, want_sts);
        CHECK(!strcmp(authz, want_authz), "%s: authorization\n  got  %s\n  want %s", name, authz, want_authz);
        jb_free(&creq);
        jb_free(&sts);
        free(want_creq); free(want_sts); free(want_authz);
        req_free(&r);
    }
    printf("%d vectors\n", n);

    // An SQS-shaped request (JSON protocol, temporary credentials) signs, and the signature
    // covers the body: one byte changed changes it.
    const sv4_hdr_t h[] = { { "Content-Type", "application/x-amz-json-1.0" },
                            { "Host", "sqs.eu-west-2.amazonaws.com" },
                            { "X-Amz-Date", "20261013T153716Z" },
                            { "X-Amz-Security-Token", "FwoGZXIvYXdzEXAMPLE" },
                            { "X-Amz-Target", "AmazonSQS.ReceiveMessage" } };
    const sv4_key_t sqs = { "ASIAEXAMPLE", "secretEXAMPLE", "eu-west-2", "sqs" };
    char body[] = "{\"QueueUrl\":\"https://sqs.eu-west-2.amazonaws.com/000000000000/q\"}";
    sv4_req_t req = { "POST", "/", NULL, h, 5, (const uint8_t *)body, strlen(body) };
    char a1[512], a2[512];
    CHECK(sv4_sign(&req, &sqs, "20261013T153716Z", a1, sizeof a1, NULL, NULL) == 0, "sqs sign");
    body[3] ^= 1;
    CHECK(sv4_sign(&req, &sqs, "20261013T153716Z", a2, sizeof a2, NULL, NULL) == 0, "sqs sign");
    CHECK(strcmp(a1, a2) != 0, "the body isn't signed");
    CHECK(strstr(a1, "SignedHeaders=content-type;host;x-amz-date;x-amz-security-token;x-amz-target,") != NULL, "%s", a1);
    CHECK(strstr(a1, "Credential=ASIAEXAMPLE/20261013/eu-west-2/sqs/aws4_request,") != NULL, "%s", a1);
    CHECK(sv4_sign(&req, &sqs, "20261013T153716Z", a1, 40, NULL, NULL) == -1, "a short buffer is refused");
    return report("sigv4");
}
