// Kata's worker: shared helpers (kw.h).
#include "kw.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

// ---- allocation ----------------------------------------------------------------------------
static kw_mem_t mem = { malloc, realloc, free };

void kw_set_mem(const kw_mem_t *m) { mem = *m; }
void *kw_alloc(size_t n) { return mem.alloc(n ? n : 1); }
void *kw_realloc(void *p, size_t n) { return mem.realloc(p, n ? n : 1); }
void kw_free(void *p) { if (p) mem.free(p); }

char *kw_strndup(const char *s, size_t n)
{
    char *d = kw_alloc(n + 1);
    if (!d) return NULL;
    memcpy(d, s, n);
    d[n] = 0;
    return d;
}

// ---- jbuf -------------------------------------------------------------------------------
void jb_init(jbuf_t *b) { memset(b, 0, sizeof *b); }

void jb_free(jbuf_t *b)
{
    kw_free(b->p);
    jb_init(b);
}

void jb_reset(jbuf_t *b)
{
    b->n = 0;
    b->oom = false;
    if (b->p) b->p[0] = 0;
}

bool jb_reserve(jbuf_t *b, size_t extra)
{
    if (b->oom) return false;
    if (b->n + extra + 1 <= b->cap) return true;
    size_t cap = b->cap ? b->cap : 256;
    while (cap < b->n + extra + 1) cap *= 2;
    char *p = kw_realloc(b->p, cap);
    if (!p) {
        b->oom = true;
        return false;
    }
    b->p = p;
    b->cap = cap;
    return true;
}

void jb_put(jbuf_t *b, const void *data, size_t n)
{
    if (!jb_reserve(b, n)) return;
    memcpy(b->p + b->n, data, n);
    b->n += n;
    b->p[b->n] = 0;
}

void jb_puts(jbuf_t *b, const char *s) { jb_put(b, s, strlen(s)); }
void jb_putc(jbuf_t *b, char c) { jb_put(b, &c, 1); }

void jb_printf(jbuf_t *b, const char *fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    int n = vsnprintf(NULL, 0, fmt, ap);
    va_end(ap);
    if (n < 0 || !jb_reserve(b, (size_t)n)) return;
    va_start(ap, fmt);
    vsnprintf(b->p + b->n, (size_t)n + 1, fmt, ap);
    va_end(ap);
    b->n += (size_t)n;
}

void jb_json_str(jbuf_t *b, const char *s, size_t n)
{
    // worst case every byte becomes \u00XX
    if (!jb_reserve(b, n + 2)) return;
    jb_putc(b, '"');
    size_t run = 0;
    for (size_t i = 0; i < n; i++) {
        unsigned char c = (unsigned char)s[i];
        if (c >= 0x20 && c != '"' && c != '\\') continue;
        jb_put(b, s + run, i - run);
        run = i + 1;
        switch (c) {
        case '"': jb_put(b, "\\\"", 2); break;
        case '\\': jb_put(b, "\\\\", 2); break;
        case '\n': jb_put(b, "\\n", 2); break;
        case '\r': jb_put(b, "\\r", 2); break;
        case '\t': jb_put(b, "\\t", 2); break;
        default: jb_printf(b, "\\u%04x", c); break;
        }
    }
    jb_put(b, s + run, n - run);
    jb_putc(b, '"');
}

char *jb_take(jbuf_t *b, size_t *len)
{
    if (b->oom || !b->p) {
        jb_free(b);
        if (len) *len = 0;
        return NULL;
    }
    char *p = b->p;
    if (len) *len = b->n;
    jb_init(b);
    return p;
}

