// CHATS-16: the LAN status endpoint's portable half (worker/httpreq.c): routing, response heads,
// and who may ask.
#include <string.h>

#include "httpreq.h"
#include "testutil.h"

static httpreq_route_t route(const char *s) { return httpreq_route(s, strlen(s)); }

static uint32_t ip(const char *s)
{
    uint32_t v = 0;
    if (!httpreq_parse_ip(s, strlen(s), &v)) printf("bad test address %s\n", s);
    return v;
}

static void test_route(void)
{
    // What nginx sends (proxy_pass, HTTP/1.0 by default)
    CHECK(route("GET /json HTTP/1.0\r\nHost: kata\r\nConnection: close\r\n\r\n") == HTTPREQ_JSON, "nginx");
    CHECK(route("GET /json HTTP/1.1\r\nHost: 172.29.0.50\r\n\r\n") == HTTPREQ_JSON, "curl");
    CHECK(route("GET /json?t=123 HTTP/1.1\r\n\r\n") == HTTPREQ_JSON, "a cache-buster query");
    CHECK(route("GET /json HTTP/1.1\n\n") == HTTPREQ_JSON, "bare LFs");
    CHECK(route("GET / HTTP/1.1\r\n\r\n") == HTTPREQ_NOT_FOUND, "the page is nginx's, not Kata's");
    CHECK(route("GET /jsonx HTTP/1.1\r\n\r\n") == HTTPREQ_NOT_FOUND, "a longer path");
    CHECK(route("GET /json/ HTTP/1.1\r\n\r\n") == HTTPREQ_NOT_FOUND, "a trailing slash");
    CHECK(route("GET /JSON HTTP/1.1\r\n\r\n") == HTTPREQ_NOT_FOUND, "paths are case-sensitive");
    CHECK(route("POST /json HTTP/1.1\r\nContent-Length: 0\r\n\r\n") == HTTPREQ_BAD_METHOD, "POST");
    CHECK(route("HEAD /json HTTP/1.1\r\n\r\n") == HTTPREQ_BAD_METHOD, "HEAD");
    CHECK(route("get /json HTTP/1.1\r\n\r\n") == HTTPREQ_BAD_METHOD, "methods are case-sensitive");

    // Not yet a whole head
    CHECK(route("") == HTTPREQ_INCOMPLETE, "nothing");
    CHECK(route("GET /json HTTP/1.1\r\n") == HTTPREQ_INCOMPLETE, "no blank line");
    CHECK(route("GET /json HTTP/1.1\r\nHost: x\r\n\r") == HTTPREQ_INCOMPLETE, "half a blank line");

    // Malformed
    CHECK(route("GET /json\r\n\r\n") == HTTPREQ_BAD, "HTTP/0.9");
    CHECK(route("GET /json HTTP/2.0\r\n\r\n") == HTTPREQ_BAD, "HTTP/2");
    CHECK(route("GET /json HTTP/1.1 \r\n\r\n") == HTTPREQ_BAD, "a trailing space");
    CHECK(route("GET  /json HTTP/1.1\r\n\r\n") == HTTPREQ_BAD, "an empty target");
    CHECK(route(" /json HTTP/1.1\r\n\r\n") == HTTPREQ_BAD, "no method");
    CHECK(route("GET json HTTP/1.1\r\n\r\n") == HTTPREQ_BAD, "a target without a slash");
    CHECK(route("GET /js\x01on HTTP/1.1\r\n\r\n") == HTTPREQ_BAD, "a control character");
    CHECK(route("\r\n\r\n") == HTTPREQ_BAD, "an empty request line");

    // Too long without its blank line: refused rather than read forever
    char big[HTTPREQ_MAX_HEAD + 8];
    memset(big, 'a', sizeof big);
    memcpy(big, "GET /json HTTP/1.1\r\nX: ", 23);
    CHECK(httpreq_route(big, HTTPREQ_MAX_HEAD - 1) == HTTPREQ_INCOMPLETE, "just under the limit");
    CHECK(httpreq_route(big, HTTPREQ_MAX_HEAD) == HTTPREQ_BAD, "at the limit");

    CHECK(httpreq_status(HTTPREQ_JSON) == 200 && httpreq_status(HTTPREQ_NOT_FOUND) == 404 &&
              httpreq_status(HTTPREQ_BAD_METHOD) == 405 && httpreq_status(HTTPREQ_BAD) == 400 &&
              httpreq_status(HTTPREQ_INCOMPLETE) == 0,
          "status codes");
}

