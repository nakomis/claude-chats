// CHATS-5: the credentials-provider client (worker/creds.c) with a fake provider, and the time
// helpers it relies on (worker/kw.c).
#include "creds.h"
#include "kw.h"
#include "testutil.h"

typedef struct {
    int calls, status;
    const char *body;
    char path[128], thing[64], host[96];
} fake_t;

static int get(void *ctx, const char *host, const char *path, const sv4_hdr_t *h, int nh, jbuf_t *resp)
{
    fake_t *f = ctx;
    f->calls++;
    snprintf(f->host, sizeof f->host, "%s", host);
    snprintf(f->path, sizeof f->path, "%s", path);
    for (int i = 0; i < nh; i++)
        if (!strcmp(h[i].name, "x-amzn-iot-thingname")) snprintf(f->thing, sizeof f->thing, "%s", h[i].value);
    if (f->status < 0) return -1;
    jb_puts(resp, f->body);
    return f->status;
}

#define OK_BODY(exp)                                                                                   \
    "{\"credentials\":{\"accessKeyId\":\"ASIATESTONLY\",\"secretAccessKey\":\"secret/TESTONLY+\","     \
    "\"sessionToken\":\"IQoJb3JpZ2luX2VjEXAMPLE==\",\"expiration\":\"" exp "\"}}"

int main(void)
{
    // time
    CHECK(kw_parse_iso8601("1970-01-01T00:00:00Z") == 0, "epoch");
    CHECK(kw_parse_iso8601("2026-10-13T18:00:00Z") == 1791914400, "%lld", (long long)kw_parse_iso8601("2026-10-13T18:00:00Z"));
    CHECK(kw_parse_iso8601("2026-10-13T18:00:00.123Z") == 1791914400, "fraction");
    CHECK(kw_parse_iso8601("2026-10-13T18:00:00+00:00") == 1791914400, "+00:00");
    CHECK(kw_parse_iso8601("2026-10-13T18:00:00+01:00") == -1, "another offset is refused");
    CHECK(kw_parse_iso8601("2026-13-13T18:00:00Z") == -1 && kw_parse_iso8601("nonsense") == -1, "malformed");
    CHECK(kw_parse_iso8601("2024-02-29T12:00:00Z") == 1709208000, "leap day");
    char d[17], ymd[11];
    kw_amz_date(1791914400 + 59, d);
    CHECK(!strcmp(d, "20261013T180059Z"), "%s", d);
    kw_amz_date(1440938160, d);
    CHECK(!strcmp(d, "20150830T123600Z"), "the SigV4 suite's date: %s", d);
    kw_ymd(1791914400, ymd);
    CHECK(!strcmp(ymd, "2026-10-13"), "%s", ymd);

    // the provider
    const int64_t now = 1791914400 - 3600;   // an hour before the credentials expire
    fake_t f = { 0, 200, OK_BODY("2026-10-13T18:00:00Z"), "", "", "" };
    aws_creds_t c = { 0 };
    char why[160];
    CHECK(creds_refresh(&c, now, "abc.credentials.iot.eu-west-2.amazonaws.com", "kata-sandbox", "kata-sandbox-blue", get, &f,
                        why, sizeof why) == 1, "fetch: %s", why);
    CHECK(!strcmp(f.path, "/role-aliases/kata-sandbox/credentials") && !strcmp(f.thing, "kata-sandbox-blue") &&
          !strcmp(f.host, "abc.credentials.iot.eu-west-2.amazonaws.com"), "request %s %s", f.path, f.thing);
    CHECK(!strcmp(c.access_key, "ASIATESTONLY") && !strcmp(c.secret_key, "secret/TESTONLY+") &&
          c.token && !strcmp(c.token, "IQoJb3JpZ2luX2VjEXAMPLE==") && c.expires == 1791914400, "parsed");
    // cached until 10 minutes before expiry
    CHECK(creds_refresh(&c, now + 2000, "e", "a", "t", get, &f, why, sizeof why) == 0 && f.calls == 1, "cached");
    CHECK(creds_refresh(&c, 1791914400 - CREDS_REFRESH_S + 1, "e", "a", "t", get, &f, why, sizeof why) == 1 && f.calls == 2,
          "refreshed near expiry");
    // failures keep what we had, and say why
    f.status = 403;
    f.body = "{\"message\":\"Access Denied\"}";
    int64_t later = 1791914400 - 100;
    CHECK(creds_refresh(&c, later, "e", "a", "t", get, &f, why, sizeof why) == -1 && strstr(why, "403") && strstr(why, "Access Denied"),
          "403: %s", why);
    CHECK(c.token != NULL, "kept the old ones");
    f.status = -1;
    CHECK(creds_refresh(&c, later, "e", "a", "t", get, &f, why, sizeof why) == -1 && strstr(why, "no response"), "%s", why);
    f.status = 200;
    f.body = "{\"credentials\":{\"accessKeyId\":\"A\"}}";
    CHECK(creds_refresh(&c, later, "e", "a", "t", get, &f, why, sizeof why) == -1 && strstr(why, "unexpected"), "%s", why);
    f.body = OK_BODY("2020-01-01T00:00:00Z");
    CHECK(creds_refresh(&c, later, "e", "a", "t", get, &f, why, sizeof why) == -1 && strstr(why, "clock"), "%s", why);
    CHECK(creds_refresh(&c, later, "", "a", "t", get, &f, why, sizeof why) == -1 && strstr(why, "provisioned"), "%s", why);
    creds_clear(&c);
    CHECK(!c.token && !creds_fresh(&c, 0, 0), "cleared");
    return report("creds");
}
