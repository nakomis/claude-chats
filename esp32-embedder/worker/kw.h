// Kata's worker (CHATS-5): the shared bits of the portable modules in this directory. Portable C11,
// no ESP-IDF: the firmware builds these files as they are, and so do the host tests (../test).
//
//   - an allocator hook: the firmware points it at PSRAM (internal RAM is for the model and the
//     network, CHATS-4); the host leaves it on malloc
//   - jbuf: a growable byte buffer, with JSON string escaping
//   - base64 (standard alphabet), strict where age needs it to be
#pragma once

#include <stdarg.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

// ---- allocation ----------------------------------------------------------------------------
typedef struct {
    void *(*alloc)(size_t n);
    void *(*realloc)(void *p, size_t n);
    void (*free)(void *p);
} kw_mem_t;
// Defaults to malloc/realloc/free. Set once, before anything here allocates.
void kw_set_mem(const kw_mem_t *mem);
void *kw_alloc(size_t n);
void *kw_realloc(void *p, size_t n);
void kw_free(void *p);
char *kw_strndup(const char *s, size_t n);

// ---- jbuf -------------------------------------------------------------------------------
// Always NUL-terminated (p[n] == 0) once anything has been put. An allocation failure sets oom and
// makes every later put a no-op, so a builder checks once at the end.
typedef struct {
    char *p;
    size_t n, cap;
    bool oom;
} jbuf_t;

void jb_init(jbuf_t *b);
void jb_free(jbuf_t *b);
void jb_reset(jbuf_t *b);                          // empty, keeping the allocation
bool jb_reserve(jbuf_t *b, size_t extra);
void jb_put(jbuf_t *b, const void *data, size_t n);
void jb_puts(jbuf_t *b, const char *s);
void jb_putc(jbuf_t *b, char c);
void jb_printf(jbuf_t *b, const char *fmt, ...) __attribute__((format(printf, 2, 3)));
// A JSON string, quotes included: ", \ and control characters escaped; other bytes as they are
// (the caller's text is UTF-8).
void jb_json_str(jbuf_t *b, const char *s, size_t n);
// Takes the buffer (caller frees with kw_free); b is left empty.
char *jb_take(jbuf_t *b, size_t *len);

// ---- base64 -----------------------------------------------------------------------------
size_t b64_encoded_len(size_t n);                  // with padding, without the NUL
// Writes b64_encoded_len(n) characters and a NUL.
void b64_encode(const uint8_t *in, size_t n, char *out);
void jb_b64(jbuf_t *b, const uint8_t *in, size_t n);
typedef enum {
    B64_PADDED,        // RFC 4648 with '=' padding (the length a multiple of 4)
    B64_RAW_STRICT,    // age: no padding, and the unused bits of the last character must be zero
} b64_mode_t;
// Decodes n characters into out (room for n * 3 / 4 + 3); the decoded length, or -1 if the input
// isn't valid in that mode. No whitespace is accepted.
long b64_decode(const char *in, size_t n, uint8_t *out, b64_mode_t mode);

// ---- time -------------------------------------------------------------------------------
// Days since 1970-01-01 of a civil (proleptic Gregorian) date, and back.
int64_t kw_days_from_civil(int y, unsigned m, unsigned d);
// "2026-10-13T18:00:00Z" (fractions and +00:00 accepted) -> Unix seconds; -1 if malformed.
int64_t kw_parse_iso8601(const char *s);
// Unix seconds -> "YYYYMMDDTHHMMSSZ" (SigV4's x-amz-date; 17 bytes with the NUL) and "YYYY-MM-DD".
void kw_amz_date(int64_t t, char out[17]);
void kw_ymd(int64_t t, char out[11]);
