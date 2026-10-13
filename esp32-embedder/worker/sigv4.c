// AWS SigV4 header signing (sigv4.h).
#include "sigv4.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "mbedtls/md.h"
#include "mbedtls/sha256.h"

#define MAX_HDRS 16
#define MAX_PARAMS 32

static void hex(const uint8_t *in, size_t n, char *out)
{
    static const char H[] = "0123456789abcdef";
    for (size_t i = 0; i < n; i++) { out[2 * i] = H[in[i] >> 4]; out[2 * i + 1] = H[in[i] & 15]; }
    out[2 * n] = 0;
}

void sv4_sha256_hex(const void *data, size_t n, char out[65])
{
    uint8_t h[32];
    mbedtls_sha256(data, n, h, 0);
    hex(h, 32, out);
}

static void hmac(const uint8_t *key, size_t klen, const void *msg, size_t mlen, uint8_t out[32])
{
    mbedtls_md_hmac(mbedtls_md_info_from_type(MBEDTLS_MD_SHA256), key, klen, msg, mlen, out);
}

static bool unreserved(unsigned char c)
{
    return (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '-' || c == '.' ||
           c == '_' || c == '~';
}

// %-encodes every byte that isn't unreserved (and, for a path, '/')
static void uri_encode(jbuf_t *b, const char *s, size_t n, bool path)
{
    for (size_t i = 0; i < n; i++) {
        unsigned char c = (unsigned char)s[i];
        if (unreserved(c) || (path && c == '/')) jb_putc(b, (char)c);
        else jb_printf(b, "%%%02X", c);
    }
}

static int hexval(char c)
{
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

// a query key or value, %-decoded and then encoded canonically
static void query_part(jbuf_t *b, const char *s, size_t n)
{
    jbuf_t raw;
    jb_init(&raw);
    for (size_t i = 0; i < n; i++) {
        int h, l;
        if (s[i] == '%' && i + 2 < n && (h = hexval(s[i + 1])) >= 0 && (l = hexval(s[i + 2])) >= 0) {
            jb_putc(&raw, (char)(h << 4 | l));
            i += 2;
        } else {
            jb_putc(&raw, s[i]);
        }
    }
    uri_encode(b, raw.p ? raw.p : "", raw.n, false);
    b->oom |= raw.oom;
    jb_free(&raw);
}

typedef struct { char *k, *v; } kv_t;

static int kv_cmp(const void *a, const void *b)
{
    const kv_t *x = a, *y = b;
    int c = strcmp(x->k, y->k);
    return c ? c : strcmp(x->v, y->v);
}

static int name_cmp(const void *a, const void *b) { return strcmp(((const kv_t *)a)->k, ((const kv_t *)b)->k); }

static void canonical_query(jbuf_t *out, const char *q)
{
    if (!q || !*q) return;
    kv_t kv[MAX_PARAMS];
    int n = 0;
    for (const char *p = q; *p && n < MAX_PARAMS;) {
        const char *amp = strchr(p, '&'), *end = amp ? amp : p + strlen(p);
        const char *eq = memchr(p, '=', (size_t)(end - p));
        jbuf_t k, v;
        jb_init(&k);
        jb_init(&v);
        query_part(&k, p, (size_t)((eq ? eq : end) - p));
        if (eq) query_part(&v, eq + 1, (size_t)(end - eq - 1));
        jb_puts(&k, "");
        jb_puts(&v, "");
        kv[n].k = jb_take(&k, NULL);
        kv[n].v = jb_take(&v, NULL);
        if (!kv[n].k || !kv[n].v) { out->oom = true; kw_free(kv[n].k); kw_free(kv[n].v); break; }
        n++;
        p = amp ? amp + 1 : end;
    }
    qsort(kv, (size_t)n, sizeof *kv, kv_cmp);
    for (int i = 0; i < n; i++) {
        if (i) jb_putc(out, '&');
        jb_puts(out, kv[i].k);
        jb_putc(out, '=');
        jb_puts(out, kv[i].v);
        kw_free(kv[i].k);
        kw_free(kv[i].v);
    }
}

// trimmed, with each run of whitespace inside folded to one space
static void header_value(jbuf_t *b, const char *s)
{
    while (*s == ' ' || *s == '\t' || *s == '\n' || *s == '\r') s++;
    bool space = false;
    for (; *s; s++) {
        bool ws = *s == ' ' || *s == '\t' || *s == '\n' || *s == '\r';
        if (ws) { space = true; continue; }
        if (space) jb_putc(b, ' ');
        space = false;
        jb_putc(b, *s);
    }
}

int sv4_sign(const sv4_req_t *r, const sv4_key_t *k, const char *amz_date, char *authz, size_t authz_len, jbuf_t *creq_out,
             jbuf_t *sts_out)
{
    if (r->n_hdrs > MAX_HDRS || strlen(amz_date) != 16) return -1;
    int rc = -1;
    jbuf_t creq, sts, signed_hdrs;
    jb_init(&creq);
    jb_init(&sts);
    jb_init(&signed_hdrs);

    // headers: names lower-cased, values folded; a name given more than once is one header whose
    // values are joined with commas, in the order given
    kv_t h[MAX_HDRS];
    int nh = 0;
    for (int i = 0; i < r->n_hdrs; i++) {
        jbuf_t name, val;
        jb_init(&name);
        jb_init(&val);
        for (const char *c = r->hdrs[i].name; *c; c++) jb_putc(&name, (char)(*c >= 'A' && *c <= 'Z' ? *c + 32 : *c));
        header_value(&val, r->hdrs[i].value);
        jb_puts(&val, "");
        int j;
        for (j = 0; j < nh && (!name.p || strcmp(h[j].k, name.p)); j++) {}
        if (j < nh) {
            jbuf_t joined;
            jb_init(&joined);
            jb_puts(&joined, h[j].v);
            jb_putc(&joined, ',');
            jb_puts(&joined, val.p ? val.p : "");
            kw_free(h[j].v);
            h[j].v = jb_take(&joined, NULL);
            jb_free(&name);
            jb_free(&val);
            if (!h[j].v) { h[j].v = kw_strndup("", 0); creq.oom = true; }
        } else {
            h[nh].k = jb_take(&name, NULL);
            h[nh].v = jb_take(&val, NULL);
            if (!h[nh].k || !h[nh].v) { kw_free(h[nh].k); kw_free(h[nh].v); creq.oom = true; continue; }
            nh++;
        }
    }
    qsort(h, (size_t)nh, sizeof *h, name_cmp);    // names are unique now

    char body_hash[65];
    sv4_sha256_hex(r->body ? (const void *)r->body : "", r->body_len, body_hash);

    jb_puts(&creq, r->method);
    jb_putc(&creq, '\n');
    uri_encode(&creq, r->path, strlen(r->path), true);
    jb_putc(&creq, '\n');
    canonical_query(&creq, r->query);
    jb_putc(&creq, '\n');
    for (int i = 0; i < nh; i++) {
        jb_puts(&creq, h[i].k);
        jb_putc(&creq, ':');
        jb_puts(&creq, h[i].v);
        jb_putc(&creq, '\n');
        if (i) jb_putc(&signed_hdrs, ';');
        jb_puts(&signed_hdrs, h[i].k);
    }
    jb_putc(&creq, '\n');
    jb_put(&creq, signed_hdrs.p, signed_hdrs.n);
    jb_putc(&creq, '\n');
    jb_puts(&creq, body_hash);
    if (creq.oom || signed_hdrs.oom) goto out;

    char date[9], scope[96], creq_hash[65];
    memcpy(date, amz_date, 8);
    date[8] = 0;
    if (snprintf(scope, sizeof scope, "%s/%s/%s/aws4_request", date, k->region, k->service) >= (int)sizeof scope) goto out;
    sv4_sha256_hex(creq.p, creq.n, creq_hash);
    jb_printf(&sts, "AWS4-HMAC-SHA256\n%s\n%s\n%s", amz_date, scope, creq_hash);
    if (sts.oom) goto out;

    uint8_t key[32];
    jbuf_t secret;
    jb_init(&secret);
    jb_printf(&secret, "AWS4%s", k->secret_key);
    if (secret.oom) { jb_free(&secret); goto out; }
    hmac((const uint8_t *)secret.p, secret.n, date, 8, key);
    memset(secret.p, 0, secret.n);
    jb_free(&secret);
    hmac(key, 32, k->region, strlen(k->region), key);
    hmac(key, 32, k->service, strlen(k->service), key);
    hmac(key, 32, "aws4_request", 12, key);
    uint8_t sig[32];
    char sig_hex[65];
    hmac(key, 32, sts.p, sts.n, sig);
    memset(key, 0, sizeof key);
    hex(sig, 32, sig_hex);
    int n = snprintf(authz, authz_len, "AWS4-HMAC-SHA256 Credential=%s/%s, SignedHeaders=%s, Signature=%s", k->access_key,
                     scope, signed_hdrs.p, sig_hex);
    if (n < 0 || (size_t)n >= authz_len) goto out;
    if (creq_out) jb_put(creq_out, creq.p, creq.n);
    if (sts_out) jb_put(sts_out, sts.p, sts.n);
    rc = 0;
out:
    for (int i = 0; i < nh; i++) { kw_free(h[i].k); kw_free(h[i].v); }
    jb_free(&creq);
    jb_free(&sts);
    jb_free(&signed_hdrs);
    return rc;
}