// ---- base64 -----------------------------------------------------------------------------
static const char B64[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

size_t b64_encoded_len(size_t n) { return (n + 2) / 3 * 4; }

void b64_encode(const uint8_t *in, size_t n, char *out)
{
    size_t i = 0;
    for (; i + 3 <= n; i += 3) {
        uint32_t v = (uint32_t)in[i] << 16 | (uint32_t)in[i + 1] << 8 | in[i + 2];
        *out++ = B64[v >> 18]; *out++ = B64[(v >> 12) & 63]; *out++ = B64[(v >> 6) & 63]; *out++ = B64[v & 63];
    }
    if (n - i == 1) {
        uint32_t v = (uint32_t)in[i] << 16;
        *out++ = B64[v >> 18]; *out++ = B64[(v >> 12) & 63]; *out++ = '='; *out++ = '=';
    } else if (n - i == 2) {
        uint32_t v = (uint32_t)in[i] << 16 | (uint32_t)in[i + 1] << 8;
        *out++ = B64[v >> 18]; *out++ = B64[(v >> 12) & 63]; *out++ = B64[(v >> 6) & 63]; *out++ = '=';
    }
    *out = 0;
}

void jb_b64(jbuf_t *b, const uint8_t *in, size_t n)
{
    size_t m = b64_encoded_len(n);
    if (!jb_reserve(b, m)) return;
    b64_encode(in, n, b->p + b->n);
    b->n += m;
}

static int b64_val(char c)
{
    if (c >= 'A' && c <= 'Z') return c - 'A';
    if (c >= 'a' && c <= 'z') return c - 'a' + 26;
    if (c >= '0' && c <= '9') return c - '0' + 52;
    if (c == '+') return 62;
    if (c == '/') return 63;
    return -1;
}

long b64_decode(const char *in, size_t n, uint8_t *out, b64_mode_t mode)
{
    if (mode == B64_PADDED) {
        if (n % 4) return -1;
        size_t pad = 0;
        while (pad < 2 && n > pad && in[n - 1 - pad] == '=') pad++;
        n -= pad;                             // what's left is 0, 3 or 2 characters into a quad
    }
    if (n % 4 == 1) return -1;
    size_t o = 0;
    uint32_t acc = 0;
    int bits = 0;
    for (size_t i = 0; i < n; i++) {
        int v = b64_val(in[i]);
        if (v < 0) return -1;
        acc = acc << 6 | (uint32_t)v;
        bits += 6;
        if (bits >= 8) {
            bits -= 8;
            out[o++] = (uint8_t)(acc >> bits);
            acc &= (1u << bits) - 1;
        }
    }
    // The leftover bits must be zero: otherwise two encodings decode the same (age requires
    // canonical base64; for padded input it's the same rule).
    if (bits && acc) return -1;
    return (long)o;
}

// ---- time -------------------------------------------------------------------------------
// Howard Hinnant's days_from_civil / civil_from_days.
int64_t kw_days_from_civil(int y, unsigned m, unsigned d)
{
    y -= m <= 2;
    const int64_t era = (y >= 0 ? y : y - 399) / 400;
    const unsigned yoe = (unsigned)(y - era * 400);
    const unsigned doy = (153 * (m + (m > 2 ? -3 : 9)) + 2) / 5 + d - 1;
    const unsigned doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    return era * 146097 + (int64_t)doe - 719468;
}

static void civil_from_days(int64_t z, int *y, unsigned *m, unsigned *d)
{
    z += 719468;
    const int64_t era = (z >= 0 ? z : z - 146096) / 146097;
    const unsigned doe = (unsigned)(z - era * 146097);
    const unsigned yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    const unsigned doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    const unsigned mp = (5 * doy + 2) / 153;
    *d = doy - (153 * mp + 2) / 5 + 1;
    *m = mp < 10 ? mp + 3 : mp - 9;
    *y = (int)(yoe + era * 400) + (*m <= 2);
}

static int digits(const char *s, int n)
{
    int v = 0;
    for (int i = 0; i < n; i++) {
        if (s[i] < '0' || s[i] > '9') return -1;
        v = v * 10 + (s[i] - '0');
    }
    return v;
}

int64_t kw_parse_iso8601(const char *s)
{
    if (!s || strlen(s) < 20 || s[4] != '-' || s[7] != '-' || s[10] != 'T' || s[13] != ':' || s[16] != ':') return -1;
    int y = digits(s, 4), mo = digits(s + 5, 2), d = digits(s + 8, 2), h = digits(s + 11, 2), mi = digits(s + 14, 2),
        se = digits(s + 17, 2);
    if (y < 0 || mo < 1 || mo > 12 || d < 1 || d > 31 || h < 0 || h > 23 || mi < 0 || mi > 59 || se < 0 || se > 60) return -1;
    const char *p = s + 19;
    if (*p == '.') for (p++; *p >= '0' && *p <= '9'; p++) {}
    if (strcmp(p, "Z") && strcmp(p, "+00:00")) return -1;
    return kw_days_from_civil(y, (unsigned)mo, (unsigned)d) * 86400 + h * 3600 + mi * 60 + se;
}

void kw_amz_date(int64_t t, char out[17])
{
    int y;
    unsigned m, d;
    int64_t days = t >= 0 ? t / 86400 : (t - 86399) / 86400, s = t - days * 86400;
    civil_from_days(days, &y, &m, &d);
    snprintf(out, 17, "%04d%02u%02uT%02d%02d%02dZ", y, m, d, (int)(s / 3600), (int)(s / 60 % 60), (int)(s % 60));
}

void kw_ymd(int64_t t, char out[11])
{
    int y;
    unsigned m, d;
    civil_from_days(t >= 0 ? t / 86400 : (t - 86399) / 86400, &y, &m, &d);
    snprintf(out, 11, "%04d-%02u-%02u", y, m, d);
}
