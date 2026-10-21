// CHATS-16: see httpreq.h.
#include "httpreq.h"

#include <stdio.h>
#include <string.h>

// The end of the head: the first blank line ("\r\n\r\n", or a bare "\n\n" from a lenient client).
static bool head_complete(const char *b, size_t n)
{
    for (size_t i = 0; i + 1 < n; i++) {
        if (b[i] != '\n') continue;
        if (b[i + 1] == '\n') return true;
        if (i + 2 < n && b[i + 1] == '\r' && b[i + 2] == '\n') return true;
    }
    return false;
}

httpreq_route_t httpreq_route(const char *buf, size_t n)
{
    if (!head_complete(buf, n)) return n >= HTTPREQ_MAX_HEAD ? HTTPREQ_BAD : HTTPREQ_INCOMPLETE;
    // The request line: METHOD SP target SP HTTP/1.x, then CRLF (or LF)
    const char *end = memchr(buf, '\n', n);
    size_t len = (size_t)(end - buf);
    if (len && buf[len - 1] == '\r') len--;
    for (size_t i = 0; i < len; i++)
        if ((unsigned char)buf[i] < 0x20 || (unsigned char)buf[i] == 0x7f) return HTTPREQ_BAD;
    const char *sp1 = memchr(buf, ' ', len);
    if (!sp1 || sp1 == buf) return HTTPREQ_BAD;
    const char *target = sp1 + 1;
    const char *sp2 = memchr(target, ' ', len - (size_t)(target - buf));
    if (!sp2 || sp2 == target) return HTTPREQ_BAD;
    const char *version = sp2 + 1;
    size_t vlen = len - (size_t)(version - buf);
    if (vlen != 8 || memcmp(version, "HTTP/1.", 7) || (version[7] != '0' && version[7] != '1')) return HTTPREQ_BAD;
    size_t mlen = (size_t)(sp1 - buf), tlen = (size_t)(sp2 - target);
    if (mlen != 3 || memcmp(buf, "GET", 3)) return HTTPREQ_BAD_METHOD;
    if (target[0] != '/') return HTTPREQ_BAD;
    if ((tlen == 5 || (tlen > 5 && target[5] == '?')) && !memcmp(target, "/json", 5)) return HTTPREQ_JSON;
    return HTTPREQ_NOT_FOUND;
}

int httpreq_status(httpreq_route_t r)
{
    switch (r) {
    case HTTPREQ_JSON: return 200;
    case HTTPREQ_NOT_FOUND: return 404;
    case HTTPREQ_BAD_METHOD: return 405;
    case HTTPREQ_BAD: return 400;
    default: return 0;
    }
}

static const char *reason(int status)
{
    switch (status) {
    case 200: return "OK";
    case 400: return "Bad Request";
    case 404: return "Not Found";
    case 405: return "Method Not Allowed";
    case 503: return "Service Unavailable";
    default: return "Error";
    }
}

size_t httpreq_head(char *out, size_t cap, int status, const char *content_type, size_t content_length,
                    int retry_after_s)
{
    if (!cap) return 0;
    char retry[40] = "";
    if (retry_after_s > 0) snprintf(retry, sizeof retry, "Retry-After: %d\r\n", retry_after_s);
    int n = snprintf(out, cap,
                     "HTTP/1.1 %d %s\r\n"
                     "Content-Type: %s\r\n"
                     "Content-Length: %zu\r\n"
                     "Cache-Control: no-store\r\n"
                     "%s%s"
                     "Connection: close\r\n"
                     "\r\n",
                     status, reason(status), content_type, content_length, status == 405 ? "Allow: GET\r\n" : "", retry);
    if (n < 0 || (size_t)n >= cap) {
        out[0] = 0;
        return 0;
    }
    return (size_t)n;
}

bool httpreq_parse_ip(const char *s, size_t n, uint32_t *ip)
{
    uint32_t v = 0;
    size_t i = 0;
    for (int part = 0; part < 4; part++) {
        if (part) {
            if (i >= n || s[i] != '.') return false;
            i++;
        }
        size_t start = i;
        unsigned octet = 0;
        while (i < n && s[i] >= '0' && s[i] <= '9' && i - start < 3) octet = octet * 10 + (unsigned)(s[i++] - '0');
        if (i == start || octet > 255) return false;
        if (i - start > 1 && s[start] == '0') return false;       // no leading zeros (octal elsewhere)
        v = v << 8 | octet;
    }
    if (i != n) return false;
    *ip = v;
    return true;
}

int httpreq_parse_allow(const char *s, uint32_t *out, int max)
{
    int count = 0;
    size_t i = 0, n = strlen(s);
    while (i < n) {
        while (i < n && (s[i] == ',' || s[i] == ' ')) i++;
        if (i == n) break;
        size_t start = i;
        while (i < n && s[i] != ',' && s[i] != ' ') i++;
        if (count == max || !httpreq_parse_ip(s + start, i - start, &out[count])) return -1;
        count++;
    }
    return count;
}

bool httpreq_peer_ok(uint32_t peer, uint32_t self, uint32_t mask, const uint32_t *allow, int n_allow)
{
    if (!self) return false;
    if (n_allow > 0) {
        for (int i = 0; i < n_allow; i++)
            if (allow[i] == peer) return true;
        return false;
    }
    return mask && (peer & mask) == (self & mask);
}