static void test_head(void)
{
    char h[512];
    size_t n = httpreq_head(h, sizeof h, 200, "application/json", 1234, 0);
    CHECK(n == strlen(h), "length");
    CHECK(!strcmp(h, "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 1234\r\n"
                     "Cache-Control: no-store\r\nConnection: close\r\n\r\n"),
          "200: %s", h);
    httpreq_head(h, sizeof h, 503, "application/json", 40, 5);
    CHECK(strstr(h, "HTTP/1.1 503 Service Unavailable\r\n") == h && strstr(h, "Retry-After: 5\r\n"), "503: %s", h);
    httpreq_head(h, sizeof h, 405, "text/plain", 0, 0);
    CHECK(strstr(h, "Allow: GET\r\n") && !strstr(h, "Retry-After"), "405: %s", h);
    httpreq_head(h, sizeof h, 404, "text/plain", 10, 0);
    CHECK(!strstr(h, "Allow:"), "404 has no Allow: %s", h);
    CHECK(httpreq_head(h, 20, 200, "application/json", 1, 0) == 0 && h[0] == 0, "too small: nothing");
}

static void test_ip(void)
{
    uint32_t v;
    CHECK(httpreq_parse_ip("172.29.0.32", 11, &v) && v == 0xac1d0020u, "Leia");
    CHECK(httpreq_parse_ip("0.0.0.0", 7, &v) && v == 0, "zeros");
    CHECK(httpreq_parse_ip("255.255.255.255", 15, &v) && v == 0xffffffffu, "ones");
    CHECK(!httpreq_parse_ip("256.1.1.1", 9, &v), "an octet over 255");
    CHECK(!httpreq_parse_ip("1.2.3", 5, &v), "three parts");
    CHECK(!httpreq_parse_ip("1.2.3.4.5", 9, &v), "five parts");
    CHECK(!httpreq_parse_ip("1.2.3.", 6, &v), "a trailing dot");
    CHECK(!httpreq_parse_ip("01.2.3.4", 8, &v), "a leading zero");
    CHECK(!httpreq_parse_ip("1.2.3.4x", 8, &v), "trailing junk");
    CHECK(!httpreq_parse_ip("1.2.3.1234", 10, &v), "a four-digit octet");
    CHECK(!httpreq_parse_ip("", 0, &v), "empty");

    uint32_t a[HTTPREQ_MAX_ALLOW];
    CHECK(httpreq_parse_allow("", a, HTTPREQ_MAX_ALLOW) == 0, "empty list");
    CHECK(httpreq_parse_allow("172.29.0.32", a, HTTPREQ_MAX_ALLOW) == 1 && a[0] == ip("172.29.0.32"), "one");
    CHECK(httpreq_parse_allow(" 172.29.0.32, 172.29.0.15 ,", a, HTTPREQ_MAX_ALLOW) == 2 && a[1] == ip("172.29.0.15"),
          "two, with spaces and a trailing comma");
    CHECK(httpreq_parse_allow("172.29.0.32,leia", a, HTTPREQ_MAX_ALLOW) == -1, "a name, not an address");
    // There, but empty of addresses: broken, not absent (absent would open it to the whole subnet)
    CHECK(httpreq_parse_allow(",", a, HTTPREQ_MAX_ALLOW) == -1, "a lone comma");
    CHECK(httpreq_parse_allow(" ", a, HTTPREQ_MAX_ALLOW) == -1, "a space");
    CHECK(httpreq_parse_allow(",, ,", a, HTTPREQ_MAX_ALLOW) == -1, "separators only");
    CHECK(httpreq_parse_allow("172.29.0.32\t", a, HTTPREQ_MAX_ALLOW) == -1, "a tab isn't a separator");
    CHECK(httpreq_parse_allow("1.1.1.1,2.2.2.2,3.3.3.3,4.4.4.4,5.5.5.5", a, HTTPREQ_MAX_ALLOW) == -1, "too many");
}

static void test_peer(void)
{
    uint32_t self = ip("172.29.0.50"), mask = ip("255.255.255.0");
    CHECK(httpreq_peer_ok(ip("172.29.0.32"), self, mask, NULL, 0), "Leia, on the subnet");
    CHECK(httpreq_peer_ok(ip("172.29.0.15"), self, mask, NULL, 0), "a Mac on the subnet");
    CHECK(!httpreq_peer_ok(ip("172.29.1.32"), self, mask, NULL, 0), "the next subnet");
    CHECK(!httpreq_peer_ok(ip("8.8.8.8"), self, mask, NULL, 0), "the internet");
    CHECK(!httpreq_peer_ok(ip("172.29.0.32"), 0, mask, NULL, 0), "no address yet");
    CHECK(!httpreq_peer_ok(ip("172.29.0.32"), self, 0, NULL, 0), "no netmask");

    uint32_t leia = ip("172.29.0.32");
    CHECK(httpreq_peer_ok(leia, self, mask, &leia, 1), "Leia, on the list");
    CHECK(!httpreq_peer_ok(ip("172.29.0.15"), self, mask, &leia, 1), "a list narrows the subnet");
}

int main(void)
{
    test_route();
    test_head();
    test_ip();
    test_peer();
    return report("test_httpreq");
}
